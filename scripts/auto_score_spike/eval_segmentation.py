"""Unanchored rally segmentation eval (AUTO_SCORE_PLAN Phase 0 step 2).

Proposes rally intervals from the motion signal WITHOUT score-event
anchors, then scores the proposals against operator truth:

- `--two-sets`: rally-START accuracy vs the 34-point truth in
  dataset/attempt1 (tolerance 0.75 s strict / 2.0 s loose). Writes a
  signal-trace PNG (motion curve + threshold + truth vs proposals) per
  the plan's debug protocol.
- `--corpus`: rally-END recall/precision vs the manual score events in
  dataset/auto_score_corpus/corpus.jsonl (end ~= t_event - press lag,
  tolerance 2.0 s). Needs motion caches for the corpus videos (build
  via motion_cache.py; full decode takes minutes per match).

Segmenter versions are the plan 6.2 improvement-ladder rungs — add a
new function per rung, never mutate a measured one.

CLI:
    venv/Scripts/python.exe scripts/auto_score_spike/eval_segmentation.py --two-sets [--seg v0]
    venv/Scripts/python.exe scripts/auto_score_spike/eval_segmentation.py --corpus  [--seg v0]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.rally_detector import BALANCED, adaptive_threshold, smooth_motion  # noqa: E402
from scripts.auto_score_spike.motion_cache import OUT_DIR, get_motion_dual  # noqa: E402

TWO_SETS_VIDEO = ROOT / "dataset" / "attempt1" / "source_videos" / "2_sets.mp4"
TWO_SETS_TRUTH = (
    ROOT / "dataset" / "attempt1" / "reviewed_matches"
    / "match_2_sets_debug_001" / "step3_1_start_truth.json"
)
CORPUS_JSONL = ROOT / "dataset" / "auto_score_corpus" / "corpus.jsonl"

# Operator presses the score key ~0.7 s after the rally really ends
# (BALANCED.score_press_lag_s) — corpus rally-end truth is t_event - lag.
PRESS_LAG_S = BALANCED.score_press_lag_s


# ---------------------------------------------------------------------------
# Segmenter ladder — one function per measured rung
# ---------------------------------------------------------------------------


def segment_v0(
    motion: np.ndarray,
    fps: float,
    *,
    percentile: float = 70.0,
    min_rally_s: float = 1.5,
    min_gap_s: float = 2.0,
) -> list[tuple[float, float]]:
    """Rung 0: smoothed signal + single adaptive threshold + duration
    priors (merge close active runs, drop too-short rallies)."""
    smoothed = smooth_motion(motion, int(round(BALANCED.smooth_window_s * fps)))
    thr = adaptive_threshold(smoothed, percentile)
    active = (smoothed >= thr).astype(np.int8)
    edges = np.flatnonzero(np.diff(np.concatenate([[0], active, [0]])))
    runs = edges.reshape(-1, 2)  # [start_idx, end_idx) pairs

    merged: list[list[int]] = []
    for s, e in runs:
        if merged and (s - merged[-1][1]) / fps < min_gap_s:
            merged[-1][1] = int(e)
        else:
            merged.append([int(s), int(e)])
    return [
        (s / fps, e / fps)
        for s, e in merged
        if (e - s) / fps >= min_rally_s
    ]


def segment_v1(
    motion: np.ndarray,
    fps: float,
    *,
    pct_hi: float = 75.0,
    pct_lo: float = 55.0,
    enter_sustain_s: float = 0.3,
    exit_sustain_s: float = 1.2,
    start_dip_s: float = 0.4,
    min_rally_s: float = 1.5,
) -> list[tuple[float, float]]:
    """Rung 1: hysteresis. ENTER a rally after `enter_sustain_s`
    continuously above the HIGH threshold; EXIT after `exit_sustain_s`
    continuously below the LOW threshold (brief dips don't split a
    rally). The start is then refined BACKWARD from the enter point
    while the signal stays above LOW (tolerating dips < `start_dip_s`)
    — catches the low-motion serve-toss ramp that made v0 starts late."""
    smoothed = smooth_motion(motion, int(round(BALANCED.smooth_window_s * fps)))
    thr_hi = adaptive_threshold(smoothed, pct_hi)
    thr_lo = adaptive_threshold(smoothed, pct_lo)
    n = len(smoothed)
    enter_w = max(1, int(round(enter_sustain_s * fps)))
    exit_w = max(1, int(round(exit_sustain_s * fps)))
    dip_w = max(1, int(round(start_dip_s * fps)))

    intervals: list[tuple[int, int]] = []
    i = 0
    while i < n:
        # --- seek ENTER: enter_w consecutive samples above thr_hi
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
        # --- refine START backward: stay above thr_lo, tolerate short dips
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
        # --- seek EXIT: exit_w consecutive samples below thr_lo
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

    return [
        (s / fps, e / fps) for s, e in intervals if (e - s) / fps >= min_rally_s
    ]


def _otsu_threshold(smoothed: np.ndarray) -> float:
    """Otsu's method on the smoothed-signal histogram. Unlike a fixed
    percentile, this adapts to the video's actual active/dead mix —
    a 6-min debug clip that is ~50% rally and a 22-min match that is
    ~64% rally get equally sensible thresholds."""
    hi = float(np.percentile(smoothed, 99.5))
    hist, edges = np.histogram(np.clip(smoothed, 0, hi), bins=256)
    centers = (edges[:-1] + edges[1:]) / 2
    total = hist.sum()
    best_thr, best_var = float(np.median(smoothed)), -1.0
    w0 = 0.0
    sum0 = 0.0
    sum_all = float((hist * centers).sum())
    for k in range(256):
        w0 += hist[k]
        if w0 == 0 or w0 == total:
            continue
        sum0 += hist[k] * centers[k]
        w1 = total - w0
        m0 = sum0 / w0
        m1 = (sum_all - sum0) / w1
        var = w0 * w1 * (m0 - m1) ** 2
        if var > best_var:
            best_var = var
            best_thr = float(centers[k])
    return best_thr


def segment_v2(
    motion: np.ndarray,
    fps: float,
    *,
    lo_frac: float = 0.6,
    enter_sustain_s: float = 0.3,
    exit_sustain_s: float = 1.2,
    start_dip_s: float = 0.4,
    min_rally_s: float = 1.5,
) -> list[tuple[float, float]]:
    """Rung 2: hysteresis around an OTSU threshold instead of fixed
    percentiles. thr_hi = otsu, thr_lo = lo_frac * otsu."""
    smoothed = smooth_motion(motion, int(round(BALANCED.smooth_window_s * fps)))
    thr_hi = _otsu_threshold(smoothed)
    thr_lo = thr_hi * lo_frac
    return _hysteresis_scan(
        smoothed, fps, thr_hi, thr_lo,
        enter_sustain_s=enter_sustain_s, exit_sustain_s=exit_sustain_s,
        start_dip_s=start_dip_s, min_rally_s=min_rally_s,
    )


def _hysteresis_scan(
    smoothed: np.ndarray,
    fps: float,
    thr_hi: float,
    thr_lo: float,
    *,
    enter_sustain_s: float,
    exit_sustain_s: float,
    start_dip_s: float,
    min_rally_s: float,
) -> list[tuple[float, float]]:
    n = len(smoothed)
    enter_w = max(1, int(round(enter_sustain_s * fps)))
    exit_w = max(1, int(round(exit_sustain_s * fps)))
    dip_w = max(1, int(round(start_dip_s * fps)))

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

    return [
        (s / fps, e / fps) for s, e in intervals if (e - s) / fps >= min_rally_s
    ]


def segment_v3(
    info: dict,
    *,
    pct_hi: float = 60.0,
    lo_frac: float = 0.75,
    exit_sustain_s: float = 1.2,
    start_dip_s: float = 0.4,
    min_rally_s: float = 1.5,
    min_switches: int = 2,
    dominance_max: float = 0.9,
    eps_frac: float = 0.2,
) -> list[tuple[float, float]]:
    """Rung 3: ping-pong alternation gate. LOWER enter threshold than
    v0/v1 (recover missed low-energy rallies), then keep only intervals
    whose near/far half-ROI motion ALTERNATES like a ball exchange:
    >= min_switches side flips (with a deadband) and no side dominating
    more than dominance_max of the active samples. Ball fetching,
    toweling, umpire motion are one-sided and get dropped."""
    fps = float(info["fps"])
    w = int(round(BALANCED.smooth_window_s * fps))
    sm = smooth_motion(info["motion"], w)
    thr_hi = adaptive_threshold(sm, pct_hi)
    thr_lo = thr_hi * lo_frac
    intervals = _hysteresis_scan(
        sm, fps, thr_hi, thr_lo,
        enter_sustain_s=0.3, exit_sustain_s=exit_sustain_s,
        start_dip_s=start_dip_s, min_rally_s=min_rally_s,
    )
    sn = smooth_motion(info["near"], w)
    sf = smooth_motion(info["far"], w)

    kept = []
    for s, e in intervals:
        i0, i1 = int(s * fps), min(len(sn), int(e * fps))
        if i1 - i0 < 2:
            continue
        d = sn[i0:i1] - sf[i0:i1]
        amp = float((sn[i0:i1] + sf[i0:i1]).mean())
        eps = eps_frac * amp
        labels = np.sign(d) * (np.abs(d) > eps)
        nz = labels[labels != 0]
        if len(nz) == 0:
            continue
        switches = int(np.count_nonzero(np.diff(nz)))
        dominance = max(float((nz > 0).mean()), float((nz < 0).mean()))
        if switches >= min_switches and dominance <= dominance_max:
            kept.append((s, e))
    return kept


def segment_v4(
    info: dict,
    *,
    pct_hi: float = 60.0,
    lo_frac: float = 0.75,
    exit_sustain_s: float = 1.2,
    start_dip_s: float = 0.4,
    min_rally_s: float = 1.5,
    min_switches: int = 2,
    dominance_max: float = 0.9,
    eps: float = 0.35,
) -> list[tuple[float, float]]:
    """Rung 3 fixed: v3's alternation gate FAILED because the near half
    is closer to the camera and its motion amplitude systematically
    dominates (perspective). v4 z-normalizes each half by its own
    robust scale before comparing, so 'which side is moving' is judged
    per-half relative to that half's own baseline."""
    fps = float(info["fps"])
    w = int(round(BALANCED.smooth_window_s * fps))
    sm = smooth_motion(info["motion"], w)
    thr_hi = adaptive_threshold(sm, pct_hi)
    intervals = _hysteresis_scan(
        sm, fps, thr_hi, thr_hi * lo_frac,
        enter_sustain_s=0.3, exit_sustain_s=exit_sustain_s,
        start_dip_s=start_dip_s, min_rally_s=min_rally_s,
    )

    def norm(x: np.ndarray) -> np.ndarray:
        s = smooth_motion(x, w)
        med = float(np.median(s))
        mad = float(np.median(np.abs(s - med))) + 1e-9
        return (s - med) / mad

    zn, zf = norm(info["near"]), norm(info["far"])

    kept = []
    for s, e in intervals:
        i0, i1 = int(s * fps), min(len(zn), int(e * fps))
        if i1 - i0 < 2:
            continue
        d = zn[i0:i1] - zf[i0:i1]
        labels = np.sign(d) * (np.abs(d) > eps)
        nz = labels[labels != 0]
        if len(nz) == 0:
            continue
        switches = int(np.count_nonzero(np.diff(nz)))
        dominance = max(float((nz > 0).mean()), float((nz < 0).mean()))
        if switches >= min_switches and dominance <= dominance_max:
            kept.append((s, e))
    return kept


def segment_v5(
    info: dict,
    *,
    ds_fps: float = 5.0,
    rally_dur_mu: float = 5.0,
    rally_dur_sigma: float = 0.6,
    break_dur_mu: float = 9.0,
    break_dur_sigma: float = 0.7,
    burst_weight: float = 0.25,
    max_dur_s: float = 60.0,
    min_rally_s: float = 1.2,
) -> list[tuple[float, float]]:
    """Rung 5 (structural): explicit-duration 2-state semi-Markov
    Viterbi over the whole timeline. RALLY emits high motion with a
    log-normal duration prior (~2-10 s); BREAK emits low motion PLUS a
    heavy tail (mixture) so ball-fetch bursts are absorbed instead of
    spawning fake rallies. Uses match rhythm — the one strong prior the
    per-burst classifiers above cannot see."""
    fps = float(info["fps"])
    w = int(round(BALANCED.smooth_window_s * fps))
    sm = smooth_motion(info["motion"], w)
    step = max(1, int(round(fps / ds_fps)))
    x = sm[::step]
    f = fps / step
    n = len(x)
    lx = np.log(np.maximum(x, 1e-6))

    lo = np.median(lx[lx <= np.percentile(lx, 50)])
    hi = np.median(lx[lx >= np.percentile(lx, 75)])
    sd = max(0.3, float(np.std(lx)) * 0.5)

    def norm_logpdf(v, mu, s):
        return -0.5 * ((v - mu) / s) ** 2 - math.log(s * math.sqrt(2 * math.pi))

    em_rally = norm_logpdf(lx, hi, sd)
    em_quiet = norm_logpdf(lx, lo, sd)
    # BREAK = mixture of quiet + rally-like bursts
    em_break = np.logaddexp(
        math.log(1 - burst_weight) + em_quiet,
        math.log(burst_weight) + em_rally,
    )
    cum = {"R": np.concatenate([[0.0], np.cumsum(em_rally)]),
           "B": np.concatenate([[0.0], np.cumsum(em_break)])}

    max_d = int(max_dur_s * f)
    durs = np.arange(1, max_d + 1)

    def logdur(mu_s, sigma):
        d_s = durs / f
        return (-0.5 * ((np.log(d_s) - math.log(mu_s)) / sigma) ** 2
                - np.log(d_s * sigma * math.sqrt(2 * math.pi)))

    ld = {"R": logdur(rally_dur_mu, rally_dur_sigma),
          "B": logdur(break_dur_mu, break_dur_sigma)}

    NEG = -1e18
    V = {"R": np.full(n + 1, NEG), "B": np.full(n + 1, NEG)}
    bp: dict[str, list] = {"R": [None] * (n + 1), "B": [None] * (n + 1)}
    V["R"][0] = V["B"][0] = 0.0
    other = {"R": "B", "B": "R"}
    for t in range(1, n + 1):
        for s in ("R", "B"):
            dmax = min(max_d, t)
            seg_em = cum[s][t] - cum[s][t - dmax:t]          # duration d = dmax..1
            prev = V[other[s]][t - dmax:t]
            cand = prev + seg_em + ld[s][dmax - 1::-1]
            k = int(np.argmax(cand))
            if cand[k] > V[s][t]:
                V[s][t] = float(cand[k])
                bp[s][t] = (t - dmax + k, other[s])

    s = "R" if V["R"][n] >= V["B"][n] else "B"
    t = n
    segs = []
    while t > 0 and bp[s][t] is not None:
        t0, ps = bp[s][t]
        if s == "R":
            segs.append((t0 / f, t / f))
        t, s = t0, ps
    segs.reverse()
    return [(a, b) for a, b in segs if b - a >= min_rally_s]


def segment_v6(
    info: dict,
    *,
    pct_hi: float = 60.0,
    lo_frac: float = 0.83,
    exit_sustain_s: float = 1.2,
    start_dip_s: float = 0.4,
    min_rally_s: float = 1.5,
    prob_min: float = 0.5,
    recover_prob: float = 0.6,
    recover_sustain_s: float = 1.5,
) -> list[tuple[float, float]]:
    """Rung 6 (vision escalation): motion candidates x P(rally) timeline
    from the trained frame classifier (info['prob'] @ info['prob_fps']).
    (a) candidates from a recall-leaning hysteresis scan are KEPT only
    if their mean P(rally) >= prob_min (junk filter); (b) gaps between
    kept candidates are scanned for sustained high-P(rally) runs the
    motion signal missed (recovery)."""
    fps = float(info["fps"])
    prob = info["prob"]
    pfps = float(info["prob_fps"])
    w = int(round(BALANCED.smooth_window_s * fps))
    sm = smooth_motion(info["motion"], w)
    thr_hi = adaptive_threshold(sm, pct_hi)
    cands = _hysteresis_scan(
        sm, fps, thr_hi, thr_hi * lo_frac,
        enter_sustain_s=0.3, exit_sustain_s=exit_sustain_s,
        start_dip_s=start_dip_s, min_rally_s=min_rally_s,
    )

    def mean_prob(a: float, b: float) -> float:
        i0, i1 = int(a * pfps), max(int(a * pfps) + 1, int(b * pfps))
        seg = prob[i0:min(i1, len(prob))]
        return float(seg.mean()) if len(seg) else 0.0

    kept = [(a, b) for a, b in cands if mean_prob(a, b) >= prob_min]

    # recovery: sustained P(rally) runs inside the gaps
    sus = max(1, int(round(recover_sustain_s * pfps)))
    hot = prob >= recover_prob
    edges = np.flatnonzero(np.diff(np.concatenate([[0], hot.view(np.int8), [0]])))
    runs = [(s / pfps, e / pfps) for s, e in edges.reshape(-1, 2) if e - s >= sus]
    recovered = []
    for a, b in runs:
        mid = (a + b) / 2
        if not any(ka - 2 <= mid <= kb + 2 for ka, kb in kept):
            recovered.append((a, b))
    return sorted(kept + recovered)


def segment_v7(
    info: dict,
    *,
    pct_hi: float = 70.0,
    pct_lo: float = 60.0,
    exit_sustain_s: float = 1.2,
    start_dip_s: float = 0.4,
    min_rally_s: float = 1.5,
    split_over_s: float = 14.0,
    valley_frac: float = 0.75,
    edge_guard_s: float = 2.5,
) -> list[tuple[float, float]]:
    """Rung 7 (from the 2026-07-08 miss audit): rapid point series
    (serve winners ~6 s apart) merge into one long active blob — 25/26
    missed events on the worst match sat ABOVE threshold inside merged
    intervals. v7 = v1 hysteresis + recursive valley split: any
    interval longer than `split_over_s` is cut at its deepest internal
    valley (must be below `valley_frac` x the interval's median level,
    at least `edge_guard_s` from both edges), recursively."""
    fps = float(info["fps"])
    w = int(round(BALANCED.smooth_window_s * fps))
    sm = smooth_motion(info["motion"], w)
    thr_hi = adaptive_threshold(sm, pct_hi)
    thr_lo = adaptive_threshold(sm, pct_lo)
    base = _hysteresis_scan(
        sm, fps, thr_hi, thr_lo,
        enter_sustain_s=0.3, exit_sustain_s=exit_sustain_s,
        start_dip_s=start_dip_s, min_rally_s=min_rally_s,
    )

    guard = int(edge_guard_s * fps)

    def split(a_i: int, b_i: int, out: list) -> None:
        if (b_i - a_i) / fps <= split_over_s or b_i - a_i <= 2 * guard:
            out.append((a_i, b_i))
            return
        seg = sm[a_i + guard:b_i - guard]
        k = int(np.argmin(seg))
        cut = a_i + guard + k
        level = float(np.median(sm[a_i:b_i]))
        if seg[k] >= valley_frac * level:
            out.append((a_i, b_i))  # no meaningful valley — keep whole
            return
        split(a_i, cut, out)
        split(cut, b_i, out)

    result: list[tuple[int, int]] = []
    for a, b in base:
        split(int(a * fps), int(b * fps), result)
    return [
        (a / fps, b / fps) for a, b in result if (b - a) / fps >= min_rally_s
    ]


SEGMENTERS = {
    "v0": lambda info, **kw: segment_v0(info["motion"], float(info["fps"]), **kw),
    "v1": lambda info, **kw: segment_v1(info["motion"], float(info["fps"]), **kw),
    "v2": lambda info, **kw: segment_v2(info["motion"], float(info["fps"]), **kw),
    "v3": segment_v3,
    "v4": segment_v4,
    "v5": segment_v5,
    "v6": segment_v6,
    "v7": segment_v7,
}


# ---------------------------------------------------------------------------
# Matching + metrics
# ---------------------------------------------------------------------------


def match_events(
    truth: list[float], proposed: list[float], tolerance_s: float
) -> list[tuple[int, int, float]]:
    """Greedy one-to-one matching by |dt| within tolerance.
    Returns [(truth_idx, proposed_idx, dt), ...]."""
    pairs = sorted(
        (
            (abs(t - p), ti, pi)
            for ti, t in enumerate(truth)
            for pi, p in enumerate(proposed)
            if abs(t - p) <= tolerance_s
        ),
    )
    used_t: set[int] = set()
    used_p: set[int] = set()
    matches = []
    for dt, ti, pi in pairs:
        if ti in used_t or pi in used_p:
            continue
        used_t.add(ti)
        used_p.add(pi)
        matches.append((ti, pi, dt))
    return matches


def report(name: str, truth: list[float], proposed: list[float], tol: float) -> dict:
    m = match_events(truth, proposed, tol)
    recall = len(m) / len(truth) if truth else 0.0
    precision = len(m) / len(proposed) if proposed else 0.0
    dts = sorted(dt for _, _, dt in m)
    med = dts[len(dts) // 2] if dts else float("nan")
    print(
        f"  {name:28} tol={tol:.2f}s  recall {len(m)}/{len(truth)} = {recall:5.1%}"
        f"  precision {len(m)}/{len(proposed)} = {precision:5.1%}"
        f"  median|dt|={med:.2f}s"
    )
    return {"tol": tol, "recall": recall, "precision": precision,
            "matched": len(m), "truth": len(truth), "proposed": len(proposed),
            "median_dt": med}


# ---------------------------------------------------------------------------
# Signal-trace plot (cv2 only — no new deps)
# ---------------------------------------------------------------------------


def trace_plot(
    out_png: Path,
    motion: np.ndarray,
    fps: float,
    thr: float,
    truth_s: list[float],
    intervals: list[tuple[float, float]],
    px_per_s: int = 8,
    height: int = 360,
) -> None:
    import cv2

    smoothed = smooth_motion(motion, int(round(BALANCED.smooth_window_s * fps)))
    dur = len(smoothed) / fps
    width = int(dur * px_per_s) + 40
    img = np.full((height, width, 3), 255, dtype=np.uint8)
    lo, hi = 0.0, float(np.percentile(smoothed, 99.5)) * 1.2

    def x_of(t: float) -> int:
        return 20 + int(t * px_per_s)

    def y_of(v: float) -> int:
        return height - 20 - int((min(v, hi) - lo) / (hi - lo) * (height - 60))

    # proposed intervals shaded
    for s, e in intervals:
        cv2.rectangle(img, (x_of(s), 20), (x_of(e), height - 20), (225, 210, 210), -1)
    # truth starts: green
    for t in truth_s:
        cv2.line(img, (x_of(t), 20), (x_of(t), height - 20), (60, 170, 60), 2)
    # proposed starts: red
    for s, _ in intervals:
        cv2.line(img, (x_of(s), 20), (x_of(s), height - 20), (60, 60, 220), 1)
    # threshold
    cv2.line(img, (20, y_of(thr)), (width - 20, y_of(thr)), (200, 130, 40), 1)
    # motion curve
    pts = np.array(
        [[x_of(i / fps), y_of(float(v))] for i, v in enumerate(smoothed)], dtype=np.int32
    )
    cv2.polylines(img, [pts], False, (80, 80, 80), 1)
    # minute ticks
    for m in range(0, int(dur) + 1, 30):
        cv2.putText(img, f"{m // 60}:{m % 60:02d}", (x_of(m) - 12, height - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 0, 0), 1)

    out_png.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_png), img)
    print(f"  trace plot -> {out_png.relative_to(ROOT)}")


# ---------------------------------------------------------------------------
# Eval modes
# ---------------------------------------------------------------------------


def eval_two_sets(seg_name: str) -> None:
    truth_doc = json.loads(TWO_SETS_TRUTH.read_text(encoding="utf-8"))
    rows = truth_doc["accepted_starts"]
    truth = [r["t_start"] for r in rows]
    scoring = [r["t_start"] for r in rows if r["kind"] == "scoring"]

    info = get_motion_dual(TWO_SETS_VIDEO)
    motion, fps = info["motion"], float(info["fps"])
    intervals = SEGMENTERS[seg_name](info)
    starts = [s for s, _ in intervals]

    print(f"2_sets.mp4  seg={seg_name}  roi={info['roi_method']}  "
          f"proposed {len(intervals)} rallies (truth {len(truth)})")
    results = {
        "strict_all": report("starts vs ALL truth", truth, starts, 0.75),
        "loose_all": report("starts vs ALL truth", truth, starts, 2.0),
        "strict_scoring": report("starts vs scoring-only", scoring, starts, 0.75),
        "loose_scoring": report("starts vs scoring-only", scoring, starts, 2.0),
    }

    smoothed = smooth_motion(motion, int(round(BALANCED.smooth_window_s * fps)))
    thr = adaptive_threshold(smoothed, 70.0)
    trace_plot(OUT_DIR / f"2sets_{seg_name}_trace.png", motion, fps, thr, truth, intervals)

    out = OUT_DIR / f"2sets_{seg_name}_results.json"
    out.write_text(json.dumps({"seg": seg_name, "results": results}, indent=1),
                   encoding="utf-8", newline="\n")


def eval_corpus(seg_name: str) -> None:
    recs = [json.loads(l) for l in CORPUS_JSONL.read_text(encoding="utf-8").splitlines()]
    by_video: dict[str, list[float]] = {}
    for r in recs:
        if r["label_source"] != "dataset":
            continue
        by_video.setdefault(r["video"], []).append(r["t_event"] - PRESS_LAG_S)
    if not by_video:
        # A 0/0 recall line looks like a measurement; it isn't.
        raise SystemExit(
            "eval_corpus: no dataset events in corpus.jsonl — rebuild "
            "the corpus (build_corpus.py) before evaluating")

    totals = {"matched": 0, "truth": 0, "proposed": 0}
    for video_rel, ends_truth in sorted(by_video.items()):
        video = ROOT / video_rel
        info = get_motion_dual(video)
        intervals = SEGMENTERS[seg_name](info)
        ends_prop = [e for _, e in intervals]
        name = Path(video_rel).parent.name
        r = report(name[:28], sorted(ends_truth), ends_prop, 2.0)
        totals["matched"] += r["matched"]
        totals["truth"] += r["truth"]
        totals["proposed"] += r["proposed"]

    if not totals["truth"]:
        raise SystemExit("eval_corpus: 0 truth events across all videos "
                         "— nothing was actually evaluated")
    print(
        f"  {'TOTAL':28} recall {totals['matched']}/{totals['truth']} "
        f"= {totals['matched'] / totals['truth']:5.1%}"
        f"  precision {totals['matched']}/{totals['proposed']} "
        f"= {totals['matched'] / max(1, totals['proposed']):5.1%}"
    )


def _f1(r: float, p: float) -> float:
    return 2 * r * p / (r + p) if (r + p) else 0.0


def sweep_two_sets() -> None:
    """Grid-search segmenter configs against the 2_sets start truth.
    2_sets is thereby a TUNING video — generalization is judged on the
    corpus matches (--corpus), never on this file."""
    truth_doc = json.loads(TWO_SETS_TRUTH.read_text(encoding="utf-8"))
    truth = [r["t_start"] for r in truth_doc["accepted_starts"]]
    info = get_motion_dual(TWO_SETS_VIDEO)

    configs: list[tuple[str, dict]] = []
    for pct in (50, 60, 65, 70, 75, 80):
        for gap in (1.5, 2.0, 3.0):
            for mr in (1.5, 2.5):
                configs.append(("v0", {"percentile": pct, "min_gap_s": gap, "min_rally_s": mr}))
    for hi in (65, 70, 75, 80):
        for lo in (40, 50, 55, 60):
            for ex in (0.8, 1.2, 1.8):
                configs.append(("v1", {"pct_hi": hi, "pct_lo": lo, "exit_sustain_s": ex}))
    for lf in (0.4, 0.5, 0.6, 0.7, 0.8):
        for ex in (0.8, 1.2, 1.8):
            for dip in (0.3, 0.5):
                configs.append(("v2", {"lo_frac": lf, "exit_sustain_s": ex, "start_dip_s": dip}))
    for hi in (50, 55, 60, 65):
        for lf in (0.6, 0.75, 0.9):
            for msw in (1, 2, 3):
                for dom in (0.85, 0.9, 0.95):
                    configs.append(("v3", {
                        "pct_hi": hi, "lo_frac": lf,
                        "min_switches": msw, "dominance_max": dom,
                    }))
    for hi in (50, 55, 60, 65, 70):
        for lf in (0.6, 0.75, 0.9):
            for msw in (1, 2, 3):
                for eps in (0.25, 0.35, 0.5):
                    configs.append(("v4", {
                        "pct_hi": hi, "lo_frac": lf,
                        "min_switches": msw, "eps": eps,
                    }))

    rows = []
    for name, kw in configs:
        intervals = SEGMENTERS[name](info, **kw)
        starts = [s for s, _ in intervals]
        loose = match_events(truth, starts, 2.0)
        strict = match_events(truth, starts, 0.75)
        rl, pl = len(loose) / len(truth), (len(loose) / len(starts) if starts else 0)
        rs = len(strict) / len(truth)
        rows.append((_f1(rl, pl), rs, name, kw, len(starts)))

    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    print(f"sweep: {len(configs)} configs, truth={len(truth)} starts, top 12 by loose-F1:")
    for f1l, rs, name, kw, n in rows[:12]:
        kws = " ".join(f"{k}={v}" for k, v in kw.items())
        print(f"  F1(2s)={f1l:5.1%}  strictR={rs:5.1%}  n={n:3}  {name}  {kws}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--two-sets", action="store_true")
    parser.add_argument("--corpus", action="store_true")
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--seg", default="v0", choices=sorted(SEGMENTERS))
    args = parser.parse_args()

    if args.sweep:
        sweep_two_sets()
    if args.two_sets:
        eval_two_sets(args.seg)
    if args.corpus:
        eval_corpus(args.seg)
    if not (args.two_sets or args.corpus or args.sweep):
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
