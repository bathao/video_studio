"""scripts/spike_rally_detector.py

Phase 0 spike for auto-trim feature. CLI only, no GUI.

Runs 2 algorithms on dataset entries and compares them:
  - score_anchored: uses operator's manual score_events as rally-end
    anchors; finds rally start (first sustained motion) in each gap.
  - blanket:        global MOG2 motion thresholding, group rally
    segments, invert to trims.

Pipeline:
  ffmpeg NVDEC -> scale_cuda 480x270 -> rawvideo pipe -> numpy frame iter
  For each frame: compute frame-diff signal + MOG2 fg-pixel signal in ROI
  Apply both grouping algorithms on stored 1D signals
  Output: trims JSON, motion CSV, metrics, plot PNG

Usage:
  python scripts/spike_rally_detector.py --entry match_001_20260516_230801
  python scripts/spike_rally_detector.py --entry all
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np


# ----- Constants -----------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = REPO_ROOT / "dataset"
ROI_FILE = REPO_ROOT / "scripts" / "spike_roi.json"
OUT_DIR = REPO_ROOT / "scripts" / "spike_out"

# Downscale target — single pass through frames at this resolution.
PROC_W = 480
PROC_H = 270
PROC_FPS = 30  # frame-skip from 59.94 to 30 via ffmpeg -r

# Score-anchored algorithm parameters
LAG_CORRECTION = 0.7   # operator reaction-time when pressing A/D
TAIL_PAD = 0.5         # seconds AFTER score (cushion before trim starts)
PRE_PAD = 1.0          # seconds BEFORE detected rally start
MIN_SUSTAIN = 0.5      # seconds of motion above threshold = rally start (forward scan)
MIN_IDLE_BACKWARD = 1.5  # seconds of sustained idle (backward scan) = rally not started
MIN_TRIM_DUR = 0.5     # don't emit trims shorter than this
RALLY_DUR_AVG = 11.0   # avg rally duration from dataset (used for pre-match)
RALLY_DUR_MIN = 3.0    # shortest plausible rally (defensive prior)
RALLY_DUR_MAX = 18.0   # longest plausible rally — anything longer is dead+rally
MIN_DEAD_AFTER_SCORE = 0.5  # player needs at least this to pick up ball

# Blanket algorithm parameters
MIN_RALLY_DUR = 1.5    # rally segments shorter than this = noise
MIN_IDLE_DUR = 0.8     # idle gaps shorter than this = inside rally

# Smoothing
SMOOTH_WIN_SEC = 1.0   # moving-avg window for motion signals


# ----- Data structures -----------------------------------------------------

@dataclass
class Entry:
    slug: str
    source_path: Path
    refframe_path: Path
    groundtruth: dict
    width: int
    height: int
    fps: float
    duration: float
    score_events: list[dict]
    manual_trims: list[dict]
    roi_norm: list[list[float]]  # [[x,y]*4]


@dataclass
class Trim:
    start: float
    end: float
    kind: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class AlgoResult:
    name: str
    trims: list[Trim]
    elapsed_sec: float = 0.0
    extras: dict = field(default_factory=dict)


@dataclass
class Params:
    name: str
    signal: str = "fg"                  # "fg" | "fg_var" | "fg_periodic" | "fg_combined"
    threshold_kind: str = "fixed"       # "fixed" | "p60" | "p65" | "p70" | "p75"
    threshold_value: float = 0.04       # used when threshold_kind == "fixed"
    min_idle_backward: float = 1.5
    pre_pad: float = 1.0
    tail_pad: float = 0.5
    rally_dur_min: float = 3.0
    rally_dur_max: float = 18.0
    lag_correction: float = 0.7
    min_trim_dur: float = 0.5
    long_gap_split: bool = False
    long_gap_factor: float = 1.6        # gaps > this × avg_gap → split-search
    var_weight: float = 0.0             # variance signal weight in combined
    per_weight: float = 0.0             # periodicity signal weight in combined


@dataclass
class Signals:
    """All motion signals derived from a single video decode pass."""
    diff: np.ndarray        # raw frame-difference
    fg: np.ndarray          # MOG2 foreground ratio
    fg_smooth: np.ndarray   # smoothed fg
    fg_variance: np.ndarray # sliding-window std of fg
    fg_periodicity: np.ndarray  # 1-4Hz band power ratio of fg
    fps: int


# ----- Loading -------------------------------------------------------------

def load_entry(slug: str) -> Entry:
    base = DATASET_DIR / slug
    if not base.exists():
        raise FileNotFoundError(f"Entry not found: {base}")

    gt = json.loads((base / "groundtruth.json").read_text(encoding="utf-8"))
    sv = gt["source_video"]
    proj = gt["project"]

    source_candidates = list(base.glob("source.*"))
    source_candidates = [p for p in source_candidates if p.suffix.lower() in {".mp4", ".mov", ".mkv"}]
    if not source_candidates:
        raise FileNotFoundError(f"No source.* video in {base}")
    source_path = source_candidates[0]

    rois = json.loads(ROI_FILE.read_text(encoding="utf-8"))
    if slug not in rois:
        raise KeyError(f"No ROI for {slug} in {ROI_FILE}")
    roi_norm = rois[slug]

    return Entry(
        slug=slug,
        source_path=source_path,
        refframe_path=base / "refframe.png",
        groundtruth=gt,
        width=int(sv["width"]),
        height=int(sv["height"]),
        fps=float(sv["fps"]),
        duration=float(sv["duration_sec"]),
        score_events=proj["score_events"],
        manual_trims=proj["trim_segments"],
        roi_norm=roi_norm,
    )


# ----- ffmpeg NVDEC decode pipeline ----------------------------------------

def open_ffmpeg_pipe(source: Path, w: int, h: int, fps: int) -> subprocess.Popen:
    """ffmpeg NVDEC decode + scale_cuda downscale + rawvideo to stdout."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-hwaccel", "cuda",
        "-hwaccel_output_format", "cuda",
        "-i", str(source),
        "-vf", f"scale_cuda={w}:{h}:format=yuv420p,hwdownload,format=yuv420p,format=rgb24",
        "-r", str(fps),
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=10**8)


def iter_frames(proc: subprocess.Popen, w: int, h: int) -> Iterator[np.ndarray]:
    bytes_per_frame = w * h * 3
    while True:
        buf = proc.stdout.read(bytes_per_frame)
        if len(buf) < bytes_per_frame:
            return
        yield np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 3))


# ----- ROI mask ------------------------------------------------------------

def build_roi_mask(roi_norm: list[list[float]], w: int, h: int) -> np.ndarray:
    pts = np.array([[round(x * w), round(y * h)] for x, y in roi_norm], dtype=np.int32)
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.fillPoly(mask, [pts], 255)
    return mask


# ----- Single-pass motion extraction ---------------------------------------

def extract_motion_signals(
    entry: Entry,
    roi_mask: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, int]:
    """Single ffmpeg pass through entire source. Returns:
      - motion_diff: frame-diff mean(abs(curr-prev)) inside ROI, per frame
      - motion_fg:   MOG2 foreground pixel ratio inside ROI, per frame
      - actual_fps:  PROC_FPS (since we use ffmpeg -r)
    """
    proc = open_ffmpeg_pipe(entry.source_path, PROC_W, PROC_H, PROC_FPS)
    if proc.stdout is None:
        raise RuntimeError("ffmpeg failed to open stdout")

    bg = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=25, detectShadows=False)

    mask_bool = roi_mask > 0
    n_mask = int(mask_bool.sum())
    if n_mask == 0:
        raise ValueError("ROI mask is empty — check ROI coords")

    diff_vals: list[float] = []
    fg_vals: list[float] = []
    prev_gray: np.ndarray | None = None

    # Expected total frames at PROC_FPS — only for progress.
    total_est = int(entry.duration * PROC_FPS)
    t0 = time.time()
    last_report = t0
    i = 0

    try:
        for frame in iter_frames(proc, PROC_W, PROC_H):
            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)

            if prev_gray is None:
                diff = 0.0
            else:
                d = cv2.absdiff(gray, prev_gray)
                # mean abs diff inside ROI
                diff = float(d[mask_bool].mean())
            prev_gray = gray

            # MOG2 foreground inside ROI
            fg = bg.apply(gray)  # learnRate default
            fg_in_roi = (fg[mask_bool] > 0).sum()
            fg_ratio = fg_in_roi / n_mask

            diff_vals.append(diff)
            fg_vals.append(float(fg_ratio))

            i += 1
            now = time.time()
            if now - last_report > 1.5:
                pct = 100.0 * i / max(total_est, 1)
                speed_fps = i / max(now - t0, 0.001)
                eta = max(0.0, (total_est - i) / max(speed_fps, 1e-3))
                print(f"  [{entry.slug}] frame {i}/{total_est} ({pct:5.1f}%) "
                      f"{speed_fps:5.1f}fps eta={eta:5.0f}s", flush=True)
                last_report = now
    finally:
        if proc.poll() is None:
            try: proc.terminate()
            except Exception: pass
        stderr = proc.stderr.read().decode("utf-8", errors="ignore") if proc.stderr else ""
        if proc.wait() != 0 and stderr.strip():
            print(f"  ffmpeg stderr tail: {stderr[-400:]}", file=sys.stderr)

    elapsed = time.time() - t0
    print(f"  [{entry.slug}] decoded {i} frames in {elapsed:.1f}s "
          f"({i/elapsed:.1f}fps)", flush=True)
    return np.asarray(diff_vals, dtype=np.float32), np.asarray(fg_vals, dtype=np.float32), PROC_FPS


def smooth_signal(sig: np.ndarray, fps: int, window_sec: float) -> np.ndarray:
    win = max(1, int(round(window_sec * fps)))
    kernel = np.ones(win, dtype=np.float32) / win
    return np.convolve(sig, kernel, mode="same")


def sliding_variance(sig: np.ndarray, fps: int, window_sec: float) -> np.ndarray:
    """Sliding-window standard deviation. Rally has higher variance (rhythmic
    peaks during strokes); dead time has lower variance (steady walking)."""
    from numpy.lib.stride_tricks import sliding_window_view
    win = max(3, int(round(window_sec * fps)))
    n = len(sig)
    if n < win:
        return np.zeros(n, dtype=np.float32)
    windows = sliding_window_view(sig, win)
    stds = windows.std(axis=1).astype(np.float32)
    out = np.zeros(n, dtype=np.float32)
    pad = win // 2
    out[pad:pad + len(stds)] = stds
    # Fill edges with edge value
    out[:pad] = stds[0]
    out[pad + len(stds):] = stds[-1]
    return out


def sliding_periodicity(sig: np.ndarray, fps: int, window_sec: float = 2.5,
                        band_hz: tuple[float, float] = (1.0, 4.0)) -> np.ndarray:
    """Power in 1-4 Hz band via sliding FFT. Rally has periodic motion
    (ball-stroke rhythm ~1-2Hz, player oscillation ~0.5-1Hz); dead time
    has random/aperiodic motion → lower band power.

    Returns power normalized by total spectral energy (so loud-but-flat
    dead time doesn't dominate purely from amplitude)."""
    from numpy.lib.stride_tricks import sliding_window_view
    win = max(8, int(round(window_sec * fps)))
    n = len(sig)
    if n < win:
        return np.zeros(n, dtype=np.float32)
    windows = sliding_window_view(sig, win).astype(np.float32)
    # Remove DC per-window
    windows = windows - windows.mean(axis=1, keepdims=True)
    spectra = np.abs(np.fft.rfft(windows, axis=1))
    # Frequency bin for f Hz: idx = f * win / fps
    bin_lo = max(1, int(round(band_hz[0] * win / fps)))
    bin_hi = min(spectra.shape[1] - 1, int(round(band_hz[1] * win / fps)))
    band = spectra[:, bin_lo:bin_hi + 1].sum(axis=1)
    total = spectra[:, 1:].sum(axis=1)  # exclude DC
    ratio = np.where(total > 1e-9, band / total, 0.0).astype(np.float32)
    out = np.zeros(n, dtype=np.float32)
    pad = win // 2
    out[pad:pad + len(ratio)] = ratio
    out[:pad] = ratio[0]
    out[pad + len(ratio):] = ratio[-1]
    return out


def adaptive_threshold(sig: np.ndarray, percentile: float) -> float:
    """Per-match threshold from signal distribution. Robust to per-match
    variance in motion baseline (different lighting, ROI tightness, etc.)."""
    return float(np.percentile(sig, percentile))


# ----- Algorithm A: score-anchored -----------------------------------------

def find_first_sustained(
    motion: np.ndarray,
    fps: int,
    start_t: float,
    end_t: float,
    threshold: float,
    min_sustain_sec: float,
) -> float | None:
    """Scan motion[start_t:end_t]. Return earliest t (sec) where motion stays
    >= threshold for >= min_sustain_sec consecutively. None if not found."""
    n = len(motion)
    i0 = max(0, int(round(start_t * fps)))
    i1 = min(n, int(round(end_t * fps)))
    if i1 - i0 < 2:
        return None
    sustain_frames = max(1, int(round(min_sustain_sec * fps)))
    active = motion[i0:i1] >= threshold
    run = 0
    for j, a in enumerate(active):
        if a:
            run += 1
            if run >= sustain_frames:
                # Rally started sustain_frames-1 frames ago.
                frame_idx = i0 + j - sustain_frames + 1
                return frame_idx / fps
        else:
            run = 0
    return None


def find_rally_start_backward(
    motion: np.ndarray,
    fps: int,
    search_start_t: float,
    rally_end_t: float,
    threshold: float,
    min_idle_sec: float,
) -> float | None:
    """Walk BACKWARDS from rally_end_t to search_start_t. Return the first
    time motion stays below threshold for >= min_idle_sec consecutively —
    that's where rally hadn't started yet, so rally_start = idle_end_t.

    Rationale: rally end is anchored (score event is reliable). Rally is
    a contiguous active period ending there. Walking backward finds the
    rally→dead transition before it. More reliable than forward scan
    which trips on pre-serve activity (players walking, picking ball).
    """
    n = len(motion)
    i_end = min(n - 1, int(round(rally_end_t * fps)))
    i_start = max(0, int(round(search_start_t * fps)))
    if i_end <= i_start + 1:
        return None
    min_idle_frames = max(1, int(round(min_idle_sec * fps)))
    idle_run = 0
    # Walk backward from i_end down to i_start
    for i in range(i_end, i_start - 1, -1):
        if motion[i] < threshold:
            idle_run += 1
            if idle_run >= min_idle_frames:
                # Rally started right after the idle period ended.
                # idle period spans [i, i + idle_run - 1] (inclusive).
                # Rally start = end of idle + 1 frame
                rally_start_idx = i + idle_run
                return rally_start_idx / fps
        else:
            idle_run = 0
    return None


def find_first_sustained_idle(
    motion: np.ndarray,
    fps: int,
    search_start_t: float,
    search_end_t: float,
    threshold: float,
    min_idle_sec: float,
) -> float | None:
    """Forward scan: return earliest t where motion stays BELOW threshold
    for >= min_idle_sec consecutively (i.e., rally has ended)."""
    n = len(motion)
    i0 = max(0, int(round(search_start_t * fps)))
    i1 = min(n, int(round(search_end_t * fps)))
    if i1 - i0 < 2:
        return None
    min_idle_frames = max(1, int(round(min_idle_sec * fps)))
    idle_run = 0
    for j in range(i0, i1):
        if motion[j] < threshold:
            idle_run += 1
            if idle_run >= min_idle_frames:
                # Idle started min_idle_frames-1 frames ago
                idle_start_idx = j - min_idle_frames + 1
                return idle_start_idx / fps
        else:
            idle_run = 0
    return None


def _select_signal(signals: Signals, kind: str, p: Params) -> np.ndarray:
    """Build the working signal for detection from precomputed Signals."""
    fg = signals.fg_smooth
    if kind == "fg":
        return fg
    if kind == "fg_var":
        # Multiplicative: boost where variance is high
        v = signals.fg_variance
        v_norm = v / (np.percentile(v, 99) + 1e-9)
        return fg * (1.0 + p.var_weight * np.clip(v_norm, 0, 2))
    if kind == "fg_periodic":
        per = signals.fg_periodicity
        return fg * (1.0 + p.per_weight * per)
    if kind == "fg_combined":
        v = signals.fg_variance
        v_norm = v / (np.percentile(v, 99) + 1e-9)
        per = signals.fg_periodicity
        return fg * (1.0 + p.var_weight * np.clip(v_norm, 0, 2)) * (1.0 + p.per_weight * per)
    raise ValueError(f"unknown signal kind: {kind}")


def _resolve_threshold(sig: np.ndarray, p: Params) -> float:
    if p.threshold_kind == "fixed":
        return p.threshold_value
    pct = {"p60": 60, "p65": 65, "p70": 70, "p75": 75, "p80": 80}.get(p.threshold_kind)
    if pct is None:
        raise ValueError(f"unknown threshold_kind: {p.threshold_kind}")
    return adaptive_threshold(sig, pct)


def detect_score_anchored(
    entry: Entry,
    signals: Signals,
    p: Params,
) -> tuple[list[Trim], dict]:
    """Score-anchored detection.

    For each inter-score gap (t_prev, t_curr):
      - Rally must end at ~t_curr - LAG (score event minus operator lag).
      - Search rally start by walking motion signal BACKWARDS from rally_end.
        The first sustained-idle period encountered marks the boundary
        BEFORE rally started. This is more robust than forward scan
        because forward scan trips on pre-serve activity (players
        walking, picking ball, bouncing ball pre-serve).
      - Rally length bounded by [RALLY_DUR_MIN, RALLY_DUR_MAX] prior.
      - Trim from (t_prev - LAG + TAIL_PAD) to (rally_start - PRE_PAD).
    """
    score_times = sorted(se["timestamp"] for se in entry.score_events)
    trims: list[Trim] = []
    debug: dict = {}

    if not score_times:
        return trims, debug

    motion = _select_signal(signals, p.signal, p)
    threshold = _resolve_threshold(motion, p)
    fps = signals.fps

    debug["threshold"] = threshold
    debug["motion_p50"] = float(np.percentile(motion, 50))
    debug["motion_p75"] = float(np.percentile(motion, 75))

    # avg gap for long-gap-split heuristic
    gaps = [score_times[i] - score_times[i - 1] for i in range(1, len(score_times))]
    avg_gap = float(np.mean(gaps)) if gaps else 12.8
    debug["avg_gap"] = avg_gap

    # Pre-match: scan backward from first_score - LAG
    first = score_times[0]
    rally_end_0 = first - p.lag_correction
    floor_0 = max(0.0, rally_end_0 - p.rally_dur_max)
    ceiling_0 = rally_end_0 - p.rally_dur_min
    rally_start_0 = find_rally_start_backward(
        motion, fps, floor_0, ceiling_0, threshold, p.min_idle_backward
    )
    if rally_start_0 is None:
        rally_start_0 = floor_0
    pre_end = rally_start_0 - p.pre_pad
    if pre_end >= p.min_trim_dur:
        trims.append(Trim(0.0, pre_end, "pre_match"))

    # Inter-score gaps
    long_gap_threshold = p.long_gap_factor * avg_gap
    n_long_gaps = 0
    n_split_success = 0
    for i in range(1, len(score_times)):
        t_prev = score_times[i - 1]
        t_curr = score_times[i]
        gap_dur = t_curr - t_prev
        if gap_dur < MIN_DEAD_AFTER_SCORE + p.rally_dur_min:
            continue

        trim_start = t_prev - p.lag_correction + p.tail_pad
        rally_end = t_curr - p.lag_correction
        rally_start_floor = max(trim_start, rally_end - p.rally_dur_max)
        rally_start_ceiling = rally_end - p.rally_dur_min
        if rally_start_ceiling <= rally_start_floor:
            continue

        # Long-gap split: gap likely contains a missed-score rally + dead.
        # Try to detect a SECOND idle-active boundary deeper into the gap.
        # If found, emit 2 trims (gap_start → first_idle_end, between_rallies).
        if p.long_gap_split and gap_dur > long_gap_threshold:
            n_long_gaps += 1
            # Conjecture: gap = dead_a + rally_a + dead_b + rally_b
            # Where rally_b ends at rally_end. Find rally_a end by scanning
            # backwards from (rally_end - 2*RALLY_DUR_MIN) to floor.
            split_ceiling = rally_end - 2 * p.rally_dur_min - 1.0
            if split_ceiling > trim_start + p.rally_dur_min:
                rally_a_start = find_rally_start_backward(
                    motion, fps, trim_start, split_ceiling, threshold, p.min_idle_backward
                )
                # Find rally_b start as usual
                rally_b_start = find_rally_start_backward(
                    motion, fps, max(trim_start, rally_end - p.rally_dur_max),
                    rally_start_ceiling, threshold, p.min_idle_backward
                )
                if rally_a_start is not None and rally_b_start is not None and rally_b_start - rally_a_start > p.rally_dur_min + 1.0:
                    # Emit 2 trims
                    t1_end = rally_a_start - p.pre_pad
                    if t1_end - trim_start >= p.min_trim_dur:
                        trims.append(Trim(trim_start, t1_end, "long_gap_pre"))
                    # Between rallies: find rally_a end. Approx: take the
                    # earliest sustained idle after rally_a_start.
                    inter_idle_start = find_first_sustained_idle(
                        motion, fps, rally_a_start + p.rally_dur_min,
                        rally_b_start, threshold, p.min_idle_backward,
                    )
                    if inter_idle_start is not None and rally_b_start - p.pre_pad - inter_idle_start >= p.min_trim_dur:
                        trims.append(Trim(inter_idle_start + p.tail_pad,
                                          rally_b_start - p.pre_pad, "long_gap_mid"))
                    n_split_success += 1
                    continue

        # Normal single-rally backward scan
        rally_start = find_rally_start_backward(
            motion, fps, rally_start_floor, rally_start_ceiling, threshold, p.min_idle_backward
        )
        if rally_start is None:
            rally_start = rally_start_floor
            kind = "gap_floor"
        elif rally_start >= rally_start_ceiling - 0.05:
            kind = "gap_ceiling"
        elif rally_start <= rally_start_floor + 0.05:
            kind = "gap_floor"
        else:
            kind = "gap"

        trim_end = rally_start - p.pre_pad
        if trim_end - trim_start >= p.min_trim_dur:
            trims.append(Trim(trim_start, trim_end, kind))

    # Post-match boundary trim
    last = score_times[-1]
    post_start = last - p.lag_correction + p.tail_pad
    if entry.duration - post_start >= p.min_trim_dur:
        trims.append(Trim(post_start, entry.duration, "post_match"))

    debug["n_long_gaps"] = n_long_gaps
    debug["n_split_success"] = n_split_success
    return _merge_overlapping(trims), debug


# ----- Algorithm B: blanket MOG2 -------------------------------------------

def detect_blanket(
    entry: Entry,
    motion: np.ndarray,
    fps: int,
    threshold: float,
) -> list[Trim]:
    """Threshold motion -> group active runs into rally segments ->
    invert to trim list. Apply MIN_RALLY_DUR and MIN_IDLE_DUR filters."""
    active = motion >= threshold

    # Fill short idle gaps inside a rally
    max_gap = max(1, int(round(MIN_IDLE_DUR * fps)))
    runs = _runs(active)  # [(start_idx, end_idx_excl, is_active)]
    # Merge inactive runs shorter than max_gap into surrounding active
    merged: list[tuple[int, int, bool]] = []
    for s, e, a in runs:
        if not a and (e - s) < max_gap and merged and merged[-1][2]:
            merged[-1] = (merged[-1][0], e, True)  # extend prev active run
        else:
            if merged and merged[-1][2] == a:
                merged[-1] = (merged[-1][0], e, a)
            else:
                merged.append((s, e, a))

    # Keep only active runs longer than MIN_RALLY_DUR
    min_rally = max(1, int(round(MIN_RALLY_DUR * fps)))
    rallies = [(s, e) for s, e, a in merged if a and (e - s) >= min_rally]

    # Invert to trim list
    trims: list[Trim] = []
    cur = 0.0
    for s, e in rallies:
        rs = s / fps
        re = e / fps
        # Trim from cur to rs (with padding to keep some rally context)
        t_start = cur
        t_end = max(cur, rs - PRE_PAD)
        if t_end - t_start >= MIN_TRIM_DUR:
            trims.append(Trim(t_start, t_end, "blanket"))
        cur = min(entry.duration, re + TAIL_PAD)
    # Final trim to end of video
    if entry.duration - cur >= MIN_TRIM_DUR:
        trims.append(Trim(cur, entry.duration, "blanket_tail"))

    return _merge_overlapping(trims)


def _runs(arr: np.ndarray) -> list[tuple[int, int, bool]]:
    """Run-length: returns [(start, end_excl, value)]."""
    if len(arr) == 0:
        return []
    out: list[tuple[int, int, bool]] = []
    cur_val = bool(arr[0])
    cur_start = 0
    for i in range(1, len(arr)):
        if bool(arr[i]) != cur_val:
            out.append((cur_start, i, cur_val))
            cur_start = i
            cur_val = bool(arr[i])
    out.append((cur_start, len(arr), cur_val))
    return out


def _merge_overlapping(trims: list[Trim]) -> list[Trim]:
    if not trims:
        return trims
    trims = sorted(trims, key=lambda t: t.start)
    merged = [trims[0]]
    for t in trims[1:]:
        last = merged[-1]
        if t.start <= last.end:
            last.end = max(last.end, t.end)
        else:
            merged.append(t)
    return merged


# ----- Metrics -------------------------------------------------------------

def manual_trim_recall(auto: list[Trim], manual: list[dict]) -> dict:
    """For each manual trim, fraction covered by union of auto trims."""
    if not manual:
        return {"manual_count": 0, "covered_count": 0, "recall": None, "manual_total_sec": 0.0, "covered_sec": 0.0}
    manual_total = 0.0
    covered_total = 0.0
    fully_covered = 0
    for m in manual:
        ms, me = float(m["start"]), float(m["end"])
        manual_total += me - ms
        # union of auto-trim intersections with this manual trim
        intersections = []
        for a in auto:
            s = max(ms, a.start)
            e = min(me, a.end)
            if e > s:
                intersections.append((s, e))
        intersections.sort()
        merged_iv = []
        for s, e in intersections:
            if merged_iv and s <= merged_iv[-1][1]:
                merged_iv[-1] = (merged_iv[-1][0], max(merged_iv[-1][1], e))
            else:
                merged_iv.append((s, e))
        cov = sum(e - s for s, e in merged_iv)
        covered_total += cov
        if (me - ms) > 0 and cov / (me - ms) >= 0.5:
            fully_covered += 1
    return {
        "manual_count": len(manual),
        "covered_count": fully_covered,
        "covered_count_recall": fully_covered / len(manual),
        "manual_total_sec": manual_total,
        "covered_sec": covered_total,
        "coverage_sec_recall": (covered_total / manual_total) if manual_total else None,
    }


def extra_trim_stats(auto: list[Trim], manual: list[dict], video_dur: float) -> dict:
    """Auto-trim time NOT inside any manual trim — likely true positives
    operator missed (see memory feedback_manual_trim_is_partial)."""
    manual_intervals = sorted((float(m["start"]), float(m["end"])) for m in manual)
    extra_sec = 0.0
    extra_segments = 0
    for a in auto:
        rem_start, rem_end = a.start, a.end
        # Subtract all manual intersections
        pieces = [(rem_start, rem_end)]
        for ms, me in manual_intervals:
            new_pieces = []
            for ps, pe in pieces:
                if me <= ps or ms >= pe:
                    new_pieces.append((ps, pe))
                else:
                    if ms > ps:
                        new_pieces.append((ps, ms))
                    if me < pe:
                        new_pieces.append((me, pe))
            pieces = new_pieces
        for ps, pe in pieces:
            if pe - ps >= 0.5:
                extra_sec += pe - ps
                extra_segments += 1
    auto_total = sum(a.duration for a in auto)
    return {
        "auto_count": len(auto),
        "auto_total_sec": auto_total,
        "auto_pct_of_video": (auto_total / video_dur) if video_dur else 0,
        "extra_segments": extra_segments,
        "extra_sec": extra_sec,
        "extra_pct_of_auto": (extra_sec / auto_total) if auto_total else 0,
    }


# ----- Plot output ---------------------------------------------------------

def render_plot(
    entry: Entry,
    motion_diff: np.ndarray,
    motion_fg: np.ndarray,
    fps: int,
    threshold_diff: float,
    threshold_fg: float,
    score_results: list[Trim],
    blanket_results: list[Trim],
    out_path: Path,
) -> None:
    """Render a single PNG showing motion signals + score events + trims."""
    W, H = 1800, 600
    img = np.full((H, W, 3), 30, dtype=np.uint8)

    dur = entry.duration
    def x_of(t: float) -> int:
        return int(80 + (W - 100) * (t / dur))

    # Title
    cv2.putText(img, f"{entry.slug}  dur={dur:.1f}s  scores={len(entry.score_events)}",
                (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (220, 220, 220), 1, cv2.LINE_AA)

    # Layout (top -> bottom):
    #   Row A: motion_diff curve + threshold
    #   Row B: motion_fg curve + threshold
    #   Row C: bands - manual trims (red), score-anchored (green), blanket (blue)
    rowA_top, rowA_bot = 50, 200
    rowB_top, rowB_bot = 220, 370
    bandY = 410
    bandH = 24

    def draw_curve(sig: np.ndarray, top: int, bot: int, color, label: str, threshold: float):
        cv2.putText(img, label, (4, top + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
        # axes
        cv2.line(img, (80, bot), (W - 20, bot), (80, 80, 80), 1)
        cv2.line(img, (80, top), (80, bot), (80, 80, 80), 1)
        # downsample to W pixels
        n_pts = W - 100
        idxs = np.linspace(0, len(sig) - 1, n_pts).astype(np.int64)
        sample = sig[idxs]
        ymax = float(sample.max()) if sample.size else 1.0
        if ymax <= 0:
            ymax = 1.0
        prev_pt = None
        for k, v in enumerate(sample):
            x = 80 + k
            y = bot - int((v / ymax) * (bot - top))
            if prev_pt is not None:
                cv2.line(img, prev_pt, (x, y), color, 1, cv2.LINE_AA)
            prev_pt = (x, y)
        # threshold line
        ty = bot - int((threshold / ymax) * (bot - top))
        cv2.line(img, (80, ty), (W - 20, ty), (180, 180, 80), 1, cv2.LINE_4)
        cv2.putText(img, f"thr={threshold:.2f}  max={ymax:.2f}",
                    (W - 220, top + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (180, 180, 80), 1, cv2.LINE_AA)

    draw_curve(motion_diff, rowA_top, rowA_bot, (160, 220, 160), "motion_diff (score-anchored)", threshold_diff)
    draw_curve(motion_fg, rowB_top, rowB_bot, (220, 160, 160), "motion_fg (blanket MOG2)", threshold_fg)

    # Score events as vertical dotted lines on row A
    for se in entry.score_events:
        t = float(se["timestamp"])
        x = x_of(t)
        for y in range(rowA_top, rowA_bot, 4):
            img[y:y+2, x:x+1] = (120, 180, 255)

    # Trim bands
    def band(top: int, height: int, segs, color, label: str):
        cv2.rectangle(img, (80, top), (W - 20, top + height), (60, 60, 60), -1)
        cv2.putText(img, label, (4, top + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
        for s in segs:
            if isinstance(s, dict):
                x1, x2 = x_of(s["start"]), x_of(s["end"])
            else:
                x1, x2 = x_of(s.start), x_of(s.end)
            cv2.rectangle(img, (x1, top), (max(x1 + 1, x2), top + height), color, -1)

    band(bandY, bandH, entry.manual_trims, (60, 60, 220), "manual trims")
    band(bandY + bandH + 6, bandH, score_results, (60, 200, 60), "auto: score-anchored")
    band(bandY + 2 * (bandH + 6), bandH, blanket_results, (220, 140, 60), "auto: blanket MOG2")

    # X-axis labels (minute marks)
    for mm in range(0, int(dur) + 1, 60):
        x = x_of(mm)
        cv2.line(img, (x, H - 30), (x, H - 22), (180, 180, 180), 1)
        cv2.putText(img, f"{mm//60}m", (x - 8, H - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (180, 180, 180), 1, cv2.LINE_AA)

    cv2.imwrite(str(out_path), img)


# ----- Debug overlay video -------------------------------------------------

def render_overlay_video(
    entry: Entry,
    signals: Signals,
    threshold_used: float,
    best_trims: list[Trim],
    sample_start: float,
    sample_dur: float,
    out_path: Path,
    overlay_w: int = 960,
    overlay_h: int = 540,
) -> None:
    """Render a debug overlay video for a sample range of the source.

    Shows: source frame + ROI polygon + motion meters + classification bar
    + score event markers + manual/auto trim coverage indicator.
    """
    src = entry.source_path
    fps_out = 30
    sample_end = min(entry.duration, sample_start + sample_dur)
    n_frames = int(round((sample_end - sample_start) * fps_out))

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
        "-ss", f"{sample_start:.3f}",
        "-i", str(src),
        "-t", f"{sample_dur:.3f}",
        "-vf", f"scale_cuda={overlay_w}:{overlay_h}:format=yuv420p,hwdownload,format=yuv420p,format=rgb24",
        "-r", str(fps_out),
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=10**8)

    # cv2 VideoWriter for output
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(out_path), fourcc, fps_out, (overlay_w, overlay_h))

    # Pre-build ROI polygon in overlay pixel space
    roi_pts = np.array(
        [[round(x * overlay_w), round(y * overlay_h)] for x, y in entry.roi_norm],
        dtype=np.int32,
    )

    motion = signals.fg_smooth
    motion_max = float(np.percentile(motion, 99))

    score_times = sorted(se["timestamp"] for se in entry.score_events)
    manual_intervals = sorted((float(m["start"]), float(m["end"])) for m in entry.manual_trims)

    bytes_per_frame = overlay_w * overlay_h * 3
    frame_idx = 0
    try:
        while frame_idx < n_frames:
            buf = proc.stdout.read(bytes_per_frame)
            if len(buf) < bytes_per_frame:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape((overlay_h, overlay_w, 3)).copy()
            t = sample_start + frame_idx / fps_out

            # ROI polygon overlay (translucent fill + bright outline)
            overlay = frame.copy()
            cv2.fillPoly(overlay, [roi_pts], (0, 80, 0))
            cv2.addWeighted(overlay, 0.18, frame, 0.82, 0, dst=frame)
            cv2.polylines(frame, [roi_pts], True, (0, 255, 0), 2)

            # Find current state: in manual? in auto? rally?
            in_manual = any(ms <= t <= me for ms, me in manual_intervals)
            in_auto = any(tr.start <= t <= tr.end for tr in best_trims)

            # State indicator bar at top
            state_color = (200, 200, 200)
            state_label = "RALLY"
            if in_manual and in_auto:
                state_color = (0, 200, 200); state_label = "DEAD (manual+auto)"
            elif in_manual:
                state_color = (60, 60, 220); state_label = "DEAD (manual only)"
            elif in_auto:
                state_color = (60, 220, 60); state_label = "DEAD (auto only)"

            cv2.rectangle(frame, (0, 0), (overlay_w, 30), state_color, -1)
            cv2.putText(frame, state_label, (10, 22), cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, (20, 20, 20), 2, cv2.LINE_AA)

            # Timestamp + threshold + motion
            sig_idx = min(len(motion) - 1, int(t * signals.fps))
            mv = float(motion[sig_idx])
            ts_str = f"t={t:6.1f}s  motion={mv:.4f}  thr={threshold_used:.4f}"
            cv2.putText(frame, ts_str, (10, 55), cv2.FONT_HERSHEY_SIMPLEX,
                        0.55, (255, 255, 255), 2, cv2.LINE_AA)

            # Motion meter (horizontal bar)
            bar_y = overlay_h - 60
            bar_h = 22
            cv2.rectangle(frame, (10, bar_y), (overlay_w - 10, bar_y + bar_h),
                          (40, 40, 40), -1)
            fill_w = int((overlay_w - 20) * min(1.0, mv / motion_max))
            mcolor = (0, 220, 0) if mv >= threshold_used else (220, 80, 80)
            cv2.rectangle(frame, (10, bar_y), (10 + fill_w, bar_y + bar_h),
                          mcolor, -1)
            # Threshold mark
            tmark = 10 + int((overlay_w - 20) * (threshold_used / motion_max))
            cv2.line(frame, (tmark, bar_y - 3), (tmark, bar_y + bar_h + 3),
                     (255, 255, 0), 2)
            cv2.putText(frame, "motion", (10, bar_y - 5), cv2.FONT_HERSHEY_SIMPLEX,
                        0.4, (200, 200, 200), 1, cv2.LINE_AA)

            # Timeline strip at bottom showing sample range + markers
            tl_y = overlay_h - 28
            tl_h = 18
            cv2.rectangle(frame, (10, tl_y), (overlay_w - 10, tl_y + tl_h),
                          (50, 50, 50), -1)
            def x_of(tt):
                return 10 + int((overlay_w - 20) * (tt - sample_start) / sample_dur)
            # Manual trim regions
            for ms, me in manual_intervals:
                s = max(ms, sample_start); e = min(me, sample_end)
                if e > s:
                    cv2.rectangle(frame, (x_of(s), tl_y), (x_of(e), tl_y + tl_h // 2),
                                  (60, 60, 220), -1)
            # Auto trims
            for tr in best_trims:
                s = max(tr.start, sample_start); e = min(tr.end, sample_end)
                if e > s:
                    cv2.rectangle(frame, (x_of(s), tl_y + tl_h // 2), (x_of(e), tl_y + tl_h),
                                  (60, 220, 60), -1)
            # Score event markers
            for st in score_times:
                if sample_start <= st <= sample_end:
                    x = x_of(st)
                    cv2.line(frame, (x, tl_y - 3), (x, tl_y + tl_h + 3),
                             (255, 200, 100), 1)
            # Time cursor
            cx = x_of(t)
            cv2.line(frame, (cx, tl_y - 3), (cx, tl_y + tl_h + 3),
                     (255, 255, 255), 2)

            # Legend
            cv2.putText(frame, "manual=blue  auto=green  score=orange",
                        (10, overlay_h - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                        (200, 200, 200), 1, cv2.LINE_AA)

            # Convert RGB->BGR for cv2 writer
            writer.write(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
            frame_idx += 1
    finally:
        writer.release()
        if proc.poll() is None:
            try: proc.terminate()
            except Exception: pass
        proc.wait()


# ----- Per-entry driver ----------------------------------------------------

def process_entry(entry: Entry, threshold_diff: float, threshold_fg: float) -> dict:
    print(f"\n=== {entry.slug} ===")
    print(f"  source: {entry.source_path.name} ({entry.width}x{entry.height} @ {entry.fps:.2f}fps {entry.duration:.1f}s)")
    print(f"  score_events={len(entry.score_events)}  manual_trims={len(entry.manual_trims)}")
    print(f"  roi (norm)={entry.roi_norm}")

    roi_mask = build_roi_mask(entry.roi_norm, PROC_W, PROC_H)
    n_mask = int((roi_mask > 0).sum())
    print(f"  roi mask: {n_mask}/{PROC_W*PROC_H} px ({100*n_mask/(PROC_W*PROC_H):.1f}% of frame)")

    # Cache motion signals — decode is 100s, signals only depend on (source mtime, ROI).
    cache_key = f"{entry.slug}_W{PROC_W}xH{PROC_H}f{PROC_FPS}_roi{hash(tuple(map(tuple, entry.roi_norm))) & 0xffffffff:08x}"
    cache_path = OUT_DIR / f"_cache_{cache_key}.npz"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    src_mtime = entry.source_path.stat().st_mtime
    if cache_path.exists():
        cached = np.load(cache_path)
        if abs(float(cached["src_mtime"]) - src_mtime) < 1.0:
            motion_diff_raw = cached["diff"]
            motion_fg_raw = cached["fg"]
            fps = int(cached["fps"])
            print(f"  motion cache hit: {cache_path.name} ({len(motion_diff_raw)} frames)")
        else:
            cache_path.unlink()
            print(f"  motion cache stale (source mtime drift) — re-extracting")
            motion_diff_raw, motion_fg_raw, fps = extract_motion_signals(entry, roi_mask)
            np.savez(cache_path, diff=motion_diff_raw, fg=motion_fg_raw, fps=fps, src_mtime=src_mtime)
    else:
        print(f"  decoding @ {PROC_W}x{PROC_H}@{PROC_FPS}fps via NVDEC...")
        motion_diff_raw, motion_fg_raw, fps = extract_motion_signals(entry, roi_mask)
        np.savez(cache_path, diff=motion_diff_raw, fg=motion_fg_raw, fps=fps, src_mtime=src_mtime)

    motion_diff = smooth_signal(motion_diff_raw, fps, SMOOTH_WIN_SEC)
    motion_fg_smooth = smooth_signal(motion_fg_raw, fps, SMOOTH_WIN_SEC)
    motion_fg_var = sliding_variance(motion_fg_raw, fps, window_sec=2.0)
    motion_fg_per = sliding_periodicity(motion_fg_raw, fps, window_sec=2.5, band_hz=(1.0, 4.0))

    print(f"  motion_diff: mean={motion_diff.mean():.3f} max={motion_diff.max():.3f}")
    print(f"  motion_fg:   mean={motion_fg_smooth.mean():.3f} max={motion_fg_smooth.max():.3f}")
    print(f"  fg variance: mean={motion_fg_var.mean():.4f} max={motion_fg_var.max():.4f}")
    print(f"  fg periodic: mean={motion_fg_per.mean():.4f} max={motion_fg_per.max():.4f}")

    signals = Signals(
        diff=motion_diff,
        fg=motion_fg_raw,
        fg_smooth=motion_fg_smooth,
        fg_variance=motion_fg_var,
        fg_periodicity=motion_fg_per,
        fps=fps,
    )

    # Variant sweep
    variants = build_variant_list(threshold_fg)
    print(f"\n  --- sweeping {len(variants)} variants ---")

    variant_results = []
    for p in variants:
        t0 = time.time()
        trims, dbg = detect_score_anchored(entry, signals, p)
        elapsed = time.time() - t0
        rec = manual_trim_recall(trims, entry.manual_trims)
        ext = extra_trim_stats(trims, entry.manual_trims, entry.duration)
        variant_results.append({
            "params": asdict(p),
            "n_trims": len(trims),
            "elapsed_ms": int(elapsed * 1000),
            "threshold_used": dbg.get("threshold"),
            "recall": rec,
            "extras": ext,
            "debug": dbg,
            "trims": [asdict(t) for t in trims],
        })
        score = _variant_score(rec, ext)
        rec_s = rec["coverage_sec_recall"]
        rec_str = f"{rec_s*100:5.1f}%" if rec_s is not None else "  N/A"
        print(f"    {p.name:30}  trims={len(trims):3}  thr={dbg.get('threshold', 0):.4f}  "
              f"recall={rec_str}  extras={ext['extra_sec']:5.0f}s  score={score:6.2f}")

    # Pick best variant by combined score (recall weight 1.0, penalize extras)
    best = max(variant_results, key=lambda r: _variant_score(r["recall"], r["extras"]))
    print(f"  BEST: {best['params']['name']}")

    # Blanket baseline
    t0 = time.time()
    trims_bl = detect_blanket(entry, signals.fg_smooth, fps, threshold_fg)
    elapsed_bl = time.time() - t0
    bl_recall = manual_trim_recall(trims_bl, entry.manual_trims)
    bl_extras = extra_trim_stats(trims_bl, entry.manual_trims, entry.duration)
    print(f"    {'blanket_baseline':30}  trims={len(trims_bl):3}  thr={threshold_fg:.4f}  "
          f"recall={bl_recall['coverage_sec_recall']*100 if bl_recall['coverage_sec_recall'] else 0:5.1f}%  "
          f"extras={bl_extras['extra_sec']:5.0f}s")

    # Outputs
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_csv = OUT_DIR / f"{entry.slug}_motion.csv"
    out_plot = OUT_DIR / f"{entry.slug}_plot.png"
    out_variants = OUT_DIR / f"{entry.slug}_variants.json"
    out_best_trims = OUT_DIR / f"{entry.slug}_best_trims.json"
    out_metrics = OUT_DIR / f"{entry.slug}_metrics.txt"

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["frame", "time_sec", "motion_diff", "motion_fg", "motion_fg_var", "motion_fg_per"])
        step = max(1, fps // 10)
        for i in range(0, len(motion_diff), step):  # ~10 samples/s
            w.writerow([i, f"{i/fps:.3f}",
                        f"{motion_diff[i]:.4f}",
                        f"{motion_fg_smooth[i]:.4f}",
                        f"{motion_fg_var[i]:.4f}",
                        f"{motion_fg_per[i]:.4f}"])

    json.dump(variant_results, out_variants.open("w", encoding="utf-8"), indent=2, ensure_ascii=False)
    json.dump(best["trims"], out_best_trims.open("w", encoding="utf-8"), indent=2, ensure_ascii=False)

    # Plot: best variant vs blanket vs manual
    best_trims = [Trim(t["start"], t["end"], t.get("kind", "")) for t in best["trims"]]
    render_plot(
        entry, motion_diff, motion_fg_smooth, fps,
        threshold_fg, threshold_fg, best_trims, trims_bl,
        out_plot,
    )

    summary = {
        "slug": entry.slug,
        "duration_sec": entry.duration,
        "score_events": len(entry.score_events),
        "manual_trims": len(entry.manual_trims),
        "manual_trim_sec": sum(float(m["end"]) - float(m["start"]) for m in entry.manual_trims),
        "best_variant": best["params"]["name"],
        "best_recall": best["recall"],
        "best_extras": best["extras"],
        "blanket_recall": bl_recall,
        "blanket_extras": bl_extras,
        "all_variants": [
            {"name": v["params"]["name"],
             "recall_coverage": v["recall"]["coverage_sec_recall"],
             "extras_sec": v["extras"]["extra_sec"],
             "n_trims": v["n_trims"],
             "threshold_used": v["threshold_used"],
             "score": _variant_score(v["recall"], v["extras"]),
             }
            for v in variant_results
        ],
    }
    out_metrics.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"  -> {out_variants.name}")
    print(f"  -> {out_best_trims.name}")
    print(f"  -> {out_csv.name}")
    print(f"  -> {out_plot.name}")
    print(f"  -> {out_metrics.name}")

    # Debug overlay video: 60s sample around a manual trim (if any), else near middle
    if entry.manual_trims:
        m = max(entry.manual_trims, key=lambda x: x["end"] - x["start"])
        sample_start = max(0.0, float(m["start"]) - 20.0)
    else:
        sample_start = entry.duration / 2 - 30
    sample_dur = 60.0
    out_overlay = OUT_DIR / f"{entry.slug}_overlay.mp4"
    print(f"  rendering overlay video [{sample_start:.0f}-{sample_start+sample_dur:.0f}s]...")
    try:
        render_overlay_video(
            entry, signals, best["threshold_used"], best_trims,
            sample_start, sample_dur, out_overlay,
        )
        print(f"  -> {out_overlay.name}")
    except Exception as e:
        print(f"  overlay render FAILED: {e}")

    return summary


def _variant_score(recall: dict, extras: dict) -> float:
    """Combined utility score for ranking variants.

    Want: high recall, low extras (relative to physical max dead time).
    Physical max dead = duration * 0.4 (~40% of video, an upper bound).

    score = recall_coverage - 0.25 * normalized_extras
    where normalized_extras is extras_sec / manual_total_sec (so 0 means
    no extras, 1.0 means extras equal manual trim total).

    Recall coverage range [0, 1]. Normalized extras typically 0-10.
    """
    r = recall.get("coverage_sec_recall") or 0.0
    m_total = recall.get("manual_total_sec") or 1.0
    norm_ext = extras["extra_sec"] / max(m_total, 1.0)
    return r - 0.10 * norm_ext


def build_variant_list(default_threshold_fg: float) -> list[Params]:
    """Define algorithm variants to sweep. Each row is one configuration.

    Naming: <signal>_<threshold>_<extras>
        signal:    fg / fg_var / fg_per / fg_combined
        threshold: thr0.03..0.05 / p60..p80
        extras:    misc flags (sg=split-gap, idle1.5/2.0, etc.)
    """
    out: list[Params] = []
    # A1: baseline (current locked params)
    out.append(Params(name="A_baseline_fg_thr04", signal="fg", threshold_kind="fixed",
                      threshold_value=default_threshold_fg, min_idle_backward=1.5))
    # A2: tighter idle
    out.append(Params(name="A_baseline_fg_thr04_idle2", signal="fg", threshold_kind="fixed",
                      threshold_value=default_threshold_fg, min_idle_backward=2.0))
    # B: adaptive thresholds on fg
    for pct in ["p60", "p65", "p70", "p75"]:
        out.append(Params(name=f"B_fg_{pct}", signal="fg", threshold_kind=pct,
                          min_idle_backward=1.5))
    # C: variance-weighted
    out.append(Params(name="C_fg_var_p65_v0.5", signal="fg_var", threshold_kind="p65",
                      min_idle_backward=1.5, var_weight=0.5))
    out.append(Params(name="C_fg_var_p65_v1.0", signal="fg_var", threshold_kind="p65",
                      min_idle_backward=1.5, var_weight=1.0))
    # D: periodicity-weighted
    out.append(Params(name="D_fg_per_p65_w1.0", signal="fg_periodic", threshold_kind="p65",
                      min_idle_backward=1.5, per_weight=1.0))
    out.append(Params(name="D_fg_per_p70_w2.0", signal="fg_periodic", threshold_kind="p70",
                      min_idle_backward=1.5, per_weight=2.0))
    # E: combined variance + periodicity
    out.append(Params(name="E_combined_p65_v0.5_p1.0", signal="fg_combined",
                      threshold_kind="p65", min_idle_backward=1.5,
                      var_weight=0.5, per_weight=1.0))
    out.append(Params(name="E_combined_p70_v1.0_p2.0", signal="fg_combined",
                      threshold_kind="p70", min_idle_backward=1.5,
                      var_weight=1.0, per_weight=2.0))
    # F: best + long-gap split
    out.append(Params(name="F_fg_p65_split", signal="fg", threshold_kind="p65",
                      min_idle_backward=1.5, long_gap_split=True))
    out.append(Params(name="F_combined_p70_split", signal="fg_combined",
                      threshold_kind="p70", min_idle_backward=1.5,
                      var_weight=1.0, per_weight=2.0, long_gap_split=True))
    # G: tighter rally-duration prior (assume rally is ≥5s, ≥7s, ≥9s)
    # Useful when motion signal is too noisy — rely more on duration prior.
    out.append(Params(name="G_fg_p65_rmin5", signal="fg", threshold_kind="p65",
                      min_idle_backward=1.5, rally_dur_min=5.0))
    out.append(Params(name="G_fg_p65_rmin7", signal="fg", threshold_kind="p65",
                      min_idle_backward=1.5, rally_dur_min=7.0))
    out.append(Params(name="G_fg_p65_rmin9", signal="fg", threshold_kind="p65",
                      min_idle_backward=1.5, rally_dur_min=9.0))
    # H: tighter rally min + adaptive threshold + split
    out.append(Params(name="H_fg_p70_rmin7_split", signal="fg", threshold_kind="p70",
                      min_idle_backward=1.5, rally_dur_min=7.0, long_gap_split=True))
    out.append(Params(name="H_combined_p70_rmin7", signal="fg_combined",
                      threshold_kind="p70", min_idle_backward=1.5,
                      rally_dur_min=7.0, var_weight=1.0, per_weight=2.0))
    # I: prior-only (no motion); fixes rally length to avg → trim is what remains
    out.append(Params(name="I_prior_only_r11", signal="fg", threshold_kind="fixed",
                      threshold_value=999.0,  # never trips → always fallback to floor
                      min_idle_backward=1.5, rally_dur_min=10.5, rally_dur_max=11.5))
    out.append(Params(name="I_prior_only_r10", signal="fg", threshold_kind="fixed",
                      threshold_value=999.0,
                      min_idle_backward=1.5, rally_dur_min=9.5, rally_dur_max=10.5))
    # J: G_rmin5 sweet-spot refinement — try rmin 4,5,6 with different signals
    out.append(Params(name="J_fg_p65_rmin4", signal="fg", threshold_kind="p65",
                      min_idle_backward=1.5, rally_dur_min=4.0))
    out.append(Params(name="J_fg_p65_rmin6", signal="fg", threshold_kind="p65",
                      min_idle_backward=1.5, rally_dur_min=6.0))
    out.append(Params(name="J_fg_p70_rmin5", signal="fg", threshold_kind="p70",
                      min_idle_backward=1.5, rally_dur_min=5.0))
    out.append(Params(name="J_per_p70_rmin5", signal="fg_periodic", threshold_kind="p70",
                      min_idle_backward=1.5, rally_dur_min=5.0, per_weight=2.0))
    out.append(Params(name="J_combined_p70_rmin5", signal="fg_combined", threshold_kind="p70",
                      min_idle_backward=1.5, rally_dur_min=5.0, var_weight=1.0, per_weight=2.0))
    out.append(Params(name="J_combined_p70_rmin6", signal="fg_combined", threshold_kind="p70",
                      min_idle_backward=1.5, rally_dur_min=6.0, var_weight=1.0, per_weight=2.0))
    # K: rmin5 + tighter rmax (assume rally is 5-15s, not 5-18s)
    out.append(Params(name="K_fg_p65_rmin5_rmax15", signal="fg", threshold_kind="p65",
                      min_idle_backward=1.5, rally_dur_min=5.0, rally_dur_max=15.0))
    out.append(Params(name="K_fg_p65_rmin5_rmax13", signal="fg", threshold_kind="p65",
                      min_idle_backward=1.5, rally_dur_min=5.0, rally_dur_max=13.0))
    # L: rmin5 + idle tuning
    out.append(Params(name="L_fg_p65_rmin5_idle2.5", signal="fg", threshold_kind="p65",
                      min_idle_backward=2.5, rally_dur_min=5.0))
    out.append(Params(name="L_fg_p65_rmin5_idle3.5", signal="fg", threshold_kind="p65",
                      min_idle_backward=3.5, rally_dur_min=5.0))
    return out


# ----- main ----------------------------------------------------------------

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument("--entry", required=True, help="dataset slug or 'all'")
    p.add_argument("--threshold-diff", type=float, default=5.0,
                   help="motion_diff threshold (score-anchored). Default 5.0")
    p.add_argument("--threshold-fg", type=float, default=0.03,
                   help="motion_fg threshold (blanket MOG2 ratio). Default 0.03")
    args = p.parse_args(argv)

    if args.entry == "all":
        manifest = json.loads((DATASET_DIR / "manifest.json").read_text(encoding="utf-8"))
        slugs = [e["slug"] for e in manifest["entries"]]
    else:
        slugs = [args.entry]

    summaries = []
    for slug in slugs:
        entry = load_entry(slug)
        summary = process_entry(entry, args.threshold_diff, args.threshold_fg)
        summaries.append(summary)

    # Aggregate report
    if len(summaries) > 1:
        agg_path = OUT_DIR / "_summary.json"
        agg_path.write_text(json.dumps(summaries, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n=== Aggregate ===")
        print(f"  -> {agg_path}")
        for s in summaries:
            sa = s["score_anchored"]
            bl = s["blanket"]
            print(f"  {s['slug'][-15:]}: "
                  f"SA recall={sa['recall']['coverage_sec_recall']!r} extras={sa['extras']['extra_sec']:.0f}s  | "
                  f"BL recall={bl['recall']['coverage_sec_recall']!r} extras={bl['extras']['extra_sec']:.0f}s")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
