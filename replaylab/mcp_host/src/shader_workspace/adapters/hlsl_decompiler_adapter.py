"""Fixed file-output adapter for 3DMigoto CMD_Decompiler.

CMD_Decompiler derives ``.hlsl`` beside its input instead of accepting an
output path.  The shader tool registry requires every operation to name its
exact job output, so this small adapter bridges that interface without a shell.
It is intended to be SHA-256 pinned together with CMD_Decompiler.exe.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args(argv)
    tool = Path(arguments.tool).resolve(strict=True)
    source = Path(arguments.input).resolve(strict=True)
    output = Path(arguments.output).resolve()
    if tool.is_symlink() or not tool.is_file():
        raise RuntimeError("CMD_Decompiler must be a regular non-symlink file")
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("input must be a regular non-symlink file")
    if output.exists() or output.is_symlink():
        raise RuntimeError("refusing to overwrite adapter output")
    derived = source.with_suffix(".hlsl")
    if derived.exists() or derived.is_symlink():
        raise RuntimeError("refusing to overwrite CMD_Decompiler derived output")
    completed = subprocess.run(
        [str(tool), "-D", "-V", "--stop-on-failure", str(source)],
        cwd=str(source.parent),
        stdin=subprocess.DEVNULL,
        stdout=None,
        stderr=None,
        shell=False,
        env=dict(os.environ),
        check=False,
    )
    if completed.returncode != 0:
        return int(completed.returncode or 1)
    if derived.is_symlink() or not derived.is_file() or derived.stat().st_size <= 0:
        raise RuntimeError("CMD_Decompiler did not produce its expected .hlsl file")
    output.parent.mkdir(parents=True, exist_ok=True)
    os.replace(derived, output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("adapter error: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        raise SystemExit(1)
