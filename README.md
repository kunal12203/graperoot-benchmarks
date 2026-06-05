# GrapeRoot Benchmarks

Run your own benchmarks comparing GrapeRoot's context engineering against vanilla Claude Code, Augment Code, or Sourcebot. Works on any machine — macOS, Linux, or WSL.

## Quick Start

```bash
# 1. Clone this repo
git clone https://github.com/kunal12203/graperoot-benchmarks.git
cd graperoot-benchmarks

# 2. Install GrapeRoot (one command — works on macOS/Linux/WSL)
curl -sSL https://raw.githubusercontent.com/kunal12203/Codex-CLI-Compact/main/install.sh | bash

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Set your API key (Anthropic or AWS Bedrock)
export ANTHROPIC_API_KEY="sk-ant-..."
# OR for Bedrock:
export AWS_ACCESS_KEY_ID="..."
export AWS_SECRET_ACCESS_KEY="..."
export AWS_DEFAULT_REGION="us-east-1"

# 5. Run a benchmark
python3 custom/run_benchmark.py --project /path/to/any/repo --prompts prompts/typescript_monorepo_30.json
```

## What This Measures

We run the **same prompts** through multiple modes on the **same codebase**, then score with an independent LLM judge:

| Mode | How it finds code | Tools available |
|------|-------------------|-----------------|
| **GrapeRoot** | Proactive — pre-indexes symbols, pre-loads relevant files before each turn | `graph_continue`, `graph_read`, `fallback_rg`, `graph_impact` |
| **Normal** (baseline) | Reactive — agent uses grep/find/cat | `grep`, `find`, `cat`, `bash` |
| **Augment/Sourcebot** | Reactive — agent queries a retrieval API | `search_symbols`, `get_context` |

## Setup Guide

### Prerequisites

- Python 3.10+
- Git
- Any Claude API access (Anthropic direct or AWS Bedrock)
- A codebase to benchmark against (or use the included prompts with public repos)

### Install GrapeRoot

**macOS / Linux / WSL:**
```bash
curl -sSL https://raw.githubusercontent.com/kunal12203/Codex-CLI-Compact/main/install.sh | bash
```

**Windows (PowerShell):**
```powershell
irm https://raw.githubusercontent.com/kunal12203/Codex-CLI-Compact/main/install.ps1 | iex
```

This installs the GrapeRoot graph server and CLI. After install, initialize on any project:
```bash
graperoot /path/to/your/project
```

### Install Dependencies

```bash
pip install -r requirements.txt
```

For SWE-bench Pro evaluation (optional):
```bash
pip install datasets
# Docker required for container-based evaluation
```

## Running Benchmarks

### Option 1: Quick A/B Test (any repo)

Compare GrapeRoot vs Normal on your own codebase:

```bash
python3 custom/run_benchmark.py \
  --project /path/to/your/repo \
  --prompts prompts/typescript_monorepo_30.json \
  --modes graperoot normal \
  --output ./my_results
```

This will:
1. Index your repo with GrapeRoot's graph builder (tree-sitter AST)
2. Run each prompt through both modes independently
3. Score each response with the LLM judge
4. Output results as JSONL + summary markdown

### Option 2: SWE-bench Pro

Run against the standard SWE-bench Pro dataset (731 real GitHub issues):

```bash
# Requires Docker for container isolation
python3 swebench/graperoot_bench.py \
  --slice 0:50 \
  --workers 2 \
  --output ./swebench_results

# Evaluate patches (applies to Docker containers, runs tests)
python3 swebench/evaluate.py ./swebench_results/preds.json
```

### Option 3: Custom Prompt Suite

Write your own prompts in JSON format:

```json
[
  {
    "id": 1,
    "title": "Find SQL injection vulnerabilities",
    "prompt": "Audit this codebase for SQL injection. List every file:line where raw user input reaches a database query without parameterization.",
    "category": "security",
    "expected_files": ["src/db/queries.ts", "src/api/users.ts"]
  }
]
```

Then run:
```bash
python3 custom/run_benchmark.py \
  --project /path/to/repo \
  --prompts my_prompts.json \
  --modes graperoot normal
```

## Scoring

### LLM Judge (0–100)

An independent LLM (Claude Haiku 4.5) scores each response based on the full git diff:

| Score | Meaning |
|-------|---------|
| 80–100 | Task fully completed, correct implementation, good code quality |
| 60–79 | Mostly complete, minor issues or missing edge cases |
| 40–59 | Partial — core logic present but incomplete |
| 20–39 | Attempted but significant issues or wrong approach |
| 0–19 | Failed, no meaningful changes or completely wrong |

**Judge input:** task prompt + agent response (5K chars) + full git diff (source files only, no lock files).

### What the judge assesses
- Whether required files were created/modified
- Structural correctness (types, logic, API design)
- Completeness against the task spec
- Obvious bugs visible in the diff

### What the judge cannot assess
- Whether code compiles (no `tsc`/`go build` run)
- Whether tests pass (no `npm test` run during scoring)
- Runtime behavior and subtle integration issues

### Re-scoring

All results include the raw diff. Re-run the judge anytime with updated logic:
```bash
python3 judge/rejudge_all.py <run_name>
```

## Key Parameters

| Parameter | Value | Why |
|-----------|-------|-----|
| Model | Claude Sonnet 4.6 | Latest, most capable coding model |
| Temperature | 0.0 | Deterministic — same prompt = same output |
| Max tokens/response | 4,096 | Enough for any single edit |
| Max agent steps | 80 | No artificial ceiling on complex tasks |
| Judge model | Claude Haiku 4.5 | Fast, cheap, consistent scoring |
| Graph builder | tree-sitter | JS/TS/Python/Go/Rust/C++ support |
| Diff filter | Excludes node_modules, lock files | Judge sees only source changes |

## Results Format

Each run produces a JSONL file. Every line contains:

```json
{
  "prompt_id": 1,
  "mode": "graperoot",
  "quality_score": 87,
  "judge_reason": "All required endpoints created...",
  "cost_usd": 0.54,
  "turns": 12,
  "tokens_in": 45000,
  "tokens_out": 8200,
  "wall_time_s": 34.2,
  "git_diff": "...",
  "response": "..."
}
```

## Our Results

| Benchmark | Prompts | Languages | GrapeRoot Quality | Cost Saved vs Normal |
|-----------|---------|-----------|-------------------|---------------------|
| 34 Microservices | 110 | 12 | 87.12 (0 below 80) | 25% |
| sonic-swss C++ | 30 | C++ | 80.4 | 33% |
| Medusa + Gitea | 33 | TS + Go | 75% win rate | 57% avg |
| Sentry Python | 24 | Python | Comparable | 53% |

Full interactive results: [graperoot.dev/benchmarks](https://graperoot.dev/benchmarks)

## Reproducibility

- Every step is a tagged git commit (`Step N: task_name`)
- All raw data in JSONL with complete metadata
- Re-run the judge anytime without re-running the agent
- No per-repo tuning — same system prompt for all tasks
- No prompt engineering per tool — identical inputs across modes

## License

MIT
