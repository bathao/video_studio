"""
Cinematic intro renderer.

Builds a broadcast-style title card (duration from config
`intro_duration_seconds`, default 4 s) by composing in ffmpeg:

  - Background : one frame from the middle of the source video, gaussian-
                 blurred and dimmed, looped for the duration.
  - Avatars    : both players' photos, centre-cropped to a square, masked
                 to a circle, and slid in from off-screen with an
                 ease-out-cubic curve.
  - Text       : tournament name (top), player names (below avatars),
                 "VS" between the avatars — all rendered by libass via
                 `build_cinematic_intro_ass`.
  - Audio      : optional `intro_sound_path` mp3 (looped + capped) with
                 volume + afade in/out; falls back to anullsrc when the
                 file is missing from disk.

The function takes ~2-3 s on an RTX 5060 Ti at 1080p and produces a
clip that's frame/sample-rate compatible with the rest of the render
pipeline (so the concat demuxer can stitch it without re-encoding).

If a player has no specific photo, the caller resolves that via
`avatars.find_avatar_or_default`; the renderer doesn't care which
image came from where as long as both inputs are valid image files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from .ass import build_cinematic_intro_ass
from .config import config
from .ffmpeg_runner import (
    aac_args,
    escape_ffmpeg_filter_path,
    extract_frame_at,
    music_filter_chain,
    music_input_args,
    nvenc_args,
    probe_video,
    run_ffmpeg_with_progress,
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
    p1_team: str = "",
    p2_team: str = "",
    cancel_check: Optional[Callable[[], bool]] = None,
    # Doubles: when match_type == "double" AND both p3/p4 avatars
    # resolve, lay out four smaller avatars (two per side) instead of
    # two large ones. The caller is responsible for combining the four
    # player names into the two team-row labels before passing them as
    # p1_name / p2_name — this function only owns the visual layout.
    match_type: str = "single",
    p3_avatar: Optional[Path] = None,
    p4_avatar: Optional[Path] = None,
) -> float:
    """Render a cinematic intro clip and return its duration."""
    duration = duration if duration is not None else config.intro_duration_seconds
    is_doubles = (
        (match_type or "single").lower() == "double"
        and p3_avatar is not None and p4_avatar is not None
    )
    # Doubles needs to fit two avatars per side, so each shrinks to
    # ~60% of the singles size — keeps the pair within W*0.27 ± 200 px
    # and well clear of the centre 'VS'.
    asize = (
        int(config.intro_avatar_size_px * 0.6) if is_doubles
        else config.intro_avatar_size_px
    )
    blur_sigma = config.intro_blur_sigma
    sound_path = config.intro_sound_path
    sound_volume = config.intro_sound_volume

    # Pull the background frame from the middle of the source so the
    # intro's blurred backdrop has the right venue / lighting flavour.
    src_dur = float(probe_video(src).get("duration", 0.0))
    midpoint = max(0.0, src_dur / 2.0 - 0.5) if src_dur > 0 else 0.0

    job_dir = out_path.parent
    bg_png = job_dir / "intro_bg.png"
    extract_frame_at(src, midpoint, bg_png)

    ass_path = out_path.with_suffix(".intro.ass")
    build_cinematic_intro_ass(
        output_path=ass_path,
        video_w=width, video_h=height,
        duration=duration,
        tournament=tournament,
        p1_name=p1_name, p2_name=p2_name,
        p1_team=p1_team, p2_team=p2_team,
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

    # Avatars bob ±7 px on a ~1.8 s sine cycle — fast enough to read as
    # deliberate motion (not just camera shake) and clearly visible at
    # 1080p. All avatars share the same expression so they breathe in
    # sync, even when there are four of them in a doubles pair.
    avatar_y = "(H-h)/2+80+sin(t*3.5)*7"

    # zoompan's z expression only supports `on` (output frame index),
    # not `t`. Pre-compute zoom-per-output-frame so the bg zooms at
    # 0.012x/s regardless of fps (~1.05x over the 4 s default intro).
    zoom_per_frame = 0.012 / fps
    bg_chain = (
        f"[0:v]scale={width}:{height},gblur=sigma={blur_sigma},"
        f"eq=brightness=-0.20,"
        f"zoompan=z='1+{zoom_per_frame:.6f}*on':"
        f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':"
        f"d=1:s={width}x{height}:fps={fps},"
        f"format=yuv420p[bg]"
    )

    if is_doubles:
        # Doubles: two avatars per side, sliding in as a pair. gap_half
        # scales with video width so 4K (3840p) gets a proportionally
        # wider gap than 1080p (1920p).
        gap_half = max(8, int(width * 0.008))
        # Slide-in (ease-out cubic over 1.0 s) for four avatars. Each
        # expression interpolates the per-frame x position from off-
        # screen to its final target. After t=1.0 every avatar locks
        # into its target so the rest of the intro is static.
        # Pair 1 (left side, slides in from left edge):
        p1a_x = (
            f"if(lt(t\\,1.0)\\,"
            f"(-w)+(W*0.27-{gap_half})*(1-(1-t)*(1-t)*(1-t))\\,"
            f"W*0.27-w-{gap_half})"
        )
        p1b_x = (
            f"if(lt(t\\,1.0)\\,"
            f"(-w)+(W*0.27+{gap_half}+w)*(1-(1-t)*(1-t)*(1-t))\\,"
            f"W*0.27+{gap_half})"
        )
        # Pair 2 (right side, slides in from right edge):
        p2a_x = (
            f"if(lt(t\\,1.0)\\,"
            f"W-(W*0.27+w+{gap_half})*(1-(1-t)*(1-t)*(1-t))\\,"
            f"W*0.73-w-{gap_half})"
        )
        p2b_x = (
            f"if(lt(t\\,1.0)\\,"
            f"W-(W*0.27-{gap_half})*(1-(1-t)*(1-t)*(1-t))\\,"
            f"W*0.73+{gap_half})"
        )
        video_chains = [
            bg_chain,
            f"[1:v]{avatar_chain}[av1]",
            f"[2:v]{avatar_chain}[av2]",
            f"[3:v]{avatar_chain}[av3]",
            f"[4:v]{avatar_chain}[av4]",
            f"[bg][av1]overlay=x='{p1a_x}':y='{avatar_y}'[s1]",
            f"[s1][av2]overlay=x='{p1b_x}':y='{avatar_y}'[s2]",
            f"[s2][av3]overlay=x='{p2a_x}':y='{avatar_y}'[s3]",
            f"[s3][av4]overlay=x='{p2b_x}':y='{avatar_y}'[s4]",
            f"[s4]ass='{ass_arg}',format=yuv420p[vout]",
        ]
        if sound_path is not None:
            video_chains.append(music_filter_chain(
                input_idx=5, duration=duration, volume=sound_volume,
            ))
            audio_map = "[aout]"
        else:
            audio_map = "5:a"
        filter_complex = ";".join(video_chains)
        # Input order is wired to the overlay chain below: [1:v] → av1
        # ends up at p1a_x (team 1 left), [2:v] → av2 at p1b_x (team 1
        # right), [3:v] → av3 at p2a_x (team 2 left), [4:v] → av4 at
        # p2b_x (team 2 right). Team 1 = P1+P3, Team 2 = P2+P4, so we
        # feed P3 before P2 here even though the caller passes them in
        # numerical order. Swapping the avatars at the filter-graph
        # boundary keeps the overlay chain free of an extra label
        # rename step.
        args = [
            "-loop", "1", "-t", f"{duration:.3f}", "-i", str(bg_png),
            "-loop", "1", "-t", f"{duration:.3f}", "-i", str(p1_avatar),
            "-loop", "1", "-t", f"{duration:.3f}", "-i", str(p3_avatar),
            "-loop", "1", "-t", f"{duration:.3f}", "-i", str(p2_avatar),
            "-loop", "1", "-t", f"{duration:.3f}", "-i", str(p4_avatar),
            *music_input_args(sound_path, duration),
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", audio_map,
            *nvenc_args(),
            *aac_args(),
            "-shortest",
            str(out_path),
        ]
    else:
        # Singles: original two-avatar layout. P1 enters from the left
        # edge, P2 from the right.
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
        video_chains = [
            bg_chain,
            f"[1:v]{avatar_chain}[av1]",
            f"[2:v]{avatar_chain}[av2]",
            f"[bg][av1]overlay=x='{p1_x}':y='{avatar_y}'[s1]",
            f"[s1][av2]overlay=x='{p2_x}':y='{avatar_y}'[s2]",
            f"[s2]ass='{ass_arg}',format=yuv420p[vout]",
        ]
        if sound_path is not None:
            video_chains.append(music_filter_chain(
                input_idx=3, duration=duration, volume=sound_volume,
            ))
            audio_map = "[aout]"
        else:
            audio_map = "3:a"
        filter_complex = ";".join(video_chains)
        args = [
            "-loop", "1", "-t", f"{duration:.3f}", "-i", str(bg_png),
            "-loop", "1", "-t", f"{duration:.3f}", "-i", str(p1_avatar),
            "-loop", "1", "-t", f"{duration:.3f}", "-i", str(p2_avatar),
            *music_input_args(sound_path, duration),
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", audio_map,
            *nvenc_args(),
            *aac_args(),
            "-shortest",
            str(out_path),
        ]

    run_ffmpeg_with_progress(
        args,
        expected_out_seconds=duration,
        on_progress=on_progress,
        log_prefix="cinematic intro: ",
        cancel_check=cancel_check,
    )

    # Clean up the extracted bg frame; .ass stays alongside the mp4 for
    # debugging (matches the convention used for scoreboard.ass).
    try:
        bg_png.unlink()
    except OSError:
        pass

    return duration
