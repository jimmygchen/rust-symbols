# rust-index

Generates a compact, grep-friendly code index for Rust workspaces. Designed for LLM agents that need to navigate large codebases without reading every file.

## Usage

```bash
cargo install --path .

# Generate index (writes to .ai/index/)
rust-index /path/to/workspace --skip-statics

# Custom output directory
rust-index /path/to/workspace --output /path/to/output --stats
```

## Output

Three index files are generated:

- **`symbols.txt`** — one line per public symbol: `name|kind|path:line|signature`
- **`symbols/<crate>.txt`** — same, split per crate
- **`crates.txt`** — workspace crates: `crate|path|internal_deps`
- **`modules.txt`** — module overview: `crate|module|file|pub_count|kinds|doc`

## How Agents Use It

Agents **grep** the index files (never read them fully):

```bash
grep "^BeaconChain::import" .ai/index/symbols.txt
# → BeaconChain::import_block|fn|beacon_node/beacon_chain/src/beacon_chain.rs:2847|pub fn import_block...
```

One grep returns the file and line number, replacing multiple rounds of glob/read exploration.

## Claude Code Hook

Auto-regenerate the index after builds by adding to `.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "if echo \"$TOOL_INPUT\" | grep -qE '(cargo build|cargo check|cargo test)'; then rust-index /path/to/project --output .ai/index --skip-statics; fi"
          }
        ]
      }
    ]
  }
}
```

## Eval Harness

See [`eval/`](eval/) for a pluggable benchmark that measures token savings, tool calls, and accuracy across different indexer implementations.
