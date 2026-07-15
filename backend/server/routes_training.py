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
  g0b      live G0b fine-tune heartbeat (train_g0b_lora.py rewrites
           runs/g0b/<run>/heartbeat.json every step). The training
           process is NOT managed by this server — health is derived
           from heartbeat freshness: a running status with a stale
           timestamp means the process died or the machine slept.

Thin by design: all logic lives in importable, unit-tested modules.
The frontend consumer is frontend/training_status.js (top-bar button).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import APIRouter

from ..dataset import training_corpus_stats
from .retrain import groundtruth_summary, retrain_status
from .state import ROOT_DIR

router = APIRouter()

_G0B_RUNS_DIR = ROOT_DIR / "runs" / "g0b"
_HEARTBEAT_STALE_S = 120.0


def read_g0b_heartbeat(runs_dir: Path | None = None,
                       now: float | None = None) -> dict | None:
    """Latest G0b train heartbeat + derived health, or None when no
    run has ever written one.

    health: "running" (fresh heartbeat), "stalled" (status says
    running but the heartbeat is older than _HEARTBEAT_STALE_S — the
    process died, was paused, or the box slept), "done", "error".
    Corrupt heartbeat files surface as health="unreadable" rather than
    disappearing (fail-loud rule, 2026-07-14)."""
    if runs_dir is None:
        runs_dir = _G0B_RUNS_DIR
    if now is None:
        now = time.time()
    candidates = sorted(runs_dir.glob("*/heartbeat.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None
    path = candidates[0]
    try:
        hb = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"health": "unreadable", "run_name": path.parent.name,
                "error": str(e)}
    age = max(0.0, now - float(hb.get("ts") or 0))
    status = str(hb.get("status") or "")
    if status == "running":
        health = "stalled" if age > _HEARTBEAT_STALE_S else "running"
    elif status in ("done", "error"):
        health = status
    else:
        health = "unreadable"
    return {**hb, "age_s": round(age, 1), "health": health}


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
        "g0b": read_g0b_heartbeat(),
    }
