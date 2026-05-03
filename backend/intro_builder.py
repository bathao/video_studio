"""
Cinematic intro renderer.

Builds a 6-second broadcast-style title card by composing in ffmpeg:

  - Background : one frame from the middle of the source video, gaussian-
                 blurred and dimmed, looped for the duration.
  - Avatars    : both players' photos, centre-cropped to a square, masked
                 to a circle, and slid in from off-screen with an
                 ease-out-cubic curve.
  - Text       : tournament name (top), player names (below avatars),
                 "VS" between the avatars — all rendered by libass via
                 `build_cinematic_intro_ass`.
  - Audio      : silent (anullsrc); music mix is reserved for v2.

The function takes ~2-3 s on an RTX 5060 Ti at 1080p and produces a
clip that's frame/sample-rate compatible with the rest of the render
pipeline (so the concat demuxer can stitch it without re-encoding).

If a player has no specific photo, the caller resolves that via
`avatars.find_avatar_or_default`; the renderer doesn't care which
image came from where as long as both inputs are valid image files.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

from .ass_builder import build_cinematic_intro_ass
from .config import config
from .ffmpeg_runner import (
    FFmpegError,
    escape_ffmpeg_filter_path,
    probe_video,
    run_ffmpeg_with_progress,
)

TARGET_AUDIO_RATE = 48000
TARGET_AUDIO_CHANNELS = 2


def _nvenc_args() -> list[str]:
    return [
        "-c:v", config.encoder,
        "-preset", config.preset,
        "-rc", "vbr",
        "-cq", str(config.cq),
        "-b:v", "0",
        "-pix_fmt", "yuv420p",
    ]


def _aac_args() -> list[str]:
    return [
        "-c:a", "aac",
        "-ar", str(TARGET_AUDIO_RATE),
        "-ac", str(TARGET_AUDIO_CHANNELS),
        "-b:a", "192k",
    ]


def _extract_bg_frame(src: Path, midpoint: float, out_png: Path) -> None:
    """Pull a single frame from `src` at `midpoint` seconds. Used as the
    static (looped) background for the intro — way cheaper than letting
    the main filter graph decode and blur live video for 6 seconds."""
    args = [
        config.ffmpeg, "-y",
        "-ss", f"{midpoint:.3f}",
        "-i", str(src),
        "-frames:v", "1",
        "-q:v", "2",
        str(out_png),
    ]
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0 or not out_png.exists():
        raise FFmpegError(
            "intro bg frame extract failed",
            stderr=proc.stderr,
        )


def render_cinematic_intro(
    *,
    out_path: Path,
    src: Path,
    width: int,
    height: int,
    fps: float,
    tournament: str,
    p1_name: str, p1_avatar: Path,
    p2_name: str, p2_avatar: Path,
    on_progress: Callable[[float, str], None],
    duration: float | None = None,
) -> float:
    """Render a cinematic intro clip and return its duration."""
    duration = duration if duration is not None else config.intro_duration_seconds
    asize = config.intro_avatar_size_px
    blur_sigma = config.intro_blur_sigma

    # Pull the background frame from the middle of the source so the
    # intro's blurred backdrop has the right venue / lighting flavour.
    src_dur = float(probe_video(src).get("duration", 0.0))
    midpoint = max(0.0, src_dur / 2.0 - 0.5) if src_dur > 0 else 0.0

    job_dir = out_path.parent
    bg_png = job_dir / "intro_bg.png"
    _extract_bg_frame(src, midpoint, bg_png)

    ass_path = out_path.with_suffix(".intro.ass")
    build_cinematic_intro_ass(
        output_path=ass_path,
        video_w=width, video_h=height,
        duration=duration,
        tournament=tournament,
        p1_name=p1_name, p2_name=p2_name,
        avatar_size_px=asize,
    )
    ass_arg = escape_ffmpeg_filter_path(ass_path)

    # Avatar processing chain (used identically for both players):
    # centre-crop to square → resize → soft circular alpha mask. The
    # `min(255,max(0,(r-d)*255))` softens the edge by 1 px so the circle
    # doesn't read jagged at 420 px.
    avatar_chain = (
        f"crop=min(iw\\,ih):min(iw\\,ih),"
        f"scale={asize}:{asize},format=yuva420p,"
        f"geq=r='r(X\\,Y)':g='g(X\\,Y)':b='b(X\\,Y)':"
        f"a='min(255\\,max(0\\,(W/2-2-hypot(X-W/2\\,Y-H/2))*255))'"
    )

    # Slide-in (ease-out cubic over 1.0 s):
    #   x(t) = start + (target - start) * (1 - (1 - t)^3)
    # P1 enters from the left edge, P2 from the right. After t=1.0 each
    # holds at its target so the rest of the intro is static.
    p1_x = (
        "if(lt(t\\,1.0)\\,"
        "(-w)+(W*0.27+w/2)*(1-(1-t)*(1-t)*(1-t))\\,"
        "W*0.27-w/2)"
    )
    p2_x = (
        "if(lt(t\\,1.0)\\,"
        "W-(W*0.27+w/2)*(1-(1-t)*(1-t)*(1-t))\\,"
        "W*0.73-w/2)"
    )
    # Avatars bob ±7 px on a ~1.8 s sine cycle — fast enough to read as
    # deliberate motion (not just camera shake) and clearly visible at
    # 1080p. Both avatars share the same expression so they breathe in
    # sync.
    avatar_y = "(H-h)/2+80+sin(t*3.5)*7"

    # zoompan's z expression only supports `on` (output frame index),
    # not `t`. Pre-compute zoom-per-output-frame so the bg lands at
    # ~1.08x by the end of the intro regardless of fps.
    zoom_per_frame = 0.012 / fps  # → ~1.0 + 0.084 over a 7 s @ 60 fps run
    filter_complex = ";".join([
        # Background: scale → blur → dim → slow Ken-Burns zoom. The zoom
        # is what keeps the still backdrop from feeling frozen after the
        # avatars settle.
        f"[0:v]scale={width}:{height},gblur=sigma={blur_sigma},"
        f"eq=brightness=-0.20,"
        f"zoompan=z='1+{zoom_per_frame:.6f}*on':"
        f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':"
        f"d=1:s={width}x{height}:fps={fps},"
        f"format=yuv420p[bg]",
        # Avatars.
        f"[1:v]{avatar_chain}[av1]",
        f"[2:v]{avatar_chain}[av2]",
        # Composite.
        f"[bg][av1]overlay=x='{p1_x}':y='{avatar_y}'[s1]",
        f"[s1][av2]overlay=x='{p2_x}':y='{avatar_y}'[s2]",
        # Text overlay (libass) + force yuv420p for NVENC.
        f"[s2]ass='{ass_arg}',format=yuv420p[vout]",
    ])

    args = [
        # 0: looped bg frame
        "-loop", "1", "-t", f"{duration:.3f}", "-i", str(bg_png),
        # 1: looped P1 avatar
        "-loop", "1", "-t", f"{duration:.3f}", "-i", str(p1_avatar),
        # 2: looped P2 avatar
        "-loop", "1", "-t", f"{duration:.3f}", "-i", str(p2_avatar),
        # 3: silent audio so the concat demuxer downstream stays happy
        "-f", "lavfi", "-t", f"{duration:.3f}",
        "-i", f"anullsrc=r={TARGET_AUDIO_RATE}:cl=stereo",
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "3:a",
        *_nvenc_args(),
        *_aac_args(),
        "-shortest",
        str(out_path),
    ]

    run_ffmpeg_with_progress(
        args,
        expected_out_seconds=duration,
        on_progress=on_progress,
        log_prefix="cinematic intro: ",
    )

    # Clean up the extracted bg frame; .ass stays alongside the mp4 for
    # debugging (matches the convention used for scoreboard.ass).
    try:
        bg_png.unlink()
    except OSError:
        pass

    return duration
