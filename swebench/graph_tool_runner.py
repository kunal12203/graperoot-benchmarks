#!/usr/bin/env python3
"""
Thin wrapper: runs a single GrapeRoot graph tool call and prints JSON result.
Called as: python3 graph_tool_runner.py <data_dir> <project_root> <tool> <args_json>
"""
import sys
import os
import json
import subprocess
from pathlib import Path

data_dir = Path(sys.argv[1])
project_root = Path(sys.argv[2])
tool_name = sys.argv[3]
args = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}

graph_path = data_dir / "info_graph.json"
sym_index_path = data_dir / "symbol_index.json"

if not graph_path.exists():
    print(json.dumps({"ok": False, "error": f"Graph not found at {graph_path}"}))
    sys.exit(0)

try:
    with open(graph_path) as f:
        graph_data = json.load(f)
except Exception as e:
    print(json.dumps({"ok": False, "error": f"Graph load error: {e}"}))
    sys.exit(0)

# Build lookup structures from nodes list
# graph_data["nodes"] is a list of dicts with 'kind': 'file' | 'symbol'
# and 'path', 'name', 'keywords', etc.
nodes = graph_data.get("nodes", [])

# files_by_path: path -> {symbols: [name, ...], keywords: [...]}
files_by_path = {}
symbol_names_by_path = {}
for n in nodes:
    path = n.get("path", "")
    if n.get("kind") == "file":
        files_by_path[path] = n
        if path not in symbol_names_by_path:
            symbol_names_by_path[path] = []
    elif n.get("kind") == "symbol":
        if path not in symbol_names_by_path:
            symbol_names_by_path[path] = []
        symbol_names_by_path[path].append(n.get("name", ""))

edges = graph_data.get("edges", [])


def do_graph_continue(query: str) -> dict:
    """Return top-ranked files for the query using BM25-style scoring."""
    query_terms = set(query.lower().split())

    scores = {}
    for path, fdata in files_by_path.items():
        score = 0
        path_lower = path.lower()
        for term in query_terms:
            if term in path_lower:
                score += 3
            # Check symbols in this file
            for sym in symbol_names_by_path.get(path, []):
                if term in sym.lower():
                    score += 2
            # Check keywords
            for kw in fdata.get("keywords", []):
                if term in str(kw).lower():
                    score += 1
        if score > 0:
            scores[path] = score

    ranked = sorted(scores.items(), key=lambda x: -x[1])[:8]
    recommended = [p for p, _ in ranked]

    # Also check symbol index if available
    sym_hits = []
    if sym_index_path.exists():
        try:
            with open(sym_index_path) as f:
                sym_index = json.load(f)
            for sym_id, sym_meta in sym_index.items():
                for term in query_terms:
                    if term in sym_id.lower():
                        sym_hits.append(f"{sym_meta.get('path', sym_id)}::{sym_id.split('::')[-1]}")
                        break
        except Exception:
            pass

    all_recommended = list(dict.fromkeys(recommended + sym_hits[:3]))[:8]

    confidence = "high" if len(all_recommended) >= 5 else "medium" if len(all_recommended) >= 2 else "low"
    return {
        "ok": True,
        "confidence": confidence,
        "recommended_files": all_recommended,
        "max_supplementary_greps": 2 if confidence == "medium" else 3,
        "max_supplementary_files": 2 if confidence == "medium" else 3,
        "hint": f"Found {len(all_recommended)} relevant files for: {query}. Total graph: {len(files_by_path)} files, {sum(len(v) for v in symbol_names_by_path.values())} symbols.",
    }


def do_graph_read(file: str) -> dict:
    """Read a file or file::symbol from disk."""
    if "::" in file:
        file_path, symbol = file.split("::", 1)
    else:
        file_path, symbol = file, None

    full_path = project_root / file_path
    if not full_path.exists():
        full_path = Path(file_path)
    if not full_path.exists():
        return {"ok": False, "error": f"File not found: {file_path}"}

    content = full_path.read_text(errors="replace")

    if symbol:
        # Find the symbol in our node list
        for n in nodes:
            if n.get("kind") == "symbol" and n.get("name") == symbol and n.get("path") == file_path:
                start = max(0, n.get("line_start", 1) - 1)
                end = min(len(content.split("\n")), n.get("line_end", start + 60))
                lines = content.split("\n")
                return {
                    "ok": True,
                    "file": file,
                    "content": "\n".join(lines[start:end]),
                    "lines": f"{start+1}-{end}",
                }
        # Fallback: search in content
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if symbol in line and any(kw in line for kw in ("def ", "function ", "class ", "const ", "=>")):
                start = max(0, i)
                end = min(len(lines), i + 60)
                return {
                    "ok": True,
                    "file": file,
                    "content": "\n".join(lines[start:end]),
                    "lines": f"{start+1}-{end}",
                }

    if len(content) > 15000:
        content = content[:7500] + "\n...[truncated]...\n" + content[-7500:]

    return {"ok": True, "file": file_path, "content": content}


def do_fallback_rg(pattern: str, path: str = ".") -> dict:
    """Run ripgrep or grep on the project."""
    search_path = str(project_root / path) if not path.startswith("/") else path

    for cmd_base in [
        ["rg", "--no-heading", "-n", "-m", "100", "--engine", "pcre2"],
        ["rg", "--no-heading", "-n", "-m", "100"],
        ["grep", "-rn"],
    ]:
        try:
            r = subprocess.run(
                cmd_base + [pattern, search_path],
                capture_output=True, text=True, timeout=30
            )
            output = r.stdout[:6000]
            lines = [l for l in output.split("\n") if l.strip()]
            return {"ok": True, "pattern": pattern, "hits": lines, "count": len(lines)}
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    return {"ok": False, "error": "Neither rg nor grep available"}


def do_graph_impact(file: str) -> dict:
    """Find files that import the given file."""
    dependents = []
    file_lower = file.lower().rstrip("/")

    for edge in edges:
        src = str(edge.get("source", "")).lower()
        tgt = str(edge.get("target", "")).lower()
        if file_lower in tgt or tgt.endswith(file_lower):
            dependents.append(edge.get("source", ""))

    return {"ok": True, "file": file, "dependents": list(set(dependents))[:20]}


# Dispatch
if tool_name == "graph_continue":
    result = do_graph_continue(args.get("query", ""))
elif tool_name == "graph_read":
    result = do_graph_read(args.get("file", ""))
elif tool_name == "fallback_rg":
    result = do_fallback_rg(args.get("pattern", ""), args.get("path", "."))
elif tool_name == "graph_impact":
    result = do_graph_impact(args.get("file", ""))
else:
    result = {"ok": False, "error": f"Unknown tool: {tool_name}"}

print(json.dumps(result))
