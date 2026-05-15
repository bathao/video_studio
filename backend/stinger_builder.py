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

Output is cached in
`assets/branding/stinger_{direction}_{src_key}_{W}x{H}_{fps}.mp4`,
keyed by source-video identity + output spec. The background of every
clip is a heavily-blurred frame from the project's source video, so
the cache must invalidate when the source changes — hence `src_key`
in the filename. Within a single match, the same cache file is reused
across re-renders.

Visual storyboard for the IN clip (duration = 1.5 s reference):

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

import hashlib
from pathlib import Path
from typing import Optional

from .ass.stinger import build_stinger_ass
from .config import config
from .ffmpeg_runner import (
    TARGET_AUDIO_RATE,
    aac_args,
    escape_ffmpeg_filter_path,
    extract_frame_at,
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


def _source_cache_key(src_path: Optional[Path]) -> str:
    """Short stable identifier for the source video. The stinger's
    background is a blurred frame from this source, so two different
    sources must produce different cached stingers — we encode an
    8-char MD5 of the source path into the filename. Re-renders of
    the same match hit the cache; switching to a new source forces
    a regeneration."""
    if src_path is None:
        return "nosrc"
    return hashlib.md5(str(src_path).encode("utf-8")).hexdigest()[:8]


def get_or_build_stinger_pair(
    *,
    width: int,
    height: int,
    fps: float,
    in_duration: float,
    out_duration: float,
    brand_color: str,
    text: str,
    logo_path: Optional[Path],
    sound_path: Optional[Path],
    source_path: Optional[Path] = None,
    bg_frame_path: Optional[Path] = None,
    channel_name: str = "",
    replay_label: str = "REPLAY",
) -> tuple[Path, Path]:
    """Return `(in_path, out_path)` for the cached stinger pair.

    IN and OUT are NOT a simple time-reverse of each other — they have
    different durations and the OUT clip carries no channel / REPLAY
    text. This makes the bracket asymmetric: long readable hold on the
    way IN, quick wipe-out on the way back to live action.

    Implementation: render IN with the full text overlay at
    `in_duration`. For OUT, render a NO-TEXT forward variant at
    `out_duration`, then time-reverse it — gives the bar-wipe-out +
    logo-fade-out + streak motion in reverse for free.

    Cache files in `assets/branding/`, keyed by source identity + spec.
    Both files are NVENC h264 + AAC 48k stereo + yuv420p, concat-demuxer
    compatible. To force regen after config / logo changes, delete the
    cached mp4s.
    """
    branding_dir = config.assets_dir / "branding"
    branding_dir.mkdir(parents=True, exist_ok=True)

    src_key = _source_cache_key(source_path)
    fps_int = int(round(fps))
    in_path = branding_dir / f"stinger_in_{src_key}_{width}x{height}_{fps_int}.mp4"
    out_path = branding_dir / f"stinger_out_{src_key}_{width}x{height}_{fps_int}.mp4"

    if in_path.exists() and out_path.exists():
        return in_path, out_path

    # If caller didn't supply a logo (config path missing or pointing
    # at a non-existent file), auto-scan assets/branding/ for any image
    # the operator dropped in. Keeps the "drop a photo, hit Render"
    # flow working without forcing the operator to edit config.json.
    resolved_logo = logo_path if logo_path is not None else find_brand_logo()

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
    out_fwd_temp = branding_dir / f"_stinger_out_tmp_{src_key}_{width}x{height}_{fps_int}.mp4"
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
