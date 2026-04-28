"""
Render orchestrator.

Three stages, each produces an MP4 in temp/<job_id>/, then we concat them
with the concat demuxer (no re-encode) into the final output.

Stage layout:

  intro.mp4       — 3 second title card built with lavfi color + drawtext
  highlight.mp4   — concat of all highlight clips, with optional slow-mo on
                    the last 2.5 seconds of each
  main.mp4        — source video minus trim_segments, with the scoreboard
                    burned in via libass

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

from .ass_builder import (
    ScoreFrame,
    build_full_match_badge_ass,
    build_highlight_badge_ass,
    build_intro_ass,
    build_scoreboard_ass,
)
from .config import config
from .ffmpeg_runner import (
    FFmpegError,
    escape_ffmpeg_filter_path,
    probe_video,
    run_ffmpeg_with_progress,
)
from .models import Highlight, ProjectData, ScoreEvent, TrimSegment

SLOWMO_TAIL_SECONDS = 2.5  # length of the slow-motion tail per highlight
TARGET_AUDIO_RATE = 48000
TARGET_AUDIO_CHANNELS = 2


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


# ---------- stages ----------------------------------------------------------


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
    return ["-c:a", "aac", "-ar", str(TARGET_AUDIO_RATE), "-ac", str(TARGET_AUDIO_CHANNELS), "-b:a", "192k"]


def _hwaccel_input_args() -> list[str]:
    if config.use_hwaccel:
        return ["-hwaccel", "cuda"]
    return []


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
        *_nvenc_args(),
        *_aac_args(),
        "-shortest",
        str(out_path),
    ]
    run_ffmpeg_with_progress(
        args,
        expected_out_seconds=duration,
        on_progress=on_progress,
        log_prefix="intro: ",
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
        *_hwaccel_input_args(),
        "-ss", f"{h.start:.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(src),
    ]
    if not has_audio:
        # Add silent audio as input #1 BEFORE any output options.
        args += ["-f", "lavfi", "-i", f"anullsrc=r={TARGET_AUDIO_RATE}:cl=stereo"]

    args += ["-filter_complex", filter_complex, "-map", "[vout]"]
    if has_audio:
        args += ["-map", "[aout]", *_nvenc_args(), *_aac_args()]
    else:
        args += ["-map", "1:a", *_nvenc_args(), *_aac_args(), "-shortest"]
    args += [str(out_path)]

    run_ffmpeg_with_progress(
        args,
        expected_out_seconds=out_duration,
        on_progress=on_progress,
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
        )
        parts.append(part_path)
        total += out_dur

    # Stitch into one highlight reel via concat demuxer (stream copy).
    concat_parts(parts, out_path, on_progress=lambda f, m: on_progress(0.999, f"highlight stitch: {m}"))
    return total


def render_main_with_scoreboard(
    *,
    src: Path,
    out_path: Path,
    ass_path: Path,
    full_match_badge_ass: Optional[Path],
    kept: list[tuple[float, float]],
    width: int,
    height: int,
    fps: float,
    has_audio: bool,
    on_progress: Callable[[float, str], None],
) -> float:
    """
    Open the source once per kept segment with input-side seeking
    (`-ss BEFORE -i`), so ffmpeg jumps straight to each kept range
    instead of demuxing the entire 10+ GB file. The kept segments are
    then concatenated and the scoreboard .ass is burned in — all in one
    NVDEC → CPU filter → NVENC pipeline.
    """
    if not kept:
        raise FFmpegError("No content kept after trim segments")

    args: list[str] = []
    for a, b in kept:
        args += [
            *_hwaccel_input_args(),
            "-ss", f"{a:.3f}",
            "-t", f"{(b - a):.3f}",
            "-i", str(src),
        ]
    n = len(kept)
    expected_total = sum(b - a for a, b in kept)

    # If the source has no audio, we still need an audio stream in the
    # output (so the final concat-demuxer doesn't fail on stream mismatch).
    # Add silent audio as input #n, BEFORE any output options.
    silent_idx: Optional[int] = None
    if not has_audio:
        silent_idx = n
        args += ["-f", "lavfi", "-i", f"anullsrc=r={TARGET_AUDIO_RATE}:cl=stereo"]

    # Build the concat + ass-burn filter graph.
    v_parts: list[str] = []
    a_parts: list[str] = []
    concat_inputs: list[str] = []
    for i in range(n):
        v_parts.append(
            f"[{i}:v]setpts=PTS-STARTPTS,scale={width}:{height},fps={fps}[vk{i}]"
        )
        if has_audio:
            a_parts.append(
                f"[{i}:a]asetpts=PTS-STARTPTS,"
                f"aformat=sample_rates={TARGET_AUDIO_RATE}:channel_layouts=stereo[ak{i}]"
            )
            concat_inputs.append(f"[vk{i}][ak{i}]")
        else:
            concat_inputs.append(f"[vk{i}]")

    if has_audio:
        concat_filter = "".join(concat_inputs) + f"concat=n={n}:v=1:a=1[vc][aout]"
    else:
        concat_filter = "".join(concat_inputs) + f"concat=n={n}:v=1:a=0[vc]"

    # Burn the scoreboard, then optionally chain the FULL MATCH badge as
    # a second `ass=` filter so both overlays composite onto the same
    # output stream.
    ass_arg = escape_ffmpeg_filter_path(ass_path)
    if full_match_badge_ass:
        badge_arg = escape_ffmpeg_filter_path(full_match_badge_ass)
        burn_filter = f"[vc]ass='{ass_arg}'[vbb];[vbb]ass='{badge_arg}'[vout]"
    else:
        burn_filter = f"[vc]ass='{ass_arg}'[vout]"
    filter_complex = ";".join(v_parts + a_parts + [concat_filter, burn_filter])

    args += [
        "-filter_complex", filter_complex,
        "-map", "[vout]",
    ]
    if has_audio:
        args += ["-map", "[aout]", *_nvenc_args(), *_aac_args()]
    else:
        args += ["-map", f"{silent_idx}:a", *_nvenc_args(), *_aac_args(), "-shortest"]
    args += [str(out_path)]

    run_ffmpeg_with_progress(
        args,
        expected_out_seconds=expected_total,
        on_progress=on_progress,
        log_prefix="main: ",
    )
    return expected_total


def concat_parts(parts: list[Path], out_path: Path, on_progress: Callable[[float, str], None]) -> None:
    if not parts:
        raise FFmpegError("Nothing to concat")
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
    include_highlights: bool = True
    include_main: bool = True
    output_name: Optional[str] = None
    state: RenderState = field(default_factory=lambda: RenderState(job_id=uuid.uuid4().hex[:12]))


def run_render(plan: RenderPlan) -> None:
    """Synchronous render pipeline. The caller (server) runs it in a thread."""
    s = plan.state
    s.status = "running"
    s.started_at = time.time()

    try:
        if not plan.project.info.video_file:
            raise FFmpegError("No source video selected in project")

        src = config.videos_dir / plan.project.info.video_file
        if not src.exists():
            raise FFmpegError(f"Source video not found: {src}")

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

        # Per-job temp directory.
        job_dir = config.temp_dir / s.job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        # Plan stage weights (for combined progress).
        weights: list[tuple[str, float]] = []
        if plan.include_intro:
            weights.append(("intro", 0.05))
        if plan.include_highlights and plan.project.highlights:
            weights.append(("highlight", 0.25))
        if plan.include_main:
            weights.append(("main", 0.65))
        weights.append(("concat", 0.05))
        total_weight = sum(w for _, w in weights)
        weights = [(n, w / total_weight) for n, w in weights]

        completed_weight = 0.0

        def make_progress(stage_name: str, stage_weight: float) -> Callable[[float, str], None]:
            def cb(frac: float, msg: str) -> None:
                if s.cancel_requested:
                    return
                s.stage = stage_name
                s.message = msg
                s.progress = completed_weight + stage_weight * max(0.0, min(1.0, frac))
            return cb

        parts: list[Path] = []
        weight_lookup = dict(weights)

        if plan.include_intro:
            intro_path = job_dir / "intro.mp4"
            render_intro(
                out_path=intro_path,
                width=width,
                height=height,
                fps=fps,
                tournament=plan.project.info.tournament,
                p1=plan.project.info.p1,
                p2=plan.project.info.p2,
                on_progress=make_progress("intro", weight_lookup["intro"]),
            )
            parts.append(intro_path)
            completed_weight += weight_lookup["intro"]

        if plan.include_highlights and plan.project.highlights:
            hi_path = job_dir / "highlight.mp4"
            written = render_highlight_clip(
                src=src,
                out_path=hi_path,
                job_dir=job_dir,
                highlights=plan.project.highlights,
                width=width,
                height=height,
                fps=fps,
                has_audio=has_audio,
                on_progress=make_progress("highlight", weight_lookup["highlight"]),
            )
            if written:
                parts.append(hi_path)
            completed_weight += weight_lookup["highlight"]

        if plan.include_main:
            ass_path = job_dir / "scoreboard.ass"
            # The trimmed main duration is the sum of kept segment lengths.
            trimmed_duration = sum(b - a for a, b in kept)
            build_scoreboard_ass(
                output_path=ass_path,
                video_w=width,
                video_h=height,
                total_duration=trimmed_duration,
                tournament=plan.project.info.tournament,
                p1_name=plan.project.info.p1,
                p2_name=plan.project.info.p2,
                score_events=remapped_events,
                best_of=plan.project.info.best_of,
            )
            # FULL MATCH badge: shown for the first 15 seconds of the
            # main render so the viewer knows the highlight reel is over.
            fm_badge_path = job_dir / "full_match_badge.ass"
            build_full_match_badge_ass(
                output_path=fm_badge_path,
                video_w=width,
                video_h=height,
                show_seconds=15.0,
            )
            main_path = job_dir / "main.mp4"
            render_main_with_scoreboard(
                src=src,
                out_path=main_path,
                ass_path=ass_path,
                full_match_badge_ass=fm_badge_path,
                kept=kept,
                width=width,
                height=height,
                fps=fps,
                has_audio=has_audio,
                on_progress=make_progress("main", weight_lookup["main"]),
            )
            parts.append(main_path)
            completed_weight += weight_lookup["main"]

        if not parts:
            raise FFmpegError("No stages selected for render")

        out_name = plan.output_name or f"{plan.project_name}_{int(time.time())}.mp4"
        if not out_name.lower().endswith(".mp4"):
            out_name += ".mp4"
        final_path = config.output_dir / out_name

        concat_parts(parts, final_path, on_progress=make_progress("concat", weight_lookup["concat"]))
        completed_weight += weight_lookup["concat"]

        s.status = "done"
        s.progress = 1.0
        s.stage = "done"
        s.message = "Render complete"
        s.output_path = str(final_path)
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
