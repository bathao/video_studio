"""Pure-logic tests for Phase 1b Step 2's auto-trim cache key + Step 4's
TrimSegment.source backwards-compat.

These tests intentionally do NOT spin up FastAPI's TestClient — the
endpoints depend on ffmpeg + real video files which are unavailable
in CI. The 22-test pure-logic suite for the detector lives at
test_rally_detector.py; this file adds 9 more focused on the layer
above it (cache key determinism + new TrimSegment field handling).
"""

from __future__ import annotations

import dataclasses

import pytest

from backend.models import ScoreEvent, TrimSegment
from backend.rally_detector import BALANCED, RallyDetectorParams
from backend.server.routes_auto_trim import _autotrim_cache_key


def _make_events(ts_list):
    return [ScoreEvent(timestamp=t, who=1) for t in ts_list]


def test_cache_key_same_inputs_same_key():
    roi = [[0.1, 0.5], [0.9, 0.5], [0.9, 0.9], [0.1, 0.9]]
    events = _make_events([1.0, 12.5, 25.0])
    params = dataclasses.asdict(BALANCED)

    k1 = _autotrim_cache_key("vid123", roi, events, params)
    k2 = _autotrim_cache_key("vid123", roi, events, params)
    assert k1 == k2
    assert len(k1) == 40  # sha1 hex


def test_cache_key_score_event_order_doesnt_matter():
    """Re-ordering score events client-side (e.g. after a sort) must
    not bust the cache — the detector itself sorts on entry."""
    roi = [[0.1, 0.5], [0.9, 0.5], [0.9, 0.9], [0.1, 0.9]]
    forward = _make_events([1.0, 12.5, 25.0])
    backward = _make_events([25.0, 12.5, 1.0])
    params = dataclasses.asdict(BALANCED)

    k_forward = _autotrim_cache_key("vid", roi, forward, params)
    k_backward = _autotrim_cache_key("vid", roi, backward, params)
    assert k_forward == k_backward


def test_cache_key_float_jitter_doesnt_bust_cache():
    """Round-tripping through JSON.parse can introduce 1e-12 jitter in
    timestamps + ROI coords. The hash rounds before hashing precisely
    to insulate against that."""
    roi1 = [[0.10000000001, 0.5], [0.9, 0.5], [0.9, 0.9], [0.1, 0.9]]
    roi2 = [[0.10000000002, 0.5], [0.9, 0.5], [0.9, 0.9], [0.1, 0.9]]
    events1 = _make_events([1.00000001, 12.5, 25.0])
    events2 = _make_events([1.00000002, 12.5, 25.0])
    params = dataclasses.asdict(BALANCED)

    k1 = _autotrim_cache_key("vid", roi1, events1, params)
    k2 = _autotrim_cache_key("vid", roi2, events2, params)
    assert k1 == k2


def test_cache_key_different_video_id_changes_key():
    roi = [[0.1, 0.5], [0.9, 0.5], [0.9, 0.9], [0.1, 0.9]]
    events = _make_events([1.0, 12.5, 25.0])
    params = dataclasses.asdict(BALANCED)
    assert (
        _autotrim_cache_key("vidA", roi, events, params)
        != _autotrim_cache_key("vidB", roi, events, params)
    )


def test_cache_key_different_roi_changes_key():
    roi1 = [[0.1, 0.5], [0.9, 0.5], [0.9, 0.9], [0.1, 0.9]]
    roi2 = [[0.2, 0.5], [0.9, 0.5], [0.9, 0.9], [0.1, 0.9]]
    events = _make_events([1.0, 12.5, 25.0])
    params = dataclasses.asdict(BALANCED)
    assert (
        _autotrim_cache_key("vid", roi1, events, params)
        != _autotrim_cache_key("vid", roi2, events, params)
    )


def test_cache_key_different_params_change_key():
    roi = [[0.1, 0.5], [0.9, 0.5], [0.9, 0.9], [0.1, 0.9]]
    events = _make_events([1.0, 12.5, 25.0])
    params_a = dataclasses.asdict(BALANCED)
    params_b = dataclasses.asdict(
        RallyDetectorParams(threshold_percentile=65.0)
    )
    assert (
        _autotrim_cache_key("vid", roi, events, params_a)
        != _autotrim_cache_key("vid", roi, events, params_b)
    )


def test_trim_segment_legacy_project_loads_without_source():
    """Older projects.json files saved before Phase 1b have no `source`
    field on trim_segments. Pydantic should accept the dict and default
    to `manual`."""
    t = TrimSegment.model_validate({"start": 1.0, "end": 5.0})
    assert t.source == "manual"


def test_trim_segment_accepts_auto_source():
    t = TrimSegment.model_validate({"start": 1.0, "end": 5.0, "source": "auto"})
    assert t.source == "auto"


def test_trim_segment_rejects_unknown_source():
    with pytest.raises(Exception):
        TrimSegment.model_validate({"start": 1.0, "end": 5.0, "source": "bogus"})


def test_project_info_side_fields_default_to_production_conventions():
    """Missing side-info keys default to the operator's production
    conventions (P1 near in set 1, swap every set, set-5 mid-swap at
    5). Explicit nulls (legacy loads normalised by project_io.js)
    still round-trip as unknown."""
    from backend.models import ProjectInfo

    info = ProjectInfo.model_validate({"p1": "A", "p2": "B"})
    assert info.p1_side_set1 == "near"
    assert info.swap_sides_each_set is True
    assert info.set5_mid_swap is True

    info = ProjectInfo.model_validate(
        {"p1_side_set1": None, "set5_mid_swap": None})
    assert info.p1_side_set1 is None
    assert info.set5_mid_swap is None


def test_project_info_side_fields_roundtrip_and_validate():
    from backend.models import ProjectInfo

    info = ProjectInfo.model_validate({
        "p1_side_set1": "far",
        "swap_sides_each_set": False,
        "set5_mid_swap": True,
    })
    dumped = info.model_dump()
    assert dumped["p1_side_set1"] == "far"
    assert dumped["swap_sides_each_set"] is False
    assert dumped["set5_mid_swap"] is True
    # Side-on matches record left/right of frame instead of near/far.
    assert ProjectInfo.model_validate(
        {"camera_angle": "side", "p1_side_set1": "left"}).p1_side_set1 == "left"
    with pytest.raises(Exception):
        ProjectInfo.model_validate({"p1_side_set1": "top"})


def test_project_info_camera_angle_defaults_and_validation():
    """Legacy projects default to the standard behind-player angle
    family; only the three known values validate."""
    from backend.models import ProjectInfo

    assert ProjectInfo.model_validate({}).camera_angle == "standard"
    assert ProjectInfo.model_validate(
        {"camera_angle": "side"}).camera_angle == "side"
    with pytest.raises(Exception):
        ProjectInfo.model_validate({"camera_angle": "90deg"})


def test_project_info_handicap_defaults_and_validation():
    """Legacy projects (no handicap keys) default to no handicap; the
    pattern only accepts digit strings and the receiver only 0/1/2."""
    from backend.models import ProjectInfo

    info = ProjectInfo.model_validate({"p1": "A"})
    assert info.handicap_receiver == 0
    assert info.handicap_pattern == ""

    info = ProjectInfo.model_validate(
        {"handicap_receiver": 2, "handicap_pattern": "232"})
    assert info.handicap_receiver == 2
    assert info.handicap_pattern == "232"

    with pytest.raises(Exception):
        ProjectInfo.model_validate({"handicap_pattern": "2a2"})
    with pytest.raises(Exception):
        ProjectInfo.model_validate({"handicap_receiver": 3})
