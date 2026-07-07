"""Render jobs + output files + scoreboard preview routes."""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..ass import ScoreFrame, build_scoreboard_ass_text
from ..config import config
from ..ffmpeg_runner import (
    FFmpegError,
    aac_args,
    hwaccel_input_args,
    nvenc_args,
    run_ffmpeg_with_progress,
)
from ..models import ProjectData, RenderRequest
from ..renderer import RenderPlan, run_render
from .state import _jobs, _jobs_lock, prune_finished_jobs
from .utils import (
    _open_or_focus_explorer,
    _resolve_external_video,
    _resolve_inside,
    _safe_name,
)


router = APIRouter()


# ---------- scoreboard preview ---------------------------------------------


class ScoreboardPreviewRequest(BaseModel):
    """Body of POST /api/preview/scoreboard.ass. The frontend sends the
    full project plus the source video's metrics so the .ass it gets
    back is generated with the same `video_w / video_h / total_duration`
    parameters the render pipeline would use. Defaults fall back to
    1080p / 1-hour so a preview still works before a video is loaded."""
    project: ProjectData
    video_w: int = Field(default=1920, ge=320, le=7680)
    video_h: int = Field(default=1080, ge=240, le=4320)
    duration: float = Field(default=3600.0, gt=0.0)


@router.post("/api/preview/scoreboard.ass", response_class=PlainTextResponse)
def preview_scoreboard(req: ScoreboardPreviewRequest) -> str:
    """Return the scoreboard .ass for the current project state. The
    frontend (`scoreboard_preview.js`) feeds this directly into a JASSUB
    libass-WASM instance bound to the `<video>` element, which renders
    the overlay byte-identically to what ffmpeg's `ass=` filter burns
    into the final main render — same `build_scoreboard_ass_text` is
    the single source of truth for both paths."""
    p = req.project
    events = [
        ScoreFrame(
            timestamp=ev.timestamp,
            p1_score=ev.p1_score, p2_score=ev.p2_score,
            p1_set=ev.p1_set, p2_set=ev.p2_set,
        )
        for ev in p.score_events
    ]
    text = build_scoreboard_ass_text(
        video_w=req.video_w, video_h=req.video_h,
        total_duration=req.duration,
        tournament=p.info.tournament,
        p1_name=p.info.p1, p2_name=p.info.p2,
        p1_team=p.info.p1_team, p2_team=p.info.p2_team,
        match_type=p.info.match_type,
        p3_name=p.info.p3, p4_name=p.info.p4,
        score_events=events,
        best_of=p.info.best_of,
    )
    return text


# ---------- output files ----------------------------------------------------


@router.get("/api/output/{name}")
def fetch_output(name: str) -> FileResponse:
    """Serve the output inline so the browser plays it instead of downloading."""
    target = _resolve_inside(config.output_dir, name)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Output not found")
    return FileResponse(str(target), media_type="video/mp4")


@router.post("/api/output/{name}/reveal")
def reveal_output(name: str) -> dict:
    """Open Windows Explorer with the file selected."""
    target = _resolve_inside(config.output_dir, name)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Output not found")
    _open_or_focus_explorer(target, select=True)
    return {"ok": True, "path": str(target)}


@router.post("/api/output-folder/open")
def open_output_folder() -> dict:
    """Open the output directory in Explorer."""
    _open_or_focus_explorer(Path(config.output_dir), select=False)
    return {"ok": True, "path": str(config.output_dir)}


def _clip_time_tag(seconds: float) -> str:
    """Filename-safe compact timestamp: 12.4s → '0m12s', 78.0s → '1m18s'."""
    total = int(round(seconds))
    m, s = divmod(total, 60)
    return f"{m}m{s:02d}s"


class HighlightExportRequest(BaseModel):
    """Body of POST /api/highlights/export. Identifies the source video
    every way the rest of the app can (a session external token, the
    project's `video_file` which may be a bare `videos/` name OR an
    absolute path, or a plain `videos/` basename) plus the highlight's
    source-time range. The token is preferred but optional: after a
    server restart the session token is gone, so `video_file` is the
    durable fallback — resolved the same way the renderer's
    `_resolve_source` does."""
    name: str | None = None
    token: str | None = None
    video_file: str | None = None
    start: float = Field(ge=0.0)
    end: float = Field(gt=0.0)
    project_name: str = "match"
    index: int = Field(default=1, ge=1)


def _resolve_export_source(req: "HighlightExportRequest") -> Path:
    """Token first (cheapest, already validated on register); fall back to
    `video_file` / `name`. A bare name is confined to `videos/`; an
    absolute path is trusted directly, exactly like the render pipeline
    (the path originates from the operator's own project, not the browser)."""
    if req.token:
        try:
            return _resolve_external_video(req.token)
        except HTTPException:
            pass  # stale token (session restart) — fall through to the path
    vf = (req.video_file or req.name or "").strip()
    if not vf:
        raise HTTPException(status_code=400, detail="No source video in request")
    p = Path(vf)
    return p if p.is_absolute() else _resolve_inside(config.videos_dir, vf)


@router.post("/api/highlights/export")
def export_highlight(req: HighlightExportRequest) -> dict:
    """Cut the raw source-video segment for one highlight to
    `output/<project>_hl<NN>_<start>-<end>.mp4` (NVENC re-encode so the
    cut is frame-accurate, no slow-mo / scoreboard). Returns the saved
    name + a `/api/output/<name>` URL the frontend uses to also trigger a
    browser download."""
    src = _resolve_export_source(req)
    if not src.exists():
        raise HTTPException(status_code=404, detail=f"Source video not found: {src}")

    dur = req.end - req.start
    if dur <= 0.05:
        raise HTTPException(status_code=400, detail="Highlight range too short")

    proj = _safe_name(req.project_name) or "match"
    out_name = (
        f"{proj}_hl{req.index:02d}_"
        f"{_clip_time_tag(req.start)}-{_clip_time_tag(req.end)}.mp4"
    )
    out = config.output_dir / out_name

    args = [
        *hwaccel_input_args(),
        "-ss", f"{req.start:.3f}",
        "-i", str(src),
        "-t", f"{dur:.3f}",
        "-map", "0:v:0", "-map", "0:a:0?",  # include audio iff the source has it
        *nvenc_args(),
        *aac_args(),
        "-write_tmcd", "0",  # don't carry the camera timecode track into the clip
        str(out),
    ]
    try:
        run_ffmpeg_with_progress(args, expected_out_seconds=dur)
    except FFmpegError as e:
        raise HTTPException(status_code=500, detail=f"Clip export failed: {e}")

    return {"ok": True, "name": out_name, "path": str(out), "url": f"/api/output/{out_name}"}


@router.get("/api/outputs")
def list_outputs() -> dict:
    items = []
    for p in sorted(config.output_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if p.is_file() and p.suffix.lower() == ".mp4":
            items.append({"name": p.name, "size": p.stat().st_size, "modified": p.stat().st_mtime})
    return {"outputs": items}


# ---------- render jobs -----------------------------------------------------


@router.post("/api/render")
def start_render(req: RenderRequest) -> dict:
    name = _safe_name(req.project_name)
    plan = RenderPlan(
        project=req.project,
        project_name=name,
        include_intro=req.include_intro,
        intro_style=req.intro_style,
        output_name=req.output_name,
    )
    with _jobs_lock:
        prune_finished_jobs(_jobs)
        _jobs[plan.state.job_id] = plan.state

    def _runner():
        run_render(plan)

    threading.Thread(target=_runner, daemon=True).start()
    return {"job_id": plan.state.job_id}


@router.get("/api/render/{job_id}")
def render_status(job_id: str) -> dict:
    with _jobs_lock:
        st = _jobs.get(job_id)
    if st is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return st.to_dict()


@router.post("/api/render/{job_id}/cancel")
def cancel_render(job_id: str) -> dict:
    """Flip the cancel flag on a running job. The worker thread picks
    this up on the next ffmpeg progress line (sub-second) and terminates
    the child process, then `run_render` flags the job as 'cancelled'.
    Idempotent — calling cancel on a finished job is a no-op."""
    with _jobs_lock:
        st = _jobs.get(job_id)
    if st is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if st.status not in ("queued", "running"):
        return {"ok": True, "already": st.status}
    st.cancel_requested = True
    return {"ok": True}


@router.get("/api/render")
def list_jobs() -> dict:
    with _jobs_lock:
        return {"jobs": [s.to_dict() for s in _jobs.values()]}
