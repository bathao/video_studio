"""
Render orchestrator.

Three stages, each produces an MP4 in temp/<job_id>/, then we concat them
with the concat demuxer (no re-encode) into the final output.

Stage layout:

  intro.mp4       — 3 second title card built with lavfi color + drawtext
  highlight.mp4   — concat of all highlight clips, with optional slow-mo on
                    the last 2.5 seconds of each
  main.mp4        — source video minus trim_segments, with the scoreboard
                    burned in via libass. Every highlight also gets a
                    50%-speed replay spliced in right after its real-time
                    occurrence in main, with a pulsing SLOW MOTION badge
                    on the top-left for the duration of each replay.

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
    build_full_match_badge_ass,
    build_highlight_badge_ass,
    build_intro_ass,
    build_scoreboard_ass,
    build_slow_motion_badge_ass,
    build_transition_ass,
)
from .ass.scoreboard import resolve_row_names
from .avatars import find_avatar_or_default
from .config import config
from .ffmpeg_runner import (
    FFmpegCancelled,
    FFmpegError,
    TARGET_AUDIO_CHANNELS,
    TARGET_AUDIO_RATE,
    aac_args,
    escape_ffmpeg_filter_path,
    hwaccel_input_args,
    nvenc_args,
    probe_video,
    run_ffmpeg_with_progress,
)
from .intro_builder import render_cinematic_intro
from .models import Highlight, ProjectData, TrimSegment

SLOWMO_TAIL_SECONDS = 2.5  # length of the slow-motion tail per highlight


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
# tennis rally and to align with the highlight-reel tail-slow-mo idiom.
REPLAY_SPEED = 0.5


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
) -> list[ScoreFrame]:
    """Shift each score event by the cumulative replay duration of
    replays that occur strictly before the event. Events that fire AT
    a replay's insert point stay put so the post-highlight score is
    visible during the replay too."""
    if not replays:
        return events
    out: list[ScoreFrame] = []
    for ev in events:
        shift = 0.0
        for r in replays:
            if r.insert_at_main < ev.timestamp:
                shift += r.replay_duration
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
    """One ffmpeg input slot for the main render. `kind` discriminates
    between a normal-speed source slice and a slow-mo replay (which
    needs setpts*2 + atempo=0.5). `final_start` / `final_end` are the
    entry's position in the final main timeline — used to time the
    SLOW MOTION badge during replay entries."""
    kind: str   # "slice" | "replay"
    src_start: float
    src_end: float
    final_start: float
    final_end: float


def build_main_playlist(
    kept: list[tuple[float, float]],
    replays: list[ReplayInsert],
) -> list[_PlaylistEntry]:
    """Walk the kept segments and splice each replay in at its insert
    point. Kept segments that contain insert points get split into
    sub-slices; replays drop in between. Returns a flat chronological
    list ready to map to ffmpeg inputs.

    When a replay's insert point lands exactly at a kept-segment
    boundary, the splice falls between two existing slices — no extra
    split is generated."""
    entries: list[_PlaylistEntry] = []
    accumulated_main = 0.0          # trimmed-main offset at start of current kept
    accumulated_final = 0.0         # final-timeline cursor (incl. replay durations)
    replay_idx = 0

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
            # Replay
            r_len = r.replay_duration
            entries.append(_PlaylistEntry(
                kind="replay",
                src_start=r.src_start, src_end=r.src_end,
                final_start=accumulated_final,
                final_end=accumulated_final + r_len,
            ))
            accumulated_final += r_len
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
        r = replays[replay_idx]
        r_len = r.replay_duration
        entries.append(_PlaylistEntry(
            kind="replay",
            src_start=r.src_start, src_end=r.src_end,
            final_start=accumulated_final,
            final_end=accumulated_final + r_len,
        ))
        accumulated_final += r_len
        replay_idx += 1

    return entries


# ---------- stages ----------------------------------------------------------


def render_transition(
    *,
    out_path: Path,
    width: int,
    height: int,
    fps: float,
    on_progress: Callable[[float, str], None],
    cancel_check: Optional[Callable[[], bool]] = None,
) -> float:
    """
    Render a 0.8 s bridge clip with a gold sweep line, used between
    the highlight reel and the main match so the boundary doesn't feel
    like a hard cut. Returns the clip duration.
    """
    duration = 0.8

    ass_path = out_path.with_suffix(".transition.ass")
    build_transition_ass(
        output_path=ass_path,
        video_w=width,
        video_h=height,
        duration=duration,
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
        log_prefix="transition: ",
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


def _render_one_highlight(
    *,
    src: Path,
    out_path: Path,
    h: Highlight,
    width: int,
    height: int,
    fps: float,
    has_audio: bool,
    badge_ass: Optional[Path],
    on_progress: Callable[[float, str], None],
    cancel_check: Optional[Callable[[], bool]] = None,
) -> float:
    """
    Render a single highlight clip with input-side seeking (`-ss BEFORE -i`),
    so ffmpeg only demuxes the few seconds of source we actually need —
    crucial when the source is 10+ GB.

    Returns the output duration (which is longer than the source range
    when slow-motion is applied to the tail).
    """
    duration = h.end - h.start
    apply_slowmo = h.slow_mo and duration > SLOWMO_TAIL_SECONDS + 0.2

    # We'll emit the concat output to [vc] (or [vout] when there's no
    # badge), then optionally chain an `ass=` burn for the HIGHLIGHT
    # badge. The final video label is always [vout].
    vc_label = "vc" if badge_ass else "vout"

    if apply_slowmo:
        head_dur = duration - SLOWMO_TAIL_SECONDS
        v_filter_parts = [
            f"[0:v]trim=duration={head_dur:.3f},setpts=PTS-STARTPTS,"
            f"scale={width}:{height},fps={fps}[vh]",
            f"[0:v]trim=start={head_dur:.3f}:duration={SLOWMO_TAIL_SECONDS},"
            f"setpts=2.0*(PTS-STARTPTS),scale={width}:{height},fps={fps}[vt]",
        ]
        if has_audio:
            a_filter_parts = [
                f"[0:a]atrim=duration={head_dur:.3f},asetpts=PTS-STARTPTS,"
                f"aformat=sample_rates={TARGET_AUDIO_RATE}:channel_layouts=stereo[ah]",
                f"[0:a]atrim=start={head_dur:.3f}:duration={SLOWMO_TAIL_SECONDS},"
                f"asetpts=PTS-STARTPTS,atempo=0.5,"
                f"aformat=sample_rates={TARGET_AUDIO_RATE}:channel_layouts=stereo[at]",
            ]
            concat = f"[vh][ah][vt][at]concat=n=2:v=1:a=1[{vc_label}][aout]"
        else:
            a_filter_parts = []
            concat = f"[vh][vt]concat=n=2:v=1:a=0[{vc_label}]"
        filter_complex = ";".join(v_filter_parts + a_filter_parts + [concat])
        out_duration = head_dur + (SLOWMO_TAIL_SECONDS * 2.0)
    else:
        v_filter = (
            f"[0:v]setpts=PTS-STARTPTS,scale={width}:{height},fps={fps}[{vc_label}]"
        )
        if has_audio:
            a_filter = (
                f"[0:a]asetpts=PTS-STARTPTS,"
                f"aformat=sample_rates={TARGET_AUDIO_RATE}:channel_layouts=stereo[aout]"
            )
            filter_complex = ";".join([v_filter, a_filter])
        else:
            filter_complex = v_filter
        out_duration = duration

    # Burn the HIGHLIGHT badge on top of the concat output.
    if badge_ass:
        badge_arg = escape_ffmpeg_filter_path(badge_ass)
        filter_complex += f";[vc]ass='{badge_arg}'[vout]"

    # Both `-ss` and `-t` placed BEFORE `-i` are input-side. `-ss` is a
    # fast keyframe seek; `-t` limits how much of the source we demux.
    # (Putting `-t` after `-i` would be an OUTPUT-duration limit, which
    # would silently truncate slow-mo highlights since slow-mo expands
    # output PTS beyond the source range.)
    args = [
        *hwaccel_input_args(),
        "-ss", f"{h.start:.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(src),
    ]
    if not has_audio:
        # Add silent audio as input #1 BEFORE any output options.
        args += ["-f", "lavfi", "-i", f"anullsrc=r={TARGET_AUDIO_RATE}:cl=stereo"]

    args += ["-filter_complex", filter_complex, "-map", "[vout]"]
    if has_audio:
        args += ["-map", "[aout]", *nvenc_args(), *aac_args()]
    else:
        args += ["-map", "1:a", *nvenc_args(), *aac_args(), "-shortest"]
    args += [str(out_path)]

    run_ffmpeg_with_progress(
        args,
        expected_out_seconds=out_duration,
        on_progress=on_progress,
        cancel_check=cancel_check,
    )
    return out_duration


def render_highlight_clip(
    *,
    src: Path,
    out_path: Path,
    job_dir: Path,
    highlights: list[Highlight],
    width: int,
    height: int,
    fps: float,
    has_audio: bool,
    on_progress: Callable[[float, str], None],
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Optional[float]:
    """
    Render each highlight as its own MP4 (with input seeking) then stitch
    them with the concat demuxer (no re-encode). One ffmpeg per highlight
    keeps NVDEC session count low and avoids walking the whole source.
    """
    valid = [h for h in highlights if h.end > h.start]
    if not valid:
        return None

    # Build one HIGHLIGHT badge .ass and reuse it for every clip. Each
    # clip restarts the .ass timeline at 0, so the same file works for
    # any clip duration (we just need the .ass to outlast the longest).
    longest = max((h.end - h.start) for h in valid)
    badge_dur = longest * 2.0 + 5.0  # generous slack for slow-mo expansion
    badge_path = job_dir / "highlight_badge.ass"
    build_highlight_badge_ass(
        output_path=badge_path,
        video_w=width,
        video_h=height,
        duration=badge_dur,
    )

    parts: list[Path] = []
    total = 0.0
    n = len(valid)
    for i, h in enumerate(valid):
        part_path = job_dir / f"hl_{i:03d}.mp4"

        def make_cb(idx: int) -> Callable[[float, str], None]:
            def cb(frac: float, msg: str) -> None:
                on_progress((idx + frac) / n, f"highlight {idx + 1}/{n}: {msg}")
            return cb

        out_dur = _render_one_highlight(
            src=src,
            out_path=part_path,
            h=h,
            width=width,
            height=height,
            fps=fps,
            has_audio=has_audio,
            badge_ass=badge_path,
            on_progress=make_cb(i),
            cancel_check=cancel_check,
        )
        parts.append(part_path)
        total += out_dur

    # Stitch into one highlight reel via concat demuxer (stream copy).
    concat_parts(
        parts, out_path,
        on_progress=lambda f, m: on_progress(0.999, f"highlight stitch: {m}"),
        cancel_check=cancel_check,
    )
    return total


def render_main_with_scoreboard(
    *,
    src: Path,
    out_path: Path,
    ass_path: Path,
    full_match_badge_ass: Optional[Path],
    slow_motion_badge_ass: Optional[Path],
    playlist: list[_PlaylistEntry],
    width: int,
    height: int,
    fps: float,
    has_audio: bool,
    on_progress: Callable[[float, str], None],
    cancel_check: Optional[Callable[[], bool]] = None,
) -> float:
    """
    Render the main match: open the source once per playlist entry with
    input-side seeking (`-ss BEFORE -i`), apply slow-mo (setpts*2,
    atempo=0.5) to replay entries, concat everything in chronological
    order, then burn scoreboard + FULL MATCH badge + SLOW MOTION badge
    — all in one NVDEC → CPU filter → NVENC pipeline.

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
        slice_len = entry.src_end - entry.src_start
        args += [
            *hwaccel_input_args(),
            "-ss", f"{entry.src_start:.3f}",
            "-t", f"{slice_len:.3f}",
            "-i", str(src),
        ]
    n = len(playlist)
    expected_total = sum(e.final_end - e.final_start for e in playlist)

    # If the source has no audio, we still need an audio stream in the
    # output (so the final concat-demuxer doesn't fail on stream mismatch).
    # Add silent audio as input #n, BEFORE any output options.
    silent_idx: Optional[int] = None
    if not has_audio:
        silent_idx = n
        args += ["-f", "lavfi", "-i", f"anullsrc=r={TARGET_AUDIO_RATE}:cl=stereo"]

    # Build the per-entry processing chains. Replay entries stretch
    # video PTS to 2× and halve audio tempo so they play at half speed.
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
                a_parts.append(
                    f"[{i}:a]asetpts=PTS-STARTPTS,atempo={REPLAY_SPEED},"
                    f"aformat=sample_rates={TARGET_AUDIO_RATE}:channel_layouts=stereo[ak{i}]"
                )
        else:
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

    # Chain overlay burns: scoreboard → FULL MATCH badge → SLOW MOTION
    # badge. Each `ass=` filter runs over the previous output, so the
    # composition order matches the visual stacking we want. Optional
    # overlays just skip their link in the chain.
    ass_arg = escape_ffmpeg_filter_path(ass_path)
    burn_chain = [f"[vc]ass='{ass_arg}'[vb0]"]
    cur_label = "vb0"
    if full_match_badge_ass is not None:
        next_label = "vb1"
        fm_arg = escape_ffmpeg_filter_path(full_match_badge_ass)
        burn_chain.append(f"[{cur_label}]ass='{fm_arg}'[{next_label}]")
        cur_label = next_label
    if slow_motion_badge_ass is not None:
        next_label = "vb2"
        sm_arg = escape_ffmpeg_filter_path(slow_motion_badge_ass)
        burn_chain.append(f"[{cur_label}]ass='{sm_arg}'[{next_label}]")
        cur_label = next_label
    # Final rename so the map below is stable regardless of which
    # optional badges were chained.
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
    include_highlights: bool = True
    include_main: bool = True
    output_name: Optional[str] = None
    state: RenderState = field(default_factory=lambda: RenderState(job_id=uuid.uuid4().hex[:12]))


@dataclass
class RenderContext:
    """Shared state passed between the per-stage helpers below.

    Built once by `_prepare_context` (probe + trim/event remap +
    weight table) and threaded through `_intro_stage`, `_highlight_stage`,
    `_bridge_stage`, `_main_stage`, `_finalize`. Each stage may append to
    `parts` and bump `completed_weight`; `make_progress` reads both at
    callback time, so progress fractions stay correct as the pipeline
    advances.
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
    if not kept and plan.include_main:
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

    # Stage weights for the unified 0..1 progress fraction. Concat is
    # always present; intro/highlight/main only contribute when their
    # stages will actually run.
    weights: list[tuple[str, float]] = []
    if plan.include_intro:
        weights.append(("intro", 0.05))
    if plan.include_highlights and plan.project.highlights:
        weights.append(("highlight", 0.25))
    if plan.include_main:
        weights.append(("main", 0.65))
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

    required_slots = ("p1", "p2", "p3", "p4") if is_doubles else ("p1", "p2")
    have_all_photos = use_cinematic and all(
        photos.get(slot, (None, False))[0] is not None for slot in required_slots
    )

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


def _highlight_stage(ctx: RenderContext) -> bool:
    """Render the highlight reel. Returns True iff a highlight clip was
    actually appended to `ctx.parts` (so the bridge stage knows whether
    to insert a transition)."""
    plan = ctx.plan
    if not (plan.include_highlights and plan.project.highlights):
        return False
    ctx._bail_if_cancelled()

    hi_path = ctx.job_dir / "highlight.mp4"
    written = render_highlight_clip(
        src=ctx.src,
        out_path=hi_path,
        job_dir=ctx.job_dir,
        highlights=plan.project.highlights,
        width=ctx.width, height=ctx.height, fps=ctx.fps,
        has_audio=ctx.has_audio,
        on_progress=ctx.make_progress("highlight"),
        cancel_check=ctx.cancel_check,
    )
    if written:
        ctx.parts.append(hi_path)
    ctx.completed_weight += ctx.weight_lookup["highlight"]
    return bool(written)


def _bridge_stage(ctx: RenderContext, highlight_appended: bool) -> None:
    """Render the gold-sweep bridge between highlight reel and main
    match — only when both segments are present in the final output."""
    plan = ctx.plan
    wants_bridge = (
        plan.include_highlights
        and plan.project.highlights
        and plan.include_main
        and highlight_appended
    )
    if not wants_bridge:
        return
    ctx._bail_if_cancelled()
    tr_path = ctx.job_dir / "transition.mp4"
    render_transition(
        out_path=tr_path,
        width=ctx.width, height=ctx.height, fps=ctx.fps,
        on_progress=lambda f, m: None,  # quick clip, no progress reporting
        cancel_check=ctx.cancel_check,
    )
    ctx.parts.append(tr_path)


def _main_stage(ctx: RenderContext) -> None:
    """Render the main match (trims removed) with the scoreboard burned
    in, the FULL MATCH badge over the first ~15 s, and slow-mo replays
    of every highlight spliced in right after the highlight's real-time
    occurrence. SLOW MOTION badge pulses on top-left during each
    replay so the viewer reads the speed change instantly."""
    plan = ctx.plan
    if not plan.include_main:
        return
    ctx._bail_if_cancelled()

    # Replays only when the operator is also producing a highlight reel
    # — same checkbox controls both behaviours, keeps the UX consistent.
    use_replays = bool(plan.include_highlights and plan.project.highlights)
    replays = build_replay_plan(plan.project.highlights, ctx.kept) if use_replays else []
    playlist = build_main_playlist(ctx.kept, replays)
    if not playlist:
        raise FFmpegError("No content kept after trim segments")

    # Total final-render duration after replays splice in. The scoreboard
    # has to span this so libass doesn't expire the panel before the
    # last slow-mo finishes.
    final_duration = playlist[-1].final_end

    # Two-step event remap: trim already happened in _prepare_context,
    # now shift each event by the replay durations that precede it.
    final_events = remap_events_with_replays(ctx.remapped_events, replays)

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
    # FULL MATCH badge: shown for the first 15 seconds of the main
    # render so the viewer knows the highlight reel is over.
    fm_badge_path = ctx.job_dir / "full_match_badge.ass"
    build_full_match_badge_ass(
        output_path=fm_badge_path,
        video_w=ctx.width, video_h=ctx.height,
        show_seconds=15.0,
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
        full_match_badge_ass=fm_badge_path,
        slow_motion_badge_ass=sm_badge_path,
        playlist=playlist,
        width=ctx.width, height=ctx.height, fps=ctx.fps,
        has_audio=ctx.has_audio,
        on_progress=ctx.make_progress("main"),
        cancel_check=ctx.cancel_check,
    )
    ctx.parts.append(main_path)
    ctx.completed_weight += ctx.weight_lookup["main"]


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
        highlight_appended = _highlight_stage(ctx)
        _bridge_stage(ctx, highlight_appended)
        _main_stage(ctx)
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
