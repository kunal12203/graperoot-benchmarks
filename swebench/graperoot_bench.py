#!/usr/bin/env python3
"""
Sonnet 4.6 + GrapeRoot Pro on SWE-bench Pro.
Checkpoints after every task. Re-run to resume.

Architecture:
  - Docker container: repo at /app (SWE-bench Pro images use /app, not /testbed)
  - After container starts: docker cp /app -> host temp dir for graph_builder
  - graph_builder runs on host copy -> info_graph.json
  - graph tools called via graph_tool_runner.py subprocess on host
  - bash tool -> docker exec into container (working in /app)
"""

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

import litellm
from datasets import load_dataset

# ── Config ─────────────────────────────────────────────────────────────────
MODEL = "bedrock/us.anthropic.claude-sonnet-4-6"
GR_DIR = Path.home() / "graperoot-pro"
GRAPH_BUILDER = GR_DIR / "graph_builder.py"
TOOL_RUNNER = GR_DIR / "graph_tool_runner.py"
HOST_TMP = Path("/tmp/bench-runs")
OUTPUT_LOCK = threading.Lock()

SYSTEM_PROMPT = """\
You are an expert software engineer fixing a GitHub issue. The repo is at /app.

## Workflow — complete in ≤15 steps

1. FIRST call: graph_continue(query) — before ANY bash/cat/grep.
   It returns recommended_files with the exact files to read.
2. Read those files with graph_read (or graph_read("file::FunctionName") for a specific function).
3. Once you understand the bug, make the edit directly with bash:
   - Use python3 -c to write the fixed content, or
   - Use sed/awk to make targeted replacements, or
   - Write the entire file with cat > /app/src/file.js << 'EOF' ... EOF
4. Verify with: cd /app && git diff
5. Submit with: echo "PATCH_COMPLETE" && cd /app && git diff

## Hard rules
- graph_continue FIRST every turn — no exceptions.
- confidence=high: read recommended_files only, then fix immediately.
- confidence=medium/low: at most 2 fallback_rg + 2 graph_read, then fix.
- NEVER explore more than 10 files total.
- Make the edit by step 8. Submit by step 12.
- Do NOT commit. Do NOT modify test files.
- When done: run `echo "PATCH_COMPLETE" && cd /app && git diff`
"""

INSTANCE_TEMPLATE = """\
Fix this GitHub issue in the repository at /app:

{problem_statement}
{interface_hint}{requirements_hint}

When you have made the fix, run: echo "PATCH_COMPLETE" && cd /app && git diff
"""

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Execute a bash command in /app (the repo root).",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}

GRAPH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "graph_continue",
            "description": (
                "CALL THIS FIRST every turn before bash/grep/cat. "
                "Returns recommended_files and confidence level for your current query. "
                "confidence=high means read only those files and fix — no more exploration."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "What you need to find or understand"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "graph_read",
            "description": (
                "Read a file or a specific symbol. Use 'path/to/file.js::functionName' to read "
                "just that function (much cheaper). Paths relative to /app."
            ),
            "parameters": {
                "type": "object",
                "properties": {"file": {"type": "string"}},
                "required": ["file"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fallback_rg",
            "description": (
                "Regex search across the codebase. "
                "Only use after graph_continue at medium/low confidence. Max 2 calls per task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "Subdirectory to search (default: whole repo)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "graph_impact",
            "description": "Find all files that import or depend on a given file.",
            "parameters": {
                "type": "object",
                "properties": {"file": {"type": "string"}},
                "required": ["file"],
            },
        },
    },
]

ALL_TOOLS = [BASH_TOOL] + GRAPH_TOOLS


# ── Docker helpers ──────────────────────────────────────────────────────────

def start_container(image: str, name: str) -> str:
    """Start container, return container id."""
    cmd = [
        "docker", "run", "-d",
        "--name", name,
        "-w", "/app",
        "--entrypoint", "/bin/sh",
        "--rm",
        image,
        "-c", "sleep 3h",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"docker run failed: {r.stderr.strip()}")
    return r.stdout.strip()


def stop_container(name: str):
    subprocess.run(["docker", "stop", "-t", "5", name], capture_output=True, timeout=30)


def docker_exec(container: str, command: str, workdir: str = "/app", timeout: int = 120) -> dict:
    cmd = ["docker", "exec", "-w", workdir, container, "bash", "-c", command]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        output = r.stdout + r.stderr
        if len(output) > 10000:
            output = output[:5000] + "\n...[truncated]...\n" + output[-5000:]
        return {"returncode": r.returncode, "output": output}
    except subprocess.TimeoutExpired:
        return {"returncode": 124, "output": f"Command timed out after {timeout}s"}


def copy_repo_to_host(container: str, host_dir: Path):
    """Copy /app from container to host for graph_builder."""
    host_dir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["docker", "cp", f"{container}:/app/.", str(host_dir)],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f"docker cp failed: {r.stderr.strip()}")


# ── Graph tools (on host copy of repo) ────────────────────────────────────

def call_graph_tool(data_dir: Path, repo_dir: Path, tool: str, args: dict) -> str:
    try:
        r = subprocess.run(
            [sys.executable, str(TOOL_RUNNER),
             str(data_dir), str(repo_dir), tool, json.dumps(args)],
            capture_output=True, text=True, timeout=30,
        )
        output = r.stdout.strip()
        if not output:
            return json.dumps({"ok": False, "error": r.stderr[:300] or "no output"})
        try:
            result = json.loads(output)
            if tool == "graph_continue":
                files = result.get("recommended_files", [])
                conf = result.get("confidence", "medium")
                return (
                    f"confidence={conf}\n"
                    f"recommended_files={json.dumps(files)}\n"
                    f"max_supplementary_greps={result.get('max_supplementary_greps', 2)}\n"
                    f"max_supplementary_files={result.get('max_supplementary_files', 2)}\n"
                    f"{result.get('hint', '')}"
                )
            elif tool == "graph_read":
                content = result.get("content", "")
                return f"# {result.get('file', '')} {result.get('lines', '')}\n{content}"
            elif tool == "fallback_rg":
                hits = result.get("hits", [])
                return "\n".join(hits[:60]) if hits else "No matches found."
            elif tool == "graph_impact":
                deps = result.get("dependents", [])
                return ("Files importing " + result.get("file", "") + ":\n" + "\n".join(deps)) if deps else "No dependents."
            return output
        except Exception:
            return output[:3000]
    except subprocess.TimeoutExpired:
        return '{"ok":false,"error":"tool timeout"}'
    except Exception as e:
        return f'{{"ok":false,"error":"{e}"}}'


def build_graph(data_dir: Path, repo_dir: Path) -> bool:
    out_path = data_dir / "info_graph.json"
    r = subprocess.run(
        [sys.executable, str(GRAPH_BUILDER),
         "--root", str(repo_dir), "--out", str(out_path)],
        capture_output=True, text=True, timeout=300,
    )
    for line in (r.stdout + r.stderr).strip().split("\n")[-3:]:
        if line.strip():
            print(f"    [graph] {line}")
    return out_path.exists()


def extract_patch(text: str) -> str:
    """Extract git diff patch from text output."""
    lines = text.split("\n")
    patch_lines = []
    collecting = False
    for line in lines:
        if line.startswith("diff --git"):
            collecting = True
        if collecting:
            patch_lines.append(line)
    patch = "\n".join(patch_lines).strip()
    if patch and not patch.endswith("\n"):
        patch += "\n"
    return patch


# ── Agent loop ─────────────────────────────────────────────────────────────

def run_agent(instance: dict, container: str, data_dir: Path, repo_dir: Path,
              max_steps: int = 80, cost_limit: float = 0) -> str:
    iface = (instance.get("interface") or "")[:400]
    reqs = (instance.get("requirements") or "")[:400]
    task = INSTANCE_TEMPLATE.format(
        problem_statement=instance["problem_statement"][:3000],
        interface_hint=f"\nInterface: {iface}\n" if iface else "",
        requirements_hint=f"\nRequirements: {reqs}\n" if reqs else "",
    )
    messages = [{"role": "user", "content": task}]
    total_cost = 0.0
    last_patch = ""

    for step in range(max_steps):
        if cost_limit > 0 and total_cost >= cost_limit:
            print(f"    [agent] cost limit ${cost_limit:.2f} reached at step {step}")
            break

        resp = None
        for attempt in range(5):
            try:
                resp = litellm.completion(
                    model=MODEL,
                    messages=messages,
                    tools=ALL_TOOLS,
                    system=SYSTEM_PROMPT,
                    temperature=0.0,
                    max_tokens=4096,
                    drop_params=True,
                )
                break
            except litellm.exceptions.RateLimitError:
                wait = min(60, 4 * (2 ** attempt))
                print(f"    [rate limit] waiting {wait}s")
                time.sleep(wait)
            except Exception as e:
                print(f"    [LLM error] {e}")
                time.sleep(5)
                if attempt == 4:
                    return last_patch

        if resp is None:
            return last_patch

        try:
            total_cost += litellm.cost_calculator.completion_cost(resp, model=MODEL)
        except Exception:
            pass

        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_unset=True))
        tool_calls = msg.tool_calls or []

        if not tool_calls:
            content = msg.content or ""
            # Check if agent included patch in text
            if "PATCH_COMPLETE" in content or "diff --git" in content:
                patch = extract_patch(content)
                if patch:
                    return patch
            if step >= max_steps - 2:
                # Final attempt: try to get the diff
                r = docker_exec(container, "cd /app && git diff")
                patch = extract_patch(r["output"])
                if patch:
                    print(f"    [agent] collected diff at final step")
                    return patch
                break
            messages.append({"role": "user", "content": "Continue. Use a tool call."})
            continue

        tool_results = []
        tool_names = []
        for tc in tool_calls:
            name = tc.function.name
            tool_names.append(name)
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}

            if name == "bash":
                r = docker_exec(container, args.get("command", ""))
                out = f"<returncode>{r['returncode']}</returncode>\n<output>\n{r['output']}\n</output>"
                # Check for patch submission signal
                if "PATCH_COMPLETE" in r["output"] or "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in r["output"]:
                    patch = extract_patch(r["output"])
                    if patch:
                        print(f"    step {step+1:02d}  ${total_cost:.3f}  PATCHED via {tool_names}")
                        return patch
                    # Maybe diff is empty here — run git diff explicitly
                    diff_r = docker_exec(container, "cd /app && git diff")
                    patch = extract_patch(diff_r["output"])
                    if patch:
                        print(f"    step {step+1:02d}  ${total_cost:.3f}  PATCHED via git diff")
                        return patch
                # Track any diff output we see along the way
                p = extract_patch(r["output"])
                if p:
                    last_patch = p
            elif name in ("graph_continue", "graph_read", "fallback_rg", "graph_impact"):
                out = call_graph_tool(data_dir, repo_dir, name, args)
            else:
                out = f"Unknown tool: {name}"

            tool_results.append({"role": "tool", "tool_call_id": tc.id, "content": out})

        messages.extend(tool_results)
        print(f"    step {step+1:02d}  ${total_cost:.3f}  {tool_names}")

    # Final fallback: collect any uncommitted changes
    if not last_patch:
        r = docker_exec(container, "cd /app && git diff")
        last_patch = extract_patch(r["output"])
        if last_patch:
            print(f"    [agent] final git diff fallback: patch collected")

    return last_patch


# ── Per-instance ────────────────────────────────────────────────────────────

def run_instance(instance: dict, cost_limit: float = 0) -> dict:
    iid = instance["instance_id"]
    tag = instance.get("dockerhub_tag", "")
    image = f"jefzda/sweap-images:{tag}"
    run_id = uuid.uuid4().hex[:8]
    cname = f"bench-{run_id}"
    repo_dir = HOST_TMP / f"{run_id}-repo"
    data_dir = HOST_TMP / f"{run_id}-data"

    print(f"\n{'='*60}")
    print(f"INSTANCE: {iid[:55]}")
    t0 = time.time()

    try:
        # 1. Start container (repo at /app inside image)
        start_container(image, cname)

        # 2. Run before_repo_set_cmd to set up correct git state
        before_cmd = instance.get("before_repo_set_cmd", "")
        if before_cmd:
            r = docker_exec(cname, before_cmd, timeout=120)
            if r["returncode"] != 0:
                print(f"    [warn] before_repo_set_cmd exit {r['returncode']}: {r['output'][:120]}")

        # 3. Copy repo from container to host for graph_builder
        data_dir.mkdir(parents=True, exist_ok=True)
        copy_repo_to_host(cname, repo_dir)

        # 4. Build graph on host copy
        graph_ok = build_graph(data_dir, repo_dir)
        if not graph_ok:
            print(f"    [warn] graph build failed")

        # 5. Run agent
        patch = run_agent(instance, cname, data_dir, repo_dir, cost_limit=cost_limit)

        elapsed = time.time() - t0
        status = "PATCHED" if patch.strip() else "NO_PATCH"
        print(f"  [{status}] elapsed={elapsed:.0f}s")
        return {"instance_id": iid, "model_patch": patch}

    except Exception as e:
        traceback.print_exc()
        return {"instance_id": iid, "model_patch": "", "error": str(e)}
    finally:
        stop_container(cname)
        for d in [repo_dir, data_dir]:
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass


# ── Checkpoint helpers ─────────────────────────────────────────────────────

def load_preds(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def save_pred(preds_path: Path, iid: str, patch: str):
    with OUTPUT_LOCK:
        preds = load_preds(preds_path)
        preds[iid] = {"model_name_or_path": MODEL, "model_patch": patch}
        preds_path.write_text(json.dumps(preds, indent=2))


def print_scoreboard(preds_path: Path, total: int):
    preds = load_preds(preds_path)
    done = len(preds)
    patched = sum(1 for v in preds.values() if v.get("model_patch", "").strip())
    pct = patched / done * 100 if done else 0.0
    filled = int(patched / max(total, 1) * 30)
    bar = "█" * filled + "░" * (30 - filled)
    print(f"\n  ┌─ CHECKPOINT {'Sonnet 4.6 + GrapeRoot Pro':^32} ─┐")
    print(f"  │  Tasks    : {done}/{total} done")
    print(f"  │  Patched  : {patched}  ({pct:.1f}% patch rate)")
    print(f"  │  [{bar}]")
    print(f"  │  Target   : beat Opus 4.8 on SWE-bench Pro")
    print(f"  └{'─'*50}┘\n")


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice", default="0:50")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--output", default="./results")
    ap.add_argument("--cost-limit", type=float, default=0)
    args = ap.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    preds_path = out_dir / "preds.json"
    HOST_TMP.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f"  Sonnet 4.6 + GrapeRoot Pro — SWE-bench Pro")
    print(f"  Model   : {MODEL}")
    print(f"  Slice   : {args.slice}  Workers: {args.workers}")
    print(f"  Cost    : ${args.cost_limit:.1f}/instance")
    print(f"  Output  : {out_dir}")
    print("=" * 60)

    ds = load_dataset("ScaleAI/SWE-bench_Pro", split="test")
    s, e = (int(x) for x in args.slice.split(":"))
    instances = list(ds)[s:e]

    existing = load_preds(preds_path)
    todo = [i for i in instances if i["instance_id"] not in existing]
    print(f"\nInstances : {len(instances)} total | {len(existing)} done | {len(todo)} todo\n")

    if not todo:
        print("All done!")
        print_scoreboard(preds_path, len(instances))
        return

    cost_limit = args.cost_limit

    def worker(inst):
        result = run_instance(inst, cost_limit=cost_limit)
        save_pred(preds_path, result["instance_id"], result.get("model_patch", ""))
        print_scoreboard(preds_path, len(instances))

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(worker, todo))

    preds = load_preds(preds_path)
    patched = sum(1 for v in preds.values() if v.get("model_patch", "").strip())
    total = len(preds)
    print("=" * 60)
    print(f"FINAL: {patched}/{total} patches = {patched/total*100:.1f}% patch rate")
    print(f"Preds: {preds_path}")


if __name__ == "__main__":
    main()
