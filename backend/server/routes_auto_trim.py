"""Auto-trim ROI workflow routes.

Phase 1a: operator clicks "Auto Trim" → modal opens → backend extracts a
midpoint refframe → backend runs auto-detect across 5 frames → modal
shows refframe + proposed ROI → operator confirms or edits the 4 corners
→ confirmed ROI saves to `project.info.roi_quadrilateral` AND appends to
a growing groundtruth dataset at
`dataset/roi_groundtruth/<video_hash>.json` for improving the detector
over time.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

from ..config import config
from ..ffmpeg_runner import probe_video
from .state import (
    ROOT_DIR,
    _REFFRAME_CACHE,
    _ROI_GROUNDTRUTH_DIR,
)
from .utils import (
    _resolve_external_video,
    _resolve_inside,
    _sanitize_for_json,
)


router = APIRouter()


def _resolve_video_for_auto_trim(name: str | None, token: str | None) -> Path:
    """Auto-trim modal passes EITHER a videos/ basename OR an external token,
    matching the existing two video-source paths the frontend already uses."""
    if token:
        return _resolve_external_video(token)
    if name:
        return _resolve_inside(config.videos_dir, name)
    raise HTTPException(status_code=400, detail="Provide either 'name' or 'token'")


def _video_identity(p: Path) -> str:
    """Stable id for a video file = sha1(absolute_path + size + mtime).
    Lets the refframe cache + ROI groundtruth survive re-imports of the
    same file (or detect re-encodes via mtime change)."""
    st = p.stat()
    raw = f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


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

    try:
        info = probe_video(video_path)
        dur = float(info.get("duration", 60.0))
    except Exception:
        dur = 60.0
    midpoint = max(1.0, dur / 2.0)

    # Use scale filter to downscale to max_w while preserving aspect ratio.
    # `-ss` before `-i` does fast seek to nearest keyframe — plenty accurate
    # for a refframe at the video midpoint.
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{midpoint:.3f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-vf", f"scale='min({max_w},iw)':-2",
        "-q:v", "3",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
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

    try:
        info = probe_video(video_path)
        dur = float(info.get("duration", 60.0))
    except Exception:
        dur = 60.0

    if n == 1:
        fractions = [0.5]
    else:
        step = (1.0 - 2 * _MULTIFRAME_MARGIN) / (n - 1)
        fractions = [_MULTIFRAME_MARGIN + i * step for i in range(n)]

    mid_idx = n // 2
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
        t = max(0.0, dur * frac)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{t:.3f}",
            "-i", str(video_path),
            "-frames:v", "1",
            "-vf", f"scale='min({max_w},iw)':-2",
            "-q:v", "3",
            str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not out.exists():
            continue

    # Mirror the mid-index frame to the single-frame cache path so a
    # subsequent _extract_refframe call returns instantly.
    if cache_paths[mid_idx].exists() and not midpoint_cache.exists():
        try:
            shutil.copy2(cache_paths[mid_idx], midpoint_cache)
        except Exception:
            pass

    return [p for p in cache_paths if p.exists()]


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
    backend/roi/ can be measured against many real inputs."""
    from ..roi import detect_roi_multiframe
    name = payload.get("name")
    token = payload.get("token")
    corners = _validate_roi_corners(payload.get("corners"))
    was_edited = bool(payload.get("was_edited", False))

    video = _resolve_video_for_auto_trim(name, token)
    video_id = _video_identity(video)

    # Compute what auto-detect would have returned so we can compare in
    # the groundtruth file → measures detector accuracy automatically.
    refframe = _extract_refframe(video)
    refframes = _extract_multi_refframes(video)
    det = detect_roi_multiframe(refframes)

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
    if gt_path.exists():
        try:
            existing = json.loads(gt_path.read_text(encoding="utf-8"))
        except Exception:
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
        "detector_proposed": det.corners,
        "detector_method": det.method,
        "detector_confidence": det.confidence,
    }))
    record = {
        "video_id": video_id,
        "video_name": video.name,
        "video_path": str(video),
        "latest_corners": corners,   # the canonical "truth" for this video
        "history": history,
    }
    gt_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "ok": True,
        "video_id": video_id,
        "saved_to": str(gt_path.relative_to(ROOT_DIR)),
        "history_count": len(history),
    }


@router.get("/api/auto_trim/groundtruth_count")
def auto_trim_groundtruth_count() -> dict:
    """Quick stat used to track milestone progress (≥10 confirmed inputs
    before unlocking downstream trim-detection work)."""
    if not _ROI_GROUNDTRUTH_DIR.exists():
        return {"count": 0, "videos": []}
    files = sorted(_ROI_GROUNDTRUTH_DIR.glob("*.json"))
    videos = []
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            videos.append({
                "video_name": d.get("video_name"),
                "history_count": len(d.get("history", [])),
            })
        except Exception:
            continue
    return {"count": len(files), "videos": videos}
