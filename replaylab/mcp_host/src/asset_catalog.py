"""Deterministic project-owned catalog for unpacked game assets.

The catalog is intentionally broader than the Shader symbol database.  It
indexes ordinary asset files and only records relationships that fixed parsers
can point back to bytes or exact path/GUID references.  Semantic guesses remain
an Agent inference and are never written as verified edges.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import struct
import time
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlparse
import uuid
import zlib


CATALOG_SCHEMA = "renderdoc-ultra-unpacked-asset-catalog/v1"
CATALOG_VERSION = 1
DATABASE_SCHEMA_VERSION = 1
CATALOG_FILENAME = "asset-catalog-v1.sqlite3"

MAX_QUERY_RESULTS = 100
MAX_QUERY_IDS = 100
MAX_RELATIONSHIPS_PER_ASSET = 256
MAX_INDEXED_REFERENCES_PER_ASSET = 256
MAX_PARSE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_FILES = 200_000
HARD_MAX_FILES = 500_000
DEFAULT_MAX_FILE_SIZE_MB = 4096
HARD_MAX_FILE_SIZE_MB = 16_384
HASH_CHUNK_BYTES = 4 * 1024 * 1024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ASSET_ID_RE = re.compile(r"^asset-v1-[0-9a-f]{64}$")
_UNITY_GUID_RE = re.compile(r"(?m)^\s*guid:\s*([0-9a-fA-F]{32})\s*$")
_UNITY_REFERENCE_GUID_RE = re.compile(r"\bguid:\s*([0-9a-fA-F]{32})\b")
_SHADER_INCLUDE_RE = re.compile(
    r"(?m)^\s*#\s*include\s*[<\"]([^>\"\r\n]+)[>\"]"
)

_KIND_EXTENSIONS: dict[str, frozenset[str]] = {
    "mesh": frozenset(
        {".obj", ".fbx", ".gltf", ".glb", ".dae", ".ply", ".stl", ".abc"}
    ),
    "scene": frozenset({".blend", ".usd", ".usda", ".usdc"}),
    "texture": frozenset(
        {
            ".png",
            ".jpg",
            ".jpeg",
            ".dds",
            ".ktx",
            ".ktx2",
            ".tga",
            ".bmp",
            ".tif",
            ".tiff",
            ".exr",
            ".hdr",
            ".webp",
        }
    ),
    "material": frozenset({".mtl", ".mat", ".material"}),
    "shader": frozenset(
        {
            ".hlsl",
            ".hlsli",
            ".glsl",
            ".vert",
            ".frag",
            ".comp",
            ".geom",
            ".tesc",
            ".tese",
            ".spv",
            ".dxbc",
            ".dxil",
            ".shader",
            ".compute",
            ".usf",
            ".ush",
        }
    ),
    "animation": frozenset({".anim", ".animation"}),
    "skeleton": frozenset({".skel", ".skeleton", ".rig"}),
    "package": frozenset({".uasset", ".umap", ".uexp"}),
    "archive": frozenset({".pak", ".ucas", ".utoc", ".bundle", ".unity3d"}),
    "metadata": frozenset({".meta", ".json", ".yaml", ".yml", ".xml"}),
}
_KNOWN_KINDS = frozenset((*_KIND_EXTENSIONS, "binary", "other"))
_WINDOWS_REPARSE_POINT = 0x0400


class AssetCatalogError(RuntimeError):
    """A fail-closed unpacked-asset catalog error."""


class AssetCatalogScanLimitError(AssetCatalogError):
    """A bounded scan stopped before publication and retained the old catalog."""

    def __init__(self, *, maximum_files: int, observed_files: int):
        super().__init__(
            "asset root exceeds max_files; narrow the root or raise the admitted bound"
        )
        self.maximum_files = maximum_files
        self.observed_files = observed_files


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _filesystem_root(path: Path) -> bool:
    return path.parent == path


def _is_redirected(path: Path) -> bool:
    """Reject symbolic links and Windows junction/other reparse entries."""

    try:
        status = path.lstat()
    except OSError:
        return path.is_symlink()
    if path.is_symlink() or bool(
        int(getattr(status, "st_file_attributes", 0)) & _WINDOWS_REPARSE_POINT
    ):
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _validate_root(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise AssetCatalogError("asset root must be absolute")
    if _is_redirected(raw):
        raise AssetCatalogError("asset root must not be a symbolic link or reparse point")
    root = raw.resolve(strict=True)
    if not root.is_dir() or _filesystem_root(root):
        raise AssetCatalogError("asset root must be a non-root directory")
    return root


def _validate_database_path(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise AssetCatalogError("catalog database path must be absolute")
    if raw.exists() and _is_redirected(raw):
        raise AssetCatalogError("catalog database must not be redirected")
    parent = raw.parent.resolve(strict=True)
    if _is_redirected(parent) or not parent.is_dir():
        raise AssetCatalogError("catalog database parent is redirected or unavailable")
    return parent / raw.name


def _classify(path: PurePosixPath) -> str:
    suffix = path.suffix.casefold()
    for kind, extensions in _KIND_EXTENSIONS.items():
        if suffix in extensions:
            return kind
    return "binary" if suffix in {".bin", ".dat", ".bytes"} else "other"


def _format_name(path: PurePosixPath) -> str:
    suffix = path.suffix.casefold().lstrip(".")
    return suffix or "extensionless"


def _hash_regular_file(path: Path, expected_size: int) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(HASH_CHUNK_BYTES)
            if not block:
                break
            total += len(block)
            digest.update(block)
    if total != expected_size:
        raise AssetCatalogError("asset changed size while it was hashed: %s" % path)
    return digest.hexdigest()


def _safe_reference(source_relative: str, raw_reference: str) -> str:
    value = unquote(str(raw_reference or "").strip()).replace("\\", "/")
    if not value or "\x00" in value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or value.startswith("/"):
        return ""
    reference = PurePosixPath(value)
    combined = PurePosixPath(source_relative).parent / reference
    stack: list[str] = []
    for part in combined.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                return ""
            stack.pop()
        else:
            stack.append(part)
    return "/".join(stack)


def _bounded_text(data: bytes) -> tuple[str, str]:
    try:
        return data.decode("utf-8-sig", errors="strict"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "utf-8-replacement"


def _bounded_reference_result(
    metadata: Mapping[str, Any], references: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Bound persisted relationship observations and disclose lost coverage."""

    result_metadata = dict(metadata)
    observed = len(references)
    indexed = min(observed, MAX_INDEXED_REFERENCES_PER_ASSET)
    result_metadata["referenceCountObserved"] = observed
    result_metadata["referenceCountIndexed"] = indexed
    result_metadata["referencesTruncated"] = observed > indexed
    return result_metadata, [dict(item) for item in references[:indexed]]


def _gltf_document(data: bytes, suffix: str) -> Mapping[str, Any] | None:
    payload = data
    if suffix == ".glb":
        if len(data) < 20 or data[:4] != b"glTF":
            return None
        version, total_length = struct.unpack_from("<II", data, 4)
        if version != 2 or total_length > len(data):
            return None
        chunk_length, chunk_type = struct.unpack_from("<II", data, 12)
        if chunk_type != 0x4E4F534A or 20 + chunk_length > total_length:
            return None
        payload = data[20 : 20 + chunk_length]
    try:
        value = json.loads(payload.decode("utf-8-sig", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _parse_gltf(data: bytes, suffix: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    document = _gltf_document(data, suffix)
    if document is None:
        return {"parser": "gltf-v2", "valid": False}, []
    meshes = document.get("meshes") if isinstance(document.get("meshes"), list) else []
    materials = (
        document.get("materials") if isinstance(document.get("materials"), list) else []
    )
    accessors = (
        document.get("accessors") if isinstance(document.get("accessors"), list) else []
    )
    primitives = sum(
        len(item.get("primitives", []))
        for item in meshes
        if isinstance(item, Mapping) and isinstance(item.get("primitives"), list)
    )
    references: list[dict[str, Any]] = []
    for relation, key in (("uses-buffer", "buffers"), ("uses-texture", "images")):
        values = document.get(key)
        if not isinstance(values, list):
            continue
        for index, item in enumerate(values):
            if isinstance(item, Mapping) and isinstance(item.get("uri"), str):
                references.append(
                    {
                        "relation": relation,
                        "reference": item["uri"],
                        "evidence": {"parser": "gltf-v2", "jsonPath": "%s[%d].uri" % (key, index)},
                    }
                )
    metadata = {
        "parser": "gltf-v2",
        "valid": True,
        "meshCount": len(meshes),
        "primitiveCount": primitives,
        "materialCount": len(materials),
        "textureCount": len(document.get("textures", []))
        if isinstance(document.get("textures"), list)
        else 0,
        "skinCount": len(document.get("skins", []))
        if isinstance(document.get("skins"), list)
        else 0,
        "animationCount": len(document.get("animations", []))
        if isinstance(document.get("animations"), list)
        else 0,
        "accessorCount": len(accessors),
        "extensionsUsed": sorted(
            str(item)[:128]
            for item in (document.get("extensionsUsed") or [])
            if isinstance(item, str)
        )[:128],
    }
    return metadata, references


def _parse_obj(text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    counts = Counter()
    references: list[dict[str, Any]] = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        token, _, rest = line.partition(" ")
        if token in {"v", "vt", "vn", "f", "o", "g", "usemtl"}:
            counts[token] += 1
        elif token.casefold() == "mtllib":
            for item in rest.split():
                references.append(
                    {
                        "relation": "uses-material-library",
                        "reference": item,
                        "evidence": {"parser": "obj", "line": line_number},
                    }
                )
    return {
        "parser": "obj",
        "positionCount": counts["v"],
        "texcoordCount": counts["vt"],
        "normalCount": counts["vn"],
        "faceCount": counts["f"],
        "objectCount": counts["o"],
        "groupCount": counts["g"],
        "materialUseCount": counts["usemtl"],
    }, references


def _parse_mtl(text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    material_count = 0
    references: list[dict[str, Any]] = []
    texture_commands = {
        "map_ka",
        "map_kd",
        "map_ks",
        "map_ke",
        "map_d",
        "map_bump",
        "bump",
        "disp",
        "decal",
        "norm",
    }
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        token, _, rest = line.partition(" ")
        lowered = token.casefold()
        if lowered == "newmtl":
            material_count += 1
        elif lowered in texture_commands and rest.strip():
            # Texture options precede the final filename. Quoted paths are not
            # fully parsed here; the exact source line remains evidence.
            candidate = rest.strip().split()[-1]
            references.append(
                {
                    "relation": "uses-texture",
                    "reference": candidate.strip('"'),
                    "evidence": {"parser": "mtl", "line": line_number, "command": token},
                }
            )
    return {"parser": "mtl", "materialCount": material_count}, references


def _parse_shader(text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    references = [
        {
            "relation": "includes-shader-source",
            "reference": match.group(1),
            "evidence": {
                "parser": "preprocessor-include",
                "byteOffset": len(text[: match.start()].encode("utf-8", errors="replace")),
            },
        }
        for match in _SHADER_INCLUDE_RE.finditer(text)
    ]
    return {
        "parser": "shader-source",
        "includeCount": len(references),
        "lineCount": text.count("\n") + (1 if text else 0),
    }, references


def _parse_unity_text(text: str, suffix: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    declared = _UNITY_GUID_RE.search(text) if suffix == ".meta" else None
    references: list[dict[str, Any]] = []
    for match in _UNITY_REFERENCE_GUID_RE.finditer(text):
        guid = match.group(1).lower()
        if declared is not None and guid == declared.group(1).lower():
            continue
        references.append(
            {
                "relation": "references-unity-guid",
                "guid": guid,
                "evidence": {"parser": "unity-yaml-guid", "characterOffset": match.start(1)},
            }
        )
    return {
        "parser": "unity-text",
        "declaredGuid": declared.group(1).lower() if declared else "",
        "referencedGuidCount": len(references),
    }, references


def _parse_json_asset(data: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        value = json.loads(data.decode("utf-8-sig", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"parser": "json", "valid": False}, []
    if not isinstance(value, Mapping):
        return {"parser": "json", "valid": True, "rootType": type(value).__name__}, []
    schema = str(value.get("schema", "") or "")
    references: list[dict[str, Any]] = []
    if schema == "uex.blender-bundle.v1":
        for index, mesh in enumerate(value.get("meshes", [])):
            if isinstance(mesh, Mapping) and isinstance(mesh.get("relativePath"), str):
                references.append(
                    {
                        "relation": "contains-lod-geometry",
                        "reference": mesh["relativePath"],
                        "evidence": {"parser": schema, "jsonPath": "meshes[%d].relativePath" % index},
                    }
                )
        if isinstance(value.get("ueModel"), str) and value["ueModel"]:
            references.append(
                {
                    "relation": "contains-uemodel",
                    "reference": value["ueModel"],
                    "evidence": {"parser": schema, "jsonPath": "ueModel"},
                }
            )
        for index, texture in enumerate(value.get("textures", [])):
            if isinstance(texture, Mapping) and isinstance(texture.get("relativePath"), str):
                references.append(
                    {
                        "relation": "uses-texture",
                        "reference": texture["relativePath"],
                        "evidence": {"parser": schema, "jsonPath": "textures[%d].relativePath" % index},
                    }
                )
        for index, material in enumerate(value.get("materials", [])):
            if not isinstance(material, Mapping):
                continue
            for field, relation in (
                ("materialJson", "uses-material-record"),
                ("rawMaterialJson", "uses-raw-material-record"),
            ):
                if isinstance(material.get(field), str) and material[field]:
                    references.append(
                        {
                            "relation": relation,
                            "reference": material[field],
                            "evidence": {"parser": schema, "jsonPath": "materials[%d].%s" % (index, field)},
                        }
                    )
            base_color = material.get("baseColor")
            if isinstance(base_color, Mapping) and isinstance(base_color.get("relativePath"), str):
                references.append(
                    {
                        "relation": "uses-base-color-texture",
                        "reference": base_color["relativePath"],
                        "evidence": {"parser": schema, "jsonPath": "materials[%d].baseColor.relativePath" % index},
                    }
                )
            for parameter_field in (
                "directTextureParameters",
                "textureParameters",
            ):
                parameters = material.get(parameter_field)
                if not isinstance(parameters, list):
                    continue
                for parameter_index, parameter in enumerate(parameters):
                    if (
                        isinstance(parameter, Mapping)
                        and isinstance(parameter.get("relativePath"), str)
                        and parameter["relativePath"]
                    ):
                        references.append(
                            {
                                "relation": "uses-texture-parameter",
                                "reference": parameter["relativePath"],
                                "evidence": {
                                    "parser": schema,
                                    "jsonPath": "materials[%d].%s[%d].relativePath"
                                    % (index, parameter_field, parameter_index),
                                },
                            }
                        )
        materials = value.get("materials") if isinstance(value.get("materials"), list) else []
        textures = value.get("textures") if isinstance(value.get("textures"), list) else []
        return {
            "parser": schema,
            "valid": True,
            "sourceAsset": str(value.get("sourceAsset", "") or "")[:4096],
            "assetType": str(value.get("assetType", "") or "")[:128],
            "meshName": str(value.get("meshName", "") or "")[:512],
            "skeleton": str(value.get("skeleton", "") or "")[:4096],
            "boneCount": value.get("boneCount"),
            "sourceLodCount": value.get("sourceLodCount"),
            "exportedLodCount": len(value.get("meshes", []))
            if isinstance(value.get("meshes"), list)
            else 0,
            "morphTargetCount": value.get("morphTargetCount"),
            "materialCount": len(materials),
            "textureCount": len(textures),
            "warningCount": len(value.get("warnings", []))
            if isinstance(value.get("warnings"), list)
            else 0,
            "materialApproximationOnly": True,
        }, references
    if schema in {"uex.blender-assembly.v1", "uex.blender-validation.v1"}:
        bundles = value.get("bundles") if isinstance(value.get("bundles"), list) else []
        return {
            "parser": schema,
            "valid": True,
            "status": str(value.get("status", "") or "")[:64],
            "ok": value.get("ok") if isinstance(value.get("ok"), bool) else None,
            "sourceAssetCount": len(value.get("sourceAssets", []))
            if isinstance(value.get("sourceAssets"), list)
            else 0,
            "bundleCount": len(bundles),
            "geometrySource": str(value.get("geometrySource", "") or "")[:64],
            "validationReportPresent": bool(value.get("validationReport")),
        }, []
    # UEX asset-report manifests are completion markers even when older/newer
    # builds do not expose a stable schema token. Recognise them by their exact
    # four portable outputs but keep statistics in the report JSON itself.
    output_names = {
        "asset-report.html",
        "asset-report.json",
        "asset-summary.csv",
    }
    encoded = _canonical_bytes(value).decode("utf-8")
    if all(name in encoded for name in output_names):
        return {
            "parser": "uex-asset-report-manifest",
            "valid": True,
            "completionMarker": True,
            "inventoryId": value.get("inventoryId", value.get("inventory")),
            "portableOutputs": sorted(output_names | {"asset-report-manifest.json"}),
        }, []
    return {
        "parser": "json",
        "valid": True,
        "schema": schema,
        "reason": "no fixed asset relationship parser for this JSON schema",
    }, []


def _parse_png(data: bytes) -> dict[str, Any]:
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return {"parser": "png-header", "valid": False}
    width, height, bit_depth, color_type = struct.unpack_from(">IIBB", data, 16)
    chunks: set[bytes] = set()
    offset = 8
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4 : offset + 8]
        if length > len(data) - offset - 12:
            break
        chunks.add(kind)
        offset += 12 + length
        if kind == b"IEND":
            break
    return {
        "parser": "png-header",
        "valid": True,
        "width": width,
        "height": height,
        "bitDepth": bit_depth,
        "colorType": color_type,
        "colorSpaceEvidence": (
            "srgb-chunk" if b"sRGB" in chunks else "gamma-chunk" if b"gAMA" in chunks else "unspecified"
        ),
    }


def _parse_dds(data: bytes) -> dict[str, Any]:
    if len(data) < 128 or data[:4] != b"DDS " or struct.unpack_from("<I", data, 4)[0] != 124:
        return {"parser": "dds-header", "valid": False}
    height, width = struct.unpack_from("<II", data, 12)
    fourcc = data[84:88].rstrip(b"\x00").decode("ascii", errors="replace")
    dxgi = struct.unpack_from("<I", data, 128)[0] if fourcc == "DX10" and len(data) >= 148 else None
    srgb_dxgi = {29, 72, 75, 78, 91, 93, 99}
    return {
        "parser": "dds-header",
        "valid": True,
        "width": width,
        "height": height,
        "fourCC": fourcc,
        "dxgiFormat": dxgi,
        "colorSpaceEvidence": "srgb-dxgi-format" if dxgi in srgb_dxgi else "unspecified",
    }


def _parse_ktx2(data: bytes) -> dict[str, Any]:
    magic = b"\xabKTX 20\xbb\r\n\x1a\n"
    if len(data) < 68 or data[:12] != magic:
        return {"parser": "ktx2-header", "valid": False}
    vk_format, type_size, width, height, depth, layers, faces, levels = struct.unpack_from(
        "<8I", data, 12
    )
    # Vulkan sRGB formats are not contiguous across every extension. This fixed
    # set covers the core uncompressed/BC/ETC2/ASTC families most often unpacked.
    srgb_vk = {
        43, 50, 57, 64, 71, 78, 85, 92, 99, 106, 113, 120, 127, 134,
        137, 140, 143, 146, 149, 152, 155, 158, 161, 164, 167, 170, 173, 176,
    }
    return {
        "parser": "ktx2-header",
        "valid": True,
        "vkFormat": vk_format,
        "typeSize": type_size,
        "width": width,
        "height": height,
        "depth": depth,
        "layers": layers,
        "faces": faces,
        "levels": levels,
        "colorSpaceEvidence": "srgb-vk-format" if vk_format in srgb_vk else "unspecified",
    }


def _parse_jpeg(data: bytes) -> dict[str, Any]:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return {"parser": "jpeg-header", "valid": False}
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break
        length = struct.unpack_from(">H", data, offset)[0]
        if length < 2 or offset + length > len(data):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and length >= 8:
            bits = data[offset + 2]
            height, width = struct.unpack_from(">HH", data, offset + 3)
            return {
                "parser": "jpeg-header",
                "valid": True,
                "width": width,
                "height": height,
                "bitDepth": bits,
                "colorSpaceEvidence": "unspecified",
            }
        offset += length
    return {"parser": "jpeg-header", "valid": False}


# Bump whenever a parser's output shape changes so incremental scans re-parse
# unchanged files instead of carrying old metadata forward.
PARSER_REVISION = 3


MAX_IMAGE_FINGERPRINT_BYTES = 2 * 1024 * 1024


def _image_fingerprint(data: bytes, suffix: str) -> dict[str, Any]:
    """Thumbnail fingerprint for a plain image file (PNG/DDS), bounded."""

    from .symbols.imaging import ImageDecodeError, decode_dds, decode_png, fingerprint

    if len(data) > MAX_IMAGE_FINGERPRINT_BYTES:
        return {"available": False, "reason": "image exceeds the %d-byte fingerprint bound" % MAX_IMAGE_FINGERPRINT_BYTES}
    try:
        if suffix == ".png":
            rgba, width, height = decode_png(data)
            fmt = "PNG"
        else:
            rgba, width, height, fmt = decode_dds(data)
        if width * height > 1024 * 1024:
            return {"available": False, "reason": "image is larger than 1M pixels; fingerprint skipped"}
        result = fingerprint(rgba, width, height)
        result.update({"available": True, "pixelFormat": fmt, "sizeX": width, "sizeY": height})
        return result
    except (ImageDecodeError, ValueError, struct.error, zlib.error, IndexError) as error:
        return {"available": False, "reason": str(error)[:200]}


def _generic_probe(relative: str, data: bytes, engine_context: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from .symbols.generic_probe import mention_references, probe

    metadata = {"parser": "generic-content-probe/v1", "engine": getattr(engine_context, "engine", "unknown")}
    # descriptors (materials, configs) are small; large blobs are geometry or
    # animation whose floats would only add noise to the value index
    metadata.update(probe(data, relative, numeric=len(data) <= 1024 * 1024))
    stem_index = getattr(engine_context, "stem_index", None) or {}
    references = mention_references(metadata.get("strings") or [], metadata.get("pathReferences") or [],
                                    stem_index, PurePosixPath(relative).stem) if stem_index else []
    return metadata, references


def _parse_asset(
    relative: str, kind: str, data: bytes, engine_context: Any = None, sibling_class: str = ""
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    suffix = PurePosixPath(relative).suffix.casefold()
    if kind == "package":
        from .symbols.engines import parse_engine_asset

        parsed = parse_engine_asset(engine_context, relative, data, sibling_class)
        if parsed is not None:
            return _bounded_reference_result(*parsed)
    if kind in {"other", "binary", "package"} or (kind == "metadata" and suffix not in {".json", ".meta", ".yaml", ".yml", ".xml"}):
        # Generic layer: any engine, any format — strings, numbers, embedded
        # images, embedded shaders, and name mentions resolved to files.
        return _bounded_reference_result(*_generic_probe(relative, data, engine_context))
    if suffix in {".gltf", ".glb"}:
        metadata, references = _parse_gltf(data, suffix)
    elif suffix == ".png":
        metadata, references = _parse_png(data), []
        metadata = dict(metadata)
        metadata["textureFingerprint"] = _image_fingerprint(data, suffix)
    elif suffix == ".dds":
        metadata, references = _parse_dds(data), []
        metadata = dict(metadata)
        metadata["textureFingerprint"] = _image_fingerprint(data, suffix)
    elif suffix == ".ktx2":
        metadata, references = _parse_ktx2(data), []
    elif suffix in {".jpg", ".jpeg"}:
        metadata, references = _parse_jpeg(data), []
    elif suffix == ".json":
        metadata, references = _parse_json_asset(data)
    elif suffix in {".obj", ".mtl", ".mat", ".material", ".meta", ".yaml", ".yml"} or kind == "shader":
        text, encoding = _bounded_text(data)
        if suffix == ".obj":
            metadata, references = _parse_obj(text)
        elif suffix == ".mtl":
            metadata, references = _parse_mtl(text)
        elif kind == "shader":
            metadata, references = _parse_shader(text)
        else:
            metadata, references = _parse_unity_text(text, suffix)
        metadata["textEncoding"] = encoding
        if suffix in {".mtl", ".mat", ".material", ".yaml", ".yml"}:
            metadata = dict(metadata)
            metadata["numericValues"] = _text_numeric_values(text)
    else:
        metadata, references = (
            {"parser": "none", "reason": "format metadata requires an admitted provider"},
            [],
        )
    return _bounded_reference_result(metadata, references)


def _asset_identity(relative: str, content_sha256: str, size: int, modified_ns: int) -> str:
    material = {
        "relativePath": relative,
        "contentSha256": content_sha256,
        "size": size,
        "modifiedNs": 0 if content_sha256 else modified_ns,
    }
    return "asset-v1-" + _canonical_sha256(material)


def _database_connection(path: Path, *, read_only: bool) -> sqlite3.Connection:
    connection = (
        sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        if read_only
        else sqlite3.connect(str(path))
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if read_only:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA temp_store = MEMORY")
    return connection


def _validate_schema(connection: sqlite3.Connection) -> dict[str, str]:
    tables = {
        str(row["name"])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if not str(row["name"]).startswith("sqlite_")
    }
    if not {"metadata", "assets", "relationships"}.issubset(tables):
        raise AssetCatalogError("asset catalog database is incomplete")
    metadata = {
        str(row["key"]): str(row["value"])
        for row in connection.execute("SELECT key, value FROM metadata")
    }
    if metadata.get("schema") != CATALOG_SCHEMA or metadata.get("schemaVersion") != str(DATABASE_SCHEMA_VERSION):
        raise AssetCatalogError("asset catalog database schema is unsupported")
    if not _SHA256_RE.fullmatch(metadata.get("catalogSha256", "")):
        raise AssetCatalogError("asset catalog database has no valid catalog identity")
    return metadata


def _validate_catalog_identity(
    connection: sqlite3.Connection, metadata: Mapping[str, str]
) -> None:
    """Recompute the embedded manifest hash from read-only SQLite contents."""

    assets = [
        {
            "assetId": row["asset_id"],
            "relativePath": row["relative_path"],
            "kind": row["kind"],
            "format": row["format"],
            "size": int(row["size"]),
            "contentSha256": row["content_sha256"],
            "hashStatus": row["hash_status"],
            "parseStatus": row["parse_status"],
            "metadata": json.loads(row["metadata_json"]),
        }
        for row in connection.execute(
            "SELECT asset_id, relative_path, kind, format, size, content_sha256, "
            "hash_status, parse_status, metadata_json FROM assets"
        )
    ]
    assets.sort(key=lambda item: str(item["relativePath"]).casefold())
    relationships = [
        {
            "sourceAssetId": row["source_asset_id"],
            "relation": row["relation"],
            "targetAssetId": row["target_asset_id"],
            "targetPath": row["target_path"],
            "targetGuid": row["target_guid"],
            "evidenceKind": row["evidence_kind"],
            "confidence": float(row["confidence"]),
            "verified": bool(row["verified"]),
            "evidence": json.loads(row["evidence_json"]),
        }
        for row in connection.execute(
            "SELECT source_asset_id, relation, target_asset_id, target_path, target_guid, "
            "evidence_kind, confidence, verified, evidence_json FROM relationships"
        )
    ]
    relationships.sort(
        key=lambda item: (
            str(item["sourceAssetId"]),
            str(item["relation"]),
            str(item.get("targetAssetId") or ""),
            str(item.get("targetPath") or ""),
            str(item.get("targetGuid") or ""),
        )
    )
    actual = _canonical_sha256(
        {
            "schema": CATALOG_SCHEMA,
            "schemaVersion": CATALOG_VERSION,
            "rootPath": os.path.normcase(str(metadata.get("rootPath", ""))),
            "buildId": str(metadata.get("buildId", "")),
            "assets": assets,
            "relationships": relationships,
        }
    )
    if actual != metadata.get("catalogSha256"):
        raise AssetCatalogError(
            "asset catalog contents do not match its embedded manifest hash"
        )


def _safe_json(text: Any) -> dict[str, Any]:
    try:
        value = json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _previous_records(path: Path, root: Path, build_id: str) -> dict[str, dict[str, Any]]:
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        with closing(_database_connection(path, read_only=True)) as db:
            metadata = _validate_schema(db)
            _validate_catalog_identity(db, metadata)
            if metadata.get("rootPath") != str(root) or metadata.get("buildId", "") != build_id:
                return {}
            result: dict[str, dict[str, Any]] = {}
            for row in db.execute(
                "SELECT asset_id, relative_path, kind, format, size, modified_ns, content_sha256, "
                "hash_status, parse_status, metadata_json FROM assets"
            ):
                relations = [
                    {
                        "relation": relation["relation"],
                        "targetPath": relation["target_path"],
                        "catalogRootRelative": bool(relation["target_path"]),
                        "targetGuid": relation["target_guid"],
                        "evidence": json.loads(relation["evidence_json"]),
                    }
                    for relation in db.execute(
                        "SELECT relation, target_path, target_guid, evidence_json FROM relationships "
                        "WHERE source_asset_id=? AND evidence_kind='parser-reference'",
                        (row["asset_id"],),
                    )
                ]
                result[str(row["relative_path"])] = {
                    "assetId": row["asset_id"],
                    "relativePath": row["relative_path"],
                    "kind": row["kind"],
                    "format": row["format"],
                    "size": int(row["size"]),
                    "modifiedNs": int(row["modified_ns"]),
                    "contentSha256": row["content_sha256"],
                    "hashStatus": row["hash_status"],
                    "parseStatus": row["parse_status"],
                    "metadata": json.loads(row["metadata_json"]),
                    "references": relations,
                }
            return result
    except (AssetCatalogError, OSError, sqlite3.Error, json.JSONDecodeError):
        return {}


_TEXT_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])-?\d+\.\d{4,}(?:[eE][-+]?\d+)?(?![A-Za-z0-9_.])")


def _text_numeric_values(text: str, limit: int = 512) -> list[str]:
    """Distinctive decimal literals in a text asset (Unity YAML, MTL, JSON)."""

    from .symbols.engines.unreal_package import is_distinctive_value, value_key

    values: list[str] = []
    seen: set[str] = set()
    for match in _TEXT_NUMBER.finditer(text):
        try:
            value = float(match.group(0))
        except ValueError:
            continue
        # Round-trip through float32 so the key matches a GPU-side lane.
        value = struct.unpack("<f", struct.pack("<f", value))[0]
        if not is_distinctive_value(value):
            continue
        key = value_key(value)
        if key in seen:
            continue
        seen.add(key)
        values.append(key)
        if len(values) >= limit:
            break
    return values


_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_TOKEN_FIELDS = ("names", "textures", "parentMaterials", "materialFunctions", "shaderTypes",
                 "vertexFactories", "defines", "className", "exportClasses", "meshes",
                 "parameterCollections", "parameters", "textureParameters", "keywords",
                 "strings", "pathReferences")
MAX_TOKENS_PER_ASSET = 4096


def asset_tokens(relative: str, metadata: Mapping[str, Any]) -> list[str]:
    """Lower-case retrieval tokens: path words, identifiers, camel-case parts."""

    seen: dict[str, None] = {}

    def add(text: Any) -> None:
        if not isinstance(text, str) or not text:
            return
        if text.startswith("/"):
            text = text.rsplit("/", 1)[-1].split(".")[-1]
        for word in _TOKEN_SPLIT.split(text):
            if not word:
                continue
            seen.setdefault(word.casefold(), None)
            parts = [part for part in _CAMEL.split(word) if len(part) >= 2]
            if len(parts) > 1:
                for part in parts:
                    seen.setdefault(part.casefold(), None)

    add(PurePosixPath(relative).stem)
    for part in PurePosixPath(relative).parts[:-1]:
        add(part)
    for field in _TOKEN_FIELDS:
        value = metadata.get(field)
        if isinstance(value, str):
            add(value)
        elif isinstance(value, list):
            for item in value[:1024]:
                add(item if isinstance(item, str) else (item.get("name") if isinstance(item, Mapping) else None))
        if len(seen) >= MAX_TOKENS_PER_ASSET:
            break
    return list(seen)[:MAX_TOKENS_PER_ASSET]


def _fts5_available(db: sqlite3.Connection) -> bool:
    try:
        db.execute("CREATE VIRTUAL TABLE temp.__fts_probe USING fts5(x)")
        db.execute("DROP TABLE temp.__fts_probe")
        return True
    except sqlite3.Error:
        return False


def _create_value_index(db: sqlite3.Connection, records: Sequence[Mapping[str, Any]]) -> None:
    """Persist distinctive numeric values per asset for cbuffer-value matching."""

    db.execute(
        "CREATE TABLE asset_values (value_key TEXT NOT NULL, asset_id TEXT NOT NULL, "
        "PRIMARY KEY(value_key, asset_id)) WITHOUT ROWID"
    )
    rows = []
    for row in records:
        metadata = row.get("metadata") or {}
        for key in metadata.get("numericValues") or ():
            if isinstance(key, str) and len(key) <= 24:
                rows.append((key, row["assetId"]))
    db.executemany("INSERT OR IGNORE INTO asset_values(value_key, asset_id) VALUES(?, ?)", rows)
    db.execute("INSERT INTO metadata(key, value) VALUES('valueIndex', ?)", (str(len(rows)),))


def _create_thumbnail_index(db: sqlite3.Connection, records: Sequence[Mapping[str, Any]]) -> None:
    """Persist texture fingerprints (8 dihedral dHashes + mean colour) per asset."""

    db.execute(
        "CREATE TABLE asset_thumbnails (asset_id TEXT PRIMARY KEY, v0 INTEGER, v1 INTEGER, v2 INTEGER, v3 INTEGER, "
        "v4 INTEGER, v5 INTEGER, v6 INTEGER, v7 INTEGER, mean_r INTEGER, mean_g INTEGER, mean_b INTEGER, "
        "width INTEGER, height INTEGER, flat INTEGER, pixel_format TEXT)"
    )
    rows = []
    for row in records:
        fingerprint = (row.get("metadata") or {}).get("textureFingerprint")
        if not isinstance(fingerprint, Mapping) or fingerprint.get("available") is not True:
            continue
        variants = fingerprint.get("dhashVariants") or []
        if len(variants) != 8:
            continue
        mean = fingerprint.get("meanColor") or [0, 0, 0, 0]
        # SQLite integers are signed 64-bit: store as signed
        signed = [int(value) - (1 << 64) if int(value) >= (1 << 63) else int(value) for value in variants]
        rows.append((row["assetId"], *signed, int(mean[0]), int(mean[1]), int(mean[2]),
                     int(fingerprint.get("sizeX") or 0), int(fingerprint.get("sizeY") or 0),
                     1 if fingerprint.get("flat") else 0, str(fingerprint.get("pixelFormat") or "")))
    db.executemany("INSERT OR IGNORE INTO asset_thumbnails VALUES(%s)" % ",".join("?" * 16), rows)
    db.execute("INSERT INTO metadata(key, value) VALUES('thumbnailIndex', ?)", (str(len(rows)),))


def _create_token_index(db: sqlite3.Connection, records: Sequence[Mapping[str, Any]]) -> None:
    """Persist per-asset retrieval tokens (FTS5 when available, else plain)."""

    rows = []
    for row in records:
        tokens = asset_tokens(str(row["relativePath"]), row.get("metadata") or {})
        if tokens:
            rows.append((row["assetId"], " ".join(tokens)))
    if _fts5_available(db):
        db.execute("CREATE VIRTUAL TABLE asset_tokens USING fts5(asset_id UNINDEXED, tokens, tokenize='unicode61')")
        db.execute("INSERT INTO metadata(key, value) VALUES('tokenIndex', 'fts5')")
    else:
        db.execute("CREATE TABLE asset_tokens (asset_id TEXT PRIMARY KEY, tokens TEXT NOT NULL)")
        db.execute("INSERT INTO metadata(key, value) VALUES('tokenIndex', 'plain')")
    db.executemany("INSERT INTO asset_tokens(asset_id, tokens) VALUES(?, ?)", rows)


def _create_database(
    path: Path,
    *,
    root: Path,
    build_id: str,
    catalog_sha256: str,
    scan_mode: str,
    records: Sequence[Mapping[str, Any]],
    relationships: Sequence[Mapping[str, Any]],
    engine: Mapping[str, Any] | None = None,
    complete: bool = True,
    progress_note: Mapping[str, Any] | None = None,
) -> None:
    temporary = path.with_name(".%s.tmp-%s" % (path.name, uuid.uuid4().hex))
    try:
        with closing(_database_connection(temporary, read_only=False)) as db:
            db.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE assets (
                    asset_id TEXT PRIMARY KEY,
                    relative_path TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    format TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    modified_ns INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    hash_status TEXT NOT NULL,
                    parse_status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE INDEX assets_kind_idx ON assets(kind, format);
                CREATE INDEX assets_hash_idx ON assets(content_sha256);
                CREATE TABLE relationships (
                    source_asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
                    relation TEXT NOT NULL,
                    target_asset_id TEXT REFERENCES assets(asset_id) ON DELETE SET NULL,
                    target_path TEXT NOT NULL,
                    target_guid TEXT NOT NULL,
                    evidence_kind TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    verified INTEGER NOT NULL,
                    evidence_json TEXT NOT NULL,
                    UNIQUE(source_asset_id, relation, target_asset_id, target_path, target_guid, evidence_kind)
                );
                CREATE INDEX relationships_source_idx ON relationships(source_asset_id);
                CREATE INDEX relationships_target_idx ON relationships(target_asset_id);
                """
            )
            metadata = {
                "schema": CATALOG_SCHEMA,
                "schemaVersion": str(DATABASE_SCHEMA_VERSION),
                "catalogSha256": catalog_sha256,
                "rootPath": str(root),
                "buildId": build_id,
                "scanMode": scan_mode,
                "createdAt": _utc_now(),
                "engine": str((engine or {}).get("engine", "unknown")),
                "engineJson": _canonical_bytes(engine or {}).decode("utf-8"),
                "parserRevision": str(PARSER_REVISION),
                # A checkpoint of an interrupted/ongoing scan is a valid,
                # queryable catalog; ``complete`` says whether the walk ended.
                "complete": "true" if complete else "false",
                "progressJson": _canonical_bytes(dict(progress_note or {})).decode("utf-8"),
            }
            db.executemany(
                "INSERT INTO metadata(key, value) VALUES(?, ?)",
                sorted(metadata.items()),
            )
            db.executemany(
                "INSERT INTO assets(asset_id, relative_path, name, kind, format, size, "
                "modified_ns, content_sha256, hash_status, parse_status, metadata_json) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        row["assetId"],
                        row["relativePath"],
                        PurePosixPath(str(row["relativePath"])).name,
                        row["kind"],
                        row["format"],
                        row["size"],
                        row["modifiedNs"],
                        row["contentSha256"],
                        row["hashStatus"],
                        row["parseStatus"],
                        _canonical_bytes(row["metadata"]).decode("utf-8"),
                    )
                    for row in records
                ],
            )
            _create_token_index(db, records)
            _create_value_index(db, records)
            _create_thumbnail_index(db, records)
            db.executemany(
                "INSERT INTO relationships(source_asset_id, relation, target_asset_id, "
                "target_path, target_guid, evidence_kind, confidence, verified, evidence_json) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        row["sourceAssetId"],
                        row["relation"],
                        row.get("targetAssetId") or None,
                        row.get("targetPath", ""),
                        row.get("targetGuid", ""),
                        row["evidenceKind"],
                        row["confidence"],
                        1 if row["verified"] else 0,
                        _canonical_bytes(row["evidence"]).decode("utf-8"),
                    )
                    for row in relationships
                ],
            )
            db.commit()
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


_MATERIAL_CLASS_NAMES = frozenset({
    "Material", "MaterialInstanceConstant", "MaterialInstanceDynamic", "MaterialFunction",
    "MaterialFunctionInstance", "MaterialParameterCollection",
})
_HEADER_KINDS = frozenset({"material", "shader", "metadata", "scene", "mesh"})
HEADER_DEPTH_MAX_BYTES = 512 * 1024
HEADER_DEPTH_MATERIAL_EXPORT_MAX_BYTES = 8 * 1024 * 1024


_TEXTURE_CLASS_NAMES = frozenset({
    "Texture2D", "TextureCube", "VolumeTexture", "Texture2DArray", "TextureCubeArray", "TextureLightProfile",
})


def _deep_admitted(depth_mode: str, kind: str, relative: str, size: int, material_stems: set[str],
                   stem_classes: Mapping[str, str] | None = None) -> bool:
    """Decide whether this file gets hashed+parsed at the requested depth."""

    if depth_mode == "full":
        return True
    if depth_mode == "names":
        return False
    suffix = PurePosixPath(relative).suffix.casefold()
    if kind == "package":
        if suffix in {".uasset", ".umap"}:
            return size <= HEADER_DEPTH_MAX_BYTES * 4
        if suffix == ".uexp":
            # A material's cooked export carries shader types; a texture's
            # carries its small inline mips (the fingerprint). Files sort so
            # the .uasset header comes first and its class is known here.
            stem = PurePosixPath(relative).stem
            if stem in material_stems:
                return size <= HEADER_DEPTH_MATERIAL_EXPORT_MAX_BYTES
            if stem_classes is not None and stem_classes.get(stem) in _TEXTURE_CLASS_NAMES:
                return size <= HEADER_DEPTH_MAX_BYTES * 4
            return False
        return False
    if kind in _HEADER_KINDS:
        return size <= HEADER_DEPTH_MAX_BYTES * 8
    if kind == "texture":
        return size <= HEADER_DEPTH_MAX_BYTES  # header-only parsers, small files
    if kind in {"other", "binary", "metadata"}:
        # generic probe of small unknown files (Unity .asset/.prefab, custom
        # engine descriptors); big serialized blobs stay deferred
        return size <= HEADER_DEPTH_MAX_BYTES
    return False


class AssetCatalogService:
    """Build and query one active-project unpacked-asset relationship catalog."""

    def __init__(self, database_path: str | Path):
        self.database_path = _validate_database_path(database_path)

    @staticmethod
    def capabilities() -> dict[str, Any]:
        return {
            "schema": "renderdoc-ultra-unpacked-asset-capabilities/v1",
            "schemaVersion": 1,
            "catalogSchema": CATALOG_SCHEMA,
            "assetKinds": sorted(_KNOWN_KINDS),
            "fixedParsers": {
                "mesh": ["glTF 2.0 JSON/GLB", "OBJ"],
                "material": ["glTF 2.0 material references", "MTL", "Unity YAML/GUID"],
                "texture": ["PNG", "DDS/DX10", "KTX2", "JPEG"],
                "shader": ["bounded preprocessor include graph"],
            },
            "providerRequired": [
                "semantic FBX or Blend internals",
                "cooked engine containers",
                "proprietary mesh/material/shader layouts",
            ],
            "scanModes": {
                "incremental": "reuse exact path/size/mtime observations from the current project catalog",
                "full": "rehash and reparse every admitted regular file",
            },
            "relationshipEvidence": [
                "exact relative path reference",
                "exact Unity GUID reference",
                "identical content SHA-256",
            ],
            "policy": {
                "configuredActiveProjectRootOnly": True,
                "symlinksFollowed": False,
                "arbitraryModelPathsAccepted": False,
                "semanticNameGuessPersisted": False,
                "heuristicRelationshipsPersisted": False,
                "contentVerifiedDelivery": True,
                "catalogContentsHashVerifiedOnRead": True,
            },
            "limits": {
                "defaultMaximumFiles": DEFAULT_MAX_FILES,
                "hardMaximumFiles": HARD_MAX_FILES,
                "defaultMaximumFileSizeMiB": DEFAULT_MAX_FILE_SIZE_MB,
                "hardMaximumFileSizeMiB": HARD_MAX_FILE_SIZE_MB,
                "maximumQueryResults": MAX_QUERY_RESULTS,
                "maximumIndexedReferencesPerAsset": MAX_INDEXED_REFERENCES_PER_ASSET,
            },
        }

    def scan(
        self,
        asset_root: str | Path,
        *,
        build_id: str = "",
        scan_mode: str = "incremental",
        max_files: int = DEFAULT_MAX_FILES,
        max_file_size_mb: int = DEFAULT_MAX_FILE_SIZE_MB,
        depth: str = "full",
        scope_prefixes: Sequence[str] = (),
        progress: Any = None,
        checkpoint_seconds: float = 90.0,
    ) -> dict[str, Any]:
        """Index the tree.

        ``depth`` chooses how much work each file gets: ``names`` walks only
        (path tokens, no hashing/parsing — seconds); ``headers`` hashes and
        parses the small descriptive files (package headers, materials,
        shaders, metadata, and a material's cooked export sibling) and defers
        bulk/large files; ``full`` hashes and parses everything admitted by
        the size bounds. ``scope_prefixes`` restricts the deep work to files
        whose catalog-relative path starts with one of the prefixes; records
        outside the scope are reused from the previous catalog or recorded as
        deferred, so an on-demand hydration of a few candidates stays cheap.
        Deferred records can always be upgraded by a later scan.
        """

        root = _validate_root(asset_root)
        if not isinstance(build_id, str) or len(build_id) > 256 or "\x00" in build_id:
            raise AssetCatalogError("build_id must be a bounded string")
        mode = str(scan_mode or "").strip().casefold()
        if mode not in {"incremental", "full"}:
            raise AssetCatalogError("scan_mode must be incremental or full")
        depth_mode = str(depth or "full").strip().casefold()
        if depth_mode not in {"full", "headers", "names"}:
            raise AssetCatalogError("depth must be full, headers or names")
        prefixes: list[str] = []
        for prefix in scope_prefixes or ():
            text = str(prefix or "").replace("\\", "/").strip("/")
            if not text or ".." in text.split("/") or len(text) > 4096:
                raise AssetCatalogError("scope_prefixes entries must be relative catalog paths")
            prefixes.append(text.casefold())
        if len(prefixes) > 256:
            raise AssetCatalogError("scope_prefixes exceeds its bound")

        def in_scope(relative_path: str) -> bool:
            if not prefixes:
                return True
            folded = relative_path.casefold()
            return any(folded == prefix or folded.startswith(prefix + "/") or folded.startswith(prefix)
                       for prefix in prefixes)
        if isinstance(max_files, bool) or not isinstance(max_files, int) or not 1 <= max_files <= HARD_MAX_FILES:
            raise AssetCatalogError("max_files is outside the fixed scan bound")
        if (
            isinstance(max_file_size_mb, bool)
            or not isinstance(max_file_size_mb, int)
            or not 1 <= max_file_size_mb <= HARD_MAX_FILE_SIZE_MB
        ):
            raise AssetCatalogError("max_file_size_mb is outside the fixed scan bound")
        maximum_file_size = max_file_size_mb * 1024 * 1024
        previous = (
            _previous_records(self.database_path, root, build_id)
            if mode == "incremental"
            else {}
        )
        records: list[dict[str, Any]] = []
        skipped_symlinks = 0
        reused = 0
        hashed = 0
        parse_failures = 0
        walked = 0
        deferred = 0
        engine_context = self._detect_engine(root, max_files)
        def _publish(records: list[dict[str, Any]], *, complete: bool, progress_note: Mapping[str, Any] | None = None):
            """Write one catalog from ``records``; partial catalogs are checkpoints."""

            records.sort(key=lambda item: str(item["relativePath"]).casefold())
            by_path = {str(item["relativePath"]): item for item in records}
            casefold_paths: dict[str, list[dict[str, Any]]] = defaultdict(list)
            guid_assets: dict[str, list[dict[str, Any]]] = defaultdict(list)
            hash_assets: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for item in records:
                casefold_paths[str(item["relativePath"]).casefold()].append(item)
                guid = str(item["metadata"].get("declaredGuid", "") or "").lower()
                if guid:
                    guid_assets[guid].append(item)
                digest = str(item.get("contentSha256", "") or "")
                if digest:
                    hash_assets[digest].append(item)
            relationships: list[dict[str, Any]] = []
            for item in records:
                for reference in item.get("references", []):
                    relation = str(reference.get("relation", "references") or "references")[:128]
                    target_guid = str(reference.get("guid", "") or "").lower()
                    target_path = ""
                    candidates: list[dict[str, Any]] = []
                    if target_guid:
                        candidates = guid_assets.get(target_guid, [])
                    else:
                        if reference.get("catalogRootRelative") is True:
                            stored_target = PurePosixPath(
                                str(reference.get("targetPath", "") or "")
                            )
                            if (
                                not stored_target.is_absolute()
                                and stored_target.parts
                                and all(
                                    part not in {"", ".", ".."}
                                    for part in stored_target.parts
                                )
                            ):
                                target_path = stored_target.as_posix()
                        else:
                            target_path = _safe_reference(
                                str(item["relativePath"]),
                                str(
                                    reference.get(
                                        "reference", reference.get("targetPath", "")
                                    )
                                    or ""
                                ),
                            )
                        if target_path in by_path:
                            candidates = [by_path[target_path]]
                        elif target_path:
                            folded = casefold_paths.get(target_path.casefold(), [])
                            if len(folded) == 1:
                                candidates = folded
                    evidence = dict(reference.get("evidence") or {})
                    evidence["sourceRelativePath"] = item["relativePath"]
                    if candidates:
                        for candidate in candidates[:MAX_RELATIONSHIPS_PER_ASSET]:
                            relationships.append(
                                {
                                    "sourceAssetId": item["assetId"],
                                    "relation": relation,
                                    "targetAssetId": candidate["assetId"],
                                    "targetPath": target_path or candidate["relativePath"],
                                    "targetGuid": target_guid,
                                    "evidenceKind": "parser-reference",
                                    "confidence": 1.0 if len(candidates) == 1 else 0.5,
                                    "verified": len(candidates) == 1,
                                    "evidence": {
                                        **evidence,
                                        "candidateCount": len(candidates),
                                        "derivationClass": (
                                            "deterministic-derived"
                                            if len(candidates) == 1
                                            else "observed"
                                        ),
                                        "resolutionStatus": (
                                            "exact" if len(candidates) == 1 else "ambiguous"
                                        ),
                                    },
                                }
                            )
                    else:
                        relationships.append(
                            {
                                "sourceAssetId": item["assetId"],
                                "relation": relation,
                                "targetAssetId": None,
                                "targetPath": target_path,
                                "targetGuid": target_guid,
                                "evidenceKind": "parser-reference",
                                "confidence": 0.0,
                                "verified": False,
                                "evidence": {
                                    **evidence,
                                    "candidateCount": 0,
                                    "derivationClass": "observed",
                                    "resolutionStatus": "unresolved",
                                },
                            }
                        )
            for digest, duplicates in hash_assets.items():
                if len(duplicates) < 2:
                    continue
                canonical = sorted(duplicates, key=lambda item: str(item["relativePath"]).casefold())[0]
                for duplicate in duplicates:
                    if duplicate is canonical:
                        continue
                    relationships.append(
                        {
                            "sourceAssetId": duplicate["assetId"],
                            "relation": "same-content",
                            "targetAssetId": canonical["assetId"],
                            "targetPath": canonical["relativePath"],
                            "targetGuid": "",
                            "evidenceKind": "content-sha256",
                            "confidence": 1.0,
                            "verified": True,
                            "evidence": {
                                "contentSha256": digest,
                                "derivationClass": "deterministic-derived",
                                "resolutionStatus": "exact",
                            },
                        }
                    )
            deduplicated: dict[tuple[Any, ...], dict[str, Any]] = {}
            for relationship in relationships:
                key = (
                    relationship["sourceAssetId"],
                    relationship["relation"],
                    relationship.get("targetAssetId") or None,
                    relationship.get("targetPath", ""),
                    relationship.get("targetGuid", ""),
                    relationship["evidenceKind"],
                )
                existing = deduplicated.get(key)
                if existing is None:
                    deduplicated[key] = relationship
                    continue
                existing_evidence = existing["evidence"]
                existing_evidence["observationCount"] = int(
                    existing_evidence.get("observationCount", 1)
                ) + 1
            relationships = list(deduplicated.values())
            relationships.sort(
                key=lambda item: (
                    str(item["sourceAssetId"]),
                    str(item["relation"]),
                    str(item.get("targetAssetId") or ""),
                    str(item.get("targetPath") or ""),
                    str(item.get("targetGuid") or ""),
                )
            )
            catalog_material = {
                "schema": CATALOG_SCHEMA,
                "schemaVersion": CATALOG_VERSION,
                "rootPath": os.path.normcase(str(root)),
                "buildId": build_id,
                "assets": [
                    {
                        "assetId": item["assetId"],
                        "relativePath": item["relativePath"],
                        "kind": item["kind"],
                        "format": item["format"],
                        "size": item["size"],
                        "contentSha256": item["contentSha256"],
                        "hashStatus": item["hashStatus"],
                        "parseStatus": item["parseStatus"],
                        "metadata": item["metadata"],
                    }
                    for item in records
                ],
                "relationships": relationships,
            }
            catalog_sha256 = _canonical_sha256(catalog_material)
            _create_database(
                self.database_path,
                root=root,
                build_id=build_id,
                catalog_sha256=catalog_sha256,
                scan_mode=mode,
                records=records,
                relationships=relationships,
                engine=engine_context.to_dict(),
                complete=complete,
                progress_note=progress_note,
            )
            return catalog_sha256, relationships
        last_checkpoint = time.monotonic()
        checkpoints = 0
        visited: set[str] = set()

        def _checkpoint() -> None:
            nonlocal last_checkpoint, checkpoints
            # Files not walked yet keep their previous observation so a
            # checkpoint never shrinks coverage below the last catalog.
            pending = [dict(item) for path, item in previous.items() if path not in visited]
            _publish(records + pending, complete=False, progress_note={
                "walked": walked, "hashed": hashed, "reused": reused, "deferred": deferred,
                "checkpoint": checkpoints + 1, "at": _utc_now()})
            checkpoints += 1
            last_checkpoint = time.monotonic()
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            material_stems: set[str] = set()
            stem_classes: dict[str, str] = {}
            directory_path = Path(directory)
            if _is_redirected(directory_path):
                raise AssetCatalogError(
                    "asset scan traversed a symbolic link or reparse point"
                )
            safe_directories: list[str] = []
            for name in sorted(directory_names, key=str.casefold):
                candidate = directory_path / name
                if _is_redirected(candidate):
                    skipped_symlinks += 1
                else:
                    safe_directories.append(name)
            directory_names[:] = safe_directories
            for name in sorted(file_names, key=str.casefold):
                walked += 1
                if progress is not None and walked % 500 == 0:
                    try:
                        progress({"phase": "scan", "walked": walked, "hashed": hashed, "reused": reused, "deferred": deferred})
                    except Exception:
                        pass
                if walked > max_files:
                    raise AssetCatalogScanLimitError(
                        maximum_files=max_files, observed_files=walked
                    )
                path = directory_path / name
                if _is_redirected(path) or not path.is_file():
                    skipped_symlinks += 1
                    continue
                resolved = path.resolve(strict=True)
                if not _within(root, resolved):
                    raise AssetCatalogError("asset file escaped the configured root")
                before = resolved.stat()
                relative = resolved.relative_to(root).as_posix()
                size = int(before.st_size)
                modified_ns = int(before.st_mtime_ns)
                old = previous.get(relative)
                kind = _classify(PurePosixPath(relative))
                deep = in_scope(relative) and _deep_admitted(depth_mode, kind, relative, size, material_stems, stem_classes)
                old_current = bool(
                    old is not None
                    and old["size"] == size
                    and old["modifiedNs"] == modified_ns
                )
                old_complete = bool(
                    old_current
                    and old.get("contentSha256")
                    and (
                        old.get("parseStatus") not in {"parsed", "provider-required", "parse-error"}
                        or old.get("metadata", {}).get("parserRevision") == PARSER_REVISION
                    )
                )
                if old_complete or (old_current and not deep):
                    record = dict(old)
                    reused += 1
                    if record.get("kind") == "package":
                        reused_class = str(record.get("metadata", {}).get("className", "") or "")
                        if reused_class:
                            stem_classes[PurePosixPath(relative).stem] = reused_class
                        if reused_class in _MATERIAL_CLASS_NAMES:
                            material_stems.add(PurePosixPath(relative).stem)
                elif not deep:
                    record = {
                        "assetId": _asset_identity(relative, "", size, modified_ns),
                        "relativePath": relative,
                        "kind": kind,
                        "format": _format_name(PurePosixPath(relative)),
                        "size": size,
                        "modifiedNs": modified_ns,
                        "contentSha256": "",
                        "hashStatus": "deferred",
                        "parseStatus": "deferred",
                        "metadata": {"parser": "deferred", "reason": "outside the requested scan depth/scope"},
                        "references": [],
                    }
                    deferred += 1
                else:
                    content_sha256 = ""
                    hash_status = "skipped-size"
                    metadata = {
                        "parser": "none",
                        "reason": "file exceeds the admitted per-file hash/parse bound",
                    }
                    references: list[dict[str, Any]] = []
                    parse_status = "skipped-size"
                    if size <= maximum_file_size:
                        content_sha256 = _hash_regular_file(resolved, size)
                        after_hash = resolved.stat()
                        if (
                            int(after_hash.st_size) != size
                            or int(after_hash.st_mtime_ns) != modified_ns
                        ):
                            raise AssetCatalogError(
                                "asset changed while it was hashed: %s" % resolved
                            )
                        hashed += 1
                        hash_status = "sha256"
                        if size <= MAX_PARSE_BYTES:
                            try:
                                data = resolved.read_bytes()
                                after_read = resolved.stat()
                                if (
                                    int(after_read.st_size) != size
                                    or int(after_read.st_mtime_ns) != modified_ns
                                ):
                                    raise AssetCatalogError("asset changed while it was parsed: %s" % resolved)
                                metadata, references = _parse_asset(
                                    relative, kind, data, engine_context,
                                    sibling_class=stem_classes.get(PurePosixPath(relative).stem, ""),
                                )
                                metadata["parserRevision"] = PARSER_REVISION
                                parse_status = "parsed" if metadata.get("parser") != "none" else "provider-required"
                                if kind == "package" and metadata.get("className"):
                                    stem_classes[PurePosixPath(relative).stem] = str(metadata["className"])
                                if kind == "package" and str(metadata.get("className", "")) in _MATERIAL_CLASS_NAMES:
                                    material_stems.add(PurePosixPath(relative).stem)
                            except (OSError, ValueError, struct.error, json.JSONDecodeError) as error:
                                metadata = {"parser": "error", "errorType": type(error).__name__}
                                references = []
                                parse_status = "parse-error"
                                parse_failures += 1
                        else:
                            parse_status = "skipped-parse-size"
                            metadata = {"parser": "none", "reason": "file exceeds fixed parser byte bound"}
                    record = {
                        "assetId": _asset_identity(relative, content_sha256, size, modified_ns),
                        "relativePath": relative,
                        "kind": kind,
                        "format": _format_name(PurePosixPath(relative)),
                        "size": size,
                        "modifiedNs": modified_ns,
                        "contentSha256": content_sha256,
                        "hashStatus": hash_status,
                        "parseStatus": parse_status,
                        "metadata": metadata,
                        "references": references,
                    }
                records.append(record)
                visited.add(relative)
                if checkpoint_seconds and time.monotonic() - last_checkpoint >= checkpoint_seconds:
                    if progress is not None:
                        try:
                            progress({"phase": "checkpoint", "walked": walked, "hashed": hashed, "reused": reused, "deferred": deferred})
                        except Exception:
                            pass
                    _checkpoint()
        if progress is not None:
            try:
                progress({"phase": "publish", "walked": walked, "hashed": hashed, "reused": reused, "deferred": deferred})
            except Exception:
                pass
        catalog_sha256, relationships = _publish(records, complete=True)
        kinds = Counter(str(item["kind"]) for item in records)
        formats = Counter(str(item["format"]) for item in records)
        return {
            "schema": "renderdoc-ultra-unpacked-asset-index/v1",
            "schemaVersion": 1,
            "catalogSha256": catalog_sha256,
            "databasePath": str(self.database_path),
            "rootPath": str(root),
            "buildId": build_id,
            "scanMode": mode,
            "depth": depth_mode,
            "scopePrefixes": prefixes,
            "engine": engine_context.to_dict(),
            "assetCount": len(records),
            "deferredCount": sum(1 for item in records if item.get("hashStatus") == "deferred"),
            "relationshipCount": len(relationships),
            "verifiedRelationshipCount": sum(1 for item in relationships if item["verified"]),
            "unresolvedRelationshipCount": sum(1 for item in relationships if not item["verified"]),
            "countsByKind": dict(sorted(kinds.items())),
            "countsByFormat": dict(sorted(formats.items())),
            "cache": {"reusedFiles": reused, "hashedFiles": hashed, "checkpoints": checkpoints},
            "complete": True,
            "diagnostics": {
                "skippedSymlinks": skipped_symlinks,
                "parseFailures": parse_failures,
                "weakIdentityFiles": sum(1 for item in records if not item["contentSha256"]),
                "referenceTruncatedAssets": sum(
                    1
                    for item in records
                    if item.get("metadata", {}).get("referencesTruncated") is True
                ),
            },
            "coverage": {
                "complete": True,
                "truncated": False,
                "walkedFiles": walked,
                "maximumFiles": max_files,
                "maximumFileSizeMiB": max_file_size_mb,
            },
            "policy": {
                "projectOwnedCatalog": True,
                "semanticGuessesPersisted": False,
                "heuristicRelationshipsPersisted": False,
                "unresolvedReferencesRemainUnverified": True,
            },
        }

    def hydrate(self, asset_root: str | Path, relative_paths: Sequence[str], *,
                max_files: int = DEFAULT_MAX_FILES) -> dict[str, Any]:
        """Upgrade a few deferred records to full hash+parse, keeping the rest."""

        db, metadata = self._open_read_only()
        try:
            build_id = str(metadata.get("buildId", "") or "")
        finally:
            db.close()
        return self.scan(asset_root, build_id=build_id, scan_mode="incremental", max_files=max_files,
                         depth="full", scope_prefixes=list(relative_paths)[:256])

    @staticmethod
    def _detect_engine(root: Path, max_files: int) -> Any:
        """Cheap name-only pre-pass so per-file parsers know the engine/mounts."""

        from .symbols.engines import detect_engine

        relative_paths: list[str] = []
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            directory_names[:] = [
                name for name in directory_names if not _is_redirected(directory_path / name)
            ]
            base = directory_path.relative_to(root).as_posix() if directory_path != root else ""
            for name in file_names:
                relative_paths.append("%s/%s" % (base, name) if base else name)
                if len(relative_paths) > max_files:
                    break
            if len(relative_paths) > max_files:
                break
        return detect_engine(relative_paths)

    def _open_read_only(self) -> tuple[sqlite3.Connection, dict[str, str]]:
        if self.database_path.is_symlink() or not self.database_path.is_file():
            raise AssetCatalogError("active project has no prepared unpacked-asset catalog")
        connection = _database_connection(self.database_path, read_only=True)
        try:
            metadata = _validate_schema(connection)
            _validate_catalog_identity(connection, metadata)
        except Exception:
            connection.close()
            raise
        return connection, metadata

    def status(self) -> dict[str, Any]:
        db, metadata = self._open_read_only()
        try:
            asset_count = int(db.execute("SELECT COUNT(*) AS n FROM assets").fetchone()["n"])
            relationship_count = int(
                db.execute("SELECT COUNT(*) AS n FROM relationships").fetchone()["n"]
            )
            counts = {
                str(row["kind"]): int(row["n"])
                for row in db.execute("SELECT kind, COUNT(*) AS n FROM assets GROUP BY kind ORDER BY kind")
            }
            return {
                "schema": "renderdoc-ultra-unpacked-asset-index-status/v1",
                "schemaVersion": 1,
                "ready": True,
                "catalogSha256": metadata["catalogSha256"],
                "rootPath": metadata["rootPath"],
                "buildId": metadata.get("buildId", ""),
                "scanMode": metadata.get("scanMode", ""),
                "createdAt": metadata.get("createdAt", ""),
                "databasePath": str(self.database_path),
                "engine": metadata.get("engine", "unknown"),
                "tokenIndex": metadata.get("tokenIndex", "none"),
                "assetCount": asset_count,
                "relationshipCount": relationship_count,
                "countsByKind": counts,
                # Older catalogs predate checkpoints and were always complete.
                "complete": metadata.get("complete", "true") != "false",
                "progress": _safe_json(metadata.get("progressJson", "")),
            }
        finally:
            db.close()

    def query(
        self,
        *,
        text: str = "",
        kinds: Sequence[str] = (),
        sha256: str = "",
        asset_ids: Sequence[str] = (),
        limit: int = 20,
        include_relationships: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(text, str) or len(text) > 512 or "\x00" in text:
            raise AssetCatalogError("text query must be a bounded string")
        normalized_kinds = tuple(str(item).strip().casefold() for item in kinds)
        if len(normalized_kinds) > len(_KNOWN_KINDS) or len(set(normalized_kinds)) != len(normalized_kinds):
            raise AssetCatalogError("kinds must be a small unique list")
        if set(normalized_kinds) - _KNOWN_KINDS:
            raise AssetCatalogError("query contains an unknown asset kind")
        digest = str(sha256 or "").strip().lower()
        if digest and not _SHA256_RE.fullmatch(digest):
            raise AssetCatalogError("sha256 must be lowercase hexadecimal")
        normalized_ids = tuple(str(item).strip() for item in asset_ids)
        if len(normalized_ids) > MAX_QUERY_IDS or len(set(normalized_ids)) != len(normalized_ids):
            raise AssetCatalogError("asset_ids must be a bounded unique list")
        if any(not _ASSET_ID_RE.fullmatch(item) for item in normalized_ids):
            raise AssetCatalogError("asset_ids contains an invalid asset identity")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_QUERY_RESULTS:
            raise AssetCatalogError("limit is outside the fixed query bound")
        if not isinstance(include_relationships, bool):
            raise AssetCatalogError("include_relationships must be a boolean")
        db, metadata = self._open_read_only()
        try:
            clauses: list[str] = []
            parameters: list[Any] = []
            if text.strip():
                clauses.append("(relative_path LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\')")
                escaped = text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                parameters.extend(["%%%s%%" % escaped, "%%%s%%" % escaped])
            if normalized_kinds:
                clauses.append("kind IN (%s)" % ",".join("?" for _ in normalized_kinds))
                parameters.extend(normalized_kinds)
            if digest:
                clauses.append("content_sha256=?")
                parameters.append(digest)
            if normalized_ids:
                clauses.append("asset_id IN (%s)" % ",".join("?" for _ in normalized_ids))
                parameters.extend(normalized_ids)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = db.execute(
                "SELECT asset_id, relative_path, name, kind, format, size, modified_ns, "
                "content_sha256, hash_status, parse_status, metadata_json FROM assets"
                + where
                + " ORDER BY kind, relative_path LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
            truncated = len(rows) > limit
            rows = rows[:limit]
            assets: list[dict[str, Any]] = []
            for row in rows:
                item = {
                    "assetId": row["asset_id"],
                    "relativePath": row["relative_path"],
                    "name": row["name"],
                    "kind": row["kind"],
                    "format": row["format"],
                    "size": int(row["size"]),
                    "modifiedNs": int(row["modified_ns"]),
                    "contentSha256": row["content_sha256"],
                    "hashStatus": row["hash_status"],
                    "parseStatus": row["parse_status"],
                    "metadata": json.loads(row["metadata_json"]),
                }
                if include_relationships:
                    relation_rows = db.execute(
                        "SELECT relation, target_asset_id, target_path, target_guid, evidence_kind, "
                        "confidence, verified, evidence_json FROM relationships WHERE source_asset_id=? "
                        "ORDER BY relation, target_path LIMIT ?",
                        (row["asset_id"], MAX_RELATIONSHIPS_PER_ASSET + 1),
                    ).fetchall()
                    item["relationships"] = []
                    for relation in relation_rows[:MAX_RELATIONSHIPS_PER_ASSET]:
                        evidence = json.loads(relation["evidence_json"])
                        item["relationships"].append(
                            {
                                "relation": relation["relation"],
                                "targetAssetId": relation["target_asset_id"],
                                "targetPath": relation["target_path"],
                                "targetGuid": relation["target_guid"],
                                "evidenceKind": relation["evidence_kind"],
                                "confidence": float(relation["confidence"]),
                                "verified": bool(relation["verified"]),
                                "derivationClass": evidence.get(
                                    "derivationClass", "observed"
                                ),
                                "resolutionStatus": evidence.get(
                                    "resolutionStatus", "unresolved"
                                ),
                                "evidence": evidence,
                            }
                        )
                    item["relationshipsTruncated"] = len(relation_rows) > MAX_RELATIONSHIPS_PER_ASSET
                assets.append(item)
            return {
                "schema": "renderdoc-ultra-unpacked-asset-query/v1",
                "schemaVersion": 1,
                "catalogSha256": metadata["catalogSha256"],
                "rootPath": metadata["rootPath"],
                "query": {
                    "text": text.strip(),
                    "kinds": list(normalized_kinds),
                    "sha256": digest,
                    "assetIds": list(normalized_ids),
                    "limit": limit,
                },
                "count": len(assets),
                "truncated": truncated,
                "assets": assets,
                "policy": {
                    "readOnly": True,
                    "semanticGuessing": False,
                    "heuristicRelationshipsPersisted": False,
                    "unverifiedRelationsExplicit": True,
                },
            }
        finally:
            db.close()

    def coverage(self) -> dict[str, Any]:
        """What the catalog could and could not read, with reasons.

        Tells a reviewer why evidence may be missing: packed archives that
        were never unpacked, compressed shader archives whose binaries cannot
        be indexed, packages that failed to parse (encrypted or foreign
        formats), and files deferred by the scan depth.
        """

        db, metadata = self._open_read_only()
        try:
            by_status: dict[str, dict[str, int]] = {}
            for row in db.execute("SELECT kind, parse_status, COUNT(*) AS n FROM assets GROUP BY kind, parse_status"):
                by_status.setdefault(str(row["kind"]), {})[str(row["parse_status"])] = int(row["n"])
            reasons: dict[str, int] = {}
            examples: dict[str, str] = {}
            for row in db.execute(
                "SELECT relative_path, metadata_json FROM assets WHERE parse_status IN ('parse-error') "
                "OR metadata_json LIKE '%\"parseStatus\":\"failed\"%' LIMIT 2000"
            ):
                stored = json.loads(row["metadata_json"])
                reason = str(stored.get("reason") or stored.get("errorType") or "unknown")[:160]
                reasons[reason] = reasons.get(reason, 0) + 1
                examples.setdefault(reason, str(row["relative_path"]))
            archives = {
                str(row["format"]): int(row["n"])
                for row in db.execute("SELECT format, COUNT(*) AS n FROM assets WHERE kind='archive' GROUP BY format")
            }
            deferred = int(db.execute("SELECT COUNT(*) AS n FROM assets WHERE parse_status='deferred'").fetchone()["n"])
            try:
                engine = json.loads(metadata.get("engineJson") or "{}")
            except ValueError:
                engine = {}
            limitations: list[str] = []
            details = engine.get("details") or {}
            if archives:
                limitations.append(
                    "%d packed archive file(s) (%s) are still packed; assets inside them are invisible until unpacked"
                    % (sum(archives.values()), ", ".join(sorted(archives))))
            if int(details.get("shaderArchiveCount") or 0):
                limitations.append(
                    "%d Unreal .ushaderbytecode archive(s) present: shader binaries are compressed and not indexable, "
                    "so hash identity is unavailable; retrieval relies on names, relations and cbuffer values"
                    % int(details["shaderArchiveCount"]))
            if reasons:
                top = sorted(reasons.items(), key=lambda item: -item[1])[:5]
                limitations.append(
                    "%d file(s) failed to parse: %s" % (
                        sum(reasons.values()),
                        "; ".join("%s (x%d, e.g. %s)" % (reason, count, examples[reason]) for reason, count in top)))
            if deferred:
                limitations.append(
                    "%d file(s) deferred by scan depth (bulk/large data not hashed or parsed); candidates are hydrated on demand"
                    % deferred)
            if engine.get("engine") in (None, "", "unknown"):
                limitations.append("engine not recognized from file names: only path/name/text tokens are available")
            unfingerprinted: dict[str, int] = {}
            for row in db.execute(
                "SELECT metadata_json FROM assets WHERE metadata_json LIKE '%\"textureFingerprint\"%' "
                "AND metadata_json LIKE '%\"available\":false%' LIMIT 5000"
            ):
                stored = json.loads(row["metadata_json"])
                reason = str(((stored.get("textureFingerprint") or {}).get("reason")) or "unknown")[:120]
                unfingerprinted[reason] = unfingerprinted.get(reason, 0) + 1
            if unfingerprinted:
                top = sorted(unfingerprinted.items(), key=lambda item: -item[1])[:4]
                limitations.append("%d texture(s) have no content fingerprint: %s" % (
                    sum(unfingerprinted.values()), "; ".join("%s (x%d)" % item for item in top)))
            return {
                "schema": "renderdoc-ultra-unpacked-asset-coverage/v1",
                "schemaVersion": 1,
                "catalogSha256": metadata["catalogSha256"],
                "engine": engine,
                "parserRevision": metadata.get("parserRevision"),
                "tokenIndex": metadata.get("tokenIndex", "none"),
                "valueIndexRows": int(metadata.get("valueIndex", "0") or 0),
                "thumbnailRows": int(metadata.get("thumbnailIndex", "0") or 0),
                "byKindAndStatus": by_status,
                "parseFailureReasons": dict(sorted(reasons.items(), key=lambda item: -item[1])[:20]),
                "archives": archives,
                "deferredCount": deferred,
                "limitations": limitations,
            }
        finally:
            db.close()

    def search_thumbnails(self, dhash: int, *, mean_color: Sequence[int] | None = None,
                          max_distance: int = 12, limit: int = 20) -> dict[str, Any]:
        """Texture assets whose thumbnail hash is within ``max_distance`` bits.

        The query hash is compared with every stored dihedral variant, so a
        mirrored/rotated copy of the same image matches; flat (uniform)
        images are excluded because they match everything.
        """

        from .symbols.imaging import hamming

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_QUERY_RESULTS:
            raise AssetCatalogError("limit is outside the fixed query bound")
        query = int(dhash) & 0xFFFFFFFFFFFFFFFF
        names = ("identity", "mirror-x", "rotate-90", "rotate-90+mirror", "rotate-180", "mirror-y", "rotate-270", "rotate-270+mirror")
        db, metadata = self._open_read_only()
        try:
            if "thumbnailIndex" not in metadata:
                raise AssetCatalogError("asset catalog was indexed without texture fingerprints; rescan it")
            matches = []
            for row in db.execute("SELECT * FROM asset_thumbnails WHERE flat=0"):
                best = None
                for index in range(8):
                    stored = int(row["v%d" % index]) & 0xFFFFFFFFFFFFFFFF
                    distance = hamming(query, stored)
                    if best is None or distance < best[0]:
                        best = (distance, index)
                if best is not None and best[0] <= max_distance:
                    color_delta = None
                    if mean_color is not None and len(mean_color) >= 3:
                        color_delta = int(abs(int(mean_color[0]) - int(row["mean_r"])) + abs(int(mean_color[1]) - int(row["mean_g"]))
                                          + abs(int(mean_color[2]) - int(row["mean_b"])))
                    matches.append((best[0], color_delta if color_delta is not None else 0, row["asset_id"], names[best[1]],
                                    color_delta, int(row["width"]), int(row["height"]), str(row["pixel_format"] or "")))
            matches.sort(key=lambda item: (item[0], item[1], item[2]))
            candidates = []
            for distance, _, asset_id, transform, color_delta, width, height, pixel_format in matches[:limit]:
                asset = db.execute("SELECT relative_path, kind, content_sha256 FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
                if asset is None:
                    continue
                # Luminance hashes cannot tell channel-swapped variants apart;
                # a large mean-colour difference demotes an exact hash hit.
                verdict = "same-image" if distance <= 6 else "plausible"
                if verdict == "same-image" and color_delta is not None and color_delta > 96:
                    verdict = "plausible"
                candidates.append({
                    "assetId": asset_id, "relativePath": asset["relative_path"], "kind": asset["kind"],
                    "contentSha256": asset["content_sha256"], "distance": distance, "transform": transform,
                    "meanColorDelta": color_delta, "sizeX": width, "sizeY": height, "pixelFormat": pixel_format,
                    "verdict": verdict,
                })
            return {"schema": "renderdoc-ultra-unpacked-asset-thumbnail-search/v1", "schemaVersion": 1,
                    "catalogSha256": metadata["catalogSha256"], "queryHash": query, "maxDistance": max_distance,
                    "indexedThumbnails": int(metadata.get("thumbnailIndex", "0") or 0), "candidates": candidates}
        finally:
            db.close()

    def search_values(self, values: Sequence[float | str], *, limit: int = 50) -> dict[str, Any]:
        """Assets containing the given distinctive numeric values.

        ``values`` are floats (rounded to float32 keys) or ready-made keys.
        Returns, per asset, which keys matched plus the asset's numeric runs
        so a caller can test adjacency against neighbouring cbuffer lanes.
        """

        from .symbols.engines.unreal_package import is_distinctive_value, value_key

        keys: list[str] = []
        for item in values:
            if isinstance(item, str):
                key = item.strip()
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                value = struct.unpack("<f", struct.pack("<f", float(item)))[0]
                if not is_distinctive_value(value):
                    continue
                key = value_key(value)
            else:
                continue
            if key and len(key) <= 24 and key not in keys:
                keys.append(key)
        if len(keys) > 4096:
            keys = keys[:4096]
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_QUERY_RESULTS:
            raise AssetCatalogError("limit is outside the fixed query bound")
        db, metadata = self._open_read_only()
        try:
            if "valueIndex" not in metadata:
                raise AssetCatalogError("asset catalog was indexed without numeric values; rescan it")
            if not keys:
                return {"schema": "renderdoc-ultra-unpacked-asset-value-search/v1", "schemaVersion": 1,
                        "catalogSha256": metadata["catalogSha256"], "queryKeys": [], "candidates": [],
                        "reason": "no distinctive values in the query"}
            hits: dict[str, list[str]] = {}
            popularity: dict[str, int] = {}
            for start in range(0, len(keys), 500):
                chunk = keys[start:start + 500]
                rows = db.execute(
                    "SELECT value_key, asset_id FROM asset_values WHERE value_key IN (%s)" % ",".join("?" for _ in chunk),
                    chunk,
                ).fetchall()
                for row in rows:
                    popularity[str(row["value_key"])] = popularity.get(str(row["value_key"]), 0) + 1
                    hits.setdefault(str(row["asset_id"]), []).append(str(row["value_key"]))
            ranked = sorted(hits.items(), key=lambda item: (-len(set(item[1])), item[0]))
            candidates = []
            for asset_id, matched in ranked[:limit]:
                row = db.execute(
                    "SELECT asset_id, relative_path, kind, content_sha256, metadata_json FROM assets WHERE asset_id=?",
                    (asset_id,),
                ).fetchone()
                if row is None:
                    continue
                stored = json.loads(row["metadata_json"])
                unique = sorted(set(matched))
                candidates.append({
                    "assetId": row["asset_id"],
                    "relativePath": row["relative_path"],
                    "kind": row["kind"],
                    "contentSha256": row["content_sha256"],
                    "className": stored.get("className", ""),
                    "matchedValues": unique,
                    "matchedCount": len(unique),
                    # values shared by many assets (engine defaults) count less
                    "rareMatchedCount": sum(1 for key in unique if popularity.get(key, 0) <= 3),
                    "numericRuns": [run for run in (stored.get("numericRuns") or [])
                                    if any(key in unique for key in run.get("values", []))][:64],
                })
            return {
                "schema": "renderdoc-ultra-unpacked-asset-value-search/v1",
                "schemaVersion": 1,
                "catalogSha256": metadata["catalogSha256"],
                "queryKeys": keys,
                "keyPopularity": popularity,
                "candidates": candidates,
            }
        finally:
            db.close()

    def referrers(self, asset_id: str, *, relation: str = "", limit: int = 50) -> dict[str, Any]:
        """Assets whose relationships point at ``asset_id`` (reverse edges)."""

        identity = str(asset_id or "").strip()
        if not _ASSET_ID_RE.fullmatch(identity):
            raise AssetCatalogError("asset_id is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_QUERY_RESULTS:
            raise AssetCatalogError("limit is outside the fixed query bound")
        db, metadata = self._open_read_only()
        try:
            clauses = ["r.target_asset_id=?"]
            parameters: list[Any] = [identity]
            if relation:
                clauses.append("r.relation=?")
                parameters.append(str(relation)[:128])
            rows = db.execute(
                "SELECT r.relation, r.verified, a.asset_id, a.relative_path, a.kind, a.metadata_json "
                "FROM relationships r JOIN assets a ON a.asset_id = r.source_asset_id "
                "WHERE " + " AND ".join(clauses) + " ORDER BY a.relative_path LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
            items = []
            for row in rows[:limit]:
                stored = json.loads(row["metadata_json"])
                items.append({
                    "assetId": row["asset_id"], "relativePath": row["relative_path"], "kind": row["kind"],
                    "relation": row["relation"], "verified": bool(row["verified"]),
                    "className": stored.get("className", ""),
                })
            return {"schema": "renderdoc-ultra-unpacked-asset-referrers/v1", "schemaVersion": 1,
                    "catalogSha256": metadata["catalogSha256"], "assetId": identity,
                    "referrers": items, "truncated": len(rows) > limit}
        finally:
            db.close()

    def search_tokens(
        self,
        tokens: Sequence[str],
        *,
        kinds: Sequence[str] = (),
        class_names: Sequence[str] = (),
        limit: int = 20,
    ) -> dict[str, Any]:
        """Rank assets by overlap with a bag of query tokens (no hashes needed).

        Each matching token contributes an IDF-style weight (rarer tokens in
        this catalog count more), so a distinctive texture name outweighs a
        generic word such as ``base`` or ``color``.  The per-asset result lists
        the matched tokens so a reviewer can see *why* it ranked.
        """

        query_tokens: list[str] = []
        for token in tokens:
            if not isinstance(token, str):
                continue
            for word in _TOKEN_SPLIT.split(token):
                folded = word.casefold()
                if 2 <= len(folded) <= 64 and folded not in query_tokens:
                    query_tokens.append(folded)
        if not query_tokens or len(query_tokens) > 256:
            raise AssetCatalogError("token search needs 1 to 256 tokens")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_QUERY_RESULTS:
            raise AssetCatalogError("limit is outside the fixed query bound")
        normalized_kinds = {str(item).strip().casefold() for item in kinds}
        if normalized_kinds - _KNOWN_KINDS:
            raise AssetCatalogError("token search contains an unknown asset kind")
        wanted_classes = {str(item) for item in class_names if isinstance(item, str) and item}
        db, metadata = self._open_read_only()
        try:
            if metadata.get("tokenIndex") not in {"fts5", "plain"}:
                raise AssetCatalogError("asset catalog was indexed without retrieval tokens; rescan it")
            total = max(1, int(db.execute("SELECT COUNT(*) AS n FROM asset_tokens").fetchone()["n"]))
            weights: dict[str, float] = {}
            hits: dict[str, dict[str, Any]] = {}
            for token in query_tokens:
                if metadata.get("tokenIndex") == "fts5":
                    rows = db.execute(
                        "SELECT asset_id FROM asset_tokens WHERE asset_tokens MATCH ? LIMIT 20000",
                        ('"%s"' % token.replace('"', '""'),),
                    ).fetchall()
                else:
                    rows = db.execute(
                        "SELECT asset_id FROM asset_tokens WHERE ' ' || tokens || ' ' LIKE ? LIMIT 20000",
                        ("%% %s %%" % token,),
                    ).fetchall()
                if not rows:
                    continue
                weight = math.log(1.0 + total / (1.0 + len(rows)))
                weights[token] = weight
                for row in rows:
                    entry = hits.setdefault(str(row["asset_id"]), {"score": 0.0, "matched": []})
                    entry["score"] += weight
                    entry["matched"].append(token)
            if not hits:
                return {"schema": "renderdoc-ultra-unpacked-asset-token-search/v1", "schemaVersion": 1,
                        "catalogSha256": metadata["catalogSha256"], "queryTokens": query_tokens,
                        "tokenWeights": {}, "candidates": []}
            ordered = sorted(hits.items(), key=lambda item: (-item[1]["score"], item[0]))
            candidates = []
            for asset_id, entry in ordered:
                row = db.execute(
                    "SELECT asset_id, relative_path, kind, format, size, content_sha256, parse_status, "
                    "metadata_json FROM assets WHERE asset_id=?", (asset_id,)
                ).fetchone()
                if row is None:
                    continue
                if normalized_kinds and str(row["kind"]) not in normalized_kinds:
                    continue
                stored = json.loads(row["metadata_json"])
                if wanted_classes and str(stored.get("className", "")) not in wanted_classes:
                    continue
                candidates.append({
                    "assetId": row["asset_id"],
                    "relativePath": row["relative_path"],
                    "kind": row["kind"],
                    "format": row["format"],
                    "size": int(row["size"]),
                    "contentSha256": row["content_sha256"],
                    "className": stored.get("className", ""),
                    "engine": stored.get("engine", ""),
                    "score": round(entry["score"], 4),
                    "matchedTokens": sorted(set(entry["matched"])),
                    "matchedCount": len(set(entry["matched"])),
                    "queryCoverage": round(len(set(entry["matched"])) / len(query_tokens), 4),
                })
                if len(candidates) >= limit:
                    break
            return {
                "schema": "renderdoc-ultra-unpacked-asset-token-search/v1",
                "schemaVersion": 1,
                "catalogSha256": metadata["catalogSha256"],
                "engine": metadata.get("engine", "unknown"),
                "queryTokens": query_tokens,
                "tokenWeights": {token: round(weight, 4) for token, weight in weights.items()},
                "candidates": candidates,
            }
        finally:
            db.close()

    def resolve(
        self,
        asset_root: str | Path,
        *,
        asset_id: str,
        expected_catalog_sha256: str,
    ) -> dict[str, Any]:
        root = _validate_root(asset_root)
        identity = str(asset_id or "").strip()
        if not _ASSET_ID_RE.fullmatch(identity):
            raise AssetCatalogError("asset_id is invalid")
        expected = str(expected_catalog_sha256 or "").strip().lower()
        if not _SHA256_RE.fullmatch(expected):
            raise AssetCatalogError("expected_catalog_sha256 is invalid")
        db, metadata = self._open_read_only()
        try:
            if metadata["catalogSha256"] != expected:
                raise AssetCatalogError("asset catalog changed after candidate selection")
            if Path(metadata["rootPath"]).resolve() != root:
                raise AssetCatalogError("asset catalog belongs to another configured root")
            row = db.execute(
                "SELECT asset_id, relative_path, kind, format, size, modified_ns, content_sha256, "
                "hash_status, parse_status, metadata_json FROM assets WHERE asset_id=?",
                (identity,),
            ).fetchone()
            if row is None:
                raise AssetCatalogError("asset identity is not present in the current catalog")
            relative = PurePosixPath(str(row["relative_path"]))
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise AssetCatalogError("stored asset path is unsafe")
            cursor = root
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise AssetCatalogError("asset delivery path traverses a symbolic link")
            path = cursor.resolve(strict=True)
            if not _within(root, path) or not path.is_file():
                raise AssetCatalogError("asset delivery path escaped or disappeared")
            stat_value = path.stat()
            if int(stat_value.st_size) != int(row["size"]):
                raise AssetCatalogError("asset size changed after indexing")
            content_sha256 = str(row["content_sha256"] or "")
            if not content_sha256:
                raise AssetCatalogError("asset was not content-hashed and cannot be delivered as verified evidence")
            actual_sha256 = _hash_regular_file(path, int(row["size"]))
            if actual_sha256 != content_sha256:
                raise AssetCatalogError("asset content changed after indexing")
            return {
                "schema": "renderdoc-ultra-unpacked-asset-delivery/v1",
                "schemaVersion": 1,
                "catalogSha256": expected,
                "assetId": identity,
                "relativePath": relative.as_posix(),
                "absolutePath": str(path),
                "kind": row["kind"],
                "format": row["format"],
                "size": int(row["size"]),
                "contentSha256": actual_sha256,
                "metadata": json.loads(row["metadata_json"]),
                "verified": True,
                "policy": {
                    "configuredActiveProjectRootOnly": True,
                    "catalogRevisionBound": True,
                    "contentRehashedAtDelivery": True,
                    "executionAuthorized": False,
                },
            }
        finally:
            db.close()


__all__ = [
    "AssetCatalogError",
    "AssetCatalogScanLimitError",
    "AssetCatalogService",
    "CATALOG_FILENAME",
    "CATALOG_SCHEMA",
    "CATALOG_VERSION",
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_FILE_SIZE_MB",
    "HARD_MAX_FILES",
    "HARD_MAX_FILE_SIZE_MB",
]
