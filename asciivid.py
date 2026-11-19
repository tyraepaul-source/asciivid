"""
asciivid — turn videos into colored ASCII frames.

Only needs the Python standard library plus the `ffmpeg` / `ffprobe`
command-line tools (used to decode the video). See README.md for the full guide.

Quick start
-----------
    import asciivid

    # 1. Convert a video into an ASCII-video file
    asciivid.convert("movie.mp4", "movie.asciv", width=160)

    # 2. Open it and iterate frames. Frames are "delta" strings by default:
    #    they only redraw what changed, so print them one after another as-is.
    video = asciivid.open_video("movie.asciv")
    for frame in video.fit_terminal():
        print(frame, end="", flush=True)

    # ...or skip the conversion and read straight from the raw video
    for frame in asciivid.open_video("movie.mp4", width="auto"):
        ...

    # 3. Or let it handle timing, cursor and live terminal resizing
    asciivid.play(video)

Resizing / options (each returns a new lightweight view, nothing is copied):
    video.resize(80)              # 80 columns, height keeps aspect ratio
    video.resize(80, 20)          # exact size
    video.resize("auto")          # fit the current terminal
    video.fit(100, 30)            # largest size fitting in 100x30
    video.fit_terminal()          # fit the current terminal
    video.with_options(color="256", charset="detailed", palette="high")
    video.with_options(fast=False)   # old behaviour: full, standalone frames

Command line:
    python asciivid.py convert movie.mp4 movie.asciv --width 160
    python asciivid.py play movie.asciv        (also accepts movie.mp4)
    python asciivid.py info movie.asciv
"""

from __future__ import annotations

import copy
import functools
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import zlib
from array import array
from typing import Iterator, List, Optional, Tuple

__all__ = ["convert", "open_video", "play", "AsciiVideo", "FrameEncoder",
           "CHARSETS", "PALETTES", "SYNC_START", "SYNC_END"]

# Dark -> bright character ramps.
CHARSETS = {
    "standard": " .:-=+*#%@",
    "detailed": " .'`^\",:;Il!i><~+_-?][}{1)(|\\/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$",
    "simple": " .oO@",
    "blocks": " ░▒▓█",
    "dense": "@",  # every cell filled; looks best with color on
}

# Terminal character cells are roughly twice as tall as they are wide.
CHAR_ASPECT = 0.5

# "Synchronized output": terminals that support it draw the whole frame at once
# (no tearing). Terminals that don't simply ignore these codes.
SYNC_START = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"

_MAGIC = b"ASCV"
_VERSION = 1
# magic, version, width, height, fps, frame_count, index_offset
_HEADER = struct.Struct("<4sBHHdIQ")


# --------------------------------------------------------------------------- #
# ffmpeg helpers
# --------------------------------------------------------------------------- #
def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(
            f"'{name}' was not found on PATH. Install ffmpeg "
            "(https://ffmpeg.org/download.html, `brew install ffmpeg`, "
            "`sudo apt install ffmpeg`, or `winget install ffmpeg`)."
        )
    return path


def _parse_rate(rate: str) -> float:
    try:
        num, _, den = rate.partition("/")
        val = float(num) / float(den or 1)
        return val if val > 0 else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: str) -> dict:
    """Return width, height, fps, frame_count (or None), duration (or None)."""
    cmd = [
        _tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,duration"
        ":stream_tags=rotate:stream_side_data=rotation"
        ":format=duration",
        "-of", "json", path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path!r}: {res.stderr.strip()}")
    data = json.loads(res.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError(f"No video stream found in {path!r}")
    s = streams[0]
    w, h = int(s["width"]), int(s["height"])

    # Phone videos are often stored sideways with a rotation flag; ffmpeg
    # auto-rotates on decode, so swap dimensions to match.
    rot = 0
    try:
        rot = int(float((s.get("tags") or {}).get("rotate", 0)))
    except ValueError:
        pass
    for sd in s.get("side_data_list") or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    if abs(rot) % 180 == 90:
        w, h = h, w

    fps = _parse_rate(s.get("avg_frame_rate", "")) or _parse_rate(s.get("r_frame_rate", "")) or 30.0
    duration = s.get("duration") or (data.get("format") or {}).get("duration")
    duration = float(duration) if duration not in (None, "N/A") else None
    nb = s.get("nb_frames")
    frames = int(nb) if nb and str(nb).isdigit() else None
    if frames is None and duration:
        frames = int(round(duration * fps))
    return {"width": w, "height": h, "fps": fps, "frame_count": frames, "duration": duration}


def _ffmpeg_frames(path: str, w: int, h: int, fps: Optional[float] = None,
                   start: float = 0.0, pix_fmt: str = "rgb24",
                   bytes_per_pixel: int = 3) -> Iterator[bytes]:
    """Yield raw frames of size w x h in `pix_fmt`, decoded and scaled by ffmpeg."""
    # sws_dither=none: dithering would make neighbouring cells differ, which
    # defeats the "same color as the last cell" and "unchanged since last
    # frame" shortcuts used for fast output.
    vf = f"scale={w}:{h}:flags=area:sws_dither=none,format={pix_fmt}"
    if fps:
        vf += f",fps={fps}"
    seek = ["-ss", f"{start:.3f}"] if start > 0 else []
    cmd = [
        _tool("ffmpeg"), "-v", "error", "-nostdin", *seek, "-i", path, "-an",
        "-vf", vf, "-f", "rawvideo", "-pix_fmt", pix_fmt, "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    size = w * h * bytes_per_pixel
    got_any = False
    finished = False
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                finished = True
                break
            got_any = True
            yield buf
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
        err = proc.stderr.read().decode(errors="replace").strip()
        proc.stderr.close()
        code = proc.wait()
        if finished and code != 0 and not got_any:
            raise RuntimeError(f"ffmpeg failed to decode {path!r}: {err}")


# --------------------------------------------------------------------------- #
# Pixel helpers
# --------------------------------------------------------------------------- #
def _terminal_box(margin_rows: int = 1) -> Tuple[int, int]:
    """(cols, rows) available in the current terminal, leaving margin_rows free."""
    cols, rows = shutil.get_terminal_size((80, 24))
    return max(1, cols), max(1, rows - margin_rows)


def _fit_size(src_w: int, src_h: int, max_w: int, max_h: Optional[int], aspect: float = 1.0) -> Tuple[int, int]:
    """Largest (cols, rows) within max_w x max_h keeping src aspect ratio."""
    ratio = src_h / src_w * aspect
    cols = max(1, int(max_w))
    rows = max(1, round(cols * ratio))
    if max_h and rows > max_h:
        rows = max(1, int(max_h))
        cols = max(1, min(int(max_w), round(rows / ratio)))
    return cols, rows


@functools.lru_cache(maxsize=32)
def _spans(src: int, dst: int) -> Tuple[Tuple[int, int], ...]:
    out = []
    for i in range(dst):
        a = i * src // dst
        b = max(a + 1, (i + 1) * src // dst)
        out.append((a, b))
    return tuple(out)


@functools.lru_cache(maxsize=8)
def _nearest_map(sw: int, sh: int, tw: int, th: int) -> List[int]:
    """For each target cell, the index of the source cell to copy."""
    xs = [x * sw // tw for x in range(tw)]
    return [(y * sh // th) * sw + x for y in range(th) for x in xs]


def _resize_rgb(buf: bytes, sw: int, sh: int, tw: int, th: int) -> bytes:
    """Box-average (shrink) / nearest-neighbour (grow) resize of an RGB24 grid."""
    if (sw, sh) == (tw, th):
        return buf
    xs, ys = _spans(sw, tw), _spans(sh, th)
    out = bytearray(tw * th * 3)
    o = 0
    stride = sw * 3
    for y0, y1 in ys:
        for x0, x1 in xs:
            if x1 - x0 == 1 and y1 - y0 == 1:
                p = y0 * stride + x0 * 3
                out[o:o + 3] = buf[p:p + 3]
            else:
                r = g = b = 0
                for y in range(y0, y1):
                    p = y * stride + x0 * 3
                    for _ in range(x1 - x0):
                        r += buf[p]; g += buf[p + 1]; b += buf[p + 2]
                        p += 3
                n = (x1 - x0) * (y1 - y0)
                out[o] = r // n; out[o + 1] = g // n; out[o + 2] = b // n
            o += 3
    return bytes(out)


_LEVELS6 = (0, 95, 135, 175, 215, 255)
_TO6 = bytes(min(range(6), key=lambda i: abs(_LEVELS6[i] - v)) for v in range(256))


def _xterm256(r: int, g: int, b: int) -> int:
    """Closest xterm-256 palette index (6x6x6 cube or grayscale ramp)."""
    ri, gi, bi = _TO6[r], _TO6[g], _TO6[b]
    cube = 16 + 36 * ri + 6 * gi + bi
    cr, cg, cb = _LEVELS6[ri], _LEVELS6[gi], _LEVELS6[bi]
    avg = (r + g + b) // 3
    gi_ = max(0, min(23, (avg - 8) // 10))
    gv = 8 + gi_ * 10
    d_cube = (cr - r) ** 2 + (cg - g) ** 2 + (cb - b) ** 2
    d_gray = (gv - r) ** 2 + (gv - g) ** 2 + (gv - b) ** 2
    return 232 + gi_ if d_gray < d_cube else cube


# --------------------------------------------------------------------------- #
# Palettes: packed pixel formats ffmpeg can output directly, so each cell is a
# single small integer we can look up in precomputed tables.
# --------------------------------------------------------------------------- #
class _Palette:
    def __init__(self, name: str, ff_base: str, bpc: int, bits: Tuple[int, int, int]):
        self.name = name
        self.bpc = bpc  # bytes per cell
        if bpc == 2:
            ff_base += "le" if sys.byteorder == "little" else "be"
        self.ff_fmt = ff_base
        rb, gb, bb = bits
        gs, rs = bb, bb + gb  # bit shifts of the G and R fields
        self.size = 1 << (rb + gb + bb)
        self.tr = [(v >> (8 - rb)) << rs for v in range(256)]
        self.tg = [(v >> (8 - gb)) << gs for v in range(256)]
        self.tb = [v >> (8 - bb) for v in range(256)]

        def expand(q: int, n: int) -> int:
            top = (1 << n) - 1
            return (q * 255 + top // 2) // top

        self.colors = [
            (expand(i >> rs, rb), expand((i >> gs) & ((1 << gb) - 1), gb), expand(i & ((1 << bb) - 1), bb))
            for i in range(self.size)
        ]

    def from_rgb24(self, buf: bytes) -> List[int]:
        tr, tg, tb = self.tr, self.tg, self.tb
        return [tr[r] | tg[g] | tb[b] for r, g, b in zip(buf[0::3], buf[1::3], buf[2::3])]

    def pack(self, cells) -> bytes:
        return bytes(cells) if self.bpc == 1 else array("H", cells).tobytes()

    def unpack(self, frame: bytes):
        return frame if self.bpc == 1 else memoryview(frame).cast("H").tolist()


PALETTES = {
    "high": _Palette("high", "rgb565", 2, (5, 6, 5)),    # 65,536 colors
    "medium": _Palette("medium", "rgb444", 2, (4, 4, 4)),  # 4,096 colors
    "low": _Palette("low", "rgb8", 1, (3, 3, 2)),         # 256 colors
}
DEFAULT_PALETTE = "medium"


@functools.lru_cache(maxsize=16)
def _tables(palette: str, color: Optional[str], charset: str):
    """Per palette index: the character, its color code (or None), and both together."""
    pal = PALETTES[palette]
    n = len(charset) - 1
    ch, esc = [], []
    shared = {}
    for r, g, b in pal.colors:
        c = charset[(((r * 299 + g * 587 + b * 114) // 1000) * n + 127) // 255]
        ch.append(c)
        if color is None or c == " ":
            esc.append(None)
        else:
            code = (f"\x1b[38;2;{r};{g};{b}m" if color == "truecolor"
                    else f"\x1b[38;5;{_xterm256(r, g, b)}m")
            # share one string object per distinct code so `is` comparisons work
            esc.append(shared.setdefault(code, code))
    full = [c if e is None else e + c for c, e in zip(ch, esc)]
    # rgb444 leaves 4 unused bits; repeat tables so any 16-bit value is valid
    rep = (1 << (8 * pal.bpc)) // pal.size
    return ch * rep, esc * rep, full * rep


def _first_diff(a: bytes, b: bytes) -> int:
    """Index of the first differing byte (a != b, same length). Binary search, C-speed compares."""
    lo, hi = 0, len(a)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if a[lo:mid] == b[lo:mid]:
            lo = mid
        else:
            hi = mid
    return lo


def _last_diff(a: bytes, b: bytes) -> int:
    """Index of the last differing byte (a != b, same length)."""
    lo, hi = 0, len(a)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if a[mid:hi] == b[mid:hi]:
            hi = mid
        else:
            lo = mid
    return lo


class FrameEncoder:
    """
    Turns packed palette frames into terminal text, as fast as possible.

    delta=True  : each call returns only what changed since the previous call,
                  with cursor-positioning codes. The first call (and the first
                  after reset()) clears the screen and draws everything.
                  Print the results in order and the screen shows the video.
    delta=False : each call returns a complete standalone frame (lines joined
                  with '\\n'), like the classic mode but faster.

    top/left    : 1-based screen position of the video's top-left corner
                  (delta mode only).
    """

    def __init__(self, width: int, height: int, *, palette: str = DEFAULT_PALETTE,
                 color: Optional[str] = "truecolor", charset: str = CHARSETS["standard"],
                 delta: bool = True, top: int = 1, left: int = 1):
        self.width, self.height = width, height
        self.palette = PALETTES[palette]
        self._ch, self._esc, self._full = _tables(palette, color, charset)
        self._reset = "\x1b[0m" if color else ""
        self.delta, self.top, self.left = delta, top, left
        self._prev: Optional[bytes] = None
        self._prev_cells = None

    # Unchanged gaps shorter than this are redrawn rather than jumped over,
    # because a cursor jump costs ~8 bytes itself.
    GAP = 4

    def reset(self) -> None:
        """Forget the screen contents: the next frame is drawn in full (call after clearing)."""
        self._prev = None
        self._prev_cells = None

    def encode(self, frame: bytes) -> str:
        w, h, bpc = self.width, self.height, self.palette.bpc
        rowb = w * bpc
        if len(frame) != rowb * h:
            raise ValueError(f"frame is {len(frame)} bytes, expected {rowb * h} for {w}x{h}")
        ch, esc, full = self._ch, self._esc, self._full
        cells = self.palette.unpack(frame)
        delta = self.delta
        prev = self._prev if delta else None
        pcells = self._prev_cells
        gap = self.GAP
        out: List[str] = []
        app = out.append
        last = None  # color code currently active in the terminal
        if delta and prev is None:
            app("\x1b[0m\x1b[2J")
        top, left = self.top, self.left
        for y in range(h):
            a = y * rowb
            base = y * w
            if prev is None:
                spans = ((0, w),)
            else:
                ra, rb = frame[a:a + rowb], prev[a:a + rowb]
                if ra == rb:
                    continue  # row unchanged: send nothing (C-speed compare)
                x0 = _first_diff(ra, rb) // bpc
                x1 = _last_diff(ra, rb) // bpc + 1
                # split into runs of changed cells, jumping over unchanged gaps
                diffs = [i for i, p, q in zip(range(x0, x1), cells[base + x0:base + x1],
                                              pcells[base + x0:base + x1]) if p != q]
                spans = []
                s = end = diffs[0]
                for i in diffs:
                    if i - end > gap:
                        spans.append((s, end + 1))
                        s = i
                    end = i
                spans.append((s, end + 1))
            for x0, x1 in spans:
                if delta:
                    app(f"\x1b[{top + y};{left + x0}H")
                elif y:
                    app("\n")
                for c in cells[base + x0:base + x1]:
                    e = esc[c]
                    if e is last or e is None:
                        app(ch[c])  # same color as the previous cell, or a blank
                    else:
                        app(full[c])
                        last = e
        app(self._reset)
        if delta:
            self._prev = frame
            self._prev_cells = cells
        return "".join(out)


# --------------------------------------------------------------------------- #
# Frame sources
# --------------------------------------------------------------------------- #
class _FileSource:
    """Frames stored in an .asciv file (zlib-compressed RGB grid per frame)."""

    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as f:
            head = f.read(_HEADER.size)
            if len(head) < _HEADER.size:
                raise ValueError(f"{path!r} is not an ASCII-video file")
            magic, ver, w, h, fps, count, index_off = _HEADER.unpack(head)
            if magic != _MAGIC:
                raise ValueError(f"{path!r} is not an ASCII-video file")
            if ver > _VERSION:
                raise ValueError(f"{path!r} uses a newer format (v{ver})")
            self.width, self.height, self.fps = w, h, fps
            self._offsets = None
            if index_off:
                f.seek(index_off)
                raw = f.read(8 * count)
                self._offsets = struct.unpack(f"<{count}Q", raw)
            self.frame_count = count if index_off else None

    def _read_at(self, f) -> Optional[bytes]:
        n = f.read(4)
        if len(n) < 4:
            return None
        (size,) = struct.unpack("<I", n)
        data = f.read(size)
        if len(data) < size:
            return None
        return zlib.decompress(data)

    def _native_frames(self, start: float) -> Iterator[bytes]:
        skip = max(0, int(round(start * self.fps))) if start > 0 else 0
        with open(self.path, "rb") as f:
            remaining = self.frame_count
            if skip and self._offsets is not None:
                if skip >= len(self._offsets):
                    return
                f.seek(self._offsets[skip])
                remaining -= skip
                skip = 0
            else:
                f.seek(_HEADER.size)
            while remaining is None or remaining > 0:
                buf = self._read_at(f)
                if buf is None:
                    break
                if skip:  # no index: skip frames by reading past them
                    skip -= 1
                    continue
                if remaining is not None:
                    remaining -= 1
                yield buf

    def frames(self, w: int, h: int, start: float = 0.0) -> Iterator[bytes]:
        for buf in self._native_frames(start):
            yield _resize_rgb(buf, self.width, self.height, w, h)

    def cells(self, w: int, h: int, start: float, pal: _Palette) -> Iterator[bytes]:
        nw, nh = self.width, self.height
        if (w, h) == (nw, nh):
            for buf in self._native_frames(start):
                yield pal.pack(pal.from_rgb24(buf))
        elif w >= nw and h >= nh:
            # growing: convert the (smaller) native frame, then copy cells
            kmap = _nearest_map(nw, nh, w, h)
            for buf in self._native_frames(start):
                idx = pal.from_rgb24(buf)
                yield pal.pack([idx[k] for k in kmap])
        else:
            # shrinking: average blocks for a smooth result, then convert
            for buf in self._native_frames(start):
                yield pal.pack(pal.from_rgb24(_resize_rgb(buf, nw, nh, w, h)))

    def frame(self, i: int, w: int, h: int) -> bytes:
        if self._offsets is None:
            raise TypeError("this file has no index (incomplete write?); iterate instead")
        with open(self.path, "rb") as f:
            f.seek(self._offsets[i])
            return _resize_rgb(self._read_at(f), self.width, self.height, w, h)


class _VideoSource:
    """Frames decoded straight from a normal video file via ffmpeg."""

    def __init__(self, path: str, width, height: Optional[int],
                 fps: Optional[float], char_aspect: float):
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        self.path = path
        info = probe(path)
        self.src_w, self.src_h = info["width"], info["height"]
        self._fps_override = fps
        self.fps = fps or info["fps"]
        if fps and info["duration"]:
            self.frame_count = int(info["duration"] * fps)
        else:
            self.frame_count = info["frame_count"]
        if width is None and height is None:
            width = 160
        if width == "auto":
            cols, rows = _terminal_box()
            self.width, self.height = _fit_size(self.src_w, self.src_h, cols, rows, char_aspect)
        elif width and height:
            self.width, self.height = int(width), int(height)
        elif width:
            self.width, self.height = _fit_size(self.src_w, self.src_h, width, None, char_aspect)
        else:
            ratio = self.src_h / self.src_w * char_aspect
            self.height = int(height)
            self.width = max(1, round(self.height / ratio))

    def frames(self, w: int, h: int, start: float = 0.0) -> Iterator[bytes]:
        # Let ffmpeg scale from the original resolution (best quality).
        return _ffmpeg_frames(self.path, w, h, self._fps_override, start)

    def cells(self, w: int, h: int, start: float, pal: _Palette) -> Iterator[bytes]:
        # ffmpeg scales *and* converts to the palette format: no Python pixel work at all
        return _ffmpeg_frames(self.path, w, h, self._fps_override, start, pal.ff_fmt, pal.bpc)

    def frame(self, i: int, w: int, h: int) -> bytes:
        raise TypeError("random access isn't supported on raw videos; convert() it first")


# --------------------------------------------------------------------------- #
# Public video object
# --------------------------------------------------------------------------- #
class AsciiVideo:
    """
    An iterable of ASCII frames. `for frame in video:` yields one string per frame.

    fast=True (default): frames are delta strings that only redraw what changed
        since the previous frame and position the cursor themselves. Print them
        in order, unmodified, with nothing else drawn over the video area.
    fast=False: every frame is a complete standalone string (lines separated
        by '\\n', full 24-bit color), e.g. for saving or printing one frame.

    Frames are produced lazily, so even long videos use little memory.

    Attributes: width, height (in characters), fps, frame_count (may be None),
                duration, color, charset, palette, fast.
    """

    def __init__(self, source, *, color: Optional[str] = "truecolor", charset: str = "standard",
                 invert: bool = False, palette: str = DEFAULT_PALETTE, fast: bool = True):
        self._src = source
        self.width, self.height = source.width, source.height
        self._set_options(color, charset, invert, palette, fast)

    # ----- options -------------------------------------------------------- #
    def _set_options(self, color, charset, invert, palette, fast):
        if color in (False, "none", "off"):
            color = None
        if color is True:
            color = "truecolor"
        if color not in (None, "truecolor", "256"):
            raise ValueError("color must be 'truecolor', '256' or None")
        if palette not in PALETTES:
            raise ValueError(f"palette must be one of {', '.join(PALETTES)}")
        chars = CHARSETS.get(charset, charset)
        if not chars:
            raise ValueError("charset must not be empty")
        if invert:
            chars = chars[::-1]
        self.color, self.charset, self.palette, self.fast = color, chars, palette, bool(fast)
        self._pal = PALETTES[palette]
        n = len(chars) - 1
        self._lut = "".join(chars[(v * n + 127) // 255] for v in range(256))

    def with_options(self, *, color=..., charset=None, invert=None, palette=None, fast=None) -> "AsciiVideo":
        """Return a view with a different color mode / character set / palette / mode."""
        v = copy.copy(self)
        v._set_options(
            self.color if color is ... else color,
            charset if charset is not None else self.charset,
            bool(invert),
            palette or self.palette,
            self.fast if fast is None else fast,
        )
        return v

    # ----- sizing --------------------------------------------------------- #
    @property
    def fps(self) -> float:
        return self._src.fps

    @property
    def frame_count(self) -> Optional[int]:
        return self._src.frame_count

    @property
    def duration(self) -> Optional[float]:
        """Length in seconds (None if unknown)."""
        return self.frame_count / self.fps if self.frame_count and self.fps else None

    @property
    def path(self) -> str:
        return self._src.path

    @property
    def is_converted(self) -> bool:
        """True for .asciv files, False for raw videos decoded on the fly."""
        return isinstance(self._src, _FileSource)

    @property
    def native_size(self) -> Tuple[int, int]:
        """Size (cols, rows) the video was converted/opened at."""
        return self._src.width, self._src.height

    def resize(self, width=None, height: Optional[int] = None) -> "AsciiVideo":
        """
        New view at a given size. Give one dimension to keep the aspect ratio,
        or width="auto" to fit the current terminal.
        """
        nw, nh = self.native_size
        if width == "auto":
            return self.fit_terminal()
        if width and height:
            size = (int(width), int(height))
        elif width:
            size = _fit_size(nw, nh, width, None)
        elif height:
            size = (max(1, round(height * nw / nh)), int(height))
        else:
            size = (nw, nh)
        v = copy.copy(self)
        v.width, v.height = size
        return v

    def fit(self, max_width: int, max_height: int) -> "AsciiVideo":
        """Largest size fitting in max_width x max_height, aspect ratio preserved."""
        nw, nh = self.native_size
        return self.resize(*_fit_size(nw, nh, max_width, max_height))

    def fit_terminal(self, margin_rows: int = 1) -> "AsciiVideo":
        """Fit the current terminal (leaves `margin_rows` free to avoid scrolling)."""
        return self.fit(*_terminal_box(margin_rows))

    # ----- frames --------------------------------------------------------- #
    def encoder(self, *, delta: bool = True, top: int = 1, left: int = 1) -> FrameEncoder:
        """A FrameEncoder matching this view's size and options."""
        return FrameEncoder(self.width, self.height, palette=self.palette, color=self.color,
                            charset=self.charset, delta=delta, top=top, left=left)

    def cell_frames(self, start: float = 0.0) -> Iterator[bytes]:
        """Iterate packed palette frames (feed these to an encoder()), from `start` seconds."""
        return self._src.cells(self.width, self.height, start, self._pal)

    def raw_frames(self, start: float = 0.0) -> Iterator[bytes]:
        """Iterate raw RGB24 bytes (width*height*3) at the current size,
        optionally starting `start` seconds in."""
        return self._src.frames(self.width, self.height, start)

    def render(self, rgb: bytes, width: Optional[int] = None, height: Optional[int] = None) -> str:
        """Turn one RGB24 grid into a complete standalone ASCII string (full color)."""
        w = width or self.width
        h = height or self.height
        lut, mode = self._lut, self.color
        lines = []
        rowlen = w * 3
        for y in range(h):
            row = rgb[y * rowlen:(y + 1) * rowlen]
            parts = []
            last = None
            for i in range(0, rowlen, 3):
                r, g, b = row[i], row[i + 1], row[i + 2]
                ch = lut[(r * 299 + g * 587 + b * 114) // 1000]
                if mode is None or ch == " ":
                    parts.append(ch)
                    continue
                if mode == "truecolor":
                    key = (r, g, b)
                    if key != last:
                        parts.append(f"\x1b[38;2;{r};{g};{b}m")
                        last = key
                else:
                    key = _xterm256(r, g, b)
                    if key != last:
                        parts.append(f"\x1b[38;5;{key}m")
                        last = key
                parts.append(ch)
            lines.append("".join(parts))
        frame = "\n".join(lines)
        return frame + "\x1b[0m" if mode else frame

    def frames(self, start: float = 0.0) -> Iterator[str]:
        """Like iterating the video, but starting `start` seconds in."""
        if self.fast:
            enc = self.encoder()
            for cells in self.cell_frames(start):
                yield enc.encode(cells)
        else:
            for buf in self.raw_frames(start):
                yield self.render(buf)

    def __iter__(self) -> Iterator[str]:
        return self.frames()

    def __len__(self) -> int:
        if self.frame_count is None:
            raise TypeError("frame count unknown for this video")
        return self.frame_count

    def __getitem__(self, i: int) -> str:
        """Random access (converted .asciv files only). Always a complete standalone frame."""
        n = len(self)
        if i < 0:
            i += n
        if not 0 <= i < n:
            raise IndexError(i)
        return self.render(self._src.frame(i, self.width, self.height))

    def play(self, **kwargs) -> None:
        play(self, **kwargs)

    def __repr__(self) -> str:
        return (f"<AsciiVideo {self.width}x{self.height} @ {self.fps:g}fps, "
                f"frames={self.frame_count}, color={self.color}, palette={self.palette}, "
                f"fast={self.fast}>")


# --------------------------------------------------------------------------- #
# Public functions
# --------------------------------------------------------------------------- #
def _is_asciv(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == _MAGIC
    except OSError:
        return False


def open_video(path: str, *, width=None, height: Optional[int] = None,
               fps: Optional[float] = None, color: Optional[str] = "truecolor",
               charset: str = "standard", invert: bool = False,
               palette: str = DEFAULT_PALETTE, fast: bool = True,
               char_aspect: float = CHAR_ASPECT) -> AsciiVideo:
    """
    Open an ASCII-video file (made by `convert`) or any normal video file.

    For an .asciv file, width/height resize the view (same as .resize()).
    For a raw video, width/height choose the decode size (default 160 cols),
    fps optionally resamples, and frames are decoded on the fly.
    width="auto" picks the largest size that fits the current terminal
    (height is then ignored).

    palette : "high" (65k colors), "medium" (4k, default) or "low" (256).
              Fewer colors = less text per frame = faster in the terminal.
    fast    : True (default) yields delta frames; False yields full frames.
    """
    if width is not None and width != "auto" and not isinstance(width, int):
        raise ValueError('width must be an int or "auto"')
    opts = dict(color=color, charset=charset, invert=invert, palette=palette, fast=fast)
    if _is_asciv(path):
        v = AsciiVideo(_FileSource(path), **opts)
        return v.resize(width, height) if (width or height) else v
    return AsciiVideo(_VideoSource(path, width, height, fps, char_aspect), **opts)


def convert(input_path: str, output_path: str, *, width=160,
            height: Optional[int] = None, fps: Optional[float] = None,
            char_aspect: float = CHAR_ASPECT, compression: int = 6,
            progress: bool = True) -> AsciiVideo:
    """
    Convert a video into an ASCII-video file and return it opened.

    width/height : size in characters. Give just one to keep the aspect ratio.
                   Store it reasonably large; it can be shrunk losslessly at
                   playback but growing it looks blocky. width="auto" uses
                   the largest size that fits the current terminal.
    fps          : optionally resample (e.g. 15 to halve the file size).

    The file stores each frame's per-character color (not pre-rendered text),
    so color mode, character set and size can all be changed at playback.
    """
    src = _VideoSource(input_path, width, height, fps, char_aspect)
    out_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(out_dir, exist_ok=True)
    tmp = output_path + ".part"
    offsets = []
    t0 = time.perf_counter()
    with open(tmp, "wb") as f:
        f.write(_HEADER.pack(_MAGIC, _VERSION, src.width, src.height, src.fps, 0, 0))
        for buf in src.frames(src.width, src.height):
            offsets.append(f.tell())
            data = zlib.compress(buf, compression)
            f.write(struct.pack("<I", len(data)))
            f.write(data)
            if progress and len(offsets) % 10 == 0:
                total = f"/{src.frame_count}" if src.frame_count else ""
                sys.stderr.write(f"\rconverting: frame {len(offsets)}{total}")
                sys.stderr.flush()
        index_off = f.tell()
        f.write(struct.pack(f"<{len(offsets)}Q", *offsets))
        f.seek(0)
        f.write(_HEADER.pack(_MAGIC, _VERSION, src.width, src.height, src.fps, len(offsets), index_off))
    os.replace(tmp, output_path)
    if progress:
        mb = os.path.getsize(output_path) / 1e6
        sys.stderr.write(f"\rconverted {len(offsets)} frames ({src.width}x{src.height} chars) "
                         f"-> {output_path} [{mb:.1f} MB, {time.perf_counter() - t0:.1f}s]\n")
    return open_video(output_path)


def _enable_windows_ansi() -> None:
    if os.name == "nt":
        os.system("")  # turns on VT escape processing in Windows consoles


def play(video: AsciiVideo, *, fit: bool = True, loop: bool = False,
         fps: Optional[float] = None, out=None) -> None:
    """
    Play in the terminal with correct timing, dropping frames if it falls behind.

    fit  : keep the video fitted to the terminal, re-fitting live if the
           window is resized (otherwise plays at video.width x video.height).
    loop : repeat until Ctrl-C.
    fps  : override playback speed.
    """
    out = out or sys.stdout
    _enable_windows_ansi()
    frame_time = 1.0 / (fps or video.fps or 30)
    step = 1.0 / (video.fps or 30)  # seconds of video per frame
    write, flush = out.write, out.flush
    position = 0.0
    write("\x1b[?25l")
    try:
        while True:
            size = shutil.get_terminal_size((80, 24))
            view = video.fit_terminal() if fit else video
            enc = view.encoder()
            base, clock, resized = position, 0.0, False
            frames = view.cell_frames(base)
            try:
                for i, cells in enumerate(frames):
                    if i == 0:
                        clock = time.perf_counter()  # start the clock once decoding is going
                    position = base + (i + 1) * step
                    if fit and shutil.get_terminal_size((80, 24)) != size:
                        resized = True
                        break
                    target = clock + i * frame_time
                    now = time.perf_counter()
                    if now > target + frame_time:
                        continue  # running behind: drop (the encoder still diffs vs. the screen)
                    if target > now:
                        time.sleep(target - now)
                    write(SYNC_START + enc.encode(cells) + SYNC_END)
                    flush()
            finally:
                frames.close()
            if resized:
                continue
            if not loop:
                break
            position = 0.0
    except KeyboardInterrupt:
        pass
    finally:
        write("\x1b[0m\x1b[?25h\n")
        flush()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _width_arg(s: str):
    if s.lower() == "auto":
        return "auto"
    try:
        return int(s)
    except ValueError:
        import argparse
        raise argparse.ArgumentTypeError('width must be a number or "auto"')


def _main(argv=None) -> None:
    import argparse

    p = argparse.ArgumentParser(description="Convert and play videos as colored ASCII.")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("convert", help="video -> .asciv file")
    c.add_argument("input")
    c.add_argument("output")
    c.add_argument("--width", type=_width_arg, default=160, help='columns, or "auto" to fit the terminal')
    c.add_argument("--height", type=int)
    c.add_argument("--fps", type=float)

    pl = sub.add_parser("play", help="play an .asciv file or a raw video")
    pl.add_argument("input")
    pl.add_argument("--width", type=_width_arg, default="auto",
                    help='fixed width in columns, or "auto" (default) to fit and follow the terminal')
    pl.add_argument("--color", choices=["truecolor", "256", "none"], default="truecolor")
    pl.add_argument("--palette", choices=list(PALETTES), default=DEFAULT_PALETTE,
                    help="how many colors to use (fewer = faster)")
    pl.add_argument("--charset", default="standard",
                    help=f"one of {', '.join(CHARSETS)} or your own dark-to-bright string")
    pl.add_argument("--invert", action="store_true", help="for light-background terminals")
    pl.add_argument("--loop", action="store_true")
    pl.add_argument("--fps", type=float)

    i = sub.add_parser("info", help="show info about a file")
    i.add_argument("input")

    a = p.parse_args(argv)
    if a.cmd == "convert":
        convert(a.input, a.output, width=a.width, height=a.height, fps=a.fps)
    elif a.cmd == "play":
        color = None if a.color == "none" else a.color
        auto = a.width == "auto"
        v = open_video(a.input, width=a.width, color=color, charset=a.charset,
                       invert=a.invert, palette=a.palette)
        play(v, fit=auto, loop=a.loop, fps=a.fps)
    elif a.cmd == "info":
        print(open_video(a.input))


if __name__ == "__main__":
    _main()
