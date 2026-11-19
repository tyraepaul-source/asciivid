"""
testvid.py — a simple terminal video player built on asciivid.

    python testvid.py -v movie.mp4
    python testvid.py -v movie.asciv -a movie.mp4    # converted file + original's sound

Controls:
    space        pause / play
    + / -        volume up / down (starts at 10%)
    → / ←        skip forward / back 10 seconds
    ctrl+c       quit

Sound is played with `ffplay`, which ships with ffmpeg.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time

import asciivid

SEEK_STEP = 10      # seconds
VOLUME_START = 10   # percent
VOLUME_STEP = 5     # percent


# --------------------------------------------------------------------------- #
# Keyboard input (non-blocking, works on macOS/Linux and Windows)
# --------------------------------------------------------------------------- #
class Keys:
    """Context manager that reads single keypresses without waiting for Enter."""

    def __enter__(self):
        if os.name == "nt":
            import msvcrt
            self._msvcrt = msvcrt
        else:
            import termios
            import tty
            self._fd = sys.stdin.fileno()
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)  # keeps ctrl+c working as KeyboardInterrupt
        return self

    def __exit__(self, *exc):
        if os.name != "nt":
            import termios
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def read(self):
        """Return a list of key names pressed since the last call."""
        keys = []
        if os.name == "nt":
            m = self._msvcrt
            while m.kbhit():
                ch = m.getwch()
                if ch in ("\x00", "\xe0"):
                    code = m.getwch()
                    keys.append({"M": "right", "K": "left"}.get(code, ""))
                else:
                    keys.append(ch)
            return keys

        import select
        data = b""
        while select.select([self._fd], [], [], 0)[0]:
            chunk = os.read(self._fd, 64)
            if not chunk:
                break
            data += chunk
        i = 0
        while i < len(data):
            if data[i:i + 3] == b"\x1b[C":
                keys.append("right"); i += 3
            elif data[i:i + 3] == b"\x1b[D":
                keys.append("left"); i += 3
            elif data[i:i + 1] == b"\x1b":
                i += 3 if data[i + 1:i + 2] == b"[" else 1  # ignore other escapes
            else:
                keys.append(chr(data[i])); i += 1
        return keys


# --------------------------------------------------------------------------- #
# Audio (an ffplay process restarted whenever we seek / pause / change volume)
# --------------------------------------------------------------------------- #
class Audio:
    def __init__(self, path):
        self.path = path
        self.proc = None
        self.ffplay = shutil.which("ffplay") if path else None
        self.enabled = bool(self.ffplay) and self._has_audio(path)

    @staticmethod
    def _has_audio(path):
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=index", "-of", "csv=p=0", path],
                capture_output=True, text=True,
            ).stdout
            return bool(out.strip())
        except OSError:
            return False

    def start(self, position, volume):
        self.stop()
        if not self.enabled or volume <= 0:
            return
        self.proc = subprocess.Popen(
            [self.ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet",
             "-ss", f"{position:.3f}", "-volume", str(volume), self.path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


# --------------------------------------------------------------------------- #
# Player
# --------------------------------------------------------------------------- #
def fmt_time(t):
    t = int(max(0, t))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


def run(video, audio):
    out = sys.stdout
    duration = video.duration
    fps = video.fps or 30
    frame_time = 1.0 / fps

    position = 0.0      # seconds into the video
    volume = VOLUME_START
    paused = False
    ended = False
    frames = None       # current iterator of packed frames
    view = None         # current terminal-fitted view
    enc = None          # turns packed frames into (delta) terminal text
    clock_start = None  # wall-clock time of the first frame after a (re)start
    base = 0.0          # video position the current iterator started at
    index = 0           # frames consumed from the current iterator
    term_size = None

    def new_view():
        """Fit the terminal (1 row kept for the status bar) with a fresh encoder."""
        nonlocal view, enc, term_size
        term_size = shutil.get_terminal_size((80, 24))
        view = video.fit_terminal(margin_rows=2)
        enc = view.encoder()  # its first frame clears the screen and draws everything

    def restart():
        """(Re)start video + audio from `position` at the current terminal size."""
        nonlocal frames, clock_start, base, index
        if frames is not None:
            frames.close()
        new_view()
        frames = view.cell_frames(start=position)
        audio.start(position, volume)
        base, index, clock_start = position, 0, None

    def stop_playback():
        nonlocal frames
        if frames is not None:
            frames.close()
            frames = None
        audio.stop()

    def status():
        icon = "❚❚" if paused else "▶ "
        total = fmt_time(duration) if duration else "--:--"
        vol = f"vol {volume}%" if audio.enabled else "no audio"
        text = (f" {icon} {fmt_time(position)} / {total}   {vol}   "
                f"[space] {'play' if paused else 'pause'}  [←/→] ±{SEEK_STEP}s  "
                f"[+/-] volume  [ctrl+c] quit")
        if ended:
            text = f" ■ end of video   [←] go back  [ctrl+c] quit"
        cols, rows = shutil.get_terminal_size((80, 24))
        return f"\x1b[{rows};1H\x1b[0m\x1b[7m" + text[:cols].ljust(cols) + "\x1b[0m"

    def draw(cells=None):
        """Draw a new frame (only what changed) and/or the status bar, in one write."""
        frame = enc.encode(cells) if cells is not None else ""
        out.write(asciivid.SYNC_START + frame + status() + asciivid.SYNC_END)
        out.flush()

    asciivid._enable_windows_ansi()
    out.write("\x1b[?25l")  # hide cursor
    restart()
    try:
        with Keys() as keys:
            while True:
                # ---- handle input ------------------------------------------
                for key in keys.read():
                    if key == " " and not ended:
                        paused = not paused
                        if paused:
                            stop_playback()
                        else:
                            restart()
                    elif key in ("+", "="):
                        volume = min(100, volume + VOLUME_STEP)
                        if not paused and not ended:
                            audio.start(position, volume)
                    elif key in ("-", "_"):
                        volume = max(0, volume - VOLUME_STEP)
                        if not paused and not ended:
                            audio.start(position, volume)
                    elif key in ("right", "left"):
                        step = SEEK_STEP if key == "right" else -SEEK_STEP
                        position = max(0.0, position + step)
                        if duration:
                            position = min(position, max(0.0, duration - frame_time))
                        if ended and key == "left":
                            ended = False
                        if paused or ended:
                            # show the new spot without resuming
                            new_view()
                            it = view.cell_frames(start=position)
                            cells = next(it, None)
                            it.close()
                            if cells is not None:
                                draw(cells)
                        else:
                            restart()
                    if paused or ended:
                        draw()  # status bar only; the frame is already on screen

                if paused or ended:
                    # a resize while paused: redraw the paused frame at the new size
                    if shutil.get_terminal_size((80, 24)) != term_size:
                        new_view()
                        it = view.cell_frames(start=position)
                        cells = next(it, None)
                        it.close()
                        if cells is not None:
                            draw(cells)
                    time.sleep(0.03)
                    continue

                # ---- refit if the terminal was resized ----------------------
                if shutil.get_terminal_size((80, 24)) != term_size:
                    restart()

                # ---- next frame ---------------------------------------------
                cells = next(frames, None)
                if cells is None:
                    ended = True
                    stop_playback()
                    draw()
                    continue
                if clock_start is None:
                    clock_start = time.perf_counter()  # start timing once decoding is going
                target = clock_start + index * frame_time
                index += 1
                position = base + index * frame_time
                now = time.perf_counter()
                if now > target + frame_time:
                    continue  # running behind: drop this frame (encoder still diffs vs. screen)
                if target > now:
                    time.sleep(target - now)
                draw(cells)
    except KeyboardInterrupt:
        pass
    finally:
        stop_playback()
        out.write("\x1b[0m\x1b[?25h\x1b[2J\x1b[H")
        out.flush()


def main():
    parser = argparse.ArgumentParser(description="Play a video as colored ASCII in the terminal.")
    parser.add_argument("-v", "--video", required=True, help="video file (.mp4 etc.) or .asciv file")
    parser.add_argument("-a", "--audio", help="file to take sound from (defaults to the video itself)")
    parser.add_argument("--color", choices=["truecolor", "256", "none"], default="truecolor",
                        help="256 = shorter color codes, faster in slow terminals")
    parser.add_argument("--palette", choices=list(asciivid.PALETTES), default=asciivid.DEFAULT_PALETTE,
                        help="number of colors: high (65k), medium (4k, default), low (256)")
    parser.add_argument("--charset", default="standard",
                        help=f"one of {', '.join(asciivid.CHARSETS)} or your own dark-to-bright string")
    args = parser.parse_args()

    if not os.path.exists(args.video):
        parser.error(f"file not found: {args.video}")

    # here's the video
    video = asciivid.open_video(
        args.video,
        width="auto",
        color=None if args.color == "none" else args.color,
        charset=args.charset,
        palette=args.palette,
    )

    # .asciv files have no sound; use -a to point at the original video
    audio_path = args.audio or (None if video.is_converted else args.video)
    audio = Audio(audio_path)
    if audio_path and not audio.ffplay:
        print("note: ffplay not found, playing without sound", file=sys.stderr)
        time.sleep(1.5)

    run(video, audio)


if __name__ == "__main__":
    main()
