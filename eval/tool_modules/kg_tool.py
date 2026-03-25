"""In-process KG query tool module for the eval runner.

Loads the knowledge graph once at setup and dispatches queries without
subprocess overhead (~1.4s saved per query).

Required tool_config keys:
  kg_script: path to kg_query.py
  kg_data: path to knowledge-graph.jsonl
"""

import importlib.util
import json
import os

_kg_module = None
_graph = None


def setup(workspace, config):
    """Import kg_query module and load the graph once."""
    global _kg_module, _graph

    kg_script = os.path.expandvars(config.get("kg_script", ""))
    kg_data = os.path.expandvars(config.get("kg_data", ""))

    if not os.path.isfile(kg_script):
        raise FileNotFoundError(f"kg_script not found: {kg_script}")
    if not os.path.isfile(kg_data):
        raise FileNotFoundError(f"kg_data not found: {kg_data}")

    # Import the module by file path (avoids polluting sys.path)
    spec = importlib.util.spec_from_file_location("kg_query", kg_script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _kg_module = mod

    # Load graph once (bypass the module's singleton cache since we control lifecycle)
    _graph = mod.load_graph(kg_data)
    return _graph


# Maps command names to either:
#   - a string (attribute name on _kg_module, called with (graph, func_ids_or_arg))
#   - a callable (called with (graph, func_ids))
_FUNC_ID_COMMANDS = {
    "callers": "query_callers",
    "callees": "query_callees",
    "reads": "query_reads",
    "writes": "query_writes",
    "shared-state": "query_shared_state",
    "guards": "query_guards",
    "effects": "query_effects",
    "function-info": "query_function_info",
    "downstream": lambda g, ids: _kg_module.query_reachable(g, ids, "downstream"),
    "upstream": lambda g, ids: _kg_module.query_reachable(g, ids, "upstream"),
}

_STRING_ARG_COMMANDS = {
    "file-functions": "query_file_functions",
    "search-functions": "query_search_functions",
    "search-effects": "query_search_effects",
    "search-guards": "query_search_guards",
    "search-locations": "query_search_locations",
    "dependents": "query_dependents",
    "stats": "query_stats",
}


def get_tool_defs():
    return [{
        "name": "kg_query",
        "description": (
            "Query the Lighthouse knowledge graph. Returns JSON with function "
            "relationships, data flow, shared state, guards, and effects.\n\n"
            "Commands:\n"
            "  Per-function: function-info, callers, callees, reads, writes, "
            "shared-state, guards, effects\n"
            "  Transitive: downstream, upstream, path\n"
            "  By file: file-functions\n"
            "  By module: dependents\n"
            "  Global search: search-functions, search-effects, search-guards, "
            "search-locations, stats"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Command name (e.g. callers, function-info, search-functions)",
                },
                "argument": {
                    "type": "string",
                    "description": "Function name, search term, file path, or 'fn1->fn2' for path command",
                },
            },
            "required": ["command", "argument"],
        },
    }]


MAX_RESULTS = 200


def _truncate_results(results):
    """Cap results list and return (results, truncated, total)."""
    total = len(results)
    if total > MAX_RESULTS:
        return results[:MAX_RESULTS], True, total
    return results, False, total


def _build_envelope(command, argument, results, matched_functions=0):
    """Build JSON response envelope with optional truncation metadata."""
    results, truncated, total = _truncate_results(results)
    envelope = {
        "query": command,
        "argument": argument,
        "matched_functions": matched_functions,
        "result_count": len(results),
        "results": results,
    }
    if truncated:
        envelope["truncated"] = True
        envelope["total_results"] = total
    return json.dumps(envelope, indent=2)


def execute(tool_name, params, context):
    """Dispatch a kg_query tool call."""
    command = params["command"]
    argument = params["argument"]
    graph = context

    # Handle 'path' command specially (needs two function resolutions)
    if command == "path":
        if "->" not in argument:
            return json.dumps({"error": "path command requires 'fn1->fn2' format"})
        src_q, tgt_q = argument.split("->", 1)
        src_funcs = _kg_module.resolve_function(graph, src_q.strip())
        tgt_funcs = _kg_module.resolve_function(graph, tgt_q.strip())
        src_ids = {f["id"] for f in src_funcs}
        tgt_ids = {f["id"] for f in tgt_funcs}
        results = _kg_module.query_path(graph, src_ids, tgt_ids)
        return _build_envelope(command, argument, results,
                               matched_functions=len(src_funcs) + len(tgt_funcs))

    # Commands that take func_ids (resolved from argument)
    if command in _FUNC_ID_COMMANDS:
        funcs = _kg_module.resolve_function(graph, argument)
        func_ids = {f["id"] for f in funcs}
        handler = _FUNC_ID_COMMANDS[command]
        if callable(handler):
            results = handler(graph, func_ids)
        else:
            results = getattr(_kg_module, handler)(graph, func_ids)
        return _build_envelope(command, argument, results,
                               matched_functions=len(funcs))

    # Commands that take a string argument directly
    if command in _STRING_ARG_COMMANDS:
        fn = getattr(_kg_module, _STRING_ARG_COMMANDS[command])
        results = fn(graph, argument)
        return _build_envelope(command, argument, results)

    return json.dumps({"error": f"Unknown command: {command}"})
