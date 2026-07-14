"""Video + external-video + avatar HTTP routes.

Grouped together because they all serve "source media" — local videos
under videos/, user-picked files anywhere on disk (token-keyed), and
player photos under assets/avatars/.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from ..avatars import find_avatar, list_avatar_names
from ..config import config
from ..ffmpeg_runner import probe_video
from .state import VIDEO_EXTS
from .utils import (
    _register_external_video,
    _register_validated_external,
    _resolve_external_video,
    _resolve_inside,
    _validate_video_path,
)


router = APIRouter()


# ---------- local videos ---------------------------------------------------


@router.get("/api/videos")
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


@router.get("/api/videos/{name}/probe")
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


@router.get("/api/videos/{name}/stream")
def stream_video(name: str, request: Request) -> Response:
    target = _resolve_inside(config.videos_dir, name)
    return _stream_file(target, request)


# ---------- external (browse-anywhere) videos ------------------------------


@router.post("/api/videos/browse")
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


@router.post("/api/videos/external/register")
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


@router.get("/api/videos/external/{token}/probe")
def probe_external(token: str) -> dict:
    target = _resolve_external_video(token)
    try:
        return probe_video(target)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/videos/external/{token}/stream")
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


@router.get("/api/avatars")
def list_avatars() -> dict:
    """Roster of player names that have a photo on disk — powers the
    type-ahead suggestions in the Setup panel name inputs."""
    return {"names": list_avatar_names()}


@router.get("/api/avatars/{name}")
def avatar_status(name: str) -> dict:
    """Tells the UI whether an avatar exists for this player name. Used
    for the live thumbnail preview next to the name input."""
    p = find_avatar(name)
    return {"name": name, "exists": p is not None, "path": str(p) if p else None}


@router.get("/api/avatars/{name}/preview")
def avatar_preview(name: str) -> Response:
    p = find_avatar(name)
    if not p:
        raise HTTPException(status_code=404, detail="No avatar")
    mime = _AVATAR_MIME.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(str(p), media_type=mime)
