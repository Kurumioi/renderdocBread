"""Vendor-native single-Agent engine for subscription connections.

ACP/Codex own the reasoning loop and call one task-scoped ReplayLab MCP
server directly.  AgentHost remains responsible for the frozen scope, effect
ceiling, event projection, report sidecars, and bounded layer-3 follow-ups.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .conversation import (
    PROVIDER_NATIVE_DOCUMENT_MEDIA_TYPES,
    PROVIDER_NATIVE_IMAGE_MEDIA_TYPES,
    ProviderBinaryAttachment,
    ProviderPathAttachment,
    prepare_provider_conversation_input,
    parse_conversation_prompt,
)
from .errors import ConfigurationError
from .events import TaskEventWriter
from .response_language import infer_response_language
from .skills import INTENT_SKILLS
from . import native_vendor as vendor
from .acp_session import ACPMainAgentSession, PromptResult
from .agent_diagnostics import configure_logging, log_exception, public_error
from .native_mcp import NativeMCPRoute, build_native_mcp_route, native_codex_argv
from .agent_contracts import OrchestratorConfig, RunRequest, RunResult
from .agent_progress import ProgressReporter
from .agent_prompts import (
    main_agent_user_prompt,
    native_agent_system_prompt,
    verification_follow_up_prompt,
)
from .agent_protocol import ClarificationOption, ClarificationRequest
from .agent_context import ProviderContext
from .agent_registry import CatalogView, SkillInfo
from .agent_report import (
    REPORT_TOOL,
    ReportGate,
    TaskReportStore,
    read_task_report_sidecars,
)

_QUESTION_BLOCK_RE = re.compile(
    r"\[REPLAYLAB QUESTION\]\s*(.*?)\s*\[/REPLAYLAB QUESTION\]",
    re.IGNORECASE | re.DOTALL,
)
_QUESTION_OPTION_RE = re.compile(r"^\s*([1-6])[.)]\s+(.+?)\s*$")


class NativeAgentEngine:
    """Run one vendor-native Agent while keeping Host policy deterministic."""

    def __init__(
        self,
        *,
        catalog: CatalogView,
        config: OrchestratorConfig | None = None,
        acp_session_factory: Callable[..., ACPMainAgentSession] = ACPMainAgentSession,
        codex_connector_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.catalog = catalog
        self.config = config or OrchestratorConfig()
        self._acp_session_factory = acp_session_factory
        self._codex_connector_factory = codex_connector_factory
        self.log = configure_logging(self.config.log_root)

    async def run(self, request: RunRequest) -> RunResult:
        writer = TaskEventWriter(self.config.event_root, request.task_id)
        task_id = writer.task_id
        event_root = writer.root.resolve()
        profile = request.profile
        reporter = ProgressReporter(
            writer,
            profile_id=profile.id,
            backend=profile.provider.protocol,
            strategy="vendor-native-agent/v1",
        )
        reporter.queued(
            prompt_chars=len(request.prompt),
            preset=profile.preset,
            profile_title=profile.title or profile.preset,
            model=request.model or profile.model,
        )
        try:
            return await self._execute(
                request,
                task_id=task_id,
                event_root=event_root,
                reporter=reporter,
            )
        except asyncio.CancelledError:
            reporter.cancelled()
            raise
        except Exception as error:
            log_exception(self.log, f"native task {task_id} failed", error)
            message = public_error(error)
            reporter.failed(message)
            return RunResult(
                task_id=task_id,
                profile_id=profile.id,
                executor=profile.provider.title,
                text="",
                terminal_status="failed",
                error=message,
                core="vendor-native-agent/v1",
            )

    async def _execute(
        self,
        request: RunRequest,
        *,
        task_id: str,
        event_root: Path,
        reporter: ProgressReporter,
    ) -> RunResult:
        profile = request.profile
        protocol = str(profile.provider.protocol)
        if profile.provider.kind != "agent" or protocol not in {"acp", "codex-app-server"}:
            raise ConfigurationError(
                f"vendor-native engine does not support profile protocol '{protocol}'"
            )
        if protocol == "acp" and profile.options.get("allow_unsandboxed_agent") is not True:
            raise ConfigurationError(
                "ACP agents run outside a sandbox; set allow_unsandboxed_agent=true for this connection"
            )

        envelope = parse_conversation_prompt(request.prompt)
        current = envelope.current_request
        clarification_answer = ""
        if envelope.clarification_response is not None:
            answer = envelope.clarification_response
            clarification_answer = (
                f"clarification {answer.clarification_id}: selected option(s) = "
                f"[{', '.join(answer.option_ids)}]"
                + (f"; free text = {answer.other_text}" if answer.other_text else "")
            )
        language = infer_response_language(current, fallback_locale="en")
        context = ProviderContext(
            profile=profile,
            model=request.model or profile.model,
            instance_id=request.instance_id,
            context=dict(request.context or {}),
            intent=request.intent,
            mcp_executable=request.mcp_executable,
            workspace_base=self.config.workspace_base,
            log_root=self.config.log_root,
            main_timeout=self.config.main_timeout,
            max_tool_calls=self.config.max_tool_calls,
            task_id=task_id,
        )
        workspace, turn_root = context.new_turn_workspace()
        report_store = TaskReportStore(task_id, event_root)
        gate = report_store.gate
        tools_used: list[str] = []
        host_facts: list[dict[str, Any]] = []
        clarification: ClarificationRequest | None = None
        selected_skill = self._select_skill(request, current)
        reporter.main_started("Vendor-native Agent is analysing the request")
        try:
            route = build_native_mcp_route(
                context,
                workspace=workspace,
                event_root=event_root,
                allowed_effects=tuple(self.config.allowed_effects),
            )
            system_prompt = native_agent_system_prompt(
                catalog=self.catalog,
                request=current,
                context=request.context,
                response_language=language,
                tool_names=tuple(sorted(route.allowed_tools)) if route else (),
                protocol=(
                    "claude-acp"
                    if profile.preset == "claude-code-acp"
                    else protocol
                ),
                effects=tuple(sorted(route.effects)) if route else (),
                loaded_skill=selected_skill,
            )
            provider_input = prepare_provider_conversation_input(
                request.prompt,
                provider_name=profile.provider.title,
                native_image_media_types=(
                    PROVIDER_NATIVE_IMAGE_MEDIA_TYPES if protocol == "acp" else None
                ),
                native_file_media_types=(
                    PROVIDER_NATIVE_DOCUMENT_MEDIA_TYPES if protocol == "acp" else None
                ),
                native_path_references=protocol == "codex-app-server",
            )
            user_prompt = main_agent_user_prompt(
                request=provider_input.text,
                history=(),
                clarification_answer=clarification_answer,
            )
            first_prompt = system_prompt + "\n\n" + user_prompt

            def sync_report_state() -> None:
                nonlocal tools_used, host_facts
                report_store.sync()
                report_payload, state_payload = read_task_report_sidecars(
                    event_root, task_id
                )
                raw_tools = (state_payload or {}).get("toolsUsed", [])
                if isinstance(raw_tools, list):
                    tools_used = list(
                        dict.fromkeys(
                            str(item) for item in raw_tools if str(item).strip()
                        )
                    )
                raw_facts = (state_payload or {}).get("toolFacts", [])
                host_facts = [dict(item) for item in raw_facts if isinstance(item, dict)][-24:] if isinstance(raw_facts, list) else []
                paths = (state_payload or {}).get("artifactPaths", [])
                if isinstance(paths, list) and paths:
                    host_facts.append({
                        "tool": "Host task artifact ledger", "outcome": "observed paths",
                        "summary": json.dumps(paths[:16], ensure_ascii=False)[:1800],
                        "truncated": len(paths) > 16,
                    })

            def capture_elicitation(message: str, mode: Any) -> None:
                nonlocal clarification
                parsed = clarification_from_elicitation(message, mode)
                if parsed is not None:
                    clarification = parsed

            if protocol == "acp":
                result, clarification = await self._run_acp(
                    context=context,
                    workspace=workspace,
                    route=route,
                    first_prompt=first_prompt,
                    attachments=provider_input.attachments,
                    current_request=current,
                    gate=gate,
                    request_follow_up=report_store.request_follow_up,
                    sync_report_state=sync_report_state,
                    tools_used=lambda: tuple(tools_used),
                    host_facts=lambda: tuple(host_facts),
                    reporter=reporter,
                    capture_elicitation=capture_elicitation,
                    clarification=lambda: clarification,
                )
                text = (result.final_text or result.text or "").strip()
                total_tool_calls = len(tools_used)
            else:
                text, total_tool_calls, clarification = await self._run_codex(
                    context=context,
                    workspace=workspace,
                    route=route,
                    first_prompt=first_prompt,
                    system_prompt=system_prompt,
                    attachments=provider_input.attachments,
                    path_attachments=provider_input.path_attachments,
                    current_request=current,
                    selected_skill=selected_skill,
                    gate=gate,
                    request_follow_up=report_store.request_follow_up,
                    sync_report_state=sync_report_state,
                    tools_used=lambda: tuple(tools_used),
                    host_facts=lambda: tuple(host_facts),
                    reporter=reporter,
                )
            sync_report_state()
        finally:
            context.cleanup_turn_workspace(turn_root)

        report = gate.report
        worked = any(name != REPORT_TOOL for name in tools_used)
        if clarification is not None:
            text = clarification.question
            status = "needs-user-input"
        else:
            if not text and report is not None:
                text = report.summary
            if not text:
                text = "任务没有返回可显示的结果。" if language == "zh_CN" else "The task returned no displayable result."
            status = "partial" if worked and report is None else "completed"
            status = gate.terminal_status(status, worked=worked)
        reporter.main_completed(
            f"Vendor-native Agent finished ({total_tool_calls} ReplayLab tool call(s))"
        )
        reporter.final_answer(text)
        reporter.completed(
            text=text,
            terminal_status=status,
            clarification=clarification,
            context=request.context,
            instance_id=request.instance_id,
            report=gate.event_payload(),
            verification=gate.verification_payload(),
        )
        return RunResult(
            task_id=task_id,
            profile_id=profile.id,
            executor=profile.provider.title,
            text=text,
            terminal_status=status,
            clarification=clarification,
            report=gate.event_payload(),
            core="vendor-native-agent/v1",
        )

    async def _run_acp(
        self,
        *,
        context: ProviderContext,
        workspace: Path,
        route: NativeMCPRoute | None,
        first_prompt: str,
        attachments: tuple[ProviderBinaryAttachment, ...],
        current_request: str,
        gate: ReportGate,
        request_follow_up: Callable[[Any], bool],
        sync_report_state: Callable[[], None],
        tools_used: Callable[[], tuple[str, ...]],
        host_facts: Callable[[], tuple[dict[str, Any], ...]],
        reporter: ProgressReporter,
        capture_elicitation: Callable[[str, Any], None],
        clarification: Callable[[], ClarificationRequest | None],
    ) -> tuple[PromptResult, ClarificationRequest | None]:
        allowed_tools = route.allowed_tools if route else frozenset()
        vendor.prepare_workspace(context.profile, workspace, allowed_tools)
        if context.profile.preset == "claude-code-acp":
            vendor.copy_claude_skills(
                workspace,
                [
                    (skill.id, skill.path)
                    for skill in self.catalog.skills.values()
                    if skill.path is not None
                ],
            )
            if route is not None:
                vendor.write_claude_mcp_config(
                    workspace,
                    command=route.command,
                    args=route.args,
                    env=route.env,
                )
        stderr_path = (
            context.log_root or context.workspace_root().parent / "logs"
        ) / f"acp-native-{context.task_id}.stderr.log"
        session = self._acp_session_factory(
            command=vendor.runtime_argv(
                context.profile,
                context.agent_argv(),
                workspace,
                allowed_tools,
            ),
            cwd=workspace,
            env=context.agent_env(
                vendor.main_agent_env(context.profile, context.main_timeout)
            ),
            allowed_tools=allowed_tools,
            mcp_stdio_command=route.command if route else "",
            mcp_stdio_args=route.args if route else (),
            mcp_stdio_env=route.env if route else None,
            on_elicitation=capture_elicitation,
            timeout=context.main_timeout,
            authentication_method=context.profile.provider.acp_auth_method,
            provider_id=context.profile.preset,
            model=context.model,
            stderr_path=stderr_path,
            session_meta=vendor.session_meta(context.profile),
        )
        async with session:
            result = await self._prompt_acp(
                session, first_prompt, reporter, attachments=attachments
            )
            sync_report_state()
            while clarification() is None:
                reasons = gate.verify(worked=bool(tools_used()))
                if not reasons or not request_follow_up(reasons):
                    break
                follow_up = verification_follow_up_prompt(
                    request=current_request,
                    reasons=reasons,
                    previous_answer=result.final_text or result.text,
                    children=(),
                    round_index=gate.follow_ups,
                    rounds=gate.verify_limit,
                    tools_used=tools_used(),
                    host_facts=host_facts(),
                )
                result = await self._prompt_acp(session, follow_up, reporter)
                sync_report_state()
        return result, clarification()

    async def _prompt_acp(
        self,
        session: ACPMainAgentSession,
        prompt: str,
        reporter: ProgressReporter,
        *,
        attachments: tuple[ProviderBinaryAttachment, ...] = (),
    ) -> PromptResult:
        return await self._with_heartbeat(
            session.prompt(
                prompt,
                on_delta=reporter.main_message,
                on_reasoning=reporter.main_reasoning,
                on_timeline=lambda event: self.log.debug("native ACP timeline: %s", dict(event)),
                attachments=attachments,
            ),
            reporter,
        )

    async def _run_codex(
        self,
        *,
        context: ProviderContext,
        workspace: Path,
        route: NativeMCPRoute | None,
        first_prompt: str,
        system_prompt: str,
        attachments: tuple[ProviderBinaryAttachment, ...],
        path_attachments: tuple[ProviderPathAttachment, ...],
        current_request: str,
        selected_skill: SkillInfo | None,
        gate: ReportGate,
        request_follow_up: Callable[[Any], bool],
        sync_report_state: Callable[[], None],
        tools_used: Callable[[], tuple[str, ...]],
        host_facts: Callable[[], tuple[dict[str, Any], ...]],
        reporter: ProgressReporter,
    ) -> tuple[str, int, ClarificationRequest | None]:
        if self._codex_connector_factory is None:
            from .connectors.codex_app_server import CodexAppServerConnector

            connector_factory = CodexAppServerConnector
        else:
            connector_factory = self._codex_connector_factory
        connector = connector_factory(
            native_codex_argv(context.agent_argv(), route),
            cwd=workspace,
            readable_roots=[workspace],
            expected_mcp_servers=(
                frozenset({"ReplayLab"}) if route is not None else frozenset()
            ),
            timeout=context.main_timeout,
            env=context.agent_env(),
        )
        prompt = first_prompt
        first_turn = True
        text = ""
        clarification: ClarificationRequest | None = None
        try:
            while True:
                run = connector.run(
                    prompt,
                    model=context.model or None,
                    skill_name=(
                        selected_skill.id
                        if selected_skill is not None and selected_skill.path is not None
                        else None
                    ),
                    skill_path=(
                        selected_skill.path
                        if selected_skill is not None and selected_skill.path is not None
                        else None
                    ),
                    on_delta=reporter.main_message,
                    on_reasoning=reporter.main_reasoning,
                    on_timeline=lambda event: self.log.debug(
                        "native Codex timeline: %s", dict(event)
                    ),
                    attachments=attachments if first_turn else (),
                    path_attachments=path_attachments if first_turn else (),
                )
                result = await self._with_heartbeat(run, reporter)
                first_turn = False
                text = str(result.text or "").strip()
                sync_report_state()
                clarification = clarification_from_numbered_text(text)
                if clarification is not None:
                    break
                reasons = gate.verify(worked=bool(tools_used()))
                if not reasons or not request_follow_up(reasons):
                    break
                prompt = system_prompt + "\n\n" + verification_follow_up_prompt(
                    request=current_request,
                    reasons=reasons,
                    previous_answer=text,
                    children=(),
                    round_index=gate.follow_ups,
                    rounds=gate.verify_limit,
                    tools_used=tools_used(),
                    host_facts=host_facts(),
                )
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(connector.close(), timeout=10.0)
        return text, len(tools_used()), clarification

    async def _with_heartbeat(
        self,
        awaitable: Any,
        reporter: ProgressReporter,
    ) -> Any:
        heartbeat = asyncio.create_task(self._heartbeat(reporter))
        try:
            return await awaitable
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(self, reporter: ProgressReporter) -> None:
        started = time.monotonic()
        interval = max(0.05, float(self.config.heartbeat_seconds))
        while True:
            await asyncio.sleep(interval)
            reporter.main_heartbeat(
                elapsed_seconds=int(time.monotonic() - started), children=0
            )

    def _select_skill(self, request: RunRequest, current: str) -> SkillInfo | None:
        skill_id = request.skill_name
        if not skill_id and request.intent:
            skill_id = INTENT_SKILLS.get(str(request.intent.get("kind", "")), "")
        if skill_id:
            return self.catalog.skill(skill_id)
        if not request.instance_id:
            return None
        ranked = self.catalog.rank_skills(current, limit=1)
        return ranked[0] if ranked else None


def _plain_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump(by_alias=True)
        return dict(result) if isinstance(result, Mapping) else {}
    root = getattr(value, "root", None)
    if root is not None and root is not value:
        return _plain_mapping(root)
    return {}


def clarification_from_elicitation(
    message: str,
    mode: Any,
) -> ClarificationRequest | None:
    """Convert the first ACP form select into ReplayLab's panel contract."""

    payload = _plain_mapping(mode)
    if "root" in payload:
        payload = _plain_mapping(payload["root"])
    schema = payload.get("requestedSchema", payload.get("requested_schema", {}))
    schema_payload = _plain_mapping(schema)
    properties = schema_payload.get("properties", {})
    if not isinstance(properties, Mapping):
        return None
    for name, raw_property in properties.items():
        if str(name).endswith("_custom"):
            continue
        prop = _plain_mapping(raw_property)
        option_values: Any = prop.get("oneOf", prop.get("one_of"))
        selection_mode = "single"
        if not isinstance(option_values, list):
            items = _plain_mapping(prop.get("items"))
            option_values = items.get("anyOf", items.get("any_of"))
            selection_mode = "multiple"
        if not isinstance(option_values, list) or not option_values:
            continue
        options: list[ClarificationOption] = []
        for index, raw_option in enumerate(option_values[:6], 1):
            option = _plain_mapping(raw_option)
            label = str(option.get("title") or option.get("const") or "").strip()
            if not label:
                continue
            options.append(
                ClarificationOption(
                    id=f"option-{index}",
                    label=label,
                    description=str(option.get("description") or "").strip(),
                )
            )
        if not options:
            continue
        question = str(prop.get("description") or message or "Please choose an option").strip()
        return ClarificationRequest(
            question=question,
            options=options,
            selection_mode=selection_mode,
            allow_other=True,
        )
    return None


def clarification_from_numbered_text(text: str) -> ClarificationRequest | None:
    """Recognise the explicit Codex numbered-choice fallback block."""

    match = _QUESTION_BLOCK_RE.search(str(text or ""))
    if match is None:
        return None
    question_lines: list[str] = []
    options: list[ClarificationOption] = []
    for line in match.group(1).splitlines():
        option_match = _QUESTION_OPTION_RE.match(line)
        if option_match is None:
            if line.strip() and not options:
                question_lines.append(line.strip())
            continue
        body = option_match.group(2).strip()
        parts = re.split(r"\s+[—–]\s+", body, maxsplit=1)
        options.append(
            ClarificationOption(
                id=f"option-{option_match.group(1)}",
                label=parts[0].strip()[:240],
                description=(parts[1].strip()[:600] if len(parts) > 1 else ""),
            )
        )
    question = " ".join(question_lines).strip()
    if not question or len(options) < 2:
        return None
    return ClarificationRequest(question=question, options=options[:6], allow_other=True)


__all__ = [
    "NativeAgentEngine",
    "clarification_from_elicitation",
    "clarification_from_numbered_text",
]
