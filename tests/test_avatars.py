"""Tests for backend.avatars — case-insensitive + NFC-normalised
flat-file lookup against assets/avatars/<Name>.<ext>.

We monkey-patch `config.assets_dir` to point at a per-test tmp_path so
real avatar files in the repo aren't needed (and aren't accidentally
asserted against)."""

from pathlib import Path

import pytest

from backend.avatars import (
    AVATAR_EXTS,
    find_avatar,
    find_avatar_or_default,
    find_default_avatar,
)
from backend import avatars as avatars_module


@pytest.fixture
def avatars_dir(tmp_path: Path, monkeypatch) -> Path:
    """Redirect the avatars lookup at a fresh empty tmp dir for the
    duration of the test. Yields the directory itself so tests can drop
    fixture files into it."""
    base = tmp_path / "avatars"
    base.mkdir()

    class _FakeConfig:
        assets_dir = tmp_path

    monkeypatch.setattr(avatars_module, "config", _FakeConfig())
    return base


def _touch(path: Path, content: bytes = b"x") -> None:
    path.write_bytes(content)


# ---------- find_avatar (strict) --------------------------------------------


def test_returns_none_for_empty_input(avatars_dir: Path):
    assert find_avatar("") is None
    assert find_avatar(None) is None  # type: ignore[arg-type]
    assert find_avatar("   ") is None


def test_returns_none_when_no_matching_file(avatars_dir: Path):
    _touch(avatars_dir / "Other Player.jpg")
    assert find_avatar("Ma Long") is None


def test_exact_filename_match(avatars_dir: Path):
    p = avatars_dir / "Ma Long.jpg"
    _touch(p)
    assert find_avatar("Ma Long") == p


def test_match_is_case_insensitive(avatars_dir: Path):
    p = avatars_dir / "Ma Long.jpg"
    _touch(p)
    for q in ["MA LONG", "ma long", "Ma LONG"]:
        assert find_avatar(q) == p, f"failed for {q!r}"


def test_match_handles_vietnamese_diacritics(avatars_dir: Path):
    p = avatars_dir / "Nguyễn Bá Thảo.jpg"
    _touch(p)
    assert find_avatar("Nguyễn Bá Thảo") == p
    assert find_avatar("nguyễn bá thảo") == p
    assert find_avatar("Nguyen Ba Thao") is None  # diacritics required


def test_png_wins_over_jpg_for_same_stem(avatars_dir: Path):
    png = avatars_dir / "Ma Long.png"
    jpg = avatars_dir / "Ma Long.jpg"
    _touch(png)
    _touch(jpg)
    assert find_avatar("Ma Long") == png


def test_jpeg_and_webp_supported(avatars_dir: Path):
    p1 = avatars_dir / "P1.jpeg"
    p2 = avatars_dir / "P2.webp"
    _touch(p1)
    _touch(p2)
    assert find_avatar("P1") == p1
    assert find_avatar("P2") == p2


def test_unknown_extensions_ignored(avatars_dir: Path):
    _touch(avatars_dir / "Ma Long.bmp")
    _touch(avatars_dir / "Ma Long.gif")
    assert find_avatar("Ma Long") is None


def test_reserved_underscore_prefix_never_matches_player(avatars_dir: Path):
    # _default.jpg is shipped as a fallback — strict find_avatar must
    # not return it even if a player happens to be called _default.
    _touch(avatars_dir / "_default.jpg")
    assert find_avatar("_default") is None
    assert find_avatar("_DEFAULT") is None


def test_subfolder_files_ignored(avatars_dir: Path):
    sub = avatars_dir / "Ma Long"
    sub.mkdir()
    _touch(sub / "photo.jpg")
    assert find_avatar("Ma Long") is None


# ---------- find_default_avatar ---------------------------------------------


def test_default_returns_none_when_missing(avatars_dir: Path):
    assert find_default_avatar() is None


def test_default_returns_the_underscore_file(avatars_dir: Path):
    p = avatars_dir / "_default.jpg"
    _touch(p)
    assert find_default_avatar() == p


def test_default_respects_extension_priority(avatars_dir: Path):
    png = avatars_dir / "_default.png"
    jpg = avatars_dir / "_default.jpg"
    _touch(png)
    _touch(jpg)
    assert find_default_avatar() == png


# ---------- find_avatar_or_default ------------------------------------------


def test_player_photo_wins_over_default(avatars_dir: Path):
    player = avatars_dir / "Ma Long.jpg"
    default = avatars_dir / "_default.jpg"
    _touch(player)
    _touch(default)
    path, used_default = find_avatar_or_default("Ma Long")
    assert path == player
    assert used_default is False


def test_falls_back_to_default_when_player_missing(avatars_dir: Path):
    default = avatars_dir / "_default.jpg"
    _touch(default)
    path, used_default = find_avatar_or_default("Ma Long")
    assert path == default
    assert used_default is True


def test_returns_none_when_both_missing(avatars_dir: Path):
    path, used_default = find_avatar_or_default("Ma Long")
    assert path is None
    assert used_default is False


# ---------- AVATAR_EXTS sanity ---------------------------------------------


def test_avatar_exts_includes_common_image_formats():
    # Order matters for the priority test above.
    assert AVATAR_EXTS == (".png", ".jpg", ".jpeg", ".webp")
