"""Vendor-native ACPMainAgentSession against an in-process fake ACP SDK.

Covers: session lifecycle, direct task-scoped stdio MCP registration,
streaming deltas, tool-call segment boundaries, the fail-closed permission
policy (only ``ReplayLab`` tools from the grant are allowed) and abort on
policy violations.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from replaylab_agent.connectors.acp import _ACPSDK
import replaylab_agent.acp_session as acp_session
import replaylab_agent.native_vendor as vendor
from replaylab_agent.errors import ConnectorError


class _Option:
    def __init__(self, option_id: str, name: str, kind: str) -> None:
        self.option_id = option_id
        self.name = name
        self.kind = kind


class _Connection:
    """Fake ACP agent connection driven by a script."""

    def __init__(self, client: Any, script: Any) -> None:
        self.client = client
        self.script = script
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.session_id = "sess-1"
        self.mcp_servers: list[Any] = []

    http_mcp = False
    config_options: tuple[Any, ...] = ()

    async def initialize(self, **kwargs: Any) -> Any:
        self.calls.append(("initialize", kwargs))
        return SimpleNamespace(
            agent_capabilities=SimpleNamespace(mcp_capabilities=SimpleNamespace(http=self.http_mcp, sse=False))
        )

    async def authenticate(self, **kwargs: Any) -> Any:
        self.calls.append(("authenticate", kwargs))

    accepts_meta = "field_meta"

    async def new_session(self, cwd: str, mcp_servers: list[Any], **kwargs: Any) -> Any:
        if kwargs and self.accepts_meta not in kwargs:
            raise TypeError("unexpected keyword " + ",".join(kwargs))
        self.calls.append(("new_session", {"cwd": cwd, "mcp_servers": mcp_servers, **kwargs}))
        self.mcp_servers = list(mcp_servers or [])
        self.meta = kwargs.get(self.accepts_meta)
        return SimpleNamespace(session_id=self.session_id, config_options=list(self.config_options))

    async def prompt(self, **kwargs: Any) -> Any:
        self.calls.append(("prompt", kwargs))
        return await self.script(self)

    async def cancel(self, **kwargs: Any) -> None:
        self.calls.append(("cancel", kwargs))

    # helpers used by scripts ---------------------------------------------
    async def say(self, text: str) -> None:
        await self.client.session_update(
            self.session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
        )

    async def think(self, text: str) -> None:
        await self.client.session_update(
            self.session_id, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": text}}
        )

    async def tool(self, call_id: str, title: str, *, server: str, tool: str, status: str = "completed") -> Any:
        permission = await self.client.request_permission(
            self.session_id,
            {"toolCallId": call_id, "title": title, "kind": "other", "rawInput": {"server": server, "tool": tool}},
            [_Option("allow", "Allow", "allow_once"), _Option("reject", "Reject", "reject_once")],
        )
        await self.client.session_update(
            self.session_id,
            {"sessionUpdate": "tool_call", "toolCallId": call_id, "title": title, "kind": "other",
             "status": "in_progress", "rawInput": {"server": server, "tool": tool}},
        )
        await self.client.session_update(
            self.session_id,
            {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "title": title, "status": status},
        )
        return permission


class _Manager:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.closed = False

    async def __aenter__(self) -> tuple[Any, Any]:
        return self.connection, None

    async def __aexit__(self, *exc: Any) -> None:
        self.closed = True


def _fake_sdk(script: Any, holder: dict[str, Any]) -> _ACPSDK:
    def spawn_agent_process(client: Any, *command: str, transport_kwargs: Any = None, **kwargs: Any) -> _Manager:
        holder["command"] = command
        holder["spawn_kwargs"] = kwargs
        holder["transport_kwargs"] = transport_kwargs
        connection = _Connection(client, script)
        holder["connection"] = connection
        manager = _Manager(connection)
        holder["manager"] = manager
        return manager

    class Client:  # the SDK base class the connector subclasses
        pass

    return _ACPSDK(
        protocol_version=1,
        spawn_agent_process=spawn_agent_process,
        text_block=lambda text: {"type": "text", "text": text},
        image_block=None,
        embedded_blob_resource=None,
        resource_block=None,
        client_type=Client,
        mcp_server_stdio_type=lambda **kw: ("stdio", kw),
        mcp_server_http_type=lambda **kw: ("http", kw),
        http_header_type=lambda **kw: kw,
        request_permission_response_type=None,
        allowed_outcome_type=None,
        denied_outcome_type=None,
        decline_elicitation_response_type=None,
    )


class ACPMainAgentSessionTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        tmp = getattr(self, "tmp", None)
        if tmp is not None:
            tmp.cleanup()

    def _session(self, holder: dict[str, Any], **overrides: Any) -> acp_session.ACPMainAgentSession:
        self.tmp = tempfile.TemporaryDirectory()
        cwd = Path(self.tmp.name)
        params: dict[str, Any] = dict(
            command=("acp-claude-code",),
            cwd=cwd,
            env={"PATH": "/usr/bin"},
            mcp_stdio_command="replaylab-ai",
            mcp_stdio_args=("-I", "-m", "replaylab_agent.mcp_proxy"),
            mcp_stdio_env={"REPLAYLAB_TASK_ID": "task-1"},
            allowed_tools=frozenset({"inspect_event", "report_task_result"}),
            timeout=30.0,
            authentication_method="claude-login",
            provider_id="claude-code-acp",
            stderr_path=cwd / "stderr.log",
            session_meta=vendor.claude_session_meta(),
        )
        params.update(overrides)
        return acp_session.ACPMainAgentSession(**params)

    async def test_lifecycle_streaming_and_segments(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            await conn.think("planning")
            await conn.say("Let me inspect. ")
            outcome = await conn.tool("call-1", "inspect_event", server="ReplayLab", tool="inspect_event")
            holder["outcome"] = outcome
            await conn.say("The answer is 42.")
            return {"stopReason": "end_turn"}

        deltas: list[str] = []
        timeline: list[dict[str, Any]] = []
        reasoning: list[dict[str, Any]] = []
        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            session = self._session(holder)
            async with session:
                connection: _Connection = holder["connection"]
                names = [name for name, _ in connection.calls]
                self.assertEqual(names, ["initialize", "authenticate", "new_session"])
                # Every vendor receives the direct task-scoped stdio filter.
                kind, mcp = connection.mcp_servers[0]
                self.assertEqual(kind, "stdio")
                self.assertEqual(mcp["name"], "ReplayLab")
                self.assertEqual(mcp["args"], ["-I", "-m", "replaylab_agent.mcp_proxy"])
                env = {item["name"]: item["value"] for item in mcp["env"]}
                self.assertEqual(env["REPLAYLAB_TASK_ID"], "task-1")
                self.assertEqual(session.mcp_transport, "stdio")
                # Only project Skills remain; AskUserQuestion is bridged through
                # ACP form elicitation when the native engine enables it.
                self.assertNotIn("disableBuiltInTools", connection.meta)
                options = connection.meta["claudeCode"]["options"]
                self.assertEqual(options["tools"], ["Skill"])
                self.assertIn("Bash", options["disallowedTools"])
                self.assertNotIn("AskUserQuestion", options["disallowedTools"])
                self.assertEqual(options["allowedTools"], ["mcp__ReplayLab"])
                self.assertEqual(options["settingSources"], ["project"])
                self.assertTrue(options["strictMcpConfig"])
                result = await session.prompt(
                    "hello",
                    on_delta=deltas.append,
                    on_timeline=timeline.append,
                    on_reasoning=reasoning.append,
                )
            self.assertTrue(holder["manager"].closed)
        self.assertEqual(result.text, "Let me inspect. The answer is 42.")
        self.assertEqual(result.final_text, "The answer is 42.")
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual("".join(deltas), result.text)
        self.assertEqual(holder["outcome"], {"outcome": {"outcome": "selected", "optionId": "allow"}})
        self.assertTrue(any(event["activityId"] == "acp-tool-1" for event in timeline))
        self.assertTrue(any("planning" in event.get("text", "") for event in reasoning))
        self.assertEqual(holder["spawn_kwargs"]["cwd"], str(session.cwd))
        self.assertEqual(holder["spawn_kwargs"]["env"], {"PATH": "/usr/bin"})
        self.assertIsNot(holder["transport_kwargs"]["stderr"], asyncio.subprocess.DEVNULL)

    async def test_native_stdio_route_and_form_elicitation(self) -> None:
        holder: dict[str, Any] = {}
        captured: list[tuple[str, Any]] = []

        async def script(conn: _Connection) -> Any:
            response = await conn.client.create_elicitation(
                "Which shader?",
                {
                    "sessionId": conn.session_id,
                    "requestedSchema": {
                        "type": "object",
                        "properties": {
                            "question_0": {
                                "type": "string",
                                "oneOf": [
                                    {"const": "VS", "title": "Vertex shader"},
                                    {"const": "PS", "title": "Pixel shader"},
                                ],
                            }
                        },
                    },
                },
            )
            holder["elicitation_response"] = response
            await conn.say("waiting")
            return {"stopReason": "end_turn"}

        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            async with self._session(
                holder,
                mcp_stdio_command="replaylab-ai",
                mcp_stdio_args=("-m", "replaylab_agent.mcp_proxy"),
                mcp_stdio_env={"REPLAYLAB_TASK_ID": "task-1"},
                on_elicitation=lambda message, mode: captured.append((message, mode)),
            ) as session:
                kind, declaration = holder["connection"].mcp_servers[0]
                self.assertEqual(kind, "stdio")
                self.assertEqual(declaration["command"], "replaylab-ai")
                self.assertEqual(
                    declaration["args"], ["-m", "replaylab_agent.mcp_proxy"]
                )
                self.assertEqual(
                    declaration["env"],
                    [{"name": "REPLAYLAB_TASK_ID", "value": "task-1"}],
                )
                initialize = holder["connection"].calls[0][1]
                self.assertEqual(
                    initialize["client_capabilities"], {"elicitation": {"form": {}}}
                )
                result = await session.prompt("ask")
        self.assertEqual(result.text, "waiting")
        self.assertEqual(captured[0][0], "Which shader?")
        self.assertEqual(holder["elicitation_response"], {"action": "decline"})

    async def _assert_model_selection_failure(self, setter: Any, message: str) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            await conn.say("must not use the default model")
            return {"stopReason": "end_turn"}

        with (
            mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)),
            mock.patch.object(_Connection, "config_options", ({"id": "model", "category": "model"},)),
            mock.patch.object(_Connection, "set_config_option", setter, create=True),
        ):
            session = self._session(holder, model="requested-model", timeout=0.1)
            with self.assertRaisesRegex(ConnectorError, message):
                async with session:
                    await session.prompt("hello")
            self.assertTrue(holder["manager"].closed)
            # A failed open must also reject a later direct prompt on this object.
            with self.assertRaisesRegex(ConnectorError, "session is not open"):
                await session.prompt("retry")

        self.assertNotIn("prompt", [name for name, _ in holder["connection"].calls])
        if setter is not None:
            selected = [args for name, args in holder["connection"].calls if name == "set_config_option"]
            self.assertEqual(selected, [{"config_id": "model", "session_id": "sess-1", "value": "requested-model"}])

    async def test_rejected_model_selection_never_enters_prompt(self) -> None:
        async def reject(conn: _Connection, **kwargs: Any) -> None:
            conn.calls.append(("set_config_option", kwargs))
            raise RuntimeError("requested model is unavailable")

        await self._assert_model_selection_failure(reject, "requested model is unavailable")

    async def test_model_selection_timeout_never_enters_prompt(self) -> None:
        async def stall(conn: _Connection, **kwargs: Any) -> None:
            conn.calls.append(("set_config_option", kwargs))
            await asyncio.Event().wait()

        await self._assert_model_selection_failure(stall, "ACP model selection timed out")

    async def test_advertised_model_selection_requires_sdk_setter(self) -> None:
        await self._assert_model_selection_failure(None, "session/set_config_option")

    async def test_exact_current_model_enters_prompt_without_redundant_setter(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            await conn.say("requested model")
            return {"stopReason": "end_turn"}

        setter = mock.AsyncMock(side_effect=AssertionError("redundant model mutation"))
        selected = "composer-2.5[fast=true]"
        with (
            mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)),
            mock.patch.object(_Connection, "config_options", (
                {"id": "model", "category": "model", "currentValue": selected},)),
            mock.patch.object(_Connection, "set_config_option", setter, create=True),
        ):
            async with self._session(holder, model=selected) as session:
                result = await session.prompt("hello")
        self.assertEqual(result.text, "requested model")
        setter.assert_not_called()

    async def test_current_model_with_different_option_suffix_still_requires_selection(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            raise AssertionError("wrong model must not receive prompt")

        setter = mock.AsyncMock(side_effect=RuntimeError("model selection refused"))
        with (
            mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)),
            mock.patch.object(_Connection, "config_options", (
                SimpleNamespace(id="model", category="model", current_value="composer-2.5[fast=false]"),)),
            mock.patch.object(_Connection, "set_config_option", setter, create=True),
        ):
            with self.assertRaisesRegex(ConnectorError, "model selection refused"):
                async with self._session(holder, model="composer-2.5[fast=true]") as session:
                    await session.prompt("hello")
        setter.assert_awaited_once()
        self.assertNotIn("prompt", [name for name, _ in holder["connection"].calls])

    async def test_vendor_environment_model_without_config_interface_is_preserved(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            await conn.say("ok")
            return {"stopReason": "end_turn"}

        environment = {"PATH": "/usr/bin", "ANTHROPIC_MODEL": "requested-model"}
        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            async with self._session(holder, model="requested-model", env=environment) as session:
                result = await session.prompt("hello")

        self.assertEqual(result.text, "ok")
        self.assertEqual(holder["spawn_kwargs"]["env"], environment)
        self.assertNotIn("set_config_option", [name for name, _ in holder["connection"].calls])

    async def test_cursor_question_extension_becomes_form_elicitation(self) -> None:
        holder: dict[str, Any] = {}
        captured: list[tuple[str, Any]] = []

        async def script(conn: _Connection) -> Any:
            holder["question_response"] = await conn.client.ext_method(
                "cursor/ask_question",
                {
                    "toolCallId": "question-1",
                    "title": "Need a shader",
                    "questions": [
                        {
                            "id": "stage",
                            "prompt": "Which shader?",
                            "options": [
                                {"id": "vs", "label": "Vertex shader"},
                                {"id": "ps", "label": "Pixel shader"},
                            ],
                            "allowMultiple": False,
                        }
                    ],
                },
            )
            await conn.say("waiting")
            return {"stopReason": "end_turn"}

        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            async with self._session(
                holder,
                command=("cursor-agent", "acp"),
                authentication_method="cursor_login",
                provider_id="cursor-acp",
                session_meta=None,
                on_elicitation=lambda message, mode: captured.append((message, mode)),
            ) as session:
                self.assertEqual(
                    [name for name, _ in holder["connection"].calls],
                    ["initialize", "new_session"],
                )
                result = await session.prompt("ask")

        self.assertEqual(result.text, "waiting")
        self.assertEqual(captured[0][0], "Need a shader")
        properties = captured[0][1]["requestedSchema"]["properties"]
        self.assertEqual(
            properties["stage"]["oneOf"],
            [
                {"const": "vs", "title": "Vertex shader"},
                {"const": "ps", "title": "Pixel shader"},
            ],
        )
        self.assertEqual(
            holder["question_response"]["outcome"]["outcome"], "skipped"
        )

    async def test_permission_for_foreign_tool_is_denied_not_fatal(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            denied = await conn.tool("call-1", "Bash", server="", tool="bash")
            holder["denied"] = denied
            allowed = await conn.tool("call-2", "inspect_event", server="ReplayLab", tool="inspect_event")
            holder["allowed"] = allowed
            await conn.say("ok")
            return {"stopReason": "end_turn"}

        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            async with self._session(holder) as session:
                result = await session.prompt("hello")
        self.assertEqual(result.text, "ok")
        self.assertEqual(holder["denied"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual(holder["allowed"]["outcome"]["outcome"], "selected")

    async def test_session_new_falls_back_when_sdk_rejects_meta(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            await conn.say("ok")
            return {"stopReason": "end_turn"}

        _Connection.accepts_meta = "nothing-accepted"
        try:
            with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
                async with self._session(holder) as session:
                    await session.prompt("hello")
            self.assertIsNone(holder["connection"].meta)
        finally:
            _Connection.accepts_meta = "field_meta"

    async def test_no_meta_for_vendors_without_one(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            await conn.say("ok")
            return {"stopReason": "end_turn"}

        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            async with self._session(holder, session_meta=None, provider_id="copilot-acp") as session:
                await session.prompt("hello")
        connection: _Connection = holder["connection"]
        _name, params = [call for call in connection.calls if call[0] == "new_session"][0]
        self.assertEqual(set(params), {"cwd", "mcp_servers"})

    def test_write_claude_settings_denies_builtins(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as tmp:
            vendor.write_claude_settings(Path(tmp))
            payload = json.loads((Path(tmp) / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("Bash", payload["permissions"]["deny"])
        self.assertIn("Task", payload["permissions"]["deny"])
        self.assertEqual(
            payload["permissions"]["allow"],
            ["Skill", "AskUserQuestion", "mcp__ReplayLab"],
        )

    async def test_permission_by_title_when_raw_input_is_arguments(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            # Claude Code style: rawInput carries the tool arguments, the title names the tool.
            allowed = await conn.client.request_permission(
                conn.session_id,
                {"toolCallId": "c1", "title": "mcp__ReplayLab__inspect_event", "kind": "other",
                 "rawInput": {"task": "explain EID 5"}},
                [_Option("allow", "Allow", "allow_once")],
            )
            denied = await conn.client.request_permission(
                conn.session_id,
                {"toolCallId": "c2", "title": "Write", "kind": "edit", "rawInput": {"path": "x"}},
                [_Option("allow", "Allow", "allow_once")],
            )
            # Gemini CLI style: only a display title, no rawInput at all.
            gemini = await conn.client.request_permission(
                conn.session_id,
                {"toolCallId": "c3", "title": "inspect_event (ReplayLab MCP Server)", "kind": "other"},
                [_Option("proceed_once", "Allow", "allow_once")],
            )
            # A shell command that merely mentions a tool name is not a tool call.
            shell = await conn.client.request_permission(
                conn.session_id,
                {"toolCallId": "c4", "title": "echo mcp__ReplayLab__inspect_event", "kind": "execute",
                 "rawInput": {"command": "echo"}},
                [_Option("allow", "Allow", "allow_once")],
            )
            holder["allowed"], holder["denied"] = allowed, denied
            holder["gemini"], holder["shell"] = gemini, shell
            await conn.say("ok")
            return {"stopReason": "end_turn"}

        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            async with self._session(holder) as session:
                await session.prompt("hello")
        self.assertEqual(holder["allowed"]["outcome"]["outcome"], "selected")
        self.assertEqual(holder["denied"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual(holder["gemini"]["outcome"]["optionId"], "proceed_once")
        self.assertEqual(holder["shell"], {"outcome": {"outcome": "cancelled"}})

    async def test_unexpected_stop_reason_raises(self) -> None:
        holder: dict[str, Any] = {}

        async def script(conn: _Connection) -> Any:
            await conn.say("partial")
            return {"stopReason": "refusal"}

        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)):
            async with self._session(holder) as session:
                with self.assertRaises(ConnectorError) as raised:
                    await session.prompt("hello")
        self.assertIn("refusal", str(raised.exception))
        self.assertIn(("cancel", {"session_id": "sess-1"}), holder["connection"].calls)

    async def test_missing_executable_is_reported(self) -> None:
        holder: dict[str, Any] = {}

        def spawn(*args: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError("acp-claude-code")

        sdk = _fake_sdk(lambda conn: None, holder)
        sdk.spawn_agent_process = spawn
        with mock.patch.object(acp_session, "_load_acp_sdk", lambda: sdk):
            session = self._session(holder)
            with self.assertRaises(ConnectorError) as raised:
                await session.open()
        self.assertIn("acp-claude-code", str(raised.exception))

    async def test_session_new_timeout_retries_once_with_fresh_process(self) -> None:
        holder: dict[str, Any] = {}
        spawned: list[_Connection] = []
        original = _Connection.new_session

        async def flaky_new_session(conn: _Connection, cwd: str, mcp_servers: list[Any], **kwargs: Any) -> Any:
            if conn not in spawned:
                spawned.append(conn)
            if len(spawned) == 1:
                await asyncio.Event().wait()  # first vendor process never answers
            return await original(conn, cwd, mcp_servers, **kwargs)

        async def script(conn: _Connection) -> Any:
            await conn.say("ready")
            return {"stopReason": "end_turn"}

        with (
            mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(script, holder)),
            mock.patch.object(acp_session, "_ACP_SESSION_NEW_TIMEOUT", 0.05),
            mock.patch.object(acp_session, "_ACP_SESSION_NEW_RETRY_TIMEOUT", 0.5),
            mock.patch.object(_Connection, "new_session", flaky_new_session),
        ):
            session = self._session(holder)
            async with session:
                result = await session.prompt("hello")
        self.assertEqual(len(spawned), 2, "the retry must use a fresh Agent process")
        self.assertIsNot(spawned[0], spawned[1])
        self.assertEqual(result.final_text, "ready")

    async def test_session_new_timeout_twice_names_the_stalled_side(self) -> None:
        holder: dict[str, Any] = {}

        async def stall(conn: _Connection, cwd: str, mcp_servers: list[Any], **kwargs: Any) -> Any:
            await asyncio.Event().wait()

        async def healthy_route(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "seconds": 1.9, "tools": 144, "error": ""}

        with (
            mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(lambda conn: None, holder)),
            mock.patch.object(acp_session, "_ACP_SESSION_NEW_TIMEOUT", 0.05),
            mock.patch.object(acp_session, "_ACP_SESSION_NEW_RETRY_TIMEOUT", 0.05),
            mock.patch.object(acp_session, "_diagnose_local_mcp_route", healthy_route),
            mock.patch.object(_Connection, "new_session", stall),
        ):
            session = self._session(holder)
            with self.assertRaises(ConnectorError) as raised:
                await session.open()
        message = str(raised.exception)
        self.assertIn("ACP session/new timed out twice", message)
        self.assertIn("local ReplayLab MCP route is healthy", message)
        self.assertIn("144 tools", message)
        self.assertTrue(holder["manager"].closed)

    async def test_session_new_timeout_reports_a_broken_local_route(self) -> None:
        holder: dict[str, Any] = {}

        async def stall(conn: _Connection, cwd: str, mcp_servers: list[Any], **kwargs: Any) -> Any:
            await asyncio.Event().wait()

        async def broken_route(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"ok": False, "seconds": 0.4, "tools": 0,
                    "error": "ConfigurationError: ReplayLab MCP Host is missing allowed tool(s): x"}

        with (
            mock.patch.object(acp_session, "_load_acp_sdk", lambda: _fake_sdk(lambda conn: None, holder)),
            mock.patch.object(acp_session, "_ACP_SESSION_NEW_TIMEOUT", 0.05),
            mock.patch.object(acp_session, "_ACP_SESSION_NEW_RETRY_TIMEOUT", 0.05),
            mock.patch.object(acp_session, "_diagnose_local_mcp_route", broken_route),
            mock.patch.object(_Connection, "new_session", stall),
        ):
            session = self._session(holder)
            with self.assertRaises(ConnectorError) as raised:
                await session.open()
        self.assertIn("local ReplayLab MCP route FAILED", str(raised.exception))
        self.assertIn("missing allowed tool(s): x", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
