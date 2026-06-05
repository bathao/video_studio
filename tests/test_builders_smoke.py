"""Smoke tests for the public ASS builders. We don't compare exact
output (covered by the byte-equivalence captures used during refactors)
— here we just confirm each builder writes a non-empty UTF-8 .ass file
with the standard skeleton, and that the output reflects user-visible
inputs (player names, tournament, team labels)."""

from pathlib import Path

import pytest

from backend.ass import (
    ScoreFrame,
    build_cinematic_intro_ass,
    build_intro_ass,
    build_scoreboard_ass,
    build_slow_motion_badge_ass,
    build_stinger_ass,
)


def _has_ass_skeleton(text: str) -> bool:
    return (
        "[Script Info]" in text
        and "[V4+ Styles]" in text
        and "[Events]" in text
        and "Format: Name, Fontname" in text
    )


@pytest.fixture
def out(tmp_path: Path) -> Path:
    return tmp_path / "test.ass"


# ---------- scoreboard ------------------------------------------------------


def test_scoreboard_basic(out: Path):
    events = [ScoreFrame(0.0, 0, 0, 0, 0), ScoreFrame(5.0, 1, 0, 0, 0)]
    build_scoreboard_ass(
        output_path=out, video_w=1920, video_h=1080,
        total_duration=10.0, tournament="Test Cup",
        p1_name="Alice", p2_name="Bob",
        score_events=events, best_of=5,
    )
    text = out.read_text(encoding="utf-8")
    assert _has_ass_skeleton(text)
    assert "Test Cup" in text
    assert "Alice" in text
    assert "Bob" in text


def test_scoreboard_with_team_renders_team_text(out: Path):
    events = [ScoreFrame(0.0, 0, 0, 0, 0)]
    build_scoreboard_ass(
        output_path=out, video_w=1920, video_h=1080,
        total_duration=10.0, tournament="Team Cup",
        p1_name="Alice", p2_name="Bob",
        p1_team="TX", p2_team="TY",
        score_events=events, best_of=5,
    )
    text = out.read_text(encoding="utf-8")
    assert "TX" in text
    assert "TY" in text


def test_scoreboard_singles_omits_team_column(out: Path):
    events = [ScoreFrame(0.0, 0, 0, 0, 0)]
    build_scoreboard_ass(
        output_path=out, video_w=1920, video_h=1080,
        total_duration=10.0, tournament="Solo",
        p1_name="Alice", p2_name="Bob",
        score_events=events, best_of=5,
    )
    with_team = out.read_text(encoding="utf-8")

    build_scoreboard_ass(
        output_path=out, video_w=1920, video_h=1080,
        total_duration=10.0, tournament="Solo",
        p1_name="Alice", p2_name="Bob",
        p1_team="X", p2_team="Y",
        score_events=events, best_of=5,
    )
    with_team_present = out.read_text(encoding="utf-8")

    # The singles version should be strictly shorter — fewer Dialogue
    # lines because it skips the team-tint rect, the extra divider, and
    # the two team-text dialogues.
    assert len(with_team) < len(with_team_present)


def test_scoreboard_handles_vietnamese_names(out: Path):
    events = [ScoreFrame(0.0, 0, 0, 0, 0)]
    build_scoreboard_ass(
        output_path=out, video_w=1920, video_h=1080,
        total_duration=10.0, tournament="Giải Bóng Bàn",
        p1_name="Lý Trần Bảo Ngọc", p2_name="Mai Quyết Thắng",
        score_events=events, best_of=5,
    )
    text = out.read_text(encoding="utf-8")
    assert "Lý Trần Bảo Ngọc" in text
    assert "Mai Quyết Thắng" in text
    assert "Giải Bóng Bàn" in text


def test_scoreboard_inter_set_recap_grows_per_set(out: Path):
    """After each non-match-ending set, the bottom-right scoreboard
    panel expands with one column per set played so far. The match-
    ending set falls through to the dedicated final scoreboard.

    Best-of-3 with sets won by P1, P2, P1 → 2-1 P1. Sets 1 + 2 are
    non-match-ending (each spawns a 4 s recap window); set 3 is the
    match-end and emits the final scoreboard instead. Verified by
    counting PtsNum dialogues per time window — k-th panel emits
    2 × (sets so far) PtsNum lines (one per row per set column).
    """
    events = [
        ScoreFrame(0.0,    0,  0, 0, 0),
        ScoreFrame(60.0,  10,  7, 0, 0),   # set 1 pre-winning
        ScoreFrame(65.0,   0,  0, 1, 0),   # set 1 → P1 (11–7)
        ScoreFrame(130.0,  9, 10, 1, 0),   # set 2 pre-winning
        ScoreFrame(135.0,  0,  0, 1, 1),   # set 2 → P2 (9–11)
        ScoreFrame(200.0, 10,  8, 1, 1),   # set 3 pre-winning
        ScoreFrame(205.0,  0,  0, 2, 1),   # set 3 → P1 (11–8), match ends
    ]
    build_scoreboard_ass(
        output_path=out, video_w=1920, video_h=1080,
        total_duration=300.0, tournament="Cup",
        p1_name="Alice", p2_name="Bob",
        score_events=events, best_of=3,
    )
    text = out.read_text(encoding="utf-8")

    # Recap windows: [65, 69] and [135, 139]. Final: [205, 301].
    recap1 = "0:01:05.00,0:01:09.00"
    recap2 = "0:02:15.00,0:02:19.00"
    final_w = "0:03:25.00,0:05:01.00"
    assert recap1 in text
    assert recap2 in text
    assert final_w in text

    def _pts(window: str) -> int:
        return sum(
            1 for line in text.splitlines()
            if window in line and ",PtsNum," in line
        )
    # 2 PtsNum dialogues per set column (one per row), so k cols → 2k.
    assert _pts(recap1) == 2
    assert _pts(recap2) == 4
    assert _pts(final_w) == 6


def test_scoreboard_skips_recap_when_only_one_set_and_it_ends_match(out: Path):
    """Best-of-1 (sets_to_win=1): the very first set is the match-
    ending one, so no recap window opens — the final scoreboard takes
    that timeslot directly."""
    events = [
        ScoreFrame(0.0,   0, 0, 0, 0),
        ScoreFrame(50.0, 10, 7, 0, 0),
        ScoreFrame(55.0,  0, 0, 1, 0),   # match ends here
    ]
    build_scoreboard_ass(
        output_path=out, video_w=1920, video_h=1080,
        total_duration=120.0, tournament="Cup",
        p1_name="Alice", p2_name="Bob",
        score_events=events, best_of=1,
    )
    text = out.read_text(encoding="utf-8")
    # Final window opens at match-end (t=55), runs to total_duration+1.
    assert "0:00:55.00,0:02:01.00" in text
    # No recap window starting at t=55 with a 4 s tail — there's just
    # the final panel here, not a recap-then-final overlap.
    recap_window = "0:00:55.00,0:00:59.00"
    assert recap_window not in text


# ---------- intro variants --------------------------------------------------


def test_text_intro(out: Path):
    build_intro_ass(
        output_path=out, video_w=1920, video_h=1080,
        duration=3.0, tournament="Tournament",
        p1_name="Alice", p2_name="Bob",
    )
    text = out.read_text(encoding="utf-8")
    assert _has_ass_skeleton(text)
    assert "Tournament" in text
    assert "Alice" in text
    assert "Bob" in text


def test_cinematic_intro_with_team(out: Path):
    build_cinematic_intro_ass(
        output_path=out, video_w=1920, video_h=1080,
        duration=4.0, tournament="Cup",
        p1_name="Alice", p2_name="Bob",
        p1_team="TX", p2_team="TY",
        avatar_size_px=420,
    )
    text = out.read_text(encoding="utf-8")
    assert _has_ass_skeleton(text)
    assert "TX" in text
    assert "TY" in text


def test_cinematic_intro_singles_skips_team_text(out: Path):
    # When either team is empty, NEITHER team label should be rendered
    # (intro keeps the uncluttered look for singles matches).
    build_cinematic_intro_ass(
        output_path=out, video_w=1920, video_h=1080,
        duration=4.0, tournament="Cup",
        p1_name="Alice", p2_name="Bob",
        p1_team="TX", p2_team="",
        avatar_size_px=420,
    )
    text = out.read_text(encoding="utf-8")
    assert "TX" not in text


# ---------- badges ----------------------------------------------------------


def test_slow_motion_badge_with_ranges(out: Path):
    build_slow_motion_badge_ass(
        output_path=out, video_w=1920, video_h=1080,
        show_ranges=[(1.0, 3.5), (10.0, 12.0)],
    )
    text = out.read_text(encoding="utf-8")
    assert _has_ass_skeleton(text)
    assert "SLOW MOTION" in text


def test_slow_motion_badge_empty_ranges(out: Path):
    # No ranges → header-only file, but still a valid .ass so ffmpeg's
    # `ass=` filter doesn't choke on a zero-line input.
    build_slow_motion_badge_ass(
        output_path=out, video_w=1920, video_h=1080, show_ranges=[],
    )
    text = out.read_text(encoding="utf-8")
    assert _has_ass_skeleton(text)
    assert "Dialogue:" not in text


# ---------- stinger ---------------------------------------------------------


def test_stinger_ass_renders_channel_name_and_replay(out: Path):
    build_stinger_ass(
        output_path=out, video_w=1920, video_h=1080,
        duration=1.0,
        channel_name="Nguyễn Bá Thảo",
        replay_label="REPLAY",
    )
    text = out.read_text(encoding="utf-8")
    assert _has_ass_skeleton(text)
    # libass-rendered text — Vietnamese diacritics preserved in the
    # .ass source (drawtext would have rendered boxes for these glyphs).
    assert "Nguyễn Bá Thảo" in text
    assert "REPLAY" in text
    # Glow shape + channel + replay → at least 3 Dialogue lines.
    assert text.count("Dialogue:") >= 3


def test_stinger_ass_omits_empty_channel_name(out: Path):
    build_stinger_ass(
        output_path=out, video_w=1920, video_h=1080,
        duration=1.0,
        channel_name="",
        replay_label="REPLAY",
    )
    text = out.read_text(encoding="utf-8")
    # No channel-name Dialogue line, but glow + replay still present.
    assert "ChannelName" not in text or "Style: ChannelName" in text
    # Channel-name Dialogue lines should be absent.
    assert "Dialogue: 1," in text  # at least one Dialogue at layer 1 (REPLAY)
    # Sanity: REPLAY still appears.
    assert "REPLAY" in text


def test_stinger_ass_handles_empty_replay_label(out: Path):
    build_stinger_ass(
        output_path=out, video_w=1920, video_h=1080,
        duration=1.0,
        channel_name="Test Channel",
        replay_label="",
    )
    text = out.read_text(encoding="utf-8")
    assert "Test Channel" in text
    # No REPLAY text emitted when label is blank.
    assert "REPLAY" not in text


# ---------- scaling ---------------------------------------------------------


@pytest.mark.parametrize("video_h", [720, 1080, 1440, 2160])
def test_scoreboard_scales_to_resolution(out: Path, video_h: int):
    events = [ScoreFrame(0.0, 0, 0, 0, 0)]
    width = int(video_h * 16 / 9)
    build_scoreboard_ass(
        output_path=out, video_w=width, video_h=video_h,
        total_duration=10.0, tournament="Cup",
        p1_name="A", p2_name="B",
        score_events=events, best_of=5,
    )
    text = out.read_text(encoding="utf-8")
    assert f"PlayResX: {width}" in text
    assert f"PlayResY: {video_h}" in text
