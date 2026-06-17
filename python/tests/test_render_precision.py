from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fd6.shapegen.gpu import EllipseBatchSearcher  # noqa: E402
from fd6.shapegen.sampling import build_center_cdf  # noqa: E402
from fd6.shapegen.scoring import compute_optimal_color, precompute_canvas_error, score_shape  # noqa: E402
from fd6.shapegen.shapes.ellipse import RotatedEllipse  # noqa: E402


def test_weighted_optimal_color_biases_toward_detail_pixel():
    target = np.array([[[0, 0, 0], [255, 0, 0]]], dtype=np.uint8)
    current = np.zeros_like(target)
    mask = np.array([[255, 255]], dtype=np.uint8)
    bbox = (0, 0, 2, 1)

    plain = compute_optimal_color(target, current, mask, bbox, 255)
    weighted = compute_optimal_color(
        target,
        current,
        mask,
        bbox,
        255,
        color_weight=np.array([[1.0, 9.0]], dtype=np.float32),
    )

    assert plain[0] == 127
    assert weighted[0] > 220


def test_gpu_batch_score_matches_cpu_with_weighted_color_and_alpha_clip():
    size = 80
    target = np.zeros((size, size, 3), dtype=np.uint8)
    target[20:60, 20:60] = (30, 80, 220)
    target[36:44, 36:44] = (255, 240, 30)
    canvas = np.full((size, size, 3), 40, dtype=np.uint8)
    alpha = np.full((size, size), 255, dtype=np.uint8)
    alpha[:8, :] = 0
    alpha[-8:, :] = 0

    edge = np.ones((size, size), dtype=np.float32)
    edge[36:44, 36:44] = 8.0
    edge *= (alpha > 0).astype(np.float32)

    shape = RotatedEllipse(color=(0, 0, 0, 128), x=40, y=40, rx=18, ry=12, angle=25)
    params = np.array([[shape.x, shape.y, shape.rx, shape.ry, shape.angle]], dtype=np.float32)
    full_sq, norm = precompute_canvas_error(canvas, target, alpha, edge)

    cpu_score, _ = score_shape(
        shape,
        canvas,
        target,
        alpha,
        canvas_full_sq=full_sq,
        canvas_norm=norm,
        edge_weight=edge,
    )
    searcher = EllipseBatchSearcher(target, alpha, edge, xp=np)
    gpu_score = float(searcher._score_batch(params, np.asarray(canvas, np.float32), full_sq)[0][0])

    assert math.isfinite(cpu_score) and math.isfinite(gpu_score)
    assert abs(cpu_score - gpu_score) < 0.05


def test_center_cdf_biases_toward_edge_weighted_residual():
    """Importance-weighted candidate placement: with the same residual
    everywhere, the half of the canvas with higher edge weight should pull
    most of the sampling mass. The boolean-gate version would treat both
    halves identically (uniform mass). Same direction as the optimal-colour
    solver's `color_weight` extension, applied to placement instead of fit."""
    h, w = 8, 8
    canvas = np.zeros((h, w, 3), dtype=np.uint8)
    target = np.full((h, w, 3), 100, dtype=np.uint8)
    edge = np.ones((h, w), dtype=np.float32)
    edge[:, w // 2:] = 6.0
    cdf, gy, gx = build_center_cdf(canvas, target, edge, grid_n=4)
    probs = np.diff(np.concatenate([[0.0], cdf])).reshape(gy, gx)
    left = float(probs[:, : gx // 2].sum())
    right = float(probs[:, gx // 2 :].sum())
    assert right > left * 10, f"high-edge half expected ≫ low-edge half, got {right=:.4f} {left=:.4f}"
