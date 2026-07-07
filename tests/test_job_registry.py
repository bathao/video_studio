"""Pure-logic tests for the job-registry eviction added in the Phase 1
robustness pass (2026-07-07).

`prune_finished_jobs` bounds the two in-process registries (`_jobs` for
renders, `_auto_trim_jobs` for rally detection) that previously grew
without limit for the life of the uvicorn process. No FastAPI TestClient
needed — the helper is a pure dict operation.
"""

from __future__ import annotations

from backend.renderer import RenderState
from backend.server.state import (
    _MAX_FINISHED_JOBS,
    AutoTrimJobState,
    prune_finished_jobs,
)


def _render_registry(n_finished: int, n_running: int = 0) -> dict:
    reg: dict[str, RenderState] = {}
    for i in range(n_finished):
        reg[f"f{i}"] = RenderState(job_id=f"f{i}", status="done")
    for i in range(n_running):
        reg[f"r{i}"] = RenderState(job_id=f"r{i}", status="running")
    return reg


def test_no_eviction_below_cap():
    reg = _render_registry(_MAX_FINISHED_JOBS)
    prune_finished_jobs(reg)
    assert len(reg) == _MAX_FINISHED_JOBS


def test_evicts_oldest_finished_beyond_cap():
    extra = 5
    reg = _render_registry(_MAX_FINISHED_JOBS + extra)
    prune_finished_jobs(reg)
    assert len(reg) == _MAX_FINISHED_JOBS
    # dict insertion order: f0..f4 are the oldest and must be gone.
    for i in range(extra):
        assert f"f{i}" not in reg
    assert f"f{extra}" in reg


def test_never_evicts_running_or_queued():
    reg = _render_registry(_MAX_FINISHED_JOBS + 3, n_running=2)
    reg["q0"] = RenderState(job_id="q0", status="queued")
    prune_finished_jobs(reg)
    assert "r0" in reg and "r1" in reg and "q0" in reg
    # Only finished jobs count against the cap.
    finished = [k for k in reg if reg[k].status == "done"]
    assert len(finished) == _MAX_FINISHED_JOBS


def test_each_terminal_status_counts_as_finished():
    reg: dict[str, RenderState] = {}
    statuses = ["done", "error", "cancelled"]
    total = _MAX_FINISHED_JOBS + len(statuses)
    for i in range(total):
        reg[f"j{i}"] = RenderState(job_id=f"j{i}", status=statuses[i % 3])
    prune_finished_jobs(reg)
    assert len(reg) == _MAX_FINISHED_JOBS
    assert "j0" not in reg and "j1" not in reg and "j2" not in reg


def test_works_for_auto_trim_job_state():
    reg: dict[str, AutoTrimJobState] = {}
    for i in range(_MAX_FINISHED_JOBS + 2):
        reg[f"a{i}"] = AutoTrimJobState(job_id=f"a{i}", status="done")
    reg["live"] = AutoTrimJobState(job_id="live", status="running")
    prune_finished_jobs(reg)
    assert "live" in reg
    assert "a0" not in reg and "a1" not in reg
    assert len(reg) == _MAX_FINISHED_JOBS + 1
