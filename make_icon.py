"""Draw the AccountDeck 2 app icon and write AccountDeck2.icns, standard library only.

    python make_icon.py [--icns [PATH]]     writes the .icns (default: AccountDeck2.icns next to this file)

The icon follows Apple's macOS app icon grid. Everything is designed on a 1024-unit canvas:

- the body is an 824-unit square centred on the canvas (100 units of transparent margin on each side)
  with continuous-curvature corners: each corner is a superellipse arc (|x|^p + |y|^p = r^p), which
  meets the straight edges with zero curvature, like Apple's "squircle" rather than a plain rounded
  rectangle. Its radius and exponent give the same diagonal inset as Apple's 185-unit corner;
- a soft drop shadow (a wide ambient one and a tight contact one) falls below the body, inside the margin;
- the body is a dark vertical gradient around the dashboard's dark surface (#0F131A) with a thin light
  rim that is strongest along the top edge.

On the body sits the AccountDeck mark (accountdeck/web/icon.svg): an accent ring whose arc runs three
quarters clockwise from 12 o'clock with round caps over a faint track, and a four-point spark in the
open quarter at 10:30.

Each pixel is shaded once from signed distances to these shapes, with the coverage of a pixel-wide box
filter, so every size is drawn from the geometry itself (crisp at 16 px, smooth at 1024 px) without
supersampling the whole image; only the small spark, whose arms are thinner than its distance field
assumes, is supersampled. Below 64 px the ring is thickened and the spark enlarged a little, so the mark
still reads in a Finder list or a menu.

The ten PNGs of an iconset (16 to 512 pt, each at 1x and 2x) are packed with /usr/bin/iconutil; where it
is missing the .icns is written directly from the same PNGs.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import zlib

CANVAS = 1024.0
CENTRE = CANVAS / 2
BODY_HALF = 412.0                   # the 824-unit body
CORNER_RADIUS, CORNER_EXPONENT = 260.0, 2.9

BODY_TOP = (0x1B, 0x22, 0x30)       # the gradient brackets the dark --surface-1 (#0F131A)
BODY_BOTTOM = (0x0A, 0x0D, 0x13)
RIM_WIDTH = 5.0                     # the light inner rim, in canvas units (at least one pixel)
RIM_ALPHA_TOP, RIM_ALPHA_BOTTOM = 0.22, 0.06
# (vertical offset, Gaussian sigma, opacity) of the two black drop shadows, in canvas units.
SHADOWS = ((22.0, 26.0, 0.30), (6.0, 5.0, 0.22))

RING_RADIUS, RING_WIDTH = 245.0, 90.0
ARC_SWEEP = 1.5 * math.pi           # three quarters, clockwise from 12 o'clock
ARC_START = (0x34, 0x86, 0xE6)      # the accent (#3D8FE8) runs a little deeper to a little brighter
ARC_END = (0x5B, 0xA8, 0xF6)
TRACK = (0x3D, 0x8F, 0xE8)
TRACK_ALPHA = 0.22
GLOW_ALPHA, GLOW_SPREAD = 0.13, 70.0    # a faint accent glow around the arc on the dark body

SPARK = (0xF2, 0xF5, 0xFA)          # --text (dark)
SPARK_ANGLE = 1.75 * math.pi        # 10:30, on the ring's centre line
SPARK_RADIUS = 104.0                # centre to tip

# (iconset file name, pixel size) of the ten images macOS reads from an .icns.
ICONSET = (("icon_16x16.png", 16), ("icon_16x16@2x.png", 32), ("icon_32x32.png", 32),
           ("icon_32x32@2x.png", 64), ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
           ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512), ("icon_512x512.png", 512),
           ("icon_512x512@2x.png", 1024))
# The .icns entry type of each iconset file, for writing the file without iconutil.
ICNS_TYPES = {"icon_16x16.png": "icp4", "icon_16x16@2x.png": "ic11", "icon_32x32.png": "icp5",
              "icon_32x32@2x.png": "ic12", "icon_128x128.png": "ic07", "icon_128x128@2x.png": "ic13",
              "icon_256x256.png": "ic08", "icon_256x256@2x.png": "ic14", "icon_512x512.png": "ic09",
              "icon_512x512@2x.png": "ic10"}
ICONUTIL = "/usr/bin/iconutil"
DEFAULT_TARGET = Path(__file__).with_name("AccountDeck2.icns")


@dataclass(frozen=True)
class Style:
    """What changes with the pixel size: small icons need a bolder mark to stay legible, and a lighter
    shadow, which would otherwise blur the edge of a body only a dozen pixels wide."""
    ring_width: float
    spark_radius: float
    spark_samples: int
    shadow: float               # share of the full shadow opacity

    @staticmethod
    def for_size(size: int) -> "Style":
        if size <= 16:
            return Style(RING_WIDTH * 1.3, SPARK_RADIUS * 1.35, 8, 0.35)
        if size <= 32:
            return Style(RING_WIDTH * 1.15, SPARK_RADIUS * 1.2, 8, 0.6)
        if size <= 64:
            return Style(RING_WIDTH * 1.05, SPARK_RADIUS * 1.1, 6, 0.85)
        return Style(RING_WIDTH, SPARK_RADIUS, 4 if size <= 512 else 3, 1.0)


# ---- signed distances (canvas units; negative inside) ----

def body_distance(x: float, y: float) -> float:
    """The body square with superellipse corners."""
    straight = BODY_HALF - CORNER_RADIUS
    qx, qy = abs(x - CENTRE) - straight, abs(y - CENTRE) - straight
    if qx > 0 and qy > 0:
        p = CORNER_EXPONENT
        return (qx ** p + qy ** p) ** (1 / p) - CORNER_RADIUS
    return max(qx, qy) - CORNER_RADIUS


def _angle(x: float, y: float) -> float:
    """0 at 12 o'clock, growing clockwise (screen y points down)."""
    return math.atan2(x - CENTRE, CENTRE - y) % (2 * math.pi)


def _arc_ends(radius: float) -> tuple[tuple[float, float], tuple[float, float]]:
    return ((CENTRE, CENTRE - radius),
            (CENTRE + radius * math.sin(ARC_SWEEP), CENTRE - radius * math.cos(ARC_SWEEP)))


def arc_distance(x: float, y: float, angle: float, width: float) -> float:
    """The accent arc with round caps."""
    if angle <= ARC_SWEEP:
        return abs(math.hypot(x - CENTRE, y - CENTRE) - RING_RADIUS) - width / 2
    return min(math.hypot(x - ex, y - ey) for ex, ey in _arc_ends(RING_RADIUS)) - width / 2


def _arc_progress(angle: float) -> float:
    """0 at the arc's start, 1 at its end; in the open quarter, whichever cap is nearer (so the start cap,
    which spills past 12 o'clock, keeps the start colour)."""
    if angle <= ARC_SWEEP:
        return angle / ARC_SWEEP
    return 1.0 if angle - ARC_SWEEP < 2 * math.pi - angle else 0.0


def _spark_centre() -> tuple[float, float]:
    return (CENTRE + RING_RADIUS * math.sin(SPARK_ANGLE), CENTRE - RING_RADIUS * math.cos(SPARK_ANGLE))


def in_spark(x: float, y: float, radius: float) -> bool:
    """A four-point star: the astroid |dx|^(2/3) + |dy|^(2/3) <= r^(2/3)."""
    sx, sy = _spark_centre()
    return (abs(x - sx) / radius) ** (2 / 3) + (abs(y - sy) / radius) ** (2 / 3) <= 1.0


# ---- shading ----

def _coverage(distance: float, unit: float) -> float:
    """How much of a pixel `unit` canvas units wide an edge at `distance` covers (a box filter)."""
    return min(1.0, max(0.0, 0.5 - distance / unit))


def _mix(base, top, alpha: float):
    return tuple(b + (t - b) * alpha for b, t in zip(base, top))


def _shadow_alpha(x: float, y: float, unit: float, strength: float) -> float:
    clear = 1.0
    for offset, sigma, opacity in SHADOWS:
        spread = math.hypot(sigma, unit / 2) * math.sqrt(2)     # never sharper than the pixel
        clear *= 1 - strength * opacity * 0.5 * math.erfc(body_distance(x, y - offset) / spread)
    return 1 - clear


def _spark_coverage(x: float, y: float, unit: float, style: Style) -> float:
    sx, sy = _spark_centre()
    reach = style.spark_radius + unit
    if abs(x - sx) > reach or abs(y - sy) > reach:
        return 0.0
    n = style.spark_samples
    step = unit / n
    left, top = x - unit / 2 + step / 2, y - unit / 2 + step / 2
    hits = sum(in_spark(left + i * step, top + j * step, style.spark_radius) for j in range(n) for i in range(n))
    return hits / (n * n)


def _body_colour(x: float, y: float, inside: float, unit: float, style: Style):
    """The body's colour at a point inside it (`inside` is the body coverage there)."""
    level = min(1.0, max(0.0, (y - (CENTRE - BODY_HALF)) / (2 * BODY_HALF)))
    colour = _mix(BODY_TOP, BODY_BOTTOM, level)
    rim = inside - _coverage(body_distance(x, y) + max(RIM_WIDTH, unit), unit)
    if rim > 0:
        colour = _mix(colour, (255, 255, 255), rim * (RIM_ALPHA_TOP + (RIM_ALPHA_BOTTOM - RIM_ALPHA_TOP) * level))
    ring = abs(math.hypot(x - CENTRE, y - CENTRE) - RING_RADIUS) - style.ring_width / 2
    angle = _angle(x, y)
    arc = arc_distance(x, y, angle, style.ring_width)
    if arc > 0:
        colour = _mix(colour, ARC_START, GLOW_ALPHA * math.exp(-(arc / GLOW_SPREAD) ** 2))
    colour = _mix(colour, TRACK, TRACK_ALPHA * _coverage(ring, unit))
    covered = _coverage(arc, unit)
    if covered:
        colour = _mix(colour, _mix(ARC_START, ARC_END, _arc_progress(angle)), covered)
    spark = _spark_coverage(x, y, unit, style)
    return _mix(colour, SPARK, spark) if spark else colour


def shade(x: float, y: float, unit: float, style: Style) -> tuple[float, float, float, float]:
    """(r, g, b, a) of the pixel centred at (x, y), straight alpha, alpha 0..1."""
    inside = _coverage(body_distance(x, y), unit)
    shadow = _shadow_alpha(x, y, unit, style.shadow) if inside < 1 else 0.0
    if inside <= 0:
        return 0.0, 0.0, 0.0, shadow
    colour = _body_colour(x, y, inside, unit, style)
    alpha = inside + shadow * (1 - inside)
    return (*(c * inside / alpha for c in colour), alpha)    # the shadow under the edge is black


def render(size: int) -> bytes:
    """The icon as `size` × `size` RGBA pixels, straight alpha, top row first."""
    unit, style = CANVAS / size, Style.for_size(size)
    pixels = bytearray(size * size * 4)
    reach = BODY_HALF + max(offset + 3 * sigma for offset, sigma, _ in SHADOWS) + unit
    for row in range(size):
        y = (row + 0.5) * unit
        if abs(y - CENTRE) > reach:
            continue
        for column in range(size):
            x = (column + 0.5) * unit
            if abs(x - CENTRE) > reach:
                continue
            r, g, b, a = shade(x, y, unit, style)
            if a > 0:
                at = (row * size + column) * 4
                pixels[at:at + 4] = bytes((round(r), round(g), round(b), round(255 * a)))
    return bytes(pixels)


# ---- files ----

def png(size: int, rgba: bytes) -> bytes:
    """A truecolour-with-alpha PNG of `size` × `size` RGBA pixels."""
    stride = size * 4
    rows = b"".join(b"\x00" + rgba[start:start + stride] for start in range(0, len(rgba), stride))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 9))
            + chunk(b"IEND", b""))


def iconset_pngs() -> dict[str, bytes]:
    """The ten iconset PNGs by file name, each pixel size drawn once."""
    drawn: dict[int, bytes] = {}
    for _name, size in ICONSET:
        if size not in drawn:
            drawn[size] = png(size, render(size))
    return {name: drawn[size] for name, size in ICONSET}


def write_iconset(folder: Path, images: dict[str, bytes]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in images.items():
        (folder / name).write_bytes(data)


def icns_bytes(images: dict[str, bytes]) -> bytes:
    """An .icns written directly: the 'icns' header, then one PNG entry per iconset file."""
    entries = b"".join(ICNS_TYPES[name].encode("ascii") + struct.pack(">I", 8 + len(data)) + data
                       for name, data in images.items())
    return b"icns" + struct.pack(">I", 8 + len(entries)) + entries


def write_icns(target: Path) -> int:
    """Draws the icon and writes `target` atomically; returns its size in bytes."""
    target = Path(target)
    images = iconset_pngs()
    temporary = target.with_name(target.name + ".tmp")
    with tempfile.TemporaryDirectory(prefix="accountdeck-icon-") as work:
        if Path(ICONUTIL).is_file():
            iconset, packed = Path(work) / "AccountDeck2.iconset", Path(work) / "AccountDeck2.icns"
            write_iconset(iconset, images)
            # iconutil insists on an .icns name, so it packs inside the work folder first.
            subprocess.run([ICONUTIL, "-c", "icns", "-o", str(packed), str(iconset)], check=True,
                           stdout=subprocess.DEVNULL)
            data = packed.read_bytes()
        else:
            data = icns_bytes(images)
    temporary.write_bytes(data)
    temporary.replace(target)
    return target.stat().st_size


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["--icns"]:
        argv = argv[1:]
    if len(argv) > 1 or (argv and argv[0].startswith("-")):
        raise SystemExit("usage: make_icon.py [--icns [PATH]]")
    target = Path(argv[0]) if argv else DEFAULT_TARGET
    written = write_icns(target)
    print(f"Wrote {target.name} ({written} bytes, 16 to 1024 px).")


if __name__ == "__main__":
    main()
