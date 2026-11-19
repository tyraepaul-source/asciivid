# asciivid

Turn any video into colored ASCII art and play it in your terminal.

- `asciivid.py` is the library: it converts videos, opens them, resizes them, and turns frames into text.
- `testvid.py` is a small video player built on it, with pause, seeking and sound.

It uses only the Python standard library (Python 3.8+). The one outside requirement is **ffmpeg**, which does the video decoding:

| OS | Install |
|---|---|
| macOS | `brew install ffmpeg` |
| Ubuntu / Debian | `sudo apt install ffmpeg` |
| Windows | `winget install ffmpeg` |

ffmpeg comes with `ffprobe`, used to read video info, and `ffplay`, used for sound in `testvid.py`.

---

## Quick start

```python
import asciivid

# Convert once, keep the .asciv file
asciivid.convert("movie.mp4", "movie.asciv", width=160)

# Play it with correct timing, fitted to the terminal
asciivid.play(asciivid.open_video("movie.asciv"))
```

Or just play a video directly:

```
python testvid.py -v movie.mp4
```

| Key | Action |
|---|---|
| space | pause / play |
| `+` / `-` | volume up / down by 5% (starts at 10%) |
| → / ← | skip forward / back 10 seconds |
| ctrl+c | quit |

---

## How it works

```
video file ──ffmpeg──▶ small grid of pixels ──▶ one character + color per pixel ──▶ terminal text
(.mp4 etc.)            (e.g. 160×45)                                               (a frame)
```

1. **Decode and shrink.** ffmpeg decodes the video and scales it down so that one pixel becomes one character. Terminal characters are about twice as tall as they are wide, so the height is halved to keep the picture's shape. That's the `CHAR_ASPECT = 0.5` setting.
2. **Pick a character.** Each pixel's brightness picks a character from a dark-to-bright ramp such as `" .:-=+*#%@"`. Dark pixels become spaces and bright ones become `@`.
3. **Color it.** Each character gets a color code, an "ANSI escape sequence", carrying the pixel's color.

### Two ways to get frames

| | Converted file (`.asciv`) | Raw video (`.mp4`, `.mkv`, …) |
|---|---|---|
| How | `convert()` once, then `open_video("x.asciv")` | `open_video("x.mp4")` |
| Startup | instant | a short ffmpeg start-up |
| Jump to frame `video[i]` | ✓ | ✗ |
| `len(video)` | exact | estimated from the video's length |
| Resizing | done in Python; shrinking looks good, enlarging looks blocky | ffmpeg rescales from the full-resolution original (best quality) |
| Sound | no (use `testvid.py -a original.mp4`) | yes, in `testvid.py` |

### What's inside an `.asciv` file

It stores each character's **color**, not finished text, so you can change the size, color mode and characters at playback without converting again.

```
header   "ASCV" | version u8 | width u16 | height u16 | fps f64 | frame_count u32 | index_offset u64
frames   [u32 length][zlib-compressed RGB bytes, width*height*3] ...
index    frame_count × u64 byte offsets (for random access and seeking)
```

All numbers are little-endian. A file written by an interrupted conversion has no index, but it can still be played from the start.

---

## Fast mode (the default)

### Why it was slow

Drawing text is not slow in Python. **The terminal itself was the bottleneck.** In the original version, a full-color character cost about 18 bytes: roughly 17 for the color code such as `\x1b[38;2;123;45;67m`, then 1 for the character. A 300×80 terminal meant about **400 KB of text per frame**, or about 12 MB/s at 30 fps. Most terminals can't process that fast, and shrinking your font makes it worse, because a smaller font means more characters per frame.

### What fast mode does

Fast mode uses three tricks, all in `FrameEncoder`:

1. **Fewer colors (the palette).** ffmpeg outputs pixels with reduced color precision. The default is 4,096 colors, which looks the same as full color in ASCII art. Neighboring characters then share a color more often, and a color code is only sent when the color *changes*. Each pixel also becomes a single small number, so Python looks up its character and color code in tables built once in advance, instead of computing them for every pixel of every frame.
2. **Only redraw what changed (delta frames).** Each frame is compared with the previous one. Unchanged rows send nothing, and inside a changed row, unchanged stretches are skipped by moving the cursor past them. Static backgrounds therefore cost almost nothing.
3. **Synchronized output.** Each frame is wrapped in `\x1b[?2026h … \x1b[?2026l`, which tells the terminal to draw the frame all at once, so you don't see half-drawn frames. Terminals that don't support it ignore these codes.

### Measured results

These were measured at 300×80 characters. "Text per frame" is what the terminal has to process.

| Clip | Original version | Fast (default) | Fast + `color="256"` |
|---|---|---|---|
| Mostly static scene, moving object | 272 KB, 9.0 ms Python | **28 KB** (10×), 0.9 ms | **13 KB** (21×) |
| Full-screen motion everywhere | 318 KB, 9.8 ms | 184 KB (1.7×), 3.1 ms | 72 KB (4.4×) |
| Full-screen motion + heavy grain (worst case) | 411 KB, 10.5 ms | 379 KB (1.1×), 4.0 ms | 116 KB (3.5×) |

Real footage usually falls between the first two rows.

### What delta frames mean for your code

With `fast=True`, each frame string only makes sense **right after the one before it**. Each string moves the cursor itself, and the first one clears the screen. So:

```python
for frame in video.fit_terminal():
    print(frame, end="", flush=True)   # print as-is, in order; no "\x1b[H" needed
```

- Don't add anything to frames, and don't print anything else over the video area. A status line *below* the video is fine, which is what `testvid.py` does.
- If you clear the screen yourself, call `encoder.reset()` so the next frame redraws everything (see the encoder section below).
- If you want **complete, standalone frames**, for example to save one to a file, use `video.with_options(fast=False)` or `video[i]`. Those use the original full-24-bit renderer and separate lines with `\n`.

---

## Making it faster

Try these in order:

1. **Use 256-color codes:** `color="256"` (or `--color 256`). The codes are about half as long, which roughly halves the text again for a small loss of color accuracy. This is the biggest single gain.
2. **Use a palette with fewer colors:** `palette="low"` (256 colors). It produces even less text in full-color mode, but colors and brightness visibly shift. Try `color="256"` first.
3. **Use a faster terminal.** GPU-accelerated terminals (kitty, WezTerm, Alacritty, Ghostty, Windows Terminal) handle far more text per second than older ones. Some older terminals, such as older versions of macOS Terminal.app, don't support 24-bit color at all; use `color="256"` there.
4. **Use fewer characters.** Each step down in font size adds many characters per frame. Fixing a width such as `resize(200)` caps the work.
5. **Lower the frame rate** with `open_video(path, fps=15)` (raw video) or `convert(..., fps=15)`.
6. **Turn off color** with `color=None`. It's the fastest option by far, but black and white.

Check what you're running with `print(video)`:

```
<AsciiVideo 300x80 @ 30fps, frames=1800, color=truecolor, palette=medium, fast=True>
```

---

## API reference

### `convert(input_path, output_path, *, width=160, height=None, fps=None, progress=True) → AsciiVideo`

Converts a video into an `.asciv` file and returns it opened.

- `width` / `height` are in characters. Give one and the other follows the video's shape. `width="auto"` fits the current terminal. Store at a generous width (160 or more): shrinking at playback looks fine, enlarging looks blocky.
- `fps` resamples the video, for example to 15, which halves the file size.
- Expect roughly 10–20 KB per frame at 160 columns.

### `open_video(path, *, width=None, height=None, fps=None, color="truecolor", charset="standard", invert=False, palette="medium", fast=True) → AsciiVideo`

Opens either an `.asciv` file or a normal video.

| Option | Values |
|---|---|
| `width` | an int, or `"auto"` to fit the terminal. For raw videos the default is 160. |
| `color` | `"truecolor"` (24-bit), `"256"`, or `None` |
| `charset` | `"standard"`, `"detailed"`, `"simple"`, `"blocks"`, `"dense"`, or your own dark-to-bright string |
| `invert` | `True` for light-background terminals |
| `palette` | `"high"` (65,536 colors), `"medium"` (4,096), or `"low"` (256). Used in fast mode. |
| `fast` | `True` gives delta frames; `False` gives complete standalone frames |

### `AsciiVideo`

**Frames**

| | |
|---|---|
| `for frame in video` | frames as strings (delta strings in fast mode) |
| `video.frames(start=0.0)` | the same, starting `start` seconds in |
| `video[i]` | one complete standalone frame (`.asciv` only) |
| `len(video)` | number of frames (raises `TypeError` if unknown) |

**Views.** Each of these returns a new view; nothing is copied or decoded.

| | |
|---|---|
| `video.resize(80)` | 80 columns, height follows the video's shape |
| `video.resize(80, 20)` | exact size |
| `video.resize("auto")` | fit the terminal |
| `video.fit(cols, rows)` | largest size inside a box, shape preserved |
| `video.fit_terminal(margin_rows=1)` | fit the terminal, leaving rows free (for a status bar, say) |
| `video.with_options(color=, charset=, invert=, palette=, fast=)` | change any option |

**Info:** `width`, `height`, `fps`, `frame_count`, `duration`, `native_size`, `path`, `is_converted`.

**Lower level**

| | |
|---|---|
| `video.cell_frames(start=0.0)` | packed palette frames, to feed to an encoder |
| `video.encoder(delta=True, top=1, left=1)` | a `FrameEncoder` matching the view |
| `video.raw_frames(start=0.0)` | raw RGB bytes (`width*height*3`) |
| `video.render(rgb)` | full-quality standalone rendering of raw RGB bytes |

### `FrameEncoder`: drawing frames yourself

This is the piece to use when you build your own player with a custom layout, as `testvid.py` does:

```python
view = video.fit_terminal(margin_rows=2)
enc = view.encoder()                  # top=, left= place the video elsewhere on screen
for cells in view.cell_frames(start=30.0):
    text = enc.encode(cells)          # only what changed; the first call clears and draws everything
    sys.stdout.write(asciivid.SYNC_START + text + my_status_bar + asciivid.SYNC_END)
    sys.stdout.flush()
```

- **Dropping frames:** to catch up when behind, skip `encode()` for a frame. The encoder compares against what is actually on screen, so it stays correct.
- **After clearing the screen** or anything else that disturbs the video area, call `enc.reset()`.
- **When the terminal is resized,** make a new view and a new encoder.

### `play(video, *, fit=True, loop=False, fps=None)`

Plays with correct timing. It drops frames if it falls behind, and with `fit=True` it refits live when the window is resized. Press Ctrl+C to stop.

---

## Command line

```
python asciivid.py convert movie.mp4 movie.asciv --width 160 [--fps 15]
python asciivid.py play movie.asciv [--color 256] [--palette low] [--charset detailed] [--loop]
python asciivid.py play movie.mp4            # raw videos work too
python asciivid.py info movie.asciv

python testvid.py -v movie.mp4 [--color 256] [--palette high] [--charset blocks]
python testvid.py -v movie.asciv -a movie.mp4   # converted file + the original's sound
```

---

## Notes and limitations

- **Sound** only exists in `testvid.py`, which plays it through `ffplay`. Every pause, skip or volume change restarts `ffplay`, so expect a brief gap each time.
- **Phone videos** recorded sideways are rotated automatically.
- **Light terminal themes:** use `invert=True` / `--invert`.
- **Windows:** color codes are switched on automatically. Use Windows Terminal rather than the old console window.
- **Seeking in `.asciv` files** uses the file's index and is instant. In raw videos it lets ffmpeg jump straight to the time.
