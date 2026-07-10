from __future__ import annotations

from typing import Literal, Optional
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
    # 4 normalized [x, y] corners of the auto-trim ROI quadrilateral, ordered
    # top-left → top-right → bottom-right → bottom-left. None means "not
    # yet defined" — auto-trim modal will run backend.roi.detect_roi_multiframe() and
    # prompt the operator. Per-project so each match's camera angle / table
    # position is captured separately.
    roi_quadrilateral: Optional[list[list[float]]] = None
    # --- Handicap (điểm chấp) ---
    # Which side RECEIVES handicap points at the start of each set:
    # 0 = no handicap (default), 1 = P1/team-1 receives (P2 gives),
    # 2 = P2/team-2 receives. Applies to singles and doubles alike —
    # it's a production-display concern; training consumers only filter
    # on it for score-grammar solver work.
    handicap_receiver: Literal[0, 1, 2] = 0
    # Digit string, one digit per set, CYCLING when the match runs past
    # its length: "232" → set1=2, set2=3, set3=2, set4=2, set5=3.
    # Digit n = the receiver starts that set leading n-0; sets still
    # play to 11 win-by-2. Handicap points are baked into the start
    # score of each set — score events remain REAL rallies only (never
    # fake key presses), which is what keeps handicap matches usable
    # as segmentation / winner-label training data.
    handicap_pattern: str = Field(default="", pattern=r"^\d*$")
    # --- Auto Score training labels (operator-confirmed in the GUI at
    # production time; meaningful for singles only — doubles matches are
    # excluded from auto-score training/eval wholesale). These flow into
    # groundtruth.json → dataset/<slug>/ so the flywheel corpus carries
    # side/swap truth with no post-hoc side_truth.json backfill.
    #
    # Camera placement relative to the table. "standard" = the
    # operator's usual family (behind one player, slightly diagonal —
    # all corpus data through 2026-07-10); "side" = ~90° side-on
    # (rare); "other" = anything else. Non-standard matches are tagged
    # so training stays angle-locked (they become eval-only) and
    # because near/far side semantics only exist for "standard".
    camera_angle: Literal["standard", "side", "other"] = "standard"
    # Which side P1 plays on in set 1. Axis depends on camera_angle:
    # "standard" uses near/far (distance from the tripod), "side" uses
    # left/right (viewer's left/right of the video frame). Default
    # "near" — the operator's P1-near-in-set-1 production convention
    # (2026-07-10) for the standard angle; the GUI swaps the option
    # set when the angle changes. None = unknown (legacy projects, or
    # "other" angles where no axis is defined).
    p1_side_set1: Optional[Literal["near", "far", "left", "right"]] = "near"
    # Players swap sides after every set (standard rule, the default).
    # Rarely a venue quirk / laziness skips the swap — operator unticks.
    swap_sides_each_set: bool = True
    # Deciding-set mid-set swap at 5 points (set 5 in best-of-5).
    # Default True — swapping at 5 is the norm; False = played through
    # (operator flips when it happens), None = unknown (legacy).
    set5_mid_swap: Optional[bool] = True


class TrimSegment(BaseModel):
    start: float
    end: float
    # "manual" (operator-marked, default for legacy projects) or "auto"
    # (Auto Trim modal's Apply). The Apply path filters existing
    # source=="auto" trims before appending fresh ones, so re-running
    # auto-trim replaces only its own output and leaves manual trims
    # untouched.
    source: Literal["manual", "auto"] = "manual"


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
    # "manual" (operator key press, default for legacy projects) or
    # "auto" (Auto Score tab's Apply). Same replace-only-own-output
    # rule as TrimSegment.source.
    source: Literal["manual", "auto"] = "manual"


class ProjectData(BaseModel):
    info: ProjectInfo = Field(default_factory=ProjectInfo)
    trim_segments: list[TrimSegment] = Field(default_factory=list)
    highlights: list[Highlight] = Field(default_factory=list)
    score_events: list[ScoreEvent] = Field(default_factory=list)
    # Auto Score review-session draft (proposals + review state), shape
    # owned by frontend/auto_score/. Persisted with the project so a
    # half-finished review survives save/load. None = no session.
    auto_score_draft: Optional[dict] = None


class RenderRequest(BaseModel):
    project_name: str
    project: ProjectData
    include_intro: bool = True
    # "cinematic" (avatars + bg blur, default) or "text" (the original
    # 3 s libass title card). Ignored when include_intro is False.
    intro_style: str = "cinematic"
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
