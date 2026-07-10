"""Top-level orchestrator: RenderPlan, RenderContext, the per-stage
helpers, and the public `run_render` entry point.

Each `_*_stage` function takes the shared `RenderContext` and threads
progress through `ctx.make_progress(stage_name)`. The orchestrator is
where the pipeline shape lives — stages.py contains the per-step
ffmpeg builders but has no view of the run loop.
"""

from __future__ import annotations

import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..ass import (
    ScoreFrame,
    build_scoreboard_ass,
    build_slow_motion_badge_ass,
)
from ..ass.scoreboard import resolve_row_names
from ..avatars import find_avatar_or_default
from ..config import config
from ..ffmpeg_runner import (
    FFmpegCancelled,
    FFmpegError,
    extract_frame_at,
    probe_video,
)
from ..intro_builder import render_cinematic_intro
from ..models import ProjectData
from ..stinger_builder import find_brand_logo, get_or_build_stinger_pair
from .replays import build_main_playlist, build_replay_plan, remap_events_with_replays
from .segments import kept_segments_from_trims, remap_score_event_to_trimmed
from .stages import (
    concat_parts,
    pre_concat_slices,
    render_intro,
    render_main_with_scoreboard,
    render_outro_card,
)
from .state import RenderState


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
        handicap_receiver=plan.project.info.handicap_receiver,
        handicap_pattern=plan.project.info.handicap_pattern,
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

    # Pre-concat optimization: when the playlist has more "slice"
    # entries than the GPU's NVDEC session budget, we can't feed each
    # slice as its own `-i src` (CUDA_ERROR_OUT_OF_MEMORY on cuvidCreate
    # at ~5-8 concurrent sessions; the software-decode fallback then
    # exhausts RAM with per-input HEVC ref-frame buffers). Solution:
    # consolidate every slice into a single intermediate via the concat
    # demuxer (one NVDEC + one NVENC), then let the main render consume
    # that file through `split` + `trim` per slice entry — one decoder
    # context, dozens of virtual sub-slices.
    SLICE_PRECONCAT_THRESHOLD = 6
    slice_ranges = [
        (e.src_start, e.src_end) for e in playlist if e.kind == "slice"
    ]
    main_slices_path: Optional[Path] = None
    if len(slice_ranges) > SLICE_PRECONCAT_THRESHOLD:
        main_slices_path = ctx.job_dir / "main_slices.mp4"
        # Pre-concat consumes the first ~25 % of the "main" weight; the
        # filter-graph render stage gets the remaining ~75 %. Empirical
        # split — pre-concat is sequential decode+encode (fast on NVENC),
        # filter-graph render does the per-replay slow-mo + 2 ass burns
        # so it's the slower of the two.
        pre_share = 0.25
        main_cb = ctx.make_progress("main")
        def _pre_cb(frac: float, msg: str) -> None:
            main_cb(frac * pre_share, f"pre-concat: {msg}")
        def _render_cb(frac: float, msg: str) -> None:
            main_cb(pre_share + (1.0 - pre_share) * frac, msg)
        pre_concat_slices(
            src=ctx.src,
            slice_ranges=slice_ranges,
            out_path=main_slices_path,
            width=ctx.width, height=ctx.height, fps=ctx.fps,
            has_audio=ctx.has_audio,
            on_progress=_pre_cb,
            cancel_check=ctx.cancel_check,
        )
        render_progress = _render_cb
    else:
        render_progress = ctx.make_progress("main")

    main_path = ctx.job_dir / "main.mp4"
    render_main_with_scoreboard(
        src=ctx.src,
        out_path=main_path,
        ass_path=ass_path,
        slow_motion_badge_ass=sm_badge_path,
        playlist=playlist,
        width=ctx.width, height=ctx.height, fps=ctx.fps,
        has_audio=ctx.has_audio,
        on_progress=render_progress,
        cancel_check=ctx.cancel_check,
        replay_sound_path=config.replay_sound_path,
        replay_sound_volume=config.replay_sound_volume,
        main_slices_path=main_slices_path,
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
    from ..groundtruth import export_groundtruth
    export_groundtruth(ctx, final_path)

    # Mirror everything (source + output + sidecars + project snapshot)
    # into `dataset/<slug>/` so the manual workflow doubles as dataset
    # accumulation. Reads the sidecar files written above so this MUST
    # run after export_groundtruth. Same best-effort policy.
    from ..dataset import archive_to_dataset
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
