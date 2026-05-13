"""
Cinematic outro — the closing card after the main render. Plays for
`outro_duration_seconds` (default 5 s) over a blurred + dimmed last
frame of `main.mp4`, with a centred headline that fades in and a
full-frame black box that fades on top during the final second so the
clip ends in pitch black.

The visual continuity trick: the outro's first frame is the SAME frame
the main render ended on (extracted, blurred, dimmed). At the
concat-demuxer boundary the viewer sees the action shot smoothly turn
into a static blurred backdrop — no xfade re-encode needed.
"""

from __future__ import annotations

from pathlib import Path

from .common import C_WHITE, _ass_escape, _ass_rgb, _ass_skeleton, _bgr, _fmt_time


def build_outro_card_ass(
    *,
    output_path: Path,
    video_w: int,
    video_h: int,
    duration: float,
    text: str,
    fade_in_ms: int = 1000,
    fade_out_black_ms: int = 1000,
) -> Path:
    """Compose the outro overlay .ass. Two Dialogue lines:

      1. Headline (white, centred). Visible the whole `duration`, with
         `\\fad(fade_in_ms, fade_out_black_ms)` so it eases in over the
         first second and dissolves as the black box rises.
      2. Full-frame black box, dialog window = the final
         `fade_out_black_ms` of the clip. `\\fad(fade_out_black_ms, 0)`
         ramps its primary alpha from transparent to opaque over that
         window, ending fully black.

    Caller renders this over a pre-blurred / pre-dimmed background, so
    this builder doesn't worry about the bg layer."""
    scale = max(0.6, video_h / 1080.0)
    cx = video_w // 2
    cy = video_h // 2

    fs_text = max(56, int(112 * scale))
    text_safe = _ass_escape((text or "THANK YOU FOR WATCHING").strip()
                            or "THANK YOU FOR WATCHING")

    end_time = _fmt_time(duration)
    # Clamp the black-fade window so a misconfigured duration smaller
    # than the fade can't produce a negative-length dialogue. The fade
    # then just covers whatever's left of the clip.
    fade_out_s = max(0.2, min(duration, fade_out_black_ms / 1000.0))
    black_start = max(0.0, duration - fade_out_s)
    black_start_ms = int(fade_out_s * 1000)
    black_bgr = _bgr(_ass_rgb(0, 0, 0))

    # Headline style — Arial Black for the "broadcast credit" weight.
    # Sub-pixel rendering through libass keeps the silhouette crisp even
    # when scaled to 4K. The full-screen Box has fontsize=1 because the
    # actual rectangle is drawn via `\p1` rather than typeset glyphs.
    styles = (
        f"Style: Headline, Arial Black, {fs_text}, {C_WHITE}, "
        f"&H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 6, 0, 1, 4, 3, 5, 0, 0, 0, 1\n"
        f"Style: Box,      Arial,       1,        {C_WHITE}, "
        f"&H000000FF, &H00000000, &H80000000,  0, 0, 0, 0, 100, 100, 0, 0, 1, 0, 0, 7, 0, 0, 0, 1"
    )
    lines: list[str] = [_ass_skeleton(video_w, video_h, styles)]

    # Entry: spin -180° → 0 while scaling 30 → 100 % across the same
    # window the alpha ramp uses. Lands clean by the time the fade-in
    # completes, so the text reads naturally during the hold phase.
    entry_anim = (
        f"\\frz-180\\fscx30\\fscy30"
        f"\\t(0,{fade_in_ms},\\frz0\\fscx100\\fscy100)"
    )

    # Subtle 3D Y-axis wiggle every ~2 s through the hold phase so the
    # credit never feels frozen. `\fry` tilts the glyphs around the
    # vertical axis (card-flip look); the small 18° swing reads as a
    # gentle wave rather than a full flip. We stop the schedule before
    # the black box starts rising, leaving the exit fade clean.
    hold_pulse = ""
    pulse_t = (fade_in_ms / 1000.0) + 0.8   # let the entry land first
    hold_end = max(pulse_t, duration - fade_out_s - 0.4)
    pulse_period = 1.8
    while pulse_t + 0.9 < hold_end:
        start_ms = int(pulse_t * 1000)
        hold_pulse += (
            f"\\t({start_ms},{start_ms + 350},\\fry18)"
            f"\\t({start_ms + 350},{start_ms + 800},\\fry0)"
        )
        pulse_t += pulse_period

    # Headline — fades in over fade_in_ms, fades out concurrently with
    # the black-box ramp at the tail so the text disappears as the
    # frame goes dark.
    lines.append(
        f"Dialogue: 1,0:00:00.00,{end_time},Headline,,0,0,0,,"
        f"{{\\an5\\pos({cx},{cy})\\fad({fade_in_ms},{black_start_ms})"
        f"{entry_anim}{hold_pulse}}}{text_safe}"
    )

    # Full-frame black box covering the final `fade_out_s` of the
    # clip. \fad ramps its primary alpha 0 → opaque over the dialog
    # lifetime, so by EOF the screen is solid black.
    lines.append(
        f"Dialogue: 0,{_fmt_time(black_start)},{end_time},Box,,0,0,0,,"
        f"{{\\an7\\pos(0,0)\\bord0\\shad0"
        f"\\1c&H{black_bgr}&\\fad({black_start_ms},0)\\p1}}"
        f"m 0 0 l {video_w} 0 l {video_w} {video_h} l 0 {video_h}{{\\p0}}"
    )

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path
