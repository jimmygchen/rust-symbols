# rust-index Eval Harness

Measures the effectiveness of code indexers by running navigation and comprehension
tasks against a codebase using Claude with tool use, comparing token usage,
tool calls, wall time, and accuracy across pluggable indexer configurations.

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
│   ├── l1_navigate.yaml    # Symbol lookup tasks (Lighthouse examples)
│   └── l2_comprehend.yaml  # Cross-file comprehension tasks (Lighthouse examples)
├── indexers/          # Pluggable indexer configs
│   ├── baseline.yaml       # No index (control group)
│   └── rust-index.yaml     # rust-index tool
└── results/           # Output (timestamped JSON + latest)
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
    level: 1               # 1=navigate, 2=comprehend (informational)
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
