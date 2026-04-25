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


class FFmpegError(RuntimeError):
    def __init__(self, message: str, stderr: str = "", returncode: int = 1) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


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
) -> None:
    """
    Run an ffmpeg command. We append `-progress pipe:1 -nostats` so ffmpeg
    writes machine-readable key=value progress lines to stdout.

    `expected_out_seconds` lets us turn the running `out_time_us` into a
    0..1 fraction.
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

    last_out_us = 0
    assert proc.stdout is not None
    for line in proc.stdout:
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
                on_progress(frac, f"{log_prefix}{seconds:.1f}s / {expected_out_seconds:.1f}s")
        elif key == "progress" and value == "end":
            if on_progress:
                on_progress(1.0, f"{log_prefix}done")

    rc = proc.wait()
    t.join(timeout=2)
    if rc != 0:
        tail = "".join(stderr_buf[-60:])
        raise FFmpegError(
            f"ffmpeg exited with code {rc}: {' '.join(full_cmd[:6])} ...",
            stderr=tail,
            returncode=rc,
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
