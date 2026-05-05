"""
Short bridge clip rendered between the highlight reel and the main
match. A gold accent line sweeps across a dark frame so the cut between
segments has a visual beat instead of an abrupt jump.
"""

from __future__ import annotations

from pathlib import Path

from .common import C_GOLD, _ass_skeleton, _bgr, _fmt_time


def build_transition_ass(
    *,
    output_path: Path,
    video_w: int,
    video_h: int,
    duration: float = 0.8,
) -> Path:
    """
    Short bridge clip rendered between the highlight reel and the main
    match. A gold accent line sweeps across the centre of a dark frame
    so the cut between segments has a visual beat instead of an
    abrupt jump.
    """
    scale = max(0.6, video_h / 1080.0)
    cy = video_h // 2
    line_w = int(video_w * 0.55)
    line_h = max(4, int(6 * scale))
    move_dur_ms = int(duration * 1000 * 0.8)  # sweep finishes before fade-out
    fade_in = 150
    fade_out = 200
    gold_bgr = _bgr(C_GOLD)

    end_time = _fmt_time(duration)

    styles = "Style: Box, Arial, 1, &H00FFFFFF, &H000000FF, &H00000000, &H80000000, 0, 0, 0, 0, 100, 100, 0, 0, 1, 0, 0, 7, 0, 0, 0, 1"
    lines: list[str] = [_ass_skeleton(video_w, video_h, styles)]

    # Gold sweep line — moves from off-screen left to off-screen right
    # across the middle of the frame.
    x_start = -line_w
    x_end   = video_w + line_w
    lines.append(
        f"Dialogue: 0,0:00:00.00,{end_time},Box,,0,0,0,,"
        f"{{\\an5\\move({x_start},{cy},{x_end},{cy},0,{move_dur_ms})"
        f"\\fad({fade_in},{fade_out})\\bord0\\shad0"
        f"\\1c&H{gold_bgr}&\\1a&H00&\\p1}}"
        f"m {-line_w // 2} 0 l {line_w // 2} 0 l {line_w // 2} {line_h} l {-line_w // 2} {line_h}"
        f"{{\\p0}}"
    )

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path
