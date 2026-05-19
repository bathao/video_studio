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
from .routes_auto_trim import router as auto_trim_router
from .routes_projects import router as projects_router
from .routes_render import router as render_router
from .routes_videos import router as videos_router
from .state import FRONTEND_DIR


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Install asyncio exception handler at server start to swallow benign
    Windows-only ConnectionResetError noise from cancelled HTTP streams."""
    import asyncio
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


def main() -> None:
    import uvicorn

    uvicorn.run(
        "backend.server:app",
        host=config.get("host", "127.0.0.1"),
        port=int(config.get("port", 8765)),
        reload=False,
    )
