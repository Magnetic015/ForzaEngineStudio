from __future__ import annotations

import configparser
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class Profile:
    name: str = "default"
    description: str = "Default profile"
    max_preview_size: int = 500
    max_resolution: int = 1200
    max_threads: int = 0  # 0 = auto (os.cpu_count())
    mutated_samples: int = 200
    posterize_levels: int = 256
    preview_every: int = 1
    random_samples: int = 1000
    redundant_check_every: int = 500
    save_at: list[int] = field(default_factory=lambda: [500, 1000, 1500, 2000, 2500, 3000])
    save_every: int = 100
    stop_at: int = 3000
    shape_types: list[str] = field(default_factory=lambda: ["rotated_ellipse"])
    # Coverage-aware colour polish after generation: re-solve each shape's
    # (colour, alpha) over only the pixels where it stays visible in the final
    # stack. Off by default (a no-op for the live stream until enabled).
    refit_final: bool = False
    # Coverage-aware polish only re-fits a shape whose visible (topmost) body is
    # at least this fraction of its area (see Engine._refit_colors_coverage_aware).
    refit_min_visible: float = 0.6
    # Residual-guided sampling: fraction of candidate centers drawn toward the
    # cells that still differ most from the target (see sampling.sample_centers).
    guided_fraction: float = 0.7
    # Per-shape alpha sweep evaluated when committing a shape (scoring.composite_optimal).
    alpha_levels: tuple = (60, 90, 120, 150, 180, 210, 235, 255)
    # Compute backend for the shape search: "auto" (GPU if a CUDA device + CuPy
    # are present, else CPU), "cpu" (force the multiprocess CPU path), or "gpu"
    # (force CuPy; silently falls back to CPU if unavailable or it errors).
    compute_backend: str = "auto"
    # Shape-size schedule: the max ellipse dimension (as a fraction of the canvas)
    # allowed at progress tiers <25% / <50% / <75% / >=75% of the layer budget.
    # Monotonically decreasing — big tonal blocks early, fine detail late. Both
    # backends honour it via max_size_frac. Default reproduces the long-standing
    # 0.30→0.10 schedule; a steeper tail (e.g. ...,0.04) pushes more of the budget
    # onto pixel-scale detail for high-frequency targets. See
    # Engine._max_size_frac_for_progress.
    size_caps: tuple = (0.30, 0.22, 0.15, 0.10)
    # Game-faithful OPAQUE mode. The livery injector (ds_v9) draws every shape as
    # a SOLID layer — it discards per-shape alpha (writes RGBA with alpha forced
    # to 255) and seeds no under-paint, so the in-game render is opaque
    # painter's-algorithm. When True the engine matches that exactly: every shape
    # is committed AND searched at alpha 255, so the saved colours are the ones
    # the game actually shows and the live preview is WYSIWYG. False keeps the
    # legacy translucent stack (lower local RMS, but it does NOT survive
    # injection). Production (sidecar) runs ON; see [[engine-fidelity-levers]].
    opaque: bool = False

    def to_ini(self) -> str:
        cp = configparser.ConfigParser()
        cp["profile"] = {
            "description": self.description,
            "maxPreviewSize": str(self.max_preview_size),
            "maxResolution": str(self.max_resolution),
            "maxThreads": str(self.max_threads),
            "mutatedSamples": str(self.mutated_samples),
            "posterizeLevels": str(self.posterize_levels),
            "previewEvery": str(self.preview_every),
            "randomSamples": str(self.random_samples),
            "redundantCheckEvery": str(self.redundant_check_every),
            "saveAt": ",".join(str(s) for s in self.save_at),
            "saveEvery": str(self.save_every),
            "stopAt": str(self.stop_at),
            "shapeTypes": ",".join(self.shape_types),
            "computeBackend": self.compute_backend,
            "refitFinal": str(self.refit_final),
            "refitMinVisible": str(self.refit_min_visible),
            "guidedFraction": str(self.guided_fraction),
            "alphaLevels": ",".join(str(a) for a in self.alpha_levels),
            "opaque": str(self.opaque),
        }
        from io import StringIO
        buf = StringIO()
        cp.write(buf)
        return buf.getvalue()


def _parse_int_list(s: str) -> list[int]:
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def _parse_str_list(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def load_profile(name: str, text: str) -> Profile:
    cp = configparser.ConfigParser()
    # forza-painter .ini files don't use a section header. Try parsing as-is first;
    # on MissingSectionHeaderError, prepend a synthetic [profile] header and retry.
    try:
        cp.read_string(text)
    except configparser.MissingSectionHeaderError:
        cp = configparser.ConfigParser()
        cp.read_string("[profile]\n" + text)
    if cp.has_section("profile"):
        section = cp["profile"]
    else:
        cp = configparser.ConfigParser()
        cp.read_string("[profile]\n" + text)
        section = cp["profile"]

    p = Profile(name=name)
    getstr = lambda k, d: section.get(k, str(d))
    getint = lambda k, d: int(section.get(k, str(d)))

    p.description = getstr("description", p.description)
    p.max_preview_size = getint("maxPreviewSize", p.max_preview_size)
    p.max_resolution = getint("maxResolution", p.max_resolution)
    p.max_threads = getint("maxThreads", p.max_threads)
    p.mutated_samples = getint("mutatedSamples", p.mutated_samples)
    p.posterize_levels = getint("posterizeLevels", p.posterize_levels)
    p.preview_every = getint("previewEvery", p.preview_every)
    p.random_samples = getint("randomSamples", p.random_samples)
    p.redundant_check_every = getint("redundantCheckEvery", p.redundant_check_every)
    if "saveAt" in section:
        p.save_at = _parse_int_list(section["saveAt"])
    p.save_every = getint("saveEvery", p.save_every)
    p.stop_at = getint("stopAt", p.stop_at)
    if "shapeTypes" in section:
        p.shape_types = _parse_str_list(section["shapeTypes"])
    backend = getstr("computeBackend", p.compute_backend).lower().strip()
    p.compute_backend = backend if backend in ("auto", "cpu", "gpu") else "auto"
    # Fidelity knobs (round-trip alongside the sample-budget fields). Absent keys
    # keep the dataclass defaults, so older / forza-painter INIs load unchanged.
    p.refit_final = section.getboolean("refitFinal", p.refit_final)
    p.refit_min_visible = section.getfloat("refitMinVisible", p.refit_min_visible)
    p.guided_fraction = section.getfloat("guidedFraction", p.guided_fraction)
    if "alphaLevels" in section:
        p.alpha_levels = tuple(_parse_int_list(section["alphaLevels"]))
    p.opaque = section.getboolean("opaque", p.opaque)
    return p


def load_profile_from_file(path: str | Path) -> Profile:
    path = Path(path)
    return load_profile(path.stem, path.read_text(encoding="utf-8"))


def list_bundled_profiles() -> list[Path]:
    base = Path(__file__).resolve().parent.parent / "settings" / "profiles"
    if not base.exists():
        return []
    return sorted(base.glob("*.ini"))
