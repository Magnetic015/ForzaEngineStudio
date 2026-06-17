from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
import ctypes
import os
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import shared_memory

import numpy as np
from concurrent.futures.process import BrokenProcessPool

from fd6.shapegen.profile import Profile
from fd6.shapegen.sampling import build_center_cdf, sample_centers
from fd6.shapegen.scoring import (
    composite,
    composite_fixed,
    composite_optimal,
    compute_edge_weight,
    precompute_canvas_error,
    rms_error,
    score_shape,
)
from fd6.shapegen.shapes import Shape, random_shape


def _available_ram_mb() -> int:
    """Best-effort free physical RAM in MB. Falls back to 4096 if detection fails."""
    if sys.platform == "win32":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        try:
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return int(stat.ullAvailPhys // (1024 * 1024))
        except Exception:
            return 4096
    # Non-Windows fallback (best-effort): read /proc/meminfo
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 4096


def _safe_worker_count(user_requested: int, random_samples: int) -> int:
    """Pick a worker count that won't crash low-end machines and won't waste cycles on overhead.

    Caps simultaneously by:
      - CPU: leave 1 thread free on 4-core boxes, 2 on bigger to keep system responsive
      - RAM: each worker process needs ~250 MB; reserve 2 GB for main app + system
      - Workload: each worker needs >= 64 random samples or IPC overhead dominates
    """
    cpu = os.cpu_count() or 1
    headroom = 1 if cpu <= 4 else 2
    cpu_cap = max(1, cpu - headroom)

    free_mb = _available_ram_mb()
    # Reserve 2 GB for main app + system; budget 250 MB per worker process.
    ram_budget_mb = max(0, free_mb - 2048)
    ram_cap = max(1, ram_budget_mb // 250)

    # Workload-size cap: small per-iteration budgets don't amortize spawn/IPC cost.
    work_cap = max(1, random_samples // 64)

    # User explicit override (profile.max_threads > 0) is honored but still safety-capped.
    requested = user_requested if user_requested > 0 else cpu_cap
    return max(1, min(requested, cpu_cap, ram_cap, work_cap))


class EngineWorkerError(RuntimeError):
    """Raised when the parallel shape search can't produce a result.

    Carries a user-facing, actionable message (already formatted). `Engine.run`
    surfaces `str(exc)` verbatim in the error dialog rather than the raw
    `Type: msg` form, so users see e.g. an out-of-memory hint instead of the
    cryptic 'TypeError: exceptions must derive from BaseException' that a dying
    worker process used to produce.
    """


@dataclass
class EngineConfig:
    profile: Profile
    seed: int = 0  # 0 → time-based


@dataclass
class EngineEvent:
    """Event emitted at preview/save points. The worker translates these into Qt signals."""
    kind: str  # "shape_committed" | "checkpoint" | "preview" | "done" | "error" | "backend"
    shape_count: int = 0
    rms: float = 0.0
    canvas: np.ndarray | None = None  # uint8 (H, W, 3); only set for preview/done
    message: str = ""


# ── Worker-side globals + functions ──────────────────────────────────────────
# These live at module top-level so they survive pickling across spawn().
# Each ProcessPoolExecutor worker calls _init_worker once at startup, then
# _worker_independent_search per task. The canvas lives in shared memory so
# the main process can mutate it in place between tasks without re-sending.

_W_TARGET: np.ndarray | None = None
_W_ALPHA: np.ndarray | None = None
_W_EDGE_WEIGHT: np.ndarray | None = None  # ndarray view onto _W_EDGE_SHM (LIVE — engine rewrites periodically)
_W_EDGE_SHM: shared_memory.SharedMemory | None = None
_W_CANVAS_SHM: shared_memory.SharedMemory | None = None
_W_CANVAS: np.ndarray | None = None


def _init_worker(
    target_bytes: bytes, target_shape: tuple,
    canvas_shm_name: str, canvas_shape: tuple,
    alpha_bytes: bytes | None, alpha_shape: tuple | None,
    edge_shm_name: str | None, edge_shape: tuple | None,
) -> None:
    """Subprocess startup hook. Wires up shared canvas + immutable target/alpha + LIVE edge weight."""
    global _W_TARGET, _W_ALPHA, _W_EDGE_WEIGHT, _W_EDGE_SHM, _W_CANVAS_SHM, _W_CANVAS
    _W_TARGET = np.frombuffer(target_bytes, dtype=np.uint8).reshape(target_shape).copy()
    if alpha_bytes is not None and alpha_shape is not None:
        _W_ALPHA = np.frombuffer(alpha_bytes, dtype=np.uint8).reshape(alpha_shape).copy()
    else:
        _W_ALPHA = None
    if edge_shm_name is not None and edge_shape is not None:
        # Attach to the LIVE edge-weight shared memory — workers see periodic
        # residual updates from the main process between iterations without
        # needing per-iteration IPC.
        _W_EDGE_SHM = shared_memory.SharedMemory(name=edge_shm_name)
        _W_EDGE_WEIGHT = np.ndarray(edge_shape, dtype=np.float32, buffer=_W_EDGE_SHM.buf)
    else:
        _W_EDGE_SHM = None
        _W_EDGE_WEIGHT = None
    _W_CANVAS_SHM = shared_memory.SharedMemory(name=canvas_shm_name)
    _W_CANVAS = np.ndarray(canvas_shape, dtype=np.uint8, buffer=_W_CANVAS_SHM.buf)


def _worker_independent_search(args: tuple) -> tuple:
    """One worker's independent (random search + hill-climb) sequence.

    Reads canvas directly from shared memory; no per-task copy. Returns a
    4-tuple ``(score, color, shape, error)``: on success ``error`` is None; on
    failure the first three are sentinels and ``error`` is a formatted string.

    CRITICAL: the whole body is wrapped so NOTHING — not even an odd library
    `raise` of a non-exception — can propagate raw across the ProcessPool
    boundary. A raw propagation is what produced the user-visible
    'TypeError: exceptions must derive from BaseException' mid-generation; the
    error now comes back as data and the main process decides what to do.

    Speed path: precomputes the full-canvas squared-error scalar ONCE at the
    start of the batch and reuses it for all 1000+ candidate evaluations.
    Without this, every score_shape call recomputed a 4096×4096×3 sum from
    scratch, which dominated the per-shape cost at high max_resolution.
    Result is mathematically identical; just no longer recomputed N times.
    """
    try:
        (types, n_random, n_mutate, w, h, seed, max_size_frac, center_cdf, guided_fraction, opaque) = args
        canvas = _W_CANVAS
        target = _W_TARGET
        alpha = _W_ALPHA
        edge_w = _W_EDGE_WEIGHT
        rng = random.Random(seed)

        # Precompute once for this batch — see precompute_canvas_error docstring.
        canvas_full_sq, canvas_norm = precompute_canvas_error(canvas, target, alpha, edge_w)

        # Residual-guided placement: pre-draw a center per random candidate biased
        # toward the cells that still differ most from the target (sampling.py).
        cxa = cya = None
        n_rand = max(1, n_random)
        if center_cdf is not None:
            cdf, gy, gx = center_cdf
            cxa, cya = sample_centers(cdf, gy, gx, w, h, n_rand, seed ^ 0x9E3779B1,
                                      p_guided=guided_fraction)

        # Random search
        best_score = float("inf")
        best_color = None
        best_shape = None
        for i in range(n_rand):
            s = random_shape(rng, w, h, types, max_size_frac=max_size_frac)
            if opaque:
                # Rank candidates at SOLID alpha so selection matches what the
                # injector draws. mutate() preserves color and commit re-enforces
                # 255 via _commit_alpha_levels, so the saved shape stays opaque.
                c = s.color
                s.color = (int(c[0]), int(c[1]), int(c[2]), 255)
            if cxa is not None and hasattr(s, "x") and hasattr(s, "y"):
                s.x = float(cxa[i]); s.y = float(cya[i])
            score, color = score_shape(s, canvas, target, alpha,
                                       canvas_full_sq=canvas_full_sq,
                                       canvas_norm=canvas_norm,
                                       edge_weight=edge_w)
            if score < best_score:
                best_score, best_color, best_shape = score, color, s
        if best_shape is None:
            return (float("inf"), None, None, None)

        # Annealed hill climb on the local best — step size shrinks across the
        # budget so early steps explore and late steps fine-tune.
        best_shape.color = best_color
        no_improve = 0
        cap = max(1, n_mutate)
        for i in range(cap):
            scale = max(0.2, 1.0 - i / cap)
            cand = best_shape.mutate(rng, w, h, scale=scale)
            score, color = score_shape(cand, canvas, target, alpha,
                                       canvas_full_sq=canvas_full_sq,
                                       canvas_norm=canvas_norm,
                                       edge_weight=edge_w)
            if score < best_score:
                best_score, best_color, best_shape = score, color, cand
                no_improve = 0
            else:
                no_improve += 1
                if no_improve >= max(20, cap // 4):
                    break
        if best_color is not None:
            best_shape.color = best_color
        return (best_score, best_color, best_shape, None)
    except Exception as exc:
        return (float("inf"), None, None, f"{type(exc).__name__}: {exc}")


# ── Engine ───────────────────────────────────────────────────────────────────

class Engine:
    """Image → shapes generator. Stateless w.r.t. external I/O — callers handle JSON/preview emission.

    Parallelism: each iteration dispatches N independent (random+hill_climb)
    searches to a ProcessPoolExecutor (N = profile.max_threads or cpu_count).
    Workers read the current canvas from shared memory; main thread mutates
    the canvas in place after committing the global best shape. This sidesteps
    Python's GIL.
    """

    def __init__(
        self,
        target_rgb: np.ndarray,
        config: EngineConfig,
        alpha_mask: np.ndarray | None = None,
        base_canvas: np.ndarray | None = None,
        importance_map: np.ndarray | None = None,
        preview_background: tuple[int, int, int] | None = None,
    ) -> None:
        """Image → shapes generator.

        Optional model-assist hooks (see ``fd6.shapegen.assist``):

        * ``base_canvas`` (H×W×3 uint8) — a low-frequency under-paint to seed
          the canvas with instead of a flat average. Ellipses then only correct
          the residual high-frequency detail, so the same fidelity is reached
          with fewer layers ("hybrid base"). Defaults preserve the original
          flat-average / grey-buffer start.
        * ``importance_map`` (H×W float32) — replaces the built-in Sobel
          edge-weight used to bias shape placement, letting a saliency/structure
          map concentrate the layer budget on detail. None → unchanged Sobel
          behaviour.
        * ``preview_background`` (r, g, b) — the solid colour the fit-buffer ring
          (alpha == 0) is filled with for the *default* (non-sticker) render, so
          the canvas (W×H) is visible and the live preview matches an Import-JSON
          reload. ``None`` selects the legacy sticker behaviour: the buffer is
          kept grey and the preview is transparent outside the silhouette. The
          buffer is never scored (the alpha mask zeroes it), so its colour does
          not perturb shape selection.
        """
        if target_rgb.ndim != 3 or target_rgb.shape[2] != 3:
            raise ValueError("target_rgb must be HxWx3 RGB uint8")
        self.target = target_rgb.astype(np.uint8)
        self.config = config
        self.profile = config.profile
        self.h, self.w = self.target.shape[:2]
        self._opaque = bool(getattr(self.profile, "opaque", False))
        # Alpha levels swept at commit. Opaque (game-faithful) mode forces solid
        # 255 so the committed/saved colours are exactly what the injector draws;
        # otherwise the profile's translucent ladder.
        self._commit_alpha_levels = (255,) if self._opaque else self.profile.alpha_levels
        self.alpha_mask = alpha_mask if alpha_mask is not None else None
        self._preview_background = (
            tuple(int(c) for c in preview_background) if preview_background is not None else None
        )
        if self.alpha_mask is not None:
            mask3 = (self.alpha_mask > 0)[:, :, None]
            self.target = self.target * mask3.astype(np.uint8)
            initial_canvas = np.full((self.h, self.w, 3), 40, dtype=np.uint8)
            # Default mode: paint the (non-scored) buffer ring the user's chosen
            # canvas colour so the W×H frame is visible. Sticker mode keeps grey.
            if self._preview_background is not None:
                initial_canvas[self.alpha_mask == 0] = self._preview_background
        else:
            avg = self.target.reshape(-1, 3).mean(axis=0).astype(np.uint8)
            initial_canvas = np.tile(avg, (self.h, self.w, 1)).astype(np.uint8)

        # Hybrid base: seed the canvas from a supplied under-paint so the search
        # starts close to the target and spends its layers on detail. In sticker
        # mode force the buffer region back to grey 40 so the seed obeys the same
        # transparent-outside convention the rest of the engine assumes.
        if base_canvas is not None:
            base = np.ascontiguousarray(base_canvas).astype(np.uint8)
            if base.shape != (self.h, self.w, 3):
                raise ValueError("base_canvas must match target as HxWx3 uint8")
            if self.alpha_mask is not None:
                mask3 = (self.alpha_mask > 0)[:, :, None]
                # Outside the subject use the chosen canvas colour (default mode)
                # or grey (sticker), matching the non-base seeding above.
                buffer_fill = np.array(self._preview_background if self._preview_background is not None
                                       else (40, 40, 40), dtype=np.uint8)
                base = np.where(mask3, base, buffer_fill).astype(np.uint8)
            initial_canvas = base

        # Allocate the shared canvas. Workers attach to this same buffer by name.
        self._canvas_shm: shared_memory.SharedMemory | None = shared_memory.SharedMemory(
            create=True, size=initial_canvas.nbytes,
        )
        self.canvas = np.ndarray(initial_canvas.shape, dtype=np.uint8, buffer=self._canvas_shm.buf)
        self.canvas[:] = initial_canvas
        # The exact seed the engine starts from (flat avg / buffer / hybrid base).
        # Kept so the coverage-aware final polish can rebuild the stack from it.
        self._seed_canvas = initial_canvas.copy()

        self.shapes: list[Shape] = []

        # Edge-weighted importance map: built ONCE from the target so the
        # scoring functions can boost contribution from edges (eyes, mouths,
        # thin outlines). Folds the alpha mask in too so transparent-buffer
        # pixels stay 0. Stored as `_base_edge_weight` (immutable); the LIVE
        # `edge_weight` shared-memory buffer starts at the base and is
        # periodically reblended with the residual error map below so unfinished
        # regions get boosted late in generation.
        if importance_map is not None:
            imp = np.ascontiguousarray(importance_map).astype(np.float32)
            if imp.shape != (self.h, self.w):
                raise ValueError("importance_map must be HxW float matching the target")
            if self.alpha_mask is not None:
                imp = imp * (self.alpha_mask > 0).astype(np.float32)
            self._base_edge_weight = imp
        else:
            self._base_edge_weight = compute_edge_weight(self.target, self.alpha_mask).astype(np.float32)
        self._edge_weight_shm: shared_memory.SharedMemory | None = shared_memory.SharedMemory(
            create=True, size=self._base_edge_weight.nbytes,
        )
        self.edge_weight = np.ndarray(
            self._base_edge_weight.shape, dtype=np.float32, buffer=self._edge_weight_shm.buf,
        )
        self.edge_weight[:] = self._base_edge_weight

        # self.rms is the user-facing "how close is the canvas to the target"
        # number that shows in the GUI progress bar. Compute it WITHOUT the
        # edge-weight so the displayed scale stays comparable to prior versions
        # of FD6. The edge weight is still active inside score_shape — that's
        # where it actually drives shape selection.
        self.rms = rms_error(self.canvas, self.target, self.alpha_mask)
        self.start_rms = self.rms
        self._stop = False
        self._pause = False
        self._center_cdf: tuple | None = None  # cached residual sampling grid
        seed = config.seed or int(time.time() * 1000) & 0xFFFFFFFF
        self.rng = random.Random(seed)

        self._n_workers = _safe_worker_count(
            user_requested=self.profile.max_threads,
            random_samples=self.profile.random_samples,
        )
        # Stash the worker init args; the CPU ProcessPool is created lazily so
        # GPU runs don't pay the process-spawn cost (and so a GPU→CPU fallback
        # can spin it up on demand).
        self._initargs = (
            self.target.tobytes(), self.target.shape,
            self._canvas_shm.name, self.canvas.shape,
            self.alpha_mask.tobytes() if self.alpha_mask is not None else None,
            self.alpha_mask.shape if self.alpha_mask is not None else None,
            self._edge_weight_shm.name, self.edge_weight.shape,
        )
        self._executor: ProcessPoolExecutor | None = None

        # Resolve the compute backend once. GPU (CuPy) only handles ellipse-only
        # runs; anything else uses the CPU path. Any failure to build the GPU
        # searcher degrades to CPU. `self._backend` is the *effective* backend.
        from fd6.shapegen import gpu as _gpu
        self._gpu = None
        self._backend = "cpu"
        self._gpu_fallback_reason = ""
        self._backend_announced = "cpu"
        ellipse_only = all(t in ("rotated_ellipse", "ellipse") for t in (self.profile.shape_types or []))
        requested = _gpu.resolve_backend(getattr(self.profile, "compute_backend", "auto"))
        if requested == "gpu" and ellipse_only:
            try:
                self._gpu = _gpu.OpenCLEllipseSearcher(
                    self.target, self.alpha_mask, self.edge_weight,
                    **({"search_alpha": 1.0} if self._opaque else {}),
                )
                self._backend = "gpu"
            except Exception:
                self._gpu = None
                self._backend = "cpu"

    def _ensure_executor(self) -> None:
        """Create the CPU worker pool on first use (CPU runs and GPU fallback)."""
        if self._executor is None:
            self._executor = ProcessPoolExecutor(
                max_workers=self._n_workers,
                initializer=_init_worker,
                initargs=self._initargs,
            )

    def request_stop(self) -> None:
        self._stop = True

    def set_pause(self, paused: bool) -> None:
        self._pause = paused

    def _preview_canvas(self) -> np.ndarray:
        """Return the canvas as RGB or RGBA for emit-to-preview events.

        Sticker mode (``preview_background is None`` with an alpha mask): attach
        the mask as the 4th channel so the preview renders transparent outside
        the silhouette — matching the (transparent) source PNG.

        Default mode (``preview_background`` set): return an opaque RGB canvas
        with the fit-buffer ring forced to the chosen colour. This makes the
        W×H canvas visible during live rendering and matches an Import-JSON
        reload (which composites the same shapes over the same solid colour),
        instead of the old transparent buffer that hid the canvas entirely.
        """
        if self.alpha_mask is not None and self._preview_background is None:
            return np.dstack([self.canvas, self.alpha_mask]).copy()
        if self.alpha_mask is not None and self._preview_background is not None:
            out = self.canvas.copy()
            out[self.alpha_mask == 0] = self._preview_background  # crisp frame, drop any edge spill
            return out
        return self.canvas.copy()

    def seed_shapes(self, shapes: list[Shape]) -> None:
        """Resume mode: replay shapes onto the canvas before generation starts.

        Opaque mode forces resumed shapes solid (alpha 255) and paints them as flat
        layers, so a resumed run stays game-faithful exactly like a fresh one — the
        injector draws them solid regardless of any saved per-shape alpha.
        """
        for s in shapes:
            if self._opaque:
                c = s.color
                s = s.with_color((int(c[0]), int(c[1]), int(c[2]), 255))
                self.canvas[:] = composite_fixed(self.canvas, s, self.alpha_mask)
                self.rms = rms_error(self.canvas, self.target, self.alpha_mask, self.edge_weight)
            else:
                new_canvas, new_rms = composite(self.canvas, s, self.target, self.alpha_mask, self.edge_weight)
                self.canvas[:] = new_canvas  # write into shared memory
                self.rms = new_rms
            self.shapes.append(s)

    # Residual reblend disabled in v0.4.0 — the size-schedule + edge-weight
    # combination already moves enough budget into detail regions on its own;
    # leaving the residual on top biased the back-half of generation toward
    # smearing big shapes over high-error areas (the opposite of what we
    # want). Flip RESIDUAL_REFRESH_EVERY back to a finite value to re-enable.
    RESIDUAL_REFRESH_EVERY = 0
    RESIDUAL_BOOST = 4.0

    # Residual-guided candidate placement (sampling.py). The coarse sampling grid
    # is rebuilt every few commits — the residual barely shifts per single shape,
    # so reusing it for a handful of iterations keeps the O(H·W) reduction off the
    # hot path while still steering candidates at the regions that need work.
    SAMPLER_GRID_N = 48
    SAMPLER_REFRESH_EVERY = 4

    # Coverage-aware polish only re-fits a shape whose visible (topmost) body is
    # at least this fraction of its area — keeps the translucency approximation
    # from bleeding re-coloured covered shapes up through later layers.
    REFIT_MIN_VISIBLE = 0.6

    def _refresh_residual_weight(self) -> None:
        """Reblend `self.edge_weight` (shared memory) with current residual error.

        Per-pixel residual = mean abs diff(target, canvas) across RGB, in [0..1].
        New weight = base * (1 + (RESIDUAL_BOOST - 1) * residual). Areas where
        the canvas is already close to target stay at the base edge weight;
        unfinished regions get up to RESIDUAL_BOOST× their original weight so
        subsequent workers preferentially place shapes there.
        """
        diff = np.abs(self.canvas.astype(np.float32) - self.target.astype(np.float32)).mean(axis=2) / 255.0
        boost = 1.0 + (self.RESIDUAL_BOOST - 1.0) * diff.astype(np.float32)
        self.edge_weight[:] = self._base_edge_weight * boost

    def _refit_colors_coverage_aware(self) -> None:
        """Coverage-aware colour polish after generation (geometry frozen).

        A forward-greedy result fits each shape's colour over its FULL footprint —
        including pixels a later shape will paint over. This pass re-solves each
        shape's (colour, alpha) over only the pixels where it stays VISIBLE in the
        final stack (topmost), against the canvas beneath it, so the colour serves
        the pixels that actually show. Plain forward colour re-fitting can't help a
        greedy stack (it reproduces the same colours); this can because it uses the
        final coverage the greedy pass didn't know yet.

        Two O(n·area) passes, no per-shape storage: an int32 owner map records the
        topmost shape per pixel, then a forward rebuild re-fits + recomposites.
        Shapes left fully hidden keep their colour (composite_fixed) so the
        translucent stack underneath is preserved.
        """
        n = len(self.shapes)
        if n == 0:
            return
        w, h = self.w, self.h
        owner = np.full((h, w), -1, dtype=np.int32)
        for i, s in enumerate(self.shapes):
            mask_local, bbox = s.rasterize_mask(w, h)
            x0, y0, x1, y1 = bbox
            if x1 <= x0 or y1 <= y0 or mask_local.size == 0:
                continue
            owner[y0:y1, x0:x1][mask_local >= 128] = i
        canvas = self._seed_canvas.copy()
        for i, s in enumerate(self.shapes):
            mask_local, bbox = s.rasterize_mask(w, h)
            x0, y0, x1, y1 = bbox
            if x1 <= x0 or y1 <= y0 or mask_local.size == 0:
                continue
            body = mask_local >= 128
            body_n = int(body.sum())
            visible_body = int(((owner[y0:y1, x0:x1] == i) & body).sum())
            # Only re-fit shapes that stay MOSTLY visible. Re-colouring a shape
            # that a later translucent layer covers would bleed its new colour up
            # through that layer (perceptible haze for a marginal RMS gain), so
            # those keep the colour generation already gave them.
            if body_n > 0 and visible_body / body_n >= self.profile.refit_min_visible:
                visible = np.where(owner[y0:y1, x0:x1] == i, mask_local, np.uint8(0))
                canvas, _ = composite_optimal(
                    canvas, s, self.target, self.alpha_mask, self.edge_weight,
                    alpha_levels=self._commit_alpha_levels,
                    fit_mask_local=visible,
                )
            else:
                canvas = composite_fixed(canvas, s, self.alpha_mask)
        self.canvas[:] = canvas
        self.rms = rms_error(self.canvas, self.target, self.alpha_mask, self.edge_weight)

    def _max_size_frac_for_progress(self, progress: float) -> float:
        """Shape-size schedule. Monotonically decreasing across iteration progress.

        Per-candidate scoring cost is O(bbox_area) and bbox area scales with
        `max_size_frac²`, so an early tier with `max_size_frac=1.0` (canvas-
        spanning shapes) is ~16× more expensive than the legacy `0.25`
        default. The values below keep T1 noticeably larger than legacy (for
        tonal coverage) without exploding scoring cost at higher
        max_resolutions (4K / 8K targets).
        """
        if progress < 0.25:
            return 0.30        # 0–25%: ~30% canvas — modest bump over legacy for tonal blocks
        if progress < 0.50:
            return 0.22        # 26–50%: ~22% canvas
        if progress < 0.75:
            return 0.15        # 51–75%: ~15% canvas
        return 0.10            # 76–100%: 10% canvas — fine detail only

    def _parallel_search(self, types: list[str], n_random: int, n_mutate: int,
                         max_size_frac: float | None = None,
                         center_cdf: tuple | None = None) -> tuple[float, Shape | None]:
        """Dispatch N independent FULL searches in parallel; return (best_score, best_shape).

        Each worker does the FULL `random_samples` random search (not a slice of
        it), picks its own local best, hill-climbs that, and returns. Main picks
        the global best across all workers.

        This preserves v0.2.0-equivalent per-iteration quality (each worker
        matches what the old single-chain code did) and adds parallel restarts
        on top: with N workers we get N independent attempts and keep the best.
        Splitting `random_samples` across workers (what an earlier rev did)
        gave each chain a much worse starting point and visibly degraded early
        shape selection — that's the regression we're correcting here.
        """
        self._ensure_executor()
        n_random = max(1, n_random)
        n_mutate = max(1, n_mutate)
        args_list = [
            (types, n_random, n_mutate, self.w, self.h,
             self.rng.randint(0, 2**31 - 1), max_size_frac, center_cdf,
             self.profile.guided_fraction, self._opaque)
            for _ in range(self._n_workers)
        ]
        best_score = float("inf")
        best_shape: Shape | None = None
        worker_errors: list[str] = []
        try:
            for (score, color, shape, err) in self._executor.map(_worker_independent_search, args_list):
                if err is not None:
                    worker_errors.append(err)
                    continue
                if shape is not None and score < best_score:
                    shape.color = color
                    best_score, best_shape = score, shape
        except BrokenProcessPool as exc:
            # A worker process died outright (most commonly the OS killed it for
            # running out of memory at high Max resolution / sample counts).
            raise EngineWorkerError(
                "A worker process was terminated unexpectedly — this usually "
                "means it ran out of memory. Try lowering Max resolution, "
                "Random samples, or Threads, or pick a lighter profile."
            ) from exc
        # Survivors carry the iteration: only fail if EVERY worker errored and we
        # have nothing to commit. Surface the underlying worker message so the
        # cause is visible instead of a cryptic exception type.
        if best_shape is None and worker_errors:
            raise EngineWorkerError(
                "Shape search failed in every worker. First error: "
                + worker_errors[0]
            )
        return best_score, best_shape

    def _search(self, types: list[str], n_random: int, n_mutate: int,
                max_size_frac: float | None = None,
                center_cdf: tuple | None = None) -> tuple[float, Shape | None]:
        """Dispatch one iteration's search to the active backend.

        GPU runs in the main process (one batched search). If a GPU op fails at
        runtime, we permanently fall back to the CPU pool for the rest of the run
        and record it so `run()` can tell the user via a backend event.
        """
        if self._backend == "gpu" and self._gpu is not None:
            try:
                return self._gpu.search(self.canvas, n_random, n_mutate, max_size_frac,
                                        self.rng, center_cdf=center_cdf,
                                        guided_fraction=self.profile.guided_fraction)
            except Exception as exc:
                # One-time graceful degrade — never crash a render over the GPU.
                self._backend = "cpu"
                self._gpu = None
                self._gpu_fallback_reason = f"{type(exc).__name__}: {exc}"
        return self._parallel_search(types, n_random, n_mutate, max_size_frac, center_cdf=center_cdf)

    def run(self) -> Iterable[EngineEvent]:
        p = self.profile
        types = [t for t in p.shape_types if t]
        if not types:
            types = ["rotated_ellipse"]
        # Per-iteration type rotation. Without this, every worker picks a type
        # at random and ellipses (which fit organic content best) win the
        # fitness comparison nearly every iteration, so checked rectangle /
        # rotated_rectangle types produce zero shapes in the final JSON. With
        # rotation, each iteration is locked to a single type so every
        # checked type gets dedicated commit slots in proportion to how many
        # types are enabled.
        type_cursor = 0
        save_at = set(p.save_at)
        # Tell the GUI which backend actually ran (status bar). `self._backend`
        # is the resolved/effective backend after any GPU build attempt.
        from fd6.shapegen import gpu as _gpu
        self._gpu_fallback_reason = ""
        self._backend_announced = self._backend
        yield EngineEvent(kind="backend", message=_gpu.backend_label(self._backend))
        try:
            consecutive_skips = 0
            MAX_CONSECUTIVE_SKIPS = 80
            while len(self.shapes) < p.stop_at and not self._stop:
                while self._pause and not self._stop:
                    time.sleep(0.05)

                iter_types = [types[type_cursor % len(types)]]
                type_cursor += 1

                progress = len(self.shapes) / max(1, p.stop_at)
                size_cap = self._max_size_frac_for_progress(progress)

                # Rebuild the residual-guided sampling grid every few commits.
                if self._center_cdf is None or len(self.shapes) % self.SAMPLER_REFRESH_EVERY == 0:
                    self._center_cdf = build_center_cdf(
                        self.canvas, self.target, self.edge_weight, grid_n=self.SAMPLER_GRID_N,
                    )

                refined_score, refined = self._search(
                    iter_types, max(1, p.random_samples), max(1, p.mutated_samples),
                    max_size_frac=size_cap, center_cdf=self._center_cdf,
                )
                # If the GPU degraded to CPU mid-run, announce the new backend once.
                if self._backend != self._backend_announced:
                    self._backend_announced = self._backend
                    note = _gpu.backend_label(self._backend)
                    if self._gpu_fallback_reason:
                        note += " (GPU unavailable mid-run, switched to CPU)"
                    yield EngineEvent(kind="backend", message=note)

                # Sticker mode: refined must fit essentially entirely inside
                # the opaque region. If it doesn't, retry up to 5 times then
                # skip this iteration.
                if self.alpha_mask is not None:
                    sticker_attempts = 0
                    while sticker_attempts < 5:
                        if refined is not None and refined_score != float("inf"):
                            break
                        refined_score, refined = self._search(
                            iter_types, max(1, p.random_samples), max(1, p.mutated_samples),
                            max_size_frac=size_cap, center_cdf=self._center_cdf,
                        )
                        sticker_attempts += 1
                    else:
                        consecutive_skips += 1
                        if consecutive_skips >= MAX_CONSECUTIVE_SKIPS:
                            # Same coverage-aware polish the normal exit runs —
                            # this early `done` must not save an unpolished canvas
                            # when refit_final is on (quality presets 2-4).
                            if self.profile.refit_final and not self._stop:
                                self._refit_colors_coverage_aware()
                            yield EngineEvent(
                                kind="done",
                                shape_count=len(self.shapes),
                                rms=self.rms,
                                canvas=self._preview_canvas(),
                                message=(
                                    f"Stopped early at {len(self.shapes)} shapes — couldn't "
                                    f"fit any more inside the opaque region after {MAX_CONSECUTIVE_SKIPS} "
                                    "consecutive attempts. Try increasing 'Random samples' or "
                                    "enabling smaller shape types."
                                ),
                            )
                            return
                        continue
                    consecutive_skips = 0

                if refined is None:
                    continue

                # Commit. Update shared canvas in place so next iteration's
                # workers see the new state on their next read. composite_optimal
                # also picks the per-shape alpha that fits the region best.
                new_canvas, new_rms = composite_optimal(self.canvas, refined, self.target, self.alpha_mask, self.edge_weight,
                                                        alpha_levels=self._commit_alpha_levels)
                self.canvas[:] = new_canvas
                self.rms = new_rms
                self.shapes.append(refined)
                count = len(self.shapes)

                # Periodic completeness check (currently disabled — see
                # RESIDUAL_REFRESH_EVERY note). When > 0, recomputes the
                # importance map so under-painted regions get extra weight on
                # the next batch of worker searches.
                if self.RESIDUAL_REFRESH_EVERY > 0 and count > 0 and count % self.RESIDUAL_REFRESH_EVERY == 0:
                    self._refresh_residual_weight()

                yield EngineEvent(kind="shape_committed", shape_count=count, rms=self.rms)

                if p.preview_every and (count % p.preview_every == 0):
                    yield EngineEvent(kind="preview", shape_count=count, rms=self.rms, canvas=self._preview_canvas())

                if count in save_at or (p.save_every and count % p.save_every == 0):
                    yield EngineEvent(kind="checkpoint", shape_count=count, rms=self.rms)

            # Coverage-aware colour polish before emitting the final canvas (opt-in).
            if self.profile.refit_final and not self._stop:
                self._refit_colors_coverage_aware()
            yield EngineEvent(kind="done", shape_count=len(self.shapes), rms=self.rms, canvas=self._preview_canvas())
        except EngineWorkerError as exc:
            # Already a user-facing, actionable message — show it as-is.
            yield EngineEvent(kind="error", message=str(exc))
        except Exception as exc:
            yield EngineEvent(kind="error", message=f"{type(exc).__name__}: {exc}")
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            if self._canvas_shm is not None:
                self._canvas_shm.close()
                self._canvas_shm.unlink()
                self._canvas_shm = None
        except Exception:
            pass
        try:
            if self._edge_weight_shm is not None:
                self._edge_weight_shm.close()
                self._edge_weight_shm.unlink()
                self._edge_weight_shm = None
        except Exception:
            pass

    def __del__(self) -> None:
        try:
            self._shutdown()
        except Exception:
            pass
