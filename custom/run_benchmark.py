#!/usr/bin/env python3
"""
GrapeRoot A/B Benchmark — compare GrapeRoot vs Normal on any codebase.

Usage:
    python3 custom/run_benchmark.py --project /path/to/repo --prompts prompts/typescript_monorepo_30.json
    python3 custom/run_benchmark.py --project . --prompts my_prompts.json --modes graperoot normal

Requirements:
    - GrapeRoot installed: curl -sSL https://raw.githubusercontent.com/kunal12203/Codex-CLI-Compact/main/install.sh | bash
    - ANTHROPIC_API_KEY or AWS Bedrock credentials set
    - pip install litellm
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from threading import Lock

try:
    import litellm
except ImportError:
    print("ERROR: litellm not installed. Run: pip install litellm")
    sys.exit(1)

# ── Config ─────────────────────────────────────────────────────────────────────
MODEL = os.environ.get("BENCH_MODEL", "claude-sonnet-4-6-20250514")
JUDGE_MODEL = os.environ.get("BENCH_JUDGE_MODEL", "claude-haiku-4-5-20251001")
GR_HOME = Path.home() / ".dual-graph"
GRAPH_BUILDER = GR_HOME / "graph_builder.py"
TOOL_RUNNER = Path(__file__).resolve().parent.parent / "swebench" / "graph_tool_runner.py"

PRICING = {
    "claude-sonnet-4-6-20250514": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00, "cache_write": 1.00, "cache_read": 0.08},
}

_print_lock = Lock()


def log(msg: str):
    with _print_lock:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {msg}")


# ── Graph Setup ────────────────────────────────────────────────────────────────

def check_graperoot_installed() -> bool:
    """Check if GrapeRoot is installed."""
    if GRAPH_BUILDER.exists():
        return True
    if shutil.which("graperoot"):
        return True
    return False


def build_graph(project_root: Path) -> Path:
    """Build the AST graph for a project. Returns data_dir."""
    data_dir = project_root / ".dual-graph"
    graph_file = data_dir / "info_graph.json"

    if graph_file.exists():
        age_hrs = (time.time() - graph_file.stat().st_mtime) / 3600
        if age_hrs < 24:
            log(f"Graph exists ({age_hrs:.1f}h old), reusing")
            return data_dir

    log(f"Building graph for {project_root.name}...")

    # Try graperoot CLI first
    gr_bin = shutil.which("graperoot")
    if gr_bin:
        subprocess.run([gr_bin, str(project_root)], capture_output=True, timeout=300)
        if graph_file.exists():
            return data_dir

    # Fall back to graph_builder.py directly
    if GRAPH_BUILDER.exists():
        python = str(GR_HOME / "venv" / "bin" / "python3")
        if not Path(python).exists():
            python = "python3"
        subprocess.run(
            [python, str(GRAPH_BUILDER), str(project_root)],
            capture_output=True, timeout=300
        )

    if not graph_file.exists():
        log("WARNING: Graph build failed. GrapeRoot mode will fall back to grep.")
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "info_graph.json").write_text('{"nodes":[],"edges":[]}')

    return data_dir


# ── Tool Execution ─────────────────────────────────────────────────────────────

def run_graph_tool(data_dir: Path, project_root: Path, tool: str, args: dict) -> dict:
    """Run a GrapeRoot graph tool via the tool runner."""
    python = str(GR_HOME / "venv" / "bin" / "python3")
    if not Path(python).exists():
        python = "python3"

    try:
        r = subprocess.run(
            [python, str(TOOL_RUNNER), str(data_dir), str(project_root), tool, json.dumps(args)],
            capture_output=True, text=True, timeout=30
        )
        return json.loads(r.stdout) if r.stdout.strip() else {"ok": False, "error": "empty output"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Agent Loops ────────────────────────────────────────────────────────────────

SYSTEM_GRAPEROOT = """\
You are an expert software engineer. The project is at {project_root}.

## Workflow
1. FIRST: call graph_continue(query) before any file exploration.
2. Read recommended_files with graph_read.
3. If confidence=medium/low, use fallback_rg for targeted searches (max 3).
4. Make your changes with bash (sed, python3 -c, cat >).
5. Verify with: git diff

## Rules
- graph_continue FIRST every turn.
- confidence=high: read recommended_files only, then act.
- NEVER explore more than 10 files total.
"""

SYSTEM_NORMAL = """\
You are an expert software engineer. The project is at {project_root}.

You have standard tools: bash (grep, find, cat, sed). Explore the codebase and complete the task.
"""

GR_TOOLS = [
    {"type": "function", "function": {"name": "graph_continue", "description": "Query the project graph for relevant files.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "graph_read", "description": "Read a file or file::symbol from the project.", "parameters": {"type": "object", "properties": {"file": {"type": "string"}}, "required": ["file"]}}},
    {"type": "function", "function": {"name": "fallback_rg", "description": "Ripgrep search in the project.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string", "default": "."}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "bash", "description": "Run a bash command in the project root.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
]

NORMAL_TOOLS = [
    {"type": "function", "function": {"name": "bash", "description": "Run a bash command in the project root.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
]


def execute_tool(tool_name: str, args: dict, project_root: Path, data_dir: Path) -> str:
    """Execute a tool call and return the result as string."""
    if tool_name == "bash":
        try:
            r = subprocess.run(
                ["bash", "-c", args.get("command", "echo 'no command'")],
                capture_output=True, text=True, timeout=60, cwd=str(project_root)
            )
            output = (r.stdout + r.stderr)[:8000]
            return output if output.strip() else "(no output)"
        except subprocess.TimeoutExpired:
            return "(command timed out after 60s)"
    elif tool_name in ("graph_continue", "graph_read", "fallback_rg", "graph_impact"):
        result = run_graph_tool(data_dir, project_root, tool_name, args)
        return json.dumps(result, indent=2)[:6000]
    else:
        return f"Unknown tool: {tool_name}"


def run_agent(prompt: str, mode: str, project_root: Path, data_dir: Path, max_steps: int = 40) -> dict:
    """Run one agent session. Returns response text, cost, turns, tokens."""
    system = (SYSTEM_GRAPEROOT if mode == "graperoot" else SYSTEM_NORMAL).format(project_root=project_root)
    tools = GR_TOOLS if mode == "graperoot" else NORMAL_TOOLS

    messages = [{"role": "user", "content": prompt}]
    total_input = 0
    total_output = 0
    turns = 0
    full_response = ""

    for step in range(max_steps):
        turns += 1
        try:
            resp = litellm.completion(
                model=MODEL,
                messages=[{"role": "system", "content": system}] + messages,
                tools=tools,
                temperature=0.0,
                max_tokens=4096,
            )
        except Exception as e:
            log(f"  LLM error at step {step}: {e}")
            break

        usage = resp.usage
        total_input += usage.prompt_tokens
        total_output += usage.completion_tokens

        choice = resp.choices[0]
        msg = choice.message

        # Accumulate text content
        if msg.content:
            full_response += msg.content + "\n"

        # If no tool calls, we're done
        if not msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content or ""})
            break

        # Process tool calls
        messages.append(msg.model_dump())
        for tc in msg.tool_calls:
            fn_name = tc.function.name
            fn_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            result = execute_tool(fn_name, fn_args, project_root, data_dir)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    # Calculate cost
    pricing = PRICING.get(MODEL, {"input": 3.0, "output": 15.0})
    cost = (total_input * pricing["input"] + total_output * pricing["output"]) / 1_000_000

    return {
        "response": full_response.strip(),
        "cost_usd": round(cost, 4),
        "turns": turns,
        "tokens_in": total_input,
        "tokens_out": total_output,
    }


# ── LLM Judge ──────────────────────────────────────────────────────────────────

def judge_response(prompt_text: str, response_text: str, diff: str) -> dict:
    """Score a response 0-100 using LLM judge."""
    judge_prompt = f"""You are a code review judge. Score this AI coding response from 0-100.

TASK:
{prompt_text[:1000]}

AI RESPONSE (first 5000 chars):
{response_text[:5000]}

CODE CHANGES (git diff):
{diff[:10000] if diff else "(no changes)"}

Score criteria:
- 80-100: Task fully completed, correct implementation, good code quality
- 60-79: Mostly complete, minor issues or missing edge cases
- 40-59: Partial completion, core logic present but incomplete
- 20-39: Attempted but significant issues or wrong approach
- 0-19: Failed, no meaningful changes or completely wrong

Respond with ONLY a JSON object: {{"score": <int>, "reason": "<one sentence>"}}"""

    try:
        resp = litellm.completion(
            model=JUDGE_MODEL,
            messages=[{"role": "user", "content": judge_prompt}],
            temperature=0.0,
            max_tokens=200,
        )
        text = resp.choices[0].message.content.strip()
        # Extract JSON from response
        if "{" in text:
            json_str = text[text.index("{"):text.rindex("}") + 1]
            return json.loads(json_str)
    except Exception as e:
        log(f"  Judge error: {e}")

    return {"score": 0, "reason": "judge failed"}


# ── Git Diff ───────────────────────────────────────────────────────────────────

def get_diff(project_root: Path) -> str:
    """Get the current git diff (staged + unstaged, source files only)."""
    try:
        r = subprocess.run(
            ["git", "diff", "HEAD", "--",
             ":(exclude)node_modules", ":(exclude)package-lock.json",
             ":(exclude)*.lock", ":(exclude).dual-graph"],
            capture_output=True, text=True, timeout=30, cwd=str(project_root)
        )
        return r.stdout[:50000]
    except Exception:
        return ""


def reset_project(project_root: Path):
    """Reset project to clean state between runs."""
    subprocess.run(["git", "checkout", "."], capture_output=True, cwd=str(project_root))
    subprocess.run(["git", "clean", "-fd", "--exclude=.dual-graph"], capture_output=True, cwd=str(project_root))


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="GrapeRoot A/B Benchmark")
    parser.add_argument("--project", required=True, help="Path to the project to benchmark")
    parser.add_argument("--prompts", required=True, help="Path to prompts JSON file")
    parser.add_argument("--modes", nargs="+", default=["graperoot", "normal"], help="Modes to run (default: graperoot normal)")
    parser.add_argument("--output", default="./results", help="Output directory")
    parser.add_argument("--slice", default=None, help="Run subset of prompts, e.g. 0:10")
    args = parser.parse_args()

    project_root = Path(args.project).resolve()
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not project_root.exists():
        print(f"ERROR: Project not found: {project_root}")
        sys.exit(1)

    # Load prompts
    prompts = json.loads(Path(args.prompts).read_text())
    if args.slice:
        start, end = map(int, args.slice.split(":"))
        prompts = prompts[start:end]

    log(f"Benchmark: {len(prompts)} prompts × {len(args.modes)} modes = {len(prompts) * len(args.modes)} runs")
    log(f"Project: {project_root}")
    log(f"Model: {MODEL}")

    # Check GrapeRoot installation
    if "graperoot" in args.modes:
        if not check_graperoot_installed():
            print("\nERROR: GrapeRoot not installed.")
            print("Install with: curl -sSL https://raw.githubusercontent.com/kunal12203/Codex-CLI-Compact/main/install.sh | bash")
            sys.exit(1)

    # Build graph if needed
    data_dir = None
    if "graperoot" in args.modes:
        data_dir = build_graph(project_root)
        log(f"Graph ready at {data_dir}")

    # Run benchmarks
    results_file = output_dir / f"results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    results = []

    for i, prompt_data in enumerate(prompts):
        pid = prompt_data.get("id", i + 1)
        title = prompt_data.get("title", f"Prompt {pid}")
        prompt_text = prompt_data.get("prompt", prompt_data.get("text", ""))

        for mode in args.modes:
            log(f"[P{pid:02d}] {title} — {mode}")

            # Reset project
            reset_project(project_root)

            # Run agent
            t0 = time.time()
            agent_result = run_agent(prompt_text, mode, project_root, data_dir or project_root / ".dual-graph")
            wall_time = time.time() - t0

            # Get diff
            diff = get_diff(project_root)

            # Judge
            judge_result = judge_response(prompt_text, agent_result["response"], diff)

            # Record
            record = {
                "prompt_id": pid,
                "title": title,
                "category": prompt_data.get("category", ""),
                "mode": mode,
                "quality_score": judge_result.get("score", 0),
                "judge_reason": judge_result.get("reason", ""),
                "cost_usd": agent_result["cost_usd"],
                "turns": agent_result["turns"],
                "tokens_in": agent_result["tokens_in"],
                "tokens_out": agent_result["tokens_out"],
                "wall_time_s": round(wall_time, 1),
                "response": agent_result["response"][:3000],
                "git_diff": diff[:5000],
            }
            results.append(record)

            # Write incrementally
            with open(results_file, "a") as f:
                f.write(json.dumps(record) + "\n")

            score = judge_result.get("score", 0)
            cost = agent_result["cost_usd"]
            log(f"  → Q={score}  ${cost:.2f}  {agent_result['turns']} turns  {wall_time:.0f}s")

    # Summary
    print(f"\n{'='*60}")
    print(f"RESULTS: {results_file}")
    print(f"{'='*60}\n")

    for mode in args.modes:
        mode_results = [r for r in results if r["mode"] == mode]
        if not mode_results:
            continue
        avg_q = sum(r["quality_score"] for r in mode_results) / len(mode_results)
        total_cost = sum(r["cost_usd"] for r in mode_results)
        avg_turns = sum(r["turns"] for r in mode_results) / len(mode_results)
        print(f"  {mode:12s}  Q={avg_q:.1f}  ${total_cost:.2f} total  {avg_turns:.0f} avg turns")

    print()


if __name__ == "__main__":
    main()
