from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from replaylab_agent.connectors.models import build_model
from replaylab_agent.profiles import Profile


def _host_env(tmp_path: Path, name: str) -> dict[str, str]:
    return {
        "REPLAYLAB_INSTANCE_ID": "test-qrenderdoc-instance",
        "RENDERDOC_ULTRA_SHADER_LIVE_EDIT_REGISTRY": str(
            (tmp_path / f"{name}-registry.json").resolve()
        ),
    }


def test_supported_model_adapters_construct_without_network(monkeypatch) -> None:
    pytest.importorskip("pydantic_ai")
    keys = {
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "AZURE_OPENAI_API_KEY",
    }
    for key in keys:
        monkeypatch.setenv(key, "test-only-not-a-real-key")
    profiles = [
        Profile("openai-test", "openai", "gpt-test"),
        Profile("compatible-test", "deepseek", "deepseek-chat"),
        Profile("anthropic-test", "anthropic", "claude-test"),
        Profile("gemini-test", "gemini", "gemini-test"),
        Profile(
            "azure-test",
            "azure-openai",
            "deployment-test",
            base_url="https://example.openai.azure.com",
            options={"api_version": "2025-04-01-preview"},
        ),
        Profile(
            "vertex-test",
            "vertex",
            "gemini-test",
            options={"project": "project-test", "location": "us-central1"},
        ),
        Profile(
            "azure-v1-test",
            "azure-openai",
            "deployment-test",
            base_url="https://resource.openai.azure.com/openai/v1",
        ),
    ]
    adapters = [build_model(profile) for profile in profiles]
    names = [type(adapter).__name__ for adapter in adapters]
    assert names == [
        "OpenAIResponsesModel",
        "OpenAIChatModel",
        "AnthropicModel",
        "GoogleModel",
        "OpenAIChatModel",
        "GoogleModel",
        "OpenAIChatModel",
    ]
    # The production provider clients must refuse redirects so a model API
    # credential cannot be replayed to a server-selected origin. Remote clients
    # still retain environment-proxy support; loopback has a separate stricter
    # trust_env=False assertion below.
    assert all(
        adapter._provider._own_http_client.follow_redirects is False
        for adapter in adapters
    )


def test_openai_and_azure_api_surface_overrides_construct_without_network(
    monkeypatch,
) -> None:
    pytest.importorskip("pydantic_ai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-a-real-key")
    monkeypatch.setenv("REPLAYLAB_CUSTOM_API_KEY", "test-only-not-a-real-key")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-only-not-a-real-key")

    profiles = [
        Profile("openai-chat", "openai", "gpt-test", options={"api": "chat"}),
        Profile(
            "compatible-responses",
            "custom-openai",
            "model-test",
            base_url="https://relay.example.test/v1/responses",
            options={"api": "responses"},
        ),
        Profile(
            "azure-legacy-responses",
            "azure-openai",
            "deployment-test",
            base_url="https://resource.openai.azure.com",
            options={"api": "responses", "api_version": "2025-04-01-preview"},
        ),
        Profile(
            "azure-v1-chat",
            "azure-openai",
            "deployment-test",
            base_url="https://resource.openai.azure.com/openai/v1",
            options={"api": "chat"},
        ),
    ]

    assert [type(build_model(profile)).__name__ for profile in profiles] == [
        "OpenAIChatModel",
        "OpenAIResponsesModel",
        "OpenAIResponsesModel",
        "OpenAIChatModel",
    ]


def test_loopback_model_adapter_disables_environment_proxies() -> None:
    pytest.importorskip("pydantic_ai")
    model = build_model(
        Profile(
            "local-proxy-boundary",
            "custom-openai",
            "local-model",
            base_url="http://127.0.0.1:11434/v1",
        ),
        environ={"REPLAYLAB_CUSTOM_API_KEY": "local-secret"},
    )
    transport = model._provider._client._client
    assert transport.trust_env is False


@pytest.mark.asyncio
async def test_existing_mcp_host_is_filtered_before_model_request(tmp_path) -> None:
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from replaylab_agent.mcp_tools import build_mcp_toolset, find_replaylab_mcp

    try:
        find_replaylab_mcp()
    except Exception as exc:
        pytest.skip(str(exc))
    model = TestModel(call_tools=[], custom_output_text="done")
    agent = Agent(
        model,
        toolsets=[
            build_mcp_toolset(
                {"get_pipeline_state"}, host_env=_host_env(tmp_path, "direct")
            )
        ],
    )
    result = await agent.run("list contract only")
    assert result.output == "done"
    assert [tool.name for tool in model.last_model_request_parameters.function_tools] == [
        "get_pipeline_state"
    ]


@pytest.mark.asyncio
async def test_stdio_proxy_exposes_only_skill_allowlist(tmp_path) -> None:
    pytest.importorskip("mcp")
    from fastmcp.client.transports import StdioTransport
    from pydantic_ai import Agent
    from pydantic_ai.mcp import MCPToolset
    from pydantic_ai.models.test import TestModel

    from replaylab_agent.mcp_tools import find_replaylab_mcp

    host = find_replaylab_mcp()
    toolset = MCPToolset(
        StdioTransport(
            command=sys.executable,
            args=[
                "-I",
                "-m",
                "replaylab_agent.mcp_proxy",
                "--host",
                str(host),
                "--allow",
                "get_pipeline_state",
                "--instance-id",
                "test-qrenderdoc-instance",
                "--registry-path",
                str((tmp_path / "proxy-registry.json").resolve()),
            ],
        )
    )
    model = TestModel(call_tools=[], custom_output_text="done")
    result = await Agent(model, toolsets=[toolset]).run("contract only")
    assert result.output == "done"
    assert [tool.name for tool in model.last_model_request_parameters.function_tools] == [
        "get_pipeline_state"
    ]


@pytest.mark.asyncio
async def test_stdio_proxy_reads_per_turn_codex_grant_from_environment(tmp_path) -> None:
    pytest.importorskip("mcp")
    from fastmcp.client.transports import StdioTransport
    from pydantic_ai import Agent
    from pydantic_ai.mcp import MCPToolset
    from pydantic_ai.models.test import TestModel

    from replaylab_agent.mcp_tools import find_replaylab_mcp

    host = find_replaylab_mcp()
    environment = dict(os.environ)
    environment["REPLAYLAB_ALLOWED_TOOLS"] = json.dumps(["get_draw_call_details"])
    environment.update(_host_env(tmp_path, "codex"))
    toolset = MCPToolset(
        StdioTransport(
            command=sys.executable,
            args=[
                "-I",
                "-m",
                "replaylab_agent.mcp_proxy",
                "--host",
                str(host),
                "--allow-env",
                "REPLAYLAB_ALLOWED_TOOLS",
            ],
            env=environment,
        )
    )
    model = TestModel(call_tools=[], custom_output_text="done")
    await Agent(model, toolsets=[toolset]).run("contract only")
    assert [tool.name for tool in model.last_model_request_parameters.function_tools] == [
        "get_draw_call_details"
    ]


@pytest.mark.asyncio
async def test_skill_tools_use_direct_contract_and_acp_first_party_adapter(
    tmp_path,
) -> None:
    pytest.importorskip("mcp")
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from replaylab_agent.mcp_tools import find_replaylab_mcp
    from replaylab_agent.process_env import host_process_environment
    from replaylab_agent.runtime import AgentRuntime
    from replaylab_agent.skills import SkillCatalog
    from replaylab_agent.tool_service import available_internal_tools

    host = find_replaylab_mcp()
    async with stdio_client(
        StdioServerParameters(
            command=str(host),
            args=["stdio"],
            env=host_process_environment(_host_env(tmp_path, "contract")),
        )
    ) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            host_tools = {tool.name for tool in (await session.list_tools()).tools}
    skills = SkillCatalog([Path(__file__).resolve().parents[2] / ".agents" / "skills"]).load()
    skill_tools: set[str] = set()
    for skill in skills.skills.values():
        if not skill.enabled:
            continue
        effective, _withheld = AgentRuntime._effective_tools(skill, None)
        skill_tools.update(effective)
    internal_composites = {"inspect_event", "analyze_pixel", "inspect_shader"}
    assert internal_composites <= skill_tools
    assert internal_composites <= set(available_internal_tools())
    assert internal_composites <= host_tools
    # API and Codex app-server execute every specialist contract in-process.
    # ACP v1 has no Host dynamic-tool callback, so its task-scoped first-party
    # MCP adapter must independently expose the same complete contract.
    assert skill_tools <= set(available_internal_tools())
    assert skill_tools <= host_tools, (
        "missing first-party ReplayLab ACP adapter tools: "
        f"{sorted(skill_tools - host_tools)}"
    )


@pytest.mark.asyncio
async def test_empty_codex_grant_never_starts_upstream_host(tmp_path) -> None:
    pytest.importorskip("mcp")
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    environment = dict(os.environ)
    environment.pop("REPLAYLAB_ALLOWED_TOOLS", None)
    missing_host = tmp_path / "must-not-be-started.exe"
    async with stdio_client(
        StdioServerParameters(
            command=sys.executable,
            args=[
                "-I",
                "-m",
                "replaylab_agent.mcp_proxy",
                "--host",
                str(missing_host),
                "--allow-env",
                "REPLAYLAB_ALLOWED_TOOLS",
            ],
            env=environment,
        )
    ) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            assert (await session.list_tools()).tools == []


def test_host_json_error_envelope_is_not_success() -> None:
    from types import SimpleNamespace

    from replaylab_agent.mcp_tools import tool_result_error

    result = SimpleNamespace(
        is_error=False,
        content=[SimpleNamespace(text='{"error":{"message":"bridge failed"}}')],
    )
    assert tool_result_error(result) == "host-error-envelope"
    assert tool_result_error(SimpleNamespace(is_error=False, content=[])) == ""
