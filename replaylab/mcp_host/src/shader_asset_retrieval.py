"""Retriever + memory that connects a captured shader to unpacked assets.

Strict identity (hash equality) is one signal among several, not the gate:
unpacked trees differ by engine, version, compression and completeness, so
this module ranks *documents* — materials, material instances, shader sources
— against what the capture shows (bound texture names, shader stage, pass
hints, the shader's own resource/cbuffer names) and returns per-signal
evidence.  The AI makes the final call through ``submit`` and that decision
is remembered per game build, per shader identity (raw hash) and per
structural signature, so the next capture or the next shader with the same
shape never repeats the search.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
from typing import Any

RETRIEVAL_SCHEMA = "renderdoc-ultra-shader-asset-retrieval/v1"
RESOLUTION_SCHEMA = "renderdoc-ultra-shader-asset-resolution/v1"
RESOLUTION_DB_SCHEMA_VERSION = 1
MAX_TOKENS = 128
MAX_CANDIDATES = 25
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASSET_ID = re.compile(r"^[0-9a-f]{16,128}$")
_TEXTURE_PREFIXES = ("t_", "tex_", "texture_", "tx_", "sp_", "spr_")
_TEXTURE_SUFFIXES = ("_d", "_n", "_m", "_r", "_a", "_e", "_orm", "_mra", "_bc", "_nrm", "_ao",
                     "_mask", "_diffuse", "_normal", "_albedo", "_basecolor", "_roughness",
                     "_metallic", "_emissive", "_specular", "_opacity", "_height")
_GENERIC_TEXTURE_NAMES = {"", "default", "defaultdiffuse", "defaultnormal", "white", "black",
                          "flat", "none", "texture", "tex", "scenecolor", "scenedepth",
                          "gbuffer", "shadowmap", "depth", "backbuffer"}
_MATERIAL_CLASSES = ("Material", "MaterialInstanceConstant", "MaterialInstanceDynamic",
                     "MaterialFunction", "MaterialFunctionInstance")


class ShaderAssetRetrievalError(RuntimeError):
    """Invalid retrieval/resolution input."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def structural_key(stage: str, constant_block_sizes: Sequence[int], texture_count: int,
                   sampler_count: int, uav_count: int, entry_point: str = "") -> str:
    """A hash-free shape signature: same layout ⇒ likely the same material family."""

    material = {
        "stage": str(stage or ""),
        "cbufferSizes": sorted(int(size) for size in constant_block_sizes),
        "textures": int(texture_count),
        "samplers": int(sampler_count),
        "uavs": int(uav_count),
        "entryPoint": str(entry_point or ""),
    }
    return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()


def texture_name_tokens(names: Sequence[str]) -> tuple[list[str], list[dict[str, Any]]]:
    """Turn RenderDoc texture debug names into weighted retrieval tokens."""

    tokens: list[str] = []
    evidence: list[dict[str, Any]] = []
    for raw in names:
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        if not name:
            continue
        # UE names often look like "Texture2D 1234 T_Ashley_Body_D" or a path.
        name = name.replace("\\", "/").rsplit("/", 1)[-1]
        name = re.sub(r"^(?:Texture2D|Texture|RenderTarget|Tex)\s+\d+\s+", "", name)
        stem = name.split(".")[0]
        folded = stem.casefold()
        core = folded
        for prefix in _TEXTURE_PREFIXES:
            if core.startswith(prefix):
                core = core[len(prefix):]
                break
        for suffix in _TEXTURE_SUFFIXES:
            if core.endswith(suffix) and len(core) > len(suffix) + 2:
                core = core[: -len(suffix)]
                break
        if core in _GENERIC_TEXTURE_NAMES or len(core) < 3:
            evidence.append({"texture": raw, "used": False, "reason": "generic or too short"})
            continue
        produced = [stem]
        if core != folded:
            produced.append(core)
        tokens.extend(produced)
        evidence.append({"texture": raw, "used": True, "tokens": produced})
        if len(tokens) >= MAX_TOKENS:
            break
    return tokens, evidence


class ShaderAssetResolutionStore:
    """Durable lookup table of AI/rule/identity decisions (positive and negative)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS resolutions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_key TEXT NOT NULL,
                shader_raw_sha256 TEXT NOT NULL,
                structural_key TEXT NOT NULL,
                stage TEXT NOT NULL,
                entry_point TEXT NOT NULL DEFAULT '',
                asset_relative_path TEXT NOT NULL,
                asset_content_sha256 TEXT NOT NULL DEFAULT '',
                asset_class TEXT NOT NULL DEFAULT '',
                catalog_sha256 TEXT NOT NULL DEFAULT '',
                negative INTEGER NOT NULL DEFAULT 0,
                confidence REAL NOT NULL,
                decided_by TEXT NOT NULL,
                rationale TEXT NOT NULL DEFAULT '',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                decided_at TEXT NOT NULL,
                UNIQUE(game_key, shader_raw_sha256, stage, entry_point, asset_relative_path)
            );
            CREATE INDEX IF NOT EXISTS resolutions_structural_idx
                ON resolutions(game_key, structural_key, negative);
            CREATE TABLE IF NOT EXISTS member_rules (
                game_key TEXT NOT NULL,
                family_key TEXT NOT NULL,
                space INTEGER NOT NULL,
                bind INTEGER NOT NULL,
                byte_offset INTEGER NOT NULL,
                byte_size INTEGER NOT NULL,
                name TEXT NOT NULL,
                base_type TEXT NOT NULL DEFAULT 'float',
                columns INTEGER NOT NULL DEFAULT 1,
                confidence REAL NOT NULL,
                source TEXT NOT NULL,
                shader_raw_sha256 TEXT NOT NULL DEFAULT '',
                decided_at TEXT NOT NULL,
                PRIMARY KEY(game_key, family_key, space, bind, byte_offset)
            );
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES('schema', ?)", (RESOLUTION_SCHEMA,)
        )
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES('schemaVersion', ?)",
            (str(RESOLUTION_DB_SCHEMA_VERSION),),
        )
        return connection

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "gameKey": row["game_key"],
            "shaderRawSha256": row["shader_raw_sha256"],
            "structuralKey": row["structural_key"],
            "stage": row["stage"],
            "entryPoint": row["entry_point"],
            "assetRelativePath": row["asset_relative_path"],
            "assetContentSha256": row["asset_content_sha256"],
            "assetClass": row["asset_class"],
            "catalogSha256": row["catalog_sha256"],
            "negative": bool(row["negative"]),
            "confidence": float(row["confidence"]),
            "decidedBy": row["decided_by"],
            "rationale": row["rationale"],
            "evidence": json.loads(row["evidence_json"] or "{}"),
            "decidedAt": row["decided_at"],
        }

    def lookup(self, game_key: str, shader_raw_sha256: str, structural: str,
               stage: str, entry_point: str = "") -> dict[str, Any]:
        """Exact-shader decisions first, then same-shape decisions as suggestions."""

        with closing(self._connect()) as db:
            exact = [self._row(row) for row in db.execute(
                "SELECT * FROM resolutions WHERE game_key=? AND shader_raw_sha256=? AND stage=? "
                "AND entry_point=? ORDER BY negative, confidence DESC, decided_at DESC",
                (game_key, shader_raw_sha256, stage, entry_point),
            )]
            similar = [self._row(row) for row in db.execute(
                "SELECT * FROM resolutions WHERE game_key=? AND structural_key=? AND negative=0 "
                "AND shader_raw_sha256<>? ORDER BY confidence DESC, decided_at DESC LIMIT 10",
                (game_key, structural, shader_raw_sha256),
            )]
        positives = [item for item in exact if not item["negative"]]
        return {
            "resolved": bool(positives),
            "resolution": positives[0] if positives else None,
            # Several assets can legitimately explain one shader (material
            # instance + parent material + a shared function); all positives
            # are returned so partial evidence is not lost.
            "resolutions": positives,
            "rejected": [item for item in exact if item["negative"]],
            "sameShapeSuggestions": similar,
        }

    def record(self, *, game_key: str, shader_raw_sha256: str, structural: str, stage: str,
               entry_point: str, asset_relative_path: str, asset_content_sha256: str,
               asset_class: str, catalog_sha256: str, negative: bool, confidence: float,
               decided_by: str, rationale: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
        if not _SHA256.fullmatch(shader_raw_sha256 or ""):
            raise ShaderAssetRetrievalError("shader raw SHA-256 is invalid")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0.0 <= float(confidence) <= 1.0:
            raise ShaderAssetRetrievalError("confidence must be between 0 and 1")
        rationale = str(rationale or "")[:2000]
        payload = _canonical(dict(evidence))[:65536]
        with closing(self._connect()) as db:
            db.execute(
                "INSERT INTO resolutions(game_key, shader_raw_sha256, structural_key, stage, entry_point, "
                "asset_relative_path, asset_content_sha256, asset_class, catalog_sha256, negative, "
                "confidence, decided_by, rationale, evidence_json, decided_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(game_key, shader_raw_sha256, stage, entry_point, asset_relative_path) DO UPDATE SET "
                "structural_key=excluded.structural_key, asset_content_sha256=excluded.asset_content_sha256, "
                "asset_class=excluded.asset_class, catalog_sha256=excluded.catalog_sha256, negative=excluded.negative, "
                "confidence=excluded.confidence, decided_by=excluded.decided_by, rationale=excluded.rationale, "
                "evidence_json=excluded.evidence_json, decided_at=excluded.decided_at",
                (game_key, shader_raw_sha256, structural, stage, entry_point, asset_relative_path,
                 asset_content_sha256, asset_class, catalog_sha256, 1 if negative else 0, float(confidence),
                 decided_by, rationale, payload, _utc_now()),
            )
            db.commit()
            row = db.execute(
                "SELECT * FROM resolutions WHERE game_key=? AND shader_raw_sha256=? AND stage=? AND entry_point=? "
                "AND asset_relative_path=?",
                (game_key, shader_raw_sha256, stage, entry_point, asset_relative_path),
            ).fetchone()
        return self._row(row)

    def record_member_rules(self, *, game_key: str, family_keys: Sequence[str], members: Sequence[Mapping[str, Any]],
                            source: str, confidence: float, shader_raw_sha256: str = "") -> int:
        """Remember confirmed CBuffer member names per material family and offset.

        A later shader resolved to the same family (an instance of the same
        parent material, or the same material in another capture) receives
        these names through the fixed repair's layout hint — no AI turn spent.
        A higher-confidence rule replaces a lower one; equal confidence keeps
        the newest.
        """

        written = 0
        with closing(self._connect()) as db:
            for family in family_keys:
                for member in members:
                    try:
                        space = int(member.get("space", 0) or 0)
                        bind = int(member.get("bind"))
                        offset = int(member.get("byteOffset"))
                        size = int(member.get("byteSize"))
                    except (TypeError, ValueError):
                        continue
                    name = str(member.get("name") or "")
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or size <= 0 or offset < 0 or bind < 0:
                        continue
                    columns = max(1, min(4, size // 4)) if size in (4, 8, 12, 16) else 1
                    base_type = str(member.get("baseType") or "float")[:16]
                    existing = db.execute(
                        "SELECT confidence FROM member_rules WHERE game_key=? AND family_key=? AND space=? AND bind=? AND byte_offset=?",
                        (game_key, family, space, bind, offset),
                    ).fetchone()
                    if existing is not None and float(existing["confidence"]) > float(confidence):
                        continue
                    db.execute(
                        "INSERT INTO member_rules(game_key, family_key, space, bind, byte_offset, byte_size, name, base_type, "
                        "columns, confidence, source, shader_raw_sha256, decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(game_key, family_key, space, bind, byte_offset) DO UPDATE SET byte_size=excluded.byte_size, "
                        "name=excluded.name, base_type=excluded.base_type, columns=excluded.columns, confidence=excluded.confidence, "
                        "source=excluded.source, shader_raw_sha256=excluded.shader_raw_sha256, decided_at=excluded.decided_at",
                        (game_key, family, space, bind, offset, size, name, base_type, columns, float(confidence),
                         str(source)[:64], shader_raw_sha256, _utc_now()),
                    )
                    written += 1
            db.commit()
        return written

    def member_rules(self, game_key: str, family_keys: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        """Rules per ``"space:bind"`` for the given families (first family wins per offset)."""

        result: dict[str, dict[int, dict[str, Any]]] = {}
        with closing(self._connect()) as db:
            for family in family_keys:
                rows = db.execute(
                    "SELECT * FROM member_rules WHERE game_key=? AND family_key=? ORDER BY space, bind, byte_offset",
                    (game_key, family),
                ).fetchall()
                for row in rows:
                    key = "%d:%d" % (int(row["space"]), int(row["bind"]))
                    block = result.setdefault(key, {})
                    if int(row["byte_offset"]) in block:
                        continue
                    block[int(row["byte_offset"])] = {
                        "name": row["name"], "byteOffset": int(row["byte_offset"]), "byteSize": int(row["byte_size"]),
                        "baseType": row["base_type"], "columns": int(row["columns"]), "elements": 1,
                        "confidence": float(row["confidence"]), "source": row["source"], "familyKey": family,
                    }
        return {key: [block[offset] for offset in sorted(block)] for key, block in result.items()}

    def forget(self, game_key: str, shader_raw_sha256: str, stage: str, entry_point: str) -> int:
        with closing(self._connect()) as db:
            cursor = db.execute(
                "DELETE FROM resolutions WHERE game_key=? AND shader_raw_sha256=? AND stage=? AND entry_point=?",
                (game_key, shader_raw_sha256, stage, entry_point),
            )
            db.commit()
            return int(cursor.rowcount)


def _material_document(catalog: Any, asset: Mapping[str, Any]) -> dict[str, Any]:
    """Compact, AI-readable view of one candidate and its cooked export data."""

    metadata = asset.get("metadata") or {}
    document: dict[str, Any] = {
        "assetId": asset.get("assetId"),
        "relativePath": asset.get("relativePath"),
        "className": metadata.get("className", ""),
        "engine": metadata.get("engine", ""),
        "names": list(metadata.get("names") or [])[:128],
        "textures": [str(item).rsplit(".", 1)[-1] for item in (metadata.get("textures") or [])][:64],
        "parentMaterials": [str(item).rsplit(".", 1)[-1] for item in (metadata.get("parentMaterials") or [])][:8],
        "materialFunctions": [str(item).rsplit(".", 1)[-1] for item in (metadata.get("materialFunctions") or [])][:32],
        "shaderTypes": [],
        "vertexFactories": [],
        "defines": [],
        "hasInlineShaderMap": False,
        "exportParseStatus": "missing",
    }
    # The cooked export sibling carries shader types/defines; find it through
    # its reverse relationship (uexp -> cooked-export-of -> uasset).
    # One level of related assets: the parent material (names/textures the
    # instance inherits) and how many instances point at this material.
    parent_relation = next((item for item in asset.get("relationships") or []
                            if item.get("relation") == "parent-material" and item.get("targetAssetId")), None)
    if parent_relation is not None:
        try:
            parents = catalog.query(asset_ids=[str(parent_relation["targetAssetId"])], limit=1,
                                    include_relationships=False).get("assets", [])
        except Exception:
            parents = []
        if parents:
            parent_metadata = parents[0].get("metadata") or {}
            document["parent"] = {
                "assetId": parents[0].get("assetId"),
                "relativePath": parents[0].get("relativePath"),
                "className": parent_metadata.get("className", ""),
                "names": list(parent_metadata.get("names") or [])[:128],
                "textures": [str(item).rsplit(".", 1)[-1] for item in (parent_metadata.get("textures") or [])][:64],
            }
    try:
        referrers = catalog.referrers(str(asset.get("assetId")), relation="parent-material", limit=50)
        document["instanceCount"] = len(referrers.get("referrers") or [])
        document["instances"] = [item.get("relativePath") for item in (referrers.get("referrers") or [])[:8]]
    except Exception:
        document["instanceCount"] = None
    try:
        users = catalog.referrers(str(asset.get("assetId")), relation="uses-material", limit=50)
        document["usedByMeshes"] = [
            {"relativePath": item.get("relativePath"), "className": item.get("className")}
            for item in (users.get("referrers") or [])[:12]]
        document["usedByMeshCount"] = len(users.get("referrers") or [])
    except Exception:
        document["usedByMeshes"] = []
    sibling = PurePosixPath(str(asset.get("relativePath") or "")).with_suffix(".uexp").as_posix()
    try:
        found = catalog.query(text=PurePosixPath(sibling).name, kinds=("package",), limit=20,
                              include_relationships=False)
    except Exception:
        found = {"assets": []}
    for item in found.get("assets", []):
        if str(item.get("relativePath")) != sibling:
            continue
        document["exportParseStatus"] = str(item.get("parseStatus") or "unknown")
        export_metadata = item.get("metadata") or {}
        document["shaderTypes"] = list(export_metadata.get("shaderTypes") or [])[:64]
        document["vertexFactories"] = list(export_metadata.get("vertexFactories") or [])[:16]
        document["defines"] = list(export_metadata.get("defines") or [])[:64]
        document["hasInlineShaderMap"] = bool(export_metadata.get("hasInlineShaderMap"))
        break
    return document


def _consistency(document: Mapping[str, Any], features: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Cheap agreement checks between a candidate document and capture facts."""

    checks: list[dict[str, Any]] = []
    stage = str(features.get("stage") or "")
    shader_types = [str(item) for item in document.get("shaderTypes") or []]
    if shader_types:
        wants = {"fragment": "PS", "vertex": "VS", "compute": "CS", "geometry": "GS", "hull": "HS", "domain": "DS"}.get(stage)
        if wants:
            has_stage = any(item.endswith(wants) for item in shader_types)
            checks.append({"signal": "shader-stage-present", "agrees": has_stage,
                           "detail": "material shader map %s a %s shader type" % ("lists" if has_stage else "lists no", wants)})
    pass_hint = str(features.get("passHint") or "").casefold()
    if pass_hint and shader_types:
        pass_map = {"depth": ("DepthOnly", "Shadow"), "shadow": ("ShadowDepth", "Shadow"), "basepass": ("BasePass",),
                    "velocity": ("Velocity",), "outline": ("Outline",), "lumen": ("LumenCard",)}
        for key, needles in pass_map.items():
            if key in pass_hint:
                agrees = any(needle in item for item in shader_types for needle in needles)
                checks.append({"signal": "pass-shader-type", "agrees": agrees,
                               "detail": "pass hint %r vs shader types containing %s" % (pass_hint, "/".join(needles))})
                break
    mesh_hint = str(features.get("meshHint") or "").casefold()
    factories = [str(item).casefold() for item in document.get("vertexFactories") or []]
    if mesh_hint and factories:
        if "skeletal" in mesh_hint or "skin" in mesh_hint:
            agrees = any("gpuskin" in item for item in factories)
            checks.append({"signal": "vertex-factory", "agrees": agrees, "detail": "skinned draw vs GPU-skin vertex factory"})
        elif "static" in mesh_hint:
            agrees = any("local" in item or "static" in item for item in factories)
            checks.append({"signal": "vertex-factory", "agrees": agrees, "detail": "static draw vs local vertex factory"})
    texture_count = features.get("textureSlotCount")
    if isinstance(texture_count, int) and document.get("textures"):
        checks.append({"signal": "texture-count-plausible", "agrees": len(document["textures"]) >= texture_count,
                       "detail": "material references %d textures, shader binds %d slots" % (len(document["textures"]), texture_count)})
    # Code logic structure vs. what the asset declares about itself.
    hints = features.get("codeHints") or {}
    names = set(str(item) for item in (document.get("names") or [])) | set(
        str(item) for item in ((document.get("parent") or {}).get("names") or []))
    defines = set(str(item) for item in (document.get("defines") or []))
    if "usesDiscard" in hints:
        masked = bool({"BLEND_Masked", "Masked", "OpacityMaskClipValue", "DitheredLODTransition"} & names) or \
            bool({"MATERIALBLENDING_MASKED", "MATERIAL_DITHER_OPACITY_MASK"} & defines)
        translucent_only = bool({"BLEND_Translucent", "BLEND_Additive"} & names) and not masked
        if hints["usesDiscard"] and masked:
            checks.append({"signal": "blend-mode", "agrees": True,
                           "detail": "shader discards pixels and the asset declares a masked blend mode"})
        elif hints["usesDiscard"] and translucent_only:
            checks.append({"signal": "blend-mode", "agrees": False,
                           "detail": "shader discards pixels but the asset declares translucent/additive blending only"})
        elif not hints["usesDiscard"] and masked and names:
            checks.append({"signal": "blend-mode", "agrees": False,
                           "detail": "asset declares a masked blend mode but the shader never discards"})
    sample_count = hints.get("textureSampleCount")
    if isinstance(sample_count, int) and sample_count > 0 and document.get("textures"):
        plausible = len(document["textures"]) + 4 >= sample_count
        checks.append({"signal": "sample-count-plausible", "agrees": plausible,
                       "detail": "shader samples textures %d time(s); asset references %d texture(s)" % (sample_count, len(document["textures"]))})
    mesh_hint = str(features.get("meshHint") or "").casefold()
    if mesh_hint == "skeletal" and names:
        skeletal_ok = "bUsedWithSkeletalMesh" in names or any("skin" in str(item).casefold() for item in document.get("vertexFactories") or [])
        if skeletal_ok:
            checks.append({"signal": "usage-flag", "agrees": True, "detail": "skinned draw and the asset is marked for skeletal meshes"})
    return checks


def _owner_path(relative: str) -> str:
    """Map a cooked export/bulk file to the package that owns it."""

    path = PurePosixPath(relative)
    if path.suffix.casefold() in {".uexp", ".ubulk", ".uptnl"}:
        return path.with_suffix(".uasset").as_posix()
    return relative


def value_evidence(catalog: Any, lanes: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Match distinctive cbuffer lane values against indexed asset values.

    Returns evidence keyed by *owner* asset path: matched keys, how many of
    them are rare in the catalog, and adjacency — asset runs whose
    consecutive values sit in consecutive cbuffer lanes. Three complex values
    in a row matching is close to proof; one popular value is noise.
    """

    from .symbols.engines.unreal_package import is_distinctive_value, value_key
    import struct as _struct

    lane_keys: dict[str, list[tuple[str, int]]] = {}
    for lane in lanes:
        try:
            value = _struct.unpack("<f", _struct.pack("<f", float(lane.get("value"))))[0]
        except (TypeError, ValueError, OverflowError, _struct.error):
            continue
        if not is_distinctive_value(value):
            continue
        block = "b%s:s%s" % (lane.get("bind"), lane.get("space"))
        lane_keys.setdefault(value_key(value), []).append((block, int(lane.get("lane", 0))))
    if not lane_keys:
        return {}
    try:
        search = catalog.search_values(list(lane_keys), limit=100)
    except Exception:
        return {}
    popularity = search.get("keyPopularity") or {}
    evidence: dict[str, dict[str, Any]] = {}
    for candidate in search.get("candidates", []):
        owner = _owner_path(str(candidate.get("relativePath") or ""))
        entry = evidence.setdefault(owner, {
            "matchedValues": set(), "rareValues": set(), "adjacentRuns": [], "sourceFiles": set(),
            "laneRefs": {},
        })
        entry["sourceFiles"].add(str(candidate.get("relativePath") or ""))
        for key in candidate.get("matchedValues") or []:
            entry["matchedValues"].add(key)
            if popularity.get(key, 0) <= 3:
                entry["rareValues"].add(key)
            entry["laneRefs"][key] = ["%s lane %d" % (block, index) for block, index in lane_keys.get(key, [])][:4]
        for run in candidate.get("numericRuns") or []:
            values = [str(item) for item in run.get("values") or []]
            # consecutive asset values that also sit in consecutive cbuffer lanes
            best = 1
            current = 1
            for previous, following in zip(values, values[1:]):
                consecutive = False
                for block_a, lane_a in lane_keys.get(previous, []):
                    for block_b, lane_b in lane_keys.get(following, []):
                        if block_a == block_b and lane_b == lane_a + 1:
                            consecutive = True
                            break
                    if consecutive:
                        break
                current = current + 1 if consecutive else 1
                best = max(best, current)
            if best >= 2:
                entry["adjacentRuns"].append({"assetOffset": run.get("offset"), "values": values[:8], "consecutiveLanes": best})
    result: dict[str, dict[str, Any]] = {}
    for owner, entry in evidence.items():
        matched = sorted(entry["matchedValues"])
        rare = sorted(entry["rareValues"])
        adjacency = sum(max(0, run["consecutiveLanes"] - 1) for run in entry["adjacentRuns"])
        # One isolated hit can be a coincidence across a large catalog (mesh
        # or animation floats look "rare" too); certainty comes from several
        # rare values and, above all, from adjacency.
        strength = max(0, len(rare) - 1) + 2 * adjacency
        if not rare:
            confidence = min(0.2, 0.1 * len(matched)) if matched else 0.0
        elif strength == 0:
            confidence = 0.25
        else:
            confidence = min(0.98, 1.0 - 0.5 ** strength)
        result[owner] = {
            "matchedValues": matched[:32],
            "matchedCount": len(matched),
            "rareMatchedCount": len(rare),
            "adjacentRuns": entry["adjacentRuns"][:8],
            "laneRefs": {key: entry["laneRefs"][key] for key in matched[:16]},
            "sourceFiles": sorted(entry["sourceFiles"])[:4],
            "confidence": round(confidence, 3),
            "summary": (
                "shares %d distinctive value(s) (%d rare in this catalog)%s"
                % (len(matched), len(rare),
                   ", %d adjacent run(s) line up with neighbouring cbuffer lanes" % len(entry["adjacentRuns"])
                   if entry["adjacentRuns"] else "")
            ),
        }
    return result


def texture_identity_evidence(catalog: Any, fingerprints: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Match bound-texture thumbnails against indexed texture fingerprints.

    Returns per-bound-texture matches (asset, hash distance, dihedral
    transform, verdict) and, keyed by *material* path, the textures each
    material is known to reference among those matches (via the catalog's
    ``uses-texture`` relations).  A same-image match is strong evidence on
    its own and a bridge to every asset related to that texture.
    """

    from .symbols.imaging import fingerprint_from_thumbnail

    per_texture: list[dict[str, Any]] = []
    by_material: dict[str, dict[str, Any]] = {}
    for item in fingerprints:
        entry: dict[str, Any] = {"texture": item.get("name") or item.get("resourceId"), "resourceId": item.get("resourceId"),
                                 "matches": []}
        if not item.get("available") or not item.get("thumbnail"):
            entry["reason"] = item.get("reason") or "no snapshot"
            per_texture.append(entry)
            continue
        try:
            query = fingerprint_from_thumbnail(str(item["thumbnail"]))
        except Exception as error:
            entry["reason"] = "thumbnail unusable: %s" % str(error)[:120]
            per_texture.append(entry)
            continue
        if query.get("flat"):
            entry["reason"] = "uniform texture (flat colour) cannot identify an asset"
            per_texture.append(entry)
            continue
        try:
            search = catalog.search_thumbnails(int(query["dhash"]), mean_color=item.get("meanColor"), max_distance=12, limit=8)
        except Exception as error:
            entry["reason"] = str(error)[:160]
            per_texture.append(entry)
            continue
        for match in search.get("candidates", []):
            owner = _owner_path(str(match.get("relativePath") or ""))
            record = {**match, "ownerPath": owner}
            entry["matches"].append(record)
            # materials referencing this texture asset
            try:
                found = catalog.query(text=PurePosixPath(owner).name, limit=20, include_relationships=False)
                owner_asset = next((asset for asset in found.get("assets", []) if str(asset.get("relativePath")) == owner), None)
                referrers = catalog.referrers(str(owner_asset["assetId"]), relation="uses-texture", limit=100) if owner_asset else {"referrers": []}
            except Exception:
                referrers = {"referrers": []}
            record["referencedByMaterialCount"] = len(referrers.get("referrers") or [])
            for material in referrers.get("referrers") or []:
                bucket = by_material.setdefault(str(material.get("relativePath")), {"textures": [], "sameImage": 0, "plausible": 0})
                bucket["textures"].append({"boundTexture": entry["texture"], "asset": owner, "distance": match.get("distance"),
                                           "transform": match.get("transform"), "verdict": match.get("verdict")})
                if match.get("verdict") == "same-image":
                    bucket["sameImage"] += 1
                else:
                    bucket["plausible"] += 1
        if not entry["matches"]:
            entry["reason"] = "no indexed texture within 12 hash bits (%d thumbnails indexed)" % int(search.get("indexedThumbnails") or 0)
        per_texture.append(entry)
    for bucket in by_material.values():
        strength = 2 * bucket["sameImage"] + bucket["plausible"]
        bucket["confidence"] = round(min(0.97, 1.0 - 0.5 ** strength), 3) if strength else 0.0
        bucket["summary"] = "%d bound texture(s) identified by content in this material (%d exact, %d plausible)" % (
            len(bucket["textures"]), bucket["sameImage"], bucket["plausible"])
    return {"perTexture": per_texture, "byMaterial": by_material}


def retrieve(catalog: Any, features: Mapping[str, Any], *, top_k: int = 10) -> dict[str, Any]:
    """Rank unpacked-asset documents for one captured shader with evidence."""

    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= MAX_CANDIDATES:
        raise ShaderAssetRetrievalError("top_k must be between 1 and %d" % MAX_CANDIDATES)
    texture_tokens, texture_evidence = texture_name_tokens(list(features.get("boundTextureNames") or []))
    extra_tokens = [str(item) for item in (features.get("extraTokens") or []) if isinstance(item, str)][:32]
    resource_tokens = [str(item) for item in (features.get("resourceNames") or []) if isinstance(item, str)
                       and not re.fullmatch(r"(?:t|s|u|b|cb)\d+|Material_Texture2D_\d+|.*_\d+", item)][:32]
    tokens = texture_tokens + extra_tokens + resource_tokens
    signals = {
        "boundTextures": texture_evidence,
        "extraTokens": extra_tokens,
        "resourceNames": resource_tokens,
    }
    lanes = list(features.get("constantBufferLanes") or [])
    fingerprints = list(features.get("boundTextureFingerprints") or [])
    if not tokens and not lanes and not fingerprints:
        return {
            "schema": RETRIEVAL_SCHEMA, "schemaVersion": 1, "queryTokens": [], "signals": signals,
            "candidates": [], "reason": ("no retrieval evidence: the draw binds no named textures, no extra tokens "
                                         "were given and no distinctive constant-buffer values were readable"),
        }
    try:
        engine = str(catalog.status().get("engine") or "unknown")
    except Exception:
        engine = "unknown"
    # Class filtering only makes sense where the extractor knows classes
    # (Unreal packages); a generic engine ranks every probed document.
    requested_classes = features.get("classNames")
    if requested_classes is None:
        class_filter = list(_MATERIAL_CLASSES) if engine == "unreal" else []
    else:
        class_filter = [str(item) for item in requested_classes]
    kinds = ("package", "material", "shader", "metadata") if engine == "unreal" else \
        ("package", "material", "shader", "metadata", "other", "binary", "scene", "mesh")
    if tokens:
        search = catalog.search_tokens(tokens, kinds=kinds, class_names=class_filter,
                                       limit=min(MAX_CANDIDATES, top_k * 3))
    else:
        search = {"catalogSha256": None, "engine": None, "queryTokens": [], "tokenWeights": {}, "candidates": []}
    values = value_evidence(catalog, lanes)
    signals["constantBufferValues"] = {
        "distinctiveLanes": len(lanes),
        "assetsSharingValues": len(values),
    }
    identity = texture_identity_evidence(catalog, fingerprints) if fingerprints else {"perTexture": [], "byMaterial": {}}
    signals["textureIdentity"] = identity["perTexture"]
    by_material_identity = identity["byMaterial"]
    if search.get("catalogSha256") is None:
        try:
            search["catalogSha256"] = catalog.status().get("catalogSha256")
            search["engine"] = catalog.status().get("engine")
        except Exception:
            pass
    token_paths = {str(item.get("relativePath")) for item in search.get("candidates", [])}
    extra_owners = [(owner, entry["confidence"]) for owner, entry in values.items()]
    extra_owners += [(owner, entry["confidence"]) for owner, entry in by_material_identity.items()]
    for owner, confidence in sorted(extra_owners, key=lambda item: -item[1]):
        if owner in token_paths or confidence < 0.3:
            continue
        # Candidate found by content alone (cbuffer values or texture pixels),
        # not by any name token.
        try:
            found = catalog.query(text=PurePosixPath(owner).name, limit=20, include_relationships=False)
        except Exception:
            found = {"assets": []}
        asset = next((item for item in found.get("assets", []) if str(item.get("relativePath")) == owner), None)
        if asset is None:
            continue
        search.setdefault("candidates", []).append({
            "assetId": asset["assetId"], "relativePath": owner, "kind": asset.get("kind"),
            "format": asset.get("format"), "size": asset.get("size"), "contentSha256": asset.get("contentSha256"),
            "className": (asset.get("metadata") or {}).get("className", ""),
            "engine": (asset.get("metadata") or {}).get("engine", ""),
            "score": 0.0, "matchedTokens": [], "matchedCount": 0, "queryCoverage": 0.0,
        })
        token_paths.add(owner)
    ids = [item["assetId"] for item in search.get("candidates", [])]
    details: dict[str, Mapping[str, Any]] = {}
    if ids:
        try:
            queried = catalog.query(asset_ids=ids[:100], limit=100, include_relationships=True)
            details = {str(item["assetId"]): item for item in queried.get("assets", [])}
        except Exception:
            details = {}
    candidates = []
    for item in search.get("candidates", []):
        asset = details.get(str(item["assetId"]), {"assetId": item["assetId"], "relativePath": item["relativePath"],
                                                    "metadata": {"className": item.get("className")}})
        document = _material_document(catalog, asset)
        checks = _consistency(document, features)
        agree = sum(1 for check in checks if check["agrees"])
        disagree = sum(1 for check in checks if not check["agrees"])
        adjusted = item["score"] * (1.0 + 0.15 * agree - 0.25 * disagree)
        value_entry = values.get(str(item.get("relativePath")))
        if value_entry is None:
            parent_path = (document.get("parent") or {}).get("relativePath")
            value_entry = values.get(str(parent_path)) if parent_path else None
            if value_entry is not None:
                value_entry = {**value_entry, "viaParent": True}
        if value_entry:
            adjusted += 3.0 * value_entry["rareMatchedCount"] + 4.0 * sum(
                max(0, run["consecutiveLanes"] - 1) for run in value_entry["adjacentRuns"])
        identity_entry = by_material_identity.get(str(item.get("relativePath")))
        if identity_entry is None:
            parent_path = (document.get("parent") or {}).get("relativePath")
            identity_entry = by_material_identity.get(str(parent_path)) if parent_path else None
            if identity_entry is not None:
                identity_entry = {**identity_entry, "viaParent": True}
        if identity_entry:
            adjusted += 6.0 * identity_entry["sameImage"] + 2.0 * identity_entry["plausible"]
        why = "matched %s; %d consistency check(s) agree, %d disagree" % (
            ", ".join(item.get("matchedTokens") or []) or "no name tokens", agree, disagree)
        if value_entry:
            why += "; " + value_entry["summary"] + (" (through its parent material)" if value_entry.get("viaParent") else "")
        if identity_entry:
            why += "; " + identity_entry["summary"] + (" (through its parent material)" if identity_entry.get("viaParent") else "")
        candidates.append({
            **item,
            "adjustedScore": round(adjusted, 4),
            "consistency": checks,
            "document": document,
            "valueEvidence": value_entry,
            "textureIdentity": identity_entry,
            "why": why,
        })
    candidates.sort(key=lambda entry: (-entry["adjustedScore"], entry["relativePath"]))
    candidates = candidates[:top_k]
    top = candidates[0]["adjustedScore"] if candidates else 0.0
    second = candidates[1]["adjustedScore"] if len(candidates) > 1 else 0.0
    weights = search.get("tokenWeights") or {}
    total_mass = sum(float(value) for value in weights.values()) or 1.0
    texture_cores = [token for token in texture_tokens if "_" not in token or token.casefold() == token]
    for index, candidate in enumerate(candidates):
        matched = set(candidate.get("matchedTokens") or [])
        mass = sum(float(weights.get(token, 0.0)) for token in matched)
        coverage = mass / total_mass
        checks = candidate.get("consistency") or []
        agree_ratio = (sum(1 for check in checks if check["agrees"]) / len(checks)) if checks else 0.5
        lead = candidate["adjustedScore"] / top if top > 0 else 0.0
        unique_leader = index == 0 and (second == 0 or top >= 1.5 * second)
        confidence = 0.10 + 0.50 * coverage + 0.20 * agree_ratio + 0.10 * lead + (0.10 if unique_leader else 0.0)
        document = candidate.get("document") or {}
        document_textures = [str(item).casefold() for item in document.get("textures") or []]
        parent_textures = [str(item).casefold() for item in (document.get("parent") or {}).get("textures") or []]
        explained = []
        unexplained = []
        for item in texture_evidence:
            if not item.get("used"):
                continue
            produced = [str(token).casefold() for token in item.get("tokens") or []]
            hit = any(core and (core in name) for core in produced for name in document_textures + parent_textures)
            (explained if hit else unexplained).append(item["texture"])
        texture_facet = (len(explained) / (len(explained) + len(unexplained))) if (explained or unexplained) else None
        # Names are reference only: shipped captures rarely carry them, and a
        # path word proves little. Without structural/value/pixel support a
        # name-only match stays below 0.6.
        if texture_facet is not None and texture_facet < 0.5:
            confidence = min(confidence, 0.6)
        elif texture_facet is None and matched:
            confidence = min(confidence, 0.6)
        value_entry = candidate.get("valueEvidence") or {}
        value_confidence = float(value_entry.get("confidence") or 0.0)
        identity_entry = candidate.get("textureIdentity") or {}
        identity_confidence = float(identity_entry.get("confidence") or 0.0)
        if value_confidence >= 0.5:
            # Shared complex numbers are near-identity evidence; they lift a
            # weak name match and are not capped by unexplained textures.
            confidence = max(confidence, value_confidence)
        if identity_confidence >= 0.5:
            confidence = max(confidence, identity_confidence)
        if value_confidence >= 0.75 and identity_confidence >= 0.75:
            confidence = max(confidence, 0.97)
        candidate["confidence"] = round(min(0.98, max(0.01, confidence)), 3)
        candidate["facets"] = {
            "textures": {
                "explained": explained, "unexplained": unexplained,
                "confidence": round(len(explained) / (len(explained) + len(unexplained)), 3)
                if (explained or unexplained) else None,
                "note": "explained = a bound texture whose name appears in this material or its parent",
            },
            "shaderTypes": {
                "available": bool(document.get("shaderTypes")),
                "agrees": [check["signal"] for check in checks if check["agrees"]],
                "disagrees": [check["signal"] for check in checks if not check["agrees"]],
            },
            "names": {
                "available": len(document.get("names") or []) + len((document.get("parent") or {}).get("names") or []),
                "note": "parameter/property names to validate against code usage; usable even when the whole match is uncertain",
            },
            "textureIdentity": {
                "textures": identity_entry.get("textures") or [],
                "sameImage": identity_entry.get("sameImage") or 0,
                "plausible": identity_entry.get("plausible") or 0,
                "confidence": identity_confidence or None,
                "viaParent": bool(identity_entry.get("viaParent")),
                "note": ("bound textures whose pixels (perceptual hash over mirror/rotation variants) match a texture "
                         "this material references; independent of names"),
            },
            "values": {
                "matched": value_entry.get("matchedValues") or [],
                "rareMatchedCount": value_entry.get("rareMatchedCount") or 0,
                "adjacentRuns": value_entry.get("adjacentRuns") or [],
                "laneRefs": value_entry.get("laneRefs") or {},
                "confidence": value_confidence or None,
                "viaParent": bool(value_entry.get("viaParent")),
                "note": ("cbuffer lane values found in the asset's serialized parameters; adjacent runs "
                         "mean consecutive lanes matched consecutive asset values"),
            },
        }
    try:
        coverage = catalog.coverage()
    except Exception as error:
        coverage = {"available": False, "reason": str(error)[:200], "limitations": []}
    for candidate in candidates:
        document = candidate.get("document") or {}
        issues = []
        if document.get("exportParseStatus") in {"deferred", "missing", "parse-error"}:
            issues.append("cooked export data %s: shader types/values for this candidate are incomplete"
                          % document.get("exportParseStatus"))
        if not document.get("names"):
            issues.append("no readable names in this package (foreign format, encrypted, or stripped)")
        candidate["parseIssues"] = issues
    return {
        "schema": RETRIEVAL_SCHEMA,
        "schemaVersion": 1,
        "catalogSha256": search.get("catalogSha256"),
        "engine": search.get("engine"),
        "coverage": coverage,
        "limitations": list(coverage.get("limitations") or []),
        "queryTokens": search.get("queryTokens"),
        "tokenWeights": search.get("tokenWeights"),
        "signals": signals,
        "candidates": candidates,
        "decisionHint": (
            "no candidate" if not candidates else
            "clear leader" if top > 0 and (second == 0 or top >= 1.5 * second) else
            "ambiguous: compare documents (parent chain, shader types, texture set) before deciding"
        ),
        "bestConfidence": candidates[0]["confidence"] if candidates else 0.0,
        "policy": {
            "hashRequired": False,
            "aiDecides": True,
            "partialEvidenceUsable": True,
            "submitTool": "submit_shader_asset_resolution",
            "rememberedPerGameBuild": True,
            "aiMustStillSearch": True,
            "note": ("confidence is per candidate and per facet; a facet that matched (e.g. textures) is "
                     "valid reference even when another facet is unexplained — record the match with the "
                     "confidence you actually have instead of discarding it. These Host signals supplement, "
                     "not replace, the AI's own catalog search when they are weak or absent"),
        },
    }
