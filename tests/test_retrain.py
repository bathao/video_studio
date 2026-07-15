"""Tests for the YOLO retrain job (backend/server/retrain.py).

The worker shells out to the two training scripts — tests fake
`subprocess.run` so nothing heavy executes; what's under test is the
guard logic (no overlap with GPU jobs / itself), the state machine,
and the model-cache invalidation hook.
"""

from __future__ import annotations

import io
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import backend.roi_yolo as roi_yolo
from backend.renderer import RenderState
from backend.server import retrain
from backend.server import state as server_state


class _FakePopen:
    """Stands in for the streaming train subprocess. Emits ultralytics-
    style epoch lines so the progress parser has something to chew on."""

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.returncode = 0
        self.stdout = io.StringIO(
            "Loading base model\n"
            "      1/10      2.5G      1.23      0.45\n"
            "     10/10      2.5G      0.80      0.30\n"
            "ok best.pt copied\n"
        )

    def wait(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


@pytest.fixture(autouse=True)
def clean_state():
    retrain._reset_for_tests()
    yield
    retrain._reset_for_tests()
    with server_state._jobs_lock:
        server_state._jobs.clear()
    with server_state._auto_trim_lock:
        server_state._auto_trim_jobs.clear()


def _wait_terminal(timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = retrain.retrain_status()
        if st["status"] in ("done", "error"):
            return st
        time.sleep(0.02)
    raise AssertionError("retrain never reached a terminal state")


def test_gpu_free_when_no_jobs():
    assert retrain.gpu_busy_reason() is None


def test_gpu_busy_when_render_running():
    with server_state._jobs_lock:
        server_state._jobs["r1"] = RenderState(job_id="r1", status="running")
    assert "render" in retrain.gpu_busy_reason()


def test_gpu_busy_when_auto_trim_detection_running():
    with server_state._auto_trim_lock:
        server_state._auto_trim_jobs["a1"] = server_state.AutoTrimJobState(
            job_id="a1", status="running")
    assert "auto-trim" in retrain.gpu_busy_reason()


def test_finished_jobs_do_not_block():
    with server_state._jobs_lock:
        server_state._jobs["r1"] = RenderState(job_id="r1", status="done")
    assert retrain.gpu_busy_reason() is None


def test_start_refused_while_render_running():
    with server_state._jobs_lock:
        server_state._jobs["r1"] = RenderState(job_id="r1", status="running")
    ok, reason = retrain.start_retrain()
    assert not ok
    assert "GPU busy" in reason
    assert retrain.retrain_status()["status"] == "idle"


def test_start_refused_while_already_running():
    with retrain._lock:
        retrain._state.status = "running"
    ok, reason = retrain.start_retrain()
    assert not ok
    assert "already running" in reason


def test_happy_path_without_existing_model_skips_comparison(monkeypatch, tmp_path):
    """First-ever train: nothing to back up → no comparison step."""
    monkeypatch.setattr(retrain, "_MODEL_PATH", tmp_path / "missing.pt")
    monkeypatch.setattr(retrain, "_MODEL_BACKUP", tmp_path / "prev.pt")
    ran: list[str] = []

    def fake_run(cmd, **kw):
        ran.append(Path(cmd[1]).name)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    def fake_popen(cmd, **kw):
        ran.append(Path(cmd[2]).name)  # cmd = [python, "-u", script]
        return _FakePopen(cmd)

    monkeypatch.setattr(retrain.subprocess, "run", fake_run)
    monkeypatch.setattr(retrain.subprocess, "Popen", fake_popen)
    reloaded: list[bool] = []
    monkeypatch.setattr(
        roi_yolo, "invalidate_model_cache", lambda: reloaded.append(True))

    ok, reason = retrain.start_retrain()
    assert ok, reason
    st = _wait_terminal()
    assert st["status"] == "done"
    assert ran == ["build_yolo_dataset.py", "train_roi_seg.py"]
    assert reloaded == [True]
    assert "SUMMARY" not in st["message"]
    assert st["progress"] == 1.0
    assert st["finished_at"] >= st["started_at"] > 0


def test_happy_path_backs_up_and_compares(monkeypatch, tmp_path):
    """With an existing model: back it up first, then after training
    run the comparison script and surface its SUMMARY line."""
    model = tmp_path / "roi_seg.pt"
    model.write_bytes(b"OLD-WEIGHTS")
    backup = tmp_path / "roi_seg.prev.pt"
    monkeypatch.setattr(retrain, "_MODEL_PATH", model)
    monkeypatch.setattr(retrain, "_MODEL_BACKUP", backup)
    ran: list[list[str]] = []

    def fake_run(cmd, **kw):
        ran.append([Path(cmd[1]).name, *cmd[2:]])
        out = "ok"
        if Path(cmd[1]).name == "compare_roi_models.py":
            out = "header\nSUMMARY: TAIL IMPROVED — within-2%: 50->53/56"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    def fake_popen(cmd, **kw):
        ran.append([Path(cmd[2]).name])
        return _FakePopen(cmd)

    monkeypatch.setattr(retrain.subprocess, "run", fake_run)
    monkeypatch.setattr(retrain.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(roi_yolo, "invalidate_model_cache", lambda: None)

    ok, reason = retrain.start_retrain()
    assert ok, reason
    st = _wait_terminal()
    assert st["status"] == "done"
    assert backup.read_bytes() == b"OLD-WEIGHTS"
    assert [r[0] for r in ran] == [
        "build_yolo_dataset.py", "train_roi_seg.py", "compare_roi_models.py"]
    # Comparison is pointed at the backup snapshot.
    assert str(backup) in ran[2]
    assert "SUMMARY: TAIL IMPROVED" in st["message"]


def test_comparison_failure_is_not_fatal(monkeypatch, tmp_path):
    """A broken comparison must never turn a successful retrain into an
    error — the verdict is advisory."""
    model = tmp_path / "roi_seg.pt"
    model.write_bytes(b"OLD")
    monkeypatch.setattr(retrain, "_MODEL_PATH", model)
    monkeypatch.setattr(retrain, "_MODEL_BACKUP", tmp_path / "prev.pt")

    def fake_run(cmd, **kw):
        if Path(cmd[1]).name == "compare_roi_models.py":
            return SimpleNamespace(returncode=2, stdout="", stderr="no frames")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(retrain.subprocess, "run", fake_run)
    monkeypatch.setattr(retrain.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(roi_yolo, "invalidate_model_cache", lambda: None)

    ok, _ = retrain.start_retrain()
    assert ok
    st = _wait_terminal()
    assert st["status"] == "done"
    assert "comparison failed" in st["message"]


def test_is_regression_parsing():
    """_is_regression is True only for a clean REGRESSED verdict.
    Broken/skipped comparisons return False here — the WORKER treats
    those as inconclusive and still rolls back (see
    test_inconclusive_comparison_rolls_back)."""
    assert retrain._is_regression(
        "SUMMARY: REGRESSED (consider restoring roi_seg.prev.pt) — "
        "within-2%: 55->50/58")
    assert retrain._is_regression(
        "SUMMARY: REGRESSED (new model misses frames the old one caught)")
    assert not retrain._is_regression("SUMMARY: IMPROVED — mean 1.1->0.6%")
    assert not retrain._is_regression(
        "SUMMARY: TAIL IMPROVED (averages equal, worst cases better)")
    assert not retrain._is_regression("SUMMARY: EQUIVALENT")
    assert not retrain._is_regression("comparison failed: no frames")
    assert not retrain._is_regression("comparison skipped: boom")
    assert not retrain._is_regression("")


def _keep_winner_run(monkeypatch, tmp_path, summary_line):
    """Drive the full worker with a fake train that overwrites the
    weights and a fake comparison returning `summary_line`. Returns
    (model_path, backup_path, final_status, invalidate_count)."""
    model = tmp_path / "roi_seg.pt"
    model.write_bytes(b"OLD-WEIGHTS")
    backup = tmp_path / "roi_seg.prev.pt"
    monkeypatch.setattr(retrain, "_MODEL_PATH", model)
    monkeypatch.setattr(retrain, "_MODEL_BACKUP", backup)

    def fake_run(cmd, **kw):
        out = "ok"
        if Path(cmd[1]).name == "compare_roi_models.py":
            out = f"header\n{summary_line}"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    def fake_popen(cmd, **kw):
        model.write_bytes(b"NEW-WEIGHTS")  # train produces fresh weights
        return _FakePopen(cmd)

    monkeypatch.setattr(retrain.subprocess, "run", fake_run)
    monkeypatch.setattr(retrain.subprocess, "Popen", fake_popen)
    invalidated: list[bool] = []
    monkeypatch.setattr(
        roi_yolo, "invalidate_model_cache", lambda: invalidated.append(True))

    ok, reason = retrain.start_retrain()
    assert ok, reason
    return model, backup, _wait_terminal(), invalidated


def test_regressed_verdict_rolls_back_to_backup(monkeypatch, tmp_path):
    """Keep-the-winner: REGRESSED → previous weights restored + model
    cache invalidated again so the server serves the restored weights."""
    summary = ("SUMMARY: REGRESSED (consider restoring roi_seg.prev.pt) — "
               "within-2%: 55->50/58")
    model, backup, st, invalidated = _keep_winner_run(
        monkeypatch, tmp_path, summary)
    assert st["status"] == "done"
    assert model.read_bytes() == b"OLD-WEIGHTS"
    assert backup.read_bytes() == b"OLD-WEIGHTS"
    assert "previous weights kept" in st["message"]
    assert "SUMMARY: REGRESSED" in st["message"]
    assert len(invalidated) == 2  # after train + after rollback


def test_improved_verdict_keeps_new_weights(monkeypatch, tmp_path):
    model, backup, st, invalidated = _keep_winner_run(
        monkeypatch, tmp_path, "SUMMARY: IMPROVED — mean 1.1->0.6%")
    assert st["status"] == "done"
    assert model.read_bytes() == b"NEW-WEIGHTS"
    assert backup.read_bytes() == b"OLD-WEIGHTS"
    assert "model retrained + reloaded" in st["message"]
    assert "SUMMARY: IMPROVED" in st["message"]
    assert len(invalidated) == 1


def test_inconclusive_comparison_rolls_back(monkeypatch, tmp_path):
    """A comparison that produced no SUMMARY line is not evidence the
    new weights are good — keep-the-winner restores the backup instead
    of silently degrading into "always keep new"."""
    model, backup, st, invalidated = _keep_winner_run(
        monkeypatch, tmp_path, "everything printed but no verdict line")
    assert st["status"] == "done"
    assert model.read_bytes() == b"OLD-WEIGHTS"
    assert "comparison inconclusive" in st["message"]
    assert "previous weights kept" in st["message"]
    assert len(invalidated) == 2  # after train + after rollback


def test_failed_script_surfaces_error_and_skips_second(monkeypatch, tmp_path):
    monkeypatch.setattr(retrain, "_MODEL_PATH", tmp_path / "missing.pt")
    monkeypatch.setattr(retrain, "_MODEL_BACKUP", tmp_path / "prev.pt")
    ran: list[str] = []

    def fake_run(cmd, **kw):
        ran.append(Path(cmd[1]).name)
        return SimpleNamespace(returncode=1, stdout="", stderr="boom explosion")

    def fake_popen(cmd, **kw):
        ran.append(Path(cmd[2]).name)
        return _FakePopen(cmd)

    monkeypatch.setattr(retrain.subprocess, "run", fake_run)
    monkeypatch.setattr(retrain.subprocess, "Popen", fake_popen)

    ok, _ = retrain.start_retrain()
    assert ok
    st = _wait_terminal()
    assert st["status"] == "error"
    assert "build_yolo_dataset.py" in st["message"]
    assert "boom" in st["message"]
    assert ran == ["build_yolo_dataset.py"]  # train never ran


def test_train_streaming_parses_epoch_progress(monkeypatch):
    """The epoch lines in the fake train stdout must move the progress
    fraction through the train span and land the message on the last
    epoch seen."""
    monkeypatch.setattr(retrain.subprocess, "Popen", _FakePopen)
    retrain._run_train_streaming()
    st = retrain.retrain_status()
    # Last line was epoch 10/10 → exactly the train-done fraction.
    assert st["progress"] == pytest.approx(retrain._PROG_TRAIN_DONE)
    assert "epoch 10/10" in st["message"]


def test_train_streaming_failure_raises_with_tail(monkeypatch):
    class _FailPopen(_FakePopen):
        def __init__(self, cmd, **kw):
            super().__init__(cmd, **kw)
            self.stdout = io.StringIO("CUDA out of memory\n")
            self.returncode = 1

    monkeypatch.setattr(retrain.subprocess, "Popen", _FailPopen)
    with pytest.raises(RuntimeError, match="CUDA out of memory"):
        retrain._run_train_streaming()


def test_groundtruth_summary_counts_staleness(monkeypatch, tmp_path):
    """Confirms newer than the model file count as stale; the videos
    list keeps the modal's shape (video_name + history_count)."""
    gt_dir = tmp_path / "roi_groundtruth"
    gt_dir.mkdir()
    model = tmp_path / "roi_seg.pt"
    model.write_bytes(b"w")
    model_mtime = model.stat().st_mtime
    import json as _json
    (gt_dir / "aaa.json").write_text(_json.dumps({
        "video_name": "old.mp4",
        "history": [{"confirmed_at": model_mtime - 100}],
    }), encoding="utf-8")
    (gt_dir / "bbb.json").write_text(_json.dumps({
        "video_name": "new.mp4",
        "history": [{"confirmed_at": model_mtime + 100},
                    {"confirmed_at": model_mtime + 200}],
    }), encoding="utf-8")
    monkeypatch.setattr(retrain, "_ROI_GROUNDTRUTH_DIR", gt_dir)
    monkeypatch.setattr(retrain, "_MODEL_PATH", model)

    s = retrain.groundtruth_summary()
    assert s["count"] == 2
    assert s["yolo_model_exists"] is True
    assert s["confirms_since_yolo_train"] == 2
    assert {v["video_name"] for v in s["videos"]} == {"old.mp4", "new.mp4"}
    assert sum(v["history_count"] for v in s["videos"]) == 3


def _load_compare_module():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "scripts" / "compare_roi_models.py"
    spec = importlib.util.spec_from_file_location("compare_roi_models", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_compare_corner_err_rotation_invariant():
    cmp_mod = _load_compare_module()
    truth = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]
    # Same quad rotated one step → zero error thanks to the cyclic min.
    rotated = truth[1:] + truth[:1]
    assert cmp_mod.corner_err(rotated, truth) < 1e-9
    # Uniform 1% shift on every corner → exactly 1.0 (percent units).
    shifted = [[x + 0.01, y] for x, y in truth]
    assert abs(cmp_mod.corner_err(shifted, truth) - 1.0) < 1e-6


def test_compare_verdict_calls():
    cmp_mod = _load_compare_module()

    def s(mean, within, max_, miss=0, n=56):
        return {"n": n, "miss": miss, "mean": mean, "median": mean,
                "p90": mean, "max": max_, "within": within}

    assert cmp_mod.verdict(s(1.0, 50, 2.8), s(0.6, 53, 2.0)) == "IMPROVED"
    assert "TAIL IMPROVED" in cmp_mod.verdict(s(1.01, 50, 2.81), s(1.07, 53, 2.28))
    assert cmp_mod.verdict(s(1.0, 50, 2.8), s(1.05, 50, 2.8)) == "EQUIVALENT"
    assert "REGRESSED" in cmp_mod.verdict(s(1.0, 50, 2.8), s(1.5, 45, 3.5))
    assert "misses" in cmp_mod.verdict(s(1.0, 50, 2.8), s(1.0, 50, 2.8, miss=2))


def test_invalidate_model_cache_resets_to_unloaded(monkeypatch):
    monkeypatch.setattr(roi_yolo, "_model_cache", "sentinel-model")
    roi_yolo.invalidate_model_cache()
    assert roi_yolo._model_cache is None
    # Also clears a cached negative (False = "missing, stop trying").
    monkeypatch.setattr(roi_yolo, "_model_cache", False)
    roi_yolo.invalidate_model_cache()
    assert roi_yolo._model_cache is None
