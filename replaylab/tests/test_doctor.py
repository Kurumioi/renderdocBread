from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

from replaylab_agent.doctor import _is_packaged_windows_executable, run_doctor
from replaylab_agent.profiles import Profile
from replaylab_agent.skills import SkillCatalog


def test_windowsapps_executable_is_not_reported_as_launchable() -> None:
    path = r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0_x64\codex.exe"
    assert _is_packaged_windows_executable(path) is (os.name == "nt")


def test_doctor_checks_current_default_and_every_enabled_model() -> None:
    profile = Profile(
        "multi-context",
        "codex",
        model="current-model",
        models=["default-model", "current-model", "third-model"],
        default_model="default-model",
        model_context_windows={
            "current-model": 65_536,
            "default-model": 131_072,
            "third-model": 262_144,
        },
    )

    checks = run_doctor(
        profile,
        SimpleNamespace(skills={"one": object()}),
        bridge=SimpleNamespace(instances=lambda: []),
        mcp_executable=Path(sys.executable),
        environ={"PATH": ""},
    )
    contexts = [check for check in checks if check.name.startswith("model-context")]

    assert [(check.status, check.detail) for check in contexts] == [
        ("ok", "65536 tokens (profile)"),
        ("ok", "default model: 131072 tokens (profile)"),
        ("ok", "enabled model 3: 262144 tokens (profile)"),
    ]
    rendered = "\n".join(check.detail for check in contexts)
    assert "current-model" not in rendered
    assert "default-model" not in rendered
    assert "third-model" not in rendered


def test_doctor_accepts_agent_provider_default_context() -> None:
    profile = Profile("provider-default", "codex")

    checks = run_doctor(
        profile,
        SimpleNamespace(skills={}),
        bridge=SimpleNamespace(instances=lambda: []),
        mcp_executable=Path(sys.executable),
        environ={"PATH": ""},
    )
    context = next(check for check in checks if check.name == "model-context")

    assert context.status == "ok"
    assert context.detail == "200000 tokens (provider-managed-fallback)"


def test_doctor_does_not_echo_available_api_key(monkeypatch) -> None:
    secret = "sk-doctor-must-not-echo-this-value"
    monkeypatch.setenv("REPLAYLAB_TEST_DOCTOR_KEY", secret)
    profile = Profile(
        "secret-safe",
        "openai",
        "private-model",
        api_key_env="REPLAYLAB_TEST_DOCTOR_KEY",
        model_context_windows={"private-model": 65_536},
    )

    checks = run_doctor(
        profile,
        SimpleNamespace(skills={}),
        bridge=SimpleNamespace(instances=lambda: []),
        mcp_executable=Path(sys.executable),
    )

    assert secret not in "\n".join(check.detail for check in checks)


def test_doctor_reports_safe_bounded_skill_quarantine_summary(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    valid = root / "valid-skill"
    valid.mkdir(parents=True)
    (valid / "SKILL.md").write_text(
        "---\nname: valid-skill\ndescription: Valid procedure.\n---\n\n"
        "Use verified evidence.\n",
        encoding="utf-8",
    )
    broken = root / "broken-skill"
    broken.mkdir(parents=True)
    secret = "UNTRUSTED-DOCTOR-CONTENT"
    (broken / "SKILL.md").write_text(secret, encoding="utf-8")
    catalog = SkillCatalog([root]).load_runtime()

    checks = run_doctor(
        Profile("provider-default", "codex"),
        catalog,
        bridge=SimpleNamespace(instances=lambda: []),
        include_external_mcp=False,
        environ={"PATH": ""},
    )

    check = next(item for item in checks if item.name == "skills-quarantine")
    assert check.status == "warning"
    assert check.detail == (
        "1 packaged skill definition(s) quarantined; "
        "bounded codes: skill-definition-invalid:1"
    )
    rendered = "\n".join(item.detail for item in checks)
    assert secret not in rendered
    assert str(tmp_path) not in rendered
