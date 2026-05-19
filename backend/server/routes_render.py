"""Render jobs + output files + scoreboard preview routes."""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..ass import ScoreFrame, build_scoreboard_ass_text
from ..config import config
from ..models import ProjectData, RenderRequest
from ..renderer import RenderPlan, run_render
from .state import _jobs, _jobs_lock
from .utils import _open_or_focus_explorer, _resolve_inside, _safe_name


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
