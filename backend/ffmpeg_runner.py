"""
Thin wrappers around ffmpeg / ffprobe.

`run_ffmpeg_with_progress` streams ffmpeg stderr/`-progress pipe:1` lines so
the renderer can report a 0..1 progress fraction while a job is running.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

from .config import config


# Audio target every rendered stage normalises to. Keeping these
# centralised means intro / highlight / main / transition all produce
# concat-demuxer-compatible streams without re-encode.
TARGET_AUDIO_RATE = 48000
TARGET_AUDIO_CHANNELS = 2


class FFmpegError(RuntimeError):
    def __init__(self, message: str, stderr: str = "", returncode: int = 1) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


class FFmpegCancelled(FFmpegError):
    """Raised when run_ffmpeg_with_progress is interrupted via cancel_check.
    Subclasses FFmpegError so existing `except FFmpegError` blocks still
    catch it; the renderer catches FFmpegCancelled first to flag the job
    as 'cancelled' rather than 'error'."""

    def __init__(self) -> None:
        super().__init__("Render cancelled by user", stderr="", returncode=-1)


def _fmt_mmss(seconds: float) -> str:
    """Compact m:ss / h:mm:ss formatter for progress messages. Pure
    seconds (e.g. '612.8s') are hard to read at a glance; '10:12' tells
    the operator how much main-stage video is left without mental math."""
    if seconds < 0 or seconds != seconds:  # NaN guard
        seconds = 0.0
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def nvenc_args() -> list[str]:
    """Standard NVENC video-encode flags pulled from `config.json`. Used
    by every render stage so encoder / preset / cq stay consistent."""
    return [
        "-c:v", config.encoder,
        "-preset", config.preset,
        "-rc", "vbr",
        "-cq", str(config.cq),
        "-b:v", "0",
        "-pix_fmt", "yuv420p",
    ]


def aac_args() -> list[str]:
    """Standard AAC audio-encode flags. Sample rate / channel count are
    locked to the package-wide TARGET_AUDIO_* constants."""
    return [
        "-c:a", "aac",
        "-ar", str(TARGET_AUDIO_RATE),
        "-ac", str(TARGET_AUDIO_CHANNELS),
        "-b:a", "192k",
    ]


def music_input_args(sound_path: Optional[Path], duration: float) -> list[str]:
    """ffmpeg input args for a fixed-duration music bed.

    With `sound_path`, the file is `-stream_loop -1` (looped infinitely)
    and `-t` capped at `duration` — works for any file length without
    the caller knowing whether the mp3 is shorter or longer than the
    clip. Without a path, returns a silent anullsrc of the same length
    so concat-demuxer stream layout stays uniform.
    """
    if sound_path is not None:
        return [
            "-stream_loop", "-1",
            "-t", f"{duration:.3f}",
            "-i", str(sound_path),
        ]
    return [
        "-f", "lavfi", "-t", f"{duration:.3f}",
        "-i", f"anullsrc=r={TARGET_AUDIO_RATE}:cl=stereo",
    ]


def music_filter_chain(
    *,
    input_idx: int,
    duration: float,
    volume: float = 0.7,
    fade_in: float = 0.3,
    fade_out: float = 0.5,
    output_label: str = "aout",
) -> str:
    """filter_complex chain that applies volume + afade in/out to an
    audio input and labels the result. The fade-out is anchored so it
    lands flush at `duration`.

    Caller `;`-joins this with the video chain and maps `[output_label]`
    instead of `<input_idx>:a`. Silent fallback path skips the chain and
    maps the input directly — afade on silence is a no-op but the chain
    just adds noise to filter_complex.
    """
    fade_out_start = max(0.0, duration - fade_out)
    return (
        f"[{input_idx}:a]volume={volume:.2f},"
        f"afade=t=in:st=0:d={fade_in:.3f},"
        f"afade=t=out:st={fade_out_start:.3f}:d={fade_out:.3f}"
        f"[{output_label}]"
    )


def hwaccel_input_args() -> list[str]:
    """Prepend before `-i <video>` to enable NVDEC for source decoding,
    or return an empty list when CUDA hwaccel is disabled in config."""
    if config.use_hwaccel:
        return ["-hwaccel", "cuda"]
    return []


def ffprobe_json(path: Path) -> dict:
    cmd = [
        config.ffprobe,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise FFmpegError(f"ffprobe failed for {path}", r.stderr, r.returncode)
    return json.loads(r.stdout or "{}")


def probe_video(path: Path) -> dict:
    """Return {width, height, fps, duration, has_audio, sample_rate}."""
    data = ffprobe_json(path)
    v_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    a_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "audio"),
        None,
    )
    if v_stream is None:
        raise FFmpegError(f"No video stream found in {path}")

    fps_raw = v_stream.get("r_frame_rate") or v_stream.get("avg_frame_rate") or "30/1"
    try:
        num, den = fps_raw.split("/")
        fps = float(num) / float(den) if float(den) else 30.0
    except Exception:
        fps = 30.0

    duration = 0.0
    fmt = data.get("format", {})
    if fmt.get("duration"):
        try:
            duration = float(fmt["duration"])
        except Exception:
            duration = 0.0
    if duration <= 0 and v_stream.get("duration"):
        try:
            duration = float(v_stream["duration"])
        except Exception:
            pass

    return {
        "width": int(v_stream.get("width", 1920)),
        "height": int(v_stream.get("height", 1080)),
        "fps": fps,
        "duration": duration,
        "has_audio": a_stream is not None,
        "sample_rate": int(a_stream.get("sample_rate", 48000)) if a_stream else 48000,
    }


def run_ffmpeg_with_progress(
    args: list[str],
    *,
    expected_out_seconds: float,
    on_progress: Optional[Callable[[float, str], None]] = None,
    log_prefix: str = "",
    cancel_check: Optional[Callable[[], bool]] = None,
) -> None:
    """
    Run an ffmpeg command. We append `-progress pipe:1 -nostats` so ffmpeg
    writes machine-readable key=value progress lines to stdout.

    `expected_out_seconds` lets us turn the running `out_time_us` into a
    0..1 fraction.

    `cancel_check`, if supplied, is called between progress lines; when it
    returns True, the ffmpeg process is terminated and FFmpegCancelled is
    raised. ffmpeg emits a progress line several times per second, so
    cancellation latency is sub-second under normal load.
    """
    full_cmd = [config.ffmpeg, "-y", "-hide_banner", *args, "-progress", "pipe:1", "-nostats"]
    if on_progress:
        on_progress(0.0, f"{log_prefix}starting")

    proc = subprocess.Popen(
        full_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    stderr_buf: list[str] = []

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_buf.append(line)
            # Cap memory; keep only last ~400 lines
            if len(stderr_buf) > 400:
                del stderr_buf[: len(stderr_buf) - 400]

    t = threading.Thread(target=_drain_stderr, daemon=True)
    t.start()

    cancelled = False
    last_out_us = 0
    assert proc.stdout is not None
    for line in proc.stdout:
        if cancel_check and cancel_check():
            cancelled = True
            try:
                proc.terminate()
            except Exception:
                pass
            break
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key == "out_time_us" or key == "out_time_ms":
            try:
                last_out_us = int(value)
            except ValueError:
                continue
            if on_progress and expected_out_seconds > 0:
                # out_time_us is microseconds in modern ffmpeg; out_time_ms is also
                # actually microseconds (legacy naming). Both treated the same.
                seconds = last_out_us / 1_000_000.0
                frac = max(0.0, min(0.999, seconds / expected_out_seconds))
                on_progress(
                    frac,
                    f"{log_prefix}{_fmt_mmss(seconds)} / {_fmt_mmss(expected_out_seconds)}",
                )
        elif key == "progress" and value == "end":
            if on_progress:
                on_progress(1.0, f"{log_prefix}done")

    if cancelled:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            proc.wait()
        t.join(timeout=2)
        raise FFmpegCancelled()

    rc = proc.wait()
    t.join(timeout=2)
    if rc != 0:
        tail = "".join(stderr_buf[-60:])
        raise FFmpegError(
            f"ffmpeg exited with code {rc}: {' '.join(full_cmd[:6])} ...",
            stderr=tail,
            returncode=rc,
        )


def extract_frame_at(src: Path, t_seconds: float, out_png: Path) -> None:
    """Pull a single frame from `src` at `t_seconds` to `out_png`. Used
    by the cinematic intro (mid-source frame for the blurred bg) and the
    outro (last-frame freeze). `-q:v 2` is the JPEG-equivalent quality
    setting libavutil applies to PNG output too — high enough that the
    blurred result is indistinguishable from a lossless capture."""
    args = [
        config.ffmpeg, "-y",
        "-ss", f"{max(0.0, t_seconds):.3f}",
        "-i", str(src),
        "-frames:v", "1",
        "-q:v", "2",
        str(out_png),
    ]
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0 or not out_png.exists():
        raise FFmpegError(
            f"frame extract failed for {src.name} @ {t_seconds:.2f}s",
            stderr=proc.stderr,
        )


def escape_ffmpeg_filter_path(p: Path) -> str:
    """
    Escape a path for use inside an ffmpeg filtergraph value (e.g. ass=...).
    Filtergraph parsing strips backslashes and treats ':' specially, so we
    convert to forward slashes and escape colons + drive letters.
    """
    s = str(p).replace("\\", "/")
    # Escape the drive-letter colon: C:/ -> C\\:/ inside a filter argument.
    if len(s) > 1 and s[1] == ":":
        s = s[0] + "\\:" + s[2:]
    return s
