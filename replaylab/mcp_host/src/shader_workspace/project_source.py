"""Managed source provenance for direct reuse of verified project HLSL."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .errors import ManifestValidationError


PROJECT_SOURCE_SCHEMA = "replaylab-verified-project-shader-source/v1"


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def source_receipt(bundle: Mapping[str, Any], input_path: str) -> dict[str, Any]:
    receipt = {key: value for key, value in bundle.items() if key != "sourceText"}
    receipt["inputPath"] = input_path
    receipt["receiptSha256"] = _digest(receipt)
    return receipt


def validate_project_source(root: Path, manifest: Mapping[str, Any], source: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the immutable imported input; later user edits remain free."""
    receipt = source.get("projectSource")
    if not isinstance(receipt, Mapping):
        raise ManifestValidationError("project source provenance is missing")
    value = dict(receipt)
    digest = value.pop("receiptSha256", None)
    if (value.get("schema") != PROJECT_SOURCE_SCHEMA or digest != _digest(value)
            or value.get("sourceSha256") != source.get("sha256")
            or value.get("sourceEntryPoint") != source.get("entryPoint")
            or value.get("originalRawSha256") != manifest["baseline"]["rawSha256"]
            or value.get("baselineAbiSha256") != manifest["baseline"].get("abiSha256")
            or value.get("targetProfile") != manifest.get("targetProfile")
            or value.get("stage") != manifest.get("stage")
            or value.get("entryPoint") != manifest.get("entryPoint")
            or value.get("applied") is not False
            or value.get("requiresCompile") is not True
            or value.get("requiresLiveAbiRevalidation") is not True
            or value.get("semanticEquivalenceVerified") is not False):
        raise ManifestValidationError("project source identity/provenance changed")
    for key, length in (("projectId", 32), ("knowledgeId", 32), ("sourceSha256", 64), ("originWorkspaceId", 32),
                        ("originalRawSha256", 64), ("baselineAbiSha256", 64)):
        item = value.get(key)
        if not isinstance(item, str) or len(item) != length or any(c not in "0123456789abcdef" for c in item):
            raise ManifestValidationError("project source %s is invalid" % key)
    relative = "source-inputs/project-%s.hlsl" % value["sourceSha256"]
    path = Path(root) / relative
    if (value.get("inputPath") != relative or path.parent.is_symlink() or path.is_symlink()
            or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024
            or hashlib.sha256(path.read_bytes()).hexdigest() != value["sourceSha256"]):
        raise ManifestValidationError("project source immutable input changed")
    return dict(receipt)
