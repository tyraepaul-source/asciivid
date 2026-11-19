# asciivid

Turn videos, GIFs and images into colored ASCII art, and use them to build things: players, terminal animations, previews, exports, effects.

```python
import asciivid
asciivid.play(asciivid.open_video("movie.mp4", width="auto"))
```

That's a complete video player. The rest of this guide covers everything else you can do with it.

> To learn how it works inside (the file format and why fast mode is fast), see [HOW_IT_WORKS.md](HOW_IT_WORKS.md).

---

## Contents

1. [Setup](#setup)
2. [The basics in 2 minutes](#the-basics-in-2-minutes)
3. [Features](#features)
   - [Opening videos, GIFs and images](#opening-videos-gifs-and-images)
   - [Converting to .asciv files](#converting-to-asciv-files)
   - [Getting frames](#getting-frames)
   - [Sizing](#sizing)
   - [Changing the look](#changing-the-look)
   - [Playing](#playing)
   - [Drawing frames yourself](#drawing-frames-yourself)
   - [Working with raw pixels](#working-with-raw-pixels)
   - [Reading video info](#reading-video-info)
4. [Recipes](#recipes)
5. [Command line](#command-line)
6. [The testvid player](#the-testvid-player)
7. [Speed tips](#speed-tips)
8. [Things to watch out for](#things-to-watch-out-for)

---

## Setup

1. Put `asciivid.py` next to your script, or anywhere on your Python path. It uses only the standard library and works with Python 3.8+.
2. Install **ffmpeg**, which decodes the video:

| OS | Command |
|---|---|
| macOS | `brew install ffmpeg` |
| Ubuntu / Debian | `sudo apt install ffmpeg` |
| Windows | `winget install ffmpeg` |

---

## The basics in 2 minutes

Everything starts with `open_video()`, which gives you a **video object**. Looping over it gives you **frames**, each a string ready to print:

```python
import asciivid

video = asciivid.open_video("movie.mp4", width=100)

for frame in video:
    print(frame, end="", flush=True)
```

That loop plays as fast as it can. For correct speed, use `asciivid.play(video)` or see the [timed loop recipe](#play-at-the-right-speed).

The video object has three handy properties:

- **It's lazy.** Frames are decoded only as you loop, so a two-hour movie uses no more memory than a two-second clip.
- **Views are free.** `video.resize(...)`, `video.fit_terminal()` and `video.with_options(...)` return a new view of the same video. Nothing is copied or decoded until you loop.
- **There are two kinds of frames:**
  - **Fast (default):** each frame only redraws what changed since the last one, and moves the cursor itself. Print them in order, unmodified. This is the right choice for playing in a terminal.
  - **Standalone (`fast=False`):** each frame is a complete picture with lines separated by `\n`. This is the right choice for saving, exporting or printing a single frame.

---

## Features

### Opening videos, GIFs and images

```python
video = asciivid.open_video("clip.mp4")          # any format ffmpeg reads: mp4, mkv, mov, webm, avi…
gif   = asciivid.open_video("funny.gif")         # animated GIFs
pic   = asciivid.open_video("photo.jpg")         # still images become a one-frame video
saved = asciivid.open_video("clip.asciv")        # files made with convert()
```

The `width` option sets how many characters wide the result is:

| `width=` | Result |
|---|---|
| *(not given)* | 160 columns for videos and images; the saved size for `.asciv` files |
| `100` | 100 columns; the height follows the picture's shape |
| `"auto"` | the biggest size that fits your terminal right now |

You can also give `height=`, either with `width` for an exact size or alone. For videos only, `fps=` resamples, for example to 12.

Phone videos recorded sideways are rotated the right way automatically.

### Converting to .asciv files

```python
asciivid.convert("movie.mp4", "movie.asciv", width=160)
```

An `.asciv` file opens instantly, lets you jump to any frame, and doesn't need ffmpeg to play. It stores each character's color rather than finished text, so you can still change the size, colors and characters later.

| Option | What it does |
|---|---|
| `width=160` / `height=` | Size in characters. `"auto"` fits the current terminal. Pick a generous size: shrinking later looks good, enlarging looks blocky. |
| `fps=15` | Resample. Half the frame rate means half the file size. |
| `compression=6` | zlib level from 1 (fastest) to 9 (smallest). |
| `progress=True` | Print progress to stderr. |

`convert()` returns the new file already opened. Expect roughly 10–20 KB per frame at 160 columns.

### Getting frames

```python
for frame in video: ...                    # every frame, from the start
for frame in video.frames(start=30): ...   # starting 30 seconds in
frame = video[100]                         # frame 100 as a complete standalone frame (.asciv only)
frames = list(video)                       # an actual list, if you really want one
```

| Info | |
|---|---|
| `len(video)` | number of frames (exact for `.asciv`, estimated for videos) |
| `video.fps` | frames per second |
| `video.duration` | length in seconds |
| `video.width`, `video.height` | current size in characters |
| `video.native_size` | size it was opened or converted at |
| `video.path` | the file it came from |
| `video.is_converted` | `True` for `.asciv` files |
| `print(video)` | a one-line summary of all the above |

### Sizing

Every sizing method keeps the picture's shape unless you give both dimensions.

```python
video.resize(80)              # 80 columns wide
video.resize(height=20)       # 20 rows tall
video.resize(80, 20)          # exactly 80×20 (may stretch)
video.resize("auto")          # fit the terminal
video.fit(100, 30)            # biggest size inside a 100×30 box
video.fit_terminal()          # fit the terminal, leaving 1 row free so nothing scrolls
video.fit_terminal(margin_rows=3)   # leave 3 rows free, e.g. for your own status lines
```

Sizes are worked out once, when you call the method. To follow a terminal that's being resized, call `fit_terminal()` again, which is what `play()` does for you. Characters are assumed to be about twice as tall as they are wide. If pictures look squashed or stretched with your font, pass `char_aspect=` to `open_video()` or `convert()`: the default is `0.5`; use a bigger number for taller pictures, a smaller one for flatter.

### Changing the look

Pass these to `open_video()`, or change them later with `video.with_options(...)`:

```python
video.with_options(color="256", charset="blocks", palette="high", invert=False, fast=True)
```

**`color`**: how characters are colored.

| Value | Looks | Speed |
|---|---|---|
| `"truecolor"` (default) | full color | good |
| `"256"` | very close to full color | about 2× faster in the terminal |
| `None` | black and white | fastest |

**`palette`**: how many colors fast mode uses. Fewer colors means less text and faster playback.

| Value | Colors | Notes |
|---|---|---|
| `"high"` | 65,536 | looks identical to the original |
| `"medium"` (default) | 4,096 | also looks identical, with less text |
| `"low"` | 256 | fastest, but colors and brightness visibly shift |

**`charset`**: the characters used, from dark to bright.

| Name | Characters |
|---|---|
| `"standard"` (default) | ` .:-=+*#%@` |
| `"detailed"` | 70 characters, for the most shading detail |
| `"simple"` | ` .oO@` |
| `"blocks"` | ` ░▒▓█`, chunky and bold |
| `"dense"` | `@` everywhere; the picture comes entirely from color |
| your own string | e.g. `" .-~*#"` or `" ·•●"`. It must go from dark to bright and start with a space for black. |

**`invert=True`** flips the character ramp, for terminals with a light background.

**`fast`**: `True` (default) gives redraw-only-what-changed frames for playing. `False` gives complete standalone frames in full 24-bit color.

### Playing

```python
asciivid.play(video)                    # fits the terminal and follows it when resized
asciivid.play(video, loop=True)         # repeat until Ctrl+C
asciivid.play(video, fps=60)            # play faster (or slower) than normal
asciivid.play(video.resize(80), fit=False)   # fixed size
video.play()                            # the same thing, as a method
```

`play()` keeps correct timing and skips frames if your terminal can't keep up, so the video never drifts behind. Ctrl+C stops it and puts your terminal back to normal. For pause, seeking and sound, use [testvid.py](#the-testvid-player).

### Drawing frames yourself

For your own players, layouts and overlays, use the lower-level pieces that `play()` is built from:

```python
view = video.fit_terminal(margin_rows=2)
enc = view.encoder()                  # turns frames into terminal text
for cells in view.cell_frames():      # compact frame data, not text yet
    text = enc.encode(cells)          # only what changed since the last encode()
    sys.stdout.write(asciivid.SYNC_START + text + asciivid.SYNC_END)
    sys.stdout.flush()
```

`view.encoder(...)` options:

| Option | What it does |
|---|---|
| `top=1, left=1` | Where the video's top-left corner goes on screen (1-based row and column). Use these to place videos anywhere. |
| `clear=True` | Whether the first frame clears the whole screen. Use `False` when several videos share the screen. |
| `delta=True` | `False` makes `encode()` return complete standalone frames instead. |

Encoder rules:

- **Skipping frames:** you can skip `encode()` for some frames, for example to catch up when running behind. The encoder compares against what's actually on screen, so it stays correct.
- **After drawing over the video area or clearing the screen,** call `enc.reset()`. The next frame then redraws everything.
- **After a resize,** make a new view and a new encoder.
- **`asciivid.SYNC_START` / `SYNC_END`** wrap a frame so the terminal shows it all at once, with no flicker. Terminals that don't support them ignore them.

### Working with raw pixels

To get the actual colors, for effects, exports or analysis:

```python
for rgb in video.raw_frames(start=0):   # bytes: width × height × 3 (red, green, blue, row by row)
    r, g, b = rgb[0], rgb[1], rgb[2]    # top-left pixel
    text = video.render(rgb)            # turn (possibly edited) pixels into a standalone ASCII frame
```

`render()` uses the view's `color` and `charset`. Pixel `(x, y)` starts at byte `(y * video.width + x) * 3`.

### Reading video info

```python
asciivid.probe("movie.mp4")
# {'width': 1920, 'height': 1080, 'fps': 29.97, 'frame_count': 5394, 'duration': 180.0}
```

---

## Recipes

Every recipe here was run and tested. Each assumes `import asciivid` and whatever else it imports.

### Play at the right speed

The simplest hand-written player. Use this as a starting point for your own.

```python
import sys, time
import asciivid

video = asciivid.open_video("movie.mp4").fit_terminal()
start = time.perf_counter()
for i, frame in enumerate(video):
    delay = start + i / video.fps - time.perf_counter()
    if delay > 0:
        time.sleep(delay)
    print(frame, end="", flush=True)
```

### Add a status bar

Keep the bottom row free and draw on it after each frame.

```python
import shutil, sys, time
import asciivid

video = asciivid.open_video("movie.mp4").fit_terminal(margin_rows=2)   # keep the bottom row free
enc = video.encoder()
start = time.perf_counter()
for i, cells in enumerate(video.cell_frames()):
    delay = start + i / video.fps - time.perf_counter()
    if delay > 0:
        time.sleep(delay)
    cols, rows = shutil.get_terminal_size()
    bar = f"\x1b[{rows};1H\x1b[7m frame {i + 1}/{len(video)} ".ljust(cols) + "\x1b[0m"
    sys.stdout.write(asciivid.SYNC_START + enc.encode(cells) + bar + asciivid.SYNC_END)
    sys.stdout.flush()
```

### Two videos side by side

```python
import sys, time
import asciivid

left = asciivid.open_video("a.mp4", width=60)
right = asciivid.open_video("b.mp4", width=60)
enc_l = left.encoder(clear=False)                           # top-left corner at column 1
enc_r = right.encoder(clear=False, left=left.width + 3)     # 2 blank columns between them

sys.stdout.write("\x1b[2J")                                 # clear the screen once ourselves
start = time.perf_counter()
for i, (a, b) in enumerate(zip(left.cell_frames(), right.cell_frames())):
    delay = start + i / left.fps - time.perf_counter()
    if delay > 0:
        time.sleep(delay)
    sys.stdout.write(asciivid.SYNC_START + enc_l.encode(a) + enc_r.encode(b) + asciivid.SYNC_END)
    sys.stdout.flush()
```

The same idea works for a grid of videos, or a small video in a corner: use `top=` and `left=`.

### Preview a video (contact sheet)

Print a small thumbnail every 2 seconds:

```python
video = asciivid.open_video("movie.mp4", width=40, fast=False, color=None)
for seconds in range(0, int(video.duration), 2):
    print(f"--- {seconds}s ---")
    print(next(video.frames(start=seconds)))
```

### Turn an image into ASCII art

```python
picture = asciivid.open_video("photo.jpg", width="auto", fast=False)
print(next(iter(picture)))
```

Use `color=None` for plain text you can paste anywhere, or `charset="detailed"` for finer shading.

### Save frames to files

```python
import os
video = asciivid.open_video("movie.mp4", width=80, fast=False)   # standalone frames

os.makedirs("frames", exist_ok=True)
for i, frame in enumerate(video):
    with open(f"frames/{i:05}.ans", "w", encoding="utf-8") as f:   # colored; view with `cat`
        f.write(frame + "\n")

with open("video.txt", "w", encoding="utf-8") as f:              # the whole video as plain text
    for frame in video.with_options(color=None):
        f.write(frame + "\n\f\n")
```

### Export a frame as a web page

```python
import html
video = asciivid.open_video("movie.mp4", width=100)
rgb = next(video.raw_frames(start=1.0))
chars = video.with_options(color=None, fast=False).render(rgb).split("\n")

rows = []
for y, line in enumerate(chars):
    spans = []
    for x, ch in enumerate(line):
        i = (y * video.width + x) * 3
        spans.append(f'<span style="color:rgb({rgb[i]},{rgb[i+1]},{rgb[i+2]})">{html.escape(ch)}</span>')
    rows.append("".join(spans))

with open("frame.html", "w") as f:
    f.write("<html><body style='background:#000'><pre style='font:10px/1 monospace'>"
            + "\n".join(rows) + "</pre></body></html>")
```

### Color effects (old green monitor)

Edit the raw pixels, then render them:

```python
import sys
video = asciivid.open_video("movie.mp4", width=80, fast=False)
for rgb in video.raw_frames():
    out = bytearray(len(rgb))
    for i in range(0, len(rgb), 3):
        out[i + 1] = (rgb[i] * 299 + rgb[i + 1] * 587 + rgb[i + 2] * 114) // 1000   # brightness → green
    sys.stdout.write("\x1b[H" + video.render(bytes(out)))
```

Swap in your own formula for sepia, thermal, inverted colors and so on. Pixel editing in pure Python is slow, so keep effect videos small, around 80 columns.

### A looping terminal animation

```python
asciivid.play(asciivid.open_video("spinner.gif", width=30), fit=False, loop=True)
```

### Convert a whole folder

```python
import pathlib
for path in pathlib.Path("videos").glob("*.mp4"):
    asciivid.convert(str(path), str(path.with_suffix(".asciv")), width=160)
```

---

## Command line

`asciivid.py` works on its own too:

```
python asciivid.py convert movie.mp4 movie.asciv [--width 160|auto] [--height N] [--fps 15]
python asciivid.py play movie.mp4          # or movie.asciv, a GIF, an image
python asciivid.py info movie.asciv
```

`play` options:

| Option | What it does |
|---|---|
| `--width N` / `--width auto` | fixed width, or fit and follow the terminal (default) |
| `--color truecolor\|256\|none` | how characters are colored |
| `--palette high\|medium\|low` | how many colors |
| `--charset NAME` / `--charset " .oO@"` | which characters to use |
| `--invert` | for light-background terminals |
| `--loop` | repeat until Ctrl+C |
| `--fps N` | playback speed |

---

## The testvid player

`testvid.py` is a full player with sound:

```
python testvid.py -v movie.mp4
python testvid.py -v movie.asciv -a movie.mp4     # converted file + the original's sound
python testvid.py -v movie.mp4 --color 256 --palette high --charset blocks
```

| Key | Action |
|---|---|
| space | pause / play |
| `+` / `-` | volume ±5% (starts at 10%) |
| → / ← | skip ±10 seconds |
| ctrl+c | quit |

The bar at the bottom shows the time, the volume, the controls and the current video width. Sound plays through `ffplay`, which comes with ffmpeg. The video is chosen in `main()` at the line marked `# here's the video`.

---

## Speed tips

If playback stutters, try these in order:

1. `color="256"` (`--color 256`) roughly halves the text the terminal has to process.
2. Use a fast terminal: kitty, WezTerm, Alacritty, Ghostty or Windows Terminal.
3. Use a smaller size: fewer characters means less work.
4. `palette="low"` is a bit faster again, but colors shift.
5. Lower the frame rate: `open_video(..., fps=15)`.
6. `color=None` is the fastest of all.

Mostly-still scenes are much cheaper than constant full-screen motion, because only changes are redrawn.

---

## Things to watch out for

- **Fast frames must be printed in order and unmodified.** Each one builds on the last. For a frame you can save or print on its own, use `fast=False` or `video[i]`.
- **Don't print anything over the video area during playback** unless you call `encoder.reset()` afterward. Printing below or beside the video is fine.
- **`video[i]` only works on `.asciv` files.** For a video, use `video.frames(start=seconds)`.
- **Sound** is only in `testvid.py`. Its sound briefly cuts out when you pause, seek or change volume.
- **Older terminals** without 24-bit color, such as older macOS Terminal.app, need `color="256"`.
- **Windows:** use Windows Terminal. Color support is switched on automatically.
