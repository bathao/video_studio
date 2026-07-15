"""Rally / dead-time detector for Auto Trim.

Score-event-anchored detector — between any two consecutive operator
score-presses there is exactly one rally that ended at the SECOND press
(modulo press lag). The detector locates the START of that rally inside
the gap, then emits a TrimSegment covering the dead time between the
prior rally's tail and the next rally's pre-roll.

Algorithm sourced from `docs/spike_archive/PHASE0_REPORT.md`'s Balanced
preset (`J_fg_p70_rmin5`). The spike script that produced those numbers
was deleted in 37b474a; the report is the authoritative spec.

Pipeline shape:

    decode NVDEC → 480x270 RGB → frame-diff motion → 0.5s moving avg
        → adaptive p70 threshold → score-anchored backward-scan
        → rally_dur_min gate → trim emission

Public API:

    run_rally_detection(video_path, roi_corners, score_events,
                        params=BALANCED, cancel_check=lambda: False,
                        emit=lambda *a: None) -> list[TrimSegment]

    gaps_to_trims(score_events, motion, fps, threshold, duration,
                  params) -> list[TrimSegment]
        # Pure logic, no ffmpeg / no cv2 — unit-testable.

The two pieces are split so Step 2 (SSE orchestration) can mock decode
and so tests can drive synthetic motion arrays. NOTHING in this module
talks to the FastAPI layer — that's the next step.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

import numpy as np

from .config import config
from .ffmpeg_runner import probe_video
from .models import ScoreEvent, TrimSegment


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RallyDetectorParams:
    """All tunables in one place. Defaults are the PHASE0_REPORT Balanced
    preset (`J_fg_p70_rmin5`): recall 92-98% vs. operator-marked manual
    trims, extras 276-323s per ~20-22 min source.

    Time fields are seconds; window fields are seconds (converted to
    sample counts internally so the same params work at any output fps)."""

    # Decode + motion signal
    decode_width: int = 480
    decode_height: int = 270
    decode_fps: int = 30
    smooth_window_s: float = 0.5

    # Threshold
    threshold_percentile: float = 70.0

    # Score-event-anchored gap detection
    anchor_offset_s: float = 0.5       # backward-scan anchor = t_{i+1} - this
    idle_sustain_s: float = 1.5        # need this much continuous idle to confirm rally boundary
    rally_dur_min_s: float = 5.0       # detected rally must be at least this long; else push start back
    score_press_lag_s: float = 0.7     # operator presses ~this long after the rally actually ends
    post_rally_tail_s: float = 0.5     # keep this much video after the actual rally end (crowd reaction)
    pre_rally_pad_s: float = 1.0       # keep this much video before the next rally starts (serve toss)
    post_match_keep_s: float = 30.0    # keep this much past the last score event — handshake + final scoreboard freeze

    # Output guards
    min_trim_dur_s: float = 0.5        # drop trims shorter than this — review burden > benefit


BALANCED = RallyDetectorParams()


# ---------------------------------------------------------------------------
# ROI mask
# ---------------------------------------------------------------------------


def build_roi_mask(corners: Sequence[Sequence[float]], width: int, height: int) -> np.ndarray:
    """Rasterize a 4-corner quad (normalized [0, 1]) into a uint8 0/1 mask
    at decode resolution. Returned mask has shape (height, width)."""
    import cv2  # local import — keeps pure-logic tests cv2-free
    if len(corners) != 4:
        raise ValueError(f"ROI must have 4 corners, got {len(corners)}")
    pts = np.array(
        [[round(x * (width - 1)), round(y * (height - 1))] for x, y in corners],
        dtype=np.int32,
    )
    mask = np.zeros((height, width), dtype=np.uint8)
    cv2.fillPoly(mask, [pts], 1)
    return mask


# ---------------------------------------------------------------------------
# Decode + motion signal
# ---------------------------------------------------------------------------


def _build_decode_cmd(
    video_path: Path,
    params: RallyDetectorParams,
    *,
    use_hwaccel: bool,
) -> list[str]:
    """Decode → fixed-size rgb24 raw frames on stdout.

    `use_hwaccel=True` offloads only the (expensive) HEVC/H.264 decode to
    NVDEC via `-hwaccel cuda`; the decoded frame is auto-downloaded to
    system memory and scaled by the SAME CPU swscale filter as the
    fallback path. HEVC reconstruction is bit-exact per spec, so the
    rgb24 bytes are byte-identical to the CPU path (verified by sha1 over
    the first 1200+ frames of all 3 spike sources) — only the decode is
    faster (~80 → ~220 fps = 2.6× → 7.5× realtime on RTX 5060 Ti).

    NOTE: an earlier version did GPU-side scaling
    (`-hwaccel_output_format cuda` + `scale_cuda`). That both FAILED on
    this ffmpeg build ("Could not open encoder" — scale_cuda unavailable,
    so every detect silently fell back to pure CPU decode) AND would have
    changed pixels (GPU resampler ≠ swscale). Keeping swscale is what lets
    the GPU-decode path stay a byte-identical drop-in.

    The trailing fps filter resamples to a fixed output rate so motion
    signal indices map deterministically to seconds regardless of source
    fps (59.94 → 30, 30 → 30, etc.).

    `use_hwaccel=False` is the CPU decode fallback for environments
    without CUDA (CI, headless servers). Slower but byte-equivalent."""
    w, h, fps = params.decode_width, params.decode_height, params.decode_fps
    hwaccel = ["-hwaccel", "cuda"] if use_hwaccel else []
    return [
        config.ffmpeg, "-hide_banner", "-loglevel", "error",
        *hwaccel,
        "-i", str(video_path),
        "-vf", f"scale={w}:{h},fps={fps}",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-",
    ]


def _iter_frames(
    video_path: Path,
    params: RallyDetectorParams,
    *,
    use_hwaccel: bool = True,
) -> Iterable[np.ndarray]:
    """Yield successive 480x270x3 uint8 RGB frames from ffmpeg's stdout.

    Falls back to CPU decode if the NVDEC pipeline fails on first read
    (no CUDA / driver mismatch / older GPU). Frames are reused-buffer
    safe — callers must copy if they want to retain a frame past the
    next iteration (we don't — motion diff just needs prev + curr)."""
    w, h = params.decode_width, params.decode_height
    frame_bytes = w * h * 3

    cmd = _build_decode_cmd(video_path, params, use_hwaccel=use_hwaccel)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        first = proc.stdout.read(frame_bytes)
        if len(first) < frame_bytes:
            # NVDEC failed — process exited without producing a full frame.
            stderr = proc.stderr.read().decode("utf-8", errors="replace")
            proc.wait()
            if use_hwaccel:
                # Retry once on the CPU path so the detector works on
                # boxes without working NVDEC.
                yield from _iter_frames(video_path, params, use_hwaccel=False)
                return
            raise RuntimeError(f"ffmpeg decode produced no frames: {stderr[-500:]}")
        yield np.frombuffer(first, dtype=np.uint8).reshape(h, w, 3)

        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)

        # A short read is either clean EOF or a mid-stream decoder
        # abort (corrupt GOP, hwaccel fault) — stdout looks identical
        # in both cases, only the exit code tells them apart. Treating
        # an abort as EOF would let detection "succeed" over a prefix
        # of the match and apply trims computed from half the video.
        stderr_tail = b""
        try:
            stderr_tail = proc.stderr.read() or b""
        except Exception:
            pass
        rc = proc.wait(timeout=5)
        if rc != 0:
            raise RuntimeError(
                f"ffmpeg decode aborted mid-stream (exit {rc}): "
                + stderr_tail.decode("utf-8", errors="replace")[-500:])
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait(timeout=5)


def compute_motion_signal(
    video_path: Path,
    roi_corners: Sequence[Sequence[float]],
    params: RallyDetectorParams = BALANCED,
    *,
    cancel_check: Callable[[], bool] = lambda: False,
    on_progress: Optional[Callable[[int, int], None]] = None,
    duration_hint: Optional[float] = None,
) -> np.ndarray:
    """Decode the video, compute per-frame ROI motion. Returns a 1-D
    float32 array of length = (n_frames - 1) where motion[i] is the
    mean absolute pixel-diff (in [0, 1]) inside the ROI between frames
    i and i+1.

    `on_progress(frames_done, frames_total)` is invoked every ~30 frames
    so the orchestrator can drive a progress bar. `frames_total` is
    estimated from `duration_hint * decode_fps` and may be off by a few
    frames at EOF — caller treats it as approximate.

    `cancel_check()` is polled every ~30 frames; returning True raises
    `RuntimeError("cancelled")` so the caller can flag the job."""
    import cv2  # local import — keeps tests cv2-free when they don't need decode
    mask = build_roi_mask(roi_corners, params.decode_width, params.decode_height)
    mask_sum = float(mask.sum())
    if mask_sum <= 0:
        raise ValueError("ROI mask is empty (degenerate quad)")
    mask_bool = mask.astype(bool)

    if duration_hint is None:
        try:
            duration_hint = float(probe_video(video_path).get("duration", 0.0))
        except Exception:
            duration_hint = 0.0
    total_estimate = max(1, int(duration_hint * params.decode_fps))

    motion: list[float] = []
    prev_gray: np.ndarray | None = None
    last_progress_emit = 0

    for i, frame in enumerate(_iter_frames(video_path, params)):
        if i & 31 == 0:
            if cancel_check():
                raise RuntimeError("cancelled")
            if on_progress and i - last_progress_emit >= 30:
                on_progress(i, total_estimate)
                last_progress_emit = i

        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        if prev_gray is not None:
            diff = cv2.absdiff(gray, prev_gray)
            # ROI-mean: dividing by mask_sum (not full frame) gives a
            # signal whose scale is invariant to ROI size. Spike numbers
            # quoted in PHASE0_REPORT (0.02–0.10) are on this scale.
            motion.append(float(diff[mask_bool].sum()) / (255.0 * mask_sum))
        prev_gray = gray

    if on_progress:
        on_progress(len(motion) + 1, total_estimate)
    return np.asarray(motion, dtype=np.float32)


# ---------------------------------------------------------------------------
# Smoothing + threshold
# ---------------------------------------------------------------------------


def smooth_motion(motion: np.ndarray, window_samples: int) -> np.ndarray:
    """Centred moving average. Window edges padded with reflection so the
    output is the same length as the input."""
    if window_samples <= 1 or len(motion) == 0:
        return motion.astype(np.float32, copy=False)
    w = int(window_samples)
    pad = w // 2
    padded = np.pad(motion, (pad, pad), mode="edge")
    kernel = np.ones(w, dtype=np.float32) / float(w)
    return np.convolve(padded, kernel, mode="valid")[: len(motion)].astype(np.float32)


def adaptive_threshold(motion: np.ndarray, percentile: float) -> float:
    """Per-match threshold = N-th percentile of the smoothed signal.
    Returns 0.0 if motion is empty so downstream guards don't divide-by."""
    if len(motion) == 0:
        return 0.0
    return float(np.percentile(motion, percentile))


# ---------------------------------------------------------------------------
# Score-anchored gap detection (PURE — no ffmpeg, no cv2)
# ---------------------------------------------------------------------------


def _find_rally_start_idx(
    motion: np.ndarray,
    anchor_idx: int,
    floor_idx: int,
    idle_sustain_samples: int,
    threshold: float,
) -> int:
    """Walk backward from `anchor_idx` toward `floor_idx`, find the first
    1.5s sustained-idle window AFTER having seen at least one active
    sample. Returns the rally_start sample index — the first active
    sample following the idle gap.

    If no such window exists in [floor_idx, anchor_idx], returns
    `anchor_idx` (caller will interpret this as "rally fills the entire
    gap → no trim").

    The `seen_active` guard prevents the trivial case where `anchor_idx`
    is already in dead time (which it usually is, since anchor sits 0.5s
    past the score press → 0.2s into post-rally dead time): we want the
    idle gap BEFORE the rally, not the brief idle AFTER it."""
    if anchor_idx <= floor_idx or len(motion) == 0:
        return max(floor_idx, anchor_idx)

    anchor_idx = min(anchor_idx, len(motion) - 1)
    floor_idx = max(0, floor_idx)
    W = max(1, idle_sustain_samples)

    seen_active = False
    run = 0
    for i in range(anchor_idx, floor_idx - 1, -1):
        if motion[i] < threshold:
            run += 1
            if seen_active and run >= W:
                # `i` is the leftmost (earliest) frame in our W-window of
                # idles. The window occupies [i, i + W - 1]. Going forward,
                # the rally starts at i + W.
                return min(i + W, anchor_idx)
        else:
            seen_active = True
            run = 0

    # Fell off the floor without confirming a 1.5s idle band — the entire
    # range is "active enough". Treat as no rally boundary found.
    return anchor_idx


def gaps_to_trims(
    score_events: Sequence[ScoreEvent],
    motion: np.ndarray,
    fps: float,
    threshold: float,
    duration: float,
    params: RallyDetectorParams = BALANCED,
) -> list[TrimSegment]:
    """Pure-logic core. Given a smoothed motion signal + threshold +
    score events + video duration, return the list of trims for the
    dead-time gaps between rallies plus the pre-first / post-last edges.

    Splitting this out from `run_rally_detection` is what makes the
    detector unit-testable: the test feeds synthetic motion arrays and
    asserts the emitted trims without spinning up ffmpeg."""
    events = sorted(score_events, key=lambda e: e.timestamp)
    trims: list[TrimSegment] = []

    if duration <= 0:
        return trims

    lag = params.score_press_lag_s
    tail = params.post_rally_tail_s
    pre = params.pre_rally_pad_s
    rally_min = params.rally_dur_min_s
    anchor_off = params.anchor_offset_s
    idle_samples = max(1, int(round(params.idle_sustain_s * fps)))
    min_trim = params.min_trim_dur_s

    def _emit(start: float, end: float) -> None:
        start = max(0.0, start)
        end = min(duration, end)
        if end - start >= min_trim:
            trims.append(TrimSegment(start=round(start, 3), end=round(end, 3)))

    # Pre-first-score edge — keep only the rally just before the first
    # score event. If no score events at all, nothing to anchor against;
    # caller should run blanket detection (Phase 6, deferred).
    if events:
        t_first = events[0].timestamp
        anchor_idx = int(round((t_first - anchor_off) * fps))
        rally_start_idx = _find_rally_start_idx(
            motion, anchor_idx, 0, idle_samples, threshold,
        )
        # rally_dur_min gate: ensure the rally is at least rally_min long
        # by pushing rally_start backward when the scan was too greedy.
        rally_end_actual_idx = int(round((t_first - lag) * fps))
        min_start_idx = rally_end_actual_idx - int(round(rally_min * fps))
        rally_start_idx = min(rally_start_idx, min_start_idx)
        rally_start_s = rally_start_idx / fps
        _emit(0.0, rally_start_s - pre)

    # Between-score gaps.
    for prev, nxt in zip(events, events[1:]):
        t_prev, t_next = prev.timestamp, nxt.timestamp
        trim_start = t_prev - lag + tail
        anchor_idx = int(round((t_next - anchor_off) * fps))
        floor_idx = int(round(trim_start * fps))
        rally_start_idx = _find_rally_start_idx(
            motion, anchor_idx, floor_idx, idle_samples, threshold,
        )
        # rally_dur_min gate — same logic as the pre-first edge.
        rally_end_actual_idx = int(round((t_next - lag) * fps))
        min_start_idx = rally_end_actual_idx - int(round(rally_min * fps))
        rally_start_idx = min(rally_start_idx, min_start_idx)
        # Floor: never push rally_start before the previous rally's tail.
        rally_start_idx = max(rally_start_idx, floor_idx)
        rally_start_s = rally_start_idx / fps
        _emit(trim_start, rally_start_s - pre)

    # Post-last-score edge — handshake + final scoreboard freeze are
    # part of the watch experience, NOT dead time. Keep
    # `post_match_keep_s` past the last rally's tail; trim only what
    # comes after (cameraman packing up, players leaving the table).
    # Per operator feedback 2026-05-28: previously we trimmed everything
    # past the last point which cut the handshake too aggressively.
    if events:
        t_last = events[-1].timestamp
        _emit(t_last - lag + tail + params.post_match_keep_s, duration)

    # Final clip: drop overlaps + sort. Overlaps shouldn't normally happen
    # but score events occasionally land very close together (operator
    # double-tap); the round(_, 3) above can also produce identical
    # boundaries. Merge those rather than emit two trims that share an edge.
    if not trims:
        return trims
    trims.sort(key=lambda t: t.start)
    merged: list[TrimSegment] = [trims[0]]
    for t in trims[1:]:
        last = merged[-1]
        if t.start <= last.end:
            merged[-1] = TrimSegment(start=last.start, end=max(last.end, t.end))
        else:
            merged.append(t)
    return merged


# ---------------------------------------------------------------------------
# Public orchestrator
# ---------------------------------------------------------------------------


def run_rally_detection(
    video_path: Path,
    roi_corners: Sequence[Sequence[float]],
    score_events: Sequence[ScoreEvent],
    params: RallyDetectorParams = BALANCED,
    *,
    cancel_check: Callable[[], bool] = lambda: False,
    emit: Callable[[str, dict], None] = lambda *a: None,
) -> list[TrimSegment]:
    """End-to-end: decode → motion → smooth → threshold → gaps → trims.

    `emit(event_type, payload)` is the hook the SSE layer (Step 2) will
    register to push live events to the frontend. In Step 1 it defaults
    to a no-op; we still call it at the major stage transitions so Step 2
    only has to wire up the callback and the events appear for free.

    Returns the final list of trims with `source` defaulted to "manual"
    (the existing TrimSegment schema). The frontend / persistence layer
    in Step 4 will retag these as "auto" once we add that field."""
    info = probe_video(video_path)
    duration = float(info.get("duration", 0.0))
    fps_out = params.decode_fps

    emit("stage", {"name": "decode", "duration": duration, "fps": fps_out})

    motion = compute_motion_signal(
        video_path, roi_corners, params,
        cancel_check=cancel_check,
        on_progress=lambda done, total: emit(
            "progress", {"stage": "decode", "frame_n": done, "frame_total": total},
        ),
        duration_hint=duration,
    )

    emit("stage", {"name": "smooth", "samples": len(motion)})
    smoothed = smooth_motion(motion, int(round(params.smooth_window_s * fps_out)))

    threshold = adaptive_threshold(smoothed, params.threshold_percentile)
    emit("stage", {"name": "threshold", "value": threshold})

    trims = gaps_to_trims(score_events, smoothed, fps_out, threshold, duration, params)
    for t in trims:
        emit("trim", {"start": t.start, "end": t.end, "source": "auto"})
    total_trimmed = sum(t.end - t.start for t in trims)
    emit("done", {
        "trims": len(trims),
        "total_trimmed_s": round(total_trimmed, 1),
        "threshold": threshold,
        "duration": duration,
    })
    return trims
