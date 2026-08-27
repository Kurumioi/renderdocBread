from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from replaylab_agent import selftest
from replaylab_agent.selftest import SelfTestItem, run_protocol_self_test


EXPECTED_NAMES = [
    "api.openai-compatible",
    "api.anthropic-compatible",
    "api.google-genai",
    "subscription.codex-app-server",
    "subscription.acp",
]


@pytest.mark.asyncio
async def test_protocol_self_test_has_fixed_schema_and_exercises_fake_peers() -> None:
    items = await run_protocol_self_test(timeout=5.0)
    payload = [item.to_wire() for item in items]

    assert [item["name"] for item in payload] == EXPECTED_NAMES
    assert all(set(item) == {"name", "status", "detail"} for item in payload)
    assert [item["status"] for item in payload[:4]] == ["ok"] * 4
    assert payload[4]["status"] in {"ok", "warning"}
    assert all(item["detail"] for item in payload)

    # Returning OK here means the production connectors parsed both Codex login
    # flows/model list and, when installed, the ACP initialize/session handshake.
    assert "browser/device login URLs" in payload[3]["detail"]
    if importlib.util.find_spec("acp") is None:
        assert "skipped" in payload[4]["detail"]
    else:
        assert "session/new handshake" in payload[4]["detail"]


@pytest.mark.asyncio
async def test_missing_optional_acp_runtime_is_an_explicit_warning(monkeypatch) -> None:
    monkeypatch.setattr(selftest.importlib.util, "find_spec", lambda name: None)
    item = await selftest._run_item(  # noqa: SLF001 - protocol failure contract
        "subscription.acp", lambda: selftest._check_acp(1.0)
    )
    assert item.to_wire() == {
        "name": "subscription.acp",
        "status": "warning",
        "detail": "optional agent-client-protocol runtime is not installed; ACP handshake skipped",
    }


@pytest.mark.asyncio
async def test_self_test_failure_redacts_its_synthetic_credential() -> None:
    async def fail() -> str:
        raise RuntimeError(f"request exposed {_self_test_token()}")

    item = await selftest._run_item(  # noqa: SLF001 - redaction contract
        "api.openai-compatible", fail
    )
    assert item.status == "error"
    assert _self_test_token() not in item.detail
    assert "<self-test-token>" in item.detail


def _self_test_token() -> str:
    return selftest._SELF_TEST_TOKEN  # noqa: SLF001 - verify exact secret redaction


@pytest.mark.parametrize("config_state", ["missing", "malformed"])
def test_cli_self_test_never_loads_profiles(
    tmp_path: Path, monkeypatch, capsys, config_state: str
) -> None:
    import replaylab_agent.cli as cli

    config = tmp_path / "profiles.json"
    if config_state == "malformed":
        config.write_text("{ definitely not profile JSON", encoding="utf-8")

    def forbidden_store_load(path):
        raise AssertionError(f"self-test tried to load profiles: {path}")

    async def fixed_result(*, timeout: float):
        assert timeout == 3.0
        return [SelfTestItem(name, "ok", "offline") for name in EXPECTED_NAMES]

    monkeypatch.setattr(cli, "_load_store", forbidden_store_load)
    monkeypatch.setattr(selftest, "run_protocol_self_test", fixed_result)

    assert cli.main(
        ["--config", str(config), "self-test", "--timeout", "3", "--json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["name"] for item in payload] == EXPECTED_NAMES
    assert all(set(item) == {"name", "status", "detail"} for item in payload)


def test_self_test_child_environment_excludes_accounts_and_credentials(monkeypatch) -> None:
    monkeypatch.setenv("HOME", "forbidden-home")
    monkeypatch.setenv("CODEX_HOME", "forbidden-codex-home")
    monkeypatch.setenv("OPENAI_API_KEY", "forbidden-openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "forbidden-anthropic-key")
    monkeypatch.setenv("PYTHONSTARTUP", "forbidden-startup")

    environment = selftest._child_environment()  # noqa: SLF001 - isolation contract

    assert "HOME" not in environment
    assert "CODEX_HOME" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "ANTHROPIC_API_KEY" not in environment
    assert "PYTHONSTARTUP" not in environment
    assert environment["PYTHONPATH"] == str(
        Path(selftest.__file__).resolve().parents[1]
    )
