"""FastAPI app for Table Tennis Studio.

Re-exports `app` and `main` so the historical entry points keep working:
  - `uvicorn backend.server:app` (used by `app.py::main()` itself)
  - `python -m backend.server`   (driven by `__main__.py` below)
"""

from .app import app, main

__all__ = ["app", "main"]
