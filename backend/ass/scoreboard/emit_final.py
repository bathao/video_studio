"""End-of-match final scoreboard with per-set columns.

Shares every layout constant with the live panel (`_Geometry`); the
only new column widths are derived from the live `sets_col` / `pts_col`
so visual rhythm matches across the cut from the live panel to the
final summary.
"""

from __future__ import annotations

from ..common import (
    C_ACCENT_HDR, C_ACCENT_P1, C_ACCENT_P2,
    C_BG_HEADER, C_BG_ROWS, C_BG_SETS,
    C_GOLD, C_GOLD_BRIGHT, C_SEP, C_WHITE,
    _ass_escape, _fmt_time, _rect,
)
from .geometry import _AssetText, _Geometry, _title_fscx_tag


def _emit_final_scoreboard(lines: list[str], g: _Geometry, t: _AssetText,
                           set_history: list[tuple[int, int, int]],
                           final_p1_sets: int, final_p2_sets: int,
                           match_end_t: float, end_ts: float) -> None:
    """End-of-match summary card with one column per played set,
    anchored at the same bottom-right corner as the live panel. Shares
    every constant with the live panel — the only new column widths
    are derived from the live `sets_col` / `pts_col` so visual rhythm
    matches across the cut."""
    n_sets = len(set_history)
    F_TOTAL_COL = g.sets_col
    F_SET_COL   = g.pts_col
    F_BAR_W = g.pad_x + g.team_col + g.name_col + F_TOTAL_COL + n_sets * F_SET_COL
    F_total_h = g.header_h + g.row_h * 2 + g.gap_rows

    F_x1 = g.video_w - F_BAR_W - g.margin
    F_y1 = g.video_h - F_total_h - g.margin
    F_x2 = F_x1 + F_BAR_W
    F_hdr_y2 = F_y1 + g.header_h
    F_mid_y  = F_hdr_y2 + g.row_h
    F_row1_cy = F_hdr_y2 + g.row_h // 2
    F_row2_cy = F_mid_y + g.gap_rows + g.row_h // 2

    F_team_x      = F_x1 + g.pad_x
    F_name_col_x  = F_team_x + g.team_col
    # Team text sits just after the accent strip (mirrors live panel),
    # name text gets larger left padding to breathe away from the
    # gold-tinted team cell.
    F_team_text_x = F_x1 + g.accent_w + max(6, int(8 * g.scale))
    F_name_pad = max(14, int(20 * g.scale))
    F_name_text_x = (F_name_col_x + F_name_pad) if g.has_team else (F_team_x + g.accent_w + 6)
    F_total_x     = F_name_col_x + g.name_col
    F_total_cx    = F_total_x + F_TOTAL_COL // 2
    F_set_cxs = [
        F_total_x + F_TOTAL_COL + i * F_SET_COL + F_SET_COL // 2
        for i in range(n_sets)
    ]
    # Column dividers: [team|]name|total, total|s1, s1|s2, ..., s(n-1)|sn.
    F_div_xs = ([F_name_col_x] if g.has_team else []) + [F_total_x] + [
        F_total_x + F_TOTAL_COL + i * F_SET_COL for i in range(n_sets)
    ]

    F_start = match_end_t
    F_end   = end_ts
    fade = "\\fad(500,300)"
    s_fmt = _fmt_time(F_start)
    e_fmt = _fmt_time(F_end)

    def fbox(x: int, y: int, w: int, h: int, color: str, alpha: str = "00", layer: int = 6) -> str:
        # Compose `_rect`'s payload + a Dialogue wrapper + a \fad tag
        # injected after the opening brace of the tag block.
        payload = "{" + fade + _rect(x, y, w, h, color, alpha_hex=alpha)[1:]
        return f"Dialogue: {layer},{s_fmt},{e_fmt},Box,,0,0,0,,{payload}"

    # Header strip + accent line — same idiom as live.
    if g.header_h > 0:
        lines.append(fbox(F_x1, F_y1, F_BAR_W, g.header_h, C_BG_HEADER, alpha="08"))
        lines.append(fbox(F_x1, F_y1, F_BAR_W, g.gold_line, C_GOLD))
        lines.append(fbox(F_x1, F_hdr_y2 - g.sep_col, F_BAR_W, g.sep_col, C_GOLD, alpha="40"))
        lines.append(fbox(F_x1, F_y1 + g.gold_line, g.accent_w, g.header_h - g.gold_line, C_ACCENT_HDR))

    lines.append(fbox(F_x1, F_hdr_y2, F_BAR_W, g.row_h * 2 + g.gap_rows, C_BG_ROWS, alpha="0C"))

    # No team-column tint here either — see _emit_live_panel.

    # Totals column tint mirrors the live SETS-column highlight.
    lines.append(fbox(F_total_x, F_hdr_y2, F_TOTAL_COL, g.row_h * 2 + g.gap_rows, C_BG_SETS, alpha="0C"))

    lines.append(fbox(F_x1, F_hdr_y2,            g.accent_w, g.row_h, C_ACCENT_P1))
    lines.append(fbox(F_x1, F_mid_y + g.gap_rows, g.accent_w, g.row_h, C_ACCENT_P2))
    lines.append(fbox(F_x1, F_mid_y, F_BAR_W, g.sep_mid, C_SEP))

    F_div_y_top = F_hdr_y2 + 6
    F_div_h     = g.row_h * 2 + g.gap_rows - 12
    for dx in F_div_xs:
        lines.append(fbox(dx, F_div_y_top, g.sep_col, F_div_h, C_SEP))

    if t.tournament:
        title = _ass_escape(t.tournament)
        clip = f"\\clip({F_x1},{F_y1 + g.gold_line},{F_x2 - g.pad_x // 2},{F_hdr_y2})"
        F_avail_w = F_BAR_W - g.pad_x - g.pad_x // 2
        F_fscx = _title_fscx_tag(t.tournament, g.fs_header, F_avail_w)
        lines.append(
            f"Dialogue: 7,{s_fmt},{e_fmt},Header,,0,0,0,,"
            f"{{\\an4\\pos({F_x1 + g.pad_x},{F_y1 + g.header_h // 2 + g.gold_line // 2})"
            f"\\q2{clip}{F_fscx}{fade}}}{title}"
        )

    if g.has_team:
        for cy_row, txt in ((F_row1_cy, t.p1_team), (F_row2_cy, t.p2_team)):
            if not txt:
                continue
            lines.append(
                f"Dialogue: 7,{s_fmt},{e_fmt},Name,,0,0,0,,"
                f"{{\\an4\\pos({F_team_text_x},{cy_row})\\q2{fade}\\b0}}{txt}"
            )

    for cy_row, name in ((F_row1_cy, t.p1), (F_row2_cy, t.p2)):
        lines.append(
            f"Dialogue: 7,{s_fmt},{e_fmt},Name,,0,0,0,,"
            f"{{\\an4\\pos({F_name_text_x},{cy_row})\\q2{fade}}}{name}"
        )

    for cy_row, total in ((F_row1_cy, final_p1_sets), (F_row2_cy, final_p2_sets)):
        lines.append(
            f"Dialogue: 7,{s_fmt},{e_fmt},SetNum,,0,0,0,,"
            f"{{\\an5\\pos({F_total_cx},{cy_row}){fade}}}{total}"
        )

    # Per-set point columns — winner of each set highlighted gold.
    for i, (p1_s, p2_s, w) in enumerate(set_history):
        cx_set = F_set_cxs[i]
        p1_c = C_GOLD_BRIGHT if w == 1 else C_WHITE
        p2_c = C_GOLD_BRIGHT if w == 2 else C_WHITE
        lines.append(
            f"Dialogue: 7,{s_fmt},{e_fmt},PtsNum,,0,0,0,,"
            f"{{\\an5\\pos({cx_set},{F_row1_cy}){fade}\\c{p1_c}}}{p1_s}"
        )
        lines.append(
            f"Dialogue: 7,{s_fmt},{e_fmt},PtsNum,,0,0,0,,"
            f"{{\\an5\\pos({cx_set},{F_row2_cy}){fade}\\c{p2_c}}}{p2_s}"
        )
