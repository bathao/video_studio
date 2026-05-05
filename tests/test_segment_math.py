"""Pure-function tests for the trim → kept-segment inversion and the
score-event source-time → trimmed-time remap. These are the math
underpinning every render; getting them wrong silently mistimes the
scoreboard updates."""

import pytest

from backend.models import TrimSegment
from backend.renderer import kept_segments_from_trims, remap_score_event_to_trimmed


# ---------- kept_segments_from_trims -----------------------------------------


def test_no_trims_keeps_full_duration():
    assert kept_segments_from_trims(60.0, []) == [(0.0, 60.0)]


def test_zero_duration_returns_empty():
    assert kept_segments_from_trims(0.0, []) == []


def test_single_middle_trim_splits_clip():
    trims = [TrimSegment(start=20.0, end=30.0)]
    assert kept_segments_from_trims(60.0, trims) == [(0.0, 20.0), (30.0, 60.0)]


def test_trim_at_start_drops_head():
    trims = [TrimSegment(start=0.0, end=5.0)]
    assert kept_segments_from_trims(60.0, trims) == [(5.0, 60.0)]


def test_trim_at_end_drops_tail():
    trims = [TrimSegment(start=55.0, end=60.0)]
    assert kept_segments_from_trims(60.0, trims) == [(0.0, 55.0)]


def test_trim_covering_whole_clip_returns_empty():
    trims = [TrimSegment(start=0.0, end=60.0)]
    assert kept_segments_from_trims(60.0, trims) == []


def test_overlapping_trims_are_merged():
    trims = [
        TrimSegment(start=10.0, end=20.0),
        TrimSegment(start=15.0, end=25.0),
    ]
    assert kept_segments_from_trims(60.0, trims) == [(0.0, 10.0), (25.0, 60.0)]


def test_trims_clamped_to_duration():
    trims = [TrimSegment(start=-5.0, end=10.0), TrimSegment(start=55.0, end=80.0)]
    assert kept_segments_from_trims(60.0, trims) == [(10.0, 55.0)]


def test_unsorted_trims_are_normalised():
    trims = [
        TrimSegment(start=40.0, end=50.0),
        TrimSegment(start=10.0, end=20.0),
    ]
    assert kept_segments_from_trims(60.0, trims) == [
        (0.0, 10.0),
        (20.0, 40.0),
        (50.0, 60.0),
    ]


def test_zero_length_trim_is_ignored():
    trims = [TrimSegment(start=20.0, end=20.0)]
    assert kept_segments_from_trims(60.0, trims) == [(0.0, 60.0)]


# ---------- remap_score_event_to_trimmed ------------------------------------


def test_remap_with_no_kept_segments_returns_none():
    assert remap_score_event_to_trimmed(10.0, []) is None


def test_remap_inside_first_segment_is_relative_to_segment_start():
    kept = [(0.0, 30.0), (40.0, 60.0)]
    assert remap_score_event_to_trimmed(15.0, kept) == 15.0


def test_remap_inside_second_segment_accumulates_first_length():
    kept = [(0.0, 30.0), (40.0, 60.0)]
    # 50.0 in source = 30 (first kept length) + (50-40) = 40 in trimmed.
    assert remap_score_event_to_trimmed(50.0, kept) == 40.0


def test_event_inside_a_removed_gap_snaps_forward_to_next_kept():
    kept = [(0.0, 30.0), (40.0, 60.0)]
    # 35.0 falls in the gap [30, 40); should snap to start of next
    # kept segment, which translates to t=30.0 in the trimmed video.
    assert remap_score_event_to_trimmed(35.0, kept) == 30.0


def test_event_past_end_returns_total_kept_length():
    kept = [(0.0, 30.0), (40.0, 60.0)]
    assert remap_score_event_to_trimmed(120.0, kept) == 50.0  # 30 + 20


def test_event_at_segment_boundary_is_inclusive_of_left():
    kept = [(0.0, 30.0), (40.0, 60.0)]
    # t=30.0 sits exactly at the right edge of the first kept segment;
    # the function treats `t_source <= b` as inside, so this maps to
    # the first segment's full length.
    assert remap_score_event_to_trimmed(30.0, kept) == 30.0
