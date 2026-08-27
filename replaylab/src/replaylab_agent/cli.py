"""Command-line entry point used by the future qrenderdoc connection panel."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from .bridge import BridgeClient
from .catalog import PRESETS, get_preset
from .context_management import CONVERSATION_SCHEMA
from .doctor import run_doctor
from .errors import ReplayLabAIError
from .mcp_tools import find_replaylab_mcp
from .profiles import Profile, ProfileStore
from .probe import discover_models, probe_profile
from .process_env import remove_codex_profile_home
from .runtime import AgentRuntime, default_skill_root
from .skills import SkillCatalog


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="replaylab-ai",
        description="ReplayLab provider, Agent, Skill and qrenderdoc intent host",
    )
    parser.add_argument("--config", type=Path, help="override profiles.json")
    sub = parser.add_subparsers(dest="command", required=True)

    presets = sub.add_parser("presets", help="list built-in model and Agent connectors")
    presets.add_argument("--json", action="store_true")

    mcp_config = sub.add_parser(
        "mcp-config", help="print legacy/manual Codex MCP configuration for diagnostics"
    )
    mcp_config.add_argument("--mcp-host", type=Path)

    profile = sub.add_parser("profile", help="manage connection profiles")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)
    profile_list = profile_sub.add_parser("list")
    profile_list.add_argument("--json", action="store_true")
    add = profile_sub.add_parser("add")
    add.add_argument("id")
    add.add_argument("--preset", required=True)
    add.add_argument("--model", default="")
    add.add_argument(
        "--allowed-model",
        action="append",
        default=[],
        help="model exposed in the qrenderdoc picker (repeatable)",
    )
    add.add_argument("--default-model", default="")
    add.add_argument("--title", default="")
    add.add_argument("--base-url", default="")
    add.add_argument("--api-key-env", default="")
    add.add_argument(
        "--agent-env",
        action="append",
        default=[],
        help="environment variable name to pass to an external Agent (repeatable)",
    )
    add.add_argument("--option", action="append", default=[], metavar="KEY=VALUE")
    add.add_argument("--capability", action="append", default=[])
    add.add_argument("--select", action="store_true")
    add.add_argument("--json", action="store_true")
    agent_command = add.add_mutually_exclusive_group()
    agent_command.add_argument(
        "--command",
        dest="agent_command",
        nargs="+",
        default=[],
        help="legacy external Agent argv; values beginning with '-' require --command-json",
    )
    agent_command.add_argument(
        "--command-json",
        default="",
        metavar="JSON",
        help="external Agent argv as a JSON string array (safe for flags and spaces)",
    )
    select = profile_sub.add_parser("select")
    select.add_argument("id")
    select.add_argument("--json", action="store_true")
    remove = profile_sub.add_parser("remove")
    remove.add_argument("id")
    remove.add_argument("--json", action="store_true")

    update = profile_sub.add_parser("update")
    update.add_argument("id")
    update.add_argument("--preset")
    update.add_argument("--model")
    update.add_argument("--allowed-model", action="append", default=None)
    update.add_argument("--default-model")
    update.add_argument("--title")
    update.add_argument("--base-url")
    update.add_argument("--api-key-env")
    update.add_argument("--agent-env", action="append", default=None)
    update.add_argument("--option", action="append", default=None, metavar="KEY=VALUE")
    update.add_argument("--capability", action="append", default=None)
    update.add_argument("--select", action="store_true")
    update.add_argument(
        "--replace-provider",
        action="store_true",
        help=(
            "atomically clear provider-coupled fields before applying --preset and "
            "the supplied settings"
        ),
    )
    update.add_argument("--json", action="store_true")
    update_command = update.add_mutually_exclusive_group()
    update_command.add_argument("--command", dest="agent_command", nargs="+")
    update_command.add_argument("--command-json", default=None, metavar="JSON")

    profile_models = profile_sub.add_parser(
        "models", help="replace enabled models and/or the default model"
    )
    profile_models.add_argument("id")
    profile_models.add_argument("--allowed-model", action="append", default=None)
    profile_models.add_argument("--default-model")
    profile_models.add_argument("--json", action="store_true")

    models = sub.add_parser(
        "models", help="discover selectable models without making a generation request"
    )
    source = models.add_mutually_exclusive_group()
    source.add_argument("--profile", default="")
    source.add_argument("--preset", default="")
    models.add_argument("--base-url", default="")
    models.add_argument("--api-key-env", default="")
    models.add_argument(
        "--api-key-stdin",
        action="store_true",
        help="read a session-only API key from stdin; never stores or echoes it",
    )
    models.add_argument("--timeout", type=float, default=15.0)
    models.add_argument("--include-hidden", action="store_true")
    models.add_argument("--json", action="store_true")

    auth = sub.add_parser("auth", help="inspect or manage vendor-owned authentication")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_status = auth_sub.add_parser("status")
    auth_status.add_argument("--profile", default="")
    auth_status.add_argument("--refresh", action="store_true")
    auth_status.add_argument("--json", action="store_true")
    auth_login = auth_sub.add_parser("login")
    auth_login.add_argument("--profile", default="")
    auth_login.add_argument("--flow", choices=("browser", "device"), default="browser")
    auth_login.add_argument("--timeout", type=float, default=600.0)
    auth_login.add_argument(
        "--start-gated",
        action="store_true",
        help="wait for a 'start' stdin line before creating the Codex app-server",
    )
    auth_login.add_argument("--json", action="store_true")
    auth_cancel = auth_sub.add_parser("cancel")
    auth_cancel.add_argument("--profile", default="")
    auth_cancel.add_argument("--login-id", required=True)
    auth_cancel.add_argument("--json", action="store_true")
    auth_logout = auth_sub.add_parser("logout")
    auth_logout.add_argument("--profile", default="")
    auth_logout.add_argument("--json", action="store_true")

    credential = sub.add_parser(
        "credential", help="manage OS-backed model API credentials"
    )
    credential_sub = credential.add_subparsers(
        dest="credential_command", required=True
    )
    credential_status = credential_sub.add_parser("status")
    credential_status.add_argument("--profile", default="")
    credential_status.add_argument("--json", action="store_true")
    credential_store = credential_sub.add_parser("store")
    credential_store.add_argument("--profile", default="")
    credential_store.add_argument("--api-key-stdin", action="store_true", required=True)
    credential_store.add_argument("--json", action="store_true")
    credential_delete = credential_sub.add_parser("delete")
    credential_delete.add_argument("--profile", default="")
    credential_delete.add_argument("--json", action="store_true")

    subscription = sub.add_parser(
        "subscription", help="detect, connect and test managed Agent subscriptions"
    )
    subscription_sub = subscription.add_subparsers(
        dest="subscription_command", required=True
    )
    subscription_status = subscription_sub.add_parser("status")
    subscription_status.add_argument("--profile", required=True)
    subscription_status.add_argument("--json", action="store_true")
    subscription_connect = subscription_sub.add_parser("connect")
    subscription_connect.add_argument("--profile", required=True)
    subscription_connect.add_argument(
        "--flow", choices=("browser", "device"), default="browser"
    )
    subscription_connect.add_argument("--timeout", type=float, default=600.0)
    subscription_connect.add_argument("--start-gated", action="store_true", required=True)
    subscription_connect.add_argument("--json", action="store_true")
    subscription_test = subscription_sub.add_parser("test")
    subscription_test.add_argument("--profile", required=True)
    subscription_test.add_argument("--timeout", type=float, default=60.0)
    subscription_test.add_argument("--start-gated", action="store_true", required=True)
    subscription_test.add_argument("--json", action="store_true")

    doctor = sub.add_parser("doctor", help="run offline connection diagnostics")
    doctor.add_argument("--profile", default="")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--mcp-host", type=Path)

    probe = sub.add_parser(
        "probe",
        help="explicitly test managed command or no-generation model discovery",
    )
    probe.add_argument("--profile", default="")
    probe.add_argument("--timeout", type=float, default=15.0)
    probe.add_argument("--json", action="store_true")

    self_test = sub.add_parser(
        "self-test",
        help="run deterministic offline API and Agent protocol compatibility checks",
    )
    self_test.add_argument("--timeout", type=float, default=10.0)
    self_test.add_argument("--json", action="store_true")

    run = sub.add_parser("run", help="run one chat or exact Skill request")
    run.add_argument("--profile", default="")
    run.add_argument("--model", default="", help="per-run enabled-model override")
    run.add_argument("--skill", default="")
    prompt_source = run.add_mutually_exclusive_group(required=True)
    prompt_source.add_argument("--prompt")
    prompt_source.add_argument(
        "--prompt-stdin",
        action="store_true",
        help="read the prompt from stdin so it is not exposed in the process command line",
    )
    run.add_argument("--context-file", type=Path)
    run.add_argument("--live-context", action="store_true")
    run.add_argument(
        "--instance-id",
        default="",
        help="bind context-file/live-context to an exact qrenderdoc bridge instance",
    )
    run.add_argument("--task-id", default="")
    run.add_argument("--mcp-host", type=Path)
    run.add_argument("--json", action="store_true")

    work = sub.add_parser("work-once", help="claim and process one qrenderdoc AI-button intent")
    work.add_argument("--profile", default="")
    work.add_argument("--model", default="", help="per-run enabled-model override")
    work.add_argument(
        "--instance-id",
        default="",
        help="claim intents only from this exact qrenderdoc bridge instance",
    )
    work.add_argument("--mcp-host", type=Path)
    work.add_argument("--json", action="store_true")

    worker = sub.add_parser("worker", help="continuously process qrenderdoc AI-button intents")
    worker.add_argument("--profile", default="")
    worker.add_argument("--model", default="", help="worker-wide enabled-model override")
    worker.add_argument(
        "--instance-id",
        default="",
        help="claim intents only from this exact qrenderdoc bridge instance",
    )
    worker.add_argument("--mcp-host", type=Path)
    worker.add_argument("--poll-seconds", type=float, default=0.75)
    worker.add_argument(
        "--idle-exit-seconds",
        type=float,
        default=0,
        help="0 keeps the worker alive until cancelled",
    )
    worker.add_argument(
        "--control-stdin",
        action="store_true",
        help="gracefully stop on a 'shutdown' line or EOF from the owning qrenderdoc process",
    )
    worker.add_argument(
        "--start-gated",
        action="store_true",
        help=(
            "wait for a 'start' control line before claiming intents; requires "
            "--control-stdin"
        ),
    )
    return parser


def _load_store(path: Path | None) -> ProfileStore:
    return ProfileStore(path).load()


def _catalog() -> SkillCatalog:
    return SkillCatalog([default_skill_root()]).load()


def _parse_options(values: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"profile option must be KEY=VALUE: {item}")
        key, raw = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("profile option key is empty")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        if isinstance(value, (dict, list)):
            raise ValueError(f"profile option must be scalar: {key}")
        result[key] = value
    return result


def _machine_json(value: Any) -> bytes:
    """Encode one machine response deterministically as compact UTF-8 JSON."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8", errors="backslashreplace")


def _print_machine_json(value: Any) -> None:
    encoded = _machine_json(value) + b"\n"
    binary = getattr(sys.stdout, "buffer", None)
    if binary is not None:
        binary.write(encoded)
        binary.flush()
        return
    # Preserve test/embedding compatibility for text-only stdout replacements while the normal
    # CLI path always writes the exact UTF-8 bytes above.
    sys.stdout.write(encoded.decode("utf-8"))
    sys.stdout.flush()


_MAX_STDERR_BYTES = 64 * 1024


def _print_utf8_error(value: str) -> None:
    """Write one bounded diagnostic as UTF-8, independent of Windows ACP.

    Machine stdout is already emitted through its binary stream. Do the same
    for terminal failures so qrenderdoc can always decode localized exception
    messages and Chinese paths with ``QString::fromUtf8``. ``backslashreplace``
    also guarantees that a malformed provider exception cannot prevent the
    original request from reaching a terminal failure state.
    """

    encoded = str(value).encode("utf-8", errors="backslashreplace")
    if len(encoded) + 1 > _MAX_STDERR_BYTES:
        suffix = b"...\n"
        encoded = (
            encoded[: _MAX_STDERR_BYTES - len(suffix)]
            .decode("utf-8", errors="ignore")
            .encode("utf-8")
            + suffix
        )
    else:
        encoded += b"\n"
    binary = getattr(sys.stderr, "buffer", None)
    if binary is not None:
        binary.write(encoded)
        binary.flush()
        return
    # Text-only replacements are used by tests and a few embedders. The
    # normal executable path always takes the byte-exact branch above.
    sys.stderr.write(encoded.decode("utf-8"))
    sys.stderr.flush()


def _configure_utf8_stdio() -> None:
    """Fix human and argparse output before any locale-dependent write."""

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="strict")
            except (AttributeError, OSError, ValueError):
                pass


def _parse_agent_command(args: argparse.Namespace) -> list[str]:
    encoded = str(getattr(args, "command_json", "") or "")
    if not encoded:
        return list(getattr(args, "agent_command", None) or [])
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise ValueError("--command-json exceeds the 64 KiB safety limit")
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError("--command-json must be valid JSON") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("--command-json must contain an array of strings")
    if len(value) > 256:
        raise ValueError("--command-json may contain at most 256 arguments")
    return value


def _print_presets(as_json: bool) -> None:
    rows = [
        {
            "id": item.id,
            "title": item.title,
            "kind": item.kind,
            "configurationMode": item.configuration_mode,
            "protocol": item.protocol,
            "auth": item.auth,
            "authenticationUi": item.authentication_ui,
            "supportsModelDiscovery": item.supports_model_discovery,
            "supportsManagedLogin": item.subscription_login_mode != "none",
            "subscriptionLoginMode": item.subscription_login_mode,
            "interactiveTerminalRequired": item.subscription_login_mode == "terminal",
            "installHint": item.install_hint,
            "defaultModel": "",
            "baseUrl": item.base_url,
            "apiKeyEnv": item.api_key_env,
            "command": list(item.command),
            "note": item.note,
        }
        for item in PRESETS
    ]
    if as_json:
        _print_machine_json(rows)
        return
    print(f"{'ID':<18} {'TYPE':<7} {'PROTOCOL':<21} TITLE")
    for row in rows:
        print(f"{row['id']:<18} {row['kind']:<7} {row['protocol']:<21} {row['title']}")


def _print_mcp_config(explicit: Path | None) -> None:
    host = str(find_replaylab_mcp(explicit)).replace("\\", "/")
    python = str(Path(sys.executable).resolve()).replace("\\", "/")
    args = [
        "-I",
        "-m",
        "replaylab_agent.mcp_proxy",
        "--host",
        host,
        "--allow-env",
        "REPLAYLAB_ALLOWED_TOOLS",
        "--host-args-env",
        "REPLAYLAB_MCP_HOST_ARGS",
    ]
    print(
        "# Legacy/manual registration only. Managed Codex profiles inject this "
        "filtered server per process in an isolated CODEX_HOME."
    )
    print("[mcp_servers.replaylab]")
    print("command = " + json.dumps(python, ensure_ascii=False))
    print("args = " + json.dumps(args, ensure_ascii=False))
    print(
        'env_vars = ["REPLAYLAB_ALLOWED_TOOLS", "REPLAYLAB_MCP_HOST_ARGS", '
        '"REPLAYLAB_INSTANCE_ID", "RENDERDOC_ULTRA_SHADER_LIVE_EDIT_REGISTRY", '
        '"REPLAYLAB_TOOL_SCOPE", "REPLAYLAB_TOOL_AUDIT"]'
    )
    print("startup_timeout_sec = 30")
    print("tool_timeout_sec = 3600")
    print("required = true")


def _profile_command(
    args: argparse.Namespace,
    store: ProfileStore,
    credential_store: Any | None = None,
) -> int:
    if args.profile_command == "list":
        rows = [
            _profile_wire(profile, active=profile.id == store.active_profile)
            for profile in sorted(store.profiles.values(), key=lambda item: item.id)
        ]
        if args.json:
            _print_machine_json(rows)
        elif not rows:
            print("No AI profiles configured.")
        else:
            print(f"{'ACTIVE':<7} {'ID':<22} {'TYPE':<7} {'PRESET':<18} MODEL")
            for row in rows:
                print(
                    f"{('*' if row['active'] else ''):<7} {row['id']:<22} "
                    f"{row['kind']:<7} {row['preset']:<18} {row['model']}"
                )
        return 0
    if args.profile_command == "add":
        get_preset(args.preset)
        allowed_models = list(dict.fromkeys(args.allowed_model))
        legacy_model = str(args.model or "").strip()
        default_model = str(args.default_model or "").strip()
        if legacy_model and legacy_model not in allowed_models:
            allowed_models.append(legacy_model)
        if default_model and default_model not in allowed_models:
            allowed_models.append(default_model)
        selected_model = legacy_model or default_model or (
            allowed_models[0] if allowed_models else ""
        )
        profile = Profile(
            id=args.id,
            preset=args.preset,
            model=selected_model,
            models=allowed_models,
            default_model=default_model or selected_model,
            title=args.title,
            base_url=args.base_url,
            api_key_env=args.api_key_env,
            command=_parse_agent_command(args),
            agent_env=list(dict.fromkeys(args.agent_env)),
            options=_parse_options(args.option),
            probed_capabilities=list(dict.fromkeys(args.capability)) or None,
        )
        if profile.id in store.profiles:
            raise ValueError(f"profile already exists: {profile.id}")
        store.add(profile, select=args.select)
        store.save()
        if args.json:
            _print_machine_json(_profile_wire(profile, active=profile.id == store.active_profile))
            return 0
        print(f"Added profile '{profile.id}' ({profile.provider.title}).")
        if profile.provider.kind == "model" and profile.key_env:
            print(
                f"Credential source: environment variable {profile.key_env}. "
                f"Next: replaylab-ai --config {store.path} doctor --profile {profile.id}"
            )
        elif profile.provider.protocol == "codex-app-server":
            print(
                "Authentication is managed by Codex in this ReplayLab profile. "
                "Use Settings > AI > Edit > Connect / Sign in, then run doctor."
            )
        else:
            print("Authentication is managed by the external Agent command; run doctor next.")
        return 0
    if args.profile_command == "select":
        selected = store.select(args.id)
        store.save()
        if args.json:
            _print_machine_json(_profile_wire(selected, active=True))
        else:
            print(f"Active AI profile: {selected.id}")
        return 0
    if args.profile_command == "remove":
        if args.id not in store.profiles:
            raise ValueError(f"unknown profile: {args.id}")
        removed_profile = store.profiles[args.id]
        if removed_profile.preset == "codex":
            # The dedicated home can contain the vendor-managed subscription
            # credential.  Clear it before the profile is removed so a failure
            # cannot leave the UI claiming that authentication was forgotten.
            remove_codex_profile_home(removed_profile)
        if (
            credential_store is not None
            and credential_store.supported
            and removed_profile.provider.kind == "model"
            and removed_profile.provider.auth == "api-key"
        ):
            credential_store.delete(args.id)
        del store.profiles[args.id]
        if store.active_profile == args.id:
            store.active_profile = sorted(store.profiles)[0] if store.profiles else ""
        store.save()
        if args.json:
            _print_machine_json({"removed": args.id, "activeProfile": store.active_profile})
        else:
            print(f"Removed profile '{args.id}'.")
        return 0
    if args.profile_command in {"update", "models"}:
        try:
            current = store.profiles[args.id]
        except KeyError as exc:
            raise ValueError(f"unknown profile: {args.id}") from exc
        raw = asdict(current)
        if args.profile_command == "update":
            requested_preset = args.preset or current.preset
            replacing_provider = bool(getattr(args, "replace_provider", False))
            if args.preset and args.preset != current.preset and not replacing_provider:
                raise ValueError(
                    "changing preset requires --replace-provider so old provider settings "
                    "cannot leak into the new connection"
                )
            if replacing_provider:
                if not args.preset:
                    raise ValueError("--replace-provider requires an explicit --preset")
                get_preset(requested_preset)
                raw = {
                    "id": current.id,
                    "preset": requested_preset,
                    "model": "",
                    "models": [],
                    "default_model": "",
                    "title": current.title,
                    "base_url": "",
                    "api_key_env": "",
                    "command": [],
                    "agent_env": [],
                    "options": {},
                    "probed_capabilities": None,
                }
            scalar_map = {
                "preset": "preset",
                "model": "model",
                "default_model": "default_model",
                "title": "title",
                "base_url": "base_url",
                "api_key_env": "api_key_env",
            }
            for argument, field_name in scalar_map.items():
                value = getattr(args, argument, None)
                if value is not None:
                    raw[field_name] = value
                    if argument == "default_model":
                        # Keep the v1 field as a persisted compatibility alias.
                        # Runtime per-run copies may intentionally set only
                        # ``model`` and must not be rewritten by validation.
                        raw["model"] = value
            if args.model is not None and args.default_model is None:
                raw["default_model"] = args.model
            if args.allowed_model is not None:
                raw["models"] = list(dict.fromkeys(args.allowed_model))
            if args.agent_env is not None:
                raw["agent_env"] = list(dict.fromkeys(args.agent_env))
            if args.option is not None:
                raw["options"] = {**raw["options"], **_parse_options(args.option)}
            if args.capability is not None:
                raw["probed_capabilities"] = list(dict.fromkeys(args.capability)) or None
            if args.command_json is not None or args.agent_command is not None:
                raw["command"] = _parse_agent_command(args)
        else:
            if args.allowed_model is None and args.default_model is None:
                raise ValueError("profile models requires --allowed-model or --default-model")
            if args.allowed_model is not None:
                raw["models"] = list(dict.fromkeys(args.allowed_model))
            if args.default_model is not None:
                raw["default_model"] = args.default_model
                raw["model"] = args.default_model
        updated = Profile.from_dict(raw)
        if updated.default_model and updated.default_model not in updated.models:
            raise ValueError("default model must also be supplied as --allowed-model")
        if (
            credential_store is not None
            and credential_store.supported
            and args.profile_command == "update"
            and bool(getattr(args, "replace_provider", False))
            and updated.preset != current.preset
            and current.provider.kind == "model"
            and current.provider.auth == "api-key"
        ):
            credential_store.delete(current.id)
        if (
            args.profile_command == "update"
            and bool(getattr(args, "replace_provider", False))
            and current.preset == "codex"
            and updated.preset != "codex"
        ):
            remove_codex_profile_home(current)
        store.profiles[updated.id] = updated
        if getattr(args, "select", False):
            store.active_profile = updated.id
        store.save()
        if args.json:
            _print_machine_json(_profile_wire(updated, active=updated.id == store.active_profile))
        else:
            print(f"Updated profile '{updated.id}'.")
        return 0
    raise AssertionError(args.profile_command)


def _profile_wire(profile: Profile, *, active: bool) -> dict[str, Any]:
    return {
        "id": profile.id,
        "title": profile.title or profile.id,
        "active": active,
        "preset": profile.preset,
        "kind": profile.provider.kind,
        "executionMode": (
            "model-api" if profile.provider.kind == "model" else "agent-protocol"
        ),
        "loopOwner": "replaylab" if profile.provider.kind == "model" else "vendor",
        "protocol": profile.provider.protocol,
        "auth": profile.provider.auth,
        "model": profile.model,
        "models": list(profile.models),
        "defaultModel": profile.default_model,
        "baseUrl": profile.base_url,
        "endpoint": profile.endpoint,
        "apiKeyEnv": profile.key_env,
        "command": profile.argv,
        "agentEnv": list(profile.agent_env),
        "options": dict(profile.options),
        "capabilities": sorted(profile.capabilities),
        "runInputSchemas": [CONVERSATION_SCHEMA],
    }


def _selected_profile(store: ProfileStore, profile_id: str) -> Profile:
    if profile_id:
        try:
            return store.profiles[profile_id]
        except KeyError as exc:
            raise ValueError(f"unknown profile: {profile_id}") from exc
    return store.active()


def _read_context(args: argparse.Namespace) -> tuple[dict[str, Any], str, str]:
    if args.context_file and args.live_context:
        raise ValueError("choose either --context-file or --live-context")
    if args.context_file:
        if not args.instance_id:
            raise ValueError("--context-file requires --instance-id for capture-tool binding")
        value = json.loads(args.context_file.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("context file must contain one JSON object")
        client = BridgeClient()
        instance = client.select_instance(args.instance_id)
        if not instance.capture_loaded:
            raise ValueError("the selected qrenderdoc instance has no loaded capture")
        capture = value.get("capture", {})
        capture_path = str(capture.get("path", "")) if isinstance(capture, dict) else ""
        return value, instance.id, capture_path or instance.capture_path
    if args.live_context:
        client = BridgeClient()
        if args.instance_id:
            instance = client.select_instance(args.instance_id)
            if not instance.capture_loaded:
                raise ValueError("the selected qrenderdoc instance has no loaded capture")
        else:
            loaded = [instance for instance in client.instances() if instance.capture_loaded]
            if len(loaded) != 1:
                raise ValueError(
                    "--live-context requires exactly one loaded qrenderdoc instance or an "
                    "explicit --instance-id"
                )
            instance = loaded[0]
        value = client.call(
            "get_current_renderdoc_context", instance_id=instance.id
        )
        if not isinstance(value, dict):
            raise ValueError("qrenderdoc returned an invalid context object")
        return value, instance.id, instance.capture_path
    if args.instance_id:
        raise ValueError("--instance-id requires --context-file or --live-context")
    return {}, "", ""


def _result_payload(result: Any) -> dict[str, Any]:
    workflow = getattr(result, "workflow", None)
    return {
        "taskId": result.task_id,
        "profile": result.profile,
        "executor": result.executor,
        "text": result.text,
        "plan": result.plan.model_dump(by_alias=True) if result.plan else None,
        "terminalStatus": (
            workflow.terminal_status if workflow else "completed"
        ),
        "contextCompacted": bool(getattr(result, "context_compacted", False)),
        "workflowReceipt": _workflow_receipt(workflow),
    }


def _workflow_receipt(workflow: Any) -> dict[str, Any] | None:
    """Export lifecycle/evidence receipts without duplicating specialist answers."""

    if workflow is None:
        return None

    truncated_fields = 0

    def bounded(value: Any, limit: int) -> str:
        nonlocal truncated_fields
        text = str(value or "")
        if len(text) <= limit:
            return text
        truncated_fields += 1
        return text[: limit - 1] + "…"

    stages: list[dict[str, Any]] = []
    for stage in tuple(workflow.stages)[:6]:
        results = {result.task_id: result for result in stage.results}
        sessions: list[dict[str, Any]] = []
        for session in tuple(stage.sessions)[:3]:
            matched = results.get(session.task_id)
            receipt: dict[str, Any] = {
                "taskId": session.task_id,
                "agentId": session.agent_id,
                "skill": bounded(session.skill, 64),
                "status": session.status,
                "attempts": session.attempts,
                "validationFeedback": [
                    bounded(item, 500) for item in tuple(session.validation_feedback)[:3]
                ],
                "failureReason": bounded(session.failure_reason, 1000),
                "contextGeneration": session.context_generation,
                "released": session.released,
                "result": None,
            }
            if matched is not None:
                receipt["result"] = {
                    "summary": bounded(matched.summary, 500),
                    "evidenceValidated": matched.evidence_validated,
                    "completedTools": [
                        bounded(tool, 128) for tool in tuple(matched.completed_tools)[:40]
                    ],
                }
            sessions.append(receipt)
        stages.append(
            {
                "stageId": stage.stage_id,
                "objective": bounded(stage.objective, 800),
                "sessions": sessions,
            }
        )
    receipt = {
        "schema": "replaylab-workflow-receipt/v1",
        "strategy": bounded(workflow.strategy, 128),
        "terminalStatus": workflow.terminal_status,
        "supervisorDecisions": workflow.supervisor_decisions,
        "totalTasks": workflow.total_tasks,
        "goalObjective": bounded(workflow.goal.objective, 1600),
        "stages": stages,
        "truncatedFieldCount": truncated_fields,
    }
    # Model fields and collection counts above keep this far below the native
    # 8 MiB stdout ceiling. Retain a hard independent bound as a protocol guard.
    if len(json.dumps(receipt, ensure_ascii=False).encode("utf-8")) > 256 * 1024:
        raise ValueError("workflow receipt exceeded its 256 KiB protocol bound")
    return receipt


async def _run_once(args: argparse.Namespace, store: ProfileStore) -> int:
    runtime = AgentRuntime(store, _catalog(), mcp_executable=args.mcp_host)
    context, instance_id, capture_path = _read_context(args)
    prompt = args.prompt
    if args.prompt_stdin:
        raw_prompt = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw_prompt) > 2 * 1024 * 1024:
            raise ValueError("stdin prompt exceeds the 2 MiB safety limit")
        try:
            prompt = raw_prompt.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("stdin prompt must be valid UTF-8") from exc
    if not prompt or not prompt.strip():
        raise ValueError("prompt cannot be empty")
    run_kwargs: dict[str, Any] = {
        "profile_id": args.profile,
        "skill_name": args.skill,
        "context": context,
        "task_id": args.task_id,
        "capture_path": capture_path,
        "instance_id": instance_id,
    }
    if getattr(args, "model", ""):
        run_kwargs["model_override"] = args.model
    result = await runtime.run(prompt, **run_kwargs)
    if args.json:
        _print_machine_json(_result_payload(result))
    else:
        print(result.text)
    return 0


async def _probe(args: argparse.Namespace, store: ProfileStore) -> int:
    report = await probe_profile(
        _selected_profile(store, args.profile), timeout=args.timeout
    )
    if args.json:
        _print_machine_json(report.to_wire())
    else:
        print(f"[{report.status}] {report.provider}: {report.detail}")
        if report.models:
            print("Models: " + ", ".join(report.models))
        for warning in report.warnings:
            print("Warning: " + warning)
    return 0


def _read_session_api_key() -> str:
    from .credential_store import normalize_api_credential

    stream = getattr(sys.stdin, "buffer", sys.stdin)
    raw = stream.read(64 * 1024 + 1)
    if isinstance(raw, str):
        encoded = raw.encode("utf-8")
    else:
        encoded = bytes(raw)
    if len(encoded) > 64 * 1024:
        raise ValueError("stdin API key exceeds the 64 KiB safety limit")
    try:
        value = encoded.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("stdin API key must be valid UTF-8") from exc
    if not value or "\0" in value or "\n" in value or "\r" in value:
        raise ValueError("stdin API key must contain one non-empty line")
    return normalize_api_credential(value)


async def _self_test_command(args: argparse.Namespace) -> int:
    """Run the protocol harness without loading profiles or credential stores."""

    from .selftest import run_protocol_self_test

    items = await run_protocol_self_test(timeout=float(args.timeout))
    payload = [item.to_wire() for item in items]
    if args.json:
        _print_machine_json(payload)
    else:
        for item in payload:
            print(f"[{item['status'].upper():7}] {item['name']}: {item['detail']}")
    return 1 if any(item["status"] == "error" for item in payload) else 0


def _credential_command(
    args: argparse.Namespace,
    profiles: ProfileStore,
    credential_store: Any | None = None,
) -> int:
    """Manage one profile's secret without ever serialising it to output."""

    from .credential_store import default_credential_store

    profile = _selected_profile(profiles, args.profile)
    backend = credential_store or default_credential_store()
    if args.credential_command == "status":
        stored = bool(backend.read(profile.id)) if backend.supported else False
        payload = {
            "profile": profile.id,
            "backend": backend.backend,
            "supported": bool(backend.supported),
            "stored": stored,
        }
    elif args.credential_command == "store":
        if profile.provider.kind != "model" or profile.provider.auth != "api-key":
            raise ValueError("only model API-key profiles can store an API credential")
        if not backend.supported:
            raise ValueError("no supported operating-system credential store is available")
        backend.write(profile.id, _read_session_api_key())
        payload = {
            "profile": profile.id,
            "backend": backend.backend,
            "supported": True,
            "stored": True,
        }
    elif args.credential_command == "delete":
        deleted = backend.delete(profile.id) if backend.supported else False
        payload = {
            "profile": profile.id,
            "backend": backend.backend,
            "supported": bool(backend.supported),
            "stored": False,
            "deleted": deleted,
        }
    else:
        raise AssertionError(args.credential_command)
    if args.json:
        _print_machine_json(payload)
    else:
        if args.credential_command == "status":
            print("saved" if payload["stored"] else "not saved")
        elif args.credential_command == "store":
            print(f"Saved API credential for profile '{profile.id}'.")
        else:
            print(f"Removed saved API credential for profile '{profile.id}'.")
    return 0


def _discovery_profile(args: argparse.Namespace, store: ProfileStore) -> Profile:
    if args.profile:
        return _selected_profile(store, args.profile)
    if args.preset:
        preset = get_preset(args.preset)
        return Profile(
            id="discovery",
            preset=preset.id,
            model="__model_discovery__" if preset.kind == "model" else "",
            models=["__model_discovery__"] if preset.kind == "model" else [],
            default_model="__model_discovery__" if preset.kind == "model" else "",
            base_url=args.base_url,
            api_key_env=args.api_key_env,
        )
    return store.active()


def _codex_connector(profile: Profile, *, timeout: float):
    if profile.provider.protocol != "codex-app-server":
        raise ValueError("this operation is available only for a Codex app-server profile")
    from .connectors.codex_app_server import CodexAppServerConnector
    from .process_env import external_agent_environment, resolved_agent_argv

    return CodexAppServerConnector(
        resolved_agent_argv(profile),
        cwd=Path.cwd(),
        timeout=timeout,
        env=external_agent_environment(profile),
    )


async def _models_command(args: argparse.Namespace, store: ProfileStore) -> int:
    profile = _discovery_profile(args, store)
    if profile.provider.protocol == "acp":
        raise ValueError(
            "ACP does not standardize pre-session model discovery; use the vendor CLI "
            "or save known model ids in the profile"
        )
    if profile.provider.protocol == "codex-app-server":
        if args.api_key_stdin:
            raise ValueError("Codex app-server owns authentication; do not pass an API key")
        async with _codex_connector(profile, timeout=args.timeout) as connector:
            details = await connector.list_models(include_hidden=args.include_hidden)
        model_ids: list[str] = []
        for item in details:
            value = item.get("id", item.get("model", ""))
            if isinstance(value, str) and value and value not in model_ids:
                model_ids.append(value)
        payload = {
            "profile": profile.id if args.profile else "",
            "preset": profile.preset,
            "kind": profile.provider.kind,
            "protocol": profile.provider.protocol,
            "endpoint": "",
            "models": model_ids,
            "modelDetails": details,
            "attempts": [],
        }
    else:
        key = _read_session_api_key() if args.api_key_stdin else None
        endpoint, models, attempts = await discover_models(
            profile,
            timeout=args.timeout,
            api_key=key,
        )
        payload = {
            "profile": profile.id if args.profile else "",
            "preset": profile.preset,
            "kind": profile.provider.kind,
            "protocol": profile.provider.protocol,
            "endpoint": endpoint,
            "models": models,
            "modelDetails": [{"id": value, "displayName": value} for value in models],
            "attempts": attempts,
        }
    if args.json:
        _print_machine_json(payload)
    else:
        print("Models: " + (", ".join(payload["models"]) or "<none>"))
        if payload["endpoint"]:
            print("Endpoint: " + payload["endpoint"])
    return 0


def _start_login_control_reader(
    loop: asyncio.AbstractEventLoop,
    start: asyncio.Event,
    cancel: asyncio.Event,
    *,
    start_gated: bool,
) -> None:
    def signal(event: asyncio.Event) -> None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(event.set)

    def records(stream: Any):
        """Read bounded records without holding Python's buffered-stdin lock.

        Subscription commands normally finish while qrenderdoc deliberately
        keeps the control pipe open for cancellation.  A daemon blocked in
        ``BufferedReader.readline`` can otherwise trip CPython's shutdown lock.
        Real file descriptors use ``os.read``; in-memory test streams retain a
        small synchronous fallback that always terminates.
        """

        try:
            descriptor = int(stream.fileno())
        except (AttributeError, OSError, TypeError, ValueError):
            while True:
                yield stream.readline(4097)
            # Unreachable, but keeps this branch visibly generator-only.

        pending = bytearray()
        while True:
            newline = pending.find(b"\n")
            if newline >= 0:
                record = bytes(pending[: newline + 1])
                del pending[: newline + 1]
                yield record
                continue
            if len(pending) > 4096:
                yield bytes(pending)
                return
            chunk = os.read(descriptor, 4096)
            if not chunk:
                yield bytes(pending) if pending else b""
                return
            pending.extend(chunk)

    def read_control() -> None:
        try:
            stream = getattr(sys.stdin, "buffer", sys.stdin)
            for raw in records(stream):
                if raw in {b"", ""}:
                    if start_gated:
                        signal(cancel)
                    return
                value = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
                if len(value) > 4096 or not value.endswith(b"\n"):
                    signal(cancel)
                    return
                command = value.strip()
                if command == b"start" and start_gated and not start.is_set():
                    signal(start)
                    continue
                if command == b"cancel":
                    signal(cancel)
                    return
                # Unknown commands and duplicate starts fail closed.
                signal(cancel)
                return
        except Exception:
            if start_gated:
                signal(cancel)
            return

    threading.Thread(
        target=read_control,
        name="replaylab-codex-login-control",
        daemon=True,
    ).start()


async def _wait_for_login_start_gate(
    start: asyncio.Event,
    cancel: asyncio.Event,
    *,
    timeout: float = 30.0,
) -> None:
    start_wait = asyncio.create_task(start.wait())
    cancel_wait = asyncio.create_task(cancel.wait())
    try:
        done, _ = await asyncio.wait(
            {start_wait, cancel_wait},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_wait in done or not done:
            raise ValueError(
                "Codex login start gate closed before 'start'; app-server was not created"
            )
    finally:
        for task in (start_wait, cancel_wait):
            if not task.done():
                task.cancel()
        await asyncio.gather(start_wait, cancel_wait, return_exceptions=True)


async def _wait_for_subscription_start_gate(
    start: asyncio.Event,
    cancel: asyncio.Event,
    *,
    timeout: float,
) -> None:
    start_wait = asyncio.create_task(start.wait())
    cancel_wait = asyncio.create_task(cancel.wait())
    try:
        done, _ = await asyncio.wait(
            {start_wait, cancel_wait},
            timeout=min(timeout, 30.0),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_wait in done or not done:
            raise ValueError(
                "subscription start gate closed before 'start'; no vendor process was created"
            )
    finally:
        for task in (start_wait, cancel_wait):
            if not task.done():
                task.cancel()
        await asyncio.gather(start_wait, cancel_wait, return_exceptions=True)


async def _subscription_command(
    args: argparse.Namespace,
    store: ProfileStore,
    backend_factory: Any | None = None,
) -> int:
    """Run the qrenderdoc-contained subscription state machine."""

    from .connectors.subscription import (
        SubscriptionBackend,
        SubscriptionCancelled,
        detect_subscription,
    )

    profile = _selected_profile(store, args.profile)
    if args.subscription_command == "status":
        # Contract: status performs PATH resolution only and must not construct
        # an app-server, ACP transport, login process or interactive terminal.
        payload = detect_subscription(profile)
        if args.json:
            _print_machine_json(payload)
        else:
            print(payload["message"])
        return 0

    timeout = float(args.timeout)
    if timeout <= 0 or timeout > 3600:
        raise ValueError("subscription timeout must be between 0 and 3600 seconds")
    if not bool(getattr(args, "start_gated", False)):
        raise ValueError("subscription connect/test requires --start-gated")

    def emit(payload: dict[str, Any]) -> None:
        if args.json:
            _print_machine_json(payload)
        elif payload.get("message"):
            print(payload["message"], flush=True)

    factory = backend_factory or SubscriptionBackend
    backend = factory(profile, timeout=timeout, emit=emit)

    def close_backend() -> None:
        closer = getattr(backend, "close", None)
        if callable(closer):
            closer()

    start = asyncio.Event()
    cancel = asyncio.Event()
    _start_login_control_reader(
        asyncio.get_running_loop(), start, cancel, start_gated=True
    )
    terminal_kind = (
        "subscriptionCompleted"
        if args.subscription_command == "connect"
        else "testCompleted"
    )
    backend.event(
        "waitingForStart",
        "waitingForStart",
        "Waiting for qrenderdoc process containment before starting the provider.",
    )
    try:
        await _wait_for_subscription_start_gate(start, cancel, timeout=timeout)
    except ValueError as exc:
        backend.event(
            terminal_kind,
            "cancelled",
            str(exc),
            authenticated=False,
            testCompleted=False,
        )
        close_backend()
        return 1

    if not backend.detection["installed"]:
        backend.event(
            terminal_kind,
            "missing",
            backend.detection["message"],
            installed=False,
            authenticated=False,
            installHint=backend.detection.get("installHint", ""),
            testCompleted=False,
        )
        close_backend()
        return 2
    if cancel.is_set():
        backend.event(
            terminal_kind,
            "cancelled",
            "Subscription operation cancelled before provider startup.",
            authenticated=False,
            testCompleted=False,
        )
        close_backend()
        return 1


    try:
        if args.subscription_command == "connect":
            backend.event(
                "connectStarted",
                "connecting",
                "Provider sign-in started inside the qrenderdoc process job.",
            )
            result = await backend.connect(args.flow, cancel)
            backend.event(
                "testCompleted",
                "ok" if result.authenticated else "failed",
                result.message,
                authenticated=result.authenticated,
                terminalOpened=result.terminal_opened,
                models=list(result.models),
                account=result.account,
                testCompleted=True,
            )
            backend.event(
                "subscriptionCompleted",
                "connected" if result.authenticated else "failed",
                result.message,
                authenticated=result.authenticated,
                terminalOpened=result.terminal_opened,
                models=list(result.models),
                account=result.account,
                testCompleted=True,
            )
        elif args.subscription_command == "test":
            backend.event(
                "testStarted",
                "testing",
                "Running a bounded no-generation provider connection test.",
            )
            result = await backend.test(cancel)
            backend.event(
                "testCompleted",
                "ok" if result.authenticated else "failed",
                result.message,
                authenticated=result.authenticated,
                terminalOpened=result.terminal_opened,
                models=list(result.models),
                account=result.account,
                testCompleted=True,
            )
        else:
            raise AssertionError(args.subscription_command)
        return 0 if result.authenticated else 1
    except SubscriptionCancelled as exc:
        backend.event(
            terminal_kind,
            "cancelled",
            str(exc),
            authenticated=False,
            testCompleted=False,
        )
        return 1
    except ReplayLabAIError as exc:
        backend.event(
            terminal_kind,
            "failed",
            str(exc),
            authenticated=False,
            testCompleted=args.subscription_command == "test",
        )
        return 1


async def _auth_command(args: argparse.Namespace, store: ProfileStore) -> int:
    profile = _selected_profile(store, args.profile)
    protocol = profile.provider.protocol
    if protocol == "acp":
        if args.auth_command != "status":
            raise ValueError(
                "ACP authentication is owned by the configured local vendor Agent; "
                "run that vendor's login command outside ReplayLab"
            )
        payload = {
            "profile": profile.id,
            "protocol": "acp",
            "status": "managedExternally",
            "command": profile.argv,
        }
        if args.json:
            _print_machine_json(payload)
        else:
            print("Authentication is managed by the configured ACP vendor CLI.")
        return 0
    if protocol != "codex-app-server":
        if args.auth_command != "status":
            raise ValueError("model APIs use the profile's session/environment API key")
        configured = profile.provider.auth in {"none", "adc"} or bool(
            os.environ.get(profile.key_env, "").strip()
        )
        payload = {
            "profile": profile.id,
            "protocol": protocol,
            "status": "configured" if configured else "missingCredential",
            "auth": profile.provider.auth,
            "apiKeyEnv": profile.key_env,
        }
        if args.json:
            _print_machine_json(payload)
        else:
            print(payload["status"])
        return 0

    timeout = float(getattr(args, "timeout", 30.0))
    login_start_event: asyncio.Event | None = None
    login_cancel_event: asyncio.Event | None = None
    if args.auth_command == "login":
        if timeout <= 0:
            raise ValueError("Codex login timeout must be positive")
        login_start_event = asyncio.Event()
        login_cancel_event = asyncio.Event()
        start_gated = bool(getattr(args, "start_gated", False))
        if not start_gated:
            login_start_event.set()
        _start_login_control_reader(
            asyncio.get_running_loop(),
            login_start_event,
            login_cancel_event,
            start_gated=start_gated,
        )
        if start_gated:
            await _wait_for_login_start_gate(
                login_start_event,
                login_cancel_event,
                timeout=min(timeout, 30.0),
            )

    async with _codex_connector(profile, timeout=timeout) as connector:
        if args.auth_command == "status":
            account = await connector.account_read(refresh_token=args.refresh)
            payload = {
                "profile": profile.id,
                "protocol": protocol,
                "status": "authenticated" if account.get("account") else "signedOut",
                **account,
            }
            if args.json:
                _print_machine_json(payload)
            else:
                print(payload["status"])
            return 0
        if args.auth_command == "logout":
            await connector.logout()
            payload = {"profile": profile.id, "status": "signedOut"}
            if args.json:
                _print_machine_json(payload)
            else:
                print("Signed out of Codex.")
            return 0
        if args.auth_command == "cancel":
            await connector.cancel_login(args.login_id)
            payload = {
                "profile": profile.id,
                "status": "cancelRequested",
                "loginId": args.login_id,
            }
            if args.json:
                _print_machine_json(payload)
            else:
                print("Codex login cancellation requested.")
            return 0
        if args.auth_command == "login":
            assert login_cancel_event is not None
            if login_cancel_event.is_set():
                raise ValueError("Codex login was cancelled before app-server startup")
            started = await connector.login_start(flow=args.flow)
            start_payload = {
                "event": "loginStarted",
                "profile": profile.id,
                "flow": args.flow,
                "url": started.get("authUrl", started.get("verificationUrl", "")),
                "code": started.get("userCode", ""),
                **started,
            }
            if args.json:
                _print_machine_json(start_payload)
            else:
                if args.flow == "browser":
                    print("Open: " + str(started["authUrl"]), flush=True)
                else:
                    print(
                        f"Open: {started['verificationUrl']}  Code: {started['userCode']}",
                        flush=True,
                    )
            login_id = str(started["loginId"])
            completion_task = asyncio.create_task(
                connector.wait_login(login_id, timeout=args.timeout)
            )
            cancel_wait = asyncio.create_task(login_cancel_event.wait())
            cancel_request: asyncio.Task[Any] | None = None
            try:
                done, _ = await asyncio.wait(
                    {completion_task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
                )
                if cancel_wait in done and not completion_task.done():
                    cancel_request = asyncio.create_task(connector.cancel_login(login_id))
                    done, _ = await asyncio.wait(
                        {completion_task, cancel_request},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if cancel_request in done:
                        await cancel_request
                    if not completion_task.done():
                        completion = await completion_task
                    else:
                        completion = completion_task.result()
                else:
                    completion = await completion_task
            finally:
                for task in (cancel_wait, cancel_request):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(
                    *(task for task in (cancel_wait, cancel_request) if task is not None),
                    return_exceptions=True,
                )
            account = await connector.account_read()
            complete_payload = {
                "event": "loginCompleted",
                "profile": profile.id,
                **completion,
                "account": account.get("account"),
            }
            if args.json:
                _print_machine_json(complete_payload)
            else:
                print("Codex login completed." if completion.get("success") else "Codex login failed.")
            return 0 if completion.get("success") else 1
    raise AssertionError(args.auth_command)


async def _work_once(args: argparse.Namespace, store: ProfileStore) -> int:
    runtime = AgentRuntime(store, _catalog(), mcp_executable=args.mcp_host)
    work_kwargs = {"profile_id": args.profile, "instance_id": args.instance_id}
    if getattr(args, "model", ""):
        work_kwargs["model_override"] = args.model
    result = await runtime.process_one_intent(**work_kwargs)
    if result is None:
        if args.json:
            _print_machine_json({"claimed": False})
        else:
            print("No queued qrenderdoc AI intent.")
        return 3
    if args.json:
        _print_machine_json(_result_payload(result))
    else:
        print(result.text)
    return 0


_WORKER_SHUTDOWN = object()
_WORKER_CONTROL_MAX_LINE_BYTES = 4096


async def _worker_operation_or_shutdown(
    operation: Any, shutdown_event: asyncio.Event | None
) -> Any:
    if shutdown_event is None:
        return await operation
    operation_task = asyncio.ensure_future(operation)
    shutdown_task = asyncio.create_task(shutdown_event.wait())
    try:
        done, _ = await asyncio.wait(
            {operation_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if shutdown_task in done:
            if not operation_task.done():
                operation_task.cancel()
            await asyncio.gather(operation_task, return_exceptions=True)
            return _WORKER_SHUTDOWN
        return await operation_task
    finally:
        if not shutdown_task.done():
            shutdown_task.cancel()
        await asyncio.gather(shutdown_task, return_exceptions=True)


async def _worker_sleep_or_shutdown(
    seconds: float, shutdown_event: asyncio.Event | None
) -> bool:
    if shutdown_event is None:
        await asyncio.sleep(seconds)
        return False
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def _worker(
    args: argparse.Namespace,
    store: ProfileStore,
    *,
    shutdown_event: asyncio.Event | None = None,
    start_event: asyncio.Event | None = None,
) -> int:
    if args.poll_seconds < 0.1 or args.poll_seconds > 60:
        raise ValueError("--poll-seconds must be between 0.1 and 60")
    if args.idle_exit_seconds < 0:
        raise ValueError("--idle-exit-seconds cannot be negative")
    if start_event is not None:
        started = await _worker_operation_or_shutdown(start_event.wait(), shutdown_event)
        if started is _WORKER_SHUTDOWN:
            return 0
    runtime = AgentRuntime(store, _catalog(), mcp_executable=args.mcp_host)
    idle_since = time.monotonic()
    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            return 0
        try:
            work_kwargs = {"profile_id": args.profile, "instance_id": args.instance_id}
            if getattr(args, "model", ""):
                work_kwargs["model_override"] = args.model
            result = await _worker_operation_or_shutdown(
                runtime.process_one_intent(**work_kwargs),
                shutdown_event,
            )
            if result is _WORKER_SHUTDOWN:
                return 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _print_utf8_error(
                f"replaylab-ai worker: intent failed ({type(exc).__name__}): "
                f"{str(exc)[:1000]}"
            )
            if await _worker_sleep_or_shutdown(
                max(args.poll_seconds, 1.0), shutdown_event
            ):
                return 0
            continue
        if result:
            print(f"[{result.task_id}] {result.text}", flush=True)
            idle_since = time.monotonic()
            continue
        if args.idle_exit_seconds and time.monotonic() - idle_since >= args.idle_exit_seconds:
            return 0
        if await _worker_sleep_or_shutdown(args.poll_seconds, shutdown_event):
            return 0


def _start_worker_control_reader(
    loop: asyncio.AbstractEventLoop,
    start_event: asyncio.Event,
    shutdown_event: asyncio.Event,
) -> None:
    def set_threadsafe(event: asyncio.Event) -> None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(event.set)

    def fail_closed() -> None:
        with contextlib.suppress(Exception):
            _print_utf8_error(
                "replaylab-ai worker: invalid control-stdin command; shutting down"
            )
        set_threadsafe(shutdown_event)

    def read_control() -> None:
        try:
            stream = getattr(sys.stdin, "buffer", sys.stdin)
            while True:
                command = stream.readline(_WORKER_CONTROL_MAX_LINE_BYTES + 1)
                if command == b"" or command == "":
                    set_threadsafe(shutdown_event)
                    return
                raw_command = (
                    command.encode("utf-8")
                    if isinstance(command, str)
                    else bytes(command)
                )
                if len(raw_command) > _WORKER_CONTROL_MAX_LINE_BYTES:
                    fail_closed()
                    return
                if not raw_command.endswith(b"\n"):
                    # readline returned a non-terminated fragment only because stdin reached EOF.
                    set_threadsafe(shutdown_event)
                    return
                normalized = raw_command.strip()
                if normalized == b"start":
                    set_threadsafe(start_event)
                    continue
                if normalized == b"shutdown":
                    set_threadsafe(shutdown_event)
                    return
                fail_closed()
                return
        except (OSError, RuntimeError, TypeError, ValueError):
            set_threadsafe(shutdown_event)

    threading.Thread(
        target=read_control,
        name="replaylab-worker-control",
        daemon=True,
    ).start()


async def _worker_main(args: argparse.Namespace, store: ProfileStore) -> int:
    start_gated = bool(getattr(args, "start_gated", False))
    control_stdin = bool(getattr(args, "control_stdin", False))
    if start_gated and not control_stdin:
        raise ValueError("--start-gated requires --control-stdin")

    shutdown_event = asyncio.Event()
    start_event = asyncio.Event()
    if not start_gated:
        start_event.set()
    loop = asyncio.get_running_loop()
    previous_sigterm: Any = None
    signal_installed = False
    try:
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        try:
            loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
            signal_installed = True
        except (NotImplementedError, RuntimeError):
            signal.signal(
                signal.SIGTERM,
                lambda _signum, _frame: loop.call_soon_threadsafe(shutdown_event.set),
            )
            signal_installed = True
    except (AttributeError, OSError, ValueError):
        signal_installed = False

    if control_stdin:
        _start_worker_control_reader(loop, start_event, shutdown_event)
    try:
        return await _worker(
            args,
            store,
            shutdown_event=shutdown_event,
            start_event=start_event,
        )
    finally:
        if signal_installed:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(signal.SIGTERM)
            if previous_sigterm is not None:
                with contextlib.suppress(Exception):
                    signal.signal(signal.SIGTERM, previous_sigterm)


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8_stdio()
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "presets":
            _print_presets(args.json)
            return 0
        if args.command == "mcp-config":
            _print_mcp_config(args.mcp_host)
            return 0
        if args.command == "self-test":
            # This branch intentionally precedes ProfileStore construction: the
            # deterministic harness must not inspect real profiles or credentials.
            return asyncio.run(_self_test_command(args))
        store = _load_store(args.config)
        if args.command == "profile":
            from .credential_store import default_credential_store

            return _profile_command(args, store, default_credential_store())
        if args.command == "credential":
            return _credential_command(args, store)
        if args.command == "subscription":
            return asyncio.run(_subscription_command(args, store))
        if args.command == "models":
            return asyncio.run(_models_command(args, store))
        if args.command == "auth":
            return asyncio.run(_auth_command(args, store))
        if args.command == "doctor":
            profile = _selected_profile(store, args.profile)
            checks = run_doctor(
                profile,
                _catalog(),
                mcp_executable=args.mcp_host,
            )
            if args.json:
                _print_machine_json(
                    [
                        {"name": check.name, "status": check.status, "detail": check.detail}
                        for check in checks
                    ]
                )
            else:
                for check in checks:
                    print(f"[{check.status.upper():7}] {check.name}: {check.detail}")
            return 1 if any(check.status == "error" for check in checks) else 0
        if args.command == "run":
            return asyncio.run(_run_once(args, store))
        if args.command == "probe":
            return asyncio.run(_probe(args, store))
        if args.command == "work-once":
            return asyncio.run(_work_once(args, store))
        if args.command == "worker":
            return asyncio.run(_worker_main(args, store))
        parser.error(f"unknown command: {args.command}")
    except (ReplayLabAIError, KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        _print_utf8_error(f"replaylab-ai: {exc}")
        return 2
    except KeyboardInterrupt:
        _print_utf8_error("replaylab-ai: cancelled")
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
