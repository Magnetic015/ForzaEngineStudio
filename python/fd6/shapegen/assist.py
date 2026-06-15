"""Image-model assist primitives for the shape engine.

The shape engine approximates a target image with a stream of translucent
rotated ellipses. Every ellipse is one *layer* in the final Forza vinyl, so the
number of layers is the headline cost: more layers = more detail, but Forza caps
them and they get expensive to place. The goal of this module is the opposite
trade — **fewer layers, more perceived detail** — by letting an image model (or
a fast local approximation of one) pre-digest the target into something the
search reproduces with far fewer shapes.

Three cooperating assists, all returned as plain numpy arrays so the engine
stays decoupled from where they came from (a local CV step here, or an external
image model via ``image_process.py``):

1. **Render-optimization** (``simplify_for_render``) — flatten smooth regions
   into clean flat-colour blocks while keeping edges crisp. A posterized,
   edge-preserving target needs dramatically fewer ellipses to reach the same
   fidelity than a noisy photo does.

2. **Importance guidance** (``saliency_importance``) — a per-pixel weight map
   that concentrates the shape budget on salient detail (faces, edges, high
   local contrast) instead of spreading it evenly. The engine already supports
   an edge-weight map; this enriches it with center-surround saliency so the
   same layer count buys more *perceived* detail.

3. **Hybrid base** (``build_base_canvas``) — a smooth low-frequency under-paint
   the engine seeds its canvas with, so ellipses only have to correct the
   residual high-frequency detail rather than build every tonal block from a
   flat average. Fewer big "background" ellipses, more budget for detail.

Everything here is pure numpy/Pillow and deterministic — no network — so it is
unit-testable offline and doubles as the graceful fallback when no external
image-model assets are supplied.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from fd6.shapegen.scoring import compute_edge_weight


# ── Render-optimization (flatten for fewer layers) ───────────────────────────

def posterize(rgb: np.ndarray, levels: int) -> np.ndarray:
    """Quantize each RGB channel to `levels` evenly-spaced levels.

    Collapsing the colour count turns gentle gradients into a handful of flat
    bands, which the ellipse search can cover with a few large shapes instead
    of a long tail of near-duplicate translucent ones.
    """
    levels = max(2, int(levels))
    arr = rgb.astype(np.float32)
    q = np.round(arr / 255.0 * (levels - 1)) / (levels - 1) * 255.0
    return np.clip(q, 0, 255).astype(np.uint8)


def edge_preserving_smooth(
    rgb: np.ndarray,
    radius: int = 2,
    color_sigma: float = 30.0,
    passes: int = 2,
) -> np.ndarray:
    """Bilateral-style smoothing: blur flat regions, keep edges sharp.

    A scipy-free bilateral filter implemented as a small shift-and-accumulate
    over a (2*radius+1)² window. Each neighbour is weighted by a spatial
    Gaussian (distance) AND a range Gaussian (colour difference), so pixels
    across a strong edge barely contribute and the edge survives while flat
    areas average out. Returned as float32 (caller posterizes/clips).
    """
    radius = max(1, int(radius))
    img = rgb.astype(np.float32)
    h, w = img.shape[:2]
    space_sigma = float(radius)
    inv2_color = 1.0 / (2.0 * color_sigma * color_sigma)
    inv2_space = 1.0 / (2.0 * space_sigma * space_sigma)
    for _ in range(max(1, int(passes))):
        pad = np.pad(img, ((radius, radius), (radius, radius), (0, 0)), mode="edge")
        acc = np.zeros_like(img)
        wsum = np.zeros((h, w, 1), dtype=np.float32)
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                shifted = pad[radius + dy:radius + dy + h, radius + dx:radius + dx + w, :]
                color_d2 = ((shifted - img) ** 2).sum(axis=2, keepdims=True)
                sw = np.exp(-(dx * dx + dy * dy) * inv2_space)
                weight = sw * np.exp(-color_d2 * inv2_color)
                acc += weight * shifted
                wsum += weight
        img = acc / np.maximum(wsum, 1e-6)
    return img


def simplify_for_render(
    rgb: np.ndarray,
    alpha_mask: np.ndarray | None = None,
    levels: int = 12,
    radius: int = 2,
    color_sigma: float = 30.0,
    passes: int = 2,
) -> np.ndarray:
    """Edge-preserving smooth + posterize → a render-optimized target.

    This is the local stand-in for "ask an image model to flatten the picture
    into clean flat-colour regions with crisp edges". The output reaches a
    given RMS with far fewer ellipses because there is simply less
    high-frequency content to chase. Edges are preserved by the bilateral
    pass, so the precision that matters (outlines, eyes, hard boundaries) is
    kept while tonal blocks are flattened.
    """
    smoothed = edge_preserving_smooth(rgb, radius=radius, color_sigma=color_sigma, passes=passes)
    out = posterize(smoothed, levels)
    if alpha_mask is not None:
        out = out * (alpha_mask > 0)[:, :, None].astype(np.uint8)
    return out


# ── Importance guidance (spend layers where the eye looks) ───────────────────

def _luminance(rgb: np.ndarray) -> np.ndarray:
    return (
        rgb[:, :, 0].astype(np.float32) * 0.299
        + rgb[:, :, 1].astype(np.float32) * 0.587
        + rgb[:, :, 2].astype(np.float32) * 0.114
    )


def _downsample_blur(lum: np.ndarray, scale: int = 16) -> np.ndarray:
    """Cheap large-radius blur via downscale→upscale (no scipy)."""
    h, w = lum.shape
    sw, sh = max(1, w // scale), max(1, h // scale)
    small = Image.fromarray(np.clip(lum, 0, 255).astype(np.uint8), "L").resize((sw, sh), Image.BILINEAR)
    big = small.resize((w, h), Image.BILINEAR)
    return np.asarray(big, dtype=np.float32)


def saliency_map(rgb: np.ndarray, scale: int = 16) -> np.ndarray:
    """Center-surround saliency in [0, 1]: |lum - large-radius-blur(lum)|.

    Highlights regions that stand out from their surroundings (a face against
    a flat sky, text, focal subjects) rather than only thin gradient edges.
    Normalized by its own max so it is resolution-independent.
    """
    lum = _luminance(rgb)
    surround = _downsample_blur(lum, scale=scale)
    sal = np.abs(lum - surround)
    m = float(sal.max())
    if m < 1e-6:
        return np.zeros_like(sal)
    return (sal / m).astype(np.float32)


def saliency_importance(
    rgb: np.ndarray,
    alpha_mask: np.ndarray | None = None,
    edge_boost: float = 6.0,
    saliency_boost: float = 3.0,
    scale: int = 16,
) -> np.ndarray:
    """Build an H×W float32 importance map = edge-weight enriched by saliency.

    Starts from the engine's own Sobel edge weight (so behaviour degrades
    gracefully toward the existing default) and multiplies in a center-surround
    saliency term so salient blobs — not just hairline edges — pull shape
    budget. The result is renormalized to preserve the *sum* of the plain edge
    weight, which keeps the engine's displayed RMS on the same scale as a
    non-assisted run (the map only redistributes attention, it doesn't inflate
    the error numbers).
    """
    edge = compute_edge_weight(rgb, alpha_mask, boost=edge_boost).astype(np.float32)
    sal = saliency_map(rgb, scale=scale)
    imp = edge * (1.0 + (saliency_boost - 1.0) * sal)
    if alpha_mask is not None:
        imp = imp * (alpha_mask > 0).astype(np.float32)
    base_sum = float(edge.sum())
    cur_sum = float(imp.sum())
    if cur_sum > 1e-6 and base_sum > 1e-6:
        imp = imp * (base_sum / cur_sum)
    return imp.astype(np.float32)


def importance_from_image(
    path: str,
    target_hw: tuple[int, int],
    alpha_mask: np.ndarray | None = None,
    lo: float = 1.0,
    hi: float = 6.0,
) -> np.ndarray:
    """Turn an external saliency/structure PNG (e.g. from an image model) into
    an engine importance map.

    The image is read as grayscale, resized to the target H×W, normalized to
    [0, 1], then mapped to weights in [lo, hi] (bright = important). Folds in
    the alpha mask so buffer/transparent pixels stay at weight 0.
    """
    h, w = target_hw
    g = np.asarray(Image.open(path).convert("L").resize((w, h), Image.BILINEAR), dtype=np.float32)
    g /= 255.0
    imp = lo + (hi - lo) * g
    if alpha_mask is not None:
        imp = imp * (alpha_mask > 0).astype(np.float32)
    return imp.astype(np.float32)


# ── Hybrid base (under-paint so ellipses only chase detail) ──────────────────

def build_base_canvas(
    rgb: np.ndarray,
    alpha_mask: np.ndarray | None = None,
    downscale: int = 8,
    levels: int = 0,
    sticker_bg: int = 40,
) -> np.ndarray:
    """A smooth low-frequency under-paint to seed the engine canvas with.

    Downscale→upscale keeps only the broad tonal structure (the "background"
    the engine would otherwise spend many large ellipses building), so the
    ellipse budget goes to high-frequency detail instead. Optional `levels`
    posterizes the base into flat bands. In sticker mode the area outside the
    silhouette is filled with the engine's grey buffer value so the seed
    matches the engine's own convention.
    """
    h, w = rgb.shape[:2]
    img = Image.fromarray(np.ascontiguousarray(rgb), "RGB")
    sw, sh = max(1, w // max(1, downscale)), max(1, h // max(1, downscale))
    base = img.resize((sw, sh), Image.LANCZOS).resize((w, h), Image.LANCZOS)
    base = np.asarray(base, dtype=np.uint8)
    if levels and levels >= 2:
        base = posterize(base, levels)
    if alpha_mask is not None:
        m = (alpha_mask > 0)[:, :, None]
        base = np.where(m, base, sticker_bg).astype(np.uint8)
    return np.ascontiguousarray(base)


def base_canvas_from_image(
    path: str,
    target_hw: tuple[int, int],
    alpha_mask: np.ndarray | None = None,
    sticker_bg: int = 40,
) -> np.ndarray:
    """Load an externally-supplied base/under-paint image (e.g. an image-model
    flattened render) and fit it to the target canvas."""
    h, w = target_hw
    base = np.asarray(Image.open(path).convert("RGB").resize((w, h), Image.LANCZOS), dtype=np.uint8)
    if alpha_mask is not None:
        m = (alpha_mask > 0)[:, :, None]
        base = np.where(m, base, sticker_bg).astype(np.uint8)
    return np.ascontiguousarray(base)


# ── Prompt preset for the external image-model route ─────────────────────────

#: Instruction handed to the image model (image_process.py) when the user asks
#: it to *assist rendering* rather than make a free-form edit. Tuned to produce
#: an output the ellipse search reproduces with fewer layers at higher fidelity.
RENDER_OPTIMIZE_PROMPT = (
    "Redraw this image as a clean, flat poster-style illustration optimized for "
    "reproduction with a small number of solid shapes. Flatten smooth areas into "
    "large flat regions of uniform color, remove photographic noise, film grain, "
    "and subtle gradients, but KEEP all important edges, outlines and small "
    "high-contrast details crisp and well defined. Preserve the overall "
    "composition, colors and subject exactly. Do not add new objects, text, "
    "borders or background."
)


def render_optimize_prompt(levels: int | None = None) -> str:
    """The render-optimization prompt, optionally hinting a target color count."""
    if levels and levels >= 2:
        return RENDER_OPTIMIZE_PROMPT + f" Aim for roughly {int(levels)} distinct flat colors."
    return RENDER_OPTIMIZE_PROMPT
