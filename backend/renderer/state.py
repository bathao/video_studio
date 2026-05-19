"""RenderState: per-job progress + status snapshot.

Lives in its own module so `backend.server.state` can import it
without pulling in the full renderer dependency graph (ffmpeg, ass
builders, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class RenderState:
    job_id: str
    status: str = "queued"
    progress: float = 0.0
    stage: str = ""
    message: str = ""
    output_path: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: str = ""
    cancel_requested: bool = False

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "progress": round(self.progress, 4),
            "stage": self.stage,
            "message": self.message,
            "output_path": self.output_path,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }
