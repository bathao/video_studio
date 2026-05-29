"""Pure-logic tests for the score-anchored rally detector.

Covers the math that decides where trims go: `gaps_to_trims`,
`_find_rally_start_idx`, `smooth_motion`, `adaptive_threshold`, and the
score-press lag / pad arithmetic. No ffmpeg, no cv2, no real videos —
synthetic motion arrays feed the detector and we assert the emitted
trims.

The end-to-end "does it actually find the right rallies on a 22-min
match" verification lives in `scripts/verify_rally_detector.py`; this
file is fast (<100 ms) and shipped with the test suite so the math
can't silently regress."""

from __future__ import annotations

import numpy as np
import pytest

from backend.models import ScoreEvent, TrimSegment
from backend.rally_detector import (
    BALANCED,
    RallyDetectorParams,
    _find_rally_start_idx,
    adaptive_threshold,
    gaps_to_trims,
    smooth_motion,
)


# ---------- helpers -----------------------------------------------------------


def _synth_motion(
    duration_s: float,
    fps: int,
    *,
    rally_windows: list[tuple[float, float]],
    active_level: float = 0.08,
    idle_level: float = 0.02,
) -> np.ndarray:
    """Build a deterministic motion signal: `idle_level` everywhere
    except inside the listed rally windows, which get `active_level`."""
    n = int(round(duration_s * fps))
    sig = np.full(n, idle_level, dtype=np.float32)
    for start_s, end_s in rally_windows:
        a = max(0, int(round(start_s * fps)))
        b = min(n, int(round(end_s * fps)))
        sig[a:b] = active_level
    return sig


def _ev(t: float) -> ScoreEvent:
    return ScoreEvent(timestamp=t, who=1)


# ---------- smooth_motion -----------------------------------------------------


def test_smooth_motion_preserves_length():
    motion = np.array([0.0, 1.0, 0.0, 1.0, 0.0, 1.0], dtype=np.float32)
    out = smooth_motion(motion, window_samples=3)
    assert len(out) == len(motion)


def test_smooth_motion_passthrough_when_window_le_1():
    motion = np.array([0.1, 0.5, 0.9], dtype=np.float32)
    assert np.array_equal(smooth_motion(motion, window_samples=1), motion)
    assert np.array_equal(smooth_motion(motion, window_samples=0), motion)


def test_smooth_motion_handles_empty():
    out = smooth_motion(np.array([], dtype=np.float32), window_samples=5)
    assert len(out) == 0


def test_smooth_motion_actually_averages():
    motion = np.array([0.0, 0.0, 1.0, 0.0, 0.0], dtype=np.float32)
    out = smooth_motion(motion, window_samples=3)
    # Middle sample sees the spike → averaged across 3 → 1/3
    assert out[2] == pytest.approx(1.0 / 3.0, abs=1e-6)


# ---------- adaptive_threshold ------------------------------------------------


def test_adaptive_threshold_p70():
    # 10 samples 0..9 → p70 ≈ 6.3
    motion = np.arange(10, dtype=np.float32)
    th = adaptive_threshold(motion, 70.0)
    assert 5.5 <= th <= 6.5


def test_adaptive_threshold_empty():
    assert adaptive_threshold(np.array([], dtype=np.float32), 70.0) == 0.0


# ---------- _find_rally_start_idx --------------------------------------------


def test_find_rally_start_locates_idle_gap_before_rally():
    # 30 fps, 30s total. Rally [10, 20]; rest is idle.
    fps = 30
    motion = _synth_motion(30.0, fps, rally_windows=[(10.0, 20.0)])
    threshold = 0.05
    idle_samples = int(1.5 * fps)  # 45
    anchor_idx = int(19.5 * fps)
    rally_start_idx = _find_rally_start_idx(motion, anchor_idx, 0, idle_samples, threshold)
    # Expected rally_start ≈ 10s = 300 samples, ± a few frames slack
    # for the W-window placement.
    assert 285 <= rally_start_idx <= 320


def test_find_rally_start_returns_anchor_when_all_active():
    # If we never see sustained idle, can't locate boundary — return anchor.
    fps = 30
    motion = np.full(10 * fps, 0.10, dtype=np.float32)
    anchor_idx = len(motion) - 1
    out = _find_rally_start_idx(motion, anchor_idx, 0, int(1.5 * fps), 0.05)
    assert out == anchor_idx


def test_find_rally_start_requires_active_seen_first():
    # All idle from the start → no active ever seen → returns anchor
    # (interpreted by caller as "rally fills the gap" — but the
    # rally_dur_min gate downstream will then push it back).
    fps = 30
    motion = np.full(10 * fps, 0.01, dtype=np.float32)
    anchor_idx = len(motion) - 1
    out = _find_rally_start_idx(motion, anchor_idx, 0, int(1.5 * fps), 0.05)
    assert out == anchor_idx


def test_find_rally_start_respects_floor():
    fps = 30
    motion = _synth_motion(30.0, fps, rally_windows=[(10.0, 20.0)])
    # Floor at 12s — we don't even consider the actual rally boundary at 10s.
    anchor_idx = int(19.5 * fps)
    floor_idx = int(12.0 * fps)
    out = _find_rally_start_idx(motion, anchor_idx, floor_idx, int(1.5 * fps), 0.05)
    # No idle gap exists in [12, 19.5] → returns anchor.
    assert out == anchor_idx


# ---------- gaps_to_trims: edges ---------------------------------------------


def test_no_events_returns_no_trims():
    out = gaps_to_trims([], np.zeros(100, dtype=np.float32), 30.0, 0.05, 10.0)
    assert out == []


def test_pre_first_score_edge_trims_from_zero():
    # 30s video, rally [10, 20], score at 20.7s. Pre-first edge should trim 0 → ~9s.
    fps = 30
    motion = _synth_motion(30.0, fps, rally_windows=[(10.0, 20.0)])
    events = [_ev(20.7)]
    trims = gaps_to_trims(events, motion, fps, 0.05, 30.0)
    pre = next((t for t in trims if t.start == 0.0), None)
    assert pre is not None
    # rally_start ≈ 10s; pre_pad 1.0s → trim ends near 9s.
    assert 8.0 <= pre.end <= 10.0


def test_post_last_score_edge_trims_to_video_end():
    fps = 30
    # Long enough that the 30s keep window doesn't swallow the post-trim.
    motion = _synth_motion(90.0, fps, rally_windows=[(10.0, 20.0)])
    events = [_ev(20.7)]
    trims = gaps_to_trims(events, motion, fps, 0.05, 90.0)
    post = next((t for t in trims if abs(t.end - 90.0) < 0.01), None)
    assert post is not None
    # trim_start = t_last - lag + tail + post_match_keep_s
    #            = 20.7 - 0.7 + 0.5 + 30.0 = 50.5
    assert abs(post.start - 50.5) < 0.05


def test_post_match_keep_window_preserves_handshake():
    """post_match_keep_s keeps the configured window past the last score
    event for handshake + final scoreboard freeze. Default is 30s."""
    fps = 30
    # 90s video. Last point at 60.7. Default keep = 30s → trim starts at
    # 60.7 - 0.7 + 0.5 + 30.0 = 90.5, past video end → no post-trim emitted.
    motion = _synth_motion(90.0, fps, rally_windows=[(50.0, 60.0)])
    events = [_ev(60.7)]
    trims = gaps_to_trims(events, motion, fps, 0.05, 90.0)
    # Only the pre-first trim should exist; no post-trim because the
    # 30s keep window extends past video end.
    assert all(t.end <= 60.0 for t in trims), (
        f"Unexpected post-trim emitted within 30s keep: {trims}"
    )


def test_post_match_keep_window_zero_falls_back_to_old_behaviour():
    fps = 30
    motion = _synth_motion(30.0, fps, rally_windows=[(10.0, 20.0)])
    events = [_ev(20.7)]
    params = RallyDetectorParams(post_match_keep_s=0.0)
    trims = gaps_to_trims(events, motion, fps, 0.05, 30.0, params)
    post = next((t for t in trims if abs(t.end - 30.0) < 0.01), None)
    assert post is not None, f"Expected post-trim with keep=0, got: {trims}"
    # Same as old behaviour: trim_start = 20.7 - 0.7 + 0.5 = 20.5
    assert abs(post.start - 20.5) < 0.05


def test_zero_duration_returns_no_trims():
    out = gaps_to_trims([_ev(5.0)], np.zeros(0, dtype=np.float32), 30.0, 0.05, 0.0)
    assert out == []


# ---------- gaps_to_trims: between-score gaps --------------------------------


def test_simple_two_rally_match_emits_three_trims():
    # 120s video. Rally1 [10, 20] → score at 20.7. Rally2 [35, 50] → score at 50.7.
    # Expected trims: pre-first (~0..9), middle (~20.5..34), post-last (~80.5..120).
    # post-last starts at t_last - lag + tail + post_match_keep_s = 50.5 + 30 = 80.5.
    fps = 30
    motion = _synth_motion(
        120.0, fps,
        rally_windows=[(10.0, 20.0), (35.0, 50.0)],
    )
    events = [_ev(20.7), _ev(50.7)]
    trims = gaps_to_trims(events, motion, fps, 0.05, 120.0)
    assert len(trims) == 3
    # Sorted by start
    assert trims[0].start == 0.0
    assert 8.0 <= trims[0].end <= 10.0
    # Middle trim: t_prev - lag + tail = 20.5 → end near rally2_start - 1.0 = 34
    assert abs(trims[1].start - 20.5) < 0.05
    assert 33.0 <= trims[1].end <= 35.0
    # Post-last trim: starts 30s past the rally tail boundary.
    assert abs(trims[2].start - 80.5) < 0.05
    assert abs(trims[2].end - 120.0) < 0.01


def test_rally_dur_min_gate_caps_oversized_rally():
    """When backward-scan would locate rally_start very early (rally
    appears to be huge), the rally_dur_min gate forces a sensible
    minimum-length rally — push rally_start backward toward the score
    event by exactly rally_dur_min seconds. The effect: a shorter
    trim, fewer extras."""
    fps = 30
    # 30s video, motion is ACTIVE the entire time (no idle gap exists).
    # Backward-scan returns anchor → rally fills the gap → rally_start
    # would be at t_score - 0.5. The gate forces rally_start ≤ t_score
    # - lag - rally_dur_min = 20.7 - 0.7 - 5 = 15.0s.
    motion = np.full(int(30.0 * fps), 0.10, dtype=np.float32)
    events = [_ev(20.7)]
    trims = gaps_to_trims(events, motion, fps, 0.05, 30.0)
    pre = next((t for t in trims if t.start == 0.0), None)
    assert pre is not None
    # trim_end = rally_start - 1.0 = 15.0 - 1.0 = 14.0
    assert abs(pre.end - 14.0) < 0.1


def test_min_trim_dur_drops_tiny_trims():
    """Trims shorter than min_trim_dur_s (0.5s) shouldn't be emitted —
    operator review cost > value."""
    fps = 30
    # Two score events very close together with constant rally motion.
    motion = np.full(int(30.0 * fps), 0.10, dtype=np.float32)
    # Events at 15.0 and 15.4 → between-score gap is 0.4s.
    # trim_start = 15.0 - 0.7 + 0.5 = 14.8; rally_start ≤ 15.4 - 0.7 - 5 = 9.7
    # but capped by floor (rally_start ≥ trim_start = 14.8) → 14.8.
    # trim_end = 14.8 - 1.0 = 13.8. trim_end < trim_start → degenerate, skipped.
    events = [_ev(15.0), _ev(15.4)]
    trims = gaps_to_trims(events, motion, fps, 0.05, 30.0)
    middles = [t for t in trims if t.start > 0 and t.end < 30.0 - 0.5]
    # Either zero middles or the single between-score trim was dropped.
    assert all((t.end - t.start) >= BALANCED.min_trim_dur_s for t in middles)


def test_press_lag_is_applied_to_trim_start():
    """The trim's start time = score_event.timestamp - lag + tail.
    With defaults lag=0.7 tail=0.5, trim_start = t_score - 0.2."""
    fps = 30
    motion = _synth_motion(60.0, fps, rally_windows=[(10.0, 20.0), (35.0, 50.0)])
    events = [_ev(20.7), _ev(50.7)]
    trims = gaps_to_trims(events, motion, fps, 0.05, 60.0)
    # Middle trim starts at 20.7 - 0.7 + 0.5 = 20.5
    middle = [t for t in trims if t.start > 0 and t.end < 60.0][0]
    assert abs(middle.start - 20.5) < 0.05


def test_overlapping_trims_get_merged():
    """If two trims emerge with overlapping or touching ranges (e.g.
    because the operator double-tapped a score event), the output should
    be a single merged trim — not two duplicates."""
    fps = 30
    motion = np.full(int(30.0 * fps), 0.10, dtype=np.float32)
    # Two events 0.1s apart. The detector emits a between-trim that
    # might overlap the post-last edge depending on lag arithmetic.
    events = [_ev(10.0), _ev(10.1)]
    trims = gaps_to_trims(events, motion, fps, 0.05, 30.0)
    # Whatever trims emerge, they must not overlap.
    for a, b in zip(trims, trims[1:]):
        assert a.end <= b.start, f"Overlap between {a} and {b}"


def test_score_events_unsorted_still_works():
    fps = 30
    # 120s so the 30s post-match keep window doesn't swallow the post-trim.
    motion = _synth_motion(120.0, fps, rally_windows=[(10.0, 20.0), (35.0, 50.0)])
    events = [_ev(50.7), _ev(20.7)]  # reversed
    trims = gaps_to_trims(events, motion, fps, 0.05, 120.0)
    # Should produce the same 3-trim structure as the sorted version.
    assert len(trims) == 3


def test_trims_never_extend_past_video_duration():
    fps = 30
    motion = _synth_motion(30.0, fps, rally_windows=[(10.0, 20.0)])
    events = [_ev(20.7)]
    trims = gaps_to_trims(events, motion, fps, 0.05, 30.0)
    for t in trims:
        assert 0.0 <= t.start <= 30.0
        assert 0.0 <= t.end <= 30.0
        assert t.start <= t.end


# ---------- params customisation ---------------------------------------------


def test_custom_params_override_defaults():
    """Changing rally_dur_min from 5 → 10 makes the gate cap rally length
    differently."""
    fps = 30
    motion = np.full(int(30.0 * fps), 0.10, dtype=np.float32)
    events = [_ev(20.7)]
    long_rally = RallyDetectorParams(rally_dur_min_s=10.0)
    trims = gaps_to_trims(events, motion, fps, 0.05, 30.0, long_rally)
    pre = next((t for t in trims if t.start == 0.0), None)
    assert pre is not None
    # rally_start = 20.7 - 0.7 - 10 = 10.0 → trim_end = 10.0 - 1.0 = 9.0
    assert abs(pre.end - 9.0) < 0.1
