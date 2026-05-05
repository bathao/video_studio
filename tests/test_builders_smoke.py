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
    build_full_match_badge_ass,
    build_highlight_badge_ass,
    build_intro_ass,
    build_scoreboard_ass,
    build_transition_ass,
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


# ---------- badges + transition --------------------------------------------


def test_highlight_badge(out: Path):
    build_highlight_badge_ass(output_path=out, video_w=1920, video_h=1080)
    text = out.read_text(encoding="utf-8")
    assert _has_ass_skeleton(text)
    assert "HIGHLIGHT" in text


def test_full_match_badge(out: Path):
    build_full_match_badge_ass(output_path=out, video_w=1920, video_h=1080)
    text = out.read_text(encoding="utf-8")
    assert _has_ass_skeleton(text)
    assert "FULL MATCH" in text


def test_transition_writes_dialogue(out: Path):
    build_transition_ass(output_path=out, video_w=1920, video_h=1080)
    text = out.read_text(encoding="utf-8")
    assert "[Script Info]" in text
    # transition has its own minimal header (no fonts beyond Box)
    assert "Style: Box" in text
    assert "Dialogue:" in text


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
