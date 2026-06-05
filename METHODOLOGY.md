# Benchmark Methodology & Findings

## Overview

This document describes the design, methodology, limitations, and key findings of the
3-way benchmark comparing **GrapeRoot (GR)**, **jCodeMunch (JCM)**, and **CodeReviewGraph (CRG)**
on sequential coding tasks over a real Node.js/TypeScript project (ColabNotes).

---

## Benchmark Design

### Task Suite

- **50 sequential coding tasks** (steps 61–110) on ColabNotes — a production-grade
  collaborative notes app with PostgreSQL, Redis, Socket.IO, Prisma, and JWT auth.
- Tasks are cumulative: each step builds on the previous state of the codebase.
- 5 categories, 10 tasks each:
  | Category | Description |
  |----------|-------------|
  | `feature` | Build new end-to-end features (schema → service → API → WebSocket) |
  | `refactor` | Restructure existing code without changing behavior |
  | `debug` | Find and fix real bugs (race conditions, cache invalidation, SQL injection) |
  | `security` | Harden endpoints (rate limiting, CSRF, input validation, 2FA) |
  | `testing` | Write comprehensive test suites (unit, integration, performance) |

- Complexity: mix of `medium` and `high` — most require changes across 5–20 files.

### Modes

| Mode | Tool | Context Strategy |
|------|------|-----------------|
| **GrapeRoot** | Dual-graph MCP (HTTP) | **Proactive** — pre-loads relevant symbols/files before each turn via `graph_continue` → `graph_read`. Claude receives context without asking. |
| **jCodeMunch** | AST + BM25 (SSE MCP) | **Reactive** — Claude must call `search_symbols()` to find relevant code. tree-sitter AST + keyword search. |
| **CodeReviewGraph** | networkx + tree-sitter (stdio MCP) | **Reactive** — Claude calls `get_review_context`, `get_impact_radius` etc. Knowledge graph with dependency tracking. |

Each mode runs in an **isolated project directory** with its own git history, so runs
are fully independent and don't interfere.

### Execution

- All 3 modes run **in parallel** on the same 50 prompts.
- Each step: Claude runs in agentic mode (multi-turn), commits changes, then judge runs.
- A git commit is made after each step tagged `Step N: step_name` for reproducibility.
- Ports: GrapeRoot=8203, jCodeMunch=8204, CodeReviewGraph=stdio.

---

## Scoring

### Two-signal quality score

Each step gets scored by two independent signals:

**1. Generic heuristic score (0–6)**
- Code block presence in response (+1–3)
- File references matching expected files (+1 per match)
- Response length proxy (+1 if >500 chars, +1 if >2000)

**2. LLM Judge score (0–100) — primary signal**
- Model: `claude-haiku-4-5-20251001`
- Input: task prompt + Claude's response (5000 chars) + **full git diff** (source files only)
- Rubric:
  - 80–100: Task fully completed, correct implementation, good code quality
  - 60–79: Mostly complete, minor issues or missing edge cases
  - 40–59: Partial completion, core logic present but incomplete
  - 20–39: Attempted but significant issues or wrong approach
  - 0–19: Failed, no meaningful changes or completely wrong

### What the LLM judge CAN assess
- Whether required files were created and modified
- Structural correctness (types, missing logic, wrong SQL, bad API design)
- Completeness against the task spec (required fields, endpoints, error handling)
- Obvious bugs visible in the diff (type mismatches, missing awaits, wrong keys)
- Whether the approach is architecturally sound

### What the LLM judge CANNOT assess
- Whether the code compiles (`tsc --noEmit` not run)
- Whether tests pass (`npm test` not run)
- Runtime behavior and edge cases that require execution
- Subtle integration correctness (e.g. Redis key consistency across files)

> **In short:** the judge is a senior developer doing a code review, not a CI pipeline.
> It catches structural and requirements issues reliably, but won't catch subtle runtime bugs.

---

## Known Issues & Fixes Applied

### 1. Diff truncation (major — caused ~15–50 point score drops)

**Problem:** Early runs capped the `git diff` at 1,500 → 5,000 → 10,000 chars.
Most feature and refactor diffs are 15k–300k chars. The judge only saw metadata files
(`.dual-graph/context-store.json` written by GrapeRoot, or `package-lock.json`) and
concluded "no real code changes visible."

**Examples of score impact:**
| Step | Truncated score | Correct score | Delta |
|------|----------------|---------------|-------|
| Step 91 security_rate_limiting (GR) | 28 | 87 | +59 |
| Step 76 refactor_api_versioning (GR) | 30 | 87 | +57 |
| Step 86 debug_websocket_reconnect (GR) | 40 | 92 | +52 |
| Step 63 feature_page_version_history (GR) | 40 | 90 | +50 |

**Fix:** Removed all size caps. Diff is now unlimited. Excludes `node_modules`,
`.dual-graph`, `package-lock.json`, and `*.lock` files so the judge budget is spent
on real source code.

**Retroactive fix:** A retro-rejudge script replays every completed step, fetches
the full clean diff from git history, and re-scores all entries.

### 2. False rate-limit detection

**Problem:** `is_rate_limited()` checked `response_text` for strings like `"401"`,
`"rate limit"`. Legitimate responses (e.g. "statusCode 401", "Rate limit exports to 10/hour")
triggered 75-minute waits.

**Fix:** Rate limit is only detected if `len(response_text) < 100` AND the `error`
field contains a rate-limit signal. Real responses (>100 chars) are never treated as errors.

### 3. node_modules committed by JCM/CRG

**Problem:** On security/testing steps involving `npm install`, JCM and CRG occasionally
committed `node_modules` (hundreds of files, 20k–70k lines). This consumed the entire
diff budget leaving no room for actual source code.

**Fix:** `node_modules` and `package-lock.json` excluded from all diffs via
`:(exclude)node_modules :(exclude)package-lock.json`.

### 4. CRG hallucination (step 75, score 5)

On `refactor_prisma_soft_delete`, CRG made **zero file changes** and claimed the work
was already done. Judge correctly scored 5. This is a known failure mode for reactive
tools when the codebase state doesn't match the tool's knowledge graph.

---

## Results (partial — 36–46/50 steps complete)

> Note: GrapeRoot results are retro-rejudged with full diffs. JCM/CRG rejudge in progress.

### Overall (steps with full clean diff)

| Mode | Steps | Avg Score | Total Cost | Avg Turns | Cost/Point |
|------|-------|-----------|------------|-----------|------------|
| **GrapeRoot** | 36/50 | **81.2** | $38.82 | 43.6 | $0.478/pt |
| **CodeReviewGraph** | 46/50 | 73.8 | $61.79 | 51.4 | $0.837/pt |
| **jCodeMunch** | 46/50 | 72.2 | $57.87 | 44.7 | $0.801/pt |

### By category (all modes, steps where all 3 have data)

| Category | GR | JCM | CRG | Winner |
|----------|----|-----|-----|--------|
| Feature | **83.6** | 69.0 | 79.3 | GR |
| Refactor | **77.4** | 74.5 | 65.9 | GR |
| Debug | 78.5 | 80.5 | **80.8** | CRG≈JCM |
| Security | **89.4** | 69.0 | 74.3 | GR |
| Testing | — | 65.3 | 65.5 | Tie |

### Key findings

1. **GrapeRoot is the most cost-efficient** — highest quality at lowest cost ($38 vs $58–62).
   Proactive context delivery means Claude spends turns coding, not searching.

2. **GrapeRoot leads on features and security** — tasks requiring broad multi-file
   awareness benefit most from pre-loaded context.

3. **Debug is a near-tie** — AST/graph search tools (JCM, CRG) are roughly equal to
   proactive loading for targeted bug fixes.

4. **JCM has a node_modules problem** — several security/testing steps contaminated
   with package installs that bloated the commit. Needs `.gitignore` enforcement.

5. **Proactive vs reactive matters most for large tasks** — on simple bug fixes the gap
   narrows; on 15+ file feature builds GR's pre-loaded context is a significant advantage.

---

## Reproducibility

All raw results stored as JSONL in `benchmark/results/raw_{mode}_50steps.jsonl`.
Each record contains: prompt id, step name, category, full response text, git diff,
generic score, LLM judge score + reason, cost, turns, token counts, wall time.

Git history in `benchmark/build-projects/collabnotes-{mode}/` has one commit per step,
tagged `Step N: step_name`, allowing full replay and re-scoring at any time.

To retro-rejudge all steps with updated judge logic:
```bash
python3 benchmark/rejudge_all.py 50steps
```

---

*Last updated: 2026-03-24. Benchmark still running — final results pending.*
