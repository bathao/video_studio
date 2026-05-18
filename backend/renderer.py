"""
Render orchestrator.

Stage helpers each produce an MP4 in temp/<job_id>/, then we concat them
with the concat demuxer (no re-encode) into the final output.

Stage layout:

  intro.mp4       — cinematic / text title card
  main.mp4        — source video minus trim_segments, with the scoreboard
                    burned in via libass. Every highlight gets a 50%-speed
                    replay spliced in right after its real-time occurrence,
                    with a pulsing SLOW MOTION badge on the top-left for
                    the duration of each replay.
  outro.mp4       — closing card over a blurred freeze-frame of main's last
                    frame, fading to black at the tail.

All produced files share the same resolution, fps, pixel format, audio
sample rate, channel layout, and codec, so the concat demuxer can stitch
them without a re-encode.
"""

from __future__ import annotations

import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .ass import (
    ScoreFrame,
    build_intro_ass,
    build_outro_card_ass,
    build_scoreboard_ass,
    build_slow_motion_badge_ass,
)
from .ass.scoreboard import resolve_row_names
from .avatars import find_avatar_or_default
from .config import config
from .ffmpeg_runner import (
    FFmpegCancelled,
    FFmpegError,
    TARGET_AUDIO_RATE,
    aac_args,
    escape_ffmpeg_filter_path,
    extract_frame_at,
    hwaccel_input_args,
    music_filter_chain,
    music_input_args,
    nvenc_args,
    probe_video,
    run_ffmpeg_with_progress,
)
from .intro_builder import render_cinematic_intro
from .models import Highlight, ProjectData, TrimSegment
from .stinger_builder import find_brand_logo, get_or_build_stinger_pair

@dataclass
class RenderState:
    job_id: str
    status: str = "queued"
    progress: float = 0.0
    stage: str = ""
    message: str = ""
    output_path: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: str = ""
    cancel_requested: bool = False

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "progress": round(self.progress, 4),
            "stage": self.stage,
            "message": self.message,
            "output_path": self.output_path,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


# ---------- segment math ----------------------------------------------------


def kept_segments_from_trims(
    duration: float,
    trims: list[TrimSegment],
) -> list[tuple[float, float]]:
    """Invert a list of remove-segments into the list of keep-segments."""
    if duration <= 0:
        return []
    if not trims:
        return [(0.0, duration)]
    cleaned: list[tuple[float, float]] = []
    for t in trims:
        a = max(0.0, min(duration, float(t.start)))
        b = max(0.0, min(duration, float(t.end)))
        if b > a:
            cleaned.append((a, b))
    cleaned.sort()
    # Merge overlaps
    merged: list[list[float]] = []
    for a, b in cleaned:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    kept: list[tuple[float, float]] = []
    cursor = 0.0
    for a, b in merged:
        if a > cursor:
            kept.append((cursor, a))
        cursor = b
    if cursor < duration:
        kept.append((cursor, duration))
    return kept


def remap_score_event_to_trimmed(
    t_source: float,
    kept: list[tuple[float, float]],
) -> Optional[float]:
    """
    Convert a source-time score timestamp to its position inside the
    trimmed main video. Returns None if there are no kept segments.
    Events that fall inside a removed gap are snapped forward to the
    start of the next kept segment so the score-change still appears.
    """
    if not kept:
        return None
    accumulated = 0.0
    for a, b in kept:
        if t_source < a:
            return accumulated  # snap forward to start of this kept segment
        if t_source <= b:
            return accumulated + (t_source - a)
        accumulated += b - a
    return accumulated  # past end → end of trimmed video


# ---------- slow-mo replay plumbing ----------------------------------------

# Speed factor for slow-mo replays inserted into main. 0.5 → 2× duration,
# atempo=0.5 audio. Picked to match what's visually readable for a table-
# tennis rally — fast enough that the replay doesn't drag, slow enough
# to show the rally's geometry clearly.
REPLAY_SPEED = 0.5

# Volume scale applied to the replay clip's audio. atempo=0.5 leaves the
# pitch intact but smears the rally noises (ball-hits, crowd) into a
# muddy drone that's worse than helpful — half the volume keeps the
# slow-mo feeling immersive without making it the loudest thing on the
# track.
REPLAY_VOLUME = 0.0

# Linear fade window (seconds) applied to BOTH ends of the replay audio.
# Smooths the snap between real-time main slice (full volume) and the
# slow-mo audio (REPLAY_VOLUME) at the concat boundaries — the dip to
# silence reads as a deliberate "wow moment" beat before/after the
# slow-mo, instead of an audible level cut. Capped to 1/4 of the
# replay's final duration so very short replays don't overlap the
# two fades into each other.
REPLAY_FADE_SECONDS = 0.3


@dataclass(frozen=True)
class ReplayInsert:
    """A slow-mo replay of a highlight, scheduled to play in the main
    render right after the highlight's real-time occurrence.

    `insert_at_main` is the moment (in TRIMMED-main coords, before any
    replay is added to the timeline) where the replay drops in. Built
    by remapping the highlight's source `end` time through the trims.

    `src_start` / `src_end` is the source range to slow down. The final
    replay clip plays for `(src_end - src_start) / REPLAY_SPEED` seconds.
    """
    insert_at_main: float
    src_start: float
    src_end: float

    @property
    def replay_duration(self) -> float:
        return (self.src_end - self.src_start) / REPLAY_SPEED


def build_replay_plan(
    highlights: list[Highlight],
    kept: list[tuple[float, float]],
) -> list[ReplayInsert]:
    """For each highlight, compute the trimmed-main insert point right
    after its real-time playback. Highlights whose `end` lies inside a
    trim get snapped forward to the start of the next kept segment, so
    the replay still plays once the main video resumes (which still
    reads as 'just after we saw the action live')."""
    plan: list[ReplayInsert] = []
    for h in highlights:
        if h.end <= h.start:
            continue
        t_main = remap_score_event_to_trimmed(h.end, kept)
        if t_main is None:
            continue
        plan.append(ReplayInsert(
            insert_at_main=t_main,
            src_start=float(h.start),
            src_end=float(h.end),
        ))
    plan.sort(key=lambda r: r.insert_at_main)
    return plan


def remap_events_with_replays(
    events: list[ScoreFrame],
    replays: list[ReplayInsert],
    *,
    stinger_total_duration: float = 0.0,
) -> list[ScoreFrame]:
    """Shift each score event by the cumulative replay (+ optional
    stinger bracket) duration of replays that occur strictly before
    the event. Events that fire AT a replay's insert point stay put
    so the post-highlight score is visible during the replay too.

    `stinger_total_duration` is `in_duration + out_duration` (the
    asymmetric bracket: long IN before, short OUT after). When > 0
    each replay adds this on top of `replay_duration` to the timeline;
    subsequent events shift by the combined amount."""
    if not replays:
        return events
    out: list[ScoreFrame] = []
    for ev in events:
        shift = 0.0
        for r in replays:
            if r.insert_at_main < ev.timestamp:
                shift += r.replay_duration + stinger_total_duration
            else:
                break
        out.append(ScoreFrame(
            timestamp=ev.timestamp + shift,
            p1_score=ev.p1_score, p2_score=ev.p2_score,
            p1_set=ev.p1_set, p2_set=ev.p2_set,
        ))
    return out


@dataclass(frozen=True)
class _PlaylistEntry:
    """One ffmpeg input slot for the main render.

    `kind` discriminates:
      - "slice"      : a normal-speed slice of the source video
      - "replay"     : a slow-mo replay (needs setpts*2 + atempo=0.5)
      - "stinger_in" : pre-rendered branded transition before a replay
      - "stinger_out": pre-rendered branded transition after a replay
                       (same content as stinger_in, played reversed)

    For "slice" / "replay" the source is the main project video and we
    use input-side `-ss src_start -t (src_end-src_start) -i <src>`. For
    stinger entries, `src_path` points to the cached stinger mp4 and the
    whole file is consumed (no -ss/-t needed).

    `final_start` / `final_end` are the entry's position in the final
    main timeline — used to time the SLOW MOTION badge during replay
    entries and to remap score events past every entry's contribution.
    """
    kind: str
    src_start: float
    src_end: float
    final_start: float
    final_end: float
    src_path: Optional[Path] = None   # only set for stinger entries


def build_main_playlist(
    kept: list[tuple[float, float]],
    replays: list[ReplayInsert],
    *,
    stinger_in_path: Optional[Path] = None,
    stinger_out_path: Optional[Path] = None,
    stinger_in_duration: float = 0.0,
    stinger_out_duration: float = 0.0,
) -> list[_PlaylistEntry]:
    """Walk the kept segments and splice each replay in at its insert
    point. Kept segments that contain insert points get split into
    sub-slices; replays drop in between. When stinger paths are supplied
    each replay is bracketed with a sting-in (before) and sting-out
    (after). IN and OUT have INDEPENDENT durations — the IN clip is
    the long readable hold, OUT is the quick wipe-out back to live.

    When a replay's insert point lands exactly at a kept-segment
    boundary, the splice falls between two existing slices — no extra
    split is generated.

    Stinger entries always have `src_path` set; slice / replay entries
    leave it None so the caller knows to use the main source with
    input-side `-ss` / `-t`.
    """
    has_stinger = (
        stinger_in_path is not None
        and stinger_out_path is not None
        and stinger_in_duration > 0.0
        and stinger_out_duration > 0.0
    )

    entries: list[_PlaylistEntry] = []
    accumulated_main = 0.0          # trimmed-main offset at start of current kept
    accumulated_final = 0.0         # final-timeline cursor (incl. replay + sting)
    replay_idx = 0

    def _append_replay_with_stingers(r: ReplayInsert) -> None:
        nonlocal accumulated_final
        if has_stinger:
            entries.append(_PlaylistEntry(
                kind="stinger_in",
                src_start=0.0, src_end=stinger_in_duration,
                final_start=accumulated_final,
                final_end=accumulated_final + stinger_in_duration,
                src_path=stinger_in_path,
            ))
            accumulated_final += stinger_in_duration
        r_len = r.replay_duration
        entries.append(_PlaylistEntry(
            kind="replay",
            src_start=r.src_start, src_end=r.src_end,
            final_start=accumulated_final,
            final_end=accumulated_final + r_len,
        ))
        accumulated_final += r_len
        if has_stinger:
            entries.append(_PlaylistEntry(
                kind="stinger_out",
                src_start=0.0, src_end=stinger_out_duration,
                final_start=accumulated_final,
                final_end=accumulated_final + stinger_out_duration,
                src_path=stinger_out_path,
            ))
            accumulated_final += stinger_out_duration

    for a, b in kept:
        seg_len = b - a
        kept_end_main = accumulated_main + seg_len
        current_src = a

        # Consume any replays whose insert point falls inside this kept
        # segment's trimmed-main range. Equal-to-end is consumed here so
        # the replay slots in before moving to the next kept.
        while replay_idx < len(replays) and replays[replay_idx].insert_at_main <= kept_end_main:
            r = replays[replay_idx]
            offset_in_kept = max(0.0, r.insert_at_main - accumulated_main)
            split_src = a + offset_in_kept
            # Pre-split slice (may be empty if replay lands at the segment start).
            if split_src > current_src:
                slice_len = split_src - current_src
                entries.append(_PlaylistEntry(
                    kind="slice",
                    src_start=current_src, src_end=split_src,
                    final_start=accumulated_final,
                    final_end=accumulated_final + slice_len,
                ))
                accumulated_final += slice_len
            _append_replay_with_stingers(r)
            current_src = split_src
            replay_idx += 1

        # Trailing slice of this kept segment.
        if b > current_src:
            slice_len = b - current_src
            entries.append(_PlaylistEntry(
                kind="slice",
                src_start=current_src, src_end=b,
                final_start=accumulated_final,
                final_end=accumulated_final + slice_len,
            ))
            accumulated_final += slice_len

        accumulated_main = kept_end_main

    # Any replays whose insert point lands past every kept segment land
    # at the very end (insert_at_main was snapped to past-end by remap).
    while replay_idx < len(replays):
        _append_replay_with_stingers(replays[replay_idx])
        replay_idx += 1

    return entries


# ---------- stages ----------------------------------------------------------


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


# ---------- top-level orchestrator -----------------------------------------


@dataclass
class RenderPlan:
    project: ProjectData
    project_name: str
    include_intro: bool = True
    intro_style: str = "cinematic"   # "cinematic" | "text"
    output_name: Optional[str] = None
    state: RenderState = field(default_factory=lambda: RenderState(job_id=uuid.uuid4().hex[:12]))


@dataclass
class RenderContext:
    """Shared state passed between the per-stage helpers below.

    Built once by `_prepare_context` (probe + trim/event remap +
    weight table) and threaded through `_intro_stage`, `_main_stage`,
    `_outro_stage`, `_finalize`. Each stage may append to `parts` and
    bump `completed_weight`; `make_progress` reads both at callback
    time, so progress fractions stay correct as the pipeline advances.
    """
    plan: RenderPlan
    state: RenderState
    src: Path
    width: int
    height: int
    fps: float
    has_audio: bool
    kept: list[tuple[float, float]]
    remapped_events: list[ScoreFrame]
    job_dir: Path
    weight_lookup: dict[str, float]
    parts: list[Path] = field(default_factory=list)
    completed_weight: float = 0.0

    def make_progress(self, stage_name: str) -> Callable[[float, str], None]:
        """Return a (frac, msg) callback that updates the shared
        RenderState with this stage's contribution to overall progress."""
        weight = self.weight_lookup.get(stage_name, 0.0)
        state = self.state
        ctx = self

        def cb(frac: float, msg: str) -> None:
            if state.cancel_requested:
                return
            state.stage = stage_name
            state.message = msg
            state.progress = ctx.completed_weight + weight * max(0.0, min(1.0, frac))

        return cb

    def cancel_check(self) -> bool:
        """Cancel predicate threaded through every ffmpeg invocation in
        this render. When the user hits Cancel, `RenderState.cancel_requested`
        flips True and the next ffmpeg progress line trips this check, which
        terminates the process and raises FFmpegCancelled."""
        return self.state.cancel_requested

    def _bail_if_cancelled(self) -> None:
        """Quick check between stages so we don't start a new ffmpeg
        process after the user has already cancelled."""
        if self.state.cancel_requested:
            raise FFmpegCancelled()


def _resolve_source(plan: RenderPlan) -> Path:
    """Resolve the project's video_file to an existing absolute Path,
    accepting either a bare filename inside videos_dir or an absolute
    path picked via the native file picker."""
    vf = plan.project.info.video_file
    if not vf:
        raise FFmpegError("No source video selected in project")
    vf_path = Path(vf)
    src = vf_path if vf_path.is_absolute() else (config.videos_dir / vf)
    if not src.exists():
        raise FFmpegError(f"Source video not found: {src}")
    return src


def _prepare_context(plan: RenderPlan) -> RenderContext:
    """Probe the source, compute trim-derived state, allocate the temp
    directory and the per-stage weight table that drives progress
    reporting."""
    s = plan.state
    src = _resolve_source(plan)

    s.stage = "probe"
    s.message = f"probing {src.name}"
    probe = probe_video(src)
    width = probe["width"]
    height = probe["height"]
    fps = probe["fps"]
    duration = probe["duration"]
    has_audio = probe["has_audio"]

    kept = kept_segments_from_trims(duration, plan.project.trim_segments)
    if not kept:
        raise FFmpegError("All content was removed by trim segments")

    # Remap score events from source time → trimmed-main time.
    remapped_events: list[ScoreFrame] = []
    for ev in plan.project.score_events:
        t = remap_score_event_to_trimmed(ev.timestamp, kept)
        if t is None:
            continue
        remapped_events.append(
            ScoreFrame(
                timestamp=t,
                p1_score=ev.p1_score,
                p2_score=ev.p2_score,
                p1_set=ev.p1_set,
                p2_set=ev.p2_set,
            )
        )

    job_dir = config.temp_dir / s.job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Stage weights for the unified 0..1 progress fraction. Main +
    # concat always run; intro only contributes when its stage will
    # actually render.
    weights: list[tuple[str, float]] = []
    if plan.include_intro:
        weights.append(("intro", 0.05))
    weights.append(("main", 0.9))
    weights.append(("concat", 0.05))
    total = sum(w for _, w in weights)
    weight_lookup = {n: w / total for n, w in weights}

    return RenderContext(
        plan=plan, state=s, src=src,
        width=width, height=height, fps=fps,
        has_audio=has_audio,
        kept=kept,
        remapped_events=remapped_events,
        job_dir=job_dir,
        weight_lookup=weight_lookup,
    )


def all_intro_photos_present(
    is_doubles: bool,
    photos: dict[str, tuple[Optional[Path], bool]],
) -> bool:
    """Return True iff every avatar slot the intro needs has a resolved
    path. Singles needs `p1` + `p2`; doubles needs `p1`-`p4`. A missing
    slot (or one whose tuple's path is None) blocks the cinematic intro
    and forces fallback to the libass title card."""
    required = ("p1", "p2", "p3", "p4") if is_doubles else ("p1", "p2")
    return all(photos.get(slot, (None, False))[0] is not None for slot in required)


def _intro_stage(ctx: RenderContext) -> None:
    """Render the intro card. Cinematic when every player has an avatar
    on disk (or falls back to the shipped placeholder) and intro_style
    is not 'text'; otherwise the libass-only title card. Doubles needs
    all four photos to resolve before it can use the 4-avatar layout —
    when one is missing even after the default fallback, we drop back
    to the text intro instead of rendering a lopsided card."""
    plan = ctx.plan
    if not plan.include_intro:
        return
    ctx._bail_if_cancelled()

    intro_path = ctx.job_dir / "intro.mp4"
    style = (plan.intro_style or "cinematic").lower()
    use_cinematic = style != "text"
    info = plan.project.info
    is_doubles = (info.match_type or "single").lower() == "double"

    # Names on each scoreboard row — used for the libass label below
    # each avatar pair in doubles, and as plain p1/p2 names in singles.
    top_label, bot_label = resolve_row_names(
        info.match_type, info.p1, info.p2, info.p3, info.p4,
    )

    photos: dict[str, tuple[Optional[Path], bool]] = {}
    if use_cinematic:
        photos["p1"] = find_avatar_or_default(info.p1)
        photos["p2"] = find_avatar_or_default(info.p2)
        if is_doubles:
            photos["p3"] = find_avatar_or_default(info.p3)
            photos["p4"] = find_avatar_or_default(info.p4)

    have_all_photos = use_cinematic and all_intro_photos_present(is_doubles, photos)

    if use_cinematic and have_all_photos:
        render_cinematic_intro(
            out_path=intro_path,
            src=ctx.src,
            width=ctx.width, height=ctx.height, fps=ctx.fps,
            tournament=info.tournament,
            p1_name=top_label, p1_avatar=photos["p1"][0],
            p2_name=bot_label, p2_avatar=photos["p2"][0],
            p1_team=info.p1_team,
            p2_team=info.p2_team,
            match_type=info.match_type,
            p3_avatar=photos.get("p3", (None, False))[0],
            p4_avatar=photos.get("p4", (None, False))[0],
            on_progress=ctx.make_progress("intro"),
            cancel_check=ctx.cancel_check,
        )
        # Build the "used default placeholder for …" message from
        # whichever slots fell back to the shipped silhouette.
        missing: list[str] = []
        for slot, raw_name in (
            ("p1", info.p1), ("p2", info.p2),
            ("p3", info.p3), ("p4", info.p4),
        ):
            entry = photos.get(slot)
            if entry and entry[1] and raw_name:
                missing.append(raw_name)
        if missing:
            ctx.state.message = (
                f"Cinematic intro used the default placeholder for "
                f"{', '.join(missing)} — drop a real photo into "
                f"assets/avatars/ when you have one."
            )
    else:
        # User picked text intro, OR cinematic was requested but at
        # least one required photo (and the default fallback) is
        # missing. The text card uses the row labels so doubles still
        # shows the combined pair names.
        render_intro(
            out_path=intro_path,
            width=ctx.width, height=ctx.height, fps=ctx.fps,
            tournament=info.tournament,
            p1=top_label,
            p2=bot_label,
            on_progress=ctx.make_progress("intro"),
            cancel_check=ctx.cancel_check,
        )
    ctx.parts.append(intro_path)
    ctx.completed_weight += ctx.weight_lookup["intro"]


def _main_stage(ctx: RenderContext) -> None:
    """Render the main match (trims removed) with the scoreboard burned
    in and slow-mo replays of every highlight spliced in right after the
    highlight's real-time occurrence. Each replay is optionally bracketed
    by a branded stinger transition (sting-in before, sting-out after).
    SLOW MOTION badge pulses on top-left during each replay so the
    viewer reads the speed change instantly."""
    plan = ctx.plan
    ctx._bail_if_cancelled()

    use_replays = bool(plan.project.highlights)
    replays = build_replay_plan(plan.project.highlights, ctx.kept) if use_replays else []

    # Stinger pair: build / fetch from cache when replays will be spliced
    # AND the operator hasn't disabled it in config. IN and OUT have
    # ASYMMETRIC durations — long readable hold on the way in, quick
    # wipe back to live on the way out. Cache is manifest-driven: as
    # long as brand colour / logo / channel name / sounds / spec all
    # match the previous render, the cached mp4s are reused untouched
    # (99 % of renders pay zero stinger overhead).
    stinger_in: Optional[Path] = None
    stinger_out: Optional[Path] = None
    stinger_in_dur = 0.0
    stinger_out_dur = 0.0
    if replays and config.stinger_enabled:
        stinger_in_dur = config.stinger_duration_seconds
        stinger_out_dur = config.stinger_out_duration_seconds

        def _extract_stinger_bg() -> Optional[Path]:
            # Pull a frame ~40 % into the source for the blurred bg.
            # Past the warm-up but well before the end — likely to
            # land on an actual rally rather than empty table at
            # either end. Lazy: only invoked on cache miss. Failure
            # falls through silently to the lavfi-colour fallback
            # inside the builder.
            bg_png = ctx.job_dir / "stinger_bg.png"
            try:
                src_dur_probe = probe_video(ctx.src).get("duration", 0.0)
                bg_t = max(0.0, float(src_dur_probe) * 0.4)
                extract_frame_at(ctx.src, bg_t, bg_png)
                return bg_png if bg_png.exists() else None
            except FFmpegError:
                return None

        stinger_in, stinger_out = get_or_build_stinger_pair(
            width=ctx.width, height=ctx.height, fps=ctx.fps,
            in_duration=stinger_in_dur,
            out_duration=stinger_out_dur,
            brand_color=config.brand_color,
            logo_path=find_brand_logo(),
            sound_path=config.stinger_sound_path,
            bg_frame_provider=_extract_stinger_bg,
            channel_name=config.channel_name,
            replay_label=config.stinger_replay_label,
        )

    have_stinger = bool(stinger_in and stinger_out)
    playlist = build_main_playlist(
        ctx.kept, replays,
        stinger_in_path=stinger_in,
        stinger_out_path=stinger_out,
        stinger_in_duration=stinger_in_dur if have_stinger else 0.0,
        stinger_out_duration=stinger_out_dur if have_stinger else 0.0,
    )
    if not playlist:
        raise FFmpegError("No content kept after trim segments")

    # Total final-render duration after replays + stingers splice in.
    # The scoreboard has to span this so libass doesn't expire the panel
    # before the last entry finishes.
    final_duration = playlist[-1].final_end

    # Two-step event remap: trim already happened in _prepare_context,
    # now shift each event by the replay + stinger (IN + OUT) durations
    # of every replay that precedes it.
    final_events = remap_events_with_replays(
        ctx.remapped_events, replays,
        stinger_total_duration=(stinger_in_dur + stinger_out_dur) if have_stinger else 0.0,
    )

    ass_path = ctx.job_dir / "scoreboard.ass"
    build_scoreboard_ass(
        output_path=ass_path,
        video_w=ctx.width, video_h=ctx.height,
        total_duration=final_duration,
        tournament=plan.project.info.tournament,
        p1_name=plan.project.info.p1,
        p2_name=plan.project.info.p2,
        p1_team=plan.project.info.p1_team,
        p2_team=plan.project.info.p2_team,
        match_type=plan.project.info.match_type,
        p3_name=plan.project.info.p3,
        p4_name=plan.project.info.p4,
        score_events=final_events,
        best_of=plan.project.info.best_of,
    )
    # SLOW MOTION badge: one Dialogue range per spliced-in replay, in
    # final-render coords. Skipped when no replays were spliced — saves
    # a no-op ass= filter from the chain.
    sm_badge_path: Optional[Path] = None
    if replays:
        sm_badge_path = ctx.job_dir / "slow_motion_badge.ass"
        build_slow_motion_badge_ass(
            output_path=sm_badge_path,
            video_w=ctx.width, video_h=ctx.height,
            show_ranges=[
                (e.final_start, e.final_end) for e in playlist if e.kind == "replay"
            ],
        )

    main_path = ctx.job_dir / "main.mp4"
    render_main_with_scoreboard(
        src=ctx.src,
        out_path=main_path,
        ass_path=ass_path,
        slow_motion_badge_ass=sm_badge_path,
        playlist=playlist,
        width=ctx.width, height=ctx.height, fps=ctx.fps,
        has_audio=ctx.has_audio,
        on_progress=ctx.make_progress("main"),
        cancel_check=ctx.cancel_check,
        replay_sound_path=config.replay_sound_path,
        replay_sound_volume=config.replay_sound_volume,
    )
    ctx.parts.append(main_path)
    ctx.completed_weight += ctx.weight_lookup["main"]


def _outro_stage(ctx: RenderContext) -> None:
    """Render the cinematic outro card that closes the final cut.

    Skipped only when the operator has disabled the outro in config.
    The configured `outro_bg_path` acts as a fallback when frame
    extraction from main.mp4 fails for any reason; failing that, a
    lavfi solid colour. Errors here never abort the render — the rest
    of the cut is already on disk in `ctx.parts`.
    """
    if not config.outro_enabled:
        return
    ctx._bail_if_cancelled()

    main_path = ctx.job_dir / "main.mp4"
    bg_png = ctx.job_dir / "outro_bg.png"
    bg_path: Optional[Path] = None

    if main_path.exists():
        try:
            probe = probe_video(main_path)
            main_dur = float(probe.get("duration", 0.0))
            if main_dur > 0.1:
                # Pull a frame just shy of EOF so we land on real
                # content, not the trailing nothing that some encoders
                # leave at the very last timestamp.
                extract_frame_at(main_path, max(0.0, main_dur - 0.1), bg_png)
                if bg_png.exists():
                    bg_path = bg_png
        except FFmpegError:
            bg_path = None

    # Operator-supplied fallback / override. Useful when main render
    # failed the frame extraction OR the operator wants a fixed shot
    # (tournament logo, sponsor card) instead of the freeze-frame.
    if bg_path is None:
        bg_path = config.outro_bg_path

    out_path = ctx.job_dir / "outro.mp4"
    render_outro_card(
        out_path=out_path,
        width=ctx.width, height=ctx.height, fps=ctx.fps,
        duration=config.outro_duration_seconds,
        text=config.outro_text,
        bg_path=bg_path,
        sound_path=config.outro_sound_path,
        sound_volume=config.outro_sound_volume,
        on_progress=lambda f, m: None,  # short clip, no progress reporting
        cancel_check=ctx.cancel_check,
    )
    ctx.parts.append(out_path)


def _finalize(ctx: RenderContext) -> None:
    """Concat all rendered parts into the final output mp4, mark the
    render done, and drop the per-job temp directory."""
    plan = ctx.plan
    s = ctx.state
    if not ctx.parts:
        raise FFmpegError("No stages selected for render")

    out_name = plan.output_name or f"{plan.project_name}_{int(time.time())}.mp4"
    if not out_name.lower().endswith(".mp4"):
        out_name += ".mp4"
    final_path = config.output_dir / out_name

    concat_parts(
        ctx.parts, final_path,
        on_progress=ctx.make_progress("concat"),
        cancel_check=ctx.cancel_check,
    )
    ctx.completed_weight += ctx.weight_lookup["concat"]

    s.status = "done"
    s.progress = 1.0
    s.stage = "done"
    s.message = "Render complete"
    s.output_path = str(final_path)

    # Auto-export a labelled-ground-truth sidecar + reference frame for
    # the auto-trim CV pipeline. Best-effort: failures append to
    # s.message but don't fail the render.
    from .groundtruth import export_groundtruth
    export_groundtruth(ctx, final_path)

    # Mirror everything (source + output + sidecars + project snapshot)
    # into `dataset/<slug>/` so the manual workflow doubles as dataset
    # accumulation. Reads the sidecar files written above so this MUST
    # run after export_groundtruth. Same best-effort policy.
    from .dataset import archive_to_dataset
    archive_to_dataset(ctx, final_path)

    # Drop the per-job temp dir now that the final mp4 is safely in
    # output/. We only do this on success — on error we keep the
    # intermediate .ass / .mp4 / .concat.txt files so the operator
    # (or a developer) can inspect what ffmpeg was actually fed.
    try:
        shutil.rmtree(ctx.job_dir)
    except OSError:
        pass  # file still locked (antivirus / open in player) — leave it


def run_render(plan: RenderPlan) -> None:
    """Synchronous render pipeline. The caller (server) runs it in a thread."""
    s = plan.state
    s.status = "running"
    s.started_at = time.time()
    try:
        ctx = _prepare_context(plan)
        _intro_stage(ctx)
        _main_stage(ctx)
        _outro_stage(ctx)
        _finalize(ctx)
    except FFmpegCancelled:
        # User pulled the plug — flag distinctly so the UI can show
        # "cancelled" instead of a red error banner. Leave temp/<job_id>
        # in place: same policy as errors, so the partial intermediates
        # are still inspectable.
        s.status = "cancelled"
        s.message = "Render cancelled by user"
        s.error = ""
    except FFmpegError as e:
        s.status = "error"
        s.error = str(e)
        s.message = e.stderr[-2000:] if e.stderr else str(e)
    except Exception as e:  # last-ditch
        s.status = "error"
        s.error = f"{type(e).__name__}: {e}"
        s.message = s.error
    finally:
        s.finished_at = time.time()
