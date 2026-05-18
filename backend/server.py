from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
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


# PowerShell payload for `_open_or_focus_explorer`: enumerate currently
# open Explorer windows via Shell.Application COM, compare each one's
# displayed folder to the target, and bring the match to the foreground
# instead of spawning a duplicate window. Falls through to a fresh
# explorer.exe launch (with `/select,` when a file is given) if nothing
# matches.
#
# Parameters are passed in via environment variables (VS_OPEN_TARGET +
# VS_OPEN_SELECT) so quoting is never an issue. The here-string for
# Add-Type is single-quoted (literal) — its closing '@ MUST stay at
# column 0 or PowerShell errors out on the parse.
_FOCUS_EXPLORER_PS = r"""
$ErrorActionPreference = 'Stop'
$target = $env:VS_OPEN_TARGET
$selectMode = ($env:VS_OPEN_SELECT -eq '1')
if (-not $target) { exit 2 }

$resolved = [System.IO.Path]::GetFullPath($target).TrimEnd('\')
$folderPath = if ($selectMode) { [System.IO.Path]::GetDirectoryName($resolved) } else { $resolved }
$folderPath = $folderPath.TrimEnd('\')

Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class VsWin {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
}
'@

$shell = New-Object -ComObject Shell.Application
$found = $null
foreach ($w in @($shell.Windows())) {
  try {
    if ($w.FullName -notlike '*\explorer.exe') { continue }
    $p = $w.Document.Folder.Self.Path
    if ([string]::IsNullOrEmpty($p)) { continue }
    $abs = [System.IO.Path]::GetFullPath($p).TrimEnd('\')
    if ([string]::Equals($abs, $folderPath, [System.StringComparison]::OrdinalIgnoreCase)) {
      $found = $w
      break
    }
  } catch { continue }
}

if ($found) {
  $hwnd = [IntPtr]$found.HWND
  if ([VsWin]::IsIconic($hwnd)) { [void][VsWin]::ShowWindow($hwnd, 9) }
  [void][VsWin]::SetForegroundWindow($hwnd)
  exit 0
}

if ($selectMode) {
  Start-Process explorer.exe -ArgumentList "/select,`"$resolved`""
} else {
  Start-Process explorer.exe -ArgumentList "`"$resolved`""
}
exit 0
"""


def _open_or_focus_explorer(target: Path, *, select: bool) -> None:
    """Bring an existing Explorer window for `target`'s folder to the
    foreground; only spawn a new window if none is already showing it.

    `select=True` mirrors `explorer /select,<file>` — the match is on
    the file's parent directory, and the fallback launch selects the
    file. Non-Windows hosts and PowerShell errors fall back to the
    plain Popen-based launch so behaviour never regresses.
    """
    abs_target = str(Path(target).absolute())
    if sys.platform != "win32":
        subprocess.Popen(["explorer", abs_target])
        return
    try:
        env = {
            **os.environ,
            "VS_OPEN_TARGET": abs_target,
            "VS_OPEN_SELECT": "1" if select else "0",
        }
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _FOCUS_EXPLORER_PS],
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        if select:
            subprocess.Popen(["explorer", f"/select,{abs_target}"])
        else:
            subprocess.Popen(["explorer", abs_target])


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
    target = _resolve_inside(config.output_dir, name)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Output not found")
    _open_or_focus_explorer(target, select=True)
    return {"ok": True, "path": str(target)}


@app.post("/api/output-folder/open")
def open_output_folder() -> dict:
    """Open the output directory in Explorer."""
    _open_or_focus_explorer(Path(config.output_dir), select=False)
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


# ---------- auto-trim ROI workflow -----------------------------------------
#
# Phase 1a: operator clicks "Auto Trim" → modal opens → backend extracts a
# midpoint refframe → backend runs naive auto-detect → modal shows refframe +
# proposed ROI → operator confirms or edits the 4 corners → confirmed ROI
# saves to `project.info.roi_quadrilateral` AND appends to a growing
# groundtruth dataset at `dataset/roi_groundtruth/<video_hash>.json` for
# improving the detector over time.

_REFFRAME_CACHE = ROOT_DIR / "temp" / "refframes"
_ROI_GROUNDTRUTH_DIR = ROOT_DIR / "dataset" / "roi_groundtruth"


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


@app.get("/api/auto_trim/refframe")
def auto_trim_refframe(name: str | None = None, token: str | None = None) -> FileResponse:
    """Serve a JPEG refframe for the source video. Lazily extracted +
    cached under `temp/refframes/<video_id>.jpg`."""
    video = _resolve_video_for_auto_trim(name, token)
    out = _extract_refframe(video)
    return FileResponse(str(out), media_type="image/jpeg")


def _sanitize_for_json(obj):
    """Recursively convert numpy scalars/arrays to plain Python types so
    FastAPI/Pydantic can serialize the detector's debug dict. Detector
    pipeline does its own float() wrapping in hot paths, but the debug
    dict accumulates intermediate values (ratios, IoUs, statistics) where
    a numpy.float32 can slip through — this is the last line of defense."""
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    # numpy scalar (float32/int64/etc.) or 0-d array — has .item()
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        try:
            return obj.item()
        except (ValueError, TypeError):
            pass
    # numpy ndarray — convert to nested Python list
    if hasattr(obj, "tolist"):
        try:
            return obj.tolist()
        except Exception:
            pass
    return obj


@app.post("/api/auto_trim/detect_roi")
def auto_trim_detect_roi(payload: dict = Body(...)) -> dict:
    """Run ROI auto-detect across 5 frames sampled along the video and
    aggregate via cluster+median. Robust to player occlusion of the
    table in any single frame. Returns 4 normalized corners, a
    confidence score, the detection method, and debug fields the modal
    can show for troubleshooting."""
    from .roi_detector import detect_roi_multiframe
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


@app.post("/api/auto_trim/confirm_roi")
def auto_trim_confirm_roi(payload: dict = Body(...)) -> dict:
    """Operator confirmed the ROI in the modal. Persist for the (eventual)
    project save AND append to a growing groundtruth dataset that the
    detector algorithm can be retrained / re-tuned against.

    The project-state mutation happens client-side (the modal sets
    `project.info.roi_quadrilateral` directly); this endpoint exists to
    capture the labeled training example so improvements to
    roi_detector.py can be measured against many real inputs."""
    from .roi_detector import detect_roi_multiframe
    import time
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
    import shutil
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


@app.get("/api/auto_trim/groundtruth_count")
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
