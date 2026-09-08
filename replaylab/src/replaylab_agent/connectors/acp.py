"""Official Agent Client Protocol adapter for external agent CLIs."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..catalog import REPLAYLAB_MCP_SERVER_NAME
from ..conversation import (
    MAX_CONVERSATION_ATTACHMENT_TOTAL_BINARY_BYTES,
    MAX_CONVERSATION_ATTACHMENTS_PER_TURN,
    ProviderBinaryAttachment,
)
from ..errors import (
    ConfigurationError,
    ConnectorError,
    LifecycleCleanupError,
    PolicyError,
)
from .codex_app_server import (
    DeltaCallback,
    ExternalAgentResult,
    ReasoningCallback,
    TimelineCallback,
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
_ACP_FINAL_PIPE_DRAIN_TIMEOUT = 0.5
_ACP_TRANSPORT_MAX_BYTES = 16 * 1024 * 1024
_ACP_STDERR_TAIL_BYTES = 4000
_CURSOR_PROVIDER_ID = "cursor-acp"


def _requires_explicit_authentication(provider_id: str, method_id: str) -> bool:
    """Return whether the ACP connection must invoke ``authenticate``.

    Cursor officially supports pre-authentication through ``agent login`` (the
    ReplayLab connection wizard uses that path).  Current Cursor ACP builds can
    leave a redundant ``authenticate(cursor_login)`` request pending even when
    the managed profile is already signed in, while ``session/new`` immediately
    validates and accepts the same stored login.  Keep the catalogued method for
    UI/provider metadata, but do not re-enter login on every probe or task.

    Other ACP adapters still receive their declared authentication method.
    """

    return bool(str(method_id or "").strip()) and str(provider_id or "").strip() != _CURSOR_PROVIDER_ID


def _read_capture_tail(capture: Any, limit: int = _ACP_STDERR_TAIL_BYTES) -> str:
    """Return a bounded tail of a captured stderr temp file for diagnostics.

    The ACP connection test spawns the vendor Agent with its stderr redirected to
    a private temporary file so a launch/handshake failure (a missing runtime, a
    crash on startup, an authentication error printed by the CLI) can be shown to
    the user instead of the bare "Connection closed" the transport surfaces. The
    tail is size-capped and blank-line stripped; it never contains credentials the
    Agent would only emit over its protocol channel, not stderr.
    """

    if capture is None:
        return ""
    try:
        capture.flush()
    except Exception:
        pass
    try:
        capture.seek(0)
        data = capture.read()
    except Exception:
        return ""
    if not data:
        return ""
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    if len(data) > limit:
        data = data[-limit:]
    text = data.decode("utf-8", "replace")
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-20:])
_MAX_ACP_PERMISSION_DENIALS = 16
_MAX_ACP_TIMELINE_ACTIVITIES = 128
_MAX_ACP_TIMELINE_EVENTS = 512
_MAX_ACP_ARGUMENT_NAMES = 16
_MAX_ACP_ARGUMENT_NAME_CHARS = 64
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
    image_block: Callable[..., Any] | None
    embedded_blob_resource: Callable[..., Any] | None
    resource_block: Callable[[Any], Any] | None
    client_type: type[Any]
    mcp_server_stdio_type: type[Any]
    mcp_server_http_type: type[Any]
    http_header_type: type[Any]
    request_permission_response_type: type[Any] | None
    allowed_outcome_type: type[Any] | None
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
        # Current ACP session/new uses the discriminated HttpMcpServer union
        # member. Older SDKs exposed the same transport as McpServerHttp.
        "HttpMcpServer": (
            getattr(schema, "HttpMcpServer", None)
            or getattr(schema, "McpServerHttp", None)
        ),
        "HttpHeader": getattr(schema, "HttpHeader", None),
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
        image_block=getattr(acp, "image_block", None),
        embedded_blob_resource=getattr(acp, "embedded_blob_resource", None),
        resource_block=getattr(acp, "resource_block", None),
        client_type=required["Client"],
        mcp_server_stdio_type=required["McpServerStdio"],
        mcp_server_http_type=required["HttpMcpServer"],
        http_header_type=required["HttpHeader"],
        request_permission_response_type=getattr(schema, "RequestPermissionResponse", None),
        allowed_outcome_type=(
            getattr(schema, "AllowedOutcome", None)
            or getattr(schema, "SelectedPermissionOutcome", None)
        ),
        denied_outcome_type=getattr(schema, "DeniedOutcome", None),
        decline_elicitation_response_type=getattr(schema, "DeclineElicitationResponse", None),
    )


def _acp_attachment_uri(attachment: ProviderBinaryAttachment) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", attachment.title).strip("._")
    safe_name = (safe_name or ("image" if attachment.kind == "image" else "file"))[:96]
    return "attachment://" + quote(attachment.id, safe="._-") + "/" + quote(
        safe_name, safe="._-"
    )


def _acp_prompt_blocks(
    sdk: _ACPSDK,
    prompt: str,
    attachments: Sequence[ProviderBinaryAttachment],
) -> list[Any]:
    values = tuple(attachments)
    if len(values) > MAX_CONVERSATION_ATTACHMENTS_PER_TURN:
        raise ConfigurationError("ACP attachment count exceeds the per-turn limit")
    if sum(len(attachment.data) for attachment in values) > (
        MAX_CONVERSATION_ATTACHMENT_TOTAL_BINARY_BYTES
    ):
        raise ConfigurationError("ACP attachments exceed the 8 MiB per-turn limit")
    blocks: list[Any] = [sdk.text_block(prompt)]
    for attachment in values:
        encoded = base64.b64encode(attachment.data).decode("ascii")
        uri = _acp_attachment_uri(attachment)
        if attachment.kind == "image":
            if sdk.image_block is None:
                raise ConnectorError(
                    "installed ACP SDK cannot transport image attachments; update "
                    "agent-client-protocol"
                )
            blocks.append(
                sdk.image_block(
                    data=encoded,
                    mime_type=attachment.media_type,
                    uri=uri,
                )
            )
            continue
        if sdk.embedded_blob_resource is None or sdk.resource_block is None:
            raise ConnectorError(
                "installed ACP SDK cannot transport file attachments; update "
                "agent-client-protocol"
            )
        resource = sdk.embedded_blob_resource(
            uri=uri,
            blob=encoded,
            mime_type=attachment.media_type,
        )
        blocks.append(sdk.resource_block(resource))
    return blocks


def _require_acp_attachment_capabilities(
    initialized: Any,
    attachments: Sequence[ProviderBinaryAttachment],
) -> None:
    values = tuple(attachments)
    if not values:
        return
    capabilities = getattr(initialized, "agent_capabilities", None)
    prompt_capabilities = getattr(capabilities, "prompt_capabilities", None)
    if any(attachment.kind == "image" for attachment in values) and (
        getattr(prompt_capabilities, "image", False) is not True
    ):
        raise ConnectorError(
            "the selected ACP Agent does not advertise image prompt support"
        )
    if any(attachment.kind == "file" for attachment in values) and (
        getattr(prompt_capabilities, "embedded_context", False) is not True
    ):
        raise ConnectorError(
            "the selected ACP Agent does not advertise embedded file context support"
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


async def _finalize_agent_process_transport(process: Any, label: str) -> None:
    """Reap the SDK-owned process and close its asyncio pipe transports.

    Python 3.10's Windows subprocess ``wait()`` completes when the process
    exits, before Proactor pipe ``connection_lost`` callbacks necessarily run.
    The ACP SDK closes stdin and waits for the process, but a short-lived CLI
    may close the event loop while stdout and the parent subprocess transport
    are still awaiting finalization. Drain any final protocol bytes briefly,
    then explicitly close the transport while its event loop is still alive.
    """

    stdin = getattr(process, "stdin", None)
    if stdin is not None:
        close_stdin = getattr(stdin, "close", None)
        if callable(close_stdin):
            close_stdin()
        wait_closed = getattr(stdin, "wait_closed", None)
        if callable(wait_closed):
            closed = wait_closed()
            if inspect.isawaitable(closed):
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    await closed

    wait = getattr(process, "wait", None)
    if callable(wait):
        waited = wait()
        if not inspect.isawaitable(waited):
            raise LifecycleCleanupError(f"{label} process wait is not awaitable")
        await asyncio.wait_for(waited, timeout=_ACP_FORCE_CLEANUP_TIMEOUT)

    try:
        communicate = getattr(process, "communicate", None)
        if callable(communicate):
            drained = communicate()
            if inspect.isawaitable(drained):
                with contextlib.suppress(
                    asyncio.TimeoutError,
                    BrokenPipeError,
                    ConnectionResetError,
                    OSError,
                ):
                    await asyncio.wait_for(
                        drained, timeout=_ACP_FINAL_PIPE_DRAIN_TIMEOUT
                    )
    finally:
        # asyncio.subprocess.Process has no public close method. The transport
        # is the owner of all Proactor pipe handles, and close() is idempotent.
        transport = getattr(process, "_transport", None)
        close_transport = getattr(transport, "close", None)
        if callable(close_transport):
            close_transport()
            # Let queued Proactor connection_lost callbacks run before an
            # enclosing asyncio.run() closes the event loop.
            await asyncio.sleep(0)


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
                result = await asyncio.wait_for(
                    asyncio.shield(cleanup_task), timeout=_ACP_CLEANUP_TIMEOUT
                )
                if process is not None:
                    await _finalize_agent_process_transport(process, label)
                return result
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
                await _finalize_agent_process_transport(process, label)
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
    """Bounded ACP connection and model diagnostics.

    Real ReplayLab tasks use :class:`ACPMainAgentSession` so the vendor owns
    the reasoning loop and receives the task-scoped MCP server directly.  This
    connector deliberately has no workflow, proxy, receipt, or audit state.
    """

    def __init__(
        self,
        command: Sequence[str] | str,
        *,
        cwd: Path | str | None = None,
        timeout: float = 300.0,
        env: Mapping[str, str] | None = None,
        authentication_method: str = "",
        provider_id: str = "",
        session_meta: Mapping[str, Any] | None = None,
        stderr_path: Path | str | None = None,
    ) -> None:
        if isinstance(command, str):
            command = (command,)
        self.command = tuple(str(value) for value in command)
        if (
            not self.command
            or not self.command[0]
            or any("\0" in value for value in self.command)
        ):
            raise ConfigurationError("ACP agent command must be a non-empty argv sequence")
        self.cwd = Path(cwd or Path.cwd()).resolve()
        if not self.cwd.is_dir():
            raise ConfigurationError(f"ACP working directory does not exist: {self.cwd}")
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
        self.session_meta = dict(session_meta) if session_meta else None
        self.stderr_path = Path(stderr_path).resolve() if stderr_path else None
        self._run_lock = asyncio.Lock()

    async def _authenticate(self, connection: Any) -> None:
        if not _requires_explicit_authentication(
            self.provider_id, self.authentication_method
        ):
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
            )
            client = _make_client(sdk, state)
            spawn_kwargs: dict[str, Any] = {"cwd": str(self.cwd)}
            if self.env is not None:
                spawn_kwargs["env"] = self.env
            # Capture the vendor Agent's stderr to a private temp file so a launch
            # or handshake failure reports the CLI's real error instead of a bare
            # "Connection closed" from the transport. The file is read only on the
            # failure paths below and always closed in the finally block.
            stderr_capture: Any = None
            with contextlib.suppress(Exception):
                stderr_capture = tempfile.TemporaryFile()
            try:
                manager = sdk.spawn_agent_process(
                    client,
                    *self.command,
                    transport_kwargs={
                        "stderr": stderr_capture
                        if stderr_capture is not None
                        else asyncio.subprocess.DEVNULL,
                        "limit": _ACP_TRANSPORT_MAX_BYTES,
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
                raise ConnectorError(
                    "cannot start ACP agent; verify the configured executable and "
                    "connection settings"
                ) from exc
            except TypeError as exc:
                raise ConnectorError(
                    "installed Agent Client Protocol SDK has an incompatible handshake API"
                ) from exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = str(exc) or type(exc).__name__
                stderr_tail = _read_capture_tail(stderr_capture)
                if stderr_tail:
                    detail = f"{detail}; agent stderr: {stderr_tail}"
                raise ConnectorError(f"ACP connection test failed: {detail}") from exc
            finally:
                if stderr_capture is not None:
                    with contextlib.suppress(Exception):
                        stderr_capture.close()

    async def test_model(
        self, *, prompt: str = "Reply with exactly: OK"
    ) -> dict[str, Any]:
        """Send one minimal prompt to confirm the configured model actually generates.

        Unlike ``test_connection`` this DOES spend a small amount of provider
        quota: it runs a single short turn with no ReplayLab tools, so an invalid
        or plan-restricted model id surfaces here instead of on the user's first
        real prompt. The model under test is chosen by the agent's own environment
        (e.g. ANTHROPIC_MODEL), which the caller sets before constructing this
        connector. Returns ``{"ok": True, "stopReason": ...}`` on success and
        raises ConnectorError (with the Agent's stderr tail) otherwise.
        """

        async with self._run_lock:
            sdk = _load_acp_sdk()
            state = _ACPClientState(
                on_delta=None,
                provider_id=self.provider_id,
            )
            client = _make_client(sdk, state)
            spawn_kwargs: dict[str, Any] = {"cwd": str(self.cwd)}
            if self.env is not None:
                spawn_kwargs["env"] = self.env
            stderr_capture: Any = None
            with contextlib.suppress(Exception):
                stderr_capture = tempfile.TemporaryFile()
            try:
                manager = sdk.spawn_agent_process(
                    client,
                    *self.command,
                    transport_kwargs={
                        "stderr": stderr_capture
                        if stderr_capture is not None
                        else asyncio.subprocess.DEVNULL,
                        "limit": _ACP_TRANSPORT_MAX_BYTES,
                        "shutdown_timeout": _ACP_TRANSPORT_SHUTDOWN_TIMEOUT,
                    },
                    **spawn_kwargs,
                )
                async with _managed_agent_process(manager, "ACP model test") as (
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
                    prompt_response = await _wait_for(
                        connection.prompt(
                            session_id=session_id,
                            prompt=_acp_prompt_blocks(sdk, prompt, ()),
                        ),
                        self.timeout,
                        "ACP session/prompt",
                    )
                    if state.policy_error is not None:
                        raise state.policy_error
                    # Reaching here means the model produced a full turn without the
                    # transport or CLI raising; the stop reason is informational.
                    return {"ok": True, "stopReason": _stop_reason(prompt_response) or "end_turn"}
            except (ConfigurationError, ConnectorError, PolicyError):
                raise
            except FileNotFoundError as exc:
                raise ConnectorError(
                    "cannot start ACP agent; verify the configured executable and "
                    "connection settings"
                ) from exc
            except TypeError as exc:
                raise ConnectorError(
                    "installed Agent Client Protocol SDK has an incompatible handshake API"
                ) from exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = str(exc) or type(exc).__name__
                stderr_tail = _read_capture_tail(stderr_capture)
                if stderr_tail:
                    detail = f"{detail}; agent stderr: {stderr_tail}"
                raise ConnectorError(f"ACP model test failed: {detail}") from exc
            finally:
                if stderr_capture is not None:
                    with contextlib.suppress(Exception):
                        stderr_capture.close()



@dataclass(slots=True)
class _ACPClientState:
    on_delta: DeltaCallback | None
    on_timeline: TimelineCallback | None = None
    on_reasoning: ReasoningCallback | None = None
    provider_id: str = ""
    allowed_tools: frozenset[str] = frozenset()
    session_id: str = ""
    permission_denials: int = 0
    permission_ids: set[str] = field(default_factory=set)
    text_chunks: list[str] = field(default_factory=list)
    text_chars: int = 0
    text_bytes: int = 0
    reasoning_chars: int = 0
    reasoning_bytes: int = 0
    reasoning_started: bool = False
    reasoning_truncated: bool = False
    policy_error: PolicyError | None = None
    callback_error: Exception | None = None
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)
    timeline_events: int = 0
    tool_activity_ids: dict[str, str] = field(default_factory=dict)
    tool_statuses: dict[str, str] = field(default_factory=dict)
    tool_kinds: dict[str, str] = field(default_factory=dict)
    tool_targets: dict[str, str] = field(default_factory=dict)
    tool_argument_names: dict[str, tuple[str, ...]] = field(default_factory=dict)
    plan_activity_ids: dict[str, str] = field(default_factory=dict)
    plan_statuses: dict[str, str] = field(default_factory=dict)


async def _forward_reasoning(
    state: _ACPClientState,
    *,
    text: str = "",
    lifecycle: str = "updated",
    availability: str = "available",
) -> None:
    callback = state.on_reasoning
    if callback is None or state.callback_error is not None:
        return
    item_id = f"acp-thought:{state.session_id or 'pending'}"
    event = {
        "source": "acp",
        "channel": "raw",
        "itemId": item_id,
        "reasoningId": json.dumps(
            [item_id, "raw", 0], ensure_ascii=False, separators=(",", ":")
        ),
        "contentIndex": 0,
        "contentFormat": "text/plain",
        "text": text,
        "reset": False,
        "lifecycle": lifecycle,
        "availability": availability,
    }
    try:
        result = callback(event)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        state.callback_error = exc
        state.abort_event.set()


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
            validation_error = _validate_scoped_permission_request(
                state, session_id, tool_call, options
            )
            if validation_error:
                state.policy_error = PolicyError(
                    "ACP agent sent an invalid permission request; ReplayLab cancelled it "
                    f"(fail-closed: {validation_error})"
                )
                state.abort_event.set()
                return _permission_denied(sdk)

            allowed_option = _scoped_replaylab_permission_option(
                state, tool_call, options
            )
            if allowed_option is not None:
                # This is not ambient Agent authority. The exact server/tool is
                # already in this fresh session's Host grant, and the direct
                # MCP filter enforces the allow-list independently. Prefer
                # allow_once; ACP allow_always is session-scoped.
                return _permission_allowed(sdk, allowed_option)

            state.permission_denials += 1
            if state.permission_denials > _MAX_ACP_PERMISSION_DENIALS:
                state.policy_error = PolicyError(
                    "ACP agent exceeded the bounded permission-denial limit; "
                    "ReplayLab cancelled the turn (fail-closed)"
                )
                state.abort_event.set()
            # Every ACP provider expects a permission response. An otherwise
            # well-formed request outside the task grant is denied without
            # aborting the connector, so the child can continue through an
            # admitted ReplayLab route or return a text-only observation.
            return _permission_denied(sdk)

        async def create_elicitation(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            _record_policy_denial(state, "request interactive input")
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
            raise _terminal_policy_violation(
                state, f"invoke unknown ACP extension method '{method}'"
            )

        async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
            del params
            if (
                state.provider_id == _CURSOR_PROVIDER_ID
                and method in _CURSOR_SAFE_NOTIFICATIONS
            ):
                return
            _terminal_policy_violation(
                state, f"send unknown ACP extension notification '{method}'"
            )

        async def session_update(
            self,
            session_id: str,
            update: Any,
            **kwargs: Any,
        ) -> None:
            del kwargs
            if state.session_id and session_id != state.session_id:
                return
            update_kind = _update_kind(update)
            if update_kind == "agent_thought_chunk":
                text = _extract_text(update)
                if not text or state.callback_error is not None or state.reasoning_truncated:
                    return
                try:
                    state.reasoning_chars, state.reasoning_bytes = checked_response_size(
                        text, state.reasoning_chars, state.reasoning_bytes
                    )
                except Exception:
                    # Raw-trace limits are observational and must not abort an
                    # otherwise valid provider turn. The runtime owns the
                    # authoritative task-wide truncation marker.
                    state.reasoning_truncated = True
                    await _forward_reasoning(
                        state,
                        lifecycle="truncated",
                        availability="limit-reached",
                    )
                    return
                lifecycle = "started" if not state.reasoning_started else "updated"
                state.reasoning_started = True
                await _forward_reasoning(state, text=text, lifecycle=lifecycle)
                return

            if update_kind in {
                "plan",
                "plan_update",
                "plan_removed",
                "tool_call",
                "tool_call_update",
            }:
                await _handle_public_timeline_update(state, update, update_kind)
                return

            if not _is_agent_message(update):
                return
            text = _extract_text(update)
            if not text or state.callback_error is not None:
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


def _cursor_question_as_elicitation(
    params: Mapping[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    """Project Cursor's first blocking question into ACP form-elicitation shape."""

    questions = params.get("questions")
    if not isinstance(questions, list):
        return None
    for raw_question in questions:
        if not isinstance(raw_question, Mapping):
            continue
        prompt = str(raw_question.get("prompt") or "").strip()[:2400]
        options = raw_question.get("options")
        if not prompt or not isinstance(options, list):
            continue
        variants: list[dict[str, str]] = []
        for raw_option in options[:6]:
            if not isinstance(raw_option, Mapping):
                continue
            option_id = str(raw_option.get("id") or "").strip()[:64]
            label = str(raw_option.get("label") or "").strip()[:240]
            if option_id and label:
                variants.append({"const": option_id, "title": label})
        if not variants:
            continue
        property_id = str(raw_question.get("id") or "question").strip()[:64]
        property_schema: dict[str, Any] = {"description": prompt}
        if raw_question.get("allowMultiple") is True:
            property_schema.update(
                {
                    "type": "array",
                    "items": {"anyOf": variants},
                }
            )
        else:
            property_schema["oneOf"] = variants
        title = str(params.get("title") or prompt).strip()[:2400]
        return (
            title,
            {
                "requestedSchema": {
                    "type": "object",
                    "properties": {property_id or "question": property_schema},
                }
            },
        )
    return None


def _policy_denial(state: _ACPClientState, action: str) -> PolicyError:
    error = PolicyError(
        f"ACP Agent attempted to {action}; ReplayLab denied it. Continue with the "
        "task-scoped ReplayLab MCP tools instead."
    )
    _record_policy_denial(state, action)
    if state.policy_error is not None:
        return state.policy_error
    return error


def _terminal_policy_violation(state: _ACPClientState, action: str) -> PolicyError:
    """Abort on an unknown protocol capability, not on a recoverable denial."""

    error = PolicyError(
        f"ACP Agent attempted to {action}; ReplayLab cancelled the turn "
        "(fail-closed: unknown protocol capability)"
    )
    state.permission_denials += 1
    if state.policy_error is None:
        state.policy_error = error
    state.abort_event.set()
    return state.policy_error


def _record_policy_denial(state: _ACPClientState, action: str) -> None:
    state.permission_denials += 1
    if state.permission_denials > _MAX_ACP_PERMISSION_DENIALS:
        state.policy_error = PolicyError(
            f"ACP Agent repeatedly attempted to {action}; ReplayLab cancelled the turn "
            "after the bounded denial limit (fail-closed)"
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


def _permission_allowed(sdk: _ACPSDK, option: Any) -> Any:
    option_id = str(_field(option, "option_id", "optionId") or "").strip()
    response_type = sdk.request_permission_response_type
    outcome_type = sdk.allowed_outcome_type
    if response_type is not None and outcome_type is not None:
        for kwargs in (
            {"optionId": option_id, "outcome": "selected"},
            {"option_id": option_id, "outcome": "selected"},
            {"optionId": option_id},
            {"option_id": option_id},
        ):
            try:
                return response_type(outcome=outcome_type(**kwargs))
            except Exception:
                continue
    # The ACP protocol also permits the equivalent mapping form.
    return {"outcome": {"outcome": "selected", "optionId": option_id}}


async def _new_session(
    connection: Any,
    *,
    cwd: str,
    mcp_servers: list[Any],
    meta: Mapping[str, Any] | None,
    timeout: float,
) -> Any:
    """``session/new`` with an optional vendor ``_meta`` block.

    SDK revisions spell the extra field differently (``field_meta`` / ``meta`` /
    ``_meta``), so the variants are tried in order and a plain request is the
    last resort.  Without *meta* this is a single plain request.
    """

    attempts: list[dict[str, Any]] = [{}]
    if meta:
        payload = dict(meta)
        attempts = [{"field_meta": payload}, {"meta": payload}, {"_meta": payload}, {}]
    last_error: Exception | None = None
    for extra in attempts:
        try:
            return await _wait_for(
                connection.new_session(cwd=cwd, mcp_servers=mcp_servers, **extra),
                timeout,
                "ACP session/new",
            )
        except TypeError as exc:  # unknown keyword on this SDK revision
            last_error = exc
            continue
        except Exception as exc:
            message = str(exc).lower()
            if extra and ("validation" in message or "extra" in message or "unexpected" in message):
                last_error = exc
                continue
            raise
    assert last_error is not None
    raise last_error


# Tool-call kinds that can never be a ReplayLab MCP call: shell commands and
# file mutations are the agent's own built-in tools.
_NON_MCP_TOOL_KINDS = frozenset({"execute", "edit", "delete", "move"})


def _tool_from_label(label: Any, allowed_tools: frozenset[str]) -> str | None:
    """Map a permission request's ``name``/``title`` onto one allowed tool.

    Only exact, vendor-known spellings match: ``mcp__ReplayLab__<tool>``
    (Claude Code), ``<tool> (ReplayLab MCP Server)`` (Gemini CLI family),
    ``ReplayLab__<tool>`` / ``ReplayLab:<tool>`` and the bare tool name.  No
    substring matching: a shell command that merely mentions a tool name does
    not qualify.
    """

    if type(label) is not str:
        return None
    text = label.strip().casefold()
    if not text or len(text) > 512:
        return None
    server = REPLAYLAB_MCP_SERVER_NAME.casefold()
    for tool in allowed_tools:
        lowered = tool.casefold()
        if text in (
            lowered,
            f"mcp__{server}__{lowered}",
            f"{server}__{lowered}",
            f"{server}_{lowered}",
            f"{server}:{lowered}",
            f"{server}: {lowered}",
            f"{server}/{lowered}",
            f"{server}.{lowered}",
            f"{server} - {lowered}",
            f"{lowered} ({server} mcp server)",
            f"{lowered} ({server})",
        ):
            return tool
    return None


def _replaylab_tool_for_permission(tool_call: Any, allowed_tools: frozenset[str]) -> str | None:
    """Return the allowed ReplayLab tool a permission request names, else ``None``.

    Two shapes are recognised.  Structured: ``rawInput.server`` +
    ``rawInput.tool`` (Zed-style adapters).  Labelled: ``rawInput`` carries the
    tool *arguments* and the tool is identified by ``name``/``title`` (Claude
    Code's ``claude-agent-acp`` sends ``mcp__ReplayLab__<tool>``, Gemini CLI
    sends ``<tool> (ReplayLab MCP Server)``).  The label path is safe because the
    only MCP endpoint the agent can reach is the task-scoped filter, which
    enforces the allow-list and the capture scope on every call regardless of
    what was approved here.
    """

    raw_input = _field(tool_call, "raw_input", "rawInput")
    if isinstance(raw_input, Mapping):
        server = raw_input.get("server")
        tool = raw_input.get("tool")
        if type(server) is str and type(tool) is str:
            if server == REPLAYLAB_MCP_SERVER_NAME and tool in allowed_tools:
                return tool
            return None
    kind = _enum_value(_field(tool_call, "kind")).replace("-", "_").lower()
    if kind in _NON_MCP_TOOL_KINDS:
        return None
    for label in (
        _field(tool_call, "name", "tool_name", "toolName"),
        _field(tool_call, "title"),
    ):
        tool = _tool_from_label(label, allowed_tools)
        if tool is not None:
            return tool
    return None


def _scoped_replaylab_permission_option(
    state: _ACPClientState,
    tool_call: Any,
    options: list[Any],
) -> Any | None:
    """Select only a Host-exposed ReplayLab MCP permission option.

    The request must name the first-party MCP server and one tool from this
    session's Host allow-list (see ``_replaylab_tool_for_permission`` for the
    accepted shapes); option names are presentation and are ignored.
    """

    if _replaylab_tool_for_permission(tool_call, state.allowed_tools) is None:
        return None
    for wanted_kind in ("allow_once", "allow_always"):
        for option in options:
            kind = _enum_value(_field(option, "kind")).replace("-", "_").lower()
            if kind == wanted_kind:
                return option
    return None


def _validate_scoped_permission_request(
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
    raw_tool_call_id = _field(tool_call, "tool_call_id", "toolCallId")
    if type(raw_tool_call_id) is not str:
        return "invalid tool-call id"
    tool_call_id = raw_tool_call_id.strip()
    if not tool_call_id or len(tool_call_id) > 256 or "\0" in tool_call_id:
        return "invalid tool-call id"
    if tool_call_id in state.permission_ids:
        return "duplicate tool-call id"
    if not isinstance(options, list) or not 1 <= len(options) <= 32:
        return "invalid option count"
    option_ids: set[str] = set()
    for option in options:
        raw_option_id = _field(option, "option_id", "optionId")
        raw_name = _field(option, "name")
        if type(raw_option_id) is not str or type(raw_name) is not str:
            return "invalid permission option"
        option_id = raw_option_id.strip()
        name = raw_name.strip()
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
    state.permission_ids.add(tool_call_id)
    return ""


def _update_kind(update: Any) -> str:
    """Return only the stable ACP session-update discriminator."""

    value = _field(update, "session_update", "sessionUpdate")
    if isinstance(value, str):
        return value.strip().casefold()
    # Keep a small compatibility fallback for SDK objects from ACP releases
    # which did not expose the discriminator as a normal attribute.
    return {
        "AgentMessageChunk": "agent_message_chunk",
        "AgentThoughtChunk": "agent_thought_chunk",
        "AgentPlanUpdate": "plan",
        "AgentPlanContentUpdate": "plan_update",
        "AgentPlanRemovedUpdate": "plan_removed",
        "ToolCallStart": "tool_call",
        "ToolCallProgress": "tool_call_update",
        "ToolCallUpdate": "tool_call_update",
    }.get(type(update).__name__, "")


def _opaque_activity_id(
    values: dict[str, str], raw_value: Any, prefix: str
) -> tuple[str, str]:
    """Map a provider id to a bounded opaque id that cannot leak its contents."""

    if isinstance(raw_value, str) and raw_value and len(raw_value) <= 256:
        key = raw_value
    else:
        key = f"__anonymous_{len(values) + 1}"
    activity_id = values.get(key)
    if activity_id is not None:
        return key, activity_id
    if len(values) >= _MAX_ACP_TIMELINE_ACTIVITIES:
        return "", ""
    activity_id = f"acp-{prefix}-{len(values) + 1}"
    values[key] = activity_id
    return key, activity_id


def _agent_reported_event(
    *,
    activity_id: str,
    activity_kind: str,
    title: str,
    status: str,
    lifecycle: str,
    detail: str = "",
) -> dict[str, Any]:
    """Build a public ACP row whose provenance cannot be mistaken for Host audit."""

    return {
        "activityId": activity_id,
        "activityKind": activity_kind,
        "activityTitle": title[:256],
        "activityDetail": detail[:4096],
        "publicDetail": detail[:4096],
        "status": status if status in {"running", "completed", "failed"} else "running",
        "lifecycle": (
            lifecycle
            if lifecycle in {"started", "updated", "completed"}
            else "updated"
        ),
        "elapsedSeconds": 0,
        # ACP notifications are provider claims.  Only ReplayLab's separate
        # MCP audit can establish that a tool actually ran and was validated.
        "verification": "agentReported",
        "statusSource": "agent",
        "hostVerified": False,
    }


def _agent_status(value: Any, fallback: str = "running") -> str:
    status = value if isinstance(value, str) else _enum_value(value)
    normalized = status.strip().replace("-", "_").casefold()
    if normalized in {"completed", "success", "succeeded"}:
        return "completed"
    if normalized in {"failed", "error", "cancelled", "canceled", "declined"}:
        return "failed"
    if normalized in {"pending", "in_progress", "running", "started"}:
        return "running"
    return fallback if fallback in {"running", "completed", "failed"} else "running"


def _plan_entries(update: Any, update_kind: str) -> Sequence[Any]:
    if update_kind == "plan":
        entries = _field(update, "entries")
    elif update_kind == "plan_update":
        entries = _field(_field(update, "plan"), "entries")
    else:
        entries = None
    if isinstance(entries, Sequence) and not isinstance(
        entries, (str, bytes, bytearray)
    ):
        return entries[:32]
    return ()


def _plan_key(update: Any, update_kind: str) -> Any:
    if update_kind == "plan":
        return "__legacy_plan"
    if update_kind == "plan_removed":
        return _field(update, "plan_id", "planId")
    plan = _field(update, "plan")
    return _field(plan, "plan_id", "planId")


def _plan_summary(entries: Sequence[Any]) -> tuple[str, str, dict[str, int]]:
    counts = {"pending": 0, "in_progress": 0, "completed": 0}
    for entry in entries:
        value = _field(entry, "status")
        status = value if isinstance(value, str) else _enum_value(value)
        status = status.strip().replace("-", "_").casefold()
        if status in counts:
            counts[status] += 1
    total = sum(counts.values())
    if not total:
        return "running", "", {"total": 0, **counts}
    overall = "completed" if counts["completed"] == total else "running"
    detail = (
        f"Steps: {total}; pending: {counts['pending']}; "
        f"in progress: {counts['in_progress']}; completed: {counts['completed']}"
    )
    return overall, detail, {"total": total, **counts}


def _safe_public_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    name = value.strip()
    if not name or len(name) > _MAX_ACP_ARGUMENT_NAME_CHARS or not name.isascii():
        return ""
    if not (name[0].isalnum() or name[0] == "_"):
        return ""
    if any(not (char.isalnum() or char in "_.-") for char in name):
        return ""
    compact = "".join(char for char in name.casefold() if char.isalnum())
    forbidden = (
        "apikey",
        "accesskey",
        "privatekey",
        "key",
        "token",
        "secret",
        "password",
        "passwd",
        "credential",
        "authorization",
        "auth",
        "cookie",
        "rawinput",
        "rawoutput",
        "content",
        "location",
        "path",
        "uri",
        "command",
        "cwd",
        "environment",
        "error",
        "output",
        "result",
        "python",
        "repr",
        "class",
        "object",
    )
    if any(fragment in compact for fragment in forbidden):
        return ""
    return name


def _argument_names(update: Any) -> tuple[str, ...]:
    """Inspect only input object keys; provider-controlled values stay untouched."""

    raw_input = _field(update, "raw_input", "rawInput")
    if not isinstance(raw_input, Mapping):
        return ()
    nested = _field(raw_input, "arguments", "args", "input")
    values = nested if isinstance(nested, Mapping) else raw_input
    names: list[str] = []
    for raw_name in values:
        if len(names) >= _MAX_ACP_ARGUMENT_NAMES:
            break
        name = _safe_public_name(raw_name)
        if name and name not in names:
            names.append(name)
    return tuple(sorted(names, key=str.casefold))


def _safe_tool_name_from_title(update: Any, allowed_tools: frozenset[str]) -> str:
    """Recognize only exact, configured ReplayLab tool spellings (title or name).

    Besides the historic ``ReplayLab:<tool>`` / ``Mcp(ReplayLab:<tool>)``
    forms this accepts the vendor spellings ``_tool_from_label`` knows
    (``mcp__ReplayLab__<tool>`` from Claude Code, ``<tool> (ReplayLab MCP
    Server)`` from Gemini CLI), so the Agent Monitor shows the real tool name
    instead of "tool activity".
    """

    safe = frozenset(
        configured for configured in allowed_tools
        if _safe_public_name(configured) == configured
    )
    title = _field(update, "title")
    if isinstance(title, str) and len(title) <= 256:
        for name in sorted(safe):
            if title in {
                name,
                f"ReplayLab.{name}",
                f"ReplayLab:{name}",
                f"Mcp(ReplayLab:{name})",
                f"MCP(ReplayLab:{name})",
            }:
                return name
    for label in (_field(update, "name", "tool_name", "toolName"), title):
        name = _tool_from_label(label, safe)
        if name:
            return name
    return ""


def _safe_tool_kind(value: Any, fallback: str = "other") -> str:
    kind = value if isinstance(value, str) else _enum_value(value)
    kind = kind.strip().replace("-", "_").casefold()
    if kind in {
        "read",
        "edit",
        "delete",
        "move",
        "search",
        "execute",
        "think",
        "fetch",
        "switch_mode",
        "other",
    }:
        return kind
    return fallback


def _generic_tool_title(kind: str, status: str) -> str:
    label = {
        "read": "read activity",
        "edit": "edit activity",
        "delete": "delete activity",
        "move": "move activity",
        "search": "search activity",
        "execute": "execution activity",
        "think": "thinking activity",
        "fetch": "fetch activity",
        "switch_mode": "mode switch",
        "other": "tool activity",
    }.get(kind, "tool activity")
    if status == "completed":
        return f"Agent reported {label} completed"
    if status == "failed":
        return f"Agent reported {label} failed"
    return f"Agent reported {label} running"


async def _handle_public_timeline_update(
    state: _ACPClientState, update: Any, update_kind: str
) -> None:
    callback = state.on_timeline
    if callback is None or state.callback_error is not None:
        return
    if state.timeline_events >= _MAX_ACP_TIMELINE_EVENTS:
        return

    event: dict[str, Any] | None = None
    if update_kind in {"plan", "plan_update", "plan_removed"}:
        key, activity_id = _opaque_activity_id(
            state.plan_activity_ids,
            _plan_key(update, update_kind),
            "plan",
        )
        if not activity_id:
            return
        previous = state.plan_statuses.get(key, "")
        plan_counts: dict[str, int] | None = None
        if update_kind == "plan_removed":
            status = "completed"
            detail = ""
            title = "Agent reported plan removed"
        else:
            status, detail, plan_counts = _plan_summary(
                _plan_entries(update, update_kind)
            )
            if status == "completed":
                title = "Agent reported plan completed"
            elif previous:
                title = "Agent reported a plan update"
            else:
                title = "Agent reported a plan"
        lifecycle = (
            "completed"
            if status in {"completed", "failed"} or update_kind == "plan_removed"
            else ("updated" if previous else "started")
        )
        state.plan_statuses[key] = status
        event = _agent_reported_event(
            activity_id=activity_id,
            activity_kind="plan",
            title=title,
            detail=detail,
            status=status,
            lifecycle=lifecycle,
        )
        if plan_counts is not None:
            event["planStepCounts"] = {
                "total": plan_counts["total"],
                "pending": plan_counts["pending"],
                "inProgress": plan_counts["in_progress"],
                "completed": plan_counts["completed"],
            }

    elif update_kind in {"tool_call", "tool_call_update"}:
        key, activity_id = _opaque_activity_id(
            state.tool_activity_ids,
            _field(update, "tool_call_id", "toolCallId"),
            "tool",
        )
        if not activity_id:
            return
        previous_status = state.tool_statuses.get(key, "running")
        status = _agent_status(_field(update, "status"), previous_status)
        kind = _safe_tool_kind(
            _field(update, "kind"), state.tool_kinds.get(key, "other")
        )
        target_name = _safe_tool_name_from_title(update, state.allowed_tools)
        if not target_name:
            target_name = state.tool_targets.get(key, "")
        names = _argument_names(update)
        if not names:
            names = state.tool_argument_names.get(key, ())
        state.tool_statuses[key] = status
        state.tool_kinds[key] = kind
        if target_name:
            state.tool_targets[key] = target_name
        if names:
            state.tool_argument_names[key] = names

        lifecycle = (
            "completed"
            if status in {"completed", "failed"}
            else ("started" if update_kind == "tool_call" else "updated")
        )
        detail = "Arguments: " + ", ".join(names) if names else ""
        if target_name:
            target = f"ReplayLab.{target_name}"
            if status == "completed":
                title = f"Agent reported {target} completed"
            elif status == "failed":
                title = f"Agent reported {target} failed"
            else:
                title = f"Agent reported calling {target}"
            event = _agent_reported_event(
                activity_id=activity_id,
                activity_kind="mcpToolCall",
                title=title,
                detail=detail,
                status=status,
                lifecycle=lifecycle,
            )
            event["activityTarget"] = target
        else:
            event = _agent_reported_event(
                activity_id=activity_id,
                activity_kind="toolCall",
                title=_generic_tool_title(kind, status),
                detail=detail,
                status=status,
                lifecycle=lifecycle,
            )
        event["argumentNames"] = list(names)

    if event is None:
        return
    state.timeline_events += 1
    event["sequence"] = state.timeline_events
    try:
        result = callback(dict(event))
        if inspect.isawaitable(result):
            await result
    except Exception:
        # Do not reflect a callback's exception text into the task failure UI;
        # it may contain a Python repr, filesystem path, or credential value.
        state.callback_error = ConnectorError("ACP public timeline callback failed")
        state.abort_event.set()


def _is_agent_message(update: Any) -> bool:
    return _update_kind(update) == "agent_message_chunk"


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
