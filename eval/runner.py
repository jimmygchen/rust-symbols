#!/usr/bin/env python3
"""Eval harness for comparing code indexer / knowledge-graph effectiveness.

Runs navigation and comprehension tasks against a Rust workspace using the
Claude API with tool use, measuring tokens, tool calls, wall time, and
correctness across pluggable indexer configurations.

Usage:
    python runner.py --workspace /path/to/lighthouse
    python runner.py --workspace /path/to/lighthouse --task-id l1_sync_manager --indexer-name baseline --runs 1
    python runner.py --workspace /path/to/lighthouse --results results/prev.json  # re-report only
"""

import argparse
import glob as glob_mod
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

import httpx

try:
    from anthropic import Anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    name: str
    input: dict
    output_bytes: int
    duration_ms: int


@dataclass
class RunResult:
    task_id: str
    indexer: str
    run_number: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    tool_calls: list  # list[ToolCall]
    wall_time_ms: int
    turns: int
    answer: str
    correct: bool
    error: Optional[str] = None


@dataclass
class Task:
    id: str
    level: int
    category: str
    prompt: str
    description: str
    verify_type: str
    verify_values: list
    max_turns: int = 25


@dataclass
class Indexer:
    name: str
    setup_command: Optional[str]
    system_prompt_extra: str
    files: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Loading configs
# ---------------------------------------------------------------------------

def load_tasks(path: str) -> list:
    tasks = []
    p = Path(path)
    files = sorted(p.glob("*.yaml")) if p.is_dir() else [p]
    for f in files:
        with open(f) as fh:
            data = yaml.safe_load(fh)
        items = data if isinstance(data, list) else data.get("tasks", [data])
        for t in items:
            tasks.append(Task(
                id=t["id"],
                level=t["level"],
                category=t["category"],
                prompt=t["prompt"],
                description=t["description"],
                verify_type=t["verify"]["type"],
                verify_values=t["verify"]["values"],
                max_turns=t.get("max_turns", 25),
            ))
    return tasks


def load_indexers(path: str) -> list:
    p = Path(path)
    files = sorted(p.glob("*.yaml")) if p.is_dir() else [p]
    indexers = []
    for f in files:
        with open(f) as fh:
            data = yaml.safe_load(fh)
        indexers.append(Indexer(
            name=data["name"],
            setup_command=data.get("setup_command"),
            system_prompt_extra=data.get("system_prompt_extra", ""),
            files=data.get("files", []),
        ))
    return indexers


# ---------------------------------------------------------------------------
# Tool definitions (what the agent sees)
# ---------------------------------------------------------------------------

TOOL_DEFS = [
    {
        "name": "read_file",
        "description": "Read a file with line numbers (max 200 lines per call). Path relative to workspace root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path from workspace root"},
                "offset": {"type": "integer", "description": "Start line (1-based)"},
                "limit": {"type": "integer", "description": "Max lines to return"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "grep",
        "description": "Search for a regex pattern in a file or directory. Returns matching lines with numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern"},
                "path": {"type": "string", "description": "File or directory (relative)"},
                "flags": {"type": "string", "description": "Extra grep flags e.g. '-r -i -l'"},
            },
            "required": ["pattern", "path"],
        },
    },
    {
        "name": "glob_files",
        "description": "Find files matching a glob pattern. Returns relative paths.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.rs')"},
                "path": {"type": "string", "description": "Base directory relative to workspace root"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "bash",
        "description": "Run a read-only shell command. Write operations are blocked.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "submit_answer",
        "description": "Submit your final answer when done.",
        "input_schema": {
            "type": "object",
            "properties": {
                "answer": {"type": "string", "description": "Your complete answer"},
            },
            "required": ["answer"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _resolve_path(path: str, workspace: str) -> str:
    resolved = Path(os.path.join(workspace, path)).resolve()
    ws = Path(workspace).resolve()
    if not str(resolved).startswith(str(ws)):
        return None
    return str(resolved)


def execute_tool(name: str, params: dict, workspace: str) -> str:
    if name == "read_file":
        resolved = _resolve_path(params["path"], workspace)
        if not resolved:
            return "Error: path outside workspace"
        try:
            with open(resolved) as f:
                lines = f.readlines()
            offset = max(0, params.get("offset", 1) - 1)
            limit = min(params.get("limit", 200), 200)  # Hard cap at 200 lines
            selected = lines[offset:offset + limit]
            total = len(lines)
            if offset + limit < total:
                # Tell agent there's more they didn't see
                truncation_note = f"\n[... truncated at line {offset + limit}/{total}. Use offset to read more.]\n"
            else:
                truncation_note = ""
            if not selected:
                return "(empty or beyond end of file)"
            content = "".join(
                f"{offset + i + 1:>6}| {line}" for i, line in enumerate(selected)
            )
            return content + truncation_note
        except Exception as e:
            return f"Error: {e}"

    elif name == "grep":
        resolved = _resolve_path(params["path"], workspace)
        if not resolved:
            return "Error: path outside workspace"
        flags = params.get("flags", "")
        cmd = f"grep -n {flags} -- {_shell_quote(params['pattern'])} {_shell_quote(resolved)}"
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=15, cwd=workspace,
            )
            lines = result.stdout.splitlines(keepends=True)
            if len(lines) > 50:
                output = "".join(lines[:50]) + f"\n[... {len(lines) - 50} more matches truncated]\n"
            else:
                output = result.stdout
            return output if output.strip() else "(no matches)"
        except subprocess.TimeoutExpired:
            return "Error: grep timed out"

    elif name == "glob_files":
        base = params.get("path", ".")
        resolved = _resolve_path(base, workspace)
        if not resolved:
            return "Error: path outside workspace"
        pattern = params["pattern"]
        matches = sorted(glob_mod.glob(os.path.join(resolved, pattern), recursive=True))
        ws = Path(workspace).resolve()
        rel = []
        if len(matches) > 100:
            truncated = True
            matches = matches[:100]
        else:
            truncated = False
        for m in matches:
            try:
                rel.append(str(Path(m).relative_to(ws)))
            except ValueError:
                pass
        result = "\n".join(rel) if rel else "(no matches)"
        if truncated:
            result += f"\n[... truncated to 100 results]"
        return result

    elif name == "bash":
        cmd = params["command"]
        blocked = ["rm ", "mv ", "> ", ">> ", "chmod", "chown", "sudo", "dd "]
        if any(b in cmd for b in blocked):
            return "Error: write/delete commands blocked in eval mode"
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=30, cwd=workspace,
            )
            output = (result.stdout + result.stderr)[:10_000]
            return output if output else "(no output)"
        except subprocess.TimeoutExpired:
            return "Error: command timed out"

    elif name == "submit_answer":
        return "ANSWER_SUBMITTED"

    return f"Error: unknown tool '{name}'"


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify(answer: str, vtype: str, values: list) -> bool:
    low = answer.lower()
    if vtype == "contains_all":
        return all(v.lower() in low for v in values)
    if vtype == "contains_any":
        return any(v.lower() in low for v in values)
    if vtype == "regex_all":
        return all(re.search(v, answer, re.IGNORECASE) for v in values)
    if vtype == "regex_any":
        return any(re.search(v, answer, re.IGNORECASE) for v in values)
    return True


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

BASE_SYSTEM = """You are a code navigation agent working on a Rust workspace.
All file paths are relative to the workspace root.
Use the available tools to complete the task efficiently — minimize tool calls while ensuring accuracy.
When you have found the answer, call submit_answer with your complete findings."""


def run_task(
    client,
    task: Task,
    indexer: Indexer,
    workspace: str,
    model: str,
    run_number: int,
) -> RunResult:
    system = BASE_SYSTEM
    if indexer.system_prompt_extra:
        system += "\n\n" + indexer.system_prompt_extra

    messages = [{"role": "user", "content": task.prompt}]

    total_input = 0
    total_output = 0
    total_cache_read = 0
    tool_calls = []
    turns = 0
    answer = ""
    error = None

    start = time.monotonic()

    try:
        while turns < task.max_turns:
            turns += 1

            # Nudge agent to wrap up near the end
            cur_messages = list(messages)
            if turns >= task.max_turns - 1:
                cur_messages.append({
                    "role": "user",
                    "content": "You have 1 turn left. Call submit_answer NOW with your best answer based on what you've found so far. Do not make any more tool calls except submit_answer.",
                })

            # Retry with exponential backoff on rate limits
            response = None
            for attempt in range(5):
                try:
                    response = client.create_message(
                        model=model,
                        max_tokens=4096,
                        system=system,
                        tools=TOOL_DEFS,
                        messages=cur_messages,
                    )
                    break
                except RuntimeError as e:
                    if "429" in str(e) and attempt < 4:
                        wait = 2 ** attempt * 5  # 5, 10, 20, 40, 80 seconds
                        print(f"\n         Rate limited, waiting {wait}s...", end="", flush=True)
                        time.sleep(wait)
                    else:
                        raise
            if response is None:
                raise RuntimeError("Failed after 5 retries")

            total_input += response.input_tokens
            total_output += response.output_tokens
            total_cache_read += response.cache_read_tokens

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]

            if not tool_uses:
                if text_blocks:
                    answer = text_blocks[0].text
                break

            tool_results = []
            for tu in tool_uses:
                if tu.name == "submit_answer":
                    answer = tu.input.get("answer", "")
                    tool_calls.append(ToolCall(
                        name="submit_answer", input=tu.input,
                        output_bytes=0, duration_ms=0,
                    ))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": "Answer submitted.",
                    })
                else:
                    t0 = time.monotonic()
                    result_text = execute_tool(tu.name, tu.input, workspace)
                    dt = int((time.monotonic() - t0) * 1000)

                    tool_calls.append(ToolCall(
                        name=tu.name,
                        input=tu.input,
                        output_bytes=len(result_text.encode()),
                        duration_ms=dt,
                    ))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": result_text,
                    })

            # Serialize assistant content for message history
            assistant_content = []
            for block in response.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": tool_results})

            if answer:
                break

    except Exception as e:
        error = str(e)

    wall_time = int((time.monotonic() - start) * 1000)
    correct = verify(answer, task.verify_type, task.verify_values) if answer else False

    return RunResult(
        task_id=task.id,
        indexer=indexer.name,
        run_number=run_number,
        input_tokens=total_input,
        output_tokens=total_output,
        cache_read_tokens=total_cache_read,
        tool_calls=tool_calls,
        wall_time_ms=wall_time,
        turns=turns,
        answer=answer,
        correct=correct,
        error=error,
    )


# ---------------------------------------------------------------------------
# Indexer setup
# ---------------------------------------------------------------------------

def setup_indexer(indexer: Indexer, workspace: str):
    if not indexer.setup_command:
        return
    print(f"  Setting up '{indexer.name}'...")
    cmd = indexer.setup_command.replace("{workspace}", workspace)
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        timeout=120, cwd=workspace,
    )
    if result.returncode != 0:
        print(f"  Warning: setup failed: {result.stderr[:500]}")
    else:
        print(f"  Done. {result.stdout.strip()}")


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def result_to_dict(r: RunResult) -> dict:
    return {
        "task_id": r.task_id,
        "indexer": r.indexer,
        "run_number": r.run_number,
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "cache_read_tokens": r.cache_read_tokens,
        "total_tokens": r.input_tokens + r.output_tokens,
        "tool_call_count": len(r.tool_calls),
        "tool_calls": [
            {
                "name": tc.name,
                "input": tc.input,
                "output_bytes": tc.output_bytes,
                "duration_ms": tc.duration_ms,
            }
            for tc in r.tool_calls
        ],
        "wall_time_ms": r.wall_time_ms,
        "turns": r.turns,
        "answer": r.answer,
        "correct": r.correct,
        "error": r.error,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_summary(results: list, indexer_names: list):
    from collections import defaultdict

    by_indexer = defaultdict(list)
    for r in results:
        by_indexer[r["indexer"]].append(r)

    names = [n for n in indexer_names if n in by_indexer]
    if not names:
        return

    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    # Overall metrics
    col_w = 22
    header = f"{'Metric':<28}" + "".join(f"{n:>{col_w}}" for n in names)
    print(header)
    print("-" * len(header))

    metrics = [
        ("Avg total tokens", lambda runs: sum(r["total_tokens"] for r in runs) / len(runs)),
        ("Avg input tokens", lambda runs: sum(r["input_tokens"] for r in runs) / len(runs)),
        ("Avg output tokens", lambda runs: sum(r["output_tokens"] for r in runs) / len(runs)),
        ("Avg tool calls", lambda runs: sum(r["tool_call_count"] for r in runs) / len(runs)),
        ("Avg turns", lambda runs: sum(r["turns"] for r in runs) / len(runs)),
        ("Avg wall time (ms)", lambda runs: sum(r["wall_time_ms"] for r in runs) / len(runs)),
        ("Accuracy (%)", lambda runs: sum(1 for r in runs if r["correct"]) / len(runs) * 100),
    ]

    baseline_vals = {}
    for label, fn in metrics:
        row = f"{label:<28}"
        for i, name in enumerate(names):
            val = fn(by_indexer[name])
            if i == 0:
                baseline_vals[label] = val
            if label == "Accuracy (%)":
                row += f"{val:>{col_w - 1}.0f}%"
            else:
                # Show delta vs first indexer (baseline)
                base = baseline_vals[label]
                if i > 0 and base > 0:
                    delta_pct = (val - base) / base * 100
                    sign = "+" if delta_pct >= 0 else ""
                    row += f"{val:>{col_w - 8},.0f} ({sign}{delta_pct:.0f}%)"
                else:
                    row += f"{val:>{col_w},.0f}"
        print(row)

    # Per-task breakdown
    print(f"\n{'Task':<28}" + "".join(f"{'calls':>8}{'tok':>8}{'ok':>6}" for _ in names))
    print(f"{'':28}" + "".join(f"{n:>22}" for n in names))
    print("-" * (28 + 22 * len(names)))

    task_ids = []
    seen = set()
    for r in results:
        if r["task_id"] not in seen:
            task_ids.append(r["task_id"])
            seen.add(r["task_id"])

    for tid in task_ids:
        row = f"{tid:<28}"
        for name in names:
            task_runs = [r for r in results if r["task_id"] == tid and r["indexer"] == name]
            if task_runs:
                avg_calls = sum(r["tool_call_count"] for r in task_runs) / len(task_runs)
                avg_tok = sum(r["total_tokens"] for r in task_runs) / len(task_runs)
                correct = sum(1 for r in task_runs if r["correct"])
                row += f"{avg_calls:>8.1f}{avg_tok:>8.0f}{correct}/{len(task_runs):>4}"
            else:
                row += f"{'N/A':>22}"
        print(row)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

MODEL_ALIASES = {
    "sonnet": "claude-sonnet-4-20250514",
    "haiku": "claude-haiku-4-5-20251001",
    "opus": "claude-opus-4-6",
}

BEDROCK_MODEL_MAP = {
    "claude-sonnet-4-20250514": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "claude-haiku-4-5-20251001": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "claude-opus-4-6": "us.anthropic.claude-opus-4-6-v1:0",
}


class BedrockClient:
    """Minimal Messages API client for Bedrock with bearer token auth."""

    def __init__(self, region: str, token: str):
        self.base = f"https://bedrock-runtime.{region}.amazonaws.com"
        self.token = token
        self.http = httpx.Client(timeout=120)

    def create_message(self, *, model: str, max_tokens: int, system: str,
                       tools: list, messages: list):
        url = f"{self.base}/model/{model}/invoke"
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system,
            "tools": tools,
            "messages": messages,
        }
        resp = self.http.post(url, json=body, headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        })
        if resp.status_code != 200:
            raise RuntimeError(f"Bedrock {resp.status_code}: {resp.text[:300]}")
        return resp.json()


class MessageResponse:
    """Unified response wrapper for both Anthropic SDK and raw Bedrock."""

    def __init__(self, data: dict):
        self.content = []
        for block in (data.get("content") or []):
            self.content.append(ContentBlock(block))
        usage = data.get("usage") or {}
        self.input_tokens = usage.get("input_tokens", 0)
        self.output_tokens = usage.get("output_tokens", 0)
        self.cache_read_tokens = usage.get("cache_read_input_tokens", 0)


class ContentBlock:
    def __init__(self, data: dict):
        self.type = data.get("type")
        self.text = data.get("text", "")
        self.id = data.get("id", "")
        self.name = data.get("name", "")
        self.input = data.get("input", {})


class UnifiedClient:
    """Wraps either Anthropic SDK or raw Bedrock HTTP."""

    def __init__(self):
        self.is_bedrock = False
        self._bedrock = None
        self._anthropic = None

        use_bedrock = os.environ.get("CLAUDE_CODE_USE_BEDROCK", "").strip()
        bearer = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
        region = os.environ.get("AWS_REGION", "us-west-2")

        if use_bedrock == "1" and bearer:
            print(f"Using Bedrock (region={region})")
            self.is_bedrock = True
            self._bedrock = BedrockClient(region, bearer)
        elif HAS_ANTHROPIC:
            print("Using direct Anthropic API")
            self._anthropic = Anthropic()
        else:
            raise RuntimeError("No API credentials found. Set ANTHROPIC_API_KEY or Bedrock env vars.")

    def create_message(self, *, model: str, max_tokens: int, system: str,
                       tools: list, messages: list) -> MessageResponse:
        if self.is_bedrock:
            bedrock_model = BEDROCK_MODEL_MAP.get(model, model)
            data = self._bedrock.create_message(
                model=bedrock_model, max_tokens=max_tokens,
                system=system, tools=tools, messages=messages,
            )
            return MessageResponse(data)
        else:
            r = self._anthropic.messages.create(
                model=model, max_tokens=max_tokens,
                system=system, tools=tools, messages=messages,
            )
            # Convert SDK response to our unified format
            data = {
                "content": [
                    {"type": b.type, "text": getattr(b, "text", ""),
                     "id": getattr(b, "id", ""), "name": getattr(b, "name", ""),
                     "input": getattr(b, "input", {})}
                    for b in r.content
                ],
                "usage": {
                    "input_tokens": r.usage.input_tokens,
                    "output_tokens": r.usage.output_tokens,
                    "cache_read_input_tokens": getattr(r.usage, "cache_read_input_tokens", 0) or 0,
                },
            }
            return MessageResponse(data)


def main():
    parser = argparse.ArgumentParser(description="Eval harness for code indexers")
    parser.add_argument("--workspace", required=True, help="Path to Rust workspace")
    parser.add_argument("--tasks", default="tasks", help="Task YAML file or directory")
    parser.add_argument("--indexers", default="indexers", help="Indexer config file or directory")
    parser.add_argument("--runs", type=int, default=3, help="Runs per task×indexer")
    parser.add_argument("--model", default="sonnet", help="Model name or alias")
    parser.add_argument("--output", default="results", help="Output directory")
    parser.add_argument("--task-id", help="Run only this task ID")
    parser.add_argument("--indexer-name", help="Run only this indexer")
    parser.add_argument("--results", help="Path to results.json — report only, no run")
    args = parser.parse_args()

    # Report-only mode
    if args.results:
        with open(args.results) as f:
            data = json.load(f)
        indexer_names = list(dict.fromkeys(r["indexer"] for r in data))
        print_summary(data, indexer_names)
        return

    model = MODEL_ALIASES.get(args.model, args.model)
    client = UnifiedClient()

    # Resolve paths relative to the script's directory
    script_dir = Path(__file__).resolve().parent
    tasks_path = str(script_dir / args.tasks) if not Path(args.tasks).is_absolute() else args.tasks
    indexers_path = str(script_dir / args.indexers) if not Path(args.indexers).is_absolute() else args.indexers

    tasks = load_tasks(tasks_path)
    indexers = load_indexers(indexers_path)

    if args.task_id:
        tasks = [t for t in tasks if t.id == args.task_id]
    if args.indexer_name:
        indexers = [i for i in indexers if i.name == args.indexer_name]

    if not tasks:
        print("No tasks found!")
        sys.exit(1)
    if not indexers:
        print("No indexers found!")
        sys.exit(1)

    total_runs = len(tasks) * len(indexers) * args.runs
    print(f"Eval: {len(tasks)} tasks x {len(indexers)} indexers x {args.runs} runs = {total_runs} runs")
    print(f"Model: {model}")
    print(f"Workspace: {args.workspace}\n")

    # Setup each indexer
    for indexer in indexers:
        setup_indexer(indexer, args.workspace)

    # Run
    all_results = []
    os.makedirs(args.output, exist_ok=True)

    for task in tasks:
        for indexer in indexers:
            for run_num in range(1, args.runs + 1):
                label = f"[{indexer.name:>12}] {task.id:<28} run {run_num}/{args.runs}"
                print(f"  {label}", end="  ", flush=True)

                result = run_task(client, task, indexer, args.workspace, model, run_num)
                rd = result_to_dict(result)
                all_results.append(rd)

                status = "PASS" if result.correct else "FAIL"
                n_tools = len(result.tool_calls)
                tok = result.input_tokens + result.output_tokens
                print(f"{status}  {result.turns}turns  {n_tools}calls  {tok:,}tok  {result.wall_time_ms:,}ms")

                if result.error:
                    print(f"         Error: {result.error}")

    # Save results
    ts = time.strftime("%Y%m%d-%H%M%S")
    results_file = os.path.join(args.output, f"results-{ts}.json")
    with open(results_file, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nResults saved to {results_file}")

    # Also save as latest
    latest = os.path.join(args.output, "results-latest.json")
    with open(latest, "w") as f:
        json.dump(all_results, f, indent=2)

    indexer_names = [i.name for i in indexers]
    print_summary(all_results, indexer_names)


if __name__ == "__main__":
    main()
