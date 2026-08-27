"""Lazy OpenAI Responses API boundary for the external process only."""

from __future__ import annotations

import os
from typing import Any


class OpenAIStartupError(RuntimeError):
    pass


class OpenAIResponsesClient:
    def __init__(self, api_key: str | None = None):
        key = str(api_key or os.environ.get("OPENAI_API_KEY", "")).strip()
        if not key:
            raise OpenAIStartupError(
                "OPENAI_API_KEY is not set. Set it only in the shell that "
                "launches renderdoc-ai-companion; do not put it in the "
                "qrenderdoc integration or ReplayLab MCP bridge configuration."
            )
        try:
            from openai import AsyncOpenAI
        except ImportError as error:
            raise OpenAIStartupError(
                "The OpenAI Python SDK is missing. Install the optional "
                "companion dependencies with: pip install -e \".[companion]\""
            ) from error
        self._client = AsyncOpenAI(api_key=key)

    async def create_response(self, **parameters: Any) -> Any:
        return await self._client.responses.create(**parameters)
