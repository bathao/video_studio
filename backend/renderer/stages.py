"""Stage helpers: each emits one mp4 in temp/<job_id>/.

These functions take plain kwargs (no RenderContext) so they don't
depend on the orchestrator. The orchestrator wires them into the
pipeline; stages.py knows nothing about job state or the run loop.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Optional

from ..ass import (
    build_intro_ass,
    build_outro_card_ass,
)
from ..ffmpeg_runner import (
    FFmpegCancelled,
    FFmpegError,
    TARGET_AUDIO_RATE,
    aac_args,
    escape_ffmpeg_filter_path,
    hwaccel_input_args,
    music_filter_chain,
    music_input_args,
    nvenc_args,
    probe_video,
    run_ffmpeg_with_progress,
)
from .replays import (
    REPLAY_FADE_SECONDS,
    REPLAY_SPEED,
    REPLAY_VOLUME,
    _PlaylistEntry,
)


def render_outro_card(
    *,
    out_path: Path,
    width: int,
    height: int,
    fps: float,
    duration: float,
    text: str,
    bg_path: Optional[Path],
    sound_path: Optional[Path] = None,
    sound_volume: float = 0.7,
    on_progress: Callable[[float, str], None],
    cancel_check: Optional[Callable[[], bool]] = None,
) -> float:
    """Render the closing outro card.

    Background is `bg_path` (typically the extracted last frame of
    main.mp4, blurred and dimmed by this filter graph); when None or
    missing, falls back to a solid dark colour from lavfi. The libass
    overlay carries the headline + a full-frame black box that fades
    in over the final second of the clip.

    Audio is the optional `sound_path` mp3 (looped + capped) with a 1 s
    afade-out anchored to the fade-to-black tail; falls back to silent
    anullsrc when the file is missing — `-an` would break the concat
    demuxer's stream-layout check since every other rendered part has
    an audio track. Returns the clip duration.
    """
    ass_path = out_path.with_suffix(".outro.ass")
    build_outro_card_ass(
        output_path=ass_path,
        video_w=width, video_h=height,
        duration=duration,
        text=text,
    )

    args: list[str] = []
    # Input 0: background frame. Real image when available — gblur +
    # eq turn the action shot into a static artistic backdrop. lavfi
    # color is the no-asset fallback; already flat so no blur applied.
    if bg_path is not None:
        # `-framerate` before `-loop 1 -i` is mandatory: image2 demuxer
        # defaults to 25 fps, which mismatches the source-native fps used
        # by every other stage and breaks the concat demuxer (silently
        # drops the section's video track — outro plays as black).
        args += [
            "-loop", "1",
            "-framerate", f"{fps}",
            "-t", f"{duration:.3f}",
            "-i", str(bg_path),
        ]
        bg_chain = (
            f"[0:v]scale={width}:{height},gblur=sigma=30,"
            f"eq=brightness=-0.3,format=yuv420p[bg]"
        )
    else:
        args += [
            "-f", "lavfi", "-t", f"{duration:.3f}",
            "-i", f"color=c=0x0a0c10:s={width}x{height}:r={fps}",
        ]
        bg_chain = f"[0:v]format=yuv420p[bg]"

    # Input 1: music bed when sound_path is set; silent anullsrc
    # otherwise. Either way the map stays at index 1 / [aout].
    args += music_input_args(sound_path, duration)

    ass_arg = escape_ffmpeg_filter_path(ass_path)
    chains = [
        bg_chain,
        f"[bg]ass='{ass_arg}',format=yuv420p[vout]",
    ]
    if sound_path is not None:
        # Outro fade-out is 1 s so the music sinks together with the
        # full-frame black box that fades in over the final second.
        chains.append(music_filter_chain(
            input_idx=1, duration=duration,
            volume=sound_volume, fade_in=0.5, fade_out=1.0,
        ))
        audio_map = "[aout]"
    else:
        audio_map = "1:a"
    filter_complex = ";".join(chains)

    args += [
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", audio_map,
        *nvenc_args(),
        *aac_args(),
        "-shortest",
        str(out_path),
    ]

    run_ffmpeg_with_progress(
        args,
        expected_out_seconds=duration,
        on_progress=on_progress,
        log_prefix="outro: ",
        cancel_check=cancel_check,
    )
    return duration


def render_intro(
    *,
    out_path: Path,
    width: int,
    height: int,
    fps: float,
    tournament: str,
    p1: str,
    p2: str,
    on_progress: Callable[[float, str], None],
    cancel_check: Optional[Callable[[], bool]] = None,
) -> None:
    """
    3-second title card. Uses libass instead of drawtext so Vietnamese
    diacritics render correctly and we get fade / slide animations for
    free.
    """
    duration = 3.0

    # Generate the intro .ass next to the intro mp4 in the same job dir.
    ass_path = out_path.with_suffix(".intro.ass")
    build_intro_ass(
        output_path=ass_path,
        video_w=width,
        video_h=height,
        duration=duration,
        tournament=tournament,
        p1_name=p1,
        p2_name=p2,
    )

    ass_arg = escape_ffmpeg_filter_path(ass_path)
    args = [
        "-f", "lavfi", "-i", f"color=c=0x101418:s={width}x{height}:r={fps}:d={duration}",
        "-f", "lavfi", "-i", f"anullsrc=r={TARGET_AUDIO_RATE}:cl=stereo",
        "-vf", f"ass='{ass_arg}',format=yuv420p",
        "-t", f"{duration}",
        *nvenc_args(),
        *aac_args(),
        "-shortest",
        str(out_path),
    ]
    run_ffmpeg_with_progress(
        args,
        expected_out_seconds=duration,
        on_progress=on_progress,
        log_prefix="intro: ",
        cancel_check=cancel_check,
    )


def render_main_with_scoreboard(
    *,
    src: Path,
    out_path: Path,
    ass_path: Path,
    slow_motion_badge_ass: Optional[Path],
    playlist: list[_PlaylistEntry],
    width: int,
    height: int,
    fps: float,
    has_audio: bool,
    on_progress: Callable[[float, str], None],
    cancel_check: Optional[Callable[[], bool]] = None,
    replay_sound_path: Optional[Path] = None,
    replay_sound_volume: float = 0.7,
) -> float:
    """
    Render the main match: open the source once per playlist entry with
    input-side seeking (`-ss BEFORE -i`), apply slow-mo (setpts*2,
    atempo=0.5) to replay entries, concat everything in chronological
    order, then burn scoreboard + SLOW MOTION badge — all in one
    NVDEC → CPU filter → NVENC pipeline.

    Each playlist entry is one ffmpeg input slice. Slice entries are
    processed normally; replay entries get setpts*2.0 / atempo=0.5 so
    the spliced-in replay clip lands at 50% speed. The concat demuxer
    can't do this rewind-inside-stream gymnastics, hence the single big
    filter_complex.
    """
    if not playlist:
        raise FFmpegError("No content kept after trim segments")

    args: list[str] = []
    for entry in playlist:
        if entry.kind in ("stinger_in", "stinger_out"):
            # Stinger is a pre-rendered mp4 already at the right
            # resolution / fps / codec — feed it as a plain input, no
            # seek, no NVDEC (the file is short so software decode is
            # fine and avoids holding an extra NVDEC session).
            args += ["-i", str(entry.src_path)]
        else:
            slice_len = entry.src_end - entry.src_start
            args += [
                *hwaccel_input_args(),
                "-ss", f"{entry.src_start:.3f}",
                "-t", f"{slice_len:.3f}",
                "-i", str(src),
            ]
    n = len(playlist)
    expected_total = sum(e.final_end - e.final_start for e in playlist)

    # Per-replay music inputs — one `-stream_loop -1 -t r_dur -i <file>`
    # per replay so a short mp3 loops to fill and a long mp3 gets
    # trimmed. Indexed right after the n source clip inputs. Disabled
    # when the source has no audio track — the concat=a=1 path needs
    # every entry to produce audio, and synthesising silence per slice
    # just to keep one branch alive isn't worth the filter-complex
    # noise.
    replay_music_idx_map: dict[int, int] = {}
    next_input_idx = n
    if has_audio and replay_sound_path is not None:
        for i, entry in enumerate(playlist):
            if entry.kind != "replay":
                continue
            r_dur = entry.final_end - entry.final_start
            args += [
                "-stream_loop", "-1",
                "-t", f"{r_dur:.3f}",
                "-i", str(replay_sound_path),
            ]
            replay_music_idx_map[i] = next_input_idx
            next_input_idx += 1

    # If the source has no audio, we still need an audio stream in the
    # output (so the final concat-demuxer doesn't fail on stream mismatch).
    # Goes AFTER the replay music inputs so indices stay valid.
    silent_idx: Optional[int] = None
    if not has_audio:
        silent_idx = next_input_idx
        args += ["-f", "lavfi", "-i", f"anullsrc=r={TARGET_AUDIO_RATE}:cl=stereo"]
        next_input_idx += 1

    # Build the per-entry processing chains. Replay entries stretch
    # video PTS to 2× and halve audio tempo so they play at half speed.
    # Stinger entries pass through (they're pre-rendered at the right
    # spec already; just scale/fps-normalise defensively and reset PTS).
    inv_speed = 1.0 / REPLAY_SPEED
    v_parts: list[str] = []
    a_parts: list[str] = []
    concat_inputs: list[str] = []
    for i, entry in enumerate(playlist):
        if entry.kind == "replay":
            v_parts.append(
                f"[{i}:v]setpts={inv_speed:.3f}*(PTS-STARTPTS),"
                f"scale={width}:{height},fps={fps}[vk{i}]"
            )
            if has_audio:
                # Fade window is capped so back-to-back in/out don't
                # overlap on tiny replays.
                r_dur = entry.final_end - entry.final_start
                fade_d = max(0.05, min(REPLAY_FADE_SECONDS, r_dur / 4.0))
                fade_out_st = max(0.0, r_dur - fade_d)
                if i in replay_music_idx_map:
                    # Replay music input — already looped + capped to
                    # r_dur via -stream_loop / -t at the input stage,
                    # so the filter chain just needs volume + fades.
                    music_idx = replay_music_idx_map[i]
                    a_parts.append(
                        f"[{music_idx}:a]"
                        f"volume={replay_sound_volume:.2f},"
                        f"afade=t=in:st=0:d={fade_d:.3f},"
                        f"afade=t=out:st={fade_out_st:.3f}:d={fade_d:.3f},"
                        f"aformat=sample_rates={TARGET_AUDIO_RATE}:channel_layouts=stereo[ak{i}]"
                    )
                else:
                    # No music file configured — fall back to muted
                    # source audio at half tempo (REPLAY_VOLUME=0). The
                    # atempo=0.5 doubles audio length to match setpts*2.
                    a_parts.append(
                        f"[{i}:a]asetpts=PTS-STARTPTS,atempo={REPLAY_SPEED},"
                        f"volume={REPLAY_VOLUME},"
                        f"afade=t=in:st=0:d={fade_d:.3f},"
                        f"afade=t=out:st={fade_out_st:.3f}:d={fade_d:.3f},"
                        f"aformat=sample_rates={TARGET_AUDIO_RATE}:channel_layouts=stereo[ak{i}]"
                    )
        else:
            # Slice OR stinger. Stinger inputs are already at target
            # resolution/fps from the cache, but we keep the scale/fps
            # filter as a safety net (no-op when the input matches).
            v_parts.append(
                f"[{i}:v]setpts=PTS-STARTPTS,scale={width}:{height},fps={fps}[vk{i}]"
            )
            if has_audio:
                a_parts.append(
                    f"[{i}:a]asetpts=PTS-STARTPTS,"
                    f"aformat=sample_rates={TARGET_AUDIO_RATE}:channel_layouts=stereo[ak{i}]"
                )
        if has_audio:
            concat_inputs.append(f"[vk{i}][ak{i}]")
        else:
            concat_inputs.append(f"[vk{i}]")

    if has_audio:
        concat_filter = "".join(concat_inputs) + f"concat=n={n}:v=1:a=1[vc][aout]"
    else:
        concat_filter = "".join(concat_inputs) + f"concat=n={n}:v=1:a=0[vc]"

    # Chain overlay burns: scoreboard → SLOW MOTION badge. Each `ass=`
    # filter runs over the previous output, so the composition order
    # matches the visual stacking we want. The slow-mo badge is optional
    # (skipped when no replays were spliced).
    ass_arg = escape_ffmpeg_filter_path(ass_path)
    burn_chain = [f"[vc]ass='{ass_arg}'[vb0]"]
    cur_label = "vb0"
    if slow_motion_badge_ass is not None:
        next_label = "vb1"
        sm_arg = escape_ffmpeg_filter_path(slow_motion_badge_ass)
        burn_chain.append(f"[{cur_label}]ass='{sm_arg}'[{next_label}]")
        cur_label = next_label
    # Final rename so the map below is stable regardless of whether the
    # optional slow-mo badge was chained.
    burn_chain.append(f"[{cur_label}]null[vout]")
    filter_complex = ";".join(v_parts + a_parts + [concat_filter] + burn_chain)

    args += [
        "-filter_complex", filter_complex,
        "-map", "[vout]",
    ]
    if has_audio:
        args += ["-map", "[aout]", *nvenc_args(), *aac_args()]
    else:
        args += ["-map", f"{silent_idx}:a", *nvenc_args(), *aac_args(), "-shortest"]
    args += [str(out_path)]

    run_ffmpeg_with_progress(
        args,
        expected_out_seconds=expected_total,
        on_progress=on_progress,
        log_prefix="main: ",
        cancel_check=cancel_check,
    )
    return expected_total


def concat_parts(
    parts: list[Path],
    out_path: Path,
    on_progress: Callable[[float, str], None],
    cancel_check: Optional[Callable[[], bool]] = None,
) -> None:
    if not parts:
        raise FFmpegError("Nothing to concat")
    if cancel_check and cancel_check():
        raise FFmpegCancelled()
    if len(parts) == 1:
        shutil.copyfile(parts[0], out_path)
        on_progress(1.0, "concat: copied single part")
        return

    list_file = out_path.with_suffix(".concat.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in parts:
            safe = str(p).replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{safe}'\n")

    args = [
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(out_path),
    ]
    # Estimate total seconds from each part for a better progress fraction.
    total = 0.0
    for p in parts:
        try:
            total += probe_video(p).get("duration", 0.0)
        except Exception:
            pass
    run_ffmpeg_with_progress(
        args,
        expected_out_seconds=max(total, 1.0),
        on_progress=on_progress,
        log_prefix="concat: ",
        cancel_check=cancel_check,
    )
    try:
        list_file.unlink()
    except OSError:
        pass
