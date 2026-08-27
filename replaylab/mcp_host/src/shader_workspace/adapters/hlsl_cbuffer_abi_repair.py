"""Fail-closed CBuffer ABI repair for a narrow SPIRV-Cross HLSL shape.

SPIRV-Cross commonly represents a stripped DXIL CBuffer as one rounded-up
``float4 member[N] : packoffset(c0)`` array.  That is useful as an algorithmic
view, but it changes a non-16-byte buffer envelope and loses integer load
overloads.  This module repairs only that mechanically provable case from a
captured CBuffer envelope and a hash-bound DXIL physical-access signature.

It deliberately does *not* repair resources, samplers, entry signatures,
interpolation, root signatures, or shader algorithms.  It also does not infer
source member names, matrices, structures, signedness, or dynamic indices.
Those concerns require independent physical-interface and semantic gates.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .dxil_access_signature import (
    DxilCBufferAccessBlock,
    DxilCBufferAccessSignature,
)

MAX_HLSL_CHARS = 64 * 1024 * 1024
MAX_CBUFFERS = 16_384
MAX_REWRITES = 262_144
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_CBUFFER = re.compile(
    rf"\bcbuffer\s+(?P<block>{_IDENTIFIER})\s*:\s*"
    rf"register\s*\(\s*b(?P<bind>\d+)\s*,\s*space(?P<space>\d+)\s*\)\s*"
    rf"\{{(?P<body>.*?)\}}\s*;",
    re.DOTALL,
)
_SIMPLE_BODY = re.compile(
    rf"\s*float4\s+(?P<member>{_IDENTIFIER})\s*"
    rf"\[\s*(?P<count>\d+)(?:[uU])?\s*\]\s*:\s*"
    rf"packoffset\s*\(\s*c0(?:\.x)?\s*\)\s*;\s*",
    re.DOTALL,
)
_COMPONENT_INDEX = {
    "x": 0,
    "r": 0,
    "y": 1,
    "g": 1,
    "z": 2,
    "b": 2,
    "w": 3,
    "a": 3,
}
_COMPONENT_NAME = "xyzw"


class HlslCBufferAbiRepairError(ValueError):
    """The HLSL and physical evidence do not admit the narrow safe repair."""


@dataclass(frozen=True)
class RepairedCBuffer:
    space: int
    bind: int
    byte_size: int
    original_member: str
    access_count: int
    tail_guard_added: bool


@dataclass(frozen=True)
class HlslCBufferAbiRepairResult:
    hlsl: str
    repaired_blocks: tuple[RepairedCBuffer, ...]


@dataclass(frozen=True)
class _BaselineBlock:
    space: int
    bind: int
    bind_array_size: int
    byte_size: int
    layout_completeness: str


@dataclass(frozen=True)
class _Declaration:
    start: int
    end: int
    block_name: str
    member_name: str
    count: int
    space: int
    bind: int


@dataclass(frozen=True)
class _Edit:
    start: int
    end: int
    replacement: str


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HlslCBufferAbiRepairError(f"{label} is invalid")
    return value


def _baseline_blocks(values: Sequence[Mapping[str, Any]]) -> dict[tuple[int, int], _BaselineBlock]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise HlslCBufferAbiRepairError("baseline_constant_blocks must be a sequence")
    if len(values) > MAX_CBUFFERS:
        raise HlslCBufferAbiRepairError("baseline CBuffer count exceeds its limit")
    result: dict[tuple[int, int], _BaselineBlock] = {}
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise HlslCBufferAbiRepairError(
                f"baseline_constant_blocks[{index}] is not an object"
            )
        block = _BaselineBlock(
            _integer(value.get("space"), "baseline space"),
            _integer(value.get("bind"), "baseline bind"),
            _integer(
                value.get("bindArraySize"),
                "baseline bindArraySize",
                minimum=1,
            ),
            _integer(value.get("byteSize"), "baseline byteSize", minimum=1),
            str(value.get("layoutCompleteness", "") or "").strip().casefold(),
        )
        if block.layout_completeness not in {
            "complete",
            "partial",
            "opaque",
            "ambiguous",
        }:
            raise HlslCBufferAbiRepairError(
                "baseline layoutCompleteness is invalid"
            )
        key = (block.space, block.bind)
        if key in result:
            raise HlslCBufferAbiRepairError("duplicate baseline CBuffer binding")
        result[key] = block
    return result


def _declarations(source: str) -> dict[tuple[int, int], _Declaration]:
    declarations: dict[tuple[int, int], _Declaration] = {}
    for match in _CBUFFER.finditer(source):
        if len(declarations) >= MAX_CBUFFERS:
            raise HlslCBufferAbiRepairError("HLSL CBuffer count exceeds its limit")
        space = int(match.group("space"), 10)
        bind = int(match.group("bind"), 10)
        key = (space, bind)
        if key in declarations:
            raise HlslCBufferAbiRepairError("duplicate HLSL CBuffer binding")
        body = _SIMPLE_BODY.fullmatch(match.group("body"))
        # Retain non-simple declarations in the index.  They are harmless when
        # their captured layout is complete, but an ambiguous block below must
        # match this exact shape or the repair fails.
        member = body.group("member") if body is not None else ""
        count = int(body.group("count"), 10) if body is not None else 0
        declarations[key] = _Declaration(
            match.start(),
            match.end(),
            match.group("block"),
            member,
            count,
            space,
            bind,
        )
    return declarations


def _physical_accesses(
    baseline: _BaselineBlock,
    access: DxilCBufferAccessBlock | None,
) -> dict[int, str]:
    if access is None:
        raise HlslCBufferAbiRepairError(
            "ambiguous CBuffer has no DXIL access evidence"
        )
    if (
        baseline.bind_array_size != 1
        or access.bind_array_size != 1
        or access.space != baseline.space
        or access.bind != baseline.bind
        or access.byte_size != baseline.byte_size
        or not access.accesses
    ):
        raise HlslCBufferAbiRepairError(
            "ambiguous CBuffer access envelope contradicts its baseline"
        )
    if baseline.byte_size % 4:
        raise HlslCBufferAbiRepairError(
            "simple HLSL repair requires a four-byte CBuffer envelope"
        )
    result: dict[int, str] = {}
    for item in access.accesses:
        if (
            item.byte_width != 4
            or item.byte_offset % 4
            or item.return_type not in {"f32", "i32"}
            or item.byte_offset + item.byte_width > baseline.byte_size
        ):
            raise HlslCBufferAbiRepairError(
                "simple HLSL repair supports aligned f32/i32 accesses only"
            )
        if item.byte_offset in result:
            raise HlslCBufferAbiRepairError(
                "CBuffer byte lane has multiple physical access types"
            )
        result[item.byte_offset] = item.return_type
    return result


def _field_name(space: int, bind: int, offset: int, return_type: str) -> str:
    register = offset // 16
    component = _COMPONENT_NAME[(offset % 16) // 4]
    return (
        f"__replaylab_s{space}_b{bind}_c{register}_{component}_{return_type}"
    )


def _tail_field_name(space: int, bind: int, offset: int) -> str:
    register = offset // 16
    component = _COMPONENT_NAME[(offset % 16) // 4]
    return f"__replaylab_s{space}_b{bind}_c{register}_{component}_tail"


def _lane_as_float(
    fields: Mapping[int, str],
    *,
    space: int,
    bind: int,
    offset: int,
) -> str:
    return_type = fields[offset]
    name = _field_name(space, bind, offset, return_type)
    return name if return_type == "f32" else f"asfloat({name})"


def _replacement_expression(
    *,
    register: int,
    swizzle: str | None,
    fields: Mapping[int, str],
    space: int,
    bind: int,
) -> tuple[str, set[int]]:
    base = register * 16
    if swizzle:
        offsets = [base + _COMPONENT_INDEX[item.casefold()] * 4 for item in swizzle]
        if any(offset not in fields for offset in offsets):
            raise HlslCBufferAbiRepairError(
                "HLSL references a CBuffer lane absent from DXIL access evidence"
            )
        values = [
            _lane_as_float(
                fields,
                space=space,
                bind=bind,
                offset=offset,
            )
            for offset in offsets
        ]
        if len(values) == 1:
            return values[0], set(offsets)
        return f"float{len(values)}({', '.join(values)})", set(offsets)

    values: list[str] = []
    consumed: set[int] = set()
    for lane in range(4):
        offset = base + lane * 4
        if offset in fields:
            values.append(
                _lane_as_float(
                    fields,
                    space=space,
                    bind=bind,
                    offset=offset,
                )
            )
            consumed.add(offset)
        else:
            values.append("0.0f")
    if not consumed:
        raise HlslCBufferAbiRepairError(
            "HLSL references a register absent from DXIL access evidence"
        )
    return f"float4({', '.join(values)})", consumed


def _reference_edits(
    source: str,
    declaration: _Declaration,
    declaration_ranges: Sequence[tuple[int, int]],
    fields: Mapping[int, str],
) -> tuple[list[_Edit], set[int]]:
    member = declaration.member_name
    reference = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(member)}\s*"
        rf"\[\s*(?P<index>\d+)(?:[uU])?\s*\]"
        rf"(?:\s*\.\s*(?P<swizzle>[xyzwrgbaXYZWRGBA]{{1,4}}))?"
    )
    token = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(member)}(?![A-Za-z0-9_])"
    )

    def inside_declaration(position: int) -> bool:
        return any(start <= position < end for start, end in declaration_ranges)

    matches = [
        match
        for match in reference.finditer(source)
        if not inside_declaration(match.start())
    ]
    by_start = {match.start(): match for match in matches}
    for occurrence in token.finditer(source):
        if inside_declaration(occurrence.start()):
            continue
        if occurrence.start() not in by_start:
            raise HlslCBufferAbiRepairError(
                "CBuffer member has a dynamic or unsupported HLSL use"
            )
    if not matches:
        raise HlslCBufferAbiRepairError(
            "DXIL access evidence has no matching HLSL CBuffer use"
        )
    edits: list[_Edit] = []
    consumed: set[int] = set()
    for match in matches:
        register = int(match.group("index"), 10)
        if register >= declaration.count:
            raise HlslCBufferAbiRepairError(
                "HLSL CBuffer index exceeds its declared array"
            )
        replacement, offsets = _replacement_expression(
            register=register,
            swizzle=match.group("swizzle"),
            fields=fields,
            space=declaration.space,
            bind=declaration.bind,
        )
        edits.append(_Edit(match.start(), match.end(), replacement))
        consumed.update(offsets)
        if len(edits) > MAX_REWRITES:
            raise HlslCBufferAbiRepairError("HLSL rewrite count exceeds its limit")
    return edits, consumed


def _declaration_text(
    declaration: _Declaration,
    baseline: _BaselineBlock,
    fields: Mapping[int, str],
    newline: str,
) -> tuple[str, bool]:
    lines = [
        (
            f"cbuffer {declaration.block_name} : "
            f"register(b{declaration.bind}, space{declaration.space})"
        ),
        "{",
    ]
    for offset, return_type in sorted(fields.items()):
        register = offset // 16
        component = _COMPONENT_NAME[(offset % 16) // 4]
        hlsl_type = "float" if return_type == "f32" else "uint"
        lines.append(
            f"    {hlsl_type} "
            f"{_field_name(declaration.space, declaration.bind, offset, return_type)} "
            f": packoffset(c{register}.{component});"
        )
    tail_offset = baseline.byte_size - 4
    tail_added = tail_offset not in fields
    if tail_added:
        register = tail_offset // 16
        component = _COMPONENT_NAME[(tail_offset % 16) // 4]
        tail_name = _tail_field_name(
            declaration.space, declaration.bind, tail_offset
        )
        lines.append(
            f"    uint {tail_name} : packoffset(c{register}.{component});"
        )
    lines.extend(("};",))
    return newline.join(lines), tail_added


def _apply_edits(source: str, edits: Sequence[_Edit]) -> str:
    ordered = sorted(edits, key=lambda item: (item.start, item.end))
    previous_end = -1
    for item in ordered:
        if item.start < 0 or item.end < item.start or item.end > len(source):
            raise HlslCBufferAbiRepairError("HLSL edit range is invalid")
        if item.start < previous_end:
            raise HlslCBufferAbiRepairError("HLSL repair edits overlap")
        previous_end = item.end
    result = source
    for item in reversed(ordered):
        result = result[: item.start] + item.replacement + result[item.end :]
    return result


def repair_spirv_cross_hlsl_cbuffers(
    source: str,
    *,
    baseline_constant_blocks: Sequence[Mapping[str, Any]],
    dxil_access: DxilCBufferAccessSignature,
) -> HlslCBufferAbiRepairResult:
    """Repair statically provable ambiguous CBuffers in SPIRV-Cross HLSL.

    Only captured blocks explicitly labelled ``ambiguous`` are changed.  Every
    such block must be a single rounded ``float4`` array at ``packoffset(c0)``,
    every HLSL use must have a static register index, and all consumed lanes
    must exactly cover the original DXIL f32/i32 access evidence.  Otherwise the
    whole operation fails without returning partially repaired source.
    """

    if not isinstance(source, str) or not source or len(source) > MAX_HLSL_CHARS:
        raise HlslCBufferAbiRepairError("HLSL source size is invalid")
    if "\x00" in source:
        raise HlslCBufferAbiRepairError("HLSL source contains NUL characters")
    if not isinstance(dxil_access, DxilCBufferAccessSignature):
        raise HlslCBufferAbiRepairError("dxil_access has the wrong type")

    baselines = _baseline_blocks(baseline_constant_blocks)
    ambiguous = {
        key: block
        for key, block in baselines.items()
        if block.layout_completeness == "ambiguous"
    }
    if not ambiguous:
        raise HlslCBufferAbiRepairError("there are no ambiguous CBuffers to repair")
    declarations = _declarations(source)
    declaration_ranges = tuple(
        (item.start, item.end) for item in declarations.values()
    )
    newline = "\r\n" if "\r\n" in source else "\n"
    edits: list[_Edit] = []
    repaired: list[RepairedCBuffer] = []
    generated_names: set[str] = set()

    for key, baseline in sorted(ambiguous.items()):
        declaration = declarations.get(key)
        if declaration is None:
            raise HlslCBufferAbiRepairError(
                "ambiguous baseline CBuffer is missing from HLSL"
            )
        if not declaration.member_name:
            raise HlslCBufferAbiRepairError(
                "ambiguous HLSL CBuffer is not the simple SPIRV-Cross shape"
            )
        expected_count = (baseline.byte_size + 15) // 16
        if declaration.count != expected_count:
            raise HlslCBufferAbiRepairError(
                "SPIRV-Cross CBuffer array does not match the rounded baseline size"
            )
        access_block = dxil_access.block(baseline.space, baseline.bind)
        fields = _physical_accesses(baseline, access_block)
        for offset, return_type in fields.items():
            generated_names.add(
                _field_name(
                    baseline.space,
                    baseline.bind,
                    offset,
                    return_type,
                )
            )
        tail_offset = baseline.byte_size - 4
        if tail_offset not in fields:
            generated_names.add(
                _tail_field_name(baseline.space, baseline.bind, tail_offset)
            )
        reference_edits, consumed = _reference_edits(
            source,
            declaration,
            declaration_ranges,
            fields,
        )
        if consumed != set(fields):
            raise HlslCBufferAbiRepairError(
                "HLSL uses do not cover the complete DXIL access evidence"
            )
        replacement, tail_added = _declaration_text(
            declaration,
            baseline,
            fields,
            newline,
        )
        edits.extend(reference_edits)
        edits.append(_Edit(declaration.start, declaration.end, replacement))
        repaired.append(
            RepairedCBuffer(
                baseline.space,
                baseline.bind,
                baseline.byte_size,
                declaration.member_name,
                len(fields),
                tail_added,
            )
        )

    for name in generated_names:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", source):
            raise HlslCBufferAbiRepairError(
                "generated CBuffer field name already exists in HLSL"
            )
    if len(edits) > MAX_REWRITES:
        raise HlslCBufferAbiRepairError("HLSL rewrite count exceeds its limit")
    return HlslCBufferAbiRepairResult(
        _apply_edits(source, edits),
        tuple(repaired),
    )
