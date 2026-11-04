"""Bounded request-level negotiation for user-selected OpenAI-compatible hosts.

Only rejected requests can negotiate. A streamed token, network ambiguity,
authentication failure or model error never restarts a request/Agent/tool turn.
"""
from __future__ import annotations
from typing import Any
import logging
from urllib.parse import urlsplit

_OPTIONAL = frozenset({"temperature", "top_p", "seed", "store", "reasoning",
                      "reasoning_effort", "stream_options", "parallel_tool_calls", "user"})
_TYPES: dict[type, type] = {}


def _repair(error, config, rejected, attempted):
    status = getattr(error, "status_code", None)
    if status not in {400, 404, 405, 422}:
        return None
    body = getattr(error, "body", None)
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]
    body = body if isinstance(body, dict) else {}
    code = str(body.get("code") or "").lower()
    param = str(body.get("param") or "")
    message = str(body.get("message") or error).lower()[:4096]
    if (code in {"model_not_found", "model_not_available", "invalid_api_key", "insufficient_quota"} or param == "model"
            or "model" in message and any(term in message for term in ("not found", "does not exist", "not available"))):
        return None
    if param in _OPTIONAL and ("unsupported" in code or "unsupported" in message
                               or "unknown parameter" in message or "unrecognized" in message):
        updated = dict(config)
        updated.pop(param, None)
        return updated, rejected | {param}
    surface = "responses" if config.get("use_responses_api") else "chat"
    alternate = "chat" if surface == "responses" else "responses"
    missing_contract = ((surface == "responses" and param == "messages") or
                        (surface == "chat" and param == "input"))
    route_error = status in {404, 405} or code in {"unsupported_api", "unsupported_endpoint", "endpoint_not_found"}
    mentions_surface = ("responses" in message or "chat/completions" in message or missing_contract)
    if not (route_error or missing_contract):
        return None
    base = str(config.get("base_url") or "").rstrip("/")
    path = urlsplit(base).path
    other_base = base[:-3].rstrip("/") if path.lower().endswith("/v1") else base + "/v1"
    # A named surface rejection is stronger than guessing a version suffix.
    candidates = [(base, alternate), (other_base, surface), (other_base, alternate)] if mentions_surface else [
        (other_base, surface), (base, alternate), (other_base, alternate)]
    for endpoint, selected in candidates:
        key = (endpoint, selected, tuple(sorted(rejected)))
        if key not in attempted:
            updated = dict(config, base_url=endpoint, use_responses_api=selected == "responses")
            if selected == "chat":
                reasoning = updated.pop("reasoning", None)
                if isinstance(reasoning, dict) and reasoning.get("effort"):
                    updated["reasoning_effort"] = reasoning["effort"]
            else:
                effort = updated.pop("reasoning_effort", None)
                if effort:
                    updated["reasoning"] = {"effort": effort}
            return updated, set(rejected)
    return None


def compatible_chat_model(model_type, config: dict[str, Any]):
    """Preserve ChatOpenAI's bind_tools/streaming contract without graph retries."""
    from pydantic import PrivateAttr
    if not isinstance(model_type, type) or not callable(getattr(model_type, "_agenerate", None)):
        return model_type(**config)  # Existing lightweight embedding/test adapters.
    adapter_type = _TYPES.get(model_type)
    if adapter_type is None:
        class CompatibleOpenAI(model_type):
            _bread_config: dict = PrivateAttr(default_factory=dict)
            _bread_rejected: set = PrivateAttr(default_factory=set)
            _bread_delegate: Any = PrivateAttr(default=None)

            def _selection_key(self):
                c = self._bread_config
                return (str(c.get("base_url") or "").rstrip("/"),
                        "responses" if c.get("use_responses_api") else "chat",
                        tuple(sorted(self._bread_rejected)))

            def _negotiate(self, error, attempted):
                repair = _repair(error, self._bread_config, self._bread_rejected, attempted)
                if repair is None:
                    return False
                updated, rejected = repair
                key = (str(updated.get("base_url") or "").rstrip("/"),
                       "responses" if updated.get("use_responses_api") else "chat", tuple(sorted(rejected)))
                if key in attempted:
                    return False
                # Credentials stay on the same origin, never on a server-supplied URL.
                old, new = urlsplit(str(self._bread_config["base_url"])), urlsplit(str(updated["base_url"]))
                if (old.scheme, old.netloc) != (new.scheme, new.netloc):
                    return False
                delegate = model_type(**updated)
                self._bread_config, self._bread_rejected, self._bread_delegate = updated, rejected, delegate
                logging.getLogger(__name__).info(
                    "OpenAI-compatible route selected: endpoint=%s surface=%s omittedOptional=%s",
                    updated["base_url"], key[1], sorted(rejected))
                return True

            async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
                attempted = set()
                for attempt in range(4):
                    attempted.add(self._selection_key())
                    try:
                        method = self._bread_delegate._agenerate if self._bread_delegate else super()._agenerate
                        return await method(messages, stop=stop, run_manager=run_manager,
                                            **{k:v for k,v in kwargs.items() if k not in self._bread_rejected})
                    except Exception as error:
                        if attempt == 3 or not self._negotiate(error, attempted):
                            raise

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                attempted = set()
                for attempt in range(4):
                    attempted.add(self._selection_key())
                    try:
                        method = self._bread_delegate._generate if self._bread_delegate else super()._generate
                        return method(messages, stop=stop, run_manager=run_manager,
                                      **{k:v for k,v in kwargs.items() if k not in self._bread_rejected})
                    except Exception as error:
                        if attempt == 3 or not self._negotiate(error, attempted):
                            raise

            def _stream(self, messages, stop=None, run_manager=None, **kwargs):
                attempted = set()
                for attempt in range(4):
                    attempted.add(self._selection_key())
                    published = False
                    try:
                        method = self._bread_delegate._stream if self._bread_delegate else super()._stream
                        for chunk in method(messages, stop=stop, run_manager=run_manager,
                                            **{k:v for k,v in kwargs.items() if k not in self._bread_rejected}):
                            published = True
                            yield chunk
                        return
                    except Exception as error:
                        if published or attempt == 3 or not self._negotiate(error, attempted):
                            raise

            async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
                attempted = set()
                for attempt in range(4):
                    attempted.add(self._selection_key())
                    published = False
                    try:
                        method = self._bread_delegate._astream if self._bread_delegate else super()._astream
                        async for chunk in method(messages, stop=stop, run_manager=run_manager,
                                                  **{k:v for k,v in kwargs.items() if k not in self._bread_rejected}):
                            published = True
                            yield chunk
                        return
                    except Exception as error:
                        if published or attempt == 3 or not self._negotiate(error, attempted):
                            raise

        adapter_type = _TYPES[model_type] = CompatibleOpenAI
    model = adapter_type(**config)
    model._bread_config = dict(config)
    return model
