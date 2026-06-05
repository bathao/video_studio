"""Scoreboard layout geometry + style block.

`_Geometry` holds every scaled pixel position + font size used by the
live and final scoreboards. `_compute_geometry` derives the values from
(video_w, video_h, has_team, tournament). The final scoreboard reuses
the same numbers for everything except its own `BAR_W`.

`_AssetText` is the tiny holder for pre-trimmed + ASS-escaped text
strings, kept here because it travels alongside `_Geometry` to every
emit helper.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common import (
    C_DEUCE, C_GP_RED, C_GREY, C_MP_RED, C_WHITE,
    _ass_skeleton,
)


@dataclass(frozen=True)
class _Geometry:
    """All scaled px-positions and font sizes for the live scoreboard
    panel. Derived once per render by `_compute_geometry` from
    (video_w, video_h, has_team, tournament). The final scoreboard
    reuses the same values for everything except its own `BAR_W`."""
    scale: float
    has_team: bool
    video_w: int
    video_h: int
    pad_x: int
    team_col: int
    name_col: int
    sets_col: int
    pts_col: int
    bar_w: int
    row_h: int
    header_h: int
    margin: int
    accent_w: int
    gold_line: int
    sep_col: int
    sep_mid: int
    gap_rows: int
    fs_header: int
    fs_name: int
    fs_sets: int
    fs_pts: int
    fs_gp: int
    total_h: int
    x1: int
    y1: int
    x2: int
    hdr_y2: int
    mid_y: int
    col_team_x: int
    col_name_x: int
    col_sets_x: int
    col_pts_x: int
    row1_cy: int
    row2_cy: int
    cx: int   # video centre x
    cy: int   # video centre y


@dataclass(frozen=True)
class _AssetText:
    """Pre-trimmed + ASS-escaped text strings ready to splice into
    Dialogue lines. Bundling them keeps emit-helper signatures short.
    All fields are already trimmed; `tournament` is empty when unset."""
    p1: str
    p2: str
    p1_team: str
    p2_team: str
    tournament: str


def _title_fscx_tag(title_text: str, fs_header: int, available_w: int) -> str:
    """Return a `\\fscx<NN>` tag that squishes the tournament title
    horizontally to fit `available_w`, or "" if the natural width
    already fits. The 0.55 multiplier is an empirical Arial-Bold mixed
    Latin/Vietnamese width-per-em estimate at fs_header; the 55%
    floor prevents pathological inputs from collapsing the text into
    illegibility (a long single-token blob would just clip instead)."""
    if not title_text:
        return ""
    needed_w = len(title_text) * fs_header * 0.55
    if needed_w <= available_w:
        return ""
    fscx = max(55, int(available_w / needed_w * 100))
    return f"\\fscx{fscx}"


def _compute_geometry(video_w: int, video_h: int,
                      has_team: bool, tournament: str) -> _Geometry:
    scale = max(0.6, video_h / 1080.0)

    # Wider columns so the panel feels broadcast-sized, not minimap-sized.
    pad_x    = int(14 * scale)
    team_col = int(130 * scale) if has_team else 0
    name_col = int(290 * scale)
    # Sets and points share width — colour alone carries the hierarchy
    # (grey sets vs. white points).
    sets_col = int(56 * scale)
    pts_col  = int(56 * scale)
    # No trailing pad: panel's right edge ends flush with the points cell.
    bar_w    = pad_x + team_col + name_col + sets_col + pts_col

    row_h      = int(42 * scale)
    header_pad = int(6 * scale)
    header_h   = (int(24 * scale) + header_pad * 2) if tournament.strip() else 0
    margin     = int(30 * scale)
    accent_w   = max(5, int(6 * scale))
    gold_line  = max(3, int(4 * scale))
    sep_col    = max(2, int(2 * scale))
    sep_mid    = max(2, int(3 * scale))
    gap_rows   = 2

    fs_header      = max(16, int(22 * scale))
    fs_name        = max(16, int(22 * scale))
    fs_sets        = max(20, int(26 * scale))
    fs_pts         = max(20, int(26 * scale))
    fs_gp          = max(28, int(40 * scale))

    total_h = header_h + row_h * 2 + gap_rows
    x1 = video_w - bar_w - margin
    y1 = video_h - total_h - margin
    x2 = x1 + bar_w
    hdr_y2 = y1 + header_h
    mid_y  = hdr_y2 + row_h

    col_team_x = x1 + pad_x
    col_name_x = col_team_x + team_col
    col_sets_x = col_name_x + name_col
    col_pts_x  = col_sets_x + sets_col

    row1_cy = hdr_y2 + row_h // 2
    row2_cy = mid_y + gap_rows + row_h // 2

    return _Geometry(
        scale=scale, has_team=has_team,
        video_w=video_w, video_h=video_h,
        pad_x=pad_x, team_col=team_col, name_col=name_col,
        sets_col=sets_col, pts_col=pts_col, bar_w=bar_w,
        row_h=row_h, header_h=header_h, margin=margin,
        accent_w=accent_w, gold_line=gold_line,
        sep_col=sep_col, sep_mid=sep_mid, gap_rows=gap_rows,
        fs_header=fs_header, fs_name=fs_name, fs_sets=fs_sets, fs_pts=fs_pts,
        fs_gp=fs_gp,
        total_h=total_h, x1=x1, y1=y1, x2=x2,
        hdr_y2=hdr_y2, mid_y=mid_y,
        col_team_x=col_team_x, col_name_x=col_name_x,
        col_sets_x=col_sets_x, col_pts_x=col_pts_x,
        row1_cy=row1_cy, row2_cy=row2_cy,
        cx=video_w // 2, cy=video_h // 2,
    )


def _scoreboard_header(video_w: int, video_h: int, fs_header: int,
                       fs_name: int, fs_sets: int, fs_pts: int,
                       fs_gp: int) -> str:
    styles = (
        f"Style: Header, Arial, {fs_header}, {C_WHITE}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 1, 0, 1, 0,   0, 5, 0, 0, 0, 1\n"
        f"Style: Name,   Arial, {fs_name},   {C_WHITE}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 0, 0, 1, 1,   0, 4, 0, 0, 0, 1\n"
        f"Style: SetNum, Arial, {fs_sets},   {C_GREY},  &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 0, 0, 1, 1,   0, 5, 0, 0, 0, 1\n"
        f"Style: GamePoint,     Arial, {fs_gp},          {C_GP_RED},      &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 3, 0, 1, 3, 1, 6, 0, 0, 0, 1\n"
        f"Style: MatchPoint,    Arial, {int(fs_gp * 1.15)}, {C_MP_RED},   &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 4, 0, 1, 4, 2, 6, 0, 0, 0, 1\n"
        f"Style: Deuce,         Arial, {fs_gp},          {C_DEUCE},       &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 3, 0, 1, 3, 1, 6, 0, 0, 0, 1\n"
        f"Style: PtsNum, Arial, {fs_pts},    {C_WHITE}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 0, 0, 1, 1.5, 0, 5, 0, 0, 0, 1\n"
        f"Style: Box,    Arial, 1,           {C_WHITE}, &H000000FF, &H00000000, &H80000000,  0, 0, 0, 0, 100, 100, 0, 0, 1, 0,   0, 7, 0, 0, 0, 1"
    )
    return _ass_skeleton(video_w, video_h, styles)
