"""Deterministic context selection and protocol-safe history compaction.

Budgets measure JSON characters, not model tokens. These functions never mutate
the task or raw trajectory and never inspect withheld tests or reference edits.
"""
from __future__ import annotations

import ast
import copy
import json
import re
from pathlib import PurePosixPath


def _chars(value):
    return len(json.dumps(value, ensure_ascii=False))


def _terms(text):
    return set(re.findall(r"[A-Za-z_][A-Za-z_0-9]{1,}", text.casefold()))


def _symbols(source):
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return []
    result = []

    def visit(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                result.append({"name": name, "line": child.lineno,
                               "kind": "class" if isinstance(child, ast.ClassDef) else "function"})
                visit(child, name + ".")
            else:
                visit(child, prefix)

    try:
        visit(tree)
    except RecursionError:
        return result[:24]
    return result[:24]


def _excerpt(source, terms, limit):
    if len(source) <= limit:
        return source
    lines = source.splitlines(keepends=True)
    if not lines:
        return ""
    scored = sorted(range(len(lines)), key=lambda i: (-len(_terms(lines[i]) & terms), i))
    centers = [i for i in scored[:4] if _terms(lines[i]) & terms] + [0]

    def render(selected):
        chunks, group = [], []
        for index in sorted(selected):
            if group and index != group[-1] + 1:
                chunks.append(group)
                group = []
            group.append(index)
        if group:
            chunks.append(group)
        return "\n".join(
            f"[Excerpt: lines {chunk[0] + 1}-{chunk[-1] + 1} of {len(lines)}]\n"
            + "".join(lines[i] for i in chunk) for chunk in chunks)

    selected = set()
    for center in centers:
        for radius in (0, 1, 2, 3):
            proposed = selected | set(range(max(0, center - radius), min(len(lines), center + radius + 1)))
            if len(render(proposed)) <= limit:
                selected = proposed
    if selected:
        return render(selected)
    line = centers[0]
    marker = f"[Excerpt: line {line + 1} of {len(lines)}; truncated]\n"
    return (marker + lines[line][:max(0, limit - len(marker))])[:limit]


def build_task_context(task, baseline, direct=False, max_chars=24000) -> dict:
    """Return a bounded, identical initial code selection for both strategies.

    `baseline` is used only for retrieval ranking; callers may separately attach
    bounded test output. `direct` intentionally does not change file selection.
    Symbol indexes contain at most 24 definitions per Python file.
    """
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("invalid_context_budget")
    files, issue, editable = task.get("files"), task.get("issue"), task.get("editable")
    if (not isinstance(files, dict) or not isinstance(issue, str) or not isinstance(editable, list)
            or any(not isinstance(path, str) for path in editable)
            or any(not isinstance(path, str) or not isinstance(source, str) for path, source in files.items())
            or not set(editable).issubset(files)):
        raise ValueError("invalid_task_context")
    terms = _terms(issue + "\n" + str(baseline.get("output", ""))[:20000]) if isinstance(baseline, dict) else _terms(issue)
    index = []
    for path, source in files.items():
        item = {"path": path, "lines": len(source.splitlines()), "bytes": len(source.encode("utf-8")),
                "editable": path in editable}
        if path.endswith(".py"):
            item["symbols"] = _symbols(source)
        index.append(item)
    context = {"issue": issue, "editable": list(editable), "files": dict(files), "file_index": index,
               "omitted_files": [], "context_note": "Full visible files. Symbol indexes list at most 24 definitions per file."}
    if _chars(context) <= max_chars:
        return context
    context.update(files={}, omitted_files=list(files), context_note=
                   "Bounded excerpts; markers are not source code. Use read_file for omitted files/regions. "
                   "Symbol indexes list at most 24 definitions per file. Selection is identical for direct and repair.")
    if _chars(context) > max_chars:
        raise ValueError("context_metadata_exceeds_budget")

    def is_test(path):
        name = PurePosixPath(path).name
        return name.startswith("test_") or name.endswith("_test.py")

    def rank(path):
        return (30 * (path in editable) + 16 * is_test(path) + 12 * len(_terms(path) & terms)
                + 3 * len(_terms(files[path]) & terms))

    ordered = sorted(files, key=lambda path: (-rank(path), path))
    # Reserve early opportunities for both implementation and visible tests.
    priority = [next((path for path in ordered if path in editable), None),
                next((path for path in ordered if is_test(path)), None)]
    ordered = list(dict.fromkeys(path for path in priority + ordered if path is not None))
    for position, path in enumerate(ordered):
        remaining = max_chars - _chars(context)
        allowance = min(6000, remaining // min(4, len(ordered) - position))
        while allowance >= 96:
            candidate = _excerpt(files[path], terms, allowance)
            context["files"][path] = candidate
            context["omitted_files"].remove(path)
            if _chars(context) <= max_chars:
                break
            context["files"].pop(path)
            context["omitted_files"].append(path)
            allowance //= 2
    context["omitted_files"] = [path for path in files if path not in context["files"]]
    return context


def _group_summary(group):
    calls = group[0]["tool_calls"]
    results = {message["tool_call_id"]: message for message in group[1:]}
    payload = {"total_operations": len(calls), "operations": [], "omitted_operations": len(calls)}
    prefix = "Earlier tool operations; recorded results may be stale and do not prove current code validity:\n"
    for call in calls:
        operation = {"tool": str(call.get("function", {}).get("name", "unknown"))[:64]}
        try:
            args = json.loads(call["function"].get("arguments", "{}"))
            if isinstance(args, dict) and isinstance(args.get("path"), str):
                operation["path"] = args["path"][:120]
        except (ValueError, TypeError, KeyError):
            pass
        content = results[call["id"]].get("content", "")
        facts = {}
        try:
            result = json.loads(content)
            if isinstance(result, dict):
                for key in ("ok", "passed", "submitted"):
                    if type(result.get(key)) is bool:
                        facts[key] = result[key]
                for key in ("total", "failures", "errors", "skipped"):
                    if type(result.get(key)) is int:
                        facts[key] = result[key]
                if isinstance(result.get("content"), str):
                    facts["returned_content_chars"] = len(result["content"])
                if isinstance(result.get("matches"), list):
                    facts["match_count"] = len(result["matches"])
                if result.get("error"):
                    error = result["error"]
                    if isinstance(error, str) and re.fullmatch(r"[A-Za-z0-9_:-]{1,80}", error):
                        facts["error"] = error
                    else:
                        facts["error_present"] = True
        except (ValueError, TypeError):
            facts["unparsed_output_chars"] = len(content) if isinstance(content, str) else 0
        operation["recorded_result"] = facts
        payload["operations"].append(operation)
        payload["omitted_operations"] -= 1
        if len(prefix) + _chars(payload) > 1600:
            payload["operations"].pop()
            payload["omitted_operations"] += 1
            break
    return {"role": "user", "content": prefix + json.dumps(payload, ensure_ascii=False)}


def compact_messages(messages, max_chars=60000, keep_recent_groups=3) -> tuple[list, dict]:
    """Compact only complete old assistant/tool groups, preserving raw inputs.

    Malformed or incomplete tool groups raise `malformed_tool_group`. If the
    protected initial/recent context cannot fit, return it with over_limit=True.
    Summaries contain tool names and whitelisted result fields, never assistant
    prose, reasoning fields, source text, replacements or test-output prose.
    """
    if (type(max_chars) is not int or max_chars < 1 or type(keep_recent_groups) is not int
            or keep_recent_groups < 0 or not isinstance(messages, list)
            or any(not isinstance(message, dict) for message in messages)):
        raise ValueError("invalid_compaction_arguments")
    original = copy.deepcopy(messages)
    before = _chars(original)
    groups, cursor = [], 0
    while cursor < len(original):
        message = original[cursor]
        if message.get("role") == "tool":
            raise ValueError("malformed_tool_group")
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if calls is not None and not isinstance(calls, list):
            raise ValueError("malformed_tool_group")
        if not calls:
            cursor += 1
            continue
        if any(not isinstance(call, dict) or not isinstance(call.get("id"), str)
                                             or call.get("type") != "function"
                                             or not isinstance(call.get("function"), dict)
                                             or not isinstance(call["function"].get("name"), str)
                                             or not isinstance(call["function"].get("arguments"), str)
                                             for call in calls):
            raise ValueError("malformed_tool_group")
        ids = [call["id"] for call in calls]
        end = cursor + 1
        while end < len(original) and original[end].get("role") == "tool":
            end += 1
        results = [message.get("tool_call_id") for message in original[cursor + 1:end]]
        if (any(not isinstance(result, str) for result in results) or len(set(ids)) != len(ids)
                or len(results) != len(ids) or set(results) != set(ids)):
            raise ValueError("malformed_tool_group")
        groups.append((cursor, end))
        cursor = end
    candidates = groups[:-keep_recent_groups] if keep_recent_groups else groups
    replacements = {}

    def assemble():
        result, cursor = [], 0
        while cursor < len(original):
            if cursor in replacements:
                end, summary = replacements[cursor]
                result.append(summary)
                cursor = end
            else:
                result.append(original[cursor])
                cursor += 1
        return result

    compacted = original
    for start, end in candidates:
        if _chars(compacted) <= max_chars:
            break
        summary = _group_summary(original[start:end])
        if _chars([summary]) < _chars(original[start:end]):
            replacements[start] = (end, summary)
            compacted = assemble()
    after = _chars(compacted)
    return compacted, {"before_chars": before, "after_chars": after,
                       "compacted_groups": len(replacements), "over_limit": after > max_chars}
