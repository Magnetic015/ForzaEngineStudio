"""Tests for the ported in-game layer injection (fd6.inject).

The injection package is the dependency-free core described in FD6's porting
guide: it writes a rendered shape design into a running Forza process. The live
memory write needs a running game, so these tests pin the *pure* contract that a
faithful port must preserve, with a fake ProcessHandle standing in for the game:

  1. color packing forces alpha 255 (the injector draws solid layers),
  2. offset-override coercion accepts the documented value forms,
  3. each game profile carries the documented CLiveryGroup/Layer offsets, and
  4. inject() writes the exact per-field byte layout (position X/-Y, scale per
     shape type, rotation 360-angle, RGBA-255 color, shape id, mask) — the §8.1
     write spec that, if it drifts, silently corrupts the in-game render.

Runnable as a script (`python tests/test_inject.py` from python/) or via pytest.
No numpy / engine import — the injection core is standard-library only.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fd6.inject import FH6Injector, VinylGroupHandle  # noqa: E402
from fd6.inject.game_profiles import get_profile, default_profile  # noqa: E402
from fd6.inject.fh6_injector import _pack_color, _coerce_int, _is_user_ptr  # noqa: E402
from fd6.inject import fh6_injector as fhi  # noqa: E402


# A layer pointer aligned so (addr & 0xFF) == field offset, letting the fake read
# dispatch on the offset alone. Comfortably inside the _is_user_ptr window.
_LAYER_BASE = 0x10000000


class FakeProc:
    """Stand-in for win_process.ProcessHandle.

    Reads return values that make every layer score a perfect 5/5 (so inject()
    doesn't skip the slot); writes are captured by field offset for assertions.
    """

    def __init__(self, shape_id: int = 102) -> None:
        self.writes: dict[int, bytes] = {}
        self._shape_id = shape_id

    def try_read(self, addr: int, size: int) -> bytes:
        off = addr & 0xFF
        if off == fhi.LAYER_POS_OFF:      # 2 x f32, finite, in range
            return struct.pack("<2f", 1.0, 1.0)
        if off == fhi.LAYER_SCALE_OFF:    # 2 x f32, 0 < |v| <= 64
            return struct.pack("<2f", 1.0, 1.0)
        if off == fhi.LAYER_COLOR_OFF:    # any 4 readable bytes
            return b"\x00\x00\x00\x00"
        if off == fhi.LAYER_MASK_OFF:     # 0 or 1
            return b"\x00"
        if off == fhi.LAYER_SHAPE_ID_OFF:
            return bytes([self._shape_id])
        return b"\x00" * size

    def write(self, addr: int, data: bytes) -> int:
        self.writes[addr & 0xFF] = bytes(data)
        return len(data)


def _inject_one(shape: dict) -> dict[int, bytes]:
    """Inject a single shape into a 1-slot fake group; return {offset: bytes}."""
    inj = FH6Injector(profile=get_profile("fh6"))
    inj._proc = FakeProc()
    handle = VinylGroupHandle(layer_count=1, meta={"layer_addrs": [_LAYER_BASE]})
    result = inj.inject([shape], handle)
    assert result.success, f"inject should succeed, got: {result.message}"
    return inj._proc.writes


def test_pack_color_forces_alpha_255():
    assert list(_pack_color({"color": [10, 20, 30, 128]})) == [10, 20, 30, 255]
    assert list(_pack_color({"color": [1, 2, 3]})) == [1, 2, 3, 255]
    # Malformed / missing color degrades to opaque white, never crashes.
    assert list(_pack_color({"color": None})) == [255, 255, 255, 255]
    assert list(_pack_color({})) == [255, 255, 255, 255]


def test_coerce_int_accepts_documented_forms():
    assert _coerce_int(90) == 90
    assert _coerce_int("0x5A") == 90       # prefixed hex
    assert _coerce_int("90") == 90         # decimal string
    assert _coerce_int("5A") == 90         # bare hex fallback
    assert _coerce_int(True) is None       # bool is rejected (not an offset)
    assert _coerce_int("zzz") is None


def test_profiles_carry_documented_offsets():
    assert default_profile().key == "fh6"
    fh6 = get_profile("fh6")
    assert fh6.livery_count_offset == 0x5A
    assert fh6.layer_table_offset == 0x78
    assert fh6.layer_position_offset == 0x18
    assert fh6.layer_scale_offset == 0x28
    assert fh6.layer_rotation_offset == 0x50
    assert fh6.layer_color_offset == 0x74
    assert fh6.layer_mask_offset == 0x78
    assert fh6.layer_shape_id_offset == 0x7A
    assert fh6.scale_divisor_ellipse == 63.0
    assert fh6.scale_divisor_other == 127.0
    assert (fh6.shape_id_ellipse, fh6.shape_id_other) == (102, 101)
    # fh5/fh4/fh3 share the same layout (only process names differ).
    for key in ("fh5", "fh4", "fh3"):
        p = get_profile(key)
        assert (p.layer_position_offset, p.layer_scale_offset, p.layer_color_offset) == (0x18, 0x28, 0x74)


def test_get_profile_rejects_unknown():
    try:
        get_profile("fh99")
    except ValueError:
        return
    raise AssertionError("get_profile should raise ValueError on an unknown key")


def test_inject_ellipse_field_contract():
    w = _inject_one({
        "type": "rotated_ellipse", "x": 100.0, "y": 50.0,
        "rx": 63.0, "ry": 31.5, "angle": 90.0, "color": [10, 20, 30, 128],
    })
    assert w[fhi.LAYER_POS_OFF] == struct.pack("<2f", 100.0, -50.0)            # X, -Y
    assert w[fhi.LAYER_SCALE_OFF] == struct.pack("<2f", 63.0 / 63.0, 31.5 / 63.0)  # radius/63
    assert w[fhi.LAYER_ROT_OFF] == struct.pack("<f", (360.0 - 90.0) % 360.0)   # 360-angle
    assert w[fhi.LAYER_COLOR_OFF] == bytes([10, 20, 30, 255])                  # alpha forced 255
    assert w[fhi.LAYER_SHAPE_ID_OFF] == bytes([102])                           # ellipse id
    assert w[fhi.LAYER_MASK_OFF] == bytes([0])


def test_inject_rectangle_scale_conversion():
    # Rectangles store HALF extents in JSON; the game wants full-width / 127.
    w = _inject_one({
        "type": "rectangle", "x": 10.0, "y": 20.0,
        "hw": 25.0, "hh": 12.0, "color": [200, 30, 30, 200],
    })
    assert w[fhi.LAYER_POS_OFF] == struct.pack("<2f", 10.0, -20.0)
    assert w[fhi.LAYER_SCALE_OFF] == struct.pack("<2f", (25.0 * 2.0) / 127.0, (12.0 * 2.0) / 127.0)
    assert w[fhi.LAYER_SHAPE_ID_OFF] == bytes([101])                           # non-ellipse id
    assert w[fhi.LAYER_COLOR_OFF] == bytes([200, 30, 30, 255])


def test_inject_refuses_when_more_shapes_than_slots():
    inj = FH6Injector(profile=get_profile("fh6"))
    inj._proc = FakeProc()
    handle = VinylGroupHandle(layer_count=1, meta={"layer_addrs": [_LAYER_BASE]})
    result = inj.inject([{"type": "circle", "x": 0, "y": 0, "r": 5, "color": [1, 2, 3, 255]}] * 2, handle)
    assert not result.success
    assert "slot" in result.message.lower()


def test_score_layer_honors_overridden_shape_ids():
    # _score_layer must match against the SEEDED module globals (which a profile /
    # .fd6_offsets.json override updates), not the baked 101/102 literals — else it
    # rejects the very ids inject() writes under an override. Set the globals as
    # _seed_module_offsets would, and restore them so other tests aren't polluted.
    saved = (fhi.SHAPE_ID_ELLIPSE, fhi.SHAPE_ID_OTHER)
    try:
        fhi.SHAPE_ID_ELLIPSE, fhi.SHAPE_ID_OTHER = 200, 199
        assert fhi._score_layer(FakeProc(shape_id=200), _LAYER_BASE) == 5  # overridden id accepted
        assert fhi._score_layer(FakeProc(shape_id=102), _LAYER_BASE) == 4  # old baked id no longer scores
    finally:
        fhi.SHAPE_ID_ELLIPSE, fhi.SHAPE_ID_OTHER = saved


def test_inject_rejects_unsupported_triangle():
    # A triangle carries x1/y1/x2/... with no center/size, so the game's layer
    # record can't represent it. It must be skipped (not written as a tiny blob
    # at (0,0)), and the result must say so rather than reporting a clean write.
    inj = FH6Injector(profile=get_profile("fh6"))
    inj._proc = FakeProc()
    handle = VinylGroupHandle(layer_count=1, meta={"layer_addrs": [_LAYER_BASE]})
    tri = {"type": "triangle", "x1": 0, "y1": 0, "x2": 1, "y2": 1,
           "x3": 2, "y3": 0, "color": [1, 2, 3, 255]}
    result = inj.inject([tri], handle)
    assert not result.success
    assert result.shapes_written == 0
    assert not inj._proc.writes, "a triangle must not be written to any layer slot"
    assert "unsupported" in result.message.lower()


def test_inject_warns_when_template_has_extra_slots():
    # Faithful to FD6, leftover slots keep their prior shapes. Injecting a
    # smaller design into a larger template must flag that the extras are stale
    # instead of letting the success message imply a clean vinyl.
    inj = FH6Injector(profile=get_profile("fh6"))
    inj._proc = FakeProc()
    handle = VinylGroupHandle(layer_count=2, meta={"layer_addrs": [_LAYER_BASE, _LAYER_BASE + 0x100]})
    circle = {"type": "circle", "x": 0, "y": 0, "r": 5, "color": [1, 2, 3, 255]}
    result = inj.inject([circle], handle)
    assert result.success and result.shapes_written == 1
    assert "untouched" in result.message.lower()
    assert "1 of the template" in result.message


def test_iter_pattern_matches_streams_without_duplicates():
    # The chunked scanner must find a pattern that straddles a chunk boundary,
    # yield each match exactly once (no double-count from the re-read overlap),
    # and honor the alignment filter — the streaming replacement for the old
    # "buffer the whole multi-GB region, then .find()" path.
    from fd6.inject.win_process import ProcessHandle

    class _StreamProc(ProcessHandle):
        def __init__(self, data: bytes, chunk: int) -> None:
            self.handle = 1  # truthy stand-in; no real Win32 handle needed
            self._data = data
            self._TRY_READ_CHUNK = chunk

        def _try_read_chunk(self, addr: int, size: int) -> bytes | None:
            seg = self._data[addr:addr + size]
            return seg if seg else None

    pat = b"\xAA\xBB"
    data = bytearray(20)
    for off in (2, 7, 12):       # 7 straddles the chunk-8 boundary
        data[off:off + 2] = pat
    proc = _StreamProc(bytes(data), chunk=8)
    assert list(proc.iter_pattern_matches(0, len(data), pat)) == [2, 7, 12]
    # alignment=4 keeps only offsets divisible by 4.
    assert list(proc.iter_pattern_matches(0, len(data), pat, alignment=4)) == [12]


def test_is_user_ptr_bounds():
    assert not _is_user_ptr(0)
    assert not _is_user_ptr(0x1000)               # below the user-space floor
    assert _is_user_ptr(_LAYER_BASE)
    assert not _is_user_ptr(0x800000000000)       # at/above the ceiling


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
