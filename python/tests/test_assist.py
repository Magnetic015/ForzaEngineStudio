"""Tests for the model-assist pipeline (fd6.shapegen.assist + Engine hooks).

Runnable two ways:
  * as a plain script:   python tests/test_assist.py     (from python/)
  * under pytest:        pytest python/tests/test_assist.py

The headline test (`test_assist_reaches_quality_with_fewer_shapes`) is the
deliverable's evidence: with model-assist ON, the engine reaches a comparable
or better *unweighted* RMS using only HALF the shape budget of a plain run —
i.e. fewer layers, equal-or-higher fidelity.
"""
from __future__ import annotations

import os

# Pin BLAS/OMP thread pools to 1 BEFORE numpy is imported. The engine's worker
# pool is forked on Linux, and fork()-ing a process with live OpenBLAS worker
# threads can segfault the child — pinning to a single thread makes the
# single-worker (max_threads=1) deterministic test path safe and reproducible
# across machines. Real multi-worker runs are unaffected.
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import base64
import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# Make the vendored `fd6` package importable when run as a bare script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fd6.shapegen import Engine, EngineConfig, Profile  # noqa: E402
from fd6.shapegen import assist as A  # noqa: E402
from fd6.shapegen.render import render_shapes  # noqa: E402
from fd6.shapegen.scoring import rms_error  # noqa: E402
from fd6.io import FD6Document  # noqa: E402


def _synthetic_image(w: int = 80, h: int = 80) -> np.ndarray:
    """A smooth two-axis gradient (cheap to flatten) plus a few sharp,
    high-contrast detail blobs (the precision that must be preserved)."""
    yy, xx = np.indices((h, w)).astype(np.float32)
    r = (40 + 120 * xx / w).astype(np.uint8)
    g = (60 + 120 * yy / h).astype(np.uint8)
    b = (np.full((h, w), 90) + 60 * np.sin(xx / 6.0)).clip(0, 255).astype(np.uint8)
    img = np.dstack([r, g, b]).astype(np.uint8)
    # sharp detail features
    img[12:20, 12:20] = (255, 255, 255)
    img[h - 22:h - 10, w - 22:w - 10] = (10, 10, 10)
    img[h // 2 - 3:h // 2 + 3, 5:w - 5] = (230, 20, 20)
    return img


def _high_freq_image(w: int = 80, h: int = 80) -> np.ndarray:
    """The gradient base plus heavy high-frequency texture (per-pixel noise + a
    fine checker). simplify_for_render's purpose is to cut high-frequency
    content, so its benefit is only demonstrable where such content exists.

    A purely SMOOTH target is no longer a valid fixture for that: per-shape alpha
    now reconstructs a smooth gradient better than its posterized (banded) form,
    so flattening a smooth image adds band edges and hurts. simplify still wins
    clearly on textured content — its actual use (real photos)."""
    rng = np.random.RandomState(3)
    base = _synthetic_image(w, h).astype(np.int32)
    base += rng.randint(-55, 55, (h, w, 3))
    yy, xx = np.indices((h, w))
    base += (((xx // 2 + yy // 2) % 2) * 40 - 20)[:, :, None]
    return base.clip(0, 255).astype(np.uint8)


def _fixed_profile(stop_at: int) -> Profile:
    # max_threads=1 forces a single worker → deterministic across machines.
    return Profile(
        name="test", stop_at=stop_at, max_resolution=80, preview_every=0,
        compute_backend="cpu", random_samples=160, mutated_samples=80, max_threads=1,
    )


def _run(target, stop_at, *, base=None, importance=None, seed=7):
    """Run the engine to completion and return (engine, final_canvas, rms).

    The final canvas is captured from the `done` event — a safe copy made
    before the engine's `finally` block unlinks the canvas shared memory.
    Reading `engine.canvas` after `run()` would dereference freed memory.
    """
    eng = Engine(target, EngineConfig(profile=_fixed_profile(stop_at), seed=seed),
                 base_canvas=base, importance_map=importance)
    final = None
    for ev in eng.run():
        if ev.kind == "done" and ev.canvas is not None:
            final = np.ascontiguousarray(ev.canvas[:, :, :3]).copy()
    assert final is not None, "engine produced no done event"
    # Apples-to-apples fidelity: plain unweighted RMS, independent of any
    # importance-map weighting used during the search.
    return eng, final, float(rms_error(final, target))


# ── primitives ───────────────────────────────────────────────────────────────

def test_simplify_reduces_unique_colors():
    img = _synthetic_image()
    simp = A.simplify_for_render(img, levels=8)
    assert simp.shape == img.shape and simp.dtype == np.uint8
    before = np.unique(img.reshape(-1, 3), axis=0).shape[0]
    after = np.unique(simp.reshape(-1, 3), axis=0).shape[0]
    assert after < before, f"simplify should cut color count ({after} !< {before})"


def test_saliency_importance_shape_and_finiteness():
    img = _synthetic_image()
    imp = A.saliency_importance(img)
    assert imp.shape == img.shape[:2] and imp.dtype == np.float32
    assert np.isfinite(imp).all() and imp.min() >= 0.0
    # The sharp white corner must out-weight a flat gradient interior pixel.
    assert imp[15, 15] > imp[40, 40]


def test_build_base_canvas_is_close_lowfreq():
    img = _synthetic_image()
    base = A.build_base_canvas(img, downscale=8)
    assert base.shape == img.shape and base.dtype == np.uint8
    # A low-frequency base is already a coarse approximation of the target.
    assert rms_error(base, img) < rms_error(np.zeros_like(img), img)


# ── engine integration ───────────────────────────────────────────────────────

def test_base_seed_lowers_initial_error():
    img = _synthetic_image()
    plain = Engine(img, EngineConfig(profile=_fixed_profile(1), seed=1))
    seeded = Engine(img, EngineConfig(profile=_fixed_profile(1), seed=1),
                    base_canvas=A.build_base_canvas(img, downscale=8))
    assert seeded.start_rms < plain.start_rms, "base seed should start closer to target"


def test_assist_reaches_quality_with_fewer_shapes():
    """Headline: hybrid base + saliency guidance reaches better fidelity with
    HALF the layers than a plain run at double the budget.

    Measured as plain *unweighted* RMS against the original target, so the
    importance weighting can't flatter the number — this is an honest
    apples-to-apples fidelity comparison at a 2× layer advantage.
    """
    img = _synthetic_image()
    half, full = 20, 40

    _, _, rms_plain = _run(img, full)                       # 40 layers, no assist

    base = A.build_base_canvas(img, downscale=8)
    imp = A.saliency_importance(img)
    _, _, rms_assist = _run(img, half, base=base, importance=imp)  # 20 layers + assist

    assert rms_assist < rms_plain, (
        f"assist@{half} layers (rms={rms_assist:.3f}) should beat "
        f"plain@{full} layers (rms={rms_plain:.3f})"
    )


def test_simplify_is_easier_to_reproduce():
    """Render-optimization: flattening a HIGH-FREQUENCY target into clean
    flat-colour regions makes it cheaper to paint than the noisy original at the
    same layer budget.

    Each run's RMS is measured against its OWN target, so a lower number means
    the simplified image is genuinely cheaper to paint — the same fidelity is
    reachable in fewer layers (each shape is one Forza layer).

    Uses a textured fixture deliberately (see _high_freq_image): the engine's
    per-shape alpha now reconstructs smooth gradients better than a posterized
    version of them, so simplify only helps where there is high-frequency
    content to remove — which is exactly its real-world target (photos).
    """
    img = _high_freq_image()
    simp = A.simplify_for_render(img, levels=8)
    _, _, rms_orig = _run(img, 40)     # reconstruct original, rms vs original
    _, _, rms_simp = _run(simp, 40)    # reconstruct simplified, rms vs simplified
    assert rms_simp < rms_orig, (
        f"simplified target should reconstruct more faithfully at equal budget "
        f"(simplified={rms_simp:.3f}, original={rms_orig:.3f})"
    )


def test_json_base_image_roundtrips():
    """A document carrying a base under-paint reloads to the same pixels."""
    img = _synthetic_image()
    base = A.build_base_canvas(img, downscale=8)
    eng, final_canvas, _ = _run(img, 20, base=base)

    buf = io.BytesIO()
    Image.fromarray(base, "RGB").save(buf, format="PNG")
    base_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    doc = FD6Document.from_engine(
        source_image="t.png", image_size=(img.shape[1], img.shape[0]),
        shapes=eng.shapes, base_image=base_b64, assist={"base": {"source": "local"}},
    )
    reloaded = FD6Document.from_dict(doc.to_dict())
    assert reloaded.base_image == base_b64 and reloaded.assist["base"]["source"] == "local"

    raw = base64.b64decode(reloaded.base_image)
    decoded = np.asarray(Image.open(io.BytesIO(raw)).convert("RGB"), dtype=np.uint8)
    rendered = render_shapes(reloaded.materialize_shapes(), img.shape[1], img.shape[0],
                             background=(40, 40, 40), base=decoded)
    # Reproduces the engine's own final canvas exactly (same base + shapes).
    assert np.array_equal(rendered, final_canvas)


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
