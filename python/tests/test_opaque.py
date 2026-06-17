"""Tests for game-faithful OPAQUE mode (Profile.opaque).

The livery injector (ds_v9 `writer.rs`) draws every shape as a SOLID layer: it
writes RGBA with alpha forced to 255 and seeds no under-paint, so the in-game
render is an opaque painter's-algorithm stack. These tests pin the two
properties that make the saved JSON reproduce in-game (and the preview WYSIWYG):

  1. every committed shape is opaque (color alpha == 255), and
  2. replaying those shapes with an INDEPENDENT opaque painter (mirroring the
     injector: solid fill, topmost wins) reproduces the engine's own canvas.

Runnable as a script (`python tests/test_opaque.py` from python/) or via pytest.
"""
from __future__ import annotations

import os

# Pin BLAS/OMP pools to 1 before numpy import — fork()+live OpenBLAS threads can
# segfault the single-worker deterministic test path (mirrors test_assist.py).
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fd6.shapegen import Engine, EngineConfig, Profile  # noqa: E402
from fd6.shapegen.shapes import Shape  # noqa: E402


def _gradient_image(w: int = 64, h: int = 64) -> np.ndarray:
    """Smooth gradient + a couple of hard blobs — translucency is tempting here,
    so it's a fair fixture for proving opaque mode really forces solid shapes."""
    yy, xx = np.indices((h, w)).astype(np.float32)
    r = (30 + 200 * xx / w).astype(np.uint8)
    g = (40 + 180 * yy / h).astype(np.uint8)
    b = (np.full((h, w), 120) + 80 * np.sin(xx / 7.0)).clip(0, 255).astype(np.uint8)
    img = np.dstack([r, g, b]).astype(np.uint8)
    img[8:18, 8:18] = (255, 255, 255)
    img[h - 18:h - 6, w - 18:w - 6] = (12, 12, 12)
    return img


def _profile(stop_at: int, *, opaque: bool, refit: bool = False) -> Profile:
    # max_threads=1 → single deterministic worker; CPU backend for reproducibility.
    return Profile(
        name="test", stop_at=stop_at, max_resolution=64, preview_every=0,
        compute_backend="cpu", random_samples=140, mutated_samples=70,
        max_threads=1, opaque=opaque, refit_final=refit,
    )


def _run(profile: Profile, target: np.ndarray, seed: int = 5):
    """Run to completion; return (engine, final_canvas_rgb)."""
    eng = Engine(target, EngineConfig(profile=profile, seed=seed))
    final = None
    for ev in eng.run():
        if ev.kind == "done" and ev.canvas is not None:
            final = np.ascontiguousarray(ev.canvas[:, :, :3]).copy()
    assert final is not None, "engine produced no done event"
    return eng, final


def _opaque_paint(shapes: list[Shape], seed_canvas: np.ndarray) -> np.ndarray:
    """Independent opaque painter's-algorithm — the injector's model: each shape
    is a SOLID fill of its (binary) mask in list order, topmost wins. No alpha,
    no blending. Mirrors writer.rs (alpha forced 255) so a match proves the saved
    shapes reproduce in-game exactly.

    Exact equality with the engine's float composite holds because ellipse masks
    are strictly binary (0/255) and this fixture has no alpha mask; an AA shape
    type or a sticker (alpha-masked) run would need this painter to clip likewise."""
    h, w = seed_canvas.shape[:2]
    canvas = seed_canvas.copy()
    for s in shapes:
        mask_local, (x0, y0, x1, y1) = s.rasterize_mask(w, h)
        if x1 <= x0 or y1 <= y0 or mask_local.size == 0:
            continue
        body = mask_local > 0
        region = canvas[y0:y1, x0:x1]
        region[body] = np.array(s.color[:3], dtype=np.uint8)
    return canvas


def test_opaque_mode_forces_alpha_255():
    """Every shape an opaque run commits is solid — the alpha the injector keeps."""
    eng, _ = _run(_profile(40, opaque=True), _gradient_image())
    assert eng.shapes, "engine committed no shapes"
    alphas = {s.color[3] for s in eng.shapes}
    assert alphas == {255}, f"opaque run must commit only alpha=255, got {sorted(alphas)}"


def test_translucent_mode_still_uses_alpha():
    """Sanity that the flag matters: the legacy path still picks sub-255 alpha on
    a smooth gradient (so opaque mode is a real, behavioural change, not a no-op)."""
    eng, _ = _run(_profile(40, opaque=False), _gradient_image())
    assert any(s.color[3] != 255 for s in eng.shapes), \
        "translucent run should pick at least one non-opaque shape on a gradient"


def test_opaque_canvas_matches_injector_render():
    """WYSIWYG: replaying the committed shapes with an independent opaque painter
    (the injector's solid-fill model) reproduces the engine's final canvas exactly.
    refit_final stays OFF so the comparison is the pure forward opaque stack."""
    eng, final = _run(_profile(40, opaque=True, refit=False), _gradient_image())
    replay = _opaque_paint(eng.shapes, eng._seed_canvas[:, :, :3])
    assert np.array_equal(replay, final), (
        "opaque engine canvas must equal an independent solid-fill replay "
        f"(max abs diff {int(np.abs(replay.astype(int) - final.astype(int)).max())})"
    )


def test_opaque_seed_shapes_forces_solid():
    """Resume path honours opaque too: a seeded translucent shape is stored solid
    (else a resumed run would save sub-255 alpha the injector silently drops)."""
    from fd6.shapegen.shapes.ellipse import RotatedEllipse
    eng = Engine(_gradient_image(), EngineConfig(profile=_profile(1, opaque=True), seed=1))
    try:
        eng.seed_shapes([RotatedEllipse(color=(200, 50, 50, 90), x=20, y=20, rx=10, ry=10, angle=0)])
        assert eng.shapes and eng.shapes[0].color[3] == 255, "seeded shape must be forced opaque"
    finally:
        eng._shutdown()  # no run() to free the shared-memory canvas


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
