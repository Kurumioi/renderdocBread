"""ReplayLab AgentHost command line.

Only three top-level commands are intentionally exposed:

* ``run`` executes one task;
* ``profile`` owns every connection-management operation;
* ``doctor`` performs offline diagnostics.

Model API profiles always use deepagents. Subscription profiles always use
the provider's native Agent protocol. There is no legacy-engine selector.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import sys
import threading
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Sequence

from .agent_contracts import RunRequest
from .agent_diagnostics import configure_logging, log_exception
from .agent_registry import CatalogView
from .bridge import BridgeClient
from .catalog import PRESETS, get_preset
from .conversation import NATIVE_PROMPT_WIRE_MAX_BYTES, SUPPORTED_CONVERSATION_SCHEMAS
from .deep_engine import DeepAgentsEngine
from .doctor import run_doctor
from .engine_config import load_engine_config
from .errors import ConfigurationError, ReplayLabAIError
from .native_engine import NativeAgentEngine
from .profiles import Profile, ProfileStore
from .probe import discover_models
from .process_env import (
    remove_codebuddy_profile_home,
    remove_codex_profile_home,
    remove_cursor_profile_home,
    remove_managed_vendor_profile_home,
)
from .skills import INTENT_SKILLS

_NATIVE_START_GATE_ENV = "REPLAYLAB_START_GATED"
_NATIVE_START_GATE_RECORD = b"start\n"
_MAX_STDERR_BYTES = 64 * 1024


class _SafeArgumentParser(argparse.ArgumentParser):
    """Keep rejected command-line values out of the terminal transcript."""

    def error(self, message: str) -> None:
        del message
        self.print_usage(sys.stderr)
        self.exit(
            2,
            "replaylab-ai: error[invalid-command-line]: Invalid command line. "
            "Use --help to review the supported options.\n",
        )


def _add_profile_definition_arguments(
    parser: argparse.ArgumentParser, *, update: bool
) -> None:
    parser.add_argument("--preset", required=not update)
    parser.add_argument("--model", default=None if update else "")
    parser.add_argument("--allowed-model", action="append", default=None if update else [])
    parser.add_argument("--default-model", default=None if update else "")
    parser.add_argument(
        "--model-context-window",
        action="append",
        default=None if update else [],
        metavar="MODEL=TOKENS",
    )
    parser.add_argument("--title", default=None if update else "")
    parser.add_argument("--base-url", default=None if update else "")
    parser.add_argument("--api-key-env", default=None if update else "")
    parser.add_argument("--agent-env", action="append", default=None if update else [])
    parser.add_argument("--option", action="append", default=None if update else [])
    parser.add_argument("--capability", action="append", default=None if update else [])
    parser.add_argument("--select", action="store_true")
    parser.add_argument("--json", action="store_true")
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--command", dest="agent_command", nargs="+", default=None if update else [])
    commands.add_argument("--command-json", default=None if update else "", metavar="JSON")
    if update:
        parser.add_argument("--replace-provider", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="replaylab-ai",
        description="ReplayLab AgentHost",
    )
    parser.add_argument("--config", type=Path, help="override profiles.json")
    parser.add_argument("--engine-config", type=Path, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run one chat or exact Skill request")
    run.add_argument("--profile", default="")
    run.add_argument("--model", default="")
    run.add_argument("--skill", default="")
    run.add_argument("--intent-kind", choices=tuple(INTENT_SKILLS), default="")
    prompt = run.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-stdin", action="store_true")
    run.add_argument("--context-file", type=Path)
    run.add_argument("--live-context", action="store_true")
    run.add_argument("--instance-id", default="")
    run.add_argument("--task-id", default="")
    run.add_argument("--cancel-file", type=Path)
    run.add_argument("--mcp-host", type=Path)
    run.add_argument("--json", action="store_true")

    profile = commands.add_parser("profile", help="manage AI connections")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    presets = profile_commands.add_parser("presets")
    presets.add_argument("--json", action="store_true")
    listing = profile_commands.add_parser("list")
    listing.add_argument("--json", action="store_true")
    add = profile_commands.add_parser("add")
    add.add_argument("id")
    _add_profile_definition_arguments(add, update=False)
    update = profile_commands.add_parser("update")
    update.add_argument("id")
    _add_profile_definition_arguments(update, update=True)
    select = profile_commands.add_parser("select")
    select.add_argument("id")
    select.add_argument("--json", action="store_true")
    remove = profile_commands.add_parser("remove")
    remove.add_argument("id")
    remove.add_argument("--json", action="store_true")
    models = profile_commands.add_parser("models")
    models.add_argument("id")
    models.add_argument("--allowed-model", action="append", default=None)
    models.add_argument("--default-model")
    models.add_argument("--model-context-window", action="append", default=None)
    models.add_argument("--json", action="store_true")

    discover = profile_commands.add_parser("discover-models")
    source = discover.add_mutually_exclusive_group()
    source.add_argument("--profile", default="")
    source.add_argument("--preset", default="")
    discover.add_argument("--base-url", default="")
    discover.add_argument("--api-key-env", default="")
    discover.add_argument("--api-key-stdin", action="store_true")
    discover.add_argument("--timeout", type=float, default=15.0)
    discover.add_argument("--include-hidden", action="store_true")
    discover.add_argument("--json", action="store_true")

    test_model = profile_commands.add_parser("test-model")
    source = test_model.add_mutually_exclusive_group()
    source.add_argument("--profile", default="")
    source.add_argument("--preset", default="")
    test_model.add_argument("--model", default="")
    test_model.add_argument("--timeout", type=float, default=90.0)
    test_model.add_argument("--json", action="store_true")

    credential = profile_commands.add_parser("credential")
    credential_commands = credential.add_subparsers(dest="credential_command", required=True)
    for name in ("status", "delete"):
        operation = credential_commands.add_parser(name)
        operation.add_argument("--profile", default="")
        operation.add_argument("--json", action="store_true")
    store = credential_commands.add_parser("store")
    store.add_argument("--profile", default="")
    store.add_argument("--api-key-stdin", action="store_true", required=True)
    store.add_argument("--json", action="store_true")

    subscription = profile_commands.add_parser("subscription")
    subscription_commands = subscription.add_subparsers(
        dest="subscription_command", required=True
    )
    status = subscription_commands.add_parser("status")
    status.add_argument("--profile", required=True)
    status.add_argument("--json", action="store_true")
    connect = subscription_commands.add_parser("connect")
    connect.add_argument("--profile", required=True)
    connect.add_argument("--flow", choices=("browser", "device"), default="browser")
    connect.add_argument("--timeout", type=float, default=600.0)
    connect.add_argument("--start-gated", action="store_true", required=True)
    connect.add_argument("--json", action="store_true")
    test = subscription_commands.add_parser("test")
    test.add_argument("--profile", required=True)
    test.add_argument("--timeout", type=float, default=60.0)
    test.add_argument("--start-gated", action="store_true", required=True)
    test.add_argument("--json", action="store_true")

    doctor = commands.add_parser("doctor", help="run offline connection diagnostics")
    doctor.add_argument("--profile", default="")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--mcp-host", type=Path)
    return parser


def default_skill_root() -> Path:
    if getattr(sys, "frozen", False):
        bundle_root = getattr(sys, "_MEIPASS", "")
        if bundle_root:
            return Path(bundle_root) / ".agents" / "skills"
    return Path(__file__).resolve().parents[3] / ".agents" / "skills"


def _load_store(path: Path | None) -> ProfileStore:
    return ProfileStore(path).load()


def _catalog() -> CatalogView:
    return CatalogView.load(skill_roots=[default_skill_root()])


def _parse_options(values: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in values:
        if "=" not in item:
            raise ValueError("profile option must be KEY=VALUE")
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


def _parse_model_context_windows(values: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in values:
        if "=" not in item:
            raise ValueError("model context window must be MODEL=TOKENS")
        model, raw = (part.strip() for part in item.rsplit("=", 1))
        if not model:
            raise ValueError("model context window model id is empty")
        try:
            window = int(raw, 10)
        except ValueError as exc:
            raise ValueError("model context window tokens must be a decimal integer") from exc
        if model in result:
            raise ValueError("model context window model ids must be unique")
        result[model] = window
    return result


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


def _machine_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8", errors="backslashreplace"
    )


def _print_machine_json(value: Any) -> None:
    encoded = _machine_json(value) + b"\n"
    binary = getattr(sys.stdout, "buffer", None)
    if binary is not None:
        binary.write(encoded)
        binary.flush()
    else:
        sys.stdout.write(encoded.decode("utf-8"))
        sys.stdout.flush()


def _selected_profile(store: ProfileStore, profile_id: str) -> Profile:
    if profile_id:
        try:
            return store.profiles[profile_id]
        except KeyError as exc:
            raise ValueError(f"unknown profile: {profile_id}") from exc
    return store.active()


def _print_presets(as_json: bool) -> int:
    rows = [
        {
            "id": item.id,
            "title": item.title,
            "kind": item.kind,
            "executionMode": "model-api" if item.kind == "model" else "agent-protocol",
            "loopOwner": "deepagents" if item.kind == "model" else "vendor",
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
    else:
        print(f"{'ID':<18} {'TYPE':<7} {'PROTOCOL':<21} TITLE")
        for row in rows:
            print(f"{row['id']:<18} {row['kind']:<7} {row['protocol']:<21} {row['title']}")
    return 0


def _profile_wire(profile: Profile, *, active: bool) -> dict[str, Any]:
    return {
        "id": profile.id,
        "title": profile.title or profile.id,
        "active": active,
        "preset": profile.preset,
        "kind": profile.provider.kind,
        "executionMode": "model-api" if profile.provider.kind == "model" else "agent-protocol",
        "loopOwner": "deepagents" if profile.provider.kind == "model" else "vendor",
        "protocol": profile.provider.protocol,
        "auth": profile.provider.auth,
        "model": profile.model,
        "models": list(profile.models),
        "defaultModel": profile.default_model,
        "modelContextWindows": dict(profile.model_context_windows),
        "baseUrl": profile.base_url,
        "endpoint": profile.endpoint,
        "apiKeyEnv": profile.key_env,
        "command": profile.argv,
        "agentEnv": list(profile.agent_env),
        "options": dict(profile.options),
        "capabilities": sorted(profile.capabilities),
        "runInputSchemas": list(SUPPORTED_CONVERSATION_SCHEMAS),
    }


def _remove_provider_state(profile: Profile) -> None:
    if profile.preset == "codex":
        remove_codex_profile_home(profile)
    elif profile.preset == "cursor-acp":
        remove_cursor_profile_home(profile)
    elif profile.preset == "codebuddy-acp":
        remove_codebuddy_profile_home(profile)
    elif profile.preset in {
        "copilot-acp",
        "gemini-acp",
        "qwen-acp",
        "kimi-acp",
        "claude-code-acp",
    }:
        remove_managed_vendor_profile_home(profile)


def _profile_command(
    args: argparse.Namespace,
    store: ProfileStore,
    credential_store: Any | None = None,
) -> int:
    command = args.profile_command
    if command == "list":
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

    if command == "add":
        get_preset(args.preset)
        models = list(dict.fromkeys(args.allowed_model))
        legacy_model = str(args.model or "").strip()
        default_model = str(args.default_model or "").strip()
        for model in (legacy_model, default_model):
            if model and model not in models:
                models.append(model)
        selected = legacy_model or default_model or (models[0] if models else "")
        profile = Profile(
            id=args.id,
            preset=args.preset,
            model=selected,
            models=models,
            default_model=default_model or selected,
            title=args.title,
            base_url=args.base_url,
            api_key_env=args.api_key_env,
            command=_parse_agent_command(args),
            agent_env=list(dict.fromkeys(args.agent_env)),
            options=_parse_options(args.option),
            probed_capabilities=list(dict.fromkeys(args.capability)) or None,
            model_context_windows=_parse_model_context_windows(args.model_context_window),
        )
        if profile.id in store.profiles:
            raise ValueError(f"profile already exists: {profile.id}")
        store.add(profile, select=args.select)
        store.save()
        if args.json:
            _print_machine_json(_profile_wire(profile, active=profile.id == store.active_profile))
        else:
            print(f"Added profile '{profile.id}' ({profile.provider.title}).")
        return 0

    if command == "select":
        selected = store.select(args.id)
        store.save()
        if args.json:
            _print_machine_json(_profile_wire(selected, active=True))
        else:
            print(f"Active AI profile: {selected.id}")
        return 0

    if command == "remove":
        if args.id not in store.profiles:
            raise ValueError(f"unknown profile: {args.id}")
        removed = store.profiles[args.id]
        _remove_provider_state(removed)
        if (
            credential_store is not None
            and credential_store.supported
            and removed.provider.kind == "model"
            and removed.provider.auth == "api-key"
        ):
            credential_store.delete(removed.id)
        del store.profiles[removed.id]
        if store.active_profile == removed.id:
            store.active_profile = sorted(store.profiles)[0] if store.profiles else ""
        store.save()
        payload = {"removed": removed.id, "activeProfile": store.active_profile}
        if args.json:
            _print_machine_json(payload)
        else:
            print(f"Removed profile '{removed.id}'.")
        return 0

    if command not in {"update", "models"}:
        raise AssertionError(command)
    try:
        current = store.profiles[args.id]
    except KeyError as exc:
        raise ValueError(f"unknown profile: {args.id}") from exc
    raw = asdict(current)
    if command == "models":
        if (
            args.allowed_model is None
            and args.default_model is None
            and args.model_context_window is None
        ):
            raise ValueError(
                "profile models requires --allowed-model, --default-model or --model-context-window"
            )
        if args.allowed_model is not None:
            raw["models"] = list(dict.fromkeys(args.allowed_model))
        if args.default_model is not None:
            raw["default_model"] = args.default_model
            raw["model"] = args.default_model
        if args.model_context_window is not None:
            raw["model_context_windows"] = _parse_model_context_windows(
                args.model_context_window
            )
    else:
        replacing = bool(args.replace_provider)
        requested_preset = args.preset or current.preset
        if args.preset and args.preset != current.preset and not replacing:
            raise ValueError(
                "changing preset requires --replace-provider so old provider settings cannot leak"
            )
        if replacing:
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
                "model_context_windows": {},
            }
        for argument, field_name in {
            "preset": "preset",
            "model": "model",
            "default_model": "default_model",
            "title": "title",
            "base_url": "base_url",
            "api_key_env": "api_key_env",
        }.items():
            value = getattr(args, argument, None)
            if value is not None:
                raw[field_name] = value
                if argument == "default_model":
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
        if args.model_context_window is not None:
            raw["model_context_windows"] = _parse_model_context_windows(
                args.model_context_window
            )
        if args.command_json is not None or args.agent_command is not None:
            raw["command"] = _parse_agent_command(args)
    updated = Profile.from_dict(raw)
    if updated.default_model and updated.default_model not in updated.models:
        raise ValueError("default model must also be supplied as --allowed-model")
    provider_changed = command == "update" and args.replace_provider and updated.preset != current.preset
    if (
        provider_changed
        and credential_store is not None
        and credential_store.supported
        and current.provider.kind == "model"
        and current.provider.auth == "api-key"
    ):
        credential_store.delete(current.id)
    if provider_changed:
        _remove_provider_state(current)
    store.profiles[updated.id] = updated
    if getattr(args, "select", False):
        store.active_profile = updated.id
    store.save()
    if args.json:
        _print_machine_json(_profile_wire(updated, active=updated.id == store.active_profile))
    else:
        print(f"Updated profile '{updated.id}'.")
    return 0


def _read_session_api_key() -> str:
    from .credential_store import normalize_api_credential

    stream = getattr(sys.stdin, "buffer", sys.stdin)
    raw = stream.read(64 * 1024 + 1)
    encoded = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    if len(encoded) > 64 * 1024:
        raise ValueError("stdin API key exceeds the 64 KiB safety limit")
    try:
        value = encoded.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("stdin API key must be valid UTF-8") from exc
    if not value or any(character in value for character in ("\0", "\r", "\n")):
        raise ValueError("stdin API key must contain one non-empty line")
    return normalize_api_credential(value)


def _credential_command(
    args: argparse.Namespace,
    profiles: ProfileStore,
    credential_store: Any | None = None,
) -> int:
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
    elif args.credential_command == "status":
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
        marker = "__model_discovery__" if preset.kind == "model" else ""
        return Profile(
            id="discovery",
            preset=preset.id,
            model=marker,
            models=[marker] if marker else [],
            default_model=marker,
            base_url=args.base_url,
            api_key_env=args.api_key_env,
        )
    return store.active()


def _codex_connector(profile: Profile, *, timeout: float) -> Any:
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
    preset = get_preset(profile.preset)
    if preset.selectable_models:
        models = list(preset.selectable_models)
        payload = {
            "profile": profile.id if args.profile else "",
            "preset": profile.preset,
            "kind": profile.provider.kind,
            "protocol": profile.provider.protocol,
            "endpoint": "",
            "models": models,
            "modelDetails": [{"id": value, "displayName": value} for value in models],
            "attempts": [],
        }
    elif profile.provider.protocol == "codex-app-server":
        if args.api_key_stdin:
            raise ValueError("Codex app-server owns authentication; do not pass an API key")
        async with _codex_connector(profile, timeout=args.timeout) as connector:
            details = await connector.list_models(include_hidden=args.include_hidden)
        models = []
        for item in details:
            value = item.get("id", item.get("model", ""))
            if isinstance(value, str) and value and value not in models:
                models.append(value)
        payload = {
            "profile": profile.id if args.profile else "",
            "preset": profile.preset,
            "kind": profile.provider.kind,
            "protocol": profile.provider.protocol,
            "endpoint": "",
            "models": models,
            "modelDetails": details,
            "attempts": [],
        }
    else:
        key = _read_session_api_key() if args.api_key_stdin else None
        endpoint, models, attempts = await discover_models(
            profile, timeout=args.timeout, api_key=key
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
    return 0


async def _test_model_command(args: argparse.Namespace, store: ProfileStore) -> int:
    """Send one bounded prompt through an ACP model selection."""

    from .connectors.acp import ACPConnector
    from .process_env import external_agent_environment, resolved_agent_argv

    profile = _discovery_profile(args, store)
    model = str(args.model or "").strip()
    shown = model or "(provider default)"
    payload: dict[str, Any] = {
        "ok": False,
        "model": shown,
        "message": "",
        "diagnostic": "",
    }
    if profile.provider.protocol != "acp":
        payload["message"] = "Model testing is available only for ACP connections."
        payload["diagnostic"] = f"protocol: {profile.provider.protocol}"
        if args.json:
            _print_machine_json(payload)
        else:
            print(payload["message"])
        return 2
    if profile.options.get("allow_unsandboxed_agent") is not True:
        payload["message"] = "The trusted ACP process is not enabled for this connection."
        if args.json:
            _print_machine_json(payload)
        else:
            print(payload["message"])
        return 2
    tested = replace(profile, model=model)
    try:
        connector = ACPConnector(
            resolved_agent_argv(tested),
            cwd=Path.cwd(),
            timeout=float(args.timeout),
            env=external_agent_environment(tested),
            authentication_method=tested.provider.acp_auth_method,
            provider_id=tested.preset,
        )
        await connector.test_model()
        payload["ok"] = True
        payload["message"] = f"Model '{shown}' responded successfully."
    except Exception as exc:  # user-requested connection diagnostic
        payload["message"] = f"Model test failed for '{shown}'."
        payload["diagnostic"] = str(exc)
    if args.json:
        _print_machine_json(payload)
    else:
        print(payload["message"] + ((" " + payload["diagnostic"]) if payload["diagnostic"] else ""))
    return 0 if payload["ok"] else 1


def _start_subscription_control_reader(
    loop: asyncio.AbstractEventLoop,
    start: asyncio.Event,
    cancel: asyncio.Event,
) -> None:
    """Consume the bounded qrenderdoc start/cancel control protocol."""

    def signal(event: asyncio.Event) -> None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(event.set)

    def records(stream: Any):
        try:
            descriptor = int(stream.fileno())
        except (AttributeError, OSError, TypeError, ValueError):
            while True:
                yield stream.readline(4097)

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
                    signal(cancel)
                    return
                value = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
                if len(value) > 4096 or not value.endswith(b"\n"):
                    signal(cancel)
                    return
                command = value.strip()
                if command == b"start" and not start.is_set():
                    signal(start)
                    continue
                if command == b"cancel":
                    signal(cancel)
                    return
                # Unknown commands and duplicate starts fail closed.
                signal(cancel)
                return
        except Exception:
            signal(cancel)

    threading.Thread(
        target=read_control,
        name="replaylab-subscription-control",
        daemon=True,
    ).start()


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
                "Subscription start gate closed before 'start'; no provider process was created."
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
        # Status is deliberately process-free; approval is required before a
        # provider process may be created.
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

    backend = (backend_factory or SubscriptionBackend)(profile, timeout=timeout, emit=emit)
    start = asyncio.Event()
    cancel = asyncio.Event()
    _start_subscription_control_reader(asyncio.get_running_loop(), start, cancel)
    terminal_kind = (
        "subscriptionCompleted"
        if args.subscription_command == "connect"
        else "testCompleted"
    )
    backend.event(
        "waitingForStart",
        "waitingForStart",
        "Waiting for qrenderdoc process containment before starting the provider.",
        messageId="subscription.waiting-for-containment",
        messageArgs=[],
    )
    try:
        try:
            await _wait_for_subscription_start_gate(start, cancel, timeout=timeout)
        except ValueError as exc:
            backend.event(
                terminal_kind,
                "cancelled",
                str(exc),
                messageId="subscription.start-gate-closed",
                messageArgs=[],
                authenticated=False,
                testCompleted=False,
            )
            return 1

        if not backend.detection["installed"]:
            backend.event(
                terminal_kind,
                "missing",
                backend.detection["message"],
                messageId=backend.detection.get("messageId", ""),
                messageArgs=list(backend.detection.get("messageArgs", [])),
                installed=False,
                authenticated=False,
                installHint=backend.detection.get("installHint", ""),
                testCompleted=False,
            )
            return 2
        if cancel.is_set():
            backend.event(
                terminal_kind,
                "cancelled",
                "Subscription operation cancelled before provider startup.",
                messageId="subscription.cancelled-before-start",
                messageArgs=[],
                authenticated=False,
                testCompleted=False,
            )
            return 1

        if args.subscription_command == "connect":
            backend.event(
                "connectStarted",
                "connecting",
                "Provider sign-in started inside the qrenderdoc process job.",
                messageId="subscription.sign-in-started",
                messageArgs=[],
            )
            result = await backend.connect(args.flow, cancel)
            backend.event(
                "testCompleted",
                "ok" if result.authenticated else "failed",
                result.message,
                messageId=getattr(result, "message_id", ""),
                messageArgs=list(getattr(result, "message_args", ())),
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
                messageId=getattr(result, "message_id", ""),
                messageArgs=list(getattr(result, "message_args", ())),
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
                messageId="subscription.test-started",
                messageArgs=[],
            )
            result = await backend.test(cancel)
            backend.event(
                "testCompleted",
                "ok" if result.authenticated else "failed",
                result.message,
                messageId=getattr(result, "message_id", ""),
                messageArgs=list(getattr(result, "message_args", ())),
                authenticated=result.authenticated,
                terminalOpened=result.terminal_opened,
                models=list(result.models),
                account=result.account,
                testCompleted=True,
            )
        else:  # pragma: no cover - argparse constrains this value
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
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()


def _validate_frozen_context(value: dict[str, Any]) -> None:
    if value.get("schema") != "renderdoc-ultra-current-context/v1":
        return
    supplied = value.get("snapshotId")
    if not isinstance(supplied, str) or len(supplied) != 64:
        raise ValueError("frozen context snapshot ID is invalid")
    body = dict(value)
    body.pop("snapshotId", None)
    encoded = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != supplied.lower():
        raise ValueError("frozen context snapshot content hash does not match")


def _read_context(args: argparse.Namespace) -> tuple[dict[str, Any], str, str]:
    if args.context_file and args.live_context:
        raise ValueError("choose either --context-file or --live-context")
    if args.context_file:
        if not args.instance_id:
            raise ValueError("--context-file requires --instance-id for capture-tool binding")
        value = json.loads(args.context_file.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("context file must contain one JSON object")
        _validate_frozen_context(value)
        instance = BridgeClient().select_instance(args.instance_id)
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
            loaded = [item for item in client.instances() if item.capture_loaded]
            if len(loaded) != 1:
                raise ValueError(
                    "--live-context requires exactly one loaded qrenderdoc instance or an "
                    "explicit --instance-id"
                )
            instance = loaded[0]
        value = client.call("get_current_renderdoc_context", instance_id=instance.id)
        if not isinstance(value, dict):
            raise ValueError("qrenderdoc returned an invalid context object")
        return value, instance.id, instance.capture_path
    if args.instance_id:
        raise ValueError("--instance-id requires --context-file or --live-context")
    return {}, "", ""


def _read_prompt(args: argparse.Namespace) -> str:
    prompt = args.prompt
    if args.prompt_stdin:
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        raw = stream.read(NATIVE_PROMPT_WIRE_MAX_BYTES + 1)
        encoded = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        if len(encoded) > NATIVE_PROMPT_WIRE_MAX_BYTES:
            raise ValueError("stdin prompt exceeds the 16 MiB safety limit")
        try:
            prompt = encoded.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("stdin prompt must be valid UTF-8") from exc
    if not prompt or not prompt.strip():
        raise ValueError("prompt cannot be empty")
    return prompt


async def _wait_for_run_cancellation(path: Path) -> None:
    while True:
        try:
            with path.open("rb") as signal:
                payload = signal.read(65)
        except (FileNotFoundError, OSError):
            await asyncio.sleep(0.1)
            continue
        if len(payload) <= 64 and payload.strip().lower() == b"cancel":
            return
        await asyncio.sleep(0.1)


async def _run_once(args: argparse.Namespace, store: ProfileStore) -> int:
    log = configure_logging()
    profile = _selected_profile(store, args.profile)
    profile.validate()
    model = profile.resolve_model(args.model)
    context, instance_id, capture_path = _read_context(args)
    prompt = _read_prompt(args)
    intent: dict[str, Any] | None = None
    if args.intent_kind:
        if not context or not instance_id:
            raise ValueError("--intent-kind requires frozen context and --instance-id")
        expected_skill = INTENT_SKILLS[args.intent_kind]
        if args.skill and args.skill != expected_skill:
            raise ValueError(
                f"--intent-kind {args.intent_kind} requires --skill {expected_skill}"
            )
        intent = {"kind": args.intent_kind, "arguments": {}}

    catalog = _catalog()
    for diagnostic in catalog.diagnostics:
        log.warning("catalog: %s", diagnostic)
    request = RunRequest(
        prompt=prompt,
        profile=profile,
        task_id=args.task_id,
        model=model,
        context=context,
        instance_id=instance_id,
        capture_path=capture_path,
        intent=intent,
        skill_name=args.skill,
        mcp_executable=args.mcp_host,
    )
    config = load_engine_config(args.engine_config)
    engine: Any
    if profile.provider.kind == "model":
        engine = DeepAgentsEngine(catalog=catalog, config=config)
    elif profile.provider.kind == "agent":
        engine = NativeAgentEngine(catalog=catalog, config=config)
    else:  # pragma: no cover - profile validation constrains this value
        raise ConfigurationError(f"unsupported profile kind '{profile.provider.kind}'")

    cancel_path = args.cancel_file
    if cancel_path is None:
        result = await engine.run(request)
    else:
        run_task = asyncio.create_task(engine.run(request))
        cancel_task = asyncio.create_task(_wait_for_run_cancellation(cancel_path))
        try:
            done, _ = await asyncio.wait(
                {run_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_task in done and not run_task.done():
                try:
                    cancel_task.result()
                except Exception as exc:
                    # A local watcher failure is not a cancellation request and
                    # must never abort a provider process on its own.
                    log.warning("run cancellation watcher failed: %s", exc)
                else:
                    run_task.cancel()
            result = await run_task
        finally:
            if not cancel_task.done():
                cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            with contextlib.suppress(OSError):
                cancel_path.unlink(missing_ok=True)
    if args.json:
        _print_machine_json(result.payload())
    else:
        print(result.text)
    return 1 if result.terminal_status == "failed" else 0


_SAFE_TERMINAL_FAILURES = {
    "invalid-request": "The request or local AI configuration is invalid.",
    "local-io-failure": "The local AI Host could not access a required resource.",
    "ai-operation-failed": "The AI operation could not be completed.",
    "internal-host-failure": "The local AI Host failed unexpectedly.",
}


def _print_utf8_error(value: str) -> None:
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
    else:
        sys.stderr.write(encoded.decode("utf-8"))
        sys.stderr.flush()


def _terminal_error_category(exc: BaseException) -> str:
    if isinstance(exc, (KeyError, ValueError, json.JSONDecodeError, ConfigurationError)):
        return "invalid-request"
    if isinstance(exc, OSError):
        return "local-io-failure"
    if isinstance(exc, ReplayLabAIError):
        return "ai-operation-failed"
    return "internal-host-failure"


def _print_safe_terminal_failure(exc: BaseException) -> None:
    category = _terminal_error_category(exc)
    _print_utf8_error(
        f"replaylab-ai: error[{category}]: {_SAFE_TERMINAL_FAILURES[category]} "
        "Check RenderDoc's diagnostic log for technical details."
    )


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(AttributeError, OSError, ValueError):
                reconfigure(encoding="utf-8", errors="strict")


def _consume_native_start_gate(args: argparse.Namespace) -> None:
    setting = os.environ.pop(_NATIVE_START_GATE_ENV, None)
    if setting is None or bool(getattr(args, "start_gated", False)):
        return
    if setting != "1":
        raise ValueError("native start gate setting is invalid")
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    record = stream.readline(len(_NATIVE_START_GATE_RECORD) + 1)
    encoded = record.encode("utf-8") if isinstance(record, str) else bytes(record)
    if encoded != _NATIVE_START_GATE_RECORD:
        raise ValueError("native start gate closed before the exact start record")


def _doctor_command(args: argparse.Namespace, store: ProfileStore) -> int:
    profile = _selected_profile(store, args.profile)
    checks = run_doctor(profile, _catalog(), mcp_executable=args.mcp_host)
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


def main(argv: Sequence[str] | None = None) -> int:
    try:
        _configure_utf8_stdio()
        args = _parser().parse_args(argv)
        _consume_native_start_gate(args)
        if args.command == "profile" and args.profile_command == "presets":
            return _print_presets(args.json)

        store = _load_store(args.config)
        if args.command == "profile":
            if args.profile_command == "credential":
                return _credential_command(args, store)
            if args.profile_command == "subscription":
                return asyncio.run(_subscription_command(args, store))
            if args.profile_command == "discover-models":
                return asyncio.run(_models_command(args, store))
            if args.profile_command == "test-model":
                return asyncio.run(_test_model_command(args, store))
            from .credential_store import default_credential_store

            return _profile_command(args, store, default_credential_store())
        if args.command == "doctor":
            return _doctor_command(args, store)
        if args.command == "run":
            return asyncio.run(_run_once(args, store))
        raise AssertionError(args.command)
    except SystemExit as exc:
        if type(exc.code) is int:
            return exc.code
        _print_safe_terminal_failure(exc)
        return 2
    except (KeyboardInterrupt, asyncio.CancelledError):
        with contextlib.suppress(Exception):
            _print_utf8_error("replaylab-ai: cancelled")
        return 130
    except Exception as exc:
        with contextlib.suppress(Exception):
            log_exception(configure_logging(), "command failed", exc)
        with contextlib.suppress(Exception):
            _print_safe_terminal_failure(exc)
        return 2
    except BaseException as exc:
        with contextlib.suppress(Exception):
            log_exception(configure_logging(), "command failed", exc)
        with contextlib.suppress(Exception):
            _print_safe_terminal_failure(exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
