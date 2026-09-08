"""
ReplayLab MCP host — capture analysis and symbol-resolution tools over
RenderDocUltra's built-in file-IPC bridge.

Architecture:
  [MCP client] <-stdio/HTTP-> [ReplayLab sidecar] <-file IPC-> [qrenderdoc built-in bridge]
"""

from __future__ import annotations
import atexit, copy, hashlib, json, logging, os, re, subprocess, tempfile, threading, time, shutil, uuid
from collections import Counter, OrderedDict
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal, Mapping, Sequence
from .active_projects import ActiveReplayProjectService
from .asset_catalog import (
    AssetCatalogScanLimitError,
    AssetCatalogService,
    CATALOG_FILENAME as ACTIVE_ASSET_CATALOG_FILENAME,
    DEFAULT_MAX_FILES as ACTIVE_ASSET_DEFAULT_MAX_FILES,
    DEFAULT_MAX_FILE_SIZE_MB as ACTIVE_ASSET_DEFAULT_MAX_FILE_SIZE_MB,
)
from .console_events import emit_console_event, instrument_mcp_tool
from . import shader_asset_retrieval
from .task_report import (
    Artifact,
    Finding,
    REPORT_TOOL_DESCRIPTION,
    current_task_report_store,
    instrument_task_tool,
)
from .event_flow_artifacts import (
    DIAGRAM_KINDS,
    build_event_flow_graph,
    publish_event_flow_artifact,
)
from .event_flow_diagram import THEME_EVIDENCE_NEUTRAL_V1
from .ipc_client import IPCClient, INSTANCE_ROOT, bind_direct_tool_call
from .internal_tool_service import (
    shader_live_edit_manager as _shader_live_edit_manager,
    shutdown_shader_live_edits as _shutdown_shader_live_edits,
)
from .material_sessions import MaterialSessionManager
from .pass_classification import (
    CATEGORIES,
    CLASSIFICATION_SCHEMA,
    CLASSIFICATION_VERSION,
    PassKnowledgeStore,
    apply_ai_semantic_proposals,
    classify_passes,
)
from .projects import ReplayProjectError, ReplayProjectService
from .resource_export import ResourceExportManager
from .unpacked_asset_jobs import (
    UnpackedAssetJobManager,
    normalize_asset_report_spec,
)
from .shader_workspace import (
    ORGANIZATION_GATE_SCHEMA,
    ORGANIZATION_POLICY_VERSION,
    ShaderOrganizationError,
    ShaderOrganizationManager,
    ShaderWorkspaceService,
    ShaderWorkspaceWatchManager,
    ToolchainCatalog,
    classify_shader_failure,
    list_exact_shader_family_uses,
    select_unique_shader_for_use,
)
from .shader_workspace.representations import publish_current_event_representation
from .shader_workspace.catalog import (
    MAX_CATALOG_BYTES,
    load_selected_shader,
    normalize_stage,
)
from .shader_workspace.io import is_within, read_json_object
from .shader_workspace.leases import (
    WorkspaceSourceLeaseError,
    WorkspaceSourceTransactionLease,
)
from .symbols import SymbolService
from .symbols.recovery_assets import (
    discover_recovery_assets,
    evaluate_recovery_candidate,
    read_recovery_regular_file,
)
from .symbols.service import default_workspace
from .version import SERVICE_VERSION

logger = logging.getLogger("renderdoc-mcp")
logging.basicConfig(level=logging.INFO, format="%(name)s | %(levelname)s | %(message)s")

class _DeferredFastMCP:
    """Collect tool declarations without importing or constructing FastMCP.

    The same high-level implementations serve the private Internal ToolService
    and the optional external MCP adapter.  Importing this module for an
    internal Shader operation must therefore remain transport-free.  The real
    FastMCP application is created only when an external stdio/HTTP transport
    explicitly requests ``run`` or ``streamable_http_app``.
    """

    def __init__(self) -> None:
        self._registrations: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []
        self._application: Any | None = None
        self._guard = threading.Lock()

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        def register(function: Any) -> Any:
            if self._application is not None:
                raise RuntimeError("MCP tools cannot be registered after startup")
            self._registrations.append((function, args, dict(kwargs)))
            return function

        return register

    def _build(self) -> Any:
        with self._guard:
            if self._application is not None:
                return self._application
            from mcp.server.fastmcp import FastMCP

            class _ConsoleAwareFastMCP(FastMCP):
                def tool(inner_self, *args: Any, **kwargs: Any) -> Any:
                    register = super(_ConsoleAwareFastMCP, inner_self).tool(
                        *args, **kwargs
                    )

                    def register_with_console(function: Any) -> Any:
                        return register(
                            instrument_mcp_tool(instrument_task_tool(function))
                        )

                    return register_with_console

            application = _ConsoleAwareFastMCP(
                "ReplayLab",
                instructions=(
                    "Capture-bound GPU replay analysis with Shader "
                    "reverse-engineering, ABI validation, material sessions, "
                    "and project symbol matching"
                ),
            )
            # FastMCP otherwise advertises the installed SDK version. Keep the
            # external initialize/serverInfo record aligned with the bundle.
            if hasattr(application, "_mcp_server"):
                application._mcp_server.version = SERVICE_VERSION
            for function, args, kwargs in self._registrations:
                application.tool(*args, **kwargs)(function)
            self._application = application
            return application

    def streamable_http_app(self) -> Any:
        return self._build().streamable_http_app()

    def run(self, **kwargs: Any) -> Any:
        return self._build().run(**kwargs)


mcp = _DeferredFastMCP()
ipc = IPCClient(timeout=30.0)
_RESOURCE_EXPORT_MANAGER = ResourceExportManager.from_environment(os.environ)
_UNPACKED_ASSET_JOB_MANAGER = UnpackedAssetJobManager.from_environment(os.environ)

def _ok(d: Any) -> str:
    # MCP tool results are JSON embedded inside the outer stdio JSON-RPC
    # document. Keep that inner machine string ASCII-only so a legacy Windows
    # stdio intermediary cannot reinterpret Chinese paths using an ANSI code
    # page; consumers recover the exact Unicode value with json.loads().
    return json.dumps(d, ensure_ascii=True, default=str)

def _err(e: Exception) -> str:
    return json.dumps(
        {"error": str(e), "type": type(e).__name__},
        ensure_ascii=True,
        default=str,
    )


def _is_lower_hex_value(value: Any, length: int) -> bool:
    text = str(value or "")
    return (
        len(text) == length
        and all(character in "0123456789abcdef" for character in text)
    )


def _shader_err(error: Exception, phase: str) -> str:
    """Return backward-compatible error text plus a stable recovery category."""

    body = {
        "error": str(error),
        "type": type(error).__name__,
        "diagnostic": classify_shader_failure(error, phase=phase),
    }
    explanation = getattr(error, "abi_explanation", None)
    if isinstance(explanation, Mapping):
        # Where the ABI failed, why, and the HLSL change per finding — the
        # input the repair channel expects.
        body["abiExplanation"] = explanation
        body["whatToFix"] = [
            {"where": item.get("identity"), "why": item.get("message"),
             "how": item.get("hint")}
            for item in (explanation.get("findings") or [])
        ]
    return json.dumps(body, ensure_ascii=True, default=str)

def _scoped_capture_session_id() -> str:
    try:
        scope = json.loads(os.environ.get("REPLAYLAB_TOOL_SCOPE", "") or "{}")
    except (TypeError, ValueError):
        return ""
    value = str(scope.get("capture_session_id", "") or "").strip().lower()
    if len(value) != 32 or any(character not in "0123456789abcdef" for character in value):
        return ""
    return value


_SCOPED_CAPTURE_SESSION_ID = _scoped_capture_session_id()


def _call(method: str, params: dict = None, timeout: float = None) -> str:
    try:
        payload = dict(params or {})
        if _SCOPED_CAPTURE_SESSION_ID:
            # Host-owned field, absent from every model-visible MCP schema.
            payload["__replaylab_expected_capture_session_id"] = (
                _SCOPED_CAPTURE_SESSION_ID
            )
        return _ok(ipc.call(method, payload, timeout=timeout))
    except Exception as e:
        return _err(e)


def _file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _bounded_file_sha256(path: str | Path, maximum_bytes: int, label: str) -> str:
    """Hash one regular file without allowing a post-stat growth to go unbounded."""

    target = Path(path)
    before = target.stat()
    if before.st_size <= 0 or before.st_size > maximum_bytes:
        raise RuntimeError("%s size is invalid" % label)
    digest = hashlib.sha256()
    total = 0
    with target.open("rb") as stream:
        while True:
            chunk = stream.read(min(1024 * 1024, maximum_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise RuntimeError("%s exceeds its size limit" % label)
            digest.update(chunk)
    after = target.stat()
    if (
        total != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise RuntimeError("%s changed while it was hashed" % label)
    return digest.hexdigest()


SHADER_TOOLS_CONFIG_ENV = "RENDERDOC_ULTRA_SHADER_TOOLS_CONFIG"
SHADER_TOOLCHAINS_CONFIG_ENV = "RENDERDOC_ULTRA_SHADER_TOOLCHAINS_CONFIG"
SHADER_REPRESENTATIONS_ROOT_ENV = "RENDERDOC_ULTRA_SHADER_REPRESENTATIONS_ROOT"
EVENT_TEXTURE_EVIDENCE_ROOT_ENV = "RENDERDOC_ULTRA_EVENT_TEXTURE_EVIDENCE_ROOT"
SHADER_LIVE_EDIT_REGISTRY_ENV = "RENDERDOC_ULTRA_SHADER_LIVE_EDIT_REGISTRY"
ACTIVE_PROJECT_STATE_ENV = "RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE"
ACTIVE_PROJECTS_HOME_ENV = "RENDERDOC_ULTRA_PROJECTS_HOME"

# Full-frame catalogs can contain hundreds of unique shaders and several
# thousand uses.  The qrenderdoc export is deliberately serialized on the
# replay side, so the ordinary five-minute shader-operation timeout is too
# short for large captures.  Keep this below the singleton host's one-hour
# tool timeout while allowing a realistic first-open catalog to finish.
ACTIVE_CAPTURE_EXPORT_TIMEOUT_SECONDS = 1800.0
MAX_ACTIVE_CAPTURE_CATALOG_BYTES = 256 * 1024 * 1024
SHADER_CATALOG_PROGRESS_SCHEMA = "replaylab-shader-catalog-progress/v1"
ACTIVE_CAPTURE_PREPARE_PROGRESS_SCHEMA = (
    "replaylab-active-capture-prepare-progress/v1"
)
SHADER_CATALOG_EXPORT_JOB_SCHEMA = "replaylab-shader-catalog-export-job/v1"
MAX_SHADER_CATALOG_PROGRESS_BYTES = 16 * 1024
MAX_SHADER_CATALOG_PROGRESS_EVENTS = 1000000
SHADER_CATALOG_PROGRESS_POLL_SECONDS = 0.05
SHADER_CATALOG_PROGRESS_JOIN_SECONDS = 2.0
SHADER_CATALOG_EXPORT_JOB_POLL_SECONDS = 0.1
SHADER_CATALOG_EXPORT_JOB_CALL_TIMEOUT_SECONDS = 30.0
_SHADER_CATALOG_PROGRESS_INSTANCE = re.compile(
    r"^[1-9][0-9]{0,19}-[0-9a-f]{32}$"
)


def _resolve_shader_tools_config_path(environment: Any) -> Path:
    """Resolve the process-owner-controlled shader tool registry at startup.

    MCP requests deliberately cannot override this path. The process owner may
    set ``RENDERDOC_ULTRA_SHADER_TOOLS_CONFIG`` before starting the server;
    otherwise the registry lives in the user's normal configuration directory.
    """

    configured = environment.get(SHADER_TOOLS_CONFIG_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = environment.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        base = Path(local_app_data).expanduser()
    else:
        xdg_config = environment.get("XDG_CONFIG_HOME", "").strip()
        base = Path(xdg_config).expanduser() if xdg_config else Path.home() / ".config"
    # ReplayLab owns discovery, hashing, and migration of this pair. Keeping a
    # second legacy RenderDocUltra default lets the embedded AgentHost and the
    # external adapter silently load different tool catalogs.
    return (base / "ReplayLab" / "config" / "shader-tools.v1.json").resolve()


_SHADER_TOOLS_CONFIG_PATH = _resolve_shader_tools_config_path(os.environ)


def _resolve_shader_toolchains_config_path(environment: Any) -> Path:
    """Resolve the process-owner-controlled capability catalog at startup."""

    configured = environment.get(SHADER_TOOLCHAINS_CONFIG_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    tool_registry = _resolve_shader_tools_config_path(environment)
    return (tool_registry.parent / "shader-toolchains.v1.json").resolve()


_SHADER_TOOLCHAINS_CONFIG_PATH = _resolve_shader_toolchains_config_path(os.environ)


def _resolve_shader_representations_root(environment: Any) -> Path:
    """Resolve the process-owner output root; MCP requests cannot override it."""

    configured = environment.get(SHADER_REPRESENTATIONS_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = environment.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        base = Path(local_app_data).expanduser()
    else:
        xdg_cache = environment.get("XDG_CACHE_HOME", "").strip()
        base = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
    return (base / "RenderDocUltra" / "shader-representations").resolve()


_SHADER_REPRESENTATIONS_ROOT = _resolve_shader_representations_root(os.environ)
_MAX_CURRENT_SHADER_RAW_BYTES = 64 * 1024 * 1024
_SHADER_CATALOG_REBIND_GUARD = threading.RLock()
_MAX_SHADER_CATALOG_REBIND_EVENTS = 1_000_000
_MAX_SHADER_CATALOG_REBIND_STAGES = 16


def _resolve_event_texture_evidence_root(environment: Any) -> Path:
    """Resolve the process-owned evidence root; MCP calls cannot override it."""

    configured = str(
        environment.get(EVENT_TEXTURE_EVIDENCE_ROOT_ENV, "") or ""
    ).strip()
    if configured:
        root = Path(configured).expanduser()
    else:
        local_app_data = str(environment.get("LOCALAPPDATA", "") or "").strip()
        if local_app_data:
            base = Path(local_app_data).expanduser()
        else:
            xdg_cache = str(environment.get("XDG_CACHE_HOME", "") or "").strip()
            base = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
        root = base / "RenderDocUltra" / "event-texture-evidence"
    # Keep the lexical process-owned path so a configured symlink/reparse
    # boundary can be rejected when the root is first used.
    return Path(os.path.abspath(str(root)))


_EVENT_TEXTURE_EVIDENCE_ROOT = _resolve_event_texture_evidence_root(os.environ)
_EVENT_TEXTURE_EVIDENCE_SCHEMA = "replaylab-event-texture-evidence/v1"
_EVENT_TEXTURE_EVIDENCE_HOST_SCHEMA = (
    "replaylab-event-texture-evidence-host/v1"
)
_MAX_EVENT_TEXTURE_EVIDENCE_SOURCE_BYTES = 256 * 1024 * 1024
_MAX_EVENT_TEXTURE_EVIDENCE_PIXELS = 16 * 1024 * 1024
_MAX_EVENT_TEXTURE_EVIDENCE_PNG_BYTES = 128 * 1024 * 1024
_MAX_EVENT_TEXTURE_EVIDENCE_MANIFEST_BYTES = 256 * 1024
_EVENT_TEXTURE_EVIDENCE_REQUEST_DIR = re.compile(
    r"^eid-([1-9][0-9]{0,9})-([0-9a-f]{32})$"
)


def _resolve_shader_live_edit_registry_path(environment: Any) -> Path:
    """Resolve the process-owned durable auto-apply authorisation registry."""

    configured = str(environment.get(SHADER_LIVE_EDIT_REGISTRY_ENV, "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = str(environment.get("LOCALAPPDATA", "") or "").strip()
    if local_app_data:
        base = Path(local_app_data).expanduser() / "ReplayLab"
    else:
        xdg_data = str(environment.get("XDG_DATA_HOME", "") or "").strip()
        data_root = (
            Path(xdg_data).expanduser()
            if xdg_data
            else Path.home() / ".local" / "share"
        )
        base = data_root / "ReplayLab"
    return (base / "config" / "shader-live-edit-authorizations.v1.json").resolve()


_SHADER_LIVE_EDIT_REGISTRY_PATH = _resolve_shader_live_edit_registry_path(os.environ)


def _resolve_active_project_paths(environment: Any) -> tuple[Path, Path]:
    """Resolve process-owner fixed active-project paths once at startup.

    MCP requests deliberately have no state-path or projects-home arguments.
    A distributor/process owner may override either path in the child process
    environment; otherwise Windows uses the documented LocalAppData layout.
    """

    local_app_data = str(environment.get("LOCALAPPDATA", "") or "").strip()
    if local_app_data:
        replaylab_root = Path(local_app_data).expanduser() / "ReplayLab"
    else:
        xdg_data = str(environment.get("XDG_DATA_HOME", "") or "").strip()
        data_root = (
            Path(xdg_data).expanduser()
            if xdg_data
            else Path.home() / ".local" / "share"
        )
        replaylab_root = data_root / "ReplayLab"
    configured_state = str(environment.get(ACTIVE_PROJECT_STATE_ENV, "") or "").strip()
    configured_home = str(environment.get(ACTIVE_PROJECTS_HOME_ENV, "") or "").strip()
    state_path = (
        Path(configured_state).expanduser()
        if configured_state
        else replaylab_root / "config" / "active-project.json"
    )
    projects_home = (
        Path(configured_home).expanduser()
        if configured_home
        else replaylab_root / "projects"
    )
    return state_path.resolve(), projects_home.resolve()


_ACTIVE_PROJECT_STATE_PATH, _ACTIVE_PROJECTS_HOME = _resolve_active_project_paths(
    os.environ
)
_ACTIVE_PROJECT_SERVICE = None
_ACTIVE_PROJECT_SERVICE_GUARD = threading.Lock()
_ACTIVE_SYMBOL_SCAN_CACHE_MAX_ENTRIES = 8
_ACTIVE_SYMBOL_SCAN_CACHE_TTL_SECONDS = 300.0
_ACTIVE_SYMBOL_SCAN_CACHE: OrderedDict[
    tuple[str, str, str, str], dict[str, Any]
] = OrderedDict()
_ACTIVE_SYMBOL_SCAN_GUARD = threading.RLock()
_ACTIVE_SYMBOL_SCAN_STRIPES = tuple(threading.Lock() for _ in range(8))
_ACTIVE_CAPTURE_CATALOG_BINDINGS_MAX_ENTRIES = 8
_ACTIVE_CAPTURE_CATALOG_BINDINGS: OrderedDict[str, dict[str, Any]] = OrderedDict()
_ACTIVE_CAPTURE_CATALOG_BINDINGS_GUARD = threading.RLock()

_RECOVERY_INSPECT_MAX_FILES = 100_000
_RECOVERY_INSPECT_MAX_FILE_SIZE_MB = 64
_RECOVERY_INSPECT_MAX_TOTAL_READ_MB = 512
_RECOVERY_INSPECT_MAX_CANDIDATES = 1_024
_RECOVERY_INSPECT_DEFAULT_CANDIDATES = 256
_RECOVERY_LEGACY_SCAN_CANDIDATES = 128
_RECOVERY_ONE_SHOT_CANDIDATES = 256
_RECOVERY_ACTIVE_CACHE_CANDIDATES = 256
_RECOVERY_MAX_CAPTURE_FACTS_JSON_BYTES = 64 * 1024
_RECOVERY_REPORT_MAX_BOUND_SHADERS = 16
_RECOVERY_REPORT_TOP_CANDIDATES_PER_SHADER = 8
_RECOVERY_REPORT_MAX_SOURCE_PREVIEWS = 4
_RECOVERY_REPORT_MAX_PREVIEW_UTF8_BYTES = 32 * 1024
_RECOVERY_REPORT_MAX_TOTAL_PREVIEW_UTF8_BYTES = 128 * 1024
_RECOVERY_REPORT_MAX_JSON_UTF8_BYTES = 1024 * 1024


def _active_project_service() -> ActiveReplayProjectService:
    """Return the singleton bound to the process-owner configured roots."""

    global _ACTIVE_PROJECT_SERVICE
    with _ACTIVE_PROJECT_SERVICE_GUARD:
        if _ACTIVE_PROJECT_SERVICE is None:
            _ACTIVE_PROJECT_SERVICE = ActiveReplayProjectService(
                _ACTIVE_PROJECT_STATE_PATH, _ACTIVE_PROJECTS_HOME
            )
        return _ACTIVE_PROJECT_SERVICE


def _recovery_server_limit(value: Any, name: str, hard_cap: int) -> int:
    """Validate an MCP-controlled recovery limit without lossy coercion."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("%s must be a positive integer" % name)
    if value <= 0:
        raise ValueError("%s must be a positive integer" % name)
    if value > hard_cap:
        raise ValueError("%s exceeds the fixed hard cap of %d" % (name, hard_cap))
    return value


def _prune_active_symbol_scan_cache_locked(now: float) -> None:
    expired = [
        key
        for key, value in _ACTIVE_SYMBOL_SCAN_CACHE.items()
        if now - float(value.get("cachedAtMonotonic", 0.0) or 0.0)
        >= _ACTIVE_SYMBOL_SCAN_CACHE_TTL_SECONDS
    ]
    for key in expired:
        _ACTIVE_SYMBOL_SCAN_CACHE.pop(key, None)
    while len(_ACTIVE_SYMBOL_SCAN_CACHE) > _ACTIVE_SYMBOL_SCAN_CACHE_MAX_ENTRIES:
        _ACTIVE_SYMBOL_SCAN_CACHE.popitem(last=False)


def _active_symbol_scan_cache_get(
    cache_key: tuple[str, str, str, str], database_path: str
) -> dict[str, Any] | None:
    now = time.monotonic()
    with _ACTIVE_SYMBOL_SCAN_GUARD:
        _prune_active_symbol_scan_cache_locked(now)
        value = _ACTIVE_SYMBOL_SCAN_CACHE.get(cache_key)
        if value is None:
            return None
        if value.get("databasePath") != database_path:
            raise RuntimeError("cached asset scan database identity changed")
        _ACTIVE_SYMBOL_SCAN_CACHE.move_to_end(cache_key)
        return value


def _active_symbol_scan_cache_put_if_absent(
    cache_key: tuple[str, str, str, str], value: dict[str, Any]
) -> dict[str, Any]:
    """Publish a fully computed entry while holding the cache lock only briefly."""

    now = time.monotonic()
    entry = dict(value)
    entry["cachedAtMonotonic"] = now
    with _ACTIVE_SYMBOL_SCAN_GUARD:
        _prune_active_symbol_scan_cache_locked(now)
        existing = _ACTIVE_SYMBOL_SCAN_CACHE.get(cache_key)
        if existing is not None:
            if existing.get("databasePath") != entry.get("databasePath"):
                raise RuntimeError("cached asset scan database identity changed")
            _ACTIVE_SYMBOL_SCAN_CACHE.move_to_end(cache_key)
            return existing
        _ACTIVE_SYMBOL_SCAN_CACHE[cache_key] = entry
        _ACTIVE_SYMBOL_SCAN_CACHE.move_to_end(cache_key)
        _prune_active_symbol_scan_cache_locked(now)
        return entry


def _export_current_shader_raw_bytes(
    event_id: int, stage: str, reverse_result: dict
) -> tuple[bytes | None, dict | None]:
    """Export and identity-check raw bytes without using the DX-only catalog loader."""

    root = _SHADER_REPRESENTATIONS_ROOT.resolve()
    if root.exists() and (not root.is_dir() or root.is_symlink()):
        raise RuntimeError("shader representation root is redirected")
    root.mkdir(parents=True, exist_ok=True)
    export_dir = root / ("_catalog-%s" % uuid.uuid4().hex)
    try:
        exported = ipc.call(
            "export_shader_catalog",
            {
                "output_dir": str(export_dir),
                "event_ids": [event_id],
                "stages": [stage],
            },
            timeout=300.0,
        )
        if not isinstance(exported, dict) or not exported.get("catalogPath"):
            raise RuntimeError("RenderDoc did not return an exported Shader catalog")
        catalog_path = Path(str(exported["catalogPath"])).resolve()
        if not is_within(export_dir.resolve(), catalog_path) or not catalog_path.is_file():
            raise RuntimeError("exported Shader catalog escaped its server-owned directory")
        if catalog_path.stat().st_size <= 0 or catalog_path.stat().st_size > 64 * 1024 * 1024:
            raise RuntimeError("exported Shader catalog size is invalid")
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        records = catalog.get("shaders") if isinstance(catalog, dict) else None
        if not isinstance(records, list) or len(records) > 4096:
            raise RuntimeError("exported Shader catalog records are invalid")
        expected_resource = int(reverse_result.get("shaderId"))
        expected_stage = normalize_stage(stage)
        expected_entry = str(reverse_result.get("entryPoint", "") or "")
        expected_hash = str(reverse_result.get("rawSha256", "") or "").lower()
        matches = []
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                resource = int(record.get("shaderResourceId", record.get("resourceId")))
            except (TypeError, ValueError, OverflowError):
                continue
            if (
                resource == expected_resource
                and normalize_stage(record.get("stage")) == expected_stage
                and str(record.get("entryPoint", record.get("entry", "")) or "")
                == expected_entry
                and str(record.get("rawSha256", "") or "").lower() == expected_hash
            ):
                matches.append(record)
        if len(matches) != 1:
            raise RuntimeError("exported catalog did not select one exact current Shader")
        record = matches[0]
        raw_path = (catalog_path.parent / str(record.get("rawPath", ""))).resolve()
        if not is_within(catalog_path.parent, raw_path) or not raw_path.is_file():
            raise RuntimeError("exported Shader raw path is missing or redirected")
        size = raw_path.stat().st_size
        if size > _MAX_CURRENT_SHADER_RAW_BYTES:
            return None, {
                "code": "raw-bytes-hash-only-size-limit",
                "message": (
                    "Captured Shader is %d bytes; the representation RPC retains its hash "
                    "but does not copy files larger than %d bytes."
                    % (size, _MAX_CURRENT_SHADER_RAW_BYTES)
                ),
            }
        if size <= 0 or int(record.get("rawSize", -1)) != size:
            raise RuntimeError("exported Shader raw size is invalid")
        raw = raw_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise RuntimeError("exported Shader bytes contradict their physical hash")
        return raw, None
    finally:
        # This exact UUID directory was created solely for one bounded export.
        # Never delete any sibling or user-supplied path.
        if (
            export_dir.parent == root
            and export_dir.name.startswith("_catalog-")
            and export_dir.exists()
        ):
            shutil.rmtree(export_dir)


def _shader_tools_config_path() -> Path:
    return _SHADER_TOOLS_CONFIG_PATH


_SHADER_WORKSPACE_SERVICE = None
_SHADER_WORKSPACE_SERVICE_FINGERPRINT = None
_SHADER_WORKSPACE_SERVICE_GUARD = threading.Lock()


def _shader_workspace_service_fingerprint() -> tuple[str, str, str, str]:
    """Bind the process cache to the exact two small owner-controlled configs."""

    tools = _shader_tools_config_path()
    routes = _SHADER_TOOLCHAINS_CONFIG_PATH
    return (
        str(tools),
        _bounded_file_sha256(tools, 4 * 1024 * 1024, "Shader tool registry"),
        str(routes),
        _bounded_file_sha256(routes, 4 * 1024 * 1024, "Shader route catalog"),
    )


def _workspace_asset_layout_provider(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    """Retrieve an unpacked-asset CBuffer layout hint for a workspace's shader.

    Evidence order for the fixed repair is capture reflection → unpacked
    assets → decompiled code.  This retriever answers the middle tier from the
    active project's managed symbol database (identity, then layout-hash
    candidates).  It is read-only, never scans, and returns None on any
    missing prerequisite so decompilation never waits on assets.
    """

    try:
        from .shader_workspace.asset_layout import ASSET_LAYOUT_SCHEMA, hint_from_relation_view, normalize_layout_hint

        capture = manifest.get("capture") if isinstance(manifest, Mapping) else None
        shader = manifest.get("shader") if isinstance(manifest, Mapping) else None
        baseline = manifest.get("baseline") if isinstance(manifest, Mapping) else None
        if not all(isinstance(item, Mapping) for item in (capture, shader, baseline)):
            return None
        capture_id = str(capture.get("captureId") or "")
        resource_id = int(shader.get("resourceId") or 0)
        stage = normalize_stage(manifest.get("stage") or shader.get("stage") or "")
        entry_point = str(manifest.get("entryPoint") or shader.get("entryPoint") or "")
        raw_sha256 = str(baseline.get("rawSha256") or "").lower()
        if not capture_id or resource_id <= 0 or not stage or not raw_sha256:
            return None
        # Tier 1: remembered material-family member rules (AI-confirmed once).
        memory = _shader_asset_memory(raw_sha256, stage, entry_point)
        if memory is not None and memory["familyKeys"]:
            rules = memory["store"].member_rules(memory["gameKey"], memory["familyKeys"])
            interface = baseline.get("compiledInterface") if isinstance(baseline.get("compiledInterface"), Mapping) else {}
            sizes = {
                "%d:%d" % (int(block.get("space", 0) or 0), int(block.get("bind", 0) or 0)): int(block.get("byteSize") or 0)
                for block in (interface.get("constantBlocks") or []) if isinstance(block, Mapping)
            }
            blocks = {}
            for key, fields in rules.items():
                if sizes.get(key):
                    blocks[key] = {"byteSize": sizes[key], "fields": [
                        {k: v for k, v in field.items() if k in ("name", "byteOffset", "byteSize", "baseType", "columns", "elements")}
                        for field in fields]}
            if blocks:
                try:
                    return normalize_layout_hint({
                        "schema": ASSET_LAYOUT_SCHEMA, "schemaVersion": 1,
                        "relation": {"retriever": "member-rules", "assetPath": memory["familyKeys"][0],
                                     "score": max(float(f.get("confidence") or 0) for fields in rules.values() for f in fields),
                                     "physicalIdentityProgramVerified": False},
                        "blocks": blocks,
                    })
                except Exception:
                    pass
        service = _active_project_service()
        project_status = service.status()
        if (
            project_status.get("assetRootConfigured") is not True
            or project_status.get("assetRootAvailable") is not True
        ):
            return None
        paths = project_status.get("paths")
        if not isinstance(paths, Mapping) or not paths.get("sharedDictionaries"):
            return None
        project_root = Path(str(project_status.get("projectRoot", "") or "")).resolve()
        shared_root = Path(str(paths["sharedDictionaries"])).resolve()
        database_entry = shared_root / "assets" / "symbols-v3.sqlite3"
        if (
            not project_root.is_dir()
            or not is_within(project_root, shared_root)
            or database_entry.is_symlink()
            or not database_entry.is_file()
        ):
            return None
        symbols = SymbolService(database_entry.resolve(), read_only=True)
        if symbols.store.get_capture_shader(capture_id, resource_id, stage, entry_point) is None:
            return None
        evidence = symbols.evidence(capture_id, resource_id, stage, entry_point, top_k=10)
        captured = evidence.get("captureShader") if isinstance(evidence, Mapping) else None
        if (
            not isinstance(captured, Mapping)
            or str(captured.get("rawSha256", "") or "").lower() != raw_sha256
        ):
            return None
        bound_identity = {
            "evidenceClass": "observed",
            "captureId": capture_id,
            "stage": stage,
            "shaderResourceId": resource_id,
            "entryPoint": entry_point,
            "rawSha256": raw_sha256,
        }
        return hint_from_relation_view(_shader_asset_relation_view(bound_identity, evidence))
    except Exception:
        return None


def _shader_route_memory():
    """The active project's Shader route memory, or None without a project.

    Stored at ``shared/dictionaries/shaders/route-memory.json``.  Every
    validated apply records the decompile/compile lanes that worked for that
    shader shape; every rejected decompile records why.  New workspaces of the
    same shape then start on the remembered lane instead of re-discovering it.
    """

    try:
        from .shader_workspace.route_memory import ShaderRouteMemory, memory_path_for_project_dictionary

        directory = _active_project_service().shared_dictionary_directory("shaders")
        return ShaderRouteMemory(memory_path_for_project_dictionary(directory))
    except Exception:
        # No active project (or an unavailable one) simply means no memory.
        return None


def _route_memory_status_key(status: Mapping[str, Any]) -> str:
    from .shader_workspace.route_memory import feature_signature, route_key

    capture = status.get("capture") if isinstance(status.get("capture"), Mapping) else {}
    plan = status.get("routePlan") if isinstance(status.get("routePlan"), Mapping) else {}
    baseline = status.get("baseline") if isinstance(status.get("baseline"), Mapping) else {}
    return route_key(
        target_api=capture.get("api"),
        capture_encoding=plan.get("inputFormat") or baseline.get("kind"),
        stage=status.get("stage"),
        target_profile=status.get("targetProfile"),
        editable_format=plan.get("editableFormat"),
        features=feature_signature(status.get("routeFeatures")),
    )


def _remember_route_success(status: Mapping[str, Any], *, details: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """Record a validated apply's lanes in the project route memory (never raises)."""

    try:
        from .shader_workspace.route_memory import plan_route_ids

        memory = _shader_route_memory()
        if memory is None:
            return None
        plan = status.get("routePlan")
        decompile_ids = plan_route_ids(plan, "decompile")
        compile_ids = plan_route_ids(plan, "compile")
        if not compile_ids:
            return None
        entry = memory.record_success(
            _route_memory_status_key(status),
            decompile_route_ids=decompile_ids,
            compile_route_ids=compile_ids,
            workspace_path=str(status.get("workspacePath", "") or ""),
            details=details,
        )
        return {"recorded": True, "key": entry.get("key"), "preferred": entry.get("preferred")}
    except Exception as exc:  # noqa: BLE001
        return {"recorded": False, "reason": str(exc)[:200]}


def _remember_route_decompile_failure(workspace_path: str, error: BaseException) -> None:
    """Record a rejected decompile lane for this shader shape (never raises)."""

    try:
        from .shader_workspace.route_memory import plan_route_ids

        memory = _shader_route_memory()
        if memory is None:
            return
        status = _shader_workspace_service().status(workspace_path)
        decompile_ids = plan_route_ids(status.get("routePlan"), "decompile")
        if not decompile_ids:
            return
        diagnostic = classify_shader_failure(error, phase="decompile")
        memory.record_decompile_failure(
            _route_memory_status_key(status),
            decompile_route_ids=decompile_ids,
            reason=str(error),
            category=str(diagnostic.get("category", "") if isinstance(diagnostic, Mapping) else ""),
        )
    except Exception:
        return


def _shader_workspace_service() -> ShaderWorkspaceService:
    """Reuse one immutable registry until either owner-controlled config changes.

    Constructing ``ShaderWorkspaceService`` hashes every executable and fixed
    companion DLL in the registry. Repeating that work for each status/request
    check made one save perform the same multi-megabyte integrity scan several
    times. The small config files are still hashed on every lookup, and a
    changed config creates a fresh fully verified service. Actual tool launches
    retain their existing just-before-exec integrity check.
    """

    global _SHADER_WORKSPACE_SERVICE, _SHADER_WORKSPACE_SERVICE_FINGERPRINT
    fingerprint = _shader_workspace_service_fingerprint()
    with _SHADER_WORKSPACE_SERVICE_GUARD:
        if (
            _SHADER_WORKSPACE_SERVICE is None
            or _SHADER_WORKSPACE_SERVICE_FINGERPRINT != fingerprint
        ):
            _SHADER_WORKSPACE_SERVICE = ShaderWorkspaceService(
                _shader_tools_config_path(), _SHADER_TOOLCHAINS_CONFIG_PATH,
                asset_layout_provider=_workspace_asset_layout_provider,
                route_memory_provider=_shader_route_memory,
            )
            _SHADER_WORKSPACE_SERVICE_FINGERPRINT = fingerprint
        return _SHADER_WORKSPACE_SERVICE


def _shader_toolchain_catalog() -> ToolchainCatalog:
    """Load a fresh digest-bound catalog from process-owner fixed paths."""

    return ToolchainCatalog(
        _shader_tools_config_path(), _SHADER_TOOLCHAINS_CONFIG_PATH
    )


_SHADER_WORKSPACE_WATCH_MANAGER = None
_SHADER_WORKSPACE_WATCH_MANAGER_GUARD = threading.Lock()


def _shader_workspace_watch_manager() -> ShaderWorkspaceWatchManager:
    """Return the one bounded, process-owned watcher manager."""

    global _SHADER_WORKSPACE_WATCH_MANAGER
    with _SHADER_WORKSPACE_WATCH_MANAGER_GUARD:
        if _SHADER_WORKSPACE_WATCH_MANAGER is None:
            _SHADER_WORKSPACE_WATCH_MANAGER = ShaderWorkspaceWatchManager(
                _shader_workspace_service,
                owner_instance_id=os.environ.get("REPLAYLAB_HOST_INSTANCE_ID"),
            )
        return _SHADER_WORKSPACE_WATCH_MANAGER


def _shutdown_shader_workspace_watches() -> None:
    with _SHADER_WORKSPACE_WATCH_MANAGER_GUARD:
        manager = _SHADER_WORKSPACE_WATCH_MANAGER
    if manager is not None:
        manager.shutdown()


atexit.register(_shutdown_shader_workspace_watches)


_SHADER_ORGANIZATION_MANAGER = None
_SHADER_ORGANIZATION_MANAGER_GUARD = threading.Lock()


def _shader_organization_validate_candidate(
    workspace_path: str, revision: int, candidate_raw_sha256: str
) -> dict[str, Any]:
    """Return only fixed fresh ABI evidence to the organization transaction."""

    unused_service, unused_status, unused_request, validated = (
        _fresh_shader_candidate_validation(
            workspace_path,
            expected_revision=revision,
            expected_candidate_raw_sha256=candidate_raw_sha256,
        )
    )
    return validated


def _shader_organization_anchor_event(status: Mapping[str, Any]) -> int:
    uses = list_exact_shader_family_uses(status["capture"]["catalogPath"], status["shader"]["shaderKey"])
    if not uses:
        raise RuntimeError("organization workspace has no exact captured shader use")
    return min(int(value) for value in uses)


def _shader_organization_cbuffer_alias_identity(workspace_path: str) -> dict[str, Any]:
    status = _shader_workspace_service().status(workspace_path)
    _ensure_workspace_capture_catalog_identity(workspace_path, status=status)
    live = ipc.call("get_capture_status", {}, timeout=30.0)
    session = _strict_lower_hex(live.get("captureSessionId"), 32, "capture session")
    event_id = _shader_organization_anchor_event(status)
    result = ipc.call("get_shader_cbuffer_alias_identity", {
        "expected_capture_id": status["capture"]["captureId"],
        "expected_capture_session_id": session, "event_id": event_id,
        "stage": status["stage"], "entry_point": status["entryPoint"],
        "expected_original_resource_id": status["shader"]["resourceId"],
        "expected_original_sha256": status["baseline"]["rawSha256"]}, timeout=300.0)
    if (not isinstance(result, dict) or result.get("captureSessionId") != session
            or result.get("captureId") != status["capture"]["captureId"]
            or result.get("eventId") != event_id
            or result.get("stage") != status["stage"]
            or result.get("entryPoint") != status["entryPoint"]
            or str(result.get("shaderResourceId")) != str(status["shader"]["resourceId"])
            or result.get("rawSha256") != status["baseline"]["rawSha256"]):
        raise RuntimeError("CBuffer alias identity returned a different capture binding")
    return result


def _shader_organization_compare_candidates(
    workspace_path: str, baseline: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    """Observe one bounded A/B replay; the model cannot choose its tolerance."""
    service = _shader_workspace_service()
    status = service.status(workspace_path)
    root = Path(workspace_path).resolve()
    latest = status.get("latestCompiled") or {}
    if any(latest.get(key) != candidate.get(key) for key in ("revision", "rawSha256", "sourceSha256")):
        raise RuntimeError("organization candidate changed before numeric observation")
    _ensure_workspace_capture_catalog_identity(workspace_path, status=status)
    live = ipc.call("get_capture_status", {}, timeout=30.0)
    session = _strict_lower_hex(live.get("captureSessionId"), 32, "capture session")
    request = service.candidate_reflection_request(workspace_path)
    params = dict(request["params"])
    for name, compiled in (("baseline", baseline), ("candidate", candidate)):
        path = Path(str(compiled.get("candidateBlobPath", "")))
        if path.is_symlink() or not path.is_file() or not is_within(root, path.resolve()):
            raise RuntimeError("organization comparison candidate path is invalid")
        if _bounded_file_sha256(path, 512 * 1024 * 1024, "comparison candidate") != compiled.get("rawSha256"):
            raise RuntimeError("organization comparison candidate bytes changed")
        params[name] = {"path": str(path.resolve()), "rawSha256": compiled["rawSha256"]}
    params.update({"expected_capture_id": status["capture"]["captureId"],
                   "expected_capture_session_id": session,
                   "event_id": _shader_organization_anchor_event(status),
                   "expected_original_resource_id": status["shader"]["resourceId"],
                   "expected_original_sha256": status["baseline"]["rawSha256"]})
    result = ipc.call("compare_shader_organization_candidates", params, timeout=300.0)
    if (not isinstance(result, dict) or result.get("captureId") != params["expected_capture_id"]
            or result.get("captureSessionId") != session or result.get("eventId") != params["event_id"]):
        raise RuntimeError("numeric shader observation returned a different capture binding")
    result.update({"schema": "renderdoc-ai-shader-organization-observation/v1",
                   "scope": "captured-event-output",
                   "baselineSourceSha256": baseline["sourceSha256"],
                   "candidateSourceSha256": candidate["sourceSha256"],
                   "baselineCandidateRawSha256": baseline["rawSha256"],
                   "candidateRawSha256": candidate["rawSha256"]})
    return result


def _shader_organization_manager() -> ShaderOrganizationManager:
    global _SHADER_ORGANIZATION_MANAGER
    with _SHADER_ORGANIZATION_MANAGER_GUARD:
        if _SHADER_ORGANIZATION_MANAGER is None:
            _SHADER_ORGANIZATION_MANAGER = ShaderOrganizationManager(
                _shader_workspace_service,
                _shader_organization_validate_candidate,
                _shader_organization_compare_candidates,
                _shader_organization_cbuffer_alias_identity,
            )
        return _SHADER_ORGANIZATION_MANAGER


def _shader_live_edit_apply_candidate(
    workspace_path: str, revision: int, candidate_raw_sha256: str
) -> dict[str, Any]:
    """Run the existing fresh ABI/shared-resource transaction for one save.

    The live-edit worker owns only the exact revision/hash observed from the
    process-owned workspace watcher.  It cannot choose ResourceIds, events,
    compiler arguments or ABI evidence.
    """

    # Once this Host has completed one ordinary replay-reflection/apply
    # transaction for the workspace, later saves can reuse that immutable ABI
    # report as the live-edit authorization lease.  The Bridge still builds the
    # new target and compares its complete physical interface with the captured
    # original immediately before mutation; only the redundant *preliminary*
    # BuildTargetShader/FreeTargetResource pass is skipped.
    leased = _apply_shader_workspace_shared_resource_with_live_abi_lease(
        workspace_path,
        expected_revision=revision,
        expected_candidate_raw_sha256=candidate_raw_sha256,
    )
    if leased is not None:
        return dict(leased)

    raw = apply_shader_workspace_shared_resource(
        workspace_path,
        expected_revision=revision,
        expected_candidate_raw_sha256=candidate_raw_sha256,
    )
    try:
        result = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "shared-resource apply returned invalid JSON to live edit"
        ) from exc
    if not isinstance(result, dict):
        raise RuntimeError("shared-resource apply returned a non-object to live edit")
    if result.get("error"):
        raise RuntimeError(
            "shared-resource apply failed: %s" % str(result.get("error"))
        )
    return result


def _shader_live_edit_workspace_identity(
    workspace_path: str, *, status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the immutable capture/shader identity used for durable consent."""

    status = _shader_workspace_service().status(workspace_path) if status is None else status
    capture = status.get("capture")
    shader = status.get("shader")
    if not isinstance(capture, dict) or not isinstance(shader, dict):
        raise RuntimeError("shader workspace identity is incomplete")
    return {
        "workspaceId": status.get("workspaceId"),
        "captureId": capture.get("captureId"),
        "capturePath": capture.get("capturePath"),
        "shaderKey": shader.get("shaderKey"),
        "shaderRawSha256": shader.get("rawSha256"),
        "resourceId": shader.get("resourceId"),
        "stage": status.get("stage"),
        "entryPoint": status.get("entryPoint"),
    }


def _shader_live_edit_workspace_migration_evidence(
    source_workspace_path: str,
    workspace_path: str,
    expected_source_sha256: str,
) -> dict[str, Any]:
    """Return fixed-program evidence for one direct authorization migration."""

    return _shader_workspace_service().live_edit_authorization_migration_evidence(
        source_workspace_path,
        workspace_path,
        expected_source_sha256=expected_source_sha256,
    )


# Registered after the low-level watcher so LIFO shutdown stops/join live-edit
# workers before their shared watcher manager is destroyed.
atexit.register(_shutdown_shader_live_edits)


_MATERIAL_SESSION_MANAGER = None
_MATERIAL_SESSION_MANAGER_GUARD = threading.Lock()


def _material_session_manager() -> MaterialSessionManager:
    """Return the bounded process-owned material session manager."""

    global _MATERIAL_SESSION_MANAGER
    with _MATERIAL_SESSION_MANAGER_GUARD:
        if _MATERIAL_SESSION_MANAGER is None:
            _MATERIAL_SESSION_MANAGER = MaterialSessionManager(
                lambda method, params, timeout=60.0: ipc.call(
                    method, params, timeout=timeout
                )
            )
        return _MATERIAL_SESSION_MANAGER


def _shutdown_material_sessions() -> None:
    with _MATERIAL_SESSION_MANAGER_GUARD:
        manager = _MATERIAL_SESSION_MANAGER
    if manager is not None:
        manager.shutdown()


atexit.register(_shutdown_material_sessions)


_CHECKED_SHADER_SHARED_APPLY_COMBINATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("D3D11", ("DXBC",)),
    ("D3D12", ("DXBC", "DXIL")),
    ("Vulkan", ("SPIRV",)),
    ("OpenGL", ("OPENGL-SPIRV",)),
)


def _shader_candidate_scope_status(result: dict, candidate_present: bool) -> str:
    """Report physical live-apply scope without guessing missing capture facts."""

    if not candidate_present:
        return "not-applicable"
    latest = result.get("latestCompiled")
    candidate_kind = result.get("kind")
    if candidate_kind is None and isinstance(latest, dict):
        candidate_kind = latest.get("kind")
    capture = result.get("capture")
    capture_api = capture.get("api") if isinstance(capture, dict) else None
    kind = str(candidate_kind or "").strip().casefold()
    api = str(capture_api or "").strip().casefold().replace(" ", "")
    if api.startswith("graphicsapi."):
        api = api[len("graphicsapi.") :]
    api = {
        "direct3d11": "d3d11",
        "direct3d12": "d3d12",
        "vk": "vulkan",
        "gl": "opengl",
    }.get(api, api)
    checked = {
        (checked_api.casefold(), checked_kind.casefold())
        for checked_api, checked_kinds in _CHECKED_SHADER_SHARED_APPLY_COMBINATIONS
        for checked_kind in checked_kinds
    }
    if (api, kind) in checked:
        return "supported-after-fresh-gates"
    if kind and api:
        return "unsupported"
    return "not-evaluated"


def _shader_workspace_response(operation: str, result: dict) -> str:
    latest = result.get("latestCompiled")
    candidate_present = operation == "compile" or isinstance(latest, dict)
    requires_validation = bool(
        result.get("requiresReplayAbiValidation")
        or (latest or {}).get("requiresReplayAbiValidation")
    ) if candidate_present else False
    replay_validated = bool(
        result.get("replayAbiValidated")
        or (latest or {}).get("replayAbiValidated")
    ) if candidate_present else False
    candidate_scope_status = _shader_candidate_scope_status(
        result, candidate_present
    )
    return _ok(
        {
            "schema": "renderdoc-ultra-shader-workspace-mcp/v1",
            "operation": operation,
            "result": result,
            "candidateState": {
                "present": candidate_present,
                "requiresReplayAbiValidation": requires_validation,
                "replayAbiValidated": replay_validated,
                "applied": False,
            },
            "renderDocReplacement": {
                "attempted": False,
                "applied": False,
                # This is a host-capability flag retained for older clients; it
                # does not mean that the current candidate is inside its scope.
                "supportedByTheseTools": True,
                "supportedByThisOperation": False,
                "supportedCaptureScope": {
                    "apis": [
                        api for api, _kinds in _CHECKED_SHADER_SHARED_APPLY_COMBINATIONS
                    ],
                    "containerKinds": list(
                        dict.fromkeys(
                            kind
                            for _api, kinds in _CHECKED_SHADER_SHARED_APPLY_COMBINATIONS
                            for kind in kinds
                        )
                    ),
                    "combinations": [
                        {"api": api, "containerKinds": list(kinds)}
                        for api, kinds in _CHECKED_SHADER_SHARED_APPLY_COMBINATIONS
                    ],
                },
                "candidateScopeStatus": candidate_scope_status,
                "applyTool": "apply_shader_workspace_shared_resource",
                "reason": (
                    "Sharing-preserving live apply supports checked D3D11/DXBC, "
                    "D3D12/DXBC-or-DXIL, Vulkan/native-SPIR-V, and OpenGL/native-"
                    "OpenGL-SPIR-V transactions. D3D12 uses captured root signatures; "
                    "Vulkan and OpenGL rebuild every supported affected pipeline/program "
                    "before committing derived mappings. Event-scoped shaders remain "
                    "D3D11-only, while event-scoped CBuffers support safe local D3D11 and "
                    "D3D12 CBV bindings. "
                    "Every candidate still requires fresh replay ABI and live-event gates."
                ),
            },
        }
    )


def _shader_workspace_watch_response(operation: str, result: dict) -> str:
    return _ok(
        {
            "schema": "renderdoc-ultra-shader-watch-mcp/v1",
            "operation": operation,
            "result": result,
            "renderDocReplacement": {
                "attempted": False,
                "applied": False,
                "supportedByTheseTools": True,
                "supportedByThisOperation": False,
                "applyTool": "apply_shader_workspace_shared_resource",
                "reason": (
                    "The save watcher only creates structurally validated local "
                    "candidates; explicit fresh replay ABI/live-event validation is required."
                ),
            },
        }
    )


def _shader_live_edit_response(operation: str, result: dict) -> str:
    return _ok(
        {
            "schema": "replaylab-shader-live-edit-mcp/v1",
            "schemaVersion": 1,
            "operation": operation,
            "result": result,
            "appliedByThisCall": False,
            "backgroundAutoApplyAuthorized": operation != "stop",
            "applyScope": "captured-shader-resource",
            "rollback": result.get("rollback") if isinstance(result, dict) else None,
            "policy": {
                "newSaveRequired": True,
                "freshCompileRequired": True,
                "initialFullReplayAbiValidationRequired": True,
                "freshReplayAbiValidationRequired": True,
                "freshPreliminaryReplayReflectionRequired": False,
                "changedBytesUseInlinePhysicalAbiGate": True,
                "identicalCandidateSkipsAbiAndMutation": True,
                "freshLiveCaptureValidationRequired": True,
                "sharedResourceIdentityPreserved": True,
                "distinctResourceIdsNeverMerged": True,
                "originalShaderPreserved": True,
                "failedSavePreservesLastGoodReplacement": True,
                "aiCanBypassApplyGate": False,
            },
        }
    )


_SHADER_OVERRIDE_RECORDS: dict[str, dict] = {}
_SHARED_SHADER_REPLACEMENT_RECORDS: dict[str, dict] = {}
_SHADER_OVERRIDE_CAPTURE_KEY = ""
_SHADER_OVERRIDE_GUARD = threading.RLock()


def _capture_path_key(value: str) -> str:
    path = str(value or "").strip()
    if not path:
        return ""
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _sync_shader_override_capture(capture_session_id: str) -> None:
    """Discard process-local apply handles when the bridge capture changes."""
    global _SHADER_OVERRIDE_CAPTURE_KEY
    capture_value = str(capture_session_id or "").strip().lower()
    with _SHADER_OVERRIDE_GUARD:
        if (
            _SHADER_OVERRIDE_CAPTURE_KEY
            and capture_value != _SHADER_OVERRIDE_CAPTURE_KEY
        ):
            _SHADER_OVERRIDE_RECORDS.clear()
            _SHARED_SHADER_REPLACEMENT_RECORDS.clear()
        _SHADER_OVERRIDE_CAPTURE_KEY = capture_value


def _workspace_catalog_binding_projection(status: dict[str, Any]) -> dict[str, Any]:
    """Return only the immutable workspace facts needed for live rebinding."""

    capture = status.get("capture")
    shader = status.get("shader")
    if not isinstance(capture, dict) or not isinstance(shader, dict):
        raise RuntimeError("Shader workspace capture/Shader identity is incomplete")
    capture_id = str(capture.get("captureId", "") or "").strip().lower()
    if len(capture_id) != 64 or any(
        character not in "0123456789abcdef" for character in capture_id
    ):
        raise RuntimeError("Shader workspace captureId is invalid")
    capture_path = str(capture.get("capturePath", "") or "").strip()
    api = str(capture.get("api", "") or "").strip()
    catalog_path = str(capture.get("catalogPath", "") or "").strip()
    catalog_sha256 = str(capture.get("catalogSha256", "") or "").strip()
    shader_key = str(shader.get("shaderKey", "") or "").strip()
    if not capture_path or not api or not catalog_path or not shader_key:
        raise RuntimeError("Shader workspace live catalog binding is incomplete")
    if (
        len(catalog_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in catalog_sha256
        )
    ):
        raise RuntimeError(
            "Shader workspace catalogSha256 is required for automatic live rebinding; "
            "migrate or rebuild this legacy workspace explicitly"
        )
    return {
        "workspaceId": str(status.get("workspaceId", "") or ""),
        "captureId": capture_id,
        "capturePath": capture_path,
        "api": api,
        "catalogPath": catalog_path,
        "catalogSha256": catalog_sha256,
        "shaderKey": shader_key,
        "stage": str(status.get("stage", "") or ""),
        "entryPoint": str(status.get("entryPoint", "") or ""),
        "resourceId": shader.get("resourceId"),
        "rawSha256": str(shader.get("rawSha256", "") or ""),
    }


def _catalog_rebind_identity_evidence(
    catalog: dict[str, Any], label: str
) -> dict[str, Any]:
    """Validate the exact fixed Bridge selection, Shader set and use tuples."""

    if (
        catalog.get("schema") != "renderdoc-ultra-capture-symbols/v1"
        or catalog.get("schemaVersion") != 1
    ):
        raise RuntimeError("%s has an unsupported schema" % label)
    selection = catalog.get("selection")
    if not isinstance(selection, dict) or set(selection) != {
        "mode",
        "eventIds",
        "stages",
    }:
        raise RuntimeError("%s selection is unavailable or invalid" % label)
    mode = str(selection.get("mode", "") or "")
    if mode not in {"explicit", "all_draw_dispatch"}:
        raise RuntimeError("%s selection mode is unsupported" % label)
    raw_event_ids = selection.get("eventIds")
    raw_stages = selection.get("stages")
    if (
        not isinstance(raw_event_ids, list)
        or not raw_event_ids
        or len(raw_event_ids) > _MAX_SHADER_CATALOG_REBIND_EVENTS
        or not isinstance(raw_stages, list)
        or not raw_stages
        or len(raw_stages) > _MAX_SHADER_CATALOG_REBIND_STAGES
    ):
        raise RuntimeError("%s selection exceeds fixed limits" % label)
    event_ids: list[int] = []
    for index, raw_event_id in enumerate(raw_event_ids):
        if isinstance(raw_event_id, bool):
            raise RuntimeError("%s selection eventIds[%d] is invalid" % (label, index))
        try:
            event_id = int(raw_event_id)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError(
                "%s selection eventIds[%d] is invalid" % (label, index)
            ) from exc
        if event_id <= 0 or event_id in event_ids:
            raise RuntimeError("%s selection eventIds[%d] is invalid" % (label, index))
        event_ids.append(event_id)
    stages: list[str] = []
    for index, raw_stage in enumerate(raw_stages):
        stage = normalize_stage(raw_stage)
        if not stage or raw_stage != stage or stage in stages:
            raise RuntimeError("%s selection stages[%d] is invalid" % (label, index))
        stages.append(stage)

    def shader_identity(value: Any, field: str) -> tuple[str, str]:
        if not isinstance(value, dict):
            raise RuntimeError("%s %s is invalid" % (label, field))
        resource_a = value.get("shaderResourceId")
        resource_b = value.get("resourceId")
        if resource_a is not None and resource_b is not None:
            try:
                if int(resource_a) != int(resource_b):
                    raise RuntimeError("%s %s ResourceId conflicts" % (label, field))
            except (TypeError, ValueError, OverflowError) as exc:
                raise RuntimeError("%s %s ResourceId is invalid" % (label, field)) from exc
        raw_resource = resource_a if resource_a is not None else resource_b
        if isinstance(raw_resource, bool):
            raise RuntimeError("%s %s ResourceId is invalid" % (label, field))
        try:
            resource_id = int(raw_resource)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("%s %s ResourceId is invalid" % (label, field)) from exc
        stage = normalize_stage(value.get("stage"))
        if resource_id <= 0 or not stage:
            raise RuntimeError("%s %s Shader identity is invalid" % (label, field))
        entry_point = str(
            value.get("entryPoint", value.get("entry", "")) or ""
        )
        raw_sha256 = str(value.get("rawSha256", "") or "").strip().lower()
        if raw_sha256 and (
            len(raw_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in raw_sha256
            )
        ):
            raise RuntimeError("%s %s rawSha256 is invalid" % (label, field))
        shader_key = "%d:%s:%s:%s" % (
            resource_id,
            stage,
            entry_point,
            raw_sha256 or "no-raw-bytes",
        )
        if value.get("shaderKey") != shader_key:
            raise RuntimeError("%s %s shaderKey is invalid" % (label, field))
        return shader_key, stage

    records = catalog.get("shaders")
    uses = catalog.get("uses")
    if not isinstance(records, list) or not records or not isinstance(uses, list):
        raise RuntimeError("%s has no Shader/use identity evidence" % label)
    declared: set[str] = set()
    for index, record in enumerate(records):
        shader_key, unused_stage = shader_identity(
            record, "shaders[%d]" % index
        )
        if shader_key in declared:
            raise RuntimeError("%s shaders[%d] identity is invalid" % (label, index))
        declared.add(shader_key)

    selection_event_ids = set(event_ids)
    selection_stages = set(stages)
    use_tuples: set[tuple[int, str]] = set()
    used_keys: set[str] = set()
    for index, use in enumerate(uses):
        shader_key, stage = shader_identity(use, "uses[%d]" % index)
        if shader_key not in declared:
            raise RuntimeError("%s uses[%d] identity is invalid" % (label, index))
        raw_event_id = use.get("eventId")
        if isinstance(raw_event_id, bool):
            raise RuntimeError("%s uses[%d].eventId is invalid" % (label, index))
        try:
            event_id = int(raw_event_id)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError(
                "%s uses[%d].eventId is invalid" % (label, index)
            ) from exc
        item = (event_id, shader_key)
        if (
            event_id not in selection_event_ids
            or stage not in selection_stages
            or item in use_tuples
        ):
            raise RuntimeError("%s uses[%d] is outside its exact selection" % (label, index))
        use_tuples.add(item)
        used_keys.add(shader_key)
    if used_keys != declared:
        raise RuntimeError("%s does not bind every Shader to a selected event" % label)
    return {
        "selection": {
            "mode": mode,
            "eventIds": event_ids,
            "stages": stages,
        },
        "shaderKeys": sorted(declared),
        "useTuples": [
            [event_id, shader_key]
            for event_id, shader_key in sorted(use_tuples)
        ],
    }


def _workspace_catalog_rebind_scope(
    status: dict[str, Any],
) -> dict[str, Any]:
    """Recover only the original immutable Bridge export selection."""

    binding = _workspace_catalog_binding_projection(status)
    raw_path = Path(binding["catalogPath"]).expanduser()
    if raw_path.is_symlink():
        raise RuntimeError("Shader workspace capture catalog is redirected")
    catalog_path = raw_path.resolve(strict=True)
    if not catalog_path.is_file():
        raise RuntimeError("Shader workspace capture catalog is unavailable")
    before_sha256 = _bounded_file_sha256(
        catalog_path, MAX_CATALOG_BYTES, "Shader workspace capture catalog"
    )
    if before_sha256 != binding["catalogSha256"]:
        raise RuntimeError("Shader workspace capture catalog hash changed")
    try:
        catalog = read_json_object(
            catalog_path, MAX_CATALOG_BYTES, "Shader workspace capture catalog"
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    selected = load_selected_shader(catalog_path, binding["shaderKey"])
    after_sha256 = _bounded_file_sha256(
        catalog_path, MAX_CATALOG_BYTES, "Shader workspace capture catalog"
    )
    if before_sha256 != after_sha256:
        raise RuntimeError("Shader workspace capture catalog changed during rebinding")
    if any(
        (
            selected.capture_id != binding["captureId"],
            _capture_path_key(selected.capture_path)
            != _capture_path_key(binding["capturePath"]),
            _active_api_key(selected.api) != _active_api_key(binding["api"]),
            selected.key.catalog_key != binding["shaderKey"],
            selected.key.stage != normalize_stage(binding["stage"]),
            selected.key.entry_point != binding["entryPoint"],
            selected.key.resource_id != binding["resourceId"],
            selected.key.raw_sha256 != binding["rawSha256"],
        )
    ):
        raise RuntimeError("Shader workspace catalog identity changed during rebinding")
    evidence = _catalog_rebind_identity_evidence(
        catalog, "Shader workspace capture catalog"
    )
    selected_events = [
        event_id
        for event_id, shader_key in evidence["useTuples"]
        if shader_key == selected.key.catalog_key
    ]
    if not selected_events:
        raise RuntimeError("Shader workspace catalog omitted its selected Shader use")
    selection = evidence["selection"]
    return {
        "eventIds": list(selection["eventIds"]),
        "stages": list(selection["stages"]),
        "requestEventIds": (
            list(selection["eventIds"])
            if selection["mode"] == "explicit"
            else None
        ),
        "selectedEventId": min(selected_events),
        "selected": selected,
        "identityEvidence": evidence,
    }


def _workspace_live_capture_status(
    binding: dict[str, Any],
) -> tuple[dict[str, Any], set[str], set[tuple[str, str]]]:
    """Validate live transport identity without accepting a caller claim."""

    live = ipc.call("get_capture_status", {}, timeout=30.0)
    if not isinstance(live, dict) or live.get("loaded") is not True:
        raise RuntimeError("No capture is loaded in RenderDoc")
    capture_session_id = str(live.get("captureSessionId", "") or "").strip()
    if len(capture_session_id) != 32 or any(
        character not in "0123456789abcdef" for character in capture_session_id
    ):
        raise RuntimeError("The active RenderDoc capture session identity is invalid")
    if (
        _capture_path_key(str(live.get("capturePath", "") or ""))
        != _capture_path_key(binding["capturePath"])
        or _active_api_key(live.get("api")) != _active_api_key(binding["api"])
    ):
        raise RuntimeError("The active RenderDoc capture does not match this workspace")
    raw_known = live.get("knownCatalogCaptureIds")
    if not isinstance(raw_known, list) or len(raw_known) > 4096:
        raise RuntimeError("RenderDoc returned invalid known catalog identities")
    known: set[str] = set()
    for value in raw_known:
        token = str(value or "").strip()
        if len(token) != 64 or any(
            character not in "0123456789abcdef" for character in token
        ):
            raise RuntimeError("RenderDoc returned an invalid catalog identity")
        if token in known:
            raise RuntimeError("RenderDoc returned duplicate catalog identities")
        known.add(token)
    raw_pairs = live.get("knownCatalogIdentityPairs")
    if not isinstance(raw_pairs, list) or len(raw_pairs) > 4096:
        raise RuntimeError("RenderDoc returned invalid exact catalog identities")
    known_pairs: set[tuple[str, str]] = set()
    for index, value in enumerate(raw_pairs):
        if not isinstance(value, dict) or set(value) != {
            "captureId",
            "identityEvidenceSha256",
        }:
            raise RuntimeError(
                "RenderDoc returned invalid exact catalog identity[%d]" % index
            )
        capture_id = str(value.get("captureId", "") or "")
        evidence_sha256 = str(
            value.get("identityEvidenceSha256", "") or ""
        )
        if (
            capture_id != capture_id.strip()
            or evidence_sha256 != evidence_sha256.strip()
            or len(capture_id) != 64
            or len(evidence_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in capture_id + evidence_sha256
            )
            or capture_id not in known
            or (capture_id, evidence_sha256) in known_pairs
        ):
            raise RuntimeError(
                "RenderDoc returned invalid exact catalog identity[%d]" % index
            )
        known_pairs.add((capture_id, evidence_sha256))
    return live, known, known_pairs


def _ensure_workspace_capture_catalog_identity(
    workspace_path: str,
    *,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rebind one trusted workspace catalog to a restarted live Bridge.

    This is a non-Shader-mutating preflight.  It either observes that the exact
    exact captureId/evidence pair is already known or asks the Bridge to
    re-export the immutable catalog's event/stage scope, validates the resulting
    catalog/Shader bytes, and then proves the Bridge cached that exact pair in the unchanged
    capture session.  It never creates/migrates a workspace, compiles source,
    applies a candidate, or retries a prior revision.
    """

    service = _shader_workspace_service()
    before = status if status is not None else service.status(workspace_path)
    binding = _workspace_catalog_binding_projection(before)
    scope = _workspace_catalog_rebind_scope(before)
    expected_identity_evidence_sha256 = _canonical_json_sha256(
        scope["identityEvidence"],
        "Shader workspace catalog rebind identity evidence",
    )
    expected_pair = (binding["captureId"], expected_identity_evidence_sha256)
    live, unused_known, known_pairs = _workspace_live_capture_status(binding)
    if expected_pair in known_pairs:
        return {
            "schema": "replaylab-live-capture-catalog-binding/v1",
            "schemaVersion": 1,
            "workspaceId": binding["workspaceId"],
            "captureId": binding["captureId"],
            "captureSessionId": live["captureSessionId"],
            "state": "already-known",
            "rebound": False,
            "workspaceCreated": False,
            "candidateCompiled": False,
            "candidateApplied": False,
            "previousCandidateRetried": False,
        }

    with _SHADER_CATALOG_REBIND_GUARD:
        current = service.status(workspace_path)
        current_binding = _workspace_catalog_binding_projection(current)
        if current_binding != binding:
            raise RuntimeError(
                "Shader workspace physical identity changed before catalog rebinding"
            )
        scope = _workspace_catalog_rebind_scope(current)
        current_identity_evidence_sha256 = _canonical_json_sha256(
            scope["identityEvidence"],
            "Shader workspace catalog rebind identity evidence",
        )
        if current_identity_evidence_sha256 != expected_identity_evidence_sha256:
            raise RuntimeError(
                "Shader workspace exact catalog identity changed before rebinding"
            )
        live, unused_known, known_pairs = _workspace_live_capture_status(binding)
        if expected_pair in known_pairs:
            return {
                "schema": "replaylab-live-capture-catalog-binding/v1",
                "schemaVersion": 1,
                "workspaceId": binding["workspaceId"],
                "captureId": binding["captureId"],
                "captureSessionId": live["captureSessionId"],
                "state": "already-known",
                "rebound": False,
                "workspaceCreated": False,
                "candidateCompiled": False,
                "candidateApplied": False,
                "previousCandidateRetried": False,
            }
        raw_root = Path(_SHADER_REPRESENTATIONS_ROOT).expanduser()
        if raw_root.exists() and (raw_root.is_symlink() or not raw_root.is_dir()):
            raise RuntimeError("Shader representation root is redirected")
        raw_root.mkdir(parents=True, exist_ok=True)
        root = raw_root.resolve()
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("Shader representation root is redirected")
        export_dir = root / ("_catalog-rebind-%s" % uuid.uuid4().hex)
        if export_dir.exists() or export_dir.is_symlink():
            raise RuntimeError("Shader catalog rebind directory already exists")
        try:
            export_params = {
                "output_dir": str(export_dir),
                "stages": scope["stages"],
                "expected_capture_id": binding["captureId"],
                "expected_identity_evidence_sha256": (
                    expected_identity_evidence_sha256
                ),
            }
            if scope["requestEventIds"] is not None:
                export_params["event_ids"] = scope["requestEventIds"]
            exported = ipc.call(
                "export_shader_catalog",
                export_params,
                timeout=ACTIVE_CAPTURE_EXPORT_TIMEOUT_SECONDS,
            )
            if not isinstance(exported, dict):
                raise RuntimeError(
                    "RenderDoc returned an invalid catalog rebind export"
                )
            exported_capture_id = str(
                exported.get("captureId", "") or ""
            ).strip()
            if exported_capture_id != binding["captureId"]:
                raise RuntimeError(
                    "Live catalog re-export did not reproduce the workspace captureId"
                )
            if (
                exported.get("identityEvidenceSha256")
                != expected_identity_evidence_sha256
                or exported.get("expectedIdentityValidatedBeforeCache") is not True
            ):
                raise RuntimeError(
                    "RenderDoc did not prove catalog identity before Bridge cache publication"
                )
            catalog_text = str(
                exported.get("catalogPath", exported.get("path", "")) or ""
            ).strip()
            if not catalog_text:
                raise RuntimeError("RenderDoc catalog rebind export omitted its path")
            raw_exported_path = Path(catalog_text).expanduser()
            if raw_exported_path.is_symlink():
                raise RuntimeError("RenderDoc catalog rebind export is redirected")
            exported_path = raw_exported_path.resolve(strict=True)
            if (
                not exported_path.is_file()
                or not is_within(export_dir.resolve(), exported_path)
            ):
                raise RuntimeError(
                    "RenderDoc catalog rebind export escaped its server-owned directory"
                )
            exported_before_sha256 = _bounded_file_sha256(
                exported_path,
                MAX_CATALOG_BYTES,
                "Live catalog rebind export",
            )
            try:
                exported_catalog = read_json_object(
                    exported_path,
                    MAX_CATALOG_BYTES,
                    "Live catalog rebind export",
                )
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
            exported_evidence = _catalog_rebind_identity_evidence(
                exported_catalog, "Live catalog rebind export"
            )
            if exported_evidence != scope["identityEvidence"]:
                raise RuntimeError(
                    "Live catalog re-export changed its exact selection, Shader set, "
                    "or event/use bindings"
                )
            rebound_selected = select_unique_shader_for_use(
                exported_path,
                scope["selectedEventId"],
                binding["stage"],
            )
            exported_after_sha256 = _bounded_file_sha256(
                exported_path,
                MAX_CATALOG_BYTES,
                "Live catalog rebind export",
            )
            if exported_before_sha256 != exported_after_sha256:
                raise RuntimeError(
                    "Live catalog rebind export changed during validation"
                )
            selected = scope["selected"]
            if any(
                (
                    rebound_selected.capture_id != binding["captureId"],
                    rebound_selected.key != selected.key,
                    rebound_selected.blob != selected.blob,
                    _capture_path_key(rebound_selected.capture_path)
                    != _capture_path_key(binding["capturePath"]),
                    _active_api_key(rebound_selected.api)
                    != _active_api_key(binding["api"]),
                )
            ):
                raise RuntimeError(
                    "Live catalog re-export changed the workspace Shader identity"
                )
            live_after, unused_known_after, known_pairs_after = (
                _workspace_live_capture_status(binding)
            )
            if (
                live_after["captureSessionId"] != live["captureSessionId"]
                or expected_pair not in known_pairs_after
            ):
                raise RuntimeError(
                    "RenderDoc capture/catalog identity changed during rebinding"
                )
            after_binding = _workspace_catalog_binding_projection(
                service.status(workspace_path)
            )
            if after_binding != binding:
                raise RuntimeError(
                    "Shader workspace physical identity changed during catalog rebinding"
                )
            return {
                "schema": "replaylab-live-capture-catalog-binding/v1",
                "schemaVersion": 1,
                "workspaceId": binding["workspaceId"],
                "captureId": binding["captureId"],
                "captureSessionId": live_after["captureSessionId"],
                "state": "rebound-from-workspace-catalog",
                "rebound": True,
                "eventCount": len(scope["eventIds"]),
                "stageCount": len(scope["stages"]),
                "selectedEventId": scope["selectedEventId"],
                "stage": binding["stage"],
                "workspaceCreated": False,
                "candidateCompiled": False,
                "candidateApplied": False,
                "previousCandidateRetried": False,
            }
        finally:
            # Remove only the exact UUID directory created for this rebind.
            # A redirected replacement is deliberately left for manual audit.
            if (
                export_dir.parent == root
                and export_dir.name.startswith("_catalog-rebind-")
                and export_dir.exists()
                and not export_dir.is_symlink()
                and export_dir.is_dir()
            ):
                shutil.rmtree(export_dir)


def _normalise_expected_shader_candidate(
    expected_revision: int | None,
    expected_candidate_raw_sha256: str,
) -> dict | None:
    """Validate an optional exact save-watcher candidate binding."""

    has_revision = expected_revision is not None
    raw_sha256 = str(expected_candidate_raw_sha256 or "")
    has_raw_sha256 = bool(raw_sha256)
    if has_revision != has_raw_sha256:
        raise ValueError(
            "expected_revision and expected_candidate_raw_sha256 must be "
            "supplied together"
        )
    if not has_revision:
        return None
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        raise ValueError("expected_revision must be a positive integer")
    if len(raw_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in raw_sha256
    ):
        raise ValueError(
            "expected_candidate_raw_sha256 must be a lowercase SHA-256"
        )
    return {
        "revision": expected_revision,
        "candidateRawSha256": raw_sha256,
    }


def _require_shader_candidate_binding(
    status: dict, expected: dict | None
) -> str:
    """Require an executable lane and, when supplied, one exact revision/hash."""

    representation = status.get("representation")
    if (
        not isinstance(representation, dict)
        or representation.get("lane") != "executable-editable"
        or representation.get("compileEligible") is not True
    ):
        raise RuntimeError(
            "Shader validation/apply requires the executable-editable representation lane"
        )
    source = status.get("source")
    latest = status.get("latestCompiled")
    current_source_sha256 = (
        str(source.get("currentSha256", "") or "")
        if isinstance(source, dict)
        else ""
    )
    candidate_source_sha256 = (
        str(latest.get("sourceSha256", "") or "")
        if isinstance(latest, dict)
        else ""
    )
    if any(
        len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in (current_source_sha256, candidate_source_sha256)
    ):
        raise RuntimeError(
            "Shader workspace has no valid current-source/candidate binding"
        )
    if current_source_sha256 != candidate_source_sha256:
        raise RuntimeError(
            "Shader source changed after the candidate compile; wait for a fresh "
            "compile before validation/apply"
        )
    if expected is None:
        return current_source_sha256
    if (
        not isinstance(latest, dict)
        or status.get("revision") != expected["revision"]
        or latest.get("revision") != expected["revision"]
        or latest.get("rawSha256") != expected["candidateRawSha256"]
    ):
        raise RuntimeError(
            "Shader workspace no longer contains the expected saved candidate"
        )
    return current_source_sha256


def _fresh_shader_candidate_validation(
    workspace_path: str,
    expected_revision: int | None = None,
    expected_candidate_raw_sha256: str = "",
) -> tuple[ShaderWorkspaceService, dict, dict, dict]:
    """Reflect and strictly ABI-validate the exact current candidate.

    The workspace core validates the manifest, immutable catalog provenance,
    baseline bytes, candidate bytes, compiler binding, and complete physical
    ABI.  The extra capture-session ID is bridge-only evidence that the
    reflection ran against the exact loaded-capture lifecycle which produced
    this validation.
    """
    expected = _normalise_expected_shader_candidate(
        expected_revision, expected_candidate_raw_sha256
    )
    service = _shader_workspace_service()
    before = service.status(workspace_path)
    source_sha256 = _require_shader_candidate_binding(before, expected)
    # Bridge catalog identities are capture-session-local and disappear when
    # qrenderdoc or its built-in integration restarts. Re-establish the exact immutable
    # workspace identity before the first reflection attempt. This is a
    # preflight, not an exception retry, so a failed saved revision remains
    # handled and still requires a newer save under live-edit policy.
    _ensure_workspace_capture_catalog_identity(
        workspace_path, status=before
    )
    request = service.candidate_reflection_request(workspace_path)
    params = dict(request["params"])
    params["expected_capture_id"] = before["capture"]["captureId"]
    reflected = ipc.call(
        "reflect_shader_candidate", params, timeout=300.0
    )
    if not isinstance(reflected, dict):
        raise RuntimeError("RenderDoc bridge returned invalid shader reflection evidence")
    if reflected.get("captureId") != before["capture"]["captureId"]:
        raise RuntimeError("Candidate reflection was not bound to the workspace capture")
    capture_session_id = str(reflected.get("captureSessionId", "") or "")
    if len(capture_session_id) != 32 or any(
        character not in "0123456789abcdef"
        for character in capture_session_id
    ):
        raise RuntimeError("Candidate reflection has no valid capture session binding")
    validated = dict(
        service.validate_candidate_reflection(workspace_path, reflected)
    )
    # This lifecycle identity deliberately stays outside the persistent ABI
    # report: it proves when the fresh reflection happened, while the immutable
    # workspace continues to be portable across qrenderdoc restarts.
    validated["captureSessionId"] = capture_session_id

    # A compiler watcher or another process may publish a newer revision while
    # reflection is running.  Re-resolve everything and require the request to
    # remain byte-for-byte bound to the same revision and candidate.
    after = service.status(workspace_path)
    source_sha256_after = _require_shader_candidate_binding(after, expected)
    request_after = service.candidate_reflection_request(workspace_path)
    if request_after != request:
        raise RuntimeError(
            "Shader workspace changed during replay ABI validation; validate again"
        )
    binding = request["binding"]
    latest = after.get("latestCompiled") or {}
    if any(
        (
            after.get("workspaceId") != binding.get("workspaceId"),
            after.get("capture", {}).get("captureId") != binding.get("captureId"),
            after.get("revision") != binding.get("revision"),
            latest.get("rawSha256") != binding.get("candidateRawSha256"),
            latest.get("sourceSha256") != source_sha256,
            source_sha256_after != source_sha256,
            validated.get("requestId") != request.get("requestId"),
            bool(
                expected
                and (
                    binding.get("revision") != expected["revision"]
                    or binding.get("candidateRawSha256")
                    != expected["candidateRawSha256"]
                )
            ),
        )
    ):
        raise RuntimeError("Shader ABI validation binding changed")
    _sync_shader_override_capture(capture_session_id)
    return service, after, request, validated


def _validated_shader_apply_response(
    response: Any,
    *,
    status: dict,
    request: dict,
    event_id: int,
    report_sha256: str,
    original_resource_id: int | None = None,
    original_raw_sha256: str | None = None,
) -> dict:
    if not isinstance(response, dict):
        raise RuntimeError("RenderDoc bridge returned an invalid shader override result")
    binding = request["binding"]
    expected = {
        "schema": "renderdoc-ultra-event-shader-override/v1",
        "schemaVersion": 1,
        "captureId": binding["captureId"],
        "workspaceId": binding["workspaceId"],
        "revision": binding["revision"],
        "eventId": event_id,
        "stage": binding["stage"],
        "entryPoint": binding["entryPoint"],
        "originalResourceId": (
            status["shader"]["resourceId"]
            if original_resource_id is None else original_resource_id
        ),
        "originalRawSha256": (
            status["baseline"]["rawSha256"]
            if original_raw_sha256 is None else original_raw_sha256
        ),
        "candidateRawSha256": binding["candidateRawSha256"],
        "physicalAbiSha256": binding["baselineAbiSha256"],
        "abiRequestId": request["requestId"],
        "abiReportSha256": report_sha256,
        "eventScoped": True,
        "configured": True,
        "appliedToCurrentEvent": True,
        "originalShaderPreserved": True,
    }
    for key, value in expected.items():
        if response.get(key) != value:
            raise RuntimeError(
                "RenderDoc shader override response mismatch at %s" % key
            )
    override_id = str(response.get("overrideId", "") or "")
    if len(override_id) != 32 or any(
        character not in "0123456789abcdef" for character in override_id
    ):
        raise RuntimeError("RenderDoc bridge returned an invalid shader override ID")
    capture_session_id = str(response.get("captureSessionId", "") or "")
    if len(capture_session_id) != 32 or any(
        character not in "0123456789abcdef"
        for character in capture_session_id
    ):
        raise RuntimeError("RenderDoc bridge returned an invalid capture session ID")
    with _SHADER_OVERRIDE_GUARD:
        if (
            _SHADER_OVERRIDE_CAPTURE_KEY
            and capture_session_id != _SHADER_OVERRIDE_CAPTURE_KEY
        ):
            raise RuntimeError("RenderDoc capture session changed during shader apply")
    target_id = response.get("targetResourceId")
    if not isinstance(target_id, int) or isinstance(target_id, bool) or target_id <= 0:
        raise RuntimeError("RenderDoc bridge returned an invalid target shader ID")
    family_id = str(response.get("shaderFamilyId", "") or "")
    if len(family_id) != 64 or any(
        character not in "0123456789abcdef" for character in family_id
    ):
        raise RuntimeError("RenderDoc bridge returned an invalid Shader family ID")
    if not isinstance(response.get("idempotent"), bool):
        raise RuntimeError("RenderDoc bridge returned invalid idempotency evidence")
    reference_count = response.get("targetReferenceCount")
    if (
        not isinstance(reference_count, int)
        or isinstance(reference_count, bool)
        or reference_count <= 0
    ):
        raise RuntimeError("RenderDoc bridge returned an invalid target reference count")
    return response


def _shader_family_event_bindings(
    *, status: dict, request: dict, event_ids: list[int]
) -> list[dict]:
    """Resolve catalog events that share the workspace's physical Shader family."""
    if not isinstance(event_ids, list) or not event_ids:
        raise ValueError("event_ids must be a non-empty array")
    if len(event_ids) > 256:
        raise ValueError("event_ids exceeds the 256 Shader-family event limit")
    values: list[int] = []
    for raw_event in event_ids:
        if isinstance(raw_event, bool):
            raise ValueError("event_ids must contain positive integers")
        event_id = int(raw_event)
        if event_id <= 0:
            raise ValueError("event_ids must contain positive integers")
        if event_id in values:
            raise ValueError("event_ids must not contain duplicates")
        values.append(event_id)

    baseline_hash = str(status["baseline"]["rawSha256"])
    baseline_abi_hash = str(request["binding"]["baselineAbiSha256"])
    bindings = []
    for event_id in values:
        selected = select_unique_shader_for_use(
            status["capture"]["catalogPath"], event_id, status["stage"]
        )
        if any(
            (
                selected.key.stage != status["stage"],
                selected.key.entry_point != status["entryPoint"],
                selected.key.resource_id != status["shader"]["resourceId"],
                selected.key.raw_sha256 != baseline_hash,
                selected.abi is None,
                selected.abi is not None
                and selected.abi.sha256 != baseline_abi_hash,
            )
        ):
            raise RuntimeError(
                "event %d does not bind the workspace's physical Shader family"
                % event_id
            )
        bindings.append(
            {
                "eventId": event_id,
                "originalResourceId": selected.key.resource_id,
                "originalRawSha256": selected.key.raw_sha256,
                "physicalAbiSha256": selected.abi.sha256,
            }
        )
    return bindings


def _expected_shared_shader_anchor(
    status: Mapping[str, Any], request: Mapping[str, Any], event_id: Any
) -> dict[str, Any]:
    """Bind one caller-frozen EID to the exact physical Shader identity.

    The caller may choose the EID, but it cannot choose the ResourceId, raw
    bytes, or ABI: those remain derived from the freshly validated workspace.
    The Bridge checks this tuple against the live PSO immediately before its
    resource-level mutation. This is the unique-resource restore path and does
    not inspect the catalog's complete ``uses`` array.
    """

    if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id < 1:
        raise ValueError("expected_anchor_event_id must be a positive integer")
    shader = status.get("shader")
    baseline = status.get("baseline")
    binding = request.get("binding")
    if not all(
        isinstance(value, Mapping) for value in (shader, baseline, binding)
    ):
        raise RuntimeError("Shader anchor binding is incomplete")
    resource_id = shader.get("resourceId")
    raw_sha256 = str(baseline.get("rawSha256", "") or "")
    abi_sha256 = str(binding.get("baselineAbiSha256", "") or "")
    if (
        isinstance(resource_id, bool)
        or not isinstance(resource_id, int)
        or resource_id < 1
        or not _is_lower_hex_value(raw_sha256, 64)
        or not _is_lower_hex_value(abi_sha256, 64)
    ):
        raise RuntimeError("Shader anchor physical identity is invalid")
    return {
        "eventId": event_id,
        "originalResourceId": resource_id,
        "originalRawSha256": raw_sha256,
        "physicalAbiSha256": abi_sha256,
    }


def _validated_shader_family_apply_response(
    response: Any,
    *,
    status: dict,
    request: dict,
    event_bindings: list[dict],
    report_sha256: str,
) -> dict:
    if not isinstance(response, dict) or any(
        (
            response.get("schema")
            != "renderdoc-ultra-event-shader-override-batch/v1",
            response.get("schemaVersion") != 1,
            response.get("captureId") != request["binding"]["captureId"],
            not _is_lower_hex_value(
                response.get("captureSessionId"), 32
            ),
            response.get("candidateRawSha256")
            != request["binding"]["candidateRawSha256"],
            response.get("physicalAbiSha256")
            != request["binding"]["baselineAbiSha256"],
            response.get("transactional") is not True,
            response.get("originalShadersPreserved") is not True,
        )
    ):
        raise RuntimeError("RenderDoc returned an invalid Shader-family result")
    overrides = response.get("overrides")
    if not isinstance(overrides, list) or len(overrides) != len(event_bindings):
        raise RuntimeError("RenderDoc returned an incomplete Shader-family result")
    expected_by_event = {item["eventId"]: item for item in event_bindings}
    if len(expected_by_event) != len(event_bindings):
        raise RuntimeError("Shader-family event bindings are not unique")
    validated = []
    for override in overrides:
        if not isinstance(override, dict):
            raise RuntimeError("RenderDoc returned an invalid Shader-family override")
        event_id = override.get("eventId")
        binding = expected_by_event.pop(event_id, None)
        if binding is None:
            raise RuntimeError("RenderDoc returned an unexpected Shader-family event")
        validated.append(
            _validated_shader_apply_response(
                override,
                status=status,
                request=request,
                event_id=event_id,
                report_sha256=report_sha256,
                original_resource_id=binding["originalResourceId"],
                original_raw_sha256=binding["originalRawSha256"],
            )
        )
    if expected_by_event:
        raise RuntimeError("RenderDoc omitted a Shader-family event")
    family_ids = {item.get("shaderFamilyId") for item in validated}
    target_ids = {item.get("targetResourceId") for item in validated}
    capture_session_ids = {item.get("captureSessionId") for item in validated}
    if (
        len(family_ids) != 1
        or None in family_ids
        or len(target_ids) != 1
        or None in target_ids
        or response.get("shaderFamilyId") not in family_ids
        or response.get("targetResourceId") not in target_ids
        or capture_session_ids != {response.get("captureSessionId")}
    ):
        raise RuntimeError("RenderDoc split one Shader family into multiple targets")
    response["overrides"] = validated
    return response


def _validated_shared_shader_apply_response(
    response: Any,
    *,
    status: dict,
    request: dict,
    anchor: dict,
    report_sha256: str,
) -> dict:
    """Validate every identity in a Bridge resource-scoped replacement."""
    if not isinstance(response, dict):
        raise RuntimeError("RenderDoc returned an invalid shared Shader replacement")
    binding = request["binding"]
    expected = {
        "schema": "renderdoc-ultra-shared-shader-replacement/v1",
        "schemaVersion": 1,
        "captureId": binding["captureId"],
        "workspaceId": binding["workspaceId"],
        "revision": binding["revision"],
        "anchorEventId": anchor["eventId"],
        "stage": binding["stage"],
        "entryPoint": binding["entryPoint"],
        "api": binding["api"],
        "encoding": binding["encoding"],
        "originalResourceId": anchor["originalResourceId"],
        "originalRawSha256": anchor["originalRawSha256"],
        "candidateRawSha256": binding["candidateRawSha256"],
        "physicalAbiSha256": anchor["physicalAbiSha256"],
        "abiRequestId": request["requestId"],
        "abiReportSha256": report_sha256,
        "resourceScoped": True,
        "eventScoped": False,
        "allOriginalResourceUsesAffected": True,
        "configured": True,
        "originalShaderPreserved": True,
        "singleOwner": True,
        "checkedDerivedPipelineRebuild": binding["api"] in {
            "d3d12", "vulkan", "opengl"
        },
        "capturedRootSignaturesReused": binding["api"] == "d3d12",
        "postApplyAnchorVerified": binding["api"] in {"vulkan", "opengl"},
    }
    for key, value in expected.items():
        if response.get(key) != value:
            raise RuntimeError(
                "RenderDoc shared Shader response mismatch at %s" % key
            )
    replacement_id = str(response.get("replacementId", "") or "")
    if len(replacement_id) != 32 or any(
        character not in "0123456789abcdef" for character in replacement_id
    ):
        raise RuntimeError("RenderDoc returned an invalid shared replacement ID")
    capture_session_id = str(response.get("captureSessionId", "") or "")
    if len(capture_session_id) != 32 or any(
        character not in "0123456789abcdef" for character in capture_session_id
    ):
        raise RuntimeError("RenderDoc returned an invalid capture session ID")
    with _SHADER_OVERRIDE_GUARD:
        if (
            _SHADER_OVERRIDE_CAPTURE_KEY
            and capture_session_id != _SHADER_OVERRIDE_CAPTURE_KEY
        ):
            raise RuntimeError("RenderDoc capture changed during shared Shader apply")
    for key in ("idempotent", "metadataOnly", "targetBuiltOnce", "targetReused"):
        if not isinstance(response.get(key), bool):
            raise RuntimeError(
                "RenderDoc returned invalid shared replacement evidence at %s" % key
            )
    reused = response["idempotent"] or response["metadataOnly"]
    operation_id = str(response.get("operationId", "") or "")
    if (
        len(operation_id) != 32
        or any(character not in "0123456789abcdef" for character in operation_id)
        or not isinstance(response.get("operationCreatedReplacement"), bool)
        or response["operationCreatedReplacement"] is not (not reused)
        or response["targetBuiltOnce"] is reused
        or response["targetReused"] is not reused
        or (response["idempotent"] and response["metadataOnly"])
    ):
        raise RuntimeError(
            "RenderDoc returned inconsistent shared target ownership evidence"
        )
    superseded_id = response.get("supersededReplacementId")
    if superseded_id is not None and (
        not isinstance(superseded_id, str)
        or len(superseded_id) != 32
        or any(character not in "0123456789abcdef" for character in superseded_id)
        or superseded_id == replacement_id
    ):
        raise RuntimeError("RenderDoc returned an invalid superseded replacement ID")
    if reused and superseded_id is not None:
        raise RuntimeError(
            "RenderDoc claimed a supersede while reusing the existing target"
        )
    if not isinstance(response.get("supersededTargetFreed"), bool):
        raise RuntimeError(
            "RenderDoc omitted superseded target release evidence"
        )
    target_id = response.get("targetResourceId")
    if not isinstance(target_id, int) or isinstance(target_id, bool) or target_id <= 0:
        raise RuntimeError("RenderDoc returned an invalid target Shader ID")
    family_id = str(response.get("shaderFamilyId", "") or "")
    if len(family_id) != 64 or any(
        character not in "0123456789abcdef" for character in family_id
    ):
        raise RuntimeError("RenderDoc returned an invalid Shader family ID")
    return response


# ==================== 1. CONNECTION ====================

@mcp.tool()
def ping() -> str:
    """Check if the RenderDoc bridge is running and responsive.

    Returns ``ok`` when RenderDoc is running with its built-in ReplayLab
    integration active.
    If this fails, make sure:
    1. RenderDoc (qrenderdoc) is open
    2. A capture file is loaded
    3. The built-in ReplayLab bridge completed initialization
    """
    return _call("ping")


@mcp.tool(description=REPORT_TOOL_DESCRIPTION)
def report_task_result(
    summary: str,
    status: Literal["completed", "partial", "failed"],
    findings: list[Finding] | None = None,
    artifacts: list[Artifact] | None = None,
    unknowns: list[str] | None = None,
    next_steps: list[str] | None = None,
) -> str:
    """Submit the Host-validated report form for the active ReplayLab task."""

    arguments = {
        "summary": summary,
        "status": status,
        "findings": [item.model_dump() for item in (findings or [])],
        "artifacts": [item.model_dump() for item in (artifacts or [])],
        "unknowns": list(unknowns or []),
        "next_steps": list(next_steps or []),
    }
    try:
        return _ok(current_task_report_store().submit(arguments))
    except Exception as error:
        return _err(error)

@mcp.tool()
def get_capture_status() -> str:
    """Check if a capture is loaded in RenderDoc and get API info."""
    return _call("get_capture_status")


@mcp.tool()
def get_current_renderdoc_context() -> str:
    """Read the capture-bound context currently visible in RenderDoc.

    The deterministic snapshot includes the capture session, selected/current
    event IDs, action marker path, every bound shader's exact identity, and the
    Texture Viewer's current resource/subresource/picked pixel when available.
    ``focusedShader`` is null unless a stage-scoped RenderDoc UI command supplied
    an explicit focus; this tool never fabricates a UI selection.
    """
    return _call("get_current_renderdoc_context")


# ==================== 2. CAPTURE MANAGEMENT ====================

@mcp.tool()
def list_captures(directory: str) -> str:
    """List all .rdc capture files in a directory.

    Args:
        directory: Directory path to search for .rdc files.
    """
    return _call("list_captures", {"directory": directory})

@mcp.tool()
def open_capture(capture_path: str) -> str:
    """Open a capture file in the running RenderDoc instance.

    Args:
        capture_path: Absolute path to the .rdc file.
    """
    return _call("open_capture", {"capture_path": capture_path})


@mcp.tool()
def debug_vulkan_bindings(event_id: int, stage: str = "fragment") -> str:
    """Debug Vulkan descriptor set bindings - dump raw data structures to diagnose texture binding resolution.

    Args:
        event_id: The event ID of the draw call.
        stage: Shader stage (default "fragment").
    """
    return _call("debug_vulkan_bindings", {"event_id": event_id, "stage": stage}, timeout=60.0)


@mcp.tool()
def analyze_lighting(event_id: int) -> str:
    """Analyze lighting setup from a draw call's shader and constant buffers.

    Extracts all lighting-related information:
    - Main directional light (direction, color, intensity)
    - Point/spot lights (position, color, range, attenuation)
    - Shadow maps (resolution, format, cascade info)
    - Environment lighting (IBL cubemap, SH coefficients, ambient color)
    - Lighting model detection (PBR/Blinn-Phong/Lambert from shader code)
    - All cbuffer variables with semantic classification

    This is the PRIMARY tool for understanding how a scene is lit.

    Args:
        event_id: The event ID of the draw call to analyze.
    """
    return _call("analyze_lighting", {"event_id": event_id}, timeout=60.0)


@mcp.tool()
def identify_drawcalls(
    event_id_min: int = 0,
    event_id_max: int = 999999,
    output_dir: str = "",
    render_target: int = 0,
) -> str:
    """Identify what each DrawCall renders by analyzing RT output, shader groups, bound textures, and screen-space bounding boxes.

    This is the PRIMARY tool for understanding what each DrawCall draws (eye, hair, armor, weapon, etc.)
    Instead of guessing by polygon count, it provides:
    - Per-DrawCall RT thumbnails (showing what was rendered at that point)
    - Shader grouping (same shader = same material type)
    - Bound texture list with resource IDs (check albedo to identify the part)
    - Screen-space bounding box (where on screen this part appears)
    - Screen coverage percentage (how much of the screen this part covers)

    Args:
        event_id_min: Start of event range (default 0).
        event_id_max: End of event range (default 999999).
        output_dir: Directory to save per-DrawCall RT thumbnails as PNG.
        render_target: Override render target resource ID (auto-detected if 0).
    """
    return _call("identify_drawcalls", {
        "event_id_min": event_id_min,
        "event_id_max": event_id_max,
        "output_dir": output_dir,
        "render_target": render_target,
    }, timeout=120.0)


@mcp.tool()
def launch_renderdoc(capture_path: str) -> str:
    """Launch RenderDoc application and open a .rdc capture file.

    Use this when RenderDoc is not yet running or you want to open a new
    RenderDoc instance with a specific capture file. This will:
    1. Find the RenderDoc executable (qrenderdoc)
    2. Launch it with the specified .rdc file
    3. Wait for the MCP bridge to become responsive

    Wait time is automatically scaled by file size:
    - <50MB: up to 40 seconds
    - 50-200MB: up to 60 seconds
    - >200MB: up to 90 seconds

    If the bridge is not ready within the timeout, the function still returns
    success (launched=True, bridgeReady=False). Use ping() to check later.

    If RenderDoc is already running with the MCP bridge active, prefer
    using open_capture() instead — it's faster since it reuses the
    existing instance.

    Args:
        capture_path: Absolute path to the .rdc capture file to open.

    A custom qrenderdoc executable cannot be supplied by an MCP request. The
    process owner may set RENDERDOC_PATH before starting this server; otherwise
    only PATH/common qrenderdoc locations are considered.
    """
    # Validate capture file
    if not capture_path:
        return _ok({"error": "capture_path is required"})
    if not os.path.isfile(capture_path):
        return _ok({"error": "File not found: %s" % capture_path})
    if not capture_path.lower().endswith(".rdc"):
        return _ok({"error": "Not an .rdc file: %s" % capture_path})

    # Find qrenderdoc executable
    exe = _find_renderdoc_exe()
    if not exe:
        return _ok({
            "error": "Could not find qrenderdoc executable. "
                     "Set RENDERDOC_PATH before starting the MCP server or install "
                     "RenderDoc to a standard location.",
            "searchedPaths": _get_renderdoc_search_paths(),
        })

    # Check if bridge is already alive — if so, just use open_capture
    if ipc.is_bridge_alive():
        logger.info("RenderDoc bridge already running, using open_capture instead")
        result = _call("open_capture", {"capture_path": capture_path})
        try:
            parsed = json.loads(result)
            parsed["method"] = "open_capture (bridge was already running)"
            return _ok(parsed)
        except Exception:
            return result

    # Launch RenderDoc with the capture file
    logger.info("Launching RenderDoc: %s %s", exe, capture_path)
    try:
        proc = subprocess.Popen(
            [exe, capture_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            shell=False,
        )
    except Exception as e:
        return _ok({"error": "Failed to launch RenderDoc: %s" % str(e), "exe": exe})

    # Wait for the MCP bridge to come online
    # Dynamic timeout based on file size: bigger files take longer to load
    file_size_mb = os.path.getsize(capture_path) / (1024 * 1024)
    if file_size_mb > 200:
        max_wait = 90  # Very large captures (>200MB)
    elif file_size_mb > 50:
        max_wait = 60  # Large captures (50-200MB)
    else:
        max_wait = 40  # Normal captures (<50MB)

    poll_interval = 2.0
    elapsed = 0.0
    bridge_ready = False

    while elapsed < max_wait:
        time.sleep(poll_interval)
        elapsed += poll_interval
        if ipc.is_bridge_alive():
            bridge_ready = True
            break

    if bridge_ready:
        # Give it a moment for capture to fully load
        time.sleep(1.0)
        return _ok({
            "launched": True,
            "exe": exe,
            "capture": capture_path,
            "pid": proc.pid,
            "bridgeReady": True,
            "waitTime": "%.1fs" % elapsed,
            "fileSizeMB": round(file_size_mb, 1),
        })
    else:
        # RenderDoc launched successfully, but bridge not yet responsive.
        # This is NORMAL for large captures — NOT an error.
        return _ok({
            "launched": True,
            "exe": exe,
            "capture": capture_path,
            "pid": proc.pid,
            "bridgeReady": False,
            "waitTime": "%.1fs" % max_wait,
            "fileSizeMB": round(file_size_mb, 1),
            "status": "RenderDoc launched successfully. The capture file (%.0fMB) is still loading. "
                      "Use ping() to check when the bridge becomes ready." % file_size_mb,
        })


def _validated_qrenderdoc_candidate(value: str) -> str:
    """Return one canonical, regular qrenderdoc executable or an empty string."""
    if not value:
        return ""
    source = Path(value).expanduser()
    candidates = (
        [source / "qrenderdoc.exe", source / "qrenderdoc"]
        if source.is_dir()
        else [source]
    )
    for candidate in candidates:
        try:
            if (
                candidate.name.casefold() in ("qrenderdoc", "qrenderdoc.exe")
                and not candidate.is_symlink()
                and candidate.is_file()
            ):
                return str(candidate.resolve())
        except OSError:
            continue
    return ""


def _find_renderdoc_exe() -> str:
    """Find qrenderdoc without accepting an MCP-controlled executable path."""
    # 1. Process-owner environment variable (set before server startup).
    env_path = os.environ.get("RENDERDOC_PATH", "") or os.environ.get("RENDERDOC_MODULE_PATH", "")
    if env_path:
        candidate = _validated_qrenderdoc_candidate(env_path)
        if candidate:
            return candidate

    # 2. PATH
    which = shutil.which("qrenderdoc")
    if which:
        candidate = _validated_qrenderdoc_candidate(which)
        if candidate:
            return candidate

    # 3. Common install locations
    for path in _get_renderdoc_search_paths():
        candidate = _validated_qrenderdoc_candidate(path)
        if candidate:
            return candidate

    return ""


def _get_renderdoc_search_paths() -> list:
    """Get common RenderDoc install paths to search."""
    paths = []
    # Windows
    for drive in ["C", "D"]:
        paths.append("%s:\\Program Files\\RenderDoc\\qrenderdoc.exe" % drive)
        paths.append("%s:\\Program Files (x86)\\RenderDoc\\qrenderdoc.exe" % drive)
    # User-local installs on Windows
    appdata = os.environ.get("LOCALAPPDATA", "")
    if appdata:
        paths.append(os.path.join(appdata, "RenderDoc", "qrenderdoc.exe"))
    # Linux
    paths.extend([
        "/usr/bin/qrenderdoc",
        "/usr/local/bin/qrenderdoc",
        "/opt/renderdoc/bin/qrenderdoc",
    ])
    # macOS
    paths.append("/Applications/RenderDoc.app/Contents/MacOS/qrenderdoc")
    return paths


# ==================== 3. DRAWCALLS ====================

@mcp.tool()
def get_draw_calls(include_children: bool = True, marker_filter: str = "", only_actions: bool = False, event_id_min: int = 0, event_id_max: int = 0) -> str:
    """Get all draw calls/actions in the currently loaded capture.

    Args:
        include_children: Include child actions in hierarchy.
        marker_filter: Only return actions under markers matching this string.
        only_actions: Only return actual draw/dispatch actions, skip markers.
        event_id_min: Filter by minimum event ID (0 = no filter).
        event_id_max: Filter by maximum event ID (0 = no filter).
    """
    params = {"include_children": include_children, "only_actions": only_actions}
    if marker_filter: params["marker_filter"] = marker_filter
    if event_id_min > 0: params["event_id_min"] = event_id_min
    if event_id_max > 0: params["event_id_max"] = event_id_max
    return _call("get_draw_calls", params)

@mcp.tool()
def get_frame_summary() -> str:
    """Get a summary of the current capture frame (total draws, dispatches, markers)."""
    return _call("get_frame_summary")

@mcp.tool()
def get_draw_call_details(event_id: int) -> str:
    """Get detailed information about a specific draw call.

    Args:
        event_id: The event ID of the draw call.
    """
    return _call("get_draw_call_details", {"event_id": event_id})


# ==================== 3A. READ-ONLY COMPOSITES ====================

@mcp.tool()
def inspect_event(event_id: int) -> str:
    """Inspect one exact RenderDoc event and return draw plus pipeline evidence.

    This is the external-MCP counterpart of AgentHost's internal composite. It
    delegates to the same bundled qrenderdoc bridge method so both transports
    preserve the same capture-bound schema and error envelope.

    Args:
        event_id: Frozen positive RenderDoc event ID.
    """

    return _call("inspect_event", {"event_id": event_id}, timeout=60.0)


@mcp.tool()
def analyze_pixel(
    texture_id: int,
    x: int,
    y: int,
    event_id: int,
    mip: int = 0,
    slice: int = 0,
    sample: int = 0,
    prefer_geometry: bool = True,
) -> str:
    """Analyze one frozen texture pixel with metadata, range, and writer evidence.

    Args:
        texture_id: Frozen texture resource ID.
        x: Pixel X coordinate.
        y: Pixel Y coordinate.
        event_id: Frozen positive cutoff event ID.
        mip: Mip level.
        slice: Array slice.
        sample: Multisample index.
        prefer_geometry: Prefer passed raster geometry over a direct writer.
    """

    return _call(
        "analyze_pixel",
        {
            "texture_id": texture_id,
            "x": x,
            "y": y,
            "event_id": event_id,
            "mip": mip,
            "slice": slice,
            "sample": sample,
            "prefer_geometry": prefer_geometry,
        },
        timeout=120.0,
    )


@mcp.tool()
def inspect_shader(event_id: int, stage: str = "fragment") -> str:
    """Inspect one exact bound Shader using reflection and reconstruction evidence.

    Args:
        event_id: Frozen positive RenderDoc event ID.
        stage: Bound Shader stage identifier.
    """

    return _call(
        "inspect_shader", {"event_id": event_id, "stage": stage}, timeout=120.0
    )


# ==================== 4. PIPELINE STATE ====================

@mcp.tool()
def get_pipeline_state(event_id: int) -> str:
    """Get the full pipeline state at a specific event.

    Returns bound shaders, render targets, textures, samplers, viewport, etc.

    Args:
        event_id: The event ID to inspect.
    """
    return _call("get_pipeline_state", {"event_id": event_id})


# ==================== 5. SHADER ====================

# Private compatibility helpers for workspace code and direct bridge tests.
# ``inspect_shader`` is the sole external/Agent inspection composite, so these
# same-source aliases are intentionally not registered as MCP tools.
def get_shader_info(event_id: int, stage: str = "fragment") -> str:
    """Get shader information for a specific stage at an event.

    Returns shader reflection data, disassembly, bound resources, etc.

    Args:
        event_id: The event ID of the draw call.
        stage: Shader stage - "vertex", "fragment"/"pixel", "geometry", "compute", "hull"/"tess_ctrl", "domain"/"tess_eval".
    """
    return _call("get_shader_info", {"event_id": event_id, "stage": stage})

def reverse_shader(event_id: int, stage: str = "fragment") -> str:
    """Inspect a shader using RenderDoc reflection, disassembly, bindings, and live cbuffer values.

    RenderDoc may expose embedded source or a high-level disassembly target, but
    this tool does not guarantee HLSL/GLSL decompilation. Use
    prepare_shader_workspace with a configured external decompiler when an
    editable source workflow is required.

    Args:
        event_id: The event ID of the draw call.
        stage: Shader stage - "vertex", "fragment"/"pixel", "geometry", "compute", "hull"/"tess_ctrl", "domain"/"tess_eval".
    """
    return _call("reverse_shader", {"event_id": event_id, "stage": stage})


@mcp.tool()
def get_current_shader_representation(event_id: int, stage: str = "fragment") -> str:
    """Persist a non-empty explanation artifact for any reflected Shader API.

    Unlike the DXBC/DXIL executable workspace, this tool needs no capture
    catalog and therefore also covers Vulkan SPIR-V, OpenGL GLSL/program
    binaries, and other formats that RenderDoc can reflect/disassemble. It
    writes only below the process-owned representation root. Embedded source
    and RenderDoc disassembly are explanatory-ir; if neither exists, it writes
    binary-only identity/hash diagnostics. No result is compile/apply eligible.
    """

    try:
        event_value = int(event_id)
        if event_value <= 0:
            raise ValueError("event_id must be a positive integer")
        if not str(stage or "").strip():
            raise ValueError("stage is required")
        response = ipc.call(
            "reverse_shader",
            {"event_id": event_value, "stage": stage},
            timeout=300.0,
        )
        if not isinstance(response, dict):
            raise RuntimeError("RenderDoc bridge returned an invalid reverse_shader result")
        try:
            raw_bytes, raw_bytes_diagnostic = _export_current_shader_raw_bytes(
                event_value, stage, response
            )
        except Exception as error:
            raw_bytes = None
            raw_bytes_diagnostic = {
                "code": "raw-bytes-export-failed",
                "message": str(error),
            }
        try:
            capture_status = ipc.call("get_capture_status", {}, timeout=30.0)
        except Exception as error:
            capture_status = {"statusUnavailable": str(error)}
        if not isinstance(capture_status, dict):
            capture_status = {"statusUnavailable": "invalid bridge response"}
        result = publish_current_event_representation(
            _SHADER_REPRESENTATIONS_ROOT,
            event_id=event_value,
            requested_stage=stage,
            bridge_response=response,
            capture_status=capture_status,
            raw_bytes=raw_bytes,
            raw_bytes_diagnostic=raw_bytes_diagnostic,
        )
        return _ok(result)
    except Exception as error:
        return _err(error)

@mcp.tool()
def get_bound_textures(event_id: int, stage: str = "fragment") -> str:
    """Get textures bound to a shader stage plus explicitly uncertain role hints.

    For each texture slot, shows shader variable name, actual bound texture,
    and a low-confidence name/slot-based role inference (albedo, normal,
    roughness, metallic, ao, emissive, etc.). Treat ``roleInference`` as a hint,
    never as capture evidence or a verified material semantic.

    Args:
        event_id: The event ID of the draw call.
        stage: Shader stage - "vertex", "fragment"/"pixel", "geometry", "compute", "hull"/"tess_ctrl", "domain"/"tess_eval".
    """
    return _call("get_bound_textures", {"event_id": event_id, "stage": stage})


# ==================== 6. SEARCH ====================

@mcp.tool()
def find_draws_by_shader(shader_name: str, stage: str = "") -> str:
    """Find all draw calls that use a shader with the given name (partial match).

    Useful for finding which draw calls render a specific material/effect.

    Args:
        shader_name: Partial shader name to search for.
        stage: Optional stage filter ("vertex", "fragment", etc.).
    """
    params = {"shader_name": shader_name}
    if stage: params["stage"] = stage
    return _call("find_by_shader", params)

@mcp.tool()
def find_draws_by_texture(texture_name: str) -> str:
    """Find all draw calls that use a texture with the given name (partial match).

    Useful for finding which draw calls use a specific texture (e.g. "eye").

    Args:
        texture_name: Partial texture name to search for (e.g. "eye", "skin", "hair").
    """
    return _call("find_by_texture", {"texture_name": texture_name})

@mcp.tool()
def find_draws_by_resource(resource_id: int) -> str:
    """Find all draw calls that use a specific resource ID.

    Args:
        resource_id: The exact resource ID to search for.
    """
    return _call("find_by_resource", {"resource_id": resource_id})


# ==================== 7. RESOURCES ====================

@mcp.tool()
def get_textures() -> str:
    """List all textures alive in the capture (dimensions, format, mips, etc.)."""
    return _call("get_textures")

@mcp.tool()
def get_buffers() -> str:
    """List all buffers alive in the capture (size, name)."""
    return _call("get_buffers")

@mcp.tool()
def get_resources() -> str:
    """List ALL resources in the capture (textures, buffers, shaders, pipelines, etc.)."""
    return _call("get_resources")

@mcp.tool()
def get_texture_info(resource_id: int) -> str:
    """Get texture metadata (dimensions, format, mips, etc.).

    Args:
        resource_id: The texture resource ID.
    """
    return _call("get_texture_info", {"resource_id": resource_id})

@mcp.tool()
def get_texture_data(resource_id: int, mip: int = 0, slice: int = 0) -> str:
    """Get texture pixel data.

    Args:
        resource_id: The texture resource ID.
        mip: Mip level (default 0).
        slice: Array slice (default 0).
    """
    return _call("get_texture_data", {"resource_id": resource_id, "mip": mip, "slice": slice})

@mcp.tool()
def get_buffer_contents(resource_id: int, offset: int = 0, length: int = 256) -> str:
    """Get buffer data.

    Args:
        resource_id: The buffer resource ID.
        offset: Byte offset to start reading.
        length: Number of bytes to read (0 = all).
    """
    return _call("get_buffer_data", {"resource_id": resource_id, "offset": offset, "length": length})


# ==================== 8. TEXTURE OPS ====================

@mcp.tool()
def pick_pixel(resource_id: int, x: int, y: int) -> str:
    """Read the RGBA value of a specific pixel from a texture.

    Args:
        resource_id: The texture resource ID.
        x: Pixel X coordinate.
        y: Pixel Y coordinate.
    """
    return _call("pick_pixel", {"resource_id": resource_id, "x": x, "y": y})


def _event_texture_evidence_uint(
    value: Any, field: str, minimum: int, maximum: int
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an integer" % field)
    if value < minimum or value > maximum:
        raise ValueError(
            "%s must be between %d and %d" % (field, minimum, maximum)
        )
    return int(value)


def _event_texture_evidence_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a finite number" % field)
    result = float(value)
    if not (-1.0e30 <= result <= 1.0e30):
        raise ValueError("%s must be a finite bounded number" % field)
    return result


def _event_texture_evidence_redirected(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return True
    if path.is_symlink():
        return True
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _event_texture_evidence_root() -> Path:
    root = Path(_EVENT_TEXTURE_EVIDENCE_ROOT)
    # Create each missing component only after every already-existing boundary
    # has passed the no-symlink/no-reparse check. ``mkdir(parents=True)`` would
    # otherwise follow a redirected ancestor before we could reject it.
    components = list(reversed(root.parents)) + [root]
    for component in components:
        if component.exists() or component.is_symlink():
            if _event_texture_evidence_redirected(component):
                raise RuntimeError(
                    "the Host-managed texture evidence root has a redirected ancestor"
                )
            continue
        os.mkdir(component, 0o700)
        if _event_texture_evidence_redirected(component):
            raise RuntimeError(
                "the Host-managed texture evidence root became redirected"
            )
    if not root.is_dir() or _event_texture_evidence_redirected(root):
        raise RuntimeError("the Host-managed texture evidence root is redirected")
    resolved = root.resolve(strict=True)
    if os.path.normcase(str(root)) != os.path.normcase(str(resolved)):
        raise RuntimeError(
            "the Host-managed texture evidence root has a redirected ancestor"
        )
    return resolved


def _allocate_event_texture_evidence_dir(
    root: Path, event_id: int
) -> tuple[str, Path]:
    for _unused in range(8):
        request_id = uuid.uuid4().hex
        request_dir = root / ("eid-%d-%s" % (event_id, request_id))
        try:
            os.mkdir(request_dir, 0o700)
        except FileExistsError:
            continue
        if (
            request_dir.parent != root
            or not is_within(root, request_dir)
            or _event_texture_evidence_redirected(request_dir)
            or request_dir.resolve(strict=True) != request_dir
        ):
            raise RuntimeError("the allocated texture evidence directory is unsafe")
        return request_id, request_dir
    raise RuntimeError("could not allocate a unique texture evidence directory")


def _cleanup_event_texture_evidence_dir(root: Path, request_dir: Path) -> None:
    """Best-effort cleanup of one exact, flat, Host-allocated request directory."""

    try:
        root = Path(root)
        request_dir = Path(request_dir)
        if (
            request_dir.parent != root
            or _EVENT_TEXTURE_EVIDENCE_REQUEST_DIR.fullmatch(request_dir.name) is None
            or _event_texture_evidence_redirected(root)
            or _event_texture_evidence_redirected(request_dir)
        ):
            return
        children = list(request_dir.iterdir())
        if any(
            child.is_dir()
            or child.is_symlink()
            or _event_texture_evidence_redirected(child)
            for child in children
        ):
            return
        for child in children:
            child.unlink()
        request_dir.rmdir()
    except Exception:
        pass


def _event_texture_evidence_filename(
    event_id: int,
    attachment_kind: str,
    attachment_index: int,
    mip: int,
    array_slice: int,
    sample: int,
    channel: str,
) -> str:
    return "eid-%d-%s%d-m%d-s%d-sample%d-%s.png" % (
        event_id,
        attachment_kind,
        attachment_index,
        mip,
        array_slice,
        sample,
        channel,
    )


def _validate_event_texture_evidence_bridge_result(
    result: Any,
    *,
    request_id: str,
    capture_session_id: str,
    event_id: int,
    original_event_id: int,
    request_dir: Path,
    attachment_kind: str,
    attachment_index: int,
    channel: str,
    mip: int,
    array_slice: int,
    sample: int,
    range_mode: str,
    black_point: float | None,
    white_point: float | None,
) -> dict[str, Any]:
    if not isinstance(result, dict) or any(
        (
            result.get("schema") != _EVENT_TEXTURE_EVIDENCE_SCHEMA,
            result.get("schemaVersion") != 1,
            result.get("requestId") != request_id,
            result.get("captureSessionId") != capture_session_id,
            result.get("eventId") != event_id,
            result.get("exactEventBinding") is not True,
            result.get("replayMutation") is not False,
        )
    ):
        raise RuntimeError("RenderDoc returned an invalid texture evidence receipt")
    restoration = result.get("restoration")
    if (
        not isinstance(restoration, dict)
        or restoration.get("originalEventId") != original_event_id
        or restoration.get("restored") is not True
    ):
        raise RuntimeError("RenderDoc did not prove restoration of the original EID")
    expected_budgets = {
        "maxTextureByteSize": _MAX_EVENT_TEXTURE_EVIDENCE_SOURCE_BYTES,
        "maxSubresourcePixels": _MAX_EVENT_TEXTURE_EVIDENCE_PIXELS,
        "maxPngByteSize": _MAX_EVENT_TEXTURE_EVIDENCE_PNG_BYTES,
    }
    if result.get("budgets") != expected_budgets:
        raise RuntimeError("RenderDoc returned unexpected texture evidence budgets")

    artifact = result.get("artifact")
    if not isinstance(artifact, dict):
        raise RuntimeError("RenderDoc omitted the texture evidence artifact receipt")
    status = artifact.get("status")
    if status not in {"saved", "skipped"}:
        raise RuntimeError("RenderDoc returned an invalid texture evidence status")
    attachment = artifact.get("attachment")
    if not isinstance(attachment, dict) or any(
        (
            attachment.get("kind") != attachment_kind,
            attachment.get("index") != attachment_index,
            attachment.get("mip") != mip,
            attachment.get("slice") != array_slice,
            attachment.get("sample") != sample,
            isinstance(attachment.get("resourceId"), bool),
            not isinstance(attachment.get("resourceId"), int),
            attachment.get("resourceId", 0) <= 0,
            isinstance(attachment.get("textureByteSize"), bool),
            not isinstance(attachment.get("textureByteSize"), int),
            attachment.get("textureByteSize", -1) < 0,
            isinstance(attachment.get("viewWidth"), bool),
            not isinstance(attachment.get("viewWidth"), int),
            attachment.get("viewWidth", 0) <= 0,
            isinstance(attachment.get("viewHeight"), bool),
            not isinstance(attachment.get("viewHeight"), int),
            attachment.get("viewHeight", 0) <= 0,
            isinstance(attachment.get("sampleCount"), bool),
            not isinstance(attachment.get("sampleCount"), int),
            attachment.get("sampleCount", 0) <= sample,
            not isinstance(attachment.get("format"), str),
            not str(attachment.get("format", "")),
            len(str(attachment.get("format", ""))) > 256,
        )
    ):
        raise RuntimeError("RenderDoc returned an invalid bound attachment receipt")
    texture_byte_size = attachment["textureByteSize"]
    pixel_count = attachment["viewWidth"] * attachment["viewHeight"]
    descriptor = attachment.get("descriptor")
    if not isinstance(descriptor, dict):
        raise RuntimeError("RenderDoc omitted exact output-view bounds")
    for field in ("firstMip", "numMips", "firstSlice", "numSlices"):
        value = descriptor.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError("RenderDoc returned an invalid output-view bound")
    if (
        descriptor["numMips"] <= 0
        or descriptor["numSlices"] <= 0
        or not descriptor["firstMip"]
        <= mip
        < descriptor["firstMip"] + descriptor["numMips"]
        or not descriptor["firstSlice"]
        <= array_slice
        < descriptor["firstSlice"] + descriptor["numSlices"]
    ):
        raise RuntimeError("RenderDoc did not bind the requested exact subresource")

    display = artifact.get("display")
    expected_extract = {"rgba": -1, "r": 0, "g": 1, "b": 2, "a": 3, "depth": 0}
    if not isinstance(display, dict) or any(
        (
            display.get("channel") != channel,
            display.get("channelExtract") != expected_extract[channel],
            display.get("rangeMode") != range_mode,
            display.get("depthInterpretation")
            != ("raw-resource-component-0" if channel == "depth" else None),
        )
    ):
        raise RuntimeError("RenderDoc returned invalid channel display metadata")
    output = artifact.get("output")
    skip_reason = artifact.get("skipReason")
    allowed_skip_reasons = {
        "source-size-unavailable",
        "source-size-budget-exceeded",
        "subresource-pixel-budget-exceeded",
        "subresource-range-flat",
        "png-byte-budget-exceeded",
    }
    if status == "saved":
        if skip_reason is not None or not isinstance(output, dict):
            raise RuntimeError(
                "RenderDoc returned invalid saved texture evidence metadata"
            )
        if (
            texture_byte_size <= 0
            or texture_byte_size > _MAX_EVENT_TEXTURE_EVIDENCE_SOURCE_BYTES
            or pixel_count > _MAX_EVENT_TEXTURE_EVIDENCE_PIXELS
        ):
            raise RuntimeError("RenderDoc saved texture evidence beyond its budget")
    elif skip_reason not in allowed_skip_reasons or output is not None:
        raise RuntimeError("RenderDoc returned an invalid budget skip receipt")

    actual_black = display.get("blackPoint")
    actual_white = display.get("whitePoint")
    if range_mode == "native":
        if (
            actual_black != 0.0
            or actual_white != 1.0
            or display.get("subresourceMin") is not None
            or display.get("subresourceMax") is not None
        ):
            raise RuntimeError("RenderDoc changed the native display range")
    elif range_mode == "explicit":
        actual_black = _event_texture_evidence_number(actual_black, "blackPoint")
        actual_white = _event_texture_evidence_number(actual_white, "whitePoint")
        if (
            actual_black != black_point
            or actual_white != white_point
            or display.get("subresourceMin") is not None
            or display.get("subresourceMax") is not None
        ):
            raise RuntimeError("RenderDoc changed the explicit display range")
    else:
        deferred_range = status == "skipped" and skip_reason in {
            "source-size-unavailable",
            "source-size-budget-exceeded",
            "subresource-pixel-budget-exceeded",
        }
        if deferred_range:
            if any(
                value is not None
                for value in (
                    actual_black,
                    actual_white,
                    display.get("subresourceMin"),
                    display.get("subresourceMax"),
                )
            ):
                raise RuntimeError(
                    "RenderDoc claimed min/max values for a pre-range budget skip"
                )
        else:
            actual_black = _event_texture_evidence_number(
                actual_black, "blackPoint"
            )
            actual_white = _event_texture_evidence_number(
                actual_white, "whitePoint"
            )
            minimum_values = display.get("subresourceMin")
            maximum_values = display.get("subresourceMax")
            if (
                not isinstance(minimum_values, list)
                or not isinstance(maximum_values, list)
                or len(minimum_values) != len(maximum_values)
                or not 1 <= len(minimum_values) <= 4
            ):
                raise RuntimeError(
                    "RenderDoc omitted exact subresource min/max values"
                )
            for minimum, maximum in zip(minimum_values, maximum_values):
                minimum = _event_texture_evidence_number(
                    minimum, "subresourceMin"
                )
                maximum = _event_texture_evidence_number(
                    maximum, "subresourceMax"
                )
                if minimum > maximum:
                    raise RuntimeError(
                        "RenderDoc returned an inverted subresource min/max range"
                    )
            selected_indices = (
                range(len(minimum_values))
                if channel == "rgba"
                else [expected_extract[channel]]
            )
            if any(index >= len(minimum_values) for index in selected_indices):
                raise RuntimeError(
                    "RenderDoc returned no min/max value for the selected channel"
                )
            expected_black = min(minimum_values[index] for index in selected_indices)
            expected_white = max(maximum_values[index] for index in selected_indices)
            if actual_black != expected_black or actual_white != expected_white:
                raise RuntimeError(
                    "RenderDoc min/max mapping does not match the selected channel"
                )
            if skip_reason == "subresource-range-flat":
                if status != "skipped" or actual_black != actual_white:
                    raise RuntimeError("RenderDoc returned an invalid flat-range skip")
            elif actual_black >= actual_white:
                raise RuntimeError(
                    "RenderDoc returned an invalid min/max display range"
                )
    if channel == "depth" and actual_black is not None and (
        _event_texture_evidence_number(actual_black, "depth blackPoint") < 0.0
        or _event_texture_evidence_number(actual_white, "depth whitePoint") > 1.0
    ):
        raise RuntimeError("RenderDoc returned a transformed/out-of-range depth view")

    if status == "skipped":
        skip_matches_budget = {
            "source-size-unavailable": texture_byte_size == 0,
            "source-size-budget-exceeded": (
                texture_byte_size > _MAX_EVENT_TEXTURE_EVIDENCE_SOURCE_BYTES
            ),
            "subresource-pixel-budget-exceeded": (
                0 < texture_byte_size <= _MAX_EVENT_TEXTURE_EVIDENCE_SOURCE_BYTES
                and pixel_count > _MAX_EVENT_TEXTURE_EVIDENCE_PIXELS
            ),
            "subresource-range-flat": (
                0 < texture_byte_size <= _MAX_EVENT_TEXTURE_EVIDENCE_SOURCE_BYTES
                and pixel_count <= _MAX_EVENT_TEXTURE_EVIDENCE_PIXELS
                and range_mode == "subresource-minmax"
                and actual_black == actual_white
            ),
            "png-byte-budget-exceeded": (
                0 < texture_byte_size <= _MAX_EVENT_TEXTURE_EVIDENCE_SOURCE_BYTES
                and pixel_count <= _MAX_EVENT_TEXTURE_EVIDENCE_PIXELS
            ),
        }[skip_reason]
        if not skip_matches_budget:
            raise RuntimeError("RenderDoc returned an invalid budget skip receipt")
        if list(request_dir.iterdir()):
            raise RuntimeError("a skipped texture evidence request left output files")
        return result

    expected_name = _event_texture_evidence_filename(
        event_id,
        attachment_kind,
        attachment_index,
        mip,
        array_slice,
        sample,
        channel,
    )
    expected_path = request_dir / expected_name
    supplied_path = Path(str(output.get("path", "") or ""))
    if (
        not supplied_path.is_absolute()
        or supplied_path != expected_path
        or output.get("mimeType") != "image/png"
        or output.get("noOverwrite") is not True
        or _event_texture_evidence_redirected(supplied_path)
        or not supplied_path.is_file()
        or supplied_path.resolve(strict=True) != supplied_path
        or not is_within(request_dir, supplied_path)
    ):
        raise RuntimeError("RenderDoc returned an unsafe texture evidence path")
    children = list(request_dir.iterdir())
    if children != [expected_path]:
        raise RuntimeError("RenderDoc returned unreported texture evidence files")
    size = output.get("byteSize")
    digest = str(output.get("sha256", "") or "")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size <= 0
        or size > _MAX_EVENT_TEXTURE_EVIDENCE_PNG_BYTES
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or supplied_path.stat().st_size != size
        or _bounded_file_sha256(
            supplied_path,
            _MAX_EVENT_TEXTURE_EVIDENCE_PNG_BYTES,
            "Event texture evidence PNG",
        )
        != digest
    ):
        raise RuntimeError("RenderDoc returned invalid texture evidence integrity data")
    with supplied_path.open("rb") as stream:
        if stream.read(8) != b"\x89PNG\r\n\x1a\n":
            raise RuntimeError("RenderDoc texture evidence is not a PNG file")
    return result


def _write_event_texture_evidence_manifest(
    request_dir: Path, payload: dict[str, Any]
) -> dict[str, Any]:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if not encoded or len(encoded) > _MAX_EVENT_TEXTURE_EVIDENCE_MANIFEST_BYTES:
        raise RuntimeError("texture evidence manifest exceeds its byte budget")
    final_path = request_dir / "manifest.json"
    temporary_path = request_dir / (".manifest-%s.json" % uuid.uuid4().hex)
    if final_path.exists() or final_path.is_symlink():
        raise RuntimeError("texture evidence manifest already exists")
    try:
        with temporary_path.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary_path, final_path)
        temporary_path.unlink()
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
    if (
        _event_texture_evidence_redirected(final_path)
        or not final_path.is_file()
        or final_path.resolve(strict=True) != final_path
    ):
        raise RuntimeError("texture evidence manifest publication is unsafe")
    digest = _bounded_file_sha256(
        final_path,
        _MAX_EVENT_TEXTURE_EVIDENCE_MANIFEST_BYTES,
        "Event texture evidence manifest",
    )
    return {
        "contentSchema": _EVENT_TEXTURE_EVIDENCE_HOST_SCHEMA,
        "path": str(final_path),
        "mimeType": "application/json",
        "byteSize": len(encoded),
        "sha256": digest,
        "noOverwrite": True,
    }


@mcp.tool()
def capture_event_texture_evidence(
    event_id: int,
    attachment_kind: str,
    attachment_index: int = 0,
    channel: str = "rgba",
    mip: int = 0,
    slice: int = 0,
    sample: int = 0,
    range_mode: str = "native",
    black_point: float | None = None,
    white_point: float | None = None,
) -> str:
    """Save one exact EID-bound RTV/DSV display PNG as managed evidence.

    The caller never supplies a filesystem path. ``attachment_kind`` is
    exactly ``rtv`` or ``dsv``; DSV only accepts ``channel=depth`` and remains
    raw resource depth (no linearisation). Channels are ``rgba/r/g/b/a/depth``.
    Range mode is ``native``, ``explicit`` (requiring black/white points), or
    ``subresource-minmax``. Mip, slice, and sample identify the exact bound
    subresource. Oversized/unknown resources return a strict skip receipt.
    """

    request_dir: Path | None = None
    root: Path | None = None
    try:
        exact_event_id = _event_texture_evidence_uint(
            event_id, "event_id", 1, 0xFFFFFFFF
        )
        if not isinstance(attachment_kind, str) or attachment_kind not in {"rtv", "dsv"}:
            raise ValueError("attachment_kind must be exactly 'rtv' or 'dsv'")
        exact_attachment_index = _event_texture_evidence_uint(
            attachment_index, "attachment_index", 0, 7
        )
        if attachment_kind == "dsv" and exact_attachment_index != 0:
            raise ValueError("a DSV attachment_index must be 0")
        if not isinstance(channel, str) or channel not in {
            "rgba", "r", "g", "b", "a", "depth"
        }:
            raise ValueError("channel must be rgba, r, g, b, a, or depth")
        if attachment_kind == "dsv" and channel != "depth":
            raise ValueError("DSV evidence only supports raw depth")
        if attachment_kind == "rtv" and channel == "depth":
            raise ValueError("depth evidence requires a DSV")
        exact_mip = _event_texture_evidence_uint(mip, "mip", 0, 0x7FFFFFFF)
        exact_slice = _event_texture_evidence_uint(slice, "slice", 0, 0x7FFFFFFF)
        exact_sample = _event_texture_evidence_uint(
            sample, "sample", 0, 0x7FFFFFFF
        )
        if not isinstance(range_mode, str) or range_mode not in {
            "native", "explicit", "subresource-minmax"
        }:
            raise ValueError(
                "range_mode must be native, explicit, or subresource-minmax"
            )
        if range_mode == "explicit":
            exact_black = _event_texture_evidence_number(
                black_point, "black_point"
            )
            exact_white = _event_texture_evidence_number(
                white_point, "white_point"
            )
            if exact_black >= exact_white:
                raise ValueError("black_point must be less than white_point")
            if channel == "depth" and (exact_black < 0.0 or exact_white > 1.0):
                raise ValueError("raw depth display range must remain within [0, 1]")
        else:
            if black_point is not None or white_point is not None:
                raise ValueError(
                    "black_point and white_point are only valid for explicit range"
                )
            exact_black = None
            exact_white = None

        context = _active_live_context(allow_missing_capture_id=True)
        capture = context["capture"]
        capture_session_id = _strict_lower_hex(
            capture.get("captureSessionId"), 32, "capture.captureSessionId"
        )
        if (
            _SCOPED_CAPTURE_SESSION_ID
            and capture_session_id != _SCOPED_CAPTURE_SESSION_ID
        ):
            raise RuntimeError(
                "the scoped capture session changed before texture evidence capture"
            )
        events = context.get("events")
        if not isinstance(events, dict):
            raise RuntimeError("RenderDoc current context omitted the visible EID")
        original_event_id = _event_texture_evidence_uint(
            events.get("currentEventId"), "events.currentEventId", 0, 0xFFFFFFFF
        )

        root = _event_texture_evidence_root()
        request_id, request_dir = _allocate_event_texture_evidence_dir(
            root, exact_event_id
        )
        bridge_result = ipc.call(
            "capture_event_texture_evidence",
            {
                "event_id": exact_event_id,
                "attachment_kind": attachment_kind,
                "attachment_index": exact_attachment_index,
                "channel": channel,
                "mip": exact_mip,
                "slice": exact_slice,
                "sample": exact_sample,
                "range_mode": range_mode,
                "black_point": exact_black,
                "white_point": exact_white,
                "request_id": request_id,
                "artifact_dir": str(request_dir),
                "expected_capture_session_id": capture_session_id,
                "__replaylab_expected_capture_session_id": capture_session_id,
            },
            timeout=300.0,
        )
        validated = _validate_event_texture_evidence_bridge_result(
            bridge_result,
            request_id=request_id,
            capture_session_id=capture_session_id,
            event_id=exact_event_id,
            original_event_id=original_event_id,
            request_dir=request_dir,
            attachment_kind=attachment_kind,
            attachment_index=exact_attachment_index,
            channel=channel,
            mip=exact_mip,
            array_slice=exact_slice,
            sample=exact_sample,
            range_mode=range_mode,
            black_point=exact_black,
            white_point=exact_white,
        )
        restored_context = _require_same_active_capture(
            context, allow_missing_capture_id=True
        )
        if (
            not isinstance(restored_context.get("events"), dict)
            or restored_context["events"].get("currentEventId")
            != original_event_id
        ):
            raise RuntimeError("RenderDoc did not restore the original visible EID")
        manifest_payload = {
            "schema": _EVENT_TEXTURE_EVIDENCE_HOST_SCHEMA,
            "schemaVersion": 1,
            "requestId": request_id,
            "capture": {
                "captureId": str(capture.get("captureId", "") or ""),
                "captureSessionId": capture_session_id,
                "path": str(capture.get("path", "") or ""),
                "api": str(capture.get("api", "") or ""),
            },
            "eventId": exact_event_id,
            "status": validated["artifact"]["status"],
            "artifact": validated["artifact"],
            "restoration": validated["restoration"],
            "budgets": validated["budgets"],
            "policy": {
                "hostManagedPath": True,
                "callerSuppliedPath": False,
                "uniqueNoOverwrite": True,
                "exactEventBinding": True,
                "rtvDsvOnly": True,
                "depthInterpretation": "raw-only",
                "replayMutation": False,
            },
        }
        manifest = _write_event_texture_evidence_manifest(
            request_dir, manifest_payload
        )
        final_context = _require_same_active_capture(
            context, allow_missing_capture_id=True
        )
        if (
            not isinstance(final_context.get("events"), dict)
            or final_context["events"].get("currentEventId")
            != original_event_id
        ):
            raise RuntimeError("the visible EID changed while publishing evidence")
        response = dict(manifest_payload)
        response["manifest"] = manifest
        return _ok(response)
    except Exception as error:
        if root is not None and request_dir is not None:
            _cleanup_event_texture_evidence_dir(root, request_dir)
        return _err(error)


@mcp.tool()
def fingerprint_event_outputs(event_id: int, grid_size: int = 12) -> str:
    """Hash and sample all MRT/depth outputs for observational A/B validation.

    Bounded whole-subresource SHA-256 detects byte-exact equality. Large or
    unknown-size outputs skip materialisation to protect qrenderdoc and retain
    the deterministic pixel grid for explicit tolerance comparisons. This
    observes one captured event and never proves equivalence for every input.
    """

    try:
        if isinstance(event_id, bool) or int(event_id) <= 0:
            raise ValueError("event_id must be a positive integer")
        if isinstance(grid_size, bool) or not isinstance(grid_size, int):
            raise ValueError("grid_size must be an integer")
        if grid_size < 2 or grid_size > 32:
            raise ValueError("grid_size must be between 2 and 32")
        live = ipc.call("get_capture_status", {}, timeout=30.0)
        if not isinstance(live, dict) or live.get("loaded") is not True:
            raise RuntimeError("No capture is loaded in RenderDoc")
        capture_session_id = str(live.get("captureSessionId", "") or "")
        if len(capture_session_id) != 32 or any(
            character not in "0123456789abcdef"
            for character in capture_session_id
        ):
            raise RuntimeError("active capture has no valid session identity")
        result = ipc.call(
            "fingerprint_event_outputs",
            {
                "event_id": int(event_id),
                "grid_size": grid_size,
                "expected_capture_session_id": capture_session_id,
            },
            timeout=300.0,
        )
        if not isinstance(result, dict) or any(
            (
                result.get("schema") != "replaylab-event-output-fingerprint/v1",
                result.get("schemaVersion") != 1,
                result.get("captureSessionId") != capture_session_id,
                result.get("eventId") != int(event_id),
                result.get("gridSize") != grid_size,
                result.get("observationalOnly") is not True,
                result.get("semanticEquivalenceVerified") is not False,
            )
        ):
            raise RuntimeError("RenderDoc returned an invalid output fingerprint")
        outputs = result.get("outputs")
        if not isinstance(outputs, list) or len(outputs) > 9:
            raise RuntimeError("RenderDoc returned invalid output fingerprint targets")
        seen = set()
        for output in outputs:
            if not isinstance(output, dict):
                raise RuntimeError("RenderDoc returned an invalid output fingerprint item")
            key = (output.get("kind"), output.get("index"))
            raw_sha256_value = output.get("rawSha256")
            raw_byte_size = output.get("rawByteSize")
            raw_hash_status = output.get("rawHashStatus")
            samples = output.get("samples")
            if (
                key in seen
                or key[0] not in {"color", "depth"}
                or not isinstance(key[1], int)
                or not isinstance(output.get("resourceId"), int)
                or raw_hash_status not in {
                    "hashed",
                    "skipped-texture-too-large",
                    "skipped-size-unavailable",
                }
                or not isinstance(output.get("textureByteSize"), int)
                or output.get("textureByteSize") < 0
                or not isinstance(output.get("fullHashByteLimit"), int)
                or output.get("fullHashByteLimit") <= 0
                or not isinstance(samples, list)
                or len(samples) > grid_size * grid_size
            ):
                raise RuntimeError("RenderDoc returned invalid output fingerprint metadata")
            if raw_hash_status == "hashed":
                raw_sha256 = str(raw_sha256_value or "")
                if (
                    len(raw_sha256) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in raw_sha256
                    )
                    or not isinstance(raw_byte_size, int)
                    or raw_byte_size < 0
                ):
                    raise RuntimeError(
                        "RenderDoc returned invalid exact output hash evidence"
                    )
            elif raw_sha256_value is not None or raw_byte_size is not None:
                raise RuntimeError(
                    "RenderDoc returned bytes for a skipped output hash"
                )
            seen.add(key)
            for sample in samples:
                value = sample.get("value") if isinstance(sample, dict) else None
                if (
                    not isinstance(sample, dict)
                    or not isinstance(sample.get("x"), int)
                    or not isinstance(sample.get("y"), int)
                    or not isinstance(value, list)
                    or len(value) != 4
                    or any(
                        not isinstance(component, (int, float, str))
                        or isinstance(component, bool)
                        for component in value
                    )
                ):
                    raise RuntimeError("RenderDoc returned an invalid output sample")
        result["capturePath"] = str(live.get("capturePath", "") or "")
        result["validationScope"] = "single-captured-event-output-observation"
        return _ok(result)
    except Exception as error:
        return _err(error)

@mcp.tool()
def get_texture_minmax(resource_id: int) -> str:
    """Get the min and max pixel values in a texture.

    Args:
        resource_id: The texture resource ID.
    """
    return _call("get_texture_minmax", {"resource_id": resource_id})

# Legacy direct-path export helper retained only for non-MCP compatibility.
# Managed resource-export workflows own Agent-visible artifact writes.
def save_texture(resource_id: int, output_path: str, mip: int = 0, slice: int = -1) -> str:
    """Save a texture to disk. Supports PNG, JPG, BMP, TGA, HDR, EXR, DDS.

    Auto-detects CubeMap textures (arraysize=6) and handles them appropriately:
    - DDS format: saves all 6 faces in a single native cubemap file
    - PNG/JPG/etc: saves 6 individual face files (e.g. texture_face0_posX.png through face5_negZ.png)
    - Use slice=0..5 to export a specific face only

    CubeMap face order: 0=+X(Right), 1=-X(Left), 2=+Y(Up), 3=-Y(Down), 4=+Z(Front), 5=-Z(Back)

    Args:
        resource_id: The texture resource ID.
        output_path: File path to save to (format inferred from extension).
        mip: Mip level (default 0).
        slice: For CubeMap: -1=all faces (default), 0-5=specific face. Ignored for 2D textures.
    """
    return _call("save_texture", {
        "resource_id": resource_id,
        "output_path": output_path,
        "mip": mip,
        "slice": slice,
    })


# ==================== 9. PIXEL HISTORY ====================

@mcp.tool()
def pixel_history(resource_id: int, x: int, y: int, event_id: int) -> str:
    """Get pixel modifications through one explicit frozen event.

    This is not automatically the whole-frame final history. ``event_id`` is
    the exact cutoff and does not depend on the user's later UI selection.

    Args:
        resource_id: The render target texture resource ID.
        x: Pixel X coordinate.
        y: Pixel Y coordinate.
        event_id: Frozen positive cutoff event ID.
    """
    return _call(
        "pixel_history",
        {"resource_id": resource_id, "x": x, "y": y, "event_id": event_id},
    )


@mcp.tool()
def inspect_pixel_object(
    texture_id: int,
    x: int,
    y: int,
    event_id: int,
    mip: int = 0,
    slice: int = 0,
    sample: int = 0,
    prefer_geometry: bool = True,
) -> str:
    """Inspect a clicked render-target pixel and return event/fragment evidence.

    The result records its selected-event cutoff and separates the last recorded modification,
    passed write candidates, the last confirmed non-direct write, passed raster geometry, and
    potential direct shader writes. A passed item is not automatically a visible contributor
    after later opaque overwrites. It deliberately does not invent an engine object or instance
    ID: fullscreen/post-process draws, direct/compute writes, and transparent overdraw may not
    have one unique owner.

    This is an expensive replay operation. Invoke it only for an explicit click, never for hover
    or continuous mouse-move polling. A UI integration should run it asynchronously and show a
    pending state.

    Args:
        texture_id: Render target or depth texture resource ID.
        x: Pixel X coordinate in the selected mip.
        y: Pixel Y coordinate in the selected mip.
        event_id: Frozen positive PixelHistory cutoff event ID.
        mip: Mip level (default 0).
        slice: Array slice (default 0).
        sample: Multisample index (default 0).
        prefer_geometry: Prefer the last passed raster fragment over a non-geometry last writer.
    """
    return _call(
        "inspect_pixel_object",
        {
            "texture_id": texture_id,
            "x": x,
            "y": y,
            "event_id": event_id,
            "mip": mip,
            "slice": slice,
            "sample": sample,
            "prefer_geometry": prefer_geometry,
        },
        timeout=120.0,
    )


# ==================== 10. SHADER DEBUG ====================

@mcp.tool()
def debug_pixel(
    x: int,
    y: int,
    event_id: int = 0,
    sample: int = 0,
    primitive_id: int = -1,
    view: int = 0,
) -> str:
    """Debug a pixel shader invocation, optionally selecting its event and fragment.

    Args:
        x: Pixel X coordinate.
        y: Pixel Y coordinate.
        event_id: Event to debug; 0 keeps the current RenderDoc event.
        sample: Multisample index.
        primitive_id: Primitive reported by inspect_pixel_object; -1 lets RenderDoc choose.
        view: Layered/multiview view index.
    """
    return _call(
        "debug_pixel",
        {
            "x": x,
            "y": y,
            "event_id": event_id,
            "sample": sample,
            "primitive_id": primitive_id,
            "view": view,
        },
        timeout=120.0,
    )

@mcp.tool()
def debug_vertex(vertex_id: int, instance_id: int = 0) -> str:
    """Debug the vertex shader execution for a specific vertex. Must set_event first.

    Args:
        vertex_id: The vertex ID to debug.
        instance_id: Instance ID (default 0).
    """
    return _call("debug_vertex", {"vertex_id": vertex_id, "instance_id": instance_id})


# ==================== 11. PERF COUNTERS ====================

@mcp.tool()
def enumerate_counters() -> str:
    """List all available GPU performance counters for the current capture."""
    return _call("enumerate_counters")

@mcp.tool()
def fetch_counters(counter_ids: str) -> str:
    """Fetch values for specific GPU performance counters.

    Args:
        counter_ids: Comma-separated list of counter IDs (e.g. "1,2,3").
    """
    ids = [int(x.strip()) for x in counter_ids.split(",")]
    return _call("fetch_counters", {"counter_ids": ids})


# ==================== 12. MESH & DEBUG MSGS ====================

@mcp.tool()
def get_post_vs_data() -> str:
    """Get post-vertex-shader mesh data for the current draw call."""
    return _call("get_post_vs_data")

@mcp.tool()
def get_debug_messages() -> str:
    """Get API validation/debug messages from the graphics driver."""
    return _call("get_debug_messages")

@mcp.tool()
def get_action_timings(event_ids: str = "", marker_filter: str = "") -> str:
    """Get GPU timing information for actions.

    Args:
        event_ids: Comma-separated event IDs (empty = all).
        marker_filter: Only actions under matching markers.
    """
    params = {}
    if event_ids: params["event_ids"] = [int(x.strip()) for x in event_ids.split(",")]
    if marker_filter: params["marker_filter"] = marker_filter
    return _call("get_action_timings", params)


# ==================== 13. EXPORT ====================

# Legacy direct-path export helpers retained only for non-MCP compatibility.
def export_drawcall(event_id: int, output_dir: str) -> str:
    """One-shot export of a draw call: shaders + textures + cbuffer values to disk.

    Exports shader disassembly, source code, all bound textures as PNG, and a summary JSON.

    Args:
        event_id: The event ID of the draw call to export.
        output_dir: Directory to save all exported files.
    """
    return _call("export_drawcall", {"event_id": event_id, "output_dir": output_dir}, timeout=60)


def export_to_unity(event_id: int, output_dir: str, mesh_name: str = "exported_mesh") -> str:
    """Export a draw call's mesh and shader data as Unity-ready assets.

    This is the ultimate tool for bringing RenderDoc captures into Unity:
    - Exports the 3D mesh as .obj AND .fbx (positions, normals, UVs, indices)
    - Collects all bound texture info with role detection (use save_texture to export textures separately)
    - Generates a Unity material definition JSON with property mappings
    - Maps shader parameters to Unity Standard/URP/HDRP material properties

    The output folder can be directly copied into a Unity project's Assets folder.

    Args:
        event_id: The event ID of the draw call to export.
        output_dir: Directory to save all exported Unity-ready files.
        mesh_name: Name for the exported mesh and material (default: "exported_mesh").
    """
    return _call("export_to_unity", {
        "event_id": event_id,
        "output_dir": output_dir,
        "mesh_name": mesh_name,
    }, timeout=60)


# ==================== 14. SYMBOL RESOLVER ====================

def _event_id_list(event_ids: str) -> list[int]:
    if not event_ids.strip():
        return []
    return [int(value.strip()) for value in event_ids.split(",") if value.strip()]


@mcp.tool()
def export_capture_symbol_catalog(output_dir: str, event_ids: str = "") -> str:
    """Export a lossless, versioned shader/constant-buffer catalog from RenderDoc.

    The bridge writes native shader blobs to ``output_dir/blobs`` instead of
    placing large binary data in MCP/IPC messages. No AI inference is performed.

    Args:
        output_dir: Directory for shader_catalog.v1.json and native shader blobs.
        event_ids: Optional comma-separated Draw/Dispatch event IDs; empty scans all actions.
    """
    return _call(
        "export_shader_catalog",
        {"output_dir": output_dir, "event_ids": _event_id_list(event_ids)},
        timeout=300.0,
    )


@mcp.tool()
def scan_asset_symbols(
    asset_root: str,
    database_path: str = "",
    build_id: str = "",
    max_files: int = 200000,
    max_file_size_mb: int = 2048,
    provider_id: str = "",
    include_recovery_candidates: bool = False,
) -> str:
    """Deterministically inventory an unpacked asset tree and index embedded DXBC/DXIL.

    This stage uses fixed parsers and hashes only. It also imports versioned JSON
    symbol manifests when present. Optional recovery discovery is deliberately
    opt-in so this legacy scan response does not unexpectedly contain a large
    source/IR/binary candidate inventory. It never asks AI to parse or execute
    asset code, and discovery never authorises workspace seeding/apply.

    Args:
        asset_root: Root of the unpacked game assets.
        database_path: Optional SQLite path; a stable LocalAppData path is used if empty.
        build_id: Optional game build/version identity used to separate releases.
        max_files: Safety limit for recursive files.
        max_file_size_mb: Per-file scan limit.
        provider_id: Optional ID from the fixed per-user trusted-provider registry.
            The MCP contract never accepts a registry path, executable, argv, or env.
        include_recovery_candidates: Also discover and value untrusted Shader
            source/IR/binary candidates under the same root.
    """
    try:
        if not isinstance(include_recovery_candidates, bool):
            raise TypeError("include_recovery_candidates must be a boolean")
        service = SymbolService.for_asset_root(asset_root, database_path or None)
        result = service.scan(
            asset_root,
            build_id=build_id,
            max_files=max_files,
            max_file_size_mb=max_file_size_mb,
            provider_id=provider_id,
        )
        if not isinstance(result, dict):
            raise RuntimeError("asset scanner returned an invalid result")
        result = dict(result)
        if include_recovery_candidates:
            result["recoveryInventory"] = discover_recovery_assets(
                asset_root,
                max_files=max(1, min(int(max_files), 50_000)),
                max_file_size_bytes=(
                    max(1, min(int(max_file_size_mb), 64)) * 1024 * 1024
                ),
                max_total_read_bytes=512 * 1024 * 1024,
                max_candidates=_RECOVERY_LEGACY_SCAN_CANDIDATES,
            )
        else:
            result["recoveryInventory"] = None
        return _ok(result)
    except Exception as error:
        return _err(error)


@mcp.tool()
def inspect_asset_shader_recovery(
    asset_root: str,
    capture_facts_json: str = "",
    max_files: int = 50000,
    max_file_size_mb: int = 64,
    max_total_read_mb: int = 512,
    max_candidates: int = _RECOVERY_INSPECT_DEFAULT_CANDIDATES,
) -> str:
    """Discover and value untrusted Shader recovery inputs below an asset root.

    Recognised inputs include common HLSL/GLSL/WGSL/Slang/MSL/engine source,
    SPIR-V assembly, embedded DXBC/DXIL, whole-file SPIR-V, and MetalLib.  This
    tool only reads, parses, hashes, and ranks; it never executes asset code,
    invokes a compiler, seeds a workspace, or applies a Shader.

    Args:
        asset_root: Absolute or relative unpacked-asset root to inspect.
        capture_facts_json: Optional JSON object with stage/profile/entryPoint
            and raw/code/interface/layout hashes used for deterministic ranking.
        max_files: Maximum regular files considered.
        max_file_size_mb: Maximum complete size of one file read.
        max_total_read_mb: Aggregate read budget.
        max_candidates: Maximum source/IR/binary candidates returned.
    """

    try:
        max_files = _recovery_server_limit(
            max_files, "max_files", _RECOVERY_INSPECT_MAX_FILES
        )
        max_file_size_mb = _recovery_server_limit(
            max_file_size_mb,
            "max_file_size_mb",
            _RECOVERY_INSPECT_MAX_FILE_SIZE_MB,
        )
        max_total_read_mb = _recovery_server_limit(
            max_total_read_mb,
            "max_total_read_mb",
            _RECOVERY_INSPECT_MAX_TOTAL_READ_MB,
        )
        max_candidates = _recovery_server_limit(
            max_candidates,
            "max_candidates",
            _RECOVERY_INSPECT_MAX_CANDIDATES,
        )
        capture_facts = None
        if (
            len(capture_facts_json.encode("utf-8", errors="replace"))
            > _RECOVERY_MAX_CAPTURE_FACTS_JSON_BYTES
        ):
            raise ValueError("capture_facts_json exceeds the fixed 64 KiB limit")
        if capture_facts_json.strip():
            capture_facts = json.loads(capture_facts_json)
            if not isinstance(capture_facts, dict):
                raise ValueError("capture_facts_json must decode to a JSON object")
        return _ok(
            discover_recovery_assets(
                asset_root,
                capture_facts,
                max_files=max_files,
                max_file_size_bytes=max_file_size_mb * 1024 * 1024,
                max_total_read_bytes=max_total_read_mb * 1024 * 1024,
                max_candidates=max_candidates,
            )
        )
    except Exception as error:
        return _err(error)


def _one_shot_recovery_context(
    catalog_path: str | Path, workspace: Path
) -> dict[str, Any]:
    """Read only bounded Shader identity fields from the bridge-owned catalog."""

    raw_path = Path(catalog_path).expanduser()
    if raw_path.exists() and raw_path.is_symlink():
        raise RuntimeError("exported Shader catalog must not be a symlink")
    path = raw_path.resolve()
    workspace_root = workspace.resolve()
    if not is_within(workspace_root, path) or not path.is_file():
        raise RuntimeError("exported Shader catalog escaped the one-shot workspace")
    before = path.stat()
    if before.st_size <= 0 or before.st_size > 64 * 1024 * 1024:
        raise RuntimeError("exported Shader catalog size is invalid")
    payload = path.read_bytes()
    after = path.stat()
    if (
        len(payload) != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise RuntimeError("exported Shader catalog changed while reading")
    try:
        value = json.loads(payload.decode("utf-8-sig", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("exported Shader catalog is not UTF-8 JSON") from exc
    shaders = value.get("shaders") if isinstance(value, dict) else None
    if not isinstance(shaders, list) or len(shaders) > 4096:
        raise RuntimeError("exported Shader catalog has an invalid Shader array")

    bound: list[dict[str, Any]] = []
    for shader in shaders:
        if not isinstance(shader, dict):
            continue
        if len(bound) >= _RECOVERY_REPORT_MAX_BOUND_SHADERS:
            break
        raw_hash = str(shader.get("rawSha256", "") or "").casefold()
        code_hash = str(shader.get("codeSha256", "") or "").casefold()
        interface_hash = str(shader.get("interfaceHash", "") or "").casefold()
        layout_hash = str(shader.get("layoutHash", "") or "").casefold()
        for name, digest in (
            ("rawSha256", raw_hash),
            ("codeSha256", code_hash),
            ("interfaceHash", interface_hash),
            ("layoutHash", layout_hash),
        ):
            if digest and (
                len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise RuntimeError("exported Shader catalog %s is invalid" % name)
        try:
            resource_id = int(
                shader.get("shaderResourceId", shader.get("resourceId", 0))
            )
        except (TypeError, ValueError, OverflowError):
            resource_id = 0
        target_profile = shader.get("targetProfile", shader.get("profile", ""))
        if isinstance(target_profile, dict):
            target_profile = target_profile.get("profile", "")
        bound.append(
            {
                "stage": str(shader.get("stage", "") or "")[:64],
                "resourceId": resource_id,
                "entryPoint": str(
                    shader.get("entryPoint", shader.get("entry", "")) or ""
                )[:256],
                "targetProfile": str(target_profile or "")[:128],
                "rawSha256": raw_hash,
                "codeSha256": code_hash,
                "interfaceHash": interface_hash,
                "layoutHash": layout_hash,
                "debugName": str(
                    shader.get("debugName", shader.get("displayName", "")) or ""
                )[:512],
                "debugPath": str(
                    shader.get("debugPath", shader.get("sourcePath", "")) or ""
                )[:2048],
            }
        )
    return {
        "shaders": {"bound": bound},
        "recoveryShaderInputCount": len(shaders),
        "recoveryReportScope": "one-shot capture catalog + asset root",
    }


@mcp.tool()
def match_capture_assets(
    asset_root: str,
    catalog_path: str,
    scan_id: str = "",
    database_path: str = "",
    build_id: str = "",
    rescan: bool = True,
    provider_id: str = "",
) -> str:
    """Match a capture catalog to an unpacked asset tree using programmatic evidence.

    Unique full-blob matches and unique executable+complete-interface+layout
    matches are committed.
    Ambiguous/hashless/layout-only cases are returned as ``NeedsAI`` and are not
    applied to RenderDoc.

    Args:
        asset_root: Root of unpacked assets.
        catalog_path: shader_catalog.v1.json produced by export_capture_symbol_catalog.
        scan_id: Existing scan ID when rescan is false.
        database_path: Optional symbol SQLite path.
        build_id: Game build/version identity.
        rescan: Re-index assets before matching.
        provider_id: Optional registered provider ID used only when rescanning.
    """
    try:
        service = SymbolService.for_asset_root(asset_root, database_path or None)
        if rescan or not scan_id:
            scan = service.scan(
                asset_root,
                build_id=build_id,
                provider_id=provider_id,
            )
            scan_id = scan["scanId"]
        result = service.match(catalog_path, scan_id)
        result["databasePath"] = str(service.database_path)
        return _ok(result)
    except Exception as error:
        return _err(error)


@mcp.tool()
def resolve_capture_symbols(
    asset_root: str,
    output_dir: str = "",
    database_path: str = "",
    build_id: str = "",
    event_ids: str = "",
    apply_verified: bool = True,
    provider_id: str = "",
) -> str:
    """Run capture export, asset scan, deterministic match, table export, and UI reload.

    This is the recommended one-shot entry. Only program-verified aliases are
    applied automatically. The response includes unresolved cases plus a
    bounded, untrusted Shader-recovery inventory summary, capture-Shader top
    candidates, and hash-revalidated source previews for later AI review.

    Args:
        asset_root: Root of unpacked game assets.
        output_dir: Working directory; defaults to a stable LocalAppData workspace.
        database_path: Optional SQLite path.
        build_id: Game build/version identity.
        event_ids: Optional comma-separated event IDs; empty scans all Draw/Dispatch actions.
        apply_verified: Load verified resource aliases into RenderDoc after matching.
        provider_id: Optional ID from the fixed per-user trusted-provider registry.
            No command, executable, registry path, or environment override is accepted.
    """
    try:
        service = SymbolService.for_asset_root(asset_root, database_path or None)
        workspace = (
            Path(output_dir).expanduser().resolve()
            if output_dir
            else default_workspace(asset_root) / "capture"
        )
        workspace.mkdir(parents=True, exist_ok=True)
        exported = ipc.call(
            "export_shader_catalog",
            {
                "output_dir": str(workspace),
                "event_ids": _event_id_list(event_ids),
            },
            timeout=300.0,
        )
        catalog_path = exported.get("catalogPath") or exported.get("path")
        if not catalog_path:
            raise RuntimeError("RenderDoc bridge did not return catalogPath")
        run = service.run(
            asset_root,
            catalog_path,
            build_id=build_id,
            provider_id=provider_id,
        )
        recovery_context = _one_shot_recovery_context(catalog_path, workspace)
        recovery_inventory = discover_recovery_assets(
            asset_root,
            max_files=50_000,
            max_file_size_bytes=64 * 1024 * 1024,
            max_total_read_bytes=512 * 1024 * 1024,
            max_candidates=_RECOVERY_ONE_SHOT_CANDIDATES,
        )
        asset_recovery = _active_asset_recovery_report(
            recovery_inventory, recovery_context, asset_root
        )
        capture_id = run["match"]["captureId"]
        alias_path = workspace / "symbol_aliases.json"
        alias_result = service.export_alias_table(
            capture_id,
            alias_path,
            verified_only=True,
            scan_id=run["scan"]["scanId"],
        )
        applied = None
        if apply_verified:
            applied = ipc.call(
                "load_symbol_aliases",
                {
                    "path": str(alias_path),
                    "confirmed_only": True,
                    "expected_sha256": _file_sha256(alias_path),
                },
                timeout=60.0,
            )
        unresolved = service.unresolved(capture_id)
        return _ok(
            {
                "schema": "renderdoc-ultra-resolve-result/v1",
                "databasePath": str(service.database_path),
                "captureExport": exported,
                "scan": run["scan"],
                "match": run["match"],
                "aliasTable": alias_result,
                "applied": applied,
                "unresolved": unresolved,
                "assetShaderRecovery": asset_recovery,
            }
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def get_symbol_match_cases(database_path: str, capture_id: str) -> str:
    """Return only unresolved candidates and their evidence for AI review.

    AI must select a listed ``candidateId``; it may not invent offsets, hashes,
    register bindings, or asset identities.
    """
    try:
        return _ok(SymbolService(database_path).unresolved(capture_id))
    except Exception as error:
        return _err(error)


@mcp.tool()
def get_symbol_match_evidence(
    database_path: str,
    capture_id: str,
    shader_resource_id: int,
    stage: str,
    entry_point: str,
    candidate_id: int = 0,
    top_k: int = 10,
) -> str:
    """Get bounded file strings, asset metadata, runtime layout and hash evidence.

    This fixed program reads at most 512 KiB around an embedded shader and never
    executes asset content. Use it before an AI proposes a candidate.
    """
    try:
        return _ok(
            SymbolService(database_path).evidence(
                capture_id,
                shader_resource_id,
                stage,
                entry_point,
                candidate_id=candidate_id or None,
                top_k=top_k,
            )
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def list_symbol_matches(database_path: str, capture_id: str) -> str:
    """List all deterministic and reviewed candidate associations for a capture."""
    try:
        return _ok(SymbolService(database_path).list_matches(capture_id))
    except Exception as error:
        return _err(error)


@mcp.tool()
def submit_symbol_match(
    database_path: str,
    capture_id: str,
    scan_id: str,
    shader_resource_id: int,
    stage: str,
    entry_point: str,
    candidate_id: int,
    confidence: float,
    rationale: str,
    evidence_id: str,
) -> str:
    """Submit one AI candidate selection for deterministic re-validation.

    A model can only select a candidate previously returned by
    get_symbol_match_cases and must cite its evidence_id. Model/MCP input is not
    a user-authority boundary, so every AI-resolved ambiguity remains provisional
    and is never exported or applied. Rejected physical evidence cannot be forced.
    """
    try:
        service = SymbolService(database_path)
        return _ok(
            service.submit_decision(
                capture_id,
                scan_id,
                shader_resource_id,
                stage,
                entry_point,
                candidate_id,
                confidence,
                rationale,
                evidence_id,
            )
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def export_symbol_alias_table(
    database_path: str,
    capture_id: str,
    output_path: str,
    scan_id: str = "",
) -> str:
    """Export only program-verified, capture-bound aliases for RenderDoc."""
    try:
        return _ok(
            SymbolService(database_path).export_alias_table(
                capture_id,
                output_path,
                verified_only=True,
                scan_id=scan_id,
            )
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def apply_symbol_alias_table(
    database_path: str,
    capture_id: str,
    scan_id: str = "",
) -> str:
    """Re-export verified DB aliases and load those exact bytes into RenderDoc.

    An MCP caller cannot provide an alias-manifest path or self-declare
    ``programVerified``. The service re-derives the table from its local symbol
    database, chooses the output path, and binds the exact file digest into the
    bridge request. This protects the model/MCP trust boundary; it is not a
    defence against a malicious same-user process that controls the IPC folder.
    """
    try:
        service = SymbolService(database_path)
        output_dir = service.database_path.parent / "applied-aliases"
        output_name = hashlib.sha256(capture_id.encode("utf-8")).hexdigest()
        alias_path = output_dir / (output_name + ".v1.json")
        exported = service.export_alias_table(
            capture_id,
            alias_path,
            verified_only=True,
            scan_id=scan_id,
        )
        if (
            not isinstance(exported, dict)
            or exported.get("schema") != "renderdoc-ultra-aliases/v1"
            or exported.get("schemaVersion") != 1
            or exported.get("verifiedOnly") is not True
            or exported.get("captureId") != capture_id
            or Path(str(exported.get("path", "") or "")).resolve()
            != alias_path.resolve()
            or type(exported.get("aliasCount")) is not int
            or exported["aliasCount"] < 0
        ):
            raise RuntimeError("verified alias export returned invalid policy evidence")
        expected_sha256 = _file_sha256(alias_path)
        applied = ipc.call(
            "load_symbol_aliases",
            {
                "path": str(alias_path),
                "confirmed_only": True,
                "expected_sha256": expected_sha256,
                "strict_atomic": True,
            },
            timeout=60.0,
        )
        counts = applied.get("counts") if isinstance(applied, dict) else None
        count_names = (
            "manifestAliases",
            "deterministicAliases",
            "liveBoundAliases",
            "activeValidatedAliases",
            "renamedShaders",
            "skipped",
            "conflicts",
        )
        valid_counts = isinstance(counts, dict) and all(
            type(counts.get(field)) is int and counts[field] >= 0
            for field in count_names
        )
        applied_session_id = str(
            applied.get("captureSessionId", "") if isinstance(applied, dict) else ""
        )
        applied_items = applied.get("applied") if isinstance(applied, dict) else None
        applied_resource_ids = (
            [item.get("resourceId") for item in applied_items]
            if isinstance(applied_items, list)
            and all(isinstance(item, dict) for item in applied_items)
            else []
        )
        valid_applied_items = bool(
            isinstance(applied_items, list)
            and len(applied_items) <= 100000
            and all(
                set(item) == {"resourceId", "displayName"}
                and type(item.get("resourceId")) is int
                and item["resourceId"] > 0
                and isinstance(item.get("displayName"), str)
                and bool(item["displayName"].strip())
                and len(item["displayName"]) <= 512
                for item in applied_items
            )
            and len(applied_resource_ids) == len(set(applied_resource_ids))
        )
        if (
            not isinstance(applied, dict)
            or applied.get("schema") != "renderdoc-ultra-aliases/v1"
            or applied.get("schemaVersion") != 1
            or applied.get("captureId") != capture_id
            or not _is_lower_hex_value(applied_session_id, 32)
            or Path(str(applied.get("path", "") or "")).resolve()
            != alias_path.resolve()
            or applied.get("confirmedOnly") is not True
            or applied.get("strictAtomic") is not True
            or applied.get("atomicity") != "complete-or-recovery-required"
            or applied.get("physicalAtomic") is not False
            or applied.get("recoveryRequired") is not False
            or applied.get("rollbackAvailable") is not True
            or applied.get("recoveryLedgerPublished") is not True
            or not _is_lower_hex_value(
                str(applied.get("transactionId", "") or ""), 32
            )
            or type(applied.get("recoveryResourceCount")) is not int
            or applied.get("recoveryResourceCount") < 0
            or applied.get("committed") is not True
            or not valid_counts
            or counts["skipped"] != 0
            or counts["conflicts"] != 0
            or counts["manifestAliases"]
            != counts["deterministicAliases"]
            or counts["manifestAliases"] != counts["liveBoundAliases"]
            or counts["manifestAliases"] != counts["activeValidatedAliases"]
            or counts["manifestAliases"] != exported["aliasCount"]
            or not valid_applied_items
            or len(applied_items) != counts["renamedShaders"]
            or applied.get("recoveryResourceCount") < counts["renamedShaders"]
            or applied.get("skipped") != []
            or applied.get("conflicts") != []
        ):
            raise RuntimeError(
                "RenderDoc did not prove a complete alias commit with a "
                "capture-bound recovery ledger"
            )
        return _ok(
            {
                "schema": "renderdoc-ultra-alias-apply/v1",
                "aliasTable": exported,
                "manifestSha256": expected_sha256,
                "applied": applied,
            }
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def rollback_symbol_alias_table(
    capture_id: str, expected_transaction_id: str = ""
) -> str:
    """Restore and verify all aliases owned by the exact active capture.

    The caller supplies no path or resource list. The bridge accepts only its
    own bounded recovery ledger (or a machine-verifiable already-restored
    state) for the matching capture identity.
    """
    try:
        capture_id = str(capture_id or "").strip().lower()
        if not _is_lower_hex_value(capture_id, 64):
            raise ValueError("capture_id must be a lowercase SHA-256")
        transaction = str(expected_transaction_id or "").strip().lower()
        params = {"expected_capture_id": capture_id}
        if transaction:
            params["expected_transaction_id"] = _strict_lower_hex(
                transaction, 32, "expected_transaction_id"
            )
        rollback = ipc.call("rollback_symbol_aliases", params, timeout=60.0)
        session_id = str(
            rollback.get("captureSessionId", "") if isinstance(rollback, dict) else ""
        )
        transaction_id = str(
            rollback.get("transactionId", "") if isinstance(rollback, dict) else ""
        )
        if (
            not isinstance(rollback, dict)
            or rollback.get("schema") != "renderdoc-ultra-alias-rollback/v1"
            or rollback.get("schemaVersion") != 1
            or str(rollback.get("captureId", "") or "").casefold() != capture_id
            or not _is_lower_hex_value(session_id, 32)
            or not _is_lower_hex_value(transaction_id, 32)
            or (transaction and transaction_id != transaction)
            or rollback.get("status") not in {"restored", "already-restored"}
            or rollback.get("restored") is not True
            or rollback.get("alreadyRestored") is not (
                rollback.get("status") == "already-restored"
            )
            or rollback.get("verified") is not True
            or rollback.get("strictReceipt") is not True
            or rollback.get("captureTokenVerified") is not True
            or rollback.get("originalNamesVerified") is not True
            or rollback.get("physicalAtomic") is not False
            or rollback.get("recoveryRequired") is not False
            or rollback.get("overlayRestored") is not True
            or rollback.get("renameLedgerRestored") is not True
            or type(rollback.get("resourceCount")) is not int
            or rollback["resourceCount"] < 0
            or rollback.get("failures") != []
        ):
            raise RuntimeError(
                "RenderDoc did not return a strict capture-bound alias rollback receipt"
            )
        return _ok(rollback)
    except Exception as error:
        return _err(error)


# ==================== 15. EXTERNAL SHADER TOOLCHAINS / WORKSPACE ====================

@mcp.tool()
def get_shader_toolchain_capabilities(stage: str) -> str:
    """Report hash-verified format routes and validators for one shader stage.

    Executables, arguments, and both config paths are fixed by the server
    process owner. This read-only probe performs fresh SHA-256 checks but does
    not run a compiler/decompiler and does not modify RenderDoc replay state.
    """
    try:
        return _ok(_shader_toolchain_catalog().diagnostic_matrix(stage))
    except Exception as error:
        return _shader_err(error, "capability-probe")


@mcp.tool()
def plan_shader_toolchain_route(
    shader_path: str,
    stage: str,
    editable_format: str = "",
    capture_encoding: str = "",
    target_api: str = "",
    allow_reconstructed: bool = True,
) -> str:
    """Deterministically plan captured binary -> editable text -> same binary.

    The content is detected and cross-checked with optional capture metadata.
    Exactly tied best routes, format/API contradictions, missing physical target
    profiles, and unavailable hash-pinned operations all fail closed. Returned
    reconstructed source capabilities are candidates, never original-source or
    semantic-equivalence claims.
    """
    try:
        if not isinstance(allow_reconstructed, bool):
            raise ValueError("allow_reconstructed must be a boolean")
        result = _shader_toolchain_catalog().select_round_trip_for_file(
            shader_path,
            stage,
            editable_format=editable_format or None,
            capture_encoding=capture_encoding or None,
            target_api=target_api or None,
            allow_reconstructed=allow_reconstructed,
        )
        return _ok(result)
    except Exception as error:
        return _shader_err(error, "route-plan")


@mcp.tool()
def plan_shader_recovery(
    shader_path: str,
    stage: str,
    editable_format: str = "",
    capture_encoding: str = "",
    target_api: str = "",
    allow_reconstructed: bool = True,
) -> str:
    """Plan deterministic fallback from captured binary to a safe workspace.

    The result distinguishes a full round trip, a compile-only recovery lane,
    and evidence-only handling. It enumerates only process-owned, hash-verified
    route IDs and finite actions. AI may choose one emitted action and may author
    provisional source only when the plan proves a fixed reverse compiler route;
    it can never supply a command/executable or authorize replay apply.
    """

    try:
        if not isinstance(allow_reconstructed, bool):
            raise ValueError("allow_reconstructed must be a boolean")
        result = _shader_toolchain_catalog().plan_recovery_for_file(
            shader_path,
            stage,
            editable_format=editable_format or None,
            capture_encoding=capture_encoding or None,
            target_api=target_api or None,
            allow_reconstructed=allow_reconstructed,
        )
        memory = _shader_route_memory()
        if memory is not None and isinstance(result, dict):
            from .shader_workspace.route_memory import route_key

            key = route_key(
                target_api=result.get("targetApi") or target_api,
                capture_encoding=result.get("captureEncoding") or capture_encoding,
                stage=result.get("stage") or stage,
                target_profile=result.get("targetProfile"),
                editable_format=result.get("editableFormat"),
            )
            result, memo = memory.prefer(result, key)
            result["routeMemory"] = memo
        return _ok(result)
    except Exception as error:
        return _shader_err(error, "recovery-plan")


@mcp.tool()
def run_shader_toolchain_round_trip_smoke(
    shader_path: str,
    output_directory: str,
    stage: str,
    editable_format: str,
    entry_point: str = "main",
    capture_encoding: str = "",
    target_api: str = "",
    allow_reconstructed: bool = True,
) -> str:
    """Run a strict format round trip with fixed, hash-pinned local tools.

    The output directory must not already exist. The input's DirectX target
    profile is parsed from its physical executable token and cannot be supplied
    or overridden by the caller. API-sensitive routes also require the target
    API exported by capture metadata; SPIR-V API/environment is never inferred
    from its module header. Success proves process/output-format checks and
    configured validators only; semantic and RenderDoc replay ABI fields remain
    false until their independent gates run.
    """
    try:
        if not isinstance(allow_reconstructed, bool):
            raise ValueError("allow_reconstructed must be a boolean")
        result = _shader_toolchain_catalog().round_trip_smoke(
            shader_path,
            editable_format,
            output_directory,
            stage,
            entry_point,
            input_format_hint=capture_encoding or None,
            target_api=target_api or None,
            allow_reconstructed=allow_reconstructed,
        )
        return _ok(result)
    except Exception as error:
        return _shader_err(error, "round-trip-smoke")

@mcp.tool()
def create_shader_workspace(
    catalog_path: str,
    shader_key: str,
    workspace_path: str,
    decompiler_tool_id: str = "",
    compiler_tool_id: str = "",
    editable_format: str = "",
) -> str:
    """Create an isolated workspace from one exact capture-catalog shader.

    The executable, fixed-file hashes, argument templates, timeouts, and
    environment policy come only from the server process's trusted local tool
    registry. An MCP caller can select registered tool IDs but cannot provide a
    command, executable, argument list, environment, or registry path.

    This operation copies and hashes the captured baseline. It neither
    decompiles nor changes replay state.

    Args:
        catalog_path: shader_catalog.v1.json exported by the RenderDoc bridge.
        shader_key: Exact catalog shaderKey string.
        workspace_path: New directory to create; an existing path is rejected.
        decompiler_tool_id: Optional legacy registered decompiler tool ID. Leave
            both tool IDs empty to bind the complete trusted automatic route.
        compiler_tool_id: Optional legacy registered compiler tool ID. Leave
            both tool IDs empty to bind the complete trusted automatic route.
        editable_format: Optional routed text format. Empty prefers HLSL for
            DirectX and GLSL for SPIR-V, then selects a complete same-family
            DXBC/DXIL or SPIR-V instruction/IR round trip only when the preferred
            lane is statically incomplete during planning. Native SPIR-V
            assembly additionally requires one integrity-valid, unambiguous
            spirv-val binding and successful baseline/candidate validation. It
            does not cross formats after a selected high-level route fails at
            runtime. An explicit value never switches formats behind the caller.
    """
    try:
        _enforce_active_project_workspace_location(
            Path(workspace_path).expanduser().resolve(), operation="create_shader_workspace"
        )
        result = _shader_workspace_service().create(
            catalog_path,
            shader_key,
            workspace_path,
            decompiler_tool_id or None,
            compiler_tool_id or None,
            editable_format=editable_format,
        )
        return _shader_workspace_response("create", result)
    except Exception as error:
        return _shader_err(error, "workspace-create")


class ShaderWorkspaceLocationError(RuntimeError):
    """A Shader workspace was requested outside the active project."""


def _enforce_active_project_workspace_location(
    workspace: Path, *, operation: str, source_workspace: Path | None = None
) -> dict[str, Any] | None:
    """With an active project, every Shader workspace lives under its root.

    A workspace created elsewhere (for example under ``%LOCALAPPDATA%``) can be
    compiled but never registered, restored or reviewed as project knowledge,
    which silently forces a second full pass later.  Refuse early and name the
    right place.  Without an active project nothing is enforced.
    """

    try:
        status = _active_project_service().status()
    except Exception:
        return None
    paths = status.get("paths")
    if not isinstance(paths, dict) or not paths.get("shaderWorkspaces"):
        return None
    root = Path(str(paths["shaderWorkspaces"])).expanduser().resolve()
    if is_within(root, workspace):
        return {"activeProject": status.get("name"), "workspaceRoot": str(root)}
    if source_workspace is not None and is_within(root, source_workspace.resolve()):
        suggestion = str(source_workspace.resolve().parent / workspace.name)
        hint = "place recovery attempts next to their source workspace"
    else:
        suggestion = str(root / workspace.name)
        hint = (
            "call get_active_shader_workspace_path for the deterministic managed path, "
            "or use a directory under the project Shader workspace root"
        )
    raise ShaderWorkspaceLocationError(
        "%s refused: the active project '%s' owns Shader workspaces under %s, but %s was "
        "requested. Workspaces outside the project cannot be registered, restored or "
        "reviewed, so %s (for example %s)."
        % (operation, status.get("name"), root, workspace, hint, suggestion)
    )


def _shader_recovery_reconstruction_receipt(
    created: Mapping[str, Any],
    execution: Mapping[str, Any],
    representation: Mapping[str, Any],
    workspace_status: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Bind an internally completed recovery decompile to exact Host state."""

    if execution.get("succeeded") is not True:
        return None
    decompiled = execution.get("result")
    recovery_attempt = created.get("recoveryAttempt")
    route_plan = created.get("routePlan")
    status_route_plan = workspace_status.get("routePlan")
    source = workspace_status.get("source")
    capture = workspace_status.get("capture")
    baseline = workspace_status.get("baseline")
    shader = workspace_status.get("shader")
    if not all(
        isinstance(value, Mapping)
        for value in (
            decompiled,
            recovery_attempt,
            route_plan,
            status_route_plan,
            source,
            capture,
            baseline,
            shader,
        )
    ):
        raise RuntimeError(
            "successful Shader recovery omitted its strict reconstruction state"
        )
    workspace_path = str(Path(created.get("workspacePath", "")).expanduser().resolve())
    source_path = str(Path(decompiled.get("sourcePath", "")).expanduser().resolve())
    source_sha256 = str(decompiled.get("sourceSha256", "") or "").strip().lower()
    source_revision = decompiled.get("revision")
    workspace_id = str(created.get("workspaceId", "") or "").strip().lower()
    source_workspace_id = str(
        recovery_attempt.get("sourceWorkspaceId", "") or ""
    ).strip().lower()
    source_plan_sha256 = str(
        recovery_attempt.get("sourceRecoveryPlanSha256", "") or ""
    ).strip().lower()
    target_plan_sha256 = str(route_plan.get("planSha256", "") or "").strip().lower()
    baseline_raw_sha256 = str(
        created.get("baselineRawSha256", "") or ""
    ).strip().lower()
    capture_id = str(created.get("captureId", "") or "").strip().lower()
    decompile_index = recovery_attempt.get("decompileCandidateIndex")
    compile_index = recovery_attempt.get("compileCandidateIndex")
    if not (
        execution.get("attempted") is True
        and _is_lower_hex_value(workspace_id, 32)
        and _is_lower_hex_value(source_workspace_id, 32)
        and _is_lower_hex_value(source_plan_sha256, 64)
        and _is_lower_hex_value(target_plan_sha256, 64)
        and _is_lower_hex_value(baseline_raw_sha256, 64)
        and _is_lower_hex_value(capture_id, 64)
        and _is_lower_hex_value(source_sha256, 64)
        and type(decompile_index) is int
        and decompile_index >= 0
        and type(compile_index) is int
        and compile_index >= 0
        and type(source_revision) is int
        and source_revision >= 0
        and _capture_path_key(decompiled.get("workspacePath"))
        == _capture_path_key(workspace_path)
        and _capture_path_key(workspace_status.get("workspacePath"))
        == _capture_path_key(workspace_path)
        and workspace_status.get("workspaceId") == workspace_id
        and workspace_status.get("revision") == source_revision
        and recovery_attempt.get("targetWorkspaceId") == workspace_id
        and recovery_attempt.get("targetRoutePlanSha256") == target_plan_sha256
        and status_route_plan.get("planSha256") == target_plan_sha256
        and capture.get("captureId") == capture_id
        and baseline.get("rawSha256") == baseline_raw_sha256
        and decompiled.get("baselineCompileVerified") is True
        and decompiled.get("requiresReplayAbiValidation") is True
        and source.get("present") is True
        and _capture_path_key(source.get("path")) == _capture_path_key(source_path)
        and source.get("currentSha256") == source_sha256
        and source.get("decompiledSha256") == source_sha256
        and source.get("modifiedSinceDecompile") is False
        and source.get("lane") == "executable-editable"
        and source.get("origin") == "trusted-decompile-route"
        and source.get("compileEligible") is True
        and source.get("baselineCompileVerified") is True
        and source.get("semanticEquivalenceVerified") is False
        and representation.get("present") is True
        and representation.get("lane") == "executable-editable"
        and _capture_path_key(representation.get("path"))
        == _capture_path_key(source_path)
        and representation.get("sha256") == source_sha256
        and representation.get("compileEligible") is True
        and representation.get("baselineCompileVerified") is True
        and representation.get("semanticEquivalenceVerified") is False
    ):
        raise RuntimeError(
            "successful Shader recovery reconstruction receipt is inconsistent"
        )
    return {
        "schema": "renderdoc-ultra-shader-recovery-reconstruction/v1",
        "schemaVersion": 1,
        "status": "succeeded",
        "sourceWorkspacePath": recovery_attempt.get("sourceWorkspacePath"),
        "workspacePath": workspace_path,
        "sourceWorkspaceId": source_workspace_id,
        "workspaceId": workspace_id,
        "sourceRecoveryPlanSha256": source_plan_sha256,
        "targetRoutePlanSha256": target_plan_sha256,
        "baselineRawSha256": baseline_raw_sha256,
        "captureId": capture_id,
        "shaderResourceId": shader.get("resourceId"),
        "stage": shader.get("stage"),
        "entryPoint": shader.get("entryPoint"),
        "decompileCandidateIndex": decompile_index,
        "compileCandidateIndex": compile_index,
        "sourceRevision": source_revision,
        "sourcePath": source_path,
        "sourceSha256": source_sha256,
        "representationLane": "executable-editable",
        "representationSha256": source_sha256,
        "baselineCompileVerified": True,
    }


@mcp.tool()
def try_shader_workspace_recovery_candidate(
    source_workspace_path: str,
    workspace_path: str,
    expected_recovery_plan_sha256: str,
    expected_source_workspace_id: str,
    expected_baseline_raw_sha256: str,
    expected_capture_id: str,
    decompile_candidate_index: int | None = None,
    compile_candidate_index: int = 0,
) -> str:
    """Try one program-listed alternative toolchain in a new workspace.

    The indices address only ``recoveryPlan.decompileCandidates`` and
    ``compileCandidates`` from the source workspace. The expected workspace,
    baseline raw and capture identities prevent a plan hash from being reused
    for another Shader. A null decompile index is accepted only when the plan
    has no decompile candidates. The server revalidates the current fixed route
    catalog, policy and executable hashes, creates a separate attempt, and runs
    the selected route once. Callers and AI cannot provide route IDs, commands,
    executables, arguments, or environment values. Failure produces a
    non-executable explanatory/binary representation in the new workspace; it
    never mutates or applies replay state.
    """

    try:
        service = _shader_workspace_service()
        _enforce_active_project_workspace_location(
            Path(workspace_path).expanduser().resolve(),
            operation="try_shader_workspace_recovery_candidate",
            source_workspace=Path(source_workspace_path).expanduser(),
        )
        created = service.create_recovery_attempt(
            source_workspace_path,
            workspace_path,
            expected_recovery_plan_sha256=expected_recovery_plan_sha256,
            expected_source_workspace_id=expected_source_workspace_id,
            expected_baseline_raw_sha256=expected_baseline_raw_sha256,
            expected_capture_id=expected_capture_id,
            decompile_candidate_index=decompile_candidate_index,
            compile_candidate_index=compile_candidate_index,
        )
        route_plan = created.get("routePlan")
        decompile_path = (
            route_plan.get("decompile")
            if isinstance(route_plan, Mapping)
            else None
        )
        execution: dict[str, Any] = {
            "attempted": bool(decompile_path),
            "succeeded": False,
            "result": None,
        }
        if decompile_path:
            try:
                execution["result"] = service.decompile(workspace_path)
                execution["succeeded"] = True
            except Exception as error:
                # The isolated target and its tool job/history are intentional
                # recovery evidence.  Keep them inspectable so the caller can
                # select another finite candidate without confusing this
                # failure with the source workspace.
                execution["error"] = str(error)
                execution["type"] = type(error).__name__
                execution["diagnostic"] = classify_shader_failure(
                    error, phase="recovery-decompile-or-baseline-compile-probe"
                )
                _remember_route_decompile_failure(workspace_path, error)
        else:
            execution["reason"] = (
                "the selected recovery plan has no decompile route; a reviewed "
                "source must be promoted before compilation"
            )
        representation = service.representation(
            workspace_path,
            # A failed candidate was already dispatched exactly once above.
            # Materialize its bounded fallback without replaying the same
            # external process under the guise of representation lookup.
            attempt_decompile=execution["succeeded"],
        )
        workspace_status = service.status(workspace_path)
        # A failed external route is still a valid, inspectable attempt result;
        # it is not a successful reconstruction receipt.  Keep its jobs,
        # diagnostics and fallback representation available so orchestration
        # can advance to another listed pair without turning the whole tool
        # response into an invalid-receipt exception.
        reconstruction_receipt = None
        if execution["succeeded"]:
            reconstruction_receipt = _shader_recovery_reconstruction_receipt(
                created,
                execution,
                representation,
                workspace_status,
            )
        return _shader_workspace_response(
            "recovery-candidate-attempt",
            {
                "sourceWorkspacePath": str(
                    Path(source_workspace_path).expanduser().resolve()
                ),
                "workspacePath": str(Path(workspace_path).expanduser().resolve()),
                "created": created,
                "execution": execution,
                "representation": representation,
                "workspaceStatus": workspace_status,
                "reconstructionReceipt": reconstruction_receipt,
                "applied": False,
            },
        )
    except Exception as error:
        return _shader_err(error, "recovery-candidate-attempt")


@mcp.tool()
def prepare_shader_workspace(
    event_id: int,
    stage: str,
    workspace_path: str,
    catalog_output_dir: str = "",
    editable_format: str = "",
    expected_resource_id: int | None = None,
    expected_entry_point: str = "",
    expected_raw_sha256: str = "",
    expected_capture_id: str = "",
    expected_capture_session_id: str = "",
    admit_existing: bool = False,
) -> str:
    """Export, uniquely select, create, and represent one event/stage shader.

    The bridge export is restricted to exactly ``event_id`` and ``stage``.
    Preparation fails closed unless the resulting catalog has exactly one
    physically verified shader use for that pair. Missing embedded or original
    HLSL is expected and never requires user clarification: preparation starts
    one registered reconstruction attempt immediately whenever one exists.
    Empty format selection prefers HLSL, then selects a complete same-family
    native assembly/IR lane when the high-level lane is statically unavailable.
    SPIR-V assembly additionally requires one integrity-valid, unambiguous
    spirv-val binding and actual baseline/candidate validation. An explicit
    format is never changed.

    A runtime failure is returned as a conclusive observation together with the
    complete hash-bound candidate inventories. This tool does not traverse or
    rank the remaining compiler/decompiler pairs. The Agent owns the next
    technical decision and may call ``try_shader_workspace_recovery_candidate``
    with any still-untried registered pair, generate a reviewed provisional
    source, change representation, or choose another available capability.
    Assembly remains instruction text, not reconstructed HLSL. When the first
    reconstruction route fails but a fixed compiler exists, this operation
    publishes RenderDoc disassembly and a hash-bound provisional candidate path
    for Agent reconstruction; promotion, compilation and fresh replay ABI
    validation remain separate gates. If no reconstruction/compile route is
    available, the tool still publishes
    RenderDoc disassembly
    (explanatory-ir) or a hash-bound captured binary view (binary-only), never
    an unexplained blank editor. Only the executable-editable lane may later
    compile; nothing is applied here.

    Args:
        event_id: Draw/dispatch event containing the bound shader.
        stage: Shader stage, for example vertex, fragment/pixel, or compute.
        workspace_path: New workspace directory to create.
        catalog_output_dir: Optional export directory. If empty, a unique sibling
            directory is generated beside the workspace.
        editable_format: Explicit text target. Empty lets fixed program routing
            prefer HLSL/GLSL and then select a complete same-family native
            assembly/IR round trip when the high-level route is statically
            unavailable. SPIR-V native assembly is eligible only with its strict
            spirv-val gate. Missing original HLSL starts this routing directly.
            Runtime route failures stay in the selected editable format and
            expose the registered candidate inventories for Agent-owned
            replanning; an explicit value never changes the emitted editable
            format behind the caller.
        expected_resource_id: Optional frozen captured Shader ResourceId.
        expected_entry_point: Optional frozen captured entry point.
        expected_raw_sha256: Optional frozen original executable SHA-256.
        expected_capture_id: Optional frozen catalog SHA-256.
        expected_capture_session_id: Optional frozen loaded-capture lifecycle ID.
        admit_existing: Re-admit an existing active-project deterministic
            workspace without overwriting its source or compiled revisions.
    """
    try:
        event_value = int(event_id)
        if event_value <= 0:
            raise ValueError("event_id must be a positive integer")
        if not str(stage or "").strip():
            raise ValueError("stage is required")
        if type(admit_existing) is not bool:
            raise ValueError("admit_existing must be a boolean")
        # Validate the immutable tool/route catalogs before exporting any
        # capture bytes. A configuration error must remain a zero-effect
        # capability failure, not leave a catalog directory that later causes
        # the Agent to infer an uncertain workspace mutation.
        service = _shader_workspace_service()
        workspace = Path(workspace_path).expanduser().resolve()
        active_binding = _active_capture_catalog_binding()
        if active_binding is not None and catalog_output_dir:
            raise RuntimeError(
                "an active-project Shader cannot bypass its full catalog or "
                "deterministic managed workspace with catalog_output_dir"
            )
        if active_binding is None:
            _enforce_active_project_workspace_location(
                workspace, operation="prepare_shader_workspace"
            )
        cached_selection = (
            None
            if catalog_output_dir
            else _active_capture_catalog_selection(event_value, stage)
        )
        expected_binding_sha256 = ""
        if cached_selection is not None:
            (
                exported,
                catalog_path,
                selected,
                expected_binding_sha256,
            ) = cached_selection
        elif catalog_output_dir:
            catalog_dir = Path(catalog_output_dir).expanduser().resolve()
        else:
            catalog_dir = (
                workspace.parent
                / ".renderdoc-ultra-catalogs"
                / (
                    "%s-e%d-%s"
                    % (workspace.name, event_value, uuid.uuid4().hex)
                )
            ).resolve()
        if cached_selection is None:
            exported = ipc.call(
                "export_shader_catalog",
                {
                    "output_dir": str(catalog_dir),
                    "event_ids": [event_value],
                    "stages": [stage],
                },
                timeout=300.0,
            )
            if not isinstance(exported, dict):
                raise RuntimeError("RenderDoc bridge returned an invalid catalog result")
            catalog_path = exported.get("catalogPath") or exported.get("path")
            if not catalog_path:
                raise RuntimeError("RenderDoc bridge did not return catalogPath")
            selected = select_unique_shader_for_use(
                catalog_path, event_value, stage
            )
        capture_session_id = _strict_lower_hex(
            exported.get("captureSessionId"),
            32,
            "catalog export captureSessionId",
        )
        expected_raw = str(expected_raw_sha256 or "").strip().lower()
        expected_entry = str(expected_entry_point or "")
        expected_capture = str(expected_capture_id or "").strip().lower()
        expected_session = str(expected_capture_session_id or "").strip().lower()
        if expected_resource_id is not None and (
            isinstance(expected_resource_id, bool)
            or not isinstance(expected_resource_id, int)
            or expected_resource_id <= 0
            or selected.key.resource_id != expected_resource_id
        ):
            raise RuntimeError(
                "selected Shader ResourceId does not match expected_resource_id"
            )
        if expected_entry and selected.key.entry_point != expected_entry:
            raise RuntimeError(
                "selected Shader entry point does not match expected_entry_point"
            )
        if expected_raw and (
            _strict_lower_hex(expected_raw, 64, "expected_raw_sha256")
            != selected.key.raw_sha256
        ):
            raise RuntimeError(
                "selected Shader bytes do not match expected_raw_sha256"
            )
        if expected_capture and (
            _strict_lower_hex(expected_capture, 64, "expected_capture_id")
            != selected.capture_id
        ):
            raise RuntimeError(
                "selected Shader catalog does not match expected_capture_id"
            )
        if expected_session and (
            _strict_lower_hex(
                expected_session, 32, "expected_capture_session_id"
            )
            != capture_session_id
        ):
            raise RuntimeError(
                "selected Shader lifecycle does not match expected_capture_session_id"
            )
        if active_binding is not None and not catalog_output_dir:
            # A managed active-project workspace has one deterministic path for
            # the exact catalog revision, route policy, stage, ResourceId and
            # original bytes.  Recompute it here instead of trusting a caller-
            # supplied path that merely happens to sit below the project root.
            (
                unused_active_context,
                active_status,
                active_record,
                unused_active_catalog,
            ) = active_binding
            deterministic_workspace = _active_project_service().shader_workspace_path(
                capture_id=selected.capture_id,
                catalog_sha256=_strict_lower_hex(
                    active_record.get("catalogSha256"),
                    64,
                    "active catalogSha256",
                ),
                route_binding_sha256=_active_shader_route_binding_sha256(),
                stage=selected.key.stage,
                shader_resource_id=selected.key.resource_id,
                raw_sha256=selected.key.raw_sha256,
                create=False,
            ).resolve()
            if workspace != deterministic_workspace:
                raise RuntimeError(
                    "workspace_path is not the active project's deterministic "
                    "path for the exact selected Shader"
                )
            if not is_within(
                _active_project_workspace_root(active_status), workspace
            ):
                raise RuntimeError(
                    "deterministic Shader workspace escaped the active project"
                )
        reused_existing = workspace.exists() or workspace.is_symlink()
        if reused_existing:
            if not admit_existing:
                raise RuntimeError(
                    "workspace already exists; set admit_existing only for an "
                    "active-project deterministic workspace"
                )
            if catalog_output_dir or active_binding is None:
                raise RuntimeError(
                    "existing workspace admission requires the active project's "
                    "verified full capture catalog"
                )
            unused_context, active_status, unused_record, unused_catalog = active_binding
            if not is_within(_active_project_workspace_root(active_status), workspace):
                raise RuntimeError(
                    "existing workspace is outside the active project workspace root"
                )
            _ensure_workspace_capture_catalog_identity(str(workspace))
            created = service.status(workspace)
            existing_shader = created.get("shader")
            existing_capture = created.get("capture")
            existing_baseline = created.get("baseline")
            existing_route_plan = created.get("routePlan")
            if (
                created.get("schema")
                != "renderdoc-ultra-shader-workspace-status/v1"
                or not isinstance(existing_shader, dict)
                or not isinstance(existing_capture, dict)
                or not isinstance(existing_baseline, dict)
                or not isinstance(existing_route_plan, dict)
                or existing_route_plan.get("schema")
                != "renderdoc-ultra-shader-workspace-route-plan/v2"
                or existing_route_plan.get("schemaVersion") != 2
                or _capture_path_key(created.get("workspacePath"))
                != _capture_path_key(str(workspace))
                or created.get("workspaceId") is None
                or existing_capture.get("captureId") != selected.capture_id
                or existing_shader.get("resourceId") != selected.key.resource_id
                or normalize_stage(existing_shader.get("stage"))
                != selected.key.stage
                or existing_shader.get("entryPoint")
                != selected.key.entry_point
                or existing_shader.get("rawSha256")
                != selected.key.raw_sha256
                or existing_baseline.get("rawSha256")
                != selected.key.raw_sha256
            ):
                raise RuntimeError(
                    "existing workspace does not match the exact selected Shader"
                )
            created = dict(created)
            created["admittedExisting"] = True
            created["sourcePreserved"] = True
        else:
            created = service.create(
                catalog_path,
                selected.key.catalog_key,
                workspace,
                editable_format=editable_format,
            )
            created["admittedExisting"] = False
            created["sourcePreserved"] = False
        project_source_reuse = None
        if (active_binding is not None and active_binding[1].get("scopeConfigured") is True
                and created.get("routePlan", {}).get("editableFormat") == "hlsl"):
            fresh_status = service.status(workspace)
            if fresh_status.get("source", {}).get("present") is False:
                project_replay = _active_project_service().replay_service()
                reuse = project_replay.reuse_plan(selected.capture_id, fresh_status["capture"]["catalogSha256"])
                candidates = [
                    candidate for match in reuse.get("matches", [])
                    if (match.get("shader", {}).get("resourceId") == selected.key.resource_id
                        and match.get("shader", {}).get("stage") == selected.key.stage
                        and match.get("shader", {}).get("entryPoint", "") == selected.key.entry_point
                        and match.get("shader", {}).get("rawSha256") == selected.key.raw_sha256)
                    for candidate in match.get("workspaceCandidates", [])
                    if candidate.get("sourceSeedEligible") is True
                    and candidate.get("activeCandidate") is True
                    and candidate.get("matchMode") == "raw-exact"
                ]
                if len(candidates) == 1:
                    bundle = project_replay.prepare_workspace_source(candidates[0]["knowledgeId"], fresh_status)
                    project_source_reuse = service.import_project_source(workspace, bundle)
        representation = service.representation(workspace)
        reconstruction_receipt = None
        provisional_reconstruction = None
        if representation.get("lane") != "executable-editable":
            try:
                bridge_representation = ipc.call(
                    "reverse_shader",
                    {"event_id": event_value, "stage": stage},
                    timeout=300.0,
                )
                if not isinstance(bridge_representation, dict):
                    bridge_representation = {
                        "error": "RenderDoc bridge returned an invalid reverse_shader result"
                    }
            except Exception as error:
                bridge_representation = {"error": str(error)}
            representation = service.representation(
                workspace,
                bridge_response=bridge_representation,
                bridge_event_id=event_value,
            )
            status_for_provisional = service.status(workspace)
            recovery_for_provisional = status_for_provisional.get("recoveryPlan")
            if (
                isinstance(recovery_for_provisional, Mapping)
                and recovery_for_provisional.get("sourceConstructionEligible") is True
            ):
                provisional_reconstruction = (
                    service.prepare_provisional_reconstruction(workspace)
                )
        if exported.get("reusedActiveProjectCatalog") is True:
            # Creation/decompilation can take long enough for the active
            # project or capture to change. Revalidate both before publishing
            # the workspace response; a changed binding fails closed.
            current_binding = _active_capture_catalog_binding()
            if current_binding is None:
                raise RuntimeError("active full-catalog binding disappeared")
            unused_context, unused_project, record, unused_path = current_binding
            if (
                not expected_binding_sha256
                or _active_capture_catalog_binding_sha256(record)
                != expected_binding_sha256
            ):
                raise RuntimeError(
                    "active full-catalog binding changed during workspace preparation"
                )
        workspace_status = service.status(workspace)
        recovery_plan = workspace_status.get("recoveryPlan")
        decompile_candidates = (
            recovery_plan.get("decompileCandidates", [])
            if isinstance(recovery_plan, Mapping)
            else []
        )
        compile_candidates = (
            recovery_plan.get("compileCandidates", [])
            if isinstance(recovery_plan, Mapping)
            else []
        )
        executable_ready = representation.get("lane") == "executable-editable"
        selectable_alternatives = max(
            0,
            (
                len(decompile_candidates) * max(1, len(compile_candidates))
                if isinstance(decompile_candidates, list)
                and isinstance(compile_candidates, list)
                else 0
            )
            - 1,
        )
        if executable_ready:
            recovery_status = "reconstruction-ready"
        elif selectable_alternatives:
            recovery_status = "agent-route-selection-required"
        elif provisional_reconstruction is not None:
            recovery_status = "agent-source-reconstruction-required"
        else:
            recovery_status = "evidence-only"
        recovery_decision = {
            "schema": "renderdoc-ultra-shader-agent-recovery/v1",
            "schemaVersion": 1,
            "ownership": "agent",
            "status": recovery_status,
            "primaryAttemptSucceeded": executable_ready,
            "registeredDecompileCandidateCount": (
                len(decompile_candidates)
                if isinstance(decompile_candidates, list)
                else 0
            ),
            "registeredCompileCandidateCount": (
                len(compile_candidates)
                if isinstance(compile_candidates, list)
                else 0
            ),
            "untriedAlternativePairCount": selectable_alternatives,
            "suggestionsAreNonAuthoritative": True,
            "agentMayReorderOrIgnoreSuggestions": True,
            "agentMaySelectAnyRegisteredCandidatePair": True,
            "hostSelectedNextRoute": False,
        }
        prepared_binding = {
            "schema": "renderdoc-ultra-prepared-shader-workspace-binding/v1",
            "schemaVersion": 1,
            "workspacePath": str(workspace),
            "workspaceId": workspace_status["workspaceId"],
            "captureId": workspace_status["capture"]["captureId"],
            "captureSessionId": capture_session_id,
            "eventId": event_value,
            "stage": selected.key.stage,
            "entryPoint": selected.key.entry_point,
            "originalResourceId": selected.key.resource_id,
            "originalRawSha256": selected.key.raw_sha256,
        }
        return _shader_workspace_response(
            "prepare",
            {
                "workspacePath": str(workspace),
                "binding": prepared_binding,
                "workspaceStatus": workspace_status,
                "catalogExport": exported,
                "selectedShader": selected.key.as_dict(),
                "created": created,
                "recoveryDecision": recovery_decision,
                "reconstructionReceipt": reconstruction_receipt,
                "provisionalReconstruction": provisional_reconstruction,
                "projectSourceReuse": project_source_reuse,
                # Retained for older clients; the object now also carries the
                # explicit representation lane contract.
                "decompiled": representation,
                "representation": representation,
            },
        )
    except Exception as error:
        return _shader_err(error, "workspace-prepare")


@mcp.tool()
def decompile_shader_workspace(workspace_path: str) -> str:
    """Run the workspace-bound route and publish its fixed UTF-8 source path.

    No executable or arguments can be supplied by this request. A repeated
    decompile reuses the pristine, fixed-program source already produced by
    ``prepare_shader_workspace``. Edited, promoted, or migrated source is never
    relabelled as a fresh decompile and remains protected from overwrite.
    """
    try:
        service = _shader_workspace_service()
        result = service.reuse_pristine_decompiled_source(workspace_path)
        if result is None:
            try:
                result = service.decompile(workspace_path)
            except Exception as error:
                _remember_route_decompile_failure(workspace_path, error)
                raise
            result["reusedPreparedSource"] = False
        return _shader_workspace_response("decompile", result)
    except Exception as error:
        return _shader_err(error, "decompile")


@mcp.tool()
def get_shader_workspace_representation(
    workspace_path: str,
    event_id: int = 0,
    stage: str = "",
    retry_decompile: bool = False,
) -> str:
    """Materialize a non-empty Shader representation with an explicit lane.

    The order is deterministic: tracked executable routed text,
    RenderDoc-native disassembly, then a hash-bound captured binary byte view.
    ``explanatory-ir`` and ``binary-only`` artifacts are facts for human/AI
    explanation and can never be compiled, watched, or applied. Supplying an
    event lets the server obtain and identity-check RenderDoc disassembly; the
    caller cannot inject disassembly text.
    """

    try:
        event_value = int(event_id)
        if event_value < 0:
            raise ValueError("event_id must be zero or a positive integer")
        bridge_response = None
        if event_value:
            if not str(stage or "").strip():
                raise ValueError("stage is required when event_id is supplied")
            try:
                bridge_response = ipc.call(
                    "reverse_shader",
                    {"event_id": event_value, "stage": stage},
                    timeout=300.0,
                )
                if not isinstance(bridge_response, dict):
                    bridge_response = {
                        "error": "RenderDoc bridge returned an invalid reverse_shader result"
                    }
            except Exception as error:
                bridge_response = {"error": str(error)}
        result = _shader_workspace_service().representation(
            workspace_path,
            bridge_response=bridge_response,
            bridge_event_id=event_value or None,
            retry_decompile=retry_decompile,
        )
        return _shader_workspace_response("representation", result)
    except Exception as error:
        return _shader_err(error, "representation")


@mcp.tool()
def promote_shader_workspace_explanation(
    workspace_path: str,
    expected_representation_sha256: str,
    expected_source_sha256: str,
    acknowledge_semantic_equivalence_unverified: bool,
) -> str:
    """Promote a reviewed fixed-language explanation candidate into source.

    Both evidence and candidate hashes are exact race guards. Promotion does
    not compile, validate, or apply anything, and explicitly records that
    semantic equivalence is unverified. The ordinary same-profile compiler,
    complete replay ABI gate, and separately authorized apply remain required.
    """

    try:
        result = _shader_workspace_service().promote_explanatory_source(
            workspace_path,
            expected_representation_sha256=expected_representation_sha256,
            expected_source_sha256=expected_source_sha256,
            acknowledge_semantic_equivalence_unverified=(
                acknowledge_semantic_equivalence_unverified
            ),
        )
        return _shader_workspace_response("promote-explanation", result)
    except Exception as error:
        return _shader_err(error, "source-promotion")


@mcp.tool()
def compile_shader_workspace(workspace_path: str) -> str:
    """Compile the workspace's executable-editable source through its fixed route.

    The source may be HLSL, GLSL, or native editable IR as admitted by the
    manifest's pinned toolchain route. Output is structurally validated as
    exactly one DXBC/DXIL container or one SPIR-V module. A native SPIR-V
    assembly lane additionally requires its hash-bound spirv-val gate. These
    checks prove only the reported structure, tool provenance, and validator
    result—not semantic equivalence. Candidates always remain unapplied here.
    Replay ABI validation and replacement cover D3D11/D3D12 DXBC/DXIL plus
    checked Vulkan/OpenGL native SPIR-V paths for conventional graphics and
    compute stages. Ray/Library replacement remains fail-closed until its
    state-object, shader-group/SBT, and per-export ABI transaction is available.
    """
    try:
        result = _shader_workspace_service().compile(workspace_path)
        return _shader_workspace_response("compile", result)
    except Exception as error:
        return _shader_err(error, "compile")


@mcp.tool()
def migrate_shader_workspace_toolchain(
    source_workspace_path: str,
    workspace_path: str,
    expected_route_plan_sha256: str,
    expected_source_sha256: str,
    recover_legacy_source_lease: bool = False,
    in_place: bool = False,
) -> str:
    """Migrate exact source bytes to the current fixed Shader toolchain.

    Set ``in_place=true`` with identical source and target paths to keep the
    document/workspace identity and refresh only its current compile binding.
    This preserves user edits, archives previous provenance, and invalidates
    all old compiled/ABI evidence. Compile and validate again before applying;
    changing Host versions alone does not require source copies or AI review.

    With the default ``in_place=false``, the source workspace is left unchanged
    and retained as the archive. The new
    workspace starts at revision zero with no imported candidate and may use
    only the same route IDs, input/output formats, and Shader stage as the old
    route plan. Both hashes are mandatory race guards. This operation never
    trusts or imports old candidate/revision/ABI evidence, claims semantic
    equivalence, validates ABI, or applies a Shader replacement.

    Set ``recover_legacy_source_lease`` only after the user explicitly chooses
    migration despite a legacy ``.workspace-watch.lock``. The singleton Host
    then archives an exact token-only lock if no source transaction residue is
    present. Malformed, current-generation, or journal-bearing leases remain
    blocked.
    """

    try:
        result = _shader_workspace_service().migrate_toolchain(
            source_workspace_path,
            workspace_path,
            expected_route_plan_sha256=expected_route_plan_sha256,
            expected_source_sha256=expected_source_sha256,
            # This is process-owned singleton-host state, not an MCP argument.
            host_instance_id=os.environ.get("REPLAYLAB_HOST_INSTANCE_ID"),
            recover_legacy_source_lease=recover_legacy_source_lease,
            in_place=in_place,
        )
        return _shader_workspace_response("toolchain-migrate", result)
    except Exception as error:
        return _shader_err(error, "toolchain-migrate")


@mcp.tool()
def get_shader_workspace_status(workspace_path: str) -> str:
    """Validate and report workspace/source/candidate state without mutation."""
    try:
        result = _shader_workspace_service().status(workspace_path)
        return _shader_workspace_response("status", result)
    except Exception as error:
        return _shader_err(error, "workspace-status")


def _shader_organization_semantic_context(
    workspace_path: str, event_id: int
) -> dict[str, Any]:
    """Collect bounded fixed-program facts which an organizer may only label."""

    service = _shader_workspace_service()
    status = service.status(workspace_path)
    baseline = status.get("baseline")
    interface = baseline.get("compiledInterface") if isinstance(baseline, dict) else None
    if not isinstance(interface, dict):
        raise ShaderOrganizationError(
            "organization workspace baseline has no complete compiled interface"
        )
    reconstruction_fidelity = status.get("reconstructionFidelity")
    if (
        not isinstance(reconstruction_fidelity, dict)
        or reconstruction_fidelity.get("schema")
        != "renderdoc-ultra-shader-reconstruction-fidelity/v1"
        or reconstruction_fidelity.get("semanticEquivalenceVerified") is not False
    ):
        raise ShaderOrganizationError(
            "organization workspace reconstruction fidelity evidence is invalid"
        )
    if isinstance(event_id, bool):
        raise TypeError("event_id must be zero or a positive integer")
    event_value = int(event_id)
    if event_value < 0:
        raise ValueError("event_id must be zero or a positive integer")
    live: dict[str, Any] = {
        "available": False,
        "eventId": 0,
        "boundTextures": [],
        "pipelineState": None,
    }
    if event_value:
        live_before = _workspace_active_capture_context(workspace_path, status=status)
        live_identity = _active_capture_transport_identity(live_before)
        selected = select_unique_shader_for_use(
            status["capture"]["catalogPath"], event_value, status["stage"]
        )
        if any(
            (
                selected.key.resource_id != status["shader"]["resourceId"],
                selected.key.stage != status["stage"],
                selected.key.entry_point != status["entryPoint"],
                selected.key.raw_sha256 != status["baseline"]["rawSha256"],
            )
        ):
            raise ShaderOrganizationError(
                "organization event_id is not an exact use of the workspace Shader"
            )
        textures = ipc.call(
            "get_bound_textures",
            {"event_id": event_value, "stage": status["stage"]},
            timeout=60.0,
        )
        # These legacy evidence calls do not enforce expected_capture_id in the
        # bridge.  Bracket each one with the authoritative current-context
        # lifecycle identity and discard all evidence on any capture reload.
        live_after_textures = _active_live_context(allow_missing_capture_id=True)
        if _active_capture_transport_identity(live_after_textures) != live_identity:
            raise ShaderOrganizationError(
                "RenderDoc capture session changed while collecting organization textures"
            )
        pipeline = ipc.call(
            "get_pipeline_state", {"event_id": event_value}, timeout=60.0
        )
        live_after_pipeline = _active_live_context(allow_missing_capture_id=True)
        if _active_capture_transport_identity(live_after_pipeline) != live_identity:
            raise ShaderOrganizationError(
                "RenderDoc capture session changed while collecting organization pipeline state"
            )
        if not isinstance(textures, list) or len(textures) > 4096:
            raise ShaderOrganizationError(
                "RenderDoc returned invalid organization bound-texture evidence"
            )
        if not isinstance(pipeline, dict):
            raise ShaderOrganizationError(
                "RenderDoc returned invalid organization pipeline-state evidence"
            )
        live = {
            "available": True,
            "eventId": event_value,
            "captureId": live_before["capture"]["captureId"],
            "captureSessionId": live_identity[0],
            "boundTextures": textures,
            "pipelineState": pipeline,
        }
    return {
        "schema": "renderdoc-ai-shader-semantic-context/v1",
        "schemaVersion": 1,
        "workspaceId": status["workspaceId"],
        "captureId": status["capture"]["captureId"],
        "shader": status["shader"],
        "stage": status["stage"],
        "entryPoint": status["entryPoint"],
        "targetProfile": status["targetProfile"],
        "reconstructionFidelity": dict(reconstruction_fidelity),
        "interface": {
            "inputSignature": interface.get("inputSignature", []),
            "outputSignature": interface.get("outputSignature", []),
            "readOnlyResources": interface.get("readOnlyResources", []),
            "readWriteResources": interface.get("readWriteResources", []),
            "samplers": interface.get("samplers", []),
            "constantBlocks": interface.get("constantBlocks", []),
        },
        "liveBindingEvidence": live,
        "authority": {
            "physicalInterface": "FixedProgram",
            "resourceNames": "AIProvisionalUnlessVerifiedBySymbols",
            "parameterNames": "AIProvisionalUnlessVerifiedBySymbols",
            "aiMayChangeRegistersOffsetsOrSemantics": False,
        },
    }


@mcp.tool()
def begin_shader_workspace_organization(
    workspace_path: str, event_id: int = 0
) -> str:
    """Open an AI-readable HLSL proposal after baseline compile and ABI gates.

    The fixed transaction preserves the byte-for-byte decompiler source and
    creates one proposal HLSL, analysis Markdown, Mermaid file, and bounded
    module-description JSON below the workspace.  AI may edit only those
    returned proposal files. Fixed code derives a clickable function-call graph
    from the final reviewed HLSL and binds every node to the workspace, source
    hash/revision, file, line, and column. Supplying an event adds exact
    bound-texture and pipeline facts after verifying that the event uses this
    physical Shader; zero keeps the transaction workspace-only. Nothing is
    applied to RenderDoc.
    """

    try:
        context = _shader_organization_semantic_context(workspace_path, event_id)
        result = _shader_organization_manager().begin(workspace_path, context)
        return _ok(result)
    except Exception as error:
        return _shader_err(error, "organization-begin")


@mcp.tool()
def repair_shader_workspace_source_abi(
    workspace_path: str,
    expected_source_sha256: str,
    source: str,
    acknowledge_semantic_equivalence_unverified: bool,
) -> str:
    """Submit AI-authored HLSL that restores the captured physical ABI.

    Use this when the decompiled workspace source compiles but fails the
    replay ABI gate (``organization-abi-failed`` / ``abi-mismatch`` with the
    ``repair-source-within-captured-abi`` action) and the Host's fixed
    SPIRV-Cross repair could not fix it. Read the current source with
    ``read_shader_workspace_file`` and the captured interface from
    ``get_shader_workspace_status`` (``baseline.compiledInterface``,
    ``lastAbiAttempt.comparison``), then send the complete repaired file with
    the exact hash of the source you read. The Host compile-checks it, swaps
    it in, compiles and replay-validates it. Only a candidate whose fresh
    reflection matches the captured ABI stays; otherwise the previous bytes
    are restored and the diagnostics / ABI differences are returned so the
    next attempt can be corrected. Nothing is applied to RenderDoc; semantic
    equivalence stays unverified and the organization review still follows.
    """

    try:
        result = _shader_organization_manager().repair_baseline_source(
            workspace_path,
            expected_source_sha256,
            source,
            acknowledge_semantic_equivalence_unverified=(
                acknowledge_semantic_equivalence_unverified
            ),
        )
        return _ok(result)
    except Exception as error:
        return _shader_err(error, "abi-source-repair")


def _project_workspace_knowledge_identity(
    knowledge: Mapping[str, Any]
) -> dict[str, Any]:
    """Project the normal workspace writer record onto its immutable identity."""

    fields = (
        "workspaceId",
        "captureId",
        "originCatalogSha256",
        "originResourceId",
        "stage",
        "entryPoint",
        "rawSha256",
        "codeSha256",
        "layoutHash",
        "interfaceHash",
        "interfaceComplete",
        "baselineAbiSha256",
        "targetProfile",
        "sourceSha256",
        "analysisSha256",
    )
    if any(field not in knowledge for field in fields):
        raise RuntimeError(
            "active project workspace registration omitted identity fields"
        )
    identity = {field: copy.deepcopy(knowledge[field]) for field in fields}
    compiler = knowledge.get("compilerBinding")
    if compiler is not None:
        if not isinstance(compiler, Mapping):
            raise RuntimeError(
                "active project workspace compiler binding is invalid"
            )
        identity["compilerBinding"] = copy.deepcopy(dict(compiler))
    disassembly = knowledge.get("assemblyDisassemblyArtifacts")
    if disassembly is not None:
        if (
            not isinstance(disassembly, list)
            or len(disassembly) > 2
            or any(not isinstance(item, Mapping) for item in disassembly)
        ):
            raise RuntimeError(
                "active project workspace disassembly identity is invalid"
            )
        clean_disassembly = []
        for item in disassembly:
            clean = copy.deepcopy(dict(item))
            clean.pop("path", None)
            clean.pop("artifactPath", None)
            clean_disassembly.append(clean)
        if clean_disassembly:
            identity["assemblyDisassemblyArtifacts"] = clean_disassembly
    compiled = knowledge.get("compiledCandidate")
    if compiled is not None:
        if not isinstance(compiled, Mapping):
            raise RuntimeError(
                "active project workspace compiled identity is invalid"
            )
        clean_compiled = copy.deepcopy(dict(compiled))
        clean_compiled.pop("artifactPath", None)
        identity["compiledCandidate"] = clean_compiled
    return identity


def _active_workspace_publication_gate(
    status: Mapping[str, Any],
) -> dict[str, Any]:
    """Require the current organize-only source and ABI candidate publication gate.

    Project knowledge is shared across captures and future sessions.  A merely
    editable HLSL file is therefore not enough evidence for this child-Agent
    publication path: the exact current source must have survived the fixed
    organize-only review, compilation, and fresh replay ABI validation.
    """

    if not isinstance(status, Mapping):
        raise RuntimeError("active project workspace status is invalid")
    declared_workspace_root = Path(str(status.get("workspacePath", "")))
    workspace_root = declared_workspace_root.resolve()
    source = status.get("source")
    capture = status.get("capture")
    latest = status.get("latestCompiled")
    if (
        not workspace_root.is_dir()
        or declared_workspace_root.is_symlink()
        or not isinstance(source, Mapping)
        or not isinstance(capture, Mapping)
        or not isinstance(latest, Mapping)
    ):
        raise RuntimeError(
            "active project publication requires a current organized, compiled, "
            "and ABI-validated HLSL workspace"
        )

    declared_source_path = Path(str(source.get("path", "")))
    source_path = declared_source_path.resolve()
    ai_dir = workspace_root / "ai"
    gate_path = ai_dir / "organize-code-gate.json"
    if (
        source.get("present") is not True
        or source.get("format") != "hlsl"
        or source.get("lane") != "executable-editable"
        or declared_source_path.is_symlink()
        or not source_path.is_file()
        or not is_within(workspace_root, source_path)
        or not ai_dir.is_dir()
        or ai_dir.is_symlink()
        or ai_dir.resolve() != ai_dir
        or not is_within(workspace_root, gate_path)
    ):
        raise RuntimeError(
            "active project publication source provenance is invalid"
        )
    gate = read_json_object(
        gate_path, 1024 * 1024, "shader organization publication gate"
    )
    binding = gate.get("finalBinding")
    abi_gate = latest.get("abiGate")
    # Two admissible outcomes: the organized proposal was accepted (exact
    # executable, or baseline + Host-applied renames), or the review budget
    # was exhausted and the always-compilable baseline is published together
    # with the analysis and flow graph ("soft publish": the user gets a
    # compilable shader and its documentation instead of nothing).
    accepted_organized = (
        gate.get("decision") == "accepted-executable-editable"
        and (
            (
                gate.get("codeSha256Equal") is True
                and gate.get("equivalenceLevel") == "exact-code"
            )
            or (
                gate.get("codeSha256Equal") is False
                and gate.get("equivalenceLevel") in ("rename-only", "observational-pass")
            )
        )
        and gate.get("organizedSourcePromoted") is True
        and gate.get("rawRecoveryCompiledAndAbiValidated") is not True
        and gate.get("safeToStartLiveEdit") is True
    )
    soft_published_baseline = (
        gate.get("decision") == "soft-accepted-baseline"
        and gate.get("softPublishAllowed") is True
        and gate.get("organizedSourcePromoted") is False
        and gate.get("rawRecoveryCompiledAndAbiValidated") is True
        and gate.get("safeToStartLiveEdit") is True
    )
    if (
        gate.get("schema") != ORGANIZATION_GATE_SCHEMA
        or gate.get("schemaVersion") != 1
        or gate.get("policyVersion") != ORGANIZATION_POLICY_VERSION
        or gate.get("requestedTransformation") != "organize-only"
        or not (accepted_organized or soft_published_baseline)
        or gate.get("baselineCompiledAndAbiValidated") is not True
        or gate.get("organizedCompiledAndAbiValidated") is not True
        or gate.get("semanticEquivalenceVerified") is not False
        or gate.get("replayApplied") is not False
        or not isinstance(binding, Mapping)
        or set(binding) != {
            "complete",
            "sourceSha256",
            "revision",
            "candidateRawSha256",
            "codeSha256",
            "abiRequestId",
            "abiReportSha256",
        }
        or binding.get("complete") is not True
        or not isinstance(abi_gate, Mapping)
        or abi_gate.get("status") != "compatible"
        or latest.get("replayAbiValidated") is not True
        or latest.get("abiCompatible") is not True
        or latest.get("requiresReplayAbiValidation") is not False
        or latest.get("validationScope")
        != "replay-reflection-abi-compatible"
        or latest.get("semanticEquivalenceVerified") is not False
        or status.get("replayAbiValidated") is not True
        or status.get("abiCompatible") is not True
    ):
        raise RuntimeError(
            "active project publication requires an accepted organize-only "
            "compile/ABI gate"
        )

    _strict_lower_hex(
        gate.get("organizationId"), 32, "organization gate organizationId"
    )
    workspace_id = _strict_lower_hex(
        status.get("workspaceId"), 32, "workspace.workspaceId"
    )
    capture_id = _strict_lower_hex(
        capture.get("captureId"), 64, "workspace.capture.captureId"
    )
    source_sha256 = _strict_lower_hex(
        source.get("currentSha256"), 64, "workspace.source.currentSha256"
    )
    source_file_sha256 = _bounded_file_sha256(
        source_path, 64 * 1024 * 1024, "workspace HLSL source"
    )
    revision = status.get("revision")
    reviewed_at = gate.get("reviewedAt")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision <= 0
        or not isinstance(reviewed_at, str)
        or not reviewed_at.strip()
        or len(reviewed_at) > 128
    ):
        raise RuntimeError(
            "active project organization gate revision/review time is invalid"
        )

    baseline_source_sha256 = _strict_lower_hex(
        gate.get("baselineSourceSha256"),
        64,
        "organization gate baselineSourceSha256",
    )
    proposal_source_sha256 = _strict_lower_hex(
        gate.get("proposalSourceSha256"),
        64,
        "organization gate proposalSourceSha256",
    )
    baseline_code_sha256 = _strict_lower_hex(
        gate.get("baselineCodeSha256"),
        64,
        "organization gate baselineCodeSha256",
    )
    organized_code_sha256 = _strict_lower_hex(
        gate.get("organizedCodeSha256"),
        64,
        "organization gate organizedCodeSha256",
    )
    final_source_sha256 = _strict_lower_hex(
        gate.get("finalSourceSha256"), 64, "organization gate finalSourceSha256"
    )
    final_candidate_sha256 = _strict_lower_hex(
        gate.get("finalCandidateRawSha256"),
        64,
        "organization gate finalCandidateRawSha256",
    )
    final_code_sha256 = _strict_lower_hex(
        gate.get("finalCodeSha256"), 64, "organization gate finalCodeSha256"
    )
    final_request_id = _strict_lower_hex(
        gate.get("finalAbiRequestId"), 64, "organization gate finalAbiRequestId"
    )
    final_report_sha256 = _strict_lower_hex(
        gate.get("finalAbiReportSha256"),
        64,
        "organization gate finalAbiReportSha256",
    )
    binding_source_sha256 = _strict_lower_hex(
        binding.get("sourceSha256"), 64, "organization gate binding sourceSha256"
    )
    binding_candidate_sha256 = _strict_lower_hex(
        binding.get("candidateRawSha256"),
        64,
        "organization gate binding candidateRawSha256",
    )
    binding_code_sha256 = _strict_lower_hex(
        binding.get("codeSha256"), 64, "organization gate binding codeSha256"
    )
    binding_request_id = _strict_lower_hex(
        binding.get("abiRequestId"), 64, "organization gate binding abiRequestId"
    )
    binding_report_sha256 = _strict_lower_hex(
        binding.get("abiReportSha256"),
        64,
        "organization gate binding abiReportSha256",
    )
    latest_candidate_sha256 = _strict_lower_hex(
        latest.get("rawSha256"), 64, "workspace.latestCompiled.rawSha256"
    )
    latest_code_sha256 = _strict_lower_hex(
        latest.get("codeSha256"), 64, "workspace.latestCompiled.codeSha256"
    )
    abi_request_id = _strict_lower_hex(
        abi_gate.get("requestId"),
        64,
        "workspace.latestCompiled.abiGate.requestId",
    )
    abi_report_sha256 = _strict_lower_hex(
        abi_gate.get("reportSha256"),
        64,
        "workspace.latestCompiled.abiGate.reportSha256",
    )
    _strict_lower_hex(
        abi_gate.get("baselineAbiSha256"),
        64,
        "workspace.latestCompiled.abiGate.baselineAbiSha256",
    )
    _strict_lower_hex(
        abi_gate.get("candidateAbiSha256"),
        64,
        "workspace.latestCompiled.abiGate.candidateAbiSha256",
    )
    declared_candidate_path = Path(str(latest.get("candidateBlobPath", "")))
    candidate_path = declared_candidate_path.resolve()
    if (
        declared_candidate_path.is_symlink()
        or not candidate_path.is_file()
        or not is_within(workspace_root, candidate_path)
        or latest.get("byteSize") != candidate_path.stat().st_size
        or _bounded_file_sha256(
            candidate_path,
            512 * 1024 * 1024,
            "workspace compiled candidate",
        )
        != latest_candidate_sha256
    ):
        raise RuntimeError(
            "active project publication compiled candidate provenance is invalid"
        )
    declared_source = gate.get("executableEditableSourcePath")
    if not isinstance(declared_source, str) or not declared_source.strip():
        raise RuntimeError("active project organization gate source is missing")
    declared_path = Path(declared_source)
    if not declared_path.is_absolute():
        declared_path = workspace_root / declared_path
    if declared_path.is_symlink():
        raise RuntimeError(
            "active project organization gate source must not be a symbolic link"
        )
    declared_path = declared_path.resolve()

    if any(
        (
            gate.get("workspaceId") != workspace_id,
            gate.get("captureId") != capture_id,
            normalize_stage(gate.get("stage"))
            != normalize_stage(status.get("stage")),
            str(gate.get("entryPoint", "") or "")
            != str(status.get("entryPoint", "") or ""),
            # The trusted organization validator independently replays the
            # sealed rename map against the reviewed source. DXIL name
            # metadata can change its code digest even for those renames.
            # Here validate the claim's consistency, not executable identity
            # a second time under a stricter, contradictory policy.
            gate.get("codeSha256Equal")
            is not (baseline_code_sha256 == organized_code_sha256),
            source_sha256 != source_file_sha256,
            (baseline_source_sha256 if soft_published_baseline else proposal_source_sha256) != final_source_sha256,
            final_source_sha256 != source_sha256,
            binding_source_sha256 != final_source_sha256,
            gate.get("finalRevision") != revision,
            binding.get("revision") != revision,
            latest.get("revision") != revision,
            latest.get("sourceSha256") != source_sha256,
            final_candidate_sha256 != latest_candidate_sha256,
            binding_candidate_sha256 != final_candidate_sha256,
            final_code_sha256 != latest_code_sha256,
            (baseline_code_sha256 if soft_published_baseline else organized_code_sha256) != final_code_sha256,
            binding_code_sha256 != final_code_sha256,
            final_request_id != abi_request_id,
            binding_request_id != final_request_id,
            final_report_sha256 != abi_report_sha256,
            binding_report_sha256 != final_report_sha256,
            declared_path != source_path,
            not is_within(workspace_root, declared_path),
        )
    ):
        raise RuntimeError(
            "active project organization gate is stale or bound to different "
            "source/compiled ABI evidence"
        )

    # Keep the complete object as the frozen transaction witness.  The caller
    # reads and validates it again after the project writer returns.
    return copy.deepcopy(gate)


def _register_reviewed_workspace_with_readback(
    project: Any, workspace_service: Any, workspace_path: str,
    *, before_publish: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Publish one HMAC-reviewed workspace under its source transaction lease."""

    located = workspace_service.status(workspace_path)
    root = Path(str(located.get("workspacePath", ""))).resolve()
    if root != Path(workspace_path).expanduser().resolve():
        raise RuntimeError("project publication workspace identity changed")
    lease = WorkspaceSourceTransactionLease(root)
    try:
        lease.acquire()
    except WorkspaceSourceLeaseError as exc:
        raise RuntimeError(
            "project publication requires the exclusive workspace source lease"
        ) from exc
    try:
        status = workspace_service.status(str(root))
        trusted_gate_before = _shader_organization_manager().validate_publication_gate(
            str(root), status
        )
        structural_gate_before = _active_workspace_publication_gate(status)
        if trusted_gate_before != structural_gate_before:
            raise RuntimeError(
                "trusted and structural organization publication gates differ"
            )

        if before_publish is not None:
            before_publish(status)
            ready = workspace_service.status(str(root))
            if (
                ready != status
                or _shader_organization_manager().validate_publication_gate(str(root), ready)
                != trusted_gate_before
                or _active_workspace_publication_gate(ready) != structural_gate_before
            ):
                raise RuntimeError(
                    "workspace or organization gate changed before project publication"
                )

        raw_result = project.register_workspace(status)
        knowledge = (
            raw_result.get("knowledge")
            if isinstance(raw_result, Mapping)
            else None
        )
        knowledge_id = (
            str(knowledge.get("knowledgeId", "") or "").strip().lower()
            if isinstance(knowledge, Mapping)
            else ""
        )
        _strict_lower_hex(knowledge_id, 32, "knowledge.knowledgeId")
        published = project.get_workspace_knowledge(knowledge_id)

        status_after = workspace_service.status(str(root))
        trusted_gate_after = _shader_organization_manager().validate_publication_gate(
            str(root), status_after
        )
        structural_gate_after = _active_workspace_publication_gate(status_after)
        if (
            status_after != status
            or trusted_gate_after != trusted_gate_before
            or structural_gate_after != structural_gate_before
            or trusted_gate_after != structural_gate_after
        ):
            raise RuntimeError(
                "workspace or organization gate changed during project publication"
            )
        return _validated_active_workspace_registration(
            raw_result, status_after, published, trusted_gate_after
        )
    finally:
        lease.release()


def _validated_active_workspace_registration(
    result: Any,
    status: Mapping[str, Any],
    published: Any,
    publication_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Require publication evidence for the exact on-disk workspace revision.

    ``ReplayProjectService`` performs the publication transaction.  This
    projection is deliberately strict as well: an adapter or test double may
    not turn a bare knowledge id and an arbitrary source digest into proof that
    the shared HLSL, workspace manifest, or current ABI-validated binary was
    actually registered.
    """

    if (
        not isinstance(result, dict)
        or not isinstance(status, Mapping)
        or not isinstance(published, Mapping)
        or (publication_gate is not None and not isinstance(publication_gate, Mapping))
    ):
        raise RuntimeError(
            "active project workspace registration returned invalid evidence"
        )
    knowledge = result.get("knowledge")
    source = status.get("source")
    capture = status.get("capture")
    shader = status.get("shader")
    baseline = status.get("baseline")
    policy = result.get("policy")
    flowcharts = result.get("flowchartArtifacts")
    if not all(
        isinstance(value, Mapping)
        for value in (knowledge, source, capture, shader, baseline, policy)
    ) or not isinstance(flowcharts, list):
        raise RuntimeError(
            "active project workspace registration omitted provenance"
        )
    knowledge_id = str(knowledge.get("knowledgeId", "") or "").strip().lower()
    _strict_lower_hex(knowledge_id, 32, "knowledge.knowledgeId")
    if dict(published) != dict(knowledge):
        raise RuntimeError(
            "active project workspace publication could not be read back exactly"
        )
    identity = _project_workspace_knowledge_identity(knowledge)
    if _canonical_json_sha256(
        identity, "workspace project knowledge identity"
    )[:32] != knowledge_id:
        raise RuntimeError(
            "active project workspace knowledgeId does not match its contents"
        )
    workspace_root = Path(str(status.get("workspacePath", ""))).resolve()
    source_path = Path(str(source.get("path", ""))).resolve()
    manifest_path = Path(str(status.get("manifestPath", ""))).resolve()
    if (
        not workspace_root.is_dir()
        or workspace_root.is_symlink()
        or source.get("present") is not True
        or source.get("lane") != "executable-editable"
        or source.get("format") != "hlsl"
        or source_path.is_symlink()
        or not source_path.is_file()
        or not is_within(workspace_root, source_path)
        or manifest_path != (workspace_root / "manifest.json").resolve()
        or manifest_path.is_symlink()
        or not manifest_path.is_file()
    ):
        raise RuntimeError(
            "active project workspace registration source/manifest provenance is invalid"
        )
    source_file_sha256 = _bounded_file_sha256(
        source_path, 64 * 1024 * 1024, "workspace HLSL source"
    )
    manifest_sha256 = _bounded_file_sha256(
        manifest_path, 64 * 1024 * 1024, "workspace manifest"
    )
    source_sha256 = _strict_lower_hex(
        source.get("currentSha256"), 64, "workspace.source.currentSha256"
    )
    expected_source_relative = "knowledge/workspaces/%s.hlsl" % knowledge_id
    if (
        result.get("applied") is not False
        or type(result.get("idempotent")) is not bool
        or knowledge.get("workspaceId") != status.get("workspaceId")
        or knowledge.get("captureId") != capture.get("captureId")
        or knowledge.get("originResourceId") != shader.get("resourceId")
        or normalize_stage(knowledge.get("stage"))
        != normalize_stage(status.get("stage", shader.get("stage")))
        or str(knowledge.get("entryPoint", "") or "")
        != str(status.get("entryPoint", shader.get("entryPoint", "")) or "")
        or knowledge.get("rawSha256") != baseline.get("rawSha256")
        or knowledge.get("sourceSha256") != source_sha256
        or source_sha256 != source_file_sha256
        or knowledge.get("sourcePath") != expected_source_relative
        or Path(str(knowledge.get("sourceOriginPath", ""))).resolve()
        != source_path
        or knowledge.get("workspaceManifestSha256") != manifest_sha256
        or Path(str(knowledge.get("workspacePath", ""))).resolve()
        != workspace_root
        or knowledge.get("targetProfile") != status.get("targetProfile")
        or not isinstance(knowledge.get("registeredAt"), str)
        or not str(knowledge.get("registeredAt", "")).strip()
        or len(flowcharts) > 16
        or policy.get("flowchartExplanatoryOnly") is not True
        or policy.get("physicalEvidence") is not False
        or policy.get("applyAuthorization") is not False
    ):
        raise RuntimeError(
            "active project workspace registration did not match the exact workspace"
        )

    latest = status.get("latestCompiled")
    expected_compiled: dict[str, Any] | None = None
    if latest is not None and not isinstance(latest, Mapping):
        raise RuntimeError(
            "active project workspace registration compiled status is invalid"
        )
    if isinstance(latest, Mapping):
        fully_validated = all(
            (
                latest.get("replayAbiValidated") is True,
                latest.get("abiCompatible") is True,
                latest.get("requiresReplayAbiValidation") is False,
                latest.get("validationScope")
                == "replay-reflection-abi-compatible",
                latest.get("semanticEquivalenceVerified") is False,
                status.get("replayAbiValidated") is True,
                status.get("abiCompatible") is True,
                latest.get("revision") == status.get("revision"),
            )
        )
        if fully_validated and latest.get("sourceSha256") == source_sha256:
            abi_gate = latest.get("abiGate")
            candidate_path = Path(
                str(latest.get("candidateBlobPath", ""))
            ).resolve()
            if (
                not isinstance(abi_gate, Mapping)
                or abi_gate.get("status") != "compatible"
                or candidate_path.is_symlink()
                or not candidate_path.is_file()
                or not is_within(workspace_root, candidate_path)
            ):
                raise RuntimeError(
                    "active project workspace registration compiled provenance is invalid"
                )
            candidate_sha256 = _bounded_file_sha256(
                candidate_path,
                512 * 1024 * 1024,
                "workspace compiled candidate",
            )
            candidate_raw_sha256 = _strict_lower_hex(
                latest.get("rawSha256"),
                64,
                "workspace.latestCompiled.rawSha256",
            )
            if (
                candidate_raw_sha256 != candidate_sha256
                or latest.get("byteSize") != candidate_path.stat().st_size
            ):
                raise RuntimeError(
                    "active project workspace registration compiled bytes differ"
                )
            expected_compiled = {
                "schema": "renderdoc-ultra-project-compiled-candidate/v1",
                "schemaVersion": 1,
                "rawSha256": candidate_raw_sha256,
                "codeSha256": _strict_lower_hex(
                    latest.get("codeSha256"),
                    64,
                    "workspace.latestCompiled.codeSha256",
                ),
                "byteSize": latest.get("byteSize"),
                "kind": latest.get("kind"),
                "sourceSha256": source_sha256,
                "sourceRevision": status.get("revision"),
                "targetProfile": status.get("targetProfile"),
                "originBaselineAbiSha256": _strict_lower_hex(
                    abi_gate.get("baselineAbiSha256"),
                    64,
                    "workspace.latestCompiled.abiGate.baselineAbiSha256",
                ),
                "originCandidateAbiSha256": _strict_lower_hex(
                    abi_gate.get("candidateAbiSha256"),
                    64,
                    "workspace.latestCompiled.abiGate.candidateAbiSha256",
                ),
                "originAbiReportSha256": _strict_lower_hex(
                    abi_gate.get("reportSha256"),
                    64,
                    "workspace.latestCompiled.abiGate.reportSha256",
                ),
                "originRequestId": str(abi_gate.get("requestId", "") or ""),
                "validationScope": "replay-reflection-abi-compatible",
                "semanticEquivalenceVerified": False,
                "artifactPath": (
                    "knowledge/workspaces/binaries/%s.bin"
                    % candidate_raw_sha256
                ),
            }
            if not expected_compiled["originRequestId"]:
                raise RuntimeError(
                    "active project workspace registration omitted the ABI request id"
                )
        elif any(
            (
                latest.get("replayAbiValidated") is True,
                latest.get("abiCompatible") is True,
                latest.get("validationScope")
                == "replay-reflection-abi-compatible",
            )
        ) and latest.get("sourceSha256") == source_sha256:
            raise RuntimeError(
                "active project workspace registration has inconsistent ABI validation"
            )

    published_compiled = knowledge.get("compiledCandidate")
    if expected_compiled is None:
        raise RuntimeError(
            "active project publication requires the current ABI-validated "
            "compiled candidate"
        )
    elif not isinstance(published_compiled, Mapping) or dict(
        published_compiled
    ) != expected_compiled:
        raise RuntimeError(
            "active project compiled candidate does not match the exact workspace"
        )
    gate_binding = publication_gate.get("finalBinding") if publication_gate is not None else None
    if publication_gate is not None and (
        not isinstance(gate_binding, Mapping)
        or expected_compiled["rawSha256"]
        != gate_binding.get("candidateRawSha256")
        or expected_compiled["codeSha256"] != gate_binding.get("codeSha256")
        or expected_compiled["sourceSha256"] != gate_binding.get("sourceSha256")
        or expected_compiled["sourceRevision"] != gate_binding.get("revision")
        or expected_compiled["originRequestId"] != gate_binding.get("abiRequestId")
        or expected_compiled["originAbiReportSha256"]
        != gate_binding.get("abiReportSha256")
    ):
        raise RuntimeError(
            "active project compiled candidate differs from the organization gate"
        )
    return dict(result)


@mcp.tool()
def review_shader_workspace_organization(
    workspace_path: str, organization_id: str
) -> str:
    """Compile/ABI-check a proposal and review its transformation.

    This tool reads only the fixed proposal files created by
    begin_shader_workspace_organization.  It compiles and freshly validates the
    proposal, compares its executable ``codeSha256`` with the untouched HLSL
    baseline. Exact code equality and Host-declared token renames use a fast
    deterministic path. Other code changes require a bounded temporary A/B
    replay with fixed numeric tolerances. This needs the replay effect and
    observes captured inputs only; the previous replay mapping is restored.
    It also publishes ``ai/flow-graph.json`` from the final source; calls and
    source locations come from fixed parsing while AI-authored descriptions are
    explanatory only. A rejected proposal stays provisional while the compiled
    baseline is restored; it may be revised in the same session. Exhausting the
    review budget can publish that usable baseline with explanatory artifacts.
    """

    try:
        return _ok(
            _shader_organization_manager().review(
                workspace_path, organization_id
            )
        )
    except Exception as error:
        result = json.loads(_shader_err(error, "organization-review"))
        if isinstance(getattr(error, "review_verdict", None), dict):
            result["verdict"] = error.review_verdict
        return _ok(result)


_WORKSPACE_FILE_MAX_BYTES = 4 * 1024 * 1024
_WORKSPACE_FILE_MAX_CHARS = 120_000
_ORGANIZATION_FILE_KINDS = {
    # kind -> (session key, byte limit, description)
    "proposal": ("proposalSourcePath", 4 * 1024 * 1024, "organized HLSL (review uses code/rename proof or captured-output comparison)"),
    "analysis": ("analysisPath", 256 * 1024, "JSON fields from analysisTemplate; Host renders Markdown"),
    "flowchart": ("flowchartPath", 64 * 1024, "auto only; diagrams are generated by the Host"),
    "module_descriptions": (
        "moduleDescriptionsPath",
        256 * 1024,
        "JSON schema/schemaVersion/modules array from moduleDescriptionsTemplate",
    ),
    # not a session key: written beside the session as flow-spec.json
    "flow_spec": (
        "",
        256 * 1024,
        "JSON data-flow spec (fill the flowSpecTemplate from begin); the Host renders the diagram",
    ),
}


def _workspace_root_for_files(workspace_path: str) -> Path:
    """Resolve a managed Shader workspace root; anything else is refused."""

    status = _shader_workspace_service().status(workspace_path)
    declared = Path(str(status.get("workspacePath", "") or ""))
    root = declared.resolve()
    if not declared or declared.is_symlink() or not root.is_dir():
        raise RuntimeError("shader workspace root is not a managed directory")
    return root


def _workspace_file_target(root: Path, relative_path: str) -> Path:
    relative = str(relative_path or "").strip().replace("\\", "/")
    if not relative or relative.startswith("/") or ".." in relative.split("/"):
        raise ValueError("relative_path must point inside the workspace (no '..', no absolute path)")
    target = (root / relative).resolve()
    if not is_within(root, target):
        raise ValueError("relative_path escapes the workspace")
    return target


@mcp.tool()
def read_shader_workspace_file(
    workspace_path: str, relative_path: str, offset: int = 0, limit: int = 60_000
) -> str:
    """Read a UTF-8 text file inside a managed Shader workspace.

    Use it for ``source/shader.hlsl`` (the decompiler output), the proposal
    files opened by ``begin_shader_workspace_organization``, ``ai/*.json`` and
    the semantic context.  ``offset``/``limit`` are character positions; the
    result says whether more text remains.  Binary files and files outside the
    workspace are refused.
    """

    try:
        root = _workspace_root_for_files(workspace_path)
        target = _workspace_file_target(root, relative_path)
        if target.is_symlink() or not target.is_file():
            raise FileNotFoundError("no such regular file in the workspace: %s" % relative_path)
        size = target.stat().st_size
        if size > _WORKSPACE_FILE_MAX_BYTES:
            raise ValueError("file exceeds the readable size limit (%d bytes)" % size)
        raw = target.read_bytes()
        if b"\0" in raw[:4096]:
            raise ValueError("file is binary")
        text = raw.decode("utf-8", errors="replace")
        start = max(0, int(offset))
        span = max(1, min(int(limit), _WORKSPACE_FILE_MAX_CHARS))
        chunk = text[start : start + span]
        return _ok(
            {
                "schema": "renderdoc-ai-shader-workspace-file/v1",
                "workspacePath": str(root),
                "relativePath": str(target.relative_to(root)).replace("\\", "/"),
                "sizeBytes": size,
                "totalChars": len(text),
                "offset": start,
                "chars": len(chunk),
                "truncated": start + len(chunk) < len(text),
                "text": chunk,
                "applied": False,
            }
        )
    except Exception as error:
        return _shader_err(error, "workspace-status")


@mcp.tool()
def get_shader_workspace_outline(
    workspace_path: str,
    relative_path: str = "source/shader.hlsl",
    function: str = "",
    lines: str = "",
) -> str:
    """Skeleton-first view of a workspace HLSL file, then exact slices.

    With neither ``function`` nor ``lines`` it returns the outline:
    declarations (cbuffers with members and packoffsets, resources with
    registers, structs/semantics), every function with its line range, size,
    calls/callers, texture sample sites and anonymous-local count, where each
    cbuffer member / resource is used (line numbers), and how much of the
    file is still anonymous.  ``function=<name>`` returns that function's
    source with line numbers; ``lines=<start>-<end>`` returns a bounded range.
    Use this instead of reading a large file in chunks: it is cheaper and
    tells you what to name first.  Read-only.
    """

    try:
        from .shader_workspace.outline import MAX_SLICE_LINES, function_slice, line_slice, outline

        root = _workspace_root_for_files(workspace_path)
        target = _workspace_file_target(root, relative_path or "source/shader.hlsl")
        if target.is_symlink() or not target.is_file():
            raise FileNotFoundError("no such regular file in the workspace: %s" % relative_path)
        if target.stat().st_size > _WORKSPACE_FILE_MAX_BYTES:
            raise ValueError("file exceeds the readable size limit")
        raw = target.read_bytes()
        if b"\0" in raw[:4096]:
            raise ValueError("file is binary")
        text = raw.decode("utf-8", errors="replace")
        base = {"workspacePath": str(root), "relativePath": str(target.relative_to(root)).replace("\\", "/"),
                "applied": False}
        if function.strip():
            try:
                result = function_slice(text, function.strip())
            except KeyError as exc:
                raise FileNotFoundError(str(exc)) from exc
        elif lines.strip():
            match = re.fullmatch(r"\s*(\d+)\s*[-:]\s*(\d+)\s*", lines)
            if match is None:
                raise ValueError("lines must look like 120-260 (at most %d lines)" % MAX_SLICE_LINES)
            result = line_slice(text, int(match.group(1)), int(match.group(2)))
        else:
            entry = ""
            try:
                entry = str(_shader_workspace_service().status(str(root)).get("entryPoint") or "")
            except Exception:
                entry = ""
            result = outline(text, entry_point=entry)
        result.update(base)
        return _ok(result)
    except Exception as error:
        return _shader_err(error, "workspace-status")


def _organization_issues(root: Path, session: Mapping[str, Any], kind: str, decoded: Any) -> list[dict[str, Any]]:
    """Every validation issue for one organization document, never raising."""

    from .review_contract import reason as template_reason

    issues: list[dict[str, Any]] = []
    try:
        if kind == "analysis":
            from .shader_workspace.templates import render_analysis_fields
            _rendered, reasons = render_analysis_fields(decoded)
            issues.extend(dict(item) for item in reasons)
        elif kind == "module_descriptions":
            from .shader_workspace.flow_graph import validate_module_descriptions
            validate_module_descriptions(decoded)
        elif kind == "flow_spec":
            from .shader_workspace.equivalence import declared_symbols, validate_flow_spec, flow_value_bindings
            from .shader_workspace.flow_graph import _function_records
            baseline_text = _workspace_file_target(root, str(session.get("proposalSourcePath", ""))).read_text(
                encoding="utf-8", errors="replace")
            symbols = declared_symbols(baseline_text)
            values = flow_value_bindings(baseline_text)
            functions = [record["name"] for record in _function_records(baseline_text)]
            renames = session.get("renameMap") if isinstance(session.get("renameMap"), dict) else {}
            functions += [renames[f] for f in functions if f in renames]
            resources = symbols["resources"] + symbols["cbuffers"]
            resources += [renames[r] for r in resources if r in renames]
            _clean, reasons = validate_flow_spec(
                decoded, functions=functions, resources=resources, outputs=symbols["outputs"],
                inputs=values["inputs"], fields=values["fields"], source=baseline_text)
            issues.extend(dict(item) for item in reasons)
    except Exception as error:  # noqa: BLE001 - a draft reports, never raises
        issues.append(template_reason("shader-template-input", str(error), location="organization." + kind,
                                      direction="Fix this field and resubmit the draft."))
    return issues[:64]


def _write_organization_draft(
    root: Path, session_dir: Path, session: Mapping[str, Any], kind: str, content: str, draft_path: Path
) -> str:
    """Merge a partial JSON document over the previous draft and report all issues."""

    from .shader_workspace.io import atomic_write_bytes

    if kind not in {"analysis", "module_descriptions", "flow_spec"}:
        raise ValueError("draft mode applies to analysis, module_descriptions and flow_spec only")
    try:
        incoming = json.loads(content)
    except ValueError as exc:
        raise ValueError("draft content must be JSON: %s" % exc) from exc
    if not isinstance(incoming, dict):
        raise ValueError("draft content must be one JSON object (only the fields you are adding)")
    previous: dict[str, Any] = {}
    if draft_path.is_file() and not draft_path.is_symlink():
        try:
            loaded = json.loads(draft_path.read_text(encoding="utf-8"))
            previous = loaded if isinstance(loaded, dict) else {}
        except ValueError:
            previous = {}
    merged = dict(previous)
    for key, value in incoming.items():
        if key == "modules" and isinstance(value, list) and isinstance(previous.get("modules"), list):
            by_name = {str(item.get("name")): item for item in previous["modules"] if isinstance(item, dict)}
            for item in value:
                if isinstance(item, dict) and item.get("name") is not None:
                    by_name[str(item["name"])] = item
            merged["modules"] = list(by_name.values())
        elif key == "stages" and isinstance(value, list) and isinstance(previous.get("stages"), list):
            by_id = {str(item.get("id", item.get("name"))): item for item in previous["stages"] if isinstance(item, dict)}
            for item in value:
                if isinstance(item, dict):
                    by_id[str(item.get("id", item.get("name")))] = item
            merged["stages"] = list(by_id.values())
        else:
            merged[key] = value
    if kind == "analysis":
        from .shader_workspace.templates import analysis_template
        # Missing fields come from the template so the report names what is
        # still empty; unexpected fields stay so they are flagged too.
        merged = {**analysis_template(), **merged}
    data = json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8")
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("draft exceeds 2 MB")
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(draft_path, data)
    issues = _organization_issues(root, session, kind, merged)
    return _ok({
        "schema": "renderdoc-ai-shader-organization-draft/v1",
        "workspacePath": str(root),
        "organizationId": session.get("organizationId"),
        "kind": kind,
        "draftPath": str(draft_path),
        "bytes": len(data),
        "complete": not issues,
        "issues": issues,
        "issueCount": len(issues),
        "next": ("write_shader_workspace_organization_file(kind=%s, content=\"\", mode=\"final\") publishes the draft"
                 % kind) if not issues else "fix only the listed locations and send another draft with just those fields",
        "counted": False,
        "applied": False,
    })


class _ReviewBudgetStop(ValueError):
    """The organization review is waiting for a user decision; nothing is counted."""


@mcp.tool()
def write_shader_workspace_organization_file(
    workspace_path: str, organization_id: str, kind: str, content: str, mode: str = "final"
) -> str:
    """Write one AI-editable file of an open organization session.

    ``mode="draft"`` stores a partial JSON document (``analysis``,
    ``module_descriptions`` or ``flow_spec``) merged over the previous draft
    of the same kind, validates the merged result and returns **every** issue
    without counting a format error or a review round — build a large
    document in pieces and fix only what is flagged.  ``mode="final"`` (the
    default) validates and publishes; pass an empty ``content`` to finalize
    the accumulated draft.

    ``kind`` is one of ``analysis`` (JSON filled from ``analysisTemplate``;
    the Host renders the six Markdown sections; legacy Markdown inputs receive
    a migration error), ``module_descriptions`` (the exact
    ``moduleDescriptionsTemplate`` schema/schemaVersion/modules array),
    ``flow_spec`` (JSON filled from the ``flowSpecTemplate`` of
    ``begin_shader_workspace_organization``: inputs/resources/outputs and
    ordered stages with reads/writes/next — the Host validates names and
    renders the diagram in a fixed style), ``flowchart`` (only ``auto``;
    hand-written Mermaid cannot override the fixed rendering) and ``proposal``
    (a free-form HLSL rewrite requiring compile/ABI
    validation plus code/rename proof or bounded captured-output comparison).
    Prefer ``rename_shader_workspace_symbols`` for naming-only edits to use
    the fast deterministic path. Only files of an open session may be
    written and the whole content is replaced on every call.  Finish with
    ``review_shader_workspace_organization``.
    """

    try:
        root = _workspace_root_for_files(workspace_path)
        organization = str(organization_id or "").strip().lower()
        if not _is_lower_hex_value(organization, 32):
            raise ValueError("organization_id must be the 32-hex id returned by begin_shader_workspace_organization")
        kind = str(kind or "").strip().lower()
        selected = _ORGANIZATION_FILE_KINDS.get(kind)
        if selected is None:
            raise ValueError("kind must be one of: " + ", ".join(sorted(_ORGANIZATION_FILE_KINDS)))
        session_key, limit, description = selected
        session_dir = (root / "ai" / "organization-sessions" / organization).resolve()
        if not is_within(root, session_dir) or not session_dir.is_dir():
            raise FileNotFoundError("organization session %s does not exist in this workspace" % organization)
        session = read_json_object(session_dir / "session.json", 2 * 1024 * 1024, "organization session")
        if session.get("organizationId") != organization:
            raise RuntimeError("organization session id mismatch")
        if session.get("state") != "awaiting-proposal-review":
            raise RuntimeError(
                "organization session is not awaiting review (state=%s); begin a new session"
                % session.get("state")
            )
        from .shader_workspace.equivalence import ReviewRounds
        rounds = ReviewRounds(root)
        if rounds.used >= rounds.limit and not rounds.user_soft_publish_allowed:
            raise _ReviewBudgetStop(
                "The automatic review budget is exhausted; do not retry. Ask the user the question in "
                "verdict.userDecision and call decide_shader_organization_review_budget with their answer."
            )
        if rounds.template_exhausted():
            raise _ReviewBudgetStop(
                "Too many template-format rejections (%d); the submitted JSON keeps deviating from the "
                "returned template. Ask the user via verdict.userDecision instead of retrying."
                % rounds.template_errors
            )
        if kind == "flow_spec":
            target = session_dir / "flow-spec.json"
        else:
            target = _workspace_file_target(root, str(session.get(session_key, "") or ""))
        if not is_within(session_dir, target) and not is_within(root / "ai", target):
            raise RuntimeError("session artifact path is outside the AI-editable tree")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        mode_value = str(mode or "final").strip().lower()
        if mode_value not in {"final", "draft"}:
            raise ValueError("mode must be final or draft")
        draft_path = session_dir / "drafts" / ("%s.json" % kind)
        if mode_value == "draft":
            return _write_organization_draft(root, session_dir, session, kind, content, draft_path)
        if not content.strip() and draft_path.is_file() and not draft_path.is_symlink():
            content = draft_path.read_text(encoding="utf-8")
        data = content.encode("utf-8")
        if not data.strip():
            raise ValueError("content is empty")
        if len(data) > limit:
            raise ValueError("%s exceeds its size limit (%d bytes)" % (kind, limit))
        if kind == "module_descriptions":
            from .shader_workspace.flow_graph import validate_module_descriptions
            decoded = json.loads(content)
            validate_module_descriptions(decoded)
        if kind == "flow_spec":
            from .shader_workspace.equivalence import declared_symbols, validate_flow_spec, flow_value_bindings
            from .shader_workspace.flow_graph import _function_records

            try:
                decoded = json.loads(content)
            except ValueError as exc:
                raise ValueError("flow_spec must be JSON: %s" % exc) from exc
            baseline_text = _workspace_file_target(root, str(session.get("proposalSourcePath", ""))).read_text(
                encoding="utf-8", errors="replace"
            )
            symbols = declared_symbols(baseline_text)
            values = flow_value_bindings(baseline_text)
            functions = [record["name"] for record in _function_records(baseline_text)]
            renames = session.get("renameMap") if isinstance(session.get("renameMap"), dict) else {}
            functions += [renames[f] for f in functions if f in renames]
            resources = symbols["resources"] + symbols["cbuffers"]
            resources += [renames[r] for r in resources if r in renames]
            _clean, reasons = validate_flow_spec(
                decoded, functions=functions, resources=resources, outputs=symbols["outputs"],
                inputs=values["inputs"], fields=values["fields"], source=baseline_text
            )
            if reasons:
                raise ValueError(
                    "flow_spec rejected (%d issue(s), all listed): " % len(reasons)
                    + "; ".join("%s (%s) -> %s" % (r["message"], r["location"], r["direction"]) for r in reasons[:64])
                )
        if kind == "analysis":
            from .shader_workspace.templates import render_analysis_fields, analysis_template
            try:
                decoded = json.loads(content)
            except ValueError as exc:
                raise ValueError("analysis now requires JSON fields from analysisTemplate, not Markdown. Fill overview, inputsResources, computation, outputs, observedFacts and inferences; the Host renders formatting.") from exc
            rendered, reasons = render_analysis_fields(decoded)
            if reasons:
                raise ValueError("analysis rejected (%d issue(s), all listed): " % len(reasons)
                                 + "; ".join(r["message"] + " (" + r["location"] + ")" for r in reasons[:64]))
            data = rendered.encode("utf-8")
        if kind == "flowchart":
            if content.strip().lower() != "auto":
                raise ValueError("Custom Mermaid is no longer accepted. Fill flowSpecTemplate using kind=flow_spec; the Host owns diagram layout and style. Existing historical diagrams remain available for reading.")
            # Ask the review to publish the Host-generated fixed-style diagram.
            from .shader_workspace.organization import _FLOWCHART_PLACEHOLDER

            data = _FLOWCHART_PLACEHOLDER
        from .shader_workspace.io import atomic_write_bytes

        atomic_write_bytes(target, data)
        digest = hashlib.sha256(data).hexdigest()
        return _ok(
            {
                "schema": "renderdoc-ai-shader-organization-file/v1",
                "workspacePath": str(root),
                "organizationId": organization,
                "kind": kind,
                "path": str(target),
                "bytes": len(data),
                "sha256": digest,
                "description": description,
                "nextTool": "review_shader_workspace_organization",
                "applied": False,
            }
        )
    except Exception as error:
        from .review_contract import reason as template_reason
        from .shader_workspace.equivalence import build_verdict
        problems = locals().get("reasons") or [template_reason(
            "shader-template-input", str(error), location="organization." + str(kind),
            direction="Use the matching template returned by begin_shader_workspace_organization; only fill its declared fields.",
            target={"analysis": "analysisTemplate", "module_descriptions": "moduleDescriptionsTemplate", "flow_spec": "flowSpecTemplate", "flowchart": "flowSpecTemplate"}.get(str(kind), "proposalSourcePath"),
            example='{"claim":"One declared color target","evidence":"source:MainPS SV_Target0"}' if kind == "analysis" else '"inferred": true')]
        counter = locals().get("rounds")
        used = 0
        limit = counter.limit if counter is not None else 3
        template_state: dict[str, Any] | None = None
        if counter is None and "organization review state" in str(error):
            used = limit
            problems.append(template_reason("shader-review-state", str(error),
                location="ai/organization-rounds.json", direction="Stop automatic retries; preserve the invalid state for inspection.",
                target="valid, unredirected persistent review state"))
        if counter is not None and isinstance(error, _ReviewBudgetStop):
            # Already stopped: report the standing decision, count nothing.
            used = counter.used
            template_state = {"count": counter.template_errors, "limit": counter.template_limit,
                              "exhausted": counter.template_exhausted()}
            if used < limit:
                used = limit  # template exhaustion presents as a spent budget
        elif counter is not None:
            try:
                used = counter.used
                # A format/schema slip is the AI's own output error.  It is
                # counted separately and never consumes a content-review round.
                errors, template_limit = counter.record_template_error(problems)
                template_state = {"count": errors, "limit": template_limit, "exhausted": errors >= template_limit}
            except Exception as state_error:
                # Invalid persistent state fails closed, never reopening retries.
                used = limit
                problems.append(template_reason("shader-review-state", str(state_error),
                    location="ai/organization-rounds.json", direction="Stop automatic retries; preserve the review state for inspection.",
                    target="valid, unredirected persistent review state"))
        result = json.loads(_shader_err(error, "organization-review"))
        verdict = build_verdict(accepted=False, level="template-rejected", round_used=used, round_limit=limit,
                                reasons=problems, workspace_path=str(locals().get("root") or workspace_path))
        if template_state is not None:
            verdict["templateErrors"] = template_state
            if template_state["exhausted"] and not verdict.get("budgetExhausted"):
                from .shader_workspace.equivalence import review_budget_user_decision
                verdict["budgetExhausted"] = True
                verdict["userDecision"] = review_budget_user_decision(str(locals().get("root") or workspace_path), problems)
                verdict["nextStep"] = (
                    "Template submissions keep failing validation. Do not retry; ask the user the question in "
                    "verdict.userDecision and call decide_shader_organization_review_budget with their answer."
                )
            elif not verdict.get("budgetExhausted"):
                verdict["nextStep"] = (
                    "Fix the template fields listed under reasons and resubmit; format errors do not consume "
                    "content-review rounds (%d format attempt(s) left)." % max(0, template_state["limit"] - template_state["count"])
                )
        result["verdict"] = verdict
        return _ok(result)


@mcp.tool()
def decide_shader_organization_review_budget(
    workspace_path: str,
    decision: str,
    user_confirmation: str,
    extra_rounds: int = 3,
) -> str:
    """Apply the user's answer to an exhausted organization review budget.

    Call this only after the user answered the question carried in a review
    verdict's ``userDecision`` block; ``user_confirmation`` must quote their
    answer.  ``decision`` is ``extend`` (grant up to 3 more content-review
    rounds and reset the format-error counter), ``publish-baseline`` (no more
    organizing: the next ``review_shader_workspace_organization`` completes the
    session as a user-authorized baseline publication so registration and the
    editor can proceed) or ``stop`` (record the choice, change nothing).
    Grants are bounded per shader; never choose an option on the user's behalf.
    """

    try:
        from .shader_workspace.equivalence import ReviewRounds

        root = _workspace_root_for_files(workspace_path)
        choice = str(decision or "").strip().lower()
        if choice not in {"extend", "publish-baseline", "stop"}:
            raise ValueError("decision must be one of: extend, publish-baseline, stop")
        confirmation = str(user_confirmation or "").strip()
        if len(confirmation) < 2:
            raise ValueError("user_confirmation must quote the user's actual answer")
        if isinstance(extra_rounds, bool) or not isinstance(extra_rounds, int) or not 1 <= extra_rounds <= 3:
            raise ValueError("extra_rounds must be an integer between 1 and 3")
        rounds = ReviewRounds(root)
        if choice == "stop":
            state = {"rounds": rounds.used, "maxRounds": rounds.limit, "roundsLeft": max(0, rounds.limit - rounds.used),
                     "userSoftPublishAllowed": rounds.user_soft_publish_allowed, "grants": rounds.grants}
            next_tool = ""
        elif choice == "extend":
            state = rounds.grant(extra_rounds=extra_rounds, soft_publish_allowed=False, note="extend: " + confirmation[:300])
            next_tool = "write_shader_workspace_organization_file / rename_shader_workspace_symbols, then review_shader_workspace_organization"
        else:
            state = rounds.grant(extra_rounds=1, soft_publish_allowed=True, note="publish-baseline: " + confirmation[:300])
            next_tool = "review_shader_workspace_organization (completes as user-authorized baseline publication), then register_active_project_workspace"
        return _ok({
            "schema": "renderdoc-ai-shader-organization-budget-decision/v1",
            "workspacePath": str(root),
            "decision": choice,
            "userConfirmation": confirmation[:400],
            "review": state,
            "nextTool": next_tool,
            "applied": False,
        })
    except Exception as error:
        return _shader_err(error, "organization-review")


@mcp.tool()
def restructure_shader_workspace_source(
    workspace_path: str,
    organization_id: str,
    operation: str,
    start_line: int = 0,
    end_line: int = 0,
    name: str = "",
    new_name: str = "",
    comment: str = "",
) -> str:
    """Host-applied structural refactoring of the organization proposal.

    ``operation="extract"``: lines ``start_line..end_line`` of the *current*
    proposal (1-based, a complete statement range inside one function) become
    a new function ``name``; the Host infers inputs, inout and out parameters
    or the single return value from data flow, re-indents, replaces the range
    with a call, places the function above its caller with ``comment`` as its
    header, replays every recorded operation on the renamed baseline and
    compiles the result — a range that is not provably safe (a ``return``
    inside it, an escaping ``break``/``continue``, a cut through if/else or a
    multi-line statement, array locals) is refused with the reason and the
    session is unchanged.  ``undo`` drops the last operation, ``remove``
    drops the operation that created ``name``, ``rename_function`` renames
    an extracted function (``name`` -> ``new_name``), ``note`` records your
    one-sentence purpose for function ``name`` (in ``comment``), ``list``
    only reports.  Every result carries ``ledger`` — one compact row per
    function: purpose note, lines, origin, inputs/inouts/outputs, calls,
    callers, cbuffer members/resources used, anonymous locals, heuristic
    flags, status — which is the working memory for deciding merges and
    splits without re-reading code, plus ``structure`` with the raw metrics.
    Line numbers change after each operation: re-read them with
    ``get_shader_workspace_outline(relative_path=<proposalSourcePath>)``.
    Nothing here counts against the review budget or touches replay state.
    """

    try:
        return _ok(
            _shader_organization_manager().restructure(
                workspace_path,
                organization_id,
                operation,
                start_line=int(start_line or 0),
                end_line=int(end_line or 0),
                name=str(name or ""),
                new_name=str(new_name or ""),
                comment=str(comment or ""),
            )
        )
    except Exception as error:
        return _shader_err(error, "organization-review")


@mcp.tool()
def rename_shader_workspace_symbols(
    workspace_path: str,
    organization_id: str,
    renames: dict,
    comments: dict | None = None,
) -> str:
    """Rename identifiers and add function comments — applied by the Host.

    ``renames`` maps old identifier -> new identifier (cbuffers, cbuffer
    fields, textures, samplers, buffers, struct fields, functions, locals).
    ``comments`` maps function name -> one or two sentences placed above its
    definition.  The Host edits the baseline HLSL at the token level, so the
    result is *rename-only equivalent* by construction and the review accepts
    it even though DXIL metadata (and therefore the executable hash) changes
    with resource names.  Keywords, types, intrinsics, semantics and register
    slots are refused; collisions are reported by name.  Maps accumulate over
    the session; call as often as needed, then
    ``review_shader_workspace_organization``.

    Renamed cbuffer fields are also published as *display aliases* for the
    RenderDoc constant-buffer panel (result key ``cbufferAliases``).  Aliases
    follow the constant block's name and layout, so every shader and capture
    of the same game that binds that block shows them; an alias the user typed
    in the panel is kept over the AI's (``keptUser``).  The captured shader
    and its reflection never change.
    """

    try:
        if not isinstance(renames, dict):
            raise ValueError("renames must be an object {old: new}")
        if comments is not None and not isinstance(comments, dict):
            raise ValueError("comments must be an object {function: text}")
        result = _shader_organization_manager().rename(
            workspace_path, organization_id, renames, comments or {}
        )
        result["memberRules"] = _remember_member_rules(workspace_path, result)
        return _ok(result)
    except Exception as error:
        return _shader_err(error, "organization-begin")


def _remember_member_rules(workspace_path: str, rename_result: Mapping[str, Any]) -> dict[str, Any]:
    """Turn confirmed cbuffer member renames into rules for the shader's material family.

    Fires only when this shader was resolved to an unpacked asset; the rule
    then names the same offsets in every later shader of that family through
    the fixed repair's layout hint.
    """

    members = ((rename_result.get("cbufferAliases") or {}).get("memberRenames")) or []
    if not members:
        return {"recorded": 0, "reason": "no cbuffer member renames with known offsets"}
    try:
        status = _shader_workspace_service().status(workspace_path)
        memory = _shader_asset_memory(
            str(status.get("baseline", {}).get("rawSha256") or ""), str(status.get("stage") or ""),
            str(status.get("entryPoint") or ""))
    except Exception as error:
        return {"recorded": 0, "reason": "workspace status unavailable: %s" % str(error)[:160]}
    if memory is None:
        return {"recorded": 0, "reason": "no indexed asset root for the active project"}
    if not memory["familyKeys"]:
        return {"recorded": 0, "reason": "shader has no remembered asset resolution; submit_shader_asset_resolution first",
                "gameKey": memory["gameKey"]}
    confidence = max((float(item.get("confidence") or 0.0) for item in memory["resolutions"]), default=0.5)
    written = memory["store"].record_member_rules(
        game_key=memory["gameKey"], family_keys=memory["familyKeys"], members=members,
        source="ai-rename", confidence=confidence,
        shader_raw_sha256=str(status.get("baseline", {}).get("rawSha256") or ""))
    return {"recorded": written, "familyKeys": memory["familyKeys"], "confidence": confidence,
            "note": "later shaders of these material families receive these member names at decompile time"}


@mcp.tool()
def validate_shader_workspace_candidate(
    workspace_path: str,
    expected_revision: int | None = None,
    expected_candidate_raw_sha256: str = "",
) -> str:
    """Reflect and strictly validate a supported native candidate ABI.

    The service revalidates its trusted
    manifest, capture catalog, baseline, tool provenance, revision, and exact
    candidate bytes before asking RenderDoc to build/reflect a temporary target
    shader. Every physical ABI field must match; the temporary target is always
    freed and no replay replacement is attempted. Save-watcher callers may bind
    both an exact expected revision and candidate raw SHA-256; either both match
    or validation fails before accepting another revision.
    """
    try:
        unused_service, status, request, validated = (
            _fresh_shader_candidate_validation(
                workspace_path,
                expected_revision,
                expected_candidate_raw_sha256,
            )
        )
        return _ok(
            {
                "schema": "renderdoc-ultra-shader-abi-validation-mcp/v1",
                "operation": "validate",
                "binding": request["binding"],
                "validation": validated,
                "candidateState": {
                    "present": True,
                    "requiresReplayAbiValidation": validated[
                        "requiresReplayAbiValidation"
                    ],
                    "replayAbiValidated": validated["replayAbiValidated"],
                    "applied": False,
                },
                "renderDocReplacement": {
                    "attempted": False,
                    "applied": False,
                    "eventScoped": True,
                },
                "workspace": {
                    "workspacePath": status["workspacePath"],
                    "workspaceId": status["workspaceId"],
                    "captureId": status["capture"]["captureId"],
                    "captureSessionId": validated["captureSessionId"],
                    "revision": status["revision"],
                    "candidateRawSha256": request["binding"][
                        "candidateRawSha256"
                    ],
                    "stage": status["shader"]["stage"],
                    "entryPoint": status["shader"]["entryPoint"],
                    "originalResourceId": status["shader"]["resourceId"],
                    "originalRawSha256": status["baseline"]["rawSha256"],
                },
                # On an incompatible verdict the Host says where and why in
                # identity terms (semantic / register / cbuffer offset) with a
                # concrete HLSL hint per finding, so the repair can start at
                # once instead of asking what the difference list means.
                "explanation": (
                    (status.get("lastAbiAttempt") or {}).get("explanation")
                    if validated.get("compatible") is not True
                    else None
                ),
                "sourceRepairTool": (
                    "repair_shader_workspace_source_abi"
                    if validated.get("compatible") is not True
                    else None
                ),
            }
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def apply_shader_workspace_candidate(
    workspace_path: str,
    event_id: int,
    expected_revision: int,
    expected_candidate_raw_sha256: str,
) -> str:
    """ABI-validate and apply a candidate to one D3D11 draw/dispatch event.

    Each call obtains fresh replay reflection, revalidates the candidate bytes
    and immutable baseline/catalog provenance, then verifies the event's live
    shader resource, entry point, and raw DXBC hash. RenderDoc builds a target
    shader and configures ``SetEventShaderOverride``; global ``ReplaceResource``
    is never used. An incompatible candidate is returned as rejected and is not
    applied.
    """
    try:
        event_value = int(event_id)
        if event_value <= 0:
            raise ValueError("event_id must be a positive integer")
        service, status, request, validated = _fresh_shader_candidate_validation(
            workspace_path,
            expected_revision,
            expected_candidate_raw_sha256,
        )
        if validated.get("replayAbiValidated") is not True:
            return _ok(
                {
                    "schema": "renderdoc-ultra-shader-apply-mcp/v1",
                    "operation": "apply",
                    "binding": request["binding"],
                    "validation": validated,
                    "applied": False,
                    "reason": "candidate ABI is not exactly compatible",
                }
            )

        # An event must be an exact use of this physical shader in the trusted
        # catalog. This prevents a caller from repurposing a workspace as an
        # arbitrary shader injector for an unrelated draw.
        selected = select_unique_shader_for_use(
            status["capture"]["catalogPath"],
            event_value,
            status["stage"],
        )
        if selected.key.catalog_key != status["shader"]["shaderKey"]:
            raise RuntimeError(
                "event_id does not bind the workspace's exact captured shader"
            )

        # Final just-in-time validation immediately before the mutating RPC.
        status_now = service.status(workspace_path)
        request_now = service.candidate_reflection_request(workspace_path)
        if request_now != request:
            raise RuntimeError(
                "Shader workspace changed after ABI validation; apply again"
            )
        latest = status_now.get("latestCompiled") or {}
        gate = latest.get("abiGate") or {}
        report_sha256 = str(validated.get("abiReportSha256", "") or "")
        if any(
            (
                status_now["workspaceId"] != request["binding"]["workspaceId"],
                status_now["revision"] != request["binding"]["revision"],
                latest.get("rawSha256")
                != request["binding"]["candidateRawSha256"],
                gate.get("status") != "compatible",
                gate.get("requestId") != request["requestId"],
                gate.get("reportSha256") != report_sha256,
            )
        ):
            raise RuntimeError("Shader candidate ABI audit binding changed")

        apply_params = dict(request_now["params"])
        apply_params.update(
            {
                "expected_capture_id": request["binding"]["captureId"],
                "event_id": event_value,
                "expected_original_resource_id": status_now["shader"][
                    "resourceId"
                ],
                "expected_original_sha256": status_now["baseline"][
                    "rawSha256"
                ],
                "workspace_id": status_now["workspaceId"],
                "revision": status_now["revision"],
                "abi_request_id": request["requestId"],
                "abi_report_sha256": report_sha256,
            }
        )
        applied = ipc.call(
            "apply_shader_candidate_override", apply_params, timeout=300.0
        )
        try:
            applied = _validated_shader_apply_response(
                applied,
                status=status_now,
                request=request_now,
                event_id=event_value,
                report_sha256=report_sha256,
            )
        except Exception:
            # A malformed success response must never leave an untracked live
            # target shader behind. Roll it back if it contains a usable token.
            if isinstance(applied, dict) and applied.get("idempotent") is False:
                possible_id = str(applied.get("overrideId", "") or "")
                if len(possible_id) == 32 and all(
                    c in "0123456789abcdef" for c in possible_id
                ):
                    try:
                        ipc.call(
                            "rollback_shader_candidate_override",
                            {
                                "override_id": possible_id,
                                "expected_capture_id": request["binding"][
                                    "captureId"
                                ],
                            },
                            timeout=60.0,
                        )
                    except Exception:
                        pass
            raise

        record = {
            "overrideId": applied["overrideId"],
            "captureId": applied["captureId"],
            "captureSessionId": applied.get("captureSessionId"),
            "workspaceId": applied["workspaceId"],
            "workspacePath": status_now["workspacePath"],
            "revision": applied["revision"],
            "eventId": applied["eventId"],
            "stage": applied["stage"],
            "entryPoint": applied["entryPoint"],
            "api": applied["api"],
            "encoding": applied["encoding"],
            "originalResourceId": applied["originalResourceId"],
            "originalRawSha256": applied["originalRawSha256"],
            "candidateRawSha256": applied["candidateRawSha256"],
            "physicalAbiSha256": applied["physicalAbiSha256"],
            "shaderFamilyId": applied["shaderFamilyId"],
            "targetResourceId": applied["targetResourceId"],
        }
        with _SHADER_OVERRIDE_GUARD:
            superseded = applied.get("supersededOverrideId")
            if isinstance(superseded, str):
                _SHADER_OVERRIDE_RECORDS.pop(superseded, None)
            _SHADER_OVERRIDE_RECORDS[applied["overrideId"]] = record
        return _ok(
            {
                "schema": "renderdoc-ultra-shader-apply-mcp/v1",
                "operation": "apply",
                "binding": request["binding"],
                "validation": validated,
                "override": applied,
                "applied": True,
                "rollback": {
                    "tool": "rollback_shader_workspace_override",
                    "workspacePath": status_now["workspacePath"],
                    "overrideId": applied["overrideId"],
                },
            }
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def list_shader_workspace_family_uses(
    workspace_path: str, offset: int = 0, limit: int = 256
) -> str:
    """List trusted catalog events using the workspace's exact captured Shader.

    Results match ResourceId, stage, entry point, raw bytes and complete ABI;
    equal bytecode under another captured ResourceId is not included. Pagination
    never changes or truncates the underlying total.
    """
    try:
        status = _shader_workspace_service().status(workspace_path)
        if isinstance(offset, bool) or isinstance(limit, bool):
            raise ValueError("offset and limit must be integers")
        offset_value = int(offset)
        limit_value = int(limit)
        if offset_value < 0:
            raise ValueError("offset must be non-negative")
        if limit_value <= 0 or limit_value > 256:
            raise ValueError("limit must be between 1 and 256")
        event_ids = list_exact_shader_family_uses(
            status["capture"]["catalogPath"], status["shader"]["shaderKey"]
        )
        page = event_ids[offset_value : offset_value + limit_value]
        return _ok(
            {
                "schema": "renderdoc-ultra-shader-family-uses/v1",
                "workspaceId": status["workspaceId"],
                "captureId": status["capture"]["captureId"],
                "shader": status["shader"],
                "scope": "all-exact-uses",
                "total": len(event_ids),
                "offset": offset_value,
                "limit": limit_value,
                "eventIds": page,
                "hasMore": offset_value + len(page) < len(event_ids),
                "truncated": False,
            }
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def apply_shader_workspace_candidate_family(
    workspace_path: str,
    expected_revision: int,
    expected_candidate_raw_sha256: str,
    event_ids: list[int] | None = None,
    scope: str = "all-exact-uses",
) -> str:
    """Atomically apply one candidate to events sharing one captured Shader RID.

    This is the advanced selected-event operation when several draw/dispatch
    events use the same Shader in the capture. The Host validates the candidate ABI once,
    verifies every catalog event uses the workspace's exact original
    ResourceId/stage/entry/raw bytes/ABI, then asks the Bridge to build or reuse
    one target Shader. If any event fails, the whole batch is restored.

    The AI MCP client, an embedded chat action, or a RenderDoc button calls this
    tool. Users should not loop ``apply_shader_workspace_candidate`` themselves
    when preserving a captured Shader family matters.
    """
    try:
        service, status, request, validated = _fresh_shader_candidate_validation(
            workspace_path,
            expected_revision,
            expected_candidate_raw_sha256,
        )
        if validated.get("replayAbiValidated") is not True:
            return _ok(
                {
                    "schema": "renderdoc-ultra-shader-family-apply-mcp/v1",
                    "operation": "apply-family",
                    "binding": request["binding"],
                    "validation": validated,
                    "applied": False,
                    "reason": "candidate ABI is not exactly compatible",
                }
            )
        scope_value = str(scope or "").strip().lower()
        if scope_value not in ("all-exact-uses", "explicit"):
            raise ValueError("scope must be all-exact-uses or explicit")
        selected_event_ids = list(event_ids or [])
        if not selected_event_ids:
            if scope_value == "explicit":
                raise ValueError("explicit scope requires event_ids")
            selected_event_ids = list_exact_shader_family_uses(
                status["capture"]["catalogPath"],
                status["shader"]["shaderKey"],
            )
            if len(selected_event_ids) > 256:
                raise RuntimeError(
                    "Shader family has %d exact uses; call "
                    "list_shader_workspace_family_uses with pagination and pass "
                    "an explicit subset of at most 256 EventIds" %
                    len(selected_event_ids)
                )
        event_bindings = _shader_family_event_bindings(
            status=status, request=request, event_ids=selected_event_ids
        )

        # Repeat every mutable workspace binding check immediately before the
        # one transactional Bridge RPC.
        status_now = service.status(workspace_path)
        request_now = service.candidate_reflection_request(workspace_path)
        if request_now != request:
            raise RuntimeError(
                "Shader workspace changed after ABI validation; apply again"
            )
        latest = status_now.get("latestCompiled") or {}
        gate = latest.get("abiGate") or {}
        report_sha256 = str(validated.get("abiReportSha256", "") or "")
        if any(
            (
                status_now["workspaceId"] != request["binding"]["workspaceId"],
                status_now["revision"] != request["binding"]["revision"],
                latest.get("rawSha256")
                != request["binding"]["candidateRawSha256"],
                gate.get("status") != "compatible",
                gate.get("requestId") != request["requestId"],
                gate.get("reportSha256") != report_sha256,
            )
        ):
            raise RuntimeError("Shader candidate ABI audit binding changed")

        apply_params = dict(request_now["params"])
        apply_params.update(
            {
                "expected_capture_id": request["binding"]["captureId"],
                "expected_original_sha256": status_now["baseline"][
                    "rawSha256"
                ],
                "workspace_id": status_now["workspaceId"],
                "revision": status_now["revision"],
                "abi_request_id": request["requestId"],
                "abi_report_sha256": report_sha256,
                "events": [
                    {
                        "event_id": item["eventId"],
                        "expected_original_resource_id": item[
                            "originalResourceId"
                        ],
                        "expected_original_sha256": item[
                            "originalRawSha256"
                        ],
                    }
                    for item in event_bindings
                ],
            }
        )
        applied = ipc.call(
            "apply_shader_candidate_overrides", apply_params, timeout=300.0
        )
        try:
            applied = _validated_shader_family_apply_response(
                applied,
                status=status_now,
                request=request_now,
                event_bindings=event_bindings,
                report_sha256=report_sha256,
            )
        except Exception:
            possible_ids = []
            if isinstance(applied, dict):
                for item in list(applied.get("overrides", []) or []):
                    if not isinstance(item, dict) or item.get("idempotent") is not False:
                        continue
                    possible_id = str(
                        item.get("overrideId", "")
                    ).strip().lower()
                    if len(possible_id) == 32 and all(
                        character in "0123456789abcdef"
                        for character in possible_id
                    ):
                        possible_ids.append(possible_id)
            if possible_ids:
                try:
                    ipc.call(
                        "rollback_shader_candidate_overrides",
                        {
                            "override_ids": possible_ids,
                            "expected_capture_id": request["binding"][
                                "captureId"
                            ],
                        },
                        timeout=60.0,
                    )
                except Exception:
                    pass
            raise

        with _SHADER_OVERRIDE_GUARD:
            for override in applied["overrides"]:
                superseded = override.get("supersededOverrideId")
                if isinstance(superseded, str):
                    _SHADER_OVERRIDE_RECORDS.pop(superseded, None)
                _SHADER_OVERRIDE_RECORDS[override["overrideId"]] = {
                    "overrideId": override["overrideId"],
                    "captureId": override["captureId"],
                    "captureSessionId": override.get("captureSessionId"),
                    "workspaceId": override["workspaceId"],
                    "workspacePath": status_now["workspacePath"],
                    "revision": override["revision"],
                    "eventId": override["eventId"],
                    "stage": override["stage"],
                    "entryPoint": override["entryPoint"],
                    "api": override["api"],
                    "encoding": override["encoding"],
                    "originalResourceId": override["originalResourceId"],
                    "originalRawSha256": override["originalRawSha256"],
                    "candidateRawSha256": override["candidateRawSha256"],
                    "physicalAbiSha256": override["physicalAbiSha256"],
                    "shaderFamilyId": override.get("shaderFamilyId"),
                    "targetResourceId": override["targetResourceId"],
                }
        return _ok(
            {
                "schema": "renderdoc-ultra-shader-family-apply-mcp/v1",
                "operation": "apply-family",
                "binding": request["binding"],
                "validation": validated,
                "family": applied,
                "scope": (
                    "explicit" if event_ids else "all-exact-uses"
                ),
                "applied": True,
                "rollback": {
                    "tool": "rollback_shader_workspace_override_family",
                    "workspacePath": status_now["workspacePath"],
                    "overrideIds": [
                        item["overrideId"] for item in applied["overrides"]
                    ],
                },
            }
        )
    except Exception as error:
        return _err(error)


def _shared_live_abi_lease(workspace_path: str) -> dict[str, Any] | None:
    """Return one same-process, same-workspace initial ABI lease if active.

    The lease exists only after the ordinary shared-resource path completed a
    fresh replay reflection, persisted its immutable ABI report, and installed
    a Bridge-owned replacement.  It is intentionally process/capture local and
    disappears when the capture session changes.
    """

    workspace_key = _capture_path_key(workspace_path)
    with _SHADER_OVERRIDE_GUARD:
        matches = [
            copy.deepcopy(record)
            for record in _SHARED_SHADER_REPLACEMENT_RECORDS.values()
            if _capture_path_key(record.get("workspacePath", "")) == workspace_key
        ]
    if len(matches) != 1:
        return None
    record = matches[0]
    required_hex = (
        ("replacementId", 32),
        ("captureId", 64),
        ("captureSessionId", 32),
        ("workspaceId", 32),
        ("candidateRawSha256", 64),
        ("abiLeaseRequestId", 64),
        ("abiLeaseReportSha256", 64),
        ("abiLeaseCandidateRawSha256", 64),
        ("physicalAbiSha256", 64),
    )
    for name, length in required_hex:
        value = str(record.get(name, "") or "")
        if len(value) != length or any(
            character not in "0123456789abcdef" for character in value
        ):
            return None
    revision = record.get("abiLeaseRevision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        return None
    anchor_event_id = record.get("anchorEventId")
    if (
        isinstance(anchor_event_id, bool)
        or not isinstance(anchor_event_id, int)
        or anchor_event_id < 1
    ):
        return None
    return record


def _remember_shared_shader_replacement(
    applied: dict[str, Any],
    *,
    status: dict[str, Any],
    abi_lease: dict[str, Any],
) -> None:
    """Publish one validated replacement plus its initial live ABI lease."""

    record = {
        "replacementId": applied["replacementId"],
        "captureId": applied["captureId"],
        "captureSessionId": applied["captureSessionId"],
        "workspaceId": applied["workspaceId"],
        "workspacePath": status["workspacePath"],
        "revision": applied["revision"],
        "stage": applied["stage"],
        "entryPoint": applied["entryPoint"],
        "api": applied["api"],
        "encoding": applied["encoding"],
        "originalResourceId": applied["originalResourceId"],
        "originalRawSha256": applied["originalRawSha256"],
        "candidateRawSha256": applied["candidateRawSha256"],
        "anchorEventId": applied["anchorEventId"],
        "abiLeaseRequestId": abi_lease["abiLeaseRequestId"],
        "abiLeaseReportSha256": abi_lease["abiLeaseReportSha256"],
        "abiLeaseCandidateRawSha256": abi_lease[
            "abiLeaseCandidateRawSha256"
        ],
        "abiLeaseRevision": abi_lease["abiLeaseRevision"],
        "physicalAbiSha256": applied["physicalAbiSha256"],
        "shaderFamilyId": applied["shaderFamilyId"],
        "targetResourceId": applied["targetResourceId"],
    }
    with _SHADER_OVERRIDE_GUARD:
        superseded = applied.get("supersededReplacementId")
        if isinstance(superseded, str):
            _SHARED_SHADER_REPLACEMENT_RECORDS.pop(superseded, None)
        for token, previous in list(_SHARED_SHADER_REPLACEMENT_RECORDS.items()):
            if (
                token != applied["replacementId"]
                and previous.get("originalResourceId")
                == applied["originalResourceId"]
            ):
                _SHARED_SHADER_REPLACEMENT_RECORDS.pop(token, None)
        _SHARED_SHADER_REPLACEMENT_RECORDS[applied["replacementId"]] = record


def _apply_shader_workspace_shared_resource_with_live_abi_lease(
    workspace_path: str,
    *,
    expected_revision: int,
    expected_candidate_raw_sha256: str,
    operation_id: str = "",
) -> dict[str, Any] | None:
    """Apply one later live-edit save with a single adjacent physical ABI gate.

    ``None`` means that no current initial lease exists and the caller must use
    the ordinary full validation path.  Once a lease exists, failures are not
    silently retried through another route: the last-good replacement remains
    active and a newer save is required by live-edit policy.
    """

    operation_token = _strict_lower_hex(
        operation_id or uuid.uuid4().hex, 32, "operation_id"
    )
    expected = _normalise_expected_shader_candidate(
        expected_revision, expected_candidate_raw_sha256
    )
    if expected is None:
        raise RuntimeError("live ABI lease requires an exact candidate binding")
    lease = _shared_live_abi_lease(workspace_path)
    if lease is None:
        return None

    service = _shader_workspace_service()
    status = service.status(workspace_path)
    validated_source_sha256 = _require_shader_candidate_binding(status, expected)
    rebound = _ensure_workspace_capture_catalog_identity(
        workspace_path, status=status
    )
    capture_session_id = str(rebound.get("captureSessionId", "") or "")
    if len(capture_session_id) != 32 or any(
        character not in "0123456789abcdef"
        for character in capture_session_id
    ):
        raise RuntimeError("live capture rebinding omitted its session identity")
    _sync_shader_override_capture(capture_session_id)

    # A capture change clears process-local replacement/lease ownership.  Fall
    # back to the ordinary path before any mutation rather than trusting the
    # stale copy captured above.
    current_lease = _shared_live_abi_lease(workspace_path)
    if (
        current_lease is None
        or current_lease.get("replacementId") != lease.get("replacementId")
        or current_lease.get("captureSessionId") != capture_session_id
        or current_lease.get("workspaceId") != status.get("workspaceId")
        or current_lease.get("captureId")
        != status.get("capture", {}).get("captureId")
    ):
        return None
    lease = current_lease

    request = service.candidate_reflection_request(workspace_path)
    if any(
        (
            request.get("binding", {}).get("revision") != expected["revision"],
            request.get("binding", {}).get("candidateRawSha256")
            != expected["candidateRawSha256"],
            request.get("binding", {}).get("workspaceId")
            != lease["workspaceId"],
            request.get("binding", {}).get("captureId")
            != lease["captureId"],
            request.get("binding", {}).get("baselineAbiSha256")
            != lease["physicalAbiSha256"],
        )
    ):
        raise RuntimeError("live ABI lease no longer matches the workspace binding")

    anchor = _expected_shared_shader_anchor(
        status, request, lease["anchorEventId"]
    )
    if anchor["physicalAbiSha256"] != lease["physicalAbiSha256"]:
        raise RuntimeError("live ABI lease physical interface changed")

    status_now = service.status(workspace_path)
    source_sha256_now = _require_shader_candidate_binding(status_now, expected)
    request_now = service.candidate_reflection_request(workspace_path)
    if (
        source_sha256_now != validated_source_sha256
        or request_now != request
    ):
        raise RuntimeError("Shader workspace changed before leased live apply")

    apply_params = dict(request_now["params"])
    apply_params.update(
        {
            "expected_capture_id": request["binding"]["captureId"],
            "event_id": anchor["eventId"],
            "expected_original_resource_id": anchor["originalResourceId"],
            "expected_original_sha256": anchor["originalRawSha256"],
            "workspace_id": status_now["workspaceId"],
            "revision": status_now["revision"],
            # requestId stays bound to the current bytes.  The report hash is
            # explicitly an authorization lease from the first fully validated
            # replacement, not a claim that the old report describes new code.
            "abi_request_id": request["requestId"],
            "abi_report_sha256": lease["abiLeaseReportSha256"],
            "abi_validation_mode": "inline-physical-with-initial-lease",
            "abi_lease_request_id": lease["abiLeaseRequestId"],
            "abi_lease_candidate_sha256": lease[
                "abiLeaseCandidateRawSha256"
            ],
            "abi_lease_revision": lease["abiLeaseRevision"],
            "expected_physical_abi_sha256": lease["physicalAbiSha256"],
            "operation_id": operation_token,
        }
    )
    raw_applied: Any = None
    applied: dict[str, Any] | None = None
    try:
        raw_applied = ipc.call(
            "apply_shader_candidate_shared_resource", apply_params, timeout=300.0
        )
        applied = _validated_shared_shader_apply_response(
            raw_applied,
            status=status_now,
            request=request_now,
            anchor=anchor,
            report_sha256=lease["abiLeaseReportSha256"],
        )
        if applied.get("operationId") != operation_token:
            raise RuntimeError(
                "RenderDoc leased Shader operation token mismatch")
        inline_expected = {
            "abiValidationMode": "inline-physical-with-initial-lease",
            "preliminaryReflectionSkipped": True,
            "freshPhysicalAbiValidationPerformed": True,
            "abiLeaseRequestId": lease["abiLeaseRequestId"],
            "abiLeaseReportSha256": lease["abiLeaseReportSha256"],
            "abiLeaseCandidateRawSha256": lease[
                "abiLeaseCandidateRawSha256"
            ],
            "abiLeaseRevision": lease["abiLeaseRevision"],
        }
        for key, value in inline_expected.items():
            if applied.get(key) != value:
                raise RuntimeError(
                    "RenderDoc leased Shader response mismatch at %s" % key
                )
        status_after_apply = service.status(workspace_path)
        source_sha256_after_apply = _require_shader_candidate_binding(
            status_after_apply, expected
        )
        request_after_apply = service.candidate_reflection_request(workspace_path)
        if (
            source_sha256_after_apply != validated_source_sha256
            or request_after_apply != request
        ):
            raise RuntimeError(
                "Shader workspace changed during leased shared-resource apply; "
                "the new replacement was rolled back"
            )
    except Exception:
        if (
            isinstance(applied, dict)
            and applied.get("operationCreatedReplacement") is True
            and applied.get("operationId") == operation_token
            and applied.get("idempotent") is False
            and applied.get("metadataOnly") is False
            and applied.get("captureId") == request["binding"]["captureId"]
            and applied.get("workspaceId") == status_now["workspaceId"]
            and applied.get("originalResourceId") == anchor["originalResourceId"]
        ):
            possible_id = str(applied.get("replacementId", "") or "")
            if len(possible_id) == 32 and all(
                character in "0123456789abcdef" for character in possible_id
            ):
                try:
                    ipc.call(
                        "rollback_shader_candidate_shared_resource",
                        {
                            "replacement_id": possible_id,
                            "expected_capture_id": request["binding"]["captureId"],
                            "workspace_id": status_now["workspaceId"],
                        },
                        timeout=60.0,
                    )
                except Exception:
                    pass
        raise

    _remember_shared_shader_replacement(
        applied, status=status_now, abi_lease=lease
    )
    return {
        "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
        "operation": "apply-shared-resource",
        "operationId": operation_token,
        "binding": request["binding"],
        "validation": {
            "compatible": True,
            "requiresReplayAbiValidation": False,
            "replayAbiValidated": True,
            "validationScope": "inline-live-physical-interface",
            "preliminaryReplayReflectionSkipped": True,
            "freshPhysicalValidationPerformed": True,
            "freshLiveCaptureValidationPerformed": True,
            "abiLeaseRequestId": lease["abiLeaseRequestId"],
            "abiLeaseReportSha256": lease["abiLeaseReportSha256"],
            "abiLeaseCandidateRawSha256": lease[
                "abiLeaseCandidateRawSha256"
            ],
            "abiLeaseRevision": lease["abiLeaseRevision"],
        },
        "replacement": applied,
        "scope": "captured-resource",
        "applied": True,
        "rollback": {
            "tool": "rollback_shader_workspace_shared_resource",
            "workspacePath": status_now["workspacePath"],
            "replacementId": applied["replacementId"],
        },
    }


@mcp.tool()
def apply_shader_workspace_shared_resource(
    workspace_path: str,
    expected_revision: int | None = None,
    expected_candidate_raw_sha256: str = "",
    operation_id: str = "",
    expected_anchor_event_id: int | None = None,
) -> str:
    """Apply a candidate to every checked use of one captured Shader ResourceId.

    This sharing-preserving mode is the default for editor save/apply flows.
    The Host performs fresh workspace, capture, candidate, ABI and live-event
    validation, then the Bridge builds one target and calls RenderDoc's
    resource-scoped replacement. D3D12, Vulkan, and OpenGL require checked
    transactions which rebuild every supported affected PSO/program before
    committing derived mappings; D3D12 also reuses the captured root signature.
    Distinct captured
    ResourceIds are never merged, even when their bytecode is identical. The
    original captured Shader is retained for rollback and is never freed. The
    optional expected revision/raw SHA-256 pair is used by save auto-apply to
    reject a newer candidate published between watcher polling and mutation.
    Project restore also supplies its inventory-frozen representative EID;
    that path validates only this one live PSO/ABI anchor and never scans the
    catalog's complete EID-use table to rediscover an anchor.
    """
    try:
        operation_token = _strict_lower_hex(
            operation_id or uuid.uuid4().hex, 32, "operation_id"
        )
        expected = _normalise_expected_shader_candidate(
            expected_revision, expected_candidate_raw_sha256
        )
        service, status, request, validated = _fresh_shader_candidate_validation(
            workspace_path,
            expected_revision,
            expected_candidate_raw_sha256,
        )
        validated_source_sha256 = _require_shader_candidate_binding(
            status, expected
        )
        if validated.get("replayAbiValidated") is not True:
            return _ok(
                {
                    "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
                    "operation": "apply-shared-resource",
                    "binding": request["binding"],
                    "validation": validated,
                    "applied": False,
                    "reason": "candidate ABI is not exactly compatible",
                }
            )

        if expected_anchor_event_id is None:
            exact_uses = list_exact_shader_family_uses(
                status["capture"]["catalogPath"], status["shader"]["shaderKey"]
            )
            if not exact_uses:
                raise RuntimeError(
                    "The workspace Shader has no exact live anchor event in its catalog"
                )
            anchor_event_id = min(int(value) for value in exact_uses)
            anchor = _shader_family_event_bindings(
                status=status, request=request, event_ids=[anchor_event_id]
            )[0]
        else:
            anchor = _expected_shared_shader_anchor(
                status, request, expected_anchor_event_id
            )

        # Repeat all mutable workspace/audit checks immediately before the one
        # resource-scoped Bridge mutation.
        status_now = service.status(workspace_path)
        source_sha256_now = _require_shader_candidate_binding(
            status_now, expected
        )
        if source_sha256_now != validated_source_sha256:
            raise RuntimeError(
                "Shader source changed after ABI validation; apply again"
            )
        request_now = service.candidate_reflection_request(workspace_path)
        if request_now != request:
            raise RuntimeError(
                "Shader workspace changed after ABI validation; apply again"
            )
        latest = status_now.get("latestCompiled") or {}
        gate = latest.get("abiGate") or {}
        report_sha256 = str(validated.get("abiReportSha256", "") or "")
        if any(
            (
                status_now["workspaceId"] != request["binding"]["workspaceId"],
                status_now["revision"] != request["binding"]["revision"],
                latest.get("rawSha256")
                != request["binding"]["candidateRawSha256"],
                gate.get("status") != "compatible",
                gate.get("requestId") != request["requestId"],
                gate.get("reportSha256") != report_sha256,
            )
        ):
            raise RuntimeError("Shader candidate ABI audit binding changed")

        apply_params = dict(request_now["params"])
        apply_params.update(
            {
                "expected_capture_id": request["binding"]["captureId"],
                "event_id": anchor["eventId"],
                "expected_original_resource_id": anchor[
                    "originalResourceId"
                ],
                "expected_original_sha256": anchor["originalRawSha256"],
                "workspace_id": status_now["workspaceId"],
                "revision": status_now["revision"],
                "abi_request_id": request["requestId"],
                "abi_report_sha256": report_sha256,
                "operation_id": operation_token,
            }
        )
        raw_applied = ipc.call(
            "apply_shader_candidate_shared_resource", apply_params, timeout=300.0
        )
        applied: dict[str, Any] | None = None
        try:
            applied = _validated_shared_shader_apply_response(
                raw_applied,
                status=status_now,
                request=request_now,
                anchor=anchor,
                report_sha256=report_sha256,
            )
            if applied.get("operationId") != operation_token:
                raise RuntimeError(
                    "RenderDoc Shader operation token mismatch")
            # The Bridge mutation may take long enough for another IDE save to
            # land.  Do not publish that replacement as current unless the
            # canonical source and candidate remain the exact ABI-validated
            # pair. A newly-created owned replacement is rolled back below.
            status_after_apply = service.status(workspace_path)
            source_sha256_after_apply = _require_shader_candidate_binding(
                status_after_apply, expected
            )
            request_after_apply = service.candidate_reflection_request(
                workspace_path
            )
            if (
                source_sha256_after_apply != validated_source_sha256
                or request_after_apply != request
            ):
                raise RuntimeError(
                    "Shader workspace changed during shared-resource apply; "
                    "the new replacement was rolled back"
                )
        except Exception:
            # Only a response that proves it created a new owned replacement
            # may be auto-rolled back. Never remove an idempotent/foreign owner
            # based on malformed evidence.
            if (
                isinstance(applied, dict)
                and applied.get("operationCreatedReplacement") is True
                and applied.get("operationId") == operation_token
                and applied.get("idempotent") is False
                and applied.get("metadataOnly") is False
                and applied.get("captureId") == request["binding"]["captureId"]
                and applied.get("workspaceId") == status_now["workspaceId"]
                and applied.get("originalResourceId")
                == anchor["originalResourceId"]
            ):
                possible_id = str(applied.get("replacementId", "") or "")
                if len(possible_id) == 32 and all(
                    character in "0123456789abcdef"
                    for character in possible_id
                ):
                    try:
                        ipc.call(
                            "rollback_shader_candidate_shared_resource",
                            {
                                "replacement_id": possible_id,
                                "expected_capture_id": request["binding"][
                                    "captureId"
                                ],
                                "workspace_id": status_now["workspaceId"],
                            },
                            timeout=60.0,
                        )
                    except Exception:
                        pass
            raise

        _remember_shared_shader_replacement(
            applied,
            status=status_now,
            abi_lease={
                "abiLeaseRequestId": request["requestId"],
                "abiLeaseReportSha256": report_sha256,
                "abiLeaseCandidateRawSha256": request["binding"][
                    "candidateRawSha256"
                ],
                "abiLeaseRevision": request["binding"]["revision"],
            },
        )
        route_memory = _remember_route_success(
            status_now,
            details={
                "candidateRawSha256": request["binding"]["candidateRawSha256"],
                "revision": request["binding"]["revision"],
            },
        )
        response = {
            "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
            "operation": "apply-shared-resource",
            "operationId": operation_token,
            "binding": request["binding"],
            "validation": validated,
            "replacement": applied,
            "scope": "captured-resource",
            "applied": True,
            "routeMemory": route_memory,
            "rollback": {
                "tool": "rollback_shader_workspace_shared_resource",
                "workspacePath": status_now["workspacePath"],
                "replacementId": applied["replacementId"],
            },
        }
        return _ok(response)
    except Exception as error:
        return _err(error)


@mcp.tool()
def register_active_project_shader_replacement(
    workspace_path: str,
    expected_revision: int,
    expected_candidate_raw_sha256: str,
    replacement_id: str,
) -> str:
    """Explicitly persist one already-applied shared Shader replacement.

    This project-effect endpoint does not apply or replace a Shader. It binds
    the caller-supplied revision, candidate SHA-256, and replacement token to
    the exact live shared-resource replacement, commits the reviewed workspace
    knowledge, then refreshes the optional RDC project association cache.
    """

    try:
        from .project_restore import sync_active_shader_workspace_after_apply

        return _ok(
            sync_active_shader_workspace_after_apply(
                workspace_path,
                expected_revision=expected_revision,
                expected_candidate_raw_sha256=expected_candidate_raw_sha256,
                replacement_id=replacement_id,
            )
        )
    except Exception as error:
        return _err(error)


def _valid_shader_rollback_physical_result(
    result: Any,
    *,
    status: Mapping[str, Any],
    local: Mapping[str, Any] | None,
    resource_scoped: bool,
) -> bool:
    """Validate a Bridge rollback tombstone against its physical executable."""

    if not isinstance(result, dict):
        return False
    try:
        stage = normalize_stage(result.get("stage"))
    except (TypeError, ValueError):
        return False
    entry_point = result.get("entryPoint")
    api = str(result.get("api", "") or "").strip().casefold()
    encoding = str(result.get("encoding", "") or "").strip().casefold()
    already_restored = result.get("alreadyRestored")
    idempotent = result.get("idempotentRollback")
    if not (
        stage
        and isinstance(entry_point, str)
        and 0 < len(entry_point) <= 512
        and (
            (api == "d3d11" and encoding == "dxbc")
            or (api == "d3d12" and encoding in {"dxbc", "dxil"})
            or (api == "vulkan" and encoding == "spirv")
            or (api == "opengl" and encoding == "opengl-spirv")
        )
        and type(result.get("originalResourceId")) is int
        and result["originalResourceId"] > 0
        and type(result.get("targetResourceId")) is int
        and result["targetResourceId"] > 0
        and all(
            isinstance(result.get(name), str)
            and len(result[name]) == 64
            and all(character in "0123456789abcdef" for character in result[name])
            for name in (
                "originalRawSha256",
                "candidateRawSha256",
                "physicalAbiSha256",
                "shaderFamilyId",
            )
        )
        and result.get("resourceScoped") is resource_scoped
        and result.get("eventScoped") is (not resource_scoped)
        and type(already_restored) is bool
        and type(idempotent) is bool
        and already_restored is idempotent
        and result.get("cleared") is True
        and result.get("originalShaderPreserved") is True
        and type(result.get("targetFreed")) is bool
    ):
        return False

    shader = status.get("shader")
    baseline = status.get("baseline")
    if isinstance(shader, Mapping):
        status_stage = shader.get("stage", status.get("stage"))
        try:
            if status_stage and normalize_stage(status_stage) != stage:
                return False
        except (TypeError, ValueError):
            return False
        if shader.get("entryPoint") not in (None, "", entry_point):
            return False
        if shader.get("resourceId") not in (None, result["originalResourceId"]):
            return False
    expected_original_raw = (
        baseline.get("rawSha256") if isinstance(baseline, Mapping) else None
    )
    if expected_original_raw not in (None, "", result["originalRawSha256"]):
        return False

    if isinstance(local, Mapping):
        observed = {
            "stage": stage,
            "entryPoint": entry_point,
            "api": api,
            "encoding": encoding,
            "originalResourceId": result["originalResourceId"],
            "originalRawSha256": result["originalRawSha256"],
            "candidateRawSha256": result["candidateRawSha256"],
            "physicalAbiSha256": result["physicalAbiSha256"],
            "shaderFamilyId": result["shaderFamilyId"],
            "targetResourceId": result["targetResourceId"],
        }
        if any(
            local.get(name) not in (None, "") and local.get(name) != value
            for name, value in observed.items()
        ):
            return False
    return True


@mcp.tool()
def rollback_shader_workspace_shared_resource(
    workspace_path: str, replacement_id: str
) -> str:
    """Restore the captured Shader for all uses and free its generated target."""
    try:
        status = _shader_workspace_service().status(workspace_path)
        capture_id = str(status["capture"]["captureId"])
        live = ipc.call("get_capture_status", {}, timeout=30.0)
        if not isinstance(live, dict) or live.get("loaded") is not True:
            _sync_shader_override_capture("")
            raise RuntimeError("No capture is loaded in RenderDoc")
        live_capture_path = str(live.get("capturePath", "") or "")
        live_session_id = str(live.get("captureSessionId", "") or "")
        _sync_shader_override_capture(live_session_id)
        if (
            not live_capture_path
            or _capture_path_key(live_capture_path)
            != _capture_path_key(str(status["capture"]["capturePath"]))
            or capture_id
            not in list(live.get("knownCatalogCaptureIds", []) or [])
        ):
            raise RuntimeError(
                "The active RenderDoc capture does not match this workspace"
            )
        token = str(replacement_id or "").strip().lower()
        if len(token) != 32 or any(
            character not in "0123456789abcdef" for character in token
        ):
            raise ValueError(
                "replacement_id must be a 32-character lowercase hex ID"
            )
        with _SHADER_OVERRIDE_GUARD:
            local = _SHARED_SHADER_REPLACEMENT_RECORDS.get(token)
        if local is not None and any(
            (
                local.get("captureId") != capture_id,
                local.get("workspaceId") != status["workspaceId"],
                local.get("captureSessionId") != live_session_id,
            )
        ):
            raise RuntimeError(
                "replacement_id is not bound to this shader workspace"
            )
        result = ipc.call(
            "rollback_shader_candidate_shared_resource",
            {
                "replacement_id": token,
                "expected_capture_id": capture_id,
                "workspace_id": status["workspaceId"],
            },
            timeout=60.0,
        )
        if not isinstance(result, dict) or any(
            (
                result.get("schema")
                != "renderdoc-ultra-shared-shader-replacement/v1",
                result.get("schemaVersion") != 1,
                result.get("replacementId") != token,
                result.get("captureId") != capture_id,
                result.get("captureSessionId") != live_session_id,
                result.get("workspaceId") != status["workspaceId"],
                not _valid_shader_rollback_physical_result(
                    result,
                    status=status,
                    local=local,
                    resource_scoped=True,
                ),
            )
        ):
            raise RuntimeError(
                "RenderDoc returned an invalid shared Shader rollback result"
            )
        with _SHADER_OVERRIDE_GUARD:
            _SHARED_SHADER_REPLACEMENT_RECORDS.pop(token, None)
        return _ok(
            {
                "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
                "operation": "rollback-shared-resource",
                "workspaceId": status["workspaceId"],
                "captureId": capture_id,
                "replacement": result,
                "applied": False,
            }
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def rollback_shader_workspace_override(
    workspace_path: str, override_id: str
) -> str:
    """Clear one event-scoped override and free its RenderDoc target shader."""
    try:
        status = _shader_workspace_service().status(workspace_path)
        capture_id = str(status["capture"]["captureId"])
        live = ipc.call("get_capture_status", {}, timeout=30.0)
        if not isinstance(live, dict) or live.get("loaded") is not True:
            _sync_shader_override_capture("")
            raise RuntimeError("No capture is loaded in RenderDoc")
        live_capture_path = str(live.get("capturePath", "") or "")
        live_session_id = str(live.get("captureSessionId", "") or "")
        workspace_capture_path = str(status["capture"]["capturePath"])
        _sync_shader_override_capture(live_session_id)
        if (
            not live_capture_path
            or _capture_path_key(live_capture_path)
            != _capture_path_key(workspace_capture_path)
            or capture_id not in list(live.get("knownCatalogCaptureIds", []) or [])
        ):
            raise RuntimeError(
                "The active RenderDoc capture does not match this workspace"
            )

        token = str(override_id or "").strip().lower()
        if len(token) != 32 or any(
            character not in "0123456789abcdef" for character in token
        ):
            raise ValueError("override_id must be a 32-character lowercase hex ID")
        with _SHADER_OVERRIDE_GUARD:
            local = _SHADER_OVERRIDE_RECORDS.get(token)
        if local is not None and any(
            (
                local.get("captureId") != capture_id,
                local.get("workspaceId") != status["workspaceId"],
                local.get("captureSessionId") != live_session_id,
            )
        ):
            raise RuntimeError("override_id is not bound to this shader workspace")
        try:
            result = ipc.call(
                "rollback_shader_candidate_override",
                {
                    "override_id": token,
                    "expected_capture_id": capture_id,
                },
                timeout=60.0,
            )
        except Exception:
            # Bridge rollback is transactional: on an RPC error it restores the
            # prior override when possible. Keep the local token so a caller can
            # inspect/retry instead of orphaning a still-live family reference.
            raise
        if not isinstance(result, dict) or any(
            (
                result.get("schema")
                != "renderdoc-ultra-event-shader-override/v1",
                result.get("schemaVersion") != 1,
                result.get("overrideId") != token,
                result.get("captureId") != capture_id,
                result.get("captureSessionId") != live_session_id,
                not _valid_shader_rollback_physical_result(
                    result,
                    status=status,
                    local=local,
                    resource_scoped=False,
                ),
            )
        ):
            raise RuntimeError("RenderDoc returned an invalid shader rollback result")
        with _SHADER_OVERRIDE_GUARD:
            _SHADER_OVERRIDE_RECORDS.pop(token, None)
        return _ok(
            {
                "schema": "renderdoc-ultra-shader-apply-mcp/v1",
                "operation": "rollback",
                "workspaceId": status["workspaceId"],
                "captureId": capture_id,
                "override": result,
                "applied": False,
            }
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def rollback_shader_workspace_override_family(
    workspace_path: str, override_ids: list[str]
) -> str:
    """Atomically roll back event overrides and release the last family target.

    A target remains alive while any event in the family still references it.
    The captured/original Shader ResourceId is never freed.
    """
    try:
        status = _shader_workspace_service().status(workspace_path)
        capture_id = str(status["capture"]["captureId"])
        live = ipc.call("get_capture_status", {}, timeout=30.0)
        if not isinstance(live, dict) or live.get("loaded") is not True:
            _sync_shader_override_capture("")
            raise RuntimeError("No capture is loaded in RenderDoc")
        live_capture_path = str(live.get("capturePath", "") or "")
        live_session_id = str(live.get("captureSessionId", "") or "")
        _sync_shader_override_capture(live_session_id)
        if (
            not live_capture_path
            or _capture_path_key(live_capture_path)
            != _capture_path_key(str(status["capture"]["capturePath"]))
            or capture_id not in list(live.get("knownCatalogCaptureIds", []) or [])
        ):
            raise RuntimeError(
                "The active RenderDoc capture does not match this workspace"
            )
        if not isinstance(override_ids, list) or not override_ids:
            raise ValueError("override_ids must be a non-empty array")
        if len(override_ids) > 256:
            raise ValueError("override_ids exceeds the 256 override limit")
        tokens = []
        for value in override_ids:
            token = str(value or "").strip().lower()
            if len(token) != 32 or any(
                character not in "0123456789abcdef" for character in token
            ):
                raise ValueError(
                    "override_ids must contain 32-character lowercase hex IDs"
                )
            if token in tokens:
                raise ValueError("override_ids must not contain duplicates")
            tokens.append(token)
        with _SHADER_OVERRIDE_GUARD:
            local_by_token = {
                token: copy.deepcopy(_SHADER_OVERRIDE_RECORDS.get(token))
                for token in tokens
            }
            for token in tokens:
                local = local_by_token[token]
                if local is not None and any(
                    (
                        local.get("captureId") != capture_id,
                        local.get("workspaceId") != status["workspaceId"],
                        local.get("captureSessionId") != live_session_id,
                    )
                ):
                    raise RuntimeError(
                        "override_id is not bound to this shader workspace"
                    )

        result = ipc.call(
            "rollback_shader_candidate_overrides",
            {
                "override_ids": tokens,
                "expected_capture_id": capture_id,
            },
            timeout=60.0,
        )
        if not isinstance(result, dict) or any(
            (
                result.get("schema")
                != "renderdoc-ultra-event-shader-rollback-batch/v1",
                result.get("schemaVersion") != 1,
                result.get("captureId") != capture_id,
                result.get("captureSessionId") != live_session_id,
                result.get("transactional") is not True,
                result.get("originalShadersPreserved") is not True,
                type(result.get("alreadyRestored")) is not bool,
                type(result.get("idempotentRollback")) is not bool,
                result.get("alreadyRestored")
                is not result.get("idempotentRollback"),
            )
        ):
            raise RuntimeError("RenderDoc returned an invalid family rollback result")
        overrides = result.get("overrides")
        if not isinstance(overrides, list) or len(overrides) != len(tokens):
            raise RuntimeError("RenderDoc returned an incomplete family rollback")
        returned = set()
        for item in overrides:
            if not isinstance(item, dict) or any(
                (
                    item.get("schema")
                    != "renderdoc-ultra-event-shader-override/v1",
                    item.get("schemaVersion") != 1,
                    item.get("captureId") != capture_id,
                    item.get("captureSessionId") != live_session_id,
                    item.get("overrideId") not in tokens,
                    not _valid_shader_rollback_physical_result(
                        item,
                        status=status,
                        local=local_by_token.get(item.get("overrideId")),
                        resource_scoped=False,
                    ),
                )
            ):
                raise RuntimeError("RenderDoc returned an invalid family rollback item")
            returned.add(item["overrideId"])
        if returned != set(tokens):
            raise RuntimeError("RenderDoc family rollback tokens do not match")
        with _SHADER_OVERRIDE_GUARD:
            for token in tokens:
                _SHADER_OVERRIDE_RECORDS.pop(token, None)
        return _ok(
            {
                "schema": "renderdoc-ultra-shader-family-apply-mcp/v1",
                "operation": "rollback-family",
                "workspaceId": status["workspaceId"],
                "captureId": capture_id,
                "family": result,
                "applied": False,
            }
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def prepare_material_session(
    event_id: int,
    constant_block_index: int,
    stage: str = "fragment",
    expected_capture_id: str = "",
) -> str:
    """Create an immutable, event-scoped local D3D11/D3D12 CBuffer session.

    The selected constant block is resolved from live shader reflection. The
    full valid binding window is copied. This preserves D3D11.1 ring-buffer
    offsets and uses a reflection-bounded window for D3D12 root CBVs instead
    of copying the remainder of an upload ring. D3D12 descriptor-table and
    root CBVs are supported; root constants are excluded. Only reflected
    32-bit float/signed-int/unsigned-int scalar and vec2/vec3/vec4 leaves (and
    arrays with explicit stride) become editable.

    Args:
        event_id: Exact draw/dispatch event to replay.
        constant_block_index: Index in the selected shader reflection.
        stage: D3D11/D3D12 shader stage; ``pixel`` is accepted as ``fragment``.
            D3D12 task/amplification and mesh stages are accepted as ``task``
            and ``mesh``.
        expected_capture_id: Optional exported catalog SHA-256. If omitted,
            the active catalog identity is used only when unambiguous.
    """

    try:
        return _ok(
            _material_session_manager().prepare(
                event_id,
                stage,
                constant_block_index,
                expected_capture_id,
            )
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def list_material_sessions() -> str:
    """List capture-valid material sessions and their typed parameters."""

    try:
        return _ok(_material_session_manager().list())
    except Exception as error:
        return _err(error)


@mcp.tool()
def get_material_session_status(session_id: str) -> str:
    """Read one material session, current values, hashes, and replay revision."""

    try:
        return _ok(_material_session_manager().status(session_id))
    except Exception as error:
        return _err(error)


@mcp.tool()
def update_material_parameter(
    session_id: str, parameter: str, value: Any
) -> str:
    """Patch one typed parameter and immediately replay its exact event.

    ``parameter`` is a bridge-issued parameter ID or an exact unique reflected
    path/display name. ``value`` is one number for a scalar or an exact-length
    JSON array for a vector. Callers cannot supply a register, byte offset,
    scalar encoding, or raw CBuffer bytes.
    """

    try:
        updated = _material_session_manager().update(
            session_id, parameter, value
        )
        response = dict(updated)
        response["request"] = {
            "parameter": parameter,
            "value": copy.deepcopy(value),
        }
        return _ok(response)
    except Exception as error:
        return _err(error)


@mcp.tool()
def update_material_parameters(session_id: str, updates: list[dict[str, Any]]) -> str:
    """Apply multiple typed material parameters in one replay transaction.

    Each item contains only ``parameter`` (a bridge-issued ID or exact unique
    reflected name) and ``value``. The host validates and packs the entire
    bounded set before one cumulative CBuffer apply, so a bad item cannot
    partially update replay state. Callers still cannot provide registers,
    byte offsets, scalar encodings, or raw CBuffer bytes.
    """

    try:
        updated = _material_session_manager().update_many(session_id, updates)
        response = dict(updated)
        response["request"] = {"updates": copy.deepcopy(updates)}
        return _ok(response)
    except Exception as error:
        return _err(error)


@mcp.tool()
def register_active_project_material_session(
    session_id: str, parameter_ids: list[str]
) -> str:
    """Explicitly persist selected values from an already-updated CBuffer session.

    This project-effect endpoint never changes live CBuffer bytes. It validates
    the active capture/project/session identities, commits only the requested
    bridge-issued parameter IDs, and refreshes the optional RDC association
    cache after the durable project commit.
    """

    try:
        from .project_restore import sync_active_material_session

        return _ok(sync_active_material_session(session_id, parameter_ids))
    except Exception as error:
        return _err(error)


@mcp.tool()
def rollback_material_session(session_id: str) -> str:
    """Clear the event CBuffer shadow and release the immutable snapshot."""

    try:
        return _ok(_material_session_manager().rollback(session_id))
    except Exception as error:
        return _err(error)


@mcp.tool()
def clear_all_material_sessions(expected_capture_id: str = "") -> str:
    """Explicitly clear all event CBuffer shadows/snapshots in this capture.

    This is the recovery path after an MCP host crash or restart has lost its
    in-memory session IDs. It never takes over an existing session: the caller
    deliberately clears every material session in the active capture lifecycle.
    """

    try:
        return _ok(
            _material_session_manager().clear_all(expected_capture_id)
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def start_shader_workspace_watch(
    workspace_path: str, debounce_ms: int = 100
) -> str:
    """Watch the manifest-bound editable source and compile stable saves.

    The debounce is restricted to 100-5000 ms. The MCP caller cannot supply a
    source path, command, executable, registry, argument, or environment. The
    source is the path bound in the manifest, such as ``source/shader.hlsl``,
    ``source/shader.dxbc.asm``, or ``source/shader.dxil.ll``. A DXBC/DXIL
    instruction lane may be selected automatically when the preferred
    high-level lane is statically incomplete, or explicitly requested. An
    untouched decompiler source establishes the baseline and is not
    compiled merely by starting the watcher. If the current file was already
    saved with different bytes, one debounced catch-up compile is scheduled.
    No candidate is ever applied to RenderDoc by this watcher.
    """
    try:
        result = _shader_workspace_watch_manager().start(
            workspace_path, debounce_ms
        )
        return _shader_workspace_watch_response("start", result)
    except Exception as error:
        return _err(error)


@mcp.tool()
def poll_shader_workspace_watch(watch_id: str) -> str:
    """Read bounded watcher state and audit events without changing it."""
    try:
        result = _shader_workspace_watch_manager().poll(watch_id)
        return _shader_workspace_watch_response("poll", result)
    except Exception as error:
        return _err(error)


@mcp.tool()
def stop_shader_workspace_watch(watch_id: str) -> str:
    """Stop scheduling compiles; an already running compiler may finish safely."""
    try:
        result = _shader_workspace_watch_manager().stop(watch_id)
        return _shader_workspace_watch_response("stop", result)
    except Exception as error:
        return _err(error)


def _shader_live_edit_remote(operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
    from .ipc_client import _CALL_BINDING
    from .shader_live_edit_client import request

    context = _active_live_context(allow_missing_capture_id=True)
    capture = context["capture"]
    direct = _CALL_BINDING.get()
    instance = direct[0] if direct is not None else ipc.instance_id
    if not instance:
        raise RuntimeError("Shader live-edit requires an exact qrenderdoc instance")
    return request(operation, parameters, {"instanceId": instance,
                   "captureSessionId": capture["captureSessionId"], "capturePath": capture["path"]})


@mcp.tool()
def start_shader_workspace_live_edit(
    workspace_path: str, debounce_ms: int = 100
) -> str:
    """Authorise compile, ABI validation and shared-resource apply on later saves.

    Starting this persistent session is the explicit auto-apply authorisation.
    Only candidates compiled after the session starts are considered.  Every
    candidate is rebound to its exact revision/hash, revalidated against the
    current capture and ABI, and then applied to all uses of the one captured
    Shader ResourceId.  Distinct ResourceIds are never merged and the captured
    Shader remains available through the reported rollback transaction.
    """

    try:
        result = _shader_live_edit_remote("start", {"workspace_path": workspace_path, "debounce_ms": debounce_ms})
        return _shader_live_edit_response("start", result)
    except Exception as error:
        return _err(error)


@mcp.tool()
def poll_shader_workspace_live_edit(live_edit_id: str) -> str:
    """Read live-edit compile/apply state, rejection evidence and audit events."""

    try:
        result = _shader_live_edit_remote("poll", {"live_edit_id": live_edit_id})
        return _shader_live_edit_response("poll", result)
    except Exception as error:
        return _err(error)


@mcp.tool()
def list_shader_workspace_live_edits() -> str:
    """List active sessions, durable authorisations and restart-resume results."""

    try:
        result = _shader_live_edit_remote("list", {})
        return _shader_live_edit_response("list", result)
    except Exception as error:
        return _err(error)


@mcp.tool()
def migrate_shader_workspace_live_edit_authorization(
    source_workspace_path: str,
    workspace_path: str,
    expected_source_sha256: str,
) -> str:
    """Explicitly move durable auto-apply consent to a migrated workspace.

    The fixed workspace service must prove a direct toolchain-migration edge,
    unchanged source bytes on both sides, and identical capture, original
    Shader ResourceId/raw hash, stage and entry point.  The authorization ID is
    moved atomically; it is never copied.  Neither an old candidate nor ABI
    evidence is imported.  This operation does not start a watcher in the
    current process; call ``start_shader_workspace_live_edit`` on the returned
    workspace to begin immediately, or let the next Host startup resume it.
    """

    try:
        result = _shader_live_edit_remote("migrate", {"source_workspace_path": source_workspace_path,
            "workspace_path": workspace_path, "expected_source_sha256": expected_source_sha256})
        return _shader_live_edit_response("migrate-authorization", result)
    except Exception as error:
        return _err(error)


@mcp.tool()
def stop_shader_workspace_live_edit(live_edit_id: str) -> str:
    """Stop monitoring; an already applied replacement remains rollbackable."""

    try:
        result = _shader_live_edit_remote("stop", {"live_edit_id": live_edit_id})
        return _shader_live_edit_response("stop", result)
    except Exception as error:
        return _err(error)


# ==================== ACTIVE REPLAY PROJECT ====================

_ACTIVE_PROJECT_MCP_POLICY = {
    "projectRootsProcessOwned": True,
    "pendingScopePhysicalReuse": False,
    "verifiedAliasesOnly": True,
    "aiProvisionalAutoApply": False,
    "shaderOrMaterialReplayMutation": False,
    "sourceSeedIsApplyAuthorization": False,
    "liveAbiRevalidationRequired": True,
}

_PASS_CLASSIFICATION_CACHE: dict[str, dict[str, Any]] = {}
_PASS_CLASSIFICATION_CACHE_GUARD = threading.RLock()
DEFAULT_FRAME_FLOW_MAX_ACTIONS = 8_000
# Keep direct MCP receipts below every supported Agent transport envelope.  In
# particular the Codex app-server accepts at most one Mi-character for a
# dynamic-tool result (and its JSONL envelope has a separate four MiB bound).
# Leaving headroom here also covers JSON escaping performed by the connector.
MAX_AGENT_TOOL_RESULT_BYTES = 768 * 1024
MAX_FRAME_FLOW_RESULT_BYTES = MAX_AGENT_TOOL_RESULT_BYTES
MAX_PASS_TOOL_RESULT_BYTES = MAX_AGENT_TOOL_RESULT_BYTES
MAX_PASS_PANEL_ACTIONS = 2_500
_PASS_AI_PROPOSAL_FIELDS = frozenset(
    {
        "eventId",
        "category",
        "subcategory",
        "displayLabel",
        "label",
        "rationale",
    }
)
_PASS_AI_PROPOSAL_TEXT_LIMITS = {
    "category": 64,
    "subcategory": 512,
    "displayLabel": 512,
    "label": 512,
    "rationale": 4096,
}
_PASS_PANEL_BIDI_CONTROLS = frozenset(
    chr(value)
    for value in (
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
    )
)


def _strict_pass_panel_text(
    value: Any,
    field: str,
    maximum: int,
    *,
    allow_empty: bool = True,
    allow_lf: bool = False,
) -> str:
    """Validate model/capture text before it reaches Qt's display boundary.

    Panel labels are single-line. AI rationale is not rendered and may retain
    LF-separated diagnostic paragraphs; every other C0/DEL code point and all
    bidi embedding, override, and isolate controls are rejected.
    """

    if not isinstance(value, str):
        raise ValueError("%s must be text" % field)
    if len(value) > maximum or (not allow_empty and not value.strip()):
        raise ValueError("%s exceeds the panel-safe text limit" % field)
    for character in value:
        codepoint = ord(character)
        if character in _PASS_PANEL_BIDI_CONTROLS:
            raise ValueError("%s contains a forbidden bidi control" % field)
        if codepoint < 0x20 or codepoint == 0x7F:
            if allow_lf and character == "\n":
                continue
            raise ValueError("%s contains a forbidden C0 control" % field)
    return value


def _pass_classification_error(operation: str, error: Exception) -> str:
    return _ok(
        {
            "schema": "replaylab-current-frame-pass-mcp-error/v1",
            "schemaVersion": 1,
            "operation": operation,
            "error": str(error),
            "type": type(error).__name__,
            "applied": False,
            "policy": {
                "classificationAuthority": "FixedProgram",
                "aiProposalAuthority": "AIProvisional",
                "aiCanChangeFixedTree": False,
                "replayMutation": False,
            },
        }
    )


def _active_project_error(operation: str, error: Exception) -> str:
    return _ok(
        {
            "schema": "replaylab-active-project-mcp-error/v1",
            "schemaVersion": 1,
            "operation": operation,
            "error": str(error),
            "type": type(error).__name__,
            "applied": False,
            "policy": dict(_ACTIVE_PROJECT_MCP_POLICY),
        }
    )


def _strict_lower_hex(value: Any, length: int, field: str) -> str:
    result = str(value or "").strip()
    if len(result) != length or any(
        character not in "0123456789abcdef" for character in result
    ):
        raise RuntimeError("%s must be %d lowercase hexadecimal characters" % (field, length))
    return result


def _shader_catalog_progress_channel(
    capture_session_id: str,
    operation_token: str | None = None,
) -> dict[str, str]:
    """Create one fixed sidecar binding under the selected live instance."""

    session_id = _strict_lower_hex(
        capture_session_id, 32, "captureSessionId"
    )
    selected = ipc._instance_mailbox()
    if selected is None:
        raise RuntimeError("No live ReplayLab instance is available for export progress")
    raw_request_dir, raw_response_dir = (Path(item) for item in selected)
    raw_instance_dir = raw_request_dir.parent
    if (
        raw_instance_dir.is_symlink()
        or raw_request_dir.is_symlink()
        or raw_response_dir.is_symlink()
    ):
        raise RuntimeError("ReplayLab progress instance mailbox is redirected")
    request_dir, response_dir = (
        raw_request_dir.resolve(), raw_response_dir.resolve()
    )
    instance_dir = request_dir.parent
    if response_dir.parent != instance_dir:
        raise RuntimeError("ReplayLab instance mailbox roots are inconsistent")
    root = Path(INSTANCE_ROOT)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("ReplayLab instance root is unavailable or redirected")
    root = root.resolve()
    if (
        instance_dir.is_symlink()
        or not instance_dir.is_dir()
        or not is_within(root, instance_dir)
        or _SHADER_CATALOG_PROGRESS_INSTANCE.fullmatch(instance_dir.name) is None
    ):
        raise RuntimeError("ReplayLab progress instance path is invalid")
    tokens = (
        [_strict_lower_hex(operation_token, 32, "progress operationToken")]
        if operation_token is not None
        else [uuid.uuid4().hex for _unused in range(8)]
    )
    for token in tokens:
        path = instance_dir / ("shader-catalog-progress-%s.json" % token)
        if not path.exists() and not path.is_symlink():
            return {
                "operationToken": token,
                "captureSessionId": session_id,
                "instanceId": instance_dir.name,
                "path": str(path),
            }
    if operation_token is not None:
        raise RuntimeError("Shader catalog progress token is already in use")
    raise RuntimeError("Could not allocate a unique Shader catalog progress token")


def _shader_catalog_progress_path(channel: Mapping[str, Any]) -> Path:
    if not isinstance(channel, Mapping) or set(channel) != {
        "operationToken", "captureSessionId", "instanceId", "path"
    }:
        raise RuntimeError("Shader catalog progress channel is invalid")
    token = _strict_lower_hex(
        channel.get("operationToken"), 32, "progress operationToken"
    )
    _strict_lower_hex(
        channel.get("captureSessionId"), 32, "progress captureSessionId"
    )
    instance_id = str(channel.get("instanceId", "") or "")
    if _SHADER_CATALOG_PROGRESS_INSTANCE.fullmatch(instance_id) is None:
        raise RuntimeError("Shader catalog progress instanceId is invalid")
    root = Path(INSTANCE_ROOT)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("ReplayLab instance root is unavailable or redirected")
    root = root.resolve()
    instance_dir = (root / instance_id).resolve()
    if (
        instance_dir.is_symlink()
        or not instance_dir.is_dir()
        or not is_within(root, instance_dir)
    ):
        raise RuntimeError("Shader catalog progress instance path is invalid")
    expected = instance_dir / ("shader-catalog-progress-%s.json" % token)
    supplied = Path(str(channel.get("path", "") or ""))
    if not supplied.is_absolute() or supplied != expected:
        raise RuntimeError("Shader catalog progress path is not the fixed sidecar")
    return expected


def _read_shader_catalog_progress(
    channel: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Read and strictly verify the latest atomic progress observation."""

    path = _shader_catalog_progress_path(channel)
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Shader catalog progress sidecar is not a regular file")
    before = path.stat()
    if before.st_size <= 0 or before.st_size > MAX_SHADER_CATALOG_PROGRESS_BYTES:
        raise RuntimeError("Shader catalog progress sidecar size is invalid")
    raw = path.read_bytes()
    after = path.stat()
    if (
        len(raw) != before.st_size
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise RuntimeError("Shader catalog progress sidecar changed while read")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key: %s" % key)
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError("non-finite JSON value: %s" % value)

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("Shader catalog progress sidecar is invalid JSON") from exc
    expected_fields = {
        "schema", "schemaVersion", "operationToken", "instanceId",
        "captureSessionId", "phase", "eventId", "current", "total",
        "updatedUnixMs",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise RuntimeError("Shader catalog progress fields are invalid")
    if (
        value.get("schema") != SHADER_CATALOG_PROGRESS_SCHEMA
        or isinstance(value.get("schemaVersion"), bool)
        or not isinstance(value.get("schemaVersion"), int)
        or value.get("schemaVersion") != 1
        or value.get("operationToken") != channel["operationToken"]
        or value.get("captureSessionId") != channel["captureSessionId"]
        or value.get("instanceId") != channel["instanceId"]
        or value.get("phase") != "export-shader-bindings"
    ):
        raise RuntimeError("Shader catalog progress binding is invalid")
    for field, minimum, maximum in (
        ("eventId", 0, 0xFFFFFFFF),
        ("current", 1, MAX_SHADER_CATALOG_PROGRESS_EVENTS),
        ("total", 1, MAX_SHADER_CATALOG_PROGRESS_EVENTS),
        ("updatedUnixMs", 1, (1 << 63) - 1),
    ):
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
            raise RuntimeError("Shader catalog progress %s is invalid" % field)
    if value["current"] > value["total"]:
        raise RuntimeError("Shader catalog progress current exceeds total")
    return value


def _cleanup_shader_catalog_progress(channel: Mapping[str, Any]) -> None:
    """Remove only the exact validated sidecar allocated for this operation."""

    try:
        path = _shader_catalog_progress_path(channel)
    except Exception:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _active_api_key(value: Any) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise RuntimeError("capture.api must be a string")
    compact = "".join(
        character
        for character in value.strip().lower()
        if character in "abcdefghijklmnopqrstuvwxyz0123456789"
    )
    if compact.startswith("graphicsapi"):
        compact = compact[len("graphicsapi") :]
    aliases = {
        "direct3d11": "d3d11",
        "directx11": "d3d11",
        "direct3d12": "d3d12",
        "directx12": "d3d12",
    }
    result = aliases.get(compact, compact)
    if not result or len(result) > 64:
        raise RuntimeError("capture.api is unavailable or invalid")
    return result


def _active_capture_transport_identity(context: dict[str, Any]) -> tuple[str, str, str]:
    capture = context.get("capture")
    if not isinstance(capture, dict):
        raise RuntimeError("RenderDoc current context has no capture binding")
    capture_session_id = _strict_lower_hex(
        capture.get("captureSessionId"), 32, "capture.captureSessionId"
    )
    capture_path = capture.get("path")
    if (
        not isinstance(capture_path, str)
        or not capture_path.strip()
        or "\x00" in capture_path
    ):
        raise RuntimeError("capture.path is unavailable or invalid")
    return (
        capture_session_id,
        _capture_path_key(capture_path),
        _active_api_key(capture.get("api")),
    )


def _active_live_context(*, allow_missing_capture_id: bool = False) -> dict[str, Any]:
    context = ipc.call("get_current_renderdoc_context", {}, timeout=60.0)
    if (
        not isinstance(context, dict)
        or context.get("schema") != "renderdoc-ultra-current-context/v1"
        or context.get("schemaVersion") != 1
    ):
        raise RuntimeError("RenderDoc returned an unsupported current-context schema")
    capture = context.get("capture")
    if not isinstance(capture, dict) or capture.get("loaded") is not True:
        raise RuntimeError("No RenderDoc capture is loaded")
    capture_id = str(capture.get("captureId", "") or "").strip()
    if capture_id:
        _strict_lower_hex(capture_id, 64, "capture.captureId")
    elif not allow_missing_capture_id:
        _strict_lower_hex(capture_id, 64, "capture.captureId")
    _strict_lower_hex(
        capture.get("captureSessionId"), 32, "capture.captureSessionId"
    )
    if allow_missing_capture_id:
        _active_capture_transport_identity(context)
    return context


def _active_capture_identity(context: dict[str, Any]) -> tuple[str, str]:
    capture = context["capture"]
    return str(capture["captureId"]), str(capture["captureSessionId"])


def _workspace_active_capture_context(
    workspace_path: str, *, status: dict[str, Any]
) -> dict[str, Any]:
    """Use the verified workspace catalog, independent of the UI symbol cache."""

    binding = _ensure_workspace_capture_catalog_identity(workspace_path, status=status)
    capture_id = _strict_lower_hex(
        status.get("capture", {}).get("captureId"), 64, "workspace capture.captureId"
    )
    if binding.get("captureId") != capture_id or binding.get("workspaceId") != status.get(
        "workspaceId"
    ):
        raise RuntimeError("verified workspace catalog identity changed")
    expected_transport = _active_capture_transport_identity(
        {
            "capture": {
                "captureSessionId": binding.get("captureSessionId"),
                "path": status["capture"].get("capturePath"),
                "api": status["capture"].get("api"),
            }
        }
    )
    context = _active_live_context(allow_missing_capture_id=True)
    if _active_capture_transport_identity(context) != expected_transport:
        raise RuntimeError("active RenderDoc capture/session does not match this workspace")
    # A bounded catalog and a full-frame UI symbol snapshot can have different
    # IDs for the same RDC. The rebind above proves the exact original Shader
    # bytes and event uses; session/path/API prove this is still that live RDC.
    return _canonical_active_capture_context(context, capture_id)


def _canonical_active_capture_context(
    context: dict[str, Any], canonical_capture_id: str
) -> dict[str, Any]:
    capture_id = _strict_lower_hex(
        canonical_capture_id, 64, "canonical captureId"
    )
    live_capture_id = str(context.get("capture", {}).get("captureId", "") or "").strip()
    # ``captureId`` identifies one exported Shader set, not the loaded RDC or
    # every byte of reflection/use metadata by itself. A full-frame and a
    # one-event export intentionally have different IDs, while two corrected
    # full exports can share an ID and differ by catalogSha256. Validate any
    # previous ID's shape, then bind this operation to the exported family;
    # _remember_active_capture_catalog binds its exact content revision.
    if live_capture_id:
        _strict_lower_hex(live_capture_id, 64, "live captureId")
    result = copy.deepcopy(context)
    result["capture"]["captureId"] = capture_id
    return result


def _require_same_active_capture(
    expected: dict[str, Any], *, allow_missing_capture_id: bool = False
) -> dict[str, Any]:
    current = (
        _active_live_context(allow_missing_capture_id=True)
        if allow_missing_capture_id
        else _active_live_context()
    )
    if not allow_missing_capture_id:
        unchanged = _active_capture_identity(current) == _active_capture_identity(expected)
    else:
        # Catalog identity may change when another bounded export runs in the
        # same loaded capture. Session/path/API close the capture-race window.
        unchanged = _active_capture_transport_identity(
            current
        ) == _active_capture_transport_identity(expected)
    if not unchanged:
        raise RuntimeError("RenderDoc capture/session changed during the active-project operation")
    return current


def _remember_active_capture_catalog(
    project_status: dict[str, Any],
    context: dict[str, Any],
    staged: dict[str, Any],
    *,
    event_id: int | None = None,
    stage: str = "",
) -> None:
    """Remember the exact full-frame or event/stage catalog scope."""

    _require_same_active_project(project_status)
    capture_id, capture_session_id = _active_capture_identity(context)
    if staged.get("captureId") != capture_id:
        raise RuntimeError("staged active catalog identity changed before binding")
    paths = project_status.get("paths")
    if not isinstance(paths, dict) or not paths.get("captureCatalogs"):
        raise RuntimeError("active project omitted its capture-catalog root")
    catalog_root = Path(str(paths["captureCatalogs"])).resolve()
    raw_catalog = Path(str(staged.get("catalogPath", "") or "")).expanduser()
    if raw_catalog.exists() and raw_catalog.is_symlink():
        raise RuntimeError("staged active catalog must not be a symlink")
    catalog_path = raw_catalog.resolve()
    if (
        catalog_root.is_symlink()
        or not catalog_root.is_dir()
        or not catalog_path.is_file()
        or not is_within(catalog_root, catalog_path)
    ):
        raise RuntimeError("staged active catalog escaped its project root")
    catalog_sha256 = _strict_lower_hex(
        staged.get("catalogSha256"), 64, "staged catalogSha256"
    )
    if (
        _bounded_file_sha256(
            catalog_path,
            MAX_ACTIVE_CAPTURE_CATALOG_BYTES,
            "staged active catalog",
        )
        != catalog_sha256
    ):
        raise RuntimeError("staged active catalog changed before session binding")
    transport = _active_capture_transport_identity(context)
    record = {
        "captureSessionId": capture_session_id,
        "captureId": capture_id,
        "capturePathKey": transport[1],
        "apiKey": transport[2],
        "projectWorkspaceId": str(project_status.get("workspaceId", "") or ""),
        "projectRoot": str(project_status.get("projectRoot", "") or ""),
        "captureCatalogRoot": str(catalog_root),
        "catalogPath": str(catalog_path),
        "catalogSha256": catalog_sha256,
        "catalogScope": "full-frame" if event_id is None else "event-stage",
        "coveredEventIds": [] if event_id is None else [int(event_id)],
        "coveredStages": [] if event_id is None else [normalize_stage(stage)],
    }
    if event_id is not None:
        _validate_event_stage_catalog(catalog_path, int(event_id), stage)
    with _ACTIVE_CAPTURE_CATALOG_BINDINGS_GUARD:
        _ACTIVE_CAPTURE_CATALOG_BINDINGS.pop(capture_session_id, None)
        _ACTIVE_CAPTURE_CATALOG_BINDINGS[capture_session_id] = record
        while (
            len(_ACTIVE_CAPTURE_CATALOG_BINDINGS)
            > _ACTIVE_CAPTURE_CATALOG_BINDINGS_MAX_ENTRIES
        ):
            _ACTIVE_CAPTURE_CATALOG_BINDINGS.popitem(last=False)


_ACTIVE_CAPTURE_BINDING_IDENTITY_FIELDS = (
    "captureSessionId",
    "captureId",
    "capturePathKey",
    "apiKey",
    "projectWorkspaceId",
    "projectRoot",
    "captureCatalogRoot",
    "catalogPath",
    "catalogSha256",
    "catalogScope",
    "coveredEventIds",
    "coveredStages",
)


def _active_capture_catalog_binding_sha256(record: dict[str, Any]) -> str:
    material = {
        field: str(record.get(field, "") or "")
        for field in _ACTIVE_CAPTURE_BINDING_IDENTITY_FIELDS
    }
    return hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _active_capture_catalog_binding(
    context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path] | None:
    """Return the hash-bound full catalog for this exact live replay session."""

    with _ACTIVE_CAPTURE_CATALOG_BINDINGS_GUARD:
        if not _ACTIVE_CAPTURE_CATALOG_BINDINGS:
            return None
    if context is None:
        context = _active_live_context(allow_missing_capture_id=True)
    capture_session_id = str(context["capture"]["captureSessionId"])
    with _ACTIVE_CAPTURE_CATALOG_BINDINGS_GUARD:
        raw_record = _ACTIVE_CAPTURE_CATALOG_BINDINGS.get(capture_session_id)
        record = copy.deepcopy(raw_record) if raw_record is not None else None
    if not isinstance(record, dict):
        return None
    transport = _active_capture_transport_identity(context)
    if (
        record.get("capturePathKey") != transport[1]
        or record.get("apiKey") != transport[2]
    ):
        with _ACTIVE_CAPTURE_CATALOG_BINDINGS_GUARD:
            _ACTIVE_CAPTURE_CATALOG_BINDINGS.pop(capture_session_id, None)
        raise RuntimeError("active full-catalog binding changed capture path or API")
    project_status = _active_project_service().status()
    if (
        record.get("projectWorkspaceId") != project_status.get("workspaceId")
        or os.path.normcase(str(record.get("projectRoot", "")))
        != os.path.normcase(str(project_status.get("projectRoot", "")))
    ):
        raise RuntimeError("active full-catalog binding belongs to another project")
    paths = project_status.get("paths")
    if not isinstance(paths, dict) or not paths.get("captureCatalogs"):
        raise RuntimeError("active project omitted its capture-catalog root")
    raw_catalog_root = Path(str(paths["captureCatalogs"])).expanduser()
    raw_catalog_path = Path(str(record.get("catalogPath", "") or "")).expanduser()
    if (
        raw_catalog_root.is_symlink()
        or not raw_catalog_root.is_dir()
        or raw_catalog_path.is_symlink()
        or not raw_catalog_path.is_file()
    ):
        raise RuntimeError("active full-catalog binding is missing or redirected")
    catalog_root = raw_catalog_root.resolve()
    catalog_path = raw_catalog_path.resolve()
    if (
        catalog_root.is_symlink()
        or not catalog_root.is_dir()
        or not catalog_path.is_file()
        or not is_within(catalog_root, catalog_path)
        or os.path.normcase(str(catalog_root))
        != os.path.normcase(str(record.get("captureCatalogRoot", "")))
        or _bounded_file_sha256(
            catalog_path,
            MAX_ACTIVE_CAPTURE_CATALOG_BYTES,
            "active full catalog",
        )
        != record.get("catalogSha256")
    ):
        raise RuntimeError("active full-catalog binding failed path/hash validation")
    _require_same_active_capture(context, allow_missing_capture_id=True)
    return context, project_status, record, catalog_path


def _active_capture_catalog_selection(
    event_id: int, stage: str
) -> tuple[dict[str, Any], Path, Any, str] | None:
    """Reuse a verified full catalog instead of exporting a drifting subset."""

    binding = _active_capture_catalog_binding()
    if binding is None:
        return None
    context, unused_project_status, record, catalog_path = binding
    if not _active_catalog_covers(record, event_id, stage):
        return None
    selected = select_unique_shader_for_use(catalog_path, event_id, stage)
    _require_same_active_capture(context, allow_missing_capture_id=True)
    exported = {
        "schema": "renderdoc-ultra-capture-symbols/v1",
        "schemaVersion": 1,
        "captureId": record["captureId"],
        "captureSessionId": context["capture"]["captureSessionId"],
        "catalogPath": str(catalog_path),
        "reusedActiveProjectCatalog": True,
        "exportPerformed": False,
        **_active_catalog_scope(record),
    }
    return (
        exported,
        catalog_path,
        selected,
        _active_capture_catalog_binding_sha256(record),
    )


def _active_catalog_scope(record: Mapping[str, Any]) -> dict[str, Any]:
    scope = record.get("catalogScope", "full-frame")
    events = record.get("coveredEventIds", [])
    stages = record.get("coveredStages", [])
    if scope not in {"full-frame", "event-stage"} or not isinstance(events, list) or not isinstance(stages, list):
        raise RuntimeError("active catalog coverage is invalid")
    if scope == "event-stage" and (
        len(events) != 1 or type(events[0]) is not int or events[0] <= 0
        or len(stages) != 1 or not stages[0] or normalize_stage(stages[0]) != stages[0]
    ):
        raise RuntimeError("active event-stage catalog coverage is invalid")
    if scope == "full-frame" and (events or stages):
        raise RuntimeError("full-frame catalog has inconsistent partial coverage")
    return {
        "catalogScope": scope,
        "fullFrame": scope == "full-frame",
        "coveredEventIds": list(events),
        "coveredStages": list(stages),
    }


def _active_catalog_covers(record: Mapping[str, Any], event_id: int, stage: str) -> bool:
    _active_catalog_scope(record)
    if record.get("catalogScope", "full-frame") == "full-frame":
        return True
    return (
        record.get("catalogScope") == "event-stage"
        and event_id in record.get("coveredEventIds", [])
        and normalize_stage(stage) in record.get("coveredStages", [])
    )


def _validate_event_stage_catalog(path: Path, event_id: int, stage: str) -> Any:
    """A partial receipt must contain exactly the requested physical Shader use."""
    stage = normalize_stage(stage)
    catalog = read_json_object(path, MAX_ACTIVE_CAPTURE_CATALOG_BYTES, "Shader catalog")
    selection = catalog.get("selection")
    errors = catalog.get("errors", [])
    if not isinstance(selection, dict) or (
        selection.get("mode") != "explicit"
        or selection.get("eventIds") != [event_id]
        or selection.get("stages") != [stage]
        or len(catalog.get("shaders", [])) != 1
        or len(catalog.get("uses", [])) != 1
        or not isinstance(errors, list)
        or any(isinstance(item, dict) and item.get("scope") == "restore_event" for item in errors)
    ):
        raise RuntimeError("event-stage catalog does not cover exactly the requested Shader")
    return select_unique_shader_for_use(path, event_id, stage)


def _active_project_capture_identity(
    context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str, str, bool]:
    """Resolve the active full-catalog ID while preserving live transport identity."""

    if context is None:
        context = _active_live_context(allow_missing_capture_id=True)
    binding = _active_capture_catalog_binding(context)
    capture_session_id = _strict_lower_hex(
        context.get("capture", {}).get("captureSessionId"),
        32,
        "capture.captureSessionId",
    )
    if binding is not None:
        unused_context, unused_project_status, record, unused_path = binding
        return context, str(record["captureId"]), capture_session_id, True
    capture_id = _strict_lower_hex(
        context.get("capture", {}).get("captureId"),
        64,
        "capture.captureId",
    )
    return context, capture_id, capture_session_id, False


def _active_project_catalog_revision_for_workspace(
    context: dict[str, Any], capture_id: str, uses_full_binding: bool
) -> tuple[str | None, str]:
    """Return the exact active catalog revision and its immutable binding ID."""

    if not uses_full_binding:
        return None, ""
    binding = _active_capture_catalog_binding(context)
    if binding is None:
        raise RuntimeError("active full-catalog binding disappeared")
    unused_context, unused_project_status, record, unused_catalog = binding
    if record.get("captureId") != capture_id:
        raise RuntimeError("active full-catalog capture identity changed")
    revision = _strict_lower_hex(
        record.get("catalogSha256"), 64, "active catalogSha256"
    )
    return revision, _active_capture_catalog_binding_sha256(record)


def _active_shader_route_binding_sha256() -> str:
    """Bind managed paths to exact configs plus automatic-format policy."""

    catalog = _shader_toolchain_catalog()
    route_catalog_sha256 = _bounded_file_sha256(
        catalog.path,
        4 * 1024 * 1024,
        "shader route catalog",
    )
    payload = {
        "schema": "replaylab-active-shader-route-binding/v1",
        "schemaVersion": 1,
        "automaticEditablePolicy": "prefer-source-then-direct-native-ir-v2",
        "routeCatalogSha256": route_catalog_sha256,
        "toolRegistrySha256": catalog.tool_registry_sha256,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _require_same_active_project(expected: dict[str, Any]) -> dict[str, Any]:
    current = _active_project_service().status()
    current_root = Path(str(current.get("projectRoot", "") or "")).resolve()
    expected_root = Path(str(expected.get("projectRoot", "") or "")).resolve()
    current_paths = current.get("paths")
    expected_paths = expected.get("paths")
    path_keys = ("captureCatalogs", "shaderWorkspaces", "replayProject")
    paths_changed = (
        not isinstance(current_paths, dict)
        or not isinstance(expected_paths, dict)
        or any(
            os.path.normcase(
                str(Path(str(current_paths.get(key, "") or "")).resolve())
            )
            != os.path.normcase(
                str(Path(str(expected_paths.get(key, "") or "")).resolve())
            )
            for key in path_keys
        )
    )
    if any(
        (
            current.get("workspaceId") != expected.get("workspaceId"),
            os.path.normcase(str(current_root))
            != os.path.normcase(str(expected_root)),
            current.get("state") != expected.get("state"),
            current.get("scope") != expected.get("scope"),
            current.get("scopeConfigured") is not expected.get("scopeConfigured"),
            current.get("assetRoot") != expected.get("assetRoot"),
            paths_changed,
        )
    ):
        raise RuntimeError("active project changed during the operation")
    return current


def _strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError("%s must be a boolean" % field)
    return value


_PASS_CAPTURE_BINDING_DISPLAY_SESSION = "display-session"
_PASS_CAPTURE_BINDING_PROJECT_CATALOG = "project-catalog"


def _display_session_capture_id(capture_session_id: Any) -> str:
    """Return a non-persistent capture identity for the current UI session.

    The semantic-pass panel needs a stable 64-hex capture key even when no
    capture catalog has been exported.  Hashing the already Host-validated
    lifecycle ID gives the display-only path that key without staging or
    importing anything into an active ReplayLab project.
    """

    session_id = _strict_lower_hex(
        capture_session_id, 32, "capture.captureSessionId"
    )
    return hashlib.sha256(
        ("replaylab-pass-display-session/v1:" + session_id).encode("ascii")
    ).hexdigest()


def _canonical_json_sha256(value: Any, field: str) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("%s is not canonical JSON" % field) from exc
    return hashlib.sha256(payload).hexdigest()


def _fixed_pass_classification_view(value: dict[str, Any]) -> dict[str, Any]:
    fixed = copy.deepcopy(value)
    for item in fixed.get("items", []):
        if isinstance(item, dict):
            item.pop("semanticProposal", None)
    fixed["provisionalOverlayCount"] = 0
    return fixed


def _pass_panel_classification_view(value: dict[str, Any]) -> dict[str, Any]:
    """Project rich frame evidence into the existing display-only panel schema."""
    items = []
    raw_items = value.get("items", [])
    if not isinstance(raw_items, list) or len(raw_items) > 100000:
        raise ValueError("classification.items must be a bounded array")
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise ValueError("classification.items[%d] must be an object" % index)
        name = _strict_pass_panel_text(
            raw.get("name"), "classification.items[%d].name" % index, 512,
            allow_empty=False,
        )
        subcategory = _strict_pass_panel_text(
            raw.get("subcategory"),
            "classification.items[%d].subcategory" % index,
            512,
            allow_empty=False,
        )
        item = {
            field: copy.deepcopy(raw.get(field))
            for field in (
                "eventId", "category", "confidence",
                "actualWorkState",
            )
        }
        item["name"] = name
        item["subcategory"] = subcategory
        participation = raw.get("participation")
        if isinstance(participation, dict):
            item["participation"] = {
                field: copy.deepcopy(participation.get(field))
                for field in (
                    "issuedAction", "nonZeroWorkload",
                    "outputAffectingCandidate", "observedContributor",
                )
            }
        proposal = raw.get("semanticProposal")
        if isinstance(proposal, dict):
            proposed_category = _strict_pass_panel_text(
                proposal.get("proposedCategory", ""),
                "classification.items[%d].semanticProposal.proposedCategory" % index,
                64,
            )
            proposed_subcategory = _strict_pass_panel_text(
                proposal.get("proposedSubcategory", ""),
                "classification.items[%d].semanticProposal.proposedSubcategory" % index,
                512,
            )
            display_label = _strict_pass_panel_text(
                proposal.get("displayLabel", ""),
                "classification.items[%d].semanticProposal.displayLabel" % index,
                512,
            )
            _strict_pass_panel_text(
                proposal.get("rationale", ""),
                "classification.items[%d].semanticProposal.rationale" % index,
                4096,
                allow_lf=True,
            )
            item["semanticProposal"] = {
                "authority": proposal.get("authority"),
                "verified": proposal.get("verified"),
                "applied": proposal.get("applied"),
                "reviewRequired": proposal.get("reviewRequired"),
                "physicalEvidence": proposal.get("physicalEvidence"),
                "proposedCategory": proposed_category,
                "proposedSubcategory": proposed_subcategory,
                "displayLabel": display_label,
                # The current panel does not render rationale. Keep its strict
                # schema while avoiding repeated multi-KiB prose in UI IPC.
                "rationale": "",
            }
        items.append(item)

    def _validated_tree(nodes: Any, path: str = "classification.tree") -> list[Any]:
        if not isinstance(nodes, list) or len(nodes) > 100000:
            raise ValueError("%s must be a bounded array" % path)
        result = []
        for index, raw_node in enumerate(nodes):
            node_path = "%s[%d]" % (path, index)
            if not isinstance(raw_node, dict):
                raise ValueError("%s must be an object" % node_path)
            node = copy.deepcopy(raw_node)
            if "label" in node:
                node["label"] = _strict_pass_panel_text(
                    node["label"], "%s.label" % node_path, 512,
                    allow_empty=False,
                )
            if "children" in node:
                node["children"] = _validated_tree(
                    node["children"], "%s.children" % node_path
                )
            result.append(node)
        return result

    raw_summary = value.get("summary", {})
    if not isinstance(raw_summary, dict):
        raise ValueError("classification.summary must be an object")
    source_truncated = raw_summary.get(
        "sourceTruncated", raw_summary.get("truncated", False)
    )
    source_truncated = _strict_bool(
        source_truncated, "classification.summary.sourceTruncated"
    )
    omitted = raw_summary.get("omittedActionCountLowerBound", 0)
    if (
        isinstance(omitted, bool)
        or not isinstance(omitted, int)
        or omitted < 0
        or omitted > 100000
    ):
        raise ValueError(
            "classification.summary.omittedActionCountLowerBound is invalid"
        )
    raw_reasons = raw_summary.get("truncationReasons", [])
    if not isinstance(raw_reasons, list) or len(raw_reasons) > 16:
        raise ValueError("classification.summary.truncationReasons is invalid")
    reasons = [
        _strict_pass_panel_text(
            reason,
            "classification.summary.truncationReasons[%d]" % index,
            256,
            allow_empty=False,
        )
        for index, reason in enumerate(raw_reasons)
    ]
    effective_limit = raw_summary.get(
        "effectiveMaxActions",
        raw_summary.get("requestedMaxActions", max(1, len(items))),
    )
    if (
        isinstance(effective_limit, bool)
        or not isinstance(effective_limit, int)
        or effective_limit < max(1, len(items))
        or effective_limit > 100000
    ):
        raise ValueError("classification.summary.effectiveMaxActions is invalid")
    if not source_truncated and (omitted != 0 or reasons):
        raise ValueError(
            "non-truncated classification must not declare omitted actions or reasons"
        )
    summary = {
        "sourceTruncated": source_truncated,
        "omittedActionCountLowerBound": omitted,
        "truncationReasons": reasons,
        "effectiveMaxActions": effective_limit,
    }
    return {
        "schema": value.get("schema"),
        "schemaVersion": value.get("schemaVersion"),
        "captureId": value.get("captureId"),
        "items": items,
        "tree": _validated_tree(value.get("tree", [])),
        "summary": summary,
        "policy": {
            "classificationAuthority": value.get("policy", {}).get(
                "classificationAuthority"
            ),
            "aiCanChangeFixedTree": value.get("policy", {}).get(
                "aiCanChangeFixedTree"
            ),
            "aiProposalAuthority": value.get("policy", {}).get(
                "aiProposalAuthority"
            ),
        },
        "provisionalOverlayCount": value.get("provisionalOverlayCount", 0),
    }


def _pass_tool_transport_view(value: dict[str, Any]) -> dict[str, Any]:
    """Keep classification identities/tree usable when the rich result is oversized."""
    result = copy.deepcopy(value)
    classification = result.get("classification")
    if not isinstance(classification, dict):
        return result
    projected = _pass_panel_classification_view(classification)
    raw_summary = classification.get("summary", {})
    summary = {}
    if isinstance(raw_summary, dict):
        for field in (
            "inputCount",
            "actionCount",
            "containerCount",
            "markerContextCount",
            "duplicateCount",
            "categoryCounts",
            "actualWorkCounts",
            "executionTimeline",
            "actionExecutionTimeline",
            "sourceTruncated",
            "truncated",
            "omittedActionCountLowerBound",
            "truncationReasons",
            "requestedMaxActions",
            "effectiveMaxActions",
            "uiTreeActionLimitApplied",
            "inputResourceOmittedCount",
            "outputResourceOmittedCount",
            "resourceFactsTruncated",
            "resourceFlowTruncated",
            "resourceFlowSubresourcesResolved",
        ):
            if field in raw_summary:
                summary[field] = copy.deepcopy(raw_summary[field])
        summary["resourceFlowEdgeCount"] = len(
            raw_summary.get("resourceFlowEdges", [])
            if isinstance(raw_summary.get("resourceFlowEdges"), list)
            else []
        )
        summary["externalInputCount"] = len(
            raw_summary.get("externalInputs", [])
            if isinstance(raw_summary.get("externalInputs"), list)
            else []
        )
        summary["terminalOutputCount"] = len(
            raw_summary.get("terminalOutputs", [])
            if isinstance(raw_summary.get("terminalOutputs"), list)
            else []
        )
    projected["summary"] = summary
    result["classification"] = projected
    result["transport"] = {
        "projected": True,
        "reason": "rich classification exceeded the direct tool byte budget",
        "omittedFields": [
            "per-item-rich-evidence",
            "resourceFlowEdges",
            "externalInputs",
            "terminalOutputs",
        ],
        "maximumUtf8Bytes": MAX_PASS_TOOL_RESULT_BYTES,
    }
    return result


def _pruned_pass_transport_tree(
    nodes: Any, retained_event_ids: set[int]
) -> list[dict[str, Any]]:
    """Keep only branches that lead to one retained action leaf.

    The panel tree duplicates the compact item labels, so merely truncating the
    item array is not enough to satisfy the Agent transport bound.  Preserve
    the existing stable node IDs and order while recomputing aggregate counts.
    """

    if not isinstance(nodes, list):
        return []
    result: list[dict[str, Any]] = []
    for raw_node in nodes:
        if not isinstance(raw_node, dict):
            continue
        node_type = raw_node.get("nodeType")
        if node_type == "action":
            event_id = raw_node.get("eventId")
            if (
                isinstance(event_id, int)
                and not isinstance(event_id, bool)
                and event_id in retained_event_ids
            ):
                result.append(copy.deepcopy(raw_node))
            continue
        children = _pruned_pass_transport_tree(
            raw_node.get("children", []), retained_event_ids
        )
        if not children:
            continue
        node = copy.deepcopy(raw_node)
        node["children"] = children

        def leaf_count(values: list[dict[str, Any]]) -> int:
            return sum(
                1
                if value.get("nodeType") == "action"
                else leaf_count(value.get("children", []))
                for value in values
                if isinstance(value, dict)
            )

        if "count" in node:
            node["count"] = leaf_count(children)
        result.append(node)
    return result


def _truncate_pass_tool_transport_view(
    projected: dict[str, Any], retained_item_count: int
) -> dict[str, Any]:
    """Return one self-describing prefix projection for a bounded receipt."""

    result = copy.deepcopy(projected)
    classification = result.get("classification")
    if not isinstance(classification, dict):
        return result
    raw_items = classification.get("items")
    if not isinstance(raw_items, list):
        return result
    original_count = len(raw_items)
    retained_item_count = max(0, min(retained_item_count, original_count))
    items = raw_items[:retained_item_count]
    classification["items"] = items
    retained_event_ids = {
        item.get("eventId")
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("eventId"), int)
        and not isinstance(item.get("eventId"), bool)
    }
    classification["tree"] = _pruned_pass_transport_tree(
        classification.get("tree", []), retained_event_ids
    )
    omitted = original_count - retained_item_count
    summary = classification.get("summary")
    if not isinstance(summary, dict):
        summary = {}
        classification["summary"] = summary
    prior_omitted = summary.get("omittedActionCountLowerBound", 0)
    if isinstance(prior_omitted, bool) or not isinstance(prior_omitted, int):
        prior_omitted = 0
    summary["sourceTruncated"] = True
    summary["truncated"] = True
    summary["actionCount"] = retained_item_count
    summary["omittedActionCountLowerBound"] = max(0, prior_omitted) + omitted
    reasons = summary.get("truncationReasons", [])
    reasons = list(reasons) if isinstance(reasons, list) else []
    if "host-result-byte-budget" not in reasons:
        reasons.append("host-result-byte-budget")
    summary["truncationReasons"] = reasons[:16]
    summary["categoryCounts"] = dict(
        Counter(
            str(item.get("category", ""))
            for item in items
            if isinstance(item, dict)
        )
    )
    summary["actualWorkCounts"] = dict(
        Counter(
            str(item.get("actualWorkState", ""))
            for item in items
            if isinstance(item, dict)
        )
    )
    for field in ("executionTimeline", "actionExecutionTimeline"):
        timeline = summary.get(field)
        if isinstance(timeline, list):
            summary[field] = [
                event_id for event_id in timeline if event_id in retained_event_ids
            ]
    transport = result.setdefault("transport", {})
    transport.update(
        {
            "truncated": True,
            "itemsTruncated": True,
            "sourceItemCount": original_count,
            "returnedItemCount": retained_item_count,
            "omittedItemCount": omitted,
        }
    )
    omitted_fields = transport.get("omittedFields", [])
    omitted_fields = (
        list(omitted_fields) if isinstance(omitted_fields, list) else []
    )
    for field in ("items-after-byte-budget", "corresponding-tree-branches"):
        if field not in omitted_fields:
            omitted_fields.append(field)
    transport["omittedFields"] = omitted_fields
    return result


def _bounded_pass_tool_json(value: dict[str, Any]) -> str:
    encoded = _ok(value)
    if len(encoded.encode("utf-8")) <= MAX_PASS_TOOL_RESULT_BYTES:
        return encoded
    projected = _pass_tool_transport_view(value)
    encoded = _ok(projected)
    if len(encoded.encode("utf-8")) > MAX_PASS_TOOL_RESULT_BYTES:
        classification = projected.get("classification")
        items = (
            classification.get("items", [])
            if isinstance(classification, dict)
            else []
        )
        item_count = len(items) if isinstance(items, list) else 0
        low = 0
        high = item_count
        best: dict[str, Any] | None = None
        # Reserve a small fixed margin for the self-measured byte field added
        # below. Prefix selection is deterministic and the receipt says exactly
        # how much was omitted, so a large frame degrades instead of failing.
        target_bytes = max(0, MAX_PASS_TOOL_RESULT_BYTES - 512)
        while low <= high:
            midpoint = (low + high) // 2
            candidate = _truncate_pass_tool_transport_view(projected, midpoint)
            candidate_encoded = _ok(candidate)
            if len(candidate_encoded.encode("utf-8")) <= target_bytes:
                best = candidate
                low = midpoint + 1
            else:
                high = midpoint - 1
        if best is None:
            best = _truncate_pass_tool_transport_view(projected, 0)
        projected = best
        encoded = _ok(projected)
        if len(encoded.encode("utf-8")) > MAX_PASS_TOOL_RESULT_BYTES:
            raise RuntimeError(
                "bounded pass-classification base receipt exceeds the direct tool byte limit"
            )
    transport = projected.setdefault("transport", {})
    for unused in range(6):
        measured = len(encoded.encode("utf-8"))
        if transport.get("serializedUtf8Bytes") == measured:
            break
        transport["serializedUtf8Bytes"] = measured
        encoded = _ok(projected)
    if len(encoded.encode("utf-8")) > MAX_PASS_TOOL_RESULT_BYTES:
        raise RuntimeError(
            "bounded pass-classification receipt exceeds the direct tool byte limit"
        )
    return encoded


def _cache_fixed_pass_classification(
    classification: dict[str, Any],
    *,
    capture_id: str,
    capture_session_id: str,
    capture_binding_mode: str,
    active_workspace_id: str,
    replay_project_path: str,
) -> tuple[str, str]:
    if capture_binding_mode not in {
        _PASS_CAPTURE_BINDING_DISPLAY_SESSION,
        _PASS_CAPTURE_BINDING_PROJECT_CATALOG,
    }:
        raise RuntimeError("fixed pass classification binding mode is invalid")
    if (
        classification.get("schema") != CLASSIFICATION_SCHEMA
        or classification.get("schemaVersion") != CLASSIFICATION_VERSION
        or classification.get("captureId") != capture_id
    ):
        raise RuntimeError("fixed pass classification identity is invalid")
    fixed = _fixed_pass_classification_view(classification)
    digest = _canonical_json_sha256(fixed, "fixed pass classification")
    token = uuid.uuid4().hex
    record = {
        "classification": fixed,
        "fixedSha256": digest,
        "captureId": capture_id,
        "captureSessionId": capture_session_id,
        "captureBindingMode": capture_binding_mode,
        "activeWorkspaceId": active_workspace_id,
        "replayProjectPath": replay_project_path,
    }
    with _PASS_CLASSIFICATION_CACHE_GUARD:
        # A semantic proposal is valid only for the latest fixed pass result.
        _PASS_CLASSIFICATION_CACHE.clear()
        _PASS_CLASSIFICATION_CACHE[token] = record
    return token, digest


def _cached_fixed_pass_classification(
    classification_id: str,
    capture_id: str,
    capture_session_id: str,
) -> dict[str, Any]:
    token = str(classification_id or "").strip().lower()
    if len(token) != 32 or any(
        character not in "0123456789abcdef" for character in token
    ):
        raise ValueError(
            "classification_id must be a 32-character lowercase hex ID"
        )
    expected_capture = _strict_lower_hex(capture_id, 64, "expected_capture_id")
    expected_session = _strict_lower_hex(
        capture_session_id, 32, "expected_capture_session_id"
    )
    with _PASS_CLASSIFICATION_CACHE_GUARD:
        record = copy.deepcopy(_PASS_CLASSIFICATION_CACHE.get(token))
    if not isinstance(record, dict):
        raise RuntimeError("classification_id is unknown or no longer current")
    if (
        record.get("captureId") != expected_capture
        or record.get("captureSessionId") != expected_session
    ):
        raise RuntimeError("classification_id belongs to a different capture/session")
    fixed = record.get("classification")
    if not isinstance(fixed, dict) or _canonical_json_sha256(
        fixed, "cached fixed pass classification"
    ) != record.get("fixedSha256"):
        raise RuntimeError("cached fixed pass classification integrity failed")
    return record


def _load_active_pass_catalog(
    prepared: dict[str, Any], capture_id: str
) -> dict[str, Any]:
    staged = prepared.get("stagedCatalog")
    project = prepared.get("project")
    if not isinstance(staged, dict) or not isinstance(project, dict):
        raise RuntimeError("active capture preparation omitted project/catalog evidence")
    if staged.get("captureId") != capture_id:
        raise RuntimeError("staged pass catalog belongs to a different capture")
    paths = project.get("paths")
    if not isinstance(paths, dict) or not paths.get("captureCatalogs"):
        raise RuntimeError("active project omitted its capture-catalog root")
    catalog_root = Path(str(paths["captureCatalogs"])).resolve()
    raw_path = Path(str(staged.get("catalogPath", "") or "")).expanduser()
    if raw_path.exists() and raw_path.is_symlink():
        raise RuntimeError("staged pass catalog must not be a symlink")
    catalog_path = raw_path.resolve()
    if (
        not catalog_root.is_dir()
        or catalog_root.is_symlink()
        or not is_within(catalog_root, catalog_path)
        or not catalog_path.is_file()
    ):
        raise RuntimeError("staged pass catalog escaped the active project")
    size = catalog_path.stat().st_size
    if size <= 0 or size > MAX_ACTIVE_CAPTURE_CATALOG_BYTES:
        raise RuntimeError("staged pass catalog size is invalid")
    digest = _strict_lower_hex(
        staged.get("catalogSha256"), 64, "stagedCatalog.catalogSha256"
    )
    try:
        payload = catalog_path.read_bytes()
    except OSError as exc:
        raise RuntimeError("staged pass catalog is unavailable") from exc
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
        raise RuntimeError("staged pass catalog changed after preparation")
    try:
        value = json.loads(payload.decode("utf-8-sig", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("staged pass catalog is not UTF-8 JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != "renderdoc-ultra-capture-symbols/v1"
        or value.get("schemaVersion") != 1
        or value.get("captureId") != capture_id
        or not isinstance(value.get("shaders"), list)
        or not isinstance(value.get("uses"), list)
    ):
        raise RuntimeError("staged pass catalog schema/identity is invalid")
    return value


def _strict_ai_pass_proposals(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 100000:
        raise ValueError("proposals must be an array with at most 100000 items")
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError("proposals[%d] must be an object" % index)
        unknown = set(item) - _PASS_AI_PROPOSAL_FIELDS
        if unknown:
            raise ValueError(
                "proposals[%d] contains forbidden fields: %s"
                % (index, ", ".join(sorted(str(key) for key in unknown)))
            )
        if "displayLabel" in item and "label" in item:
            raise ValueError(
                "proposals[%d] must not contain both displayLabel and label" % index
            )
        event_id = item.get("eventId")
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
            raise ValueError("proposals[%d].eventId must be a positive integer" % index)
        for field in set(item) - {"eventId"}:
            _strict_pass_panel_text(
                item[field],
                "proposals[%d].%s" % (index, field),
                _PASS_AI_PROPOSAL_TEXT_LIMITS[field],
                allow_lf=(field == "rationale"),
            )
        category = item.get("category", "").strip()
        if category and category not in CATEGORIES:
            raise ValueError("proposals[%d].category is unsupported" % index)
        result.append(copy.deepcopy(item))
    return result


def _publish_pass_classification(
    classification: dict[str, Any],
    expected_context: dict[str, Any],
    *,
    capture_id: str,
    capture_binding_mode: str,
) -> dict[str, Any]:
    capture_session_id = _strict_lower_hex(
        expected_context.get("capture", {}).get("captureSessionId"),
        32,
        "capture.captureSessionId",
    )
    capture_id = _strict_lower_hex(capture_id, 64, "pass captureId")
    if capture_binding_mode not in {
        _PASS_CAPTURE_BINDING_DISPLAY_SESSION,
        _PASS_CAPTURE_BINDING_PROJECT_CATALOG,
    }:
        raise RuntimeError("pass-panel capture binding mode is invalid")
    if classification.get("captureId") != capture_id:
        raise RuntimeError("pass-panel classification capture identity changed")
    _require_same_active_capture(expected_context, allow_missing_capture_id=True)
    panel_classification = _pass_panel_classification_view(classification)
    result = ipc.call(
        "load_pass_classification",
        {
            "classification": panel_classification,
            "expected_capture_session_id": capture_session_id,
            "capture_binding_mode": capture_binding_mode,
        },
        timeout=60.0,
    )
    if (
        not isinstance(result, dict)
        or result.get("schema") != "replaylab-pass-panel-load/v1"
        or result.get("schemaVersion") != 1
        or result.get("captureId") != capture_id
        or result.get("captureSessionId") != capture_session_id
        or result.get("captureBindingMode") != capture_binding_mode
        or result.get("loaded") is not True
        or result.get("replayMutation") is not False
        or result.get("classificationAuthority") != "FixedProgram"
        or result.get("aiProposalAuthority") != "AIProvisional"
    ):
        raise RuntimeError("RenderDoc returned invalid pass-panel publication evidence")
    _require_same_active_capture(expected_context, allow_missing_capture_id=True)
    return result


def _active_bound_shader(
    context: dict[str, Any], event_id: int, stage: str
) -> dict[str, Any]:
    if isinstance(event_id, bool) or int(event_id) <= 0:
        raise ValueError("event_id must be a positive integer")
    requested_event = int(event_id)
    requested_stage = normalize_stage(stage)
    if not requested_stage:
        raise ValueError("stage is required")

    # Prefer the immutable, hash-validated full-capture catalog.  It resolves
    # the exact frozen EID even if the user has navigated to another event in
    # qrenderdoc since the task snapshot was taken.
    selected_binding = _active_capture_catalog_selection(
        requested_event, requested_stage
    )
    if selected_binding is not None:
        selected = selected_binding[2]
        return {
            "stage": selected.key.stage,
            "resourceId": selected.key.resource_id,
            "rawSha256": selected.key.raw_sha256,
            "entryPoint": selected.key.entry_point,
        }

    # Reuse the already frozen context when it describes the requested event.
    # This preserves the no-extra-replay fast path used by current-event UI
    # actions, while a task whose frozen EID differs from the user's new UI
    # selection falls through to the explicit event query below.
    events = context.get("events")
    try:
        context_event = int(events.get("currentEventId", 0) or 0)
    except (AttributeError, TypeError, ValueError, OverflowError):
        context_event = 0
    shaders = context.get("shaders")
    bound = shaders.get("bound") if isinstance(shaders, dict) else None
    matches = []
    if context_event == requested_event and isinstance(bound, list):
        for item in bound:
            if not isinstance(item, dict):
                continue
            try:
                item_stage = normalize_stage(item.get("stage"))
            except Exception:
                continue
            if item_stage == requested_stage:
                matches.append(item)
    if len(matches) == 1:
        shader = matches[0]
    else:
        shader = None

    # A project without a full catalog still queries the requested event
    # explicitly.  Never reinterpret a frozen task from context.currentEventId.
    if shader is None:
        shader = ipc.call(
            "get_shader_info",
            {"event_id": requested_event, "stage": requested_stage},
            timeout=120.0,
        )
    if not isinstance(shader, dict) or shader.get("error"):
        raise RuntimeError("RenderDoc did not return the frozen event Shader identity")
    resource = shader.get(
        "resourceId", shader.get("shaderResourceId", shader.get("shaderId"))
    )
    if isinstance(resource, bool):
        raise RuntimeError("bound Shader ResourceId is invalid")
    try:
        resource = int(resource)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("bound Shader ResourceId is invalid") from exc
    if resource <= 0:
        raise RuntimeError("bound Shader ResourceId is invalid")
    raw_sha256 = _strict_lower_hex(shader.get("rawSha256"), 64, "shader.rawSha256")
    return {
        "stage": requested_stage,
        "resourceId": resource,
        "rawSha256": raw_sha256,
        "entryPoint": str(shader.get("entryPoint", "") or ""),
    }


def _active_project_workspace_root(status: dict[str, Any]) -> Path:
    paths = status.get("paths")
    if not isinstance(paths, dict) or not paths.get("shaderWorkspaces"):
        raise RuntimeError("active project did not expose its Shader workspace root")
    root = Path(str(paths["shaderWorkspaces"])).resolve()
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("active project Shader workspace root is unavailable")
    return root


def _require_managed_active_workspace(
    active_status: dict[str, Any], workspace_path: str
) -> Path:
    root = _active_project_workspace_root(active_status)
    raw = Path(workspace_path).expanduser()
    if raw.exists() and raw.is_symlink():
        raise RuntimeError("Shader workspace must not be a symlink")
    workspace = raw.resolve()
    if not workspace.is_dir() or not is_within(root, workspace):
        raise RuntimeError("Shader workspace is outside the active project")
    return workspace


def _validate_active_export(
    exported: Any, export_root: Path, live_capture_id: str
) -> tuple[Path, str]:
    if not isinstance(exported, dict):
        raise RuntimeError("RenderDoc returned an invalid capture catalog export")
    exported_capture_id = _strict_lower_hex(
        exported.get("captureId"), 64, "exported captureId"
    )
    # The bridge's captureId is scoped to the exported catalog. Validate a
    # pre-existing live catalog ID, but do not require a filtered and a full
    # export from the same capture session to have the same digest.
    if live_capture_id:
        _strict_lower_hex(live_capture_id, 64, "live captureId")
    catalog_text = str(exported.get("catalogPath", exported.get("path", "")) or "")
    if not catalog_text:
        raise RuntimeError("RenderDoc did not return catalogPath")
    raw = Path(catalog_text).expanduser()
    if raw.exists() and raw.is_symlink():
        raise RuntimeError("exported capture catalog must not be a symlink")
    path = raw.resolve()
    if not is_within(export_root, path) or not path.is_file():
        raise RuntimeError("exported capture catalog escaped its server-owned directory")
    size = path.stat().st_size
    if size <= 0 or size > MAX_ACTIVE_CAPTURE_CATALOG_BYTES:
        raise RuntimeError("exported capture catalog size is invalid")
    return path, exported_capture_id


def _validate_verified_active_alias_table(
    path: Path, capture_id: str, catalog_sha256: str
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("verified alias export is missing or redirected")
    size = path.stat().st_size
    if size <= 0 or size > 32 * 1024 * 1024:
        raise RuntimeError("verified alias export size is invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("verified alias export is not UTF-8 JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != "renderdoc-ultra-aliases/v1"
        or value.get("schemaVersion") != 1
        or value.get("verifiedOnly") is not True
        or value.get("captureId") != capture_id
        or value.get("catalogSha256") != catalog_sha256
    ):
        raise RuntimeError(
            "alias export is not catalog-revision-bound verified-only evidence"
        )
    aliases = value.get("aliases")
    if not isinstance(aliases, list) or len(aliases) > 1000000:
        raise RuntimeError("verified alias export aliases are invalid or oversized")
    for index, alias in enumerate(aliases):
        if (
            not isinstance(alias, dict)
            or alias.get("verified") is not True
            or alias.get("captureId") != capture_id
            or str(alias.get("status", "") or "") == "AIProvisional"
        ):
            raise RuntimeError("aliases[%d] is not program-verified" % index)
    return value


def _active_recovery_source_preview(
    asset_root: Path, candidate: dict[str, Any], maximum_utf8_bytes: int
) -> dict[str, Any] | None:
    """Revalidate one small text candidate and return data, never instructions."""

    if (
        candidate.get("format") != "text"
        or candidate.get("role") not in {"source", "include"}
        or int(candidate.get("fileOffset", -1) or 0) != 0
    ):
        return None
    metadata = candidate.get("sourceMetadata")
    if not isinstance(metadata, dict) or metadata.get("decodeErrors") is True:
        return None
    try:
        expected_size = int(candidate.get("fileByteSize", -1))
        expected_hash = str(candidate.get("fileSha256", "") or "").casefold()
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        expected_size <= 0
        or expected_size > 2 * 1024 * 1024
        or len(expected_hash) != 64
        or any(character not in "0123456789abcdef" for character in expected_hash)
    ):
        return None
    relative_value = str(candidate.get("relativePath", "") or "")
    relative = Path(relative_value)
    if not relative_value or relative.is_absolute():
        return None
    root = asset_root.absolute()
    try:
        payload, _read_error, _consumed = read_recovery_regular_file(
            root, relative, expected_size
        )
    except (OSError, TypeError, ValueError):
        return None
    if (
        payload is None
        or len(payload) != expected_size
        or hashlib.sha256(payload).hexdigest() != expected_hash
    ):
        return None
    encoding = str(metadata.get("encoding", "") or "").casefold()
    try:
        source = payload.decode("utf-16" if encoding == "utf-16" else "utf-8-sig")
    except UnicodeDecodeError:
        return None
    normalised = source.encode("utf-8")
    preview_bytes = normalised[:maximum_utf8_bytes]
    if len(preview_bytes) < len(normalised):
        preview_bytes = preview_bytes.decode("utf-8", errors="ignore").encode("utf-8")
    preview = preview_bytes.decode("utf-8")
    return {
        "candidateId": candidate.get("candidateId", ""),
        "relativePath": relative_value,
        "fileSha256": expected_hash,
        "sourcePreview": preview,
        "sourcePreviewSha256": hashlib.sha256(preview_bytes).hexdigest(),
        "sourcePreviewUtf8Bytes": len(preview_bytes),
        "sourcePreviewTruncated": len(preview_bytes) < len(normalised),
    }


def _recovery_candidate_report_summary(
    candidate: dict[str, Any], assessment: dict[str, Any]
) -> dict[str, Any]:
    """Project cached parser metadata to a small, fixed report shape."""

    source = candidate.get("sourceMetadata")
    source_summary: dict[str, Any] = {}
    if isinstance(source, dict):
        source_summary = {
            "encoding": str(source.get("encoding", "") or "")[:16],
            "decodeErrors": bool(source.get("decodeErrors")),
            "inspectionBytes": int(source.get("inspectionBytes", 0) or 0),
            "inspectionTruncated": bool(source.get("inspectionTruncated")),
            "stageHints": [str(value)[:64] for value in source.get("stageHints", [])[:8]],
            "entryPointHints": copy.deepcopy(source.get("entryPointHints", [])[:8]),
            "includeDirectiveCount": int(
                source.get("includeDirectiveCount", 0) or 0
            ),
            "includeDetailsOmitted": True,
            "hintsTrusted": False,
        }
    return {
        "candidateId": str(candidate.get("candidateId", "") or "")[:64],
        "relativePath": str(candidate.get("relativePath", "") or "")[:4096],
        "fileSha256": str(candidate.get("fileSha256", "") or "")[:64],
        "fileOffset": int(candidate.get("fileOffset", 0) or 0),
        "byteSize": int(candidate.get("byteSize", 0) or 0),
        "format": str(candidate.get("format", "") or "")[:64],
        "language": str(candidate.get("language", "") or "")[:64],
        "role": str(candidate.get("role", "") or "")[:64],
        "fingerprints": copy.deepcopy(candidate.get("fingerprints", {})),
        "shaderFacts": copy.deepcopy(candidate.get("shaderFacts", {})),
        "engineHints": [
            str(value)[:64] for value in candidate.get("engineHints", [])[:8]
        ],
        "sourceMetadata": source_summary,
        "assessment": assessment,
    }


def _server_json_utf8_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def _finalise_bounded_recovery_report(report: dict[str, Any]) -> dict[str, Any]:
    """Enforce the active/one-shot report byte cap after all projections."""

    report["reportJsonUtf8Bytes"] = 0

    def measure() -> int:
        for _ in range(4):
            value = _server_json_utf8_bytes(report)
            if report["reportJsonUtf8Bytes"] == value:
                return value
            report["reportJsonUtf8Bytes"] = value
        return _server_json_utf8_bytes(report)

    measured = measure()
    previews = report.get("sourcePreviews")
    while measured > _RECOVERY_REPORT_MAX_JSON_UTF8_BYTES and isinstance(previews, list) and previews:
        previews.pop()
        report["sourcePreviewsTruncatedByReportBudget"] = True
        measured = measure()
    shader_reports = report.get("boundShaders")
    while measured > _RECOVERY_REPORT_MAX_JSON_UTF8_BYTES and isinstance(shader_reports, list):
        removable = [
            item
            for item in shader_reports
            if isinstance(item, dict) and isinstance(item.get("topCandidates"), list)
            and item["topCandidates"]
        ]
        if not removable:
            break
        # Remove only the least valuable tail item from the currently largest
        # list, preserving every per-shader best-first prefix.
        target = max(removable, key=lambda item: len(item["topCandidates"]))
        target["topCandidates"].pop()
        target["candidatesTruncated"] = True
        report["candidateMetadataTruncatedByReportBudget"] = True
        measured = measure()
    if measured > _RECOVERY_REPORT_MAX_JSON_UTF8_BYTES:
        raise RuntimeError("asset Shader recovery summary exceeds its fixed byte bound")
    return report


def _active_asset_recovery_report(
    inventory: dict[str, Any], context: dict[str, Any], asset_root: str
) -> dict[str, Any]:
    """Produce a bounded current-pipeline view over a cached inventory."""

    candidates = inventory.get("candidates")
    if not isinstance(candidates, list):
        raise RuntimeError("asset recovery inventory has no candidate list")
    shaders = context.get("shaders")
    bound = shaders.get("bound") if isinstance(shaders, dict) else None
    bound = bound if isinstance(bound, list) else []
    shader_reports: list[dict[str, Any]] = []
    candidate_by_id = {
        str(candidate.get("candidateId", "")): candidate
        for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("candidateId")
    }

    def rank_key(item: dict[str, Any]) -> tuple[Any, ...]:
        assessment = item["assessment"]
        matches = assessment.get("matches")
        match_values = matches if isinstance(matches, dict) else {}
        exact_fingerprint_tier = (
            2
            if match_values.get("rawSha256Equal") is True
            else 1 if match_values.get("codeSha256Equal") is True else 0
        )
        return (
            -exact_fingerprint_tier,
            -int(assessment["recoveryLevel"]),
            -int(assessment["identityValue"]),
            -int(assessment["sourceValue"]),
            -int(assessment["compileUtility"]),
            str(item["relativePath"]).casefold(),
            str(item["candidateId"]),
        )

    for shader in bound[:_RECOVERY_REPORT_MAX_BOUND_SHADERS]:
        if not isinstance(shader, dict):
            continue
        try:
            stage = normalize_stage(shader.get("stage"))
        except (TypeError, ValueError):
            continue
        if not stage:
            continue
        raw_resource_id = shader.get(
            "resourceId", shader.get("shaderResourceId", 0)
        )
        if isinstance(raw_resource_id, bool):
            continue
        try:
            resource_id = int(raw_resource_id)
        except (TypeError, ValueError, OverflowError):
            continue
        if resource_id <= 0 or resource_id > 0xFFFFFFFFFFFFFFFF:
            continue
        raw_hash = str(shader.get("rawSha256", "") or "").strip().casefold()
        if raw_hash and (
            len(raw_hash) != 64
            or any(character not in "0123456789abcdef" for character in raw_hash)
        ):
            continue
        facts = {
            "stage": stage,
            "profile": shader.get("targetProfile", shader.get("profile", "")),
            "entryPoint": shader.get("entryPoint", ""),
            "rawSha256": raw_hash,
            "codeSha256": shader.get("codeSha256", ""),
            "interfaceHash": shader.get("interfaceHash", ""),
            "layoutHash": shader.get("layoutHash", ""),
            "name": shader.get("debugName", shader.get("name", "")),
            "path": shader.get("debugPath", shader.get("sourcePath", "")),
        }
        ranked: list[dict[str, Any]] = []
        valuable_count = 0
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            try:
                assessment = evaluate_recovery_candidate(candidate, facts)
            except (TypeError, ValueError):
                continue
            if int(assessment.get("recoveryLevel", 0) or 0) <= 0:
                continue
            valuable_count += 1
            ranked.append(_recovery_candidate_report_summary(candidate, assessment))
            if len(ranked) > _RECOVERY_REPORT_TOP_CANDIDATES_PER_SHADER * 2:
                ranked.sort(key=rank_key)
                del ranked[_RECOVERY_REPORT_TOP_CANDIDATES_PER_SHADER:]
        ranked.sort(key=rank_key)
        del ranked[_RECOVERY_REPORT_TOP_CANDIDATES_PER_SHADER:]
        shader_reports.append(
            {
                "stage": stage,
                "shaderResourceId": resource_id,
                "entryPoint": str(shader.get("entryPoint", "") or ""),
                "rawSha256": raw_hash,
                "candidateCountWithValue": valuable_count,
                "topCandidates": ranked,
                "candidatesTruncated": valuable_count > len(ranked),
            }
        )

    preview_ids: list[str] = []
    for candidate_rank in range(_RECOVERY_REPORT_TOP_CANDIDATES_PER_SHADER):
        for shader_report in shader_reports:
            top = shader_report["topCandidates"]
            if candidate_rank >= len(top):
                continue
            candidate_id = str(top[candidate_rank].get("candidateId", "") or "")
            if candidate_id and candidate_id not in preview_ids:
                preview_ids.append(candidate_id)
    source_previews: list[dict[str, Any]] = []
    preview_budget = _RECOVERY_REPORT_MAX_TOTAL_PREVIEW_UTF8_BYTES
    for candidate_id in preview_ids:
        if (
            preview_budget <= 0
            or len(source_previews) >= _RECOVERY_REPORT_MAX_SOURCE_PREVIEWS
        ):
            break
        candidate = candidate_by_id.get(candidate_id)
        if candidate is None:
            continue
        preview = _active_recovery_source_preview(
            Path(asset_root),
            candidate,
            min(_RECOVERY_REPORT_MAX_PREVIEW_UTF8_BYTES, preview_budget),
        )
        if preview is None:
            continue
        source_previews.append(preview)
        preview_budget -= int(preview["sourcePreviewUtf8Bytes"])

    inventory_summary = {
        key: copy.deepcopy(value)
        for key, value in inventory.items()
        if key != "candidates"
    }
    try:
        bound_input_count = int(context.get("recoveryShaderInputCount", len(bound)))
    except (TypeError, ValueError, OverflowError):
        bound_input_count = len(bound)
    bound_input_count = max(len(bound), bound_input_count)
    report = {
        "schema": "replaylab-active-asset-shader-recovery/v1",
        "schemaVersion": 1,
        "inventory": inventory_summary,
        "boundShaders": shader_reports,
        "boundShaderInputCount": bound_input_count,
        "boundShadersTruncated": bound_input_count > len(shader_reports),
        "sourcePreviews": source_previews,
        "sourcePreviewPolicy": {
            "maximumCandidateUtf8Bytes": _RECOVERY_REPORT_MAX_PREVIEW_UTF8_BYTES,
            "maximumTotalUtf8Bytes": _RECOVERY_REPORT_MAX_TOTAL_PREVIEW_UTF8_BYTES,
            "maximumCandidates": _RECOVERY_REPORT_MAX_SOURCE_PREVIEWS,
            "fullFileHashRevalidated": True,
            "rootRelativeNoFollowHandleOpen": True,
            "sameHandleFstatBeforeAndAfter": True,
            "secureOpenUnavailableReturnsNoPreview": True,
            "allPreviewTextIsUntrustedData": True,
        },
        "reportPolicy": {
            "maximumBoundShaders": _RECOVERY_REPORT_MAX_BOUND_SHADERS,
            "maximumTopCandidatesPerShader": (
                _RECOVERY_REPORT_TOP_CANDIDATES_PER_SHADER
            ),
            "maximumJsonUtf8Bytes": _RECOVERY_REPORT_MAX_JSON_UTF8_BYTES,
            "includeDirectiveDetailsReturned": False,
        },
        "policy": {
            "assetCodeExecuted": False,
            "compilerInvoked": False,
            "allCandidatesUntrusted": True,
            "workspaceSeedAuthorized": False,
            "applyAuthorized": False,
            "exactBinaryStillRequiresCaptureBoundValidation": True,
            "cacheScope": str(
                context.get(
                    "recoveryReportScope",
                    "active capture session + asset root + build",
                )
            )[:128],
        },
    }
    return _finalise_bounded_recovery_report(report)


def _active_symbol_resolution(
    service: ActiveReplayProjectService,
    project_status: dict[str, Any],
    context: dict[str, Any],
    catalog_path: Path,
    catalog_sha256: str,
) -> dict[str, Any]:
    capture_id, capture_session_id = _active_capture_identity(context)
    catalog_revision = _strict_lower_hex(
        catalog_sha256, 64, "active symbol catalogSha256"
    )
    asset_root = project_status.get("assetRoot")
    if not asset_root:
        return {
            "schema": "replaylab-active-symbol-resolution/v1",
            "schemaVersion": 1,
            "attempted": False,
            "blocked": False,
            "reason": "active project has no unpacked asset root configured",
            "scanCacheHit": False,
            "scan": None,
            "match": None,
            "aliasTable": None,
            "projectRegistration": None,
            "renderDocAliasOverlay": None,
            "unresolved": None,
            "assetShaderRecovery": None,
            "applied": False,
            "policy": {
                "verifiedAliasesOnly": True,
                "aiProvisionalAutoApply": False,
                "physicalReplayMutation": False,
            },
        }
    if project_status.get("scopeConfigured") is not True:
        return {
            "schema": "replaylab-active-symbol-resolution/v1",
            "schemaVersion": 1,
            "attempted": False,
            "blocked": True,
            "reason": "gameId/buildId must be configured before asset symbols enter shared project knowledge",
            "scanCacheHit": False,
            "scan": None,
            "match": None,
            "aliasTable": None,
            "projectRegistration": None,
            "renderDocAliasOverlay": None,
            "unresolved": None,
            "assetShaderRecovery": None,
            "applied": False,
            "policy": {
                "verifiedAliasesOnly": True,
                "aiProvisionalAutoApply": False,
                "physicalReplayMutation": False,
            },
        }
    if project_status.get("assetRootAvailable") is not True:
        raise RuntimeError("configured active-project asset root is unavailable")
    asset_root = str(Path(str(asset_root)).resolve())
    build_id = str(project_status.get("scope", {}).get("buildId", "") or "")
    assets_directory = service.shared_dictionary_directory("assets")
    database_path = assets_directory / "symbols-v3.sqlite3"
    symbols = SymbolService.for_asset_root(asset_root, database_path)
    cache_key = (
        str(project_status.get("workspaceId", "")),
        capture_session_id,
        os.path.normcase(asset_root),
        build_id,
    )
    _require_same_active_capture(context, allow_missing_capture_id=True)
    database_identity = str(symbols.database_path)
    cached = _active_symbol_scan_cache_get(cache_key, database_identity)
    if cached is None:
        stripe_material = "\0".join(cache_key).encode("utf-8")
        stripe = _ACTIVE_SYMBOL_SCAN_STRIPES[
            hashlib.sha256(stripe_material).digest()[0]
            % len(_ACTIVE_SYMBOL_SCAN_STRIPES)
        ]
        with stripe:
            # Recheck after waiting for a same-key scan. File walking, hashing,
            # SQLite work, and candidate assessment remain outside the global
            # LRU lock while duplicate same-key scans are avoided.
            cached = _active_symbol_scan_cache_get(cache_key, database_identity)
            if cached is None:
                computed_scan = symbols.scan(asset_root, build_id=build_id)
                if not isinstance(computed_scan, dict) or not str(
                    computed_scan.get("scanId", "") or ""
                ):
                    raise RuntimeError("asset scanner returned an invalid scan identity")
                computed_inventory = discover_recovery_assets(
                    asset_root,
                    max_candidates=_RECOVERY_ACTIVE_CACHE_CANDIDATES,
                )
                cached = _active_symbol_scan_cache_put_if_absent(
                    cache_key,
                    {
                        "scan": dict(computed_scan),
                        "databasePath": database_identity,
                        "recoveryInventory": computed_inventory,
                    },
                )
                cache_hit = False
            else:
                cache_hit = True
    else:
        cache_hit = True
    scan = dict(cached["scan"])
    recovery_inventory = cached.get("recoveryInventory")
    if not isinstance(recovery_inventory, dict):
        raise RuntimeError("cached asset recovery inventory is invalid")
    # Per-bound-Shader ranking and source preview validation can read/sort; it
    # must never serialize all sessions behind the cache guard.
    asset_recovery = _active_asset_recovery_report(
        recovery_inventory, context, asset_root
    )
    _require_same_active_capture(context, allow_missing_capture_id=True)
    match = symbols.match(catalog_path, str(scan["scanId"]))
    if not isinstance(match, dict) or match.get("captureId") != capture_id:
        raise RuntimeError("asset matcher returned a different capture identity")
    aliases_directory = service.shared_dictionary_directory("aliases")
    alias_key = hashlib.sha256(
        (
            capture_id
            + "\0"
            + catalog_revision
            + "\0"
            + str(scan["scanId"])
        ).encode("utf-8")
    ).hexdigest()
    alias_path = aliases_directory / (alias_key + ".verified.json")
    alias_result = symbols.export_alias_table(
        capture_id,
        alias_path,
        verified_only=True,
        scan_id=str(scan["scanId"]),
        catalog_sha256=catalog_revision,
    )
    if (
        not isinstance(alias_result, dict)
        or alias_result.get("verifiedOnly") is not True
        or alias_result.get("captureId") != capture_id
        or alias_result.get("catalogSha256") != catalog_revision
        or Path(str(alias_result.get("path", ""))).resolve() != alias_path.resolve()
    ):
        raise RuntimeError("symbol resolver returned an invalid verified alias export")
    _validate_verified_active_alias_table(
        alias_path, capture_id, catalog_revision
    )
    registered = service.register_alias_table(alias_path)
    if not isinstance(registered, dict) or registered.get("applied") is not False:
        raise RuntimeError("active project alias registration returned invalid policy evidence")
    _require_same_active_capture(context, allow_missing_capture_id=True)
    overlay = ipc.call(
        "load_symbol_aliases",
        {
            "path": str(alias_path),
            "confirmed_only": True,
            "expected_sha256": _file_sha256(alias_path),
        },
        timeout=60.0,
    )
    if (
        not isinstance(overlay, dict)
        or overlay.get("schema") != "renderdoc-ultra-aliases/v1"
        or overlay.get("schemaVersion") != 1
        or overlay.get("captureId") != capture_id
        or overlay.get("confirmedOnly") is not True
    ):
        raise RuntimeError("RenderDoc returned invalid verified alias-overlay evidence")
    _require_same_active_capture(context, allow_missing_capture_id=True)
    unresolved = symbols.unresolved(capture_id)
    return {
        "schema": "replaylab-active-symbol-resolution/v1",
        "schemaVersion": 1,
        "attempted": True,
        "blocked": False,
        "reason": "",
        "scanCacheHit": cache_hit,
        "databasePath": str(symbols.database_path),
        "scan": scan,
        "match": match,
        "aliasTable": alias_result,
        "projectRegistration": registered,
        "renderDocAliasOverlay": overlay,
        "unresolved": unresolved,
        "assetShaderRecovery": asset_recovery,
        "applied": False,
        "policy": {
            "verifiedAliasesOnly": True,
            "aiProvisionalAutoApply": False,
            "physicalReplayMutation": False,
            "displayAliasOverlayLoaded": True,
            "assetRecoveryAutoDiscovered": True,
            "assetRecoveryMaySeedWorkspace": False,
        },
    }


def _active_shader_asset_evidence_not_ready(
    reason_code: str,
    reason: str,
    *,
    prerequisites: list[str] | None = None,
) -> str:
    """Return a non-blocking, mutation-free active asset evidence state."""

    return _ok(
        {
            "schema": "replaylab-active-shader-asset-evidence/v1",
            "schemaVersion": 1,
            "ready": False,
            "reasonCode": str(reason_code or "not-ready"),
            "reason": str(reason or "active Shader asset evidence is unavailable"),
            "requiredAction": "prepare_active_capture_project",
            "prerequisites": list(prerequisites or []),
            "boundIdentity": None,
            "symbolEvidence": None,
            "relationView": None,
            "applied": False,
            "policy": {
                "activeProjectOnly": True,
                "activeFullCatalogOnly": True,
                "managedSymbolDatabaseOnly": True,
                "readOnlyDatabase": True,
                "assetScanTriggered": False,
                "compilerInvoked": False,
                "aliasMutation": False,
                "materialEntitySynthesized": False,
            },
        }
    )


def _shader_asset_constant_blocks(
    metadata: Any,
) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(metadata, Mapping):
        return "", []
    for field in ("constantBlocks", "constantBuffers", "constant_buffers"):
        value = metadata.get(field)
        if isinstance(value, list):
            return field, [
                copy.deepcopy(item) for item in value if isinstance(item, Mapping)
            ]
    return "", []


def _shader_asset_material_hints(candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return bounded unverified material hints without creating an entity."""

    asset = candidate.get("asset")
    if not isinstance(asset, Mapping):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(value: Any, basis: str) -> None:
        if len(result) >= 20 or not isinstance(value, str):
            return
        text = value.strip()
        if not text or len(text) > 2048 or "\x00" in text:
            return
        key = (text, basis)
        if key in seen:
            return
        seen.add(key)
        result.append(
            {
                "evidenceClass": "inferred",
                "value": text,
                "basis": basis,
                "materialEntityVerified": False,
            }
        )

    material_token = re.compile(
        r"material|(?:^|[/_.-])mat(?:[/_.-]|$)", re.I
    )
    for field in ("displayName", "assetPath"):
        value = asset.get(field)
        if isinstance(value, str) and material_token.search(value):
            add(value, "asset-record-%s-token" % field)

    metadata = asset.get("metadata")
    if isinstance(metadata, Mapping):
        for key, value in metadata.items():
            key_text = str(key or "")
            if "material" not in key_text.casefold():
                continue
            if isinstance(value, str):
                add(value, "unverified-asset-metadata:%s" % key_text[:128])
            elif isinstance(value, list):
                for item in value[:20]:
                    add(item, "unverified-asset-metadata:%s" % key_text[:128])
    return result


def _shader_asset_relation_view(
    bound_identity: Mapping[str, Any], evidence: Mapping[str, Any]
) -> dict[str, Any]:
    capture_shader = evidence.get("captureShader")
    capture_shader = capture_shader if isinstance(capture_shader, Mapping) else {}
    capture_field, capture_blocks = _shader_asset_constant_blocks(capture_shader)
    relations: list[dict[str, Any]] = []
    raw_candidates = evidence.get("candidates")
    candidates = raw_candidates if isinstance(raw_candidates, list) else []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        match = candidate.get("match")
        asset = candidate.get("asset")
        match = match if isinstance(match, Mapping) else {}
        asset = asset if isinstance(asset, Mapping) else {}
        match_evidence = match.get("evidence")
        match_evidence = (
            match_evidence if isinstance(match_evidence, Mapping) else {}
        )
        physical_identity = bool(
            match_evidence.get("rawHashEqual") is True
            and match_evidence.get("captureRawHashVerified") is True
            and match_evidence.get("assetRawHashVerified") is True
        ) or bool(
            match_evidence.get("executableHashEqual") is True
            and match_evidence.get("captureExecutableHashVerified") is True
            and match_evidence.get("assetExecutableHashVerified") is True
            and match_evidence.get("assetSymbolMetadataTrusted") is True
            and match_evidence.get("layoutHashEqual") is True
            and match_evidence.get("interfaceHashEqual") is True
        )
        metadata = asset.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        asset_field, asset_blocks = _shader_asset_constant_blocks(metadata)
        asset_metadata_trusted = metadata.get("_symbolMetadataTrusted") is True
        layout_observed = bool(
            asset_metadata_trusted
            and match_evidence.get("layoutHashEqual") is True
        )
        related_text = []
        raw_related = asset.get("relatedTextShaders")
        for related in raw_related if isinstance(raw_related, list) else []:
            if not isinstance(related, Mapping):
                continue
            related_text.append(
                {
                    "evidenceClass": "inferred",
                    "relationship": "deterministic-token-overlap",
                    "relativePath": str(related.get("relativePath", "") or ""),
                    "tokenScore": int(related.get("tokenScore", 0) or 0),
                    "sourceOrOwnershipVerified": False,
                }
            )
        candidate_id = int(
            match.get("candidateId", asset.get("assetShaderId", 0)) or 0
        )
        relations.append(
            {
                "candidateId": candidate_id,
                "assetRecord": {
                    "evidenceClass": "observed",
                    "assetPath": str(asset.get("assetPath", "") or ""),
                    "displayName": str(asset.get("displayName", "") or ""),
                    "provenance": str(asset.get("provenance", "") or ""),
                    "rawSha256": str(asset.get("rawSha256", "") or ""),
                    "codeSha256": str(asset.get("codeSha256", "") or ""),
                    "layoutHash": str(asset.get("layoutHash", "") or ""),
                },
                "identityRelation": {
                    "evidenceClass": "observed" if physical_identity else "inferred",
                    "status": str(match.get("status", "") or ""),
                    "score": match.get("score"),
                    "selected": match.get("selected") is True,
                    "physicalIdentityProgramVerified": physical_identity,
                    "basis": copy.deepcopy(dict(match_evidence)),
                },
                "relatedTextEvidence": related_text,
                "materialEvidence": {
                    "observedEntities": [],
                    "inferredHints": _shader_asset_material_hints(candidate),
                    "materialEntityObserved": False,
                    "materialEntitySynthesized": False,
                    "reason": (
                        "the symbol database contains no typed material-entity relation; "
                        "names and metadata remain hints only"
                    ),
                },
                "cbufferEvidence": {
                    "captureEvidenceRef": "relationView.captureShader.cbufferEvidence",
                    "asset": {
                        "evidenceClass": (
                            "observed" if asset_metadata_trusted else "inferred"
                        ),
                        "sourceField": asset_field,
                        "metadataTrusted": asset_metadata_trusted,
                        "blocks": asset_blocks,
                    },
                    "layoutRelation": {
                        "evidenceClass": "observed" if layout_observed else "inferred",
                        "layoutHashEqual": (
                            match_evidence.get("layoutHashEqual") is True
                        ),
                        "assetSymbolMetadataTrusted": asset_metadata_trusted,
                    },
                },
            }
        )
    return {
        "schema": "replaylab-shader-asset-relations/v1",
        "schemaVersion": 1,
        "captureShader": {
            "evidenceClass": "observed",
            "identity": copy.deepcopy(dict(bound_identity)),
            "cbufferEvidence": {
                "evidenceClass": "observed",
                "sourceField": capture_field,
                "blocks": capture_blocks,
            },
        },
        "assetCandidates": relations,
        "candidateCount": len(relations),
        "evidenceBoundary": {
            "observed": (
                "capture/catalog identity, reflected capture CBuffers, database records, "
                "and hash/interface facts explicitly reported by the fixed matcher"
            ),
            "inferred": (
                "untrusted asset metadata, token-related text, semantic ownership, "
                "and every material hint without a typed material entity"
            ),
        },
    }


@mcp.tool()
def ensure_active_replay_project(name: str = "Untitled ReplayLab Project") -> str:
    """Create/select the process-owned pending project when none is active."""

    try:
        status = _active_project_service().ensure_pending(name)
        return _ok(
            {
                "schema": "replaylab-active-project-mcp/v1",
                "schemaVersion": 1,
                "operation": "ensure",
                "project": status,
                "applied": False,
                "policy": dict(_ACTIVE_PROJECT_MCP_POLICY),
            }
        )
    except Exception as error:
        return _active_project_error("ensure", error)


@mcp.tool()
def get_active_replay_project_status() -> str:
    """Validate the active project selected by the process-owned state file."""

    try:
        status = _active_project_service().status()
        return _ok(
            {
                "schema": "replaylab-active-project-mcp/v1",
                "schemaVersion": 1,
                "operation": "status",
                "project": status,
                "applied": False,
                "policy": dict(_ACTIVE_PROJECT_MCP_POLICY),
            }
        )
    except Exception as error:
        return _active_project_error("status", error)


@mcp.tool()
def get_active_capture_catalog_binding_status() -> str:
    """Report the exact catalog revision and its full or partial coverage."""

    try:
        context = _active_live_context(allow_missing_capture_id=True)
        binding = _active_capture_catalog_binding(context)
        if binding is None:
            result = {
                "bound": False,
                "captureSessionId": str(
                    context["capture"]["captureSessionId"]
                ),
            }
        else:
            unused_context, project_status, record, catalog_path = binding
            result = {
                "bound": True,
                "captureSessionId": record["captureSessionId"],
                "captureId": record["captureId"],
                "catalogPath": str(catalog_path),
                "catalogSha256": record["catalogSha256"],
                "catalogRevisionSha256": record["catalogSha256"],
                "catalogBindingSha256": (
                    _active_capture_catalog_binding_sha256(record)
                ),
                "projectWorkspaceId": project_status["workspaceId"],
                "projectRoot": project_status["projectRoot"],
                **_active_catalog_scope(record),
            }
        return _ok(
            {
                "schema": "replaylab-active-catalog-binding-status/v1",
                "schemaVersion": 1,
                **result,
                "applied": False,
                "policy": {
                    "fullFrameCatalogRequired": False,
                    "captureTransportVerified": True,
                    "projectOwnershipVerifiedWhenBound": True,
                    "catalogContentHashVerifiedWhenBound": True,
                    "physicalReplayMutation": False,
                },
            }
        )
    except Exception as error:
        return _active_project_error("capture-catalog-binding-status", error)


@mcp.tool()
def configure_active_replay_project(
    game_id: str,
    build_id: str,
    platform: str = "",
    api_family: str = "",
    name: str = "Untitled ReplayLab Project",
) -> str:
    """Bind the active pending project to one immutable game/build scope."""

    try:
        service = _active_project_service()
        service.ensure_pending(name)
        status = service.configure_scope(
            game_id=game_id,
            build_id=build_id,
            platform=platform,
            api_family=api_family,
        )
        return _ok(
            {
                "schema": "replaylab-active-project-mcp/v1",
                "schemaVersion": 1,
                "operation": "configure-scope",
                "project": status,
                "applied": False,
                "policy": dict(_ACTIVE_PROJECT_MCP_POLICY),
            }
        )
    except Exception as error:
        return _active_project_error("configure-scope", error)


@mcp.tool()
def configure_active_project_asset_root(asset_root: str = "") -> str:
    """Configure/clear the unpacked-asset root for the active project."""

    try:
        status = _active_project_service().configure_asset_root(asset_root or None)
        return _ok(
            {
                "schema": "replaylab-active-project-mcp/v1",
                "schemaVersion": 1,
                "operation": "configure-asset-root",
                "project": status,
                "applied": False,
                "policy": dict(_ACTIVE_PROJECT_MCP_POLICY),
            }
        )
    except Exception as error:
        return _active_project_error("configure-asset-root", error)


def _active_asset_catalog_context(
    *, require_catalog: bool, create_dictionary: bool = False
) -> tuple[
    ActiveReplayProjectService,
    dict[str, Any],
    Path,
    Path,
]:
    """Resolve the path-free active-project asset catalog boundary."""

    service = _active_project_service()
    status = service.status()
    if status.get("scopeConfigured") is not True:
        raise RuntimeError(
            "active project game/build scope must be configured before asset indexing"
        )
    if status.get("assetRootConfigured") is not True:
        raise RuntimeError("active project has no unpacked asset root configured")
    if status.get("assetRootAvailable") is not True:
        raise RuntimeError("configured active-project asset root is unavailable")
    project_root = Path(str(status.get("projectRoot", "") or "")).resolve()
    asset_root = Path(str(status.get("assetRoot", "") or "")).resolve()
    if project_root.is_symlink() or not project_root.is_dir():
        raise RuntimeError("active project root is redirected or unavailable")
    if asset_root.is_symlink() or not asset_root.is_dir():
        raise RuntimeError("active project asset root is redirected or unavailable")
    if create_dictionary:
        assets_directory = service.shared_dictionary_directory("assets")
    else:
        paths = status.get("paths")
        if not isinstance(paths, Mapping) or not paths.get("sharedDictionaries"):
            raise RuntimeError("active project omitted its shared dictionary root")
        shared_root = Path(str(paths["sharedDictionaries"])).resolve()
        assets_directory = (shared_root / "assets").resolve()
        if (
            shared_root.is_symlink()
            or not shared_root.is_dir()
            or assets_directory.is_symlink()
            or not assets_directory.is_dir()
            or not is_within(project_root, shared_root)
            or not is_within(shared_root, assets_directory)
        ):
            raise RuntimeError("active project asset dictionary is unavailable")
    database_path = (assets_directory / ACTIVE_ASSET_CATALOG_FILENAME).resolve()
    if not is_within(project_root, database_path) or not is_within(
        assets_directory, database_path
    ):
        raise RuntimeError("active project asset catalog path escaped its dictionary")
    if require_catalog and (
        database_path.is_symlink() or not database_path.is_file()
    ):
        raise RuntimeError("active project asset catalog has not been prepared")
    return service, status, asset_root, database_path


@mcp.tool()
def get_unpacked_asset_index_capabilities() -> str:
    """Report fixed generic/UEX-aware asset index and relationship capabilities."""

    try:
        return _ok(AssetCatalogService.capabilities())
    except Exception as error:
        return _err(error)


@mcp.tool()
def get_active_unpacked_asset_index_status() -> str:
    """Read the current project-owned unpacked-asset catalog status without scanning."""

    try:
        unused_service, status, asset_root, database_path = (
            _active_asset_catalog_context(require_catalog=True)
        )
        result = AssetCatalogService(database_path).status()
        if Path(str(result.get("rootPath", "") or "")).resolve() != asset_root:
            raise RuntimeError("asset catalog belongs to a stale configured root")
        if str(result.get("buildId", "") or "") != str(
            status.get("scope", {}).get("buildId", "") or ""
        ):
            raise RuntimeError("asset catalog belongs to a stale project build")
        result["project"] = {
            "workspaceId": status["workspaceId"],
            "projectRoot": status["projectRoot"],
        }
        result["backgroundJob"] = _asset_index_status(database_path)
        result["applied"] = False
        return _ok(result)
    except Exception as error:
        return _ok(
            {
                "schema": "renderdoc-ultra-unpacked-asset-index-status/v1",
                "schemaVersion": 1,
                "ready": False,
                "reason": str(error),
                "requiredAction": "index_active_unpacked_assets",
                "applied": False,
            }
        )


@mcp.tool()
def index_active_unpacked_assets(
    scan_mode: str = "incremental",
    max_files: int = ACTIVE_ASSET_DEFAULT_MAX_FILES,
    max_file_size_mb: int = ACTIVE_ASSET_DEFAULT_MAX_FILE_SIZE_MB,
    depth: str = "headers",
) -> str:
    """Build the active project's persistent generic asset relationship catalog.

    The request accepts no root, database, SQL, provider executable, or output
    path.  A complete scan atomically replaces only the managed catalog file;
    an interrupted or invalid scan leaves the preceding catalog readable.
    ``depth``: ``headers`` (default) parses the small descriptive files —
    engine package headers, materials, shaders, metadata and a material's
    cooked export — and defers bulk data, which is what shader/asset
    retrieval needs and finishes in minutes on large trees; ``full`` hashes
    and parses everything admitted by the size bounds; ``names`` only walks.
    Calling this up front is optional: ``retrieve_shader_asset_candidates``
    indexes on first need and hydrates individual candidates later.
    """

    try:
        service, status, asset_root, database_path = _active_asset_catalog_context(
            require_catalog=False,
            create_dictionary=True,
        )
        workspace_id = str(status.get("workspaceId", "") or "")
        project_root = Path(str(status.get("projectRoot", "") or "")).resolve()
        build_id = str(status.get("scope", {}).get("buildId", "") or "")
        with service.pin_active_project(workspace_id, project_root):
            result = AssetCatalogService(database_path).scan(
                asset_root,
                build_id=build_id,
                scan_mode=scan_mode,
                max_files=max_files,
                max_file_size_mb=max_file_size_mb,
                depth=depth,
            )
            current = _require_same_active_project(status)
            if (
                current.get("assetRoot") != status.get("assetRoot")
                or current.get("scope") != status.get("scope")
            ):
                raise RuntimeError("active project scope changed during asset indexing")
        result["project"] = {
            "workspaceId": workspace_id,
            "projectRoot": str(project_root),
        }
        result["applied"] = True
        return _ok(result)
    except AssetCatalogScanLimitError as error:
        return _ok(
            {
                "schema": "renderdoc-ultra-unpacked-asset-index-error/v1",
                "schemaVersion": 1,
                "operation": "index-unpacked-assets",
                "error": str(error),
                "type": type(error).__name__,
                "applied": False,
                "catalogReplaced": False,
                "coverage": {
                    "complete": False,
                    "truncated": True,
                    "walkedFiles": error.observed_files,
                    "maximumFiles": error.maximum_files,
                },
                "policy": dict(_ACTIVE_PROJECT_MCP_POLICY),
            }
        )
    except Exception as error:
        return _active_project_error("index-unpacked-assets", error)


@mcp.tool()
def query_active_unpacked_assets(
    text: str = "",
    kinds: list[str] | None = None,
    sha256: str = "",
    asset_ids: list[str] | None = None,
    limit: int = 20,
    include_relationships: bool = True,
) -> str:
    """Query only the active project's prepared catalog using typed filters."""

    try:
        service, status, asset_root, database_path = _active_asset_catalog_context(
            require_catalog=True
        )
        workspace_id = str(status.get("workspaceId", "") or "")
        project_root = Path(str(status.get("projectRoot", "") or "")).resolve()
        with service.pin_active_project(workspace_id, project_root):
            result = AssetCatalogService(database_path).query(
                text=text,
                kinds=kinds or (),
                sha256=sha256,
                asset_ids=asset_ids or (),
                limit=limit,
                include_relationships=include_relationships,
            )
            if Path(str(result.get("rootPath", "") or "")).resolve() != asset_root:
                raise RuntimeError("asset catalog belongs to another configured root")
            _require_same_active_project(status)
        result["project"] = {
            "workspaceId": workspace_id,
            "projectRoot": str(project_root),
        }
        result["applied"] = False
        return _ok(result)
    except Exception as error:
        return _active_project_error("query-unpacked-assets", error)


SHADER_ASSET_RESOLUTIONS_FILENAME = "shader-asset-resolutions-v1.sqlite3"


def _shader_asset_memory(raw_sha256: str, stage: str, entry_point: str) -> dict[str, Any] | None:
    """Remembered asset resolutions + material-family keys for one shader.

    Returns None when the active project has no indexed asset root; never
    raises, because memory is optional evidence for decompile/rename paths.
    """

    try:
        service, status, asset_root, database_path = _active_asset_catalog_context(require_catalog=True)
    except Exception:
        return None
    try:
        game_key = _shader_asset_game_key(status, asset_root)
        store = shader_asset_retrieval.ShaderAssetResolutionStore(
            database_path.parent / SHADER_ASSET_RESOLUTIONS_FILENAME)
        catalog = AssetCatalogService(database_path)
        exact = store.lookup(game_key, str(raw_sha256).lower(), "", normalize_stage(stage), str(entry_point or ""))
        families: list[str] = []
        for resolution in exact.get("resolutions") or []:
            path = str(resolution.get("assetRelativePath") or "")
            if path and path not in families:
                families.append(path)
            try:
                found = catalog.query(text=PurePosixPath(path).name, limit=20, include_relationships=True)
            except Exception:
                found = {"assets": []}
            for asset in found.get("assets", []):
                if str(asset.get("relativePath")) != path:
                    continue
                for relation in asset.get("relationships") or []:
                    if relation.get("relation") == "parent-material" and relation.get("targetPath"):
                        parent = str(relation["targetPath"])
                        if parent not in families:
                            families.append(parent)
        return {"store": store, "gameKey": game_key, "catalog": catalog, "resolutions": exact.get("resolutions") or [],
                "familyKeys": families, "databasePath": database_path}
    except Exception:
        return None


def _shader_asset_game_key(status: Mapping[str, Any], asset_root: Path) -> str:
    scope = status.get("scope") if isinstance(status.get("scope"), Mapping) else {}
    game = str((scope or {}).get("gameId", "") or "").strip()
    build = str((scope or {}).get("buildId", "") or "").strip()
    if game or build:
        return "%s@%s" % (game or "game", build or "build")
    return "root:" + hashlib.sha256(os.path.normcase(str(asset_root)).encode("utf-8")).hexdigest()[:16]


def _shader_asset_capture_features(event_id: int, stage: str, *, pass_hint: str, mesh_hint: str,
                                   extra_tokens: Sequence[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Collect capture-side retrieval features for one event/stage shader."""

    context = _active_live_context(allow_missing_capture_id=True)
    selection = _active_capture_catalog_selection(int(event_id), stage)
    if selection is None:
        raise RuntimeError(
            "no active Shader catalog covers this event/stage; call "
            "get_active_shader_workspace_path(event_id, stage) first"
        )
    unused_exported, unused_path, selected, unused_binding = selection
    identity = {
        "captureId": selected.capture_id,
        "eventId": int(event_id),
        "stage": selected.key.stage,
        "entryPoint": selected.key.entry_point,
        "shaderResourceId": selected.key.resource_id,
        "rawSha256": selected.key.raw_sha256,
    }
    textures = ipc.call("get_bound_textures", {"event_id": int(event_id), "stage": selected.key.stage}, timeout=60.0)
    if not isinstance(textures, list):
        textures = []
    bound_names = []
    resource_names = []
    texture_ids: list[int] = []
    for entry in textures[:256]:
        if not isinstance(entry, Mapping):
            continue
        tex_name = str(entry.get("texName", "") or "")
        if tex_name:
            bound_names.append(tex_name)
        reflection_name = str(entry.get("name", "") or "")
        if reflection_name:
            resource_names.append(reflection_name)
        try:
            rid = int(entry.get("resourceId") or 0)
        except (TypeError, ValueError):
            rid = 0
        if rid > 0 and rid not in texture_ids and len(texture_ids) < 16:
            texture_ids.append(rid)
    # Content fingerprints: RenderDoc converts a small mip of each bound texture
    # to PNG (any GPU format), the Host hashes it. Names are not needed.
    fingerprints: list[dict[str, Any]] = []
    fingerprint_status: dict[str, Any] = {"status": "skipped", "reason": "no bound textures"}
    if texture_ids:
        try:
            import base64 as _base64

            from .symbols.imaging import decode_png, fingerprint as image_fingerprint

            snapshots = ipc.call("get_texture_fingerprints", {"event_id": int(event_id), "resource_ids": texture_ids}, timeout=120.0)
            fingerprint_status = {"status": "ok", "requested": len(texture_ids), "available": 0}
            for snapshot in (snapshots.get("textures") if isinstance(snapshots, Mapping) else []) or []:
                if not isinstance(snapshot, Mapping):
                    continue
                item: dict[str, Any] = {"resourceId": snapshot.get("resourceId"), "name": snapshot.get("name"),
                                        "width": snapshot.get("width"), "height": snapshot.get("height"),
                                        "format": snapshot.get("format"), "available": False}
                if not snapshot.get("available"):
                    item["reason"] = snapshot.get("reason") or "no snapshot"
                    fingerprints.append(item)
                    continue
                try:
                    rgba, width, height = decode_png(_base64.b64decode(str(snapshot.get("pngBase64") or "")))
                    computed = image_fingerprint(rgba, width, height)
                    item.update({"available": True, "thumbnail": computed["thumbnail"], "dhash": computed["dhash"],
                                 "meanColor": computed["meanColor"], "flat": computed["flat"],
                                 "snapshot": {"mip": snapshot.get("mip"), "width": width, "height": height}})
                    fingerprint_status["available"] += 1
                except Exception as error:
                    item["reason"] = "snapshot undecodable: %s" % str(error)[:160]
                fingerprints.append(item)
        except Exception as error:
            fingerprint_status = {"status": "error", "reason": str(error)[:300]}
    # Raw constant-buffer lanes: distinctive floats become value evidence
    # (a complex number shared with an asset's parameter data is near-identity).
    lanes: list[dict[str, Any]] = []
    cbuffer_status: dict[str, Any] = {"status": "unavailable"}
    try:
        from .symbols.engines.unreal_package import is_distinctive_value

        values = ipc.call("get_constant_buffer_values", {"event_id": int(event_id), "stage": selected.key.stage}, timeout=60.0)
        if isinstance(values, Mapping):
            blocks = values.get("constantBuffers") or []
            cbuffer_status = {
                "status": str(values.get("status") or "unknown"),
                "blocks": [{"bind": block.get("bind"), "space": block.get("space"), "byteSize": block.get("byteSize"),
                            "status": block.get("status"), "reason": block.get("reason")} for block in blocks[:32]],
            }
            for block in blocks:
                for index, value in enumerate(block.get("floats") or []):
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and is_distinctive_value(float(value)):
                        lanes.append({"bind": block.get("bind"), "space": block.get("space"), "lane": index, "value": float(value)})
                        if len(lanes) >= 1024:
                            break
                if len(lanes) >= 1024:
                    break
    except Exception as error:
        cbuffer_status = {"status": "error", "reason": str(error)[:300]}
    # Draw geometry: counts/layout survive even when every name is stripped.
    geometry: dict[str, Any] = {"available": False}
    try:
        geometry_result = ipc.call("get_draw_geometry", {"event_id": int(event_id)}, timeout=60.0)
        if isinstance(geometry_result, Mapping):
            geometry = {key: geometry_result.get(key) for key in (
                "available", "indexCount", "vertexCount", "instanceCount", "topology", "vertexInputs",
                "vertexBuffers", "indexBuffer", "meshHint")}
    except Exception as error:
        geometry = {"available": False, "reason": str(error)[:200]}
    if not mesh_hint and geometry.get("meshHint") in ("skeletal", "static", "instanced"):
        mesh_hint = str(geometry["meshHint"])
    # Code logic hints from the workspace source when one already exists.
    code_hints: dict[str, Any] = {}
    try:
        workspace_status = None
        project = _active_project_service()
        project_status = project.status()
        binding_now = _active_capture_catalog_binding()
        if binding_now is not None:
            catalog_sha = str(binding_now[2].get("catalogSha256") or "")
            workspace_dir = project.shader_workspace_path(
                capture_id=selected.capture_id, catalog_sha256=catalog_sha,
                route_binding_sha256=_active_shader_route_binding_sha256(), stage=selected.key.stage,
                shader_resource_id=selected.key.resource_id, raw_sha256=selected.key.raw_sha256, create=False)
            if workspace_dir and Path(str(workspace_dir)).is_dir():
                workspace_status = _shader_workspace_service().status(str(workspace_dir))
        source_path = (workspace_status or {}).get("source", {}).get("path") if workspace_status else None
        if source_path and Path(str(source_path)).is_file():
            text = Path(str(source_path)).read_text("utf-8", errors="replace")[:2 * 1024 * 1024]
            code_hints = {
                "usesDiscard": bool(re.search(r"\bdiscard\b|\bclip\s*\(", text)),
                "textureSampleCount": len(re.findall(r"\.(?:Sample|SampleLevel|SampleGrad|SampleBias|SampleCmp|Load|Gather\w*)\s*\(", text)),
                "usesDerivatives": bool(re.search(r"\bdd[xy](?:_fine|_coarse)?\s*\(", text)),
                "outputTargetCount": len(set(re.findall(r"SV_Target(\d*)", text))),
                "sourceLines": text.count("\n") + 1,
            }
    except Exception as error:
        code_hints = {"unavailable": str(error)[:160]}
    abi = getattr(selected, "abi", None)
    block_sizes: list[int] = []
    sampler_count = uav_count = 0
    if abi is not None:
        try:
            block_sizes = [int(block.byte_size) for block in abi.constant_blocks]
            sampler_count = len(abi.samplers)
            uav_count = len(abi.read_write_resources)
        except Exception:
            block_sizes = []
    features = {
        "stage": selected.key.stage,
        "entryPoint": selected.key.entry_point,
        "boundTextureNames": bound_names,
        "resourceNames": resource_names,
        "textureSlotCount": len(textures),
        "constantBlockSizes": block_sizes,
        "passHint": str(pass_hint or "")[:64],
        "meshHint": str(mesh_hint or "")[:64],
        "extraTokens": [str(item)[:64] for item in extra_tokens][:32],
        "constantBufferLanes": lanes,
        "constantBufferValueStatus": cbuffer_status,
        "boundTextureFingerprints": fingerprints,
        "textureFingerprintStatus": fingerprint_status,
        "drawGeometry": geometry,
        "codeHints": code_hints,
    }
    features["structuralKey"] = shader_asset_retrieval.structural_key(
        selected.key.stage, block_sizes, len(textures), sampler_count, uav_count, selected.key.entry_point
    )
    _require_same_active_capture(context, allow_missing_capture_id=True)
    return identity, features


ASSET_INDEX_FOREGROUND_BUDGET_SECONDS = 25.0
# A checkpointed catalog is already usable: wait only briefly for more.
ASSET_INDEX_PARTIAL_FOREGROUND_BUDGET_SECONDS = 5.0


class _AssetCatalogNotReady(RuntimeError):
    """Raised while the on-demand index is still being built."""

    def __init__(self, receipt: Mapping[str, Any]):
        super().__init__("asset catalog is being indexed")
        self.receipt = dict(receipt)


def _asset_index_job(database_path: Path, asset_root: Path, build_id: str, depth: str = "headers") -> dict[str, Any]:
    """Start (or join) the background scan, preferring qrenderdoc's persistent service.

    ``ReplayLabInternal`` outlives this task-scoped process, so the scan keeps
    running after the task ends and later tasks simply join it.  When that
    service is unreachable (source runs, tests, old runtime) the scan runs in
    this process; its checkpoints still make the next attempt resume.
    """

    from . import asset_index_client
    from .asset_index_jobs import ensure_index_job

    try:
        result = asset_index_client.request("ensure", depth=depth)
        job = dict(result.get("job") or {})
        job.setdefault("owner", "ReplayLabInternal")
        return job
    except asset_index_client.InternalAssetIndexUnavailable as exc:
        fallback_reason = str(exc)
    job = ensure_index_job(database_path, asset_root, build_id, depth=depth, owner="task-process")
    job["fallbackReason"] = fallback_reason
    return job


def _asset_index_status(database_path: Path) -> dict[str, Any] | None:
    from . import asset_index_client
    from .asset_index_jobs import index_job_status

    try:
        result = asset_index_client.request("status")
        job = result.get("job")
        return dict(job) if isinstance(job, Mapping) else None
    except asset_index_client.InternalAssetIndexUnavailable:
        return index_job_status(database_path)
    except Exception:
        return index_job_status(database_path)


def _ensure_shader_asset_catalog() -> tuple[Any, dict[str, Any], Path, Path, dict[str, Any]]:
    """Index the asset root on first need (header depth); never up front.

    A short scan finishes inside this call.  A long one keeps running in the
    background (in ReplayLabInternal when available) and the caller receives
    an "indexing" receipt.  A checkpointed partial catalog is returned as
    usable evidence with ``partial: true`` while the scan continues.
    """

    service, status, asset_root, database_path = _active_asset_catalog_context(
        require_catalog=False, create_dictionary=True)
    receipt: dict[str, Any] = {"action": "reused", "depth": None, "seconds": 0.0}
    needs_scan = database_path.is_symlink() or not database_path.is_file()
    partial = False
    if not needs_scan:
        try:
            current = AssetCatalogService(database_path).status()
            if current.get("tokenIndex") not in {"fts5", "plain"} or current.get("engine") in (None, ""):
                needs_scan = True
                receipt["reason"] = "catalog predates retrieval tokens/engine parsing"
            elif current.get("complete") is False:
                partial = True
                receipt["reason"] = "catalog is a checkpoint of an unfinished scan"
                receipt["checkpoint"] = dict(current.get("progress") or {})
                receipt["assetCount"] = current.get("assetCount")
        except Exception as error:
            needs_scan = True
            receipt["reason"] = "catalog unreadable: %s" % str(error)[:200]
    if needs_scan or partial:
        build_id = str(status.get("scope", {}).get("buildId", "") or "")
        job = _asset_index_job(database_path, asset_root, build_id)
        budget = ASSET_INDEX_FOREGROUND_BUDGET_SECONDS if needs_scan else ASSET_INDEX_PARTIAL_FOREGROUND_BUDGET_SECONDS
        started = time.monotonic()
        deadline = started + budget
        while job.get("status") == "running" and time.monotonic() < deadline:
            time.sleep(1.0)
            job = _asset_index_status(database_path) or job
        elapsed = round(float(job.get("elapsedSeconds") or (time.monotonic() - started)), 1)
        if job.get("status") == "running":
            indexing = {
                "action": "indexing-in-background", "status": "running", "depth": "headers",
                "owner": job.get("owner"), "elapsedSeconds": elapsed,
                "progress": dict(job.get("progress") or {}), "retryAfterSeconds": 30,
            }
            if job.get("fallbackReason"):
                indexing["fallbackReason"] = job["fallbackReason"]
            if not partial:
                raise _AssetCatalogNotReady(indexing)
            receipt.update({"action": "partial-catalog-while-indexing", "partial": True, "indexing": indexing})
            return service, status, asset_root, database_path, receipt
        if job.get("status") == "failed":
            if partial:
                receipt.update({"action": "partial-catalog-after-failed-scan", "partial": True,
                                "indexing": {"status": "failed", "error": job.get("error")}})
                return service, status, asset_root, database_path, receipt
            raise RuntimeError("on-demand asset indexing failed: %s" % job.get("error"))
        result = job.get("result") or {}
        receipt.update({
            "action": "indexed-on-demand", "depth": "headers", "seconds": elapsed,
            "owner": job.get("owner"),
            "assetCount": result.get("assetCount"), "deferredCount": result.get("deferredCount"),
            "engine": (result.get("engine") or {}).get("engine"),
        })
    return service, status, asset_root, database_path, receipt


def _shader_asset_debug(features: Mapping[str, Any], retrieval: Mapping[str, Any]) -> dict[str, Any]:
    """Per-channel debug for the Agent: what was available, what was not, what to try."""

    channels: dict[str, Any] = {}
    next_steps: list[str] = []
    names = [item for item in features.get("boundTextureNames") or [] if item]
    channels["names"] = {"boundTextureNames": len(names), "note": "reference only; stripped in most shipped captures"}
    if not names:
        next_steps.append("no texture debug names in this capture: rely on pixel identity, cbuffer values and structure, not names")
    fp_status = features.get("textureFingerprintStatus") or {}
    fingerprints = features.get("boundTextureFingerprints") or []
    channels["texturePixels"] = {"status": fp_status.get("status"), "snapshots": sum(1 for f in fingerprints if f.get("available")),
                                 "failed": [{"texture": f.get("name") or f.get("resourceId"), "reason": f.get("reason")}
                                            for f in fingerprints if not f.get("available")][:8]}
    identity = (retrieval.get("signals") or {}).get("textureIdentity") or []
    unmatched = [item for item in identity if not item.get("matches")]
    if fingerprints and unmatched and len(unmatched) == len(identity):
        next_steps.append("no bound texture matched an indexed fingerprint: textures may be in bulk files not indexed, "
                          "generated at runtime, or in an unsupported format — check coverage.limitations")
    cb = features.get("constantBufferValueStatus") or {}
    lanes = features.get("constantBufferLanes") or []
    channels["cbufferValues"] = {"status": cb.get("status"), "distinctiveLanes": len(lanes),
                                 "blocks": cb.get("blocks")}
    if not lanes:
        next_steps.append("no distinctive constant-buffer values were readable (unbound blocks or round numbers only): "
                          "value matching is inactive for this draw")
    geometry = features.get("drawGeometry") or {}
    channels["geometry"] = {"available": geometry.get("available"), "meshHint": geometry.get("meshHint"),
                            "indexCount": geometry.get("indexCount"), "vertexInputs": len(geometry.get("vertexInputs") or [])}
    channels["codeHints"] = features.get("codeHints") or {}
    candidates = retrieval.get("candidates") or []
    if not candidates:
        next_steps.append("no candidate from any Host channel: search the catalog yourself (query_active_unpacked_assets with "
                          "shader-derived tokens, kinds=package/material) and read the closest documents")
    elif retrieval.get("decisionHint", "").startswith("ambiguous"):
        next_steps.append("candidates are close: compare parent chains, usedByMeshes against the draw geometry, and the "
                          "blend-mode/sample-count checks; reject the wrong ones so memory improves")
    if isinstance(features.get("codeHints"), Mapping) and features["codeHints"].get("unavailable"):
        next_steps.append("code hints unavailable before the workspace exists: call again after prepare_shader_workspace for blend-mode/sample checks")
    return {"channels": channels, "nextSteps": next_steps}


@mcp.tool()
def retrieve_shader_asset_candidates(
    event_id: int,
    stage: str = "fragment",
    top_k: int = 8,
    pass_hint: str = "",
    mesh_hint: str = "",
    extra_tokens: list[str] | None = None,
    force_search: bool = False,
) -> str:
    """Find the unpacked asset (material / material instance / shader source) behind a captured shader.

    Retrieval, not hash verification: the bound textures' debug names, the
    shader's own resource names, the stage, and optional pass/mesh hints are
    turned into weighted tokens and ranked against the active project's asset
    catalog (any engine the catalog indexed; Unreal packages contribute class,
    referenced textures, parent material, shader types and defines). Each
    candidate returns a compact document, the matched tokens, and consistency
    checks so the decision can be made — and explained — from evidence. A
    remembered decision for this shader (or a same-shape suggestion) is
    returned first; ``force_search`` re-runs retrieval anyway. Submit the
    decision with ``submit_shader_asset_resolution`` so it is not searched
    again for this game build. Nothing here mutates replay state.
    """

    try:
        try:
            service, status, asset_root, database_path, indexing = _ensure_shader_asset_catalog()
        except _AssetCatalogNotReady as pending:
            return _ok({
                "schema": "replaylab-shader-asset-candidates/v1", "schemaVersion": 1, "ready": False,
                "indexing": pending.receipt,
                "reason": "the unpacked-asset catalog is still being built on first use",
                "next": ("continue with prepare_shader_workspace / compile now and call "
                         "retrieve_shader_asset_candidates again before naming (retryAfterSeconds)"),
                "applied": False,
            })
        except Exception as error:
            # No configured/available asset root is a normal state, not a
            # failure: report it so the Agent continues without asset evidence.
            return _ok({
                "schema": "replaylab-shader-asset-candidates/v1", "schemaVersion": 1, "ready": False,
                "reason": str(error)[:300],
                "next": ("continue without unpacked-asset evidence; configure_active_project_asset_root enables "
                         "retrieval later"),
                "applied": False,
            })
        identity, features = _shader_asset_capture_features(
            int(event_id), stage, pass_hint=pass_hint, mesh_hint=mesh_hint, extra_tokens=extra_tokens or [])
        catalog = AssetCatalogService(database_path)
        game_key = _shader_asset_game_key(status, asset_root)
        store = shader_asset_retrieval.ShaderAssetResolutionStore(
            database_path.parent / SHADER_ASSET_RESOLUTIONS_FILENAME)
        memory = store.lookup(game_key, identity["rawSha256"], features["structuralKey"],
                              identity["stage"], identity["entryPoint"])
        result: dict[str, Any] = {
            "schema": "replaylab-shader-asset-candidates/v1",
            "schemaVersion": 1,
            "gameKey": game_key,
            "identity": identity,
            "features": features,
            "memory": memory,
            "indexing": indexing,
            "retrieval": None,
        }
        if indexing.get("partial"):
            result["partialCatalog"] = {
                "note": ("candidates come from a checkpoint of an unfinished scan (%s assets so far); "
                         "usable now, retry later for full coverage" % indexing.get("assetCount")),
                "indexing": indexing.get("indexing"),
            }
        if memory.get("resolved") and not force_search:
            resolution = memory["resolution"]
            try:
                found = catalog.query(text=PurePosixPath(str(resolution["assetRelativePath"])).name,
                                      limit=20, include_relationships=False)
                asset = next((item for item in found.get("assets", [])
                              if str(item.get("relativePath")) == resolution["assetRelativePath"]), None)
            except Exception:
                asset = None
            result["resolvedDocument"] = (
                shader_asset_retrieval._material_document(catalog, asset) if asset else None)
            result["resolvedAssetStillPresent"] = asset is not None
            result["next"] = "use resolvedDocument names/textures/shader types as reference; no search needed"
        else:
            retrieval = shader_asset_retrieval.retrieve(catalog, features, top_k=top_k)
            # Hydrate candidates whose cooked export data was deferred so the
            # document carries shader types/defines before the decision.
            pending = [
                PurePosixPath(str(item["relativePath"])).with_suffix(".uexp").as_posix()
                for item in retrieval.get("candidates", [])
                if str(item.get("relativePath", "")).endswith((".uasset", ".umap"))
                and item.get("document", {}).get("exportParseStatus") == "deferred"
            ]
            if pending:
                try:
                    started = time.monotonic()
                    catalog.hydrate(asset_root, pending[:16])
                    catalog = AssetCatalogService(database_path)
                    retrieval = shader_asset_retrieval.retrieve(catalog, features, top_k=top_k)
                    indexing["hydrated"] = {"paths": pending[:16], "seconds": round(time.monotonic() - started, 1)}
                except Exception as error:
                    indexing["hydrated"] = {"error": str(error)[:200]}
            # Structure memory: shaders with the same cbuffer sizes / texture
            # counts that were already resolved point at their material family.
            suggestions = memory.get("sameShapeSuggestions") or []
            if suggestions:
                by_path = {str(item.get("assetRelativePath")): item for item in suggestions}
                for candidate in retrieval.get("candidates", []):
                    hit = by_path.get(str(candidate.get("relativePath")))
                    if hit is not None:
                        candidate["facets"]["structure"] = {
                            "sameShapeAsShader": hit.get("shaderRawSha256"), "confidence": hit.get("confidence"),
                            "note": "a shader with identical cbuffer sizes/texture counts was resolved to this asset before"}
                        candidate["adjustedScore"] = round(float(candidate.get("adjustedScore") or 0) + 4.0, 4)
                        candidate["confidence"] = round(min(0.95, max(float(candidate.get("confidence") or 0), 0.5 + 0.4 * float(hit.get("confidence") or 0))), 3)
                        candidate["why"] += "; same structural shape as a previously resolved shader of this family"
                retrieval["candidates"].sort(key=lambda item: -float(item.get("adjustedScore") or 0))
                retrieval["structureSuggestions"] = [
                    {"assetRelativePath": item.get("assetRelativePath"), "confidence": item.get("confidence"),
                     "fromShader": item.get("shaderRawSha256"), "decidedBy": item.get("decidedBy")} for item in suggestions[:5]]
            retrieval["debug"] = _shader_asset_debug(features, retrieval)
            result["retrieval"] = retrieval
            result["next"] = ("decide with the evidence, then submit_shader_asset_resolution(event_id, stage, "
                              "asset_id, confidence, rationale); reject a wrong leader with reject=true")
        result["applied"] = False
        return _ok(result)
    except Exception as error:
        return _active_project_error("retrieve-shader-asset-candidates", error)


@mcp.tool()
def submit_shader_asset_resolution(
    event_id: int,
    stage: str,
    asset_id: str,
    confidence: float,
    rationale: str,
    reject: bool = False,
    facets_json: str = "",
) -> str:
    """Remember which unpacked asset a captured shader comes from (or does not).

    Records the decision for this game build keyed by the shader's raw hash
    and by its structural shape, so later captures and same-shape shaders
    reuse it instead of searching. ``reject=true`` stores a negative entry
    for a wrong candidate. Submit more than one positive when several assets
    explain the shader (instance + parent material + shared function), each
    with its own confidence; ``facets_json`` may carry per-facet confidences
    (e.g. {"textures":0.9,"parameters":0.6}) so later use knows which part
    of the match to trust. Decisions are evidence-labelled memory, never ABI
    authority: captured reflection still decides every physical fact.
    """

    try:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
            raise ValueError("confidence must be a number between 0 and 1")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError("rationale must state the evidence behind the decision")
        facets: dict[str, Any] = {}
        if facets_json:
            parsed = json.loads(facets_json)
            if not isinstance(parsed, dict) or len(parsed) > 32:
                raise ValueError("facets_json must be a small JSON object")
            for key, value in parsed.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
                    raise ValueError("facet %r confidence must be between 0 and 1" % str(key)[:32])
                facets[str(key)[:64]] = float(value)
        service, status, asset_root, database_path = _active_asset_catalog_context(require_catalog=True)
        identity, features = _shader_asset_capture_features(int(event_id), stage, pass_hint="", mesh_hint="", extra_tokens=[])
        catalog = AssetCatalogService(database_path)
        found = catalog.query(asset_ids=[str(asset_id)], limit=1, include_relationships=False)
        assets = found.get("assets", [])
        if not assets:
            raise ValueError("asset_id is not present in the active asset catalog")
        asset = assets[0]
        store = shader_asset_retrieval.ShaderAssetResolutionStore(
            database_path.parent / SHADER_ASSET_RESOLUTIONS_FILENAME)
        record = store.record(
            game_key=_shader_asset_game_key(status, asset_root),
            shader_raw_sha256=identity["rawSha256"],
            structural=features["structuralKey"],
            stage=identity["stage"],
            entry_point=identity["entryPoint"],
            asset_relative_path=str(asset["relativePath"]),
            asset_content_sha256=str(asset.get("contentSha256") or ""),
            asset_class=str((asset.get("metadata") or {}).get("className", "") or ""),
            catalog_sha256=str(found.get("catalogSha256") or ""),
            negative=bool(reject),
            confidence=float(confidence),
            decided_by="ai",
            rationale=rationale,
            evidence={"boundTextureNames": features["boundTextureNames"], "eventId": int(event_id),
                      "facets": facets},
        )
        return _ok({
            "schema": "replaylab-shader-asset-resolution-receipt/v1",
            "schemaVersion": 1,
            "identity": identity,
            "resolution": record,
            "document": shader_asset_retrieval._material_document(catalog, asset),
            "applied": False,
        })
    except Exception as error:
        return _active_project_error("submit-shader-asset-resolution", error)


@mcp.tool()
def forget_shader_asset_resolution(event_id: int, stage: str = "fragment") -> str:
    """Drop the remembered asset decision(s) for one captured shader."""

    try:
        service, status, asset_root, database_path = _active_asset_catalog_context(require_catalog=True)
        identity, unused_features = _shader_asset_capture_features(int(event_id), stage, pass_hint="", mesh_hint="", extra_tokens=[])
        store = shader_asset_retrieval.ShaderAssetResolutionStore(
            database_path.parent / SHADER_ASSET_RESOLUTIONS_FILENAME)
        removed = store.forget(_shader_asset_game_key(status, asset_root), identity["rawSha256"],
                               identity["stage"], identity["entryPoint"])
        return _ok({"schema": "replaylab-shader-asset-resolution-forget/v1", "schemaVersion": 1,
                    "identity": identity, "removed": removed, "applied": False})
    except Exception as error:
        return _active_project_error("forget-shader-asset-resolution", error)


@mcp.tool()
def resolve_active_unpacked_asset(
    asset_id: str,
    expected_catalog_sha256: str,
) -> str:
    """Deliver one selected catalog asset after a fresh content-hash check.

    No caller path is accepted.  The absolute path in the receipt is evidence
    for a trusted consumer and is not authorization to execute asset content.
    """

    try:
        service, status, asset_root, database_path = _active_asset_catalog_context(
            require_catalog=True
        )
        workspace_id = str(status.get("workspaceId", "") or "")
        project_root = Path(str(status.get("projectRoot", "") or "")).resolve()
        with service.pin_active_project(workspace_id, project_root):
            result = AssetCatalogService(database_path).resolve(
                asset_root,
                asset_id=asset_id,
                expected_catalog_sha256=expected_catalog_sha256,
            )
            _require_same_active_project(status)
        result["project"] = {
            "workspaceId": workspace_id,
            "projectRoot": str(project_root),
        }
        result["applied"] = False
        return _ok(result)
    except Exception as error:
        return _active_project_error("resolve-unpacked-asset", error)


def _read_catalog_json_delivery(
    delivery: Mapping[str, Any], *, purpose: str
) -> tuple[dict[str, Any], Path]:
    """Read a freshly hash-verified JSON delivery selected by catalog identity."""

    if delivery.get("verified") is not True or delivery.get("format") != "json":
        raise RuntimeError("%s must resolve to a verified JSON asset" % purpose)
    size = int(delivery.get("size", 0) or 0)
    if not 1 <= size <= 32 * 1024 * 1024:
        raise RuntimeError("%s is outside the fixed JSON byte bound" % purpose)
    path = Path(str(delivery.get("absolutePath", "") or ""))
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("%s is not valid UTF-8 JSON" % purpose) from error
    if len(payload) != size or not isinstance(value, dict):
        raise RuntimeError("%s changed or is not a JSON object" % purpose)
    if hashlib.sha256(payload).hexdigest() != delivery.get("contentSha256"):
        raise RuntimeError("%s changed after catalog delivery" % purpose)
    return value, path


def _validate_strong_inventory_evidence(
    value: Mapping[str, Any], *, inventory_id: int, profile: str
) -> None:
    """Require explicit, machine-readable Strong/complete Inventory evidence."""

    candidate: Mapping[str, Any] = value
    for key in ("inventory", "selectedInventory", "summary"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            candidate = nested
            break
    metadata = candidate.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}

    selected_id = candidate.get("inventoryId", candidate.get("id"))
    if isinstance(selected_id, bool) or int(selected_id or 0) != inventory_id:
        raise RuntimeError("Inventory evidence does not identify the requested concrete id")
    confidence = str(
        candidate.get(
            "discoveryConfidence",
            candidate.get("confidence", metadata.get("discoveryConfidence", "")),
        )
        or ""
    )
    if confidence.casefold() != "strong":
        raise RuntimeError("Inventory evidence is not Strong")
    truncated = candidate.get("truncated", metadata.get("truncated"))
    if truncated is not False:
        raise RuntimeError("Inventory evidence must explicitly state truncated=false")
    complete = candidate.get("complete", metadata.get("complete"))
    if complete is not True:
        raise RuntimeError("Inventory evidence must explicitly state complete=true")
    evidence_profile = str(candidate.get("profile", metadata.get("profile", "")) or "")
    if evidence_profile and evidence_profile != profile:
        raise RuntimeError("Inventory evidence belongs to another UEX profile")


def _validate_asset_report_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    """Use the same bounded canonical UEX v1 gate as the job manager."""

    return normalize_asset_report_spec(value)


@mcp.tool()
def get_unpacked_asset_provider_capabilities() -> str:
    """Report registered UEX/report/Blender providers and fixed route gaps."""

    try:
        result = _UNPACKED_ASSET_JOB_MANAGER.capabilities()
        result["decisionDiscovery"] = _UNPACKED_ASSET_JOB_MANAGER.discover_uex_decisions(
            inventory_limit=20
        )
        result["applied"] = False
        return _ok(result)
    except Exception as error:
        return _err(error)


@mcp.tool()
def get_unpacked_asset_provider_decisions(
    profile: str = "", inventory_limit: int = 20
) -> str:
    """List safe UEX profile names and committed Inventory quality decisions.

    The Host reads only its process-owned profiles registration and whitelisted
    Inventory columns. It never returns the profiles file, analysis paths, pak
    roots, AES values, or arbitrary metadata.
    """

    try:
        result = _UNPACKED_ASSET_JOB_MANAGER.discover_uex_decisions(
            profile=profile, inventory_limit=inventory_limit
        )
        result["applied"] = False
        return _ok(result)
    except Exception as error:
        return _err(error)


@mcp.tool()
def query_unpacked_asset_inventory_assets(
    profile: str,
    inventory_id: int,
    kind: str = "",
    search: str = "",
    offset: int = 0,
    limit: int = 50,
) -> str:
    """Query bounded safe identities from one exact Host-owned UEX Inventory.

    Large provider detail JSON and local paths are omitted. ``exportAssetPath``
    is the validated package-shaped value accepted by the managed Blender job.
    """

    try:
        result = _UNPACKED_ASSET_JOB_MANAGER.query_uex_inventory_assets(
            profile=profile,
            inventory_id=inventory_id,
            kind=kind,
            search=search,
            offset=offset,
            limit=limit,
        )
        result["applied"] = False
        return _ok(result)
    except Exception as error:
        return _err(error)


@mcp.tool()
def start_unpacked_asset_report(
    operation_id: str,
    profile: str,
    inventory_id: int,
    inventory_evidence_asset_id: str = "",
    spec_asset_id: str = "",
    expected_catalog_sha256: str = "",
    allow_partial: bool = False,
    report_spec: dict[str, Any] | None = None,
) -> str:
    """Start one managed UEX report from bounded project-specific grouping rules.

    Executable, profile-database, source-root, SQL, and output paths are fixed by
    the Host and cannot be supplied by the caller. ``report_spec`` is canonicalized
    and staged under the operation; the legacy catalog asset form remains
    compatible. Host-owned UEX Inventory data is the Strong/complete authority.
    """

    try:
        inline_requested = report_spec is not None
        legacy_values = (
            inventory_evidence_asset_id,
            spec_asset_id,
            expected_catalog_sha256,
        )
        legacy_requested = any(value != "" for value in legacy_values)
        if inline_requested and legacy_requested:
            raise RuntimeError(
                "choose either inline report_spec or legacy catalog report inputs"
            )
        if legacy_requested and any(
            not isinstance(value, str) or not value for value in legacy_values
        ):
            raise RuntimeError("legacy catalog report inputs must be complete")
        if not inline_requested and not legacy_requested:
            raise RuntimeError(
                "project-specific report_spec grouping rules are required; yield for a decision"
            )

        service = _active_project_service()
        status = service.status()
        if status.get("scopeConfigured") is not True:
            raise RuntimeError(
                "active project game/build scope must be configured before UEX reporting"
            )
        workspace_id = str(status.get("workspaceId", "") or "")
        project_root = Path(str(status.get("projectRoot", "") or "")).resolve()
        if project_root.is_symlink() or not project_root.is_dir():
            raise RuntimeError("active project root is redirected or unavailable")
        project_binding_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "schema": "replaylab-unpacked-report-project-binding/v1",
                    "workspaceId": workspace_id,
                    "scope": status.get("scope"),
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

        with service.pin_active_project(workspace_id, project_root):
            inventory_evidence_sha256 = ""
            normalized_spec: dict[str, Any]
            if inline_requested:
                if not isinstance(report_spec, Mapping):
                    raise RuntimeError("report_spec must be an object")
                normalized_spec = _validate_asset_report_spec(report_spec)
            else:
                service, status, asset_root, database_path = (
                    _active_asset_catalog_context(require_catalog=True)
                )
                catalog = AssetCatalogService(database_path)
                evidence_delivery = catalog.resolve(
                    asset_root,
                    asset_id=inventory_evidence_asset_id,
                    expected_catalog_sha256=expected_catalog_sha256,
                )
                spec_delivery = catalog.resolve(
                    asset_root,
                    asset_id=spec_asset_id,
                    expected_catalog_sha256=expected_catalog_sha256,
                )
                evidence, unused_evidence_path = _read_catalog_json_delivery(
                    evidence_delivery, purpose="supplemental Strong Inventory evidence"
                )
                spec, unused_spec_path = _read_catalog_json_delivery(
                    spec_delivery, purpose="UEX asset-report spec"
                )
                _validate_strong_inventory_evidence(
                    evidence, inventory_id=inventory_id, profile=profile
                )
                normalized_spec = _validate_asset_report_spec(spec)
                inventory_evidence_sha256 = str(evidence_delivery["contentSha256"])
            _require_same_active_project(status)
            result = _UNPACKED_ASSET_JOB_MANAGER.start_report(
                operation_id=operation_id,
                profile=profile,
                inventory_id=inventory_id,
                inventory_evidence_asset_id=inventory_evidence_asset_id,
                inventory_evidence_sha256=inventory_evidence_sha256,
                spec_path=None,
                spec_asset_id=spec_asset_id,
                catalog_sha256=expected_catalog_sha256,
                allow_partial=allow_partial,
                report_spec=normalized_spec,
                project_binding_sha256=project_binding_sha256,
            )
        result["project"] = {
            "workspaceId": workspace_id,
            "projectRoot": str(project_root),
            "catalogSha256": expected_catalog_sha256 if legacy_requested else "",
            "bindingSha256": project_binding_sha256,
        }
        result["applied"] = True
        return _ok(result)
    except Exception as error:
        return _active_project_error("start-unpacked-asset-report", error)


@mcp.tool()
def start_unpacked_asset_blender_export(
    operation_id: str,
    profile: str,
    inventory_id: int,
    expected_inventory_decision_sha256: str,
    virtual_assets: list[str],
    lod0_only: bool = False,
    export_morph_targets: bool = True,
    prefer_uemodel: bool = True,
    export_only: bool = False,
) -> str:
    """Start one Inventory-decision-bound exact-SkeletalMesh Blender job."""

    try:
        status = _active_project_service().status()
        if status.get("scopeConfigured") is not True:
            raise RuntimeError("active project scope must be configured before UEX export")
        workspace_id = str(status.get("workspaceId", "") or "")
        project_root = Path(str(status.get("projectRoot", "") or "")).resolve()
        with _active_project_service().pin_active_project(workspace_id, project_root):
            result = _UNPACKED_ASSET_JOB_MANAGER.start_blender_export(
                operation_id=operation_id,
                profile=profile,
                inventory_id=inventory_id,
                expected_inventory_decision_sha256=(
                    expected_inventory_decision_sha256
                ),
                virtual_assets=virtual_assets,
                lod0_only=lod0_only,
                export_morph_targets=export_morph_targets,
                prefer_uemodel=prefer_uemodel,
                export_only=export_only,
            )
            _require_same_active_project(status)
        result["project"] = {
            "workspaceId": workspace_id,
            "projectRoot": str(project_root),
        }
        result["applied"] = True
        return _ok(result)
    except Exception as error:
        return _active_project_error("start-unpacked-asset-blender-export", error)


@mcp.tool()
def continue_unpacked_asset_blender_export(
    operation_id: str,
    parent_operation_id: str,
    expected_parent_request_sha256: str,
    expected_parent_result_sha256: str,
) -> str:
    """Continue one partial Blender job without caller-supplied artifact paths.

    The Host revalidates and retains every accepted parent bundle, derives only
    failed/open assets for retry, and (when requested by the parent) assembles
    and validates one shared `.blend`. The two parent hashes bind the exact
    immutable receipt; all provider and artifact paths remain Host-owned.
    """

    try:
        status = _active_project_service().status()
        if status.get("scopeConfigured") is not True:
            raise RuntimeError(
                "active project scope must be configured before UEX continuation"
            )
        workspace_id = str(status.get("workspaceId", "") or "")
        project_root = Path(str(status.get("projectRoot", "") or "")).resolve()
        with _active_project_service().pin_active_project(workspace_id, project_root):
            result = _UNPACKED_ASSET_JOB_MANAGER.start_blender_continuation(
                operation_id=operation_id,
                parent_operation_id=parent_operation_id,
                expected_parent_request_sha256=expected_parent_request_sha256,
                expected_parent_result_sha256=expected_parent_result_sha256,
            )
            _require_same_active_project(status)
        result["project"] = {
            "workspaceId": workspace_id,
            "projectRoot": str(project_root),
        }
        result["applied"] = True
        return _ok(result)
    except Exception as error:
        return _active_project_error(
            "continue-unpacked-asset-blender-export", error
        )


@mcp.tool()
def get_unpacked_asset_job_status(operation_id: str) -> str:
    """Poll one Host-managed UEX report or Blender export operation."""

    try:
        result = _UNPACKED_ASSET_JOB_MANAGER.status(operation_id)
        result["applied"] = False
        return _ok(result)
    except Exception as error:
        return _err(error)


@mcp.tool()
def activate_active_replay_project(project_root: str) -> str:
    """Activate an existing project strictly inside the fixed projects home."""

    try:
        raw = Path(project_root).expanduser()
        if not raw.is_absolute():
            raise ValueError("project_root must be an absolute path")
        status = _active_project_service().activate(raw)
        return _ok(
            {
                "schema": "replaylab-active-project-mcp/v1",
                "schemaVersion": 1,
                "operation": "activate",
                "project": status,
                "applied": False,
                "policy": dict(_ACTIVE_PROJECT_MCP_POLICY),
            }
        )
    except Exception as error:
        return _active_project_error("activate", error)


def _emit_active_capture_prepare_progress(
    callback: Any,
    phase: str,
    message: str,
    *,
    current: int = 0,
    total: int = 0,
    item: str = "",
    event_id: int = 0,
) -> None:
    """Forward one bounded advisory preparation observation.

    The callback may run either on the restore worker or on the legacy
    sidecar polling thread. It is display-only: consumer failures never alter
    the physical export/import transaction.
    """

    if not callable(callback):
        return
    if not isinstance(item, str) or any(ord(character) < 0x20 for character in item):
        item = ""
    else:
        try:
            item_bytes = item.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            item = ""
        else:
            if len(item_bytes) > 1024:
                item = item_bytes[:1024].decode("utf-8", errors="ignore").rstrip()
    if (
        not isinstance(phase, str)
        or not phase
        or len(phase.encode("utf-8")) > 128
        or not isinstance(message, str)
        or not message
        or len(message.encode("utf-8")) > 1024
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (current, total, event_id)
        )
        or current > MAX_SHADER_CATALOG_PROGRESS_EVENTS
        or total > MAX_SHADER_CATALOG_PROGRESS_EVENTS
        or (total > 0 and current > total)
        or event_id > 0xFFFFFFFF
    ):
        logger.warning("Invalid active-capture preparation progress was ignored")
        return
    try:
        callback(
            {
                "schema": ACTIVE_CAPTURE_PREPARE_PROGRESS_SCHEMA,
                "schemaVersion": 1,
                "phase": phase,
                "message": message,
                "current": current,
                "total": total,
                "item": item,
                "eventId": event_id,
                "updatedUnixMs": int(time.time() * 1000),
            }
        )
    except Exception as exc:
        logger.warning(
            "Active-capture preparation progress callback failed: %s: %s",
            type(exc).__name__,
            exc,
        )


def _shader_catalog_export_job_snapshot(
    value: Any,
    expected_capture_session_id: str,
    *,
    expected_job_id: str | None = None,
    allow_result: bool = False,
) -> dict[str, Any]:
    """Strictly validate one bridge-owned asynchronous export snapshot."""

    if not isinstance(value, Mapping):
        raise RuntimeError("Shader catalog export job returned a non-object snapshot")
    required = {
        "schema",
        "schemaVersion",
        "jobId",
        "status",
        "captureSessionId",
        "cancellationRequested",
        "current",
        "total",
        "eventId",
    }
    allowed = set(required)
    allowed.add("error")
    if allow_result:
        allowed.add("result")
    if not required.issubset(value) or not set(value).issubset(allowed):
        raise RuntimeError("Shader catalog export job fields are invalid")
    if (
        value.get("schema") != SHADER_CATALOG_EXPORT_JOB_SCHEMA
        or value.get("schemaVersion") != 1
    ):
        raise RuntimeError("Shader catalog export job schema is unsupported")
    job_id = _strict_lower_hex(value.get("jobId"), 32, "export jobId")
    if expected_job_id is not None and job_id != expected_job_id:
        raise RuntimeError("Shader catalog export job identity changed")
    session_id = _strict_lower_hex(
        value.get("captureSessionId"), 32, "export captureSessionId"
    )
    if session_id != expected_capture_session_id:
        raise RuntimeError("Shader catalog export job changed capture session")
    status = value.get("status")
    if status not in {"queued", "running", "completed", "failed", "cancelled"}:
        raise RuntimeError("Shader catalog export job status is invalid")
    if not isinstance(value.get("cancellationRequested"), bool):
        raise RuntimeError("Shader catalog export cancellation state is invalid")
    counts: dict[str, int] = {}
    for field, maximum in (
        ("current", MAX_SHADER_CATALOG_PROGRESS_EVENTS),
        ("total", MAX_SHADER_CATALOG_PROGRESS_EVENTS),
        ("eventId", 0xFFFFFFFF),
    ):
        count = value.get(field)
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= maximum:
            raise RuntimeError("Shader catalog export job %s is invalid" % field)
        counts[field] = count
    if counts["total"] and counts["current"] > counts["total"]:
        raise RuntimeError("Shader catalog export job progress exceeds its total")
    error = value.get("error")
    if status in {"failed", "cancelled"}:
        if (
            not isinstance(error, str)
            or not error.strip()
            or len(error.encode("utf-8")) > 4096
            or "\x00" in error
        ):
            raise RuntimeError("Shader catalog export job error is invalid")
    elif "error" in value:
        raise RuntimeError("Shader catalog export job has an unexpected error")
    result = value.get("result")
    if "result" in value and (
        not allow_result or status != "completed" or not isinstance(result, Mapping)
    ):
        raise RuntimeError("Shader catalog export job result is invalid")
    return copy.deepcopy(dict(value))


def _bridge_method_is_unavailable(error: BaseException, method: str) -> bool:
    return str(error).strip() == (
        "Bridge error [-32601]: Unknown method: %s" % method
    )


def _cancel_shader_catalog_export_job_best_effort(job_id: str) -> None:
    try:
        ipc.call(
            "cancel_export_shader_catalog",
            {"job_id": job_id},
            timeout=SHADER_CATALOG_EXPORT_JOB_CALL_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "Shader catalog export job cancellation failed: %s: %s",
            type(exc).__name__,
            exc,
        )


def _export_shader_catalog_async(
    export_params: Mapping[str, Any],
    capture_session_id: str,
    progress_callback: Any,
    control_checkpoint: Any,
) -> tuple[bool, Any]:
    """Use the responsive bridge export job protocol when it is available.

    ``False`` is returned only for an explicit JSON-RPC method-not-found from
    the initial begin call. Once a job exists, every protocol/transport failure
    is terminal so physical export is never duplicated through the legacy path.
    """

    method = "begin_export_shader_catalog"
    try:
        raw = ipc.call(
            method,
            dict(export_params),
            timeout=SHADER_CATALOG_EXPORT_JOB_CALL_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        if _bridge_method_is_unavailable(exc, method):
            return False, None
        raise
    snapshot = _shader_catalog_export_job_snapshot(raw, capture_session_id)
    job_id = str(snapshot["jobId"])
    started = time.monotonic()
    consumed = False
    try:
        while True:
            if callable(control_checkpoint):
                control_checkpoint("during asynchronous full-frame Shader catalog export")
            status = str(snapshot["status"])
            current = int(snapshot["current"])
            total = int(snapshot["total"])
            event_id = int(snapshot["eventId"])
            if total > 0 and current > 0:
                _emit_active_capture_prepare_progress(
                    progress_callback,
                    "export-shader-bindings",
                    "Exporting Shader bindings",
                    current=current,
                    total=total,
                    item=("EID %d" % event_id) if event_id else "",
                    event_id=event_id,
                )
            else:
                _emit_active_capture_prepare_progress(
                    progress_callback,
                    "prepare-export-wait",
                    "Waiting for full-capture Shader export",
                    current=1,
                    total=7,
                )
            if status == "completed":
                if callable(control_checkpoint):
                    control_checkpoint("before consuming full-frame Shader catalog export")
                consumed_snapshot = _shader_catalog_export_job_snapshot(
                    ipc.call(
                        "consume_export_shader_catalog",
                        {"job_id": job_id},
                        timeout=SHADER_CATALOG_EXPORT_JOB_CALL_TIMEOUT_SECONDS,
                    ),
                    capture_session_id,
                    expected_job_id=job_id,
                    allow_result=True,
                )
                if "result" not in consumed_snapshot:
                    raise RuntimeError(
                        "completed Shader catalog export job has no result"
                    )
                consumed = True
                return True, copy.deepcopy(consumed_snapshot["result"])
            if status in {"failed", "cancelled"}:
                raise RuntimeError(
                    "Shader catalog export job %s: %s"
                    % (status, str(snapshot["error"]).strip())
                )
            if time.monotonic() - started >= ACTIVE_CAPTURE_EXPORT_TIMEOUT_SECONDS:
                raise RuntimeError("Shader catalog export job timed out")
            time.sleep(SHADER_CATALOG_EXPORT_JOB_POLL_SECONDS)
            snapshot = _shader_catalog_export_job_snapshot(
                ipc.call(
                    "poll_export_shader_catalog",
                    {"job_id": job_id},
                    timeout=SHADER_CATALOG_EXPORT_JOB_CALL_TIMEOUT_SECONDS,
                ),
                capture_session_id,
                expected_job_id=job_id,
            )
    finally:
        if not consumed:
            _cancel_shader_catalog_export_job_best_effort(job_id)


def _prepare_active_capture_project(
    progress_token: str | None = None,
    progress_callback: Any = None,
    control_checkpoint: Any = None,
) -> str:
    """Stage/import the full current capture and resolve verified asset symbols.

    Pending projects only stage content-addressed evidence. Bound projects also
    import it for cross-frame matching. Asset scans, when configured, export
    and load only fixed-program verified aliases; AIProvisional rows never enter
    the export, project, or RenderDoc overlay.
    """

    export_root = None
    try:
        service = _active_project_service()
        project_status = service.ensure_pending()
        # A newly opened capture has a session/path/API immediately, while its
        # canonical content identity is populated only by the full export.
        context = _active_live_context(allow_missing_capture_id=True)
        capture = context["capture"]
        capture_session_id = str(capture["captureSessionId"])
        _emit_active_capture_prepare_progress(
            progress_callback,
            "prepare-export-start",
            "Starting full-capture Shader export",
            current=1,
            total=7,
            item=str(capture.get("path", "") or ""),
        )
        if callable(control_checkpoint):
            control_checkpoint("before active-capture Shader catalog export")
        progress_operation_token = (
            _strict_lower_hex(progress_token, 32, "progress_token")
            if progress_token is not None
            else None
        )
        live_capture_id = str(capture.get("captureId", "") or "")
        live_capture_path = str(capture["path"])
        live_api = str(capture["api"])
        catalogs_root = Path(project_status["paths"]["captureCatalogs"]).resolve()
        if catalogs_root.is_symlink() or not catalogs_root.is_dir():
            raise RuntimeError("active project capture-catalog directory is unavailable")
        export_root = Path(
            tempfile.mkdtemp(
                prefix=".current-%s-" % capture_session_id[:12],
                dir=str(catalogs_root),
            )
        ).resolve()
        export_params = {"output_dir": str(export_root), "event_ids": []}
        if progress_operation_token is not None:
            export_params.update(
                {
                    "progress_token": progress_operation_token,
                    "progress_capture_session_id": capture_session_id,
                }
            )
        async_available, exported = _export_shader_catalog_async(
            export_params,
            capture_session_id,
            progress_callback,
            control_checkpoint,
        )
        if not async_available:
            # Downward compatibility is deliberately restricted to an explicit
            # JSON-RPC method-not-found from begin. Any error after a job could
            # have started is terminal so the physical export is never run
            # twice.
            _emit_active_capture_prepare_progress(
                progress_callback,
                "prepare-export-wait",
                "Waiting for full-capture Shader export",
                current=1,
                total=7,
                item=live_capture_path,
            )
            exported = ipc.call(
                "export_shader_catalog",
                export_params,
                timeout=ACTIVE_CAPTURE_EXPORT_TIMEOUT_SECONDS,
            )
        if callable(control_checkpoint):
            control_checkpoint("after active-capture Shader catalog export")
        _emit_active_capture_prepare_progress(
            progress_callback,
            "prepare-export-validate",
            "Validating the exported Shader catalog",
            current=2,
            total=7,
            item=str(export_root),
        )
        catalog_path, exported_capture_id = _validate_active_export(
            exported, export_root, live_capture_id
        )
        context = _canonical_active_capture_context(context, exported_capture_id)
        _require_same_active_capture(context, allow_missing_capture_id=True)
        expected_binding = {
            "expected_capture_id": exported_capture_id,
            "expected_capture_path": live_capture_path,
            "expected_api": live_api,
        }
        with service.pin_active_project(
            str(project_status.get("workspaceId", "") or ""),
            str(project_status.get("projectRoot", "") or ""),
        ):
            _require_same_active_project(project_status)
            _emit_active_capture_prepare_progress(
                progress_callback,
                "prepare-project-import",
                "Importing the Shader catalog into the project",
                current=3,
                total=7,
                item=str(catalog_path),
            )
            if callable(control_checkpoint):
                control_checkpoint("before active-capture project import")
            if project_status.get("scopeConfigured") is True:
                imported = service.import_capture(catalog_path, **expected_binding)
                staged = imported["stagedCatalog"]
                reuse_plan = imported["reusePlan"]
            else:
                staged = service.stage_capture_catalog(catalog_path, **expected_binding)
                reuse_plan = service.future_frame_plan(exported_capture_id)
            if callable(control_checkpoint):
                control_checkpoint("after active-capture project import")
            _emit_active_capture_prepare_progress(
                progress_callback,
                "prepare-reuse-plan",
                "Matching project Shader and CBuffer knowledge",
                current=4,
                total=7,
            )
            capture_id = str(staged.get("captureId", "") or "")
            if capture_id != exported_capture_id:
                raise RuntimeError("active project staged a different capture identity")
            _require_same_active_capture(context, allow_missing_capture_id=True)
            project_status = service.status()
            _emit_active_capture_prepare_progress(
                progress_callback,
                "prepare-catalog-register",
                "Registering the active capture catalog",
                current=5,
                total=7,
                item=str(staged.get("catalogPath", "") or ""),
            )
            if callable(control_checkpoint):
                control_checkpoint("before active-capture catalog registration")
            _remember_active_capture_catalog(project_status, context, staged)
            if callable(control_checkpoint):
                control_checkpoint("after active-capture catalog registration")
            _emit_active_capture_prepare_progress(
                progress_callback,
                "prepare-symbol-resolve",
                "Resolving project Shader and CBuffer aliases",
                current=6,
                total=7,
                item=str(staged.get("catalogPath", "") or ""),
            )
            if callable(control_checkpoint):
                control_checkpoint("before active-capture symbol resolution")
            symbol_resolution = _active_symbol_resolution(
                service,
                project_status,
                context,
                Path(str(staged["catalogPath"])),
                str(staged["catalogSha256"]),
            )
            if callable(control_checkpoint):
                control_checkpoint("after active-capture symbol resolution")
            _require_same_active_project(project_status)
        _require_same_active_capture(context, allow_missing_capture_id=True)
        persistent_export = {
            "schema": "replaylab-active-staged-capture-export/v1",
            "schemaVersion": 1,
            "captureId": capture_id,
            "catalogPath": staged["catalogPath"],
            "catalogSha256": staged["catalogSha256"],
            "shaderBlobCount": staged["shaderBlobCount"],
            "fullFrame": True,
            "temporaryExportRemoved": True,
        }
        _emit_active_capture_prepare_progress(
            progress_callback,
            "prepare-complete",
            "Current capture project preparation completed",
            current=7,
            total=7,
        )
        return _ok(
            {
                "schema": "replaylab-active-capture-prepare/v1",
                "schemaVersion": 1,
                "capture": {
                    "captureId": capture_id,
                    "captureSessionId": capture_session_id,
                },
                "project": project_status,
                "captureExport": persistent_export,
                "stagedCatalog": staged,
                "reusePlan": reuse_plan,
                "symbolResolution": symbol_resolution,
                "applied": False,
                "policy": dict(_ACTIVE_PROJECT_MCP_POLICY),
            }
        )
    except Exception as error:
        if type(error).__name__ in {"RestoreCancelled", "RestoreDeadlineExceeded"}:
            raise
        return _active_project_error("prepare-current-capture", error)
    finally:
        if (
            export_root is not None
            and export_root.exists()
            and export_root.name.startswith(".current-")
        ):
            shutil.rmtree(export_root)


def _prepare_active_capture_project_with_progress(
    progress_token: str,
    progress_callback: Any,
    control_checkpoint: Any = None,
) -> str:
    """Prepare the capture while forwarding strictly bound export progress.

    New bridges execute the replay traversal through their bounded asynchronous
    export job while this worker polls lightweight snapshots. The sidecar
    reader remains for legacy/full-fidelity EID telemetry and the synchronous
    method-not-found fallback. Progress is advisory: malformed observations,
    reader races, and callback failures never change the physical preparation
    result.
    """

    operation_token = _strict_lower_hex(
        progress_token, 32, "progress_token"
    )
    if not callable(progress_callback):
        raise TypeError("progress_callback must be callable")
    if control_checkpoint is not None and not callable(control_checkpoint):
        raise TypeError("control_checkpoint must be callable or None")
    callback_lock = threading.RLock()
    callback_available = True
    last_signature: tuple[Any, ...] | None = None
    last_export: tuple[int, int] | None = None

    def forward_progress(observation: Any) -> None:
        """Serialize high-level and sidecar progress onto one bounded stream."""

        nonlocal callback_available, last_signature, last_export
        if not isinstance(observation, Mapping):
            return
        phase = observation.get("phase")
        current = observation.get("current")
        total = observation.get("total")
        event_id = observation.get("eventId", 0)
        item = observation.get("item", "")
        message = observation.get("message", "")
        if (
            not isinstance(phase, str)
            or not phase
            or isinstance(current, bool)
            or not isinstance(current, int)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or isinstance(event_id, bool)
            or not isinstance(event_id, int)
            or not isinstance(item, str)
            or not isinstance(message, str)
        ):
            return
        signature = (phase, current, total, event_id, item, message)
        with callback_lock:
            if phase == "export-shader-bindings":
                if total <= 0 or current <= 0 or current > total:
                    return
                if last_export is not None and (
                    total != last_export[1] or current < last_export[0]
                ):
                    logger.warning(
                        "Regressing combined Shader catalog progress was ignored"
                    )
                    return
                last_export = (current, total)
            if signature == last_signature:
                return
            last_signature = signature
            if not callback_available:
                return
            try:
                progress_callback(copy.deepcopy(dict(observation)))
            except Exception as exc:
                callback_available = False
                logger.warning(
                    "Shader catalog progress callback was disabled: %s: %s",
                    type(exc).__name__,
                    exc,
                )
    try:
        context = _active_live_context(allow_missing_capture_id=True)
        capture_session_id = str(context["capture"]["captureSessionId"])
        channel = _shader_catalog_progress_channel(
            capture_session_id,
            operation_token=operation_token,
        )
    except Exception as exc:
        # The export is physical project work; progress is only advisory. If a
        # legacy IPC client has no discoverable sidecar mailbox, keep the old
        # preparation behaviour and simply omit fine-grained observations.
        logger.warning(
            "Shader catalog progress channel is unavailable: %s: %s",
            type(exc).__name__,
            exc,
        )
        return _prepare_active_capture_project(
            progress_callback=forward_progress,
            control_checkpoint=control_checkpoint,
        )
    stopped = threading.Event()

    def poll_progress() -> None:
        previous: dict[str, Any] | None = None
        callback_available = True
        reader_warning_emitted = False
        deadline = (
            time.monotonic() + ACTIVE_CAPTURE_EXPORT_TIMEOUT_SECONDS + 5.0
        )

        def observe() -> None:
            nonlocal previous, callback_available, reader_warning_emitted
            try:
                observation = _read_shader_catalog_progress(channel)
            except Exception as exc:
                if not reader_warning_emitted:
                    logger.warning(
                        "Shader catalog progress telemetry was ignored: %s: %s",
                        type(exc).__name__,
                        exc,
                    )
                    reader_warning_emitted = True
                return
            if observation is None:
                return
            if previous is not None:
                if observation == previous:
                    return
                if (
                    observation["total"] != previous["total"]
                    or observation["current"] < previous["current"]
                    or observation["updatedUnixMs"]
                    < previous["updatedUnixMs"]
                ):
                    if not reader_warning_emitted:
                        logger.warning(
                            "Regressing Shader catalog progress telemetry was ignored"
                        )
                        reader_warning_emitted = True
                    return
            previous = copy.deepcopy(observation)
            if not callback_available:
                return
            try:
                forward_progress(copy.deepcopy(observation))
            except Exception as exc:
                # A UI callback is display-only. Disable it after the first
                # failure so a broken consumer cannot flood the log while the
                # physical export job or legacy fallback continues.
                callback_available = False
                logger.warning(
                    "Shader catalog progress callback was disabled: %s: %s",
                    type(exc).__name__,
                    exc,
                )

        while not stopped.is_set() and time.monotonic() < deadline:
            observe()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            stopped.wait(min(SHADER_CATALOG_PROGRESS_POLL_SECONDS, remaining))
        # The export may complete between the last poll and the IPC response.
        # Always perform one final bounded read before the caller cleans up.
        observe()

    poller = threading.Thread(
        target=poll_progress,
        name="ReplayLabShaderCatalogProgress",
        daemon=True,
    )
    try:
        poller.start()
    except Exception as exc:
        logger.warning(
            "Shader catalog progress thread is unavailable: %s: %s",
            type(exc).__name__,
            exc,
        )
        _cleanup_shader_catalog_progress(channel)
        return _prepare_active_capture_project(
            progress_callback=forward_progress,
            control_checkpoint=control_checkpoint,
        )
    try:
        return _prepare_active_capture_project(
            progress_token=channel["operationToken"],
            progress_callback=forward_progress,
            control_checkpoint=control_checkpoint,
        )
    finally:
        stopped.set()
        poller.join(timeout=SHADER_CATALOG_PROGRESS_JOIN_SECONDS)
        if poller.is_alive():
            logger.warning(
                "Shader catalog progress callback did not stop before cleanup"
            )
        _cleanup_shader_catalog_progress(channel)


@mcp.tool()
def prepare_active_capture_project() -> str:
    """Stage/import the full current capture and resolve verified asset symbols."""

    return _prepare_active_capture_project()


@mcp.tool()
def query_active_shader_asset_evidence(
    event_id: int,
    stage: str = "fragment",
    top_k: int = 10,
) -> str:
    """Read prepared asset/symbol evidence for one active-project Shader.

    The request accepts no filesystem path. It uses only the current active
    project, its current hash-validated full-catalog binding, and the prepared
    ``shared/dictionaries/assets/symbols-v3.sqlite3`` database. It never scans
    assets, compiles source, submits a match, or applies aliases. A missing
    prerequisite is returned as ``ready=false`` so the child Agent can run the
    explicit preparation step and resume.
    """

    if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
        return _err(ValueError("event_id must be a positive integer"))
    try:
        requested_stage = normalize_stage(stage)
    except (TypeError, ValueError) as error:
        return _err(error)
    if not requested_stage:
        return _err(ValueError("stage is required"))
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        return _err(TypeError("top_k must be an integer from 1 to 10"))
    if top_k < 1 or top_k > 10:
        return _err(ValueError("top_k must be an integer from 1 to 10"))

    try:
        service = _active_project_service()
        project_status = service.status()
    except Exception as error:
        return _active_shader_asset_evidence_not_ready(
            "active-project-unavailable",
            str(error),
            prerequisites=["ensure_active_replay_project"],
        )

    if project_status.get("assetRootConfigured") is not True:
        return _active_shader_asset_evidence_not_ready(
            "asset-root-not-configured",
            "active project has no unpacked asset root configured",
            prerequisites=["configure_active_project_asset_root"],
        )
    if project_status.get("assetRootAvailable") is not True:
        return _active_shader_asset_evidence_not_ready(
            "asset-root-unavailable",
            "configured active-project asset root is unavailable",
            prerequisites=["configure_active_project_asset_root"],
        )
    if project_status.get("scopeConfigured") is not True:
        return _active_shader_asset_evidence_not_ready(
            "project-scope-not-configured",
            "active project game/build scope is not configured",
            prerequisites=["configure_active_replay_project"],
        )

    try:
        workspace_id = str(project_status.get("workspaceId", "") or "")
        project_root = Path(
            str(project_status.get("projectRoot", "") or "")
        ).resolve()
        with service.pin_active_project(workspace_id, project_root):
            _require_same_active_project(project_status)
            binding = _active_capture_catalog_binding()
            if binding is None:
                return _active_shader_asset_evidence_not_ready(
                    "full-catalog-not-prepared",
                    "current capture has no active-project full-catalog binding",
                )
            context, bound_project, record, catalog_path = binding
            if record.get("catalogScope", "full-frame") != "full-frame":
                return _active_shader_asset_evidence_not_ready(
                    "full-catalog-not-prepared",
                    "current Shader catalog covers one event/stage, not the full capture",
                )
            if (
                bound_project.get("workspaceId") != workspace_id
                or Path(str(bound_project.get("projectRoot", "") or "")).resolve()
                != project_root
            ):
                raise RuntimeError(
                    "active full-catalog binding belongs to another project"
                )
            selected = select_unique_shader_for_use(
                catalog_path, event_id, requested_stage
            )
            if selected.capture_id != record.get("captureId"):
                raise RuntimeError(
                    "selected Shader capture identity differs from the active binding"
                )

            paths = project_status.get("paths")
            if not isinstance(paths, Mapping) or not paths.get("sharedDictionaries"):
                raise RuntimeError(
                    "active project omitted its shared dictionary root"
                )
            shared_root = Path(str(paths["sharedDictionaries"])).resolve()
            assets_dictionary = shared_root / "assets"
            managed_database_entry = assets_dictionary / "symbols-v3.sqlite3"
            database_path = managed_database_entry.resolve()
            if (
                project_root.is_symlink()
                or not project_root.is_dir()
                or shared_root.is_symlink()
                or not shared_root.is_dir()
                or assets_dictionary.is_symlink()
                or managed_database_entry.is_symlink()
                or not is_within(project_root, shared_root)
                or not is_within(shared_root, database_path)
                or not database_path.is_file()
            ):
                return _active_shader_asset_evidence_not_ready(
                    "managed-symbol-database-unavailable",
                    "prepared active-project symbol database is unavailable",
                )
            before = database_path.stat()
            before_identity = (
                int(before.st_dev),
                int(before.st_ino),
                int(before.st_size),
                int(before.st_mtime_ns),
            )
            symbols = SymbolService(database_path, read_only=True)
            if symbols.database_path != database_path:
                raise RuntimeError(
                    "symbol service escaped the managed project database"
                )
            stored_catalog = symbols.store.get_capture_catalog(selected.capture_id)
            if not isinstance(stored_catalog, Mapping):
                return _active_shader_asset_evidence_not_ready(
                    "capture-not-matched-in-managed-database",
                    "managed symbol database has no current capture match",
                )
            stored_catalog_path = Path(
                str(stored_catalog.get("catalog_path", "") or "")
            ).expanduser().resolve()
            if stored_catalog_path != catalog_path:
                return _active_shader_asset_evidence_not_ready(
                    "catalog-revision-not-matched",
                    "managed symbol database is not bound to the active catalog revision",
                )
            symbol_evidence = symbols.evidence(
                selected.capture_id,
                selected.key.resource_id,
                selected.key.stage,
                selected.key.entry_point,
                top_k=top_k,
            )
            capture_shader = symbol_evidence.get("captureShader")
            if not isinstance(capture_shader, Mapping):
                raise RuntimeError("symbol evidence omitted the captured Shader")
            if any(
                (
                    symbol_evidence.get("captureId") != selected.capture_id,
                    symbol_evidence.get("shaderResourceId")
                    != selected.key.resource_id,
                    normalize_stage(symbol_evidence.get("stage"))
                    != selected.key.stage,
                    str(symbol_evidence.get("entryPoint", "") or "")
                    != selected.key.entry_point,
                    int(
                        capture_shader.get(
                            "shaderResourceId",
                            capture_shader.get("resourceId", 0),
                        )
                        or 0
                    )
                    != selected.key.resource_id,
                    normalize_stage(capture_shader.get("stage"))
                    != selected.key.stage,
                    str(capture_shader.get("entryPoint", "") or "")
                    != selected.key.entry_point,
                    str(capture_shader.get("rawSha256", "") or "").lower()
                    != selected.key.raw_sha256,
                )
            ):
                raise RuntimeError(
                    "managed symbol evidence is not bound to the selected Shader"
                )

            after = database_path.stat()
            after_identity = (
                int(after.st_dev),
                int(after.st_ino),
                int(after.st_size),
                int(after.st_mtime_ns),
            )
            if (
                assets_dictionary.is_symlink()
                or managed_database_entry.is_symlink()
                or managed_database_entry.resolve() != database_path
                or before_identity != after_identity
            ):
                raise RuntimeError(
                    "managed symbol database changed during the read-only query"
                )
            _require_same_active_capture(context, allow_missing_capture_id=True)
            current_binding = _active_capture_catalog_binding(context)
            if current_binding is None:
                raise RuntimeError(
                    "active full-catalog binding disappeared during evidence query"
                )
            current_record = current_binding[2]
            binding_sha256 = _active_capture_catalog_binding_sha256(record)
            if (
                _active_capture_catalog_binding_sha256(current_record)
                != binding_sha256
            ):
                raise RuntimeError(
                    "active full-catalog binding changed during evidence query"
                )
            _require_same_active_project(project_status)

        bound_identity = {
            "evidenceClass": "observed",
            "captureId": selected.capture_id,
            "captureSessionId": str(
                context.get("capture", {}).get("captureSessionId", "") or ""
            ),
            "eventId": int(event_id),
            "stage": selected.key.stage,
            "shaderResourceId": selected.key.resource_id,
            "entryPoint": selected.key.entry_point,
            "rawSha256": selected.key.raw_sha256,
            "catalogSha256": str(record.get("catalogSha256", "") or ""),
            "catalogBindingSha256": binding_sha256,
        }
        return _ok(
            {
                "schema": "replaylab-active-shader-asset-evidence/v1",
                "schemaVersion": 1,
                "ready": True,
                "reasonCode": "ready",
                "reason": "",
                "requiredAction": "",
                "prerequisites": [],
                "boundIdentity": bound_identity,
                "symbolEvidence": symbol_evidence,
                "relationView": _shader_asset_relation_view(
                    bound_identity, symbol_evidence
                ),
                "managedDatabase": {
                    "projectRelativePath": (
                        "shared/dictionaries/assets/symbols-v3.sqlite3"
                    ),
                    "readOnly": True,
                    "fileIdentityStable": True,
                },
                "applied": False,
                "policy": {
                    "activeProjectOnly": True,
                    "activeFullCatalogOnly": True,
                    "managedSymbolDatabaseOnly": True,
                    "readOnlyDatabase": True,
                    "assetScanTriggered": False,
                    "compilerInvoked": False,
                    "aliasMutation": False,
                    "materialEntitySynthesized": False,
                    "unverifiedRelationsRemainInference": True,
                },
            }
        )
    except Exception as error:
        return _active_shader_asset_evidence_not_ready(
            "prepared-evidence-unavailable",
            str(error),
        )


def _current_shader_reuse_projection(
    plan: dict[str, Any], shader: dict[str, Any], event_id: int
) -> dict[str, Any]:
    """Keep current-Shader bootstrap responses bounded to its real candidates."""
    result = copy.deepcopy(plan)
    def select(items: Any) -> list[dict[str, Any]]:
        selected = []
        for item in items or []:
            identity = item.get("shader", {}) if isinstance(item, dict) else {}
            if any(identity.get(key) != shader[key]
                   for key in ("resourceId", "stage", "entryPoint", "rawSha256")):
                continue
            item = copy.deepcopy(item)
            event_ids = item["shader"].get("eventIds", [])
            item["shader"]["catalogUseCount"] = len(event_ids)
            item["shader"]["eventIds"] = [event_id]
            item["shader"]["eventIdsScope"] = "selected-event"
            selected.append(item)
        return selected
    result["matchScope"] = "selected-shader"
    result["preparationSteps"] = select(result.get("preparationSteps"))
    reuse = result.get("reusePlan")
    if isinstance(reuse, dict):
        reuse["matches"] = select(reuse.get("matches"))
        reuse["matchedShaderCount"] = len(reuse["matches"])
        reuse["preparationSteps"] = select(reuse.get("preparationSteps"))
        reuse["matchScope"] = "selected-shader"
    return result


def _bootstrap_active_shader_catalog(
    project: Any, project_status: dict[str, Any], context: dict[str, Any],
    event_id: int, stage: str,
    expected_shader: dict[str, Any] | None = None,
) -> None:
    """Prepare only the frozen current event/stage, retaining exact scope."""
    stage = normalize_stage(stage)
    instance_id = str(getattr(ipc, "instance_id", "") or "")
    if not instance_id:
        raise RuntimeError("current Shader preparation requires a pinned ReplayLab instance")
    events = context.get("events", {})
    bound = context.get("shaders", {}).get("bound", [])
    matches = [item for item in bound if isinstance(item, dict)
               and normalize_stage(item.get("stage")) == stage]
    if expected_shader is None:
        if events.get("currentEventId") != event_id or len(matches) != 1:
            raise RuntimeError("frozen event Shader identity is unavailable in the current context")
        if "resourceId" not in matches[0]:
            raise RuntimeError("current context omitted the exact Shader ResourceId")
    capture = context["capture"]
    export_root = None
    catalog_root = Path(project_status["paths"]["captureCatalogs"]).resolve()
    if catalog_root.is_symlink() or not catalog_root.is_dir():
        raise RuntimeError("active project capture-catalog directory is unavailable")

    def unchanged() -> None:
        current = _require_same_active_capture(context, allow_missing_capture_id=True)
        if current.get("events") != events:
            raise RuntimeError("current event selection changed during Shader preparation")
        current_bound = current.get("shaders", {}).get("bound", [])
        current_matches = [item for item in current_bound if isinstance(item, dict)
                           and normalize_stage(item.get("stage")) == stage]
        if events.get("currentEventId") == event_id and len(matches) == 1:
            if len(current_matches) != 1 or any(
                str(current_matches[0].get(key, "")) != str(matches[0].get(key, ""))
                for key in ("resourceId", "rawSha256", "entryPoint")
            ):
                raise RuntimeError("current Shader identity changed during Shader preparation")
        _require_same_active_project(project_status)

    with bind_direct_tool_call(instance_id, capture["captureSessionId"]):
        # Bound context is already available; this must never fall through to a
        # broad get_shader_info query or enumerate another event.
        expected = expected_shader or _active_bound_shader(context, event_id, stage)
        unchanged()
        try:
            export_root = Path(tempfile.mkdtemp(
                prefix=".shader-%s-" % capture["captureSessionId"][:12],
                dir=str(catalog_root),
            )).resolve()
            exported = ipc.call("export_shader_catalog", {
                "output_dir": str(export_root), "event_ids": [event_id], "stages": [stage],
            }, timeout=300.0)
            path, capture_id = _validate_active_export(exported, export_root, str(capture.get("captureId", "")))
            if exported.get("captureSessionId") != capture["captureSessionId"]:
                raise RuntimeError("Shader catalog export belongs to another capture session")
            selected = _validate_event_stage_catalog(path, event_id, stage)
            if selected.capture_id != capture_id or (
                selected.key.resource_id != expected["resourceId"]
                or selected.key.raw_sha256 != expected["rawSha256"]
                or selected.key.entry_point != expected["entryPoint"]
                or _capture_path_key(selected.capture_path) != _capture_path_key(capture["path"])
                or _active_api_key(selected.api) != _active_api_key(capture["api"])
            ):
                raise RuntimeError("Shader catalog differs from the frozen current Shader identity")
            unchanged()
            import_args = dict(expected_capture_id=capture_id,
                               expected_capture_path=capture["path"], expected_api=capture["api"])
            if project_status.get("scopeConfigured") is True:
                staged = project.import_capture(path, **import_args)["stagedCatalog"]
            else:
                staged = project.stage_capture_catalog(path, **import_args)
            unchanged()
            canonical = _canonical_active_capture_context(context, capture_id)
            _remember_active_capture_catalog(project_status, canonical, staged,
                                             event_id=event_id, stage=stage)
        finally:
            if export_root is not None and export_root.exists():
                if (export_root.is_symlink() or not is_within(catalog_root, export_root)
                        or not export_root.name.startswith(".shader-")):
                    raise RuntimeError("Shader catalog temporary directory changed")
                shutil.rmtree(export_root)


@mcp.tool()
def get_active_shader_workspace_path(
    event_id: int, stage: str = "fragment",
    expected_resource_id: int | None = None,
    expected_entry_point: str | None = None,
    expected_raw_sha256: str = "",
    expected_capture_session_id: str = "",
) -> str:
    """Prepare the current Shader catalog and return its managed workspace path.

    A new capture needs only its current event/stage. The fixed Host freezes
    instance/session/path and physical Shader identity, exports and stages that
    exact catalog, and imports it when project scope is configured. It does not
    scan the frame, compile a Shader or apply a replacement. Partial catalog
    scope is explicit; an existing full catalog is reused when available.
    A frozen inventory anchor different from the UI event requires all four
    expected identity fields (ResourceId, entry point, raw hash and capture
    session). Its single-event export must prove that tuple. Present/another
    UI event never supplies the target Shader identity; UI selection is kept.

    The path is namespaced by the exact catalog revision and by a hash of the
    route catalog, tool registry, and automatic-format policy. This prevents a
    workspace produced under an older route/tool policy from being silently
    reused after configuration changes.
    """

    try:
        supplied = (
            expected_resource_id is not None, expected_entry_point is not None,
            bool(expected_raw_sha256), bool(expected_capture_session_id),
        )
        expected_shader = None
        if any(supplied):
            if not all(supplied):
                raise ValueError("all four frozen Shader identity fields are required")
            if type(expected_resource_id) is not int or expected_resource_id <= 0:
                raise ValueError("expected_resource_id must be a positive integer")
            if not isinstance(expected_entry_point, str) or len(expected_entry_point) > 1024:
                raise ValueError("expected_entry_point must be a bounded string")
            expected_shader = {
                "resourceId": expected_resource_id, "entryPoint": expected_entry_point,
                "rawSha256": _strict_lower_hex(expected_raw_sha256, 64, "expected_raw_sha256"),
                "stage": normalize_stage(stage),
            }
            _strict_lower_hex(expected_capture_session_id, 32, "expected_capture_session_id")
        service = _active_project_service()
        project_status = service.ensure_pending()
        with service.pin_active_project(
            str(project_status.get("workspaceId", "") or ""),
            str(project_status.get("projectRoot", "") or ""),
        ):
            _require_same_active_project(project_status)
            context = _active_live_context(allow_missing_capture_id=True)
            if expected_shader is not None and context["capture"]["captureSessionId"] != expected_capture_session_id:
                raise RuntimeError("frozen Shader capture session differs from the current capture")
            if type(event_id) is not int or event_id <= 0 or not normalize_stage(stage):
                raise ValueError("event_id and stage must identify one current Shader")
            event_value = event_id
            route_binding_sha256 = _active_shader_route_binding_sha256()
            binding = _active_capture_catalog_binding(context)
            if binding is None or not _active_catalog_covers(binding[2], event_value, stage):
                _bootstrap_active_shader_catalog(service, project_status, context, event_value, stage, expected_shader)
            context, capture_id, capture_session_id, uses_full_binding = (
                _active_project_capture_identity(context)
            )
            catalog_sha256, expected_binding_sha256 = (
                _active_project_catalog_revision_for_workspace(
                    context, capture_id, uses_full_binding
                )
            )
            shader = _active_bound_shader(context, event_id, stage)
            if expected_shader is not None and shader != expected_shader:
                raise RuntimeError("catalog Shader differs from the frozen Shader identity")
            binding = _active_capture_catalog_binding(context)
            if binding is None:
                raise RuntimeError("active Shader catalog binding disappeared")
            catalog_scope = _active_catalog_scope(binding[2])
            try:
                reuse_plan = service.future_frame_plan(capture_id, catalog_sha256 or "")
            except ReplayProjectError as error:
                if str(error) not in {"capture_id is not imported in this project", "catalog revision is not imported in this project"}:
                    raise
                # The user may bind a previously pending project between two
                # calls. Import its already verified catalog without exporting.
                service.import_capture(binding[3], expected_capture_id=capture_id,
                                       expected_capture_path=context["capture"]["path"],
                                       expected_api=context["capture"]["api"])
                reuse_plan = service.future_frame_plan(capture_id, catalog_sha256 or "")
            reuse_plan = {**_current_shader_reuse_projection(reuse_plan, shader, event_id),
                          **catalog_scope}
            workspace = service.shader_workspace_path(
                capture_id=capture_id,
                catalog_sha256=catalog_sha256,
                route_binding_sha256=route_binding_sha256,
                stage=shader["stage"],
                shader_resource_id=shader["resourceId"],
                raw_sha256=shader["rawSha256"],
                create=False,
            )
            _require_same_active_capture(
                context, allow_missing_capture_id=uses_full_binding
            )
            _require_same_active_project(project_status)
            if uses_full_binding:
                current_binding = _active_capture_catalog_binding(context)
                if current_binding is None or (
                    _active_capture_catalog_binding_sha256(current_binding[2])
                    != expected_binding_sha256
                ):
                    raise RuntimeError(
                        "active full-catalog binding changed during workspace path resolution"
                    )
        return _ok(
            {
                "schema": "replaylab-active-shader-workspace-path/v1",
                "schemaVersion": 1,
                "captureId": capture_id,
                "captureSessionId": capture_session_id,
                "eventId": int(event_id),
                "stage": shader["stage"],
                "entryPoint": shader["entryPoint"],
                "shaderResourceId": shader["resourceId"],
                "rawSha256": shader["rawSha256"],
                "catalogSha256": catalog_sha256,
                "routeBindingSha256": route_binding_sha256,
                "workspaceLayout": "content-addressed-v2",
                "workspaceBindingSha256": workspace.name,
                "workspacePath": str(workspace),
                **catalog_scope,
                "projectReuse": reuse_plan,
                "applied": False,
                "policy": dict(_ACTIVE_PROJECT_MCP_POLICY),
            }
        )
    except Exception as error:
        return _active_project_error("shader-workspace-path", error)


def _ensure_active_workspace_project_capture(
    project: Any, status: dict[str, Any], context: dict[str, Any]
) -> None:
    """Import a reviewed workspace's exact catalog only when its revision is absent."""

    # Called only after both publication gates agree, with the source lease
    # held. Never export a new frame or infer an identity from the UI cache.
    binding = _workspace_catalog_binding_projection(status)
    _workspace_catalog_rebind_scope(status)
    if binding["captureId"] != context["capture"]["captureId"]:
        raise RuntimeError("reviewed workspace capture changed before project import")
    _require_same_active_capture(context, allow_missing_capture_id=True)
    catalog_sha256 = ReplayProjectService._capture_status_catalog_sha256(status["capture"])
    replay = project.replay_service()
    try:
        replay.reuse_plan(binding["captureId"], catalog_sha256)
    except ReplayProjectError as error:
        if str(error) not in {
            "capture_id is not imported in this project",
            "catalog revision is not imported in this project",
        }:
            raise
        imported = project.import_capture(
            binding["catalogPath"],
            expected_capture_id=binding["captureId"],
            expected_capture_path=binding["capturePath"],
            expected_api=binding["api"],
        )
        staged = imported.get("stagedCatalog", {})
        if (
            staged.get("captureId") != binding["captureId"]
            or staged.get("catalogSha256") != catalog_sha256
        ):
            raise RuntimeError("project imported a different workspace catalog revision")
        replay.reuse_plan(binding["captureId"], catalog_sha256)
    # The importer validates its bytes before writing. Reject publication if
    # the original immutable catalog or live capture changed in the meantime.
    _workspace_catalog_rebind_scope(status)
    _require_same_active_capture(context, allow_missing_capture_id=True)


@mcp.tool()
def register_active_project_workspace(workspace_path: str) -> str:
    """Register validated HLSL/analysis and open the project Shader Workspace.

    The registered library is read-only until a persistent live-edit session
    supplies the canonical editable source. This call does not apply a shader.
    """

    try:
        active = _active_project_service()
        active_status = active.status()
        with active.pin_active_project(
            str(active_status.get("workspaceId", "") or ""),
            str(active_status.get("projectRoot", "") or ""),
        ):
            _require_same_active_project(active_status)
            workspace = _require_managed_active_workspace(active_status, workspace_path)
            workspace_service = _shader_workspace_service()
            status = workspace_service.status(str(workspace))
            context = _workspace_active_capture_context(str(workspace), status=status)
            capture_id, capture_session_id = _active_capture_identity(context)
            result = _register_reviewed_workspace_with_readback(
                active, workspace_service, str(workspace),
                before_publish=lambda reviewed_status: _ensure_active_workspace_project_capture(
                    active, reviewed_status, context
                ),
            )
            _require_same_active_capture(
                context, allow_missing_capture_id=True
            )
            _require_same_active_project(active_status)
        return _ok(
            {
                "schema": "replaylab-active-workspace-registration/v1",
                "schemaVersion": 1,
                "captureId": capture_id,
                "captureSessionId": capture_session_id,
                "workspacePath": str(workspace),
                "result": result,
                "applied": False,
                "policy": dict(_ACTIVE_PROJECT_MCP_POLICY),
            }
        )
    except Exception as error:
        return _active_project_error("register-workspace", error)


@mcp.tool()
def get_active_project_future_frame_plan(capture_id: str = "") -> str:
    """Return preparation-only cross-frame candidates for the current capture."""

    try:
        service = _active_project_service()
        active_status = service.status()
        with service.pin_active_project(
            str(active_status.get("workspaceId", "") or ""),
            str(active_status.get("projectRoot", "") or ""),
        ):
            _require_same_active_project(active_status)
            context, live_capture_id, capture_session_id, uses_full_binding = (
                _active_project_capture_identity()
            )
            requested = capture_id.strip().lower() if capture_id else live_capture_id
            _strict_lower_hex(requested, 64, "capture_id")
            if requested != live_capture_id:
                raise RuntimeError("capture_id differs from the current RenderDoc capture")
            binding = _active_capture_catalog_binding(context)
            plan = service.future_frame_plan(
                requested, str(binding[2]["catalogSha256"]) if binding is not None else ""
            )
            catalog_scope = (_active_catalog_scope(binding[2]) if binding is not None
                             else {"catalogScope": "imported-catalog", "fullFrame": False,
                                   "coveredEventIds": [], "coveredStages": []})
            plan = {**plan, **catalog_scope}
            _require_same_active_capture(
                context, allow_missing_capture_id=uses_full_binding
            )
            _require_same_active_project(active_status)
        return _ok(
            {
                "schema": "replaylab-active-future-frame-plan-mcp/v1",
                "schemaVersion": 1,
                "captureId": requested,
                "captureSessionId": capture_session_id,
                "plan": plan,
                **catalog_scope,
                "applied": False,
                "policy": dict(_ACTIVE_PROJECT_MCP_POLICY),
            }
        )
    except Exception as error:
        return _active_project_error("future-frame-plan", error)


@mcp.tool()
def seed_active_shader_workspace(knowledge_id: str, workspace_path: str) -> str:
    """Seed a managed current-frame workspace from active-project HLSL only."""

    try:
        active = _active_project_service()
        active_status = active.status()
        with active.pin_active_project(
            str(active_status.get("workspaceId", "") or ""),
            str(active_status.get("projectRoot", "") or ""),
        ):
            _require_same_active_project(active_status)
            workspace = _require_managed_active_workspace(active_status, workspace_path)
            context, capture_id, capture_session_id, uses_full_binding = (
                _active_project_capture_identity()
            )
            workspace_service = _shader_workspace_service()
            before = workspace_service.status(str(workspace))
            if before.get("capture", {}).get("captureId") != capture_id:
                raise RuntimeError("workspace captureId differs from the current RenderDoc capture")
            result = active.seed_workspace(knowledge_id, before)
            if not isinstance(result, dict) or result.get("applied") is not False:
                raise RuntimeError("active project seed returned invalid policy evidence")
            after = workspace_service.status(str(workspace))
            source = after.get("source") if isinstance(after, dict) else None
            if (
                not isinstance(source, dict)
                or source.get("currentSha256") != result.get("sourceSha256")
                or source.get("modifiedSinceDecompile") is not True
                or after.get("capture", {}).get("captureId") != capture_id
            ):
                raise RuntimeError("seeded active workspace did not revalidate as edited source")
            _require_same_active_capture(
                context, allow_missing_capture_id=uses_full_binding
            )
            _require_same_active_project(active_status)
        return _ok(
            {
                "schema": "replaylab-active-workspace-seed/v1",
                "schemaVersion": 1,
                "captureId": capture_id,
                "captureSessionId": capture_session_id,
                "workspacePath": str(workspace),
                "result": result,
                "destinationStatus": after,
                "applied": False,
                "policy": dict(_ACTIVE_PROJECT_MCP_POLICY),
            }
        )
    except Exception as error:
        return _active_project_error("seed-workspace", error)


@mcp.tool()
def restore_current_shader(
    expected_event_id: int,
    expected_resource_id: int,
    expected_entry_point: str,
    expected_raw_sha256: str,
    expected_capture_id: str,
    expected_capture_session_id: str,
    expected_workspace_path: str,
    expected_workspace_id: str,
    expected_operation_id: str = "",
    expected_replacement_id: str = "",
    stage: str = "fragment",
) -> str:
    """Restore one exactly bound shared-resource Shader replacement.

    This works after an MCP host restart: the bridge reports only bounded
    replacement metadata for the exact capture lifecycle.  The host resolves
    the active project's deterministic workspace and verifies capture/session,
    stage, entry point, original ResourceId/raw hash and workspaceId before it
    invokes the existing rollback transaction.  No host-memory token or fuzzy
    bytecode match is trusted. All expected identity fields, including the
    deterministic workspace path/ID, are required so a later UI navigation or
    another pending workspace cannot redirect recovery to another Shader. An
    active replacement is restored only when expected_operation_id matches its
    caller-issued apply token; expected_replacement_id additionally freezes a
    known rollback token. With neither token, only exact absence can succeed.
    Event/family overrides use their exact idempotent apply route to recover
    lost tokens; this tool intentionally handles shared-resource replacements
    only.
    """

    try:
        active = _active_project_service()
        active_status = active.status()
        context, capture_id, capture_session_id, uses_full_binding = (
            _active_project_capture_identity()
        )
        unused_catalog_sha256, expected_binding_sha256 = (
            _active_project_catalog_revision_for_workspace(
                context, capture_id, uses_full_binding
            )
        )
        events = context.get("events")
        if not isinstance(events, dict):
            raise RuntimeError("RenderDoc current context omitted its event identity")
        current_event = events.get("currentEventId")
        if (
            isinstance(current_event, bool)
            or not isinstance(current_event, int)
            or current_event <= 0
        ):
            raise RuntimeError("RenderDoc has no positive current event")
        if (
            isinstance(expected_event_id, bool)
            or not isinstance(expected_event_id, int)
            or expected_event_id <= 0
        ):
            raise ValueError("expected_event_id must be a positive integer")
        if current_event != expected_event_id:
            raise RuntimeError(
                "RenderDoc current event changed from the frozen recovery target"
            )
        if (
            isinstance(expected_resource_id, bool)
            or not isinstance(expected_resource_id, int)
            or expected_resource_id <= 0
        ):
            raise ValueError("expected_resource_id must be a positive integer")
        expected_entry_point = str(expected_entry_point or "")
        if not expected_entry_point or len(expected_entry_point) > 512:
            raise ValueError(
                "expected_entry_point must contain 1 to 512 characters"
            )
        expected_raw_sha256 = _strict_lower_hex(
            expected_raw_sha256, 64, "expected_raw_sha256"
        )
        expected_capture_id = _strict_lower_hex(
            expected_capture_id, 64, "expected_capture_id"
        )
        expected_capture_session_id = _strict_lower_hex(
            expected_capture_session_id,
            32,
            "expected_capture_session_id",
        )
        expected_workspace_input = Path(str(expected_workspace_path or ""))
        if (
            not str(expected_workspace_path or "").strip()
            or "\0" in str(expected_workspace_path)
            or not expected_workspace_input.is_absolute()
        ):
            raise ValueError("expected_workspace_path must be an absolute path")
        expected_workspace = expected_workspace_input.resolve()
        expected_workspace_id = _strict_lower_hex(
            expected_workspace_id, 32, "expected_workspace_id"
        )
        expected_operation_id = str(expected_operation_id or "").strip().lower()
        if expected_operation_id:
            expected_operation_id = _strict_lower_hex(
                expected_operation_id, 32, "expected_operation_id"
            )
        expected_replacement_id = str(
            expected_replacement_id or ""
        ).strip().lower()
        if expected_replacement_id:
            expected_replacement_id = _strict_lower_hex(
                expected_replacement_id, 32, "expected_replacement_id"
            )
        if capture_id != expected_capture_id:
            raise RuntimeError(
                "active captureId changed from the frozen recovery target"
            )
        if capture_session_id != expected_capture_session_id:
            raise RuntimeError(
                "active capture session changed from the frozen recovery target"
            )
        bound = _active_bound_shader(context, current_event, stage)
        if bound["resourceId"] != expected_resource_id:
            raise RuntimeError(
                "current Shader changed from the frozen resource recovery target"
            )
        if bound["entryPoint"] != expected_entry_point:
            raise RuntimeError(
                "current Shader changed from the frozen entry-point recovery target"
            )
        queried = ipc.call(
            "get_shared_shader_replacements",
            {
                "expected_capture_id": capture_id,
                "expected_capture_session_id": capture_session_id,
            },
            timeout=60.0,
        )
        if (
            not isinstance(queried, dict)
            or queried.get("schema")
            != "renderdoc-ultra-shared-shader-replacement-list/v1"
            or queried.get("schemaVersion") != 1
            or queried.get("captureId") != capture_id
            or queried.get("captureSessionId") != capture_session_id
            or queried.get("originalShadersPreserved") is not True
            or queried.get("replayMutation") is not False
            or not isinstance(queried.get("replacements"), list)
            or isinstance(queried.get("count"), bool)
            or not isinstance(queried.get("count"), int)
            or queried.get("count") != len(queried["replacements"])
            or len(queried["replacements"]) > 256
            or isinstance(queried.get("mutationEpoch"), bool)
            or not isinstance(queried.get("mutationEpoch"), int)
            or queried.get("mutationEpoch") < 0
        ):
            raise RuntimeError("RenderDoc returned invalid shared-replacement metadata")
        records = []
        same_resource_records = []
        for index, item in enumerate(queried["replacements"]):
            if not isinstance(item, dict):
                raise RuntimeError("replacement[%d] is not an object" % index)
            replacement_id = str(item.get("replacementId", "") or "")
            operation_id = str(item.get("operationId", "") or "")
            workspace_id = str(item.get("workspaceId", "") or "")
            _strict_lower_hex(replacement_id, 32, "replacementId")
            _strict_lower_hex(operation_id, 32, "replacement.operationId")
            _strict_lower_hex(workspace_id, 32, "replacement.workspaceId")
            revision = item.get("revision")
            anchor_event = item.get("anchorEventId")
            original_resource = item.get("originalResourceId")
            target_resource = item.get("targetResourceId")
            if any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                for value in (
                    revision,
                    anchor_event,
                    original_resource,
                    target_resource,
                )
            ):
                raise RuntimeError("replacement[%d] numeric identity is invalid" % index)
            record_stage = normalize_stage(item.get("stage"))
            entry_point = item.get("entryPoint")
            api = item.get("api")
            encoding = item.get("encoding")
            if any(
                not isinstance(value, str) or not value or len(value) > 512
                for value in (entry_point, api, encoding)
            ) or not (
                (api == "d3d11" and encoding == "dxbc")
                or (api == "d3d12" and encoding in {"dxbc", "dxil"})
                or (api == "vulkan" and encoding == "spirv")
                or (api == "opengl" and encoding == "opengl-spirv")
            ):
                raise RuntimeError("replacement[%d] text identity is invalid" % index)
            original_raw = _strict_lower_hex(
                item.get("originalRawSha256"),
                64,
                "replacement.originalRawSha256",
            )
            candidate_raw = _strict_lower_hex(
                item.get("candidateRawSha256"),
                64,
                "replacement.candidateRawSha256",
            )
            physical_abi = _strict_lower_hex(
                item.get("physicalAbiSha256"),
                64,
                "replacement.physicalAbiSha256",
            )
            shader_family_id = _strict_lower_hex(
                item.get("shaderFamilyId"),
                64,
                "replacement.shaderFamilyId",
            )
            if any(
                (
                    item.get("resourceScoped") is not True,
                    item.get("eventScoped") is not False,
                    item.get("allOriginalResourceUsesAffected") is not True,
                    item.get("originalShaderPreserved") is not True,
                )
            ):
                raise RuntimeError("replacement[%d] scope evidence is invalid" % index)
            clean = dict(item)
            clean.update(
                {
                    "replacementId": replacement_id,
                    "operationId": operation_id,
                    "workspaceId": workspace_id,
                    "stage": record_stage,
                    "originalRawSha256": original_raw,
                    "candidateRawSha256": candidate_raw,
                    "physicalAbiSha256": physical_abi,
                    "shaderFamilyId": shader_family_id,
                }
            )
            if clean["originalResourceId"] == bound["resourceId"]:
                same_resource_records.append(clean)
            if (
                clean["stage"] == bound["stage"]
                and clean["originalResourceId"] == bound["resourceId"]
                and clean["originalRawSha256"] == expected_raw_sha256
                and str(clean.get("entryPoint", "") or "")
                == bound["entryPoint"]
                and bound["rawSha256"]
                in {clean["originalRawSha256"], clean["candidateRawSha256"]}
            ):
                records.append(clean)
        _require_same_active_capture(
            context, allow_missing_capture_id=uses_full_binding
        )
        if len(same_resource_records) != len(records):
            raise RuntimeError(
                "an active shared replacement owns the frozen Shader ResourceId "
                "but not every owner matches its stage, entry point and original bytes"
            )
        if not records:
            if same_resource_records:
                raise RuntimeError(
                    "an active shared replacement owns the frozen Shader ResourceId "
                    "but its stage, entry point, or original raw identity does not match"
                )
            if bound["rawSha256"] != expected_raw_sha256:
                raise RuntimeError(
                    "current Shader raw identity differs and no exact shared replacement owns it"
                )
            _require_same_active_project(active_status)
            workspace = expected_workspace
            if workspace.is_symlink() or not workspace.is_dir():
                raise RuntimeError(
                    "the active project has no deterministic workspace for the original Shader"
                )
            if not is_within(_active_project_workspace_root(active_status), workspace):
                raise RuntimeError("deterministic Shader workspace escaped the active project")
            status = _shader_workspace_service().status(str(workspace))
            shader = status.get("shader") if isinstance(status, dict) else None
            capture = status.get("capture") if isinstance(status, dict) else None
            if (
                status.get("schema")
                != "renderdoc-ultra-shader-workspace-status/v1"
                or not isinstance(shader, dict)
                or not isinstance(capture, dict)
                or Path(str(status.get("workspacePath", ""))).resolve()
                != workspace.resolve()
                or not _strict_lower_hex(
                    status.get("workspaceId"), 32, "workspace.workspaceId"
                )
                or status.get("workspaceId") != expected_workspace_id
                or capture.get("captureId") != capture_id
                or normalize_stage(shader.get("stage")) != bound["stage"]
                or shader.get("resourceId") != bound["resourceId"]
                or shader.get("rawSha256") != expected_raw_sha256
                or str(shader.get("entryPoint", "") or "") != bound["entryPoint"]
            ):
                raise RuntimeError(
                    "active deterministic workspace does not match the original Shader"
                )
            _require_same_active_project(active_status)
            _require_same_active_capture(
                context, allow_missing_capture_id=uses_full_binding
            )
            if uses_full_binding:
                current_binding = _active_capture_catalog_binding(context)
                if current_binding is None or (
                    _active_capture_catalog_binding_sha256(current_binding[2])
                    != expected_binding_sha256
                ):
                    raise RuntimeError(
                        "active full-catalog binding changed before Shader restore verification"
                    )
            target_profile = status.get("targetProfile")
            baseline = status.get("baseline")
            if not isinstance(target_profile, dict) or not isinstance(
                baseline, dict
            ):
                raise RuntimeError(
                    "active deterministic workspace omitted its physical Shader identity"
                )
            workspace_api = _active_api_key(capture.get("api"))
            workspace_encoding = str(
                target_profile.get("containerFormat", "") or ""
            ).strip().lower()
            if not (
                (workspace_api == "d3d11" and workspace_encoding == "dxbc")
                or (
                    workspace_api == "d3d12"
                    and workspace_encoding in {"dxbc", "dxil"}
                )
                or (
                    workspace_api == "vulkan"
                    and workspace_encoding == "spirv"
                )
                or (
                    workspace_api == "opengl"
                    and workspace_encoding == "opengl-spirv"
                )
            ):
                raise RuntimeError(
                    "active deterministic workspace API/encoding is unsupported"
                )
            workspace_abi_sha256 = _strict_lower_hex(
                baseline.get("abiSha256"),
                64,
                "workspace.baseline.abiSha256",
            )
            # Linearize the target-specific absence claim inside the same
            # Bridge serialization domain as every shared apply/rollback. An
            # owner that appears after the earlier inventory observation is a
            # hard conflict, never a no-op success.
            exact_restore = ipc.call(
                "restore_shader_candidate_shared_resource_exact",
                {
                    "expected_capture_id": capture_id,
                    "expected_capture_session_id": capture_session_id,
                    "workspace_id": status["workspaceId"],
                    "expected_event_id": expected_event_id,
                    "stage": bound["stage"],
                    "entry_point": bound["entryPoint"],
                    "expected_original_resource_id": bound["resourceId"],
                    "expected_original_sha256": expected_raw_sha256,
                },
                timeout=60.0,
            )
            if (
                not isinstance(exact_restore, dict)
                or exact_restore.get("schema")
                != "renderdoc-ultra-exact-shared-shader-restore/v1"
                or exact_restore.get("schemaVersion") != 1
                or exact_restore.get("captureId") != capture_id
                or exact_restore.get("captureSessionId") != capture_session_id
                or exact_restore.get("workspaceId") != status["workspaceId"]
                or exact_restore.get("eventId") != expected_event_id
                or normalize_stage(exact_restore.get("stage")) != bound["stage"]
                or exact_restore.get("entryPoint") != bound["entryPoint"]
                or exact_restore.get("originalResourceId") != bound["resourceId"]
                or exact_restore.get("originalRawSha256") != expected_raw_sha256
                or exact_restore.get("api") != workspace_api
                or exact_restore.get("encoding") != workspace_encoding
                or exact_restore.get("physicalAbiSha256")
                != workspace_abi_sha256
                or exact_restore.get("linearized") is not True
                or exact_restore.get("stateAtLinearizationVerified") is not True
                or exact_restore.get("originalShaderPreserved") is not True
                or exact_restore.get("state") != "absent"
                or exact_restore.get("restored") is not True
                or exact_restore.get("alreadyRestored") is not True
                or exact_restore.get("activeReplacementCountForResource") != 0
                or exact_restore.get("rollback") is not None
                or isinstance(exact_restore.get("mutationEpoch"), bool)
                or not isinstance(exact_restore.get("mutationEpoch"), int)
                or exact_restore.get("mutationEpoch") < queried["mutationEpoch"]
            ):
                raise RuntimeError(
                    "RenderDoc returned invalid exact Shader absence evidence"
                )
            refreshed_context = _require_same_active_capture(
                context, allow_missing_capture_id=uses_full_binding
            )
            refreshed_events = refreshed_context.get("events")
            if (
                not isinstance(refreshed_events, dict)
                or refreshed_events.get("currentEventId") != expected_event_id
                or _active_bound_shader(
                    refreshed_context, expected_event_id, bound["stage"]
                )
                != bound
            ):
                raise RuntimeError(
                    "current Shader binding changed during restore verification"
                )
            return _ok(
                {
                    "schema": "replaylab-current-shader-restore/v1",
                    "schemaVersion": 1,
                    "operation": "restore-current-shader",
                    "captureId": capture_id,
                    "captureSessionId": capture_session_id,
                    "eventId": current_event,
                    "stage": bound["stage"],
                    "entryPoint": bound["entryPoint"],
                    "workspacePath": str(workspace),
                    "workspaceId": status["workspaceId"],
                    "api": workspace_api,
                    "encoding": workspace_encoding,
                    "physicalAbiSha256": workspace_abi_sha256,
                    "recoveryOperationId": expected_operation_id,
                    "recoveryReplacementId": expected_replacement_id,
                    "originalResourceId": bound["resourceId"],
                    "originalRawSha256": expected_raw_sha256,
                    "restored": True,
                    "alreadyRestored": True,
                    "applied": False,
                    "stateAtLinearizationVerified": True,
                    "noActiveReplacementForTargetAtLinearization": True,
                    "mutationEpoch": exact_restore["mutationEpoch"],
                    "inventoryObservationEpoch": queried["mutationEpoch"],
                    "replacementQueryCount": queried["count"],
                    "matchingReplacementCount": 0,
                    "reason": "current Shader is already the exact captured original",
                    "policy": {
                        "fuzzyMatchUsed": False,
                        "hostMemoryRequired": False,
                        "captureSessionGuarded": True,
                        "originalShaderPreserved": True,
                    },
                }
            )
        if len(records) != 1:
            raise RuntimeError(
                "current Shader matched more than one shared replacement"
            )
        replacement = records[0]
        if not expected_operation_id:
            raise RuntimeError(
                "an active shared Shader replacement requires its frozen "
                "expected_operation_id"
            )
        if replacement["operationId"] != expected_operation_id:
            raise RuntimeError(
                "the active shared Shader replacement belongs to another operation"
            )
        if (
            expected_replacement_id
            and replacement["replacementId"] != expected_replacement_id
        ):
            raise RuntimeError(
                "the active shared Shader replacement ID changed before restore"
            )

        _require_same_active_project(active_status)
        workspace = expected_workspace
        if workspace.is_symlink() or not workspace.is_dir():
            raise RuntimeError(
                "the active project has no deterministic workspace for this replacement"
            )
        if not is_within(_active_project_workspace_root(active_status), workspace):
            raise RuntimeError("deterministic Shader workspace escaped the active project")
        status = _shader_workspace_service().status(str(workspace))
        shader = status.get("shader") if isinstance(status, dict) else None
        capture = status.get("capture") if isinstance(status, dict) else None
        if (
            status.get("schema")
            != "renderdoc-ultra-shader-workspace-status/v1"
            or not isinstance(shader, dict)
            or not isinstance(capture, dict)
            or Path(str(status.get("workspacePath", ""))).resolve()
            != workspace.resolve()
            or status.get("workspaceId") != replacement["workspaceId"]
            or status.get("workspaceId") != expected_workspace_id
            or capture.get("captureId") != capture_id
            or normalize_stage(shader.get("stage")) != bound["stage"]
            or shader.get("resourceId") != bound["resourceId"]
            or shader.get("rawSha256") != replacement["originalRawSha256"]
            or str(shader.get("entryPoint", "") or "") != bound["entryPoint"]
            or isinstance(status.get("revision"), bool)
            or not isinstance(status.get("revision"), int)
            or status["revision"] < replacement["revision"]
        ):
            raise RuntimeError(
                "active deterministic workspace does not match the live replacement"
            )
        _require_same_active_project(active_status)
        _require_same_active_capture(
            context, allow_missing_capture_id=uses_full_binding
        )
        if uses_full_binding:
            current_binding = _active_capture_catalog_binding(context)
            if current_binding is None or (
                _active_capture_catalog_binding_sha256(current_binding[2])
                != expected_binding_sha256
            ):
                raise RuntimeError(
                    "active full-catalog binding changed before Shader restore"
                )
        exact_restore = ipc.call(
            "restore_shader_candidate_shared_resource_exact",
            {
                "expected_capture_id": capture_id,
                "expected_capture_session_id": capture_session_id,
                "workspace_id": replacement["workspaceId"],
                "expected_event_id": expected_event_id,
                "stage": replacement["stage"],
                "entry_point": replacement["entryPoint"],
                "expected_original_resource_id": replacement[
                    "originalResourceId"
                ],
                "expected_original_sha256": replacement[
                    "originalRawSha256"
                ],
                "expected_replacement_id": replacement["replacementId"],
                "expected_operation_id": replacement["operationId"],
                "expected_revision": replacement["revision"],
                "expected_anchor_event_id": replacement["anchorEventId"],
                "expected_api": replacement["api"],
                "expected_encoding": replacement["encoding"],
                "expected_candidate_sha256": replacement[
                    "candidateRawSha256"
                ],
                "expected_physical_abi_sha256": replacement[
                    "physicalAbiSha256"
                ],
                "expected_shader_family_id": replacement["shaderFamilyId"],
                "expected_target_resource_id": replacement["targetResourceId"],
            },
            timeout=60.0,
        )
        result = (
            exact_restore.get("rollback")
            if isinstance(exact_restore, dict)
            else None
        )
        with _SHADER_OVERRIDE_GUARD:
            local_replacement = _SHARED_SHADER_REPLACEMENT_RECORDS.get(
                replacement["replacementId"]
            )
        expected_physical = {
            "operationId": replacement["operationId"],
            "stage": replacement["stage"],
            "entryPoint": replacement["entryPoint"],
            "api": replacement["api"],
            "encoding": replacement["encoding"],
            "originalResourceId": replacement["originalResourceId"],
            "originalRawSha256": replacement["originalRawSha256"],
            "candidateRawSha256": replacement["candidateRawSha256"],
            "physicalAbiSha256": replacement["physicalAbiSha256"],
            "shaderFamilyId": replacement["shaderFamilyId"],
            "targetResourceId": replacement["targetResourceId"],
        }
        if (
            not isinstance(exact_restore, dict)
            or exact_restore.get("schema")
            != "renderdoc-ultra-exact-shared-shader-restore/v1"
            or exact_restore.get("schemaVersion") != 1
            or exact_restore.get("captureId") != capture_id
            or exact_restore.get("captureSessionId") != capture_session_id
            or exact_restore.get("workspaceId") != replacement["workspaceId"]
            or exact_restore.get("eventId") != expected_event_id
            or normalize_stage(exact_restore.get("stage")) != bound["stage"]
            or exact_restore.get("entryPoint") != bound["entryPoint"]
            or exact_restore.get("originalResourceId") != bound["resourceId"]
            or exact_restore.get("originalRawSha256")
            != replacement["originalRawSha256"]
            or exact_restore.get("linearized") is not True
            or exact_restore.get("stateAtLinearizationVerified") is not True
            or exact_restore.get("originalShaderPreserved") is not True
            or exact_restore.get("state")
            not in {"rolled-back", "already-restored"}
            or exact_restore.get("restored") is not True
            or type(exact_restore.get("alreadyRestored")) is not bool
            or exact_restore.get("activeReplacementCountForResource") != 0
            or isinstance(exact_restore.get("mutationEpoch"), bool)
            or not isinstance(exact_restore.get("mutationEpoch"), int)
            or exact_restore.get("mutationEpoch") <= queried["mutationEpoch"]
            or not isinstance(result, dict)
            or result.get("replacementId") != replacement["replacementId"]
            or result.get("captureId") != capture_id
            or result.get("captureSessionId") != capture_session_id
            or result.get("workspaceId") != replacement["workspaceId"]
            or any(
                result.get(name) != value
                for name, value in expected_physical.items()
            )
            or not _valid_shader_rollback_physical_result(
                result,
                status=status,
                local=local_replacement,
                resource_scoped=True,
            )
        ):
            raise RuntimeError("exact shared-resource restore returned invalid evidence")
        if (
            (exact_restore["state"] == "already-restored")
            != exact_restore["alreadyRestored"]
            or result.get("alreadyRestored")
            is not exact_restore["alreadyRestored"]
        ):
            raise RuntimeError(
                "exact shared-resource restore state is internally inconsistent"
            )
        with _SHADER_OVERRIDE_GUARD:
            _SHARED_SHADER_REPLACEMENT_RECORDS.pop(
                replacement["replacementId"], None
            )
        rollback = {
            "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
            "operation": "rollback-shared-resource",
            "workspaceId": replacement["workspaceId"],
            "captureId": capture_id,
            "replacement": result,
            "applied": False,
        }
        _require_same_active_project(active_status)
        _require_same_active_capture(
            context, allow_missing_capture_id=uses_full_binding
        )
        return _ok(
            {
                "schema": "replaylab-current-shader-restore/v1",
                "schemaVersion": 1,
                "operation": "restore-current-shader",
                "captureId": capture_id,
                "captureSessionId": capture_session_id,
                "eventId": current_event,
                "stage": bound["stage"],
                "entryPoint": bound["entryPoint"],
                "workspacePath": str(workspace),
                "workspaceId": replacement["workspaceId"],
                "api": replacement["api"],
                "encoding": replacement["encoding"],
                "physicalAbiSha256": replacement["physicalAbiSha256"],
                "recoveryOperationId": expected_operation_id,
                "recoveryReplacementId": expected_replacement_id,
                "originalResourceId": bound["resourceId"],
                "originalRawSha256": replacement["originalRawSha256"],
                "replacementId": replacement["replacementId"],
                "operationId": replacement["operationId"],
                "restored": True,
                "alreadyRestored": exact_restore["alreadyRestored"],
                "applied": exact_restore["state"] == "rolled-back",
                "stateAtLinearizationVerified": True,
                "mutationEpoch": exact_restore["mutationEpoch"],
                "inventoryObservationEpoch": queried["mutationEpoch"],
                "rollback": rollback,
                "policy": {
                    "fuzzyMatchUsed": False,
                    "hostMemoryRequired": False,
                    "captureSessionGuarded": True,
                    "originalShaderPreserved": True,
                },
            }
        )
    except Exception as error:
        return _ok(
            {
                "schema": "replaylab-current-shader-restore-error/v1",
                "schemaVersion": 1,
                "operation": "restore-current-shader",
                "error": str(error),
                "type": type(error).__name__,
                "restored": False,
                "applied": False,
            }
        )


@mcp.tool()
def analyze_current_frame_flow(max_actions: int = 8000) -> str:
    """Analyze the current frame without project, UI, or replay mutation effects."""

    try:
        if (
            isinstance(max_actions, bool)
            or not isinstance(max_actions, int)
            or max_actions < 1
            or max_actions > 100000
        ):
            raise ValueError("max_actions must be between 1 and 100000")
        context = _active_live_context(allow_missing_capture_id=True)
        capture = context.get("capture", {})
        capture_session_id = _strict_lower_hex(
            capture.get("captureSessionId"), 32, "capture.captureSessionId"
        )
        capture_id = str(capture.get("captureId", "") or "").strip().lower()
        if capture_id:
            capture_id = _strict_lower_hex(capture_id, 64, "capture.captureId")
        params: dict[str, Any] = {
            "expected_capture_session_id": capture_session_id,
            "max_actions": max_actions,
        }
        if capture_id:
            params["expected_capture_id"] = capture_id
        inputs = ipc.call(
            "get_current_frame_flow_inputs", params, timeout=300.0
        )
        input_summary = inputs.get("summary") if isinstance(inputs, dict) else None
        if (
            not isinstance(inputs, dict)
            or inputs.get("schema") != "replaylab-current-frame-flow-inputs/v1"
            or inputs.get("schemaVersion") != 1
            or inputs.get("captureId", "") != capture_id
            or inputs.get("captureSessionId") != capture_session_id
            or not isinstance(inputs.get("actions"), list)
            or len(inputs["actions"]) > max_actions
            or not isinstance(inputs.get("pipelineStates"), (dict, list))
            or not isinstance(input_summary, dict)
            or not isinstance(input_summary.get("truncated"), bool)
        ):
            raise RuntimeError("RenderDoc returned invalid current-frame flow inputs")
        _require_same_active_capture(context, allow_missing_capture_id=True)
        all_actions = inputs["actions"]

        def _pipeline_prefix(event_ids: set[int]) -> Any:
            states = inputs["pipelineStates"]
            if isinstance(states, dict):
                return {
                    str(key): copy.deepcopy(value)
                    for key, value in states.items()
                    if (
                        (int(key) if str(key).isdigit() else -1)
                        in event_ids
                    )
                }
            return [
                copy.deepcopy(value)
                for value in states
                if isinstance(value, dict)
                and value.get("eventId") in event_ids
            ]

        def _response(action_count: int, host_truncated: bool) -> dict[str, Any]:
            selected_actions = copy.deepcopy(all_actions[:action_count])
            selected_events = {
                int(value.get("eventId", 0))
                for value in selected_actions
                if isinstance(value, dict)
                and isinstance(value.get("eventId"), int)
            }
            summary = copy.deepcopy(input_summary)
            if host_truncated:
                removed = len(all_actions) - action_count
                summary["truncated"] = True
                summary["actionCount"] = action_count
                summary["omittedActionCountLowerBound"] = max(
                    0,
                    int(summary.get("omittedActionCountLowerBound", 0) or 0),
                ) + removed
                reasons = summary.get("truncationReasons", [])
                reasons = list(reasons) if isinstance(reasons, list) else []
                if "host-result-byte-budget" not in reasons:
                    reasons.append("host-result-byte-budget")
                summary["truncationReasons"] = reasons
            flow = classify_passes(
                {"actions": selected_actions, "summary": summary},
                pipeline_states=_pipeline_prefix(selected_events),
                capture_id=capture_id,
            )
            flow["summary"]["resultSerializedBudgetBytes"] = (
                MAX_FRAME_FLOW_RESULT_BYTES
            )
            return {
                "schema": "replaylab-current-frame-flow-mcp/v1",
                "schemaVersion": 1,
                "operation": "analyze-current-frame-flow",
                "captureId": capture_id,
                "captureSessionId": capture_session_id,
                "flow": flow,
                "transport": {
                    "truncated": (
                        host_truncated or input_summary.get("truncated") is True
                    ),
                    "maximumUtf8Bytes": MAX_FRAME_FLOW_RESULT_BYTES,
                    "omittedActionCountLowerBound": max(
                        0,
                        int(input_summary.get(
                            "omittedActionCountLowerBound", 0) or 0),
                    ) + (len(all_actions) - action_count),
                },
                "applied": False,
                "policy": {
                    "readOnly": True,
                    "projectMutation": False,
                    "uiMutation": False,
                    "replayMutation": False,
                    "captureSessionGuarded": True,
                    "observedContributorDefault": False,
                },
            }

        action_count = len(all_actions)
        response = _response(action_count, False)
        encoded = _ok(response)
        while len(encoded.encode("utf-8")) > MAX_FRAME_FLOW_RESULT_BYTES:
            if action_count <= 0:
                raise RuntimeError(
                    "current-frame flow receipt exceeds the direct tool byte limit"
                )
            measured = len(encoded.encode("utf-8"))
            action_count = max(
                0,
                min(
                    action_count - 1,
                    int(
                        action_count
                        * float(MAX_FRAME_FLOW_RESULT_BYTES - 65536)
                        / float(measured)
                        * 0.90
                    ),
                ),
            )
            response = _response(action_count, True)
            encoded = _ok(response)
        for unused in range(6):
            measured = len(encoded.encode("utf-8"))
            summary = response["flow"]["summary"]
            transport = response["transport"]
            if (
                summary.get("resultSerializedBytes") == measured
                and transport.get("serializedUtf8Bytes") == measured
            ):
                break
            summary["resultSerializedBytes"] = measured
            transport["serializedUtf8Bytes"] = measured
            encoded = _ok(response)
        _require_same_active_capture(context, allow_missing_capture_id=True)
        if len(encoded.encode("utf-8")) > MAX_FRAME_FLOW_RESULT_BYTES:
            raise RuntimeError(
                "current-frame flow receipt exceeds the direct tool byte limit"
            )
        return encoded
    except Exception as error:
        return _ok(
            {
                "schema": "replaylab-current-frame-flow-mcp-error/v1",
                "schemaVersion": 1,
                "operation": "analyze-current-frame-flow",
                "error": str(error),
                "type": type(error).__name__,
                "applied": False,
                "policy": {
                    "readOnly": True,
                    "projectMutation": False,
                    "uiMutation": False,
                    "replayMutation": False,
                },
            }
        )


@mcp.tool()
def render_current_frame_flow_diagram(
    diagram_kind: str = "resource-flow",
    event_id: int = 0,
    max_actions: int = 8000,
    theme: str = THEME_EVIDENCE_NEUTRAL_V1,
) -> str:
    """Render a Host-validated current-frame diagram to a managed SVG artifact.

    ``resource-flow`` renders definite producer/consumer handoffs,
    ``pipeline-stage`` renders the bound/involved compact API stages for
    one exact EID, and ``pass-overview`` renders bounded pass representatives
    with physical GBuffer/DSV attachment bindings. The model cannot provide a path, CSS,
    Mermaid, HTML, URL, or custom palette; the fixed evidence-neutral theme is
    used for every artifact.
    """

    try:
        if not isinstance(diagram_kind, str) or diagram_kind not in DIAGRAM_KINDS:
            raise ValueError(
                "diagram_kind must be resource-flow, pipeline-stage, or pass-overview"
            )
        if theme != THEME_EVIDENCE_NEUTRAL_V1:
            raise ValueError("theme must be evidence-neutral-v1")
        if (
            isinstance(event_id, bool)
            or not isinstance(event_id, int)
            or event_id < 0
            or event_id > 0xFFFFFFFF
        ):
            raise ValueError("event_id must be a non-negative native EID")
        if diagram_kind == "pipeline-stage" and event_id < 1:
            raise ValueError("pipeline-stage requires a positive event_id")
        if diagram_kind != "pipeline-stage" and event_id != 0:
            raise ValueError("event_id is only valid for pipeline-stage")
        if (
            isinstance(max_actions, bool)
            or not isinstance(max_actions, int)
            or max_actions < 1
            or max_actions > 100000
        ):
            raise ValueError("max_actions must be between 1 and 100000")

        context = _active_live_context(allow_missing_capture_id=True)
        capture = context.get("capture", {})
        capture_session_id = _strict_lower_hex(
            capture.get("captureSessionId"), 32, "capture.captureSessionId"
        )
        capture_id = str(capture.get("captureId", "") or "").strip().lower()
        if capture_id:
            capture_id = _strict_lower_hex(capture_id, 64, "capture.captureId")
        raw_flow = analyze_current_frame_flow(max_actions=max_actions)
        try:
            flow_receipt = json.loads(raw_flow)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("current-frame flow returned invalid JSON") from exc
        if (
            not isinstance(flow_receipt, dict)
            or flow_receipt.get("schema")
            != "replaylab-current-frame-flow-mcp/v1"
            or flow_receipt.get("captureSessionId") != capture_session_id
            or flow_receipt.get("captureId", "") != capture_id
        ):
            raise RuntimeError("current-frame flow identity is invalid")
        graph = build_event_flow_graph(
            flow_receipt,
            diagram_kind=diagram_kind,
            capture_session_id=capture_session_id,
            capture_id=capture_id or None,
            event_id=event_id,
            capture_api=str(capture.get("api", "") or ""),
        )
        _require_same_active_capture(context, allow_missing_capture_id=True)
        receipt = publish_event_flow_artifact(graph)
        _require_same_active_capture(context, allow_missing_capture_id=True)
        receipt["diagramKind"] = diagram_kind
        receipt["eventId"] = event_id if diagram_kind == "pipeline-stage" else None
        # ``build_event_flow_graph`` hashes the strict canonical source before
        # rendering. The same identity is embedded in graph metadata and the
        # content-addressed manifest, so it cannot be added after publication.
        receipt["sourceFlowResultSha256"] = graph.snapshot_id
        return _ok(receipt)
    except Exception as error:
        return _ok(
            {
                "schema": "replaylab-event-flow-artifact-error/v1",
                "schemaVersion": 1,
                "operation": "render-event-flow-artifact",
                "error": str(error),
                "type": type(error).__name__,
                "applied": False,
                "policy": {
                    "hostManagedPath": True,
                    "projectMutation": False,
                    "uiMutation": False,
                    "replayMutation": False,
                },
            }
        )


@mcp.tool()
def classify_current_frame_passes(
    max_actions: int = 100000,
    publish_to_ui: bool = True,
    persist_project: bool = False,
) -> str:
    """Build and optionally publish the fixed foldable EID/pass tree.

    RenderDoc collects every action and actionable pipeline state in one replay
    transaction.  ExecuteIndirect/MultiAction parents become folded containers;
    concrete Draw/Dispatch children remain visible.  A bound active project may
    supply and receive exact pass-fingerprint knowledge.  AI is not involved in
    this tool and cannot alter the fixed category tree.
    """

    try:
        if (
            isinstance(max_actions, bool)
            or not isinstance(max_actions, int)
            or max_actions < 1
            or max_actions > 100000
        ):
            raise ValueError("max_actions must be between 1 and 100000")
        publish = _strict_bool(publish_to_ui, "publish_to_ui")
        persist = _strict_bool(persist_project, "persist_project")
        effective_max_actions = min(max_actions, MAX_PASS_PANEL_ACTIONS)
        context = _active_live_context(allow_missing_capture_id=True)
        capture_session_id = _strict_lower_hex(
            context.get("capture", {}).get("captureSessionId"),
            32,
            "capture.captureSessionId",
        )

        capture_binding_mode = _PASS_CAPTURE_BINDING_DISPLAY_SESSION
        capture_id = _display_session_capture_id(capture_session_id)
        prepared = None
        project_status = None
        project_prepared = False
        workspace_id = ""
        replay_project_path = ""
        shader_catalog = None
        project_knowledge = None
        knowledge_store = None
        knowledge_loaded = False
        persistence_reason = (
            "persist_project was not requested; no active project was prepared"
        )

        # Project preparation exports and imports a full capture catalog.  It is
        # therefore performed only for an explicit persistence request; opening
        # the foldable UI by itself remains a display-only operation.
        if persist:
            capture_binding_mode = _PASS_CAPTURE_BINDING_PROJECT_CATALOG
            raw_prepared = prepare_active_capture_project()
            try:
                prepared = json.loads(raw_prepared)
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "active capture preparation returned invalid JSON"
                ) from exc
            if not isinstance(prepared, dict):
                raise RuntimeError(
                    "active capture preparation returned a non-object"
                )
            if prepared.get("error"):
                raise RuntimeError(
                    "active capture preparation failed: %s" % prepared.get("error")
                )
            prepared_capture = prepared.get("capture")
            if (
                prepared.get("schema") != "replaylab-active-capture-prepare/v1"
                or prepared.get("schemaVersion") != 1
                or not isinstance(prepared_capture, dict)
                or prepared_capture.get("captureSessionId") != capture_session_id
            ):
                raise RuntimeError("active capture preparation identity changed")
            capture_id = _strict_lower_hex(
                prepared_capture.get("captureId"), 64, "prepared captureId"
            )
            project_prepared = True
            shader_catalog = _load_active_pass_catalog(prepared, capture_id)

            project_status = prepared.get("project")
            if not isinstance(project_status, dict):
                raise RuntimeError(
                    "active capture preparation omitted project status"
                )
            if not isinstance(project_status.get("scopeConfigured"), bool):
                raise RuntimeError("active project scope state is invalid")
            _require_same_active_project(project_status)
            workspace_id = str(project_status.get("workspaceId", "") or "")
            _strict_lower_hex(workspace_id, 32, "active project workspaceId")
            persistence_reason = "active project scope is pending"
            if project_status.get("scopeConfigured") is True:
                paths = project_status.get("paths")
                replay = project_status.get("replayProject")
                if (
                    not isinstance(paths, dict)
                    or not paths.get("replayProject")
                    or not isinstance(replay, dict)
                ):
                    raise RuntimeError(
                        "bound active project omitted replay-project identity"
                    )
                replay_project = Path(str(paths["replayProject"])).resolve()
                if (
                    replay_project.is_symlink()
                    or not replay_project.is_dir()
                    or Path(str(replay.get("projectPath", ""))).resolve()
                    != replay_project
                ):
                    raise RuntimeError(
                        "bound active replay-project path is invalid"
                    )
                replay_project_path = str(replay_project)
                knowledge_store = PassKnowledgeStore(replay_project)
                project_knowledge = knowledge_store.load()
                knowledge_loaded = True

        context = _canonical_active_capture_context(context, capture_id)
        _require_same_active_capture(context, allow_missing_capture_id=True)

        inputs = ipc.call(
            (
                "get_pass_classification_inputs"
                if persist
                else "get_current_frame_flow_inputs"
            ),
            {
                "expected_capture_id": capture_id,
                "expected_capture_session_id": capture_session_id,
                "max_actions": effective_max_actions,
            },
            timeout=300.0,
        )
        if (
            not isinstance(inputs, dict)
            or inputs.get("schema")
            != (
                "replaylab-pass-classification-inputs/v1"
                if persist
                else "replaylab-current-frame-flow-inputs/v1"
            )
            or inputs.get("schemaVersion") != 1
            or inputs.get("captureId") != capture_id
            or inputs.get("captureSessionId") != capture_session_id
            or not isinstance(inputs.get("actions"), list)
            or len(inputs["actions"]) > effective_max_actions
            or not isinstance(inputs.get("pipelineStates"), (dict, list))
        ):
            raise RuntimeError("RenderDoc returned invalid pass-classification inputs")
        _require_same_active_capture(context, allow_missing_capture_id=True)

        classification = classify_passes(
            {"actions": inputs["actions"], "summary": inputs.get("summary", {})},
            pipeline_states=inputs["pipelineStates"],
            shader_catalog=shader_catalog,
            project_knowledge=project_knowledge,
            capture_id=capture_id,
        )
        classification["summary"]["requestedMaxActions"] = max_actions
        classification["summary"]["effectiveMaxActions"] = effective_max_actions
        classification["summary"]["uiTreeActionLimitApplied"] = (
            effective_max_actions < max_actions
        )
        # Validate the exact display projection before either project
        # persistence or UI publication. This keeps the Host as the trust
        # boundary even when publish_to_ui is disabled.
        _pass_panel_classification_view(classification)
        _require_same_active_capture(context, allow_missing_capture_id=True)
        persistence = None
        if (
            persist
            and knowledge_store is not None
            and classification["summary"].get("sourceTruncated") is not True
        ):
            if project_status is None:
                raise RuntimeError("project persistence has no prepared project")
            _require_same_active_project(project_status)
            persistence = knowledge_store.save(
                classification, capture_id=capture_id
            )
            persistence_reason = "fixed pass knowledge persisted"
        elif persist and classification["summary"].get("sourceTruncated") is True:
            persistence_reason = "truncated pass classification was not persisted"
        elif persist and knowledge_store is None:
            persistence_reason = (
                "gameId/buildId must be configured before pass knowledge is shared"
            )

        panel = None
        if publish:
            panel = _publish_pass_classification(
                classification,
                context,
                capture_id=capture_id,
                capture_binding_mode=capture_binding_mode,
            )
        if project_status is not None:
            _require_same_active_project(project_status)
        _require_same_active_capture(context, allow_missing_capture_id=True)
        classification_id, fixed_sha256 = _cache_fixed_pass_classification(
            classification,
            capture_id=capture_id,
            capture_session_id=capture_session_id,
            capture_binding_mode=capture_binding_mode,
            active_workspace_id=workspace_id,
            replay_project_path=replay_project_path,
        )
        effect_mode = (
            "project-persisted"
            if persistence is not None
            else ("project-staged" if project_prepared else "display-only")
        )
        return _bounded_pass_tool_json(
            {
                "schema": "replaylab-current-frame-pass-classification-mcp/v1",
                "schemaVersion": 1,
                "operation": "classify-fixed",
                "captureId": capture_id,
                "captureSessionId": capture_session_id,
                "captureBindingMode": capture_binding_mode,
                "effectMode": effect_mode,
                "classificationId": classification_id,
                "fixedClassificationSha256": fixed_sha256,
                "classification": classification,
                "projectKnowledge": {
                    "activeWorkspaceId": workspace_id,
                    "projectPrepared": project_prepared,
                    "loaded": knowledge_loaded,
                    "persistRequested": persist,
                    "persisted": persistence is not None,
                    "reason": persistence_reason,
                    "result": persistence,
                },
                "panel": {
                    "publishRequested": publish,
                    "published": panel is not None,
                    "publicationMode": (
                        effect_mode if panel is not None else "not-published"
                    ),
                    "result": panel,
                },
                "applied": False,
                "policy": {
                    "classificationAuthority": "FixedProgram",
                    "aiProposalAuthority": "AIProvisional",
                    "aiCanChangeFixedTree": False,
                    "projectPreparationRequiresExplicitPersistence": True,
                    "projectMutationAuthorized": persist,
                    "projectMutationPerformed": project_prepared,
                    "uiMutationPerformed": panel is not None,
                    "replayMutation": False,
                    "captureSessionGuarded": True,
                    "executeIndirectParentsAreContainers": True,
                    "concreteIndirectChildrenRemainVisible": True,
                },
            }
        )
    except Exception as error:
        return _pass_classification_error("classify-fixed", error)


@mcp.tool()
def apply_current_frame_pass_ai_proposals(
    classification_id: str,
    expected_capture_id: str,
    expected_capture_session_id: str,
    proposals: list[dict[str, Any]],
    publish_to_ui: bool = True,
    persist_project: bool = False,
) -> str:
    """Attach review-only AI semantics to the current cached fixed pass result.

    The caller supplies no tree, offsets, registers, resources or executable
    state.  Proposals may only label event IDs already present in the latest
    server-owned fixed result.  They remain ``AIProvisional`` and never replace
    fixed categories or become physical replay authority.
    """

    try:
        publish = _strict_bool(publish_to_ui, "publish_to_ui")
        persist = _strict_bool(persist_project, "persist_project")
        clean_proposals = _strict_ai_pass_proposals(proposals)
        record = _cached_fixed_pass_classification(
            classification_id,
            expected_capture_id,
            expected_capture_session_id,
        )
        capture_binding_mode = str(record.get("captureBindingMode", "") or "")
        active_status = None
        if capture_binding_mode == _PASS_CAPTURE_BINDING_DISPLAY_SESSION:
            if persist:
                raise RuntimeError(
                    "display-only pass classification cannot be persisted; rerun "
                    "fixed classification with explicit persist_project=true"
                )
            context = _active_live_context(allow_missing_capture_id=True)
            capture_session_id = _strict_lower_hex(
                context.get("capture", {}).get("captureSessionId"),
                32,
                "capture.captureSessionId",
            )
            capture_id = _display_session_capture_id(capture_session_id)
        elif capture_binding_mode == _PASS_CAPTURE_BINDING_PROJECT_CATALOG:
            context, capture_id, capture_session_id, unused_full_binding = (
                _active_project_capture_identity()
            )
            active_status = _active_project_service().status()
            if active_status.get("workspaceId") != record["activeWorkspaceId"]:
                raise RuntimeError(
                    "active project changed after fixed pass classification"
                )
        else:
            raise RuntimeError("cached pass classification binding mode is invalid")
        if (
            capture_id != record["captureId"]
            or capture_session_id != record["captureSessionId"]
        ):
            raise RuntimeError("RenderDoc capture/session changed after classification")

        classification = apply_ai_semantic_proposals(
            record["classification"], clean_proposals
        )
        if _canonical_json_sha256(
            _fixed_pass_classification_view(classification),
            "AI-overlaid fixed pass classification",
        ) != record["fixedSha256"]:
            raise RuntimeError("AI proposals changed fixed pass classification data")
        _pass_panel_classification_view(classification)
        _require_same_active_capture(context, allow_missing_capture_id=True)

        persistence = None
        persistence_reason = (
            "persist_project was not requested; proposal remains display-only"
        )
        replay_project_path = str(record.get("replayProjectPath", "") or "")
        source_truncated = (
            classification.get("summary", {}).get("sourceTruncated") is True
        )
        if replay_project_path:
            if active_status is None:
                raise RuntimeError("project-backed classification lost project status")
            current_replay = active_status.get("replayProject")
            if (
                not isinstance(current_replay, dict)
                or Path(str(current_replay.get("projectPath", ""))).resolve()
                != Path(replay_project_path).resolve()
            ):
                raise RuntimeError("active replay project changed after classification")
            if persist and not source_truncated:
                _require_same_active_project(active_status)
                persistence = PassKnowledgeStore(replay_project_path).save(
                    classification, capture_id=capture_id
                )
                persistence_reason = "AIProvisional overlays persisted for review"
            elif persist:
                persistence_reason = (
                    "truncated pass classification overlays were not persisted"
                )
        elif persist:
            persistence_reason = (
                "gameId/buildId must be configured before pass overlays are shared"
            )

        panel = None
        if publish:
            panel = _publish_pass_classification(
                classification,
                context,
                capture_id=capture_id,
                capture_binding_mode=capture_binding_mode,
            )
        if active_status is not None:
            _require_same_active_project(active_status)
        _require_same_active_capture(
            context, allow_missing_capture_id=True
        )
        effect_mode = (
            "project-persisted" if persistence is not None else "display-only"
        )
        return _bounded_pass_tool_json(
            {
                "schema": "replaylab-current-frame-pass-classification-mcp/v1",
                "schemaVersion": 1,
                "operation": "attach-ai-provisional",
                "captureId": capture_id,
                "captureSessionId": capture_session_id,
                "captureBindingMode": capture_binding_mode,
                "effectMode": effect_mode,
                "classificationId": str(classification_id).strip().lower(),
                "fixedClassificationSha256": record["fixedSha256"],
                "classification": classification,
                "projectKnowledge": {
                    "activeWorkspaceId": record["activeWorkspaceId"],
                    "projectBackedClassification": (
                        capture_binding_mode
                        == _PASS_CAPTURE_BINDING_PROJECT_CATALOG
                    ),
                    "persistRequested": persist,
                    "persisted": persistence is not None,
                    "reason": persistence_reason,
                    "result": persistence,
                    "proposalAuthority": "AIProvisional",
                },
                "panel": {
                    "publishRequested": publish,
                    "published": panel is not None,
                    "publicationMode": (
                        effect_mode if panel is not None else "not-published"
                    ),
                    "result": panel,
                },
                "applied": False,
                "policy": {
                    "classificationAuthority": "FixedProgram",
                    "aiProposalAuthority": "AIProvisional",
                    "aiCanChangeFixedTree": False,
                    "aiCanChangeOffsetsRegistersOrResources": False,
                    "projectMutationAuthorized": persist,
                    "projectMutationPerformed": persistence is not None,
                    "uiMutationPerformed": panel is not None,
                    "replayMutation": False,
                    "captureSessionGuarded": True,
                },
            }
        )
    except Exception as error:
        return _pass_classification_error("attach-ai-provisional", error)


# ==================== REPLAY PROJECTS ====================

@mcp.tool()
def create_replay_project(
    project_path: str,
    game_id: str,
    build_id: str,
    name: str = "",
    platform: str = "",
    api_family: str = "",
) -> str:
    """Create a bounded project index for knowledge shared across captures.

    A replay project stores only content-bound source/alias/material knowledge.
    Capture sessions, replay handles, compiled candidates and ABI validation
    reports are deliberately never promoted into project authority.
    """

    try:
        return _ok(
            ReplayProjectService.create(
                project_path,
                name,
                game_id,
                build_id,
                platform,
                api_family,
            )
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def get_replay_project_status(project_path: str) -> str:
    """Validate and summarize a replay project without changing it."""

    try:
        return _ok(ReplayProjectService(project_path).status())
    except Exception as error:
        return _err(error)


@mcp.tool()
def import_replay_project_capture(project_path: str, catalog_path: str) -> str:
    """Import one strict capture catalog and return safe cross-frame matches.

    Only physically verified raw/code hashes enter the project. Matches are
    plans, never replay mutations or automatic shader/material applications.
    """

    try:
        return _ok(ReplayProjectService(project_path).import_capture(catalog_path))
    except Exception as error:
        return _err(error)


@mcp.tool()
def register_replay_project_workspace(
    project_path: str, workspace_path: str
) -> str:
    """Publish reviewed HLSL and its current compiled ABI candidate.

    This low-level explicit-project endpoint uses the same organize-only HMAC,
    source lease, current compiled-candidate/ABI, and exact persisted read-back
    gates as the active-project Shader child-Agent path. It never applies a
    replay replacement.
    """

    try:
        workspace_service = _shader_workspace_service()
        project = ReplayProjectService(project_path)
        return _ok(
            _register_reviewed_workspace_with_readback(
                project, workspace_service, workspace_path
            )
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def register_replay_project_alias_table(
    project_path: str, alias_table_path: str
) -> str:
    """Register a verified, capture-bound symbol alias export as candidates."""

    try:
        return _ok(
            ReplayProjectService(project_path).register_alias_table(alias_table_path)
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def save_replay_project_material_knowledge(
    project_path: str, session_id: str, parameter_ids: list[str]
) -> str:
    """Save selected typed values from a currently live material session.

    The old session and its override are not reused in another frame. A reuse
    plan exposes these values only after shader matching and requires a new
    live material layout/session validation before any future update.
    """

    try:
        session = _material_session_manager().status(session_id)
        return _ok(
            ReplayProjectService(project_path).save_material_knowledge(
                session, parameter_ids
            )
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def get_replay_project_reuse_plan(
    project_path: str, capture_id: str, catalog_sha256: str = ""
) -> str:
    """Return hash/ABI-bound source, alias and material candidates for a frame."""

    try:
        return _ok(
            ReplayProjectService(project_path).reuse_plan(
                capture_id, catalog_sha256
            )
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def seed_shader_workspace_from_replay_project(
    project_path: str, knowledge_id: str, workspace_path: str
) -> str:
    """Seed one new capture workspace with matched project HLSL only.

    The destination must be a freshly decompiled, unedited workspace. This
    writes source but never compiles or applies it; the normal compile plus
    live replay ABI validation gates remain mandatory.
    """

    try:
        service = _shader_workspace_service()
        before = service.status(workspace_path)
        result = ReplayProjectService(project_path).seed_workspace(
            knowledge_id, before
        )
        after = service.status(workspace_path)
        source = after.get("source") if isinstance(after, dict) else None
        if (
            not isinstance(source, dict)
            or source.get("currentSha256") != result.get("sourceSha256")
            or source.get("modifiedSinceDecompile") is not True
        ):
            raise RuntimeError(
                "seeded workspace did not revalidate as an edited source"
            )
        result["destinationStatus"] = after
        return _ok(result)
    except Exception as error:
        return _err(error)


# ==================== RESOURCE EXPORT ====================

def _resource_export_live_identity() -> tuple[dict[str, Any], str, str, str]:
    """Freeze the live replay transport identity for one export operation."""

    context = _active_live_context(allow_missing_capture_id=True)
    capture = context["capture"]
    capture_session_id = _strict_lower_hex(
        capture.get("captureSessionId"), 32, "capture.captureSessionId"
    )
    capture_id = str(capture.get("captureId", "") or "").strip()
    if capture_id:
        capture_id = _strict_lower_hex(capture_id, 64, "capture.captureId")
    api = str(capture.get("api", "") or "")
    return context, capture_id, capture_session_id, api


def _reconcile_resource_export_tool_postcondition(
    tool: str,
    arguments: Mapping[str, Any],
    expected_capture_session_id: str,
) -> dict[str, Any]:
    """Host-only durable recovery; never exposed through model MCP schemas."""

    return _RESOURCE_EXPORT_MANAGER.reconcile_tool_postcondition(
        tool,
        arguments,
        expected_capture_session_id=expected_capture_session_id,
    )


@mcp.tool()
def get_resource_export_capabilities() -> str:
    """Report fixed export targets, limits and optional provider availability.

    FBX and external validation are never reported as successful merely because
    an executable happens to be installed.  The portable fixed target is glTF
    2.0 plus the complete capture-evidence sidecar.
    """

    try:
        return _ok(_RESOURCE_EXPORT_MANAGER.capabilities())
    except Exception as error:
        return _err(error)


@mcp.tool()
def inspect_resource_export(
    event_id: int,
    instance: int = 0,
    view: int = 0,
) -> str:
    """Inspect one draw's export surface without writing files.

    The bridge returns all vertex inputs (including undecodable raw formats),
    indices/topology, Shader/reflection identities, texture view/sampler and
    color-space evidence, live CBuffer layouts/values, render state and known
    pass dependencies.  The live capture session is frozen across the call.
    """

    try:
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
            raise ValueError("event_id must be a positive integer")
        if isinstance(instance, bool) or not isinstance(instance, int) or instance < 0:
            raise ValueError("instance must be a non-negative integer")
        if isinstance(view, bool) or not isinstance(view, int) or view < 0:
            raise ValueError("view must be a non-negative integer")
        context, capture_id, capture_session_id, unused_api = _resource_export_live_identity()
        result = ipc.call(
            "inspect_resource_export",
            {
                "event_id": event_id,
                "instance": instance,
                "view": view,
                "expected_capture_id": capture_id,
                "expected_capture_session_id": capture_session_id,
            },
            timeout=300.0,
        )
        if not isinstance(result, dict):
            raise RuntimeError("RenderDoc returned an invalid resource-export inspection")
        if result.get("eventId") not in (None, event_id):
            raise RuntimeError("resource-export inspection changed event identity")
        if result.get("captureSessionId") not in (None, "", capture_session_id):
            raise RuntimeError("resource-export inspection changed capture session")
        _require_same_active_capture(context, allow_missing_capture_id=True)
        return _ok(result)
    except Exception as error:
        return _err(error)


@mcp.tool()
def prepare_resource_export(
    event_id: int,
    operation_id: str,
    asset_name: str = "RenderDocAsset",
    instance: int = 0,
    view: int = 0,
) -> str:
    """Capture one draw into a nonce-bound Host-managed evidence bundle.

    ``operation_id`` is a new 32-character lowercase hexadecimal nonce.  It is
    bound to the live capture/session, EID, instance, view and asset name.  No
    request-controlled filesystem path is accepted.  An exact retry is
    idempotent; reusing the nonce for a different request is rejected.
    """

    try:
        context, capture_id, capture_session_id, api = _resource_export_live_identity()
        prepared = _RESOURCE_EXPORT_MANAGER.prepare_ticket(
            operation_id=operation_id,
            event_id=event_id,
            instance=instance,
            view=view,
            asset_name=asset_name,
            capture_session_id=capture_session_id,
            capture_id=capture_id,
            api=api,
        )
        if not prepared.get("needsCapture"):
            _require_same_active_capture(context, allow_missing_capture_id=True)
            return _ok(
                {
                    "schema": "replaylab-resource-export-prepare/v1",
                    "schemaVersion": 1,
                    "idempotent": True,
                    "operation": prepared["operation"],
                    "applied": True,
                }
            )
        adopted = _RESOURCE_EXPORT_MANAGER.adopt_completed_capture(operation_id)
        if adopted is not None:
            _require_same_active_capture(context, allow_missing_capture_id=True)
            return _ok(
                {
                    "schema": "replaylab-resource-export-prepare/v1",
                    "schemaVersion": 1,
                    "idempotent": True,
                    "recoveredAfterLostResponse": True,
                    "operation": adopted,
                    "applied": True,
                }
            )
        ticket = prepared["ticket"]
        try:
            response = ipc.call(
                "capture_resource_export_bundle",
                {
                    "event_id": event_id,
                    "instance": instance,
                    "view": view,
                    "expected_capture_id": capture_id,
                    "expected_capture_session_id": capture_session_id,
                    "request_sha256": prepared["operation"]["requestSha256"],
                    "export_ticket": ticket,
                    "ticket_path": prepared["ticketPath"],
                    "ticket_sha256": prepared["ticketSha256"],
                },
                timeout=900.0,
            )
            _require_same_active_capture(context, allow_missing_capture_id=True)
            state = _RESOURCE_EXPORT_MANAGER.record_capture(operation_id, response)
            recovered_after_lost_response = False
        except Exception:
            state = _RESOURCE_EXPORT_MANAGER.adopt_completed_capture(operation_id)
            if state is None:
                raise
            recovered_after_lost_response = True
        _require_same_active_capture(context, allow_missing_capture_id=True)
        return _ok(
            {
                "schema": "replaylab-resource-export-prepare/v1",
                "schemaVersion": 1,
                "idempotent": recovered_after_lost_response,
                "recoveredAfterLostResponse": recovered_after_lost_response,
                "operation": state,
                "applied": True,
            }
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def finalize_resource_export(
    operation_id: str,
    expected_bundle_sha256: str,
    transfer_spec: dict[str, Any],
) -> str:
    """Convert an accepted capture bundle using an explicit semantic plan.

    The fixed conversion supports glTF 2.0, Unity URP and Blender scaffolds.
    ``transfer_spec`` owns the AI/user decisions: vertex semantic mapping,
    explicit basis/unit/UV conversion, texture color spaces and samplers,
    material versus system CBuffer values, surface mode and Shader strategy.
    """

    try:
        result = _RESOURCE_EXPORT_MANAGER.finalize(
            operation_id,
            expected_bundle_sha256,
            transfer_spec,
        )
        result["applied"] = True
        return _ok(result)
    except Exception as error:
        return _err(error)


@mcp.tool()
def get_resource_export_status(operation_id: str) -> str:
    """Return the durable Host receipt for one managed resource export."""

    try:
        return _ok(_RESOURCE_EXPORT_MANAGER.status(operation_id))
    except Exception as error:
        return _err(error)


@mcp.tool()
def run_resource_export_target_validation(operation_id: str) -> str:
    """Run fixed Host-managed validators for one immutable export package.

    The request supplies only the existing operation nonce. Khronos glTF,
    Blender headless and optional Unity provider paths are fixed when the MCP
    Host process starts. Logs, generated scenes and strict receipts are written
    beneath the operation's validation directory, never into the package.
    """

    try:
        result = _RESOURCE_EXPORT_MANAGER.run_target_validation(operation_id)
        result["applied"] = True
        return _ok(result)
    except Exception as error:
        return _err(error)


@mcp.tool()
def validate_resource_export(operation_id: str) -> str:
    """Re-hash a package and verify its cached target-validator receipt.

    This tool is read-only and never starts an external process. Target
    validation is complete only when the cached receipt matches the operation,
    immutable package and current provider hashes and every requested target
    actually passed.
    """

    try:
        return _ok(_RESOURCE_EXPORT_MANAGER.validate(operation_id))
    except Exception as error:
        return _err(error)


# ==================== ENTRYPOINT ====================

def main():
    emit_console_event(
        "host",
        "info",
        "ReplayLab MCP 标准输入服务正在启动。",
        phase="lifecycle",
        code="host.starting",
        context={"operation": "stdio"},
    )
    try:
        logger.info("Starting ReplayLab MCP Server (RenderDoc replay bridge)...")
        if ipc.is_bridge_alive():
            logger.info("RenderDoc bridge: CONNECTED")
        else:
            logger.warning("RenderDoc bridge: NOT responding. Start RenderDoc with a capture loaded.")
            emit_console_event(
                "bridge",
                "warning",
                "RenderDoc Bridge 当前未连接；请打开捕获并确认 ReplayLab 扩展已启用。",
                phase="connection",
                code="bridge.not-connected",
                context={"operation": "stdio-startup"},
            )
        # Persistent jobs belong to ReplayLabInternal, the same process used by
        # qrenderdoc source saves. A task-scoped MCP process must never resume
        # them or acquire their watcher leases.
        emit_console_event(
            "host",
            "info",
            "ReplayLab MCP 标准输入服务已就绪。",
            phase="lifecycle",
            code="host.ready",
            context={"operation": "stdio"},
        )
        mcp.run(transport="stdio")
    except Exception as error:
        emit_console_event(
            "host",
            "error",
            "ReplayLab MCP 标准输入服务失败：%s" % str(error)[:2048],
            phase="lifecycle",
            code="host.failed",
            context={"operation": "stdio"},
        )
        raise
    finally:
        emit_console_event(
            "host",
            "info",
            "ReplayLab MCP 标准输入服务已停止。",
            phase="lifecycle",
            code="host.stopped",
            context={"operation": "stdio"},
        )

if __name__ == "__main__":
    main()
