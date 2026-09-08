"""Exercise the frozen AgentHost filter -> frozen ReplayLabMCP stdio route."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
import tempfile
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from replaylab_agent.policy import EFFECTFUL_TOOLS, READ_ONLY_TOOLS  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser


def _regular_file(path: Path) -> Path:
    resolved = path.resolve()
    details = resolved.lstat()
    if not stat.S_ISREG(details.st_mode) or resolved.is_symlink():
        raise ValueError("probe executable must be an unlinked regular file")
    return resolved


async def _probe(agent: Path, host: Path, timeout: float) -> dict[str, object]:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    allowed = READ_ONLY_TOOLS | EFFECTFUL_TOOLS
    with tempfile.TemporaryDirectory(prefix="replaylab-frozen-mcp-probe-") as temporary:
        registry = Path(temporary) / "live-edit-registry.json"
        arguments = [
            "-I",
            "-m",
            "replaylab_agent.mcp_proxy",
            "--host",
            str(host),
            "--host-arg",
            "stdio",
            "--instance-id",
            "packaging-route-probe",
            "--registry-path",
            str(registry),
            "--max-tool-calls",
            "1",
        ]
        for name in sorted(allowed):
            arguments.extend(("--allow", name))
        for effect in ("workspace", "project", "ui", "replay"):
            arguments.extend(("--effect", effect))

        parameters = StdioServerParameters(
            command=str(agent),
            args=arguments,
            env=dict(os.environ),
        )

        async def connect_and_list() -> dict[str, object]:
            async with stdio_client(parameters) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    return {str(tool.name): tool for tool in listing.tools}

        return await asyncio.wait_for(connect_and_list(), timeout=timeout)


def main() -> int:
    args = _parser().parse_args()
    if args.timeout <= 0 or args.timeout > 300:
        sys.stderr.write("frozen-mcp-route-probe:invalid-timeout\n")
        return 2
    try:
        agent = _regular_file(args.agent)
        host = _regular_file(args.host)
        tools = asyncio.run(_probe(agent, host, args.timeout))
        observed = set(tools)
        expected = READ_ONLY_TOOLS | EFFECTFUL_TOOLS
        if observed != expected:
            missing = sorted(expected - observed)
            unexpected = sorted(observed - expected)
            sys.stderr.write(
                "frozen-mcp-route-probe:catalog-mismatch:"
                + json.dumps(
                    {"missing": missing, "unexpected": unexpected},
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            return 3
        invalid_annotations = []
        for name, tool in tools.items():
            annotations = getattr(tool, "annotations", None)
            read_only = name in READ_ONLY_TOOLS
            if (
                annotations is None
                or getattr(annotations, "readOnlyHint", None) is not read_only
                or getattr(annotations, "destructiveHint", None) is not (not read_only)
                or getattr(annotations, "idempotentHint", None) is not read_only
                or getattr(annotations, "openWorldHint", None) is not False
            ):
                invalid_annotations.append(name)
        if invalid_annotations:
            sys.stderr.write(
                "frozen-mcp-route-probe:annotation-mismatch:"
                + json.dumps(sorted(invalid_annotations), separators=(",", ":"))
                + "\n"
            )
            return 6
    except asyncio.TimeoutError:
        sys.stderr.write("frozen-mcp-route-probe:timeout\n")
        return 4
    except Exception as exc:
        sys.stderr.write(
            "frozen-mcp-route-probe:failed:" + type(exc).__name__ + "\n"
        )
        return 5

    print(
        json.dumps(
            {
                "schema": "replaylab-frozen-mcp-route-probe/v1",
                "status": "ok",
                "toolCount": len(observed),
                "annotatedToolCount": len(tools),
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
