"""API-mode engine on `deepagents <https://github.com/langchain-ai/deepagents>`_.

Model API connections use a maintained open-source agent loop. ReplayLab only
contributes

* the model (``connectors.langchain_models.build_chat_model``),
* the RenderDoc tools discovered from one task-scoped ReplayLab MCP process,
* one sub-agent per Skill (system prompt = standing rules + Skill text) plus a
  read-only ``general-purpose`` one,
* ``ask_user`` (ends the run with ``needs-user-input``; the answer comes back
  in the next request's conversation envelope),
* the event mapping onto the panel's ``replaylab-ai-task-event/v1`` stream.

Everything that makes the output consistent (compile/ABI/review gates, the
Host-rendered flow chart, the analysis template) lives in the Host tools, so
this engine and the subscription-mode vendor loops produce the same
artifacts.

All model API profiles select this engine. ``deepagents`` and the supported
LangChain provider adapters are required AgentHost dependencies.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .conversation import (
    PROVIDER_NATIVE_DOCUMENT_MEDIA_TYPES,
    PROVIDER_NATIVE_IMAGE_MEDIA_TYPES,
    prepare_provider_conversation_input,
    parse_conversation_prompt,
)
from .errors import ConfigurationError, ConnectorError
from .events import TaskEventWriter
from .response_language import infer_response_language
from .agent_diagnostics import configure_logging, get_logger, log_exception, public_error
from .agent_contracts import OrchestratorConfig, RunRequest, RunResult
from .agent_progress import ProgressReporter
from .agent_prompts import (
    deep_child_system_prompt,
    deep_main_system_prompt,
    main_agent_user_prompt,
    verification_follow_up_prompt,
)
from .agent_protocol import ChildResult, ClarificationRequest
from .agent_context import child_tool_grant, normalized_effects
from .agent_registry import CatalogView, SkillInfo
from .agent_report import (
    REPORT_TOOL, REPORT_TOOL_DESCRIPTION, REPORT_TOOL_SCHEMA, TaskReport, TaskReportStore,
    read_task_report_sidecars,
)
from .agent_context import ProviderContext
from .native_mcp import build_native_mcp_route

ENGINE_ID = "deepagents/v1"
GENERAL_SUBAGENT = "general-purpose"
TASK_TOOL = "task"
ASK_USER_TOOL = "ask_user"
DEFAULT_RECURSION_LIMIT = 200
_TOOL_ERROR_PREFIX = "ERROR: "


def import_deepagents() -> Any:
    try:
        import deepagents
    except ImportError as exc:
        raise ConfigurationError(
            "the required deepagents runtime is missing; reinstall ReplayLab AgentHost"
        ) from exc
    return deepagents


# --------------------------------------------------------------------- tools
def _langchain_tool(name: str, function: Any, description: str = "", args_schema: Any = None) -> Any:
    from langchain_core.tools import StructuredTool

    kwargs: dict[str, Any] = {"name": name}
    if description:
        kwargs["description"] = description
    if args_schema is not None:
        kwargs["args_schema"] = args_schema
    if inspect.iscoroutinefunction(function):
        kwargs["coroutine"] = function
    else:
        kwargs["func"] = function
    return StructuredTool.from_function(**kwargs)


def _tool_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(value)


def _mcp_result_text(result: Any) -> str:
    parts: list[str] = []
    for block in list(getattr(result, "content", ()) or ()):
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
            continue
        dump = getattr(block, "model_dump", None)
        if callable(dump):
            parts.append(_tool_text(dump(by_alias=True)))
    rendered = "\n".join(part for part in parts if part)
    if getattr(result, "isError", getattr(result, "is_error", False)):
        return _TOOL_ERROR_PREFIX + (rendered or "ReplayLab MCP tool failed")
    return rendered or _tool_text(result)


class ReplayLabMCPTools:
    """One direct, task-scoped MCP session shared by all deepagents children."""

    def __init__(
        self,
        request: RunRequest,
        config: OrchestratorConfig,
        event_root: Any,
    ) -> None:
        self.request = request
        self.config = config
        self.event_root = event_root
        self._stack = AsyncExitStack()
        self._context: ProviderContext | None = None
        self._turn_root: Any = None

    async def __aenter__(self) -> dict[str, Any]:
        try:
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client
        except ImportError as exc:
            raise ConfigurationError(
                "the required MCP client runtime is missing; reinstall ReplayLab AgentHost"
            ) from exc

        request = self.request
        self._context = ProviderContext(
            profile=request.profile,
            model=request.model or request.profile.model,
            instance_id=request.instance_id,
            context=dict(request.context or {}),
            intent=request.intent,
            mcp_executable=request.mcp_executable,
            workspace_base=self.config.workspace_base,
            log_root=self.config.log_root,
            main_timeout=self.config.main_timeout,
            max_tool_calls=self.config.max_tool_calls,
            task_id=request.task_id,
        )
        workspace, self._turn_root = self._context.new_turn_workspace()
        route = build_native_mcp_route(
            self._context,
            workspace=workspace,
            event_root=self.event_root,
            allowed_effects=self.config.allowed_effects,
        )
        if route is None:
            return {}
        try:
            streams = await self._stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=route.command,
                        args=list(route.args),
                        env=dict(route.env),
                    )
                )
            )
            session = await self._stack.enter_async_context(ClientSession(*streams))
            await session.initialize()
            listing = await session.list_tools()
            tools: dict[str, Any] = {}
            for spec in list(getattr(listing, "tools", ()) or ()):
                name = str(getattr(spec, "name", "") or "")
                if not name or name == REPORT_TOOL or name not in route.allowed_tools:
                    continue
                description = str(getattr(spec, "description", "") or "")
                schema = getattr(spec, "inputSchema", None)
                if schema is None:
                    schema = getattr(spec, "input_schema", None)

                def make(tool_name: str) -> Any:
                    async def call(**arguments: Any) -> str:
                        try:
                            return _mcp_result_text(
                                await session.call_tool(tool_name, dict(arguments))
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            message = public_error(exc)
                            get_logger().warning("tool %s failed: %s", tool_name, message)
                            return _TOOL_ERROR_PREFIX + message

                    call.__name__ = tool_name
                    call.__doc__ = description or f"Call ReplayLab MCP tool {tool_name}."
                    return call

                tools[name] = _langchain_tool(
                    name,
                    make(name),
                    description=description,
                    args_schema=schema,
                )
            return tools
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        with contextlib.suppress(Exception):
            await self._stack.aclose()
        if self._context is not None:
            self._context.cleanup_turn_workspace(self._turn_root)


# ------------------------------------------------------------------ engine
@dataclass(slots=True)
class _Child:
    child_id: str
    run_id: str
    subagent: str
    description: str
    started: float
    tool_calls: int = 0
    tools_used: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)


class DeepAgentsEngine:
    """Run model API profiles through deepagents and the first-party MCP Host."""

    def __init__(
        self,
        *,
        catalog: CatalogView,
        config: OrchestratorConfig | None = None,
        deepagents_module: Any = None,
        tool_session_factory: Any = None,
        model_factory: Any = None,
    ) -> None:
        self.catalog = catalog
        self.config = config or OrchestratorConfig()
        self._deepagents = deepagents_module
        self._tool_session_factory = tool_session_factory
        self._model_factory = model_factory
        self.log = configure_logging(self.config.log_root)

    async def run(self, request: RunRequest) -> RunResult:
        writer = TaskEventWriter(self.config.event_root, request.task_id)
        # TaskEventWriter generates an ID when the caller leaves it blank.  All
        # MCP scope, sidecars, logs and the final result must use that one ID.
        request.task_id = writer.task_id
        profile = request.profile
        reporter = ProgressReporter(
            writer,
            profile_id=profile.id,
            backend=profile.provider.protocol,
            strategy=ENGINE_ID,
        )
        reporter.queued(
            prompt_chars=len(request.prompt),
            preset=profile.preset,
            profile_title=profile.title or profile.preset,
            model=request.model or profile.model,
        )
        run = _DeepRun(self, request, reporter, event_root=writer.root.resolve())
        self.log.info(
            "task %s started (deepagents): profile=%s model=%s prompt_chars=%d",
            request.task_id,
            profile.id,
            request.model or profile.model or "<default>",
            len(request.prompt),
        )
        try:
            result = await run.execute()
        except asyncio.CancelledError:
            reporter.cancelled()
            self.log.info("task %s cancelled", request.task_id)
            raise
        except Exception as exc:
            log_exception(self.log, f"task {request.task_id} failed", exc)
            message = public_error(exc)
            reporter.failed(message)
            return RunResult(
                task_id=request.task_id,
                profile_id=profile.id,
                executor=profile.provider.title,
                text="",
                terminal_status="failed",
                children=list(run.children),
                error=message,
            )
        self.log.info(
            "task %s finished: status=%s children=%d chars=%d",
            request.task_id,
            result.terminal_status,
            len(result.children),
            len(result.text),
        )
        return result

    # ------------------------------------------------------------ plumbing
    def deepagents(self) -> Any:
        if self._deepagents is None:
            self._deepagents = import_deepagents()
        return self._deepagents

    def tool_session(self, request: RunRequest, event_root: Any) -> Any:
        if self._tool_session_factory is not None:
            return self._tool_session_factory(request, self.config, event_root)
        return ReplayLabMCPTools(request, self.config, event_root)

    def model(self, request: RunRequest) -> Any:
        if self._model_factory is not None:
            return self._model_factory(request.profile, request.model)
        from .connectors.langchain_models import build_chat_model

        return build_chat_model(request.profile, model=request.model)

    def subagent_effects(self) -> tuple[str, ...]:
        # Effects are a connection-level ceiling here (deepagents picks the
        # sub-agent, not the effect). Preserve the configured replay allowance
        # so shader review can run a temporary output comparison. The Skill
        # still requires user authorization for persistent apply/live watch.
        return tuple(sorted(self.config.allowed_effects))


class _DeepRun:
    def __init__(
        self,
        engine: DeepAgentsEngine,
        request: RunRequest,
        reporter: ProgressReporter,
        *,
        event_root: Any,
    ) -> None:
        self.engine = engine
        self.catalog = engine.catalog
        self.config = engine.config
        self.request = request
        self.reporter = reporter
        self.event_root = event_root
        self.renderdoc_tool_map: dict[str, Any] = {}
        self.children: list[ChildResult] = []
        self.clarification: ClarificationRequest | None = None
        self._active: dict[str, _Child] = {}
        self._main_text: list[str] = []
        self._final_text = ""
        self._main_tool_calls = 0
        self._started = time.monotonic()
        self.report_store = TaskReportStore(request.task_id, event_root)
        self.report_gate = self.report_store.gate

    def tools_used(self) -> set[str]:
        self.report_store.sync()
        return set(self.report_store.tools_used())

    def host_facts(self) -> tuple[dict[str, Any], ...]:
        unused_report, state = read_task_report_sidecars(self.event_root, self.request.task_id)
        facts = (state or {}).get("toolFacts", [])
        records = [dict(item) for item in facts if isinstance(item, Mapping)] if isinstance(facts, list) else []
        paths = (state or {}).get("artifactPaths", [])
        if isinstance(paths, list) and paths:
            records.append({"tool": "Host task artifact ledger", "outcome": "observed paths",
                            "summary": json.dumps(paths[:16], ensure_ascii=False)[:1800], "truncated": len(paths) > 16})
        return tuple(records)

    # ------------------------------------------------------------ building
    def subagent_specs(self) -> list[dict[str, Any]]:
        request = self.request
        specs: list[dict[str, Any]] = []
        if not request.instance_id:
            return [self._subagent(
                name=GENERAL_SUBAGENT,
                description="General knowledge only; no capture or Host tools are available.",
                skill=None, effects=(), tools=[],
            )]
        effects = self.engine.subagent_effects()
        skills = [skill for skill in self.catalog.skills.values() if not skill.always]
        for skill in skills:
            specs.append(
                self._subagent(
                    name=skill.id,
                    description=skill.description,
                    skill=skill,
                    effects=effects,
                    tools=self._tools_for(effects),
                )
            )
        specs.append(
            self._subagent(
                name=GENERAL_SUBAGENT,
                description="Read-only RenderDoc inspection for anything no Skill covers (events, pipeline state, resources, pixels, counters).",
                skill=None,
                effects=(),
                tools=self._tools_for(()),
            )
        )
        return specs

    def _tools_for(self, effects: Sequence[str]) -> list[Any]:
        allowed = child_tool_grant(normalized_effects(effects))
        return [
            self.renderdoc_tool_map[name]
            for name in sorted(self.renderdoc_tool_map)
            if name in allowed
        ]

    def _subagent(
        self,
        *,
        name: str,
        description: str,
        skill: SkillInfo | None,
        effects: Sequence[str],
        tools: list[Any],
    ) -> dict[str, Any]:
        prompt = deep_child_system_prompt(
            catalog=self.catalog, skill=skill, context=self.request.context, effects=effects
        )
        spec: dict[str, Any] = {"name": name, "description": description, "tools": tools}
        spec[_subagent_prompt_key(self.engine.deepagents())] = prompt
        return spec

    def ask_user_tool(self) -> Any:
        async def ask_user(
            question: str,
            options: list[dict[str, Any]] | None = None,
            selection_mode: str = "single",
            allow_other: bool = True,
            clarification_id: str = "",
        ) -> str:
            """Ask the user a question with 2-6 choices and END this turn.

            Use only when a material choice cannot be inferred. After calling it,
            stop working and reply with one short sentence; the Host delivers the
            question and the answer arrives with the user's next message.

            Args:
                question: The question shown to the user.
                options: 2-6 choices, each ``{"id": ..., "label": ..., "description": ...}``.
                selection_mode: ``single`` or ``multiple``.
                allow_other: Whether the user may type a free-text answer instead.
                clarification_id: Optional stable id for the question.
            """

            if self.clarification is not None:
                return _TOOL_ERROR_PREFIX + "a question is already pending; end your turn now"
            try:
                request = ClarificationRequest.model_validate(
                    {
                        "question": question,
                        "options": options or [],
                        "selection_mode": selection_mode,
                        "allow_other": allow_other,
                        "clarification_id": clarification_id,
                    }
                )
            except Exception as exc:
                return _TOOL_ERROR_PREFIX + f"invalid ask_user call: {public_error(exc)}"
            self.clarification = request
            get_logger().info("ask_user: %s", request.question[:200])
            return "Question recorded. End your turn now with one short sentence; do not call more tools."

        return _langchain_tool(ASK_USER_TOOL, ask_user)

    def report_tool(self) -> Any:
        store = self.report_store

        async def report_task_result(**arguments: Any) -> str:
            """Submit raw fields to the Host gate, including malformed forms."""
            return _tool_text(store.submit(arguments))

        return _langchain_tool(REPORT_TOOL, report_task_result, description=REPORT_TOOL_DESCRIPTION,
                               args_schema=REPORT_TOOL_SCHEMA)

    def build_agent(self) -> tuple[Any, list[tuple[str, str]]]:
        deepagents = self.engine.deepagents()
        envelope = parse_conversation_prompt(self.request.prompt)
        current = envelope.current_request
        language = infer_response_language(current, fallback_locale="en")
        subagents = self.subagent_specs()
        listing = [(s["name"], s["description"]) for s in subagents]
        system_prompt = deep_main_system_prompt(
            catalog=self.catalog,
            request=current,
            context=self.request.context,
            response_language=language,
            subagents=listing,
        )
        create = deepagents.create_deep_agent
        kwargs: dict[str, Any] = {
            "model": self.engine.model(self.request),
            "tools": [self.ask_user_tool(), self.report_tool()],
            "subagents": subagents,
        }
        kwargs[_create_prompt_key(create)] = system_prompt
        return create(**kwargs), listing

    def user_prompt(self) -> tuple[Any, str]:
        envelope = parse_conversation_prompt(self.request.prompt)
        current = envelope.current_request
        answer = ""
        if envelope.clarification_response is not None:
            response = envelope.clarification_response
            selected = ", ".join(response.option_ids)
            answer = (
                f"clarification {response.clarification_id}: selected option(s) = [{selected}]"
                + (f"; free text = {response.other_text}" if response.other_text else "")
            )
        notes: list[str] = []
        if self.request.intent and self.request.intent.get("kind"):
            notes.append(
                f"(This request was started from the RenderDoc button '{self.request.intent.get('kind')}'; "
                "the frozen environment identifies the exact target. Run one task for it.)"
            )
        if self.request.skill_name:
            notes.append(f"(The user selected the Skill '{self.request.skill_name}': use that sub-agent.)")
        provider_input = prepare_provider_conversation_input(
            self.request.prompt,
            provider_name=self.request.profile.provider.title,
            native_image_media_types=(
                PROVIDER_NATIVE_IMAGE_MEDIA_TYPES
                if self.request.profile.options.get("image_input") is True
                else ()
            ),
            native_file_media_types=(
                PROVIDER_NATIVE_DOCUMENT_MEDIA_TYPES
                if self.request.profile.options.get("document_input") is True
                else ()
            ),
        )
        decorated = provider_input.text
        if notes:
            decorated += "\n\n" + "\n".join(notes)
        language = infer_response_language(current, fallback_locale="en")
        text = main_agent_user_prompt(
            request=decorated,
            history=(),
            clarification_answer=answer,
        )
        if not provider_input.attachments:
            return text, language
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for attachment in provider_input.attachments:
            content.append(
                {
                    "type": "image" if attachment.kind == "image" else "file",
                    "id": attachment.id,
                    "mime_type": attachment.media_type,
                    "base64": base64.b64encode(attachment.data).decode("ascii"),
                }
            )
        return content, language

    # ------------------------------------------------------------- running
    async def execute(self) -> RunResult:
        if self.request.instance_id:
            async with self.engine.tool_session(self.request, self.event_root) as tools:
                self.renderdoc_tool_map = dict(tools)
                return await self._execute_with_tools()
        return await self._execute_with_tools()

    async def _execute_with_tools(self) -> RunResult:
        agent, _listing = self.build_agent()
        prompt, language = self.user_prompt()
        self.reporter.main_started()
        await self._main_turn(agent, prompt)
        while self.clarification is None:
            reasons = self.report_gate.verify(worked=bool(self.tools_used()))
            if not reasons or not self.report_store.request_follow_up(reasons):
                break
            get_logger().info(
                "verification round %d/%d: %s",
                self.report_gate.follow_ups,
                self.report_gate.verify_limit,
                "; ".join(item["code"] for item in reasons),
            )
            follow_up = verification_follow_up_prompt(
                request=parse_conversation_prompt(self.request.prompt).current_request,
                reasons=reasons,
                previous_answer=(self._final_text or "".join(self._main_text)),
                children=self.children,
                round_index=self.report_gate.follow_ups,
                rounds=self.report_gate.verify_limit,
                tools_used=tuple(sorted(self.tools_used())),
                host_facts=self.host_facts(),
            )
            self._main_text, self._final_text = [], ""
            await self._main_turn(agent, follow_up)

        failed = sum(1 for child in self.children if child.status != "completed")
        report: TaskReport | None = self.report_gate.report
        if self.clarification is not None:
            text = self.clarification.question
            status = "needs-user-input"
        else:
            text = (self._final_text or "".join(self._main_text)).strip()
            if not text and report is not None:
                text = report.summary
            if not text:
                text = "主 Agent 没有返回文本答案。" if language == "zh_CN" else "The main Agent returned no text answer."
                status = "partial"
            else:
                status = "partial" if failed else "completed"
            status = self.report_gate.terminal_status(status, worked=bool(self.tools_used()))
        self.reporter.main_completed(
            f"Main Agent finished ({len(self.children)} sub-agent task(s), {self._main_tool_calls} tool call(s))"
        )
        self.reporter.final_answer(text)
        self.reporter.completed(
            text=text,
            terminal_status=status,
            clarification=self.clarification,
            context=self.request.context,
            instance_id=self.request.instance_id,
            report=self.report_gate.event_payload(),
            verification=self.report_gate.verification_payload(),
        )
        return RunResult(
            task_id=self.request.task_id,
            profile_id=self.request.profile.id,
            executor=self.request.profile.provider.title,
            text=text,
            terminal_status=status,
            clarification=self.clarification,
            children=list(self.children),
            report=self.report_gate.event_payload(),
        )

    async def _main_turn(self, agent: Any, prompt: Any) -> None:
        """Run one main-Agent turn and settle unfinished sub-agents."""

        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            await asyncio.wait_for(self._stream(agent, prompt), timeout=self.config.main_timeout)
        except asyncio.TimeoutError as exc:
            raise ConnectorError(f"the deepagents run exceeded {self.config.main_timeout:.0f}s") from exc
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self._settle_children("cancelled", "run ended before the sub-agent reported")

    async def _heartbeat(self) -> None:
        interval = max(0.05, float(self.config.heartbeat_seconds))
        while True:
            await asyncio.sleep(interval)
            self.reporter.main_heartbeat(
                elapsed_seconds=int(time.monotonic() - self._started), children=len(self.children)
            )

    async def _stream(self, agent: Any, prompt: Any) -> None:
        config = {
            "recursion_limit": DEFAULT_RECURSION_LIMIT,
            "configurable": {"thread_id": self.request.task_id or "replaylab"},
        }
        payload = {"messages": [{"role": "user", "content": prompt}]}
        try:
            async for event in agent.astream_events(payload, config=config, version="v2"):
                self._on_event(event)
        except (ConnectorError, ConfigurationError, asyncio.CancelledError):
            raise
        except Exception as exc:
            raise ConnectorError(f"deepagents run failed: {public_error(exc)}") from exc

    # -------------------------------------------------------------- events
    def _child_for(self, event: Mapping[str, Any]) -> _Child | None:
        for run_id in reversed(list(event.get("parent_ids") or [])):
            child = self._active.get(str(run_id))
            if child is not None:
                return child
        return None

    def _on_event(self, event: Mapping[str, Any]) -> None:
        kind = str(event.get("event") or "")
        name = str(event.get("name") or "")
        run_id = str(event.get("run_id") or "")
        data = event.get("data") or {}
        child = self._child_for(event)

        if kind == "on_tool_start":
            arguments = data.get("input") if isinstance(data.get("input"), Mapping) else {}
            if name == TASK_TOOL and child is None:
                self._start_child(run_id, arguments)
            elif child is not None:
                child.tool_calls += 1
                if name in self.renderdoc_tool_map and name not in child.tools_used:
                    child.tools_used.append(name)
                self.reporter.child_tool_call(child.child_id, name, call_id=run_id, arguments=dict(arguments))
            else:
                self._main_tool_calls += 1
            return
        if kind in {"on_tool_end", "on_tool_error"}:
            output = data.get("output") if kind == "on_tool_end" else data.get("error")
            text = _message_text(output)
            failed = kind == "on_tool_error" or text.startswith(_TOOL_ERROR_PREFIX)
            if name == TASK_TOOL and run_id in self._active:
                self._finish_child(run_id, text, failed)
            elif child is not None:
                self.reporter.child_tool_result(
                    child.child_id, name, call_id=run_id, failed=failed, detail=text[:300] if failed else ""
                )
            return
        if kind == "on_chat_model_stream":
            text, thinking = _chunk_parts(data.get("chunk"))
            if child is not None:
                if text:
                    child.text.append(text)
                    self.reporter.child_message(child.child_id, text, skill=child.subagent)
                if thinking:
                    self.reporter.child_reasoning(child.child_id, {"text": thinking})
            else:
                if text:
                    self._main_text.append(text)
                    self.reporter.main_message(text)
                if thinking:
                    self.reporter.main_reasoning({"text": thinking})
            return
        if kind == "on_chat_model_end" and child is None:
            message = data.get("output")
            if message is not None and not getattr(message, "tool_calls", None):
                text = _message_text(message).strip()
                if text:
                    self._final_text = text
            return

    def _start_child(self, run_id: str, arguments: Mapping[str, Any]) -> None:
        subagent = str(arguments.get("subagent_type") or GENERAL_SUBAGENT)
        description = str(arguments.get("description") or "")
        child_id = self.reporter.new_child_id()
        child = _Child(child_id=child_id, run_id=run_id, subagent=subagent, description=description, started=time.monotonic())
        self._active[run_id] = child
        skill = subagent if subagent in self.catalog.skills else ""
        self.reporter.child_started(
            child_id, skill=skill, agent_id=subagent, summary=description[:200] or f"sub-agent {subagent}"
        )

    def _finish_child(self, run_id: str, report: str, failed: bool) -> None:
        child = self._active.pop(run_id)
        skill = child.subagent if child.subagent in self.catalog.skills else ""
        duration = time.monotonic() - child.started
        if failed:
            self.reporter.child_failed(child.child_id, skill=skill, agent_id=child.subagent, error=report[:2000])
            status = "failed"
        else:
            self.reporter.child_completed(
                child.child_id,
                skill=skill,
                agent_id=child.subagent,
                summary=f"Sub-agent {child.subagent} reported ({len(report)} chars)",
                tool_calls=child.tool_calls,
            )
            status = "completed"
        self.children.append(
            ChildResult(
                child_id=child.child_id,
                status=status,
                report="" if failed else report,
                error=report[:2000] if failed else "",
                agent_id=child.subagent,
                skills=[skill] if skill else [],
                tool_calls=child.tool_calls,
                tools_used=list(child.tools_used),
                duration_seconds=round(duration, 3),
            )
        )

    def _settle_children(self, status: str, error: str) -> None:
        for run_id in list(self._active):
            child = self._active.pop(run_id)
            skill = child.subagent if child.subagent in self.catalog.skills else ""
            self.reporter.child_failed(child.child_id, skill=skill, agent_id=child.subagent, error=error)
            self.children.append(
                ChildResult(
                    child_id=child.child_id,
                    status="cancelled" if status == "cancelled" else "failed",
                    error=error,
                    agent_id=child.subagent,
                    skills=[skill] if skill else [],
                    tool_calls=child.tool_calls,
                    tools_used=list(child.tools_used),
                    duration_seconds=round(time.monotonic() - child.started, 3),
                )
            )


# ------------------------------------------------------------------ helpers
def _create_prompt_key(create: Any) -> str:
    try:
        parameters = inspect.signature(create).parameters
    except (TypeError, ValueError):
        return "system_prompt"
    if "system_prompt" in parameters:
        return "system_prompt"
    if "instructions" in parameters:
        return "instructions"
    return "system_prompt"


def _subagent_prompt_key(deepagents: Any) -> str:
    subagent = getattr(deepagents, "SubAgent", None)
    annotations = getattr(subagent, "__annotations__", None) or {}
    if "system_prompt" in annotations:
        return "system_prompt"
    if "prompt" in annotations:
        return "prompt"
    return "system_prompt"


def _message_text(message: Any) -> str:
    """Text of a LangChain message / chunk / tool output (content blocks flattened)."""

    if message is None:
        return ""
    if isinstance(message, str):
        return message
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping) and block.get("type") in {"text", None} and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    if isinstance(content, Mapping):
        return _tool_text(content)
    return str(content)


def _chunk_parts(chunk: Any) -> tuple[str, str]:
    """(answer text, thinking text) of one streamed model chunk."""

    if chunk is None:
        return "", ""
    text = ""
    thinking = ""
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                text += block
            elif isinstance(block, Mapping):
                kind = block.get("type")
                if kind in {"text", None} and isinstance(block.get("text"), str):
                    text += block["text"]
                elif kind in {"thinking", "reasoning"}:
                    thinking += str(block.get("thinking") or block.get("reasoning") or block.get("text") or "")
    extra = getattr(chunk, "additional_kwargs", None) or {}
    reasoning = extra.get("reasoning") if isinstance(extra, Mapping) else None
    if isinstance(reasoning, Mapping):
        for item in reasoning.get("summary") or []:
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                thinking += item["text"]
    elif isinstance(reasoning, str):
        thinking += reasoning
    return text, thinking


__all__ = ["ENGINE_ID", "DeepAgentsEngine", "ReplayLabMCPTools", "import_deepagents"]
