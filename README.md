# GrapeRoot Benchmarks

Benchmark harness for evaluating GrapeRoot's context engineering against baselines (Normal Claude, Augment Code, Sourcebot, jCodeMunch, CodeReviewGraph).

## Results Summary

| Benchmark | Prompts | GrapeRoot | Best Baseline | Cost Saved |
|-----------|---------|-----------|---------------|------------|
| Agentic v1 (34 microservices, 12 langs) | 110 | 87.12 | 85.21 (Normal) | 25% |
| sonic-swss (C++, 520 files) | 30 | 80.4 | 78.0 (Normal) | 33% |
| Medusa + Gitea (TS + Go) | 33 | 75% win rate | — | 57% avg |
| Boris 4-Way (Medusa TS) | 21 | Best Q/$ | — | 66% |
| Sentry Python (7,762 files) | 24 | Comparable Q | — | 53% |

## Structure

```
swebench/           SWE-bench Pro harness (Docker + graph tools)
  graperoot_bench.py    Orchestrator — runs tasks with graph context
  graph_tool_runner.py  Standalone tool dispatcher (graph_continue, graph_read, fallback_rg)
  evaluate.py           Apply patches + run tests in Docker
  METHODOLOGY.md        SWE-bench specific methodology

custom/             Custom multi-tool comparison benchmarks
  run_medusa_v6.py      3-way comparison runner (GR vs CRG vs Normal)

judge/              LLM judge and rejudge tooling
  rejudge_all.py        Retro-rejudge with full clean diffs

prompts/            Prompt suites (JSON)
  typescript_monorepo_30.json   Medusa (1,571 TS files)
  gitea_go_30.json              Gitea (Go monorepo)
  calcom_vs_augment.json        Cal.com vs Augment comparison

METHODOLOGY.md      Full benchmark methodology documentation
```

## How It Works

### Architecture

```
1. Load prompt from suite (JSON)
2. Start isolated environment (Docker container or git worktree)
3. Agent loop:
   - LLM call (Claude Sonnet 4.6 via Bedrock)
   - Tool calls (mode-specific: graph / grep / MCP)
   - File edits + bash commands
4. git commit — tagged "Step N: task_name"
5. Extract full source-only diff
6. LLM Judge scores (0–100) with rubric
7. Log: prompt, response, diff, score, cost, tokens
```

### Scoring

Independent LLM judge (Claude Haiku 4.5) scores each step 0-100:

| Score | Meaning |
|-------|---------|
| 80-100 | Task fully completed, correct implementation |
| 60-79 | Mostly complete, minor issues |
| 40-59 | Partial, core logic present but incomplete |
| 20-39 | Attempted but significant issues |
| 0-19 | Failed or completely wrong |

**Judge input:** task prompt + agent response (5K chars) + full git diff (source files only).

### Mode Isolation

Each mode runs in its own project directory with independent git history:

| Mode | Context Strategy | Tools |
|------|-----------------|-------|
| GrapeRoot | Proactive — pre-loads files/symbols | graph_continue, graph_read, fallback_rg |
| Sourcebot/Augment | Reactive — agent queries retrieval | search_symbols, get_context |
| Normal (baseline) | Reactive — standard bash | grep, find, cat |

### Key Parameters

- **Model:** Claude Sonnet 4.6 (Bedrock), temperature 0.0
- **Max steps:** 80 (no cost limit)
- **Max tokens/response:** 4,096
- **Judge:** Claude Haiku 4.5
- **Graph builder:** tree-sitter (JS/TS/Python/Go/Rust/C++)
- **Diff filtering:** Excludes node_modules, lock files, .dual-graph/

## Quick Start

### SWE-bench Pro

```bash
pip install litellm datasets

# Run 50 tasks with 2 parallel workers
python3 swebench/graperoot_bench.py --slice 0:50 --workers 2 --output ./results

# Evaluate patches
python3 swebench/evaluate.py ./results/preds.json
```

### Custom Benchmarks

```bash
# 3-way comparison on Medusa TypeScript monorepo
python3 custom/run_medusa_v6.py --modes v61_cont normal_cont --prompts 1-30

# Rejudge all with updated scoring
python3 judge/rejudge_all.py 50steps
```

## Reproducibility

- All results stored as JSONL with full metadata
- Git history has one commit per step, tagged `Step N: step_name`
- Re-run the judge anytime: `python3 judge/rejudge_all.py <run>`
- No per-repo tuning — same system prompt for all tasks

## Limitations

- Judge cannot verify compilation (structural correctness only)
- No test execution during solving (only final eval)
- LLM judge has ~+/-3 point variance on repeated scoring
- Cost includes graph tool overhead (~$0.01/query)

## License

MIT
