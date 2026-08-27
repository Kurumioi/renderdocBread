from __future__ import annotations

import os

from replaylab_agent.doctor import _is_packaged_windows_executable


def test_windowsapps_executable_is_not_reported_as_launchable() -> None:
    path = r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0_x64\codex.exe"
    assert _is_packaged_windows_executable(path) is (os.name == "nt")
