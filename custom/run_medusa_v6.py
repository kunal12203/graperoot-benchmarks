#!/usr/bin/env python3
"""
Medusa Benchmark — Continued Chat Mode
3-way: CRG vs Normal vs GrapeRoot v6.1

All modes run as persistent sessions — the agent accumulates codebase
knowledge across all 30 prompts instead of starting cold each time.

Modes:
  crg_cont    — code-review-graph (CRG) MCP, persistent session
  v61_cont    — GrapeRoot Pro v6.1,          persistent session
  normal_cont — No MCP tools,                persistent session

Usage:
    python3 benchmark/run_medusa_v61_continued.py                       # all 3 modes
    python3 benchmark/run_medusa_v61_continued.py --modes crg_cont      # one mode
    python3 benchmark/run_medusa_v61_continued.py --prompts 1-10
    python3 benchmark/run_medusa_v61_continued.py --resume              # skip done

Setup (one-time):
    cd benchmark/build-projects/medusa-gr-final
    git worktree add --detach ../medusa-crg-cont   HEAD
    git worktree add --detach ../medusa-v61-cont   HEAD
    git worktree add --detach ../medusa-norm-cont  HEAD   # may already exist
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from threading import Lock

# ── Paths ──────────────────────────────────────────────────────────────────────
_BENCH_DIR   = Path(__file__).resolve().parent
_RESULTS_DIR = _BENCH_DIR / "results"
_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

PROMPTS_FILE  = _BENCH_DIR / "prompts_70_typescript_monorepo.json"
RAW_FILE      = _RESULTS_DIR / "raw_medusa_v6_100.jsonl"
REPORT_FILE   = _RESULTS_DIR / "benchmark_medusa_v6_100.md"
RESPONSES_DIR = _RESULTS_DIR / "responses_medusa_v6_100"
RESPONSES_DIR.mkdir(parents=True, exist_ok=True)

PROJECT_CRG    = _BENCH_DIR / "build-projects" / "medusa-crg-cont"
PROJECT_V61    = _BENCH_DIR / "build-projects" / "medusa-v61-cont"
PROJECT_NORMAL = _BENCH_DIR / "build-projects" / "medusa-norm-cont"

_CRG_BIN    = Path(shutil.which("code-review-graph") or "/opt/homebrew/bin/code-review-graph")
_GR61_SERVER = Path("/tmp/mcp_gr_v6.py")

VERSION     = "medusa-v6-100"
TIMEOUT_S   = None
COOLDOWN_S  = 10
MODEL       = "claude-sonnet-4-6"
JUDGE_MODEL = "claude-sonnet-4-6"

ALL_MODES = ["crg_cont", "v61_cont", "normal_cont"]

PRICING = {
    "claude-sonnet-4-6": {
        "input": 3.00, "output": 15.00,
        "cache_write": 3.75, "cache_read": 0.30,
    }
}

_print_lock = Lock()


def pprint(*args, **kwargs):
    with _print_lock:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}]", *args, **kwargs)


# ── CLAUDE.md policies ────────────────────────────────────────────────────────

_CRG_CLAUDE_MD = """\
<!-- code-review-graph-continued-v1 -->
# code-review-graph (CRG) — Continued Session Policy

> Enterprise TypeScript monorepo (~1571 source files, 4 packages: medusa, admin, cli, plugins).
> This is a CONTINUED SESSION — you have prior context about this codebase.
> Build on what you already know. Don't re-index or re-explore unnecessarily.

## Tools available (v1.8.4)

| Tool | Key params | Purpose |
|------|-----------|---------|
| `build_or_update_graph_tool()` | `full_rebuild=False` | Build/update AST graph (call ONCE on first prompt only) |
| `list_graph_stats_tool()` | — | Nodes, edges count |
| `semantic_search_nodes_tool(query)` | `query, kind?, limit=20` | Search by name/keyword |
| `query_graph_tool(pattern, target)` | `pattern, target` | Trace callers/callees/imports |
| `get_impact_radius_tool()` | `changed_files?, max_depth=2` | Blast radius |
| `get_review_context_tool()` | `changed_files?` | Token-efficient review context |
| `find_large_functions_tool()` | `min_lines=50, file_path_pattern?` | Large functions |
| `embed_graph_tool()` | — | Compute embeddings for semantic search |
| `get_docs_section_tool(section_name)` | `section_name` | Plugin docs |

## query_graph patterns
`callers_of`, `callees_of`, `imports_of`, `importers_of`, `children_of`, `tests_for`, `inheritors_of`, `file_summary`

## Rules
- On FIRST prompt: call `build_or_update_graph_tool(full_rebuild=False)` to index. Then `list_graph_stats_tool()` to confirm nodes > 0.
- On SUBSEQUENT prompts: skip build — use search/query tools directly. The graph persists on disk.
- For exhaustive tasks: explicitly check medusa, admin, cli, plugins packages.
- Use multiple `semantic_search_nodes_tool` queries with different terms for exhaustive tasks.
- Output all findings in your response with file:line references and concrete fixes.
"""

_V61_CLAUDE_MD = """\
<!-- graperoot-pro-v6.1-continued -->
# GrapeRoot Pro v6.1 — Continued Session Policy

> Enterprise TypeScript monorepo (~1571 source files, 4 packages: medusa, admin, cli, plugins).
> This is a CONTINUED SESSION — you have prior context about this codebase.

## MANDATORY: Always follow this order

### Step 1 — graph_continue (non-negotiable)
Call `graph_continue` FIRST before any file read, grep, or bash command.

### Step 2 — Read recommended_files
Call `graph_read` once per file. Pass `file::symbol` entries verbatim.

### Step 3 — v6.1 fields
- `task_type` — targeted / exhaustive / behavioral
- `package_hints` — top-level directories
- `cost_guidance` / `turn_guidance` — soft guidance for exhaustive tasks

### Exhaustive enumeration tools
- `graph_dead_exports(file_prefix?)` — pre-computed dead exports per package
- `graph_find_cycles(file_prefix?)` — circular imports via Tarjan SCC
- `graph_grep_all(pattern, file_glob?)` — exhaustive grep, no cap

## Multi-package rule
For ANY exhaustive task: cover medusa, admin, cli, plugins — not just the first hit.

## Findings format
1. Cite exact file path + line number
2. Show vulnerable/buggy code snippet
3. Show concrete fix

## Confidence caps (HARD — after reading recommended_files)
Always read ALL `recommended_files` from `graph_continue` first. Then:
- `confidence=high`   → STOP. Do NOT call any more tools beyond recommended_files.
- `confidence=medium` → After recommended_files: `fallback_rg` at most `max_supplementary_greps` times + `graph_read` at most `max_supplementary_files` additional files. Then STOP.
- `confidence=low`    → Same caps as medium. Then STOP.

## Rules
- `graph_continue` MUST be the first tool called.
- In continued session: skip `graph_scan` if project is already indexed.
- Output all findings in your response. Do NOT call `graph_add_memory`.
- After the caps above are reached, write findings immediately — no more tool calls.
"""

_NORMAL_CLAUDE_MD = """\
<!-- normal-continued-v1 -->
# Continued Session — No MCP Tools

> Enterprise TypeScript monorepo (~1571 source files, 4 packages: medusa, admin, cli, plugins).
> This is a CONTINUED SESSION — build on your prior codebase knowledge.

## Instructions
- You do NOT have MCP tools. Use bash: grep, find, cat, head.
- On FIRST prompt: explore the codebase structure to build your mental model.
- On SUBSEQUENT prompts: use what you already know — skip re-exploration.
- For exhaustive tasks: check all packages: packages/medusa, packages/admin, packages/cli, packages/plugins.
- Output findings with exact file:line references + code snippets + concrete fixes.
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def compute_cost(inp, out, cache_write=0, cache_read=0, model=MODEL):
    p = PRICING.get(model, PRICING[MODEL])
    return (
        inp           * p["input"]       / 1_000_000
        + out         * p["output"]      / 1_000_000
        + cache_write * p["cache_write"] / 1_000_000
        + cache_read  * p["cache_read"]  / 1_000_000
    )


def _venv_path():
    return _BENCH_DIR.parent / "bin" / "venv"


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


_FILTER_PATS = [
    re.compile(r"^\.dual-graph/"), re.compile(r"^\.code-review-graph/"),
    re.compile(r"^dist/"), re.compile(r"__pycache__"),
    re.compile(r"\.jsonl$"), re.compile(r"^node_modules/"), re.compile(r"^CLAUDE\.md$"),
]


def filter_src(files):
    return [f for f in files if not any(p.search(f) for p in _FILTER_PATS)]


def git_diff_stats(root):
    try:
        subprocess.run(["git", "add", "-A"], cwd=str(root), capture_output=True)
        stat = subprocess.run(["git", "diff", "--cached", "--shortstat"],
                              cwd=str(root), capture_output=True, text=True).stdout.strip()
        diff = subprocess.run(["git", "diff", "--cached", "--name-only"],
                              cwd=str(root), capture_output=True, text=True).stdout.strip()
        changed = [f for f in diff.splitlines() if f]
        m_ins = re.search(r"(\d+) insertion", stat)
        m_del = re.search(r"(\d+) deletion",  stat)
        m_fil = re.search(r"(\d+) file",       stat)
        return {
            "files_changed": int(m_fil.group(1)) if m_fil else 0,
            "insertions":    int(m_ins.group(1)) if m_ins else 0,
            "deletions":     int(m_del.group(1)) if m_del else 0,
            "changed_files": changed,
        }
    except Exception:
        return {"files_changed": 0, "insertions": 0, "deletions": 0, "changed_files": []}


def light_reset(root, claude_md_content):
    """Unstage changes but keep graph indexes and session continuity."""
    subprocess.run(["git", "checkout", "."], cwd=str(root), capture_output=True)
    subprocess.run(["git", "clean", "-fd",
                    "--exclude=.dual-graph",
                    "--exclude=.code-review-graph",
                    "--exclude=CLAUDE.md"],
                   cwd=str(root), capture_output=True)
    # Always re-write CLAUDE.md to prevent drift across prompts
    (root / "CLAUDE.md").write_text(claude_md_content, encoding="utf-8")


def append_jsonl(path, record):
    with _print_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── LLM Judge ─────────────────────────────────────────────────────────────────

LLM_JUDGE_PROMPT = """\
You are a senior staff engineer judging an AI assistant's response to a code audit task on the Medusa e-commerce monorepo (~1571 TypeScript source files, 4 packages: medusa, admin, cli, plugins).

## The Task Asked
{prompt}

## Task Type
{task_type} — {task_type_desc}

## The Response to Judge
{response}

## Changes Made
Files changed: {files_changed} | Insertions: {insertions} | Deletions: {deletions}
Source files modified: {changed_files}

## Scoring Rubric (100 points total)

### 1. findings_accuracy (0-25)
Specific, accurate file paths + code snippets = 18-25. Vague/hallucinated = 0-8.
PENALIZE (-10) if only 1-2 files mentioned.

### 2. coverage_breadth (0-25)
EXHAUSTIVE: all 4 packages checked? One package = max 12/25.
TARGETED: right file found quickly?

### 3. depth_quality (0-20)
WHY is it a problem? Specific code context? Line numbers? Concrete fixes?

### 4. fix_completeness (0-20)
All major instances enumerated? Surface-level = 0-8.

### 5. actionability (0-10)
Developer can act immediately? Clear priority?

## Critical rules:
- 2-3 instances with vague refs: coverage_breadth ≤ 10
- One package for exhaustive: coverage_breadth ≤ 12
- Plausible paths but no snippets: findings_accuracy ≤ 15

## Output Format
Reply with ONLY a JSON object (no markdown):
{{"findings_accuracy": <int>, "coverage_breadth": <int>, "depth_quality": <int>, "fix_completeness": <int>, "actionability": <int>, "total": <int>, "explanation": "<2-3 sentences>"}}
"""


def score_quality_llm(response_text, prompt_text, task_type, diff_stats):
    if not response_text or len(response_text) < 80:
        return {"total": 0, "explanation": "Response too short."}
    truncated = response_text[:7000]
    if len(response_text) > 7000:
        truncated += f"\n\n[... truncated — {len(response_text)} chars total ...]"
    src_files = filter_src(diff_stats.get("changed_files", []))
    task_type_desc = (
        "TARGETED: find the specific relevant file/pattern quickly"
        if task_type == "targeted"
        else "EXHAUSTIVE: enumerate ALL instances across ALL packages"
    )
    judge_prompt = LLM_JUDGE_PROMPT.format(
        prompt=prompt_text, task_type=task_type, task_type_desc=task_type_desc,
        response=truncated,
        files_changed=diff_stats.get("files_changed", 0),
        insertions=diff_stats.get("insertions", 0),
        deletions=diff_stats.get("deletions", 0),
        changed_files=", ".join(src_files[:20]) or "none",
    )
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_SESSION", None)
    try:
        proc = subprocess.run(
            ["claude", "-p", judge_prompt, "--model", JUDGE_MODEL,
             "--output-format", "text", "--dangerously-skip-permissions",
             "--no-session-persistence"],
            capture_output=True, text=True, timeout=300, env=env)
        raw = proc.stdout.strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as e:
        pprint(f"  [judge] Error: {e}")
    return None


# ── JSON parser ───────────────────────────────────────────────────────────────

def _parse_claude_json(proc) -> tuple[dict, str | None]:
    """Returns (result_dict, session_id). session_id is in the outer envelope."""
    raw = proc.stdout.strip()
    session_id: str | None = None
    try:
        outer = json.loads(raw)
        if isinstance(outer, dict):
            session_id = outer.get("session_id") or outer.get("sessionId")
            result = outer.get("result", outer)
            if isinstance(result, str):
                try: result = json.loads(result)
                except json.JSONDecodeError: pass
            if isinstance(result, dict):
                for key in ("response", "content", "text", "message"):
                    if key in result:
                        result["response_text"] = result[key]
                        break
                if "response_text" not in result:
                    result["response_text"] = outer.get("result", raw[:2000])
                for field in ("total_cost_usd", "usage", "modelUsage"):
                    if field not in result and field in outer:
                        result[field] = outer[field]
                return result, session_id
    except (json.JSONDecodeError, TypeError):
        pass
    lines = [l for l in raw.splitlines() if l.strip()]
    for l in reversed(lines):
        try:
            d = json.loads(l)
            if isinstance(d, dict) and ("result" in d or "total_cost_usd" in d):
                if not session_id:
                    session_id = d.get("session_id") or d.get("sessionId")
                d.setdefault("response_text", raw)
                return d, session_id
        except json.JSONDecodeError:
            pass
    return {"response_text": raw, "raw_output": raw[:3000]}, session_id


# ── Session runner ────────────────────────────────────────────────────────────

def run_with_session(prompt: str, project_root: Path, mcp_cfg: Path | None,
                     session_id: str | None) -> tuple[dict, str | None]:
    """Run a prompt, optionally resuming an existing session.
    Returns (result_dict, new_session_id).
    """
    settings_local = project_root / ".claude" / "settings.local.json"
    if settings_local.exists():
        settings_local.unlink()

    cmd = ["claude", "-p", prompt, "--model", MODEL, "--output-format", "json",
           "--dangerously-skip-permissions"]
    # No --no-session-persistence: we WANT sessions saved for --resume
    if session_id:
        cmd += ["--resume", session_id]
    if mcp_cfg:
        cmd += ["--mcp-config", str(mcp_cfg), "--strict-mcp-config"]

    env_c = os.environ.copy()
    env_c.pop("CLAUDECODE", None)
    env_c.pop("CLAUDE_CODE_SESSION", None)

    t0 = time.time()
    new_session_id: str | None = None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=TIMEOUT_S, cwd=str(project_root), env=env_c)
        wall = time.time() - t0
        result, new_session_id = _parse_claude_json(proc)
        result["wall_time_s"] = round(wall, 2)
        agent_cost = (result.get("total_cost_usd") or result.get("cost_usd") or
                      result.get("computed_cost_usd") or 0)
        usage = result.get("usage") or {}
        result["total_cost_usd"] = agent_cost or compute_cost(
            usage.get("input_tokens", result.get("input_tokens", 0)),
            usage.get("output_tokens", result.get("output_tokens", 0)),
            usage.get("cache_creation_input_tokens", 0),
            usage.get("cache_read_input_tokens", 0),
        )
    except Exception as e:
        result = {"error": str(e)[:300], "wall_time_s": time.time() - t0,
                  "response_text": "", "total_cost_usd": 0}
    return result, new_session_id


# ── Mode runners ──────────────────────────────────────────────────────────────

def run_crg_cont(prompt, project_root, session_id):
    """CRG continued: stdio server per turn, but --resume carries the session.
    The CRG graph DB (.code-review-graph/graph.db) persists on disk — no re-index.
    """
    crg_bin = str(_CRG_BIN)
    # Stable path — same config every prompt so prompt cache is warm
    mcp_cfg = project_root / ".code-review-graph" / "_bench_mcp.json"
    mcp_cfg.parent.mkdir(parents=True, exist_ok=True)
    mcp_cfg.write_text(
        json.dumps({"mcpServers": {"code-review-graph": {
            "command": crg_bin, "args": ["serve"]
        }}}), encoding="utf-8")
    return run_with_session(prompt, project_root, mcp_cfg, session_id)


def run_v61_cont(prompt, project_root, session_id, srv_port):
    """v6.1 continued: server stays alive the entire run.
    The MCP config URL is stable across prompts for cache warmth.
    """
    data_dir = project_root / ".dual-graph"
    data_dir.mkdir(parents=True, exist_ok=True)
    mcp_cfg = data_dir / "_cont_v61_mcp.json"
    mcp_cfg.write_text(
        json.dumps({"mcpServers": {"dual-graph": {
            "type": "http", "url": f"http://127.0.0.1:{srv_port}/mcp"
        }}}), encoding="utf-8")
    return run_with_session(prompt, project_root, mcp_cfg, session_id)


def run_normal_cont(prompt, project_root, session_id):
    """Normal continued: no MCP, session accumulates codebase knowledge."""
    return run_with_session(prompt, project_root, None, session_id)


# ── v6.1 server lifecycle ─────────────────────────────────────────────────────

def start_v61_server(project_root):
    """Start GR v6.1 server, return (proc, port)."""
    venv   = _venv_path()
    py_bin = venv / "bin" / "python3"
    py_str = str(py_bin) if py_bin.exists() else "python3"
    port   = _find_free_port()
    data_dir = project_root / ".dual-graph"
    data_dir.mkdir(parents=True, exist_ok=True)

    env_srv = os.environ.copy()
    env_srv["PORT"] = str(port)
    env_srv["DG_DATA_DIR"] = str(data_dir)
    env_srv["DUAL_GRAPH_PROJECT_ROOT"] = str(project_root)
    env_srv["GRAPEROOT_AST"] = "1"

    log = Path(f"/tmp/gr_v61_cont_{port}.log")
    srv = subprocess.Popen(
        [py_str, str(_GR61_SERVER)], env=env_srv,
        stdout=subprocess.DEVNULL, stderr=open(log, "w"))

    for _ in range(40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.5)

    pprint(f"  v6.1 server started on port {port}")
    return srv, port


def stop_v61_server(srv, port):
    if srv:
        srv.terminate()
        try: srv.wait(timeout=5)
        except subprocess.TimeoutExpired: srv.kill()
    Path(f"/tmp/gr_v61_cont_{port}.log").unlink(missing_ok=True)


# ── Pre-build indexes ─────────────────────────────────────────────────────────

def prebuild_crg_index(project_root):
    """Pre-build CRG graph so P01 doesn't pay index cost."""
    crg_bin = str(_CRG_BIN)
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    try:
        result = subprocess.run([crg_bin, "build"], cwd=str(project_root), env=env,
                                capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            pprint(f"  CRG index built OK ({project_root.name})")
        else:
            pprint(f"  CRG build warning: {result.stderr[:100]}")
    except Exception as e:
        pprint(f"  CRG build error: {e}")


def prebuild_v61_index(project_root):
    """Pre-scan with v6.1 builder so P01 doesn't pay graph_scan cost."""
    venv   = _venv_path()
    py_bin = venv / "bin" / "python3"
    py_str = str(py_bin) if py_bin.exists() else "python3"
    data_dir = project_root / ".dual-graph"
    data_dir.mkdir(parents=True, exist_ok=True)
    GR_PRO = Path.home() / "Documents" / "Personal Projects" / "GrapeRoot Pro"
    pprint(f"  Pre-building v6.1 index for {project_root.name}…")
    try:
        result = subprocess.run(
            [py_str, str(GR_PRO / "graph_builder_v6.py"),
             "--root", str(project_root),
             "--out", str(data_dir / "info_graph.json")],
            capture_output=True, text=True, timeout=180)
        pprint(f"    {(result.stdout + result.stderr).strip()[-100:]}")
    except Exception as e:
        pprint(f"    v6.1 pre-index warning: {e}")


# ── Load completed IDs ────────────────────────────────────────────────────────

def load_completed_ids():
    if not RAW_FILE.exists():
        return set()
    ids = set()
    for line in RAW_FILE.read_text().splitlines():
        try:
            ids.add(json.loads(line)["id"])
        except (json.JSONDecodeError, KeyError):
            pass
    return ids


# ── Report ────────────────────────────────────────────────────────────────────

def generate_report(all_records):
    by_mode: dict[str, list] = {}
    for r in all_records:
        by_mode.setdefault(r["mode"], []).append(r)

    def avg(lst): return sum(lst) / len(lst) if lst else 0

    def stats(mode):
        recs = by_mode.get(mode, [])
        qs = [r["quality_llm"]["total"] for r in recs if r.get("quality_llm")]
        cs = [r["agent"].get("total_cost_usd", 0) or 0 for r in recs]
        aq, ac = avg(qs), avg(cs)
        return aq, ac, aq / ac if ac > 0 else 0, len(recs)

    crg_q, crg_c, crg_v, crg_n = stats("crg_cont")
    v61_q, v61_c, v61_v, v61_n = stats("v61_cont")
    nm_q,  nm_c,  nm_v,  nm_n  = stats("normal_cont")

    lines = [
        "# Medusa — Continued Chat Benchmark: CRG vs v6.1 vs Normal",
        "",
        f"**Codebase:** Medusa (~1571 TypeScript files, 4 packages)",
        f"**Model:** {MODEL}  |  **Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**Mode:** Persistent sessions — continued from 30-prompt run, prompts P31–P100",
        "",
        "## Summary",
        "",
        "| Metric | CRG (continued) | v6.1 (continued) | Normal (continued) |",
        "|--------|----------------|------------------|--------------------|",
        f"| Avg Quality | {crg_q:.1f} | {v61_q:.1f} | {nm_q:.1f} |",
        f"| Avg Cost | ${crg_c:.3f} | ${v61_c:.3f} | ${nm_c:.3f} |",
        f"| Quality/$ | {crg_v:.1f} | {v61_v:.1f} | {nm_v:.1f} |",
        f"| Prompts | {crg_n} | {v61_n} | {nm_n} |",
        "",
        "---",
        "",
        "## Per-prompt results",
        "",
        "| P# | Title | Type | CRG Q | CRG $ | v6.1 Q | v6.1 $ | Norm Q | Norm $ |",
        "|-----|-------|------|-------|-------|--------|--------|--------|--------|",
    ]

    prompts_data = json.loads(PROMPTS_FILE.read_text()) if PROMPTS_FILE.exists() else []
    pid_title = {p["id"]: p["title"] for p in prompts_data}

    all_pids = sorted({r["prompt_id"] for r in all_records})
    for pid in all_pids:
        def get(mode):
            for r in by_mode.get(mode, []):
                if r["prompt_id"] == pid:
                    q = r["quality_llm"]["total"] if r.get("quality_llm") else "-"
                    c = r["agent"].get("total_cost_usd", 0) or 0
                    return q, c
            return "-", 0

        crg_q2, crg_c2 = get("crg_cont")
        v61_q2, v61_c2 = get("v61_cont")
        nm_q2,  nm_c2  = get("normal_cont")
        title = pid_title.get(pid, f"P{pid}")[:32]
        tt = next((r["task_type"] for r in all_records if r["prompt_id"] == pid), "?")
        lines.append(
            f"| P{pid:02d} | {title} | {tt} "
            f"| {crg_q2} | ${crg_c2:.3f} "
            f"| {v61_q2} | ${v61_c2:.3f} "
            f"| {nm_q2} | ${nm_c2:.3f} |"
        )

    # ── Category breakdown ──────────────────────────────────────────────────
    prompts_data2 = json.loads(PROMPTS_FILE.read_text()) if PROMPTS_FILE.exists() else []
    cat_map = {p["id"]: p.get("category", "other") for p in prompts_data2}
    cats: dict[str, dict[str, list]] = {}
    for r in all_records:
        cat = cat_map.get(r["prompt_id"], "other")
        mode = r["mode"]
        q = r["quality_llm"]["total"] if r.get("quality_llm") else None
        if q is not None:
            cats.setdefault(cat, {}).setdefault(mode, []).append(q)

    lines += ["", "---", "", "## Category breakdown", "",
              "| Category | CRG avg Q | v6 avg Q | Normal avg Q | Winner |",
              "|----------|-----------|----------|--------------|--------|"]
    for cat in sorted(cats):
        cq = avg(cats[cat].get("crg_cont", []))
        vq = avg(cats[cat].get("v61_cont", []))
        nq = avg(cats[cat].get("normal_cont", []))
        best = max({"CRG": cq, "v6": vq, "Normal": nq}.items(), key=lambda x: x[1])[0]
        lines.append(f"| {cat} | {cq:.1f} | {vq:.1f} | {nq:.1f} | **{best}** |")

    # ── What was missed (low Q prompts) ─────────────────────────────────────
    lines += ["", "---", "", "## Notable findings", ""]
    crashes = []
    wins = []
    for pid in sorted({r["prompt_id"] for r in all_records}):
        title = pid_title.get(pid, f"P{pid}")
        for mode, label in [("crg_cont","CRG"),("v61_cont","v6"),("normal_cont","Normal")]:
            for r in by_mode.get(mode,[]):
                if r["prompt_id"] == pid and r.get("quality_llm"):
                    q = r["quality_llm"]["total"]
                    turns = r["agent"].get("num_turns", 0)
                    citations = bool(r["agent"].get("response_text","") and ".ts:" in r["agent"].get("response_text",""))
                    if q <= 20:
                        crashes.append(f"- **{label} P{pid:02d}** ({title}): Q={q} turns={turns} {'⚠ no citations' if not citations else ''}")
                    elif q >= 90:
                        wins.append(f"- **{label} P{pid:02d}** ({title}): Q={q}")

    lines.append("### Crashes (Q≤20)")
    lines += crashes if crashes else ["- None"]
    lines += ["", "### Top scores (Q≥90)"]
    lines += wins if wins else ["- None"]

    # ── Tool health summary ──────────────────────────────────────────────────
    lines += ["", "---", "", "## Tool health (v6)", "",
              "| Metric | Value |", "|--------|-------|"]
    v6_recs = by_mode.get("v61_cont", [])
    all_turns = [r["agent"].get("num_turns",0) for r in v6_recs if r.get("agent")]
    low_turns = sum(1 for t in all_turns if t < 5)
    lines.append(f"| Avg turns/prompt | {avg(all_turns):.1f} |")
    lines.append(f"| Prompts with <5 turns | {low_turns} |")
    lines.append(f"| Prompts with citations | {sum(1 for r in v6_recs if r.get('agent') and '.ts:' in r['agent'].get('response_text',''))} / {len(v6_recs)} |")

    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")
    pprint(f"Report written → {REPORT_FILE}")
    pprint(f"  CRG:    Q={crg_q:.1f}  ${crg_c:.3f}/prompt")
    pprint(f"  v6.1:   Q={v61_q:.1f}  ${v61_c:.3f}/prompt")
    pprint(f"  Normal: Q={nm_q:.1f}  ${nm_c:.3f}/prompt")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--modes",   default="all",
                        help="Comma-separated: crg_cont,v61_cont,normal_cont  or 'all'")
    parser.add_argument("--prompts", default="31-100", help="Range e.g. 31-100")
    parser.add_argument("--resume",  action="store_true")
    parser.add_argument("--no-judge", action="store_true")
    args = parser.parse_args()

    modes = ALL_MODES if args.modes == "all" else [m.strip() for m in args.modes.split(",")]

    # Validate worktrees
    worktree_map = {
        "crg_cont":    PROJECT_CRG,
        "v61_cont":    PROJECT_V61,
        "normal_cont": PROJECT_NORMAL,
    }
    for mode in modes:
        p = worktree_map[mode]
        if not p.exists():
            print(f"ERROR: worktree not found: {p}")
            print(f"Run: cd benchmark/build-projects/medusa-gr-final && git worktree add --detach ../{p.name} HEAD")
            sys.exit(1)

    prompts = json.loads(PROMPTS_FILE.read_text())
    if "-" in args.prompts:
        lo, hi = args.prompts.split("-", 1)
        prompts = [p for p in prompts if int(lo) <= p["id"] <= int(hi)]
    else:
        n = int(args.prompts)
        prompts = [p for p in prompts if p["id"] == n]

    completed_ids = load_completed_ids() if args.resume else set()
    skip_judge = args.no_judge

    # Kill orphan servers
    subprocess.run(["pkill", "-f", "mcp_gr_v61"], capture_output=True)
    time.sleep(1)

    # Per-mode session state — seeded from P30 of the 30-prompt continued run
    _SEED_SESSIONS: dict[str, str] = {
        "crg_cont":    "7d918b21-7fc1-496a-9bcc-a547bcaeec41",
        "v61_cont":    "eff6e1f2-f5b6-48b7-b811-e14bee33adf1",
        "normal_cont": "bad6c39a-10b6-4534-9bfa-b07d11b91c1e",
    }
    session_ids: dict[str, str | None] = {
        m: _SEED_SESSIONS.get(m) for m in modes
    }

    # v6.1 server (stays alive the whole run)
    v61_srv, v61_port = None, None
    if "v61_cont" in modes:
        pprint("Pre-building v6.1 index…")
        prebuild_v61_index(PROJECT_V61)
        v61_srv, v61_port = start_v61_server(PROJECT_V61)

    # CRG pre-build
    if "crg_cont" in modes:
        pprint("Pre-building CRG index…")
        prebuild_crg_index(PROJECT_CRG)

    # Write initial CLAUDE.md for all worktrees
    claude_md_map = {
        "crg_cont":    _CRG_CLAUDE_MD,
        "v61_cont":    _V61_CLAUDE_MD,
        "normal_cont": _NORMAL_CLAUDE_MD,
    }
    for mode in modes:
        root = worktree_map[mode]
        (root / "CLAUDE.md").write_text(claude_md_map[mode], encoding="utf-8")

    pprint(f"{'='*70}")
    pprint(f"  CONTINUED 3-WAY BENCHMARK: {', '.join(modes)}")
    pprint(f"  {len(prompts)} prompts  |  Model: {MODEL}")
    pprint(f"{'='*70}")

    all_records: list[dict] = []

    try:
        for p in prompts:
            pid       = p["id"]
            title     = p["title"]
            task_type = p.get("task_type", "exhaustive")
            prompt    = p["prompt"]

            pprint(f"\n=== P{pid:02d}: {title} ({task_type}) ===")

            for mode in modes:
                run_id = f"{pid}_{mode}"
                if run_id in completed_ids:
                    pprint(f"[{mode}] SKIP (already done)")
                    continue

                root = worktree_map[mode]
                label = mode.replace("_cont", "")

                # Run
                if mode == "crg_cont":
                    agent_result, new_sid = run_crg_cont(prompt, root, session_ids[mode])
                elif mode == "v61_cont":
                    agent_result, new_sid = run_v61_cont(prompt, root, session_ids[mode], v61_port)
                else:
                    agent_result, new_sid = run_normal_cont(prompt, root, session_ids[mode])

                # Save new session_id (fall back to old if new is None)
                if new_sid:
                    session_ids[mode] = new_sid

                diff_stats = git_diff_stats(root)
                src_files  = filter_src(diff_stats.get("changed_files", []))

                quality_llm = None
                if not skip_judge and agent_result.get("response_text"):
                    quality_llm = score_quality_llm(
                        agent_result.get("response_text", ""), prompt, task_type, diff_stats)

                cost = agent_result.get("total_cost_usd", 0) or 0
                wall = agent_result.get("wall_time_s", 0)
                q    = quality_llm.get("total", "-") if quality_llm else "-"
                err  = agent_result.get("error", "")
                sid  = session_ids[mode] or "new"
                pprint(f"[{label}] P{pid:02d} → Q={q}/100  ${cost:.4f}  {wall:.0f}s  "
                       f"src_Δ={len(src_files)}  sid={sid[:8]}"
                       + (f"  ERR:{err[:40]}" if err else ""))

                record = {
                    "id": run_id, "prompt_id": pid, "title": title,
                    "category": p.get("category", ""), "task_type": task_type,
                    "mode": mode, "agent": agent_result, "diff_stats": diff_stats,
                    "quality_llm": quality_llm, "timestamp": datetime.now().isoformat(),
                    "session_id": session_ids[mode], "version": VERSION,
                }
                (RESPONSES_DIR / f"P{pid:02d}_{mode}.md").write_text(
                    agent_result.get("response_text", "(empty)"), encoding="utf-8")
                append_jsonl(RAW_FILE, record)
                all_records.append(record)

                # Light reset: unstage changes, preserve graph indexes + session
                light_reset(root, claude_md_map[mode])

            if p != prompts[-1]:
                pprint(f"  Cooldown {COOLDOWN_S}s…")
                time.sleep(COOLDOWN_S)

    finally:
        if v61_srv:
            stop_v61_server(v61_srv, v61_port)

    # Load all records for report (including any from prior --resume runs)
    if RAW_FILE.exists():
        all_records = []
        for line in RAW_FILE.read_text().splitlines():
            try:
                all_records.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    pprint(f"\n{'='*70}")
    pprint(f"  BENCHMARK COMPLETE")
    generate_report(all_records)
    pprint(f"{'='*70}")


if __name__ == "__main__":
    main()
