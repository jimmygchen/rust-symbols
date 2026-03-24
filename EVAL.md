# rust-index Effectiveness Evaluation

## Setup
- Codebase: Lighthouse (86 crates, 750 files, 7479 pub symbols after --skip-statics)
- Method: 10 identical navigation tasks run 3 ways
- Agent: Claude Explore subagent (read-only)
- Date: 2026-03-23

## Results: 3-Way Comparison

| Task | Description | No Index | Index (read) | Index (grep) |
|------|-------------|----------|-------------|-------------|
| 1 | SyncManager struct + method | 8 | 4 | **2** |
| 2 | BeaconChain block import | 3 | 6 | **2** |
| 3 | ForkChoice::on_block | 3 | 2 | **2** |
| 4 | PeerManager disconnect | 4 | 3 | **2** |
| 5 | HotColdDB finalized state | 6 | 2 | **2** |
| 6 | Attestation verification | 5 | 3 | **3** |
| 7 | ProposerSlashing typedef | 2 | 2 | **2** |
| 8 | Gossipsub block validation | 5 | 5 | **3** |
| 9 | ExecutionLayer proposal | 4 | 2 | **2** |
| 10 | canonical_head lock ordering | 2 | 2 | **2** |
| **Total** | | **42** | **31** | **22** |

## Token & Context Usage

| Metric | No Index | Index (read) | Index (grep) |
|--------|----------|-------------|-------------|
| Total tokens | 43,108 | 48,237 (+12%) | **36,519 (-15%)** |
| Tool uses | 54 | 55 (+2%) | **36 (-33%)** |
| Duration (ms) | 142,656 | 86,350 (-39%) | **89,533 (-37%)** |

## Key Finding: Read vs Grep Changes Everything

The first "with index" run (read-based) was **worse** on tokens (+12%) because the agent read symbols.idx (1.2MB / ~300K tokens) into context. This defeats the entire purpose.

The grep-based approach fixes this completely:
- Each grep returns only matching lines (~100-1000 bytes)
- Agent never loads the full index into context
- Pattern: `grep "^TypeName::method" symbols.idx` → ~1-10 lines returned

**Context cost per grep query:**
- `^SyncManager`: 107 bytes (1 line)
- `^ForkChoice::on_block`: 180 bytes (1 line)
- `^BeaconChain::.*import`: 1,200 bytes (8 lines)
- `^BeaconChain` (BAD - too broad): 102KB (matches all methods)

**Lesson: grep precision matters.** Use `^ExactName` anchored patterns, not substring matches.

## Analysis

### Where index helps most (>= 50% reduction):
- **Deeply nested symbols** (SyncManager: 8→2, HotColdDB: 6→2): Without index, agent explores directory trees. With index, one grep gives file:line.
- **Large types with many methods** (BeaconChain import: 3→2): Index disambiguates which import method exists.
- **Cross-crate navigation** (ExecutionLayer: 4→2): crates.idx tells agent which crate to look in.

### Where index helps least (0% or small reduction):
- **Unique, well-named types** (ProposerSlashing: 2→2): A direct grep on source code is equally fast.
- **Conceptual searches** (gossipsub validation: 5→3): "Where does X happen" doesn't map to a single symbol name, though index still helps narrow down.

### Quality: 10/10 accuracy in all 3 runs
- No cases where the index misled the agent.
- Grep-based agent found more precise locations (specific methods vs file start).

## Conclusions

1. **48% fewer tool calls** (42→22) with grep-based index usage
2. **15% fewer tokens** (43K→36.5K) — context savings, not overhead
3. **33% fewer tool uses** (54→36)
4. **37% faster** wall-clock time
5. **Every task completed in 2-3 calls** (1 grep + 1 verify read)

## Critical Usage Rule

**NEVER read symbols.idx with Read. ALWAYS grep it.**

Good: `grep "^BeaconChain::import" .ai/index/symbols.idx`  (returns ~1KB)
Bad: `read .ai/index/symbols.idx` (loads 1.2MB into context)

## Go/No-Go Decision

**GO** — with the grep-only discipline, the index delivers clear wins on all metrics: fewer calls, fewer tokens, less context, faster completion, same accuracy.
