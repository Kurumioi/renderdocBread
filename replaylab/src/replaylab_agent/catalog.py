"""Built-in connection presets.

Presets contain no secrets and make no claim that an OpenAI-compatible endpoint
implements every OpenAI feature.  Capabilities begin as declarations and are
replaced with probe results by the connection wizard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ConnectorKind = Literal["model", "agent"]
Protocol = Literal[
    "openai-responses",
    "openai-chat",
    "anthropic-messages",
    "google-genai",
    "google-cloud",
    "azure-openai",
    "codex-app-server",
    "acp",
]
SubscriptionLoginMode = Literal["none", "codex", "captured", "terminal"]


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    id: str
    title: str
    kind: ConnectorKind
    protocol: Protocol
    base_url: str = ""
    api_key_env: str = ""
    command: tuple[str, ...] = ()
    auth: Literal["api-key", "managed", "adc", "none"] = "api-key"
    declared_capabilities: frozenset[str] = field(default_factory=frozenset)
    note: str = ""
    subscription_login: tuple[str, ...] = ()
    subscription_login_mode: SubscriptionLoginMode = "none"
    subscription_instruction: str = ""
    install_hint: str = ""

    @property
    def configuration_mode(self) -> Literal["api", "subscription"]:
        return "api" if self.kind == "model" else "subscription"

    @property
    def authentication_ui(self) -> Literal[
        "api-key", "cloud-adc", "codex-managed", "vendor-cli", "none"
    ]:
        if self.protocol == "codex-app-server":
            return "codex-managed"
        if self.protocol == "acp":
            return "vendor-cli"
        if self.auth == "adc":
            return "cloud-adc"
        if self.auth == "none":
            return "none"
        return "api-key"

    @property
    def supports_model_discovery(self) -> bool:
        return self.protocol in {
            "openai-responses",
            "openai-chat",
            "anthropic-messages",
            "google-genai",
            "codex-app-server",
        }


_CHAT = frozenset({"text", "stream", "tools"})
_RICH = frozenset({"text", "stream", "tools", "json-schema", "vision"})


PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        "openai",
        "OpenAI API",
        "model",
        "openai-responses",
        "https://api.openai.com/v1",
        "OPENAI_API_KEY",
        declared_capabilities=_RICH,
    ),
    ProviderPreset(
        "anthropic",
        "Anthropic API",
        "model",
        "anthropic-messages",
        "https://api.anthropic.com",
        "ANTHROPIC_API_KEY",
        declared_capabilities=_RICH,
        note=(
            "Claude API is supported here. Claude Code does not publish an ACP server mode, so "
            "subscription access is not presented as ACP. A future dedicated CLI connector must "
            "also follow Anthropic's third-party authentication terms."
        ),
    ),
    ProviderPreset(
        "gemini",
        "Google Gemini API",
        "model",
        "google-genai",
        "https://generativelanguage.googleapis.com",
        "GOOGLE_API_KEY",
        declared_capabilities=_RICH,
    ),
    ProviderPreset(
        "vertex",
        "Google Cloud / Vertex AI",
        "model",
        "google-cloud",
        auth="adc",
        declared_capabilities=_RICH,
    ),
    ProviderPreset(
        "azure-openai",
        "Azure OpenAI",
        "model",
        "azure-openai",
        api_key_env="AZURE_OPENAI_API_KEY",
        declared_capabilities=_RICH,
        note=(
            "The profile model is the Azure deployment name. Legacy endpoints require "
            "api_version; Azure v1/Foundry endpoints must omit it."
        ),
    ),
    ProviderPreset(
        "openrouter",
        "OpenRouter",
        "model",
        "openai-chat",
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
        declared_capabilities=_CHAT,
    ),
    ProviderPreset(
        "xai",
        "xAI / Grok",
        "model",
        "openai-responses",
        "https://api.x.ai/v1",
        "XAI_API_KEY",
        declared_capabilities=_RICH,
    ),
    ProviderPreset(
        "mistral",
        "Mistral AI",
        "model",
        "openai-chat",
        "https://api.mistral.ai/v1",
        "MISTRAL_API_KEY",
        declared_capabilities=_CHAT,
    ),
    ProviderPreset(
        "groq",
        "GroqCloud",
        "model",
        "openai-chat",
        "https://api.groq.com/openai/v1",
        "GROQ_API_KEY",
        declared_capabilities=_CHAT,
        note="OpenAI compatibility is partial; run the connection probe for the chosen model.",
    ),
    ProviderPreset(
        "deepseek",
        "DeepSeek",
        "model",
        "openai-chat",
        "https://api.deepseek.com",
        "DEEPSEEK_API_KEY",
        declared_capabilities=_CHAT,
    ),
    ProviderPreset(
        "qwen",
        "Alibaba Model Studio / Qwen",
        "model",
        "openai-chat",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY",
        declared_capabilities=_CHAT,
        note="Standard API keys and Coding Plan keys use their documented endpoints.",
    ),
    ProviderPreset(
        "qwen-coding-plan",
        "Alibaba Model Studio Coding Plan",
        "model",
        "openai-chat",
        "https://coding.dashscope.aliyuncs.com/v1",
        "BAILIAN_CODING_PLAN_API_KEY",
        declared_capabilities=_CHAT,
        note=(
            "The Coding Plan is a subscription key with a dedicated endpoint, not an OAuth login. "
            "Use the international endpoint as a custom base URL when applicable."
        ),
    ),
    ProviderPreset(
        "kimi",
        "Moonshot / Kimi",
        "model",
        "openai-chat",
        "https://api.moonshot.cn/v1",
        "MOONSHOT_API_KEY",
        declared_capabilities=_CHAT,
    ),
    ProviderPreset(
        "minimax",
        "MiniMax",
        "model",
        "anthropic-messages",
        "https://api.minimax.io/anthropic",
        "MINIMAX_API_KEY",
        declared_capabilities=_CHAT,
        note="Supports metered keys and official Token Plan keys; it is not a browser login.",
    ),
    ProviderPreset(
        "glm",
        "Zhipu GLM",
        "model",
        "openai-chat",
        "https://open.bigmodel.cn/api/paas/v4",
        "ZHIPUAI_API_KEY",
        declared_capabilities=_CHAT,
    ),
    ProviderPreset(
        "doubao",
        "Volcengine Ark / Doubao",
        "model",
        "openai-chat",
        "https://ark.cn-beijing.volces.com/api/v3",
        "ARK_API_KEY",
        declared_capabilities=_CHAT,
    ),
    ProviderPreset(
        "hunyuan",
        "Tencent Hunyuan / TokenHub",
        "model",
        "openai-chat",
        "https://api.hunyuan.cloud.tencent.com/v1",
        "HUNYUAN_API_KEY",
        declared_capabilities=_CHAT,
    ),
    ProviderPreset(
        "qianfan",
        "Baidu Qianfan",
        "model",
        "openai-chat",
        "https://qianfan.baidubce.com/v2",
        "QIANFAN_API_KEY",
        declared_capabilities=_CHAT,
    ),
    ProviderPreset(
        "siliconflow",
        "SiliconFlow",
        "model",
        "openai-chat",
        "https://api.siliconflow.cn/v1",
        "SILICONFLOW_API_KEY",
        declared_capabilities=_CHAT,
    ),
    ProviderPreset(
        "ollama",
        "Ollama (local)",
        "model",
        "openai-chat",
        "http://127.0.0.1:11434/v1",
        auth="none",
        declared_capabilities=frozenset({"text", "stream"}),
    ),
    ProviderPreset(
        "custom-openai",
        "Custom OpenAI-compatible endpoint",
        "model",
        "openai-chat",
        api_key_env="REPLAYLAB_CUSTOM_API_KEY",
        declared_capabilities=frozenset({"text"}),
        note="Capabilities are unknown until probed; no feature is inferred from the name.",
    ),
    ProviderPreset(
        "codex",
        "Codex / ChatGPT subscription",
        "agent",
        "codex-app-server",
        command=("codex", "app-server"),
        auth="managed",
        declared_capabilities=frozenset({"chat", "history", "tools", "approvals", "skills"}),
        subscription_login_mode="codex",
        install_hint="Install the official Codex CLI: https://developers.openai.com/codex/cli/",
    ),
    ProviderPreset(
        "copilot-acp",
        "GitHub Copilot CLI (ACP)",
        "agent",
        "acp",
        command=("copilot", "--acp"),
        auth="managed",
        declared_capabilities=frozenset({"chat", "tools", "approvals"}),
        subscription_login=("copilot", "login"),
        subscription_login_mode="captured",
        note=(
            "Uses the official `copilot login --web-flow/--device-code` command, then tests "
            "`copilot --acp`. The ACP handshake may expose model choices; otherwise leave the "
            "model blank to use Copilot's default or enter an exact supported model manually."
        ),
        install_hint=(
            "GitHub Copilot CLI was not found. Install it with `winget install GitHub.Copilot` "
            "or `npm install -g @github/copilot`, then restart qrenderdoc. Official guide: "
            "https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli"
        ),
    ),
    ProviderPreset(
        "gemini-acp",
        "Gemini CLI (ACP)",
        "agent",
        "acp",
        command=("gemini", "--acp"),
        auth="managed",
        declared_capabilities=frozenset({"chat", "tools", "approvals"}),
        subscription_login=("gemini",),
        subscription_login_mode="terminal",
        subscription_instruction=(
            "Choose Sign in with Google in the Gemini terminal, complete the browser flow, "
            "then exit the terminal after authentication succeeds."
        ),
        note=(
            "Gemini authenticates interactively before ReplayLab starts `gemini --acp`. Model "
            "choices are used only when the ACP session exposes them; otherwise use the provider "
            "default or enter an exact model supported by your installed CLI."
        ),
        install_hint=(
            "Gemini CLI was not found. Install it with `npm install -g @google/gemini-cli`, "
            "then restart qrenderdoc. Official guide: "
            "https://github.com/google-gemini/gemini-cli/blob/main/docs/get-started/installation.mdx"
        ),
    ),
    ProviderPreset(
        "qwen-acp",
        "Qwen Code / Coding Plan (ACP)",
        "agent",
        "acp",
        command=("qwen", "--acp"),
        auth="managed",
        declared_capabilities=frozenset({"chat", "tools", "approvals"}),
        subscription_login=("qwen",),
        subscription_login_mode="terminal",
        subscription_instruction=(
            "Type /auth in the Qwen terminal, configure Alibaba ModelStudio Coding Plan or an "
            "API provider, then exit after /doctor reports a usable configuration."
        ),
        note=(
            "Qwen OAuth was discontinued on 2026-04-15. This wizard opens `qwen` for the current "
            "/auth Coding Plan/API-provider setup, then tests `qwen --acp`. ACP model choices are "
            "used when exposed; otherwise use the configured default or enter an exact model."
        ),
        install_hint=(
            "Qwen Code was not found. Install it with `npm install -g "
            "@qwen-code/qwen-code@latest` (or the official Windows installer), then restart "
            "qrenderdoc. Official guide: "
            "https://qwenlm.github.io/qwen-code-docs/en/users/quickstart/"
        ),
    ),
    ProviderPreset(
        "kimi-acp",
        "Kimi CLI (ACP)",
        "agent",
        "acp",
        command=("kimi", "acp"),
        auth="managed",
        declared_capabilities=frozenset({"chat", "tools", "approvals"}),
        subscription_login=("kimi", "login"),
        subscription_login_mode="captured",
        note=(
            "Uses the official RFC 8628 `kimi login` device-code flow, then tests `kimi acp`. "
            "ReplayLab imports model choices exposed by the ACP session; if none are exposed, "
            "leave the model blank for Kimi's default or enter a configured model alias."
        ),
        install_hint=(
            "Kimi Code CLI was not found. Install it with the official Windows script or `npm "
            "install -g @moonshot-ai/kimi-code`, then restart qrenderdoc. Official guide: "
            "https://www.kimi.com/code/docs/en/kimi-code-cli/guides/getting-started.html"
        ),
    ),
    ProviderPreset(
        "custom-acp",
        "Custom ACP agent",
        "agent",
        "acp",
        auth="managed",
        declared_capabilities=frozenset({"chat"}),
    ),
)

PRESET_BY_ID = {preset.id: preset for preset in PRESETS}


def get_preset(preset_id: str) -> ProviderPreset:
    try:
        return PRESET_BY_ID[preset_id]
    except KeyError as exc:
        raise KeyError(f"unknown provider preset: {preset_id}") from exc
