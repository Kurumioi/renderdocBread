"""A long-lived ACP session for the main Agent.

Unlike ``connectors.acp.ACPConnector`` (connection diagnostics only), this
session keeps the provider process alive so the main Agent can call the
task-scoped ReplayLab MCP tools and receive bounded verification follow-ups in
the same context.

It reuses the connector module's SDK loader, client implementation and
timeline projection; only the process/session lifecycle is different.  The
Host-side MCP endpoint is registered under the same server name
(``ReplayLab``) so the connector's fail-closed permission policy applies
unchanged: the Agent may call exactly the tools in ``allowed_tools`` on that
server and nothing else (no shell, no files, no other MCP).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .catalog import REPLAYLAB_MCP_SERVER_NAME
from .connectors.acp import (
    _ACPClientState,
    _acp_prompt_blocks,
    _cursor_question_as_elicitation,
    _enum_value,
    _field,
    _load_acp_sdk,
    _make_client,
    _managed_agent_process,
    _new_session,
    _permission_allowed,
    _permission_denied,
    _requires_explicit_authentication,
    _require_acp_attachment_capabilities,
    _replaylab_tool_for_permission,
    _stop_reason,
    _validate_scoped_permission_request,
    _wait_for,
)
from .conversation import ProviderBinaryAttachment
from .errors import ConfigurationError, ConnectorError
from .agent_diagnostics import get_logger, public_error

_ACP_TRANSPORT_MAX_BYTES = 64 * 1024 * 1024
_ACP_TRANSPORT_SHUTDOWN_TIMEOUT = 10.0
# ``session/new`` budget per attempt.  Vendors validate the stored login and
# start the declared ReplayLab MCP process inside this window; a healthy Cursor
# takes 5-8 s, so one transient stall (vendor backend, auto-update, network)
# earns exactly one fresh-process retry with a longer window before failing.
_ACP_SESSION_NEW_TIMEOUT = 60.0
_ACP_SESSION_NEW_RETRY_TIMEOUT = 90.0
_ACP_STARTUP_STAGES = ("ACP initialize", "ACP session/new")
_ACP_ROUTE_DIAGNOSIS_TIMEOUT = 20.0


@dataclass(slots=True)
class PromptResult:
    text: str
    segments: list[str]
    stop_reason: str
    tool_calls: int

    @property
    def final_text(self) -> str:
        for segment in reversed(self.segments):
            if segment.strip():
                return segment
        return self.text


@dataclass
class ACPMainAgentSession:
    command: Sequence[str]
    cwd: Path
    env: Mapping[str, str] | None
    allowed_tools: frozenset[str]
    # Native subscription mode supplies the task-scoped ReplayLab MCP filter
    # directly over stdio.
    mcp_stdio_command: str = ""
    mcp_stdio_args: Sequence[str] = ()
    mcp_stdio_env: Mapping[str, str] | None = None
    on_elicitation: Callable[[str, Any], Any] | None = None
    timeout: float = 3600.0
    authentication_method: str = ""
    provider_id: str = ""
    model: str = ""
    stderr_path: Path | None = None
    # Vendor-specific ``session/new`` ``_meta``.
    session_meta: Mapping[str, Any] | None = None
    mcp_transport: str = field(default="", init=False)
    _sdk: Any = field(default=None, init=False, repr=False)
    _stack: contextlib.AsyncExitStack | None = field(default=None, init=False, repr=False)
    _connection: Any = field(default=None, init=False, repr=False)
    _session_id: str = field(default="", init=False)
    _stderr_handle: Any = field(default=None, init=False, repr=False)
    _state: _ACPClientState | None = field(default=None, init=False, repr=False)
    _initialized: Any = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------- lifecycle
    async def __aenter__(self) -> "ACPMainAgentSession":
        await self.open()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def open(self) -> None:
        if not self.command:
            raise ConfigurationError("ACP main Agent command is empty")
        budgets = (
            min(self.timeout, _ACP_SESSION_NEW_TIMEOUT),
            min(self.timeout, _ACP_SESSION_NEW_RETRY_TIMEOUT),
        )
        for attempt, budget in enumerate(budgets):
            try:
                await self._open_once(budget)
                return
            except ConnectorError as exc:
                stage = _startup_timeout_stage(exc)
                if stage is None:
                    raise
                if attempt + 1 < len(budgets):
                    get_logger().warning(
                        "%s timed out after %.0fs (%s); retrying once with a fresh Agent process",
                        stage,
                        budget,
                        Path(str(self.command[0])).name,
                    )
                    continue
                raise ConnectorError(
                    "%s timed out twice (%.0fs then %.0fs) for %s. %s"
                    % (
                        stage,
                        budgets[0],
                        budget,
                        Path(str(self.command[0])).name,
                        await self._diagnose_startup_timeout(),
                    )
                ) from exc

    async def _diagnose_startup_timeout(self) -> str:
        """Say which side stalled: the vendor process or the local ReplayLab MCP route."""

        if not self.mcp_stdio_command or not self.allowed_tools:
            return (
                "No ReplayLab MCP server was declared for this session, so the stall is "
                "inside the vendor Agent (login state, vendor backend, network, or an "
                "auto-update on first launch)."
            )
        try:
            verdict = await _diagnose_local_mcp_route(
                self.mcp_stdio_command,
                tuple(self.mcp_stdio_args),
                dict(self.mcp_stdio_env or {}),
                cwd=self.cwd,
                timeout=_ACP_ROUTE_DIAGNOSIS_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - diagnosis must never mask the timeout
            return "Local ReplayLab MCP route could not be checked: %s" % public_error(exc)
        if verdict["ok"]:
            return (
                "The local ReplayLab MCP route is healthy (initialize %.1fs, %d tools), so "
                "the stall is inside the vendor Agent: check its login state, backend "
                "reachability, or an auto-update on first launch, then retry."
                % (verdict["seconds"], verdict["tools"])
            )
        return (
            "The local ReplayLab MCP route FAILED, which is what the vendor waited on: %s"
            % verdict["error"]
        )

    async def _open_once(self, session_new_timeout: float) -> None:
        self._sdk = sdk = _load_acp_sdk()
        self._state = state = _ACPClientState(
            on_delta=None,
            provider_id=self.provider_id,
            allowed_tools=frozenset(self.allowed_tools),
        )
        client = _main_agent_client(
            sdk,
            state,
            frozenset(self.allowed_tools),
            on_elicitation=self.on_elicitation,
        )
        stderr: Any = asyncio.subprocess.DEVNULL
        if self.stderr_path is not None:
            try:
                self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
                self._stderr_handle = open(self.stderr_path, "ab")  # noqa: SIM115
                stderr = self._stderr_handle
            except OSError as exc:
                get_logger().warning("cannot capture ACP stderr: %s", public_error(exc))
        spawn_kwargs: dict[str, Any] = {"cwd": str(self.cwd)}
        if self.env is not None:
            spawn_kwargs["env"] = dict(self.env)
        stack = contextlib.AsyncExitStack()
        try:
            manager = sdk.spawn_agent_process(
                client,
                *self.command,
                transport_kwargs={
                    "stderr": stderr,
                    "limit": _ACP_TRANSPORT_MAX_BYTES,
                    "shutdown_timeout": _ACP_TRANSPORT_SHUTDOWN_TIMEOUT,
                },
                **spawn_kwargs,
            )
            connection, _process = await stack.enter_async_context(
                _managed_agent_process(manager, "ACP main Agent")
            )
            self._connection = connection
            initialize_arguments: dict[str, Any] = {
                "protocol_version": sdk.protocol_version
            }
            if self.on_elicitation is not None:
                initialize_arguments["client_capabilities"] = {
                    "elicitation": {"form": {}}
                }
            initialized = await _wait_for(
                connection.initialize(**initialize_arguments),
                min(self.timeout, 30.0),
                "ACP initialize",
            )
            self._initialized = initialized
            if _requires_explicit_authentication(
                self.provider_id, self.authentication_method
            ):
                await _wait_for(
                    connection.authenticate(method_id=self.authentication_method),
                    min(self.timeout, 60.0),
                    "ACP authenticate",
                )
            mcp_servers = (
                [self._mcp_server_declaration(sdk, initialized)]
                if self.allowed_tools
                else []
            )
            session = await _new_session(
                connection,
                cwd=str(self.cwd),
                mcp_servers=mcp_servers,
                meta=self.session_meta,
                timeout=session_new_timeout,
            )
            session_id = str(getattr(session, "session_id", "") or getattr(session, "sessionId", ""))
            if not session_id:
                raise ConnectorError("ACP session/new returned no session id")
            self._session_id = state.session_id = session_id
            if self.model:
                await self._select_model(connection, session, session_id, self.model)
        except FileNotFoundError as exc:
            await stack.aclose()
            self._close_stderr()
            raise ConnectorError(
                f"cannot start the ACP agent executable '{self.command[0]}'"
            ) from exc
        except BaseException:
            await stack.aclose()
            self._close_stderr()
            raise
        self._stack = stack
        get_logger().info(
            "ACP main Agent session %s opened (%s, host tools via %s MCP)",
            session_id,
            Path(str(self.command[0])).name,
            self.mcp_transport,
        )

    def _mcp_server_declaration(self, sdk: Any, initialized: Any) -> Any:
        """Declare the one task-scoped ReplayLab stdio MCP process directly."""

        del initialized
        if not self.mcp_stdio_command:
            raise ConfigurationError(
                "ACP ReplayLab tools require a direct task-scoped stdio MCP route"
            )
        self.mcp_transport = "stdio"
        environment = [
            {"name": str(name), "value": str(value)}
            for name, value in (self.mcp_stdio_env or {}).items()
        ]
        arguments = {
            "name": REPLAYLAB_MCP_SERVER_NAME,
            "command": self.mcp_stdio_command,
            "args": list(self.mcp_stdio_args),
            "env": environment,
        }
        try:
            return sdk.mcp_server_stdio_type(**arguments)
        except Exception:
            return sdk.mcp_server_stdio_type(type="stdio", **arguments)

    async def close(self) -> None:
        stack, self._stack = self._stack, None
        connection, self._connection = self._connection, None
        self._initialized = None
        if connection is not None and self._session_id:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(connection.cancel(session_id=self._session_id), timeout=2.0)
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as exc:
                get_logger().warning("ACP main Agent cleanup: %s", public_error(exc))
        self._close_stderr()

    def _close_stderr(self) -> None:
        handle, self._stderr_handle = self._stderr_handle, None
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.close()

    async def _select_model(self, connection: Any, session: Any, session_id: str, model: str) -> None:
        options = _field(session, "config_options", "configOptions") or []
        model_option = None
        for option in options:
            category = _enum_value(_field(option, "category")).lower()
            option_id = str(_field(option, "id") or "")
            if category == "model" or option_id.lower() in {"model", "models"}:
                model_option = option
                break
        setter = getattr(connection, "set_config_option", None)
        if model_option is None:
            # acp-claude-code takes the model from ANTHROPIC_MODEL in its environment.
            return
        # session/new is authoritative for the model already selected by the
        # vendor. Cursor can leave a redundant set_config_option request pending
        # indefinitely. Exact equality needs no mutation; do not normalize IDs,
        # strip option suffixes or accept a different/default model here.
        if _field(model_option, "current_value", "currentValue") == model:
            return
        if not callable(setter):
            raise ConnectorError(
                "ACP agent advertised model selection, but the connection does not "
                "support session/set_config_option"
            )
        try:
            await _wait_for(
                setter(
                    config_id=str(_field(model_option, "id") or ""),
                    session_id=session_id,
                    value=model,
                ),
                min(self.timeout, 30.0),
                "ACP model selection",
            )
        except Exception as exc:
            raise ConnectorError(
                f"ACP model selection for '{model}' failed: {public_error(exc)}"
            ) from exc

    # ---------------------------------------------------------------- prompt
    async def prompt(
        self,
        text: str,
        *,
        attachments: Sequence[ProviderBinaryAttachment] = (),
        on_delta: Callable[[str], Any] | None = None,
        on_timeline: Callable[[Mapping[str, Any]], Any] | None = None,
        on_reasoning: Callable[[Mapping[str, Any]], Any] | None = None,
        tool_boundary: Callable[[], None] | None = None,
    ) -> PromptResult:
        if self._stack is None or self._connection is None or self._state is None or self._sdk is None:
            raise ConnectorError("ACP main Agent session is not open")
        if not text.strip():
            raise ConfigurationError("ACP prompt must not be empty")
        _require_acp_attachment_capabilities(self._initialized, attachments)
        state = self._state
        segments: list[str] = [""]
        tool_calls = 0
        seen_tools: set[str] = set()

        async def delta(chunk: str) -> None:
            segments[-1] += chunk
            if on_delta is not None:
                result = on_delta(chunk)
                if inspect.isawaitable(result):
                    await result

        async def timeline(event: Mapping[str, Any]) -> None:
            nonlocal tool_calls
            activity_id = str(event.get("activityId") or "")
            kind = str(event.get("activityKind") or "")
            if activity_id and activity_id not in seen_tools and "plan" not in kind.lower():
                seen_tools.add(activity_id)
                tool_calls += 1
                if segments[-1]:
                    segments.append("")
                if tool_boundary is not None:
                    tool_boundary()
            if on_timeline is not None:
                result = on_timeline(event)
                if inspect.isawaitable(result):
                    await result

        # Reset per-prompt accumulators on the shared state object.
        state.on_delta = delta
        state.on_timeline = timeline
        state.on_reasoning = on_reasoning
        state.text_chunks = []
        state.text_chars = state.text_bytes = 0
        state.reasoning_chars = state.reasoning_bytes = 0
        state.reasoning_started = False
        state.reasoning_truncated = False
        state.policy_error = None
        state.callback_error = None
        state.abort_event = asyncio.Event()
        state.timeline_events = 0

        connection = self._connection
        prompt_task = asyncio.create_task(
            _wait_for(
                connection.prompt(
                    session_id=self._session_id,
                    prompt=_acp_prompt_blocks(self._sdk, text, attachments),
                ),
                self.timeout,
                "ACP session/prompt",
            )
        )
        abort_task = asyncio.create_task(state.abort_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {prompt_task, abort_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if abort_task in done and prompt_task not in done:
                raise (
                    state.callback_error
                    or state.policy_error
                    or ConnectorError("ACP response collection was aborted")
                )
            response = await prompt_task
        except BaseException:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(connection.cancel(session_id=self._session_id), timeout=2.0)
            raise
        finally:
            for task in (prompt_task, abort_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(prompt_task, abort_task, return_exceptions=True)
        if state.policy_error is not None:
            raise state.policy_error
        if state.callback_error is not None:
            raise ConnectorError(f"ACP delta callback failed: {state.callback_error}") from state.callback_error
        stop_reason = _stop_reason(response)
        if stop_reason not in {"end_turn", "max_tokens"}:
            raise ConnectorError(f"ACP Agent stopped without completing the turn: {stop_reason}")
        return PromptResult(
            text="".join(state.text_chunks),
            segments=[segment for segment in segments],
            stop_reason=stop_reason,
            tool_calls=tool_calls,
        )


def _main_agent_client(
    sdk: Any,
    state: _ACPClientState,
    allowed_tools: frozenset[str],
    *,
    on_elicitation: Callable[[str, Any], Any] | None = None,
) -> Any:
    """The connector client without the bounded-denial abort.

    The main Agent has exactly one task-scoped ReplayLab MCP server
    and file/terminal access are denied at the client level.  A permission
    request is granted when it names one of the allowed tools in a recognised
    shape (``_replaylab_tool_for_permission``); anything else is denied without
    aborting the turn.
    """

    base_client = _make_client(sdk, state)
    base_type = type(base_client)

    class MainAgentClient(base_type):  # type: ignore[misc, valid-type]
        async def request_permission(
            self, session_id: str, tool_call: Any, options: list[Any], **kwargs: Any
        ) -> Any:
            del kwargs
            validation_error = _validate_scoped_permission_request(state, session_id, tool_call, options)
            if validation_error:
                return await super().request_permission(session_id, tool_call, options)
            state.permission_ids.discard(str(_field(tool_call, "tool_call_id", "toolCallId") or "").strip())
            if _replaylab_tool_for_permission(tool_call, allowed_tools) is not None:
                for wanted in ("allow_once", "allow_always"):
                    for option in options:
                        kind = _enum_value(_field(option, "kind")).replace("-", "_").lower()
                        if kind == wanted:
                            state.permission_ids.add(
                                str(_field(tool_call, "tool_call_id", "toolCallId") or "").strip()
                            )
                            return _permission_allowed(sdk, option)
                return _permission_denied(sdk)
            return await super().request_permission(session_id, tool_call, options)

        async def create_elicitation(
            self, message: str, mode: Any, **kwargs: Any
        ) -> Any:
            del kwargs
            if on_elicitation is not None:
                try:
                    result = on_elicitation(message, mode)
                    if inspect.isawaitable(result):
                        await result
                except Exception as error:
                    state.callback_error = error
                    state.abort_event.set()
            response_type = sdk.decline_elicitation_response_type
            if response_type is not None:
                try:
                    return response_type(action="decline")
                except Exception:
                    pass
            return {"action": "decline"}

        async def ext_method(
            self, method: str, params: dict[str, Any]
        ) -> dict[str, Any]:
            if state.provider_id == "cursor-acp" and method == "cursor/ask_question":
                projected = _cursor_question_as_elicitation(params)
                if projected is not None and on_elicitation is not None:
                    try:
                        result = on_elicitation(*projected)
                        if inspect.isawaitable(result):
                            await result
                    except Exception as error:
                        state.callback_error = error
                        state.abort_event.set()
            # Cursor must receive a response to unblock its turn. The native
            # ReplayLab task ends as needs-user-input and carries the answer in
            # the next self-contained turn, so this particular ACP request is
            # intentionally marked skipped rather than answered speculatively.
            return await super().ext_method(method, params)

    return MainAgentClient()



def _startup_timeout_stage(exc: BaseException) -> str | None:
    message = str(exc)
    for stage in _ACP_STARTUP_STAGES:
        if message.startswith(stage) and "timed out" in message:
            return stage
    return None


async def _diagnose_local_mcp_route(
    command: str,
    arguments: Sequence[str],
    environment: Mapping[str, str],
    *,
    cwd: Path,
    timeout: float,
) -> dict[str, Any]:
    """Run the declared ReplayLab MCP command once and report initialize/list_tools."""

    import os
    import time

    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    env = dict(os.environ)
    env.update({str(key): str(value) for key, value in environment.items()})
    parameters = StdioServerParameters(
        command=str(command), args=[str(item) for item in arguments], env=env, cwd=str(cwd)
    )
    started = time.monotonic()
    try:
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                await asyncio.wait_for(session.initialize(), timeout=timeout)
                listing = await asyncio.wait_for(session.list_tools(), timeout=timeout)
    except BaseException as exc:  # noqa: BLE001
        detail = _flatten_exception(exc)
        return {"ok": False, "seconds": time.monotonic() - started, "tools": 0, "error": detail}
    tools = len(list(getattr(listing, "tools", ()) or ()))
    return {"ok": True, "seconds": time.monotonic() - started, "tools": tools, "error": ""}


def _flatten_exception(exc: BaseException) -> str:
    nested = getattr(exc, "exceptions", None)
    if nested:
        return "; ".join(_flatten_exception(item) for item in nested)
    text = public_error(exc) if not isinstance(exc, asyncio.TimeoutError) else "timed out"
    return "%s: %s" % (type(exc).__name__, text) if text else type(exc).__name__

__all__ = ["ACPMainAgentSession", "CLAUDE_BUILTIN_TOOLS", "PromptResult", "write_claude_settings"]
