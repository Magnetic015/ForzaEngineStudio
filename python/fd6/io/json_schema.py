from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Iterable

from fd6.shapegen.shapes import Shape, shape_from_json

FD6_FORMAT = "fd6.shapes"
FD6_VERSION = 1


def _parse_background(value) -> tuple[int, int, int] | None:
    """Coerce a stored/passed background to an (r, g, b) uint8 tuple, or None.

    Accepts a 3-sequence of numbers (clamped to 0..255); anything falsy or
    malformed -> None (caller falls back to the legacy grey buffer).
    """
    if not value:
        return None
    try:
        r, g, b = (int(value[0]), int(value[1]), int(value[2]))
    except (TypeError, ValueError, IndexError):
        return None
    clamp = lambda c: max(0, min(255, c))
    return (clamp(r), clamp(g), clamp(b))


@dataclass
class FD6Document:
    """v1 of the FD6 shape JSON document. See README for schema details."""

    format: str = FD6_FORMAT
    version: int = FD6_VERSION
    source_image: str = ""
    image_size: tuple[int, int] = (0, 0)  # (width, height)
    shape_count: int = 0
    generated_at: str = ""
    profile: str = ""
    # True when the JSON was generated with sticker mode (transparent backdrop —
    # "Add white background to transparent images" was UNCHECKED). Default False
    # for backwards compat with older JSONs that pre-date this field. Affects
    # how the GUI re-renders the preview on Upload JSON: sticker JSONs get a
    # transparent preview, non-sticker JSONs get a white canvas as before.
    sticker_mode: bool = False
    # Model-assist metadata (v0.5+). Empty / absent on non-assisted documents.
    # `base_image` is the hybrid under-paint the engine seeded its canvas with,
    # stored as a base64-encoded PNG so the JSON renders back exactly as the
    # engine produced it (shapes composited over the base). `assist` records
    # which assists ran for provenance/debugging. Both default empty so older
    # JSONs (and shape-only Forza exports) load unchanged.
    base_image: str = ""
    assist: dict = field(default_factory=dict)
    # Default-mode canvas fill colour (r, g, b) for the fit-buffer ring around
    # the image. Stored so Import-JSON paints the same frame the engine showed.
    # None / absent on sticker docs and older JSONs (render falls back to the
    # legacy grey 40 so pre-existing documents reload exactly as before).
    background: tuple[int, int, int] | None = None
    shapes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["image_size"] = list(self.image_size)
        d["background"] = list(self.background) if self.background is not None else None
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "FD6Document":
        fmt = data.get("format")
        if fmt != FD6_FORMAT:
            raise ValueError(f"Unsupported document format: {fmt!r} (expected {FD6_FORMAT!r})")
        ver = data.get("version")
        if ver != FD6_VERSION:
            raise ValueError(f"Unsupported document version: {ver!r} (expected {FD6_VERSION})")
        size = data.get("image_size", [0, 0])
        return cls(
            format=fmt,
            version=ver,
            source_image=str(data.get("source_image", "")),
            image_size=(int(size[0]), int(size[1])),
            shape_count=int(data.get("shape_count", len(data.get("shapes", [])))),
            generated_at=str(data.get("generated_at", "")),
            profile=str(data.get("profile", "")),
            sticker_mode=bool(data.get("sticker_mode", False)),
            base_image=str(data.get("base_image", "") or ""),
            assist=dict(data.get("assist", {}) or {}),
            background=_parse_background(data.get("background")),
            shapes=list(data.get("shapes", [])),
        )

    def materialize_shapes(self) -> list[Shape]:
        return [shape_from_json(s) for s in self.shapes]

    @classmethod
    def from_engine(
        cls,
        source_image: str,
        image_size: tuple[int, int],
        shapes: Iterable[Shape],
        profile_name: str = "",
        sticker_mode: bool = False,
        base_image: str = "",
        assist: dict | None = None,
        background: tuple[int, int, int] | None = None,
    ) -> "FD6Document":
        shape_list = [s.to_json() for s in shapes]
        return cls(
            format=FD6_FORMAT,
            version=FD6_VERSION,
            source_image=source_image,
            image_size=image_size,
            shape_count=len(shape_list),
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            profile=profile_name,
            sticker_mode=sticker_mode,
            base_image=base_image or "",
            assist=dict(assist or {}),
            background=_parse_background(background),
            shapes=shape_list,
        )
