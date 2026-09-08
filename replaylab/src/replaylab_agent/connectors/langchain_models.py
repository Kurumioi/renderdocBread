"""LangChain chat-model construction for the deepagents engine.

This is the sole model-adapter layer. Profiles keep the same endpoint,
credential and option names
(``api``, ``api_version``, ``temperature``, ``timeout_seconds``,
``reasoning_effort``, ``thinking_budget``, ``stream_thinking``).
"""

from __future__ import annotations

import importlib
from typing import Any, Mapping

from ..errors import ConfigurationError, ConnectorError
from ..profiles import Profile

_INSTALL_HINT = 'pip install "replaylab-agenthost"'
_LOCAL_API_KEY_PLACEHOLDER = "replaylab-local-no-key"


def _string_option(profile: Profile, name: str, default: str) -> str:
    value = profile.options.get(name, default)
    if not isinstance(value, str):
        raise ConfigurationError(
            f"profile '{profile.id}' options.{name} must be a string"
        )
    return value.strip()


def _require_endpoint(profile: Profile) -> str:
    endpoint = profile.endpoint
    if not endpoint:
        raise ConfigurationError(
            f"profile '{profile.id}' requires base_url for protocol "
            f"'{profile.provider.protocol}'"
        )
    return endpoint


def _load(module: str, profile: Profile) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ConnectorError(
            f"profile '{profile.id}' needs the LangChain package providing '{module}': {_INSTALL_HINT}"
        ) from exc


def _common_kwargs(profile: Profile, max_retries: int | None = None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    if "temperature" in profile.options:
        kwargs["temperature"] = float(profile.options["temperature"])
    if "timeout_seconds" in profile.options:
        kwargs["timeout"] = float(profile.options["timeout_seconds"])
    return kwargs


def _max_output_tokens(profile: Profile) -> int:
    value = profile.options.get("max_output_tokens", 0)
    return int(value) if value else 0


def _thinking_budget(profile: Profile) -> int:
    from ..native_vendor import thinking_budget

    return int(thinking_budget(profile))


def build_chat_model(
    profile: Profile,
    *,
    model: str = "",
    environ: Mapping[str, str] | None = None,
    max_retries: int | None = None,
) -> Any:
    """A LangChain ``BaseChatModel`` for *profile* (no request is made)."""

    profile.validate()
    if max_retries is not None and (type(max_retries) is not int or not 0 <= max_retries <= 10):
        raise ConfigurationError("max_retries must be an integer from 0 to 10")
    provider = profile.provider
    if provider.kind != "model":
        raise ConfigurationError(
            f"profile '{profile.id}' is an external Agent connector ({provider.protocol}); "
            "the deepagents engine only drives model API connections"
        )
    environment = None if environ is None else dict(environ)
    model_name = profile.resolve_model(model)
    protocol = provider.protocol
    stream_thinking = profile.options.get("stream_thinking")
    budget = 0 if stream_thinking is False else _thinking_budget(profile)

    if protocol in {"openai-responses", "openai-chat"}:
        endpoint = _require_endpoint(profile)
        api_key = profile.resolve_api_key(environment)
        if provider.auth == "none":
            api_key = _LOCAL_API_KEY_PLACEHOLDER
        module = _load("langchain_openai", profile)
        default_surface = "responses" if protocol == "openai-responses" else "chat"
        surface = _string_option(profile, "api", default_surface).lower()
        if surface not in {"chat", "responses"}:
            raise ConfigurationError(f"profile '{profile.id}' options.api must be 'chat' or 'responses'")
        kwargs = _common_kwargs(profile, max_retries)
        kwargs.update({"model": model_name, "api_key": api_key, "base_url": endpoint})
        if _max_output_tokens(profile):
            kwargs["max_tokens"] = _max_output_tokens(profile)
        if surface == "responses":
            kwargs["use_responses_api"] = True
            if budget > 0:
                kwargs["reasoning"] = {"summary": "detailed"}
        effort = profile.options.get("reasoning_effort")
        if effort is not None:
            kwargs["reasoning_effort"] = str(effort)
        return module.ChatOpenAI(**kwargs)

    if protocol == "anthropic-messages":
        endpoint = _require_endpoint(profile)
        api_key = profile.resolve_api_key(environment)
        module = _load("langchain_anthropic", profile)
        kwargs = _common_kwargs(profile, max_retries)
        kwargs.update({"model": model_name, "api_key": api_key, "base_url": endpoint})
        if _max_output_tokens(profile):
            kwargs["max_tokens"] = _max_output_tokens(profile)
        if budget > 0 and (stream_thinking is True or profile.preset == "anthropic"):
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": max(1024, budget)}
            kwargs.pop("temperature", None)  # Anthropic refuses temperature with thinking
            kwargs.setdefault("max_tokens", max(4096, budget + 2048))
        return module.ChatAnthropic(**kwargs)

    if protocol == "google-genai":
        api_key = profile.resolve_api_key(environment)
        module = _load("langchain_google_genai", profile)
        kwargs = _common_kwargs(profile, max_retries)
        kwargs.update({"model": model_name, "google_api_key": api_key})
        if _max_output_tokens(profile):
            kwargs["max_output_tokens"] = _max_output_tokens(profile)
        if budget > 0:
            kwargs["include_thoughts"] = True
        return module.ChatGoogleGenerativeAI(**kwargs)

    if protocol == "google-cloud":
        module = _load("langchain_google_vertexai", profile)
        kwargs = _common_kwargs(profile, max_retries)
        kwargs["model_name"] = model_name
        project = _string_option(profile, "project", "")
        location = _string_option(profile, "location", "")
        if project:
            kwargs["project"] = project
        if location:
            kwargs["location"] = location
        if _max_output_tokens(profile):
            kwargs["max_output_tokens"] = _max_output_tokens(profile)
        return module.ChatVertexAI(**kwargs)

    if protocol == "azure-openai":
        endpoint = _require_endpoint(profile)
        api_key = profile.resolve_api_key(environment)
        module = _load("langchain_openai", profile)
        kwargs = _common_kwargs(profile, max_retries)
        kwargs.update(
            {
                "azure_deployment": model_name,
                "azure_endpoint": endpoint,
                "api_key": api_key,
                "api_version": _string_option(profile, "api_version", "") or None,
            }
        )
        if _max_output_tokens(profile):
            kwargs["max_tokens"] = _max_output_tokens(profile)
        effort = profile.options.get("reasoning_effort")
        if effort is not None:
            kwargs["reasoning_effort"] = str(effort)
        return module.AzureChatOpenAI(**kwargs)

    raise ConfigurationError(
        f"profile '{profile.id}' uses model protocol '{protocol}', which the deepagents engine does not support yet"
    )


__all__ = ["build_chat_model"]
