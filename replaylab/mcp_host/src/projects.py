"""Fail-closed project index for knowledge shared across capture frames.

Projects persist only portable, content-bound knowledge. Replay handles,
capture sessions, ABI reports and material snapshots remain capture-local and
are never promoted into project authority.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
from typing import Any, Iterator, Mapping, Sequence
import uuid

from .shader_workspace.io import atomic_write_bytes, atomic_write_json, is_within, read_json_object
from .symbols.matcher import ALIAS_SCHEMA, ALIAS_VERSION, _normalise_catalog


PROJECT_SCHEMA = "renderdoc-ultra-replay-project/v1"
PROJECT_VERSION = 1
MAX_PROJECT_BYTES = 64 * 1024 * 1024
MAX_CAPTURE_COUNT = 256
MAX_CAPTURE_REVISION_COUNT = 4096
MAX_SHADER_COUNT = 65536
MAX_KNOWLEDGE_COUNT = 4096
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_ANALYSIS_BYTES = 1024 * 1024
MAX_ALIAS_BYTES = 64 * 1024 * 1024
MAX_CAPTURE_CATALOG_BYTES = 256 * 1024 * 1024
MAX_CAPTURE_SHADER_BYTES = 512 * 1024 * 1024
MAX_ALIASES = 65536
MAX_MATERIAL_PARAMETERS = 256
MAX_REUSE_CACHE_BYTES = 256 * 1024 * 1024
MAX_ACTIVE_KNOWLEDGE_REVISION = (1 << 63) - 1
REUSE_CACHE_SCHEMA = "renderdoc-ultra-project-reuse-cache/v1"
REUSE_CACHE_VERSION = 1
REUSE_MATCH_POLICY = "raw-exact-or-code-interface-layout-exact-v1"
REUSE_MATCH_POLICY_SHA256 = hashlib.sha256(REUSE_MATCH_POLICY.encode("utf-8")).hexdigest()
RESTORE_KNOWLEDGE_SCOPE_SCHEMA = (
    "renderdoc-ultra-project-referenced-knowledge/v1"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HEX32_RE = re.compile(r"^[0-9a-f]{32}$")

_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class ReplayProjectError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_bytes(path: Path, maximum: int, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ReplayProjectError("%s must be a regular non-symlink file" % label)
    size = path.stat().st_size
    if size <= 0 or size > maximum:
        raise ReplayProjectError("%s must contain 1..%d bytes" % (label, maximum))
    value = path.read_bytes()
    if len(value) != size:
        raise ReplayProjectError("%s changed while it was read" % label)
    return value


def _sha(value: Any, field: str, allow_empty: bool = False) -> str:
    text = str(value or "").strip().lower()
    if not text and allow_empty:
        return ""
    if not SHA256_RE.fullmatch(text):
        raise ReplayProjectError("%s must be a lowercase SHA-256" % field)
    return text


def _positive(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ReplayProjectError("%s must be a positive integer" % field)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReplayProjectError("%s must be a positive integer" % field) from exc
    if result <= 0:
        raise ReplayProjectError("%s must be a positive integer" % field)
    return result


def _text(value: Any, field: str, maximum: int = 4096, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ReplayProjectError("%s must be a string" % field)
    result = value.strip()
    if (not result and not allow_empty) or len(result) > maximum or "\x00" in result:
        raise ReplayProjectError("%s has an invalid length/content" % field)
    return result


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normal_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _api_family(value: Any, allow_empty: bool = False) -> str:
    text = re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())
    aliases = {
        "direct3d11": "d3d11",
        "directx11": "d3d11",
        "direct3d12": "d3d12",
        "directx12": "d3d12",
        "opengl": "opengl",
        "vulkan": "vulkan",
        "metal": "metal",
    }
    result = aliases.get(text, text)
    if not result and allow_empty:
        return ""
    if not result or len(result) > 64:
        raise ReplayProjectError("api_family is invalid")
    return result


def _thread_lock(root: Path) -> threading.RLock:
    key = os.path.normcase(str(root))
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _project_lock(root: Path) -> Iterator[None]:
    lock_path = root / ".project-operation.lock"
    token = uuid.uuid4().hex
    descriptor = None
    try:
        descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(descriptor, token.encode("ascii"))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
    except FileExistsError as exc:
        raise ReplayProjectError(
            "project is locked; a stale lock must be reviewed and removed manually"
        ) from exc
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            if lock_path.read_text(encoding="ascii") == token:
                lock_path.unlink()
        except OSError:
            pass


@contextmanager
def _workspace_seed_lock(root: Path) -> Iterator[None]:
    """Participate in the workspace service's cross-process operation lock.

    This intentionally uses the same fail-closed filename and token ownership
    rules as the workspace lifecycle without importing its private helpers.
    """

    lock_path = root / ".workspace-operation.lock"
    token = uuid.uuid4().hex + "\n"
    descriptor = None
    try:
        descriptor = os.open(
            str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        os.write(descriptor, token.encode("ascii"))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
    except FileExistsError as exc:
        raise ReplayProjectError(
            "destination workspace is locked by another operation"
        ) from exc
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            if lock_path.read_text(encoding="ascii") == token:
                lock_path.unlink()
        except OSError:
            pass


def _compact_shader(shader: Mapping[str, Any]) -> dict[str, Any]:
    raw = str(shader.get("rawSha256", "") or "").lower()
    code = str(shader.get("codeSha256", "") or "").lower()
    raw_verified = shader.get("_rawHashVerified") is True
    code_verified = shader.get("_codeHashVerified") is True
    return {
        "resourceId": _positive(shader.get("shaderResourceId"), "shader.resourceId"),
        "stage": _text(str(shader.get("stage", "")), "shader.stage", 64),
        "entryPoint": _text(
            str(shader.get("entryPoint", "") or ""),
            "shader.entryPoint",
            512,
            allow_empty=True,
        ),
        "rawSha256": _sha(raw, "shader.rawSha256") if raw_verified else "",
        "codeSha256": _sha(code, "shader.codeSha256") if code_verified else "",
        "layoutHash": _sha(shader.get("layoutHash"), "shader.layoutHash", True),
        "interfaceHash": _sha(
            shader.get("interfaceHash"), "shader.interfaceHash", True
        ),
        "interfaceComplete": shader.get("interfaceComplete") is True,
        "encoding": _text(
            str(shader.get("encoding", "") or ""),
            "shader.encoding",
            64,
            allow_empty=True,
        ),
        "eventIds": sorted(
            {
                int(use.get("eventId"))
                for use in list(shader.get("uses", []) or [])
                if isinstance(use, Mapping)
                and not isinstance(use.get("eventId"), bool)
                and isinstance(use.get("eventId"), int)
                and int(use.get("eventId")) > 0
            }
        )[:4096],
    }


def _shader_identity(value: Mapping[str, Any]) -> tuple[int, str, str, str]:
    return (
        int(value.get("resourceId", 0)),
        str(value.get("stage", "")),
        str(value.get("entryPoint", "")),
        str(value.get("rawSha256", "")),
    )


def _match_mode(knowledge: Mapping[str, Any], shader: Mapping[str, Any]) -> str:
    if (
        knowledge.get("stage") != shader.get("stage")
        or knowledge.get("entryPoint", "") != shader.get("entryPoint", "")
    ):
        return ""
    raw = str(knowledge.get("rawSha256", "") or "")
    if raw and raw == shader.get("rawSha256"):
        return "raw-exact"
    code = str(knowledge.get("codeSha256", "") or "")
    if not code or code != shader.get("codeSha256"):
        return ""
    if not (
        knowledge.get("interfaceComplete") is True
        and shader.get("interfaceComplete") is True
        and knowledge.get("interfaceHash")
        and knowledge.get("interfaceHash") == shader.get("interfaceHash")
        # A complete interface proves that an empty CB list is known-empty,
        # rather than missing reflection. Its canonical layout hash is empty.
        and knowledge.get("layoutHash", "") == shader.get("layoutHash", "")
    ):
        return ""
    return "code-interface-layout-exact"


def _normalised_catalog_evidence(
    catalog_path: str | Path,
) -> tuple[Path, dict[str, Any], bytes, dict[str, bytes]]:
    """Return one deterministic, self-contained catalog revision payload."""

    raw_source = Path(catalog_path).expanduser()
    if raw_source.exists() and raw_source.is_symlink():
        raise ReplayProjectError("capture catalog must not be a symlink")
    source = raw_source.resolve()
    before = _file_bytes(source, MAX_CAPTURE_CATALOG_BYTES, "capture catalog")
    try:
        normalised_path, catalog = _normalise_catalog(source)
    except Exception as exc:
        raise ReplayProjectError(
            "capture catalog validation failed: %s" % exc
        ) from exc
    after = _file_bytes(source, MAX_CAPTURE_CATALOG_BYTES, "capture catalog")
    if before != after or normalised_path != source:
        raise ReplayProjectError("capture catalog changed while it was imported")

    total_blob_bytes = 0
    blobs: dict[str, bytes] = {}
    for shader in catalog["shaders"]:
        raw_path = str(shader.get("rawPath", "") or "")
        if not raw_path:
            continue
        blob = _file_bytes(
            Path(raw_path), MAX_CAPTURE_CATALOG_BYTES, "capture shader blob"
        )
        total_blob_bytes += len(blob)
        if total_blob_bytes > MAX_CAPTURE_SHADER_BYTES:
            raise ReplayProjectError(
                "capture shader blobs exceed the project revision limit"
            )
        digest = _sha256_bytes(blob)
        if digest != shader.get("rawSha256"):
            raise ReplayProjectError(
                "capture shader blob changed after catalog validation"
            )
        blobs.setdefault(digest, blob)
        shader["rawPath"] = "blobs/%s.bin" % digest

    payload = (
        json.dumps(
            catalog,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if not payload or len(payload) > MAX_CAPTURE_CATALOG_BYTES:
        raise ReplayProjectError(
            "normalised capture catalog exceeds the project limit"
        )
    return source, catalog, payload, blobs


class ReplayProjectService:
    def __init__(self, project_path: str | Path):
        self.root = _normal_path(project_path)

    @property
    def manifest_path(self) -> Path:
        return self.root / "project.json"

    @classmethod
    def create(
        cls,
        project_path: str | Path,
        name: str = "",
        game_id: str = "",
        build_id: str = "",
        platform: str = "",
        api_family: str = "",
    ) -> dict[str, Any]:
        root = _normal_path(project_path)
        if root.exists():
            raise ReplayProjectError("project path already exists: %s" % root)
        root.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".%s.staging-" % root.name, dir=str(root.parent)))
        now = _utc_now()
        manifest = {
            "schema": PROJECT_SCHEMA,
            "schemaVersion": PROJECT_VERSION,
            "projectId": uuid.uuid4().hex,
            "name": _text(str(name or ""), "name", 256, allow_empty=True),
            "scope": {
                "gameId": _text(str(game_id or ""), "game_id", 256),
                "buildId": _text(str(build_id or ""), "build_id", 256),
                "platform": _text(
                    str(platform or ""), "platform", 128, allow_empty=True
                ),
                "apiFamily": _api_family(api_family, allow_empty=True),
            },
            "createdAt": now,
            "updatedAt": now,
            "captures": [],
            "knowledge": {"workspaces": [], "aliases": [], "materials": []},
            "activeKnowledge": {
                "revision": 0,
                "workspaces": {},
                "materials": {},
            },
        }
        try:
            (staging / "captures" / "catalogs").mkdir(parents=True)
            (staging / "knowledge" / "workspaces").mkdir(parents=True)
            (staging / "knowledge" / "aliases").mkdir(parents=True)
            atomic_write_json(staging / "project.json", manifest)
            staging.replace(root)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return cls(root).status()

    @staticmethod
    def _active_knowledge_state(manifest: dict[str, Any]) -> dict[str, Any]:
        """Return the validated persistent activation state.

        Early v1 projects did not have an explicit active-head index.  For
        those manifests only, derive the exact ordering they previously used
        from the immutable knowledge arrays.  The derived state is attached to
        the in-memory manifest and is persisted by the next ordinary project
        mutation.  Once the field exists it is strict: partial/tampered state
        is never silently repaired.
        """

        knowledge = manifest.get("knowledge")
        if not isinstance(knowledge, Mapping):
            raise ReplayProjectError("project knowledge index is invalid")

        ids: dict[str, list[str]] = {}
        for kind in ("workspaces", "materials"):
            records = knowledge.get(kind)
            if not isinstance(records, list):
                raise ReplayProjectError("project %s knowledge is invalid" % kind)
            ordered: list[str] = []
            seen: set[str] = set()
            for index, record in enumerate(records):
                if not isinstance(record, Mapping):
                    raise ReplayProjectError(
                        "project %s knowledge[%d] is invalid" % (kind, index)
                    )
                token = str(record.get("knowledgeId", "") or "")
                if not HEX32_RE.fullmatch(token) or token in seen:
                    raise ReplayProjectError(
                        "project %s knowledgeId is invalid or duplicated" % kind
                    )
                seen.add(token)
                ordered.append(token)
            ids[kind] = ordered

        if "activeKnowledge" not in manifest:
            revision = 0
            derived: dict[str, Any] = {
                "revision": 0,
                "workspaces": {},
                "materials": {},
            }
            for kind in ("workspaces", "materials"):
                for token in ids[kind]:
                    revision += 1
                    derived[kind][token] = revision
            derived["revision"] = revision
            manifest["activeKnowledge"] = derived
            return derived

        active = manifest.get("activeKnowledge")
        if not isinstance(active, dict) or set(active) != {
            "revision",
            "workspaces",
            "materials",
        }:
            raise ReplayProjectError("project activeKnowledge index is invalid")
        revision = active.get("revision")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
            or revision > MAX_ACTIVE_KNOWLEDGE_REVISION
        ):
            raise ReplayProjectError("project activeKnowledge revision is invalid")

        serials: set[int] = set()
        for kind in ("workspaces", "materials"):
            activations = active.get(kind)
            if not isinstance(activations, dict) or set(activations) != set(ids[kind]):
                raise ReplayProjectError(
                    "project activeKnowledge %s index is incomplete" % kind
                )
            for token, serial in activations.items():
                if (
                    not HEX32_RE.fullmatch(str(token))
                    or isinstance(serial, bool)
                    or not isinstance(serial, int)
                    or serial <= 0
                    or serial > revision
                    or serial in serials
                ):
                    raise ReplayProjectError(
                        "project activeKnowledge activation is invalid"
                    )
                serials.add(serial)
        if revision != (max(serials) if serials else 0):
            raise ReplayProjectError(
                "project activeKnowledge revision does not name its newest activation"
            )
        return active

    @classmethod
    def _activate_knowledge(
        cls, manifest: dict[str, Any], kind: str, knowledge_id: str
    ) -> int:
        if kind not in {"workspaces", "materials"}:
            raise ReplayProjectError("active knowledge kind is invalid")
        token = str(knowledge_id or "")
        if not HEX32_RE.fullmatch(token):
            raise ReplayProjectError("active knowledgeId is invalid")
        active = manifest.get("activeKnowledge")
        if not isinstance(active, dict) or kind not in active:
            raise ReplayProjectError("project activeKnowledge index is invalid")
        record_ids = {
            str(record.get("knowledgeId", "") or "")
            for record in manifest["knowledge"][kind]
            if isinstance(record, Mapping)
        }
        if token not in record_ids:
            raise ReplayProjectError("active knowledgeId is absent")
        revision = active.get("revision")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
            or revision >= MAX_ACTIVE_KNOWLEDGE_REVISION
        ):
            raise ReplayProjectError("project activeKnowledge revision is exhausted")
        revision += 1
        active["revision"] = revision
        active[kind][token] = revision
        # This validates both the previously loaded state and the one allowed
        # new immutable record after its activation has been inserted.
        cls._active_knowledge_state(manifest)
        return revision

    def _load(self) -> dict[str, Any]:
        if self.root.is_symlink() or not self.root.is_dir():
            raise ReplayProjectError("project path must be a regular directory")
        try:
            manifest = read_json_object(self.manifest_path, MAX_PROJECT_BYTES, "project manifest")
        except ValueError as exc:
            raise ReplayProjectError(str(exc)) from exc
        if manifest.get("schema") != PROJECT_SCHEMA or manifest.get("schemaVersion") != PROJECT_VERSION:
            raise ReplayProjectError("unsupported project manifest schema/version")
        if not HEX32_RE.fullmatch(str(manifest.get("projectId", ""))):
            raise ReplayProjectError("projectId is invalid")
        scope = manifest.get("scope")
        if not isinstance(scope, dict) or set(scope) != {
            "gameId",
            "buildId",
            "platform",
            "apiFamily",
        }:
            raise ReplayProjectError("project scope is invalid")
        _text(scope.get("gameId"), "scope.gameId", 256)
        _text(scope.get("buildId"), "scope.buildId", 256)
        _text(scope.get("platform"), "scope.platform", 128, allow_empty=True)
        if _api_family(scope.get("apiFamily"), allow_empty=True) != scope.get(
            "apiFamily"
        ):
            raise ReplayProjectError("project apiFamily is not canonical")
        captures = manifest.get("captures")
        knowledge = manifest.get("knowledge")
        if not isinstance(captures, list) or len(captures) > MAX_CAPTURE_COUNT:
            raise ReplayProjectError("project captures are invalid or exceed the limit")
        if not isinstance(knowledge, dict) or set(knowledge) != {"workspaces", "aliases", "materials"}:
            raise ReplayProjectError("project knowledge index is invalid")
        capture_ids: set[str] = set()
        capture_revision_total = 0
        shader_total = 0

        def validate_shaders(value: Any, label: str) -> list[dict[str, Any]]:
            if not isinstance(value, list):
                raise ReplayProjectError("%s shaders are invalid" % label)
            seen: set[tuple[int, str, str, str]] = set()
            for shader in value:
                if not isinstance(shader, dict):
                    raise ReplayProjectError("%s shader record is invalid" % label)
                identity = _shader_identity(shader)
                if identity in seen:
                    raise ReplayProjectError(
                        "%s has duplicate shader identity" % label
                    )
                seen.add(identity)
                _positive(identity[0], "shader.resourceId")
                _text(identity[1], "shader.stage", 64)
                _sha(identity[3], "shader.rawSha256", True)
                _sha(shader.get("codeSha256"), "shader.codeSha256", True)
                _sha(shader.get("layoutHash"), "shader.layoutHash", True)
                _sha(shader.get("interfaceHash"), "shader.interfaceHash", True)
            return value

        for capture in captures:
            if not isinstance(capture, dict):
                raise ReplayProjectError("project capture record is invalid")
            capture_id = _sha(capture.get("captureId"), "capture.captureId")
            if capture_id in capture_ids:
                raise ReplayProjectError("project contains duplicate captureId")
            capture_ids.add(capture_id)
            _text(capture.get("capturePath"), "capture.capturePath", 4096, True)
            _text(capture.get("api"), "capture.api", 128, True)
            current_catalog_path = _text(
                capture.get("catalogPath"), "capture.catalogPath", 4096
            )
            current_catalog_sha256 = _sha(
                capture.get("catalogSha256"), "capture.catalogSha256"
            )
            current_imported_at = _text(
                capture.get("importedAt"), "capture.importedAt", 128
            )
            current_shaders = validate_shaders(
                capture.get("shaders"), "project capture"
            )
            revisions = capture.get("catalogRevisions")
            if revisions is not None:
                if not isinstance(revisions, list) or not revisions:
                    raise ReplayProjectError(
                        "project capture catalogRevisions must be a non-empty array"
                    )
                revision_hashes: set[str] = set()
                active_catalog_sha256 = _sha(
                    capture.get("activeCatalogSha256"),
                    "capture.activeCatalogSha256",
                )
                if active_catalog_sha256 != current_catalog_sha256:
                    raise ReplayProjectError(
                        "active capture revision does not match the top-level mirror"
                    )
                active_revision: dict[str, Any] | None = None
                for revision in revisions:
                    if not isinstance(revision, dict) or set(revision) != {
                        "catalogPath",
                        "catalogSha256",
                        "importedAt",
                        "shaders",
                    }:
                        raise ReplayProjectError(
                            "project capture catalog revision is invalid"
                        )
                    revision_path = _text(
                        revision.get("catalogPath"),
                        "capture.revision.catalogPath",
                        4096,
                    )
                    revision_sha256 = _sha(
                        revision.get("catalogSha256"),
                        "capture.revision.catalogSha256",
                    )
                    _text(
                        revision.get("importedAt"),
                        "capture.revision.importedAt",
                        128,
                    )
                    if revision_sha256 in revision_hashes:
                        raise ReplayProjectError(
                            "project capture has duplicate catalog revision"
                        )
                    revision_hashes.add(revision_sha256)
                    revision_shaders = validate_shaders(
                        revision.get("shaders"), "project capture revision"
                    )
                    shader_total += len(revision_shaders)
                    snapshot = self._artifact(
                        revision_path, "capture revision catalog"
                    )
                    snapshot_bytes = _file_bytes(
                        snapshot,
                        MAX_CAPTURE_CATALOG_BYTES,
                        "capture revision catalog",
                    )
                    if _sha256_bytes(snapshot_bytes) != revision_sha256:
                        raise ReplayProjectError(
                            "capture revision catalog hash mismatch"
                        )
                    if revision_sha256 == active_catalog_sha256:
                        active_revision = revision
                if active_revision is None:
                    raise ReplayProjectError(
                        "current capture catalog is absent from its revisions"
                    )
                if (
                    active_revision["catalogPath"] != current_catalog_path
                    or active_revision["catalogSha256"]
                    != current_catalog_sha256
                    or active_revision["importedAt"] != current_imported_at
                    or active_revision["shaders"] != current_shaders
                ):
                    raise ReplayProjectError(
                        "active capture revision mirror is inconsistent"
                    )
                capture_revision_total += len(revisions)
            else:
                # Backward-compatible v1 manifests predate explicit revision
                # history and represent exactly one revision.
                capture_revision_total += 1
                if capture.get("activeCatalogSha256") not in (None, ""):
                    raise ReplayProjectError(
                        "legacy capture must not contain an active revision pointer"
                    )
                shader_total += len(current_shaders)
        if shader_total > MAX_SHADER_COUNT:
            raise ReplayProjectError("project shader count exceeds the limit")
        if capture_revision_total > MAX_CAPTURE_REVISION_COUNT:
            raise ReplayProjectError("project capture revision count exceeds the limit")
        for kind in ("workspaces", "aliases", "materials"):
            records = knowledge.get(kind)
            if not isinstance(records, list) or len(records) > MAX_KNOWLEDGE_COUNT:
                raise ReplayProjectError("project %s knowledge exceeds the limit" % kind)
        # Missing state is the sole legacy-v1 migration path. It is derived in
        # memory here and atomically persisted by the next locked mutation.
        self._active_knowledge_state(manifest)
        for record in knowledge["workspaces"]:
            artifact = self._artifact(record.get("sourcePath"), "workspace source")
            if _sha256_bytes(_file_bytes(artifact, MAX_SOURCE_BYTES, "workspace source")) != _sha(record.get("sourceSha256"), "sourceSha256"):
                raise ReplayProjectError("project workspace source hash mismatch")
            analysis_path = record.get("analysisPath")
            if analysis_path is not None:
                analysis = self._artifact(analysis_path, "workspace analysis")
                if _sha256_bytes(
                    _file_bytes(analysis, MAX_ANALYSIS_BYTES, "workspace analysis")
                ) != _sha(record.get("analysisSha256"), "analysisSha256"):
                    raise ReplayProjectError("project workspace analysis hash mismatch")
        for record in knowledge["aliases"]:
            artifact = self._artifact(record.get("artifactPath"), "alias artifact")
            if _sha256_bytes(_file_bytes(artifact, MAX_ALIAS_BYTES, "alias artifact")) != _sha(record.get("artifactSha256"), "artifactSha256"):
                raise ReplayProjectError("project alias artifact hash mismatch")
        return manifest

    def _artifact(self, relative: Any, label: str) -> Path:
        value = _text(relative, label, 4096)
        path = (self.root / value).resolve()
        if not is_within(self.root, path):
            raise ReplayProjectError("%s escapes the project" % label)
        return path

    def _stage_catalog_snapshot(
        self,
        capture_id: str,
        catalog_sha256: str,
        payload: bytes,
        blobs: Mapping[str, bytes],
    ) -> str:
        """Publish one immutable project-owned catalog revision."""

        capture = _sha(capture_id, "captureId")
        revision = _sha(catalog_sha256, "catalogSha256")
        catalogs_root = self.root / "captures" / "catalogs"
        revisions_root = catalogs_root / capture / "revisions"
        revision_root = revisions_root / revision
        catalog_path = revision_root / "catalog.json"
        for path, label in (
            (catalogs_root, "capture catalog root"),
            (catalogs_root / capture, "capture catalog family"),
            (revisions_root, "capture catalog revisions"),
        ):
            path.mkdir(parents=True, exist_ok=True)
            if path.is_symlink() or not path.is_dir():
                raise ReplayProjectError("%s is redirected" % label)

        if revision_root.exists() or revision_root.is_symlink():
            if revision_root.is_symlink() or not revision_root.is_dir():
                raise ReplayProjectError("capture catalog revision is redirected")
            existing = _file_bytes(
                catalog_path,
                MAX_CAPTURE_CATALOG_BYTES,
                "project capture catalog snapshot",
            )
            if existing != payload or _sha256_bytes(existing) != revision:
                raise ReplayProjectError(
                    "project capture catalog revision bytes conflict"
                )
            for digest, blob in blobs.items():
                existing_blob = _file_bytes(
                    revision_root / "blobs" / (digest + ".bin"),
                    MAX_CAPTURE_CATALOG_BYTES,
                    "project capture shader snapshot",
                )
                if existing_blob != blob or _sha256_bytes(existing_blob) != digest:
                    raise ReplayProjectError(
                        "project capture shader snapshot hash mismatch"
                    )
        else:
            staging = Path(
                tempfile.mkdtemp(
                    prefix=".%s.staging-" % revision, dir=str(revisions_root)
                )
            )
            try:
                (staging / "blobs").mkdir()
                for digest, blob in blobs.items():
                    atomic_write_bytes(
                        staging / "blobs" / (digest + ".bin"), blob
                    )
                atomic_write_bytes(staging / "catalog.json", payload)
                staging.replace(revision_root)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return catalog_path.relative_to(self.root).as_posix()

    def _write(self, manifest: dict[str, Any]) -> None:
        self._active_knowledge_state(manifest)
        manifest["updatedAt"] = _utc_now()
        atomic_write_json(self.manifest_path, manifest)

    def status(self) -> dict[str, Any]:
        manifest = self._load()
        knowledge = manifest["knowledge"]
        return {
            "schema": PROJECT_SCHEMA,
            "schemaVersion": PROJECT_VERSION,
            "projectId": manifest["projectId"],
            "name": manifest.get("name", ""),
            "scope": dict(manifest["scope"]),
            "projectPath": str(self.root),
            "manifestPath": str(self.manifest_path),
            "captureCount": len(manifest["captures"]),
            "captureRevisionCount": sum(
                len(item.get("catalogRevisions", [])) or 1
                for item in manifest["captures"]
            ),
            "shaderCount": sum(len(item["shaders"]) for item in manifest["captures"]),
            "knowledgeCounts": {key: len(value) for key, value in knowledge.items()},
            "policy": {
                "crossFrameReplayStateReused": False,
                "oldAbiReportsReused": False,
                "autoApplyAuthorized": False,
                "liveAbiRevalidationRequired": True,
                "oneGameBuildPlatformApiScope": True,
            },
        }

    def import_capture(self, catalog_path: str | Path) -> dict[str, Any]:
        source, catalog, payload, blobs = _normalised_catalog_evidence(catalog_path)
        shaders = [_compact_shader(item) for item in catalog["shaders"]]
        if len(shaders) > MAX_SHADER_COUNT:
            raise ReplayProjectError("capture shader count exceeds the project limit")
        imported_at = _utc_now()
        catalog_sha256 = _sha256_bytes(payload)
        capture_id = _sha(catalog.get("captureId"), "captureId")
        capture_path = _text(
            str(catalog.get("capturePath", "") or ""),
            "capturePath",
            4096,
            True,
        )
        api = _text(str(catalog.get("api", "") or ""), "api", 128, True)
        with _thread_lock(self.root), _project_lock(self.root):
            manifest = self._load()
            scope = manifest["scope"]
            catalog_game = str(catalog.get("gameId", "") or "").strip()
            catalog_build = str(catalog.get("buildId", "") or "").strip()
            catalog_platform = str(catalog.get("platform", "") or "").strip()
            if catalog_game and catalog_game != scope["gameId"]:
                raise ReplayProjectError("capture catalog gameId conflicts with project scope")
            if catalog_build and catalog_build != scope["buildId"]:
                raise ReplayProjectError("capture catalog buildId conflicts with project scope")
            if scope["platform"] and catalog_platform and catalog_platform != scope["platform"]:
                raise ReplayProjectError("capture catalog platform conflicts with project scope")
            if not scope["platform"] and catalog_platform:
                scope["platform"] = _text(catalog_platform, "catalog.platform", 128)
            capture_api = _api_family(catalog.get("api"))
            if scope["apiFamily"] and capture_api != scope["apiFamily"]:
                raise ReplayProjectError("capture API conflicts with project scope")
            if not scope["apiFamily"]:
                scope["apiFamily"] = capture_api
            snapshot_path = self._stage_catalog_snapshot(
                capture_id, catalog_sha256, payload, blobs
            )
            revision = {
                "catalogPath": snapshot_path,
                "catalogSha256": catalog_sha256,
                "importedAt": imported_at,
                "shaders": shaders,
            }
            record = {
                "captureId": capture_id,
                "capturePath": capture_path,
                "api": api,
                "catalogPath": snapshot_path,
                "catalogSha256": catalog_sha256,
                "importedAt": imported_at,
                "activeCatalogSha256": catalog_sha256,
                "catalogRevisions": [revision],
                "shaders": shaders,
            }
            existing_index = next(
                (
                    index
                    for index, item in enumerate(manifest["captures"])
                    if item["captureId"] == record["captureId"]
                ),
                None,
            )
            existing = (
                manifest["captures"][existing_index]
                if existing_index is not None
                else None
            )
            if existing is not None:
                if (
                    existing.get("capturePath") != capture_path
                    or existing.get("api") != api
                ):
                    raise ReplayProjectError(
                        "captureId conflicts with its imported transport evidence"
                    )
                revisions = list(existing.get("catalogRevisions", []))
                if not revisions:
                    # Upgrade an old v1 record by reconstructing an immutable,
                    # canonical project-owned snapshot from its original
                    # catalog. Failure is closed: compact claims alone are not
                    # enough to manufacture historical catalog evidence.
                    (
                        unused_legacy_source,
                        legacy_catalog,
                        legacy_payload,
                        legacy_blobs,
                    ) = _normalised_catalog_evidence(
                        (
                            Path(existing["catalogPath"])
                            if Path(existing["catalogPath"]).is_absolute()
                            else self.root / existing["catalogPath"]
                        )
                    )
                    if _sha(legacy_catalog.get("captureId"), "legacy.captureId") != capture_id:
                        raise ReplayProjectError(
                            "legacy capture catalog identity changed before migration"
                        )
                    legacy_shaders = [
                        _compact_shader(item) for item in legacy_catalog["shaders"]
                    ]
                    if legacy_shaders != existing["shaders"]:
                        raise ReplayProjectError(
                            "legacy capture catalog evidence changed before migration"
                        )
                    legacy_sha256 = _sha256_bytes(legacy_payload)
                    legacy_path = self._stage_catalog_snapshot(
                        capture_id,
                        legacy_sha256,
                        legacy_payload,
                        legacy_blobs,
                    )
                    revisions = [
                        {
                            "catalogPath": legacy_path,
                            "catalogSha256": legacy_sha256,
                            "importedAt": existing["importedAt"],
                            "shaders": legacy_shaders,
                        }
                    ]
                    legacy_head = revisions[0]
                    existing = {
                        **existing,
                        "catalogPath": legacy_head["catalogPath"],
                        "catalogSha256": legacy_head["catalogSha256"],
                        "activeCatalogSha256": legacy_head["catalogSha256"],
                        "catalogRevisions": revisions,
                        "shaders": legacy_head["shaders"],
                    }

                matching_revisions = [
                    item
                    for item in revisions
                    if item["catalogSha256"] == catalog_sha256
                ]
                if len(matching_revisions) > 1:
                    raise ReplayProjectError(
                        "project contains duplicate capture catalog revision"
                    )
                if matching_revisions:
                    known = matching_revisions[0]
                    if known["shaders"] != shaders:
                        raise ReplayProjectError(
                            "known catalog revision has contradictory shader evidence"
                        )
                    exact_capture = self._capture_from_revision(existing, known)
                    plan = self._reuse_plan(manifest, exact_capture)
                    plan.update(
                        {
                            "idempotent": True,
                            "catalogRevisionKnown": True,
                            "catalogRevisionAdded": False,
                            "activeRevisionChanged": False,
                            "activeCatalogSha256": existing.get(
                                "activeCatalogSha256",
                                existing["catalogSha256"],
                            ),
                        }
                    )
                    # Persist only a necessary legacy migration. Reimporting a
                    # known historical revision never moves the active head.
                    if manifest["captures"][existing_index] != existing:
                        manifest["captures"][existing_index] = existing
                        self._write(manifest)
                    return plan

                if {
                    _shader_identity(item) for item in existing["shaders"]
                } != {_shader_identity(item) for item in shaders}:
                    raise ReplayProjectError(
                        "captureId is already imported with different Shader identities"
                    )
                revision_total = sum(
                    len(item.get("catalogRevisions", [])) or 1
                    for item in manifest["captures"]
                )
                if revision_total >= MAX_CAPTURE_REVISION_COUNT:
                    raise ReplayProjectError(
                        "project capture revision limit reached"
                    )
                shader_revision_total = sum(
                    sum(
                        len(revision_item.get("shaders", item["shaders"]))
                        for revision_item in item.get("catalogRevisions", [{}])
                    )
                    for item in manifest["captures"]
                )
                if shader_revision_total + len(shaders) > MAX_SHADER_COUNT:
                    raise ReplayProjectError(
                        "project capture revision shaders exceed the limit"
                    )
                revisions.append(revision)

                # captureId is a catalog-family identity (transport + Shader
                # byte identities), not a hash of reflection, use-sites, or
                # diagnostics. Select the just-imported validated revision for
                # matching while retaining every prior content hash/path.
                record["catalogRevisions"] = revisions
                manifest["captures"][existing_index] = record
                self._write(manifest)
                plan = self._reuse_plan(manifest, record)
                plan.update(
                    {
                        "idempotent": False,
                        "catalogRevisionKnown": False,
                        "catalogRevisionAdded": True,
                        "activeRevisionChanged": True,
                        "activeCatalogSha256": catalog_sha256,
                    }
                )
                return plan
            if len(manifest["captures"]) >= MAX_CAPTURE_COUNT:
                raise ReplayProjectError("project capture limit reached")
            manifest["captures"].append(record)
            self._write(manifest)
            plan = self._reuse_plan(manifest, record)
            plan.update(
                {
                    "idempotent": False,
                    "catalogRevisionKnown": False,
                    "catalogRevisionAdded": True,
                    "activeRevisionChanged": True,
                    "activeCatalogSha256": catalog_sha256,
                }
            )
            return plan

    @staticmethod
    def _capture_from_revision(
        capture: Mapping[str, Any], revision: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "captureId": capture["captureId"],
            "capturePath": capture.get("capturePath", ""),
            "api": capture.get("api", ""),
            "catalogPath": revision["catalogPath"],
            "catalogSha256": revision["catalogSha256"],
            "importedAt": revision["importedAt"],
            "shaders": revision["shaders"],
        }

    @staticmethod
    def _capture(
        manifest: Mapping[str, Any],
        capture_id: str,
        catalog_sha256: str = "",
    ) -> dict[str, Any]:
        digest = _sha(capture_id, "capture_id")
        matches = [item for item in manifest["captures"] if item["captureId"] == digest]
        if len(matches) != 1:
            raise ReplayProjectError("capture_id is not imported in this project")
        capture = matches[0]
        if not catalog_sha256:
            return capture
        revision_sha256 = _sha(catalog_sha256, "catalog_sha256")
        revisions = capture.get("catalogRevisions")
        if revisions is None:
            if capture.get("catalogSha256") != revision_sha256:
                raise ReplayProjectError(
                    "catalog revision is not imported in this project"
                )
            return capture
        revision_matches = [
            item
            for item in revisions
            if item["catalogSha256"] == revision_sha256
        ]
        if len(revision_matches) != 1:
            raise ReplayProjectError(
                "catalog revision is not imported in this project"
            )
        return ReplayProjectService._capture_from_revision(
            capture, revision_matches[0]
        )

    @staticmethod
    def _capture_for_provenance(
        manifest: Mapping[str, Any],
        capture_id: str,
        catalog_sha256: str = "",
    ) -> dict[str, Any]:
        if catalog_sha256:
            return ReplayProjectService._capture(
                manifest, capture_id, catalog_sha256
            )
        capture = ReplayProjectService._capture(manifest, capture_id)
        revisions = capture.get("catalogRevisions")
        if isinstance(revisions, list) and len(revisions) > 1:
            raise ReplayProjectError(
                "catalog revision is ambiguous; exact catalogSha256 provenance is required"
            )
        return capture

    @staticmethod
    def _capture_status_catalog_sha256(
        capture_status: Mapping[str, Any]
    ) -> str:
        declared_text = str(capture_status.get("catalogSha256", "") or "")
        declared = (
            _sha(declared_text, "workspace.capture.catalogSha256")
            if declared_text
            else ""
        )
        catalog_path = str(capture_status.get("catalogPath", "") or "")
        if not catalog_path:
            if declared:
                raise ReplayProjectError(
                    "workspace catalogSha256 requires catalogPath provenance"
                )
            return ""
        raw_path = Path(catalog_path).expanduser()
        if raw_path.exists() and raw_path.is_symlink():
            raise ReplayProjectError("workspace capture catalog is redirected")
        canonical_path = raw_path.resolve()
        physical = _file_bytes(
            canonical_path,
            MAX_CAPTURE_CATALOG_BYTES,
            "workspace capture catalog",
        )
        if declared and _sha256_bytes(physical) != declared:
            raise ReplayProjectError(
                "workspace catalogSha256 does not match catalogPath"
            )
        (
            unused_source,
            catalog,
            payload,
            unused_blobs,
        ) = _normalised_catalog_evidence(canonical_path)
        if catalog.get("captureId") != capture_status.get("captureId"):
            raise ReplayProjectError(
                "workspace capture catalog belongs to a different capture"
            )
        if capture_status.get("capturePath") not in (None, "") and catalog.get(
            "capturePath"
        ) != capture_status.get("capturePath"):
            raise ReplayProjectError(
                "workspace capturePath differs from its catalog"
            )
        if capture_status.get("api") not in (None, "") and catalog.get(
            "api"
        ) != capture_status.get("api"):
            raise ReplayProjectError("workspace capture API differs from its catalog")
        return _sha256_bytes(payload)

    @staticmethod
    def _capture_shader(capture: Mapping[str, Any], *, resource_id: int, stage: str, entry_point: str, raw_sha256: str = "") -> dict[str, Any]:
        matches = [
            item
            for item in capture["shaders"]
            if item["resourceId"] == resource_id
            and item["stage"] == stage
            and item.get("entryPoint", "") == entry_point
            and (not raw_sha256 or item.get("rawSha256") == raw_sha256)
        ]
        if len(matches) != 1:
            raise ReplayProjectError("knowledge does not select one imported capture shader")
        return matches[0]

    def register_workspace(self, status: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(status, Mapping) or status.get("schema") != "renderdoc-ultra-shader-workspace-status/v1":
            raise ReplayProjectError("workspace status must come from strict workspace validation")
        source = status.get("source")
        capture_status = status.get("capture")
        shader_status = status.get("shader")
        baseline = status.get("baseline")
        if not all(isinstance(value, Mapping) for value in (source, capture_status, shader_status, baseline)):
            raise ReplayProjectError("workspace status provenance is incomplete")
        if source.get("present") is not True:
            raise ReplayProjectError("workspace has no decompiled/edited source to register")
        # Project source reuse is intentionally HLSL-only.  Instruction-level
        # workspaces such as DXBC assembly have their own executable-editable
        # lane and fixed filename, but must not be persisted under a .hlsl
        # artifact or later seeded into an HLSL workspace.
        if (
            source.get("lane") != "executable-editable"
            or source.get("format") != "hlsl"
        ):
            raise ReplayProjectError(
                "project workspace source reuse supports only executable-editable HLSL"
            )
        source_path = _normal_path(str(source.get("path", "")))
        source_bytes = _file_bytes(source_path, MAX_SOURCE_BYTES, "workspace source")
        workspace_root = _normal_path(str(status.get("workspacePath", "")))
        analysis_path = workspace_root / "ai" / "analysis.md"
        analysis_bytes: bytes | None = None
        analysis_sha256 = ""
        if analysis_path.exists() or analysis_path.is_symlink():
            if not is_within(workspace_root, analysis_path.resolve()):
                raise ReplayProjectError("workspace analysis escapes its workspace")
            analysis_bytes = _file_bytes(
                analysis_path, MAX_ANALYSIS_BYTES, "workspace ai/analysis.md"
            )
            try:
                analysis_text = analysis_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ReplayProjectError("workspace analysis must be UTF-8 text") from exc
            if "\x00" in analysis_text:
                raise ReplayProjectError("workspace analysis contains NUL characters")
            analysis_sha256 = _sha256_bytes(analysis_bytes)
        manifest_path = _normal_path(str(status.get("manifestPath", "")))
        manifest_bytes = _file_bytes(manifest_path, MAX_PROJECT_BYTES, "workspace manifest")
        capture_id = _sha(capture_status.get("captureId"), "workspace.captureId")
        catalog_revision_sha256 = self._capture_status_catalog_sha256(
            capture_status
        )
        stage = _text(str(status.get("stage", "")), "workspace.stage", 64)
        entry = _text(str(status.get("entryPoint", "") or ""), "workspace.entryPoint", 512, True)
        resource_id = _positive(shader_status.get("resourceId"), "workspace.resourceId")
        raw = _sha(baseline.get("rawSha256"), "workspace.rawSha256")
        with _thread_lock(self.root), _project_lock(self.root):
            manifest = self._load()
            capture = self._capture_for_provenance(
                manifest, capture_id, catalog_revision_sha256
            )
            shader = self._capture_shader(capture, resource_id=resource_id, stage=stage, entry_point=entry, raw_sha256=raw)
            material = {
                "workspaceId": _text(str(status.get("workspaceId", "")), "workspaceId", 64),
                "captureId": capture_id,
                "originCatalogSha256": capture["catalogSha256"],
                "stage": stage,
                "entryPoint": entry,
                "rawSha256": raw,
                "codeSha256": _sha(baseline.get("codeSha256"), "workspace.codeSha256", True),
                "layoutHash": shader.get("layoutHash", ""),
                "interfaceHash": shader.get("interfaceHash", ""),
                "interfaceComplete": shader.get("interfaceComplete") is True,
                "baselineAbiSha256": _sha(baseline.get("abiSha256"), "workspace.abiSha256", True),
                "targetProfile": status.get("targetProfile"),
                "sourceSha256": _sha256_bytes(source_bytes),
                "analysisSha256": analysis_sha256,
            }
            knowledge_id = _canonical_sha256(material)[:32]
            relative = "knowledge/workspaces/%s.hlsl" % knowledge_id
            destination = self._artifact(relative, "workspace knowledge path")
            analysis_relative = (
                "knowledge/workspaces/%s.analysis.md" % analysis_sha256
                if analysis_bytes is not None
                else ""
            )
            existing = next((item for item in manifest["knowledge"]["workspaces"] if item["knowledgeId"] == knowledge_id), None)
            if existing is not None:
                if _sha256_bytes(_file_bytes(destination, MAX_SOURCE_BYTES, "workspace source")) != material["sourceSha256"]:
                    raise ReplayProjectError("existing workspace knowledge artifact changed")
                self._activate_knowledge(manifest, "workspaces", knowledge_id)
                self._write(manifest)
                return {"knowledge": existing, "idempotent": True, "applied": False}
            if len(manifest["knowledge"]["workspaces"]) >= MAX_KNOWLEDGE_COUNT:
                raise ReplayProjectError("workspace knowledge limit reached")
            atomic_write_bytes(destination, source_bytes)
            if analysis_bytes is not None:
                analysis_destination = self._artifact(
                    analysis_relative, "workspace analysis knowledge path"
                )
                if analysis_destination.exists():
                    if _sha256_bytes(
                        _file_bytes(
                            analysis_destination,
                            MAX_ANALYSIS_BYTES,
                            "workspace analysis",
                        )
                    ) != analysis_sha256:
                        raise ReplayProjectError(
                            "existing content-addressed analysis changed"
                        )
                else:
                    atomic_write_bytes(analysis_destination, analysis_bytes)
            record = dict(material)
            record.update({
                "knowledgeId": knowledge_id,
                "sourcePath": relative,
                "sourceOriginPath": str(source_path),
                "workspacePath": str(status.get("workspacePath", "")),
                "workspaceManifestSha256": _sha256_bytes(manifest_bytes),
                "registeredAt": _utc_now(),
            })
            if analysis_bytes is not None:
                record.update(
                    {
                        "analysisPath": analysis_relative,
                        "analysisKind": "ai-explanatory-text",
                        "analysisPolicy": {
                            "physicalEvidence": False,
                            "applyAuthorization": False,
                            "reviewRequired": True,
                        },
                    }
                )
            manifest["knowledge"]["workspaces"].append(record)
            self._activate_knowledge(manifest, "workspaces", knowledge_id)
            self._write(manifest)
            return {"knowledge": record, "idempotent": False, "applied": False}

    def register_alias_table(self, alias_table_path: str | Path) -> dict[str, Any]:
        source = _normal_path(alias_table_path)
        raw = _file_bytes(source, MAX_ALIAS_BYTES, "alias table")
        try:
            table = json.loads(raw.decode("utf-8-sig", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReplayProjectError("alias table is not UTF-8 JSON") from exc
        if not isinstance(table, dict) or table.get("schema") != ALIAS_SCHEMA or table.get("schemaVersion") != ALIAS_VERSION or table.get("verifiedOnly") is not True:
            raise ReplayProjectError("only a verified exported alias table can enter a project")
        aliases = table.get("aliases")
        if not isinstance(aliases, list) or len(aliases) > MAX_ALIASES:
            raise ReplayProjectError("alias table aliases are invalid or oversized")
        capture_id = _sha(table.get("captureId"), "alias.captureId")
        alias_catalog_text = str(table.get("catalogSha256", "") or "")
        alias_catalog_sha256 = (
            _sha(alias_catalog_text, "alias.catalogSha256")
            if alias_catalog_text
            else ""
        )
        with _thread_lock(self.root), _project_lock(self.root):
            manifest = self._load()
            capture = self._capture_for_provenance(
                manifest, capture_id, alias_catalog_sha256
            )
            groups: dict[tuple[int, str, str, str], dict[str, Any]] = {}
            for index, alias in enumerate(aliases):
                if not isinstance(alias, Mapping) or alias.get("verified") is not True or alias.get("captureId") != capture_id:
                    raise ReplayProjectError("aliases[%d] is not verified/capture-bound" % index)
                resource = _positive(alias.get("shaderResourceId"), "alias.shaderResourceId")
                stage = _text(str(alias.get("stage", "")), "alias.stage", 64)
                entry = _text(str(alias.get("entryPoint", "") or ""), "alias.entryPoint", 512, True)
                raw_hash = _sha(alias.get("rawSha256"), "alias.rawSha256")
                shader = self._capture_shader(capture, resource_id=resource, stage=stage, entry_point=entry, raw_sha256=raw_hash)
                code_hash = _sha(alias.get("codeSha256"), "alias.codeSha256", True)
                if code_hash and code_hash != shader.get("codeSha256"):
                    raise ReplayProjectError("alias codeSha256 differs from imported capture")
                key = (resource, stage, entry, raw_hash)
                group = groups.setdefault(key, {
                    "stage": stage,
                    "entryPoint": entry,
                    "rawSha256": raw_hash,
                    "codeSha256": shader.get("codeSha256", ""),
                    "layoutHash": shader.get("layoutHash", ""),
                    "interfaceHash": shader.get("interfaceHash", ""),
                    "interfaceComplete": shader.get("interfaceComplete") is True,
                    "aliases": [],
                })
                compact = {
                    "bindingKind": _text(str(alias.get("bindingKind", "")), "alias.bindingKind", 64),
                    "registerSpace": int(alias.get("registerSpace", 0)),
                    "bindNumber": int(alias.get("bindNumber", 0)),
                    "byteOffset": int(alias.get("byteOffset", 0)),
                    "canonicalPath": _text(str(alias.get("canonicalPath", "") or ""), "alias.canonicalPath", 2048, True),
                    "alias": _text(str(alias.get("alias", "")), "alias.alias", 1024),
                }
                if min(compact["registerSpace"], compact["bindNumber"], compact["byteOffset"]) < 0:
                    raise ReplayProjectError("alias physical coordinates must be non-negative")
                group["aliases"].append(compact)
            group_values = list(groups.values())
            identity = {
                "captureId": capture_id,
                "originCatalogSha256": capture["catalogSha256"],
                "groups": group_values,
                "artifactSha256": _sha256_bytes(raw),
            }
            knowledge_id = _canonical_sha256(identity)[:32]
            relative = "knowledge/aliases/%s.json" % knowledge_id
            destination = self._artifact(relative, "alias knowledge path")
            existing = next((item for item in manifest["knowledge"]["aliases"] if item["knowledgeId"] == knowledge_id), None)
            if existing is not None:
                return {"knowledge": existing, "idempotent": True, "applied": False}
            if len(manifest["knowledge"]["aliases"]) >= MAX_KNOWLEDGE_COUNT:
                raise ReplayProjectError("alias knowledge limit reached")
            atomic_write_bytes(destination, raw)
            record = {
                "knowledgeId": knowledge_id,
                "captureId": capture_id,
                "originCatalogSha256": capture["catalogSha256"],
                "artifactPath": relative,
                "artifactSha256": _sha256_bytes(raw),
                "groups": group_values,
                "aliasCount": len(aliases),
                "registeredAt": _utc_now(),
            }
            manifest["knowledge"]["aliases"].append(record)
            self._write(manifest)
            return {"knowledge": record, "idempotent": False, "applied": False}

    @staticmethod
    def _live_material_parameters(session: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_parameters = session.get("parameters")
        if (
            not isinstance(raw_parameters, list)
            or not raw_parameters
            or len(raw_parameters) > MAX_MATERIAL_PARAMETERS
        ):
            raise ReplayProjectError("material session parameters are invalid")
        parameters: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_typed: set[tuple[str, str, int]] = set()
        for index, raw_parameter in enumerate(raw_parameters):
            if not isinstance(raw_parameter, Mapping):
                raise ReplayProjectError(
                    "material session parameter[%d] is invalid" % index
                )
            parameter_id = str(raw_parameter.get("parameterId", "") or "").lower()
            if not HEX32_RE.fullmatch(parameter_id) or parameter_id in seen_ids:
                raise ReplayProjectError(
                    "material session parameter ID is invalid or duplicated"
                )
            path = _text(
                str(raw_parameter.get("path", "")),
                "material.path",
                2048,
            )
            base_type = _text(
                str(raw_parameter.get("baseType", "")),
                "material.baseType",
                32,
            )
            if base_type not in {"Float", "SInt", "UInt"}:
                raise ReplayProjectError("material parameter baseType is unsupported")
            components = _positive(
                raw_parameter.get("components"), "material.components"
            )
            if components > 4:
                raise ReplayProjectError("material parameter components exceeds vec4")
            typed = (path, base_type, components)
            if typed in seen_typed:
                raise ReplayProjectError(
                    "material session contains duplicate typed parameter paths"
                )
            value = raw_parameter.get("value")
            values = value if isinstance(value, list) else [value]
            if len(values) != components:
                raise ReplayProjectError(
                    "material parameter value shape differs from components"
                )
            if base_type == "Float":
                if any(
                    isinstance(number, bool)
                    or not isinstance(number, (int, float))
                    or not math.isfinite(float(number))
                    or abs(float(number)) > 3.4028234663852886e38
                    for number in values
                ):
                    raise ReplayProjectError(
                        "material Float value is not finite float32 data"
                    )
            else:
                lower, upper = (
                    (-2147483648, 2147483647)
                    if base_type == "SInt"
                    else (0, 4294967295)
                )
                if any(
                    isinstance(number, bool)
                    or not isinstance(number, int)
                    or number < lower
                    or number > upper
                    for number in values
                ):
                    raise ReplayProjectError(
                        "material integer value is outside its reflected 32-bit type"
                    )
            seen_ids.add(parameter_id)
            seen_typed.add(typed)
            parameters.append(
                {
                    "parameterId": parameter_id,
                    "path": path,
                    "baseType": base_type,
                    "components": components,
                    "value": copy.deepcopy(value),
                }
            )
        return parameters

    def save_material_knowledge(self, session: Mapping[str, Any], parameter_ids: Sequence[str]) -> dict[str, Any]:
        if not isinstance(session, Mapping) or session.get("schema") != "renderdoc-ultra-material-session/v1":
            raise ReplayProjectError("material status must come from a live material session")
        requested = [str(value or "").strip().lower() for value in parameter_ids]
        if not requested or len(requested) > MAX_MATERIAL_PARAMETERS or len(set(requested)) != len(requested):
            raise ReplayProjectError("parameter_ids must contain unique bounded parameter IDs")
        live_parameters = self._live_material_parameters(session)
        available = {item["parameterId"]: item for item in live_parameters}
        if any(value not in available for value in requested):
            raise ReplayProjectError("parameter_ids contains an unknown material parameter")
        capture_id = _sha(session.get("captureId"), "material.captureId")
        material_catalog_text = str(session.get("catalogSha256", "") or "")
        material_catalog_sha256 = (
            _sha(material_catalog_text, "material.catalogSha256")
            if material_catalog_text
            else ""
        )
        resource = _positive(session.get("shaderResourceId"), "material.shaderResourceId")
        stage = _text(str(session.get("stage", "")), "material.stage", 64)
        with _thread_lock(self.root), _project_lock(self.root):
            manifest = self._load()
            capture = self._capture_for_provenance(
                manifest, capture_id, material_catalog_sha256
            )
            matches = [item for item in capture["shaders"] if item["resourceId"] == resource and item["stage"] == stage]
            if len(matches) != 1:
                raise ReplayProjectError("material session does not select one imported shader")
            shader = matches[0]
            block_path = _text(
                str(session.get("constantBlockPath", "")),
                "material.constantBlockPath",
                2048,
            )
            block_layout_sha256 = _sha(
                session.get("layoutSha256"), "material.layoutSha256"
            )
            active = self._active_knowledge_state(manifest)
            head_candidates: list[tuple[int, str]] = []
            for raw_record in manifest["knowledge"]["materials"]:
                if (
                    isinstance(raw_record, Mapping)
                    and raw_record.get("constantBlockPath") == block_path
                    and raw_record.get("constantBlockLayoutSha256")
                    == block_layout_sha256
                    and _match_mode(raw_record, shader)
                ):
                    token = str(raw_record.get("knowledgeId", "") or "")
                    serial = active["materials"].get(token)
                    if not isinstance(serial, int):
                        raise ReplayProjectError(
                            "material active-head activation is invalid"
                        )
                    head_candidates.append((serial, token))

            live_by_typed = {
                (item["path"], item["baseType"], item["components"]): item
                for item in live_parameters
            }
            merged: dict[tuple[str, str, int], dict[str, Any]] = {}
            if head_candidates:
                unused_serial, head_id = max(head_candidates)
                head = self._material_knowledge_from_manifest(manifest, head_id)
                for saved in head["parameters"]:
                    typed = (
                        saved["path"],
                        saved["baseType"],
                        saved["components"],
                    )
                    live_parameter = live_by_typed.get(typed)
                    if (
                        not isinstance(live_parameter, Mapping)
                        or live_parameter.get("parameterId")
                        != saved.get("parameterId")
                    ):
                        raise ReplayProjectError(
                            "durable material head differs from the exact live layout"
                        )
                    merged[typed] = {
                        "parameterId": live_parameter["parameterId"],
                        "path": live_parameter["path"],
                        "baseType": live_parameter["baseType"],
                        "components": live_parameter["components"],
                        "value": copy.deepcopy(saved["value"]),
                    }

            for parameter_id in requested:
                item = available[parameter_id]
                typed = (item["path"], item["baseType"], item["components"])
                merged[typed] = copy.deepcopy(item)
            parameters = [
                merged[(item["path"], item["baseType"], item["components"])]
                for item in live_parameters
                if (item["path"], item["baseType"], item["components"])
                in merged
            ]
            material = {
                "captureId": capture_id,
                "originCatalogSha256": capture["catalogSha256"],
                "stage": shader["stage"],
                "entryPoint": shader.get("entryPoint", ""),
                "rawSha256": shader.get("rawSha256", ""),
                "codeSha256": shader.get("codeSha256", ""),
                "layoutHash": shader.get("layoutHash", ""),
                "interfaceHash": shader.get("interfaceHash", ""),
                "interfaceComplete": shader.get("interfaceComplete") is True,
                "constantBlockPath": block_path,
                "constantBlockLayoutSha256": block_layout_sha256,
                "parameters": parameters,
            }
            knowledge_id = _canonical_sha256(material)[:32]
            existing = next((item for item in manifest["knowledge"]["materials"] if item["knowledgeId"] == knowledge_id), None)
            if existing is not None:
                self._activate_knowledge(manifest, "materials", knowledge_id)
                self._write(manifest)
                return {"knowledge": existing, "idempotent": True, "applied": False}
            if len(manifest["knowledge"]["materials"]) >= MAX_KNOWLEDGE_COUNT:
                raise ReplayProjectError("material knowledge limit reached")
            record = dict(material)
            record.update({"knowledgeId": knowledge_id, "registeredAt": _utc_now()})
            manifest["knowledge"]["materials"].append(record)
            self._activate_knowledge(manifest, "materials", knowledge_id)
            self._write(manifest)
            return {"knowledge": record, "idempotent": False, "applied": False}

    def restore_binding_state(
        self, capture_id: str, catalog_sha256: str
    ) -> dict[str, Any]:
        """Return the exact immutable project hashes used by an RDC hint.

        This is deliberately read-only.  The returned capture/catalog and
        knowledge hashes are recomputed from a fully validated manifest; an
        embedded RDC index cannot supply or relax any of them.
        """

        manifest = self._load()
        capture = self._capture(manifest, capture_id, catalog_sha256)
        state = self._reuse_knowledge_state(manifest)
        restore_scope = self._capture_referenced_knowledge_scope(capture, state)
        binding = self._reuse_cache_binding(
            manifest, capture, restore_scope["knowledgeContentSha256"]
        )
        return {
            "projectId": binding["projectId"],
            "projectPath": str(self.root),
            "scopeSha256": binding["scopeSha256"],
            "captureId": binding["captureId"],
            "catalogSha256": binding["catalogSha256"],
            "knowledgeContentSha256": binding["knowledgeContentSha256"],
            "matcherPolicySha256": binding["matcherPolicySha256"],
            "capture": copy.deepcopy(capture),
        }

    def _workspace_knowledge_from_manifest(
        self,
        manifest: Mapping[str, Any],
        knowledge_id: str,
        *,
        record: Mapping[str, Any] | None = None,
        source_artifact_verified: bool = False,
        provenance_index: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        token = str(knowledge_id or "").strip().lower()
        if not HEX32_RE.fullmatch(token):
            raise ReplayProjectError(
                "knowledge_id must be 32 lowercase hexadecimal characters"
            )
        if record is None:
            matches = [
                item
                for item in manifest["knowledge"]["workspaces"]
                if isinstance(item, Mapping) and item.get("knowledgeId") == token
            ]
            if len(matches) != 1:
                raise ReplayProjectError(
                    "workspace knowledge_id does not select one project record"
                )
            record = matches[0]
        elif (
            not isinstance(record, Mapping)
            or record.get("knowledgeId") != token
        ):
            raise ReplayProjectError(
                "indexed workspace knowledge differs from its knowledge_id"
            )
        workspace_id = str(record.get("workspaceId", "") or "")
        if not HEX32_RE.fullmatch(workspace_id):
            raise ReplayProjectError("workspace knowledge workspaceId is invalid")
        capture_id = _sha(record.get("captureId"), "workspace.captureId")
        origin_catalog_sha256 = _sha(
            record.get("originCatalogSha256"), "workspace.originCatalogSha256"
        )
        stage = _text(str(record.get("stage", "")), "workspace.stage", 64)
        entry_point = _text(
            str(record.get("entryPoint", "") or ""),
            "workspace.entryPoint",
            512,
            allow_empty=True,
        )
        raw_sha256 = _sha(record.get("rawSha256"), "workspace.rawSha256")
        code_sha256 = _sha(
            record.get("codeSha256"), "workspace.codeSha256", allow_empty=True
        )
        layout_hash = _sha(
            record.get("layoutHash"), "workspace.layoutHash", allow_empty=True
        )
        interface_hash = _sha(
            record.get("interfaceHash"),
            "workspace.interfaceHash",
            allow_empty=True,
        )
        interface_complete = record.get("interfaceComplete") is True
        baseline_abi_sha256 = _sha(
            record.get("baselineAbiSha256"),
            "workspace.baselineAbiSha256",
            allow_empty=True,
        )
        source_sha256 = _sha(
            record.get("sourceSha256"), "workspace.sourceSha256"
        )
        analysis_sha256 = _sha(
            record.get("analysisSha256"),
            "workspace.analysisSha256",
            allow_empty=True,
        )
        if provenance_index is None:
            capture = self._capture_for_provenance(
                manifest, capture_id, origin_catalog_sha256
            )
            shader = self._capture_shader(
                capture,
                resource_id=_positive(
                    next(
                        (
                            item.get("resourceId")
                            for item in capture["shaders"]
                            if item.get("stage") == stage
                            and item.get("entryPoint", "") == entry_point
                            and item.get("rawSha256") == raw_sha256
                        ),
                        0,
                    ),
                    "workspace shader resourceId",
                ),
                stage=stage,
                entry_point=entry_point,
                raw_sha256=raw_sha256,
            )
        else:
            provenance = provenance_index.get(
                (capture_id, origin_catalog_sha256)
            )
            shader_matches = (
                provenance.get("shaders", {}).get(
                    (stage, entry_point, raw_sha256), []
                )
                if isinstance(provenance, Mapping)
                else []
            )
            if not shader_matches or not isinstance(shader_matches[0], Mapping):
                raise ReplayProjectError(
                    "workspace knowledge does not select an imported Shader"
                )
            # Workspace identity is resource-agnostic. This retains the existing
            # behaviour for physically identical shaders bound at several slots.
            shader = shader_matches[0]
        if any(
            (
                shader.get("codeSha256", "") != code_sha256,
                shader.get("layoutHash", "") != layout_hash,
                shader.get("interfaceHash", "") != interface_hash,
                (shader.get("interfaceComplete") is True) != interface_complete,
            )
        ):
            raise ReplayProjectError(
                "workspace knowledge physical identity differs from its capture"
            )
        source_path = self._artifact(
            record.get("sourcePath"), "workspace knowledge source"
        )
        if not source_artifact_verified and (
            _sha256_bytes(
                _file_bytes(source_path, MAX_SOURCE_BYTES, "workspace knowledge source")
            )
            != source_sha256
        ):
            raise ReplayProjectError("workspace knowledge source hash mismatch")
        identity = {
            "workspaceId": workspace_id,
            "captureId": capture_id,
            "originCatalogSha256": origin_catalog_sha256,
            "stage": stage,
            "entryPoint": entry_point,
            "rawSha256": raw_sha256,
            "codeSha256": code_sha256,
            "layoutHash": layout_hash,
            "interfaceHash": interface_hash,
            "interfaceComplete": interface_complete,
            "baselineAbiSha256": baseline_abi_sha256,
            "targetProfile": copy.deepcopy(record.get("targetProfile")),
            "sourceSha256": source_sha256,
            "analysisSha256": analysis_sha256,
        }
        if _canonical_sha256(identity)[:32] != token:
            raise ReplayProjectError("workspace knowledgeId does not match its contents")
        return copy.deepcopy(dict(record))

    def get_workspace_knowledge(self, knowledge_id: str) -> dict[str, Any]:
        """Read one content-bound HLSL knowledge record with full provenance."""

        return self._workspace_knowledge_from_manifest(self._load(), knowledge_id)

    def _material_knowledge_from_manifest(
        self,
        manifest: Mapping[str, Any],
        knowledge_id: str,
        *,
        record: Mapping[str, Any] | None = None,
        provenance_index: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        token = str(knowledge_id or "").strip().lower()
        if not HEX32_RE.fullmatch(token):
            raise ReplayProjectError(
                "knowledge_id must be 32 lowercase hexadecimal characters"
            )
        if record is None:
            matches = [
                item
                for item in manifest["knowledge"]["materials"]
                if isinstance(item, Mapping) and item.get("knowledgeId") == token
            ]
            if len(matches) != 1:
                raise ReplayProjectError(
                    "material knowledge_id does not select one project record"
                )
            record = matches[0]
        elif (
            not isinstance(record, Mapping)
            or record.get("knowledgeId") != token
        ):
            raise ReplayProjectError(
                "indexed material knowledge differs from its knowledge_id"
            )
        capture_id = _sha(record.get("captureId"), "material.captureId")
        origin_catalog_sha256 = _sha(
            record.get("originCatalogSha256"), "material.originCatalogSha256"
        )
        stage = _text(str(record.get("stage", "")), "material.stage", 64)
        entry_point = _text(
            str(record.get("entryPoint", "") or ""),
            "material.entryPoint",
            512,
            allow_empty=True,
        )
        raw_sha256 = _sha(record.get("rawSha256"), "material.rawSha256")
        code_sha256 = _sha(
            record.get("codeSha256"), "material.codeSha256", allow_empty=True
        )
        layout_hash = _sha(
            record.get("layoutHash"), "material.layoutHash", allow_empty=True
        )
        interface_hash = _sha(
            record.get("interfaceHash"),
            "material.interfaceHash",
            allow_empty=True,
        )
        interface_complete = record.get("interfaceComplete") is True
        block_path = _text(
            str(record.get("constantBlockPath", "")),
            "material.constantBlockPath",
            2048,
        )
        block_layout_sha256 = _sha(
            record.get("constantBlockLayoutSha256"),
            "material.constantBlockLayoutSha256",
        )
        raw_parameters = record.get("parameters")
        if (
            not isinstance(raw_parameters, list)
            or not raw_parameters
            or len(raw_parameters) > MAX_MATERIAL_PARAMETERS
        ):
            raise ReplayProjectError("material knowledge parameters are invalid")
        parameters: list[dict[str, Any]] = []
        seen_parameter_ids: set[str] = set()
        for index, raw_parameter in enumerate(raw_parameters):
            if not isinstance(raw_parameter, Mapping) or set(raw_parameter) != {
                "parameterId",
                "path",
                "baseType",
                "components",
                "value",
            }:
                raise ReplayProjectError(
                    "material parameter[%d] fields are invalid" % index
                )
            parameter_id = str(raw_parameter.get("parameterId", "") or "")
            if (
                not HEX32_RE.fullmatch(parameter_id)
                or parameter_id in seen_parameter_ids
            ):
                raise ReplayProjectError(
                    "material parameter[%d] ID is invalid or duplicated" % index
                )
            seen_parameter_ids.add(parameter_id)
            path = _text(
                str(raw_parameter.get("path", "")),
                "material.parameter.path",
                2048,
            )
            base_type = _text(
                str(raw_parameter.get("baseType", "")),
                "material.parameter.baseType",
                32,
            )
            if base_type not in {"Float", "SInt", "UInt"}:
                raise ReplayProjectError("material parameter baseType is unsupported")
            components = _positive(
                raw_parameter.get("components"), "material.parameter.components"
            )
            if components > 4:
                raise ReplayProjectError(
                    "material parameter components exceeds vec4"
                )
            raw_value = raw_parameter.get("value")
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            if len(values) != components:
                raise ReplayProjectError(
                    "material parameter value shape differs from components"
                )
            if base_type == "Float":
                if any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or abs(float(value)) > 3.4028234663852886e38
                    for value in values
                ):
                    raise ReplayProjectError(
                        "material Float value is not finite float32 data"
                    )
            else:
                lower, upper = (
                    (-2147483648, 2147483647)
                    if base_type == "SInt"
                    else (0, 4294967295)
                )
                if any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < lower
                    or value > upper
                    for value in values
                ):
                    raise ReplayProjectError(
                        "material integer value is outside its reflected 32-bit type"
                    )
            parameters.append(
                {
                    "parameterId": parameter_id,
                    "path": path,
                    "baseType": base_type,
                    "components": components,
                    "value": copy.deepcopy(raw_value),
                }
            )
        if provenance_index is None:
            capture = self._capture_for_provenance(
                manifest, capture_id, origin_catalog_sha256
            )
            matching_shaders = [
                item
                for item in capture["shaders"]
                if item.get("stage") == stage
                and item.get("entryPoint", "") == entry_point
                and item.get("rawSha256") == raw_sha256
            ]
        else:
            provenance = provenance_index.get(
                (capture_id, origin_catalog_sha256)
            )
            matching_shaders = (
                list(
                    provenance.get("shaders", {}).get(
                        (stage, entry_point, raw_sha256), []
                    )
                )
                if isinstance(provenance, Mapping)
                else []
            )
        if len(matching_shaders) != 1:
            raise ReplayProjectError(
                "material knowledge does not select one imported Shader"
            )
        shader = matching_shaders[0]
        if any(
            (
                shader.get("codeSha256", "") != code_sha256,
                shader.get("layoutHash", "") != layout_hash,
                shader.get("interfaceHash", "") != interface_hash,
                (shader.get("interfaceComplete") is True) != interface_complete,
            )
        ):
            raise ReplayProjectError(
                "material knowledge physical identity differs from its capture"
            )
        identity = {
            "captureId": capture_id,
            "originCatalogSha256": origin_catalog_sha256,
            "stage": stage,
            "entryPoint": entry_point,
            "rawSha256": raw_sha256,
            "codeSha256": code_sha256,
            "layoutHash": layout_hash,
            "interfaceHash": interface_hash,
            "interfaceComplete": interface_complete,
            "constantBlockPath": block_path,
            "constantBlockLayoutSha256": block_layout_sha256,
            "parameters": parameters,
        }
        if _canonical_sha256(identity)[:32] != token:
            raise ReplayProjectError("material knowledgeId does not match its contents")
        _text(record.get("registeredAt"), "material.registeredAt", 128)
        return copy.deepcopy(dict(record))

    def get_material_knowledge(self, knowledge_id: str) -> dict[str, Any]:
        """Read and strictly validate one portable material-value record."""

        return self._material_knowledge_from_manifest(self._load(), knowledge_id)

    @staticmethod
    def _restore_provenance_index(
        manifest: Mapping[str, Any],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Index exact capture revisions and their shaders for one restore pass."""

        result: dict[tuple[str, str], dict[str, Any]] = {}
        for capture in manifest["captures"]:
            revisions = capture.get("catalogRevisions")
            exact_captures = (
                [
                    ReplayProjectService._capture_from_revision(capture, revision)
                    for revision in revisions
                ]
                if isinstance(revisions, list)
                else [capture]
            )
            for exact_capture in exact_captures:
                key = (
                    _sha(exact_capture.get("captureId"), "capture.captureId"),
                    _sha(
                        exact_capture.get("catalogSha256"),
                        "capture.catalogSha256",
                    ),
                )
                if key in result:
                    raise ReplayProjectError(
                        "capture provenance revision is duplicated"
                    )
                shaders: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
                for shader in exact_capture["shaders"]:
                    shader_key = (
                        _text(str(shader.get("stage", "")), "shader.stage", 64),
                        _text(
                            str(shader.get("entryPoint", "") or ""),
                            "shader.entryPoint",
                            512,
                            allow_empty=True,
                        ),
                        _sha(shader.get("rawSha256"), "shader.rawSha256"),
                    )
                    shaders.setdefault(shader_key, []).append(shader)
                result[key] = {
                    "capture": exact_capture,
                    "shaders": shaders,
                }
        return result

    def restore_knowledge_snapshot(
        self, capture_id: str, catalog_sha256: str
    ) -> dict[str, Any]:
        """Validate one project revision once for a bounded restore operation.

        The returned dictionaries contain only knowledge referenced by exact
        matches for this capture. The manifest loader has already hash-verified
        each project artifact once; indexed per-record validation therefore
        stays linear without weakening source/provenance/ABI checks.
        """

        manifest = self._load()
        capture = self._capture(manifest, capture_id, catalog_sha256)
        state = self._reuse_knowledge_state(manifest)
        restore_scope = self._capture_referenced_knowledge_scope(capture, state)
        binding = self._reuse_cache_binding(
            manifest, capture, restore_scope["knowledgeContentSha256"]
        )
        provenance_index = self._restore_provenance_index(manifest)
        workspaces: dict[str, dict[str, Any]] = {}
        for knowledge_id_value in restore_scope["workspaceIds"]:
            workspaces[knowledge_id_value] = self._workspace_knowledge_from_manifest(
                manifest,
                knowledge_id_value,
                record=state["workspaces"][knowledge_id_value],
                source_artifact_verified=True,
                provenance_index=provenance_index,
            )
        materials: dict[str, dict[str, Any]] = {}
        for knowledge_id_value in restore_scope["materialIds"]:
            materials[knowledge_id_value] = self._material_knowledge_from_manifest(
                manifest,
                knowledge_id_value,
                record=state["materials"][knowledge_id_value],
                provenance_index=provenance_index,
            )
        return {
            "projectId": binding["projectId"],
            "projectPath": str(self.root),
            "scopeSha256": binding["scopeSha256"],
            "captureId": binding["captureId"],
            "catalogSha256": binding["catalogSha256"],
            "knowledgeContentSha256": binding["knowledgeContentSha256"],
            "matcherPolicySha256": binding["matcherPolicySha256"],
            "capture": copy.deepcopy(capture),
            "workspaces": workspaces,
            "materials": materials,
        }

    @staticmethod
    def _reuse_shader_identity(shader: Mapping[str, Any]) -> dict[str, Any]:
        """Project only the physical fields which can influence reuse matching."""

        return {
            "resourceId": _positive(shader.get("resourceId"), "shader.resourceId"),
            "stage": _text(str(shader.get("stage", "")), "shader.stage", 64),
            "entryPoint": _text(
                str(shader.get("entryPoint", "") or ""),
                "shader.entryPoint",
                512,
                allow_empty=True,
            ),
            "rawSha256": _sha(
                shader.get("rawSha256"), "shader.rawSha256", allow_empty=True
            ),
            "codeSha256": _sha(
                shader.get("codeSha256"), "shader.codeSha256", allow_empty=True
            ),
            "interfaceHash": _sha(
                shader.get("interfaceHash"),
                "shader.interfaceHash",
                allow_empty=True,
            ),
            "layoutHash": _sha(
                shader.get("layoutHash"), "shader.layoutHash", allow_empty=True
            ),
            "interfaceComplete": shader.get("interfaceComplete") is True,
        }

    @staticmethod
    def _reuse_index_keys(value: Mapping[str, Any]) -> tuple[tuple[Any, ...] | None, tuple[Any, ...] | None]:
        stage = _text(str(value.get("stage", "")), "knowledge.stage", 64)
        entry = _text(
            str(value.get("entryPoint", "") or ""),
            "knowledge.entryPoint",
            512,
            allow_empty=True,
        )
        raw = _sha(value.get("rawSha256"), "knowledge.rawSha256", True)
        code = _sha(value.get("codeSha256"), "knowledge.codeSha256", True)
        interface_hash = _sha(
            value.get("interfaceHash"), "knowledge.interfaceHash", True
        )
        layout_hash = _sha(value.get("layoutHash"), "knowledge.layoutHash", True)
        raw_key = (stage, entry, raw) if raw else None
        code_key = (
            (stage, entry, code, interface_hash, layout_hash)
            if code and interface_hash and value.get("interfaceComplete") is True
            else None
        )
        return raw_key, code_key

    def _reuse_knowledge_state(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        """Build linear-time lookup tables over the exact manifest knowledge revision."""

        knowledge = manifest["knowledge"]
        if not isinstance(manifest, dict):
            raise ReplayProjectError("project manifest must be a JSON object")
        active = self._active_knowledge_state(manifest)
        state: dict[str, Any] = {
            # Derived reuse-plan caches are global to the project. Activation
            # changes must invalidate them even when immutable record bytes do
            # not change (for example A -> B -> historical A).
            "knowledgeContentSha256": _canonical_sha256(
                {"knowledge": knowledge, "activeKnowledge": active}
            ),
            "indexes": {
                kind: {"raw": {}, "code": {}}
                for kind in ("workspaces", "aliases", "materials")
            },
            "workspaces": {},
            "aliases": {},
            "materials": {},
            "activation": {
                "workspaces": dict(active["workspaces"]),
                "materials": dict(active["materials"]),
            },
            "ordinals": {
                "workspaces": {},
                "materials": {},
            },
        }

        def knowledge_id(record: Mapping[str, Any], label: str) -> str:
            value = str(record.get("knowledgeId", "") or "")
            if not HEX32_RE.fullmatch(value):
                raise ReplayProjectError("%s knowledgeId is invalid" % label)
            return value

        def index_entry(
            kind: str,
            token: str,
            ordinal: tuple[int, int],
            matcher: Mapping[str, Any],
            reference: Mapping[str, Any],
        ) -> None:
            raw_key, code_key = self._reuse_index_keys(matcher)
            entry = {
                "token": token,
                "ordinal": ordinal,
                "matcher": matcher,
                "reference": dict(reference),
            }
            if raw_key is not None:
                state["indexes"][kind]["raw"].setdefault(raw_key, []).append(entry)
            if code_key is not None:
                state["indexes"][kind]["code"].setdefault(code_key, []).append(entry)

        for index, workspace in enumerate(knowledge["workspaces"]):
            if not isinstance(workspace, Mapping):
                raise ReplayProjectError("workspace knowledge record is invalid")
            key = knowledge_id(workspace, "workspace")
            if key in state["workspaces"]:
                raise ReplayProjectError("workspace knowledgeId is duplicated")
            state["workspaces"][key] = workspace
            state["ordinals"]["workspaces"][key] = (index, 0)
            index_entry(
                "workspaces",
                "workspace:%s" % key,
                (index, 0),
                workspace,
                {"knowledgeId": key},
            )

        for alias_index, alias_set in enumerate(knowledge["aliases"]):
            if not isinstance(alias_set, Mapping):
                raise ReplayProjectError("alias knowledge record is invalid")
            key = knowledge_id(alias_set, "alias")
            groups = alias_set.get("groups")
            if not isinstance(groups, list):
                raise ReplayProjectError("alias knowledge groups are invalid")
            for group_index, group in enumerate(groups):
                if not isinstance(group, Mapping):
                    raise ReplayProjectError("alias knowledge group is invalid")
                group_key = (key, group_index)
                if group_key in state["aliases"]:
                    raise ReplayProjectError("alias knowledge group is duplicated")
                state["aliases"][group_key] = (alias_set, group)
                index_entry(
                    "aliases",
                    "alias:%s:%d" % (key, group_index),
                    (alias_index, group_index),
                    group,
                    {"knowledgeId": key, "groupIndex": group_index},
                )

        for index, material in enumerate(knowledge["materials"]):
            if not isinstance(material, Mapping):
                raise ReplayProjectError("material knowledge record is invalid")
            key = knowledge_id(material, "material")
            if key in state["materials"]:
                raise ReplayProjectError("material knowledgeId is duplicated")
            state["materials"][key] = material
            state["ordinals"]["materials"][key] = (index, 0)
            index_entry(
                "materials",
                "material:%s" % key,
                (index, 0),
                material,
                {"knowledgeId": key},
            )
        return state

    @staticmethod
    def _indexed_reuse_matches(
        index: Mapping[str, Any], shader: Mapping[str, Any]
    ) -> list[tuple[dict[str, Any], str]]:
        raw_key, code_key = ReplayProjectService._reuse_index_keys(shader)
        candidates: list[Mapping[str, Any]] = []
        if raw_key is not None:
            candidates.extend(index["raw"].get(raw_key, []))
        if code_key is not None:
            candidates.extend(index["code"].get(code_key, []))
        unique: dict[str, Mapping[str, Any]] = {}
        for candidate in candidates:
            unique.setdefault(str(candidate["token"]), candidate)
        result: list[tuple[dict[str, Any], str]] = []
        for candidate in sorted(unique.values(), key=lambda item: item["ordinal"]):
            # Keep the original matcher as the final authority. The indexes are
            # only candidate routing and can never relax raw/interface/layout.
            mode = _match_mode(candidate["matcher"], shader)
            if mode:
                result.append((dict(candidate["reference"]), mode))
        return result

    def _build_reuse_mappings(
        self, capture: Mapping[str, Any], state: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        mappings: list[dict[str, Any]] = []
        for shader in capture["shaders"]:
            workspace_refs = [
                {**reference, "matchMode": mode}
                for reference, mode in self._indexed_reuse_matches(
                    state["indexes"]["workspaces"], shader
                )
            ]
            alias_refs = [
                {**reference, "matchMode": mode}
                for reference, mode in self._indexed_reuse_matches(
                    state["indexes"]["aliases"], shader
                )
            ]
            material_refs = [
                {**reference, "matchMode": mode}
                for reference, mode in self._indexed_reuse_matches(
                    state["indexes"]["materials"], shader
                )
            ]
            if workspace_refs or alias_refs or material_refs:
                mappings.append(
                    {
                        "shader": self._reuse_shader_identity(shader),
                        "workspaces": workspace_refs,
                        "aliases": alias_refs,
                        "materials": material_refs,
                    }
                )
        return mappings

    def _capture_referenced_knowledge_scope(
        self, capture: Mapping[str, Any], state: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Bind an RDC only to exact knowledge referenced by this capture."""

        mappings = self._build_reuse_mappings(capture, state)
        workspace_ids: set[str] = set()
        material_ids: set[str] = set()
        alias_keys: set[tuple[str, int]] = set()
        for mapping in mappings:
            if not isinstance(mapping, Mapping):
                raise ReplayProjectError("capture reuse mapping is invalid")
            for kind, destination, records in (
                ("workspaces", workspace_ids, state["workspaces"]),
                ("materials", material_ids, state["materials"]),
            ):
                references = mapping.get(kind)
                if not isinstance(references, list):
                    raise ReplayProjectError(
                        "capture reuse %s references are invalid" % kind
                    )
                for reference in references:
                    knowledge_id = (
                        str(reference.get("knowledgeId", "") or "")
                        if isinstance(reference, Mapping)
                        else ""
                    )
                    if (
                        not HEX32_RE.fullmatch(knowledge_id)
                        or knowledge_id not in records
                    ):
                        raise ReplayProjectError(
                            "capture reuse %s reference is absent" % kind
                        )
                    destination.add(knowledge_id)

            references = mapping.get("aliases")
            if not isinstance(references, list):
                raise ReplayProjectError(
                    "capture reuse aliases references are invalid"
                )
            for reference in references:
                knowledge_id = (
                    str(reference.get("knowledgeId", "") or "")
                    if isinstance(reference, Mapping)
                    else ""
                )
                group_index = (
                    reference.get("groupIndex")
                    if isinstance(reference, Mapping)
                    else None
                )
                key = (knowledge_id, group_index)
                if (
                    not HEX32_RE.fullmatch(knowledge_id)
                    or isinstance(group_index, bool)
                    or not isinstance(group_index, int)
                    or group_index < 0
                    or key not in state["aliases"]
                ):
                    raise ReplayProjectError(
                        "capture reuse alias reference is absent"
                    )
                alias_keys.add((knowledge_id, group_index))

        ordered_workspace_ids = tuple(sorted(workspace_ids))
        ordered_material_ids = tuple(sorted(material_ids))
        aliases: list[dict[str, Any]] = []
        for knowledge_id, group_index in sorted(alias_keys):
            alias_set, group = state["aliases"][(knowledge_id, group_index)]
            aliases.append(
                {
                    "knowledgeId": knowledge_id,
                    "groupIndex": group_index,
                    "captureId": _sha(
                        alias_set.get("captureId"), "alias.captureId"
                    ),
                    "originCatalogSha256": _sha(
                        alias_set.get("originCatalogSha256"),
                        "alias.originCatalogSha256",
                    ),
                    "artifactPath": _text(
                        alias_set.get("artifactPath"), "alias.artifactPath", 4096
                    ),
                    "artifactSha256": _sha(
                        alias_set.get("artifactSha256"), "alias.artifactSha256"
                    ),
                    "group": copy.deepcopy(dict(group)),
                }
            )

        digest_document = {
            "schema": RESTORE_KNOWLEDGE_SCOPE_SCHEMA,
            "captureId": _sha(capture.get("captureId"), "capture.captureId"),
            "catalogSha256": _sha(
                capture.get("catalogSha256"), "capture.catalogSha256"
            ),
            "matcherPolicySha256": REUSE_MATCH_POLICY_SHA256,
            "workspaces": [
                copy.deepcopy(dict(state["workspaces"][knowledge_id]))
                for knowledge_id in ordered_workspace_ids
            ],
            "aliases": aliases,
            "materials": [
                copy.deepcopy(dict(state["materials"][knowledge_id]))
                for knowledge_id in ordered_material_ids
            ],
            # Scope the activation authority to records physically reusable by
            # this capture. Unrelated project edits remain cache-stable, while
            # reactivating an existing referenced record invalidates the RDC
            # hint even though immutable history is unchanged.
            "activeKnowledge": {
                "workspaces": {
                    knowledge_id: state["activation"]["workspaces"][knowledge_id]
                    for knowledge_id in ordered_workspace_ids
                },
                "materials": {
                    knowledge_id: state["activation"]["materials"][knowledge_id]
                    for knowledge_id in ordered_material_ids
                },
            },
        }
        return {
            "knowledgeContentSha256": _canonical_sha256(digest_document),
            "workspaceIds": ordered_workspace_ids,
            "materialIds": ordered_material_ids,
        }

    def _reuse_plan_from_mappings(
        self,
        manifest: Mapping[str, Any],
        capture: Mapping[str, Any],
        state: Mapping[str, Any],
        mappings: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        shaders: dict[str, Mapping[str, Any]] = {}
        for shader in capture["shaders"]:
            identity = self._reuse_shader_identity(shader)
            key = _canonical_sha256(identity)
            if key in shaders:
                raise ReplayProjectError("capture has duplicate reusable Shader identity")
            shaders[key] = shader

        results: list[dict[str, Any]] = []
        used_shaders: set[str] = set()
        if len(mappings) > MAX_SHADER_COUNT:
            raise ReplayProjectError("reuse cache mapping count exceeds the project limit")
        for raw_mapping in mappings:
            if not isinstance(raw_mapping, Mapping) or set(raw_mapping) != {
                "shader",
                "workspaces",
                "aliases",
                "materials",
            }:
                raise ReplayProjectError("reuse cache mapping is invalid")
            identity = raw_mapping["shader"]
            if not isinstance(identity, Mapping):
                raise ReplayProjectError("reuse cache Shader identity is invalid")
            clean_identity = self._reuse_shader_identity(identity)
            if dict(identity) != clean_identity:
                raise ReplayProjectError("reuse cache Shader identity is not canonical")
            shader_key = _canonical_sha256(clean_identity)
            shader = shaders.get(shader_key)
            if shader is None or self._reuse_shader_identity(shader) != clean_identity:
                raise ReplayProjectError("reuse cache Shader is absent from the exact catalog")
            if shader_key in used_shaders:
                raise ReplayProjectError("reuse cache maps one Shader more than once")
            used_shaders.add(shader_key)
            item: dict[str, Any] = {
                "shader": shader,
                "workspaceCandidates": [],
                "aliasCandidates": [],
                "materialCandidates": [],
            }

            workspace_refs = raw_mapping["workspaces"]
            if not isinstance(workspace_refs, list) or len(workspace_refs) > MAX_KNOWLEDGE_COUNT:
                raise ReplayProjectError("reuse cache workspace references are invalid")
            seen_workspace: set[str] = set()
            workspace_candidates: list[tuple[int, dict[str, Any]]] = []
            for reference in workspace_refs:
                if not isinstance(reference, Mapping) or set(reference) != {
                    "knowledgeId",
                    "matchMode",
                }:
                    raise ReplayProjectError("reuse cache workspace reference is invalid")
                knowledge_id = str(reference["knowledgeId"])
                if knowledge_id in seen_workspace:
                    raise ReplayProjectError("reuse cache workspace reference is duplicated")
                seen_workspace.add(knowledge_id)
                workspace = state["workspaces"].get(knowledge_id)
                mode = _match_mode(workspace or {}, shader)
                if not mode or mode != reference["matchMode"]:
                    raise ReplayProjectError(
                        "reuse cache workspace reference failed exact matching"
                    )
                seed_eligible = bool(workspace.get("baselineAbiSha256"))
                candidate = {
                    "knowledgeId": knowledge_id,
                    "matchMode": mode,
                    "sourceSha256": workspace["sourceSha256"],
                    "sourceSeedEligible": seed_eligible,
                    "analysisCandidate": (
                        {
                            "path": str(
                                self._artifact(
                                    workspace["analysisPath"],
                                    "workspace analysis",
                                )
                            ),
                            "sha256": workspace["analysisSha256"],
                            "explanatoryOnly": True,
                            "physicalEvidence": False,
                            "applyAuthorization": False,
                        }
                        if workspace.get("analysisPath")
                        else None
                    ),
                }
                if not seed_eligible:
                    candidate["blockedReason"] = (
                        "origin workspace has no complete captured baseline ABI"
                    )
                activation = state["activation"]["workspaces"].get(knowledge_id)
                if not isinstance(activation, int) or activation <= 0:
                    raise ReplayProjectError(
                        "workspace knowledge activation is invalid"
                    )
                workspace_candidates.append((activation, candidate))

            if workspace_candidates:
                # Knowledge remains append-only, but automatic restore consumes
                # one deterministic active head. Prefer the newest record that
                # can actually seed an executable source; retain a newest
                # blocked record only when no valid source head exists.
                eligible = [
                    entry
                    for entry in workspace_candidates
                    if entry[1].get("sourceSeedEligible") is True
                ]
                unused_activation, active_workspace = max(
                    eligible or workspace_candidates,
                    key=lambda entry: (entry[0], entry[1]["knowledgeId"]),
                )
                active_workspace["activeCandidate"] = True
                active_workspace["supersededCandidateCount"] = (
                    len(workspace_candidates) - 1
                )
                item["workspaceCandidates"] = [active_workspace]

            alias_refs = raw_mapping["aliases"]
            if not isinstance(alias_refs, list) or len(alias_refs) > MAX_ALIASES:
                raise ReplayProjectError("reuse cache alias references are invalid")
            seen_alias: set[tuple[str, int]] = set()
            for reference in alias_refs:
                if not isinstance(reference, Mapping) or set(reference) != {
                    "knowledgeId",
                    "groupIndex",
                    "matchMode",
                }:
                    raise ReplayProjectError("reuse cache alias reference is invalid")
                knowledge_id = str(reference["knowledgeId"])
                if isinstance(reference["groupIndex"], bool):
                    raise ReplayProjectError("reuse cache alias groupIndex is invalid")
                group_index = int(reference["groupIndex"])
                alias_key = (knowledge_id, group_index)
                if group_index < 0 or alias_key in seen_alias:
                    raise ReplayProjectError("reuse cache alias reference is duplicated")
                seen_alias.add(alias_key)
                alias_pair = state["aliases"].get(alias_key)
                group = alias_pair[1] if alias_pair is not None else {}
                mode = _match_mode(group, shader)
                if not mode or mode != reference["matchMode"]:
                    raise ReplayProjectError(
                        "reuse cache alias reference failed exact matching"
                    )
                aliases = group.get("aliases")
                if not isinstance(aliases, list) or len(aliases) > MAX_ALIASES:
                    raise ReplayProjectError("alias knowledge bindings are invalid")
                item["aliasCandidates"].append(
                    {
                        "knowledgeId": knowledge_id,
                        "matchMode": mode,
                        "aliasCount": len(aliases),
                        "candidateOnly": True,
                    }
                )

            material_refs = raw_mapping["materials"]
            if not isinstance(material_refs, list) or len(material_refs) > MAX_KNOWLEDGE_COUNT:
                raise ReplayProjectError("reuse cache material references are invalid")
            seen_material: set[str] = set()
            material_candidates: dict[str, list[tuple[int, dict[str, Any]]]] = {}
            for reference in material_refs:
                if not isinstance(reference, Mapping) or set(reference) != {
                    "knowledgeId",
                    "matchMode",
                }:
                    raise ReplayProjectError("reuse cache material reference is invalid")
                knowledge_id = str(reference["knowledgeId"])
                if knowledge_id in seen_material:
                    raise ReplayProjectError("reuse cache material reference is duplicated")
                seen_material.add(knowledge_id)
                material = state["materials"].get(knowledge_id)
                mode = _match_mode(material or {}, shader)
                if not mode or mode != reference["matchMode"]:
                    raise ReplayProjectError(
                        "reuse cache material reference failed exact matching"
                    )
                parameters = material.get("parameters")
                if not isinstance(parameters, list) or len(parameters) > MAX_MATERIAL_PARAMETERS:
                    raise ReplayProjectError("material knowledge parameters are invalid")
                block_path = _text(
                    str(material.get("constantBlockPath", "")),
                    "material constantBlockPath",
                    2048,
                )
                activation = state["activation"]["materials"].get(knowledge_id)
                if not isinstance(activation, int) or activation <= 0:
                    raise ReplayProjectError("material knowledge activation is invalid")
                material_candidates.setdefault(block_path, []).append(
                    (
                        activation,
                        {
                            "knowledgeId": knowledge_id,
                            "matchMode": mode,
                            "parameterCount": len(parameters),
                            "constantBlockPath": block_path,
                            "constantBlockLayoutSha256": material[
                                "constantBlockLayoutSha256"
                            ],
                            "materialLayoutLiveRevalidationRequired": True,
                        },
                    )
                )
            for block_path in sorted(material_candidates):
                history = material_candidates[block_path]
                unused_activation, active_material = max(
                    history,
                    key=lambda entry: (entry[0], entry[1]["knowledgeId"]),
                )
                active_material["activeCandidate"] = True
                active_material["supersededCandidateCount"] = len(history) - 1
                item["materialCandidates"].append(active_material)
            results.append(item)

        return {
            "schema": "renderdoc-ultra-project-reuse-plan/v1",
            "schemaVersion": 1,
            "projectId": manifest["projectId"],
            "projectPath": str(self.root),
            "captureId": capture["captureId"],
            "catalogSha256": capture["catalogSha256"],
            "matchedShaderCount": len(results),
            "matches": results,
            "policy": {
                "automaticHashMatching": True,
                "rawExactOrCodeInterfaceLayoutExactOnly": True,
                "oldWorkspaceManifestReused": False,
                "oldCaptureAbiReportReused": False,
                "oldMaterialSessionReused": False,
                "autoApplyAuthorized": False,
                "compileRequiredAfterSourceSeed": True,
                "liveAbiRevalidationRequiredBeforeShaderApply": True,
                "liveMaterialLayoutRevalidationRequired": True,
                "appendOnlyKnowledgeHistoryPreserved": True,
                "newestValidActiveHeadOnly": True,
            },
        }

    def _reuse_cache_binding(
        self,
        manifest: Mapping[str, Any],
        capture: Mapping[str, Any],
        knowledge_content_sha256: str,
    ) -> dict[str, str]:
        material = {
            "projectId": str(manifest["projectId"]),
            "scopeSha256": _canonical_sha256(manifest["scope"]),
            "captureId": _sha(capture.get("captureId"), "capture.captureId"),
            "catalogSha256": _sha(
                capture.get("catalogSha256"), "capture.catalogSha256"
            ),
            "knowledgeContentSha256": _sha(
                knowledge_content_sha256, "knowledgeContentSha256"
            ),
            "matcherPolicySha256": REUSE_MATCH_POLICY_SHA256,
        }
        return {**material, "cacheKey": _canonical_sha256(material)}

    def _reuse_cache_path(self, cache_key: str, *, create: bool) -> Path:
        digest = _sha(cache_key, "reuse cache key")
        derived = self.root / "derived"
        directory = derived / "reuse-plans"
        for path, label in (
            (derived, "project derived directory"),
            (directory, "project reuse-cache directory"),
        ):
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_dir():
                    raise ReplayProjectError("%s is redirected" % label)
            elif create:
                path.mkdir()
        raw_result = directory / (digest + ".json")
        if raw_result.is_symlink():
            raise ReplayProjectError("reuse cache path is redirected")
        result = raw_result.resolve()
        if not is_within(self.root, result):
            raise ReplayProjectError("reuse cache path escapes the project")
        return result

    def _read_reuse_cache(
        self, binding: Mapping[str, str]
    ) -> tuple[list[dict[str, Any]] | None, Path]:
        path = self._reuse_cache_path(binding["cacheKey"], create=False)
        if not path.exists() and not path.is_symlink():
            return None, path
        if path.is_symlink() or not path.is_file():
            raise ReplayProjectError("reuse cache must be a regular non-symlink file")
        try:
            value = read_json_object(path, MAX_REUSE_CACHE_BYTES, "project reuse cache")
        except ValueError:
            return None, path
        required = {
            "schema",
            "schemaVersion",
            "projectId",
            "scopeSha256",
            "captureId",
            "catalogSha256",
            "knowledgeContentSha256",
            "matcherPolicySha256",
            "cacheKey",
            "mappings",
            "sealSha256",
        }
        if set(value) != required:
            return None, path
        if (
            value.get("schema") != REUSE_CACHE_SCHEMA
            or value.get("schemaVersion") != REUSE_CACHE_VERSION
            or any(value.get(key) != expected for key, expected in binding.items())
        ):
            return None, path
        seal = str(value.get("sealSha256", "") or "")
        if not SHA256_RE.fullmatch(seal):
            return None, path
        unsigned = {key: item for key, item in value.items() if key != "sealSha256"}
        if _canonical_sha256(unsigned) != seal:
            return None, path
        mappings = value.get("mappings")
        if not isinstance(mappings, list) or len(mappings) > MAX_SHADER_COUNT:
            return None, path
        return mappings, path

    def _write_reuse_cache(
        self, binding: Mapping[str, str], mappings: Sequence[Mapping[str, Any]]
    ) -> tuple[bool, Path]:
        path = self._reuse_cache_path(binding["cacheKey"], create=True)
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise ReplayProjectError("reuse cache destination is redirected")
        document: dict[str, Any] = {
            "schema": REUSE_CACHE_SCHEMA,
            "schemaVersion": REUSE_CACHE_VERSION,
            **dict(binding),
            "mappings": list(mappings),
        }
        document["sealSha256"] = _canonical_sha256(document)
        encoded = (
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_REUSE_CACHE_BYTES:
            return False, path
        atomic_write_bytes(path, encoded)
        verified, unused_path = self._read_reuse_cache(binding)
        if verified != list(mappings):
            raise ReplayProjectError("reuse cache failed post-write verification")
        return True, path

    def _reuse_plan(self, manifest: Mapping[str, Any], capture: Mapping[str, Any]) -> dict[str, Any]:
        state = self._reuse_knowledge_state(manifest)
        binding = self._reuse_cache_binding(
            manifest, capture, state["knowledgeContentSha256"]
        )
        mappings, cache_path = self._read_reuse_cache(binding)
        cache_hit = mappings is not None
        stored = cache_hit
        plan = None
        if mappings is not None:
            try:
                plan = self._reuse_plan_from_mappings(
                    manifest, capture, state, mappings
                )
            except (KeyError, TypeError, ValueError, ReplayProjectError):
                # Derived cache bytes never become authority. Any stale or
                # malformed correspondence is recomputed from exact knowledge.
                mappings = None
                cache_hit = False
        if mappings is None:
            mappings = self._build_reuse_mappings(capture, state)
            plan = self._reuse_plan_from_mappings(manifest, capture, state, mappings)
            stored, cache_path = self._write_reuse_cache(binding, mappings)
        if plan is None:
            raise ReplayProjectError("reuse plan was not materialized")
        plan["derivedCache"] = {
            "schema": REUSE_CACHE_SCHEMA,
            "schemaVersion": REUSE_CACHE_VERSION,
            "hit": cache_hit,
            "stored": stored,
            "path": str(cache_path),
            "cacheKey": binding["cacheKey"],
            "catalogSha256": binding["catalogSha256"],
            "knowledgeContentSha256": binding["knowledgeContentSha256"],
            "matcherPolicySha256": binding["matcherPolicySha256"],
            "mappingCount": len(mappings),
            "physicalGateBypassed": False,
        }
        return plan

    def reuse_plan(
        self, capture_id: str, catalog_sha256: str = ""
    ) -> dict[str, Any]:
        manifest = self._load()
        return self._reuse_plan(
            manifest,
            self._capture(manifest, capture_id, catalog_sha256),
        )

    def seed_workspace(self, knowledge_id: str, destination_status: Mapping[str, Any]) -> dict[str, Any]:
        token = str(knowledge_id or "").strip().lower()
        if not HEX32_RE.fullmatch(token):
            raise ReplayProjectError("knowledge_id must be 32 lowercase hexadecimal characters")
        if not isinstance(destination_status, Mapping) or destination_status.get("schema") != "renderdoc-ultra-shader-workspace-status/v1":
            raise ReplayProjectError("destination workspace status is invalid")
        source_status = destination_status.get("source")
        baseline = destination_status.get("baseline")
        capture_status = destination_status.get("capture")
        shader_status = destination_status.get("shader")
        if not all(isinstance(value, Mapping) for value in (source_status, baseline, capture_status, shader_status)):
            raise ReplayProjectError("destination workspace provenance is incomplete")
        if source_status.get("present") is not True or source_status.get("modifiedSinceDecompile") is not False:
            raise ReplayProjectError("destination source must be present and still unmodified")
        if (
            source_status.get("lane") != "executable-editable"
            or source_status.get("format") != "hlsl"
        ):
            raise ReplayProjectError(
                "project source seeding supports only executable-editable HLSL"
            )
        capture_id = _sha(
            capture_status.get("captureId"), "destination.captureId"
        )
        catalog_revision_sha256 = self._capture_status_catalog_sha256(
            capture_status
        )
        with _thread_lock(self.root), _project_lock(self.root):
            manifest = self._load()
            records = [item for item in manifest["knowledge"]["workspaces"] if item["knowledgeId"] == token]
            if len(records) != 1:
                raise ReplayProjectError("unknown project workspace knowledge ID")
            knowledge = records[0]
            capture = self._capture_for_provenance(
                manifest, capture_id, catalog_revision_sha256
            )
            shader = self._capture_shader(
                capture,
                resource_id=_positive(shader_status.get("resourceId"), "destination.resourceId"),
                stage=_text(str(destination_status.get("stage", "")), "destination.stage", 64),
                entry_point=_text(str(destination_status.get("entryPoint", "") or ""), "destination.entryPoint", 512, True),
                raw_sha256=_sha(baseline.get("rawSha256"), "destination.rawSha256"),
            )
            mode = _match_mode(knowledge, shader)
            if not mode:
                raise ReplayProjectError("project source knowledge does not match the destination shader")
            if (
                knowledge.get("targetProfile") != destination_status.get("targetProfile")
                or not knowledge.get("baselineAbiSha256")
                or knowledge.get("baselineAbiSha256") != baseline.get("abiSha256")
            ):
                raise ReplayProjectError("destination target profile/physical ABI differs from project knowledge")
            project_source = self._artifact(knowledge["sourcePath"], "workspace source")
            source_bytes = _file_bytes(project_source, MAX_SOURCE_BYTES, "workspace source")
            destination = _normal_path(str(source_status.get("path", "")))
            workspace_root = _normal_path(str(destination_status.get("workspacePath", "")))
            if destination.is_symlink() or not destination.is_file() or not is_within(workspace_root, destination):
                raise ReplayProjectError("destination source path is redirected or escapes its workspace")
            expected_destination_hash = _sha(
                source_status.get("currentSha256"),
                "destination.source.currentSha256",
            )
            with _workspace_seed_lock(workspace_root):
                if (
                    _sha256_bytes(
                        _file_bytes(destination, MAX_SOURCE_BYTES, "destination source")
                    )
                    != expected_destination_hash
                ):
                    raise ReplayProjectError(
                        "destination source changed after workspace validation"
                    )
                atomic_write_bytes(destination, source_bytes)
                if (
                    _sha256_bytes(
                        _file_bytes(destination, MAX_SOURCE_BYTES, "seeded source")
                    )
                    != knowledge["sourceSha256"]
                ):
                    raise ReplayProjectError("seeded workspace source hash mismatch")
            return {
                "schema": "renderdoc-ultra-project-workspace-seed/v1",
                "schemaVersion": 1,
                "projectId": manifest["projectId"],
                "captureId": capture["captureId"],
                "knowledgeId": token,
                "workspacePath": str(workspace_root),
                "sourcePath": str(destination),
                "sourceSha256": knowledge["sourceSha256"],
                "matchMode": mode,
                "applied": False,
                "compiled": False,
                "requiresCompile": True,
                "requiresLiveAbiRevalidation": True,
            }
