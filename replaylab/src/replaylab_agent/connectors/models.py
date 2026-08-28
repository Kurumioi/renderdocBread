"""Build Pydantic AI models from ReplayLab provider profiles.

This module deliberately has no provider SDK imports at module import time.
Secrets are resolved only through :meth:`Profile.resolve_api_key`, which reads
an explicit environment override or the profile-bound OS credential vault; this
code never inspects browser cookies, CLI login caches, or vendor token files.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any, Mapping

from ..errors import ConfigurationError, ConnectorError
from ..profiles import Profile, azure_endpoint_uses_v1, is_loopback_endpoint

_MODEL_EXTRA = "replaylab-agenthost[model]"
_LOCAL_API_KEY_PLACEHOLDER = "replaylab-local-no-key"


def build_model(
    profile: Profile,
    *,
    environ: Mapping[str, str] | None = None,
) -> Any:
    """Construct the model adapter selected by *profile* without making a request.

    Args:
        profile: A validated ReplayLab provider profile.
        environ: Optional environment mapping, primarily for deterministic tests.
            API keys are never accepted as a direct argument or profile option;
            an explicit mapping intentionally disables OS-vault fallback.

    Returns:
        A Pydantic AI ``Model`` instance.

    Raises:
        ConfigurationError: If the profile is an external Agent connector or
            contains unsupported/invalid model options.
        AuthenticationError: Propagated from ``Profile.resolve_api_key`` when
            its configured environment variable is absent.
        ConnectorError: If the optional Pydantic AI provider dependencies are
            missing or incompatible.
    """

    profile.validate()
    provider = profile.provider
    if provider.kind != "model":
        raise ConfigurationError(
            f"profile '{profile.id}' is an external Agent connector ({provider.protocol}); "
            "use the Codex app-server or ACP connector instead of build_model()"
        )

    environment = None if environ is None else dict(environ)
    protocol = provider.protocol

    if protocol in {"openai-responses", "openai-chat"}:
        return _build_openai(profile, environment)
    if protocol == "anthropic-messages":
        return _build_anthropic(profile, environment)
    if protocol == "google-genai":
        return _build_google(profile, environment)
    if protocol == "google-cloud":
        return _build_google_cloud(profile)
    if protocol == "azure-openai":
        return _build_azure(profile, environment)

    raise ConfigurationError(
        f"profile '{profile.id}' uses unsupported model protocol '{protocol}'"
    )


def _build_openai(profile: Profile, environ: dict[str, str] | None) -> Any:
    endpoint = _require_endpoint(profile)
    api_key = profile.resolve_api_key(environ)
    if profile.provider.auth == "none":
        # The OpenAI client requires a non-empty value even when a local server
        # such as Ollama performs no authentication. This is not a credential.
        api_key = _LOCAL_API_KEY_PLACEHOLDER

    model_module = _load_module("pydantic_ai.models.openai", profile)
    provider_module = _load_module("pydantic_ai.providers.openai", profile)
    provider_type = _symbol(provider_module, "OpenAIProvider", profile)

    provider_kwargs: dict[str, Any] = {"api_key": api_key, "base_url": endpoint}
    if http_client := _loopback_http_client(endpoint):
        provider_kwargs["http_client"] = http_client
    adapter = provider_type(**provider_kwargs)
    default_surface = (
        "responses" if profile.provider.protocol == "openai-responses" else "chat"
    )
    api_surface = _string_option(profile, "api", default_surface).lower()
    if api_surface == "responses":
        model_type = _symbol(model_module, "OpenAIResponsesModel", profile)
        return model_type(profile.model, provider=adapter)
    if api_surface != "chat":
        raise ConfigurationError(
            f"profile '{profile.id}' options.api must be 'chat' or 'responses'"
        )
    model_type = _symbol(model_module, "OpenAIChatModel", profile)
    model_profile = _openai_compatible_profile(profile)
    return model_type(profile.model, provider=adapter, profile=model_profile)


def _openai_compatible_profile(profile: Profile) -> Any:
    profiles_module = _load_module("pydantic_ai.profiles.openai", profile)
    profile_type = _symbol(profiles_module, "OpenAIModelProfile", profile)

    # Tool support is tri-state at routing time. An unprobed OpenAI-compatible
    # model is attempted and the provider response becomes the capability
    # evidence; only an explicit option or completed negative probe disables
    # tools before the request. This avoids turning missing metadata into a
    # permanent permission dead end.
    default_tool_support = (
        "tools" in profile.capabilities or profile.probed_capabilities is None
    )
    return profile_type(
        supports_tools=_bool_option(
            profile, "supports_tools", default_tool_support
        ),
        supports_json_schema_output=_bool_option(
            profile,
            "supports_json_schema_output",
            "json-schema" in profile.capabilities,
        ),
        openai_supports_strict_tool_definition=_bool_option(
            profile, "strict_tools", False
        ),
        openai_supports_tool_choice_required=_bool_option(
            profile, "tool_choice_required", False
        ),
        openai_chat_supports_multiple_system_messages=_bool_option(
            profile, "multiple_system_messages", False
        ),
        openai_chat_supports_max_completion_tokens=_bool_option(
            profile, "max_completion_tokens", False
        ),
        openai_chat_supports_document_input=_bool_option(
            profile, "document_input", False
        ),
    )


def _build_anthropic(profile: Profile, environ: dict[str, str] | None) -> Any:
    endpoint = _require_endpoint(profile)
    api_key = profile.resolve_api_key(environ)
    model_module = _load_module("pydantic_ai.models.anthropic", profile)
    provider_module = _load_module("pydantic_ai.providers.anthropic", profile)
    model_type = _symbol(model_module, "AnthropicModel", profile)
    provider_type = _symbol(provider_module, "AnthropicProvider", profile)

    provider_kwargs: dict[str, Any] = {"api_key": api_key, "base_url": endpoint}
    if http_client := _loopback_http_client(endpoint):
        provider_kwargs["http_client"] = http_client
    adapter = provider_type(**provider_kwargs)
    return model_type(profile.model, provider=adapter)


def _build_google(profile: Profile, environ: dict[str, str] | None) -> Any:
    endpoint = _require_endpoint(profile)
    api_key = profile.resolve_api_key(environ)
    model_module = _load_module("pydantic_ai.models.google", profile)
    provider_module = _load_module("pydantic_ai.providers.google", profile)
    model_type = _symbol(model_module, "GoogleModel", profile)
    provider_type = _symbol(provider_module, "GoogleProvider", profile)

    provider_kwargs: dict[str, Any] = {"api_key": api_key, "base_url": endpoint}
    if http_client := _loopback_http_client(endpoint):
        provider_kwargs["http_client"] = http_client
    adapter = provider_type(**provider_kwargs)
    return model_type(profile.model, provider=adapter)


def _build_google_cloud(profile: Profile) -> Any:
    model_module = _load_module("pydantic_ai.models.google", profile)
    provider_module = _load_module("pydantic_ai.providers.google_cloud", profile)
    model_type = _symbol(model_module, "GoogleModel", profile)
    provider_type = _symbol(provider_module, "GoogleCloudProvider", profile)

    project = _string_option(profile, "project", "")
    location = _string_option(profile, "location", "")
    provider_kwargs: dict[str, str] = {}
    if project:
        provider_kwargs["project"] = project
    if location:
        provider_kwargs["location"] = location

    # Authentication is delegated only to Google's documented Application
    # Default Credentials flow. ReplayLab itself does not open credential files.
    adapter = provider_type(**provider_kwargs)
    return model_type(profile.model, provider=adapter)


def _build_azure(profile: Profile, environ: dict[str, str] | None) -> Any:
    endpoint = _require_endpoint(profile)
    api_key = profile.resolve_api_key(environ)
    model_module = _load_module("pydantic_ai.models.openai", profile)
    provider_module = _load_module("pydantic_ai.providers.azure", profile)
    provider_type = _symbol(provider_module, "AzureProvider", profile)

    api_version = _string_option(profile, "api_version", "")
    provider_kwargs: dict[str, Any] = {
        "azure_endpoint": endpoint,
        "api_version": None if azure_endpoint_uses_v1(endpoint) else api_version,
        "api_key": api_key,
    }
    if http_client := _loopback_http_client(endpoint):
        provider_kwargs["http_client"] = http_client
    adapter = provider_type(**provider_kwargs)

    api_surface = _string_option(profile, "api", "chat").lower()
    if api_surface == "chat":
        model_type = _symbol(model_module, "OpenAIChatModel", profile)
    elif api_surface == "responses":
        model_type = _symbol(model_module, "OpenAIResponsesModel", profile)
    else:
        raise ConfigurationError(
            f"profile '{profile.id}' options.api must be 'chat' or 'responses'"
        )
    return model_type(profile.model, provider=adapter)


def _bool_option(profile: Profile, name: str, default: bool) -> bool:
    value = profile.options.get(name, default)
    if not isinstance(value, bool):
        raise ConfigurationError(
            f"profile '{profile.id}' options.{name} must be a boolean"
        )
    return value


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


def _loopback_http_client(endpoint: str) -> Any | None:
    """Build a proxy-free client only for a local credential-bearing API.

    Remote HTTPS providers retain HTTPX/SDK environment-proxy support for
    enterprise compatibility.  Local endpoints must never depend on a user's
    NO_PROXY configuration because the request can contain an API key.
    """

    if not is_loopback_endpoint(endpoint):
        return None
    # Pydantic AI 2.x is migrating its provider transports from httpx to the
    # wire-compatible httpx2 package.  Prefer the new client when that optional
    # model dependency provides it, while retaining compatibility with earlier
    # supported 2.x builds and the project's direct httpx dependency.
    try:
        # Presence here means the installed Pydantic AI build itself selected
        # and supports httpx2; merely finding an unrelated httpx2 package is
        # not enough to change transports on an older supported release.
        http_module = getattr(import_module("pydantic_ai._http"), "httpx2")
    except (ImportError, AttributeError):
        http_module = import_module("httpx")
    return http_module.AsyncClient(trust_env=False, follow_redirects=False)


def _load_module(module_name: str, profile: Profile) -> Any:
    try:
        return import_module(module_name)
    except (ImportError, AttributeError) as exc:
        raise ConnectorError(
            f"profile '{profile.id}' requires compatible optional model dependencies; "
            f"install the '{_MODEL_EXTRA}' extra ({module_name}: {exc})"
        ) from exc


def _symbol(module: Any, name: str, profile: Profile) -> Any:
    try:
        return getattr(module, name)
    except AttributeError as exc:
        raise ConnectorError(
            f"profile '{profile.id}' needs a newer compatible Pydantic AI build: "
            f"{module.__name__}.{name} is unavailable"
        ) from exc
