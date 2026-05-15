"""Tests for `get_or_build_stinger_pair`'s manifest-driven cache.

The cache key is the JSON manifest dict — any input that affects
rendered pixels (W, H, fps, brand colour, channel name, REPLAY label,
in/out durations, logo + sound paths + mtimes) is captured. Identical
manifest + both mp4 files present → cache hit, no ffmpeg invocation.
Any diff → both clips re-rendered + manifest rewritten.

These tests stub the actual ffmpeg renderers so the suite stays pure
Python (no NVENC, no encode time). We assert on call counts to the
forward / reverse helpers."""

import json
from pathlib import Path

import pytest

from backend import stinger_builder
from backend.config import config


@pytest.fixture
def tmp_assets(tmp_path, monkeypatch):
    """Point `config.assets_dir` at a tmp dir so the cache lives in
    isolation and doesn't touch the real `assets/branding/`."""
    assets = tmp_path / "assets"
    (assets / "branding").mkdir(parents=True)
    # Patch the assets_dir backing dict entry — `config.assets_dir`
    # reads via `self.path("assets_dir")` which resolves against ROOT_DIR
    # when the value is relative, so set an absolute path here.
    monkeypatch.setitem(config._data, "assets_dir", str(assets))
    return assets


@pytest.fixture
def stub_renderers(monkeypatch):
    """Replace the heavy ffmpeg renderers with no-op stubs that just
    `touch` the output path. Returns a `calls` dict so tests can assert
    invocation counts."""
    calls = {"forward": 0, "reverse": 0}

    def _stub_forward(*, out_path, **kwargs):
        calls["forward"] += 1
        Path(out_path).write_bytes(b"")  # placeholder mp4

    def _stub_reverse(src, dst, *, duration):
        calls["reverse"] += 1
        Path(dst).write_bytes(b"")

    monkeypatch.setattr(stinger_builder, "_render_stinger_forward", _stub_forward)
    monkeypatch.setattr(stinger_builder, "_reverse_clip", _stub_reverse)
    return calls


_BASE_ARGS = dict(
    width=1920, height=1080, fps=60,
    in_duration=2.0, out_duration=0.6,
    brand_color="#FF5722",
    logo_path=None,
    sound_path=None,
    channel_name="",
    replay_label="REPLAY",
)


# ---------- cache miss (first call) -----------------------------------------


def test_first_call_renders_in_and_out(tmp_assets, stub_renderers):
    in_path, out_path = stinger_builder.get_or_build_stinger_pair(**_BASE_ARGS)

    assert in_path.exists()
    assert out_path.exists()
    # IN clip + OUT-forward-variant = 2 forward renders, 1 reverse.
    assert stub_renderers["forward"] == 2
    assert stub_renderers["reverse"] == 1
    # Manifest written alongside the mp4s.
    manifest_path = in_path.parent / "stinger.manifest.json"
    assert manifest_path.exists()
    snapshot = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert snapshot["brand_color"] == "#FF5722"
    assert snapshot["version"] == 2  # bumped when stinger_text dropped


# ---------- cache hit (second call, identical args) -------------------------


def test_second_call_with_same_args_is_cache_hit(tmp_assets, stub_renderers):
    stinger_builder.get_or_build_stinger_pair(**_BASE_ARGS)
    forward_before = stub_renderers["forward"]
    reverse_before = stub_renderers["reverse"]

    # Re-call with identical args. Cache files + manifest are on disk
    # from the first call; the manifest snapshot must compare equal so
    # neither renderer is invoked.
    in_path, out_path = stinger_builder.get_or_build_stinger_pair(**_BASE_ARGS)

    assert in_path.exists()
    assert out_path.exists()
    assert stub_renderers["forward"] == forward_before  # no new renders
    assert stub_renderers["reverse"] == reverse_before


# ---------- cache invalidation ----------------------------------------------


def test_brand_color_change_invalidates_cache(tmp_assets, stub_renderers):
    stinger_builder.get_or_build_stinger_pair(**_BASE_ARGS)
    forward_before = stub_renderers["forward"]

    changed = dict(_BASE_ARGS, brand_color="#1976D2")
    stinger_builder.get_or_build_stinger_pair(**changed)

    # Cache miss → both clips re-rendered.
    assert stub_renderers["forward"] == forward_before + 2


def test_resolution_change_invalidates_cache(tmp_assets, stub_renderers):
    stinger_builder.get_or_build_stinger_pair(**_BASE_ARGS)
    forward_before = stub_renderers["forward"]

    changed = dict(_BASE_ARGS, width=2688, height=1512)
    stinger_builder.get_or_build_stinger_pair(**changed)

    assert stub_renderers["forward"] == forward_before + 2


def test_in_duration_change_invalidates_cache(tmp_assets, stub_renderers):
    stinger_builder.get_or_build_stinger_pair(**_BASE_ARGS)
    forward_before = stub_renderers["forward"]

    changed = dict(_BASE_ARGS, in_duration=1.5)
    stinger_builder.get_or_build_stinger_pair(**changed)

    assert stub_renderers["forward"] == forward_before + 2


def test_channel_name_change_invalidates_cache(tmp_assets, stub_renderers):
    stinger_builder.get_or_build_stinger_pair(**_BASE_ARGS)
    forward_before = stub_renderers["forward"]

    changed = dict(_BASE_ARGS, channel_name="Nguyễn Bá Thảo")
    stinger_builder.get_or_build_stinger_pair(**changed)

    assert stub_renderers["forward"] == forward_before + 2


def test_logo_mtime_change_invalidates_cache(tmp_assets, stub_renderers):
    """Editing the logo file in place (same path, new mtime) must
    trigger a rebuild — that's the whole point of tracking mtime in
    the manifest."""
    logo = tmp_assets / "branding" / "logo.jpg"
    logo.write_bytes(b"original")

    args = dict(_BASE_ARGS, logo_path=logo)
    stinger_builder.get_or_build_stinger_pair(**args)
    forward_before = stub_renderers["forward"]

    # Touch the logo to bump its mtime, then re-call with same args.
    import os
    import time
    time.sleep(0.05)  # ensure mtime resolution registers a diff
    os.utime(logo, None)

    stinger_builder.get_or_build_stinger_pair(**args)
    assert stub_renderers["forward"] == forward_before + 2


# ---------- defensive: missing cache files force rebuild --------------------


def test_missing_mp4_forces_rebuild_even_with_matching_manifest(
    tmp_assets, stub_renderers,
):
    """A stale manifest with no companion mp4s should not be trusted —
    the cache-hit branch checks file existence before comparing."""
    stinger_builder.get_or_build_stinger_pair(**_BASE_ARGS)
    forward_before = stub_renderers["forward"]

    # Delete the IN mp4 but leave the manifest behind.
    (tmp_assets / "branding" / "stinger_in.mp4").unlink()

    stinger_builder.get_or_build_stinger_pair(**_BASE_ARGS)
    assert stub_renderers["forward"] == forward_before + 2
