"""Render orchestrator.

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

Module layout under backend/renderer/:
  state.py         — RenderState dataclass (per-job progress + status)
  segments.py      — kept_segments_from_trims, remap_score_event_to_trimmed
  replays.py       — slow-mo replay plumbing: ReplayInsert,
                     build_replay_plan, remap_events_with_replays,
                     _PlaylistEntry, build_main_playlist
  stages.py        — render_intro / render_main_with_scoreboard /
                     render_outro_card / concat_parts (kwargs-only,
                     no ctx dependency)
  orchestrator.py  — RenderPlan, RenderContext, _intro/_main/_outro/
                     _finalize, run_render
"""

from .orchestrator import (
    RenderContext,
    RenderPlan,
    _finalize,
    _intro_stage,
    _main_stage,
    _outro_stage,
    _prepare_context,
    _resolve_source,
    all_intro_photos_present,
    render_intro_clip,
    run_render,
)
from .replays import (
    ReplayInsert,
    build_main_playlist,
    build_replay_plan,
    remap_events_with_replays,
)
from .segments import kept_segments_from_trims, remap_score_event_to_trimmed
from .stages import (
    concat_parts,
    render_intro,
    render_main_with_scoreboard,
    render_outro_card,
)
from .state import RenderState

__all__ = [
    # Public API used by server + tests
    "RenderPlan",
    "RenderContext",
    "RenderState",
    "run_render",
    # Segment math (also used by groundtruth + tests)
    "kept_segments_from_trims",
    "remap_score_event_to_trimmed",
    # Replay plumbing (used by tests)
    "ReplayInsert",
    "build_replay_plan",
    "build_main_playlist",
    "remap_events_with_replays",
    # Stage helpers (used by tests)
    "render_intro",
    "render_main_with_scoreboard",
    "render_outro_card",
    "concat_parts",
    "all_intro_photos_present",
    # Intro clip (shared by _intro_stage and /api/preview/intro)
    "render_intro_clip",
]
