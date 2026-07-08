"""Module-level state shared across route modules.

These are deliberately mutable globals — FastAPI's in-process model
makes them safe with the locks below. They survive only within a
single uvicorn process; the frontend re-registers any external-video
tokens on project load.
"""

from __future__ import annotations

import queue
import re
import threading
from dataclasses import dataclass, field
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

# Auto-trim job registry — Phase 1b Step 2. Mirrors the render-job
# pattern (one dict + one lock), but each entry also owns a queue.Queue
# of events that the SSE handler drains. The worker thread pushes
# (event_type, data) tuples; None is the sentinel for "stream done".
_AUTOTRIM_CACHE_DIR = ROOT_DIR / "temp" / "auto_trim_cache"


@dataclass
class AutoTrimJobState:
    """Per-job state for one rally-detection run.

    Lives in `_auto_trim_jobs` keyed by `job_id`. Held by both the worker
    thread (writer) and the SSE handler (reader of `event_queue`). The
    primitive fields are written from the worker and read everywhere,
    but they're only ever read for status display — eventual consistency
    is fine here, no per-field lock needed.

    `trims` must be PUBLISHED, never mutated in place: the worker builds
    a local list and assigns it in one step, so the status endpoint can
    iterate whatever list object it sees without a lock."""
    job_id: str
    status: str = "queued"  # queued | running | done | error | cancelled
    progress: float = 0.0
    stage: str = ""
    error: str = ""
    cancel: bool = False
    trims: list = field(default_factory=list)
    cache_key: str = ""
    cache_hit: bool = False
    started_at: float = 0.0
    finished_at: float = 0.0
    # SSE event stream. Worker pushes (event_type, data); a final None
    # closes the stream. Unbounded — events fit in memory easily for a
    # 22 min video (~150 progress emits + a few dozen trims).
    event_queue: "queue.Queue" = field(default_factory=queue.Queue)


_auto_trim_jobs: dict[str, AutoTrimJobState] = {}
_auto_trim_lock = threading.Lock()


# Auto-score (Live Score Auto tab) job registry — same shape as
# auto-trim: one dict + one lock, each job owning an SSE event queue.
_AUTOSCORE_CACHE_DIR = ROOT_DIR / "temp" / "auto_score_cache"


@dataclass
class AutoScoreJobState:
    """Per-job state for one rally-segmentation run (Auto Score tab).

    Same concurrency contract as AutoTrimJobState: primitive fields are
    eventually-consistent status display; `proposals` must be PUBLISHED
    (built locally, assigned once), never mutated in place."""
    job_id: str
    status: str = "queued"  # queued | running | done | error | cancelled
    progress: float = 0.0
    stage: str = ""
    error: str = ""
    cancel: bool = False
    proposals: list = field(default_factory=list)
    cache_key: str = ""
    cache_hit: bool = False
    started_at: float = 0.0
    finished_at: float = 0.0
    event_queue: "queue.Queue" = field(default_factory=queue.Queue)


_auto_score_jobs: dict[str, AutoScoreJobState] = {}
_auto_score_lock = threading.Lock()


# Job-registry eviction. Neither registry was ever pruned, so a long
# editing session accumulated every RenderState / AutoTrimJobState (the
# latter owning an unbounded queue.Queue) for the life of the process.
# Keep the most recent N finished jobs so status endpoints still answer
# for recently-finished work; running/queued jobs are never evicted.
_TERMINAL_STATUSES = ("done", "error", "cancelled")
_MAX_FINISHED_JOBS = 20


def prune_finished_jobs(registry: dict) -> None:
    """Evict the oldest finished jobs beyond `_MAX_FINISHED_JOBS`.

    Works for both `_jobs` (RenderState) and `_auto_trim_jobs`
    (AutoTrimJobState) — both expose `.status`. Caller must hold the
    registry's lock. Dict insertion order makes `finished[:excess]`
    the oldest entries."""
    finished = [k for k, v in registry.items() if v.status in _TERMINAL_STATUSES]
    excess = len(finished) - _MAX_FINISHED_JOBS
    if excess > 0:
        for k in finished[:excess]:
            registry.pop(k, None)
