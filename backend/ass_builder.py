"""
Advanced SubStation Alpha (.ass) overlay generator for the live
scoreboard.

Visual style is a port of the broadcast-look scoreboard from
`scoreboard_tool/render/renderer.py`, redrawn using ASS primitives so
the overlay can be burned in by libass at NVENC speed instead of being
rendered frame-by-frame in Python.

Layout (anchored bottom-right):

    ┌──────────────────────────────────────────────┐
    │ TOURNAMENT NAME · ROUND                      │ ← header strip
    ├──────────────────────────────────────────────┤    (gold accent line on top)
    │▌  PLAYER A                  │  0  │    11   │ ← row 1 (orange accent at left)
    ├──────────────────────────────────────────────┤
    │▌  PLAYER B                  │  0  │     7   │ ← row 2 (blue accent at left)
    └──────────────────────────────────────────────┘

Geometry scales linearly with `video_h`, so the panel keeps proportional
size at 720p, 1080p, 1440p (2K), 2160p (4K).

Vietnamese coverage: file is written as UTF-8 and the default font is
Arial, which on Windows ships with full Latin-Extended-Additional.
libass falls back through DirectWrite to system fonts if a glyph is
missing.
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


def _trim_name(text: str, max_len: int = 22) -> str:
    text = (text or "").strip()
    if not text:
        return "PLAYER"
    if len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


@dataclass
class ScoreFrame:
    timestamp: float
    p1_score: int
    p2_score: int
    p1_set: int
    p2_set: int


# ASS colours are written &H00BBGGRR& (alpha BB GG RR). We pre-convert
# the RGB intent into that format.
def _ass_rgb(r: int, g: int, b: int) -> str:
    return f"&H00{b:02X}{g:02X}{r:02X}&"


# Palette (R, G, B). Each comment names the on-screen colour.
C_WHITE       = _ass_rgb(255, 255, 255)
C_GREY        = _ass_rgb(185, 185, 185)
C_GOLD        = _ass_rgb(180, 140,  40)   # broadcast amber accent
C_SEP         = _ass_rgb( 75,  75,  75)   # divider lines
C_BG_HEADER   = _ass_rgb( 35,  35,  35)   # near-black header strip
C_BG_ROWS     = _ass_rgb( 18,  18,  18)   # near-black player rows
C_ACCENT_P1   = _ass_rgb(210, 100,  30)   # warm orange (player A)
C_ACCENT_P2   = _ass_rgb( 50, 140, 220)   # sky blue (player B)


def _ass_header(video_w: int, video_h: int, fs_header: int,
                fs_name: int, fs_sets: int, fs_pts: int) -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Header, Arial, {fs_header}, {C_GREY},  &H000000FF, &H00000000, &H80000000,  0, 0, 0, 0, 100, 100, 0, 0, 1, 0,   0, 4, 0, 0, 0, 1
Style: Name,   Arial, {fs_name},   {C_WHITE}, &H000000FF, &H00000000, &H80000000,  0, 0, 0, 0, 100, 100, 0, 0, 1, 1,   0, 4, 0, 0, 0, 1
Style: SetNum, Arial, {fs_sets},   {C_GREY},  &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 0, 0, 1, 1,   0, 5, 0, 0, 0, 1
Style: PtsNum, Arial, {fs_pts},    {C_WHITE}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 0, 0, 1, 1.5, 0, 5, 0, 0, 0, 1
Style: Box,    Arial, 1,           {C_WHITE}, &H000000FF, &H00000000, &H80000000,  0, 0, 0, 0, 100, 100, 0, 0, 1, 0,   0, 7, 0, 0, 0, 1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _rect(x: int, y: int, w: int, h: int, color: str, alpha_hex: str = "00", layer: int = 0) -> str:
    """ASS drawing primitive: filled rectangle anchored top-left at (x,y)."""
    bgr = color.strip("&H&")  # remove leading &H and trailing &
    return (
        f"{{\\an7\\pos({x},{y})\\bord0\\shad0"
        f"\\1c&H{bgr}&\\1a&H{alpha_hex}&\\p1}}"
        f"m 0 0 l {w} 0 l {w} {h} l 0 {h}{{\\p0}}"
    )


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

    # All geometry is tuned for 1080p; everything scales linearly with
    # video_h so the panel keeps the same on-screen footprint at 2K/4K.
    scale = max(0.6, video_h / 1080.0)

    PAD_X      = int(18 * scale)
    NAME_COL   = int(320 * scale)
    SETS_COL   = int(66  * scale)
    PTS_COL    = int(82  * scale)
    BAR_W      = PAD_X + NAME_COL + SETS_COL + PTS_COL + PAD_X
    ROW_H      = int(56 * scale)
    HEADER_PAD = int(8  * scale)
    HEADER_H   = (int(20 * scale) + HEADER_PAD * 2) if tournament.strip() else 0
    MARGIN     = int(26 * scale)
    ACCENT_W   = max(4, int(5 * scale))
    GOLD_LINE  = max(2, int(2 * scale))
    SEP_THIN   = 1
    SEP_MID    = 2
    GAP_ROWS   = 2

    fs_header = max(14, int(20 * scale))
    fs_name   = max(20, int(26 * scale))
    fs_sets   = max(22, int(30 * scale))
    fs_pts    = max(24, int(32 * scale))

    total_h = HEADER_H + ROW_H * 2 + GAP_ROWS
    x1 = video_w - BAR_W - MARGIN
    y1 = video_h - total_h - MARGIN
    x2 = x1 + BAR_W
    y2 = y1 + total_h
    hdr_y2 = y1 + HEADER_H        # bottom of header strip = top of player rows
    mid_y = hdr_y2 + ROW_H        # divider between the two player rows

    col_sets_x = x1 + PAD_X + NAME_COL
    col_pts_x  = col_sets_x + SETS_COL

    # vertical centre of each row, used for middle-anchored text
    row1_cy = hdr_y2 + ROW_H // 2
    row2_cy = mid_y + GAP_ROWS + ROW_H // 2

    end_ts = total_duration + 1
    p1_safe = _ass_escape(_trim_name(p1_name))
    p2_safe = _ass_escape(_trim_name(p2_name))

    lines: list[str] = [_ass_header(video_w, video_h, fs_header, fs_name, fs_sets, fs_pts)]

    def add_static(payload: str, layer: int = 0) -> None:
        lines.append(f"Dialogue: {layer},{_fmt_time(0)},{_fmt_time(end_ts)},Box,,0,0,0,,{payload}")

    # ------------------------------------------------------------------ static layer
    # Header strip background (slightly lighter than the player rows).
    if HEADER_H > 0:
        add_static(_rect(x1, y1, BAR_W, HEADER_H, C_BG_HEADER, alpha_hex="1A"))
        # Gold accent line at the very top.
        add_static(_rect(x1, y1, BAR_W, GOLD_LINE, C_GOLD, alpha_hex="00"))
        # 1px divider under the header.
        add_static(_rect(x1, hdr_y2 - SEP_THIN, BAR_W, SEP_THIN, C_SEP, alpha_hex="20"))

    # Player rows background.
    add_static(_rect(x1, hdr_y2, BAR_W, ROW_H * 2 + GAP_ROWS, C_BG_ROWS, alpha_hex="1F"))

    # Left edge accent bars (one per player).
    add_static(_rect(x1, hdr_y2,                ACCENT_W, ROW_H, C_ACCENT_P1, alpha_hex="00"))
    add_static(_rect(x1, mid_y + GAP_ROWS,      ACCENT_W, ROW_H, C_ACCENT_P2, alpha_hex="00"))

    # Mid separator between the two player rows.
    add_static(_rect(x1, mid_y, BAR_W, SEP_MID, C_SEP, alpha_hex="20"))

    # Vertical column dividers.
    add_static(_rect(col_sets_x, hdr_y2 + 4, SEP_THIN, ROW_H * 2 + GAP_ROWS - 8, C_SEP, alpha_hex="30"))
    add_static(_rect(col_pts_x,  hdr_y2 + 4, SEP_THIN, ROW_H * 2 + GAP_ROWS - 8, C_SEP, alpha_hex="30"))

    # Header text (tournament name). \q2 forbids word wrapping.
    if tournament.strip():
        tag = _ass_escape(tournament.strip())
        lines.append(
            f"Dialogue: 1,{_fmt_time(0)},{_fmt_time(end_ts)},Header,,0,0,0,,"
            f"{{\\an4\\pos({x1 + PAD_X},{y1 + HEADER_H // 2})\\q2}}{tag}"
        )

    # Player names (static, full duration).
    name_x = x1 + PAD_X + ACCENT_W + 6
    lines.append(
        f"Dialogue: 1,{_fmt_time(0)},{_fmt_time(end_ts)},Name,,0,0,0,,"
        f"{{\\an4\\pos({name_x},{row1_cy})\\q2}}{p1_safe}"
    )
    lines.append(
        f"Dialogue: 1,{_fmt_time(0)},{_fmt_time(end_ts)},Name,,0,0,0,,"
        f"{{\\an4\\pos({name_x},{row2_cy})\\q2}}{p2_safe}"
    )

    # ------------------------------------------------------------------ dynamic numbers
    # Each event holds (sets_a, sets_b, pts_a, pts_b) for the time range
    # [event.timestamp, next_event.timestamp). We emit four Dialogue lines
    # per range — sets and points for each player.
    sets_cx = col_sets_x + SETS_COL // 2
    pts_cx  = col_pts_x  + PTS_COL  // 2

    for i, ev in enumerate(events):
        start = max(0.0, ev.timestamp)
        end = events[i + 1].timestamp if i + 1 < len(events) else end_ts
        if end <= start:
            continue

        # P1 sets / pts
        lines.append(
            f"Dialogue: 2,{_fmt_time(start)},{_fmt_time(end)},SetNum,,0,0,0,,"
            f"{{\\an5\\pos({sets_cx},{row1_cy})}}{ev.p1_set}"
        )
        lines.append(
            f"Dialogue: 2,{_fmt_time(start)},{_fmt_time(end)},PtsNum,,0,0,0,,"
            f"{{\\an5\\pos({pts_cx},{row1_cy})}}{ev.p1_score}"
        )
        # P2 sets / pts
        lines.append(
            f"Dialogue: 2,{_fmt_time(start)},{_fmt_time(end)},SetNum,,0,0,0,,"
            f"{{\\an5\\pos({sets_cx},{row2_cy})}}{ev.p2_set}"
        )
        lines.append(
            f"Dialogue: 2,{_fmt_time(start)},{_fmt_time(end)},PtsNum,,0,0,0,,"
            f"{{\\an5\\pos({pts_cx},{row2_cy})}}{ev.p2_score}"
        )

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path
