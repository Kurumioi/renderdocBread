from __future__ import annotations

import json

import pytest

from replaylab_agent.errors import ConfigurationError
from replaylab_agent.profiles import PROFILE_SCHEMA, Profile, ProfileStore


def test_profile_store_persists_only_environment_reference_and_switches_active(
    tmp_path, monkeypatch
) -> None:
    secret = "sk-test-this-must-never-reach-disk"
    monkeypatch.setenv("REPLAYLAB_TEST_OPENAI_KEY", secret)
    path = tmp_path / "profiles.json"
    store = ProfileStore(path)
    store.add(
        Profile(
            id="openai-main",
            preset="openai",
            model="gpt-test",
            api_key_env="REPLAYLAB_TEST_OPENAI_KEY",
        )
    )
    store.add(Profile(id="local", preset="ollama", model="local-test"))
    store.select("local")
    store.save()

    encoded = path.read_text(encoding="utf-8")
    payload = json.loads(encoded)
    assert payload["schema"] == PROFILE_SCHEMA
    assert payload["activeProfile"] == "local"
    assert secret not in encoded
    assert "REPLAYLAB_TEST_OPENAI_KEY" in encoded

    loaded = ProfileStore(path).load()
    assert loaded.active().id == "local"
    loaded.select("openai-main")
    assert loaded.active().resolve_api_key() == secret
    loaded.save()
    assert ProfileStore(path).load().active().id == "openai-main"


def test_raw_secret_field_is_not_a_supported_profile_field() -> None:
    with pytest.raises(ConfigurationError, match="unknown profile fields"):
        Profile.from_dict(
            {
                "id": "unsafe",
                "preset": "openai",
                "model": "gpt-test",
                "api_key_env": "OPENAI_API_KEY",
                "api_key": "raw-secret",
            }
        )


def test_secret_like_arbitrary_options_are_rejected() -> None:
    with pytest.raises(ConfigurationError, match="unknown or unsafe profile options"):
        Profile(
            id="unsafe-option",
            preset="openai",
            model="gpt-test",
            options={"api_key": "raw-secret"},
        ).validate()


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:secret@example.test/v1",
        "https://example.test/v1?api_key=secret",
        "https://example.test/v1#secret",
    ],
)
def test_endpoint_cannot_persist_credentials(base_url: str) -> None:
    with pytest.raises(ConfigurationError, match="must not contain credentials"):
        Profile(
            id="unsafe-endpoint",
            preset="custom-openai",
            model="model",
            base_url=base_url,
        ).validate()


def test_api_key_environment_variable_name_is_validated() -> None:
    with pytest.raises(ConfigurationError, match="environment variable name"):
        Profile(
            id="unsafe-env",
            preset="openai",
            model="model",
            api_key_env="NOT A VARIABLE",
        ).validate()


@pytest.mark.parametrize(
    "name",
    [
        "PATH",
        "home",
        "CODEX_HOME",
        "LOCALAPPDATA",
        "PYTHONPATH",
        "HTTPS_PROXY",
        "REPLAYLAB_INSTANCE_ID",
    ],
)
def test_api_key_environment_cannot_reuse_process_or_replaylab_control_names(name) -> None:
    with pytest.raises(ConfigurationError, match="must not reuse"):
        Profile(
            id="unsafe-env-role",
            preset="openai",
            model="model",
            api_key_env=name,
        ).validate()


@pytest.mark.parametrize(
    "secret_argument",
    [
        "--api-key=raw-secret",
        "--token",
        "--client-secret:raw-secret",
        "--credential=raw-secret",
        "--refresh_token",
        "--github-token=raw-secret",
        "--personal-access-token",
        "--access-key:raw-secret",
        "--private-key=raw-secret",
        "--clientSecret=raw-secret",
        "--accessToken=raw-secret",
        "--refreshToken=raw-secret",
        "--privateKey=raw-secret",
        "--bearer=raw-secret",
        "--header",
        "-H",
        "GITHUB_TOKEN=raw-secret",
        "Authorization: Bearer raw-secret",
        "/password:raw-secret",
    ],
)
def test_external_agent_command_rejects_persisted_secret_flags(secret_argument) -> None:
    with pytest.raises(ConfigurationError, match="not persisted command arguments"):
        Profile(
            id="unsafe-agent",
            preset="custom-acp",
            command=["agent", secret_argument],
        ).validate()


def test_posix_absolute_path_named_token_is_not_mistaken_for_a_slash_option() -> None:
    Profile(
        id="absolute-path-agent",
        preset="custom-acp",
        command=["agent", "/token"],
    ).validate()


@pytest.mark.parametrize(
    "benign_argument",
    ["--auth-mode=browser", "--token-budget=4096", "--header-size=8192"],
)
def test_non_secret_custom_agent_tuning_flags_remain_compatible(benign_argument) -> None:
    Profile(
        id="benign-agent-option",
        preset="custom-acp",
        command=["agent", benign_argument],
    ).validate()


def test_remote_plaintext_endpoint_is_rejected_but_loopback_is_allowed() -> None:
    with pytest.raises(ConfigurationError, match="must use https"):
        Profile(
            id="remote-http",
            preset="custom-openai",
            model="model",
            base_url="http://api.example.test/v1",
        ).validate()
    Profile(
        id="local-http",
        preset="custom-openai",
        model="model",
        base_url="http://127.0.0.1:8080/v1",
    ).validate()


def test_azure_v1_omits_api_version_and_legacy_requires_it() -> None:
    Profile(
        id="azure-v1",
        preset="azure-openai",
        model="deployment",
        base_url="https://resource.openai.azure.com/openai/v1",
    ).validate()
    with pytest.raises(ConfigurationError, match="must omit"):
        Profile(
            id="azure-v1-versioned",
            preset="azure-openai",
            model="deployment",
            base_url="https://resource.openai.azure.com/openai/v1",
            options={"api_version": "2025-04-01-preview"},
        ).validate()


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"id": 1, "preset": "ollama", "model": "m"}, "must be strings"),
        (
            {"id": "x", "preset": "ollama", "model": "m", "options": None},
            "options must be an object",
        ),
        (
            {"id": "x", "preset": "custom-acp", "command": "agent --acp"},
            "argv array",
        ),
    ],
)
def test_profile_json_types_fail_as_configuration_errors(payload, message) -> None:
    with pytest.raises(ConfigurationError, match=message):
        Profile.from_dict(payload)


def test_profile_numeric_options_are_finite_and_bounded() -> None:
    with pytest.raises(ConfigurationError, match="finite"):
        Profile(
            id="bad-timeout",
            preset="ollama",
            model="model",
            options={"timeout_seconds": float("nan")},
        ).validate()

    Profile(
        id="bounded-agent-stages",
        preset="codex",
        options={
            "planner_timeout_seconds": 60,
            "specialist_timeout_seconds": 300,
            "chat_timeout_seconds": 90,
        },
    ).validate()
    with pytest.raises(ConfigurationError, match="specialist_timeout_seconds"):
        Profile(
            id="bad-specialist-timeout",
            preset="codex",
            options={"specialist_timeout_seconds": 29},
        ).validate()


def test_model_settings_are_mapped_instead_of_silently_ignored() -> None:
    from replaylab_agent.runtime import AgentRuntime

    profile = Profile(
        id="settings",
        preset="openai",
        model="model",
        options={
            "max_output_tokens": 512,
            "temperature": 0.2,
            "timeout_seconds": 30,
            "reasoning_effort": "medium",
        },
    )
    profile.validate()
    assert AgentRuntime._model_settings(profile) == {
        "max_tokens": 512,
        "temperature": 0.2,
        "timeout": 30.0,
        "openai_reasoning_effort": "medium",
    }
    with pytest.raises(ConfigurationError, match="requires options.api_version"):
        Profile(
            id="azure-legacy",
            preset="azure-openai",
            model="deployment",
            base_url="https://resource.openai.azure.com",
        ).validate()


def test_unsandboxed_acp_trust_gate_is_explicit_and_boolean() -> None:
    Profile(
        id="trusted-acp",
        preset="custom-acp",
        command=["trusted-agent", "--acp"],
        options={"allow_unsandboxed_agent": True},
    ).validate()
    with pytest.raises(ConfigurationError, match="must be a boolean"):
        Profile(
            id="bad-trust-gate",
            preset="custom-acp",
            command=["agent", "--acp"],
            options={"allow_unsandboxed_agent": "yes"},
        ).validate()


def test_subscription_auth_flow_is_agent_only_and_bounded() -> None:
    Profile(
        id="codex-device",
        preset="codex",
        options={"subscription_auth_flow": "device"},
    ).validate()
    with pytest.raises(ConfigurationError, match="must be 'browser' or 'device'"):
        Profile(
            id="bad-codex-flow",
            preset="codex",
            options={"subscription_auth_flow": "popup"},
        ).validate()
    with pytest.raises(ConfigurationError, match="Agent subscription profiles"):
        Profile(
            id="bad-model-flow",
            preset="openai",
            model="gpt-test",
            options={"subscription_auth_flow": "browser"},
        ).validate()


def test_renderdoc_agent_tool_capability_fails_early_and_can_be_explicitly_enabled() -> None:
    from replaylab_agent.runtime import AgentRuntime

    for chat_only in (
        Profile("local", "ollama", "model"),
        Profile("openai-unconfirmed", "openai", "some-model"),
    ):
        chat_only.validate()
        with pytest.raises(ConfigurationError, match="explicitly confirmed tool support"):
            AgentRuntime._require_agent_capabilities(chat_only)
        assert "tools" not in chat_only.capabilities
        assert "json-schema" not in chat_only.capabilities
        assert "vision" not in chat_only.capabilities

    enabled = Profile(
        "local-tools",
        "ollama",
        "model",
        options={"supports_tools": True},
    )
    enabled.validate()
    AgentRuntime._require_agent_capabilities(enabled)
    assert "tools" in enabled.capabilities


def test_managed_agent_presets_keep_their_protocol_tool_contract() -> None:
    codex = Profile("codex", "codex", "agent")
    codex.validate()
    assert "tools" in codex.capabilities


def test_v1_profile_is_migrated_to_enabled_and_default_models(tmp_path) -> None:
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "schema": "replaylab-ai-profiles/v1",
                "activeProfile": "legacy",
                "profiles": [
                    {
                        "id": "legacy",
                        "preset": "ollama",
                        "model": "legacy-model",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = ProfileStore(path).load()
    assert store.active().models == ["legacy-model"]
    assert store.active().default_model == "legacy-model"
    store.save()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema"] == PROFILE_SCHEMA
    assert saved["profiles"][0]["models"] == ["legacy-model"]
    assert saved["profiles"][0]["default_model"] == "legacy-model"


def test_profile_resolves_only_enabled_per_run_models() -> None:
    profile = Profile(
        "picker",
        "ollama",
        model="model-a",
        models=["model-a", "model-b", "model-a"],
        default_model="model-b",
    )
    profile.validate()
    assert profile.models == ["model-a", "model-b"]
    assert profile.resolve_model("model-b") == "model-b"
    with pytest.raises(ConfigurationError, match="not enabled"):
        profile.resolve_model("model-c")


def test_default_model_must_be_enabled() -> None:
    with pytest.raises(ConfigurationError, match="default_model must be present"):
        Profile(
            "bad-picker",
            "ollama",
            model="model-a",
            models=["model-a"],
            default_model="model-b",
        ).validate()


@pytest.mark.parametrize(
    "entered, expected",
    [
        (
            "https://example.test/v1/chat/completions",
            "https://example.test/v1",
        ),
        ("https://example.test/responses", "https://example.test"),
        ("https://example.test/v1/models/", "https://example.test/v1"),
    ],
)
def test_openai_compatible_profile_normalizes_pasted_operation_url(
    entered, expected
) -> None:
    profile = Profile(
        "normalized",
        "custom-openai",
        "model",
        base_url=entered,
    )
    profile.validate()
    assert profile.endpoint == expected


@pytest.mark.parametrize("title", ["x" * 129, "unsafe\nname", "unsafe\0name"])
def test_profile_display_name_is_bounded_and_single_line(title) -> None:
    with pytest.raises(ConfigurationError, match="profile title"):
        Profile("bounded-name", "ollama", "model", title=title).validate()


def test_profile_base_url_is_bounded() -> None:
    with pytest.raises(ConfigurationError, match="at most 4096"):
        Profile(
            "bounded-url",
            "custom-openai",
            "model",
            base_url="https://example.test/" + "x" * 4096,
        ).validate()
