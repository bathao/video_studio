"""FastAPI app instance + lifespan + middleware + static files + entry.

This module is the composition root: it creates the singleton `app`,
includes the routers from every `routes_*.py` module, mounts the
frontend static dir, and exposes `main()` for the uvicorn launcher.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from ..config import config
from .routes_auto_score import router as auto_score_router
from .routes_auto_trim import router as auto_trim_router
from .routes_projects import router as projects_router
from .routes_render import router as render_router
from .routes_training import router as training_router
from .routes_videos import router as videos_router
from .state import FRONTEND_DIR
from .utils import prune_stale_job_dirs


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Install asyncio exception handler at server start to swallow benign
    Windows-only ConnectionResetError noise from cancelled HTTP streams.

    Also kicks off ROI detector warmup in a background daemon thread
    (unless `roi_warmup_enabled` is false in config.json): YOLO weights
    load + CUDA JIT (~2-3s) and groundtruth example cache build (~54
    imreads + ORB feature extraction) move from the operator's first
    ⚡ Auto Trim click to server startup. Threaded so it doesn't block
    uvicorn from accepting connections. Disabled → server starts light
    (no torch in RAM); the first Auto Trim click pays the load instead."""
    import asyncio
    import logging
    import threading

    # Failed renders deliberately keep temp/<job_id> for inspection;
    # reclaim the ones nobody will ever look at again (age-gated).
    stale = prune_stale_job_dirs(config.temp_dir)
    if stale:
        logging.getLogger("startup").info(
            "pruned %d stale render job dir(s): %s",
            len(stale), ", ".join(stale))

    if sys.platform == "win32":
        def _handler(loop, context):
            exc = context.get("exception")
            if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
                return
            loop.default_exception_handler(context)
        asyncio.get_running_loop().set_exception_handler(_handler)

    def _warmup_worker() -> None:
        log = logging.getLogger("roi.warmup")
        try:
            from ..roi_yolo import warm_up as _warm_yolo
            from ..roi import warm_up_groundtruth_cache as _warm_cache
        except Exception:
            log.exception("ROI warmup imports failed")
            return
        import time
        t0 = time.monotonic()
        try:
            ok_yolo = _warm_yolo()
        except Exception:
            log.exception("YOLO warmup failed")
            ok_yolo = False
        t1 = time.monotonic()
        try:
            n_examples = _warm_cache()
        except Exception:
            log.exception("Groundtruth cache warmup failed")
            n_examples = -1
        t2 = time.monotonic()
        log.info(
            "ROI warmup done: yolo=%s (%.2fs), groundtruth_examples=%s (%.2fs)",
            "ok" if ok_yolo else "skipped", t1 - t0, n_examples, t2 - t1,
        )

    if config.roi_warmup_enabled:
        threading.Thread(target=_warmup_worker, daemon=True, name="roi_warmup").start()
    else:
        logging.getLogger("roi.warmup").info(
            "ROI warmup disabled by config — first Auto Trim click pays the load",
        )
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


# Mount feature-area routers.
app.include_router(videos_router)
app.include_router(projects_router)
app.include_router(render_router)
app.include_router(auto_trim_router)
app.include_router(auto_score_router)
app.include_router(training_router)


def main() -> None:
    import uvicorn

    uvicorn.run(
        "backend.server:app",
        host=config.get("host", "127.0.0.1"),
        port=int(config.get("port", 8765)),
        reload=False,
    )
