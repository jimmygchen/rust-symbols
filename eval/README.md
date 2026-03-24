# rust-index Eval Harness

Measures the effectiveness of code indexers by running navigation and comprehension
tasks against a Rust workspace using Claude with tool use, comparing token usage,
tool calls, wall time, and accuracy across indexer configurations.

## Quick Start

```bash
# Prerequisites: Python 3.10+, anthropic SDK
pip install anthropic pyyaml

# Run all tasks, all indexers, 1 run each (validation pass)
python runner.py --workspace /path/to/lighthouse --runs 1

# Run a single task with a single indexer (for debugging)
python runner.py --workspace /path/to/lighthouse --task-id l1_sync_manager --indexer-name baseline --runs 1

# Full eval (3 runs for statistical significance)
python runner.py --workspace /path/to/lighthouse --runs 3

# Re-print summary from previous results
python runner.py --workspace /path/to/lighthouse --results results/results-latest.json
```

## Authentication

The runner auto-detects credentials:

| Env vars | Method |
|----------|--------|
| `CLAUDE_CODE_USE_BEDROCK=1` + `AWS_BEARER_TOKEN_BEDROCK` | Bedrock with bearer token |
| `ANTHROPIC_API_KEY` | Direct Anthropic API |

## Model Selection

```bash
python runner.py --workspace ... --model sonnet   # default
python runner.py --workspace ... --model haiku    # cheaper, for iteration
python runner.py --workspace ... --model opus     # most capable
python runner.py --workspace ... --model claude-sonnet-4-20250514  # explicit ID
```

## Directory Structure

```
eval/
├── runner.py          # Main harness (agent loop, tools, reporting)
├── tasks/             # Task definitions (YAML)
│   ├── l1_navigate.yaml    # Symbol lookup tasks
│   └── l2_comprehend.yaml  # Cross-file comprehension tasks
├── indexers/          # Pluggable indexer configs
│   ├── baseline.yaml       # No index (control group)
│   └── rust-index.yaml     # rust-index tool
└── results/           # Output (timestamped JSON + latest symlink)
```

## Adding a New Indexer

Create `indexers/my-indexer.yaml`:

```yaml
name: my-indexer
# Shell command to generate the index. {workspace} is replaced with the workspace path.
setup_command: "my-tool generate --output {workspace}/.ai/my-index"
# Extra instructions appended to the agent's system prompt
system_prompt_extra: |
  You have an index at .ai/my-index/. Use grep to search it:
  - grep "^TypeName" .ai/my-index/symbols.txt
  NEVER read the index files fully.
# Files the index produces (for documentation)
files:
  - .ai/my-index/symbols.txt
```

Run it:
```bash
python runner.py --workspace /path/to/lighthouse --indexer-name my-indexer --runs 1
```

## Adding Tasks

Create or edit YAML files in `tasks/`. Format:

```yaml
tasks:
  - id: unique_id          # Used in results and --task-id filter
    level: 1               # 1=navigate, 2=comprehend
    category: navigate
    description: "Short description"
    prompt: >
      The full prompt sent to the agent.
    max_turns: 10           # Cap on agent turns (prevents runaway token usage)
    verify:
      type: contains_all    # or contains_any, regex_all, regex_any
      values:
        - "string that must appear in answer"
        - "another required string"
```

## What Gets Measured

Per run:
- **input_tokens** / **output_tokens** — from API usage (cumulative across all turns)
- **tool_call_count** — number of tool invocations
- **turns** — number of agent loop iterations
- **wall_time_ms** — end-to-end wall clock time
- **correct** — whether the answer passes verification

The summary report shows averages per indexer and per-task breakdowns with deltas vs baseline.

## Tool Limits

To prevent runaway token usage, tool outputs are capped:
- `read_file`: max 200 lines per call
- `grep`: max 50 matching lines
- `glob_files`: max 100 results
- `bash`: max 10KB output

On the last turn, the agent is prompted to submit its best answer immediately.

## Tips

- Start with `--runs 1` to validate tasks and verification before scaling up
- Use `--task-id` and `--indexer-name` to iterate on specific combinations
- Check `results/results-latest.json` for full details including answers and tool call traces
- If verification fails but the agent found the right code, loosen the verify values
