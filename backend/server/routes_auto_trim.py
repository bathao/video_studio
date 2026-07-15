"""Auto-trim ROI workflow + rally detection routes.

Phase 1a (ROI confirmation):
    operator clicks "Auto Trim" → modal opens → backend extracts a
    midpoint refframe → backend runs auto-detect across 5 frames → modal
    shows refframe + proposed ROI → operator confirms or edits the 4
    corners → confirmed ROI saves to `project.info.roi_quadrilateral`
    AND appends to a growing groundtruth dataset at
    `dataset/roi_groundtruth/<video_hash>.json` for improving the
    detector over time.

Phase 1b (rally detection, Step 2):
    After confirming ROI, operator clicks "Run detection" → backend
    streams a rally-detector job via SSE: stage / progress / trim
    events as the detector decodes the video + runs gap analysis.
    The job-and-stream pattern mirrors render jobs but with SSE
    instead of polling. Cache layer at `temp/auto_trim_cache/<sha1>.json`
    keyed by (video_id + roi + score_events + params) lets a re-run
    replay the same events in milliseconds.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from ..config import config
from ..ffmpeg_runner import probe_video
from ..models import ScoreEvent, TrimSegment
from .retrain import groundtruth_summary, retrain_status, start_retrain
from .state import (
    ROOT_DIR,
    _AUTOTRIM_CACHE_DIR,
    _REFFRAME_CACHE,
    _ROI_GROUNDTRUTH_DIR,
    _auto_trim_jobs,
    _auto_trim_lock,
    AutoTrimJobState,
    prune_finished_jobs,
)
from .utils import (
    _resolve_external_video,
    _resolve_inside,
    _sanitize_for_json,
)


router = APIRouter()

_log = logging.getLogger(__name__)


def _resolve_video_for_auto_trim(name: str | None, token: str | None) -> Path:
    """Auto-trim modal passes EITHER a videos/ basename OR an external token,
    matching the existing two video-source paths the frontend already uses."""
    if token:
        return _resolve_external_video(token)
    if name:
        p = _resolve_inside(config.videos_dir, name)
        # _resolve_inside only guards traversal; without this check a
        # missing file surfaces later as an unhandled FileNotFoundError
        # from p.stat() in _video_identity — a 500 instead of a 404.
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"Video not found: {name}")
        return p
    raise HTTPException(status_code=400, detail="Provide either 'name' or 'token'")


def _video_identity(p: Path) -> str:
    """Stable id for a video file = sha1(absolute_path + size + mtime).
    Lets the refframe cache + ROI groundtruth survive re-imports of the
    same file (or detect re-encodes via mtime change)."""
    st = p.stat()
    raw = f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _probe_duration(video_path: Path) -> float:
    """Probe the source duration, failing loudly.

    This used to fall back to 60.0 on any probe error, which silently
    sampled every refframe from the first minute of a long video — the
    detector then saw 5 near-identical early frames instead of the
    10..90% spread. An unreadable video should error here, visibly."""
    try:
        info = probe_video(video_path)
        dur = float(info.get("duration", 0.0))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"ffprobe failed on {video_path.name}: {e}",
        )
    if dur <= 0.0:
        raise HTTPException(
            status_code=500,
            detail=f"ffprobe returned no duration for {video_path.name}",
        )
    return dur


def _refframe_cmd(video_path: Path, t: float, out: Path, max_w: int) -> list[str]:
    """Single-frame JPEG extract command, shared by the single- and
    multi-refframe paths (they had drifted into two copies). Scale
    filter downscales to max_w preserving aspect ratio; `-ss` before
    `-i` is a fast keyframe seek — plenty accurate for refframes."""
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{t:.3f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-vf", f"scale='min({max_w},iw)':-2",
        "-q:v", "3",
        str(out),
    ]


def _extract_refframe(video_path: Path, *, max_w: int = 960) -> Path:
    """Extract a single frame at the midpoint of the video as JPEG.
    Cached by video identity so subsequent calls are instant.

    Kept as the canonical "this is what the operator sees + what gets
    saved to dataset on confirm" frame. Multi-frame detection uses
    `_extract_multi_refframes` separately."""
    vid = _video_identity(video_path)
    out = _REFFRAME_CACHE / f"{vid}.jpg"
    if out.exists():
        return out
    _REFFRAME_CACHE.mkdir(parents=True, exist_ok=True)

    dur = _probe_duration(video_path)
    midpoint = max(1.0, dur / 2.0)

    proc = subprocess.run(
        _refframe_cmd(video_path, midpoint, out, max_w),
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not out.exists():
        raise HTTPException(
            status_code=500,
            detail=f"refframe extract failed: {proc.stderr[-300:]}",
        )
    return out


# Number of frames sampled per detect call. 5 spaced at 10/30/50/70/90%
# of the video is the sweet spot in cost/coverage: high probability that
# at least one frame has an unobstructed table even when players occlude
# the centre at any given moment, without making detection feel sluggish.
_MULTIFRAME_N = 5
_MULTIFRAME_MARGIN = 0.10


def _extract_multi_refframes(video_path: Path, *, max_w: int = 960,
                              n: int = _MULTIFRAME_N) -> list[Path]:
    """Extract N evenly-spaced frames for cluster+median ROI detection.

    Frame at index `n // 2` lands at the midpoint, matching what
    `_extract_refframe` returns — so the canonical "operator-facing"
    refframe is part of the multi-frame set and gets reused (no extra
    ffmpeg call for the duplicate timestamp). Per-frame files cached
    as `temp/refframes/{video_id}_f{i}.jpg`.

    Skips any frame whose extraction fails (rare; e.g. corrupted GOP at
    the seek point) — detect_roi_multiframe tolerates fewer than n
    inputs."""
    vid = _video_identity(video_path)
    cache_paths = [_REFFRAME_CACHE / f"{vid}_f{i}.jpg" for i in range(n)]
    midpoint_cache = _REFFRAME_CACHE / f"{vid}.jpg"

    if all(p.exists() for p in cache_paths):
        return cache_paths

    _REFFRAME_CACHE.mkdir(parents=True, exist_ok=True)

    dur = _probe_duration(video_path)

    if n == 1:
        fractions = [0.5]
    else:
        step = (1.0 - 2 * _MULTIFRAME_MARGIN) / (n - 1)
        fractions = [_MULTIFRAME_MARGIN + i * step for i in range(n)]

    mid_idx = n // 2

    # Plan the per-frame work. Each entry is either a "reuse" (copy the
    # already-extracted midpoint into the f{mid_idx} slot) or an
    # "extract" (run ffmpeg with input-side fast-seek).
    extract_jobs: list[tuple[Path, float]] = []
    for i, frac in enumerate(fractions):
        out = cache_paths[i]
        if out.exists():
            continue
        # If the midpoint single-frame cache already exists (e.g. from a
        # prior single-frame call), reuse it as f{mid_idx} to avoid a
        # redundant ffmpeg call.
        if i == mid_idx and midpoint_cache.exists():
            try:
                shutil.copy2(midpoint_cache, out)
                continue
            except Exception:
                pass
        extract_jobs.append((out, max(0.0, dur * frac)))

    # Run the ffmpeg extractions in parallel. Sequential was the easy
    # default but multi-GB MP4s pay ~few-hundred-ms per ffmpeg fast-seek
    # (read enough of the file to land on a keyframe), so 4 of them
    # in a row was the dominant cost in the Auto Trim modal latency.
    # max_workers caps at n so we never spin up more threads than jobs;
    # 5 concurrent ffmpegs are cheap on a modern SSD + the operator's
    # 16-core CPU. Each subprocess is independent — no shared state.
    def _extract_one(out: Path, t: float) -> str | None:
        """Returns an error description on failure, None on success.
        A failed frame is tolerated (detect_roi_multiframe accepts fewer
        than n inputs) but no longer invisible — failures are logged and
        an all-frames-failed run raises instead of detecting on nothing."""
        proc = subprocess.run(
            _refframe_cmd(video_path, t, out, max_w),
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or not out.exists():
            return (
                f"t={t:.1f}s: "
                f"{proc.stderr[-200:].strip() or f'exit code {proc.returncode}'}"
            )
        return None

    failures: list[str] = []
    if extract_jobs:
        with ThreadPoolExecutor(max_workers=min(n, len(extract_jobs))) as pool:
            futures = [pool.submit(_extract_one, out, t) for out, t in extract_jobs]
            failures = [err for err in (f.result() for f in futures) if err]
        if failures:
            _log.warning(
                "refframe extract: %d/%d frames failed for %s — %s",
                len(failures), len(extract_jobs), video_path.name,
                "; ".join(failures),
            )

    # Mirror the mid-index frame to the single-frame cache path so a
    # subsequent _extract_refframe call returns instantly.
    if cache_paths[mid_idx].exists() and not midpoint_cache.exists():
        try:
            shutil.copy2(cache_paths[mid_idx], midpoint_cache)
        except Exception:
            pass

    frames = [p for p in cache_paths if p.exists()]
    if not frames:
        raise HTTPException(
            status_code=500,
            detail=(
                f"All refframe extracts failed for {video_path.name}: "
                f"{failures[-1] if failures else 'unknown error'}"
            ),
        )
    return frames


@router.get("/api/auto_trim/refframe")
def auto_trim_refframe(name: str | None = None, token: str | None = None) -> FileResponse:
    """Serve a JPEG refframe for the source video. Lazily extracted +
    cached under `temp/refframes/<video_id>.jpg`."""
    video = _resolve_video_for_auto_trim(name, token)
    out = _extract_refframe(video)
    return FileResponse(str(out), media_type="image/jpeg")


@router.post("/api/auto_trim/detect_roi")
def auto_trim_detect_roi(payload: dict = Body(...)) -> dict:
    """Run ROI auto-detect across 5 frames sampled along the video and
    aggregate via cluster+median. Robust to player occlusion of the
    table in any single frame. Returns 4 normalized corners, a
    confidence score, the detection method, and debug fields the modal
    can show for troubleshooting."""
    from ..roi import detect_roi_multiframe
    name = payload.get("name")
    token = payload.get("token")
    video = _resolve_video_for_auto_trim(name, token)
    refframes = _extract_multi_refframes(video)
    det = detect_roi_multiframe(refframes)
    return _sanitize_for_json({
        "video_id": _video_identity(video),
        "video_name": video.name,
        "corners": det.corners,
        "confidence": det.confidence,
        "method": det.method,
        "debug": det.debug,
    })


def _validate_roi_corners(corners) -> list[list[float]]:
    """Coerce + validate input. Must be 4 [x, y] pairs in [0, 1]."""
    if not isinstance(corners, list) or len(corners) != 4:
        raise HTTPException(status_code=400, detail="ROI must be 4 corners")
    out: list[list[float]] = []
    for i, pt in enumerate(corners):
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            raise HTTPException(status_code=400, detail=f"corner[{i}] not [x, y]")
        x, y = float(pt[0]), float(pt[1])
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise HTTPException(status_code=400, detail=f"corner[{i}] out of [0,1]")
        out.append([x, y])
    return out


@router.post("/api/auto_trim/confirm_roi")
def auto_trim_confirm_roi(payload: dict = Body(...)) -> dict:
    """Operator confirmed the ROI in the modal. Persist for the (eventual)
    project save AND append to a growing groundtruth dataset that the
    detector algorithm can be retrained / re-tuned against.

    The project-state mutation happens client-side (the modal sets
    `project.info.roi_quadrilateral` directly); this endpoint exists to
    capture the labeled training example so improvements to
    backend/roi/ can be measured against many real inputs.

    The detector's proposal (corners + method + confidence) is taken
    from the request body — the modal already ran detection on open and
    has the result in memory. Re-running detect_roi_multiframe here
    just to record it would cost the operator another 5–10 s per confirm
    for zero new information. When the fields are absent (older client
    OR a recovery path), fall back to re-running."""
    name = payload.get("name")
    token = payload.get("token")
    corners = _validate_roi_corners(payload.get("corners"))
    was_edited = bool(payload.get("was_edited", False))

    video = _resolve_video_for_auto_trim(name, token)
    video_id = _video_identity(video)

    # Refframe extract is cheap if cached (modal-open already triggered it)
    # — needed for the dataset/roi_groundtruth jpg copy below.
    refframe = _extract_refframe(video)

    # Detector proposal from the request body. Frontend sends what it
    # already computed in loadRefframeAndDetect; absence triggers the
    # legacy re-run path.
    det_corners = payload.get("detector_proposed")
    det_method = payload.get("detector_method")
    det_confidence = payload.get("detector_confidence")
    if det_corners is None or det_method is None or det_confidence is None:
        from ..roi import detect_roi_multiframe
        refframes = _extract_multi_refframes(video)
        det = detect_roi_multiframe(refframes)
        det_corners = det.corners
        det_method = det.method
        det_confidence = det.confidence

    _ROI_GROUNDTRUTH_DIR.mkdir(parents=True, exist_ok=True)
    # Copy the refframe alongside the groundtruth json so the learned
    # nearest-neighbor detector can compute color-histogram similarity
    # against ALL confirmed inputs — each confirm enriches the training
    # set without any extra step.
    refframe_dst = _ROI_GROUNDTRUTH_DIR / f"{video_id}.jpg"
    try:
        shutil.copyfile(refframe, refframe_dst)
    except Exception:
        pass  # similarity matching just falls back to naive without this file

    gt_path = _ROI_GROUNDTRUTH_DIR / f"{video_id}.json"
    existing: dict = {}
    corrupt_note = ""
    if gt_path.exists():
        try:
            existing = json.loads(gt_path.read_text(encoding="utf-8"))
        except Exception as e:
            # NEVER silently reset history — the prior confirms are
            # one-of-a-kind labels YOLO trains on. Quarantine the bad
            # file so it stays recoverable, and tell the operator.
            quarantine = gt_path.with_name(
                f"{video_id}.corrupt.{int(time.time())}.json")
            try:
                gt_path.rename(quarantine)
                corrupt_note = (f"existing groundtruth was unreadable "
                                f"({e}); moved to {quarantine.name} — "
                                "history restarted from this confirm")
            except OSError:
                corrupt_note = (f"existing groundtruth unreadable ({e}) "
                                "and quarantine failed — history "
                                "restarted from this confirm")
            _log.warning("confirm_roi %s: %s", video_id, corrupt_note)
            existing = {}

    # Track multiple confirmations per video over time — operator may
    # re-confirm with adjusted corners as the algorithm changes. New
    # entries append. Sanitize detector outputs since they may contain
    # numpy scalars (e.g. confidence from torch tensor, ratios derived
    # from numpy arrays in classical-CV stages) that json.dump can't
    # serialize.
    history = existing.get("history", [])
    history.append(_sanitize_for_json({
        "confirmed_at": time.time(),
        "corners": corners,
        "was_edited": was_edited,
        "detector_proposed": det_corners,
        "detector_method": det_method,
        "detector_confidence": det_confidence,
    }))
    record = {
        "video_id": video_id,
        "video_name": video.name,
        "video_path": str(video),
        "latest_corners": corners,   # the canonical "truth" for this video
        "history": history,
    }
    gt_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    result = {
        "ok": True,
        "video_id": video_id,
        "saved_to": str(gt_path.relative_to(ROOT_DIR)),
        "history_count": len(history),
    }
    if corrupt_note:
        result["warning"] = corrupt_note
    return result


@router.get("/api/auto_trim/groundtruth_count")
def auto_trim_groundtruth_count() -> dict:
    """Quick stat used to track milestone progress (≥10 confirmed inputs
    before unlocking downstream trim-detection work).

    Also reports how STALE the YOLO tier is: the classical tiers (ORB +
    learned-NN) read the groundtruth dir live on every detect, but the
    top-priority YOLO model only learns when the operator re-runs
    `scripts/build_yolo_dataset.py` + `scripts/train_roi_seg.py`.
    `confirms_since_yolo_train` counts confirm events newer than the
    model file's mtime so the modal can suggest a retrain at the right
    moment — this is what closes the confirm → better-detector loop.
    Computation lives in retrain.groundtruth_summary (shared with the
    training-status dashboard)."""
    return groundtruth_summary()


@router.post("/api/auto_trim/retrain_yolo")
def auto_trim_retrain_yolo() -> dict:
    """Kick off the YOLO retrain pipeline (dataset build + train) on a
    worker thread. 409 when a retrain or any GPU job is already running.
    Job logic lives in `backend/server/retrain.py`; on success the
    in-process model cache reloads — no server restart needed."""
    ok, reason = start_retrain()
    if not ok:
        raise HTTPException(status_code=409, detail=reason)
    return {"ok": True}


@router.get("/api/auto_trim/retrain_status")
def auto_trim_retrain_status() -> dict:
    return retrain_status()


# ===========================================================================
# Phase 1b — rally detection (Step 2: backend SSE orchestration)
# ===========================================================================
#
# Lifecycle:
#   1. Frontend POSTs /api/auto_trim/start with {video, roi, score_events,
#      params?} → backend returns {job_id}. Worker thread spins up. If
#      a matching cache entry exists, the worker replays it; otherwise
#      it runs run_rally_detection from scratch.
#   2. Frontend opens EventSource at /api/auto_trim/events/{job_id} and
#      consumes stage / progress / trim / done events as they arrive.
#   3. On Apply (Step 4), frontend uses the trims it collected from the
#      stream — no backend round-trip needed. Server-side `job.trims` is
#      still populated for /api/auto_trim/job/{id} debug introspection.
#
# Cache key: sha1 over a canonical JSON of (video_identity + roi corners
# + score events + detector params). Score events are sorted by
# (timestamp, who) before hashing so a noop reordering on the frontend
# doesn't invalidate. Params are dataclasses.asdict()'d with sort_keys
# at json.dumps time → deterministic across runs.
# ===========================================================================


def _autotrim_cache_key(
    video_id: str,
    roi: list[list[float]],
    score_events: list[ScoreEvent],
    params_dict: dict,
) -> str:
    """Deterministic sha1 over the inputs that affect detector output.

    Rounded to 3 decimals (timestamps) / 6 decimals (roi) so floating-
    point jitter from JSON round-trips doesn't bust the cache."""
    payload = {
        "video_id": video_id,
        "roi": [[round(float(x), 6), round(float(y), 6)] for x, y in roi],
        "score_events": sorted(
            [
                {"t": round(float(e.timestamp), 3), "who": int(e.who)}
                for e in score_events
            ],
            key=lambda e: (e["t"], e["who"]),
        ),
        "params": params_dict,
    }
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()


def _autotrim_cache_path(key: str) -> Path:
    return _AUTOTRIM_CACHE_DIR / f"{key}.json"


def _run_auto_trim_job_worker(
    job_id: str,
    video_path: Path,
    roi: list[list[float]],
    score_events: list[ScoreEvent],
    params,  # RallyDetectorParams
) -> None:
    """Background worker for one rally-detection run.

    Two paths:
      A. Cache hit  — load cached events from disk, replay them into the
         job's event queue, populate `job.trims`, push "done", exit.
      B. Cache miss — call `run_rally_detection` with an `emit` callback
         that BOTH pushes into the queue (live to SSE consumer) AND
         buffers into `captured_events` for caching on success.

    Either path ends by pushing `None` into the queue so the SSE
    generator knows to close the stream."""
    job = _auto_trim_jobs[job_id]
    cache_path = _autotrim_cache_path(job.cache_key)

    try:
        # ---------------- Cache hit path ----------------
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                cached = None
            loaded: list[TrimSegment] = []
            if cached is not None:
                # Parse trims BEFORE replaying: one malformed trim means
                # the entry is from a partial write or an old schema —
                # dropping just that trim would report "done" with fewer
                # trims than the detection actually found. Discard the
                # whole entry and recompute instead.
                try:
                    loaded = [TrimSegment(**t)
                              for t in cached.get("trims", [])]
                except Exception as e:
                    _log.warning(
                        "auto-trim cache %s… malformed (%s) — recomputing",
                        job.cache_key[:8], e)
                    cached = None
                    try:
                        cache_path.unlink()
                    except OSError:
                        pass
            if cached is not None:
                job.status = "running"
                job.cache_hit = True
                job.event_queue.put((
                    "log", {"msg": f"cache hit ({job.cache_key[:8]}...) replaying"},
                ))
                # Replay everything EXCEPT per-frame progress events — a
                # 22 min video caches thousands of them, and pushing each
                # through the queue made the "instant" cache hit take
                # visible seconds. The frontend doesn't need them: its
                # `close` handler jumps the bar to 100% on done.
                for ev in cached.get("events", []):
                    et = ev.get("type")
                    data = ev.get("data", {})
                    if et == "progress":
                        continue
                    if et == "stage":
                        job.stage = str(data.get("name", job.stage))
                    job.event_queue.put((et, data))
                # Publish atomically — the /job/{id} status endpoint
                # iterates job.trims from another thread, and appending
                # in place raced with that iteration.
                job.trims = loaded
                job.progress = 1.0
                job.status = "done"
                job.finished_at = time.time()
                done_payload = cached.get("done", {
                    "trims": len(job.trims),
                    "total_trimmed_s": round(
                        sum(t.end - t.start for t in job.trims), 1,
                    ),
                })
                job.event_queue.put(("done", done_payload))
                return

        # ---------------- Fresh run path ----------------
        from ..rally_detector import run_rally_detection

        job.status = "running"
        captured_events: list[dict] = []
        captured_done: dict = {}

        def emit(event_type: str, data: dict) -> None:
            # Mutate job-level fields for /job/{id} status polls.
            if event_type == "progress":
                total = max(1, int(data.get("frame_total", 1)))
                job.progress = min(1.0, int(data.get("frame_n", 0)) / total)
            elif event_type == "stage":
                job.stage = str(data.get("name", job.stage))
            elif event_type == "done":
                captured_done.update(data)
            # Buffer for cache (everything except "done" — that's stored
            # separately so the replay path can re-emit it at the end).
            if event_type != "done":
                captured_events.append({"type": event_type, "data": data})
            # Push live to SSE consumer.
            job.event_queue.put((event_type, data))

        def cancel_check() -> bool:
            return job.cancel

        trims = run_rally_detection(
            video_path=video_path,
            roi_corners=roi,
            score_events=score_events,
            params=params,
            cancel_check=cancel_check,
            emit=emit,
        )
        job.trims = list(trims)

        if job.cancel:
            job.status = "cancelled"
        else:
            job.status = "done"
            # Persist cache for next run. Failure to write the cache is
            # non-fatal — next run will just recompute.
            try:
                _AUTOTRIM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps(
                        {
                            "events": captured_events,
                            "trims": [t.model_dump() for t in trims],
                            "done": captured_done,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            except Exception as e:
                job.event_queue.put((
                    "log", {"msg": f"cache write failed: {e}"},
                ))

        job.finished_at = time.time()

    except RuntimeError as e:
        if str(e) == "cancelled":
            job.status = "cancelled"
        else:
            job.status = "error"
            job.error = str(e)
            job.event_queue.put(("error", {"msg": str(e)}))
        job.finished_at = time.time()
    except Exception as e:
        job.status = "error"
        job.error = repr(e)
        job.event_queue.put(("error", {"msg": repr(e)}))
        job.finished_at = time.time()
    finally:
        # Close the stream regardless of how we got here.
        job.event_queue.put(None)


@router.post("/api/auto_trim/start")
def auto_trim_start(payload: dict = Body(...)) -> dict:
    """Kick off a rally-detection job. Body fields:

      name | token              source video (one of)
      roi  | corners            4 normalized [x, y] pairs (TL TR BR BL)
      score_events              list of {timestamp, who, ...} dicts
      params (optional)         partial RallyDetectorParams override

    Returns {job_id, cache_key, will_cache_hit}. The frontend should
    immediately open EventSource('/api/auto_trim/events/<job_id>')."""
    from ..rally_detector import BALANCED, RallyDetectorParams

    name = payload.get("name")
    token = payload.get("token")
    roi = _validate_roi_corners(payload.get("roi") or payload.get("corners"))

    raw_events = payload.get("score_events") or []
    if not isinstance(raw_events, list):
        raise HTTPException(status_code=400, detail="score_events must be a list")
    score_events: list[ScoreEvent] = []
    dropped_events = 0
    for e in raw_events:
        try:
            score_events.append(ScoreEvent.model_validate(e))
        except Exception:
            # Defensive: skip malformed events rather than fail the whole
            # job. Real frontend always sends valid shape (frontend
            # mirrors backend ScoreEvent) — so any drop is a client bug
            # worth surfacing, not hiding.
            dropped_events += 1
            continue
    if dropped_events:
        _log.warning(
            "auto_trim_start: dropped %d/%d malformed score events",
            dropped_events, len(raw_events),
        )
    if len(score_events) < 10:
        raise HTTPException(
            status_code=400,
            detail=f"Auto-trim needs ≥10 score events (got {len(score_events)}). "
                   "Operator should press A/D throughout the match first.",
        )

    # Merge param overrides into BALANCED defaults.
    params_override = payload.get("params") or {}
    if not isinstance(params_override, dict):
        raise HTTPException(status_code=400, detail="params must be an object")
    merged = {**dataclasses.asdict(BALANCED), **params_override}
    try:
        params = RallyDetectorParams(**merged)
    except TypeError as e:
        raise HTTPException(status_code=400, detail=f"bad params: {e}")

    video = _resolve_video_for_auto_trim(name, token)
    # Probe early so a bad video errors HERE (synchronous) instead of
    # opaquely on the worker thread 200ms later.
    try:
        info = probe_video(video)
        duration = float(info.get("duration", 0.0))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"probe failed: {e}")
    if duration < 60.0:
        raise HTTPException(
            status_code=400,
            detail=f"Source video too short for auto-trim ({duration:.1f}s)",
        )

    video_id = _video_identity(video)
    cache_key = _autotrim_cache_key(
        video_id, roi, score_events, dataclasses.asdict(params),
    )
    will_hit = _autotrim_cache_path(cache_key).exists()

    job_id = uuid.uuid4().hex[:12]
    job = AutoTrimJobState(
        job_id=job_id,
        cache_key=cache_key,
        started_at=time.time(),
    )
    with _auto_trim_lock:
        prune_finished_jobs(_auto_trim_jobs)
        _auto_trim_jobs[job_id] = job

    threading.Thread(
        target=_run_auto_trim_job_worker,
        args=(job_id, video, roi, score_events, params),
        daemon=True,
        name=f"auto_trim_{job_id}",
    ).start()

    return {
        "job_id": job_id,
        "dropped_events": dropped_events,
        "cache_key": cache_key,
        "will_cache_hit": will_hit,
        "video_duration": duration,
    }


@router.get("/api/auto_trim/events/{job_id}")
async def auto_trim_events(job_id: str) -> StreamingResponse:
    """SSE stream of detector events for a running (or just-finished) job.

    Event types pushed by the detector:
      stage    {name, ...}            major pipeline transition
      progress {stage, frame_n, frame_total}    decode progress
      trim     {start, end, source}   one trim emitted at end of pipeline
      done     {trims, total_trimmed_s, threshold, duration}    final summary
      log      {msg}                  free-form log line from the worker
      error    {msg}                  job failed (caller can show toast)

    The handler also emits a final synthetic `close` event with the
    job's terminal status so the frontend can distinguish done /
    cancelled / error without a separate API call."""
    job = _auto_trim_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")

    async def generator():
        loop = asyncio.get_running_loop()
        # Initial hello so the EventSource readyState flips to OPEN on
        # the client before the first detector event (which can take a
        # few seconds while ffmpeg starts).
        yield (
            f"event: hello\n"
            f"data: {json.dumps({'job_id': job_id, 'cache_key': job.cache_key})}\n\n"
        )
        try:
            while True:
                ev = await loop.run_in_executor(None, job.event_queue.get)
                if ev is None:
                    break
                event_type, data = ev
                clean = _sanitize_for_json(data)
                yield (
                    f"event: {event_type}\n"
                    f"data: {json.dumps(clean, ensure_ascii=False)}\n\n"
                )
        finally:
            yield (
                f"event: close\n"
                f"data: {json.dumps({'status': job.status, 'error': job.error})}\n\n"
            )

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx hint; harmless locally
        },
    )


@router.post("/api/auto_trim/cancel/{job_id}")
def auto_trim_cancel(job_id: str) -> dict:
    """Request cancellation. The detector polls the cancel flag every
    ~30 frames (~1 s) and raises RuntimeError("cancelled") which the
    worker converts to status=='cancelled'. Idempotent."""
    job = _auto_trim_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    if job.status in ("done", "cancelled", "error"):
        return {"ok": True, "already": job.status}
    job.cancel = True
    return {"ok": True, "job_id": job_id, "status": job.status}


@router.get("/api/auto_trim/job/{job_id}")
def auto_trim_job_status(job_id: str) -> dict:
    """Snapshot status. Mostly for debugging — the SSE stream is the
    canonical way to follow a job. Useful when reopening the modal
    after a refresh: frontend can check `status` + `trims` without
    re-running."""
    job = _auto_trim_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "progress": job.progress,
        "stage": job.stage,
        "error": job.error,
        "cache_key": job.cache_key,
        "cache_hit": job.cache_hit,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "trims": [t.model_dump() for t in job.trims],
    }
