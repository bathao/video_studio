"""Tests for the intro preview path: the shared `render_intro_clip`
decision logic (cinematic vs text fallback + placeholder reporting) and
the preview cache key. The ffmpeg builders are monkeypatched — what's
under test is the routing, not the encoding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.models import ProjectInfo
from backend.renderer import orchestrator
from backend.server import routes_render


def _info(**kw) -> ProjectInfo:
    return ProjectInfo(**{"p1": "Alice Anh", "p2": "Bob Ba", **kw})


# ---------------------------------------------------------------------------
# render_intro_clip decision logic
# ---------------------------------------------------------------------------


@pytest.fixture()
def intro_calls(monkeypatch, tmp_path):
    """Stub both ffmpeg builders and the avatar lookup; returns the dict
    the stubs record into. `avatars` maps name → (path, is_default);
    unlisted names resolve to the placeholder."""
    calls: dict = {"cinematic": None, "text": None,
                   "avatars": {}}

    def fake_cinematic(**kw):
        calls["cinematic"] = kw

    def fake_text(**kw):
        calls["text"] = kw

    default = tmp_path / "_default.jpg"
    default.write_bytes(b"x")

    def fake_find(name):
        return calls["avatars"].get(name, (default, True))

    monkeypatch.setattr(orchestrator, "render_cinematic_intro", fake_cinematic)
    monkeypatch.setattr(orchestrator, "render_intro", fake_text)
    monkeypatch.setattr(orchestrator, "find_avatar_or_default", fake_find)
    return calls


def _run(info, style, tmp_path):
    return orchestrator.render_intro_clip(
        out_path=tmp_path / "intro.mp4",
        src=tmp_path / "src.mp4",
        width=1920, height=1080, fps=30.0,
        info=info, intro_style=style,
    )


def test_cinematic_with_real_photos(intro_calls, tmp_path):
    real = tmp_path / "alice.jpg"
    real.write_bytes(b"x")
    intro_calls["avatars"]["Alice Anh"] = (real, False)
    intro_calls["avatars"]["Bob Ba"] = (real, False)
    used_cinematic, placeholders = _run(_info(), "cinematic", tmp_path)
    assert used_cinematic is True
    assert placeholders == []
    assert intro_calls["cinematic"] is not None
    assert intro_calls["text"] is None


def test_cinematic_reports_placeholder_names(intro_calls, tmp_path):
    real = tmp_path / "alice.jpg"
    real.write_bytes(b"x")
    intro_calls["avatars"]["Alice Anh"] = (real, False)
    # Bob falls back to the shipped placeholder → reported by name.
    used_cinematic, placeholders = _run(_info(), "cinematic", tmp_path)
    assert used_cinematic is True
    assert placeholders == ["Bob Ba"]


def test_text_style_never_touches_avatars(intro_calls, tmp_path):
    used_cinematic, placeholders = _run(_info(), "text", tmp_path)
    assert used_cinematic is False
    assert placeholders == []
    assert intro_calls["text"] is not None
    assert intro_calls["cinematic"] is None
    # Row labels flow into the text card.
    assert intro_calls["text"]["p1"] == "Alice Anh"


def test_cinematic_falls_back_when_photo_unresolvable(intro_calls, tmp_path):
    intro_calls["avatars"]["Alice Anh"] = (None, False)  # no photo, no default
    used_cinematic, _ = _run(_info(), "cinematic", tmp_path)
    assert used_cinematic is False
    assert intro_calls["text"] is not None


def test_doubles_requires_all_four_photos(intro_calls, tmp_path):
    real = tmp_path / "a.jpg"
    real.write_bytes(b"x")
    for n in ("Alice Anh", "Bob Ba", "Carol Chi"):
        intro_calls["avatars"][n] = (real, False)
    intro_calls["avatars"]["Dan Dung"] = (None, False)  # p4 unresolvable
    info = _info(match_type="double", p3="Carol Chi", p4="Dan Dung")
    used_cinematic, _ = _run(info, "cinematic", tmp_path)
    assert used_cinematic is False


# ---------------------------------------------------------------------------
# _intro_preview_key
# ---------------------------------------------------------------------------


@pytest.fixture()
def key_env(monkeypatch, tmp_path):
    """Hermetic avatar lookup + config stamp for the cache key."""
    avatar = tmp_path / "ava.jpg"
    avatar.write_bytes(b"x")
    monkeypatch.setattr(routes_render, "find_avatar_or_default",
                        lambda name: (avatar, False))
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(routes_render, "CONFIG_FILE", cfg)
    return avatar


def _key(info, style="cinematic", video="vid123"):
    return routes_render._intro_preview_key(video, style, info)


def test_key_deterministic(key_env):
    assert _key(_info()) == _key(_info())


def test_key_sensitive_to_pixel_inputs(key_env):
    base = _key(_info())
    assert _key(_info(tournament="CUP")) != base
    assert _key(_info(p1="Other Name")) != base
    assert _key(_info(), style="text") != base
    assert _key(_info(), video="other-video") != base


def test_key_changes_when_avatar_file_changes(key_env, monkeypatch):
    import os
    base = _key(_info())
    st = key_env.stat()
    os.utime(key_env, (st.st_atime, st.st_mtime + 10))  # photo swapped
    assert _key(_info()) != base


def test_key_text_style_ignores_avatars(key_env, monkeypatch):
    base = _key(_info(), style="text")
    import os
    st = key_env.stat()
    os.utime(key_env, (st.st_atime, st.st_mtime + 10))
    assert _key(_info(), style="text") == base  # text card has no photos


def test_key_singles_ignores_p3_p4(key_env):
    assert _key(_info(p3="X")) == _key(_info(p3="Y"))


def test_fetch_intro_preview_rejects_traversal(monkeypatch, tmp_path):
    """The GET endpoint only serves .mp4 confined to the preview dir."""
    from fastapi import HTTPException
    monkeypatch.setattr(routes_render, "_INTRO_PREVIEW_DIR", tmp_path)
    (tmp_path / "ok.mp4").write_bytes(b"x")
    resp = routes_render.fetch_intro_preview("ok.mp4")
    assert resp.path == str(tmp_path / "ok.mp4")
    with pytest.raises(HTTPException):
        routes_render.fetch_intro_preview("nope.mp4")
    with pytest.raises(HTTPException):
        routes_render.fetch_intro_preview("ok.json")
    with pytest.raises(HTTPException):
        routes_render.fetch_intro_preview("..\\secret.mp4")
