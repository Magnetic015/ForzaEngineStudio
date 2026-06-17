"""Residual-guided candidate placement.

The search wastes most of its random samples late in a run: once 90% of the
canvas already matches the target, candidate ellipses drawn uniformly land on
already-good pixels and score poorly, so the *effective* search over the few
regions that still need work is tiny. This module builds a cheap coarse
probability grid from the current per-pixel residual and draws a fraction of
candidate centers from it, concentrating the layer budget where the canvas is
still wrong. The rest stay uniform so exploration never collapses.

Used by both backends (host-side candidate generation): the CPU workers and the
OpenCL searcher each call `sample_centers` to override the (cx, cy) of their
random candidates.
"""
from __future__ import annotations

import numpy as np


def _block_sum(arr: np.ndarray, gy: int, gx: int) -> np.ndarray:
    """Sum `arr` (H×W) into a gy×gx grid of (near-equal) blocks. O(H·W)."""
    h, w = arr.shape
    ys = (np.arange(gy) * h // gy).astype(np.intp)
    xs = (np.arange(gx) * w // gx).astype(np.intp)
    rows = np.add.reduceat(arr, ys, axis=0)      # (gy, W)
    return np.add.reduceat(rows, xs, axis=1)     # (gy, gx)


def build_center_cdf(
    canvas: np.ndarray,
    target: np.ndarray,
    edge_weight: np.ndarray | None = None,
    grid_n: int = 48,
    sharpen: float = 1.5,
) -> tuple[np.ndarray, int, int]:
    """Build a flat CDF over a coarse grid from the current residual.

    Cell weight ∝ (summed per-pixel residual)**sharpen, restricted to the scored
    region (edge_weight > 0 — folds in the alpha gate). `sharpen` > 1 biases
    sampling toward the worst cells while the floor from un-sharpened mass keeps
    moderate cells reachable. Returns (cdf flat float64 of length gy*gx, gy, gx).
    When nothing remains to fix, falls back to a uniform CDF over valid cells.
    """
    h, w = canvas.shape[:2]
    gy = max(1, min(grid_n, h))
    gx = max(1, min(grid_n, w))
    resid = np.abs(canvas.astype(np.float32) - target.astype(np.float32)).mean(axis=2)
    if edge_weight is not None:
        valid = (edge_weight > 0).astype(np.float32)
        resid = resid * valid
    else:
        valid = None
    cell = _block_sum(resid, gy, gx).astype(np.float64)
    total = float(cell.sum())
    if total <= 1e-9:
        # Canvas already matches everywhere (or fully masked) — sample uniformly
        # over whichever cells are inside the scored region.
        if valid is not None:
            cell = (_block_sum(valid, gy, gx) > 0).astype(np.float64)
        if cell.sum() <= 0:
            cell = np.ones((gy, gx), dtype=np.float64)
    else:
        if sharpen != 1.0:
            cell = np.power(cell, sharpen)
    flat = cell.ravel()
    s = flat.sum()
    flat = flat / s if s > 0 else np.full(flat.shape, 1.0 / flat.size)
    cdf = np.cumsum(flat)
    cdf[-1] = 1.0
    return cdf, gy, gx


def sample_centers(
    cdf: np.ndarray,
    gy: int,
    gx: int,
    w: int,
    h: int,
    n: int,
    seed: int,
    p_guided: float = 0.7,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (cx, cy) float32 arrays of length n.

    A `p_guided` fraction are drawn from the residual CDF (pick a cell ∝ its
    weight, then a uniform position inside that cell); the remainder are uniform
    over the whole canvas to preserve exploration. `seed` keeps it deterministic
    so a fixed engine seed still reproduces a run.
    """
    rs = np.random.RandomState(seed & 0x7FFFFFFF)
    n = max(0, int(n))
    cx = np.empty(n, dtype=np.float32)
    cy = np.empty(n, dtype=np.float32)
    if n == 0:
        return cx, cy
    n_guided = int(round(n * max(0.0, min(1.0, p_guided))))
    # Uniform exploration tail.
    if n - n_guided > 0:
        cx[n_guided:] = rs.uniform(0, w - 1, n - n_guided)
        cy[n_guided:] = rs.uniform(0, h - 1, n - n_guided)
    if n_guided > 0:
        u = rs.random_sample(n_guided)
        idx = np.clip(np.searchsorted(cdf, u, side="right"), 0, gy * gx - 1)
        ci = idx // gx
        cj = idx % gx
        ys0 = (ci * h // gy); ys1 = ((ci + 1) * h // gy)
        xs0 = (cj * w // gx); xs1 = ((cj + 1) * w // gx)
        cy[:n_guided] = ys0 + rs.random_sample(n_guided) * np.maximum(1, ys1 - ys0)
        cx[:n_guided] = xs0 + rs.random_sample(n_guided) * np.maximum(1, xs1 - xs0)
    np.clip(cx, 0, w - 1, out=cx)
    np.clip(cy, 0, h - 1, out=cy)
    return cx, cy
