"""Console and tkinter entry points for the external AI companion."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from .engine import CompanionConfig, CompanionEngine
from .mcp_client import connect_stdio_mcp
from .openai_client import OpenAIResponsesClient
from .policy import EFFECTFUL_TOOLS, parse_authorized_message


Receive = Callable[[], Awaitable[str | None]]
Emit = Callable[[str, str], None]


def _default_workspace_root() -> str:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return str((Path(local_app_data) / "ReplayLab" / "workspaces").resolve())
    try:
        base = Path.home() / ".replaylab"
    except RuntimeError:
        # Some packaged/headless hosts intentionally omit every home-directory
        # environment variable. Keep the default usable without accepting a
        # model-controlled path; tempfile resolves from the process owner.
        base = Path(tempfile.gettempdir()) / "ReplayLab"
    return str((base / "workspaces").resolve())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="renderdoc-ai-companion",
        description=(
            "Context-aware RenderDoc chat and UI-intent sidecar. The OpenAI "
            "credential stays in this external process."
        ),
    )
    parser.add_argument("mode", nargs="?", choices=("console", "gui"), default="console")
    parser.add_argument(
        "--model", default=os.environ.get("RENDERDOC_AI_MODEL", "gpt-5.4")
    )
    parser.add_argument(
        "--mcp-command",
        default=os.environ.get("RENDERDOC_MCP_COMMAND", ""),
        help=(
            "Fixed local stdio MCP command (never model controlled). When "
            "omitted, use this virtual environment's Python with -m src.server."
        ),
    )
    parser.add_argument(
        "--mcp-arg",
        action="append",
        default=[],
        help="Fixed argument for the MCP command; repeat for multiple arguments.",
    )
    parser.add_argument("--mcp-cwd", default="")
    parser.add_argument(
        "--consumer-id",
        default="renderdoc-ai-%s" % uuid.uuid4().hex[:12],
    )
    parser.add_argument("--intent-poll-seconds", type=float, default=1.0)
    parser.add_argument("--no-intent-poller", action="store_true")
    parser.add_argument(
        "--workspace-root",
        default=os.environ.get("REPLAYLAB_WORKSPACE_ROOT", _default_workspace_root()),
        help=(
            "Root for generated shader workspaces. Defaults to the per-user "
            "ReplayLab workspaces directory and is created by the process owner."
        ),
    )
    parser.add_argument(
        "--project-root",
        default="",
        help=(
            "Existing process-owner directory containing bounded replay projects "
            "and capture catalogs; requires --project."
        ),
    )
    parser.add_argument(
        "--project",
        default="",
        help=(
            "Safe single project name under --project-root. New capture sessions "
            "are imported for review/source seeding, never automatic replay apply."
        ),
    )
    parser.add_argument(
        "--project-game-id",
        default="",
        help=(
            "Explicit game identity for creating a new replay project. Required "
            "with --project-build-id when the project does not already exist."
        ),
    )
    parser.add_argument(
        "--project-build-id",
        default="",
        help=(
            "Explicit build identity for creating a new replay project. Required "
            "with --project-game-id when the project does not already exist."
        ),
    )
    parser.add_argument(
        "--project-platform",
        default="",
        help="Optional platform boundary for a replay project.",
    )
    parser.add_argument(
        "--project-api-family",
        default="",
        help=(
            "Optional API-family boundary. A new project otherwise pins the API "
            "reported by the first imported capture."
        ),
    )
    parser.add_argument("--decompiler-tool-id", default="")
    parser.add_argument("--compiler-tool-id", default="")
    parser.add_argument("--editor-executable", default="")
    parser.add_argument(
        "--organize-source",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Transactionally organize HLSL after baseline compile/ABI validation "
            "and emit analysis plus a safe Mermaid flowchart (default: enabled)."
        ),
    )
    parser.add_argument(
        "--watch-saved-shaders",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Reverse/Edit starts the fixed "
            "workspace save watcher, which compiles candidates but does not apply "
            "unless compatible-save auto-apply is separately enabled (default: enabled)."
        ),
    )
    parser.add_argument(
        "--auto-apply-compatible-saves",
        action="store_true",
        help=(
            "Explicit process-owner authorisation: after an editor save compiles, "
            "freshly validate ABI/live capture and replace the captured Shader "
            "ResourceId for all of its uses."
        ),
    )
    return parser


def _config(arguments: argparse.Namespace) -> CompanionConfig:
    workspace_root = Path(str(arguments.workspace_root or "")).expanduser()
    if str(workspace_root).strip():
        if workspace_root.exists() and (
            workspace_root.is_symlink() or not workspace_root.is_dir()
        ):
            raise ValueError("workspace_root must be a non-symlink directory")
        workspace_root.mkdir(parents=True, exist_ok=True)
    return CompanionConfig(
        model=arguments.model,
        consumer_id=arguments.consumer_id,
        intent_poll_seconds=arguments.intent_poll_seconds,
        workspace_root=str(workspace_root.resolve()) if str(workspace_root).strip() else "",
        project_root=arguments.project_root,
        project=arguments.project,
        project_game_id=arguments.project_game_id,
        project_build_id=arguments.project_build_id,
        project_platform=arguments.project_platform,
        project_api_family=arguments.project_api_family,
        decompiler_tool_id=arguments.decompiler_tool_id,
        compiler_tool_id=arguments.compiler_tool_id,
        editor_executable=arguments.editor_executable,
        organize_source=arguments.organize_source,
        watch_saved_shaders=(
            arguments.watch_saved_shaders
            or arguments.auto_apply_compatible_saves
        ),
        auto_apply=arguments.auto_apply_compatible_saves,
    )


def _mcp_launch(arguments: argparse.Namespace) -> tuple[str, list[str], str | None]:
    """Resolve one process-owner-controlled MCP launch without relying on PATH."""

    explicit_command = str(arguments.mcp_command or "").strip()
    fixed_arguments = list(arguments.mcp_arg or ())
    if explicit_command:
        command = explicit_command
    else:
        command = sys.executable
        fixed_arguments = ["-m", "src.server", *fixed_arguments]
    return command, fixed_arguments, arguments.mcp_cwd or None


def _help_text() -> str:
    examples = (
        "/allow validate_shader_workspace_candidate,"
        "apply_shader_workspace_shared_resource | "
        "验证并替换原始 Shader RID 的全部共享使用"
    )
    return (
        "Ask about the currently open capture in natural language. Every turn "
        "automatically snapshots RenderDoc first.\n"
        "After Reverse/Edit prepares a workspace and the editor save compiles a "
        "candidate, type /apply-last to explicitly validate and replace every "
        "use of the captured Shader ResourceId; /rollback-last restores its "
        "captured original. You do "
        "not call MCP functions yourself.\n"
        "Effectful tools are denied by default. Authorise exact tools for one "
        "advanced model-driven turn with:\n  %s\n"
        "Available effectful tool names:\n  %s\n"
        "Commands: /help, /apply-last, /rollback-last, /quit"
        % (examples, ", ".join(sorted(EFFECTFUL_TOOLS)))
    )


async def run_session(
    arguments: argparse.Namespace,
    receive: Receive,
    emit: Emit,
) -> None:
    responses = OpenAIResponsesClient()

    config = _config(arguments)
    if config.auto_apply:
        emit(
            "system",
            "autoApplyCompatibleSaves is ENABLED. A save still must compile, pass "
            "fresh complete ABI validation, and pass live capture/original-byte gates.",
        )

    mcp_command, mcp_arguments, mcp_cwd = _mcp_launch(arguments)
    async with connect_stdio_mcp(mcp_command, mcp_arguments, mcp_cwd) as mcp:
        engine = CompanionEngine(
            mcp,
            responses,
            config,
            notify=lambda message: emit("system", message),
        )
        await engine.initialise()
        stop = asyncio.Event()
        intent_task = None
        if not arguments.no_intent_poller:
            intent_task = asyncio.create_task(
                engine.poll_intents(stop), name="renderdoc-ui-intent-poller"
            )
        emit(
            "system",
            "Connected. RenderDoc context will be read before every chat turn. "
            "Type /help for mutation-authorisation syntax.",
        )
        try:
            while True:
                line = await receive()
                if line is None:
                    break
                command = line.strip()
                if not command:
                    continue
                if command in {"/quit", "/exit"}:
                    break
                if command == "/help":
                    emit("system", _help_text())
                    continue
                if command == "/apply-last":
                    try:
                        emit("assistant", await engine.apply_last_workspace())
                    except Exception as error:
                        emit("system", "Apply failed closed: %s" % error)
                    continue
                if command == "/rollback-last":
                    try:
                        emit("assistant", await engine.rollback_last_workspace())
                    except Exception as error:
                        emit("system", "Rollback failed closed: %s" % error)
                    continue
                try:
                    message, authorised = parse_authorized_message(command)
                    answer = await engine.chat(message, authorised)
                    emit("assistant", answer)
                except Exception as error:
                    # Keep the sidecar alive after a failed model/MCP turn. The
                    # error is visible and no implicit retry broadens authority.
                    emit("system", "Turn failed: %s" % error)
        finally:
            stop.set()
            if intent_task is not None:
                intent_task.cancel()
                await asyncio.gather(intent_task, return_exceptions=True)
            await engine.close()


async def _console(arguments: argparse.Namespace) -> None:
    async def receive() -> str | None:
        try:
            return await asyncio.to_thread(input, "you> ")
        except EOFError:
            return None

    def emit(role: str, message: str) -> None:
        label = {"assistant": "ai", "system": "system"}.get(role, role)
        print("\n%s> %s" % (label, message), flush=True)

    await run_session(arguments, receive, emit)


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.mode == "gui":
            from .gui import run_gui

            return run_gui(
                lambda receive, emit: run_session(arguments, receive, emit)
            )
        asyncio.run(_console(arguments))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print("renderdoc-ai-companion: %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
