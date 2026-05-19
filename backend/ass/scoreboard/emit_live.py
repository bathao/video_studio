"""Live scoreboard panel emitters: static chrome + per-event numbers.

Two Dialogue-emitting helpers:
  - `_emit_live_panel`: header strip + row backgrounds + column tints +
    accent bars + dividers + player names + team text + tournament.
    Static for the whole match duration.
  - `_emit_dynamic_numbers`: per-event Dialogue lines for the sets / pts
    cells, visible for [event, next_event).
"""

from __future__ import annotations

from ..common import (
    C_ACCENT_HDR, C_ACCENT_P1, C_ACCENT_P2,
    C_BG_HEADER, C_BG_ROWS, C_BG_SETS,
    C_GOLD, C_SEP,
    _ass_escape, _fmt_time, _rect,
)
from .events import ScoreFrame
from .geometry import _AssetText, _Geometry, _title_fscx_tag


def _emit_live_panel(lines: list[str], g: _Geometry, t: _AssetText,
                     scoreboard_end_t: float) -> None:
    """Static elements visible from t=0 until match end: header strip,
    row backgrounds, column tints, accent bars, dividers, plus the
    tournament / team / player name text."""
    end = _fmt_time(scoreboard_end_t)
    start = _fmt_time(0)

    def add_static(payload: str, layer: int = 0) -> None:
        lines.append(f"Dialogue: {layer},{start},{end},Box,,0,0,0,,{payload}")

    # Header strip — anchors the panel and makes the title prominent.
    if g.header_h > 0:
        add_static(_rect(g.x1, g.y1, g.bar_w, g.header_h, C_BG_HEADER, alpha_hex="08"))
        add_static(_rect(g.x1, g.y1, g.bar_w, g.gold_line, C_GOLD, alpha_hex="00"))
        add_static(_rect(g.x1, g.hdr_y2 - g.sep_col, g.bar_w, g.sep_col, C_GOLD, alpha_hex="40"))
        # Header left accent bar — same idiom as the player rows.
        add_static(_rect(g.x1, g.y1 + g.gold_line, g.accent_w, g.header_h - g.gold_line, C_ACCENT_HDR, alpha_hex="00"))

    # Player rows background.
    add_static(_rect(g.x1, g.hdr_y2, g.bar_w, g.row_h * 2 + g.gap_rows, C_BG_ROWS, alpha_hex="0C"))

    # No team-column tint — the dark row background carries through and
    # the vertical divider after the team text is enough to set the
    # column apart visually. The gold tint here read too garish.

    # Set-point column tint.
    add_static(_rect(g.col_sets_x, g.hdr_y2, g.sets_col, g.row_h * 2 + g.gap_rows, C_BG_SETS, alpha_hex="0C"))

    # Per-player accent bars on the left edge.
    add_static(_rect(g.x1, g.hdr_y2,                 g.accent_w, g.row_h, C_ACCENT_P1, alpha_hex="00"))
    add_static(_rect(g.x1, g.mid_y + g.gap_rows,     g.accent_w, g.row_h, C_ACCENT_P2, alpha_hex="00"))

    # Mid separator between the two player rows.
    add_static(_rect(g.x1, g.mid_y, g.bar_w, g.sep_mid, C_SEP, alpha_hex="00"))

    # Vertical column dividers — fully opaque so cells read clearly.
    div_y_top = g.hdr_y2 + 6
    div_h     = g.row_h * 2 + g.gap_rows - 12
    if g.has_team:
        add_static(_rect(g.col_name_x, div_y_top, g.sep_col, div_h, C_SEP, alpha_hex="00"))
    add_static(_rect(g.col_sets_x, div_y_top, g.sep_col, div_h, C_SEP, alpha_hex="00"))
    add_static(_rect(g.col_pts_x,  div_y_top, g.sep_col, div_h, C_SEP, alpha_hex="00"))

    # Tournament name in the header strip — clipped to the strip
    # rectangle so a long title can't overflow visually. Already
    # trimmed at build time per the layout's `has_team` mode. Long
    # titles further auto-squish horizontally via `\fscx` so a 45-char
    # tournament name stays readable inside the panel width.
    if t.tournament:
        title = _ass_escape(t.tournament)
        clip = f"\\clip({g.x1},{g.y1 + g.gold_line},{g.x2 - g.pad_x // 2},{g.hdr_y2})"
        avail_w = g.bar_w - g.pad_x - g.pad_x // 2
        fscx = _title_fscx_tag(t.tournament, g.fs_header, avail_w)
        lines.append(
            f"Dialogue: 1,{start},{end},Header,,0,0,0,,"
            f"{{\\an4\\pos({g.x1 + g.pad_x},{g.y1 + g.header_h // 2 + g.gold_line // 2})\\q2{clip}{fscx}}}{title}"
        )

    # Team text — white + non-bold over the gold-tint cell. Skipped
    # cell-wise when one player has a team and the other doesn't.
    # Anchored just after the accent strip so the text sits visually
    # flush against the left of the cell (matches the team-tint extent).
    if g.has_team:
        team_text_x = g.x1 + g.accent_w + max(6, int(8 * g.scale))
        for cy_row, txt in ((g.row1_cy, t.p1_team), (g.row2_cy, t.p2_team)):
            if not txt:
                continue
            lines.append(
                f"Dialogue: 1,{start},{end},Name,,0,0,0,,"
                f"{{\\an4\\pos({team_text_x},{cy_row})\\q2\\b0}}{txt}"
            )

    # Player names — bold white. Anchor x shifts when the team column
    # is present so names tuck inside the team divider, with extra
    # breathing room from the gold-tinted team cell.
    name_pad = max(14, int(20 * g.scale))
    name_text_x = (g.col_name_x + name_pad) if g.has_team else (g.col_team_x + g.accent_w + 6)
    lines.append(
        f"Dialogue: 1,{start},{end},Name,,0,0,0,,"
        f"{{\\an4\\pos({name_text_x},{g.row1_cy})\\q2}}{t.p1}"
    )
    lines.append(
        f"Dialogue: 1,{start},{end},Name,,0,0,0,,"
        f"{{\\an4\\pos({name_text_x},{g.row2_cy})\\q2}}{t.p2}"
    )


def _emit_dynamic_numbers(lines: list[str], g: _Geometry,
                          events: list[ScoreFrame],
                          scoreboard_end_t: float, end_ts: float) -> None:
    """Per-event sets/pts for both rows. Each event drives a Dialogue
    line that's visible for the time range [event, next_event)."""
    sets_cx = g.col_sets_x + g.sets_col // 2
    pts_cx  = g.col_pts_x  + g.pts_col  // 2

    for i, ev in enumerate(events):
        start_t = max(0.0, ev.timestamp)
        end_t = events[i + 1].timestamp if i + 1 < len(events) else end_ts
        end_t = min(end_t, scoreboard_end_t)
        if end_t <= start_t:
            continue
        start = _fmt_time(start_t)
        end = _fmt_time(end_t)
        # P1
        lines.append(
            f"Dialogue: 2,{start},{end},SetNum,,0,0,0,,"
            f"{{\\an5\\pos({sets_cx},{g.row1_cy})}}{ev.p1_set}"
        )
        lines.append(
            f"Dialogue: 2,{start},{end},PtsNum,,0,0,0,,"
            f"{{\\an5\\pos({pts_cx},{g.row1_cy})}}{ev.p1_score}"
        )
        # P2
        lines.append(
            f"Dialogue: 2,{start},{end},SetNum,,0,0,0,,"
            f"{{\\an5\\pos({sets_cx},{g.row2_cy})}}{ev.p2_set}"
        )
        lines.append(
            f"Dialogue: 2,{start},{end},PtsNum,,0,0,0,,"
            f"{{\\an5\\pos({pts_cx},{g.row2_cy})}}{ev.p2_score}"
        )
