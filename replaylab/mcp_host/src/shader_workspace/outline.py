"""Structured views of a (possibly huge) workspace HLSL file.

A 300 KB decompiled shader cannot be understood by reading it in 15 000-char
slices, and every slice costs tokens.  The outline gives the AI the skeleton
first — declarations, functions with their line ranges and call graph, where
each cbuffer member / resource is used, how much is still anonymous — and
then exact slices (one function, or a line range) on demand.  Everything is
derived from the file text; nothing here mutates the workspace.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
import re
from typing import Any

from .flow_graph import _function_records, _tokens

OUTLINE_SCHEMA = "renderdoc-ultra-shader-workspace-outline/v1"
MAX_SLICE_LINES = 400
MAX_USE_SITES = 12
_ANONYMOUS_LOCAL = re.compile(r"\b_\d{2,}\b")
_ANONYMOUS_MEMBER = re.compile(r"\b__replaylab_\w+\b")
_CBUFFER = re.compile(r"\bcbuffer\s+(?P<name>[A-Za-z_]\w*)\s*(?::\s*register\((?P<reg>[^)]*)\))?\s*\{(?P<body>[^{}]*)\}", re.DOTALL)
_MEMBER = re.compile(r"^\s*(?P<type>[A-Za-z_]\w*(?:\s*<[^>]*>)?)\s+(?P<name>[A-Za-z_]\w*)\s*(?:\[(?P<count>[^\]]*)\])?\s*(?::\s*packoffset\((?P<offset>[^)]*)\))?\s*;", re.MULTILINE)
_RESOURCE = re.compile(
    r"^\s*(?P<type>(?:RW)?Texture\w*|(?:RW)?(?:Structured|ByteAddress|Append|Consume)?Buffer\w*|"
    r"SamplerState|SamplerComparisonState|ConstantBuffer|TextureBuffer)\s*(?:<(?P<template>[^>\n]*)>)?\s+(?P<name>[A-Za-z_]\w*)"
    r"(?:\s*\[(?P<count>[^\]]*)\])?\s*:\s*register\((?P<reg>[^)]*)\)", re.MULTILINE)
_SEMANTIC = re.compile(r"\b(?P<type>[A-Za-z_]\w*)\s+(?P<name>[A-Za-z_]\w*)\s*:\s*(?P<semantic>[A-Za-z_]\w*)")
_STRUCT = re.compile(r"\bstruct\s+(?P<name>[A-Za-z_]\w*)\s*\{(?P<body>[^{}]*)\}", re.DOTALL)
_SAMPLE = re.compile(r"\b(?P<texture>[A-Za-z_]\w*)\.(?P<method>Sample\w*|Load|Gather\w*|GetDimensions)\s*\(")
_WORD = re.compile(r"[A-Za-z_]\w*")


def _strip_comments(source: str) -> str:
    return re.sub(r"//[^\n]*|/\*.*?\*/", lambda m: " " * len(m.group()), source, flags=re.DOTALL)


def _line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def outline(source: str, *, entry_point: str = "") -> dict[str, Any]:
    """Declarations, functions, use sites and anonymity metrics for one HLSL text."""

    clean = _strip_comments(source)
    lines = source.splitlines()
    declarations: dict[str, Any] = {"cbuffers": [], "resources": [], "structs": [], "entryInputs": [], "entryOutputs": []}
    member_names: dict[str, str] = {}
    for match in _CBUFFER.finditer(clean):
        members = []
        for member in _MEMBER.finditer(match.group("body")):
            members.append({"type": member.group("type").strip(), "name": member.group("name"),
                            "count": member.group("count"), "packoffset": member.group("offset")})
            member_names[member.group("name")] = match.group("name")
        declarations["cbuffers"].append({
            "name": match.group("name"), "register": match.group("reg"), "line": _line_of(clean, match.start()),
            "memberCount": len(members),
            "anonymousMembers": sum(1 for item in members if item["name"].startswith("__replaylab_") or re.fullmatch(r"_\w*\d+", item["name"])),
            "members": members[:64], "membersTruncated": len(members) > 64,
        })
    resource_names: list[str] = []
    for match in _RESOURCE.finditer(clean):
        resource_names.append(match.group("name"))
        declarations["resources"].append({"type": match.group("type"), "template": match.group("template"),
                                          "name": match.group("name"), "count": match.group("count"),
                                          "register": match.group("reg"), "line": _line_of(clean, match.start())})
    for match in _STRUCT.finditer(clean):
        fields = [{"type": m.group("type"), "name": m.group("name"), "semantic": m.group("semantic")}
                  for m in _SEMANTIC.finditer(match.group("body"))]
        declarations["structs"].append({"name": match.group("name"), "line": _line_of(clean, match.start()),
                                        "fields": fields[:48], "fieldCount": len(fields)})
    try:
        records = _function_records(source)
    except Exception:  # noqa: BLE001 - outline must degrade, never fail
        records = []
    tokens = _tokens(source) if records else []
    callers: dict[str, list[str]] = defaultdict(list)
    for record in records:
        for callee in record.get("calls", []):
            callers[callee].append(record["name"])
    functions = []
    entry = str(entry_point or "")
    for record in records:
        start_line = record["line"]
        end_line = tokens[record["bodyEnd"]].line if record.get("bodyEnd") is not None and record["bodyEnd"] < len(tokens) else start_line
        body_text = "\n".join(lines[start_line - 1:end_line]) if 0 < start_line <= len(lines) else ""
        samples = Counter(m.group("texture") for m in _SAMPLE.finditer(body_text))
        functions.append({
            "name": record["name"], "line": start_line, "endLine": end_line, "lines": end_line - start_line + 1,
            "parameters": [p.get("name") if isinstance(p, Mapping) else str(p) for p in record.get("parameters", [])][:16],
            "calls": record.get("calls", [])[:32], "callers": callers.get(record["name"], [])[:16],
            "isEntry": record["name"] == entry,
            "anonymousLocals": len(set(_ANONYMOUS_LOCAL.findall(body_text))),
            "textureSamples": dict(samples.most_common(8)),
        })
    # use sites: cbuffer members and resources -> lines
    tracked = set(member_names) | set(resource_names)
    uses: dict[str, list[int]] = defaultdict(list)
    counts: Counter = Counter()
    if tracked:
        in_declaration_block = False
        for number, line in enumerate(lines, 1):
            stripped = line.lstrip()
            if stripped.startswith(("cbuffer", "struct")):
                in_declaration_block = "}" not in stripped
                continue
            if in_declaration_block:
                if stripped.startswith("}"):
                    in_declaration_block = False
                continue
            if stripped.startswith(("Texture", "SamplerState", "RW", "//", "ConstantBuffer", "Buffer", "StructuredBuffer", "ByteAddressBuffer")):
                continue
            for word in set(_WORD.findall(line)):
                if word in tracked:
                    counts[word] += 1
                    if len(uses[word]) < MAX_USE_SITES:
                        uses[word].append(number)
    symbol_use = {
        name: {"owner": member_names.get(name, "resource"), "uses": counts[name], "lines": uses[name]}
        for name in sorted(tracked, key=lambda item: (-counts[item], item))
    }
    anonymous_locals = set(_ANONYMOUS_LOCAL.findall(clean))
    anonymous_members = set(_ANONYMOUS_MEMBER.findall(clean))
    entry_record = next((f for f in functions if f["isEntry"]), None)
    return {
        "schema": OUTLINE_SCHEMA,
        "totals": {"lines": len(lines), "chars": len(source), "functions": len(functions),
                   "cbuffers": len(declarations["cbuffers"]), "resources": len(declarations["resources"])},
        "declarations": declarations,
        "functions": functions,
        "symbolUse": symbol_use,
        "anonymity": {
            "anonymousLocals": len(anonymous_locals),
            "anonymousCBufferMembers": len(anonymous_members),
            "unusedDeclared": sorted(name for name in tracked if counts[name] == 0)[:64],
        },
        "entry": entry_record,
        "howToRead": (
            "Do not read the whole file. Take slices: get_shader_workspace_outline(function=<name>) returns one "
            "function with line numbers; lines=<start>-<end> returns a range (<= %d lines). Name cbuffer members "
            "and resources first (symbolUse tells where they matter), then the entry function, then helpers by "
            "caller order." % MAX_SLICE_LINES
        ),
    }


def function_slice(source: str, name: str) -> dict[str, Any]:
    records = _function_records(source)
    tokens = _tokens(source)
    lines = source.splitlines()
    for record in records:
        if record["name"] != name:
            continue
        start = record["line"]
        end = tokens[record["bodyEnd"]].line if record["bodyEnd"] < len(tokens) else start
        return line_slice(source, start, end, label="function " + name, lines=lines)
    raise KeyError("no function named %r in this file" % name)


def line_slice(source: str, start: int, end: int, *, label: str = "", lines: list[str] | None = None) -> dict[str, Any]:
    lines = source.splitlines() if lines is None else lines
    start = max(1, int(start))
    end = min(len(lines), int(end))
    if end < start:
        raise ValueError("line range is empty")
    truncated = False
    if end - start + 1 > MAX_SLICE_LINES:
        end = start + MAX_SLICE_LINES - 1
        truncated = True
    text = "\n".join("%6d| %s" % (number, lines[number - 1]) for number in range(start, end + 1))
    return {"schema": OUTLINE_SCHEMA, "slice": label or ("lines %d-%d" % (start, end)),
            "startLine": start, "endLine": end, "truncated": truncated, "text": text}


__all__ = ["MAX_SLICE_LINES", "OUTLINE_SCHEMA", "function_slice", "line_slice", "outline"]
