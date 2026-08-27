from __future__ import annotations

import asyncio
from io import BytesIO
import pytest
from types import SimpleNamespace

from replaylab_agent.cli import _parser, _profile_command
from replaylab_agent.process_env import codex_profile_home
from replaylab_agent.profiles import Profile, ProfileStore


def test_profile_machine_commands_manage_picker_models(tmp_path, monkeypatch) -> None:
    import replaylab_agent.cli as cli

    store = ProfileStore(tmp_path / "profiles.json")
    emitted: list[object] = []
    monkeypatch.setattr(cli, "_print_machine_json", emitted.append)
    parser = _parser()

    add = parser.parse_args(
        [
            "profile",
            "add",
            "local-main",
            "--preset",
            "ollama",
            "--title",
            "My Local AI",
            "--allowed-model",
            "model-a",
            "--allowed-model",
            "model-b",
            "--default-model",
            "model-a",
            "--select",
            "--json",
        ]
    )
    assert _profile_command(add, store) == 0
    assert emitted[-1]["id"] == "local-main"
    assert emitted[-1]["title"] == "My Local AI"
    assert emitted[-1]["models"] == ["model-a", "model-b"]
    assert emitted[-1]["defaultModel"] == "model-a"

    update = parser.parse_args(
        [
            "profile",
            "update",
            "local-main",
            "--default-model",
            "model-b",
            "--json",
        ]
    )
    assert _profile_command(update, store) == 0
    assert emitted[-1]["model"] == "model-b"
    assert emitted[-1]["defaultModel"] == "model-b"
    reloaded = ProfileStore(store.path).load().active()
    assert reloaded.default_model == "model-b"
    assert reloaded.model == "model-b"
    assert reloaded.resolve_model() == "model-b"

    remove = parser.parse_args(
        ["profile", "remove", "local-main", "--json"]
    )
    assert _profile_command(remove, store) == 0
    assert emitted[-1] == {"removed": "local-main", "activeProfile": ""}


def test_parser_exposes_secret_safe_model_discovery_and_auth_commands() -> None:
    parser = _parser()
    models = parser.parse_args(
        [
            "models",
            "--preset",
            "custom-openai",
            "--base-url",
            "https://example.test/v1/chat/completions",
            "--api-key-stdin",
            "--json",
        ]
    )
    assert models.command == "models"
    assert models.api_key_stdin is True
    assert not hasattr(models, "api_key")

    login = parser.parse_args(
        [
            "auth",
            "login",
            "--profile",
            "codex",
            "--flow",
            "device",
            "--start-gated",
            "--json",
        ]
    )
    assert login.auth_command == "login"
    assert login.flow == "device"
    assert login.start_gated is True


def test_profile_machine_commands_round_trip_subscription_auth_flow(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.cli as cli

    store = ProfileStore(tmp_path / "profiles.json")
    emitted: list[dict] = []
    monkeypatch.setattr(cli, "_print_machine_json", emitted.append)
    parser = _parser()

    add = parser.parse_args(
        [
            "profile",
            "add",
            "codex-device",
            "--preset",
            "codex",
            "--option",
            "subscription_auth_flow=device",
            "--json",
        ]
    )
    assert _profile_command(add, store) == 0
    assert emitted[-1]["options"]["subscription_auth_flow"] == "device"

    reloaded = ProfileStore(store.path).load().profiles["codex-device"]
    assert reloaded.options["subscription_auth_flow"] == "device"

    update = parser.parse_args(
        [
            "profile",
            "update",
            "codex-device",
            "--option",
            "subscription_auth_flow=browser",
            "--json",
        ]
    )
    assert _profile_command(update, store) == 0
    assert emitted[-1]["options"]["subscription_auth_flow"] == "browser"
    assert ProfileStore(store.path).load().profiles["codex-device"].options[
        "subscription_auth_flow"
    ] == "browser"


def test_provider_replacement_clears_custom_acp_state_but_preserves_identity(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.cli as cli

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "stable-id",
            "custom-acp",
            title="My Subscription",
            command=["old-agent", "--acp"],
            agent_env=["OLD_AGENT_TOKEN"],
            options={"allow_unsandboxed_agent": True},
            probed_capabilities=["chat"],
        )
    )
    emitted: list[dict] = []
    monkeypatch.setattr(cli, "_print_machine_json", emitted.append)
    args = _parser().parse_args(
        [
            "profile",
            "update",
            "stable-id",
            "--replace-provider",
            "--preset",
            "codex",
            "--json",
        ]
    )

    assert _profile_command(args, store) == 0
    updated = store.active()
    assert updated.id == "stable-id"
    assert updated.title == "My Subscription"
    assert updated.preset == "codex"
    assert updated.command == []
    assert updated.argv == ["codex", "app-server"]
    assert updated.agent_env == []
    assert updated.options == {}
    assert updated.probed_capabilities is None
    assert updated.models == []
    assert updated.default_model == ""
    assert emitted[-1]["active"] is True


def test_codex_profile_remove_clears_only_profile_scoped_home(
    tmp_path, monkeypatch
) -> None:
    local = (tmp_path / "local").resolve()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    store = ProfileStore(tmp_path / "profiles.json")
    profile = Profile("codex-remove", "codex")
    store.add(profile)
    store.save()
    home = codex_profile_home(profile)
    home.mkdir(parents=True)
    (home / "auth.json").write_text("test-only", encoding="utf-8")

    args = _parser().parse_args(["profile", "remove", profile.id, "--json"])
    assert _profile_command(args, store) == 0
    assert not home.exists()
    assert local.is_dir()


def test_replacing_codex_provider_clears_profile_scoped_home(
    tmp_path, monkeypatch
) -> None:
    local = (tmp_path / "local").resolve()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    store = ProfileStore(tmp_path / "profiles.json")
    profile = Profile("codex-replace", "codex", title="Keep title")
    store.add(profile)
    store.save()
    home = codex_profile_home(profile)
    home.mkdir(parents=True)
    (home / "auth.json").write_text("test-only", encoding="utf-8")

    args = _parser().parse_args(
        [
            "profile",
            "update",
            profile.id,
            "--replace-provider",
            "--preset",
            "ollama",
            "--allowed-model",
            "test-model",
            "--default-model",
            "test-model",
            "--json",
        ]
    )
    assert _profile_command(args, store) == 0
    assert store.profiles[profile.id].preset == "ollama"
    assert store.profiles[profile.id].title == "Keep title"
    assert not home.exists()


def test_api_provider_replacement_clears_old_endpoint_key_options_and_models(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.cli as cli

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "stable-api",
            "openai",
            model="old-model",
            models=["old-model", "other-old-model"],
            default_model="old-model",
            title="Production API",
            base_url="https://old.example.test/v1",
            api_key_env="OLD_VENDOR_API_KEY",
            options={"temperature": 0.5, "supports_tools": True},
            probed_capabilities=["tools"],
        )
    )
    monkeypatch.setattr(cli, "_print_machine_json", lambda value: None)
    args = _parser().parse_args(
        [
            "profile",
            "update",
            "stable-api",
            "--replace-provider",
            "--preset",
            "anthropic",
            "--allowed-model",
            "claude-new",
            "--default-model",
            "claude-new",
            "--json",
        ]
    )

    assert _profile_command(args, store) == 0
    updated = store.active()
    assert updated.id == "stable-api"
    assert updated.title == "Production API"
    assert updated.preset == "anthropic"
    assert updated.endpoint == "https://api.anthropic.com"
    assert updated.key_env == "ANTHROPIC_API_KEY"
    assert updated.models == ["claude-new"]
    assert updated.default_model == "claude-new"
    assert updated.options == {}
    assert updated.probed_capabilities is None
    encoded = store.path.read_text(encoding="utf-8")
    assert "old.example.test" not in encoded
    assert "OLD_VENDOR_API_KEY" not in encoded
    assert "old-model" not in encoded


def test_cross_preset_update_requires_explicit_replacement(tmp_path) -> None:
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(Profile("stable", "ollama", "old-model"))
    args = _parser().parse_args(
        [
            "profile",
            "update",
            "stable",
            "--preset",
            "openai",
            "--allowed-model",
            "new-model",
            "--default-model",
            "new-model",
        ]
    )
    with pytest.raises(ValueError, match="requires --replace-provider"):
        _profile_command(args, store)
    assert store.active().preset == "ollama"


def test_parser_exposes_per_run_model_override() -> None:
    parser = _parser()
    run = parser.parse_args(
        ["run", "--profile", "p", "--model", "m", "--prompt", "hello"]
    )
    worker = parser.parse_args(["worker", "--profile", "p", "--model", "m"])
    assert run.model == "m"
    assert worker.model == "m"


@pytest.mark.asyncio
async def test_codex_login_jsonl_uses_uniform_url_and_code_fields(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.cli as cli

    class FakeConnector:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def login_start(self, *, flow):
            assert flow == "device"
            return {
                "type": "chatgptDeviceCode",
                "loginId": "login-id",
                "verificationUrl": "https://auth.openai.com/codex/device",
                "userCode": "ABCD-1234",
            }

        async def wait_login(self, login_id, *, timeout):
            assert login_id == "login-id"
            return {"loginId": login_id, "success": True, "error": None}

        async def account_read(self):
            return {"account": {"type": "chatgpt", "planType": "plus"}}

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(Profile("codex-test", "codex"))
    emitted: list[dict] = []
    monkeypatch.setattr(cli, "_codex_connector", lambda *args, **kwargs: FakeConnector())
    monkeypatch.setattr(cli, "_print_machine_json", emitted.append)
    monkeypatch.setattr(
        cli,
        "_start_login_control_reader",
        lambda loop, start, cancel, *, start_gated: loop.call_soon(start.set),
    )
    args = _parser().parse_args(
        [
            "auth",
            "login",
            "--profile",
            "codex-test",
            "--flow",
            "device",
            "--timeout",
            "1",
            "--start-gated",
            "--json",
        ]
    )

    assert await cli._auth_command(args, store) == 0
    assert emitted[0]["event"] == "loginStarted"
    assert emitted[0]["url"] == "https://auth.openai.com/codex/device"
    assert emitted[0]["code"] == "ABCD-1234"
    assert emitted[1]["event"] == "loginCompleted"


@pytest.mark.asyncio
async def test_codex_login_gate_fails_before_connector_on_cancel(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.cli as cli

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(Profile("codex-test", "codex"))
    created = False

    def connector_factory(*args, **kwargs):
        nonlocal created
        created = True
        raise AssertionError("connector must not be created before the gate")

    def cancel_gate(loop, start, cancel, *, start_gated):
        assert start_gated is True
        loop.call_soon(cancel.set)

    monkeypatch.setattr(cli, "_codex_connector", connector_factory)
    monkeypatch.setattr(cli, "_start_login_control_reader", cancel_gate)
    args = _parser().parse_args(
        [
            "auth",
            "login",
            "--profile",
            "codex-test",
            "--start-gated",
            "--timeout",
            "1",
            "--json",
        ]
    )
    with pytest.raises(ValueError, match="app-server was not created"):
        await cli._auth_command(args, store)
    assert created is False


@pytest.mark.asyncio
async def test_codex_login_gate_treats_stdin_eof_as_cancel(monkeypatch) -> None:
    import replaylab_agent.cli as cli

    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=BytesIO(b"")))
    start = asyncio.Event()
    cancel = asyncio.Event()
    cli._start_login_control_reader(
        asyncio.get_running_loop(), start, cancel, start_gated=True
    )
    await asyncio.wait_for(cancel.wait(), timeout=1)
    assert start.is_set() is False


@pytest.mark.asyncio
async def test_run_forwards_model_override_only_for_this_run(tmp_path, monkeypatch) -> None:
    import replaylab_agent.cli as cli

    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "local",
            "ollama",
            model="model-a",
            models=["model-a", "model-b"],
            default_model="model-a",
        )
    )

    class FakeRuntime:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, prompt, **kwargs):
            assert prompt == "hello"
            assert kwargs["model_override"] == "model-b"
            return SimpleNamespace(
                task_id="task",
                profile="local",
                executor="RenderDoc Agent",
                text="ok",
                plan=None,
            )

    monkeypatch.setattr(cli, "AgentRuntime", FakeRuntime)
    args = SimpleNamespace(
        mcp_host=None,
        context_file=None,
        live_context=False,
        instance_id="",
        prompt="hello",
        prompt_stdin=False,
        profile="local",
        model="model-b",
        skill="",
        task_id="task",
        json=False,
    )
    assert await cli._run_once(args, store) == 0
    assert store.active().model == "model-a"


@pytest.mark.asyncio
async def test_work_once_and_worker_forward_model_override(tmp_path, monkeypatch) -> None:
    import replaylab_agent.cli as cli

    seen: list[dict] = []

    class FakeRuntime:
        def __init__(self, *args, **kwargs):
            pass

        async def process_one_intent(self, **kwargs):
            seen.append(kwargs)
            return None

    monkeypatch.setattr(cli, "AgentRuntime", FakeRuntime)
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(Profile("local", "ollama", "model-a"))
    once = SimpleNamespace(
        mcp_host=None,
        profile="local",
        model="model-b",
        instance_id="instance",
        json=True,
    )
    assert await cli._work_once(once, store) == 3
    worker = SimpleNamespace(
        mcp_host=None,
        profile="local",
        model="model-b",
        instance_id="instance",
        poll_seconds=0.1,
        idle_exit_seconds=1e-12,
    )
    assert await cli._worker(worker, store) == 0
    assert len(seen) >= 2
    assert all(
        item
        == {
            "profile_id": "local",
            "instance_id": "instance",
            "model_override": "model-b",
        }
        for item in seen
    )
