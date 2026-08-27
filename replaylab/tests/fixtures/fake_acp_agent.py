from __future__ import annotations

import asyncio
import os
import sys

from acp import PROTOCOL_VERSION, PromptResponse, run_agent, text_block
from acp import schema


class EchoAgent:
    def on_connect(self, connection) -> None:
        self.connection = connection

    async def initialize(self, protocol_version, **kwargs):
        return schema.InitializeResponse(
            protocolVersion=min(protocol_version, PROTOCOL_VERSION),
            agentInfo=schema.Implementation(name="fake-acp", version="1"),
        )

    async def new_session(self, cwd, mcp_servers=None, **kwargs):
        assert cwd
        if "no-mcp" in sys.argv:
            assert not mcp_servers
            return schema.NewSessionResponse(sessionId="session-test")
        assert mcp_servers and mcp_servers[0].name == "ReplayLab"
        if "filtered" in sys.argv:
            server = mcp_servers[0]
            assert server.command == sys.executable
            assert "replaylab_agent.mcp_proxy" in server.args
            assert server.args.count("--allow") == 1
            assert "get_pipeline_state" in server.args
            assert "--instance-id" in server.args
            assert "--registry-path" in server.args
        return schema.NewSessionResponse(sessionId="session-test")

    async def prompt(self, session_id, prompt, **kwargs):
        assert session_id == "session-test"
        if "exit" in sys.argv:
            os._exit(23)
        text = prompt[0].text
        chunks = ["x" * 8192] * 129 if "many-deltas" in sys.argv else ["ACP: " + text]
        for chunk in chunks:
            await self.connection.session_update(
                session_id,
                schema.AgentMessageChunk(
                    content=text_block(chunk),
                    sessionUpdate="agent_message_chunk",
                ),
            )
        return PromptResponse(stopReason="refusal" if "refusal" in sys.argv else "end_turn")

    async def cancel(self, session_id, **kwargs):
        return None


asyncio.run(run_agent(EchoAgent()))
