from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class ProjectInfo(BaseModel):
    tournament: str = ""
    p1: str = "Player 1"
    p2: str = "Player 2"
    video_file: str = ""


class TrimSegment(BaseModel):
    start: float
    end: float


class Highlight(BaseModel):
    start: float
    end: float
    slow_mo: bool = False
    label: str = ""


class ScoreEvent(BaseModel):
    timestamp: float
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
    include_highlights: bool = True
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
