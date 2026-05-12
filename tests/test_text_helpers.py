"""Tests for the small text/format helpers in backend.ass.common —
trimming, escaping, and the time-stamp formatter. Cheap to test, easy
to break silently if someone tweaks them, so worth pinning down."""

from backend.ass.common import (
    _ass_escape, _ass_rgb, _bgr, _fmt_time,
    _trim_name, _trim_team, _trim_title,
)


# ---------- _trim_name ------------------------------------------------------


def test_trim_name_returns_placeholder_for_empty():
    assert _trim_name("") == "PLAYER"
    assert _trim_name(None) == "PLAYER"
    assert _trim_name("   ") == "PLAYER"


def test_trim_name_strips_whitespace():
    assert _trim_name("  Ma Long  ") == "Ma Long"


def test_trim_name_truncates_with_ellipsis():
    long_name = "A" * 30
    out = _trim_name(long_name, max_len=10)
    assert len(out) == 10
    assert out.endswith("…")


def test_trim_name_unchanged_at_exact_limit():
    name = "A" * 22
    assert _trim_name(name) == name


def test_trim_name_handles_vietnamese_diacritics():
    assert _trim_name("Nguyễn Bá Thảo") == "Nguyễn Bá Thảo"


# ---------- _trim_team ------------------------------------------------------


def test_trim_team_returns_empty_for_blank_input():
    assert _trim_team("") == ""
    assert _trim_team(None) == ""
    assert _trim_team("   ") == ""


def test_trim_team_truncates_at_default_limit():
    out = _trim_team("A" * 20, max_len=14)
    assert len(out) == 14
    assert out.endswith("…")


def test_trim_team_keeps_short_input():
    assert _trim_team("TP.HCM") == "TP.HCM"


def test_trim_team_default_cap_is_20():
    # Exactly 20 chars must pass through; 21 gets ellipsised. Pins
    # down the cap so a tweak elsewhere doesn't silently shrink the
    # team column's tolerance for longer names.
    assert _trim_team("A" * 20) == "A" * 20
    out = _trim_team("A" * 21)
    assert len(out) == 20 and out.endswith("…")


# ---------- _trim_title -----------------------------------------------------


def test_trim_title_caps_word_count():
    long_title = " ".join(["WORD"] * 15)
    out = _trim_title(long_title, max_words=5)
    assert out.count(" ") == 4   # 5 words = 4 spaces
    assert out.endswith("…")


def test_trim_title_unchanged_under_limit():
    assert _trim_title("Spring Open 2026") == "Spring Open 2026"


def test_trim_title_returns_empty_for_blank():
    assert _trim_title("") == ""
    assert _trim_title("   ") == ""


def test_trim_title_max_chars_truncates_long_input():
    # Singles layout passes max_chars=45 to allow longer titles. Verify
    # the cap fires when exceeded and stays inert when not.
    fits = "A" * 45
    assert _trim_title(fits, max_chars=45) == fits
    over = "A" * 50
    out = _trim_title(over, max_chars=45)
    assert len(out) == 45 and out.endswith("…")


def test_trim_title_word_cap_still_applies_with_max_chars():
    # 14 words of two letters each = 41 chars — under the 45 char cap,
    # but the word cap should still kick in if exceeded.
    long_by_words = " ".join(["AB"] * 20)
    out = _trim_title(long_by_words, max_chars=45)
    assert out.endswith("…")
    assert out.count(" ") == 13  # 14 words remaining = 13 spaces


# ---------- _ass_escape -----------------------------------------------------


def test_ass_escape_handles_braces_and_backslash():
    assert _ass_escape("a{b}c") == "a\\{b\\}c"
    assert _ass_escape("a\\b") == "a\\\\b"


def test_ass_escape_idempotent_on_safe_text():
    assert _ass_escape("Lý Trần Bảo Ngọc") == "Lý Trần Bảo Ngọc"


# ---------- _ass_rgb / _bgr round-trip --------------------------------------


def test_ass_rgb_format_is_alpha_bgr():
    # &H00BBGGRR& — alpha 00, then BB, GG, RR (note the BGR order).
    assert _ass_rgb(0xFF, 0x80, 0x10) == "&H001080FF&"


def test_bgr_strips_ass_wrapping():
    assert _bgr("&H001080FF&") == "001080FF"


# ---------- _fmt_time -------------------------------------------------------


def test_fmt_time_zero():
    assert _fmt_time(0.0) == "0:00:00.00"


def test_fmt_time_fractional_seconds():
    assert _fmt_time(1.5) == "0:00:01.50"


def test_fmt_time_minutes_and_seconds():
    assert _fmt_time(75.25) == "0:01:15.25"


def test_fmt_time_hours():
    assert _fmt_time(3725.0) == "1:02:05.00"


def test_fmt_time_negative_clamps_to_zero():
    assert _fmt_time(-10.0) == "0:00:00.00"
