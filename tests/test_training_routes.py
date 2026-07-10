"""Tests for the training-status dashboard endpoint (routes_training).

The endpoint is a thin aggregator — the three sources are unit-tested
in their own modules (test_dataset.py for corpus stats, test_retrain.py
for groundtruth summary + retrain state), so here we only pin the
response shape the frontend (training_status.js) depends on.
"""

from __future__ import annotations

from backend.server import routes_training


def test_training_status_aggregates_three_sources(monkeypatch):
    monkeypatch.setattr(
        routes_training, "training_corpus_stats",
        lambda: {"target_matches": 15, "labeled_matches": 2,
                 "ready": False, "matches": []})
    monkeypatch.setattr(
        routes_training, "groundtruth_summary",
        lambda: {"count": 57, "videos": [{"video_name": "x", "history_count": 1}],
                 "yolo_model_exists": True, "confirms_since_yolo_train": 3})
    monkeypatch.setattr(
        routes_training, "retrain_status",
        lambda: {"status": "running", "message": "epoch 5/120",
                 "progress": 0.42, "started_at": 1.0, "finished_at": 0.0})

    out = routes_training.training_status()

    assert out["corpus"]["labeled_matches"] == 2
    # roi keeps only the scalar stats — the per-video list stays on the
    # Auto Trim modal's own endpoint.
    assert out["roi"] == {"count": 57, "yolo_model_exists": True,
                          "confirms_since_yolo_train": 3}
    assert out["retrain"]["progress"] == 0.42
