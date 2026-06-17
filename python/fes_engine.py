#!/usr/bin/env python
"""Frozen-engine dispatcher: one PyInstaller exe, three sidecar entry points.

In a packaged release the three sidecars (`sidecar.py`, `image_process.py`,
`render_json.py`) are not shipped as loose scripts — they are frozen into a
single `fes-engine` executable behind this dispatcher. The Rust backend selects
the entry point with a leading subcommand:

    fes-engine generate    --image ... --stop-at ...      (-> sidecar.main)
    fes-engine ai          --image ... --api-key ...      (-> image_process.main)
    fes-engine render-json --json ...                     (-> render_json.main)

The subcommand is popped off argv before delegating, so each tool's own
argparse sees exactly the flags it already expects — identical to running the
loose script in dev mode.
"""
from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

# Keep the vendored `fd6/` package and the sibling sidecar modules importable in
# both a source run and a frozen run (harmless when PyInstaller already bundled
# them on the frozen import path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

_TOOLS = ("generate", "ai", "render-json")


def main() -> int:
    usage = f"usage: fes-engine <{'|'.join(_TOOLS)}> [args...]\n"
    if sys.argv[1:2] == ["-h"] or sys.argv[1:2] == ["--help"]:
        sys.stdout.write(usage)   # explicit help → stdout, exit 0 (conventional CLI)
        return 0
    if len(sys.argv) < 2:
        sys.stderr.write(usage)   # missing subcommand → stderr, exit 2
        return 2

    # Drop the subcommand so the delegated tool's argparse sees only its flags.
    cmd = sys.argv.pop(1)
    if cmd == "generate":
        from sidecar import main as run
    elif cmd == "ai":
        from image_process import main as run
    elif cmd == "render-json":
        from render_json import main as run
    else:
        sys.stderr.write(f"fes-engine: unknown subcommand {cmd!r} (expected one of {_TOOLS})\n")
        return 2
    return run()


if __name__ == "__main__":
    # MUST be the first thing in __main__: a frozen ProcessPoolExecutor worker
    # re-launches this exe; freeze_support() intercepts that bootstrap and runs
    # the worker (then exits) before any dispatch logic runs. Without it the
    # frozen CPU render path would recursively spawn the dispatcher.
    multiprocessing.freeze_support()
    sys.exit(main())
