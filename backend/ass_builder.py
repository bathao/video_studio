"""
Advanced SubStation Alpha (.ass) overlay generator for the live
scoreboard. The scoreboard sits in the bottom-right corner with one row
per player, in the style of broadcast table-tennis graphics:

    ┌──────────────────────────────────┐
    │ NGUYỄN VĂN A         ▶  0    11  │
    │ TRẦN VĂN B              1     7  │
    └──────────────────────────────────┘

The ▶ marker shows which player most recently scored (or won the most
recent set).

Vietnamese support: the file is written as UTF-8 and the default font is
Arial, which on Windows ships with full Latin-Extended-Additional coverage
including all Vietnamese diacritics. libass falls back to the system font
list if a glyph is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


def _fmt_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - (h * 3600 + m * 60)
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
    )


@dataclass
class ScoreFrame:
    timestamp: float
    p1_score: int
    p2_score: int
    p1_set: int
    p2_set: int


# Colour palette. ASS uses BGR (not RGB), with leading &H and trailing &.
# Each comment shows the intended on-screen colour.
C_WHITE      = "&H00FFFFFF&"
C_NAME       = "&H00F0F0F0&"  # near-white name text
C_DIVIDER    = "&H00505868&"  # slate divider line
C_SET_NUM    = "&H00BFC8D6&"  # muted blue-grey for set count
C_PTS_NUM    = "&H00FFFFFF&"  # white for non-active points
C_PTS_HI     = "&H004AC8FF&"  # gold/orange for the active row's points  (BGR 4A,C8,FF = R=FF G=C8 B=4A)
C_ARROW      = "&H004AC8FF&"  # same gold as the active points
C_BG         = "&H00141821&"  # dark slate panel
C_BG_ROW_HI  = "&H001E2734&"  # slightly lighter slate for the active row

# Box geometry (px). PlayResX/PlayResY of the script equals the video
# resolution, so these numbers map directly to video pixels.
BOX_W = 520
BOX_H = 140
MARGIN_R = 36
MARGIN_B = 36
ROW_H = 60
ROW_GAP = 4
INNER_PAD = 14
NAME_X_PAD = 22
ARROW_OFFSET = 220   # right offset where ▶ goes
SET_OFFSET = 145     # right offset for set count
PTS_OFFSET = 60      # right offset for points


def _ass_header(video_w: int, video_h: int) -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Name,   Arial, 34, {C_NAME},    &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 0, 0, 1, 1.5, 0, 4, 0, 0, 0, 1
Style: SetNum, Arial, 34, {C_SET_NUM}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 0, 0, 1, 1.5, 0, 5, 0, 0, 0, 1
Style: PtsNum, Arial, 48, {C_PTS_NUM}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 0, 0, 1, 2,   0, 5, 0, 0, 0, 1
Style: Arrow,  Arial, 30, {C_ARROW},   &H000000FF, &H00000000, &H80000000,  0, 0, 0, 0, 100, 100, 0, 0, 1, 0,   0, 5, 0, 0, 0, 1
Style: Box,    Arial, 1,  &H00FFFFFF,  &H000000FF, &H00000000, &H80000000,  0, 0, 0, 0, 100, 100, 0, 0, 1, 0,   0, 7, 0, 0, 0, 1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _rect(x: int, y: int, w: int, h: int, color: str, alpha_hex: str = "20", layer: int = 0) -> str:
    """
    Build an ASS drawing dialogue for an opaque-ish filled rectangle.
    `alpha_hex` is the *transparency* (00 = fully opaque, FF = invisible).
    Returns just the {tags}drawing-cmd{p0} payload — the caller wraps it
    in a `Dialogue:` line.
    """
    bgr = color.strip("&H&")
    return (
        f"{{\\an7\\pos({x},{y})\\bord0\\shad0"
        f"\\1c&H{bgr}&\\1a&H{alpha_hex}&\\p1}}"
        f"m 0 0 l {w} 0 l {w} {h} l 0 {h}{{\\p0}}"
    )


def _detect_active(prev: ScoreFrame | None, ev: ScoreFrame) -> int:
    """Return 1 / 2 if that player scored most recently, 0 otherwise."""
    if prev is None:
        return 0
    if ev.p1_score > prev.p1_score:
        return 1
    if ev.p2_score > prev.p2_score:
        return 2
    if ev.p1_set > prev.p1_set:
        return 1
    if ev.p2_set > prev.p2_set:
        return 2
    return 0


def build_scoreboard_ass(
    *,
    output_path: Path,
    video_w: int,
    video_h: int,
    total_duration: float,
    tournament: str,
    p1_name: str,
    p2_name: str,
    score_events: Iterable[ScoreFrame],
) -> Path:
    events = sorted(score_events, key=lambda e: e.timestamp)
    if not events or events[0].timestamp > 0.0:
        events = [ScoreFrame(0.0, 0, 0, 0, 0), *events]

    box_x = video_w - BOX_W - MARGIN_R
    box_y = video_h - BOX_H - MARGIN_B
    row1_y = box_y + INNER_PAD
    row2_y = row1_y + ROW_H + ROW_GAP

    # Centre Y of each row (used for vertical-middle anchors)
    row1_cy = row1_y + ROW_H // 2
    row2_cy = row2_y + ROW_H // 2

    # X positions inside the box (anchored to the right edge of the box)
    name_x = box_x + NAME_X_PAD
    arrow_x = box_x + BOX_W - ARROW_OFFSET
    set_x   = box_x + BOX_W - SET_OFFSET
    pts_x   = box_x + BOX_W - PTS_OFFSET

    p1_safe = _ass_escape(p1_name or "Player 1")
    p2_safe = _ass_escape(p2_name or "Player 2")

    lines: list[str] = [_ass_header(video_w, video_h)]

    # Static background panel (full duration). Use a more opaque alpha so
    # the panel reads clearly even on bright/busy backgrounds.
    end_ts = total_duration + 1
    bg = _rect(box_x, box_y, BOX_W, BOX_H, C_BG, alpha_hex="20", layer=0)
    lines.append(f"Dialogue: 0,{_fmt_time(0)},{_fmt_time(end_ts)},Box,,0,0,0,,{bg}")

    # Subtle divider line between the two rows.
    div_y = row1_y + ROW_H + ROW_GAP // 2 - 1
    div = (
        f"{{\\an7\\pos({box_x + 12},{div_y})\\bord0\\shad0"
        f"\\1c{C_DIVIDER.strip('&H&')}&\\1a&H60&\\p1}}"
        f"m 0 0 l {BOX_W - 24} 0 l {BOX_W - 24} 1 l 0 1{{\\p0}}"
    )
    lines.append(f"Dialogue: 0,{_fmt_time(0)},{_fmt_time(end_ts)},Box,,0,0,0,,{div}")

    # Optional small tournament tag above the panel, right-aligned to the
    # panel's right edge. \an3 anchors the text at its bottom-right corner,
    # \q2 disables word-wrapping so long names stay on a single line.
    if tournament.strip():
        tag = _ass_escape(tournament.strip())
        tag_x = box_x + BOX_W
        tag_y = box_y - 4
        lines.append(
            f"Dialogue: 0,{_fmt_time(0)},{_fmt_time(end_ts)},Box,,0,0,0,,"
            f"{{\\an3\\pos({tag_x},{tag_y})\\q2\\fnArial\\fs22\\b0\\c&H00B5C2D6&}}{tag}"
        )

    prev: ScoreFrame | None = None
    for i, ev in enumerate(events):
        start = max(0.0, ev.timestamp)
        end = events[i + 1].timestamp if i + 1 < len(events) else end_ts
        if end <= start:
            continue
        active = _detect_active(prev, ev)
        prev = ev

        # Highlight the row that just scored with a slightly lighter fill.
        if active == 1:
            hi = _rect(box_x, row1_y - 4, BOX_W, ROW_H + 4, C_BG_ROW_HI, "30")
            lines.append(f"Dialogue: 0,{_fmt_time(start)},{_fmt_time(end)},Box,,0,0,0,,{hi}")
        elif active == 2:
            hi = _rect(box_x, row2_y - 4, BOX_W, ROW_H + 4, C_BG_ROW_HI, "30")
            lines.append(f"Dialogue: 0,{_fmt_time(start)},{_fmt_time(end)},Box,,0,0,0,,{hi}")

        # --- row 1 (P1) ---
        # name (\an4 = middle-left anchor), uppercase preserved as-is
        lines.append(
            f"Dialogue: 1,{_fmt_time(start)},{_fmt_time(end)},Name,,0,0,0,,"
            f"{{\\an4\\pos({name_x},{row1_cy})}}{p1_safe}"
        )
        if active == 1:
            lines.append(
                f"Dialogue: 1,{_fmt_time(start)},{_fmt_time(end)},Arrow,,0,0,0,,"
                f"{{\\an5\\pos({arrow_x},{row1_cy})}}▶"
            )
        lines.append(
            f"Dialogue: 1,{_fmt_time(start)},{_fmt_time(end)},SetNum,,0,0,0,,"
            f"{{\\an5\\pos({set_x},{row1_cy})}}{ev.p1_set}"
        )
        pts_color = C_PTS_HI if active == 1 else C_PTS_NUM
        lines.append(
            f"Dialogue: 1,{_fmt_time(start)},{_fmt_time(end)},PtsNum,,0,0,0,,"
            f"{{\\an5\\pos({pts_x},{row1_cy})\\c{pts_color}}}{ev.p1_score}"
        )

        # --- row 2 (P2) ---
        lines.append(
            f"Dialogue: 1,{_fmt_time(start)},{_fmt_time(end)},Name,,0,0,0,,"
            f"{{\\an4\\pos({name_x},{row2_cy})}}{p2_safe}"
        )
        if active == 2:
            lines.append(
                f"Dialogue: 1,{_fmt_time(start)},{_fmt_time(end)},Arrow,,0,0,0,,"
                f"{{\\an5\\pos({arrow_x},{row2_cy})}}▶"
            )
        lines.append(
            f"Dialogue: 1,{_fmt_time(start)},{_fmt_time(end)},SetNum,,0,0,0,,"
            f"{{\\an5\\pos({set_x},{row2_cy})}}{ev.p2_set}"
        )
        pts_color = C_PTS_HI if active == 2 else C_PTS_NUM
        lines.append(
            f"Dialogue: 1,{_fmt_time(start)},{_fmt_time(end)},PtsNum,,0,0,0,,"
            f"{{\\an5\\pos({pts_x},{row2_cy})\\c{pts_color}}}{ev.p2_score}"
        )

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path
