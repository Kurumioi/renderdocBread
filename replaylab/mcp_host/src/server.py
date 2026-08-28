"""
ReplayLab MCP host — capture analysis and symbol-resolution tools over
RenderDocUltra's built-in file-IPC bridge.

Architecture:
  [MCP client] <-stdio/HTTP-> [ReplayLab sidecar] <-file IPC-> [qrenderdoc built-in bridge]
"""

from __future__ import annotations
import atexit, copy, hashlib, json, logging, os, re, subprocess, tempfile, threading, time, shutil, uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Mapping
from .active_projects import ActiveReplayProjectService
from .console_events import emit_console_event, instrument_mcp_tool
from .ipc_client import IPCClient, INSTANCE_ROOT
from .internal_tool_service import (
    shader_live_edit_manager as _shader_live_edit_manager,
    shutdown_shader_live_edits as _shutdown_shader_live_edits,
)
from .material_sessions import MaterialSessionManager
from .pass_classification import (
    CLASSIFICATION_SCHEMA,
    CLASSIFICATION_VERSION,
    PassKnowledgeStore,
    apply_ai_semantic_proposals,
    classify_passes,
)
from .projects import ReplayProjectService
from .shader_workspace import (
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
                        return register(instrument_mcp_tool(function))

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


def _shader_err(error: Exception, phase: str) -> str:
    """Return backward-compatible error text plus a stable recovery category."""

    return json.dumps(
        {
            "error": str(error),
            "type": type(error).__name__,
            "diagnostic": classify_shader_failure(error, phase=phase),
        },
        ensure_ascii=True,
        default=str,
    )

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
MAX_SHADER_CATALOG_PROGRESS_BYTES = 16 * 1024
MAX_SHADER_CATALOG_PROGRESS_EVENTS = 1000000
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
    return (base / "RenderDocUltra" / "shader-tools.v1.json").resolve()


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
                _shader_tools_config_path(), _SHADER_TOOLCHAINS_CONFIG_PATH
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


def _shader_organization_manager() -> ShaderOrganizationManager:
    global _SHADER_ORGANIZATION_MANAGER
    with _SHADER_ORGANIZATION_MANAGER_GUARD:
        if _SHADER_ORGANIZATION_MANAGER is None:
            _SHADER_ORGANIZATION_MANAGER = ShaderOrganizationManager(
                _shader_workspace_service,
                _shader_organization_validate_candidate,
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
        result = dict(leased)
        result["projectSync"] = _best_effort_project_shader_sync(
            workspace_path,
            revision,
            candidate_raw_sha256,
            result,
        )
        return result

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


def _best_effort_project_shader_sync(
    workspace_path: str,
    revision: int,
    candidate_raw_sha256: str,
    applied: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist an exact successful apply without weakening the apply result."""

    try:
        from .project_restore import (
            automatic_project_sync_suppressed,
            sync_active_shader_workspace_after_apply,
        )

        if automatic_project_sync_suppressed():
            return {
                "attempted": False,
                "succeeded": False,
                "reason": "project restore owns the current persistence transaction",
            }
        replacement = applied.get("replacement")
        if applied.get("applied") is not True or not isinstance(replacement, Mapping):
            return {
                "attempted": False,
                "succeeded": False,
                "reason": "Shader was not applied",
            }
        replacement_id = str(replacement.get("replacementId", "") or "")
        synced = sync_active_shader_workspace_after_apply(
            workspace_path,
            expected_revision=revision,
            expected_candidate_raw_sha256=candidate_raw_sha256,
            replacement_id=replacement_id,
        )
        return {"attempted": True, "succeeded": True, "result": synced}
    except Exception as error:
        # Project/RDC persistence is downstream of a completed replay mutation.
        # A read-only RDC or inactive/pending project must not turn a successful
        # Shader apply into an apparent apply failure.
        return {
            "attempted": True,
            "succeeded": False,
            "error": str(error),
            "type": type(error).__name__,
        }


def _shader_live_edit_workspace_identity(workspace_path: str) -> dict[str, Any]:
    """Return the immutable capture/shader identity used for durable consent."""

    status = _shader_workspace_service().status(workspace_path)
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
    if kind == "dxbc" and api in {"d3d11", "direct3d11"}:
        return "supported-after-fresh-gates"
    if kind in {"dxbc", "dxil"} and api in {"d3d12", "direct3d12"}:
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
                    "apis": ["D3D11", "D3D12"],
                    "containerKinds": ["DXBC", "DXIL"],
                    "combinations": [
                        {"api": "D3D11", "containerKinds": ["DXBC"]},
                        {"api": "D3D12", "containerKinds": ["DXBC", "DXIL"]},
                    ],
                },
                "candidateScopeStatus": candidate_scope_status,
                "applyTool": "apply_shader_workspace_shared_resource",
                "reason": (
                    "Live apply supports D3D11/DXBC and sharing-preserving D3D12/"
                    "DXBC-or-DXIL. D3D12 uses a checked all-PSO rebuild with captured "
                    "root signatures; event-scoped shaders remain D3D11-only, while event-scoped "
                    "CBuffers support safe local D3D11 and D3D12 CBV bindings. "
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
    ABI.  The extra capture ID is bridge-only evidence that the reflection ran
    against the capture lifecycle which produced this workspace.
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
    validated = service.validate_candidate_reflection(workspace_path, reflected)

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
            response.get("candidateRawSha256")
            != request["binding"]["candidateRawSha256"],
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
    if (
        len(family_ids) != 1
        or None in family_ids
        or len(target_ids) != 1
        or None in target_ids
        or response.get("shaderFamilyId") not in family_ids
        or response.get("targetResourceId") not in target_ids
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
        "checkedDerivedPipelineRebuild": binding["api"] == "d3d12",
        "capturedRootSignaturesReused": binding["api"] == "d3d12",
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
    if (
        response["targetBuiltOnce"] is reused
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

@mcp.tool()
def get_shader_info(event_id: int, stage: str = "fragment") -> str:
    """Get shader information for a specific stage at an event.

    Returns shader reflection data, disassembly, bound resources, etc.

    Args:
        event_id: The event ID of the draw call.
        stage: Shader stage - "vertex", "fragment"/"pixel", "geometry", "compute", "hull"/"tess_ctrl", "domain"/"tess_eval".
    """
    return _call("get_shader_info", {"event_id": event_id, "stage": stage})

@mcp.tool()
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
    """Get all textures bound to a shader stage with auto-detected roles.

    For each texture slot, shows shader variable name, actual bound texture,
    and inferred role (albedo, normal, roughness, metallic, ao, emissive, etc.)

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

@mcp.tool()
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

@mcp.tool()
def export_drawcall(event_id: int, output_dir: str) -> str:
    """One-shot export of a draw call: shaders + textures + cbuffer values to disk.

    Exports shader disassembly, source code, all bound textures as PNG, and a summary JSON.

    Args:
        event_id: The event ID of the draw call to export.
        output_dir: Directory to save all exported files.
    """
    return _call("export_drawcall", {"event_id": event_id, "output_dir": output_dir}, timeout=60)


@mcp.tool()
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
        expected_sha256 = _file_sha256(alias_path)
        applied = ipc.call(
            "load_symbol_aliases",
            {
                "path": str(alias_path),
                "confirmed_only": True,
                "expected_sha256": expected_sha256,
            },
            timeout=60.0,
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
            DXBC/DXIL instruction/IR round trip only when the preferred lane is
            statically incomplete during planning. It does not cross formats
            after a selected high-level route fails at runtime. SPIR-V assembly
            is excluded from this automatic fallback until its workspace tools
            and validator are integrated. An explicit value never switches
            formats behind the caller.
    """
    try:
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
        representation = service.representation(workspace_path)
        return _shader_workspace_response(
            "recovery-candidate-attempt",
            {
                "sourceWorkspacePath": str(
                    Path(source_workspace_path).expanduser().resolve()
                ),
                "workspacePath": str(Path(workspace_path).expanduser().resolve()),
                "created": created,
                "representation": representation,
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
    decompiler_tool_id: str = "",
    compiler_tool_id: str = "",
    catalog_output_dir: str = "",
    editable_format: str = "",
) -> str:
    """Export, uniquely select, create, and represent one event/stage shader.

    The bridge export is restricted to exactly ``event_id`` and ``stage``.
    Preparation fails closed unless the resulting catalog has exactly one
    physically verified shader use for that pair. A valid configured text
    round trip is executable-editable. Empty format selection prefers HLSL,
    then a unique direct DXBC/DXIL assembly round trip only when the high-level
    route is statically incomplete during planning; an explicit format is never
    changed. A runtime failure in a selected high-level route does not currently
    trigger a cross-format retry. SPIR-V assembly is excluded from the automatic
    fallback until its workspace tools and validator are integrated. Assembly is
    instruction text, not reconstructed HLSL. If the chosen route is
    unavailable, the tool still
    publishes RenderDoc disassembly (explanatory-ir) or a hash-bound captured
    binary view (binary-only), never an unexplained blank editor. Only the
    executable-editable lane may later compile; nothing is applied here.

    Args:
        event_id: Draw/dispatch event containing the bound shader.
        stage: Shader stage, for example vertex, fragment/pixel, or compute.
        workspace_path: New workspace directory to create.
        decompiler_tool_id: Optional legacy registered decompiler tool ID. Leave
            both tool IDs empty for trusted automatic multi-hop routing.
        compiler_tool_id: Optional legacy registered compiler tool ID. Leave
            both tool IDs empty for trusted automatic multi-hop routing.
        catalog_output_dir: Optional export directory. If empty, a unique sibling
            directory is generated beside the workspace.
        editable_format: Explicit text target. Empty lets fixed program routing
            prefer HLSL/GLSL and then select a complete DXBC/DXIL native
            assembly/IR round trip when the high-level route is statically
            unavailable. Runtime route failures stay in the selected format.
            An explicit value never switches formats behind the caller.
    """
    try:
        event_value = int(event_id)
        if event_value <= 0:
            raise ValueError("event_id must be a positive integer")
        if not str(stage or "").strip():
            raise ValueError("stage is required")
        workspace = Path(workspace_path).expanduser().resolve()
        active_binding = _active_capture_catalog_binding()
        if active_binding is not None and catalog_output_dir:
            unused_context, active_status, unused_record, unused_catalog = active_binding
            if is_within(_active_project_workspace_root(active_status), workspace):
                raise RuntimeError(
                    "an active-project workspace cannot bypass its full catalog with "
                    "catalog_output_dir"
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
        service = _shader_workspace_service()
        created = service.create(
            catalog_path,
            selected.key.catalog_key,
            workspace,
            decompiler_tool_id or None,
            compiler_tool_id or None,
            editable_format=editable_format,
        )
        representation = service.representation(workspace)
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
        return _shader_workspace_response(
            "prepare",
            {
                "workspacePath": str(workspace),
                "catalogExport": exported,
                "selectedShader": selected.key.as_dict(),
                "created": created,
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
    decompile that would overwrite edited source is rejected by the workspace
    core.
    """
    try:
        result = _shader_workspace_service().decompile(workspace_path)
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
    """Compile edited HLSL into an auditable, unapplied shader candidate.

    Successful output is validated as exactly one DXBC/DXIL container, but that
    proves only container structure. The candidate always remains unapplied and
    requires a future event-scoped replay ABI validation step.
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
) -> str:
    """Migrate exact source bytes to the current fixed Shader toolchain.

    The source workspace is left unchanged and retained as the archive. The new
    workspace starts at revision zero with no imported candidate and may use
    only the same route IDs, input/output formats, and Shader stage as the old
    route plan. Both hashes are mandatory race guards. This operation never
    trusts or imports old candidate/revision/ABI evidence, claims semantic
    equivalence, validates ABI, or applies a Shader replacement.
    """

    try:
        result = _shader_workspace_service().migrate_toolchain(
            source_workspace_path,
            workspace_path,
            expected_route_plan_sha256=expected_route_plan_sha256,
            expected_source_sha256=expected_source_sha256,
            # This is process-owned singleton-host state, not an MCP argument.
            host_instance_id=os.environ.get("REPLAYLAB_HOST_INSTANCE_ID"),
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
        expected_capture_id = _strict_lower_hex(
            status.get("capture", {}).get("captureId"),
            64,
            "workspace capture.captureId",
        )
        live_before = _active_live_context()
        live_identity = _active_capture_identity(live_before)
        if live_identity[0] != expected_capture_id:
            raise ShaderOrganizationError(
                "active RenderDoc capture does not match the organization workspace"
            )
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
        live_after_textures = _active_live_context()
        if _active_capture_identity(live_after_textures) != live_identity:
            raise ShaderOrganizationError(
                "RenderDoc capture session changed while collecting organization textures"
            )
        pipeline = ipc.call(
            "get_pipeline_state", {"event_id": event_value}, timeout=60.0
        )
        live_after_pipeline = _active_live_context()
        if _active_capture_identity(live_after_pipeline) != live_identity:
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
            "captureId": live_identity[0],
            "captureSessionId": live_identity[1],
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
    creates one proposal HLSL, analysis Markdown, and Mermaid file below the
    workspace.  AI may edit only those returned proposal files.  Supplying an
    event adds exact bound-texture and pipeline facts after verifying that the
    event uses this physical Shader; zero keeps the transaction workspace-only.
    Nothing is applied to RenderDoc.
    """

    try:
        context = _shader_organization_semantic_context(workspace_path, event_id)
        result = _shader_organization_manager().begin(workspace_path, context)
        return _ok(result)
    except Exception as error:
        return _shader_err(error, "organization-begin")


def _best_effort_project_organization_sync(
    workspace_path: str, reviewed: Mapping[str, Any]
) -> dict[str, Any]:
    """Persist a promoted, ABI-reviewed HLSL source without applying it."""

    gate = reviewed.get("gate") if isinstance(reviewed, Mapping) else None
    if (
        not isinstance(gate, Mapping)
        or gate.get("organizedSourcePromoted") is not True
        or gate.get("safeToStartLiveEdit") is not True
    ):
        return {
            "attempted": False,
            "succeeded": False,
            "reason": "organized HLSL was not promoted by the review gate",
        }
    try:
        active = _active_project_service()
        active_status = active.status()
        with active.pin_active_project(
            str(active_status.get("workspaceId", "") or ""),
            str(active_status.get("projectRoot", "") or ""),
        ):
            _require_same_active_project(active_status)
            workspace = _require_managed_active_workspace(
                active_status, workspace_path
            )
            status = _shader_workspace_service().status(str(workspace))
            result = active.register_workspace(status)
            if not isinstance(result, dict) or result.get("applied") is not False:
                raise RuntimeError(
                    "active project workspace registration returned invalid policy evidence"
                )
            _require_same_active_project(active_status)
        return {"attempted": True, "succeeded": True, "result": result}
    except Exception as error:
        # The organization review and its compile/ABI evidence already
        # completed. A pending project, a non-managed workspace, or persistence
        # fault must be visible but cannot rewrite that physical review result.
        return {
            "attempted": True,
            "succeeded": False,
            "error": str(error),
            "type": type(error).__name__,
        }


@mcp.tool()
def review_shader_workspace_organization(
    workspace_path: str, organization_id: str
) -> str:
    """Compile/ABI-check a proposal and promote only exact executable code.

    This tool reads only the fixed proposal files created by
    begin_shader_workspace_organization.  It compiles and freshly validates the
    proposal, compares its executable ``codeSha256`` with the untouched HLSL
    baseline, and leaves it as ``source/shader.hlsl`` only on exact equality.
    A code mismatch is retained as explanatory provisional HLSL while the raw
    editable source is restored and revalidated.  No Shader replacement occurs.
    """

    try:
        result = dict(
            _shader_organization_manager().review(
                workspace_path, organization_id
            )
        )
        result["projectSync"] = _best_effort_project_organization_sync(
            workspace_path, result
        )
        return _ok(result)
    except Exception as error:
        return _shader_err(error, "organization-review")


@mcp.tool()
def validate_shader_workspace_candidate(
    workspace_path: str,
    expected_revision: int | None = None,
    expected_candidate_raw_sha256: str = "",
) -> str:
    """Reflect and strictly validate a supported D3D11/D3D12 candidate ABI.

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
                    "revision": status["revision"],
                    "candidateRawSha256": request["binding"][
                        "candidateRawSha256"
                    ],
                },
            }
        )
    except Exception as error:
        return _err(error)


@mcp.tool()
def apply_shader_workspace_candidate(workspace_path: str, event_id: int) -> str:
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
            workspace_path
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
            "candidateRawSha256": applied["candidateRawSha256"],
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
            workspace_path
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
                    "candidateRawSha256": override["candidateRawSha256"],
                    "shaderFamilyId": override.get("shaderFamilyId"),
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
        "originalResourceId": applied["originalResourceId"],
        "candidateRawSha256": applied["candidateRawSha256"],
        "abiLeaseRequestId": abi_lease["abiLeaseRequestId"],
        "abiLeaseReportSha256": abi_lease["abiLeaseReportSha256"],
        "abiLeaseCandidateRawSha256": abi_lease[
            "abiLeaseCandidateRawSha256"
        ],
        "abiLeaseRevision": abi_lease["abiLeaseRevision"],
        "physicalAbiSha256": applied["physicalAbiSha256"],
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
) -> dict[str, Any] | None:
    """Apply one later live-edit save with a single adjacent physical ABI gate.

    ``None`` means that no current initial lease exists and the caller must use
    the ordinary full validation path.  Once a lease exists, failures are not
    silently retried through another route: the last-good replacement remains
    active and a newer save is required by live-edit policy.
    """

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
        }
    )
    applied: Any = None
    try:
        applied = ipc.call(
            "apply_shader_candidate_shared_resource", apply_params, timeout=300.0
        )
        applied = _validated_shared_shader_apply_response(
            applied,
            status=status_now,
            request=request_now,
            anchor=anchor,
            report_sha256=lease["abiLeaseReportSha256"],
        )
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
) -> str:
    """Apply a candidate to every use of one captured D3D11/D3D12 Shader ResourceId.

    This sharing-preserving mode is the default for editor save/apply flows.
    The Host performs fresh workspace, capture, candidate, ABI and live-event
    validation, then the Bridge builds one target and calls RenderDoc's
    resource-scoped replacement. D3D12 requires the checked transactional path,
    which rebuilds every affected PSO with its captured root signature before
    committing any mapping. Distinct captured
    ResourceIds are never merged, even when their bytecode is identical. The
    original captured Shader is retained for rollback and is never freed. The
    optional expected revision/raw SHA-256 pair is used by save auto-apply to
    reject a newer candidate published between watcher polling and mutation.
    """
    try:
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
            }
        )
        applied = ipc.call(
            "apply_shader_candidate_shared_resource", apply_params, timeout=300.0
        )
        try:
            applied = _validated_shared_shader_apply_response(
                applied,
                status=status_now,
                request=request_now,
                anchor=anchor,
                report_sha256=report_sha256,
            )
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
        response = {
            "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
            "operation": "apply-shared-resource",
            "binding": request["binding"],
            "validation": validated,
            "replacement": applied,
            "scope": "captured-resource",
            "applied": True,
            "rollback": {
                "tool": "rollback_shader_workspace_shared_resource",
                "workspacePath": status_now["workspacePath"],
                "replacementId": applied["replacementId"],
            },
        }
        response["projectSync"] = _best_effort_project_shader_sync(
            workspace_path,
            int(request["binding"]["revision"]),
            str(request["binding"]["candidateRawSha256"]),
            response,
        )
        return _ok(response)
    except Exception as error:
        return _err(error)


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
                result.get("resourceScoped") is not True,
                result.get("eventScoped") is not False,
                result.get("cleared") is not True,
                result.get("originalShaderPreserved") is not True,
                not isinstance(result.get("targetFreed"), bool),
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
                result.get("cleared") is not True,
                not isinstance(result.get("targetFreed"), bool),
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
            for token in tokens:
                local = _SHADER_OVERRIDE_RECORDS.get(token)
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
                    item.get("cleared") is not True,
                    not isinstance(item.get("targetFreed"), bool),
                    item.get("originalShaderPreserved") is not True,
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


def _best_effort_project_material_sync(
    session_id: str, updated: Mapping[str, Any]
) -> dict[str, Any]:
    """Persist successfully changed typed leaves after live replay accepts them."""

    try:
        from .project_restore import (
            automatic_project_sync_suppressed,
            sync_active_material_session,
        )

        if automatic_project_sync_suppressed():
            return {
                "attempted": False,
                "succeeded": False,
                "reason": "project restore owns the current persistence transaction",
            }
        raw_parameter_ids = updated.get("updatedParameterIds")
        if isinstance(raw_parameter_ids, list):
            parameter_ids = [
                str(value or "") for value in raw_parameter_ids
                if str(value or "")
            ]
        else:
            parameter_id = str(updated.get("updatedParameterId", "") or "")
            parameter_ids = [parameter_id] if parameter_id else []
        if (
            updated.get("operation") not in ("update", "update-many")
            or not parameter_ids
            or len(parameter_ids) != len(set(parameter_ids))
        ):
            return {
                "attempted": False,
                "succeeded": False,
                "reason": "material parameters were not updated",
            }
        synced = sync_active_material_session(session_id, parameter_ids)
        return {"attempted": True, "succeeded": True, "result": synced}
    except Exception as error:
        # As with Shader apply, project/RDC persistence is downstream. Keep the
        # accepted live material edit and expose a bounded synchronization
        # warning rather than misreporting replay failure.
        return {
            "attempted": True,
            "succeeded": False,
            "error": str(error),
            "type": type(error).__name__,
        }


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
        response["projectSync"] = _best_effort_project_material_sync(
            session_id, updated
        )
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
        response["projectSync"] = _best_effort_project_material_sync(
            session_id, updated
        )
        return _ok(response)
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
        result = _shader_live_edit_manager().start(workspace_path, debounce_ms)
        return _shader_live_edit_response("start", result)
    except Exception as error:
        return _err(error)


@mcp.tool()
def poll_shader_workspace_live_edit(live_edit_id: str) -> str:
    """Read live-edit compile/apply state, rejection evidence and audit events."""

    try:
        result = _shader_live_edit_manager().poll(live_edit_id)
        return _shader_live_edit_response("poll", result)
    except Exception as error:
        return _err(error)


@mcp.tool()
def list_shader_workspace_live_edits() -> str:
    """List active sessions, durable authorisations and restart-resume results."""

    try:
        result = _shader_live_edit_manager().list()
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
        result = _shader_live_edit_manager().migrate_authorization(
            source_workspace_path,
            workspace_path,
            expected_source_sha256,
        )
        return _shader_live_edit_response("migrate-authorization", result)
    except Exception as error:
        return _err(error)


@mcp.tool()
def stop_shader_workspace_live_edit(live_edit_id: str) -> str:
    """Stop monitoring; an already applied replacement remains rollbackable."""

    try:
        result = _shader_live_edit_manager().stop(live_edit_id)
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


def _shader_catalog_progress_channel(capture_session_id: str) -> dict[str, str]:
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
    for _unused in range(8):
        token = uuid.uuid4().hex
        path = instance_dir / ("shader-catalog-progress-%s.json" % token)
        if not path.exists() and not path.is_symlink():
            return {
                "operationToken": token,
                "captureSessionId": session_id,
                "instanceId": instance_dir.name,
                "path": str(path),
            }
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
) -> None:
    """Remember one full-frame catalog for same-session Shader workspaces."""

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
    }
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
    selected = select_unique_shader_for_use(catalog_path, event_id, stage)
    _require_same_active_capture(context, allow_missing_capture_id=True)
    exported = {
        "schema": "renderdoc-ultra-capture-symbols/v1",
        "schemaVersion": 1,
        "captureId": record["captureId"],
        "catalogPath": str(catalog_path),
        "reusedActiveProjectCatalog": True,
        "exportPerformed": False,
    }
    return (
        exported,
        catalog_path,
        selected,
        _active_capture_catalog_binding_sha256(record),
    )


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


def _cache_fixed_pass_classification(
    classification: dict[str, Any],
    *,
    capture_id: str,
    capture_session_id: str,
    active_workspace_id: str,
    replay_project_path: str,
) -> tuple[str, str]:
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
            if not isinstance(item[field], str):
                raise ValueError("proposals[%d].%s must be text" % (index, field))
        result.append(copy.deepcopy(item))
    return result


def _publish_pass_classification(
    classification: dict[str, Any], expected_context: dict[str, Any]
) -> dict[str, Any]:
    capture_id, capture_session_id = _active_capture_identity(expected_context)
    _require_same_active_capture(expected_context, allow_missing_capture_id=True)
    result = ipc.call(
        "load_pass_classification",
        {
            "classification": classification,
            "expected_capture_session_id": capture_session_id,
        },
        timeout=60.0,
    )
    if (
        not isinstance(result, dict)
        or result.get("schema") != "replaylab-pass-panel-load/v1"
        or result.get("schemaVersion") != 1
        or result.get("captureId") != capture_id
        or result.get("captureSessionId") != capture_session_id
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
        resource_id = shader.get("resourceId", shader.get("shaderResourceId", 0))
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
    """Report the exact hash-bound full catalog for the current replay session."""

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
            }
        return _ok(
            {
                "schema": "replaylab-active-catalog-binding-status/v1",
                "schemaVersion": 1,
                **result,
                "applied": False,
                "policy": {
                    "fullFrameCatalogRequired": True,
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


def _prepare_active_capture_project(progress_token: str | None = None) -> str:
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
        exported = ipc.call(
            "export_shader_catalog",
            export_params,
            timeout=ACTIVE_CAPTURE_EXPORT_TIMEOUT_SECONDS,
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
            if project_status.get("scopeConfigured") is True:
                imported = service.import_capture(catalog_path, **expected_binding)
                staged = imported["stagedCatalog"]
                reuse_plan = imported["reusePlan"]
            else:
                staged = service.stage_capture_catalog(catalog_path, **expected_binding)
                reuse_plan = service.future_frame_plan(exported_capture_id)
            capture_id = str(staged.get("captureId", "") or "")
            if capture_id != exported_capture_id:
                raise RuntimeError("active project staged a different capture identity")
            _require_same_active_capture(context, allow_missing_capture_id=True)
            project_status = service.status()
            _remember_active_capture_catalog(project_status, context, staged)
            symbol_resolution = _active_symbol_resolution(
                service,
                project_status,
                context,
                Path(str(staged["catalogPath"])),
                str(staged["catalogSha256"]),
            )
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
        return _active_project_error("prepare-current-capture", error)
    finally:
        if (
            export_root is not None
            and export_root.exists()
            and export_root.name.startswith(".current-")
        ):
            shutil.rmtree(export_root)


@mcp.tool()
def prepare_active_capture_project() -> str:
    """Stage/import the full current capture and resolve verified asset symbols."""

    return _prepare_active_capture_project()


@mcp.tool()
def get_active_shader_workspace_path(
    event_id: int, stage: str = "fragment"
) -> str:
    """Return the deterministic managed path for the current Shader.

    The path is namespaced by the exact catalog revision and by a hash of the
    route catalog, tool registry, and automatic-format policy. This prevents a
    workspace produced under an older route/tool policy from being silently
    reused after configuration changes.
    """

    try:
        service = _active_project_service()
        project_status = service.ensure_pending()
        with service.pin_active_project(
            str(project_status.get("workspaceId", "") or ""),
            str(project_status.get("projectRoot", "") or ""),
        ):
            _require_same_active_project(project_status)
            context, capture_id, capture_session_id, uses_full_binding = (
                _active_project_capture_identity()
            )
            catalog_sha256, expected_binding_sha256 = (
                _active_project_catalog_revision_for_workspace(
                    context, capture_id, uses_full_binding
                )
            )
            route_binding_sha256 = _active_shader_route_binding_sha256()
            shader = _active_bound_shader(context, event_id, stage)
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
                "applied": False,
                "policy": dict(_ACTIVE_PROJECT_MCP_POLICY),
            }
        )
    except Exception as error:
        return _active_project_error("shader-workspace-path", error)


@mcp.tool()
def register_active_project_workspace(workspace_path: str) -> str:
    """Register validated HLSL/analysis in the active project without applying it."""

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
            status = _shader_workspace_service().status(str(workspace))
            if status.get("capture", {}).get("captureId") != capture_id:
                raise RuntimeError("workspace captureId differs from the current RenderDoc capture")
            result = active.register_workspace(status)
            if not isinstance(result, dict) or result.get("applied") is not False:
                raise RuntimeError(
                    "active project workspace registration returned invalid policy evidence"
                )
            _require_same_active_capture(
                context, allow_missing_capture_id=uses_full_binding
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
            plan = service.future_frame_plan(requested)
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
def restore_current_shader(stage: str = "fragment") -> str:
    """Restore the original captured Shader bound at the current event/stage.

    This works after an MCP host restart: the bridge reports only bounded
    replacement metadata for the exact capture lifecycle.  The host resolves
    the active project's deterministic workspace and verifies capture/session,
    stage, entry point, original ResourceId/raw hash and workspaceId before it
    invokes the existing rollback transaction.  No host-memory token or fuzzy
    bytecode match is trusted.
    """

    try:
        active = _active_project_service()
        active_status = active.status()
        context, capture_id, capture_session_id, uses_full_binding = (
            _active_project_capture_identity()
        )
        catalog_sha256, expected_binding_sha256 = (
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
        bound = _active_bound_shader(context, current_event, stage)
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
        ):
            raise RuntimeError("RenderDoc returned invalid shared-replacement metadata")
        records = []
        for index, item in enumerate(queried["replacements"]):
            if not isinstance(item, dict):
                raise RuntimeError("replacement[%d] is not an object" % index)
            replacement_id = str(item.get("replacementId", "") or "")
            workspace_id = str(item.get("workspaceId", "") or "")
            _strict_lower_hex(replacement_id, 32, "replacementId")
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
                not isinstance(value, str) or len(value) > 512
                for value in (entry_point, api, encoding)
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
                    "workspaceId": workspace_id,
                    "stage": record_stage,
                    "originalRawSha256": original_raw,
                    "candidateRawSha256": candidate_raw,
                }
            )
            if (
                clean["stage"] == bound["stage"]
                and clean["originalResourceId"] == bound["resourceId"]
                and str(clean.get("entryPoint", "") or "")
                == bound["entryPoint"]
                and bound["rawSha256"]
                in {clean["originalRawSha256"], clean["candidateRawSha256"]}
            ):
                records.append(clean)
        _require_same_active_capture(
            context, allow_missing_capture_id=uses_full_binding
        )
        if not records:
            return _ok(
                {
                    "schema": "replaylab-current-shader-restore/v1",
                    "schemaVersion": 1,
                    "operation": "restore-current-shader",
                    "captureId": capture_id,
                    "captureSessionId": capture_session_id,
                    "eventId": current_event,
                    "stage": bound["stage"],
                    "originalResourceId": bound["resourceId"],
                    "restored": False,
                    "applied": False,
                    "reason": "current Shader has no bridge-owned shared replacement",
                    "policy": {
                        "fuzzyMatchUsed": False,
                        "hostMemoryRequired": False,
                        "originalShaderPreserved": True,
                    },
                }
            )
        if len(records) != 1:
            raise RuntimeError(
                "current Shader matched more than one shared replacement"
            )
        replacement = records[0]

        _require_same_active_project(active_status)
        workspace = active.shader_workspace_path(
            capture_id=capture_id,
            catalog_sha256=catalog_sha256,
            stage=bound["stage"],
            shader_resource_id=bound["resourceId"],
            raw_sha256=replacement["originalRawSha256"],
            create=False,
        )
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
        raw_rollback = rollback_shader_workspace_shared_resource(
            str(workspace), replacement["replacementId"]
        )
        try:
            rollback = json.loads(raw_rollback)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("shared-resource rollback returned invalid JSON") from exc
        result = rollback.get("replacement") if isinstance(rollback, dict) else None
        if (
            not isinstance(rollback, dict)
            or rollback.get("error")
            or rollback.get("schema")
            != "renderdoc-ultra-shader-shared-resource-apply-mcp/v1"
            or rollback.get("operation") != "rollback-shared-resource"
            or rollback.get("workspaceId") != replacement["workspaceId"]
            or rollback.get("captureId") != capture_id
            or not isinstance(result, dict)
            or result.get("replacementId") != replacement["replacementId"]
            or result.get("captureId") != capture_id
            or result.get("captureSessionId") != capture_session_id
            or result.get("workspaceId") != replacement["workspaceId"]
            or result.get("originalResourceId") != bound["resourceId"]
            or result.get("cleared") is not True
            or result.get("originalShaderPreserved") is not True
        ):
            if isinstance(rollback, dict) and rollback.get("error"):
                raise RuntimeError(
                    "shared-resource rollback failed: %s" % rollback.get("error")
                )
            raise RuntimeError("shared-resource rollback returned invalid evidence")
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
                "workspacePath": str(workspace),
                "workspaceId": replacement["workspaceId"],
                "originalResourceId": bound["resourceId"],
                "replacementId": replacement["replacementId"],
                "restored": True,
                "applied": True,
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
def classify_current_frame_passes(
    max_actions: int = 100000,
    publish_to_ui: bool = True,
    persist_project: bool = True,
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
        context = _active_live_context(allow_missing_capture_id=True)
        capture_session_id = _strict_lower_hex(
            context.get("capture", {}).get("captureSessionId"),
            32,
            "capture.captureSessionId",
        )

        raw_prepared = prepare_active_capture_project()
        try:
            prepared = json.loads(raw_prepared)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("active capture preparation returned invalid JSON") from exc
        if not isinstance(prepared, dict):
            raise RuntimeError("active capture preparation returned a non-object")
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
        context = _canonical_active_capture_context(context, capture_id)
        _require_same_active_capture(context, allow_missing_capture_id=True)
        shader_catalog = _load_active_pass_catalog(prepared, capture_id)

        inputs = ipc.call(
            "get_pass_classification_inputs",
            {
                "expected_capture_id": capture_id,
                "expected_capture_session_id": capture_session_id,
                "max_actions": max_actions,
            },
            timeout=300.0,
        )
        if (
            not isinstance(inputs, dict)
            or inputs.get("schema")
            != "replaylab-pass-classification-inputs/v1"
            or inputs.get("schemaVersion") != 1
            or inputs.get("captureId") != capture_id
            or inputs.get("captureSessionId") != capture_session_id
            or not isinstance(inputs.get("actions"), list)
            or len(inputs["actions"]) > max_actions
            or not isinstance(inputs.get("pipelineStates"), (dict, list))
        ):
            raise RuntimeError("RenderDoc returned invalid pass-classification inputs")
        _require_same_active_capture(context, allow_missing_capture_id=True)

        project_status = prepared.get("project")
        if not isinstance(project_status, dict):
            raise RuntimeError("active capture preparation omitted project status")
        if not isinstance(project_status.get("scopeConfigured"), bool):
            raise RuntimeError("active project scope state is invalid")
        _require_same_active_project(project_status)
        workspace_id = str(project_status.get("workspaceId", "") or "")
        _strict_lower_hex(workspace_id, 32, "active project workspaceId")
        replay_project_path = ""
        project_knowledge = None
        knowledge_store = None
        knowledge_loaded = False
        persistence_reason = "active project scope is pending"
        if project_status.get("scopeConfigured") is True:
            paths = project_status.get("paths")
            replay = project_status.get("replayProject")
            if (
                not isinstance(paths, dict)
                or not paths.get("replayProject")
                or not isinstance(replay, dict)
            ):
                raise RuntimeError("bound active project omitted replay-project identity")
            replay_project = Path(str(paths["replayProject"])).resolve()
            if (
                replay_project.is_symlink()
                or not replay_project.is_dir()
                or Path(str(replay.get("projectPath", ""))).resolve()
                != replay_project
            ):
                raise RuntimeError("bound active replay-project path is invalid")
            replay_project_path = str(replay_project)
            knowledge_store = PassKnowledgeStore(replay_project)
            project_knowledge = knowledge_store.load()
            knowledge_loaded = True
            persistence_reason = "persist_project was disabled"

        classification = classify_passes(
            inputs["actions"],
            pipeline_states=inputs["pipelineStates"],
            shader_catalog=shader_catalog,
            project_knowledge=project_knowledge,
            capture_id=capture_id,
        )
        _require_same_active_capture(context, allow_missing_capture_id=True)
        persistence = None
        if persist and knowledge_store is not None:
            _require_same_active_project(project_status)
            persistence = knowledge_store.save(
                classification, capture_id=capture_id
            )
            persistence_reason = "fixed pass knowledge persisted"
        elif persist and knowledge_store is None:
            persistence_reason = (
                "gameId/buildId must be configured before pass knowledge is shared"
            )

        panel = None
        if publish:
            panel = _publish_pass_classification(classification, context)
        _require_same_active_project(project_status)
        _require_same_active_capture(context, allow_missing_capture_id=True)
        classification_id, fixed_sha256 = _cache_fixed_pass_classification(
            classification,
            capture_id=capture_id,
            capture_session_id=capture_session_id,
            active_workspace_id=workspace_id,
            replay_project_path=replay_project_path,
        )
        return _ok(
            {
                "schema": "replaylab-current-frame-pass-classification-mcp/v1",
                "schemaVersion": 1,
                "operation": "classify-fixed",
                "captureId": capture_id,
                "captureSessionId": capture_session_id,
                "classificationId": classification_id,
                "fixedClassificationSha256": fixed_sha256,
                "classification": classification,
                "projectKnowledge": {
                    "activeWorkspaceId": workspace_id,
                    "loaded": knowledge_loaded,
                    "persistRequested": persist,
                    "persisted": persistence is not None,
                    "reason": persistence_reason,
                    "result": persistence,
                },
                "panel": {
                    "publishRequested": publish,
                    "published": panel is not None,
                    "result": panel,
                },
                "applied": False,
                "policy": {
                    "classificationAuthority": "FixedProgram",
                    "aiProposalAuthority": "AIProvisional",
                    "aiCanChangeFixedTree": False,
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
    persist_project: bool = True,
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
        context, capture_id, capture_session_id, uses_full_binding = (
            _active_project_capture_identity()
        )
        if (
            capture_id != record["captureId"]
            or capture_session_id != record["captureSessionId"]
        ):
            raise RuntimeError("RenderDoc capture/session changed after classification")
        active_status = _active_project_service().status()
        if active_status.get("workspaceId") != record["activeWorkspaceId"]:
            raise RuntimeError("active project changed after fixed pass classification")

        classification = apply_ai_semantic_proposals(
            record["classification"], clean_proposals
        )
        if _canonical_json_sha256(
            _fixed_pass_classification_view(classification),
            "AI-overlaid fixed pass classification",
        ) != record["fixedSha256"]:
            raise RuntimeError("AI proposals changed fixed pass classification data")
        _require_same_active_capture(
            context, allow_missing_capture_id=uses_full_binding
        )

        persistence = None
        persistence_reason = "active project scope is pending"
        replay_project_path = str(record.get("replayProjectPath", "") or "")
        if replay_project_path:
            current_replay = active_status.get("replayProject")
            if (
                not isinstance(current_replay, dict)
                or Path(str(current_replay.get("projectPath", ""))).resolve()
                != Path(replay_project_path).resolve()
            ):
                raise RuntimeError("active replay project changed after classification")
            if persist:
                _require_same_active_project(active_status)
                persistence = PassKnowledgeStore(replay_project_path).save(
                    classification, capture_id=capture_id
                )
                persistence_reason = "AIProvisional overlays persisted for review"
            else:
                persistence_reason = "persist_project was disabled"
        elif persist:
            persistence_reason = (
                "gameId/buildId must be configured before pass overlays are shared"
            )

        panel = None
        if publish:
            panel = _publish_pass_classification(classification, context)
        _require_same_active_project(active_status)
        _require_same_active_capture(
            context, allow_missing_capture_id=uses_full_binding
        )
        return _ok(
            {
                "schema": "replaylab-current-frame-pass-classification-mcp/v1",
                "schemaVersion": 1,
                "operation": "attach-ai-provisional",
                "captureId": capture_id,
                "captureSessionId": capture_session_id,
                "classificationId": str(classification_id).strip().lower(),
                "fixedClassificationSha256": record["fixedSha256"],
                "classification": classification,
                "projectKnowledge": {
                    "activeWorkspaceId": record["activeWorkspaceId"],
                    "persistRequested": persist,
                    "persisted": persistence is not None,
                    "reason": persistence_reason,
                    "result": persistence,
                    "proposalAuthority": "AIProvisional",
                },
                "panel": {
                    "publishRequested": publish,
                    "published": panel is not None,
                    "result": panel,
                },
                "applied": False,
                "policy": {
                    "classificationAuthority": "FixedProgram",
                    "aiProposalAuthority": "AIProvisional",
                    "aiCanChangeFixedTree": False,
                    "aiCanChangeOffsetsRegistersOrResources": False,
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
    """Copy validated workspace HLSL into immutable project knowledge.

    The workspace service first validates all capture, shader, baseline and
    tool-route provenance. No compiled candidate or old ABI report is copied.
    """

    try:
        status = _shader_workspace_service().status(workspace_path)
        return _ok(ReplayProjectService(project_path).register_workspace(status))
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
        resume = _shader_live_edit_manager().resume_persistent()
        logger.info("Persistent shader live-edit resume: %s", resume)
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
