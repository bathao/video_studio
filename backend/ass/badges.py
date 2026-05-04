"""
Top-left mode badges burned over highlight clips and the start of the
main match.

  - HIGHLIGHT (red, pulsing dot)  — every individual highlight clip
  - FULL MATCH (gold, slide-in)   — first ~15 s of the main match

Both share a common header / palette and never time-overlap, so they
sit happily in this small module together.
"""

from __future__ import annotations

from pathlib import Path

from .common import C_GOLD, C_WHITE, _ass_rgb, _bgr, _fmt_time


def _badge_header(video_w: int, video_h: int, fs_text: int) -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: BadgeText, Arial, {fs_text}, {C_WHITE}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 2, 0, 1, 1, 0, 5, 0, 0, 0, 1
Style: Dot,       Arial, {int(fs_text * 1.4)}, &H000000FF, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 0, 0, 1, 0, 0, 5, 0, 0, 0, 1
Style: Box,       Arial, 1,           {C_WHITE}, &H000000FF, &H00000000, &H80000000, 0,  0, 0, 0, 100, 100, 0, 0, 1, 0, 0, 7, 0, 0, 0, 1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_highlight_badge_ass(
    *,
    output_path: Path,
    video_w: int,
    video_h: int,
    duration: float = 30.0,
) -> Path:
    """
    "HIGHLIGHT" badge at top-left with a pulsing red dot. Burned over
    every individual highlight clip so the viewer always knows they are
    watching a highlight (not the live match feed).

    `duration` only needs to exceed the longest single highlight clip;
    the same .ass is reused for every clip (each clip restarts the .ass
    timeline at 0).
    """
    scale = max(0.6, video_h / 1080.0)

    margin     = int(36 * scale)
    badge_w    = int(280 * scale)
    badge_h    = int(56  * scale)
    dot_x_off  = int(28 * scale)
    text_x_off = int(58 * scale)
    fs_text    = max(20, int(26 * scale))

    badge_x = margin
    badge_y = margin

    end_time = _fmt_time(duration)

    # Pulse the dot's primary alpha from 00 (visible) to A0 (faint) every
    # 800 ms. Multiple \t tags chain — ASS evaluates them in order.
    duration_ms = int(duration * 1000)
    period_ms = 800
    pulse = ""
    cycles = duration_ms // period_ms + 1
    for i in range(cycles):
        t0 = i * period_ms
        t_mid = t0 + period_ms // 2
        t_end = t0 + period_ms
        pulse += f"\\t({t0},{t_mid},\\1a&HA0&)\\t({t_mid},{t_end},\\1a&H00&)"

    bg_bgr  = _bgr(_ass_rgb(15, 15, 18))
    red_bgr = _bgr(_ass_rgb(220, 50, 50))
    dot_bgr = _bgr(_ass_rgb(255, 70, 70))

    lines: list[str] = [_badge_header(video_w, video_h, fs_text)]

    # Background pill
    lines.append(
        f"Dialogue: 0,0:00:00.00,{end_time},Box,,0,0,0,,"
        f"{{\\an7\\pos({badge_x},{badge_y})\\bord0\\shad0"
        f"\\1c&H{bg_bgr}&\\1a&H30&\\p1}}"
        f"m 0 0 l {badge_w} 0 l {badge_w} {badge_h} l 0 {badge_h}{{\\p0}}"
    )

    # Top red accent line — signals "live / hot moment"
    accent_h = max(3, int(3 * scale))
    lines.append(
        f"Dialogue: 0,0:00:00.00,{end_time},Box,,0,0,0,,"
        f"{{\\an7\\pos({badge_x},{badge_y})\\bord0\\shad0"
        f"\\1c&H{red_bgr}&\\1a&H00&\\p1}}"
        f"m 0 0 l {badge_w} 0 l {badge_w} {accent_h} l 0 {accent_h}{{\\p0}}"
    )

    # Pulsing red dot — uses the "●" Unicode bullet so we don't have to
    # draw a circle by hand. Inline \1c override picks the red colour.
    dot_cx = badge_x + dot_x_off
    dot_cy = badge_y + badge_h // 2
    lines.append(
        f"Dialogue: 1,0:00:00.00,{end_time},Dot,,0,0,0,,"
        f"{{\\an5\\pos({dot_cx},{dot_cy})\\1c&H{dot_bgr}&{pulse}}}●"
    )

    # "HIGHLIGHT" text
    text_x = badge_x + text_x_off
    lines.append(
        f"Dialogue: 1,0:00:00.00,{end_time},BadgeText,,0,0,0,,"
        f"{{\\an4\\pos({text_x},{dot_cy})}}HIGHLIGHT"
    )

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def build_full_match_badge_ass(
    *,
    output_path: Path,
    video_w: int,
    video_h: int,
    show_seconds: float = 15.0,
) -> Path:
    """
    "FULL MATCH" badge at top-left. Slides in from off-screen, holds for
    `show_seconds`, then fades + slides out. Burned over the start of
    the main match so the viewer knows the highlight reel is over and
    the full-length recording has begun.
    """
    scale = max(0.6, video_h / 1080.0)

    margin     = int(36 * scale)
    badge_w    = int(260 * scale)
    badge_h    = int(56  * scale)
    text_x_off = int(28 * scale)
    fs_text    = max(20, int(26 * scale))

    badge_x = margin
    badge_y = margin

    # Total visible window: show_seconds + 1s for the exit fade.
    visible_total = show_seconds + 1.0
    end_time = _fmt_time(visible_total)
    fade_in_ms  = 350
    fade_out_ms = 700
    move_in_ms  = 400

    bg_bgr   = _bgr(_ass_rgb(15, 15, 18))
    gold_bgr = _bgr(C_GOLD)

    accent_h = max(3, int(3 * scale))

    # Slide-in: bg starts at x = -badge_w (off-screen left), arrives at badge_x.
    move = f"\\move({-badge_w - margin},{badge_y},{badge_x},{badge_y},0,{move_in_ms})"
    fade = f"\\fad({fade_in_ms},{fade_out_ms})"

    lines: list[str] = [_badge_header(video_w, video_h, fs_text)]

    # Background pill
    lines.append(
        f"Dialogue: 0,0:00:00.00,{end_time},Box,,0,0,0,,"
        f"{{\\an7{move}{fade}\\bord0\\shad0"
        f"\\1c&H{bg_bgr}&\\1a&H30&\\p1}}"
        f"m 0 0 l {badge_w} 0 l {badge_w} {badge_h} l 0 {badge_h}{{\\p0}}"
    )

    # Top gold accent line
    lines.append(
        f"Dialogue: 0,0:00:00.00,{end_time},Box,,0,0,0,,"
        f"{{\\an7{move}{fade}\\bord0\\shad0"
        f"\\1c&H{gold_bgr}&\\1a&H00&\\p1}}"
        f"m 0 0 l {badge_w} 0 l {badge_w} {accent_h} l 0 {accent_h}{{\\p0}}"
    )

    # "FULL MATCH" text — text moves together with the pill, so its
    # \move start/end x-positions are offset by text_x_off from the box.
    text_y = badge_y + badge_h // 2
    text_move = (
        f"\\move({-badge_w - margin + text_x_off},{text_y},"
        f"{badge_x + text_x_off},{text_y},0,{move_in_ms})"
    )
    lines.append(
        f"Dialogue: 1,0:00:00.00,{end_time},BadgeText,,0,0,0,,"
        f"{{\\an4{text_move}{fade}}}FULL MATCH"
    )

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path
