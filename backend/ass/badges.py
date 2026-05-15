"""
Top-left mode badges burned over the main match.

  - SLOW MOTION (cyan, pulsing dot) — during each slow-mo replay
                                       spliced into the main render

Pulses to read as "live-feed mode change".
"""

from __future__ import annotations

from pathlib import Path

from .common import C_WHITE, _ass_rgb, _ass_skeleton, _bgr, _fmt_time


def _badge_header(video_w: int, video_h: int, fs_text: int) -> str:
    styles = (
        f"Style: BadgeText, Arial, {fs_text}, {C_WHITE}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 2, 0, 1, 1, 0, 5, 0, 0, 0, 1\n"
        f"Style: Dot,       Arial, {int(fs_text * 1.4)}, &H000000FF, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 0, 0, 1, 0, 0, 5, 0, 0, 0, 1\n"
        f"Style: Box,       Arial, 1,           {C_WHITE}, &H000000FF, &H00000000, &H80000000, 0,  0, 0, 0, 100, 100, 0, 0, 1, 0, 0, 7, 0, 0, 0, 1"
    )
    return _ass_skeleton(video_w, video_h, styles)


def build_slow_motion_badge_ass(
    *,
    output_path: Path,
    video_w: int,
    video_h: int,
    show_ranges: list[tuple[float, float]],
) -> Path:
    """
    "SLOW MOTION" badge — top-left, pulsing cyan dot.

    `show_ranges` is a list of (start_s, end_s) intervals in the FINAL
    main-render timeline (post-concat, post-replay-splicing). Each
    interval emits its own set of Dialogue lines so the badge appears
    during every slow-mo replay and is invisible elsewhere.
    """
    if not show_ranges:
        # Empty .ass still needs the header so ffmpeg's `ass=` filter
        # doesn't choke on a zero-line file.
        scale = max(0.6, video_h / 1080.0)
        fs_text = max(20, int(26 * scale))
        output_path.write_text(_badge_header(video_w, video_h, fs_text), encoding="utf-8")
        return output_path

    scale = max(0.6, video_h / 1080.0)

    margin     = int(36 * scale)
    badge_w    = int(280 * scale)
    badge_h    = int(56  * scale)
    dot_x_off  = int(28 * scale)
    text_x_off = int(58 * scale)
    fs_text    = max(20, int(26 * scale))

    badge_x = margin
    badge_y = margin

    bg_bgr   = _bgr(_ass_rgb(15, 15, 18))
    cyan_bgr = _bgr(_ass_rgb(60, 180, 230))
    dot_bgr  = _bgr(_ass_rgb(90, 210, 255))
    accent_h = max(3, int(3 * scale))

    lines: list[str] = [_badge_header(video_w, video_h, fs_text)]

    period_ms = 800

    for show_start, show_end in show_ranges:
        if show_end <= show_start:
            continue
        start_t = _fmt_time(show_start)
        end_t = _fmt_time(show_end)
        duration_ms = int(max(0.0, show_end - show_start) * 1000)
        # Pulse the dot's primary alpha 00 → A0 every 800 ms.
        pulse = ""
        cycles = duration_ms // period_ms + 1
        for i in range(cycles):
            t0 = i * period_ms
            t_mid = t0 + period_ms // 2
            t_end = t0 + period_ms
            pulse += f"\\t({t0},{t_mid},\\1a&HA0&)\\t({t_mid},{t_end},\\1a&H00&)"

        # Background pill
        lines.append(
            f"Dialogue: 0,{start_t},{end_t},Box,,0,0,0,,"
            f"{{\\an7\\pos({badge_x},{badge_y})\\bord0\\shad0"
            f"\\1c&H{bg_bgr}&\\1a&H30&\\p1}}"
            f"m 0 0 l {badge_w} 0 l {badge_w} {badge_h} l 0 {badge_h}{{\\p0}}"
        )
        # Top cyan accent line
        lines.append(
            f"Dialogue: 0,{start_t},{end_t},Box,,0,0,0,,"
            f"{{\\an7\\pos({badge_x},{badge_y})\\bord0\\shad0"
            f"\\1c&H{cyan_bgr}&\\1a&H00&\\p1}}"
            f"m 0 0 l {badge_w} 0 l {badge_w} {accent_h} l 0 {accent_h}{{\\p0}}"
        )
        # Pulsing dot
        dot_cx = badge_x + dot_x_off
        dot_cy = badge_y + badge_h // 2
        lines.append(
            f"Dialogue: 1,{start_t},{end_t},Dot,,0,0,0,,"
            f"{{\\an5\\pos({dot_cx},{dot_cy})\\1c&H{dot_bgr}&{pulse}}}●"
        )
        # "SLOW MOTION" text
        text_x = badge_x + text_x_off
        lines.append(
            f"Dialogue: 1,{start_t},{end_t},BadgeText,,0,0,0,,"
            f"{{\\an4\\pos({text_x},{dot_cy})}}SLOW MOTION"
        )

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path
