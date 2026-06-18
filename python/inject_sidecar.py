"""FES injection sidecar — write a rendered shape design into a running Forza game.

Faithfully replicates FD6's injection chain (fd6/gui/inject_worker.py) for the
Tauri app: it drives the ported `fd6.inject` package (zero third-party deps) and
streams progress as line-delimited JSON on stdout, instead of Qt signals.

Protocol: one JSON object per line on stdout (UTF-8, flushed):
  {"type":"status","message":"...","severity":"info|success|warning|error"}
  {"type":"scan","scanned":s,"total":t,"hits":h}     # memory-region scan progress
  {"type":"write","written":w,"total":n}             # per-shape write progress
  {"type":"done","success":bool,"shapes_written":n,"message":"..."}
  {"type":"error","message":"..."}

One-shot invocation:
  python inject_sidecar.py --json PATH [--profile fh6]

The JSON is an FD6 shape document; only its top-level `shapes` array (a list of
shape dicts) is consumed — exactly the input contract `FH6Injector.inject()`
expects. No numpy / PIL / fd6.shapegen import, so the injection path stays the
dependency-free core the porting guide describes.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows pipes default to the locale code page (e.g. GBK), which corrupts
# non-ASCII paths (Chinese filenames) when Rust decodes stdout as UTF-8.
# Force UTF-8 so the emitted JSON survives intact (mirrors sidecar.py).
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Make the vendored `fd6` package importable regardless of the launch CWD
# (this script sits next to the `fd6/` directory).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fd6.inject import FH6Injector  # noqa: E402
from fd6.inject.game_profiles import get_profile  # noqa: E402


def emit(obj: dict) -> None:
    """Write one line-delimited JSON event and flush so Rust sees it immediately."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def status(message: str, severity: str = "info") -> None:
    emit({"type": "status", "message": message, "severity": severity})


def _load_shapes(json_path: str) -> list[dict]:
    """Read the top-level `shapes` array from an FD6 shape document.

    Only the shapes list is needed for injection (see the porting guide §4), so
    we read it directly with the standard library rather than materializing the
    full FD6Document (which would pull in numpy via fd6.shapegen).
    """
    raw = json.loads(Path(json_path).read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("document root is not a JSON object")
    shapes = raw.get("shapes")
    if not isinstance(shapes, list) or not shapes:
        raise ValueError("document has no 'shapes' array")
    return shapes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True, help="FD6 shape JSON to inject")
    ap.add_argument("--profile", default="fh6",
                    help="game profile key (fh6 [default] / fh5 / fh4 / fh3)")
    args = ap.parse_args()

    try:
        profile = get_profile(args.profile)
    except ValueError as exc:
        # Don't silently fall back to FH6 — injecting into the wrong game's
        # process is exactly the kind of mistake the explicit profile guards.
        emit({"type": "error", "message": f"Unknown game profile: {exc}"})
        return 1

    try:
        shapes = _load_shapes(args.json)
    except Exception as exc:
        emit({"type": "error", "message": f"Could not load JSON: {type(exc).__name__}: {exc}"})
        return 1

    n_shapes = len(shapes)
    status(f"Loaded {n_shapes} shapes from {Path(args.json).name}.", "info")
    if profile.beta:
        status(f"⚠ BETA target: {profile.label}. {profile.beta_note}", "warning")

    inj = FH6Injector(profile=profile)
    try:
        status(f"Attaching to {profile.label}…", "info")
        inj.attach()
        status(
            f"Attached. Scanning memory for the {n_shapes}-layer LiveryGroup template…",
            "info",
        )
        # Kick the UI out of "preparing" immediately, before real region progress.
        emit({"type": "scan", "scanned": 0, "total": 1, "hits": 0})

        def on_scan(scanned: int, total: int, hits: int) -> None:
            emit({"type": "scan", "scanned": int(scanned), "total": int(total), "hits": int(hits)})

        def on_write(written: int, total: int) -> None:
            emit({"type": "write", "written": int(written), "total": int(total)})

        def on_phase(msg: str) -> None:
            status(msg, "warning")

        handle = inj.find_active_vinyl_group(
            progress_cb=on_scan,
            layer_count=n_shapes,
            status_cb=on_phase,
        )
        slots = handle.layer_count
        if n_shapes > slots:
            emit({"type": "done", "success": False, "shapes_written": 0,
                  "message": (f"Template has {slots} shape slots but JSON has {n_shapes}. "
                              f"Load a larger template (e.g. an {n_shapes}-sphere vinyl group) "
                              f"and re-inject.")})
            return 0

        status(f"Found {slots} shape slots. Writing {n_shapes} shapes…", "info")
        result = inj.inject(shapes, handle, progress_cb=on_write)
        emit({"type": "done", "success": bool(result.success),
              "shapes_written": int(result.shapes_written), "message": result.message})
        return 0
    except Exception as exc:
        emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        return 1
    finally:
        try:
            inj.detach()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
