"""Render an existing FD6 shape JSON to a PNG (for the GUI's "Import JSON").

Loads a `*_engine.json` document, materializes its shapes, and composites them
onto a canvas via fd6.shapegen.render — no generation, no source image needed.
Emits exactly one line of JSON on stdout (UTF-8):
  {"type":"done","png":"<base64 PNG>","width":W,"height":H,"shape_count":N}
  {"type":"error","message":"..."}

Invocation:
  python render_json.py --json PATH
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# Make the vendored `fd6` package importable regardless of the launch CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows pipes default to the locale code page; force UTF-8 so non-ASCII paths
# in any error message survive the stdout round-trip to Rust.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from fd6.io import load_json  # noqa: E402
from fd6.shapegen.render import render_shapes  # noqa: E402


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True)
    args = ap.parse_args()

    try:
        doc = load_json(args.json)
    except Exception as exc:
        emit({"type": "error", "message": f"无法解析 JSON：{type(exc).__name__}: {exc}"})
        return 1

    try:
        w, h = int(doc.image_size[0]), int(doc.image_size[1])
        shapes = doc.materialize_shapes()
        # Hybrid-base documents carry the under-paint as a base64 PNG; decode it
        # so the shapes composite over the same seed the engine used.
        base = None
        if getattr(doc, "base_image", ""):
            try:
                raw = base64.b64decode(doc.base_image)
                base = np.asarray(Image.open(io.BytesIO(raw)).convert("RGB"), dtype=np.uint8)
            except Exception:
                base = None
        if doc.sticker_mode:
            canvas = render_shapes(shapes, w, h, transparent_bg=True)        # RGBA
        else:
            # Default mode: rebuild the EXACT seed the engine composited over so
            # the reload pixel-matches the live render. The engine seeds grey-40
            # (or the hybrid under-paint) inside the fitted-image rect and the
            # chosen `background` in the buffer ring, then masks shapes out of
            # the buffer. Reproduce all three. Legacy docs (no background / rect)
            # fall back to a flat fill, exactly as they reloaded before.
            bg = doc.background if doc.background is not None else (40, 40, 40)
            rect = doc.image_rect
            if base is not None and base.shape[:2] == (h, w):
                seed = np.ascontiguousarray(base[:, :, :3]).astype(np.uint8).copy()
            else:
                seed = np.full((h, w, 3), bg, dtype=np.uint8)
                if rect is not None:
                    ox, oy, rw, rh = rect
                    seed[oy:oy + rh, ox:ox + rw] = (40, 40, 40)  # engine's image-region seed
            canvas = render_shapes(shapes, w, h, base=seed)
            # The engine never paints the buffer (composite ANDs the alpha mask),
            # so restore the clean canvas colour there instead of letting the
            # unmasked replay's edge spill show.
            if rect is not None:
                ox, oy, rw, rh = rect
                mask = np.ones((h, w), dtype=bool)
                mask[oy:oy + rh, ox:ox + rw] = False
                canvas[mask] = bg
        mode = "RGBA" if (canvas.ndim == 3 and canvas.shape[2] == 4) else "RGB"
        img = Image.fromarray(np.ascontiguousarray(canvas), mode)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:
        emit({"type": "error", "message": f"渲染失败：{type(exc).__name__}: {exc}"})
        return 1

    emit({"type": "done", "png": b64, "width": w, "height": h, "shape_count": int(doc.shape_count)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
