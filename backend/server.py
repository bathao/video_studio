from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

from contextlib import asynccontextmanager

from pydantic import BaseModel, Field

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .ass import ScoreFrame, build_scoreboard_ass_text
from .avatars import find_avatar
from .config import config
from .ffmpeg_runner import probe_video
from .models import ProjectData, RenderRequest
from .renderer import RenderPlan, RenderState, run_render

ROOT_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT_DIR / "frontend"

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".ts"}
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._\- ]+$")

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Install asyncio exception handler at server start to swallow benign
    Windows-only ConnectionResetError noise from cancelled HTTP streams."""
    import asyncio
    import sys
    if sys.platform == "win32":
        def _handler(loop, context):
            exc = context.get("exception")
            if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
                return
            loop.default_exception_handler(context)
        asyncio.get_running_loop().set_exception_handler(_handler)
    yield


app = FastAPI(title="Table Tennis Studio", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve assets and frontend
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """Disable caching for /static/* and the index page so the browser
    never serves a stale app.js after a code change. The cost (always
    refetching ~30 KB of HTML/JS/CSS on each load) is negligible on
    localhost."""
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# In-memory job registry. Renders are short-running and local-only,
# so we don't need persistence here.
_jobs: dict[str, RenderState] = {}
_jobs_lock = threading.Lock()


# In-memory registry of "external" videos — files chosen by the user
# from anywhere on disk via the native file picker (or restored from a
# saved project). Keyed by a short token derived from the absolute path,
# so the streaming URL doesn't expose the path. Lost on server restart;
# the frontend re-registers on project load.
_external_videos: dict[str, Path] = {}
_external_lock = threading.Lock()


def _validate_video_path(path: Path) -> Path:
    """Resolve the path, ensure it points at an existing video file, or
    raise an appropriate HTTPException."""
    abs_path = path.resolve()
    if not abs_path.exists() or not abs_path.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {abs_path}")
    if abs_path.suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(status_code=400, detail=f"Not a supported video file: {abs_path.suffix}")
    return abs_path


def _register_validated_external(abs_path: Path) -> str:
    """Stash an already-validated absolute path under a stable token."""
    token = hashlib.sha1(str(abs_path).encode("utf-8")).hexdigest()[:16]
    with _external_lock:
        _external_videos[token] = abs_path
    return token


def _register_external_video(path: Path) -> tuple[str, Path]:
    abs_path = _validate_video_path(path)
    return _register_validated_external(abs_path), abs_path


def _resolve_external_video(token: str) -> Path:
    with _external_lock:
        p = _external_videos.get(token)
    if p is None or not p.exists():
        raise HTTPException(status_code=404, detail="External video not registered (re-pick the file)")
    return p


def _safe_name(name: str) -> str:
    if not name or not SAFE_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid name (alphanumerics, spaces, dot/dash/underscore only)")
    return name


def _resolve_inside(base: Path, name: str) -> Path:
    """Resolve `base / name` and refuse path-traversal."""
    candidate = (base / name).resolve()
    base_resolved = base.resolve()
    if base_resolved not in candidate.parents and candidate != base_resolved:
        raise HTTPException(status_code=400, detail="Path escapes base directory")
    return candidate


# ---------- routes ----------------------------------------------------------


@app.get("/")
def index() -> Response:
    """
    Serve index.html with a cache-busting `?v=<mtime>` appended to every
    /static/*.js and /static/*.css reference. The browser treats a new
    URL as a fresh resource and won't reuse a cached copy from before a
    code change.
    """
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    js_v  = int((FRONTEND_DIR / "app.js").stat().st_mtime)
    css_v = int((FRONTEND_DIR / "styles.css").stat().st_mtime)
    html = html.replace("/static/app.js", f"/static/app.js?v={js_v}")
    html = html.replace("/static/styles.css", f"/static/styles.css?v={css_v}")
    return Response(content=html, media_type="text/html; charset=utf-8")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "encoder": config.encoder, "preset": config.preset}


@app.get("/api/videos")
def list_videos() -> dict:
    videos_dir = config.videos_dir
    items = []
    for p in sorted(videos_dir.iterdir()):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            try:
                size = p.stat().st_size
            except OSError:
                continue
            items.append({"name": p.name, "size": size})
    return {"dir": str(videos_dir), "videos": items}


@app.get("/api/videos/{name}/probe")
def probe(name: str) -> dict:
    target = _resolve_inside(config.videos_dir, name)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    try:
        return probe_video(target)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _stream_file(target: Path, request: Request) -> Response:
    """
    Range-aware streaming for the HTML5 <video> tag. We implement Range
    ourselves rather than using FileResponse because the browser will
    seek and we want efficient partial reads from large files.
    """
    if not target.exists():
        raise HTTPException(status_code=404, detail="Video not found")

    file_size = target.stat().st_size
    range_header = request.headers.get("range") or request.headers.get("Range")
    mime = "video/mp4"
    if target.suffix.lower() == ".webm":
        mime = "video/webm"
    elif target.suffix.lower() == ".mkv":
        mime = "video/x-matroska"

    # Cap any single Range response so we never have to buffer or send
    # gigabytes through a single asyncio write (Windows WSASend chokes
    # past a few hundred MB and raises "buffer too large"). The browser
    # will issue further Range requests as it seeks/buffers ahead.
    # 32 MiB is enough for ~10s of 25 Mbps 2K video, which keeps seek
    # round-trips low on multi-gigabyte source files.
    MAX_RANGE_BYTES = 32 * 1024 * 1024  # 32 MiB per response
    READ_CHUNK = 512 * 1024  # 512 KiB per asyncio write

    if range_header:
        m = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if not m:
            raise HTTPException(status_code=416, detail="Invalid Range header")
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else file_size - 1
        end = min(end, file_size - 1, start + MAX_RANGE_BYTES - 1)
        if start >= file_size:
            raise HTTPException(status_code=416, detail="Range out of bounds")
        chunk_size = end - start + 1

        def gen():
            with open(target, "rb") as f:
                f.seek(start)
                remaining = chunk_size
                while remaining > 0:
                    data = f.read(min(READ_CHUNK, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(chunk_size),
            "Content-Type": mime,
        }
        return StreamingResponse(gen(), status_code=206, headers=headers, media_type=mime)

    # No range: stream the whole file in small chunks too. FileResponse
    # would normally do this, but we want explicit control on Windows.
    def gen_full():
        with open(target, "rb") as f:
            while True:
                data = f.read(READ_CHUNK)
                if not data:
                    break
                yield data

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(file_size),
        "Content-Type": mime,
    }
    return StreamingResponse(gen_full(), headers=headers, media_type=mime)


@app.get("/api/videos/{name}/stream")
def stream_video(name: str, request: Request) -> Response:
    target = _resolve_inside(config.videos_dir, name)
    return _stream_file(target, request)


# ---------- external (browse-anywhere) videos ------------------------------


@app.post("/api/videos/browse")
def browse_video() -> dict:
    """
    Open a native OS file picker so the user can pick a video file from
    anywhere on disk. Default folder is the configured videos_dir. Returns
    the chosen file's token + metadata, or {"cancelled": true} if the user
    closed the dialog without picking.

    The picker runs in a subprocess (tkinter) so it doesn't block the
    asyncio loop or share state with FastAPI's worker thread.
    """
    initial_dir = str(config.videos_dir)
    script = textwrap.dedent(
        """
        import sys, tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
        path = filedialog.askopenfilename(
            initialdir=sys.argv[1],
            title="Select Source Video",
            filetypes=[
                ("Video files", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.ts"),
                ("All files", "*.*"),
            ],
        )
        sys.stdout.write(path or "")
        root.destroy()
        """
    )
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, initial_dir],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
            creationflags=creation_flags,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="File picker timed out")
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"File picker failed: {result.stderr.strip()[-500:]}")
    chosen = (result.stdout or "").strip()
    if not chosen:
        return {"cancelled": True}
    abs_path = _validate_video_path(Path(chosen))

    # If the picked file lives directly inside videos_dir, it's no
    # different from picking it from the list — just stream it under
    # its bare name. The token/registry path is reserved for files
    # outside videos_dir (or in a subfolder).
    try:
        rel = abs_path.relative_to(config.videos_dir.resolve())
    except ValueError:
        rel = None
    if rel is not None and len(rel.parts) == 1:
        return {
            "kind": "local",
            "name": rel.name,
            "path": str(abs_path),
            "size": abs_path.stat().st_size,
        }

    token = _register_validated_external(abs_path)
    return {
        "kind": "external",
        "token": token,
        "name": abs_path.name,
        "path": str(abs_path),
        "size": abs_path.stat().st_size,
    }


@app.post("/api/videos/external/register")
def register_external_video(payload: dict = Body(...)) -> dict:
    """
    Re-register an absolute path the frontend already knows about (used
    when loading a saved project that referenced a file outside videos_dir).
    Tokens are session-scoped; this restores them without going through
    the file picker again.
    """
    raw = (payload.get("path") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Missing 'path' field")
    token, abs_path = _register_external_video(Path(raw))
    return {
        "token": token,
        "name": abs_path.name,
        "path": str(abs_path),
        "size": abs_path.stat().st_size,
    }


@app.get("/api/videos/external/{token}/probe")
def probe_external(token: str) -> dict:
    target = _resolve_external_video(token)
    try:
        return probe_video(target)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/videos/external/{token}/stream")
def stream_external(token: str, request: Request) -> Response:
    target = _resolve_external_video(token)
    return _stream_file(target, request)


# ---------- avatars ---------------------------------------------------------


_AVATAR_MIME = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


@app.get("/api/avatars/{name}")
def avatar_status(name: str) -> dict:
    """Tells the UI whether an avatar exists for this player name. Used
    for the live thumbnail preview next to the name input."""
    p = find_avatar(name)
    return {"name": name, "exists": p is not None, "path": str(p) if p else None}


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


@app.post("/api/preview/scoreboard.ass", response_class=PlainTextResponse)
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


@app.get("/api/avatars/{name}/preview")
def avatar_preview(name: str) -> Response:
    p = find_avatar(name)
    if not p:
        raise HTTPException(status_code=404, detail="No avatar")
    mime = _AVATAR_MIME.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(str(p), media_type=mime)


# ---------- projects --------------------------------------------------------


@app.get("/api/projects")
def list_projects() -> dict:
    items = []
    for p in sorted(config.projects_dir.iterdir()):
        if p.is_file() and p.suffix.lower() == ".json":
            items.append({"name": p.stem, "modified": p.stat().st_mtime})
    return {"projects": items}


@app.get("/api/projects/{name}")
def get_project(name: str) -> dict:
    name = _safe_name(name)
    target = _resolve_inside(config.projects_dir, f"{name}.json")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Project not found")
    with open(target, "r", encoding="utf-8") as f:
        return json.load(f)


@app.put("/api/projects/{name}")
def save_project(name: str, project: ProjectData) -> dict:
    name = _safe_name(name)
    target = _resolve_inside(config.projects_dir, f"{name}.json")
    with open(target, "w", encoding="utf-8") as f:
        json.dump(project.model_dump(), f, ensure_ascii=False, indent=2)
    return {"ok": True, "name": name, "path": str(target)}


@app.delete("/api/projects/{name}")
def delete_project(name: str) -> dict:
    name = _safe_name(name)
    target = _resolve_inside(config.projects_dir, f"{name}.json")
    if target.exists():
        target.unlink()
    return {"ok": True}


@app.get("/api/output/{name}")
def fetch_output(name: str) -> FileResponse:
    """Serve the output inline so the browser plays it instead of downloading."""
    target = _resolve_inside(config.output_dir, name)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Output not found")
    return FileResponse(str(target), media_type="video/mp4")


@app.post("/api/output/{name}/reveal")
def reveal_output(name: str) -> dict:
    """Open Windows Explorer with the file selected."""
    import subprocess
    target = _resolve_inside(config.output_dir, name)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Output not found")
    # /select, expects the path immediately after; spaces in `target` are fine
    # because we pass argv as a list (no shell parsing).
    subprocess.Popen(["explorer", f"/select,{target}"])
    return {"ok": True, "path": str(target)}


@app.post("/api/output-folder/open")
def open_output_folder() -> dict:
    """Open the output directory in Explorer."""
    import subprocess
    subprocess.Popen(["explorer", str(config.output_dir)])
    return {"ok": True, "path": str(config.output_dir)}


@app.get("/api/outputs")
def list_outputs() -> dict:
    items = []
    for p in sorted(config.output_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if p.is_file() and p.suffix.lower() == ".mp4":
            items.append({"name": p.name, "size": p.stat().st_size, "modified": p.stat().st_mtime})
    return {"outputs": items}


# ---------- render jobs -----------------------------------------------------


@app.post("/api/render")
def start_render(req: RenderRequest) -> dict:
    name = _safe_name(req.project_name)
    plan = RenderPlan(
        project=req.project,
        project_name=name,
        include_intro=req.include_intro,
        intro_style=req.intro_style,
        include_replays=req.include_replays,
        include_main=req.include_main,
        output_name=req.output_name,
    )
    with _jobs_lock:
        _jobs[plan.state.job_id] = plan.state

    def _runner():
        run_render(plan)

    threading.Thread(target=_runner, daemon=True).start()
    return {"job_id": plan.state.job_id}


@app.get("/api/render/{job_id}")
def render_status(job_id: str) -> dict:
    with _jobs_lock:
        st = _jobs.get(job_id)
    if st is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return st.to_dict()


@app.post("/api/render/{job_id}/cancel")
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


@app.get("/api/render")
def list_jobs() -> dict:
    with _jobs_lock:
        return {"jobs": [s.to_dict() for s in _jobs.values()]}


# ---------- entrypoint -----------------------------------------------------


def main() -> None:
    import uvicorn

    uvicorn.run(
        "backend.server:app",
        host=config.get("host", "127.0.0.1"),
        port=int(config.get("port", 8765)),
        reload=False,
    )


if __name__ == "__main__":
    main()
