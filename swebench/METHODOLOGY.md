# SWE-bench Pro Benchmark: Sonnet 4.6 + GrapeRoot Pro

## Claim
Claude Sonnet 4.6 + GrapeRoot Pro (context engineering) vs vanilla Claude models on SWE-bench Pro.

## Setup

### Infrastructure
- **EC2**: c5.4xlarge, us-east-1 ($0.68/hr)
- **Model**: `bedrock/us.anthropic.claude-sonnet-4-6` via AWS Bedrock + litellm
- **Docker**: SWE-bench Pro images from `jefzda/sweap-images:{dockerhub_tag}`
- **Dataset**: `ScaleAI/SWE-bench_Pro` (731 tasks total, we ran 50)

### Architecture
```
┌─────────────────────────────────────────────────────┐
│  graperoot_bench.py (orchestrator)                  │
│                                                     │
│  1. Start Docker container (repo at /app)           │
│  2. docker cp /app → host copy                      │
│  3. graph_builder.py → info_graph.json              │
│     (AST-based: tree-sitter for JS/TS/Python/Go/Rust)
│  4. Agent loop:                                     │
│     - LLM call (Bedrock Sonnet 4.6)                 │
│     - graph_continue → recommended files + confidence│
│     - graph_read → file or symbol excerpt           │
│     - fallback_rg → ripgrep with pcre2              │
│     - bash → docker exec in container               │
│  5. Extract git diff as patch                       │
│  6. Checkpoint to preds.json                        │
└─────────────────────────────────────────────────────┘
```

### What GrapeRoot Pro provides (context engineering)
1. **graph_continue**: Before any file exploration, queries the project graph.
   Returns ranked `recommended_files` and `confidence` level (high/medium/low).
   Built from AST — extracts symbols, imports, exports, edges.

2. **graph_read**: Reads files or specific symbols (`file::FunctionName`).
   Symbol-level reads return only the function body (30-60 lines) instead of
   the full 400-line file. Saves tokens, focuses attention.

3. **fallback_rg**: Ripgrep with pcre2 engine. Scoped to graph-retrieved
   directories first for relevance.

4. **graph_impact**: Find all dependents of a file via import edges.


## How to reproduce

```bash
# 1. EC2 setup
sudo apt-get install docker.io python3-pip
pip install litellm datasets

# 2. GrapeRoot Pro
tar xzf graperoot-pro.tar.gz -C ~/graperoot-pro/
pip install tree-sitter tree-sitter-javascript tree-sitter-typescript tree-sitter-python

# 3. Run
python3 graperoot_bench.py --slice 0:50 --workers 2 --output ./results

# 4. Evaluate
python3 evaluate.py ./results/preds.json
```

## Evaluation
- Each patch is applied to a fresh Docker container
- Test patch (new tests from the ground truth) is applied on top
- Test suite runs (`pytest`, `mocha`, `go test` depending on language)
- **Resolved** = all tests pass (including fail_to_pass tests)
- Resolve rate = resolved / total_instances

## Key parameters
- **Max steps**: 80 (no cost limit)
- **Temperature**: 0.0
- **Max tokens per response**: 4096
- **Workers**: 2 (parallel instances)
- **No prompt engineering** — same system prompt for all tasks
- **No per-repo tuning** — graph_builder handles JS/TS/Python/Go/Rust uniformly

## Comparison targets
| Model | SWE-bench Pro |
|-------|--------------|
| Opus 4.6 (thinking) | 51.9% |
| Sonnet 4.5 | 43.6% |
| Sonnet 4.6 + GrapeRoot Pro | **TBD** |

## Cost
- Bedrock: ~$1.50-3.00 per instance (no cost cap)
- EC2: ~$0.68/hr × ~4hr = ~$2.72
- Total for 50 tasks: ~$100-150
