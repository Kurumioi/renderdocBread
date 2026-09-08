"""Host-applied structural refactoring of decompiled HLSL.

A 300 KB single-function shader cannot be rewritten by an AI in one response,
so readable structure is built the way the Host already applies renames: the
AI decides *where* a cohesive computation starts and ends and *what* it is
called; the Host performs the mechanical extraction, infers the interface
from data flow, re-indents, replaces the range with a call, and the caller
compiles the result before anything is kept.  Operations are recorded in the
organization session and replayed deterministically on top of the renamed
baseline, so renames, extractions and comments compose in a fixed order.

Supported operation: ``extract`` (a complete statement range inside one
function body becomes a new function).  Everything that cannot be proven safe
by text analysis — a ``return`` inside the range, ``break``/``continue`` that
escape it, a range cutting through an ``if``/``else`` or a multi-line
statement, array locals — is refused with the exact reason and the nearest
acceptable boundaries.  The compile gate remains the final judge.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

from .flow_graph import _function_records, _tokens

RESTRUCTURE_SCHEMA = "renderdoc-ultra-shader-restructure/v1"
MAX_PARAMETERS = 12
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_KEYWORDS = frozenset({
    "if", "else", "for", "while", "do", "switch", "case", "default", "break", "continue", "return",
    "discard", "true", "false", "const", "static", "in", "out", "inout", "uniform", "precise",
    "struct", "cbuffer", "register", "packoffset", "void", "row_major", "column_major", "nointerpolation",
    "linear", "centroid", "sample", "noperspective", "unroll", "loop", "branch", "flatten", "groupshared",
})
_TYPE_LIKE = re.compile(r"^(?:(?:float|half|double|int|uint|bool|min16float|min16int|min16uint|min10float|float16_t|int16_t|uint16_t|int64_t|uint64_t|dword)(?:[1-4](?:x[1-4])?)?|matrix|vector)$")
_ASSIGN_OPS = {"+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>"}


class RestructureError(ValueError):
    """The requested structural edit is not provably safe."""


def _newline(source: str) -> str:
    return "\r\n" if "\r\n" in source else "\n"


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _matching(tokens: Sequence[Any], open_index: int, opening: str, closing: str) -> int:
    depth = 0
    for index in range(open_index, len(tokens)):
        text = tokens[index].text
        if text == opening:
            depth += 1
        elif text == closing:
            depth -= 1
            if depth == 0:
                return index
    return -1


def _declarations(tokens: Sequence[Any], start: int, end: int) -> dict[str, dict[str, Any]]:
    """Local declarations ``TYPE name`` inside one function body (token index range)."""

    found: dict[str, dict[str, Any]] = {}
    index = start + 1
    while index < end:
        token = tokens[index]
        previous = tokens[index - 1].text if index > 0 else ""
        statement_start = previous in {";", "{", "}"} or (previous == "(" and index >= 2 and tokens[index - 2].text == "for")
        if statement_start and _IDENT.fullmatch(token.text) and token.text not in _KEYWORDS:
            type_end = index
            if index + 1 < end and tokens[index + 1].text == "<":
                close = _matching(tokens, index + 1, "<", ">")
                if close > 0:
                    type_end = close
            cursor = type_end + 1
            # qualifiers such as ``const float x`` / ``precise float y``
            if token.text in {"const", "precise", "static"}:
                index += 1
                continue
            if cursor < end and _IDENT.fullmatch(tokens[cursor].text) and tokens[cursor].text not in _KEYWORDS:
                after = tokens[cursor + 1].text if cursor + 1 < end else ""
                if after in {"=", ";", ","} or after == "[":
                    type_text = "".join(t.text for t in tokens[index:type_end + 1])
                    is_array = after == "["
                    name = tokens[cursor].text
                    found.setdefault(name, {"type": type_text, "line": tokens[cursor].line, "array": is_array})
                    # ``float a = 1.0f, b = 2.0f;``
                    scan = cursor + 1
                    while scan < end and tokens[scan].text != ";":
                        if tokens[scan].text == "," and scan + 1 < end and _IDENT.fullmatch(tokens[scan + 1].text):
                            found.setdefault(tokens[scan + 1].text, {"type": type_text, "line": tokens[scan + 1].line, "array": False})
                        if tokens[scan].text in {"(", "["}:
                            close = _matching(tokens, scan, tokens[scan].text, ")" if tokens[scan].text == "(" else "]")
                            scan = close if close > scan else scan
                        scan += 1
                    index = cursor + 1
                    continue
        index += 1
    return found


def _parameter_types(source_tokens: Sequence[Any], record: Mapping[str, Any], tokens: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """Parameter name -> {type, mode} of the enclosing function signature."""

    # find '(' after the name token
    name_index = next((i for i, t in enumerate(tokens) if t.line == record["line"] and t.column == record["column"]), None)
    result: dict[str, dict[str, Any]] = {}
    if name_index is None or name_index + 1 >= len(tokens) or tokens[name_index + 1].text != "(":
        return result
    close = _matching(tokens, name_index + 1, "(", ")")
    group: list[str] = []
    depth = 0
    for token in tokens[name_index + 2:close]:
        if token.text in {"(", "<", "["}:
            depth += 1
        elif token.text in {")", ">", "]"}:
            depth -= 1
        if token.text == "," and depth == 0:
            _record_parameter(group, result)
            group = []
        else:
            group.append(token.text)
    _record_parameter(group, result)
    return result


def _record_parameter(words: list[str], result: dict[str, dict[str, Any]]) -> None:
    words = list(words)
    if ":" in words:
        words = words[: words.index(":")]
    if "=" in words:
        words = words[: words.index("=")]
    mode = "inout" if "inout" in words else "out" if "out" in words else "in"
    words = [w for w in words if w not in {"in", "out", "inout", "const", "uniform", "precise"}]
    identifiers = [w for w in words if _IDENT.fullmatch(w)]
    if len(identifiers) < 2:
        return
    name = identifiers[-1]
    type_text = "".join(words[: len(words) - 1]) if words and words[-1] == name else "".join(words[:-1])
    result[name] = {"type": type_text, "mode": mode}


def extract_function(source: str, start_line: int, end_line: int, name: str, *, comment: str = "") -> dict[str, Any]:
    """Extract lines ``start_line..end_line`` (1-based, inclusive) into ``name``."""

    if not isinstance(name, str) or _IDENT.fullmatch(name) is None or name in _KEYWORDS or _TYPE_LIKE.fullmatch(name):
        raise RestructureError("function name must be a valid HLSL identifier")
    if re.search(r"\b%s\b" % re.escape(name), source):
        raise RestructureError("function name %r already exists in the source; pick another" % name)
    newline = _newline(source)
    lines = source.split(newline)
    if not (1 <= start_line <= end_line <= len(lines)):
        raise RestructureError("line range %d-%d is outside the file (%d lines)" % (start_line, end_line, len(lines)))
    tokens = _tokens(source)
    records = _function_records(source)
    enclosing = None
    for record in records:
        first = tokens[record["bodyStart"]].line
        last = tokens[record["bodyEnd"]].line
        if first < start_line and end_line < last:
            if enclosing is None or record["bodyStart"] > enclosing["bodyStart"]:
                enclosing = record
    if enclosing is None:
        raise RestructureError("the range must lie strictly inside one function body (not including its braces)")
    body_start, body_end = enclosing["bodyStart"], enclosing["bodyEnd"]
    body_tokens = list(range(body_start + 1, body_end))
    range_indices = [i for i in body_tokens if start_line <= tokens[i].line <= end_line]
    if not range_indices:
        raise RestructureError("the range contains no code")
    before_indices = [i for i in body_tokens if tokens[i].line < start_line]
    after_indices = [i for i in body_tokens if tokens[i].line > end_line]

    # ---- statement boundaries and block balance
    depth = 0
    min_depth = 0
    for i in range_indices:
        text = tokens[i].text
        if text == "{":
            depth += 1
        elif text == "}":
            depth -= 1
            min_depth = min(min_depth, depth)
    if depth != 0 or min_depth < 0:
        raise RestructureError("the range must contain complete blocks: braces opened and closed inside it")
    previous_text = tokens[before_indices[-1]].text if before_indices else "{"
    if previous_text not in {";", "{", "}"}:
        raise RestructureError("the range starts in the middle of a statement (line %d); start at a statement boundary"
                               % start_line)
    first_text = tokens[range_indices[0]].text
    last_text = tokens[range_indices[-1]].text
    if first_text == "else":
        raise RestructureError("the range starts with 'else'; include the matching 'if' or start after the else block")
    if last_text not in {";", "}"}:
        raise RestructureError("the range ends in the middle of a statement (line %d); end at ';' or '}'" % end_line)
    next_text = tokens[after_indices[0]].text if after_indices else "}"
    if next_text == "else":
        raise RestructureError("the range ends just before an 'else'; include the else block or stop before the 'if'")
    # a '{' as the previous token that belongs to an if/for header we split
    if previous_text == "{" and before_indices:
        # find the token before the '{': ')' means we are the first statement of a block – allowed
        pass

    # ---- control-flow hazards
    loop_depths: list[int] = []
    depth = 0
    pending_loop = False
    for i in range_indices:
        text = tokens[i].text
        if text in {"for", "while", "do", "switch"}:
            pending_loop = True
        if text == "{":
            depth += 1
            if pending_loop:
                loop_depths.append(depth)
                pending_loop = False
        elif text == "}":
            if loop_depths and loop_depths[-1] == depth:
                loop_depths.pop()
            depth -= 1
        elif text == ";" and pending_loop and depth == 0:
            pending_loop = False  # single-statement loop body without braces: keep conservative
        if text == "return":
            raise RestructureError("the range contains 'return' (line %d); a return cannot move into a helper" % tokens[i].line)
        if text in {"break", "continue"} and not loop_depths:
            raise RestructureError("'%s' at line %d escapes the range; include its whole loop/switch" % (text, tokens[i].line))

    # ---- data flow
    declarations = _declarations(tokens, body_start, body_end)
    parameters = _parameter_types(tokens, enclosing, tokens)
    for pname, pinfo in parameters.items():
        declarations.setdefault(pname, {"type": pinfo["type"], "line": enclosing["line"], "array": False, "param": pinfo["mode"]})
    user_functions = {record["name"] for record in records}

    def classify(indices: list[int]) -> tuple[dict[str, dict[str, Any]], set[str]]:
        seen: dict[str, dict[str, Any]] = {}
        for pos, i in enumerate(indices):
            token = tokens[i]
            if not _IDENT.fullmatch(token.text) or token.text not in declarations:
                continue
            if i > 0 and tokens[i - 1].text in {".", "::"}:
                continue  # member access such as x.y where y is coincidentally a local name
            entry = seen.setdefault(token.text, {"read": False, "assigned": False, "first": token.line})
            # skip member / index chain
            cursor = i + 1
            while cursor < len(tokens) and tokens[cursor].text in {".", "["}:
                if tokens[cursor].text == ".":
                    cursor += 2
                else:
                    close = _matching(tokens, cursor, "[", "]")
                    cursor = close + 1 if close > cursor else cursor + 1
            nxt = tokens[cursor].text if cursor < len(tokens) else ""
            nxt2 = tokens[cursor + 1].text if cursor + 1 < len(tokens) else ""
            prev = tokens[i - 1].text if i > 0 else ""
            if nxt == "=" and nxt2 != "=":
                entry["assigned"] = True
                if cursor != i + 1:
                    entry["read"] = True  # partial write (x.y = / x[i] =) keeps the rest
            elif nxt in _ASSIGN_OPS and nxt2 == "=":
                entry["assigned"] = True
                entry["read"] = True
            elif nxt in {"++", "--"} or prev in {"++", "--"}:
                entry["assigned"] = True
                entry["read"] = True
            else:
                entry["read"] = True
                # argument of a user function: may be written through out/inout
                call_depth = 0
                for back in range(i - 1, max(body_start, i - 200), -1):
                    text = tokens[back].text
                    if text == ")":
                        call_depth += 1
                    elif text == "(":
                        if call_depth == 0:
                            callee = tokens[back - 1].text if back > 0 else ""
                            if callee in user_functions:
                                entry["assigned"] = True
                            break
                        call_depth -= 1
                    elif text in {";", "{", "}"} and call_depth == 0:
                        break
        return seen, set(seen)

    used_in, _ = classify(range_indices)
    used_before, before_names = classify(before_indices)
    used_after, after_names = classify(after_indices)
    declared_in = {n for n, d in declarations.items() if "param" not in d and start_line <= d["line"] <= end_line}
    declared_before = {n for n, d in declarations.items() if "param" in d or d["line"] < start_line}

    inputs: list[str] = []
    inouts: list[str] = []
    outputs: list[str] = []
    for var, use in sorted(used_in.items(), key=lambda item: item[1]["first"]):
        if var in declared_in:
            if var in after_names:
                outputs.append(var)
            continue
        if var not in declared_before:
            continue
        if use["assigned"]:
            inouts.append(var)
        elif use["read"]:
            inputs.append(var)
    for var in inputs + inouts + outputs:
        if declarations[var].get("array"):
            raise RestructureError("local array %r crosses the range boundary; arrays cannot be passed by the extractor" % var)
    if len(inputs) + len(inouts) + len(outputs) > MAX_PARAMETERS:
        raise RestructureError(
            "the range would need %d parameters (%s); choose a more cohesive range or extract an inner part first"
            % (len(inputs) + len(inouts) + len(outputs), ", ".join(inputs + inouts + outputs)))

    return_var = outputs[0] if len(outputs) == 1 else None
    out_params = [] if return_var else outputs
    signature_parts = ["%s %s" % (declarations[v]["type"], v) for v in inputs]
    signature_parts += ["inout %s %s" % (declarations[v]["type"], v) for v in inouts]
    signature_parts += ["out %s %s" % (declarations[v]["type"], v) for v in out_params]
    return_type = declarations[return_var]["type"] if return_var else "void"

    base_indent = _indent_of(lines[start_line - 1])
    body_lines: list[str] = []
    for raw in lines[start_line - 1:end_line]:
        stripped = raw[len(base_indent):] if raw.startswith(base_indent) else raw.lstrip()
        body_lines.append(("    " + stripped) if stripped.strip() else "")
    function_lines: list[str] = []
    if comment.strip():
        for text in comment.strip().splitlines()[:6]:
            function_lines.append("// " + text.strip())
    function_lines.append("%s %s(%s)" % (return_type, name, ", ".join(signature_parts)))
    function_lines.append("{")
    function_lines.extend(body_lines)
    if return_var:
        function_lines.append("    return %s;" % return_var)
    function_lines.append("}")
    function_lines.append("")

    call_args = ", ".join(inputs + inouts + out_params)
    replacement: list[str] = []
    for var in out_params:
        replacement.append("%s%s %s;" % (base_indent, declarations[var]["type"], var))
    if return_var:
        replacement.append("%s%s %s = %s(%s);" % (base_indent, declarations[return_var]["type"], return_var, name, call_args))
    else:
        replacement.append("%s%s(%s);" % (base_indent, name, call_args))

    enclosing_line = enclosing["line"]
    # keep attached comment lines above the enclosing function with it
    insert_at = enclosing_line - 1
    while insert_at > 0 and lines[insert_at - 1].lstrip().startswith("//"):
        insert_at -= 1
    new_lines = lines[:insert_at] + function_lines + lines[insert_at:start_line - 1] + replacement + lines[end_line:]
    added = len(function_lines)
    info = {
        "schema": RESTRUCTURE_SCHEMA,
        "operation": "extract",
        "name": name,
        "enclosingFunction": enclosing["name"],
        "sourceLines": [start_line, end_line],
        "extractedLines": end_line - start_line + 1,
        "returnType": return_type,
        "returns": return_var,
        "inputs": inputs,
        "inouts": inouts,
        "outParams": out_params,
        "insertedAtLine": insert_at + 1,
        "newFunctionLines": [insert_at + 1, insert_at + added - 1],
        "callLine": start_line + added,
    }
    return {"source": newline.join(new_lines), "info": info}


def apply_operations(source: str, operations: Sequence[Mapping[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Replay recorded operations in order; raises RestructureError on the first failure."""

    receipts: list[dict[str, Any]] = []
    text = source
    for position, operation in enumerate(operations):
        kind = str(operation.get("operation", ""))
        if kind == "extract":
            result = extract_function(
                text,
                int(operation["startLine"]),
                int(operation["endLine"]),
                str(operation["name"]),
                comment=str(operation.get("comment", "") or ""),
            )
            text = result["source"]
            receipt = dict(result["info"])
            receipt["position"] = position
            receipts.append(receipt)
        else:
            raise RestructureError("unknown structural operation %r" % kind)
    return text, receipts


def structure_report(source: str, entry_point: str = "") -> dict[str, Any]:
    """Per-function metrics and heuristic flags for the AI's self-review pass."""

    from .outline import outline

    view = outline(source, entry_point=entry_point)
    tracked: dict[str, str] = {}
    for block in view["declarations"]["cbuffers"]:
        for member in block.get("members", []):
            tracked[member["name"]] = block["name"]
    for resource in view["declarations"]["resources"]:
        tracked[resource["name"]] = "resource"
    lines = source.splitlines()
    functions = []
    for item in view["functions"]:
        body = "\n".join(lines[item["line"] - 1:item["endLine"]])
        words = set(re.findall(r"[A-Za-z_]\w*", body))
        globals_used = sorted(name for name in words if name in tracked)
        item = {**item, "globalsUsed": globals_used[:24], "globalsUsedCount": len(globals_used)}
        flags = []
        params = len(item.get("parameters") or [])
        if item["lines"] > 120:
            flags.append("large: split at the next computation boundary")
        if item["lines"] < 4 and item["name"] != entry_point:
            flags.append("tiny: consider inlining or merging with its caller")
        if params > 8:
            flags.append("many-parameters: the range probably mixes two computations")
        if item.get("anonymousLocals", 0) > 0:
            flags.append("anonymous-locals: %d temporaries still need names" % item["anonymousLocals"])
        if re.fullmatch(r"(?i)(stage|part|section|block|chunk|step|func|helper)\d*", item["name"] or ""):
            flags.append("placeholder-name: name the computation, not its position")
        if len(globals_used) > 12 and item["name"] != entry_point:
            flags.append("many-globals: touches %d cbuffer members/resources; probably several concerns" % len(globals_used))
        functions.append({**item, "flags": flags})
    return {
        "schema": RESTRUCTURE_SCHEMA,
        "totals": view["totals"],
        "anonymity": view["anonymity"],
        "functions": functions,
        "flagged": sum(1 for item in functions if item["flags"]),
    }


__all__ = ["RESTRUCTURE_SCHEMA", "RestructureError", "apply_operations", "extract_function", "structure_report"]
