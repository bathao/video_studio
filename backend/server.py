from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

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


@app.get("/api/videos/{name}/stream")
def stream_video(name: str, request: Request) -> Response:
    """
    Range-aware streaming for the HTML5 <video> tag. We implement Range
    ourselves rather than using FileResponse because the browser will
    seek and we want efficient partial reads from large files.
    """
    target = _resolve_inside(config.videos_dir, name)
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
        include_highlights=req.include_highlights,
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
