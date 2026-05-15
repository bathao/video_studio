"""Tests for the cinematic-intro photo-presence gate. The cinematic
intro needs every player photo resolved (either real avatar or the
shipped `_default.jpg`) before it can render. Doubles raises the bar
from 2 photos to 4 — a missing partner avatar must fall back to the
libass title card instead of producing a lopsided 4-avatar layout."""

from pathlib import Path

from backend.renderer import all_intro_photos_present


# Helpers — the second tuple element (is_default flag) isn't checked
# by the gate, but we pass realistic values for clarity.
_FAKE = Path("/fake/avatar.jpg")


def test_singles_with_both_photos_passes():
    photos = {"p1": (_FAKE, False), "p2": (_FAKE, False)}
    assert all_intro_photos_present(is_doubles=False, photos=photos) is True


def test_singles_missing_p2_fails():
    photos = {"p1": (_FAKE, False), "p2": (None, False)}
    assert all_intro_photos_present(is_doubles=False, photos=photos) is False


def test_singles_ignores_p3_p4_when_present():
    """Singles only looks at p1 + p2; spurious p3/p4 entries don't
    affect the decision."""
    photos = {
        "p1": (_FAKE, False), "p2": (_FAKE, False),
        "p3": (None, False), "p4": (None, False),
    }
    assert all_intro_photos_present(is_doubles=False, photos=photos) is True


def test_doubles_with_all_four_photos_passes():
    photos = {
        "p1": (_FAKE, False), "p2": (_FAKE, False),
        "p3": (_FAKE, False), "p4": (_FAKE, False),
    }
    assert all_intro_photos_present(is_doubles=True, photos=photos) is True


def test_doubles_missing_p3_falls_back():
    """The exact regression case the TODO flagged: doubles with only
    3 of 4 avatars must not produce a lopsided cinematic intro."""
    photos = {
        "p1": (_FAKE, False), "p2": (_FAKE, False),
        "p3": (None, False), "p4": (_FAKE, False),
    }
    assert all_intro_photos_present(is_doubles=True, photos=photos) is False


def test_doubles_missing_p4_falls_back():
    photos = {
        "p1": (_FAKE, False), "p2": (_FAKE, False),
        "p3": (_FAKE, False), "p4": (None, False),
    }
    assert all_intro_photos_present(is_doubles=True, photos=photos) is False


def test_doubles_missing_p3_key_entirely_falls_back():
    """`photos.get(slot, (None, False))[0]` defaults to None — slot
    absence is treated the same as `(None, False)`."""
    photos = {
        "p1": (_FAKE, False), "p2": (_FAKE, False),
        "p4": (_FAKE, False),
    }
    assert all_intro_photos_present(is_doubles=True, photos=photos) is False


def test_doubles_with_default_silhouette_still_passes():
    """`is_default=True` is informational only; the gate accepts a
    resolved path regardless of whether it's the shipped fallback."""
    photos = {
        "p1": (_FAKE, True), "p2": (_FAKE, True),
        "p3": (_FAKE, True), "p4": (_FAKE, True),
    }
    assert all_intro_photos_present(is_doubles=True, photos=photos) is True
