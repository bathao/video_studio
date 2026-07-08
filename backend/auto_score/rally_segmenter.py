"""Unanchored rally segmentation for the Auto Score tab (Phase 1).

Finds candidate rally intervals in a full match WITHOUT score-event
anchors — the operator reviews the proposals and enters winners.
Ported from the Phase 0 spike (scripts/auto_score_spike/
eval_segmentation.py, segmenter "v7-tuned2"): hysteresis around
percentile thresholds + recursive valley split for merged rapid-point
blobs. Measured coverage recall on the corpus singles: train 98.0%,
held-out 94.9%, truly-unseen 97.8% (docs/AUTO_SCORE_PLAN.md §6 step 2).

Decode + motion machinery is reused from backend.rally_detector
(read-only imports); nothing in the auto-trim path is modified.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from backend.ffmpeg_runner import probe_video
from backend.rally_detector import (
    BALANCED,
    adaptive_threshold,
    compute_motion_signal,
    smooth_motion,
)


@dataclass(frozen=True)
class RallySegmenterParams:
    """v7-tuned2 — frozen from the 2026-07-08 sweep. Change only with
    a fresh eval_unseen.py run on ALL corpus singles."""

    pct_hi: float = 70.0
    pct_lo: float = 55.0
    enter_sustain_s: float = 0.3
    exit_sustain_s: float = 1.2
    start_dip_s: float = 0.4
    min_rally_s: float = 1.0
    split_over_s: float = 8.0
    valley_frac: float = 0.9
    edge_guard_s: float = 1.5


TUNED2 = RallySegmenterParams()


def _hysteresis_scan(
    smoothed: np.ndarray,
    fps: float,
    thr_hi: float,
    thr_lo: float,
    p: RallySegmenterParams,
) -> list[tuple[int, int]]:
    """ENTER after enter_sustain_s continuously above thr_hi; EXIT after
    exit_sustain_s continuously below thr_lo; start refined backward
    while above thr_lo (tolerating dips < start_dip_s). Sample-index
    intervals; min_rally filtering happens after the valley split."""
    n = len(smoothed)
    enter_w = max(1, int(round(p.enter_sustain_s * fps)))
    exit_w = max(1, int(round(p.exit_sustain_s * fps)))
    dip_w = max(1, int(round(p.start_dip_s * fps)))

    intervals: list[tuple[int, int]] = []
    i = 0
    while i < n:
        run = 0
        enter = -1
        while i < n:
            if smoothed[i] >= thr_hi:
                run += 1
                if run >= enter_w:
                    enter = i - enter_w + 1
                    break
            else:
                run = 0
            i += 1
        if enter < 0:
            break
        s = enter
        dip = 0
        j = enter - 1
        floor = intervals[-1][1] if intervals else 0
        while j > floor:
            if smoothed[j] >= thr_lo:
                s = j
                dip = 0
            else:
                dip += 1
                if dip >= dip_w:
                    break
            j -= 1
        run = 0
        end = n
        while i < n:
            if smoothed[i] < thr_lo:
                run += 1
                if run >= exit_w:
                    end = i - exit_w + 1
                    break
            else:
                run = 0
            i += 1
        intervals.append((s, end))
    return intervals


def segment_rallies(
    motion: np.ndarray,
    fps: float,
    params: RallySegmenterParams = TUNED2,
) -> list[tuple[float, float]]:
    """Motion signal -> candidate rally intervals in seconds. Pure."""
    if len(motion) == 0:
        return []
    sm = smooth_motion(
        np.asarray(motion, dtype=np.float32),
        int(round(BALANCED.smooth_window_s * fps)),
    )
    thr_hi = adaptive_threshold(sm, params.pct_hi)
    thr_lo = adaptive_threshold(sm, params.pct_lo)
    base = _hysteresis_scan(sm, fps, thr_hi, thr_lo, params)

    guard = int(params.edge_guard_s * fps)

    def split(a_i: int, b_i: int, out: list[tuple[int, int]]) -> None:
        # Rapid point series (~6 s apart) merge into one blob; cut at
        # the deepest internal valley, recursively.
        if (b_i - a_i) / fps <= params.split_over_s or b_i - a_i <= 2 * guard:
            out.append((a_i, b_i))
            return
        seg = sm[a_i + guard:b_i - guard]
        k = int(np.argmin(seg))
        cut = a_i + guard + k
        level = float(np.median(sm[a_i:b_i]))
        if seg[k] >= params.valley_frac * level:
            out.append((a_i, b_i))  # no meaningful valley — keep whole
            return
        split(a_i, cut, out)
        split(cut, b_i, out)

    result: list[tuple[int, int]] = []
    for a, b in base:
        split(a, b, result)
    return [
        (a / fps, b / fps)
        for a, b in result
        if (b - a) / fps >= params.min_rally_s
    ]


def run_rally_segmentation(
    video_path: Path,
    roi_corners: list[list[float]],
    params: RallySegmenterParams = TUNED2,
    *,
    cancel_check: Callable[[], bool] = lambda: False,
    emit: Callable[[str, dict], None] = lambda *a: None,
) -> list[dict]:
    """End-to-end: probe -> motion -> segment -> proposal dicts.

    Emits the same SSE event vocabulary as run_rally_detection
    (stage / progress / proposal / done) so the frontend client is a
    sibling of auto_trim/detection.js.
    """
    meta = probe_video(video_path)
    duration = float(meta.get("duration", 0.0))
    fps = float(BALANCED.decode_fps)
    emit("stage", {"name": "decode", "duration": duration, "fps": fps})

    def on_progress(done: int, total: int) -> None:
        emit("progress", {"stage": "decode", "frame_n": done, "frame_total": total})

    motion = compute_motion_signal(
        video_path, roi_corners, BALANCED,
        cancel_check=cancel_check,
        on_progress=on_progress,
        duration_hint=duration,
    )
    emit("stage", {"name": "segment", "samples": int(len(motion))})
    intervals = segment_rallies(motion, fps, params)

    proposals = [
        {
            "id": f"r{idx:03d}",
            "t_start": round(s, 3),
            "t_end": round(e, 3),
            "who": 0,
            "status": "pending",
        }
        for idx, (s, e) in enumerate(intervals)
    ]
    for prop in proposals:
        emit("proposal", prop)
    emit("done", {
        "proposals": proposals,
        "count": len(proposals),
        "duration": duration,
    })
    return proposals
