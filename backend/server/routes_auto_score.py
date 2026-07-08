"""Auto Score routes — Phase 1 (semi-auto rally proposals).

The Live Score panel's Auto tab runs UNANCHORED rally segmentation
(backend/auto_score/rally_segmenter.py, the measured v7-tuned2
recipe) and streams proposals over SSE. The operator reviews the
proposal list keyboard-first, enters winners, and Apply writes
ordinary score_events with source="auto" — all scoring state stays
frontend-owned exactly like the manual path.

Job + SSE + cache mechanics are a deliberate sibling of the auto-trim
pipeline in routes_auto_trim.py: one thread per job, a queue.Queue
drained by a StreamingResponse generator, and a JSON result cache at
temp/auto_score_cache/<sha1>.json that replays events (minus
per-frame progress) on identical inputs.

Winner detection (VLM / classifier / solver) is Gate G0b work and has
no endpoint yet — see docs/AUTO_SCORE_PLAN.md.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse

from ..ffmpeg_runner import probe_video
from .routes_auto_trim import (
    _resolve_video_for_auto_trim,
    _validate_roi_corners,
    _video_identity,
)
from .state import (
    _AUTOSCORE_CACHE_DIR,
    _auto_score_jobs,
    _auto_score_lock,
    AutoScoreJobState,
    prune_finished_jobs,
)
from .utils import _sanitize_for_json


router = APIRouter()

_log = logging.getLogger(__name__)


def _autoscore_cache_key(
    video_id: str, roi: list[list[float]], params_dict: dict,
) -> str:
    """sha1 over the inputs that affect segmenter output. No score
    events in the key — segmentation is unanchored by design."""
    payload = {
        "video_id": video_id,
        "roi": [[round(float(x), 6), round(float(y), 6)] for x, y in roi],
        "params": params_dict,
    }
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()


def _autoscore_cache_path(key: str) -> Path:
    return _AUTOSCORE_CACHE_DIR / f"{key}.json"


def _run_auto_score_job_worker(
    job_id: str,
    video_path: Path,
    roi: list[list[float]],
    params,  # RallySegmenterParams
) -> None:
    """Background worker for one segmentation run. Mirrors
    _run_auto_trim_job_worker: cache-hit replay (skipping progress
    events) or fresh run with a dual live/buffer emit; always ends by
    pushing the None stream-close sentinel."""
    job = _auto_score_jobs[job_id]
    cache_path = _autoscore_cache_path(job.cache_key)

    try:
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                cached = None
            if cached is not None:
                job.status = "running"
                job.cache_hit = True
                job.event_queue.put((
                    "log", {"msg": f"cache hit ({job.cache_key[:8]}...) replaying"},
                ))
                for ev in cached.get("events", []):
                    et = ev.get("type")
                    data = ev.get("data", {})
                    if et == "progress":
                        continue
                    if et == "stage":
                        job.stage = str(data.get("name", job.stage))
                    job.event_queue.put((et, data))
                job.proposals = list(cached.get("proposals", []))
                job.progress = 1.0
                job.status = "done"
                job.finished_at = time.time()
                job.event_queue.put(("done", cached.get("done", {
                    "proposals": job.proposals,
                    "count": len(job.proposals),
                })))
                return

        from ..auto_score import run_rally_segmentation

        job.status = "running"
        captured_events: list[dict] = []
        captured_done: dict = {}

        def emit(event_type: str, data: dict) -> None:
            if event_type == "progress":
                total = max(1, int(data.get("frame_total", 1)))
                job.progress = min(1.0, int(data.get("frame_n", 0)) / total)
            elif event_type == "stage":
                job.stage = str(data.get("name", job.stage))
            elif event_type == "done":
                captured_done.update(data)
            if event_type != "done":
                captured_events.append({"type": event_type, "data": data})
            job.event_queue.put((event_type, data))

        proposals = run_rally_segmentation(
            video_path,
            roi,
            params,
            cancel_check=lambda: job.cancel,
            emit=emit,
        )
        job.proposals = list(proposals)

        if job.cancel:
            job.status = "cancelled"
        else:
            job.status = "done"
            try:
                _AUTOSCORE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps(
                        {
                            "events": captured_events,
                            "proposals": proposals,
                            "done": captured_done,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            except Exception as e:
                job.event_queue.put(("log", {"msg": f"cache write failed: {e}"}))

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
        job.event_queue.put(None)


@router.post("/api/auto_score/start")
def auto_score_start(payload: dict = Body(...)) -> dict:
    """Kick off a rally-segmentation job. Body fields:

      name | token       source video (one of)
      roi  | corners     4 normalized [x, y] pairs (TL TR BR BL) —
                         operator-confirmed via the Auto Trim ROI modal
                         (MANDATORY gate, enforced by the frontend flow)
      params (optional)  partial RallySegmenterParams override

    Returns {job_id, cache_key, will_cache_hit, video_duration}."""
    from ..auto_score import TUNED2, RallySegmenterParams

    name = payload.get("name")
    token = payload.get("token")
    roi = _validate_roi_corners(payload.get("roi") or payload.get("corners"))

    params_override = payload.get("params") or {}
    if not isinstance(params_override, dict):
        raise HTTPException(status_code=400, detail="params must be an object")
    merged = {**dataclasses.asdict(TUNED2), **params_override}
    try:
        params = RallySegmenterParams(**merged)
    except TypeError as e:
        raise HTTPException(status_code=400, detail=f"bad params: {e}")

    video = _resolve_video_for_auto_trim(name, token)
    try:
        info = probe_video(video)
        duration = float(info.get("duration", 0.0))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"probe failed: {e}")
    if duration < 60.0:
        raise HTTPException(
            status_code=400,
            detail=f"Source video too short for auto-score ({duration:.1f}s)",
        )

    video_id = _video_identity(video)
    cache_key = _autoscore_cache_key(video_id, roi, dataclasses.asdict(params))
    will_hit = _autoscore_cache_path(cache_key).exists()

    job_id = uuid.uuid4().hex[:12]
    job = AutoScoreJobState(
        job_id=job_id,
        cache_key=cache_key,
        started_at=time.time(),
    )
    with _auto_score_lock:
        prune_finished_jobs(_auto_score_jobs)
        _auto_score_jobs[job_id] = job

    threading.Thread(
        target=_run_auto_score_job_worker,
        args=(job_id, video, roi, params),
        daemon=True,
        name=f"auto_score_{job_id}",
    ).start()

    return {
        "job_id": job_id,
        "cache_key": cache_key,
        "will_cache_hit": will_hit,
        "video_duration": duration,
    }


@router.get("/api/auto_score/events/{job_id}")
async def auto_score_events(job_id: str) -> StreamingResponse:
    """SSE stream for a segmentation job. Event types:

      hello    {job_id, cache_key}
      stage    {name, ...}
      progress {stage, frame_n, frame_total}
      proposal {id, t_start, t_end, who, status}
      done     {proposals, count, duration}
      log      {msg}
      error    {msg}
      close    {status, error}   synthetic terminal event
    """
    job = _auto_score_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")

    async def generator():
        loop = asyncio.get_running_loop()
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
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/auto_score/cancel/{job_id}")
def auto_score_cancel(job_id: str) -> dict:
    """Request cancellation; the decoder polls ~1 s. Idempotent."""
    job = _auto_score_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    if job.status in ("done", "cancelled", "error"):
        return {"ok": True, "already": job.status}
    job.cancel = True
    return {"ok": True, "job_id": job_id, "status": job.status}


@router.get("/api/auto_score/job/{job_id}")
def auto_score_job_status(job_id: str) -> dict:
    """Snapshot status (debug; SSE is canonical)."""
    job = _auto_score_jobs.get(job_id)
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
        "proposals": job.proposals,
    }
