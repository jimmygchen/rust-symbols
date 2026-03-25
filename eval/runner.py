#!/usr/bin/env python3
"""Eval harness for comparing code indexer / knowledge-graph effectiveness.

Runs navigation and comprehension tasks against a codebase using the Claude API
with tool use, measuring tokens, tool calls, wall time, and correctness across
pluggable indexer configurations.

Usage:
    python runner.py --workspace /path/to/project --runs 1
    python runner.py --workspace /path/to/project --task-id find_struct --indexer-name baseline --runs 1
    python runner.py --workspace /path/to/project --results results/results-latest.json
"""

import argparse
import concurrent.futures
import glob as glob_mod
import importlib.util
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml

try:
    import anthropic as _anthropic_mod
    from anthropic import Anthropic
    HAS_ANTHROPIC = True
except ImportError:
    _anthropic_mod = None
    HAS_ANTHROPIC = False


# ---------------------------------------------------------------------------
# API client (supports both direct Anthropic API and Bedrock)
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


class ContentBlock:
    """A single content block from an API response."""
    def __init__(self, data: dict):
        self.type = data.get("type")
        self.text = data.get("text", "")
        self.id = data.get("id", "")
        self.name = data.get("name", "")
        self.input = data.get("input", {})


class MessageResponse:
    """Unified response wrapper for both Anthropic SDK and raw Bedrock."""
    def __init__(self, data: dict):
        self.content = [ContentBlock(b) for b in (data.get("content") or [])]
        usage = data.get("usage") or {}
        self.input_tokens = usage.get("input_tokens", 0)
        self.output_tokens = usage.get("output_tokens", 0)
        self.cache_read_tokens = usage.get("cache_read_input_tokens", 0)


class BedrockClient:
    """Minimal Messages API client for Bedrock with bearer token auth."""

    def __init__(self, region: str, token: str):
        self.base = f"https://bedrock-runtime.{region}.amazonaws.com"
        self.token = token
        self.http = httpx.Client(timeout=120)

    def create_message(self, *, model: str, max_tokens: int, system: str,
                       tools: list, messages: list,
                       temperature: float = 1.0) -> dict:
        url = f"{self.base}/model/{model}/invoke"
        resp = self.http.post(url, json={
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "tools": tools,
            "messages": messages,
        }, headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        })
        if resp.status_code != 200:
            raise RuntimeError(f"Bedrock {resp.status_code}: {resp.text[:300]}")
        return resp.json()


class UnifiedClient:
    """Auto-detects Bedrock or direct Anthropic API based on env vars."""

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
            print("Using Anthropic API")
            self._anthropic = Anthropic()
        else:
            raise RuntimeError(
                "No API credentials found. Set ANTHROPIC_API_KEY or "
                "CLAUDE_CODE_USE_BEDROCK=1 + AWS_BEARER_TOKEN_BEDROCK."
            )

    def create_message(self, *, model: str, max_tokens: int, system: str,
                       tools: list, messages: list,
                       temperature: float = 1.0) -> MessageResponse:
        if self.is_bedrock:
            bedrock_model = BEDROCK_MODEL_MAP.get(model, model)
            data = self._bedrock.create_message(
                model=bedrock_model, max_tokens=max_tokens,
                system=system, tools=tools, messages=messages,
                temperature=temperature,
            )
            return MessageResponse(data)

        r = self._anthropic.messages.create(
            model=model, max_tokens=max_tokens, temperature=temperature,
            system=system, tools=tools, messages=messages,
        )
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
    peak_input_tokens: int
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
    max_turns: int = 10


@dataclass
class Indexer:
    name: str
    setup_command: Optional[str]
    system_prompt_extra: str
    files: list = field(default_factory=list)
    tool_module_path: Optional[str] = None
    tool_config: dict = field(default_factory=dict)
    # Set at runtime by setup_indexer:
    tool_module: Any = field(default=None, repr=False)
    tool_context: Any = field(default=None, repr=False)
    tool_names: set = field(default_factory=set, repr=False)


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
                max_turns=t.get("max_turns", 10),
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
            tool_module_path=data.get("tool_module"),
            tool_config=data.get("tool_config", {}),
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
                "limit": {"type": "integer", "description": "Max lines to return (capped at 200)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "grep",
        "description": "Search for a regex pattern in a file or directory. Returns matching lines with line numbers (max 50 matches).",
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
        "description": "Find files matching a glob pattern. Returns relative paths (max 100).",
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
        "description": "Run a read-only shell command (max 50KB output). Write operations are blocked.",
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

MAX_READ_LINES = 200
MAX_GREP_MATCHES = 50
MAX_GLOB_RESULTS = 100
MAX_BASH_OUTPUT = 50_000
MAX_TOOL_OUTPUT = 50_000  # Universal cap on any single tool result (bytes)

# Context budget: truncate old tool results when message history gets large.
# Sonnet has 200K context; leave headroom for system prompt + current turn.
MAX_CONTEXT_CHARS = 500_000  # ~125K tokens (chars / 4 ≈ tokens)
TRUNCATED_RESULT_PLACEHOLDER = "[output truncated to save context — use more specific queries]"


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _resolve_path(path: str, workspace: str) -> Optional[str]:
    """Resolve a relative path within the workspace. Returns None if it escapes."""
    resolved = Path(os.path.join(workspace, path)).resolve()
    ws = Path(workspace).resolve()
    if not str(resolved).startswith(str(ws)):
        return None
    return str(resolved)


def _cap_output(result: str) -> str:
    """Apply universal byte cap to any tool output."""
    if len(result) > MAX_TOOL_OUTPUT:
        return (
            result[:MAX_TOOL_OUTPUT]
            + f"\n[... output truncated at {MAX_TOOL_OUTPUT} bytes. "
            f"Use more specific queries to narrow results.]"
        )
    return result


def execute_tool(name: str, params: dict, workspace: str,
                  indexer: Optional["Indexer"] = None) -> str:
    """Execute a tool call and return the result string."""

    # Delegate to indexer tool module if it owns this tool
    if indexer and indexer.tool_module and name in indexer.tool_names:
        return _cap_output(indexer.tool_module.execute(name, params, indexer.tool_context))

    if name == "read_file":
        resolved = _resolve_path(params["path"], workspace)
        if not resolved:
            return "Error: path outside workspace"
        try:
            with open(resolved) as f:
                lines = f.readlines()
            offset = max(0, params.get("offset", 1) - 1)
            limit = min(params.get("limit", MAX_READ_LINES), MAX_READ_LINES)
            selected = lines[offset:offset + limit]
            if not selected:
                return "(empty or beyond end of file)"
            content = "".join(
                f"{offset + i + 1:>6}| {line}" for i, line in enumerate(selected)
            )
            total = len(lines)
            if offset + limit < total:
                content += f"\n[... truncated at line {offset + limit}/{total}. Use offset to read more.]\n"
            return _cap_output(content)
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
            if len(lines) > MAX_GREP_MATCHES:
                output = "".join(lines[:MAX_GREP_MATCHES])
                output += f"\n[... {len(lines) - MAX_GREP_MATCHES} more matches truncated]\n"
            else:
                output = result.stdout
            return _cap_output(output) if output.strip() else "(no matches)"
        except subprocess.TimeoutExpired:
            return "Error: grep timed out"

    elif name == "glob_files":
        base = params.get("path", ".")
        resolved = _resolve_path(base, workspace)
        if not resolved:
            return "Error: path outside workspace"
        pattern = params["pattern"]
        matches = sorted(glob_mod.glob(os.path.join(resolved, pattern), recursive=True))
        truncated = len(matches) > MAX_GLOB_RESULTS
        ws = Path(workspace).resolve()
        rel = []
        for m in matches[:MAX_GLOB_RESULTS]:
            try:
                rel.append(str(Path(m).relative_to(ws)))
            except ValueError:
                pass
        result = "\n".join(rel) if rel else "(no matches)"
        if truncated:
            result += f"\n[... truncated to {MAX_GLOB_RESULTS} results]"
        return _cap_output(result)

    elif name == "bash":
        cmd = params["command"]
        blocked = ["rm ", "mv ", "> ", ">> ", "chmod", "chown", "sudo", "dd "]
        if any(b in cmd for b in blocked):
            return "Error: write/delete commands blocked in eval mode"
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=60, cwd=workspace,
            )
            output = (result.stdout + result.stderr)[:MAX_BASH_OUTPUT]
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

BASE_SYSTEM = """\
You are a code navigation agent working on a codebase.
All file paths are relative to the workspace root.
Use the available tools to complete the task efficiently — minimize tool calls while ensuring accuracy.
You have a limited context window (~200K tokens). Prefer targeted queries over broad ones — \
large tool outputs consume your budget and older results will be truncated.
When you have found the answer, call submit_answer with your complete findings."""


def _estimate_message_chars(messages):
    """Rough estimate of total character count in messages."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += len(block.get("text", ""))
                    total += len(block.get("content", ""))
                    total += len(json.dumps(block.get("input", {}))) if block.get("input") else 0
    return total


def _truncate_messages(messages):
    """Truncate old tool results when total message size exceeds budget.

    Preserves the first message (task prompt) and the most recent 2 turns.
    Replaces tool_result content in older messages with a short placeholder.
    """
    if _estimate_message_chars(messages) <= MAX_CONTEXT_CHARS:
        return messages

    # Keep first message + last 4 messages (2 turns = assistant + user each)
    protected = {0} | {len(messages) - i - 1 for i in range(min(4, len(messages)))}

    truncated = []
    for i, msg in enumerate(messages):
        if i in protected:
            truncated.append(msg)
            continue

        content = msg.get("content")
        if isinstance(content, list):
            new_content = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    c = block.get("content", "")
                    if len(c) > 200:
                        new_content.append({**block, "content": TRUNCATED_RESULT_PLACEHOLDER})
                    else:
                        new_content.append(block)
                else:
                    new_content.append(block)
            truncated.append({**msg, "content": new_content})
        else:
            truncated.append(msg)

    return truncated


def run_task(
    client: UnifiedClient,
    task: Task,
    indexer: Indexer,
    workspace: str,
    model: str,
    run_number: int,
    temperature: float = 0.0,
) -> RunResult:
    system = BASE_SYSTEM
    if indexer.system_prompt_extra:
        system += "\n\n" + indexer.system_prompt_extra

    # Merge base tools with any indexer-provided tools
    tools = list(TOOL_DEFS)
    if indexer.tool_module:
        tools.extend(indexer.tool_module.get_tool_defs())

    messages = [{"role": "user", "content": task.prompt}]

    total_input = 0
    total_output = 0
    total_cache_read = 0
    peak_input = 0
    tool_calls = []
    turns = 0
    answer = ""
    error = None

    start = time.monotonic()

    try:
        while turns < task.max_turns:
            turns += 1

            # Truncate old tool results if context is getting large
            cur_messages = _truncate_messages(messages)

            # Nudge agent to wrap up near the end
            if turns >= task.max_turns - 1:
                cur_messages = list(cur_messages)
                cur_messages.append({
                    "role": "user",
                    "content": (
                        "You have 1 turn left. Call submit_answer NOW with your "
                        "best answer based on what you've found so far. Do not "
                        "make any more tool calls except submit_answer."
                    ),
                })

            # Retry with exponential backoff + jitter on rate limits
            response = None
            for attempt in range(5):
                try:
                    response = client.create_message(
                        model=model,
                        max_tokens=4096,
                        system=system,
                        tools=tools,
                        messages=cur_messages,
                        temperature=temperature,
                    )
                    break
                except Exception as e:
                    is_rate_limit = (
                        (isinstance(e, RuntimeError) and "429" in str(e)) or
                        (_anthropic_mod and isinstance(e, _anthropic_mod.RateLimitError))
                    )
                    if is_rate_limit and attempt < 4:
                        wait = 2 ** attempt * 5 * random.uniform(0.8, 1.5)
                        print(f"\n         Rate limited, waiting {wait:.0f}s...", end="", flush=True)
                        time.sleep(wait)
                    else:
                        raise
            if response is None:
                raise RuntimeError("Failed after 5 retries")

            total_input += response.input_tokens
            total_output += response.output_tokens
            total_cache_read += response.cache_read_tokens
            peak_input = max(peak_input, response.input_tokens)

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
                    result_text = execute_tool(tu.name, tu.input, workspace, indexer)
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
        peak_input_tokens=peak_input,
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
    if indexer.setup_command:
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

    if indexer.tool_module_path:
        print(f"  Loading tool module for '{indexer.name}'...")
        # Resolve relative to script dir
        script_dir = Path(__file__).resolve().parent
        mod_path = script_dir / indexer.tool_module_path
        spec = importlib.util.spec_from_file_location(
            f"tool_module_{indexer.name}", str(mod_path),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        indexer.tool_module = mod

        t0 = time.monotonic()
        indexer.tool_context = mod.setup(workspace, indexer.tool_config)
        dt = time.monotonic() - t0
        print(f"  Tool module loaded in {dt:.1f}s")

        # Cache the tool names this module provides
        indexer.tool_names = {td["name"] for td in mod.get_tool_defs()}


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
        "peak_input_tokens": r.peak_input_tokens,
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

def _stddev(values):
    """Population standard deviation."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def _per_run_correct(runs, tasks):
    """Group runs by run_number and return per-run correct counts."""
    by_run = defaultdict(list)
    for r in runs:
        by_run[r["run_number"]].append(r)
    return [sum(1 for r in by_run[k] if r["correct"]) for k in sorted(by_run)]


def print_summary(results: list, indexer_names: list):
    by_indexer = defaultdict(list)
    for r in results:
        by_indexer[r["indexer"]].append(r)

    names = [n for n in indexer_names if n in by_indexer]
    if not names:
        return

    # Check if we have multiple runs per task (for variance display)
    run_numbers = {r["run_number"] for r in results}
    multi_run = len(run_numbers) > 1

    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    col_w = 22 if not multi_run else 30
    header = f"{'Metric':<28}" + "".join(f"{n:>{col_w}}" for n in names)
    print(header)
    print("-" * len(header))

    def _fmt_with_stddev(val, values, is_pct=False):
        """Format a value, appending ±stddev when multi-run and stddev > 0."""
        sd = _stddev(values) if multi_run else 0.0
        if is_pct:
            if sd > 0:
                return f"{val:.0f}% ±{sd:.0f}"
            return f"{val:.0f}%"
        if sd > 0:
            return f"{val:,.0f} ±{sd:,.0f}"
        return f"{val:,.0f}"

    # Collect task IDs for per-run accuracy breakdown
    task_ids = list(dict.fromkeys(r["task_id"] for r in results))

    metrics = [
        ("Avg total tokens", lambda runs: [r["total_tokens"] for r in runs]),
        ("Avg input tokens", lambda runs: [r["input_tokens"] for r in runs]),
        ("Avg output tokens", lambda runs: [r["output_tokens"] for r in runs]),
        ("Avg peak context", lambda runs: [r.get("peak_input_tokens", 0) for r in runs]),
        ("Avg tool calls", lambda runs: [r["tool_call_count"] for r in runs]),
        ("Avg turns", lambda runs: [r["turns"] for r in runs]),
        ("Avg wall time (ms)", lambda runs: [r["wall_time_ms"] for r in runs]),
    ]

    baseline_vals = {}
    for label, values_fn in metrics:
        row = f"{label:<28}"
        for i, name in enumerate(names):
            values = values_fn(by_indexer[name])
            val = sum(values) / len(values)
            if i == 0:
                baseline_vals[label] = val
            base = baseline_vals[label]
            formatted = _fmt_with_stddev(val, values)
            if i > 0 and base > 0:
                delta_pct = (val - base) / base * 100
                sign = "+" if delta_pct >= 0 else ""
                row += f"{formatted + ' (' + sign + f'{delta_pct:.0f}%)':>{col_w}}"
            else:
                row += f"{formatted:>{col_w}}"
        print(row)

    # Accuracy row with per-run breakdown
    label = "Accuracy (%)"
    row = f"{label:<28}"
    for i, name in enumerate(names):
        runs = by_indexer[name]
        val = sum(1 for r in runs if r["correct"]) / len(runs) * 100
        if multi_run:
            per_run = _per_run_correct(runs, task_ids)
            breakdown = ",".join(str(c) for c in per_run)
            formatted = f"{val:.0f}% ({breakdown})"
        else:
            formatted = f"{val:.0f}%"
        row += f"{formatted:>{col_w}}"
    print(row)

    # Per-task breakdown
    print(f"\n{'Task':<28}" + "".join(f"{'calls':>8}{'tok':>8}{'ok':>6}" for _ in names))
    print(f"{'':28}" + "".join(f"{n:>22}" for n in names))
    print("-" * (28 + 22 * len(names)))

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

def main():
    parser = argparse.ArgumentParser(description="Eval harness for code indexers")
    parser.add_argument("--workspace", required=True, help="Path to codebase root")
    parser.add_argument("--tasks", default="tasks", help="Task YAML file or directory")
    parser.add_argument("--indexers", default="indexers", help="Indexer config file or directory")
    parser.add_argument("--runs", type=int, default=3, help="Runs per task per indexer")
    parser.add_argument("--model", default="sonnet", help="Model name or alias (sonnet/haiku/opus)")
    parser.add_argument("--output", default="results", help="Output directory for results")
    parser.add_argument("--task-id", help="Run only this task ID")
    parser.add_argument("--indexer-name", help="Run only this indexer")
    parser.add_argument("--temperature", type=float, default=0.3, help="Sampling temperature (0.3 default balances consistency and variance)")
    parser.add_argument("--parallel", type=int, default=1, help="Max parallel runs (default 1, recommend 3-4)")
    parser.add_argument("--results", help="Path to results.json (report only, no run)")
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
    print(f"Temperature: {args.temperature}")
    print(f"Parallel: {args.parallel}")
    print(f"Workspace: {args.workspace}\n")

    for indexer in indexers:
        setup_indexer(indexer, args.workspace)

    # Build flat job list and indexer lookup
    indexer_map = {i.name: i for i in indexers}
    jobs = [
        (task, indexer, run_num)
        for task in tasks
        for indexer in indexers
        for run_num in range(1, args.runs + 1)
    ]

    all_results = []
    output_dir = Path(args.output) if Path(args.output).is_absolute() else script_dir / args.output
    os.makedirs(output_dir, exist_ok=True)

    ts = time.strftime("%Y%m%d-%H%M%S")
    results_file = output_dir / f"results-{ts}.json"
    latest = output_dir / "results-latest.json"

    print_lock = threading.Lock()
    completed = [0]  # mutable counter for closure

    def _save_incremental():
        """Save current results to disk (caller must not hold print_lock)."""
        with open(results_file, "w") as f:
            json.dump(all_results, f, indent=2)
        with open(latest, "w") as f:
            json.dump(all_results, f, indent=2)

    def _run_job(job):
        task, indexer, run_num = job
        result = run_task(client, task, indexer, args.workspace, model, run_num, args.temperature)
        rd = result_to_dict(result)

        with print_lock:
            completed[0] += 1
            n = completed[0]
            status = "PASS" if result.correct else "FAIL"
            n_tools = len(result.tool_calls)
            tok = result.input_tokens + result.output_tokens
            label = f"[{n}/{total_runs}] [{indexer.name:>12}] {task.id:<28} run {run_num}/{args.runs}"
            print(f"  {label}  {status}  {result.turns}turns  {n_tools}calls  {tok:,}tok  {result.wall_time_ms:,}ms")
            if result.error:
                print(f"         Error: {result.error}")
            all_results.append(rd)
            _save_incremental()

        return rd

    if args.parallel <= 1:
        # Sequential mode — no thread overhead
        for job in jobs:
            _run_job(job)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = [pool.submit(_run_job, job) for job in jobs]
            # Wait for all; exceptions propagate on .result()
            for fut in concurrent.futures.as_completed(futures):
                fut.result()

    # Sort results for deterministic output order
    all_results.sort(key=lambda r: (r["task_id"], r["indexer"], r["run_number"]))
    _save_incremental()
    print(f"\nResults saved to {results_file}")

    indexer_names = [i.name for i in indexers]
    print_summary(all_results, indexer_names)


if __name__ == "__main__":
    main()
