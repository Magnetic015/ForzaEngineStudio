from __future__ import annotations

import numpy as np

from fd6.shapegen.shapes.base import Shape


# Edge-weighted scoring: how much more an edge pixel counts toward fitness vs
# a smooth interior pixel. With EDGE_BOOST=6, a shape that nails a 3px pupil
# outline is worth more than a shape that smooths over a 100px cheek block —
# without this, the random sampler drifts toward big translucent ellipses
# because they get good *averaged* error even when they miss every salient
# detail (eyes, mouths, hard outlines). Cheap Sobel magnitude, normalized to
# [1, EDGE_BOOST] so smooth regions still contribute baseline weight 1.
EDGE_BOOST = 6.0


def compute_edge_weight(
    target: np.ndarray,
    alpha_mask: np.ndarray | None = None,
    boost: float = EDGE_BOOST,
) -> np.ndarray:
    """Build an H×W float32 importance map for `target`.

    Combines a Sobel-gradient magnitude (normalized 0..1) with the alpha mask
    so the result is:
        - 0       where alpha_mask says transparent / buffer (ignored entirely)
        - 1       in smooth interior regions
        - up to `boost` on the strongest edges

    Pass this to `rms_error` / `precompute_canvas_error` / `score_shape` /
    `composite` as the `edge_weight` keyword. Build it ONCE per generation
    (the target doesn't change) and reuse for every score.
    """
    h, w = target.shape[:2]
    # Luminance — cheap proxy for "what the eye sees" so eye/mouth/outline
    # edges register on grayscale gradients even when the RGB diff is mild.
    lum = (
        target[:, :, 0].astype(np.float32) * 0.299
        + target[:, :, 1].astype(np.float32) * 0.587
        + target[:, :, 2].astype(np.float32) * 0.114
    )
    # 3×3 Sobel kernels expressed as a manual convolution (avoids a SciPy
    # dependency, fast enough since we only run it once per generation).
    pad = np.pad(lum, 1, mode="edge")
    gx = (
        -1.0 * pad[0:h, 0:w]   + 0.0 * pad[0:h, 1:w+1]   + 1.0 * pad[0:h, 2:w+2]
        + -2.0 * pad[1:h+1, 0:w] + 0.0 * pad[1:h+1, 1:w+1] + 2.0 * pad[1:h+1, 2:w+2]
        + -1.0 * pad[2:h+2, 0:w] + 0.0 * pad[2:h+2, 1:w+1] + 1.0 * pad[2:h+2, 2:w+2]
    )
    gy = (
        -1.0 * pad[0:h, 0:w]   + -2.0 * pad[0:h, 1:w+1]   + -1.0 * pad[0:h, 2:w+2]
        + 0.0 * pad[1:h+1, 0:w] + 0.0 * pad[1:h+1, 1:w+1] + 0.0 * pad[1:h+1, 2:w+2]
        + 1.0 * pad[2:h+2, 0:w] + 2.0 * pad[2:h+2, 1:w+1] + 1.0 * pad[2:h+2, 2:w+2]
    )
    mag = np.sqrt(gx * gx + gy * gy)
    max_mag = float(mag.max())
    if max_mag < 1e-6:
        # Flat image — every pixel is baseline weight.
        norm = np.ones((h, w), dtype=np.float32)
    else:
        norm = 1.0 + (boost - 1.0) * (mag / max_mag).astype(np.float32)
    if alpha_mask is not None:
        norm = norm * (alpha_mask > 0).astype(np.float32)
    return norm


def rms_error(
    a: np.ndarray,
    b: np.ndarray,
    alpha_mask: np.ndarray | None = None,
    edge_weight: np.ndarray | None = None,
) -> float:
    """RMS pixel error between two (H, W, 3) uint8 images. Lower is better.

    If `alpha_mask` (H, W) uint8 is given, only pixels where alpha>0 contribute; transparent
    pixels are ignored (sticker mode). The RMS is normalized by the count of contributing pixels.

    If `edge_weight` (H, W) float32 is given, per-pixel squared error is
    multiplied by the weight (smooth interior=1, edge≈EDGE_BOOST). When
    combined with `alpha_mask`, the weight already encodes the alpha gate so
    transparent pixels stay at zero.
    """
    diff = a.astype(np.int32) - b.astype(np.int32)
    sq = diff * diff
    if edge_weight is not None:
        weight = edge_weight[:, :, None]
        total = float((sq * weight).sum())
        n = float(edge_weight.sum() * 3)
        if n < 1:
            return 0.0
        return float(np.sqrt(total / n))
    if alpha_mask is None:
        return float(np.sqrt(sq.mean()))
    weight = (alpha_mask > 0)[:, :, None].astype(np.float32)
    total = float((sq * weight).sum())
    n = float(weight.sum() * 3)
    if n < 1:
        return 0.0
    return float(np.sqrt(total / n))


def compute_optimal_color(
    target: np.ndarray,
    current: np.ndarray,
    mask_local: np.ndarray,
    bbox: tuple[int, int, int, int],
    alpha: int,
    color_weight: np.ndarray | None = None,
) -> tuple[int, int, int, int]:
    """For a given shape mask and fixed alpha, compute the RGB color that minimizes RMS over the masked region.

    Closed-form: with `over` compositing `out = a*src + (1-a)*dst`, RMS is minimized when
    src = (target - (1-a)*dst) / a, averaged over the masked pixels.
    `color_weight` optionally biases that average toward pixels that matter
    more to the active score, e.g. edge/saliency/detail regions.
    """
    x0, y0, x1, y1 = bbox
    if x1 <= x0 or y1 <= y0 or mask_local.size == 0:
        return (0, 0, 0, alpha)
    tgt = target[y0:y1, x0:x1].astype(np.float32)
    cur = current[y0:y1, x0:x1].astype(np.float32)
    m = mask_local.astype(np.float32) / 255.0
    if color_weight is not None:
        m = m * np.maximum(color_weight.astype(np.float32), 0.0)
    weight = float(m.sum())
    if weight < 0.5:
        return (0, 0, 0, alpha)
    a = alpha / 255.0
    if a < 1e-6:
        return (0, 0, 0, alpha)
    src = (tgt - (1.0 - a) * cur) / a
    src_masked = src * m[:, :, None]
    avg = src_masked.reshape(-1, 3).sum(axis=0) / weight
    avg = np.clip(avg, 0, 255).astype(np.int32)
    return (int(avg[0]), int(avg[1]), int(avg[2]), alpha)


def composite(
    current: np.ndarray,
    shape: Shape,
    target: np.ndarray,
    alpha_mask: np.ndarray | None = None,
    edge_weight: np.ndarray | None = None,
) -> tuple[np.ndarray, float]:
    """Composite shape over current canvas with optimal color. Return (new_canvas, new_rms).

    In sticker mode (alpha_mask provided), the shape's per-pixel mask is AND-ed with the
    target's alpha mask so paint never lands in transparent areas — the dark-grey canvas
    background stays visible there, which is what the user expects from sticker mode.
    """
    h, w = current.shape[:2]
    mask_local, bbox = shape.rasterize_mask(w, h)
    x0, y0, x1, y1 = bbox
    if x1 <= x0 or y1 <= y0 or mask_local.size == 0:
        return current, rms_error(current, target, alpha_mask)
    # Combine shape mask with alpha mask if in sticker mode
    if alpha_mask is not None:
        region_alpha = alpha_mask[y0:y1, x0:x1]
        # Element-wise min: paint only where both shape AND opaque
        effective_mask = np.minimum(mask_local, region_alpha)
    else:
        effective_mask = mask_local
    color_weight = edge_weight[y0:y1, x0:x1] if edge_weight is not None else None
    color = compute_optimal_color(target, current, effective_mask, bbox, shape.color[3], color_weight)
    new = current.copy()
    a = color[3] / 255.0
    region_cur = new[y0:y1, x0:x1].astype(np.float32)
    region_tgt_color = np.array(color[:3], dtype=np.float32)
    m = (effective_mask.astype(np.float32) / 255.0)[:, :, None]
    blended = m * (a * region_tgt_color + (1.0 - a) * region_cur) + (1.0 - m) * region_cur
    new[y0:y1, x0:x1] = np.clip(blended, 0, 255).astype(np.uint8)
    shape.color = color
    return new, rms_error(new, target, alpha_mask, edge_weight)


# Per-shape alpha levels searched at commit time. The shape search ranks
# candidates at a fixed alpha (cheap, just to locate the shape); the winner then
# picks the opacity that fits its region best — high (near-opaque) for sharp,
# high-contrast detail, low for soft gradient fills. Spans 60..255 so the search
# can press a layer hard where the old fixed 128 could not. Both backends commit
# through this, so the GPU path gains per-shape alpha with no kernel change.
ALPHA_LEVELS: tuple[int, ...] = (60, 90, 120, 150, 180, 210, 235, 255)


def composite_optimal(
    current: np.ndarray,
    shape: Shape,
    target: np.ndarray,
    alpha_mask: np.ndarray | None = None,
    edge_weight: np.ndarray | None = None,
    alpha_levels: tuple[int, ...] = ALPHA_LEVELS,
    fit_mask_local: np.ndarray | None = None,
) -> tuple[np.ndarray, float]:
    """Like `composite`, but also choose the per-shape alpha that minimizes the
    committed error. One rasterize; for each candidate alpha the optimal RGB is
    closed-form (`compute_optimal_color`) and the resulting region error is a
    cheap masked sum, so the whole sweep costs a small constant times one
    `composite`. Sets `shape.color` to the winning RGBA and returns
    (new_canvas, new_rms) — identical contract to `composite`.

    `fit_mask_local` (bbox-local uint8, optional) restricts where colour/alpha
    are FIT without changing where the shape PAINTS: the optimal colour and the
    error compared across alphas are evaluated only over `fit_mask_local`, while
    the full shape mask is still composited. The coverage-aware final polish uses
    this to re-fit each shape over only the pixels where it stays visible in the
    final stack. When None, fitting and painting both use the shape's own mask;
    the error is then gated to the shape's mask, which does not change the chosen
    colour/alpha (out-of-mask pixels are constant across alphas) — so the normal
    commit path is unaffected.
    """
    h, w = current.shape[:2]
    mask_local, bbox = shape.rasterize_mask(w, h)
    x0, y0, x1, y1 = bbox
    if x1 <= x0 or y1 <= y0 or mask_local.size == 0:
        return current, rms_error(current, target, alpha_mask, edge_weight)
    fit = mask_local if fit_mask_local is None else fit_mask_local
    # Sticker/buffer contract: never paint outside the opaque region. The paint
    # mask is clipped to alpha (matching the original composite()); the fit/colour
    # mask is the fit region also clipped to alpha.
    if alpha_mask is not None:
        region_alpha = alpha_mask[y0:y1, x0:x1]
        color_mask = np.minimum(fit, region_alpha)
        paint_mask = np.minimum(mask_local, region_alpha)
    else:
        color_mask = fit
        paint_mask = mask_local
    region_cur = current[y0:y1, x0:x1].astype(np.float32)
    region_tgt = target[y0:y1, x0:x1].astype(np.float32)
    m = (paint_mask.astype(np.float32) / 255.0)[:, :, None]
    # Region error weight: edge-weighted supersedes the boolean alpha gate, which
    # supersedes an unweighted sum — mirrors score_shape's three branches so the
    # chosen alpha minimizes exactly the metric the search optimizes. Gated to the
    # FIT region so only those pixels drive the alpha choice.
    fit_gate = (fit > 0).astype(np.float32)
    if edge_weight is not None:
        wreg = edge_weight[y0:y1, x0:x1] * fit_gate
    elif alpha_mask is not None:
        wreg = (region_alpha > 0).astype(np.float32) * fit_gate
    else:
        wreg = fit_gate
    wreg = wreg[:, :, None]
    best_err = float("inf")
    best_color = shape.color
    best_blended = None
    for a8 in alpha_levels:
        color_weight = edge_weight[y0:y1, x0:x1] if edge_weight is not None else None
        color = compute_optimal_color(target, current, color_mask, bbox, a8, color_weight)
        a = color[3] / 255.0
        src = np.array(color[:3], dtype=np.float32)
        blended = m * (a * src + (1.0 - a) * region_cur) + (1.0 - m) * region_cur
        diff = blended - region_tgt
        err = float(((diff * diff) * wreg).sum())
        if err < best_err:
            best_err, best_color, best_blended = err, color, blended
    new = current.copy()
    if best_blended is not None:
        new[y0:y1, x0:x1] = np.clip(best_blended, 0, 255).astype(np.uint8)
    shape.color = best_color
    return new, rms_error(new, target, alpha_mask, edge_weight)


def composite_fixed(current: np.ndarray, shape: Shape,
                    alpha_mask: np.ndarray | None = None) -> np.ndarray:
    """Composite `shape` with its EXISTING colour (no re-solve, no scoring).

    Used by the coverage-aware polish to replay a shape whose pixels are all
    hidden by later shapes: re-fitting it has no visible reference, so keep the
    colour it was given and just paint it so the translucent stack is unchanged.
    Clips painting to `alpha_mask` when present, matching composite()/composite_optimal.
    """
    h, w = current.shape[:2]
    mask_local, bbox = shape.rasterize_mask(w, h)
    x0, y0, x1, y1 = bbox
    if x1 <= x0 or y1 <= y0 or mask_local.size == 0:
        return current
    paint_mask = mask_local
    if alpha_mask is not None:
        paint_mask = np.minimum(mask_local, alpha_mask[y0:y1, x0:x1])
    color = shape.color
    a = (color[3] / 255.0) if len(color) >= 4 else 1.0
    region_cur = current[y0:y1, x0:x1].astype(np.float32)
    src = np.array(color[:3], dtype=np.float32)
    m = (paint_mask.astype(np.float32) / 255.0)[:, :, None]
    blended = m * (a * src + (1.0 - a) * region_cur) + (1.0 - m) * region_cur
    new = current.copy()
    new[y0:y1, x0:x1] = np.clip(blended, 0, 255).astype(np.uint8)
    return new


# In sticker mode, virtually every "solid" pixel of a candidate shape must sit
# inside the opaque region. Anything less and the shape's body bleeds past the
# alpha edge in FH6 (no per-pixel alpha there → solid blob in transparent space).
# Counted against pixels where mask_local >= 128 (i.e., the shape's actual body,
# excluding anti-aliased fringe) so AA at the silhouette doesn't disqualify
# otherwise-clean shapes.
STICKER_OVERLAP_MIN = 0.995


def precompute_canvas_error(
    current: np.ndarray,
    target: np.ndarray,
    alpha_mask: np.ndarray | None = None,
    edge_weight: np.ndarray | None = None,
) -> tuple[float, float]:
    """Return (full_canvas_squared_error, normalizer_n) for the current canvas.

    These are constants for the lifetime of a single canvas snapshot — they
    don't depend on the candidate shape being scored — so a batch of N
    candidate evaluations against the same canvas can compute them ONCE
    instead of N times. This is what made score_shape O(image_size × N) at
    high resolutions; with the cache it's O(image_size + bbox_size × N).

    The math is identical to what score_shape did inline before. Same
    result, ~N× less work for the random-search phase.

    When `edge_weight` is provided it supersedes the boolean alpha gate (the
    weight map already folds in alpha=0 from compute_edge_weight).
    """
    if edge_weight is not None:
        weight_full = edge_weight[:, :, None]
        diff = (current.astype(np.float32) - target.astype(np.float32)) ** 2
        full_sq = float((diff * weight_full).sum())
        n = float(edge_weight.sum() * 3)
        return full_sq, n
    if alpha_mask is None:
        diff = current.astype(np.int32) - target.astype(np.int32)
        full_sq = float((diff * diff).sum())
        n = float(current.shape[0] * current.shape[1] * 3)
        return full_sq, n
    weight_full = (alpha_mask > 0)[:, :, None].astype(np.float32)
    diff = (current.astype(np.float32) - target.astype(np.float32)) ** 2
    full_sq = float((diff * weight_full).sum())
    n = float(weight_full.sum() * 3)
    return full_sq, n


def score_shape(
    shape: Shape,
    current: np.ndarray,
    target: np.ndarray,
    alpha_mask: np.ndarray | None = None,
    *,
    canvas_full_sq: float | None = None,
    canvas_norm: float | None = None,
    edge_weight: np.ndarray | None = None,
) -> tuple[float, tuple[int, int, int, int]]:
    """Score a candidate without modifying the working canvas. Returns (rms_if_committed, optimal_color).

    `canvas_full_sq` and `canvas_norm` may be precomputed via
    `precompute_canvas_error` and reused across many candidate evaluations
    against the SAME canvas. When None, they're computed here — semantically
    identical, just slower.

    Sticker-mode contract: a shape must sit ESSENTIALLY ENTIRELY inside the
    opaque region or it gets rejected with +inf. FH6 paints the full ellipse
    with no per-pixel alpha, so any shape that bleeds past the silhouette
    will render its body in what should be transparent space — exactly the
    'black outline artifacts' the user reported.
    """
    h, w = current.shape[:2]
    mask_local, bbox = shape.rasterize_mask(w, h)
    x0, y0, x1, y1 = bbox
    if x1 <= x0 or y1 <= y0 or mask_local.size == 0:
        return float("inf"), shape.color
    effective_mask = mask_local
    if alpha_mask is not None:
        region_alpha = alpha_mask[y0:y1, x0:x1]
        # Count only "solid" body pixels (alpha >=128) — ignores AA fringe so
        # antialiased silhouette edges don't artificially disqualify shapes.
        shape_body = mask_local >= 128
        body_total = float(shape_body.sum())
        if body_total < 1.0:
            return float("inf"), shape.color
        opaque_body = region_alpha >= 128
        if not opaque_body.any():
            return float("inf"), shape.color
        inside = float((shape_body & opaque_body).sum())
        if inside / body_total < STICKER_OVERLAP_MIN:
            return float("inf"), shape.color
        # AND-mask for color so the zeroed-out RGB of transparent pixels in
        # `target` can't drag the optimal color toward black.
        effective_mask = np.minimum(mask_local, region_alpha)
    color_weight = edge_weight[y0:y1, x0:x1] if edge_weight is not None else None
    color = compute_optimal_color(target, current, effective_mask, bbox, shape.color[3], color_weight)
    a = color[3] / 255.0
    region_cur = current[y0:y1, x0:x1].astype(np.float32)
    region_tgt = target[y0:y1, x0:x1].astype(np.float32)
    src = np.array(color[:3], dtype=np.float32)
    m = (effective_mask.astype(np.float32) / 255.0)[:, :, None]
    blended = m * (a * src + (1.0 - a) * region_cur) + (1.0 - m) * region_cur
    diff_in = blended - region_tgt
    # Edge-weighted path supersedes the boolean alpha gate when present.
    if edge_weight is not None:
        if canvas_full_sq is None or canvas_norm is None:
            full_sq, n = precompute_canvas_error(current, target, alpha_mask, edge_weight)
        else:
            full_sq, n = canvas_full_sq, canvas_norm
        weight_region = edge_weight[y0:y1, x0:x1][:, :, None]
        region_old_sq = float((((region_cur - region_tgt) ** 2) * weight_region).sum())
        region_new_sq = float(((diff_in ** 2) * weight_region).sum())
        total_sq = full_sq - region_old_sq + region_new_sq
        if n < 1:
            return 0.0, color
        return float(np.sqrt(max(0.0, total_sq) / n)), color
    if alpha_mask is None:
        if canvas_full_sq is None or canvas_norm is None:
            full_sq, n_px = precompute_canvas_error(current, target, None)
        else:
            full_sq, n_px = canvas_full_sq, canvas_norm
        region_old_sq = float(((region_cur - region_tgt) ** 2).sum())
        region_new_sq = float((diff_in ** 2).sum())
        total_sq = full_sq - region_old_sq + region_new_sq
        return float(np.sqrt(max(0.0, total_sq) / n_px)), color
    # Sticker mode (no edge weight): weighted RMS, only opaque pixels contribute
    if canvas_full_sq is None or canvas_norm is None:
        full_sq, n = precompute_canvas_error(current, target, alpha_mask)
    else:
        full_sq, n = canvas_full_sq, canvas_norm
    weight_region = ((alpha_mask[y0:y1, x0:x1] > 0).astype(np.float32))[:, :, None]
    region_old_sq = float((((region_cur - region_tgt) ** 2) * weight_region).sum())
    region_new_sq = float(((diff_in ** 2) * weight_region).sum())
    total_sq = full_sq - region_old_sq + region_new_sq
    if n < 1:
        return 0.0, color
    return float(np.sqrt(max(0.0, total_sq) / n)), color
