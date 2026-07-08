"""Pure-logic tests for backend/auto_score/rally_segmenter.py.

Synthetic motion signals only — no ffmpeg, no cv2 (smooth_motion and
adaptive_threshold are numpy). The segmenter thresholds are signal
PERCENTILES (p70/p55), designed for real matches where rallies occupy
roughly 35-65% of the samples — every fixture keeps burst mass in that
regime. Bursts carry a +0-10% jitter (constant-level bursts would make
thr_hi an exact-float-equality comparison) and the moments right after
a burst are painted DEEP-quiet where end precision matters (matching
reality: a player walking to fetch the ball reads near-zero in the
table ROI).

Covers the v7-tuned2 behaviours the Phase 0 spike measured: hysteresis
intervals, short-spike rescue (min_rally_s 1.5 -> 1.0), recursive
valley split for merged rapid-point blobs, and the shallow-valley
keep-whole guard.
"""

from __future__ import annotations

import numpy as np
import pytest

from backend.auto_score import TUNED2, RallySegmenterParams, segment_rallies

FPS = 30.0
QUIET = 0.001
DEEP = 0.0002
LOUD = 0.05


def _signal(
    total_s: float,
    bursts: list[tuple[float, float]],
    deadzones: list[tuple[float, float]] = (),
    dips: list[tuple[float, float, float]] = (),
) -> np.ndarray:
    n = int(total_s * FPS)
    rng = np.random.default_rng(7)
    sig = rng.uniform(QUIET * 0.5, QUIET * 2.0, n).astype(np.float32)
    for s, e in bursts:
        a, b = int(s * FPS), int(e * FPS)
        sig[a:b] = LOUD * rng.uniform(1.0, 1.1, b - a)
    for s, e in deadzones:
        sig[int(s * FPS):int(e * FPS)] = DEEP
    for s, e, level in dips:
        sig[int(s * FPS):int(e * FPS)] = level
    return sig


def test_empty_signal_no_intervals():
    assert segment_rallies(np.array([], dtype=np.float32), FPS) == []


def test_single_burst_one_interval():
    # One 25 s rally in a 60 s clip (~42% burst mass), deep-quiet
    # shoulders. Jittered interior -> min/median stays >= ~0.95 so the
    # valley split keeps the blob whole despite exceeding split_over_s.
    sig = _signal(60.0, bursts=[(20.0, 45.0)],
                  deadzones=[(15.0, 20.0), (45.0, 50.0)])
    out = segment_rallies(sig, FPS)
    assert len(out) == 1
    s, e = out[0]
    assert s == pytest.approx(20.0, abs=1.0)
    assert e == pytest.approx(45.0, abs=2.0)  # exit_sustain trails a bit


def test_short_spike_survives_tuned2_min_rally():
    # A 0.9 s spike between deep-quiet shoulders segments to a ~1.4 s
    # interval (hysteresis start-refine + exit tolerance pad it):
    # dropped by the old min_rally_s=1.5, kept by tuned2's 1.0 — this
    # rescue is where held-out coverage went 83.5% -> 94.9% in the
    # Phase 0 audit.
    sig = _signal(60.0, bursts=[(5.0, 25.0), (40.0, 40.9)],
                  deadzones=[(25.0, 28.0), (36.0, 40.0), (40.9, 45.0)])
    assert len(segment_rallies(sig, FPS)) == 2
    old = RallySegmenterParams(min_rally_s=1.5)
    assert len(segment_rallies(sig, FPS, old)) == 1


def test_merged_blob_splits_at_valley():
    # Two 5 s rallies separated by a 0.6 s deep dip — too brief for the
    # 1.2 s exit_sustain, so hysteresis fuses them into one ~10.6 s
    # blob (> split_over_s=8). The valley split must cut it in two.
    sig = _signal(
        60.0,
        bursts=[(5.0, 20.0), (30.0, 35.0), (35.6, 40.6)],
        deadzones=[(27.0, 30.0), (40.6, 44.0)],
        dips=[(35.0, 35.6, DEEP)],
    )
    out = segment_rallies(sig, FPS)
    assert len(out) == 3
    # The cut lands inside the 35.0-35.6 dip.
    assert out[1][1] == pytest.approx(35.3, abs=0.6)
    assert out[2][0] == pytest.approx(35.3, abs=0.6)


def test_shallow_valley_not_split():
    # Same blob shape but the dip only sags to the bottom of the burst
    # jitter band (~0.95x the blob median) — above valley_frac (0.9),
    # no meaningful valley: keep whole.
    sig = _signal(
        60.0,
        bursts=[(5.0, 20.0), (30.0, 40.6)],
        deadzones=[(27.0, 30.0), (40.6, 44.0)],
        dips=[(35.0, 35.6, LOUD)],
    )
    out = segment_rallies(sig, FPS)
    assert len(out) == 2


def test_tuned2_defaults_frozen():
    # Guard against silent drift from the measured config — these
    # numbers carry the G0a result (coverage 97.5%); change them only
    # with a fresh eval_unseen.py run.
    assert TUNED2.min_rally_s == 1.0
    assert TUNED2.pct_lo == 55.0
    assert TUNED2.split_over_s == 8.0
    assert TUNED2.valley_frac == 0.9
    assert TUNED2.edge_guard_s == 1.5
