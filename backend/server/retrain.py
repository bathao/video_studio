"""YOLO ROI-model retrain job — closes the confirm → better-detector loop.

Every Confirm in the Auto Trim modal appends a labeled example to
`dataset/roi_groundtruth/`; the classical tiers (ORB + learned-NN) read
that directory live on the very next detect, but the top-priority YOLO
tier only learns when `scripts/build_yolo_dataset.py` +
`scripts/train_roi_seg.py` run. That used to be a manual operator chore
that silently lagged (13 confirms were pending when this module was
written). This module owns the in-server retrain job the modal
triggers:

  start_retrain()    spawn the 2-script pipeline on a worker thread
                     (one subprocess per script, same venv python),
                     guarded so it never overlaps itself or any GPU job
                     (render / auto-trim detection / auto-score).
  retrain_status()   plain-dict snapshot for the frontend poller.
  gpu_busy_reason()  why a retrain can't start right now, or None.

On success the in-process YOLO model cache is invalidated
(`backend.roi_yolo.invalidate_model_cache`), so the NEXT detect click
uses the new weights without a server restart.

Keep-the-winner policy: after every retrain the old-vs-new comparison
runs on all confirmed refframes, and a ``REGRESSED`` verdict triggers an
automatic rollback to the pre-retrain weights (`roi_seg.prev.pt`) — the
production model can only stay equal or get better, no matter how often
the operator retrains. EQUIVALENT keeps the NEW weights on purpose: the
benchmark is a production replay on known venues, while the new model
trained on more venues — same replay score with more data wins.

The HTTP wrappers live in routes_auto_trim.py and stay thin — this
module is importable and unit-testable without FastAPI. Deliberately
NOT the render-job registry pattern: there is at most ONE retrain,
ever, so a single state object + lock is the honest shape.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

from .state import (
    ROOT_DIR,
    _ROI_GROUNDTRUTH_DIR,
    _auto_score_jobs,
    _auto_score_lock,
    _auto_trim_jobs,
    _auto_trim_lock,
    _jobs,
    _jobs_lock,
)

_logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = ("queued", "running")

# Generous ceiling per script; the pair takes ~2 min on the RTX 5060 Ti
# at the current dataset size (~130 images).
_SCRIPT_TIMEOUT_S = 1800

# Progress fractions per stage. Training dominates wall-clock, so it
# gets the bulk of the bar; within it we interpolate by epoch (parsed
# live from ultralytics stdout).
_PROG_BUILD_DONE = 0.10
_PROG_TRAIN_DONE = 0.90

# Ultralytics prints "  <epoch>/<total>  <gpu_mem>G ..." on every batch
# line of the training bar (tqdm falls back to plain lines when stdout
# is a pipe).
_EPOCH_RE = re.compile(r"^\s*(\d+)/(\d+)\s")

_MODEL_PATH = ROOT_DIR / "assets" / "models" / "roi_seg.pt"
# Pre-retrain snapshot of the weights. copy2 preserves mtime, which the
# comparison script uses to split "frames the old model trained on" vs
# "confirmed after". Gitignored with the rest of assets/models/.
_MODEL_BACKUP = ROOT_DIR / "assets" / "models" / "roi_seg.prev.pt"
_COMPARE_SCRIPT = "compare_roi_models.py"
_COMPARE_TIMEOUT_S = 600


@dataclass
class RetrainState:
    status: str = "idle"  # idle | running | done | error
    message: str = ""
    progress: float = 0.0  # 0..1 across build → train (by epoch) → compare
    started_at: float = 0.0
    finished_at: float = 0.0

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "progress": round(self.progress, 4),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


_state = RetrainState()
_lock = threading.Lock()


def gpu_busy_reason() -> str | None:
    """Human-readable reason the GPU is spoken for, or None when free.

    Training contends with NVENC renders and the NVDEC decode jobs, so a
    retrain refuses to start while any of them is queued/running (the
    reverse is not enforced — production renders always win)."""
    with _jobs_lock:
        if any(j.status in _ACTIVE_STATUSES for j in _jobs.values()):
            return "a render job is running"
    with _auto_trim_lock:
        if any(j.status in _ACTIVE_STATUSES for j in _auto_trim_jobs.values()):
            return "an auto-trim detection is running"
    with _auto_score_lock:
        if any(j.status in _ACTIVE_STATUSES for j in _auto_score_jobs.values()):
            return "an auto-score detection is running"
    return None


def start_retrain() -> tuple[bool, str]:
    """Start the retrain worker. Returns (started, reason_if_not).

    Refuses when a retrain is already running or any GPU job is active —
    the caller (HTTP layer) translates the refusal into a 409."""
    with _lock:
        if _state.status == "running":
            return False, "a retrain is already running"
        busy = gpu_busy_reason()
        if busy is not None:
            return False, f"GPU busy: {busy} — retry when it finishes"
        _state.status = "running"
        _state.message = "starting…"
        _state.progress = 0.0
        _state.started_at = time.time()
        _state.finished_at = 0.0
    threading.Thread(target=_worker, daemon=True, name="yolo-retrain").start()
    return True, ""


def retrain_status() -> dict:
    with _lock:
        return _state.snapshot()


def _run_script(script_name: str, extra_args: list[str] | None = None,
                timeout: int = _SCRIPT_TIMEOUT_S) -> subprocess.CompletedProcess:
    script = ROOT_DIR / "scripts" / script_name
    return subprocess.run(
        [sys.executable, str(script), *(extra_args or [])],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _backup_current_model() -> bool:
    """Snapshot the pre-retrain weights for the after-train comparison.
    Returns True when a backup exists to compare against."""
    if not _MODEL_PATH.exists():
        return False
    try:
        shutil.copy2(_MODEL_PATH, _MODEL_BACKUP)
        return True
    except OSError:
        _logger.warning("could not back up %s — comparison will be skipped",
                        _MODEL_PATH, exc_info=True)
        return False


def _compare_old_vs_new() -> str:
    """Run scripts/compare_roi_models.py (old backup vs fresh weights)
    and return its SUMMARY line, or a short skip note. Never raises —
    the retrain itself already succeeded at this point."""
    try:
        proc = _run_script(
            _COMPARE_SCRIPT,
            ["--old", str(_MODEL_BACKUP)],
            timeout=_COMPARE_TIMEOUT_S,
        )
        if proc.returncode == 0:
            for line in reversed((proc.stdout or "").splitlines()):
                if line.startswith("SUMMARY:"):
                    return line
            return "comparison ran but produced no SUMMARY line"
        tail = (proc.stderr or proc.stdout or "").strip()[-200:]
        return f"comparison failed: {tail}"
    except Exception as e:  # noqa: BLE001 — advisory step, never fatal
        return f"comparison skipped: {e}"


def _is_regression(summary: str) -> bool:
    """True when the comparison verdict says the fresh weights are worse.

    Only a clean ``SUMMARY: REGRESSED…`` line counts — "comparison
    failed/skipped" strings return False so a rollback never happens on
    missing evidence."""
    if not summary.startswith("SUMMARY:"):
        return False
    return summary.removeprefix("SUMMARY:").strip().startswith("REGRESSED")


def _rollback_to_backup() -> bool:
    """Restore the pre-retrain weights after a REGRESSED verdict.

    Uses copyfile (not copy2) so the restored file's mtime is NOW, not
    the backup's: training is seeded, so re-running on the same confirms
    would reproduce the same regressed weights — the staleness counter
    should only wake up again on NEW confirms. Returns True when the
    restore succeeded."""
    try:
        shutil.copyfile(_MODEL_BACKUP, _MODEL_PATH)
        return True
    except OSError:
        _logger.warning("rollback to %s failed — keeping fresh weights",
                        _MODEL_BACKUP, exc_info=True)
        return False


def _set_progress(fraction: float, message: str | None = None) -> None:
    with _lock:
        _state.progress = max(_state.progress, min(1.0, fraction))
        if message is not None:
            _state.message = message


def _run_train_streaming() -> None:
    """Run train_roi_seg.py with live stdout parsing so the UI progress
    bar tracks real epochs instead of jumping between stages. Raises
    RuntimeError on failure (same contract as _run_script + check)."""
    script = ROOT_DIR / "scripts" / "train_roi_seg.py"
    proc = subprocess.Popen(  # noqa: S603 — fixed script path in-repo
        [sys.executable, "-u", str(script)],
        cwd=str(ROOT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # Watchdog instead of run(timeout=): we're blocked in readline, not
    # wait(), so a silent hang would never hit a wait-timeout.
    killer = threading.Timer(_SCRIPT_TIMEOUT_S, proc.kill)
    killer.start()
    tail: deque[str] = deque(maxlen=40)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.strip():
                tail.append(line)
            m = _EPOCH_RE.match(line)
            if m:
                cur, total = int(m.group(1)), int(m.group(2))
                if 0 < cur <= total:
                    span = _PROG_TRAIN_DONE - _PROG_BUILD_DONE
                    _set_progress(
                        _PROG_BUILD_DONE + span * cur / total,
                        f"training YOLO model — epoch {cur}/{total}",
                    )
        rc = proc.wait()
    finally:
        killer.cancel()
    if rc != 0:
        raise RuntimeError(
            f"train_roi_seg.py failed (exit {rc}): "
            + "".join(tail).strip()[-1000:]
        )


def _worker() -> None:
    try:
        have_backup = _backup_current_model()
        _set_progress(0.01, "building YOLO dataset")
        proc = _run_script("build_yolo_dataset.py")
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-1000:]
            raise RuntimeError(
                f"build_yolo_dataset.py failed (exit {proc.returncode}): {tail}"
            )
        _set_progress(_PROG_BUILD_DONE, "training YOLO model")
        _run_train_streaming()
        _set_progress(_PROG_TRAIN_DONE)
        # New weights are on disk — drop the in-process cache so the
        # next detect lazy-loads them. Imported lazily to keep this
        # module free of the cv2/torch surface at import time.
        from ..roi_yolo import invalidate_model_cache
        invalidate_model_cache()
        # Prove (or disprove) the improvement on the operator's own
        # confirmed corners — the verdict lands in the status message
        # the modal shows. Keep-the-winner: a REGRESSED verdict rolls
        # the weights back to the pre-retrain backup automatically.
        summary = ""
        rolled_back = False
        if have_backup:
            _set_progress(0.92, "comparing old vs new model")
            summary = _compare_old_vs_new()
            if _is_regression(summary):
                rolled_back = _rollback_to_backup()
                if rolled_back:
                    invalidate_model_cache()
        msg = ("model retrained but REGRESSED — previous weights kept"
               if rolled_back else "model retrained + reloaded")
        if summary:
            msg += f" — {summary}"
        with _lock:
            _state.status = "done"
            _state.message = msg
            _state.progress = 1.0
            _state.finished_at = time.time()
        _logger.info("YOLO retrain finished OK: %s", summary or "no comparison")
    except Exception as e:  # noqa: BLE001 — job boundary, surface everything
        with _lock:
            _state.status = "error"
            _state.message = str(e)[-1000:]
            _state.finished_at = time.time()
        _logger.exception("YOLO retrain failed")


def groundtruth_summary() -> dict:
    """ROI groundtruth + YOLO staleness snapshot.

    The classical tiers (ORB + learned-NN) read the groundtruth dir live
    on every detect, but the top-priority YOLO model only learns from a
    retrain — `confirms_since_yolo_train` counts confirm events newer
    than the model file's mtime so the UI can suggest a retrain at the
    right moment. Shared by the Auto Trim modal endpoint AND the
    training-status dashboard (routes_training.py)."""
    if not _ROI_GROUNDTRUTH_DIR.exists():
        return {"count": 0, "videos": [],
                "yolo_model_exists": False, "confirms_since_yolo_train": 0}
    model_mtime = _MODEL_PATH.stat().st_mtime if _MODEL_PATH.exists() else None
    files = sorted(_ROI_GROUNDTRUTH_DIR.glob("*.json"))
    videos = []
    confirms_since_train = 0
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            hist = d.get("history", [])
            confirms_since_train += sum(
                1 for h in hist
                if model_mtime is None
                or float(h.get("confirmed_at", 0) or 0) > model_mtime
            )
            videos.append({
                "video_name": d.get("video_name"),
                "history_count": len(hist),
            })
        except Exception:
            continue
    return {
        "count": len(files),
        "videos": videos,
        "yolo_model_exists": model_mtime is not None,
        "confirms_since_yolo_train": confirms_since_train,
    }


def _reset_for_tests() -> None:
    """Restore pristine state between unit tests."""
    with _lock:
        _state.status = "idle"
        _state.message = ""
        _state.progress = 0.0
        _state.started_at = 0.0
        _state.finished_at = 0.0
