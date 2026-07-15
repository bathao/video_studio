"""Tests for the training-status dashboard endpoint (routes_training).

The endpoint is a thin aggregator — the three sources are unit-tested
in their own modules (test_dataset.py for corpus stats, test_retrain.py
for groundtruth summary + retrain state), so here we only pin the
response shape the frontend (training_status.js) depends on.
"""

from __future__ import annotations

import json

from backend.server import routes_training


def _write_heartbeat(runs_dir, run_name, **fields):
    d = runs_dir / run_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "heartbeat.json").write_text(json.dumps(fields), encoding="utf-8")


def test_g0b_heartbeat_none_when_no_runs(tmp_path):
    assert routes_training.read_g0b_heartbeat(tmp_path) is None


def test_g0b_heartbeat_fresh_running(tmp_path):
    _write_heartbeat(tmp_path, "v1", status="running", step=42,
                     total_steps=210, seconds_per_step=61.0, ts=1000.0)
    hb = routes_training.read_g0b_heartbeat(tmp_path, now=1030.0)
    assert hb["health"] == "running"
    assert hb["step"] == 42
    assert hb["age_s"] == 30.0


def test_g0b_heartbeat_stale_running_is_stalled(tmp_path):
    """status says running but nothing written for >2 min — the process
    died or the machine slept. Must NOT render as a healthy run."""
    _write_heartbeat(tmp_path, "v1", status="running", step=42,
                     total_steps=210, ts=1000.0)
    hb = routes_training.read_g0b_heartbeat(tmp_path, now=1000.0 + 300)
    assert hb["health"] == "stalled"


def test_g0b_heartbeat_terminal_states_ignore_age(tmp_path):
    _write_heartbeat(tmp_path, "v1", status="done", step=210,
                     total_steps=210, ts=1000.0)
    hb = routes_training.read_g0b_heartbeat(tmp_path, now=99999.0)
    assert hb["health"] == "done"
    _write_heartbeat(tmp_path, "v1", status="error", step=7,
                     total_steps=210, ts=1000.0)
    assert routes_training.read_g0b_heartbeat(
        tmp_path, now=99999.0)["health"] == "error"


def test_g0b_heartbeat_corrupt_file_surfaces_unreadable(tmp_path):
    d = tmp_path / "v1"
    d.mkdir(parents=True)
    (d / "heartbeat.json").write_text("{broken", encoding="utf-8")
    hb = routes_training.read_g0b_heartbeat(tmp_path)
    assert hb["health"] == "unreadable"
    assert hb["run_name"] == "v1"


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
