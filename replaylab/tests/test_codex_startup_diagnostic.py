import os
from pathlib import Path
import sys

import pytest

from replaylab_agent.connectors.codex_app_server import _JsonlProcess
from replaylab_agent.errors import ConnectorError


@pytest.mark.asyncio
async def test_actual_early_cli_exit_retains_reason_without_credentials(tmp_path):
    secret = "test-secret-value-only"
    command = [sys.executable, "-c",
        "import sys;sys.stderr.write('Error loading configuration: unsupported field; credential test-secret-value-only\\n');sys.stderr.flush();sys.exit(1)"]
    process = _JsonlProcess(tuple(command), cwd=tmp_path, env=dict(os.environ, OPENAI_API_KEY=secret))
    try:
        with pytest.raises(ConnectorError) as error:
            await process.request("initialize", {}, timeout=5)
        assert "unsupported field" in str(error.value)
        assert "code 1" in str(error.value)
        assert secret not in str(error.value)
    finally:
        await process.close()
