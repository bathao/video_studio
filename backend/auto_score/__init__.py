"""Auto Score package — Phase 1 (semi-auto): unanchored rally
segmentation proposals for the Live Score Auto tab. Winner detection
(VLM/classifier/solver) arrives with Gate G0b — see
docs/AUTO_SCORE_PLAN.md."""

from backend.auto_score.rally_segmenter import (
    TUNED2,
    RallySegmenterParams,
    run_rally_segmentation,
    segment_rallies,
)

__all__ = [
    "TUNED2",
    "RallySegmenterParams",
    "run_rally_segmentation",
    "segment_rallies",
]
