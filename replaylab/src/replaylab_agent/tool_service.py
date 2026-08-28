"""Transport-neutral RenderDoc tools owned by the internal AgentHost.

The AgentHost must not spawn ReplayLabMCP in order to call back into the
qrenderdoc process that launched it.  This module keeps the model-visible
schemas, per-invocation policy/audit state, and the shared bridge transport in
one process.  ReplayLabMCP remains an external adapter for IDEs and other
out-of-process clients.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import hashlib
import importlib
import json
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .bridge import BridgeClient, default_ipc_root
from .errors import ConfigurationError, PolicyError
from .events import TaskEventWriter
from .policy import ToolCallScope, WorkflowGuard


# These contracts already have one reviewed high-level implementation in the
# bundled workflow package.  AgentHost invokes those Python functions directly
# in-process; it never starts ReplayLabMCP for an internal model/API or Codex
# dynamicTools turn.  ReplayLabMCP remains only an ACP/external protocol
# adapter over the same contracts.
_DIRECT_WORKFLOW_TOOLS = frozenset(
    {
        "apply_current_frame_pass_ai_proposals",
        "apply_shader_workspace_candidate",
        "apply_shader_workspace_candidate_family",
        "apply_shader_workspace_shared_resource",
        "begin_shader_workspace_organization",
        "classify_current_frame_passes",
        "compile_shader_workspace",
        "decompile_shader_workspace",
        "get_active_shader_workspace_path",
        "get_replay_project_reuse_plan",
        "get_replay_project_status",
        "get_shader_toolchain_capabilities",
        "get_shader_workspace_representation",
        "get_shader_workspace_status",
        "list_shader_workspace_family_uses",
        "plan_shader_recovery",
        "plan_shader_toolchain_route",
        "prepare_active_capture_project",
        "prepare_shader_workspace",
        "promote_shader_workspace_explanation",
        "register_active_project_workspace",
        "review_shader_workspace_organization",
        "rollback_shader_workspace_override",
        "rollback_shader_workspace_override_family",
        "rollback_shader_workspace_shared_resource",
        "validate_shader_workspace_candidate",
    }
)
_DIRECT_WORKFLOW_RESULT_MAX_BYTES = 16 * 1024 * 1024
_DIRECT_WORKFLOW_LOCK = threading.Lock()


def _direct_workflow_module() -> Any:
    """Load the bundled workflow implementation without opening MCP transport."""

    try:
        module = importlib.import_module("src.server")
    except ImportError as exc:
        raise ConfigurationError(
            "the bundled ReplayLab workflow implementation is unavailable"
        ) from exc
    from . import __version__ as agent_version

    if str(getattr(module, "SERVICE_VERSION", "")) != agent_version:
        raise ConfigurationError(
            "the bundled ReplayLab workflow implementation version does not match AgentHost"
        )
    missing = [
        name
        for name in _DIRECT_WORKFLOW_TOOLS
        if not callable(getattr(module, name, None))
    ]
    if missing:
        raise ConfigurationError(
            "the bundled ReplayLab workflow contract is incomplete: "
            + ", ".join(sorted(missing))
        )
    return module


def _direct_workflow_function(name: str) -> Any:
    if name not in _DIRECT_WORKFLOW_TOOLS:
        raise PolicyError(f"tool '{name}' is not a direct workflow contract")
    return getattr(_direct_workflow_module(), name)


def _decode_direct_workflow_result(name: str, result: Any) -> dict[str, Any]:
    value = result
    if isinstance(value, str):
        encoded = value.encode("utf-8", errors="strict")
        if len(encoded) > _DIRECT_WORKFLOW_RESULT_MAX_BYTES:
            raise ConfigurationError(
                f"internal workflow tool '{name}' exceeded its result byte limit"
            )
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                f"internal workflow tool '{name}' returned invalid JSON"
            ) from exc
    if not isinstance(value, dict):
        raise ConfigurationError(
            f"internal workflow tool '{name}' returned no JSON object"
        )
    return value


def _invoke_direct_workflow(
    name: str,
    arguments: dict[str, Any],
    instance_id: str,
    capture_session_id: str,
) -> dict[str, Any]:
    """Run one shared workflow function against an explicitly pinned instance.

    The workflow implementation has process-owned Shader override, Pass and
    project state, so calls are serialized.  Its IPC client uses a ContextVar
    binding for the exact qrenderdoc instance and capture session; no mutable
    global client is swapped and every nested bridge request carries the frozen
    session guard.  The surrounding async replay lock additionally keeps the
    verify/call/verify transaction indivisible.
    """

    module = _direct_workflow_module()
    try:
        ipc_module = importlib.import_module("src.ipc_client")
        bind_call = getattr(ipc_module, "bind_direct_tool_call")
    except (ImportError, AttributeError) as exc:
        raise ConfigurationError(
            "the bundled ReplayLab workflow bridge client is unavailable"
        ) from exc
    function = getattr(module, name)
    with _DIRECT_WORKFLOW_LOCK:
        with bind_call(instance_id, capture_session_id):
            result = function(**dict(arguments))
    return _decode_direct_workflow_result(name, result)


@dataclass(slots=True)
class ToolAudit:
    """In-memory evidence used by the specialist acceptance gate."""

    started: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    evidence: list["ToolEvidenceRecord"] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ToolEvidenceRecord:
    """Bounded host-owned digest for context compaction and receipts."""

    tool_call_id: str
    tool: str
    arguments_sha256: str
    result_sha256: str
    outcome: Literal["completed", "failed"]


def evidence_digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8", errors="replace")
    except (TypeError, ValueError, OverflowError):
        encoded = str(value).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()


_DYNAMIC_CALL_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "replaylab_dynamic_tool_call_id", default=""
)


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """Task-local authority wrapped around the process-shared service."""

    allowed_tools: frozenset[str]
    instance_id: str
    scope: ToolCallScope
    writer: TaskEventWriter | None = None
    audit: ToolAudit | None = None
    plan_task_id: str = ""
    skill_name: str = ""
    workflow_guard: WorkflowGuard | None = None


class InternalToolService:
    """One AgentHost-owned bridge service shared by all specialists in a run."""

    def __init__(
        self,
        bridge: BridgeClient | None = None,
        *,
        catalog_root: Path | str | None = None,
    ) -> None:
        self.bridge = bridge or BridgeClient()
        self.catalog_root = Path(
            catalog_root or (default_ipc_root().parent / "internal-catalogs")
        ).resolve()
        # Replay state is mutable even for observational calls.  Keep each
        # verify/call/verify transaction indivisible across parallel specialists.
        self._replay_lock = asyncio.Lock()

    def invocation(
        self,
        allowed_tools: set[str] | frozenset[str],
        *,
        instance_id: str,
        scope: ToolCallScope,
        writer: TaskEventWriter | None = None,
        audit: ToolAudit | None = None,
        plan_task_id: str = "",
        skill_name: str = "",
        workflow_guard: WorkflowGuard | None = None,
    ) -> ToolInvocation:
        allowed = frozenset(str(name).strip() for name in allowed_tools if str(name).strip())
        if not allowed:
            raise ConfigurationError("an Agent skill must grant at least one internal RenderDoc tool")
        unknown = allowed - available_internal_tools()
        if unknown:
            raise ConfigurationError(
                "internal RenderDoc tool contract is missing: " + ", ".join(sorted(unknown))
            )
        pinned = _validate_instance_id(instance_id)
        return ToolInvocation(
            allowed_tools=allowed,
            instance_id=pinned,
            scope=scope,
            writer=writer,
            audit=audit,
            plan_task_id=plan_task_id,
            skill_name=skill_name,
            workflow_guard=workflow_guard,
        )

    async def call(
        self,
        invocation: ToolInvocation,
        name: str,
        arguments: dict[str, Any],
    ) -> Any:
        """Execute one granted tool directly through the bundled bridge."""

        if name not in invocation.allowed_tools:
            raise PolicyError(f"tool '{name}' is outside the active skill grant")
        if name not in available_internal_tools():
            raise PolicyError(f"tool '{name}' is not an internal RenderDoc tool")
        if not isinstance(arguments, dict):
            raise PolicyError("RenderDoc tool arguments must be an object")
        invocation.scope.validate(name, arguments)
        if invocation.workflow_guard is not None:
            invocation.workflow_guard.check(name, arguments)

        audit = invocation.audit
        if audit is not None:
            audit.started.append(name)
        supplied_call_id = _DYNAMIC_CALL_ID.get().strip()
        tool_call_id = (supplied_call_id or uuid.uuid4().hex)[:256]
        arguments_sha256 = evidence_digest(arguments)
        activity_id = (
            f"{invocation.plan_task_id or invocation.skill_name or 'tool'}:"
            f"{name}:{uuid.uuid4().hex[:12]}"
        )[:512]
        if invocation.writer:
            data: dict[str, Any] = {
                "argumentNames": sorted(str(key) for key in arguments),
                "activityId": activity_id,
            }
            if invocation.plan_task_id:
                data["planTaskId"] = invocation.plan_task_id
            if invocation.skill_name:
                data["skill"] = invocation.skill_name
            invocation.writer.emit("tool-started", tool=name, data=data)

        try:
            async with self._replay_lock:
                await self._verify_live_scope(invocation)
                result = await self._execute(invocation, name, arguments)
                if _tool_result_error(result):
                    raise ConfigurationError(
                        f"internal RenderDoc tool '{name}' returned an error envelope"
                    )
                await self._verify_live_scope(invocation)
                if invocation.workflow_guard is not None:
                    invocation.workflow_guard.record_result(name, arguments, result)
        except Exception as exc:
            if invocation.workflow_guard is not None and not isinstance(exc, PolicyError):
                invocation.workflow_guard.record_failure(name)
            if audit is not None:
                audit.failed.append(name)
                audit.evidence.append(
                    ToolEvidenceRecord(
                        tool_call_id=tool_call_id,
                        tool=name,
                        arguments_sha256=arguments_sha256,
                        result_sha256=evidence_digest(type(exc).__name__),
                        outcome="failed",
                    )
                )
            if invocation.writer:
                data = {
                    "status": "failed",
                    "errorType": type(exc).__name__,
                    "activityId": activity_id,
                }
                if invocation.plan_task_id:
                    data["planTaskId"] = invocation.plan_task_id
                if invocation.skill_name:
                    data["skill"] = invocation.skill_name
                invocation.writer.emit(
                    "tool-completed", tool=name, summary="tool failed", data=data
                )
            raise

        if audit is not None:
            audit.completed.append(name)
            audit.evidence.append(
                ToolEvidenceRecord(
                    tool_call_id=tool_call_id,
                    tool=name,
                    arguments_sha256=arguments_sha256,
                    result_sha256=evidence_digest(result),
                    outcome="completed",
                )
            )
        if invocation.writer:
            data = {"status": "completed", "activityId": activity_id}
            if invocation.plan_task_id:
                data["planTaskId"] = invocation.plan_task_id
            if invocation.skill_name:
                data["skill"] = invocation.skill_name
            invocation.writer.emit("tool-completed", tool=name, data=data)
        return result

    async def _verify_live_scope(self, invocation: ToolInvocation) -> None:
        if not invocation.scope.capture_session_id:
            return
        context = await self._bridge_call(
            invocation, "get_current_renderdoc_context", {}, timeout=30.0
        )
        if not isinstance(context, dict):
            raise ConfigurationError("qrenderdoc capture verification returned no JSON object")
        invocation.scope.validate_live_context(context)

    async def _execute(
        self, invocation: ToolInvocation, name: str, arguments: dict[str, Any]
    ) -> Any:
        if name in _DIRECT_WORKFLOW_TOOLS:
            return await self._direct_workflow_call(invocation, name, arguments)

        # High-level calls are bundled inside qrenderdoc as one replay-thread
        # request.  The model never orchestrates their low-level subcalls and
        # shader disassembly is not repeated.
        if name in {"inspect_event", "analyze_pixel", "inspect_shader"}:
            timeout = 120.0 if name in {"analyze_pixel", "inspect_shader"} else 60.0
            return await self._bridge_call(
                invocation, name, dict(arguments), timeout=timeout
            )

        method, params, timeout = _bridge_request(name, arguments)
        return await self._bridge_call(invocation, method, params, timeout=timeout)

    async def _direct_workflow_call(
        self, invocation: ToolInvocation, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        worker = asyncio.create_task(
            asyncio.to_thread(
                _invoke_direct_workflow,
                name,
                dict(arguments),
                invocation.instance_id,
                invocation.scope.capture_session_id,
            )
        )
        # A cancelled asyncio.to_thread task keeps running in its OS thread.
        # Retain the surrounding replay lock until the direct workflow has
        # actually stopped, exactly like the native bridge path below.
        cancel_requested = False
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancel_requested = True
            except Exception:
                break
        try:
            result = worker.result()
        except Exception:
            if cancel_requested:
                raise asyncio.CancelledError
            raise
        if cancel_requested:
            raise asyncio.CancelledError
        return result

    async def _bridge_call(
        self,
        invocation: ToolInvocation,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 60.0,
    ) -> Any:
        payload = dict(params)
        if invocation.scope.capture_session_id:
            # The in-process bridge checks this immediately before dispatch.
            # The surrounding transaction also verifies before and after so a
            # capture switch can never silently publish evidence for another RDC.
            payload["__replaylab_expected_capture_session_id"] = (
                invocation.scope.capture_session_id
            )
        worker = asyncio.create_task(
            asyncio.to_thread(
                self.bridge.call,
                method,
                payload,
                instance_id=invocation.instance_id,
                timeout=timeout,
            )
        )
        # Cancelling asyncio.to_thread does not stop its OS thread.  Keep the
        # replay lock owned until the bridge call has actually terminated;
        # otherwise a cancelled task could overlap a new replay transaction.
        cancel_requested = False
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancel_requested = True
            except Exception:
                break
        try:
            result = worker.result()
        except Exception:
            if cancel_requested:
                raise asyncio.CancelledError
            raise
        if cancel_requested:
            raise asyncio.CancelledError
        return result

    async def export_shader_catalog(
        self,
        *,
        instance_id: str,
        capture_session_id: str,
        output_dir: str,
        event_ids: list[int],
        allow_full_capture: bool = False,
    ) -> Any:
        """Run the internal catalog primitive with an explicit bounded scope.

        Empty event lists are rejected unless the caller explicitly selects a
        full-capture job.  The current bridge implementation is synchronous;
        callers must not use ``allow_full_capture`` from an interactive/open
        path until the chunked operation API is available.
        """

        pinned_instance = _validate_instance_id(instance_id)
        session = str(capture_session_id or "").strip().lower()
        if len(session) != 32 or any(character not in "0123456789abcdef" for character in session):
            raise PolicyError("ExportShaderCatalog requires an exact capture session identity")
        if not event_ids and not allow_full_capture:
            raise PolicyError(
                "ExportShaderCatalog requires explicit event IDs; full capture must be explicit"
            )
        if allow_full_capture:
            raise ConfigurationError(
                "full-capture Shader catalog export requires the chunked background job service"
            )
        if not isinstance(event_ids, list):
            raise PolicyError("ExportShaderCatalog event IDs must be a list")
        if len(event_ids) > 64:
            raise PolicyError("ExportShaderCatalog accepts at most 64 explicit events per call")
        bounded_events: list[int] = []
        for value in event_ids:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise PolicyError("ExportShaderCatalog event IDs must be positive integers")
            bounded_events.append(value)
        if len(set(bounded_events)) != len(bounded_events):
            raise PolicyError("ExportShaderCatalog event IDs must be unique")

        destination = Path(str(output_dir or "")).resolve()
        try:
            destination.relative_to(self.catalog_root)
        except ValueError as exc:
            raise PolicyError(
                "ExportShaderCatalog output must stay inside the AgentHost catalog root"
            ) from exc

        pinned = ToolInvocation(
            allowed_tools=frozenset(),
            instance_id=pinned_instance,
            scope=ToolCallScope(capture_session_id=session),
        )
        async with self._replay_lock:
            await self._verify_live_scope(pinned)
            result = await self._bridge_call(
                pinned,
                "export_shader_catalog",
                {"output_dir": str(destination), "event_ids": bounded_events},
                timeout=120.0,
            )
            if _tool_result_error(result):
                raise ConfigurationError("ExportShaderCatalog returned an error envelope")
            await self._verify_live_scope(pinned)
            return result


def _validate_instance_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigurationError("internal RenderDoc tools require a pinned qrenderdoc instance")
    pinned = value.strip()
    if not pinned or pinned != value or "\0" in pinned or len(pinned) > 512:
        raise ConfigurationError("internal RenderDoc tools require a pinned qrenderdoc instance")
    return pinned


def _tool_result_error(result: Any) -> bool:
    if isinstance(result, dict):
        return result.get("isError") is True or result.get("error") not in (
            None,
            "",
            False,
            {},
        )
    return False


def _bridge_request(
    name: str, arguments: dict[str, Any]
) -> tuple[str, dict[str, Any], float]:
    args = dict(arguments)
    if name == "get_draw_calls":
        return "get_draw_calls", args, 60.0
    if name in {
        "get_frame_summary",
        "get_draw_call_details",
        "get_pipeline_state",
        "get_shader_info",
        "reverse_shader",
        "get_bound_textures",
        "get_textures",
        "get_buffers",
        "get_resources",
        "get_texture_info",
        "get_texture_minmax",
        "inspect_pixel_object",
        "enumerate_counters",
        "get_debug_messages",
    }:
        timeout = 120.0 if name in {"reverse_shader", "inspect_pixel_object"} else 60.0
        return name, args, timeout
    if name == "find_draws_by_shader":
        return "find_by_shader", args, 60.0
    if name == "find_draws_by_texture":
        return "find_by_texture", args, 60.0
    if name == "find_draws_by_resource":
        return "find_by_resource", args, 60.0
    if name == "fetch_counters":
        raw = str(args.get("counter_ids", ""))
        args = {"counter_ids": [int(value.strip()) for value in raw.split(",") if value.strip()]}
        return "fetch_counters", args, 120.0
    if name == "get_action_timings":
        raw = str(args.pop("event_ids", ""))
        if raw:
            args["event_ids"] = [int(value.strip()) for value in raw.split(",") if value.strip()]
        return "get_action_timings", args, 300.0
    raise PolicyError(f"tool '{name}' has no internal bridge route")


def _direct_workflow_functions(
    service: InternalToolService, invocation: ToolInvocation
) -> tuple[Any, ...]:
    """Build async wrappers while retaining the canonical typed signatures."""

    functions: list[Any] = []
    for name in sorted(invocation.allowed_tools & _DIRECT_WORKFLOW_TOOLS):
        original = _direct_workflow_function(name)

        def create(function_name: str, schema_source: Any) -> Any:
            @functools.wraps(schema_source)
            async def invoke(*args: Any, **kwargs: Any) -> Any:
                if args:
                    raise PolicyError(
                        f"internal workflow tool '{function_name}' accepts keyword arguments only"
                    )
                return await service.call(invocation, function_name, dict(kwargs))

            return invoke

        functions.append(create(name, original))
    return tuple(functions)


def create_internal_tools(
    service: InternalToolService, invocation: ToolInvocation
) -> dict[str, Any]:
    """Create one schema inventory backed by the shared internal service."""

    try:
        from pydantic_ai.tools import Tool
    except ImportError as exc:
        raise ConfigurationError(
            "model Agent support is not installed; install replaylab-agenthost[model]"
        ) from exc

    async def inspect_event(event_id: int) -> Any:
        """Inspect one exact RenderDoc event and return draw plus pipeline evidence.

        Args:
            event_id: Frozen positive RenderDoc event ID.
        """

        return await service.call(invocation, "inspect_event", {"event_id": event_id})

    async def analyze_pixel(
        texture_id: int,
        x: int,
        y: int,
        event_id: int,
        mip: int = 0,
        slice: int = 0,
        sample: int = 0,
        prefer_geometry: bool = True,
    ) -> Any:
        """Analyze one frozen texture pixel and return metadata, range, and writer evidence.

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

        return await service.call(
            invocation,
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
        )

    async def inspect_shader(event_id: int, stage: str = "fragment") -> Any:
        """Inspect one exact bound shader using reflection and reconstruction evidence.

        Args:
            event_id: Frozen positive RenderDoc event ID.
            stage: Bound shader stage identifier.
        """

        return await service.call(
            invocation, "inspect_shader", {"event_id": event_id, "stage": stage}
        )

    async def get_draw_calls(
        include_children: bool = True,
        marker_filter: str = "",
        only_actions: bool = False,
        event_id_min: int = 0,
        event_id_max: int = 0,
    ) -> Any:
        """List capture actions with optional marker and event filters."""

        return await service.call(
            invocation,
            "get_draw_calls",
            {
                "include_children": include_children,
                "marker_filter": marker_filter,
                "only_actions": only_actions,
                "event_id_min": event_id_min,
                "event_id_max": event_id_max,
            },
        )

    async def get_frame_summary() -> Any:
        """Return a bounded summary of the current capture frame."""

        return await service.call(invocation, "get_frame_summary", {})

    async def get_draw_call_details(event_id: int) -> Any:
        """Return detailed evidence for one RenderDoc event."""

        return await service.call(
            invocation, "get_draw_call_details", {"event_id": event_id}
        )

    async def get_pipeline_state(event_id: int) -> Any:
        """Return the bound pipeline state at one RenderDoc event."""

        return await service.call(invocation, "get_pipeline_state", {"event_id": event_id})

    async def get_shader_info(event_id: int, stage: str = "fragment") -> Any:
        """Return reflection and identity for a shader bound at an event."""

        return await service.call(
            invocation, "get_shader_info", {"event_id": event_id, "stage": stage}
        )

    async def reverse_shader(event_id: int, stage: str = "fragment") -> Any:
        """Return reflection, disassembly, bindings, and reconstruction evidence."""

        return await service.call(
            invocation, "reverse_shader", {"event_id": event_id, "stage": stage}
        )

    async def get_bound_textures(event_id: int, stage: str = "fragment") -> Any:
        """Return textures bound to a shader stage at one event."""

        return await service.call(
            invocation, "get_bound_textures", {"event_id": event_id, "stage": stage}
        )

    async def find_draws_by_shader(shader_name: str, stage: str = "") -> Any:
        """Find events using a partially matched shader name and optional stage."""

        return await service.call(
            invocation,
            "find_draws_by_shader",
            {"shader_name": shader_name, "stage": stage},
        )

    async def find_draws_by_texture(texture_name: str) -> Any:
        """Find events using a partially matched texture name."""

        return await service.call(
            invocation, "find_draws_by_texture", {"texture_name": texture_name}
        )

    async def find_draws_by_resource(resource_id: int) -> Any:
        """Find events using one exact RenderDoc resource ID."""

        return await service.call(
            invocation, "find_draws_by_resource", {"resource_id": resource_id}
        )

    async def get_textures() -> Any:
        """List textures alive in the capture."""

        return await service.call(invocation, "get_textures", {})

    async def get_buffers() -> Any:
        """List buffers alive in the capture."""

        return await service.call(invocation, "get_buffers", {})

    async def get_resources() -> Any:
        """List capture resources."""

        return await service.call(invocation, "get_resources", {})

    async def get_texture_info(resource_id: int) -> Any:
        """Return metadata for one texture resource."""

        return await service.call(
            invocation, "get_texture_info", {"resource_id": resource_id}
        )

    async def get_texture_minmax(resource_id: int) -> Any:
        """Return minimum and maximum component values for one texture."""

        return await service.call(
            invocation, "get_texture_minmax", {"resource_id": resource_id}
        )

    async def inspect_pixel_object(
        texture_id: int,
        x: int,
        y: int,
        event_id: int,
        mip: int = 0,
        slice: int = 0,
        sample: int = 0,
        prefer_geometry: bool = True,
    ) -> Any:
        """Return exact fragment/write evidence for one frozen texture pixel."""

        return await service.call(
            invocation,
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
        )

    async def enumerate_counters() -> Any:
        """List GPU performance counters available for the capture."""

        return await service.call(invocation, "enumerate_counters", {})

    async def fetch_counters(counter_ids: str) -> Any:
        """Fetch comma-separated GPU counter IDs."""

        return await service.call(invocation, "fetch_counters", {"counter_ids": counter_ids})

    async def get_debug_messages() -> Any:
        """Return graphics API validation and driver messages."""

        return await service.call(invocation, "get_debug_messages", {})

    async def get_action_timings(event_ids: str = "", marker_filter: str = "") -> Any:
        """Return GPU timings for explicit events or a marker filter."""

        return await service.call(
            invocation,
            "get_action_timings",
            {"event_ids": event_ids, "marker_filter": marker_filter},
        )

    functions = (
        inspect_event,
        analyze_pixel,
        inspect_shader,
        get_draw_calls,
        get_frame_summary,
        get_draw_call_details,
        get_pipeline_state,
        get_shader_info,
        reverse_shader,
        get_bound_textures,
        find_draws_by_shader,
        find_draws_by_texture,
        find_draws_by_resource,
        get_textures,
        get_buffers,
        get_resources,
        get_texture_info,
        get_texture_minmax,
        inspect_pixel_object,
        enumerate_counters,
        fetch_counters,
        get_debug_messages,
        get_action_timings,
    ) + _direct_workflow_functions(service, invocation)
    tools = {
        function.__name__: Tool(function, sequential=True, max_retries=0)
        for function in functions
    }
    return {name: tool for name, tool in tools.items() if name in invocation.allowed_tools}


def available_internal_tools() -> frozenset[str]:
    return frozenset(
        {
            "inspect_event",
            "analyze_pixel",
            "inspect_shader",
            "get_draw_calls",
            "get_frame_summary",
            "get_draw_call_details",
            "get_pipeline_state",
            "get_shader_info",
            "reverse_shader",
            "get_bound_textures",
            "find_draws_by_shader",
            "find_draws_by_texture",
            "find_draws_by_resource",
            "get_textures",
            "get_buffers",
            "get_resources",
            "get_texture_info",
            "get_texture_minmax",
            "inspect_pixel_object",
            "enumerate_counters",
            "fetch_counters",
            "get_debug_messages",
            "get_action_timings",
        }
    ) | _DIRECT_WORKFLOW_TOOLS


def build_internal_toolset(
    service: InternalToolService, invocation: ToolInvocation
) -> Any:
    try:
        from pydantic_ai.toolsets import FunctionToolset
    except ImportError as exc:
        raise ConfigurationError(
            "model Agent support is not installed; install replaylab-agenthost[model]"
        ) from exc
    return FunctionToolset(list(create_internal_tools(service, invocation).values()))


def dynamic_tool_specs(
    service: InternalToolService, invocation: ToolInvocation
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for name, tool in sorted(create_internal_tools(service, invocation).items()):
        schema = tool.function_schema
        specs.append(
            {
                "type": "function",
                "name": name,
                "description": str(tool.description or schema.description or ""),
                "inputSchema": schema.json_schema,
                "deferLoading": False,
            }
        )
    return specs


async def call_dynamic_tool(
    service: InternalToolService,
    invocation: ToolInvocation,
    name: str,
    arguments: dict[str, Any],
    *,
    call_id: str = "",
) -> Any:
    tools = create_internal_tools(service, invocation)
    try:
        tool = tools[name]
    except KeyError as exc:
        raise PolicyError(f"dynamic tool '{name}' is outside the active grant") from exc
    try:
        validated = tool.function_schema.validator.validate_python(arguments)
    except Exception as exc:
        raise PolicyError(f"dynamic tool '{name}' arguments are invalid") from exc
    token = _DYNAMIC_CALL_ID.set(str(call_id or "")[:256])
    try:
        return await tool.function(**validated)
    finally:
        _DYNAMIC_CALL_ID.reset(token)


__all__ = [
    "InternalToolService",
    "ToolAudit",
    "ToolEvidenceRecord",
    "ToolInvocation",
    "available_internal_tools",
    "build_internal_toolset",
    "call_dynamic_tool",
    "create_internal_tools",
    "dynamic_tool_specs",
    "evidence_digest",
]
