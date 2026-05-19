"""Module-level state shared across route modules.

These are deliberately mutable globals — FastAPI's in-process model
makes them safe with the locks below. They survive only within a
single uvicorn process; the frontend re-registers any external-video
tokens on project load.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from ..renderer import RenderState


ROOT_DIR = Path(__file__).resolve().parent.parent.parent
FRONTEND_DIR = ROOT_DIR / "frontend"

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".ts"}
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._\- ]+$")


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


# Auto-trim runtime caches.
_REFFRAME_CACHE = ROOT_DIR / "temp" / "refframes"
_ROI_GROUNDTRUTH_DIR = ROOT_DIR / "dataset" / "roi_groundtruth"
