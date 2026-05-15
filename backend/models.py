from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class ProjectInfo(BaseModel):
    tournament: str = ""
    # "single" → 1 vs 1; "double" → 2 vs 2 (P1+P2 vs P3+P4). The render
    # pipeline + preview branch on this to combine player names and to
    # decide how many avatars to lay out in the cinematic intro.
    match_type: str = "single"
    p1: str = "Player 1"
    p2: str = "Player 2"
    # Doubles partners. Empty in singles mode. p3 partners p1 on team 1,
    # p4 partners p2 on team 2.
    p3: str = ""
    p4: str = ""
    # Optional team / club affiliation per side. In singles this is per
    # player; in doubles each side has ONE team that covers both players
    # (so p1_team covers p1+p3, p2_team covers p2+p4). Empty hides the
    # team column entirely.
    p1_team: str = ""
    p2_team: str = ""
    video_file: str = ""
    # Best-of N format: 3, 5, or 7 sets. Lets the renderer distinguish
    # GAME POINT (next point wins this set) from MATCH POINT (next point
    # wins the entire match).
    best_of: int = 5


class TrimSegment(BaseModel):
    start: float
    end: float


class Highlight(BaseModel):
    start: float
    end: float
    label: str = ""


class ScoreEvent(BaseModel):
    timestamp: float
    # `who` is the canonical action — which player scored at this
    # timestamp (1 or 2). The score / set fields are a derived cache,
    # recomputed by the frontend whenever events change so they always
    # reflect a chronological replay of all actions.
    # `who = 0` means "unknown" (legacy projects pre-v0.2) and the
    # frontend will derive it on load.
    who: int = 0
    p1_score: int = 0
    p2_score: int = 0
    p1_set: int = 0
    p2_set: int = 0


class ProjectData(BaseModel):
    info: ProjectInfo = Field(default_factory=ProjectInfo)
    trim_segments: list[TrimSegment] = Field(default_factory=list)
    highlights: list[Highlight] = Field(default_factory=list)
    score_events: list[ScoreEvent] = Field(default_factory=list)


class RenderRequest(BaseModel):
    project_name: str
    project: ProjectData
    include_intro: bool = True
    # "cinematic" (avatars + bg blur, default) or "text" (the original
    # 3 s libass title card). Ignored when include_intro is False.
    intro_style: str = "cinematic"
    # Splice a 50%-speed replay of each highlight into main, right after
    # its real-time occurrence. When false, highlights are ignored at
    # render time (they still exist in the project for re-render later).
    include_replays: bool = True
    include_main: bool = True
    output_name: Optional[str] = None


class RenderJob(BaseModel):
    job_id: str
    status: str  # queued | running | done | error
    progress: float = 0.0
    stage: str = ""
    message: str = ""
    output_path: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
