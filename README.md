# rust-index

Generates a compact, grep-friendly code index for Rust workspaces. Designed for LLM agents that need to navigate large codebases without reading every file.

## Quick Start

**1. Install and generate the index:**

```bash
cargo install --path .
rust-index /path/to/project --skip-statics
```

**2. Add to your `CLAUDE.md`** so the agent knows how to use it:

```markdown
## Code Index

A pre-built index of all public symbols is available in `.ai/index/`.
**Before using Grep or Glob to explore the codebase**, check the index first:

- Find a type: `grep "^MyStruct" .ai/index/symbols.txt`
- Find a method: `grep "^MyStruct::my_method" .ai/index/symbols.txt`
- Find all methods on a type: `grep "^MyStruct::" .ai/index/symbols.txt`
- Find which crate owns something: `grep "my_crate" .ai/index/crates.txt`
- Browse a crate's symbols: `grep "." .ai/index/symbols/my_crate.txt`

Each line returns: `name|kind|path:line|signature` — go directly to the file and line.

**Rules:**
- Always use anchored grep patterns (`^TypeName`) to avoid broad matches
- NEVER read symbols.txt with the Read tool — it's too large. Always grep it.
- After finding a symbol, read the actual source file for context.
```

**3. Auto-regenerate after builds** by adding to `.claude/settings.json`:

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

## Output

- **`symbols.txt`** — one line per public symbol: `name|kind|path:line|signature`
- **`symbols/<crate>.txt`** — same, split per crate
- **`crates.txt`** — workspace crates: `crate|path|internal_deps`
- **`modules.txt`** — module overview: `crate|module|file|pub_count|kinds|doc`

## Eval Harness

See [`eval/`](eval/) for a pluggable benchmark that measures token savings, tool calls, and accuracy across different indexer implementations.
