# Eval Harness

Measures how code-navigation tools affect LLM-assisted development on Lighthouse.
The goal is to find how tools — individually or together — can reduce context usage,
token cost, and wall-clock time while improving accuracy across navigation,
comprehension, and impact analysis tasks.

The harness is language and project agnostic. The included example tasks use
[Lighthouse](https://github.com/sigp/lighthouse) (a large Rust workspace) as a
target, but you can write tasks for any codebase.

## Quick Start

```bash
# Prerequisites: Python 3.10+
pip install anthropic pyyaml httpx

# Validate tasks (1 run each)
python runner.py --workspace /path/to/project --runs 1

# Single task, single indexer (for debugging)
python runner.py --workspace /path/to/project --task-id l1_sync_manager --indexer-name baseline --runs 1

# Full eval (3 runs for statistical significance)
python runner.py --workspace /path/to/project --runs 3

# Re-print summary from previous results (no API calls)
python runner.py --workspace /path/to/project --results results/results-latest.json
```

## Authentication

The runner auto-detects credentials:

| Env vars | Method |
|----------|--------|
| `ANTHROPIC_API_KEY` | Direct Anthropic API |
| `CLAUDE_CODE_USE_BEDROCK=1` + `AWS_BEARER_TOKEN_BEDROCK` | Bedrock with bearer token |

## Model Selection

```bash
--model sonnet   # default
--model haiku    # cheaper, for iterating on tasks
--model opus     # most capable
--model claude-sonnet-4-20250514  # explicit model ID
```

## Directory Structure

```
eval/
├── runner.py          # Main harness (agent loop, tools, reporting)
├── tasks/             # Task definitions (YAML)
│   ├── l1_navigate.yaml    # L1: Symbol lookup tasks
│   ├── l2_comprehend.yaml  # L2: Cross-file comprehension tasks
│   └── l3_impact.yaml      # L3: Impact analysis tasks (caller reachability, shared state)
├── indexers/          # Pluggable indexer configs
│   ├── baseline.yaml         # No index (control group)
│   ├── rust-symbols.yaml     # Flat symbol index
│   └── knowledge-graph.yaml  # Structural KG (caller/callee, shared-state, effects)
└── results/           # Output (gitignored: timestamped JSON + reports)
```

## Adding a New Indexer

Create `indexers/my-indexer.yaml`:

```yaml
name: my-indexer
# Shell command to generate the index. {workspace} is replaced at runtime.
setup_command: "my-tool generate --output {workspace}/.ai/my-index"
# Extra instructions appended to the agent's system prompt
system_prompt_extra: |
  You have an index at .ai/my-index/. Use grep to search it:
  - grep "^TypeName" .ai/my-index/symbols.txt
  NEVER read the index files fully.
# Files the index produces (for documentation only)
files:
  - .ai/my-index/symbols.txt
```

Then run:
```bash
python runner.py --workspace /path/to/project --indexer-name my-indexer --runs 1
```

## Adding Tasks

Create or edit YAML files in `tasks/`:

```yaml
tasks:
  - id: unique_id
    level: 1               # 1=navigate, 2=comprehend, 3=impact
    category: navigate
    description: "Short description"
    prompt: >
      The full prompt sent to the agent.
    max_turns: 10           # Cap on agent turns (prevents runaway token usage)
    verify:
      type: contains_all    # or: contains_any, regex_all, regex_any
      values:
        - "string that must appear in answer"
```

### Task Levels

| Level | Category | What it tests | Example |
|-------|----------|---------------|---------|
| L1 | Navigate | Find a specific struct/method/type | "Find where SyncManager is defined" |
| L2 | Comprehend | Trace a flow or explain a subsystem | "Trace the block import flow" |
| L3 | Impact | Analyse call chains, shared state, side effects | "Trace callers of verify_proposer_slashing to entry points" |

L1/L2 tasks test basic tool-assisted navigation. L3 tasks test structural reasoning that
requires relationship data (call graphs, shared state) beyond what grep can provide.

## Knowledge-Graph Indexer Setup

The `knowledge-graph` indexer requires two env vars pointing to the KG tool:

```bash
export KG_QUERY_SCRIPT=/path/to/kg_query.py
export KG_DATA_PATH=/path/to/knowledge-graph.jsonl
```

The system prompt in `indexers/knowledge-graph.yaml` references `${KG_QUERY_SCRIPT}` and
`${KG_DATA_PATH}`. These are expanded by bash when the agent runs KG queries.

## Running a Full Comparison

```bash
# Run each indexer separately (runner accepts one --indexer-name at a time)
python runner.py --workspace /path/to/lighthouse --indexer-name baseline --runs 1 --model sonnet
python runner.py --workspace /path/to/lighthouse --indexer-name rust-symbols --runs 1 --model sonnet
python runner.py --workspace /path/to/lighthouse --indexer-name knowledge-graph --runs 1 --model sonnet
```

Each run produces a timestamped JSON in `results/`. Use `--runs 3` for variance data.

## What Gets Measured

Per run:
- **input_tokens** / **output_tokens** — cumulative across all turns
- **peak_input_tokens** — highest single-turn input (actual context window usage)
- **tool_call_count** — number of tool invocations
- **turns** — number of agent loop iterations
- **wall_time_ms** — end-to-end wall clock time
- **correct** — whether the answer passes verification

The summary shows averages per indexer with deltas vs baseline (first indexer listed).

## Tool Limits

To prevent runaway token usage, tool outputs are capped:
- `read_file`: 200 lines per call
- `grep`: 50 matching lines
- `glob_files`: 100 results
- `bash`: 10KB output

Near the turn limit, the agent is prompted to submit its best answer immediately.

## Tips

- Start with `--runs 1` to validate tasks and verification before scaling
- Use `--task-id` and `--indexer-name` to iterate on specific combinations
- Inspect `results/results-latest.json` for full answers and tool call traces
- If verification fails but the agent found the right info, loosen the verify values

## Generating a Report

### 1. Run the eval

Run all three indexers as described in "Running a Full Comparison" above.

### 2. Print summary

```bash
python runner.py --workspace . --results results/results-latest.json
```

### 3. Manual quality scoring (L3 tasks)

Automated verification (`contains_all`) is coarse — it checks keyword presence, not answer quality.
For L3 tasks, manually score each answer against ground truth:

| Score | Meaning |
|-------|---------|
| 0 | Wrong or no answer |
| 1 | Partial — found some relevant info but missed key elements |
| 2 | Mostly correct — identified key items but vague on some details |
| 3 | Comprehensive — complete with file paths and function names |

To establish ground truth, query the KG and read source code to build the full picture
(e.g., all callers of a function traced to entry points, all shared state fields with types).

### 4. Compile the report

Structure:

```
Goal            — One-line question the eval answers (e.g. "can these tools
                  improve accuracy / reduce tokens / speed up dev tasks?")
Exec Summary    — Headline metrics table with Δ vs baseline columns
                — Short findings grouped by metric (accuracy, efficiency, bottom line)
                — No prose blobs — use labeled paragraphs, keep each to 1-2 sentences
Task Design     — Task table, verification criteria, rationale
Per-Task Results — Full table (baseline + each tool), failure analysis
Recommendation  — Short/medium/long term, tied to the numbers
Appendix        — Earlier iterations, raw files, methodology
```

### Reporting principles

- **Always compare against baseline** — summary table must include Δ columns
- **Lead with the question** — exec summary opens with what we're trying to learn
- **Findings by metric, not by tool** — group by accuracy/efficiency/cost, not by indexer
- **Short labeled paragraphs** — `**Accuracy**: ...` not multi-sentence prose blocks
- **Show all metrics** — accuracy, tokens, wall time, tool calls; no single metric tells the full story
- **Include per-task data** — averages hide important differences
- **Explain the mechanism** — not just "fewer tokens" but *why* (output density, query overhead)
- **Document limitations** — N, cold-load overhead, output caps, snapshot freshness
