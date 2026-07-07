"""
Auto-Stinger generator. Renders short branded transition clips used to
bracket every slow-mo replay in the main render — sting-in before the
replay (full reveal animation + channel/REPLAY hold), sting-out after
(shorter, no text, wipe back to live action).

IN and OUT are ASYMMETRIC: IN is the long readable hold, OUT is a
quick wipe-out. OUT is rendered by generating a no-text forward
variant at the OUT duration and then time-reversing it, so the
"wipe out + logo fade out + streak reverse" motion comes for free
without authoring a second animation.

## Cache model

Output is cached at fixed paths:
  - `assets/branding/stinger_in.mp4`
  - `assets/branding/stinger_out.mp4`
  - `assets/branding/stinger.manifest.json`  (config snapshot)

The manifest captures every input that affects the rendered pixels —
brand colour, logo path + mtime, sound path + mtime, channel name,
REPLAY label, IN/OUT durations, and the output spec (W, H, fps). On
call, the builder compares the current snapshot against the on-disk
manifest. Identical → cache hit (no re-render). Any diff → rebuild
both clips + rewrite the manifest.

This means: 99 % of renders (operator hasn't touched config or logo)
pay zero stinger overhead. Editing `logo.jpg` in place triggers a
rebuild via mtime. Switching match resolution / fps triggers a
rebuild via the spec fields. The background blur is from whatever
source triggered the LAST rebuild — accepted because the blur is
heavy enough to be effectively content-agnostic.

## Visual storyboard

IN clip (storyboard times absolute; config default duration = 1.5 s):

  0.00 – 0.20 s : brand-colour bar wipes in from the left (alpha 0.5,
                  so the blurred bg is still readable through it).
  0.05 – 0.30 s : diagonal light streak sweeps across.
  0.20 – 0.35 s : circular logo + channel name + REPLAY label fade in.
  0.35 s – end  : everything holds.

OUT clip (duration = 0.6 s reference, plays IN-no-text reversed):

  0.00 – 0.25 s : hold (logo + bar visible, no text).
  0.25 – 0.40 s : logo fades out.
  0.36 – 0.55 s : streak sweeps the reverse direction.
  0.40 – 0.60 s : bar wipes off to the right.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

from .ass.stinger import build_stinger_ass
from .config import config
from .ffmpeg_runner import (
    TARGET_AUDIO_RATE,
    aac_args,
    escape_ffmpeg_filter_path,
    nvenc_args,
    probe_video,
    run_ffmpeg_with_progress,
)


# Reserved filename prefix for cached stinger output. Anything in
# `assets/branding/` starting with this prefix is auto-managed and
# should NOT be treated as an operator-supplied logo source.
_STINGER_PREFIX = "stinger_"

# Image extensions accepted as a brand logo when auto-scanning the
# branding folder. PNG preferred (alpha), but JPG / JPEG / WEBP work
# too — the circular alpha mask is applied to the cropped square
# regardless of input alpha.
_LOGO_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def find_brand_logo() -> Optional[Path]:
    """Resolve the brand logo path. Prefers `config.brand_logo_path`
    when the configured file actually exists, otherwise auto-detects
    the first image file in `assets/branding/` (sorted by name) that
    isn't a cached stinger mp4 or a reserved `_`-prefixed filename.

    Returns None when nothing usable is present — the renderer handles
    this by producing a logo-less stinger (brand wipe + text only)."""
    explicit = config.brand_logo_path
    if explicit is not None:
        return explicit

    branding_dir = config.assets_dir / "branding"
    if not branding_dir.exists():
        return None
    for p in sorted(branding_dir.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in _LOGO_EXTS:
            continue
        if p.name.startswith("_") or p.name.startswith(_STINGER_PREFIX):
            continue
        return p
    return None


def _file_mtime(p: Optional[Path]) -> Optional[float]:
    """File mtime to 3 decimal places, or None if the path is missing
    / unreadable. Used in the cache manifest so editing `logo.jpg`
    in place invalidates the cache even though the path string hasn't
    changed."""
    if p is None:
        return None
    try:
        return round(os.path.getmtime(p), 3)
    except OSError:
        return None


def _build_manifest(
    *,
    width: int,
    height: int,
    fps: float,
    brand_color: str,
    channel_name: str,
    replay_label: str,
    logo_path: Optional[Path],
    sound_path: Optional[Path],
    in_duration: float,
    out_duration: float,
) -> dict[str, Any]:
    """Snapshot of every input that affects the rendered stinger
    pixels. Two snapshots compare equal iff a cache hit is safe."""
    return {
        "version": 2,
        "width": int(width),
        "height": int(height),
        "fps": int(round(fps)),
        "brand_color": brand_color,
        "channel_name": channel_name,
        "stinger_replay_label": replay_label,
        "brand_logo_path": str(logo_path) if logo_path is not None else None,
        "brand_logo_mtime": _file_mtime(logo_path),
        "stinger_sound_path": str(sound_path) if sound_path is not None else None,
        "stinger_sound_mtime": _file_mtime(sound_path),
        "in_duration": round(float(in_duration), 6),
        "out_duration": round(float(out_duration), 6),
    }


def _read_manifest(path: Path) -> Optional[dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_manifest(path: Path, data: dict[str, Any]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError:
        pass  # best-effort; next render will rebuild from scratch


def get_or_build_stinger_pair(
    *,
    width: int,
    height: int,
    fps: float,
    in_duration: float,
    out_duration: float,
    brand_color: str,
    logo_path: Optional[Path],
    sound_path: Optional[Path],
    bg_frame_provider: Optional[Callable[[], Optional[Path]]] = None,
    channel_name: str = "",
    replay_label: str = "REPLAY",
) -> tuple[Path, Path]:
    """Return `(in_path, out_path)` for the cached stinger pair.

    IN and OUT are NOT a simple time-reverse of each other — they have
    different durations and the OUT clip carries no channel / REPLAY
    text. This makes the bracket asymmetric: long readable hold on the
    way IN, quick wipe-out on the way back to live action.

    Caching is manifest-driven (`stinger.manifest.json` next to the
    cached mp4s). When the manifest snapshot matches current inputs,
    the existing mp4 files are returned untouched — 99 % of renders
    pay zero stinger overhead. Editing the logo / sound in place
    triggers a rebuild via mtime tracking.

    `bg_frame_provider` is a callable returning a PNG path to use as
    the blurred background. Invoked LAZILY — only on cache miss —
    so a cache-hit render skips frame extraction entirely.
    """
    branding_dir = config.assets_dir / "branding"
    branding_dir.mkdir(parents=True, exist_ok=True)

    in_path = branding_dir / "stinger_in.mp4"
    out_path = branding_dir / "stinger_out.mp4"
    manifest_path = branding_dir / "stinger.manifest.json"

    # If caller didn't supply a logo (config path missing or pointing
    # at a non-existent file), auto-scan assets/branding/ for any image
    # the operator dropped in. Keeps the "drop a photo, hit Render"
    # flow working without forcing the operator to edit config.json.
    resolved_logo = logo_path if logo_path is not None else find_brand_logo()

    current_manifest = _build_manifest(
        width=width, height=height, fps=fps,
        brand_color=brand_color,
        channel_name=channel_name, replay_label=replay_label,
        logo_path=resolved_logo, sound_path=sound_path,
        in_duration=in_duration, out_duration=out_duration,
    )

    if in_path.exists() and out_path.exists():
        existing = _read_manifest(manifest_path)
        if existing == current_manifest:
            return in_path, out_path  # cache hit

    # Cache miss: extract the blurred-bg frame from the current source
    # (lazy — only paid on miss) and rebuild both clips.
    bg_frame_path = bg_frame_provider() if bg_frame_provider is not None else None

    # --- IN: full reveal animation with channel + REPLAY text ---
    _render_stinger_forward(
        out_path=in_path,
        width=width, height=height, fps=fps,
        duration=in_duration,
        brand_color=brand_color,
        logo_path=resolved_logo,
        sound_path=sound_path,
        bg_frame_path=bg_frame_path,
        channel_name=channel_name,
        replay_label=replay_label,
    )

    # --- OUT: render a short no-text forward variant, reverse it ---
    # The reversed clip will visually be: brief hold → logo fade out →
    # streak sweeps the OTHER direction → brand bar wipes off right.
    # Faster than IN and never shows the channel/REPLAY text.
    out_fwd_temp = branding_dir / "_stinger_out_tmp.mp4"
    _render_stinger_forward(
        out_path=out_fwd_temp,
        width=width, height=height, fps=fps,
        duration=out_duration,
        brand_color=brand_color,
        logo_path=resolved_logo,
        sound_path=sound_path,
        bg_frame_path=bg_frame_path,
        channel_name="",         # no text in OUT
        replay_label="",
    )
    _reverse_clip(out_fwd_temp, out_path, duration=out_duration)
    # Cleanup: the temp mp4 AND the .stinger.ass that `_render_stinger_forward`
    # left next to it (`build_stinger_ass` writes to out_path.with_suffix).
    for leftover in (out_fwd_temp, out_fwd_temp.with_suffix(".stinger.ass")):
        try:
            leftover.unlink()
        except OSError:
            pass  # leftover from a prior crash, will be overwritten next time

    _write_manifest(manifest_path, current_manifest)
    return in_path, out_path


def _render_stinger_forward(
    *,
    out_path: Path,
    width: int, height: int, fps: float,
    duration: float,
    brand_color: str,
    logo_path: Optional[Path],
    sound_path: Optional[Path],
    bg_frame_path: Optional[Path],
    channel_name: str,
    replay_label: str,
) -> None:
    """Render the forward stinger clip.

    Layer order (bottom to top):
      1. Blurred + dimmed source-video frame (background texture).
      2. Brand-colour bar wiping in from the left, alpha ≈ 0.5 so the
         blur stays readable through the wash.
      3. Vignette darkening around the edges (broadcast finish).
      4. Circular-masked logo at centre, fades in clean (no glow halo).
      5. libass overlay: diagonal light streak (sweeps through the
         logo area as a flash) + channel name + REPLAY label.

    Animation timings are ABSOLUTE seconds (not percentages of
    duration), so increasing `duration` extends the readable hold at
    the end without slowing down the wipe / fade / streak:

      0.00 – 0.20 s : brand bar wipes in.
      0.05 – 0.30 s : light streak sweeps across (ASS-driven).
      0.20 – 0.35 s : logo + text fade in.
      0.35 s – end  : hold. With duration = 1.5 s this is 1.15 s of
                      fully-visible "Nguyễn Bá Thảo · REPLAY".

    The min() guards keep things sane if the operator sets a
    pathologically short duration (< 0.6 s).
    """
    wipe_dur = min(0.20, duration * 0.30)
    logo_fade_start = wipe_dur
    logo_fade_dur = min(0.15, duration * 0.20)

    brand = _ffmpeg_color(brand_color)

    args: list[str] = []
    # ---------------- inputs ----------------

    # Input 0: blurred background. Real source frame when supplied,
    # solid dark colour fallback otherwise (e.g. when frame extraction
    # failed). The `-loop 1 -framerate fps` pair is mandatory on image
    # inputs: image2 defaults to 25 fps which mismatches the source
    # native fps and silently drops video at the concat boundary.
    if bg_frame_path is not None and bg_frame_path.exists():
        args += [
            "-loop", "1",
            "-framerate", f"{fps}",
            "-t", f"{duration:.3f}",
            "-i", str(bg_frame_path),
        ]
        bg_chain = (
            f"[0:v]scale={width}:{height},"
            f"gblur=sigma=40,eq=brightness=-0.35,format=yuv420p[bg]"
        )
    else:
        args += [
            "-f", "lavfi",
            "-t", f"{duration:.3f}",
            "-i", f"color=c=0x0a0c10:s={width}x{height}:r={fps}",
        ]
        bg_chain = f"[0:v]format=yuv420p[bg]"

    # Input 1: brand-colour full-frame source for the sliding bar.
    # Rendered at 50 % opacity so the blurred bg shows through after
    # the bar lands — gives the layered "spotlight wash" look rather
    # than a flat colour card.
    args += [
        "-f", "lavfi",
        "-t", f"{duration:.3f}",
        "-i", f"color=c={brand}:s={width}x{height}:r={fps}",
    ]
    next_input_idx = 2

    chains: list[str] = [bg_chain]

    # ---------------- brand wipe ----------------
    # Bar slides from x = -W to x = 0 across wipe_dur, then holds. We
    # set the brand colour's alpha via `format=yuva420p` + the overlay
    # filter's `alpha=0.5` instead of pre-baking transparency — keeps
    # the brand input as a plain `color=` source.
    chains.append(
        f"[1:v]format=yuva420p,colorchannelmixer=aa=0.50[brand_a]"
    )
    chains.append(
        f"[bg][brand_a]overlay="
        f"x='if(lt(t,{wipe_dur:.3f}),-{width}+(t/{wipe_dur:.3f})*{width},0)':y=0"
        f"[wiped]"
    )
    cur_v = "wiped"

    # ---------------- vignette ----------------
    # Cinematic darkening at the corners. `vignette=PI/4` is the
    # default angle and works at every aspect ratio. Adds a subtle
    # "broadcast finish" without obscuring the central composition.
    chains.append(f"[{cur_v}]vignette=PI/4[vig]")
    cur_v = "vig"

    # ---------------- logo (optional) ----------------
    if logo_path is not None and logo_path.exists():
        logo_idx = next_input_idx
        next_input_idx += 1
        args += [
            "-loop", "1",
            "-framerate", f"{fps}",
            "-t", f"{duration:.3f}",
            "-i", str(logo_path),
        ]
        logo_size = int(min(width, height) * 0.25)
        # Circular alpha mask + fade-in. Same idiom as the cinematic
        # intro's avatar processing in `intro_builder.py`.
        chains.append(
            f"[{logo_idx}:v]"
            f"crop=min(iw\\,ih):min(iw\\,ih),"
            f"scale={logo_size}:{logo_size},format=yuva420p,"
            f"geq=r='r(X\\,Y)':g='g(X\\,Y)':b='b(X\\,Y)':"
            f"a='min(255\\,max(0\\,(W/2-2-hypot(X-W/2\\,Y-H/2))*255))',"
            f"fade=t=in:st={logo_fade_start:.3f}:"
            f"d={logo_fade_dur:.3f}:alpha=1"
            f"[logo_fx]"
        )
        # Sit the logo a bit above vertical centre so the text below
        # (channel name + REPLAY) has breathing room.
        chains.append(
            f"[{cur_v}][logo_fx]overlay="
            f"x=(W-w)/2:y=(H-h)/2-{int(height * 0.10)}"
            f"[v_logo]"
        )
        cur_v = "v_logo"

    # ---------------- libass text + glow ----------------
    # Build the .ass once per render and burn it. libass handles
    # Vietnamese diacritics; drawtext doesn't without a bundled font.
    # The .ass also draws the soft warm glow behind the logo (a
    # heavily-blurred bright shape — easier to author in ASS than via
    # a chain of gblur+overlay filters).
    ass_path = out_path.with_suffix(".stinger.ass")
    build_stinger_ass(
        output_path=ass_path,
        video_w=width, video_h=height,
        duration=duration,
        channel_name=channel_name,
        replay_label=replay_label,
    )
    ass_arg = escape_ffmpeg_filter_path(ass_path)
    chains.append(f"[{cur_v}]ass='{ass_arg}'[v_text]")
    cur_v = "v_text"

    # NVENC requires yuv420p; final format conversion strips any
    # lingering alpha intermediates from the logo branch.
    chains.append(f"[{cur_v}]format=yuv420p[vout]")

    # ---------------- audio ----------------
    audio_idx = next_input_idx
    if sound_path is not None and sound_path.exists():
        args += ["-i", str(sound_path)]
        chains.append(
            f"[{audio_idx}:a]"
            f"aformat=sample_rates={TARGET_AUDIO_RATE}:channel_layouts=stereo,"
            f"apad,atrim=0:{duration:.3f}[aout]"
        )
        audio_map = "[aout]"
    else:
        args += [
            "-f", "lavfi", "-t", f"{duration:.3f}",
            "-i", f"anullsrc=r={TARGET_AUDIO_RATE}:cl=stereo",
        ]
        audio_map = f"{audio_idx}:a"

    filter_complex = ";".join(chains)
    args += [
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", audio_map,
        *nvenc_args(),
        *aac_args(),
        "-shortest",
        str(out_path),
    ]

    run_ffmpeg_with_progress(
        args,
        expected_out_seconds=duration,
        on_progress=lambda f, m: None,
        log_prefix="stinger-fwd: ",
    )


def _reverse_clip(src: Path, dst: Path, *, duration: float) -> None:
    """Render `dst` as the time-reversed version of `src`. The `reverse`
    filter buffers the whole stream so it's expensive on long clips,
    but at 1 s it's free."""
    args = [
        "-i", str(src),
        "-vf", "reverse",
        "-af", "areverse",
        *nvenc_args(),
        *aac_args(),
        str(dst),
    ]
    try:
        dur_hint = float(probe_video(src).get("duration", duration))
    except Exception:
        dur_hint = duration
    run_ffmpeg_with_progress(
        args,
        expected_out_seconds=dur_hint,
        on_progress=lambda f, m: None,
        log_prefix="stinger-rev: ",
    )


def _ffmpeg_color(s: str) -> str:
    """Normalise a config-supplied colour string into something ffmpeg's
    `color` filter accepts. Supports `#RRGGBB`, `0xRRGGBB`, bare hex,
    and named colours (passed through)."""
    s = (s or "").strip()
    if not s:
        return "#FF5722"
    if s.startswith("#") or s.startswith("0x"):
        return s
    if len(s) == 6 and all(c in "0123456789abcdefABCDEF" for c in s):
        return f"#{s}"
    return s
