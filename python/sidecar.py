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
from fd6.shapegen import assist as fes_assist  # noqa: E402
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
    # ── Model-assist (fewer layers, more detail). `--assist` turns on all three
    # assists; each can be toggled off with its --no-* form. External image-model
    # assets (a flattened under-paint / a saliency map) override the local ones.
    ap.add_argument("--assist", action="store_true",
                    help="enable model-assist (render-optimize + hybrid base + saliency guidance)")
    ap.add_argument("--assist-simplify", action=argparse.BooleanOptionalAction, default=True,
                    help="flatten the target into clean flat-color regions before rendering")
    ap.add_argument("--assist-base", action=argparse.BooleanOptionalAction, default=True,
                    help="seed the canvas with a low-frequency under-paint (hybrid render)")
    ap.add_argument("--assist-importance", action=argparse.BooleanOptionalAction, default=True,
                    help="bias shape placement with a saliency/structure importance map")
    ap.add_argument("--assist-levels", type=int, default=12,
                    help="posterize level count for render-optimization / base")
    ap.add_argument("--base-image", default="",
                    help="external under-paint image (e.g. an image-model flattened render)")
    ap.add_argument("--importance-map", default="",
                    help="external saliency/structure map (grayscale, bright = important)")
    args = ap.parse_args()

    try:
        target, alpha_mask = preprocess(args.image, args.sticker, args.canvas_width, args.canvas_height)
    except Exception as exc:
        emit({"type": "error", "message": f"preprocess failed: {type(exc).__name__}: {exc}"})
        return 1

    h, w = target.shape[:2]
    emit({"type": "meta", "width": int(w), "height": int(h)})

    # ── Build model-assist inputs ────────────────────────────────────────────
    # Any external asset implies assist even without the master flag. Each piece
    # degrades to the local numpy approximation when no external asset is given.
    use_assist = args.assist or bool(args.base_image) or bool(args.importance_map)
    base_canvas = None
    importance_map = None
    assist_meta: dict = {}
    if use_assist:
        # Build every assist against a working copy and only commit it to
        # `target` after ALL enabled assists succeed. Otherwise a later failure
        # (e.g. a bad external --base-image) would leave `target` already
        # simplified while the except-path reports a plain fallback — rendering
        # a silently posterized/recolored image with no assist metadata.
        assisted_target = target
        try:
            if args.assist_simplify:
                assisted_target = fes_assist.simplify_for_render(target, alpha_mask, levels=args.assist_levels)
                assist_meta["simplify"] = {"levels": int(args.assist_levels)}
            if args.assist_base or args.base_image:
                # The hybrid base is intentionally skipped in sticker mode: the
                # JSON carries no per-pixel silhouette alpha, so an Import-JSON
                # reload (which composites shapes over a transparent backdrop)
                # could not reproduce a base-seeded RGBA result. Simplify +
                # importance still apply and stay fully reproducible because the
                # output is shapes only.
                if args.sticker:
                    emit({"type": "log", "message": "hybrid base skipped in sticker mode (keeps Import-JSON reproducible)"})
                elif args.base_image:
                    base_canvas = fes_assist.base_canvas_from_image(args.base_image, (h, w), alpha_mask)
                    assist_meta["base"] = {"source": "external"}
                else:
                    base_canvas = fes_assist.build_base_canvas(assisted_target, alpha_mask, levels=args.assist_levels)
                    assist_meta["base"] = {"source": "local"}
            if args.assist_importance or args.importance_map:
                if args.importance_map:
                    importance_map = fes_assist.importance_from_image(args.importance_map, (h, w), alpha_mask)
                    assist_meta["importance"] = {"source": "external"}
                else:
                    importance_map = fes_assist.saliency_importance(assisted_target, alpha_mask)
                    assist_meta["importance"] = {"source": "local"}
            # All enabled assists succeeded — now it's safe to adopt the target.
            target = assisted_target
        except Exception as exc:
            # Assist is best-effort — never block a render over it. Fall back to
            # the plain pipeline (original `target` untouched) and tell the UI.
            base_canvas = None
            importance_map = None
            assist_meta = {}
            emit({"type": "log", "message": f"assist disabled ({type(exc).__name__}: {exc})"})
        if assist_meta:
            emit({"type": "assist", "applied": assist_meta})

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
        engine = Engine(
            target, EngineConfig(profile=profile, seed=args.seed), alpha_mask,
            base_canvas=base_canvas, importance_map=importance_map,
        )
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
                # Persist the hybrid under-paint so an Import-JSON reload paints
                # the shapes over the same seed. base_canvas is only ever set in
                # non-sticker runs (sticker seeding is skipped above for
                # reproducibility), so this is implicitly non-sticker.
                base_b64 = encode_png(base_canvas) if base_canvas is not None else ""
                try:
                    doc = FD6Document.from_engine(
                        source_image=src.name,
                        image_size=(int(w), int(h)),
                        shapes=engine.shapes,
                        profile_name=profile.name,
                        sticker_mode=args.sticker,
                        base_image=base_b64,
                        assist=assist_meta,
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
