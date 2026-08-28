"""Official Agent Client Protocol adapter for external agent CLIs."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import (
    ConfigurationError,
    ConnectorError,
    LifecycleCleanupError,
    PolicyError,
)
from ..policy import WORKFLOW_EFFECTS, ToolCallScope
from .codex_app_server import (
    DeltaCallback,
    ExternalAgentResult,
    checked_response_size,
)


# Give a well-behaved Agent a short graceful window. The ACP SDK transport must
# wait longer than ReplayLab's tree escalation, otherwise its Windows
# ``Process.terminate()`` kills only a cmd/PowerShell launcher and can orphan the
# real Agent. After the tree stop, keep a separate bounded window for the SDK to
# finish its pipe and receive-task cleanup.
_ACP_CLEANUP_TIMEOUT = 2.0
_ACP_TREE_KILL_TIMEOUT = 5.0
_ACP_FORCE_CLEANUP_TIMEOUT = 5.0
_ACP_TRANSPORT_SHUTDOWN_TIMEOUT = 15.0
_CURSOR_PROVIDER_ID = "cursor-acp"
_MAX_CURSOR_PERMISSION_DENIALS = 16
_CURSOR_SAFE_NOTIFICATIONS = frozenset(
    {
        "cursor/update_todos",
        "cursor/task",
        "cursor/generate_image",
    }
)


@dataclass(slots=True)
class _ACPSDK:
    protocol_version: Any
    spawn_agent_process: Callable[..., Any]
    text_block: Callable[[str], Any]
    client_type: type[Any]
    mcp_server_stdio_type: type[Any]
    request_permission_response_type: type[Any] | None
    denied_outcome_type: type[Any] | None
    decline_elicitation_response_type: type[Any] | None


def _load_acp_sdk() -> _ACPSDK:
    """Import the optional ACP SDK only when an ACP profile is selected."""

    try:
        import acp
        from acp import schema
    except ImportError as exc:
        raise ConnectorError(
            "ACP support is not installed; install ReplayLab with the 'acp' extra "
            "(agent-client-protocol>=0.12,<1)"
        ) from exc
    try:
        from acp.interfaces import Client
    except ImportError:
        Client = getattr(acp, "Client", None)
    required = {
        "PROTOCOL_VERSION": getattr(acp, "PROTOCOL_VERSION", None),
        "spawn_agent_process": getattr(acp, "spawn_agent_process", None),
        "text_block": getattr(acp, "text_block", None),
        "Client": Client,
        "McpServerStdio": getattr(schema, "McpServerStdio", None),
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise ConnectorError(
            "installed Agent Client Protocol SDK is incompatible; missing: "
            + ", ".join(missing)
            + ". Install a supported agent-client-protocol>=0.12,<1 release."
        )
    return _ACPSDK(
        protocol_version=required["PROTOCOL_VERSION"],
        spawn_agent_process=required["spawn_agent_process"],
        text_block=required["text_block"],
        client_type=required["Client"],
        mcp_server_stdio_type=required["McpServerStdio"],
        request_permission_response_type=getattr(schema, "RequestPermissionResponse", None),
        denied_outcome_type=getattr(schema, "DeniedOutcome", None),
        decline_elicitation_response_type=getattr(schema, "DeclineElicitationResponse", None),
    )


def _owned_agent_process(value: Any) -> Any | None:
    if not isinstance(value, (tuple, list)) or len(value) < 2:
        return None
    process = value[1]
    pid = getattr(process, "pid", None)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    return process


def _discard_cleanup_task(task: asyncio.Future[Any]) -> None:
    task.cancel()

    def consume(done: asyncio.Future[Any]) -> None:
        with contextlib.suppress(BaseException):
            done.exception()

    task.add_done_callback(consume)


async def _force_stop_agent_process_tree(process: Any) -> bool:
    """Stop the exact owned Agent process tree without a shell.

    ``asyncio.subprocess.Process.kill`` is only ``TerminateProcess`` on Windows.
    Cursor's official launcher is cmd -> PowerShell -> node, so root-only
    termination is not proof that the ACP Agent was reclaimed. Use the absolute
    system taskkill executable for that bounded Windows escalation, then retain
    a root-process fallback for platforms without process-tree support here.
    """

    pid = getattr(process, "pid", None)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if getattr(process, "returncode", None) is not None:
        # A completed Windows launcher is not proof that an already detached
        # PowerShell/node descendant exited. Without a live root PID taskkill
        # cannot verify the tree, so retain fail-closed semantics there.
        return os.name != "nt"

    tree_stop_confirmed = os.name != "nt"
    if os.name == "nt":
        system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        taskkill = system_root / "System32" / "taskkill.exe"
        if taskkill.is_file():
            killer: asyncio.subprocess.Process | None = None
            try:
                killer = await asyncio.create_subprocess_exec(
                    str(taskkill),
                    "/PID",
                    str(pid),
                    "/T",
                    "/F",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                return_code = await asyncio.wait_for(
                    killer.wait(), timeout=_ACP_TREE_KILL_TIMEOUT
                )
                tree_stop_confirmed = return_code == 0
            except (OSError, asyncio.TimeoutError):
                if killer is not None and killer.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        killer.kill()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(killer.wait(), timeout=1.0)

    if getattr(process, "returncode", None) is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    return tree_stop_confirmed


@contextlib.asynccontextmanager
async def _managed_agent_process(manager: Any, label: str):
    """Preserve body failures while making an uncertain child exit fatal."""

    value = await manager.__aenter__()
    process = _owned_agent_process(value)

    async def close(
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: Any,
    ) -> bool | None:
        try:
            awaitable = manager.__aexit__(exception_type, exception, traceback)
            if not inspect.isawaitable(awaitable):
                raise TypeError("ACP process manager __aexit__ is not awaitable")
            cleanup_task = asyncio.ensure_future(awaitable)
            try:
                return await asyncio.wait_for(
                    asyncio.shield(cleanup_task), timeout=_ACP_CLEANUP_TIMEOUT
                )
            except (asyncio.TimeoutError, asyncio.CancelledError) as cleanup_error:
                cancellation_error = (
                    cleanup_error
                    if isinstance(cleanup_error, asyncio.CancelledError)
                    else None
                )
                if process is None:
                    _discard_cleanup_task(cleanup_task)
                    raise LifecycleCleanupError(
                        f"{label} process cleanup was not confirmed within the deadline"
                    ) from cleanup_error
                force_task = asyncio.create_task(
                    _force_stop_agent_process_tree(process)
                )
                try:
                    tree_stop_confirmed = await asyncio.shield(force_task)
                except asyncio.CancelledError as tree_cancelled:
                    # Do not let a second user cancellation interrupt the exact
                    # tree stop. Preserve it and propagate only after both the
                    # process and SDK transport have reached a terminal state.
                    cancellation_error = tree_cancelled
                    try:
                        tree_stop_confirmed = await asyncio.wait_for(
                            asyncio.shield(force_task),
                            timeout=_ACP_TREE_KILL_TIMEOUT + 1.0,
                        )
                    except (asyncio.TimeoutError, asyncio.CancelledError) as tree_error:
                        _discard_cleanup_task(force_task)
                        _discard_cleanup_task(cleanup_task)
                        raise LifecycleCleanupError(
                            f"{label} process tree cleanup was not confirmed"
                        ) from tree_error
                except Exception as tree_error:
                    _discard_cleanup_task(cleanup_task)
                    raise LifecycleCleanupError(
                        f"{label} process tree cleanup failed"
                    ) from tree_error
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(cleanup_task),
                        timeout=_ACP_FORCE_CLEANUP_TIMEOUT,
                    )
                except asyncio.CancelledError as forced_cancelled:
                    cancellation_error = forced_cancelled
                    try:
                        result = await asyncio.wait_for(
                            asyncio.shield(cleanup_task),
                            timeout=_ACP_FORCE_CLEANUP_TIMEOUT,
                        )
                    except (asyncio.TimeoutError, asyncio.CancelledError) as forced_error:
                        _discard_cleanup_task(cleanup_task)
                        raise LifecycleCleanupError(
                            f"{label} process cleanup was not confirmed within the deadline"
                        ) from forced_error
                except asyncio.TimeoutError as forced_error:
                    _discard_cleanup_task(cleanup_task)
                    raise LifecycleCleanupError(
                        f"{label} process cleanup was not confirmed within the deadline"
                    ) from forced_error
                if os.name == "nt" and not tree_stop_confirmed:
                    raise LifecycleCleanupError(
                        f"{label} process tree cleanup was not confirmed"
                    ) from cleanup_error
                if cancellation_error is not None:
                    raise cancellation_error
                return result
        except LifecycleCleanupError:
            raise
        except Exception as cleanup_error:
            raise LifecycleCleanupError(
                f"{label} process cleanup failed"
            ) from cleanup_error

    try:
        yield value
    except BaseException as primary:
        suppressed = await close(type(primary), primary, primary.__traceback__)
        if suppressed:
            return
        raise
    else:
        await close(None, None, None)


class ACPConnector:
    """Run one ACP session with ReplayLab MCP and no implicit permissions."""

    def __init__(
        self,
        command: Sequence[str] | str,
        *,
        cwd: Path | str | None = None,
        replaylab_mcp_executable: Path | str | None = None,
        replaylab_mcp_args: Sequence[str] = ("stdio",),
        allowed_tools: set[str] | frozenset[str] = frozenset(),
        instance_id: str = "",
        registry_path: Path | str | None = None,
        tool_scope: ToolCallScope | None = None,
        workflow_effects: set[str] | frozenset[str] = frozenset(),
        audit_file: Path | str | None = None,
        timeout: float = 300.0,
        env: Mapping[str, str] | None = None,
        authentication_method: str = "",
        provider_id: str = "",
    ) -> None:
        if isinstance(command, str):
            command = (command,)
        self.command = tuple(str(value) for value in command)
        if not self.command or any(not value or "\0" in value for value in self.command):
            raise ConfigurationError("ACP agent command must be a non-empty argv sequence")
        self.cwd = Path(cwd or Path.cwd()).resolve()
        if not self.cwd.is_dir():
            raise ConfigurationError(f"ACP working directory does not exist: {self.cwd}")
        cleaned = frozenset(str(value).strip() for value in allowed_tools if str(value).strip())
        if any("\0" in value for value in cleaned):
            raise ConfigurationError("ACP tool allow-list contains an invalid NUL character")
        self.allowed_tools = cleaned
        self.replaylab_mcp_executable = (
            Path(replaylab_mcp_executable).resolve()
            if replaylab_mcp_executable is not None
            else None
        )
        if self.replaylab_mcp_executable is not None and not self.allowed_tools:
            raise ConfigurationError(
                "ReplayLab MCP cannot be attached without any task tools"
            )
        if self.allowed_tools and (
            self.replaylab_mcp_executable is None
            or not self.replaylab_mcp_executable.is_file()
        ):
            raise ConfigurationError(
                "ReplayLab MCP executable does not exist: "
                f"{self.replaylab_mcp_executable or '<not configured>'}"
            )
        self.replaylab_mcp_args = tuple(str(value) for value in replaylab_mcp_args)
        if any("\0" in value for value in self.replaylab_mcp_args):
            raise ConfigurationError("ReplayLab MCP arguments contain an invalid NUL character")
        self.instance_id = str(instance_id or "").strip()
        self.registry_path = Path(registry_path).resolve() if registry_path else None
        self.tool_scope = tool_scope or ToolCallScope()
        self.workflow_effects = frozenset(
            str(value).strip() for value in workflow_effects if str(value).strip()
        )
        unknown_effects = self.workflow_effects - WORKFLOW_EFFECTS
        if unknown_effects:
            raise ConfigurationError(
                "ACP workflow contains unknown effect classes: "
                + ", ".join(sorted(unknown_effects))
            )
        if "replay" in self.workflow_effects and "workspace" not in self.workflow_effects:
            raise ConfigurationError("ACP replay workflow requires the workspace effect")
        self.audit_file = Path(audit_file).resolve() if audit_file else None
        if self.allowed_tools and (
            not self.instance_id
            or "\0" in self.instance_id
            or self.registry_path is None
            or not self.registry_path.is_absolute()
        ):
            raise ConfigurationError(
                "ACP capture tools require a pinned qrenderdoc instance and isolated registry"
            )
        if timeout <= 0:
            raise ConfigurationError("ACP timeout must be positive")
        self.timeout = float(timeout)
        self.env = dict(env) if env is not None else None
        self.authentication_method = str(authentication_method or "").strip()
        if len(self.authentication_method) > 128 or "\0" in self.authentication_method:
            raise ConfigurationError("ACP authentication method is invalid")
        self.provider_id = str(provider_id or "").strip()
        if len(self.provider_id) > 128 or "\0" in self.provider_id:
            raise ConfigurationError("ACP provider id is invalid")
        self._run_lock = asyncio.Lock()

    async def _authenticate(self, connection: Any) -> None:
        if not self.authentication_method:
            return
        await _wait_for(
            connection.authenticate(method_id=self.authentication_method),
            min(self.timeout, 60.0),
            "ACP authenticate",
        )

    async def test_connection(self) -> dict[str, Any]:
        """Perform a no-prompt ACP initialize/session handshake.

        This deliberately stops before ``session/prompt`` so the Settings
        connection test cannot consume model quota or grant capture tools.
        Creating a session is still required: several vendor Agents validate
        their saved account only at ``session/new``.
        """

        async with self._run_lock:
            sdk = _load_acp_sdk()
            state = _ACPClientState(
                on_delta=None,
                provider_id=self.provider_id,
                permission_audit_file=self._permission_audit_file(),
            )
            client = _make_client(sdk, state)
            spawn_kwargs: dict[str, Any] = {"cwd": str(self.cwd)}
            if self.env is not None:
                spawn_kwargs["env"] = self.env
            try:
                manager = sdk.spawn_agent_process(
                    client,
                    *self.command,
                    transport_kwargs={
                        "stderr": asyncio.subprocess.DEVNULL,
                        "limit": 4 * 1024 * 1024,
                        "shutdown_timeout": _ACP_TRANSPORT_SHUTDOWN_TIMEOUT,
                    },
                    **spawn_kwargs,
                )
                async with _managed_agent_process(manager, "ACP connection test") as (
                    connection,
                    _process,
                ):
                    initialized = await _wait_for(
                        connection.initialize(protocol_version=sdk.protocol_version),
                        min(self.timeout, 30.0),
                        "ACP initialize",
                    )
                    await self._authenticate(connection)
                    session = await _wait_for(
                        connection.new_session(cwd=str(self.cwd), mcp_servers=[]),
                        min(self.timeout, 60.0),
                        "ACP session/new",
                    )
                    session_id = str(getattr(session, "session_id", ""))
                    if not session_id:
                        raise ConnectorError("ACP session/new returned no session id")
                    state.session_id = session_id
                    if state.policy_error is not None:
                        raise state.policy_error
                    return {
                        "sessionId": session_id,
                        "models": _connection_models(session),
                        "agentInfo": _wire_agent_info(initialized),
                    }
            except (ConfigurationError, ConnectorError, PolicyError):
                raise
            except FileNotFoundError as exc:
                rendered = " ".join(self.command)
                raise ConnectorError(f"cannot start ACP agent ({rendered}): {exc}") from exc
            except TypeError as exc:
                raise ConnectorError(
                    "installed Agent Client Protocol SDK has an incompatible handshake API"
                ) from exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise ConnectorError(f"ACP connection test failed: {exc}") from exc

    async def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        skill_name: str | None = None,
        skill_path: Path | str | None = None,
        on_delta: DeltaCallback | None = None,
    ) -> ExternalAgentResult:
        # ACP does not define Codex-style skill input blocks. ReplayLab's runtime
        # has already embedded the selected skill instructions into ``prompt``;
        # accept the uniform connector arguments without asking the ACP agent to
        # resolve or read another local file.
        del skill_name, skill_path
        if not prompt.strip():
            raise ConfigurationError("ACP prompt must not be empty")
        async with self._run_lock:
            sdk = _load_acp_sdk()
            state = _ACPClientState(
                on_delta=on_delta,
                provider_id=self.provider_id,
                permission_audit_file=self._permission_audit_file(),
            )
            client = _make_client(sdk, state)
            mcp_servers = self._mcp_servers(sdk)

            spawn_kwargs: dict[str, Any] = {}
            spawn_kwargs["cwd"] = str(self.cwd)
            if self.env is not None:
                spawn_kwargs["env"] = self.env
            session_id = ""
            try:
                manager = sdk.spawn_agent_process(
                    client,
                    *self.command,
                    transport_kwargs={
                        "stderr": asyncio.subprocess.DEVNULL,
                        "limit": 4 * 1024 * 1024,
                        "shutdown_timeout": _ACP_TRANSPORT_SHUTDOWN_TIMEOUT,
                    },
                    **spawn_kwargs,
                )
                async with _managed_agent_process(manager, "ACP specialist") as (
                    connection,
                    _process,
                ):
                    await _wait_for(
                        connection.initialize(protocol_version=sdk.protocol_version),
                        min(self.timeout, 30.0),
                        "ACP initialize",
                    )
                    await self._authenticate(connection)
                    session = await _wait_for(
                        connection.new_session(
                            cwd=str(self.cwd),
                            mcp_servers=mcp_servers,
                        ),
                        min(self.timeout, 60.0),
                        "ACP session/new",
                    )
                    session_id = str(getattr(session, "session_id", ""))
                    if not session_id:
                        raise ConnectorError("ACP session/new returned no session id")
                    state.session_id = session_id
                    if model:
                        await self._select_model(connection, session, session_id, model)
                    try:
                        prompt_task = asyncio.create_task(
                            _wait_for(
                                connection.prompt(
                                    session_id=session_id,
                                    prompt=[sdk.text_block(prompt)],
                                ),
                                self.timeout,
                                "ACP session/prompt",
                            )
                        )
                        abort_task = asyncio.create_task(state.abort_event.wait())
                        try:
                            done, _ = await asyncio.wait(
                                {prompt_task, abort_task}, return_when=asyncio.FIRST_COMPLETED
                            )
                            if abort_task in done:
                                raise (
                                    state.callback_error
                                    or state.policy_error
                                    or ConnectorError("ACP response collection was aborted")
                                )
                            prompt_response = await prompt_task
                        finally:
                            for task in (prompt_task, abort_task):
                                if not task.done():
                                    task.cancel()
                            await asyncio.gather(prompt_task, abort_task, return_exceptions=True)
                        if state.policy_error is not None:
                            raise state.policy_error
                        stop_reason = _stop_reason(prompt_response)
                        if stop_reason != "end_turn":
                            raise ConnectorError(
                                f"ACP Agent stopped without completing the turn: {stop_reason}"
                            )
                    except Exception:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(
                                connection.cancel(session_id=session_id), timeout=2.0
                            )
                        raise
            except (ConfigurationError, ConnectorError, PolicyError):
                raise
            except FileNotFoundError as exc:
                rendered = " ".join(self.command)
                raise ConnectorError(
                    f"cannot start ACP agent ({rendered}): {exc}; configure an explicit command"
                ) from exc
            except TypeError as exc:
                raise ConnectorError(
                    "installed ACP SDK has an incompatible process/session API; "
                    "install a supported agent-client-protocol>=0.12,<1 release"
                ) from exc
            except Exception as exc:
                raise ConnectorError(f"ACP agent failed: {exc}") from exc

            if state.callback_error is not None:
                raise ConnectorError(f"ACP delta callback failed: {state.callback_error}") from state.callback_error
            if state.policy_error is not None:
                raise state.policy_error
            return ExternalAgentResult(
                text="".join(state.text_chunks),
                backend="acp",
                session_id=session_id,
            )

    def _mcp_servers(self, sdk: _ACPSDK) -> list[Any]:
        # An empty set means chat without capture access. A non-empty set is
        # enforced by a separate proxy before the Agent sees tool descriptions.
        if self.allowed_tools == frozenset():
            return []
        if self.replaylab_mcp_executable is None:
            raise ConfigurationError("ReplayLab MCP executable is not configured")
        command = sys.executable
        args = [
            "-I",
            "-m",
            "replaylab_agent.mcp_proxy",
            "--host",
            str(self.replaylab_mcp_executable),
        ]
        for upstream_arg in self.replaylab_mcp_args:
            args.extend(("--host-arg", upstream_arg))
        for tool_name in sorted(self.allowed_tools):
            args.extend(("--allow", tool_name))
        for effect in sorted(self.workflow_effects):
            args.extend(("--effect", effect))
        args.extend(("--instance-id", self.instance_id))
        args.extend(("--registry-path", str(self.registry_path)))
        args.extend(("--scope-json", self.tool_scope.to_json()))
        if self.audit_file is not None:
            args.extend(("--audit-file", str(self.audit_file)))
        try:
            return [
                sdk.mcp_server_stdio_type(
                    name="ReplayLab",
                    command=command,
                    args=args,
                    env=[],
                )
            ]
        except Exception as exc:
            raise ConnectorError(
                "installed ACP SDK rejected the ReplayLab MCP stdio definition; "
                "install a supported agent-client-protocol>=0.12,<1 release"
            ) from exc

    def _permission_audit_file(self) -> Path | None:
        if self.provider_id != _CURSOR_PROVIDER_ID or self.audit_file is None:
            return None
        return self.audit_file.with_name("permission-audit.jsonl")

    async def _select_model(
        self,
        connection: Any,
        session: Any,
        session_id: str,
        model: str,
    ) -> None:
        options = _field(session, "config_options", "configOptions") or []
        model_option: Any | None = None
        for option in options:
            category = _enum_value(_field(option, "category")).lower()
            option_id = str(_field(option, "id") or "")
            if category == "model" or option_id.lower() in {"model", "models"}:
                model_option = option
                break
        setter = getattr(connection, "set_config_option", None)
        if model_option is None or not callable(setter):
            raise ConnectorError(
                "this ACP agent does not expose the standard model configuration option; "
                "leave the profile model unset and use the provider CLI's configured default"
            )
        await _wait_for(
            setter(
                config_id=str(_field(model_option, "id")),
                session_id=session_id,
                value=model,
            ),
            min(self.timeout, 30.0),
            "ACP model selection",
        )


@dataclass(slots=True)
class _ACPClientState:
    on_delta: DeltaCallback | None
    provider_id: str = ""
    session_id: str = ""
    cursor_permission_denials: int = 0
    cursor_permission_ids: set[str] = field(default_factory=set)
    permission_audit_file: Path | None = None
    text_chunks: list[str] = field(default_factory=list)
    text_chars: int = 0
    text_bytes: int = 0
    policy_error: PolicyError | None = None
    callback_error: Exception | None = None
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)


def _make_client(sdk: _ACPSDK, state: _ACPClientState) -> Any:
    class ReplayLabACPClient(sdk.client_type):  # type: ignore[misc, valid-type]
        def on_connect(self, conn: Any) -> None:
            self.connection = conn

        async def request_permission(
            self,
            session_id: str,
            tool_call: Any,
            options: list[Any],
            **kwargs: Any,
        ) -> Any:
            del kwargs
            if state.provider_id == _CURSOR_PROVIDER_ID:
                validation_error = _validate_cursor_permission_request(
                    state, session_id, tool_call, options
                )
                if validation_error:
                    state.policy_error = PolicyError(
                        "Cursor ACP sent an invalid permission request; ReplayLab cancelled it "
                        f"(fail-closed: {validation_error})"
                    )
                    state.abort_event.set()
                else:
                    state.cursor_permission_denials += 1
                    _audit_cursor_permission_denial(state)
                    if state.cursor_permission_denials > _MAX_CURSOR_PERMISSION_DENIALS:
                        state.policy_error = PolicyError(
                            "Cursor ACP exceeded the bounded permission-denial limit; "
                            "ReplayLab cancelled the turn (fail-closed)"
                        )
                        state.abort_event.set()
                # Cursor expects every permission request to receive a decision.
                # Exact ReplayLab MCP tools are approved before the request layer
                # by the profile-scoped Cursor allowlist; every request reaching
                # this callback is therefore cancelled. A normal cancellation is
                # non-fatal so Cursor can continue with a text-only answer.
                return _permission_denied(sdk)
            state.policy_error = PolicyError(
                "ACP agent requested extra permission; ReplayLab cancelled it (fail-closed)"
            )
            state.abort_event.set()
            return _permission_denied(sdk)

        async def create_elicitation(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            state.policy_error = PolicyError(
                "ACP agent requested interactive input; ReplayLab declined it (fail-closed)"
            )
            state.abort_event.set()
            response_type = sdk.decline_elicitation_response_type
            if response_type is not None:
                try:
                    return response_type(action="decline")
                except Exception:
                    pass
            return {"action": "decline"}

        async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
            del elicitation_id, kwargs
            _record_policy_denial(state, "complete elicitation")

        async def read_text_file(
            self,
            session_id: str,
            path: str,
            line: int | None = None,
            limit: int | None = None,
            **kwargs: Any,
        ) -> Any:
            del session_id, path, line, limit, kwargs
            raise _policy_denial(state, "read a local file")

        async def write_text_file(
            self, session_id: str, path: str, content: str, **kwargs: Any
        ) -> Any:
            del session_id, path, content, kwargs
            raise _policy_denial(state, "write a local file")

        async def create_terminal(
            self,
            session_id: str,
            command: str,
            args: list[str] | None = None,
            env: list[Any] | None = None,
            cwd: str | None = None,
            output_byte_limit: int | None = None,
            **kwargs: Any,
        ) -> Any:
            del session_id, command, args, env, cwd, output_byte_limit, kwargs
            raise _policy_denial(state, "create a terminal")

        async def terminal_output(
            self, session_id: str, terminal_id: str, **kwargs: Any
        ) -> Any:
            del session_id, terminal_id, kwargs
            raise _policy_denial(state, "read terminal output")

        async def release_terminal(
            self, session_id: str, terminal_id: str, **kwargs: Any
        ) -> Any:
            del session_id, terminal_id, kwargs
            raise _policy_denial(state, "release a terminal")

        async def wait_for_terminal_exit(
            self, session_id: str, terminal_id: str, **kwargs: Any
        ) -> Any:
            del session_id, terminal_id, kwargs
            raise _policy_denial(state, "wait for a terminal")

        async def kill_terminal(
            self, session_id: str, terminal_id: str, **kwargs: Any
        ) -> Any:
            del session_id, terminal_id, kwargs
            raise _policy_denial(state, "kill a terminal")

        async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            del params
            if state.provider_id == _CURSOR_PROVIDER_ID:
                if method == "cursor/ask_question":
                    return {
                        "outcome": {
                            "outcome": "skipped",
                            "reason": "ReplayLab does not provide interactive answers during this automated turn.",
                        }
                    }
                if method == "cursor/create_plan":
                    return {
                        "outcome": {
                            "outcome": "rejected",
                            "reason": "ReplayLab does not approve external execution plans; continue with read-only analysis.",
                        }
                    }
            raise _policy_denial(state, f"invoke ACP extension method '{method}'")

        async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
            del params
            if (
                state.provider_id == _CURSOR_PROVIDER_ID
                and method in _CURSOR_SAFE_NOTIFICATIONS
            ):
                return
            _record_policy_denial(state, f"send ACP extension notification '{method}'")

        async def session_update(
            self,
            session_id: str,
            update: Any,
            **kwargs: Any,
        ) -> None:
            del session_id, kwargs
            if not _is_agent_message(update):
                return
            text = _extract_text(update)
            if not text:
                return
            if state.callback_error is not None:
                return
            try:
                state.text_chars, state.text_bytes = checked_response_size(
                    text, state.text_chars, state.text_bytes
                )
            except Exception as exc:
                state.callback_error = exc
                state.abort_event.set()
                return
            state.text_chunks.append(text)
            callback = state.on_delta
            if callback is None or state.callback_error is not None:
                return
            try:
                result = callback(text)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                # Keep the protocol notification handler responsive, then surface
                # the host callback failure by cancelling the active prompt.
                state.callback_error = exc
                state.abort_event.set()

    return ReplayLabACPClient()


def _policy_denial(state: _ACPClientState, action: str) -> PolicyError:
    error = PolicyError(f"ACP Agent attempted to {action}; ReplayLab denied it (fail-closed)")
    state.policy_error = error
    state.abort_event.set()
    return error


def _record_policy_denial(state: _ACPClientState, action: str) -> None:
    state.policy_error = PolicyError(
        f"ACP Agent attempted to {action}; ReplayLab denied it (fail-closed)"
    )
    state.abort_event.set()


def _permission_denied(sdk: _ACPSDK) -> Any:
    response_type = sdk.request_permission_response_type
    outcome_type = sdk.denied_outcome_type
    if response_type is not None and outcome_type is not None:
        try:
            return response_type(outcome=outcome_type(outcome="cancelled"))
        except Exception:
            pass
    # The official quickstart also permits this mapping form.
    return {"outcome": {"outcome": "cancelled"}}


def _validate_cursor_permission_request(
    state: _ACPClientState,
    session_id: str,
    tool_call: Any,
    options: list[Any],
) -> str:
    if (
        not isinstance(session_id, str)
        or not session_id
        or len(session_id) > 256
        or "\0" in session_id
        or session_id != state.session_id
    ):
        return "session mismatch"
    tool_call_id = str(_field(tool_call, "tool_call_id", "toolCallId") or "").strip()
    if not tool_call_id or len(tool_call_id) > 256 or "\0" in tool_call_id:
        return "invalid tool-call id"
    if tool_call_id in state.cursor_permission_ids:
        return "duplicate tool-call id"
    if not isinstance(options, list) or not 1 <= len(options) <= 32:
        return "invalid option count"
    option_ids: set[str] = set()
    for option in options:
        option_id = str(_field(option, "option_id", "optionId") or "").strip()
        name = str(_field(option, "name") or "").strip()
        kind = _enum_value(_field(option, "kind")).replace("-", "_").lower()
        if (
            not option_id
            or len(option_id) > 256
            or "\0" in option_id
            or option_id in option_ids
            or not name
            or len(name) > 512
            or "\0" in name
            or kind
            not in {"allow_once", "allow_always", "reject_once", "reject_always"}
        ):
            return "invalid permission option"
        option_ids.add(option_id)
    state.cursor_permission_ids.add(tool_call_id)
    return ""


def _audit_cursor_permission_denial(state: _ACPClientState) -> None:
    path = state.permission_audit_file
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(
                    {
                        "event": "permissionDenied",
                        "provider": _CURSOR_PROVIDER_ID,
                        "sequence": state.cursor_permission_denials,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
    except OSError:
        state.policy_error = PolicyError(
            "ReplayLab could not record the Cursor permission denial; "
            "the turn was cancelled (fail-closed)"
        )
        state.abort_event.set()


def _is_agent_message(update: Any) -> bool:
    if isinstance(update, dict):
        kind = update.get("sessionUpdate", update.get("session_update"))
    else:
        kind = getattr(update, "session_update", None)
    return kind == "agent_message_chunk" or type(update).__name__ == "AgentMessageChunk"


def _extract_text(update: Any) -> str:
    content = update.get("content") if isinstance(update, dict) else getattr(update, "content", None)
    if isinstance(content, dict):
        if content.get("type") == "text" and isinstance(content.get("text"), str):
            return content["text"]
        return ""
    if getattr(content, "type", None) == "text":
        value = getattr(content, "text", "")
        return value if isinstance(value, str) else ""
    return ""


def _stop_reason(response: Any) -> str:
    if isinstance(response, dict):
        value = response.get("stopReason", response.get("stop_reason", ""))
    else:
        value = getattr(response, "stop_reason", "")
    return str(value or "unknown")


def _connection_models(session: Any) -> list[str]:
    """Extract bounded model ids from stable and legacy ACP session shapes."""

    result: list[str] = []
    options = _field(session, "config_options", "configOptions") or []
    for option in options:
        category = _enum_value(_field(option, "category")).lower()
        option_id = str(_field(option, "id") or "")
        if category != "model" and option_id.lower() not in {"model", "models"}:
            continue
        _append_model_choices(
            result, _field(option, "options", "values") or [], depth=0
        )
        if len(result) >= 200:
            return result

    # Older ACP agents used a dedicated ``models.availableModels`` object.
    # Keep this read-only fallback so a current ReplayLab client can still
    # populate the selector without pretending every Agent supports the newer
    # stable session config-option API.
    legacy = _field(session, "models")
    _append_model_choices(
        result,
        _field(legacy, "available_models", "availableModels") or [],
        depth=0,
        value_names=("model_id", "modelId", "value", "id"),
    )
    return result


def _field(value: Any, *names: str) -> Any:
    if value is None:
        return None
    for name in names:
        if isinstance(value, dict):
            if name in value:
                return value[name]
        elif hasattr(value, name):
            return getattr(value, name)
    return None


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _append_model_choices(
    result: list[str],
    choices: Any,
    *,
    depth: int,
    value_names: tuple[str, ...] = ("value", "id"),
) -> None:
    if depth > 2 or not isinstance(choices, (list, tuple)):
        return
    for choice in choices:
        nested = _field(choice, "options")
        if nested is not None:
            _append_model_choices(
                result,
                nested,
                depth=depth + 1,
                value_names=value_names,
            )
        else:
            value = choice if isinstance(choice, str) else _field(choice, *value_names)
            model = str(value or "").strip()
            if model and len(model) <= 512 and model not in result:
                result.append(model)
        if len(result) >= 200:
            return


def _wire_agent_info(initialized: Any) -> dict[str, str] | None:
    info = getattr(initialized, "agent_info", None)
    if info is None and isinstance(initialized, dict):
        info = initialized.get("agentInfo", initialized.get("agent_info"))
    if info is None:
        return None
    if isinstance(info, dict):
        name = info.get("name", "")
        version = info.get("version", "")
    else:
        name = getattr(info, "name", "")
        version = getattr(info, "version", "")
    return {"name": str(name or "")[:256], "version": str(version or "")[:128]}


async def _wait_for(awaitable: Any, timeout: float, stage: str) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise ConnectorError(f"{stage} timed out") from exc
