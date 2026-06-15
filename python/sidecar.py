"""FES engine sidecar — reuse the fd6 shapegen engine, stream preview frames to stdout.

Protocol: one JSON object per line on stdout (UTF-8, flushed):
  {"type":"meta","width":W,"height":H}
  {"type":"backend","message":"CPU"}
  {"type":"progress","shape_count":n,"total":t,"rms":r}
  {"type":"frame","shape_count":n,"total":t,"rms":r,"png":"<base64 PNG>"}
  {"type":"done","shape_count":n,"rms":r,"png":"<base64>","json_path":"...","message":"..."}
  {"type":"error","message":"..."}

One-shot invocation:
  python sidecar.py --image PATH --stop-at N [--max-resolution N] [--sticker] [--seed N] [--out PATH]
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import multiprocessing
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# Windows pipes default to the locale code page (e.g. GBK), which corrupts
# non-ASCII paths (Chinese filenames) when Rust decodes stdout as UTF-8.
# Force UTF-8 so the emitted JSON (incl. json_path) survives intact.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Make the vendored `fd6` package importable regardless of the launch CWD
# (this script sits next to the `fd6/` directory).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fd6.shapegen import Engine, EngineConfig, Profile  # noqa: E402
from fd6.io import FD6Document, save_json  # noqa: E402


def emit(obj: dict) -> None:
    """Write one line-delimited JSON event and flush so Rust sees it immediately."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def encode_png(canvas: np.ndarray) -> str:
    """uint8 (H,W,3) or (H,W,4) ndarray -> base64-encoded PNG string."""
    mode = "RGBA" if (canvas.ndim == 3 and canvas.shape[2] == 4) else "RGB"
    img = Image.fromarray(canvas, mode)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def preprocess(image_path: str, sticker_mode: bool, canvas_w: int, canvas_h: int):
    """Qt-free preprocessing that fits the image into an explicit W×H canvas.

    Alpha handling is unchanged (sticker keeps transparency; non-sticker
    composites onto white). The image is then scaled to fit inside the requested
    canvas — aspect ratio preserved, with an 8% buffer ring so shapes near the
    subject's edge aren't clipped — and centered. Returns
    (canvas HxWx3 uint8, alpha_mask HxW uint8: 255 over the fitted image / the
    silhouette in sticker mode, 0 in the surrounding buffer so the engine
    ignores it).
    """
    img = Image.open(image_path)
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    alpha_mask = None
    if has_alpha:
        rgba = img.convert("RGBA")
        if sticker_mode:
            arr = np.asarray(rgba, dtype=np.uint8)
            img = Image.fromarray(arr[:, :, :3], "RGB")
            alpha_mask = arr[:, :, 3].copy()
        else:
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[3])
            img = bg
    else:
        img = img.convert("RGB")

    # Fit the image into the requested canvas, preserving aspect ratio, leaving an
    # 8% buffer ring; center it.
    cw, ch = max(1, int(canvas_w)), max(1, int(canvas_h))
    BUFFER_FRAC = 0.08
    avail_w = max(1, int(round(cw * (1 - 2 * BUFFER_FRAC))))
    avail_h = max(1, int(round(ch * (1 - 2 * BUFFER_FRAC))))
    src_w, src_h = img.size
    scale = min(avail_w / src_w, avail_h / src_h)
    nw, nh = max(1, int(round(src_w * scale))), max(1, int(round(src_h * scale)))
    img = img.resize((nw, nh), Image.LANCZOS)
    ox, oy = (cw - nw) // 2, (ch - nh) // 2

    if alpha_mask is not None:
        am = np.asarray(Image.fromarray(alpha_mask, "L").resize((nw, nh), Image.LANCZOS), dtype=np.uint8)
    else:
        am = np.full((nh, nw), 255, dtype=np.uint8)

    canvas_img = Image.new("RGB", (cw, ch), (255, 255, 255))
    canvas_img.paste(img, (ox, oy))
    full_alpha = np.zeros((ch, cw), dtype=np.uint8)
    full_alpha[oy:oy + nh, ox:ox + nw] = am
    alpha_mask = full_alpha

    target = np.asarray(canvas_img, dtype=np.uint8)
    return target, alpha_mask


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--stop-at", type=int, default=3000)
    ap.add_argument("--canvas-width", type=int, default=1000)
    ap.add_argument("--canvas-height", type=int, default=1000)
    ap.add_argument("--sticker", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="")
    ap.add_argument("--backend", default="gpu", choices=["gpu", "cpu", "auto"])
    args = ap.parse_args()

    try:
        target, alpha_mask = preprocess(args.image, args.sticker, args.canvas_width, args.canvas_height)
    except Exception as exc:
        emit({"type": "error", "message": f"preprocess failed: {type(exc).__name__}: {exc}"})
        return 1

    h, w = target.shape[:2]
    emit({"type": "meta", "width": int(w), "height": int(h)})

    stop_at = max(1, args.stop_at)
    # Frequent preview frames so the canvas visibly fills in from the start.
    # Cap near ~150 frames/run; min 1 so the very first shapes already stream.
    preview_every = max(1, stop_at // 150)
    progress_every = max(1, stop_at // 300)   # smooth, lightweight progress bar

    profile = Profile(
        name="balanced",
        stop_at=stop_at,
        max_resolution=max(args.canvas_width, args.canvas_height),
        preview_every=preview_every,
        compute_backend=args.backend,         # default GPU (OpenCL); engine auto-falls back to CPU if unavailable
        shape_types=["rotated_ellipse"],
    )

    try:
        engine = Engine(target, EngineConfig(profile=profile, seed=args.seed), alpha_mask)
    except Exception as exc:
        emit({"type": "error", "message": f"engine init failed: {type(exc).__name__}: {exc}"})
        return 1

    last_progress = 0
    try:
        for ev in engine.run():
            if ev.kind == "backend":
                emit({"type": "backend", "message": ev.message})
            elif ev.kind == "shape_committed":
                if ev.shape_count - last_progress >= progress_every:
                    last_progress = ev.shape_count
                    emit({"type": "progress", "shape_count": ev.shape_count,
                          "total": stop_at, "rms": round(float(ev.rms), 4)})
            elif ev.kind == "preview" and ev.canvas is not None:
                emit({"type": "frame", "shape_count": ev.shape_count, "total": stop_at,
                      "rms": round(float(ev.rms), 4), "png": encode_png(ev.canvas)})
            elif ev.kind == "error":
                emit({"type": "error", "message": ev.message})
                return 1
            elif ev.kind == "done":
                src = Path(args.image)
                out_path = Path(args.out) if args.out else src.with_name(src.stem + "_engine.json")
                try:
                    doc = FD6Document.from_engine(
                        source_image=src.name,
                        image_size=(int(w), int(h)),
                        shapes=engine.shapes,
                        profile_name=profile.name,
                        sticker_mode=args.sticker,
                    )
                    save_json(doc, out_path)
                    json_path = str(out_path)
                except Exception as exc:
                    json_path = f"(save failed: {exc})"
                emit({"type": "done", "shape_count": ev.shape_count,
                      "rms": round(float(ev.rms), 4),
                      "png": encode_png(ev.canvas) if ev.canvas is not None else None,
                      "json_path": json_path, "message": ev.message})
                return 0
    except Exception as exc:
        emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        return 1
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()   # required: ProcessPoolExecutor uses spawn on Windows
    sys.exit(main())
