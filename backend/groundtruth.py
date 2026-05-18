"""
Ground-truth sidecar export for auto-trim validation.

Runs from `_finalize` after the final mp4 lands in output/. Writes two
files next to the output mp4:

  <name>.groundtruth.json    Project snapshot + derived kept segments
                              + source video metadata + stats. Includes
                              real-rally estimates derived from score
                              events (1 event ≈ 1 rally ending) since
                              kept segments are just post-trim chunks
                              and may contain many real rallies each.
  <name>.refframe.png        Single frame at the midpoint of the first
                              kept segment — guaranteed to have a player
                              at the table. Used as the backdrop when
                              drawing the ROI quadrilateral.

Best-effort: failure to write either file lands in RenderState.message
but does NOT abort the render — the mp4 is already on disk and a
debug-only sidecar shouldn't surface a red error banner.

Schema versions:
- v1: `rally_segments` field, `stats.rally_count` etc. — misleading
      names; "rally_segments" were actually kept segments (post-trim
      chunks), not real rallies.
- v2 (current): `kept_segments` with `rally_count_estimate` per segment
      + `stats.real_rally_count` / `stats.avg_real_rally_seconds`
      derived from score events. Names match what the data really is.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .ffmpeg_runner import FFmpegError, extract_frame_at, probe_video
from .models import ProjectData

if TYPE_CHECKING:
    from .renderer import RenderContext

GROUNDTRUTH_SCHEMA_VERSION = 2


def build_groundtruth(
    project: ProjectData,
    source_video: dict,
    *,
    exported_at: str,
) -> dict:
    """Pure builder: produce the groundtruth.json dict from project
    state + source video metadata. Caller supplies `exported_at` so the
    function stays clock-free (and the migration script can preserve
    the original timestamp).

    Two pieces of derived structure that distinguish v2 from v1:
    - `kept_segments[*].rally_count_estimate` — number of score events
      whose timestamp falls inside the segment. Lets the spike score
      precision/recall *per chunk* rather than against the whole match.
    - `stats.real_rally_count` / `avg_real_rally_seconds` — match-wide
      rally count proxied by score events (1 score = 1 rally ended).
      The auto-trim spike's `min_rally_duration` should typically sit
      well below `avg_real_rally_seconds`.
    """
    # Imported lazily to dodge the renderer ↔ groundtruth circular
    # import. Pure logic, no side effects from importing.
    from .renderer import kept_segments_from_trims

    duration = float(source_video.get("duration_sec", 0.0))
    kept_raw = kept_segments_from_trims(duration, project.trim_segments)
    event_times = [float(e.timestamp) for e in project.score_events]

    kept_segments: list[dict] = []
    for start, end in kept_raw:
        in_segment = sum(1 for t in event_times if start <= t <= end)
        kept_segments.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(end - start, 3),
            "rally_count_estimate": in_segment,
        })

    total_kept = sum(s["duration"] for s in kept_segments)
    total_dead = max(0.0, duration - total_kept)
    dead_pct = (total_dead / duration) if duration > 0 else 0.0
    real_rally_count = len(event_times)
    avg_real_rally = total_kept / real_rally_count if real_rally_count > 0 else 0.0

    return {
        "version": GROUNDTRUTH_SCHEMA_VERSION,
        "exported_at": exported_at,
        "source_video": source_video,
        "project": project.model_dump(),
        "kept_segments": kept_segments,
        "stats": {
            "kept_segment_count": len(kept_segments),
            "total_kept_duration": round(total_kept, 3),
            "total_dead_time": round(total_dead, 3),
            "dead_time_pct": round(dead_pct, 4),
            "real_rally_count": real_rally_count,
            "avg_real_rally_seconds": round(avg_real_rally, 3),
        },
    }


def export_groundtruth(ctx: "RenderContext", output_mp4: Path) -> None:
    """Write `<base>.groundtruth.json` + `<base>.refframe.png` next to
    the output mp4. Never raises — appends any failure into
    `RenderState.message` so the operator sees the warning but the
    render still reports `done`."""
    base = output_mp4.with_suffix("")
    json_path = base.with_suffix(".groundtruth.json")
    frame_path = base.with_suffix(".refframe.png")

    try:
        probe = probe_video(ctx.src)
    except FFmpegError as e:
        _append_msg(ctx, f"Groundtruth export skipped (probe failed): {e}")
        return

    source_video = {
        "path": str(ctx.src),
        "duration_sec": round(float(probe.get("duration", 0.0)), 3),
        "fps": round(float(probe.get("fps", 0.0)), 3),
        "width": int(probe.get("width", 0)),
        "height": int(probe.get("height", 0)),
        "has_audio": bool(probe.get("has_audio", False)),
    }

    sidecar = build_groundtruth(
        ctx.plan.project,
        source_video,
        exported_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )

    try:
        json_path.write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        _append_msg(ctx, f"Groundtruth JSON write failed: {e}")
        return

    # Reference frame: midpoint of first kept segment. _prepare_context
    # already errors on empty kept, so kept_segments is virtually always
    # non-empty here — the source-midpoint fallback is just defensive.
    kept = sidecar["kept_segments"]
    source_duration = float(source_video["duration_sec"])
    if kept:
        s0 = float(kept[0]["start"])
        e0 = float(kept[0]["end"])
        ref_t = s0 + (e0 - s0) / 2.0
    elif source_duration > 0.0:
        ref_t = source_duration / 2.0
    else:
        ref_t = 0.0

    try:
        extract_frame_at(ctx.src, ref_t, frame_path)
    except FFmpegError as e:
        _append_msg(ctx, f"Reference frame extract failed: {e}")


def _append_msg(ctx: "RenderContext", msg: str) -> None:
    prev = ctx.state.message
    ctx.state.message = f"{prev} | {msg}" if prev else msg
