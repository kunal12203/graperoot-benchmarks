#!/usr/bin/env python3
"""
Retro-rejudge all completed steps with full clean diff (no truncation, no node_modules).
Updates the JSONL in-place for each mode.
"""
import json, os, re, subprocess, sys, time
from pathlib import Path

RESULTS   = Path(__file__).parent / "results"
BUILD     = Path(__file__).parent / "build-projects"
PROMPTS   = Path(__file__).parent / "prompts_50steps.json"
EMPTY_MCP = Path(__file__).parent / "results" / "dgc_mcp_config_live.json"

MODES = {
    "graperoot":      BUILD / "collabnotes-graperoot",
    "jcodemunch":     BUILD / "collabnotes-jcodemunch",
    "codereviewgraph": BUILD / "collabnotes-codereviewgraph",
}

SUFFIX = sys.argv[1] if len(sys.argv) > 1 else "50steps"

# Load prompts indexed by id
prompts = {p["id"]: p for p in json.loads(PROMPTS.read_text())}

def get_clean_diff(project_dir: Path, commit_hash: str) -> dict:
    """Get full clean diff for a commit, excluding node_modules/.dual-graph/locks."""
    # stat
    r = subprocess.run(
        ["git", "diff", f"{commit_hash}~1", commit_hash, "--stat"],
        capture_output=True, text=True, timeout=60, cwd=str(project_dir),
    )
    last = (r.stdout.strip().split("\n") or [""])[-1]
    files = int(re.search(r"(\d+) file", last).group(1)) if re.search(r"(\d+) file", last) else 0
    ins   = int(re.search(r"(\d+) insertion", last).group(1)) if re.search(r"(\d+) insertion", last) else 0
    dels  = int(re.search(r"(\d+) deletion", last).group(1)) if re.search(r"(\d+) deletion", last) else 0

    # full clean patch
    p = subprocess.run(
        ["git", "diff", f"{commit_hash}~1", commit_hash, "--unified=2",
         "--", ":(exclude)node_modules", ":(exclude).dual-graph",
         ":(exclude)package-lock.json", ":(exclude)*.lock"],
        capture_output=True, text=True, timeout=60, cwd=str(project_dir),
    )
    patch = p.stdout if p.stdout else ""
    return {"files_changed": files, "insertions": ins, "deletions": dels,
            "net_lines": ins - dels, "patch": patch}

def get_commit_for_step(project_dir: Path, step_id: int) -> str | None:
    r = subprocess.run(
        ["git", "log", "--oneline", "--all"],
        capture_output=True, text=True, cwd=str(project_dir),
    )
    for line in r.stdout.splitlines():
        # matches "abc1234 Step 61: feature_audit_log"
        if re.search(rf"\bStep {step_id}\b", line, re.IGNORECASE):
            return line.split()[0]
    return None

def llm_judge(prompt_text: str, response_text: str, diff: dict) -> dict:
    if not response_text or len(response_text) < 50:
        return {"score": 0, "reason": "empty response"}

    diff_summary = f"+{diff['insertions']}/-{diff['deletions']} lines across {diff['files_changed']} files"
    patch_section = f"\n\nACTUAL CODE DIFF:\n{diff['patch']}" if diff.get('patch') else ""
    judge_prompt = f"""You are a code review judge. Score this AI coding response from 0-100.

TASK:
{prompt_text[:800]}

AI RESPONSE (truncated to 5000 chars):
{response_text[:5000]}

CODE CHANGES MADE: {diff_summary}{patch_section}

Score criteria:
- 80-100: Task fully completed, correct implementation, good code quality
- 60-79: Mostly complete, minor issues or missing edge cases
- 40-59: Partial completion, core logic present but incomplete
- 20-39: Attempted but significant issues or wrong approach
- 0-19: Failed, no meaningful changes or completely wrong

Reply with ONLY this JSON (no markdown):
{{"score": <0-100>, "reason": "<one sentence max>"}}"""

    env = os.environ.copy()
    for key in ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "ANTHROPIC_HMR_ENABLED"]:
        env.pop(key, None)

    def _once():
        r = subprocess.run(
            ["claude", "--dangerously-skip-permissions", "--output-format", "json",
             "--model", "claude-haiku-4-5-20251001", "--strict-mcp-config",
             "--mcp-config", str(EMPTY_MCP), "-p", judge_prompt],
            capture_output=True, text=True, timeout=120, env=env, cwd="/tmp",
        )
        raw = r.stdout.strip()
        if not raw: return None, "no output"
        data = json.loads(raw)
        if data.get("is_error") or not data.get("result", "").strip():
            return None, str(data.get("error", ""))[:80]
        text = data["result"].strip()
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:]).rsplit("```", 1)[0].strip()
        if not text: return None, "empty after fence"
        parsed = json.loads(text)
        return {"score": int(parsed["score"]), "reason": parsed.get("reason", "")}, None

    try:
        result, err = _once()
        if result is None:
            time.sleep(30)
            result, err = _once()
        return result or {"score": 0, "reason": f"judge failed: {err}"}
    except Exception as e:
        return {"score": 0, "reason": f"error: {str(e)[:80]}"}


def rejudge_mode(mode: str):
    project_dir = MODES[mode]
    jsonl_path  = RESULTS / f"raw_{mode}_{SUFFIX}.jsonl"

    if not jsonl_path.exists():
        print(f"[{mode}] no JSONL found, skipping")
        return

    records = [json.loads(l) for l in jsonl_path.read_text().splitlines() if l.strip()]
    print(f"\n[{mode}] {len(records)} steps to rejudge")

    updated = []
    for rec in records:
        step_id = rec["id"]
        prompt_obj = prompts.get(step_id)
        if not prompt_obj:
            print(f"  step {step_id}: no prompt found, keeping old")
            updated.append(rec)
            continue

        commit = get_commit_for_step(project_dir, step_id)
        if not commit:
            print(f"  step {step_id}: no commit found, keeping old")
            updated.append(rec)
            continue

        diff = get_clean_diff(project_dir, commit)
        old_score = rec.get("quality", {}).get("llm_judge", {}).get("score", "?")
        old_patch  = len(rec.get("diff", {}).get("patch", ""))

        print(f"  step {step_id} ({rec['step'][:30]}): old_score={old_score} old_patch={old_patch}chars → new_patch={len(diff['patch'])}chars", flush=True)

        response_text = rec["result"].get("response_text", "")
        new_judge = llm_judge(prompt_obj["prompt"], response_text, diff)

        print(f"    → new_score={new_judge['score']}  reason: {new_judge['reason'][:120]}")

        # Update record
        rec["diff"] = diff
        if "quality" not in rec: rec["quality"] = {}
        rec["quality"]["llm_judge"] = new_judge
        updated.append(rec)

    # Write back
    with open(jsonl_path, "w") as f:
        for rec in updated:
            f.write(json.dumps(rec) + "\n")

    scores = [r["quality"]["llm_judge"]["score"] for r in updated if r.get("quality",{}).get("llm_judge",{}).get("score") is not None]
    print(f"[{mode}] done. new avg: {sum(scores)/len(scores):.1f} (was {sum(r.get('quality',{}).get('llm_judge',{}).get('score',0) for r in records)/len(records):.1f})")


if __name__ == "__main__":
    target_modes = sys.argv[2:] if len(sys.argv) > 2 else list(MODES.keys())
    print(f"Rejudging modes: {target_modes} (suffix={SUFFIX})")
    for mode in target_modes:
        rejudge_mode(mode)
    print("\nAll done.")
