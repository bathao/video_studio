"""Tests for the ground-truth sidecar export.

`export_groundtruth` is best-effort — failures must NOT raise and must
land in `RenderState.message` so the operator sees a warning while the
final mp4 still reports `done`. ffmpeg calls are mocked since `tests/`
is pure-logic; the JSON shape and the reference-frame timestamp are
what we actually want to lock down."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from backend import groundtruth
from backend.ffmpeg_runner import FFmpegError
from backend.models import (
    Highlight,
    ProjectData,
    ProjectInfo,
    ScoreEvent,
    TrimSegment,
)


def _fake_probe(_path):
    return {
        "width": 1920,
        "height": 1080,
        "fps": 30.0,
        "duration": 100.0,
        "has_audio": True,
        "sample_rate": 48000,
    }


def _fake_ctx(tmp_path: Path, project: ProjectData) -> SimpleNamespace:
    """Minimal stand-in for RenderContext. `export_groundtruth` only
    reads .src / .plan.project / .state.message — everything else on
    RenderContext is irrelevant for the sidecar."""
    src = tmp_path / "match.mp4"
    src.write_bytes(b"")  # existence is all probe_video needs (mocked anyway)
    return SimpleNamespace(
        src=src,
        plan=SimpleNamespace(project=project),
        state=SimpleNamespace(message=""),
    )


def _setup(monkeypatch, frame_calls: list):
    monkeypatch.setattr(groundtruth, "probe_video", _fake_probe)

    def _fake_extract(src, t, out_png):
        frame_calls.append((src, t, out_png))
        out_png.write_bytes(b"PNG")

    monkeypatch.setattr(groundtruth, "extract_frame_at", _fake_extract)


def test_sidecar_writes_json_and_frame(tmp_path, monkeypatch):
    frame_calls: list = []
    _setup(monkeypatch, frame_calls)

    project = ProjectData(
        info=ProjectInfo(p1="Alice", p2="Bob"),
        trim_segments=[TrimSegment(start=10.0, end=20.0)],
        highlights=[Highlight(start=30.0, end=35.0)],
        score_events=[ScoreEvent(timestamp=25.0, who=1, p1_score=1)],
    )
    ctx = _fake_ctx(tmp_path, project)
    out_mp4 = tmp_path / "out.mp4"

    groundtruth.export_groundtruth(ctx, out_mp4)

    json_path = tmp_path / "out.groundtruth.json"
    frame_path = tmp_path / "out.refframe.png"
    assert json_path.exists()
    assert frame_path.exists()
    assert ctx.state.message == ""  # no warnings on the happy path

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["version"] == groundtruth.GROUNDTRUTH_SCHEMA_VERSION
    assert data["source_video"]["duration_sec"] == 100.0
    assert data["source_video"]["width"] == 1920
    # kept_segments = invert([10,20]) over [0,100] → [0,10] + [20,100]
    kept = data["kept_segments"]
    assert len(kept) == 2
    assert kept[0]["start"] == 0.0 and kept[0]["end"] == 10.0
    assert kept[0]["duration"] == 10.0
    assert kept[1]["start"] == 20.0 and kept[1]["end"] == 100.0
    assert kept[1]["duration"] == 80.0
    # Score event at t=25 falls inside the second kept segment [20, 100]
    assert kept[0]["rally_count_estimate"] == 0
    assert kept[1]["rally_count_estimate"] == 1
    # 10 + 80 = 90 kept, 10 dead, 10% dead, 1 real rally (1 score event)
    assert data["stats"]["kept_segment_count"] == 2
    assert data["stats"]["total_kept_duration"] == 90.0
    assert data["stats"]["total_dead_time"] == 10.0
    assert data["stats"]["dead_time_pct"] == 0.1
    assert data["stats"]["real_rally_count"] == 1
    assert data["stats"]["avg_real_rally_seconds"] == 90.0
    # Project snapshot is verbatim
    assert data["project"]["info"]["p1"] == "Alice"
    assert len(data["project"]["highlights"]) == 1


def test_reference_frame_is_midpoint_of_first_kept_segment(tmp_path, monkeypatch):
    """First kept segment = [0, 10], midpoint = 5.0 → that's what gets
    passed to extract_frame_at. Pins the rule the operator depends on
    when drawing the ROI."""
    frame_calls: list = []
    _setup(monkeypatch, frame_calls)

    project = ProjectData(
        trim_segments=[TrimSegment(start=10.0, end=20.0)],
    )
    ctx = _fake_ctx(tmp_path, project)

    groundtruth.export_groundtruth(ctx, tmp_path / "out.mp4")

    assert len(frame_calls) == 1
    _src, t, _out = frame_calls[0]
    assert t == 5.0


def test_no_trims_yields_one_full_kept_segment(tmp_path, monkeypatch):
    frame_calls: list = []
    _setup(monkeypatch, frame_calls)

    ctx = _fake_ctx(tmp_path, ProjectData())
    groundtruth.export_groundtruth(ctx, tmp_path / "out.mp4")

    data = json.loads((tmp_path / "out.groundtruth.json").read_text(encoding="utf-8"))
    assert len(data["kept_segments"]) == 1
    only = data["kept_segments"][0]
    assert only["start"] == 0.0 and only["end"] == 100.0
    assert only["duration"] == 100.0
    assert only["rally_count_estimate"] == 0  # no score events
    assert data["stats"]["dead_time_pct"] == 0.0
    assert data["stats"]["real_rally_count"] == 0
    assert data["stats"]["avg_real_rally_seconds"] == 0.0  # avoid /0
    # Midpoint of the single full kept segment = 50.0
    assert frame_calls[0][1] == 50.0


def test_per_segment_rally_count_estimate(tmp_path, monkeypatch):
    """Score event timestamps falling inside each kept segment determine
    that segment's `rally_count_estimate`. This is the per-chunk data
    the auto-trim spike needs for precision/recall scoring."""
    frame_calls: list = []
    _setup(monkeypatch, frame_calls)

    # Source [0, 100]; trim [40, 60] → kept segments [0, 40] + [60, 100]
    # Score events: 3 in first kept, 2 in second
    events = [
        ScoreEvent(timestamp=5.0, who=1),
        ScoreEvent(timestamp=15.0, who=2),
        ScoreEvent(timestamp=35.0, who=1),
        ScoreEvent(timestamp=70.0, who=1),
        ScoreEvent(timestamp=90.0, who=2),
    ]
    project = ProjectData(
        trim_segments=[TrimSegment(start=40.0, end=60.0)],
        score_events=events,
    )
    ctx = _fake_ctx(tmp_path, project)
    groundtruth.export_groundtruth(ctx, tmp_path / "out.mp4")

    data = json.loads((tmp_path / "out.groundtruth.json").read_text(encoding="utf-8"))
    kept = data["kept_segments"]
    assert len(kept) == 2
    assert kept[0]["rally_count_estimate"] == 3
    assert kept[1]["rally_count_estimate"] == 2
    assert data["stats"]["real_rally_count"] == 5
    # avg = total_kept (80) / 5 events = 16.0
    assert data["stats"]["avg_real_rally_seconds"] == 16.0


def test_probe_failure_does_not_raise(tmp_path, monkeypatch):
    def _boom(_path):
        raise FFmpegError("probe broke")

    monkeypatch.setattr(groundtruth, "probe_video", _boom)
    ctx = _fake_ctx(tmp_path, ProjectData())

    # Must not raise — render is already done.
    groundtruth.export_groundtruth(ctx, tmp_path / "out.mp4")

    assert "probe broke" in ctx.state.message
    assert not (tmp_path / "out.groundtruth.json").exists()


def test_frame_extract_failure_keeps_json(tmp_path, monkeypatch):
    """If JSON write succeeded but frame extract failed, the JSON must
    remain on disk — that's the half we actually need for ground truth.
    The .png is a nice-to-have."""
    monkeypatch.setattr(groundtruth, "probe_video", _fake_probe)

    def _bad_extract(_src, _t, _out):
        raise FFmpegError("ffmpeg sad")

    monkeypatch.setattr(groundtruth, "extract_frame_at", _bad_extract)

    ctx = _fake_ctx(tmp_path, ProjectData())
    groundtruth.export_groundtruth(ctx, tmp_path / "out.mp4")

    assert (tmp_path / "out.groundtruth.json").exists()
    assert not (tmp_path / "out.refframe.png").exists()
    assert "Reference frame extract failed" in ctx.state.message
