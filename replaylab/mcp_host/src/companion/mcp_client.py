"""Small stdio MCP adapter used by the external companion."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator


class MCPClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class MCPToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]

    def as_openai_tool(self) -> dict[str, Any]:
        schema = self.input_schema or {"type": "object", "properties": {}}
        return {
            "type": "function",
            "name": self.name,
            "description": self.description or "RenderDoc MCP tool",
            "parameters": schema,
            # MCP schemas were not authored under OpenAI strict-mode rules.
            # The execution-side policy remains the security boundary.
            "strict": False,
        }


def _sanitised_child_environment() -> dict[str, str]:
    """Do not leak the model credential into the local MCP subprocess."""

    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("OPENAI_")
    }


class StdioMCPClient:
    def __init__(self, session: Any):
        self._session = session
        self._tools_lock = asyncio.Lock()
        self._tools: dict[str, MCPToolSpec] | None = None

    async def list_tools(self) -> dict[str, MCPToolSpec]:
        if self._tools is not None:
            return dict(self._tools)
        tools: dict[str, MCPToolSpec] = {}
        cursor: str | None = None
        async with self._tools_lock:
            while True:
                page = await self._session.list_tools(cursor=cursor)
                for item in page.tools:
                    tools[item.name] = MCPToolSpec(
                        name=item.name,
                        description=item.description or "",
                        input_schema=dict(item.inputSchema or {}),
                    )
                cursor = getattr(page, "nextCursor", None)
                if not cursor:
                    break
        self._tools = tools
        return dict(tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        # ClientSession assigns request IDs and supports concurrent in-flight
        # calls. This matters for intent lease renewal while a local decompiler
        # is still running. The MCP host's file IPC layer independently orders
        # the short qrenderdoc requests that require it.
        result = await self._session.call_tool(name, arguments)
        structured = getattr(result, "structuredContent", None)
        # FastMCP wraps a Python ``-> str`` result as both text content and
        # structuredContent={"result": "<the actual string>"}. All existing
        # RenderDoc tools intentionally return JSON strings, so preserve that
        # inner payload instead of producing a second JSON wrapper.
        if (
            isinstance(structured, dict)
            and set(structured) == {"result"}
            and isinstance(structured.get("result"), str)
        ):
            output = structured["result"]
        elif structured is not None:
            output = json.dumps(structured, ensure_ascii=False, default=str)
        else:
            chunks: list[str] = []
            for item in getattr(result, "content", ()):
                text = getattr(item, "text", None)
                if text is not None:
                    chunks.append(str(text))
                else:
                    chunks.append(str(item))
            output = "\n".join(chunks)
        if getattr(result, "isError", False):
            raise MCPClientError("MCP tool %s failed: %s" % (name, output))
        return output

    async def call_json(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raw = await self.call_tool(name, arguments)
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as error:
            raise MCPClientError(
                "MCP tool %s did not return JSON: %s" % (name, error)
            ) from error
        if not isinstance(result, dict):
            raise MCPClientError("MCP tool %s returned non-object JSON" % name)
        if result.get("error"):
            raise MCPClientError(
                "MCP tool %s failed: %s" % (name, result.get("error"))
            )
        return result


@asynccontextmanager
async def connect_stdio_mcp(
    command: str,
    arguments: list[str] | None = None,
    cwd: str | None = None,
) -> AsyncIterator[StdioMCPClient]:
    """Spawn and initialise one local MCP server over private stdio pipes."""

    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as error:  # pragma: no cover - packaging/startup path
        raise MCPClientError(
            "The MCP Python SDK is not installed. Install the ReplayLab MCP host dependencies first."
        ) from error

    parameters = StdioServerParameters(
        command=command,
        args=list(arguments or ()),
        cwd=cwd,
        env=_sanitised_child_environment(),
        encoding="utf-8",
        encoding_error_handler="replace",
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            yield StdioMCPClient(session)
