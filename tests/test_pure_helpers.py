"""Pure-logic tests for previously-untested helpers, added in the Phase 5
test/debt pass (2026-07-07).

Covers the quad-geometry helpers under backend/roi/ (these underpin every
clustering decision the multi-frame ROI detector makes — a silent
regression here skews detection without any error), the YOLO polygon
reducer, the ffmpeg path/time formatters, the .ass text escape (including
the long-standing apostrophe backlog item), stinger colour normalisation,
and config fallback properties.

No ffmpeg execution, no model loading — cv2/numpy usage is in-memory only.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from backend.ass.common import _ass_escape
from backend.config import Config
from backend.ffmpeg_runner import _fmt_mmss, escape_ffmpeg_filter_path
from backend.roi.quad import _default_roi, _order_clockwise_from_tl, _quad_iou
from backend.roi_yolo import _polygon_to_quad
from backend.stinger_builder import _ffmpeg_color


# ---------------------------------------------------------------- _quad_iou

UNIT_SQUARE = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]


def test_quad_iou_identical_is_one():
    assert _quad_iou(UNIT_SQUARE, UNIT_SQUARE) == pytest.approx(1.0, abs=0.01)


def test_quad_iou_disjoint_is_zero():
    left = [[0.0, 0.0], [0.4, 0.0], [0.4, 0.4], [0.0, 0.4]]
    right = [[0.6, 0.6], [1.0, 0.6], [1.0, 1.0], [0.6, 1.0]]
    assert _quad_iou(left, right) == 0.0


def test_quad_iou_half_overlap():
    # [0, .5] and [.25, .75] strips: intersection .25 wide, union .75 wide.
    a = [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]]
    b = [[0.25, 0.0], [0.75, 0.0], [0.75, 1.0], [0.25, 1.0]]
    assert _quad_iou(a, b) == pytest.approx(1 / 3, abs=0.02)


def test_quad_iou_symmetric():
    a = [[0.1, 0.1], [0.6, 0.15], [0.55, 0.7], [0.05, 0.65]]
    b = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.9], [0.2, 0.9]]
    assert _quad_iou(a, b) == pytest.approx(_quad_iou(b, a), abs=1e-9)


# ------------------------------------------------- _order_clockwise_from_tl

def test_order_clockwise_trapezoid_from_shuffled_input():
    # Perspective trapezoid as the detector sees a table: back edge
    # (small y) narrower than front edge.
    tl, tr, br, bl = [0.3, 0.4], [0.7, 0.4], [0.8, 0.8], [0.2, 0.8]
    shuffled = [br, tl, bl, tr]
    ordered = _order_clockwise_from_tl(shuffled)
    assert ordered == [tl, tr, br, bl]


def test_order_clockwise_tl_tiebreak_leftmost():
    # Axis-aligned rectangle: both top corners share min-y — TL must be
    # the LEFT one (tiebreak on x).
    ordered = _order_clockwise_from_tl([[0.9, 0.1], [0.1, 0.9], [0.1, 0.1], [0.9, 0.9]])
    assert ordered == [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]


def test_order_clockwise_preserves_points():
    pts = [[0.5, 0.2], [0.9, 0.5], [0.5, 0.9], [0.1, 0.5]]  # diamond
    ordered = _order_clockwise_from_tl(list(reversed(pts)))
    assert sorted(map(tuple, ordered)) == sorted(map(tuple, pts))
    assert ordered[0] == [0.5, 0.2]  # unique min-y is TL


def test_roi_yolo_local_ordering_matches_quad_module():
    """roi_yolo keeps a deliberate local copy of the ordering helper —
    this pins the two implementations to identical behaviour so drift
    between them (the documented risk of the copy) fails a test."""
    from backend.roi_yolo import _order_clockwise_from_tl_local
    cases = [
        [[0.3, 0.4], [0.8, 0.8], [0.7, 0.4], [0.2, 0.8]],
        [[0.9, 0.1], [0.1, 0.9], [0.1, 0.1], [0.9, 0.9]],
        [[0.5, 0.2], [0.9, 0.5], [0.5, 0.9], [0.1, 0.5]],
    ]
    for pts in cases:
        assert _order_clockwise_from_tl_local(pts) == _order_clockwise_from_tl(pts)


# --------------------------------------------------------------- _default_roi

def test_default_roi_shape_and_tags():
    det = _default_roi("unit-test")
    assert len(det.corners) == 4
    assert all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in det.corners)
    assert det.method == "default"
    assert det.confidence == 0.0
    assert det.debug["reason"] == "unit-test"


# ------------------------------------------------------------ _polygon_to_quad

def test_polygon_to_quad_none_and_short_input():
    assert _polygon_to_quad(None) is None
    assert _polygon_to_quad(np.array([[0, 0], [10, 0], [10, 10]])) is None


def test_polygon_to_quad_dense_rectangle_recovers_corners():
    # 40 points along the boundary of a 200×100 rectangle — the reducer
    # must find the 4 true corners, not interior perimeter points.
    xs = np.linspace(0, 200, 11)
    ys = np.linspace(0, 100, 6)
    boundary = (
        [(x, 0) for x in xs] + [(200, y) for y in ys]
        + [(x, 100) for x in xs[::-1]] + [(0, y) for y in ys[::-1]]
    )
    quad = _polygon_to_quad(np.array(boundary, dtype=np.float32))
    assert quad is not None and quad.shape == (4, 2)
    area = float(abs(__import__("cv2").contourArea(quad)))
    assert area == pytest.approx(200 * 100, rel=0.05)


def test_polygon_to_quad_thin_sliver_keeps_four_distinct_corners():
    # Thin tilted sliver (3 px tall) — the shape YOLO produces for a
    # heavily-occluded table edge. Whether approxPolyDP or the
    # minAreaRect fallback wins, the quad must keep 4 distinct corners.
    # (NOTE: a mathematically zero-area polygon CAN degenerate the
    # minAreaRect fallback — impossible for real masks, which always
    # have pixel area, and yolo_tier's mask-area sanity bounds reject
    # tiny masks before this runs.)
    pts = np.array(
        [[i, 0.02 * i] for i in range(0, 100, 5)]
        + [[i, 0.02 * i + 3.0] for i in range(0, 100, 5)],
        dtype=np.float32,
    )
    quad = _polygon_to_quad(pts)
    assert quad is not None and quad.shape == (4, 2)
    dists = [np.linalg.norm(quad[i] - quad[(i + 1) % 4]) for i in range(4)]
    assert all(d > 0 for d in dists)


# -------------------------------------------------------------------- _fmt_mmss

@pytest.mark.parametrize("seconds,expected", [
    (0, "0:00"),
    (59.9, "0:59"),
    (612.8, "10:12"),
    (3600, "1:00:00"),
    (3661, "1:01:01"),
    (-5, "0:00"),
    (float("nan"), "0:00"),
])
def test_fmt_mmss(seconds, expected):
    assert _fmt_mmss(seconds) == expected


# ------------------------------------------------- escape_ffmpeg_filter_path

def test_escape_ffmpeg_filter_path_windows_drive():
    assert escape_ffmpeg_filter_path(Path(r"C:\temp\job\sb.ass")) == "C\\:/temp/job/sb.ass"


def test_escape_ffmpeg_filter_path_relative_no_colon():
    assert escape_ffmpeg_filter_path(Path("temp/job/sb.ass")) == "temp/job/sb.ass"


# ------------------------------------------------------------------ _ass_escape

def test_ass_escape_apostrophe_passes_through():
    # Long-standing backlog item: apostrophes are legal in ASS dialogue
    # text and must NOT be escaped or mangled (player names like
    # "O'Brien" or Vietnamese teams quoting nicknames).
    assert _ass_escape("O'Brien's team") == "O'Brien's team"


def test_ass_escape_braces_and_backslash():
    # Braces open ASS override blocks; backslash starts override tags —
    # both must be escaped so user text can't inject styling.
    assert _ass_escape("{\\b1}bold?") == "\\{\\\\b1\\}bold?"


def test_ass_escape_vietnamese_untouched():
    assert _ass_escape("Nguyễn Bá Thảo") == "Nguyễn Bá Thảo"


# ---------------------------------------------------------------- _ffmpeg_color

@pytest.mark.parametrize("raw,expected", [
    ("", "#FF5722"),           # empty → brand default
    ("  ", "#FF5722"),
    ("#1A2B3C", "#1A2B3C"),    # passthrough
    ("0x1A2B3C", "0x1A2B3C"),  # passthrough
    ("1a2b3c", "#1a2b3c"),     # bare hex gains '#'
    ("red", "red"),            # named colour passthrough
    ("zzz", "zzz"),            # unknown string passthrough (ffmpeg will complain)
])
def test_ffmpeg_color(raw, expected):
    assert _ffmpeg_color(raw) == expected


# -------------------------------------------------------------- config fallbacks

def _config_with(data: dict) -> Config:
    c = Config()          # reads the real config.json first
    c._data = dict(data)  # then replace wholesale for the test
    return c


def test_outro_text_double_fallback():
    assert _config_with({})._data == {}
    assert _config_with({}).outro_text == "THANK YOU FOR WATCHING"
    assert _config_with({"outro_text": ""}).outro_text == "THANK YOU FOR WATCHING"
    assert _config_with({"outro_text": "   "}).outro_text == "THANK YOU FOR WATCHING"
    assert _config_with({"outro_text": " GG "}).outro_text == "GG"


def test_optional_asset_missing_file_is_none(tmp_path):
    c = _config_with({"intro_sound_path": str(tmp_path / "nope.mp3")})
    assert c.intro_sound_path is None


def test_optional_asset_existing_file_resolves(tmp_path):
    f = tmp_path / "bed.mp3"
    f.write_bytes(b"x")
    c = _config_with({"intro_sound_path": str(f)})
    assert c.intro_sound_path == f
