from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

import pytest


def test_context_file_requires_and_returns_exact_instance(monkeypatch, tmp_path) -> None:
    import replaylab_agent.cli as cli

    context_file = tmp_path / "context.json"
    context_file.write_text(
        json.dumps({"capture": {"path": "C:/captures/frozen.rdc"}}),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        context_file=context_file,
        live_context=False,
        instance_id="",
    )
    with pytest.raises(ValueError, match="requires --instance-id"):
        cli._read_context(args)

    class FakeBridge:
        def select_instance(self, instance_id):
            assert instance_id == "qrd-1"
            return SimpleNamespace(
                id="qrd-1",
                capture_loaded=True,
                capture_path="C:/captures/frozen.rdc",
            )

    monkeypatch.setattr(cli, "BridgeClient", FakeBridge)
    args.instance_id = "qrd-1"
    context, instance_id, capture_path = cli._read_context(args)
    assert context["capture"]["path"] == capture_path
    assert instance_id == "qrd-1"
