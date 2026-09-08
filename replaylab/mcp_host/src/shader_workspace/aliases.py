"""Per-user CBuffer alias store shared by the RenderDoc panel and the Host.

An alias is a *display name* for a reflected constant-buffer block or variable. The
captured shader, the decompiled baseline and every compiled candidate keep
their original identifiers; only what the user sees changes.

Aliases follow the **constant-block layout**, not one shader or one capture:
the key is the SHA-256 of the block name plus its top-level members
(``name@byteOffset`` in declaration order). Every shader in every ``.rdc`` of
the same game that binds a block with that name and layout shows the same
aliases, which is what makes the store shareable between captures.

Store layout (identical for qrenderdoc ``ReplayLabCBufferAliases`` and this
module — change both together)::

    %LOCALAPPDATA%\\ReplayLab\\aliases\\cbuffer\\<layoutSha256>.json

    {
      "schema": "replaylab-cbuffer-aliases/v2",
      "layoutSha256": "<sha-256 of the layout signature>",
      "block": "cb0",
      "members": [{"name": "v0", "offset": 0}, ...],
      "blockAlias": {"alias": "MaterialConstants", "source": "user",
                     "updatedAt": "..."},
      "updatedAt": "2026-09-03T12:00:00Z",
      "aliases": [
        {"path": "v0", "offset": 0, "alias": "tintAndAlpha",
         "source": "user", "updatedAt": "..."}
      ]
    }

``path`` is the *reflected* variable path RenderDoc shows (dotted:
``light.color`` or ``arr[2]``); ``offset`` is the absolute byte offset of a
leaf inside the block or ``-1`` when unknown. The panel matches an entry by
path, then by offset for leaves.

``source`` is ``"user"`` (typed in the panel) or ``"ai"`` (published by
``rename_shader_workspace_symbols``). A user entry is never overwritten by an
AI entry.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from .io import atomic_write_json, read_json_object

SCHEMA = "replaylab-cbuffer-aliases/v2"
MAX_ALIAS_CHARS = 64
MAX_ENTRIES = 4096
MAX_FILE_BYTES = 4 * 1024 * 1024
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_CBUFFER_RE = re.compile(
    rf"\bcbuffer\s+(?P<block>{_IDENTIFIER})\s*"
    rf"(?::\s*register\s*\(\s*b(?P<bind>\d+)\s*(?:,\s*space(?P<space>\d+)\s*)?\))?\s*"
    rf"\{{(?P<body>[^{{}}]*)\}}",
    re.DOTALL,
)
_DECL_RE = re.compile(
    rf"^\s*(?:(?P<layout>row_major|column_major)\s+)?"
    rf"(?P<type>{_IDENTIFIER}(?:\s*<[^>]*>)?)\s+(?P<name>{_IDENTIFIER})"
    rf"(?P<arrays>(?:\s*\[\s*\d+\s*\])*)\s*"
    rf"(?::\s*packoffset\s*\(\s*c(?P<reg>\d+)(?:\.(?P<comp>[xyzw]))?\s*\))?\s*$"
)
_SCALAR_BYTES = {
    "float": 4, "int": 4, "uint": 4, "bool": 4, "half": 4, "min16float": 4,
    "min16int": 4, "min16uint": 4, "dword": 4, "float16_t": 2, "int16_t": 2,
    "uint16_t": 2, "double": 8, "int64_t": 8, "uint64_t": 8,
}
_TYPE_RE = re.compile(r"^(?P<scalar>[a-z0-9_]+?)(?P<rows>[1-4])?(?:x(?P<cols>[1-4]))?$")
_COMPONENT_INDEX = {"x": 0, "y": 1, "z": 2, "w": 3}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def store_root() -> Path:
    """Alias root; ``REPLAYLAB_ALIAS_ROOT`` overrides it (tests, portable setups)."""

    override = str(os.environ.get("REPLAYLAB_ALIAS_ROOT", "") or "").strip()
    if override:
        return Path(override)
    local = str(os.environ.get("LOCALAPPDATA", "") or "").strip()
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "ReplayLab" / "aliases" / "cbuffer"


def is_valid_alias(alias: str) -> bool:
    text = str(alias or "")
    if not text.strip() or text != text.strip() or len(text) > MAX_ALIAS_CHARS:
        return False
    return all(ch.isprintable() and ch not in "\r\n\t" for ch in text)


def layout_signature(block: str, members: list[Mapping[str, Any]]) -> str:
    """Canonical text hashed into the layout key; ``""`` when any offset is unknown.

    Keep byte-identical with ``ReplayLabCBufferAliases::LayoutSignature``.
    """

    lines = [str(block or "")]
    for member in members:
        offset = _int(member.get("offset"), -1)
        name = str(member.get("name", "") or "")
        if offset < 0 or not name:
            return ""
        lines.append("%s@%d" % (name, offset))
    return "\n".join(lines)


def layout_sha256(block: str, members: list[Mapping[str, Any]]) -> str:
    signature = layout_signature(block, members)
    if not signature:
        return ""
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


def layout_alias_path(layout_sha256_value: str) -> Path:
    value = str(layout_sha256_value or "").strip().lower()
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError("layoutSha256 must be a lowercase SHA-256 hex digest")
    return store_root() / ("%s.json" % value)


# --------------------------------------------------------------------------- HLSL parsing


def _type_bytes(type_text: str, layout: str | None) -> tuple[int, bool] | None:
    """Byte size of a scalar/vector/matrix type and whether it is a matrix."""

    match = _TYPE_RE.match(type_text.strip())
    if not match:
        return None
    scalar = _SCALAR_BYTES.get(match.group("scalar"))
    if scalar is None:
        return None
    rows = int(match.group("rows") or 1)
    cols = match.group("cols")
    if cols is None:
        return scalar * rows, False
    columns = int(cols)
    # HLSL packs a matrix as one 16-byte register per column (column_major, the
    # default) or per row (row_major).
    vectors = rows if layout == "row_major" else columns
    return vectors * 16, True


def cbuffer_members(source: str) -> list[dict[str, Any]]:
    """Top-level cbuffer variables declared in HLSL with best-effort byte offsets.

    ``offset`` is exact when the member carries ``packoffset`` or when every
    earlier member of the block is a scalar/vector/matrix/array of those; it
    is ``-1`` from the first struct or unknown type onwards (the panel then
    matches that member by name only).
    """

    members: list[dict[str, Any]] = []
    for block_match in _CBUFFER_RE.finditer(source or ""):
        block = block_match.group("block")
        bind = int(block_match.group("bind")) if block_match.group("bind") is not None else -1
        space = int(block_match.group("space")) if block_match.group("space") is not None else (0 if bind >= 0 else -1)
        cursor: int | None = 0
        for statement in block_match.group("body").split(";"):
            statement = re.sub(r"//[^\n]*|/\*.*?\*/", "", statement, flags=re.DOTALL).strip()
            if not statement:
                continue
            decl = _DECL_RE.match(statement)
            if not decl:
                cursor = None
                continue
            name = decl.group("name")
            counts = [int(n) for n in re.findall(r"\d+", decl.group("arrays") or "")]
            elements = 1
            for count in counts:
                elements *= max(count, 1)
            sized = _type_bytes(decl.group("type"), decl.group("layout"))
            offset: int | None = None
            if decl.group("reg") is not None:
                offset = int(decl.group("reg")) * 16 + _COMPONENT_INDEX.get(decl.group("comp") or "x", 0) * 4
            if sized is None:
                cursor = None
            else:
                size, is_matrix = sized
                if offset is None and cursor is not None:
                    if counts or is_matrix or (cursor % 16) + size > 16:
                        cursor = (cursor + 15) // 16 * 16
                    offset = cursor
                if offset is not None:
                    stride = (size + 15) // 16 * 16 if (counts or is_matrix) else size
                    cursor = offset + (stride * (elements - 1) + size if counts else size)
                else:
                    cursor = None
            members.append({
                "block": block,
                "bind": bind,
                "space": space,
                "name": name,
                "offset": offset if offset is not None else -1,
                "leaf": sized is not None and not counts,
                "byteSize": sized[0] if sized is not None and not counts else 0,
            })
            if len(members) >= MAX_ENTRIES:
                return members
    return members


# --------------------------------------------------------------------------- store I/O


def _int(value: Any, default: int) -> int:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _clean_block_alias(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    alias = str(value.get("alias", "") or "")
    if not is_valid_alias(alias):
        return None
    return {
        "alias": alias,
        "source": "user" if value.get("source") == "user" else "ai",
        "updatedAt": str(value.get("updatedAt", "") or ""),
    }


def _read_store(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if not path.is_file():
        return [], None
    payload = read_json_object(path, MAX_FILE_BYTES, "cbuffer alias store")
    if payload.get("schema") != SCHEMA:
        return [], None
    entries = payload.get("aliases")
    clean: list[dict[str, Any]] = []
    for item in entries if isinstance(entries, list) else []:
        if not isinstance(item, dict):
            continue
        alias = str(item.get("alias", "") or "")
        path_value = str(item.get("path", "") or "")
        if not is_valid_alias(alias) or not path_value:
            continue
        clean.append({
            "path": path_value,
            "offset": _int(item.get("offset"), -1),
            "alias": alias,
            "source": "user" if item.get("source") == "user" else "ai",
            "updatedAt": str(item.get("updatedAt", "") or ""),
        })
        if len(clean) >= MAX_ENTRIES:
            break
    return clean, _clean_block_alias(payload.get("blockAlias"))


def _read_entries(path: Path) -> list[dict[str, Any]]:
    return _read_store(path)[0]


def read_aliases(layout_sha256_value: str) -> list[dict[str, Any]]:
    return _read_entries(layout_alias_path(layout_sha256_value))


def read_block_alias(layout_sha256_value: str) -> dict[str, Any] | None:
    """Return the stored display alias for a block layout, if any."""

    return _read_store(layout_alias_path(layout_sha256_value))[1]


def publish_aliases(
    block: str,
    members: list[Mapping[str, Any]],
    entries: list[Mapping[str, Any]],
    source: str = "ai",
    *,
    block_alias: str | None = None,
) -> dict[str, Any]:
    """Merge ``entries`` into the alias file of this block layout.

    Each entry needs ``path`` and ``alias``; ``offset`` is optional. Existing
    ``user`` entries for the same path win over an ``ai`` publish; a ``user``
    publish replaces anything.
    """

    key = layout_sha256(block, members)
    if not key:
        raise ValueError("block layout is incomplete (a member offset is unknown)")
    path = layout_alias_path(key)
    existing, existing_block_alias = _read_store(path)
    source = "user" if source == "user" else "ai"
    written: list[str] = []
    kept_user: list[str] = []
    rejected: list[dict[str, str]] = []
    block_alias_written = False
    block_alias_kept_user = False
    now = _now()
    for item in entries:
        member = str(item.get("path", "") or "")
        alias = str(item.get("alias", "") or "")
        label = "%s.%s" % (block, member) if block else member
        if not member or not is_valid_alias(alias):
            rejected.append({"path": label, "reason": "alias must be 1-%d printable characters" % MAX_ALIAS_CHARS})
            continue
        index = next((i for i, e in enumerate(existing) if e["path"] == member), -1)
        if index >= 0 and existing[index]["source"] == "user" and source != "user":
            kept_user.append(label)
            continue
        record = {
            "path": member,
            "offset": _int(item.get("offset"), -1),
            "alias": alias,
            "source": source,
            "updatedAt": now,
        }
        if index >= 0:
            existing[index] = record
        else:
            existing.append(record)
        written.append(label)
        if len(existing) >= MAX_ENTRIES:
            break
    if block_alias is not None:
        requested_block_alias = str(block_alias or "")
        if not is_valid_alias(requested_block_alias):
            rejected.append({
                "path": str(block or ""),
                "reason": "block alias must be 1-%d printable characters" % MAX_ALIAS_CHARS,
            })
        elif (
            existing_block_alias is not None
            and existing_block_alias["source"] == "user"
            and source != "user"
        ):
            block_alias_kept_user = True
        else:
            existing_block_alias = {
                "alias": requested_block_alias,
                "source": source,
                "updatedAt": now,
            }
            block_alias_written = True
    if written or block_alias_written:
        payload = {
            "schema": SCHEMA,
            "layoutSha256": key,
            "block": str(block or ""),
            "members": [{"name": str(m.get("name")), "offset": _int(m.get("offset"), -1)} for m in members],
            "updatedAt": now,
            "aliases": existing,
        }
        if existing_block_alias is not None:
            payload["blockAlias"] = existing_block_alias
        atomic_write_json(path, payload)
    return {
        "block": str(block or ""),
        "layoutSha256": key,
        "path": str(path),
        "written": written,
        "keptUser": kept_user,
        "blockAliasWritten": block_alias_written,
        "blockAliasKeptUser": block_alias_kept_user,
        "blockAlias": existing_block_alias,
        "rejected": rejected,
        "total": len(existing),
    }


def blocks_from_source(source: str) -> list[dict[str, Any]]:
    """Declared cbuffer blocks with their top-level members, in source order."""

    blocks: list[dict[str, Any]] = []
    for member in cbuffer_members(source):
        if not blocks or blocks[-1]["block"] != member["block"]:
            blocks.append({"block": member["block"], "bind": member["bind"], "space": member["space"], "members": []})
        blocks[-1]["members"].append(member)
    return blocks


def aliases_from_renames(
    baseline_source: str, renames: Mapping[str, str],
    captured_identity: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Per-block alias publications implied by a Host rename map.

    Members declared inside a ``cbuffer`` and the block name itself are aliases
    the panel can show; functions, textures and locals remain only in the
    workspace ``ai/aliases.json``. A block whose layout
    cannot be computed (struct members, unknown types) is reported, not
    published.
    """

    publications: list[dict[str, Any]] = []
    for block in blocks_from_source(baseline_source):
        block_alias = str(renames.get(block["block"], "") or "")
        target = block
        captured_targets = []
        if captured_identity is not None:
            captured_targets = [item for item in captured_identity.get("blocks", [])
                                if isinstance(item, Mapping)
                                and item.get("bind") == block["bind"]
                                and item.get("space") == block["space"]]
            if len(captured_targets) == 1:
                target = captured_targets[0]
        entries = []
        unmapped = []
        for member in block["members"]:
            alias = renames.get(member["name"])
            if not alias or alias == member["name"]:
                continue
            path = member["name"]
            offset = member["offset"] if member["leaf"] else -1
            if captured_identity is not None:
                matches = [leaf for leaf in target.get("leaves", [])
                           if isinstance(leaf, Mapping)
                           and leaf.get("offset") == offset
                           and leaf.get("byteSize") == member["byteSize"]]
                if (len(captured_targets) != 1
                        or target.get("memberMappingAvailable") is not True
                        or not member["leaf"] or member["byteSize"] <= 0
                        or len(matches) != 1 or not matches[0].get("path")):
                    unmapped.append(member["name"])
                    continue
                path = str(matches[0]["path"])
            entries.append({"path": path, "offset": offset, "alias": alias,
                            "byteSize": member["byteSize"], "original": member["name"]})
        if not entries and not block_alias and not unmapped:
            continue
        target_members = [{"name": m["name"], "offset": m["offset"]} for m in target["members"]]
        target_key = layout_sha256(target["block"], target_members)
        if captured_identity is not None and len(captured_targets) != 1:
            target_key = ""
        publications.append({
            "block": target["block"],
            "sourceBlock": block["block"],
            "bind": block["bind"],
            "space": block["space"],
            "members": target_members,
            "layoutSha256": target_key,
            "entries": entries,
            "blockAlias": block_alias or None,
            "unmapped": unmapped,
            "capturedIdentityMatched": captured_identity is not None and len(captured_targets) == 1,
        })
    return publications


def evidence_for_source(source: str, captured_identity: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Aliases already stored for every cbuffer block declared in ``source``.

    ``user`` entries were typed in RenderDoc's constant-buffer panel and are
    the names an organizer must reuse; ``ai`` entries are earlier publishes.
    """

    blocks: list[dict[str, Any]] = []
    for block in blocks_from_source(source):
        target = block
        matched = False
        if captured_identity is not None:
            targets = [item for item in captured_identity.get("blocks", [])
                       if isinstance(item, Mapping) and item.get("bind") == block["bind"]
                       and item.get("space") == block["space"]]
            if len(targets) == 1:
                target, matched = targets[0], True
        key = layout_sha256(target["block"], target["members"])
        if captured_identity is not None and not matched:
            key = ""
        record: dict[str, Any] = {"block": block["block"], "layoutSha256": key or None, "user": [], "ai": []}
        if captured_identity is not None:
            record.update({"capturedBlock": target["block"] if matched else None,
                           "capturedIdentityMatched": matched, "unmapped": []})
        if key:
            try:
                for entry in read_aliases(key):
                    source_path = entry["path"]
                    if captured_identity is not None:
                        leaves = [leaf for leaf in target.get("leaves", [])
                                  if leaf.get("path") == entry["path"]]
                        sources = [member for member in block["members"]
                                   if len(leaves) == 1 and member["leaf"]
                                   and member["offset"] == leaves[0].get("offset")
                                   and member["byteSize"] == leaves[0].get("byteSize")]
                        if target.get("memberMappingAvailable") is not True or len(sources) != 1:
                            record["unmapped"].append({"path": entry["path"], "alias": entry["alias"], "source": entry["source"]})
                            continue
                        source_path = sources[0]["name"]
                    record["user" if entry["source"] == "user" else "ai"].append(
                        {"path": source_path, "alias": entry["alias"]}
                    )
                block_alias = read_block_alias(key)
                if block_alias is not None:
                    record["blockAlias"] = block_alias
            except (OSError, ValueError) as exc:
                record["error"] = str(exc)
        else:
            record["error"] = "layout incomplete: member offsets unknown"
        blocks.append(record)
    return {"schema": SCHEMA, "storeRoot": str(store_root()), "blocks": blocks}
