"""Training-status dashboard endpoint.

One GET aggregating everything the operator needs to answer "is my
manual production paying off, and is any training action due?" without
leaving the web UI:

  corpus   auto-score corpus readiness for the G0b winner-detection
           fine-tune milestone (~15-20 fully-labeled matches) —
           computed by dataset.training_corpus_stats from the archive
           manifest + per-entry project snapshots.
  roi      ROI groundtruth count + YOLO staleness (confirms newer than
           the model file) — retrain.groundtruth_summary, same numbers
           the Auto Trim modal shows.
  retrain  live retrain job snapshot (status / message / progress) so
           the dashboard can render a progress bar for a run started
           from either the dashboard or the Auto Trim modal.

Thin by design: all logic lives in importable, unit-tested modules.
The frontend consumer is frontend/training_status.js (top-bar button).
"""

from __future__ import annotations

from fastapi import APIRouter

from ..dataset import training_corpus_stats
from .retrain import groundtruth_summary, retrain_status

router = APIRouter()


@router.get("/api/training/status")
def training_status() -> dict:
    gt = groundtruth_summary()
    return {
        "corpus": training_corpus_stats(),
        "roi": {
            "count": gt["count"],
            "yolo_model_exists": gt["yolo_model_exists"],
            "confirms_since_yolo_train": gt["confirms_since_yolo_train"],
        },
        "retrain": retrain_status(),
    }
