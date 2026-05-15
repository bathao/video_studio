"""Tests for the main playlist builder + event remap when stingers are
spliced around every slow-mo replay. The IN and OUT stinger have
INDEPENDENT durations (asymmetric bracket — long IN with channel/REPLAY
text, short OUT wipe-back-to-live) so each replay contributes
`replay_dur + in_dur + out_dur` to the final timeline and every score
event past a replay's insert point must shift by that combined amount.
"""

from pathlib import Path

import pytest

from backend.ass import ScoreFrame
from backend.renderer import (
    ReplayInsert,
    build_main_playlist,
    remap_events_with_replays,
)


# ---------- build_main_playlist (no stinger — regression) -------------------


def test_playlist_without_stinger_unchanged():
    """When stinger paths are absent, playlist mirrors the pre-stinger
    behaviour exactly: slice → replay → slice."""
    kept = [(0.0, 30.0)]
    replays = [ReplayInsert(insert_at_main=10.0, src_start=8.0, src_end=10.0)]

    entries = build_main_playlist(kept, replays)

    kinds = [e.kind for e in entries]
    assert kinds == ["slice", "replay", "slice"]
    # final timeline: 10s slice + 4s replay (2s @ 0.5x) + 20s slice = 34s
    assert entries[-1].final_end == pytest.approx(34.0)


# ---------- build_main_playlist (with stinger) ------------------------------


def test_stinger_brackets_each_replay_asymmetric():
    """With stinger paths supplied, every replay is preceded by a
    stinger_in (full duration) and followed by a stinger_out (shorter)."""
    kept = [(0.0, 30.0)]
    replays = [ReplayInsert(insert_at_main=10.0, src_start=8.0, src_end=10.0)]
    sting_in = Path("/fake/stinger_in.mp4")
    sting_out = Path("/fake/stinger_out.mp4")

    entries = build_main_playlist(
        kept, replays,
        stinger_in_path=sting_in,
        stinger_out_path=sting_out,
        stinger_in_duration=1.5,
        stinger_out_duration=0.6,
    )

    kinds = [e.kind for e in entries]
    assert kinds == ["slice", "stinger_in", "replay", "stinger_out", "slice"]
    # IN duration 1.5, OUT duration 0.6 → entries reflect those.
    assert entries[1].final_end - entries[1].final_start == pytest.approx(1.5)
    assert entries[3].final_end - entries[3].final_start == pytest.approx(0.6)
    # final timeline: 10s + 1.5s + 4s + 0.6s + 20s = 36.1s
    assert entries[-1].final_end == pytest.approx(36.1)
    # Stinger entries carry their src_path; slice/replay leave it None.
    sting_entries = [e for e in entries if e.kind.startswith("stinger")]
    assert all(e.src_path is not None for e in sting_entries)
    non_sting = [e for e in entries if not e.kind.startswith("stinger")]
    assert all(e.src_path is None for e in non_sting)


def test_stinger_skipped_when_paths_missing():
    """Either path None → bracket is skipped — defensive against
    half-configured calls."""
    kept = [(0.0, 30.0)]
    replays = [ReplayInsert(insert_at_main=10.0, src_start=8.0, src_end=10.0)]

    entries = build_main_playlist(
        kept, replays,
        stinger_in_path=None,
        stinger_out_path=Path("/fake/stinger_out.mp4"),
        stinger_in_duration=1.5,
        stinger_out_duration=0.6,
    )

    assert [e.kind for e in entries] == ["slice", "replay", "slice"]


def test_stinger_skipped_when_either_duration_zero():
    """Either duration 0 → bracket is skipped — no zero-length entries
    in the playlist."""
    kept = [(0.0, 30.0)]
    replays = [ReplayInsert(insert_at_main=10.0, src_start=8.0, src_end=10.0)]

    entries = build_main_playlist(
        kept, replays,
        stinger_in_path=Path("/fake/in.mp4"),
        stinger_out_path=Path("/fake/out.mp4"),
        stinger_in_duration=1.5,
        stinger_out_duration=0.0,  # disabled
    )

    assert [e.kind for e in entries] == ["slice", "replay", "slice"]


def test_multiple_replays_each_get_stinger_pair():
    kept = [(0.0, 60.0)]
    replays = [
        ReplayInsert(insert_at_main=10.0, src_start=8.0, src_end=10.0),
        ReplayInsert(insert_at_main=30.0, src_start=28.0, src_end=30.0),
    ]
    sting_in = Path("/fake/stinger_in.mp4")
    sting_out = Path("/fake/stinger_out.mp4")

    entries = build_main_playlist(
        kept, replays,
        stinger_in_path=sting_in,
        stinger_out_path=sting_out,
        stinger_in_duration=1.5,
        stinger_out_duration=0.6,
    )

    kinds = [e.kind for e in entries]
    assert kinds == [
        "slice", "stinger_in", "replay", "stinger_out",
        "slice", "stinger_in", "replay", "stinger_out",
        "slice",
    ]


def test_replay_at_kept_boundary_still_gets_stinger():
    """When a replay's insert point lands exactly at a kept-segment
    boundary, no pre-split slice is generated — but the stinger pair
    still brackets the replay correctly."""
    kept = [(0.0, 10.0), (20.0, 30.0)]
    replays = [ReplayInsert(insert_at_main=10.0, src_start=8.0, src_end=10.0)]
    sting_in = Path("/fake/stinger_in.mp4")
    sting_out = Path("/fake/stinger_out.mp4")

    entries = build_main_playlist(
        kept, replays,
        stinger_in_path=sting_in,
        stinger_out_path=sting_out,
        stinger_in_duration=1.5,
        stinger_out_duration=0.6,
    )

    kinds = [e.kind for e in entries]
    assert kinds == ["slice", "stinger_in", "replay", "stinger_out", "slice"]


# ---------- remap_events_with_replays (with stinger) ------------------------


def test_event_before_replay_unshifted_even_with_stinger():
    events = [ScoreFrame(timestamp=5.0, p1_score=1, p2_score=0, p1_set=0, p2_set=0)]
    replays = [ReplayInsert(insert_at_main=10.0, src_start=8.0, src_end=10.0)]

    out = remap_events_with_replays(events, replays, stinger_total_duration=2.1)

    assert out[0].timestamp == pytest.approx(5.0)


def test_event_after_replay_shifts_by_replay_plus_stinger_total():
    """An event past a replay's insert point shifts forward by
    `replay_dur + stinger_total` (= in_dur + out_dur)."""
    events = [ScoreFrame(timestamp=15.0, p1_score=2, p2_score=0, p1_set=0, p2_set=0)]
    # Replay: 2s at 0.5x = 4s final dur. Sting total: 1.5 + 0.6 = 2.1s.
    replays = [ReplayInsert(insert_at_main=10.0, src_start=8.0, src_end=10.0)]

    out = remap_events_with_replays(events, replays, stinger_total_duration=2.1)

    # 15.0 + 4.0 (replay) + 2.1 (stingers) = 21.1
    assert out[0].timestamp == pytest.approx(21.1)


def test_event_after_multiple_replays_accumulates_all_shifts():
    events = [ScoreFrame(timestamp=50.0, p1_score=5, p2_score=0, p1_set=0, p2_set=0)]
    replays = [
        ReplayInsert(insert_at_main=10.0, src_start=8.0, src_end=10.0),
        ReplayInsert(insert_at_main=30.0, src_start=28.0, src_end=30.0),
    ]

    out = remap_events_with_replays(events, replays, stinger_total_duration=2.1)

    # Each replay adds 4s replay + 2.1s sting = 6.1s. Two replays = 12.2s.
    assert out[0].timestamp == pytest.approx(62.2)


def test_no_stinger_matches_pre_stinger_behaviour():
    """`stinger_total_duration=0.0` reduces to the original replay-only shift."""
    events = [ScoreFrame(timestamp=15.0, p1_score=2, p2_score=0, p1_set=0, p2_set=0)]
    replays = [ReplayInsert(insert_at_main=10.0, src_start=8.0, src_end=10.0)]

    out = remap_events_with_replays(events, replays, stinger_total_duration=0.0)

    # Only the 4s replay duration shifts.
    assert out[0].timestamp == pytest.approx(19.0)
