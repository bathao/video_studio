"""Render jobs + output files + scoreboard/intro preview routes."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..ass import ScoreFrame, build_scoreboard_ass_text
from ..avatars import find_avatar_or_default
from ..config import CONFIG_FILE, config
from ..ffmpeg_runner import (
    FFmpegError,
    aac_args,
    hwaccel_input_args,
    nvenc_args,
    probe_video,
    run_ffmpeg_with_progress,
)
from ..models import ProjectData, RenderRequest
from ..renderer import RenderPlan, render_intro_clip, run_render
from .retrain import gpu_busy_reason
from .routes_auto_trim import _video_identity
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
        handicap_receiver=p.info.handicap_receiver,
        handicap_pattern=p.info.handicap_pattern,
    )
    return text


# ---------- intro preview ---------------------------------------------------


_INTRO_PREVIEW_DIR = config.temp_dir / "intro_preview"
_INTRO_PREVIEW_MAX_AGE_S = 7 * 24 * 3600.0


def _intro_preview_key(video_id: str, intro_style: str, info) -> str:
    """Cache key over everything that changes the preview's pixels:
    source identity, intro style, every name/team field, the resolved
    avatar files (path + mtime — catches photo swaps in assets/avatars/)
    and config.json's mtime (folds in the intro_* tuning knobs without
    enumerating them). Same key → the cached mp4 is byte-fresh."""
    style = (intro_style or "cinematic").lower()
    is_doubles = ((info.match_type or "single").lower() == "double")
    names = [info.p1, info.p2] + ([info.p3, info.p4] if is_doubles else [])
    avatars: list[list | None] = []
    if style != "text":
        for n in names:
            p, _ = find_avatar_or_default(n)
            avatars.append([str(p), int(p.stat().st_mtime)] if p else None)
    try:
        config_stamp = int(CONFIG_FILE.stat().st_mtime)
    except OSError:
        config_stamp = 0
    raw = json.dumps({
        "video": video_id,
        "style": style,
        "tournament": info.tournament,
        "names": names,
        "teams": [info.p1_team, info.p2_team],
        "match_type": info.match_type,
        "avatars": avatars,
        "config": config_stamp,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _prune_old_intro_previews() -> None:
    """Previews are tiny (~1-5 MB) but write-only — age them out so the
    cache dir can't grow unbounded across weeks of title tweaking."""
    cutoff = time.time() - _INTRO_PREVIEW_MAX_AGE_S
    try:
        for f in _INTRO_PREVIEW_DIR.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError:
        pass


class IntroPreviewRequest(BaseModel):
    """Body of POST /api/preview/intro. Carries the full project (the
    intro reads info.* only) + the render panel's intro style + the
    session token for externally-picked source files."""
    project: ProjectData
    intro_style: str = "cinematic"   # "cinematic" | "text"
    token: str | None = None


@router.post("/api/preview/intro")
def preview_intro(req: IntroPreviewRequest) -> dict:
    """Render ONLY the intro clip (~4 s) with the exact production code
    path (`render_intro_clip` — same function `_intro_stage` calls) so
    the operator can check title fit / avatars in seconds instead of
    waiting out a full render. Cached by content key; a repeat click
    with nothing changed returns instantly."""
    busy = gpu_busy_reason()
    if busy is not None:
        raise HTTPException(
            status_code=409,
            detail=f"GPU busy: {busy} — preview when it finishes",
        )
    src = _resolve_request_source(req.token, req.project.info.video_file)
    if not src.exists():
        raise HTTPException(status_code=404, detail=f"Source video not found: {src}")

    probe = probe_video(src)
    key = _intro_preview_key(_video_identity(src), req.intro_style, req.project.info)
    _INTRO_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    _prune_old_intro_previews()
    out = _INTRO_PREVIEW_DIR / f"{key}.mp4"
    meta_path = _INTRO_PREVIEW_DIR / f"{key}.json"

    if out.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return {**meta, "cached": True}

    try:
        used_cinematic, placeholders = render_intro_clip(
            out_path=out,
            src=src,
            width=probe["width"], height=probe["height"], fps=probe["fps"],
            info=req.project.info,
            intro_style=req.intro_style,
        )
    except FFmpegError as e:
        raise HTTPException(status_code=500, detail=f"Intro preview failed: {e}")

    meta = {
        "ok": True,
        "url": f"/api/preview/intro/{key}.mp4",
        "used_cinematic": used_cinematic,
        "placeholders": placeholders,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return {**meta, "cached": False}


@router.get("/api/preview/intro/{name}")
def fetch_intro_preview(name: str) -> FileResponse:
    """Serve a rendered intro preview inline for the modal's <video>."""
    target = _resolve_inside(_INTRO_PREVIEW_DIR, name)
    if not target.exists() or target.suffix.lower() != ".mp4":
        raise HTTPException(status_code=404, detail="Preview not found")
    return FileResponse(str(target), media_type="video/mp4")


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


def _resolve_request_source(token: str | None, video_file: str | None) -> Path:
    """Token first (cheapest, already validated on register); fall back to
    `video_file`. A bare name is confined to `videos/`; an absolute path
    is trusted directly, exactly like the render pipeline (the path
    originates from the operator's own project, not the browser). Shared
    by highlight export and the intro preview."""
    if token:
        try:
            return _resolve_external_video(token)
        except HTTPException:
            pass  # stale token (session restart) — fall through to the path
    vf = (video_file or "").strip()
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
    src = _resolve_request_source(req.token, req.video_file or req.name)
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
