"""
Live scoreboard + end-of-match recap, recap/transition cards between
sets, and the GAME POINT / MATCH POINT / DEUCE flag — all generated as
a single ASS file that ffmpeg burns over the main match render.

Visual style is a port of the broadcast-look scoreboard, redrawn using
ASS primitives so the overlay can be burned in by libass at NVENC speed
instead of being rendered frame-by-frame in Python.

Layout (anchored bottom-right):

    ┌──────────────────────────────────────────────┐
    │ TOURNAMENT NAME · ROUND                      │ ← header strip
    ├──────────────────────────────────────────────┤    (gold accent line on top)
    │▌ [TEAM]  PLAYER A             │  0  │   11   │ ← row 1 (purple accent at left)
    ├──────────────────────────────────────────────┤
    │▌ [TEAM]  PLAYER B             │  0  │    7   │ ← row 2 (blue accent at left)
    └──────────────────────────────────────────────┘

Geometry scales linearly with `video_h`, so the panel keeps proportional
size at 720p, 1080p, 1440p (2K), 2160p (4K). The team column appears
only when at least one player has a team affiliation set; singles
matches keep the compact two-column layout.

Vietnamese coverage: file is written as UTF-8 and the default font is
Arial, which on Windows ships with full Latin-Extended-Additional.
libass falls back through DirectWrite to system fonts if a glyph is
missing.

The public entry point is `build_scoreboard_ass`. Internally it
delegates to small helpers (`_compute_geometry`, `_walk_events`,
`_emit_*`) so each panel section can be read and modified in
isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .common import (
    C_ACCENT_HDR, C_ACCENT_P1, C_ACCENT_P2,
    C_BG_HEADER, C_BG_ROWS, C_BG_SETS,
    C_DEUCE, C_GOLD, C_GOLD_BRIGHT, C_GP_RED, C_GREY, C_MP_RED, C_SEP, C_WHITE,
    _ass_escape, _ass_skeleton, _combine_doubles_name, _fmt_time, _rect,
    _trim_name, _trim_team, _trim_title,
)


def resolve_row_names(
    match_type: str,
    p1: str, p2: str, p3: str, p4: str,
) -> tuple[str, str]:
    """Map raw setup fields to the two scoreboard row labels.

    Singles uses (p1, p2) directly. Doubles combines partners with
    `_combine_doubles_name` so each row reads e.g. 'Văn An + Hoàng Nam'
    — short enough to fit the name column at 1080p. Exported so the
    server-side preview, the renderer, AND any future frontend mirror
    can share the same rule rather than re-deriving it three places."""
    if (match_type or "single").lower() == "double":
        return (
            _combine_doubles_name(p1, p3),
            _combine_doubles_name(p2, p4),
        )
    return (p1 or "", p2 or "")


@dataclass
class ScoreFrame:
    timestamp: float
    p1_score: int
    p2_score: int
    p1_set: int
    p2_set: int


# ---------------------------------------------------------------------------
# Internal helpers (geometry + per-section emitters)
# ---------------------------------------------------------------------------


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


def _set_final_score(prev_p1: int, prev_p2: int, won_by: int) -> tuple[int, int]:
    """
    Recover the final score of a set from the event immediately BEFORE
    the winning point. Just adds 1 to whoever won — no rule enforcement.
    What's in the events is what gets shown.
    """
    if won_by == 1:
        return (prev_p1 + 1, prev_p2)
    return (prev_p1, prev_p2 + 1)


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
    fs_recap_lbl: int
    fs_recap_score: int
    fs_transition: int
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
    fs_recap_lbl   = max(28, int(40 * scale))
    fs_recap_score = max(80, int(140 * scale))
    fs_transition  = max(110, int(200 * scale))
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
        fs_recap_lbl=fs_recap_lbl, fs_recap_score=fs_recap_score,
        fs_transition=fs_transition, fs_gp=fs_gp,
        total_h=total_h, x1=x1, y1=y1, x2=x2,
        hdr_y2=hdr_y2, mid_y=mid_y,
        col_team_x=col_team_x, col_name_x=col_name_x,
        col_sets_x=col_sets_x, col_pts_x=col_pts_x,
        row1_cy=row1_cy, row2_cy=row2_cy,
        cx=video_w // 2, cy=video_h // 2,
    )


def _walk_events(events: list[ScoreFrame],
                 sets_to_win: int) -> tuple[list[tuple[int, int, int]], Optional[float]]:
    """Walk a chronological event list, returning:

      - `set_history`: one (p1_final, p2_final, won_by) tuple per
        completed set, derived from the score immediately BEFORE the
        winning point (the live scorer pushes the post-reset event with
        set++ and score = 0,0).
      - `match_end_t`: timestamp of the event that pushed one player to
        `sets_to_win`, or None if the match is still in progress.
    """
    set_history: list[tuple[int, int, int]] = []
    match_end_t: Optional[float] = None
    for i in range(1, len(events)):
        prev = events[i - 1]
        cur = events[i]
        if cur.p1_set > prev.p1_set:
            won_by = 1
        elif cur.p2_set > prev.p2_set:
            won_by = 2
        else:
            continue
        p1f, p2f = _set_final_score(prev.p1_score, prev.p2_score, won_by)
        set_history.append((p1f, p2f, won_by))
        if match_end_t is None and max(cur.p1_set, cur.p2_set) >= sets_to_win:
            match_end_t = cur.timestamp
    return set_history, match_end_t


def _scoreboard_header(video_w: int, video_h: int, fs_header: int,
                       fs_name: int, fs_sets: int, fs_pts: int,
                       fs_recap_lbl: int, fs_recap_score: int,
                       fs_transition: int, fs_gp: int) -> str:
    styles = (
        f"Style: Header, Arial, {fs_header}, {C_WHITE}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 1, 0, 1, 0,   0, 5, 0, 0, 0, 1\n"
        f"Style: Name,   Arial, {fs_name},   {C_WHITE}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 0, 0, 1, 1,   0, 4, 0, 0, 0, 1\n"
        f"Style: SetNum, Arial, {fs_sets},   {C_GREY},  &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 0, 0, 1, 1,   0, 5, 0, 0, 0, 1\n"
        f"Style: SetLabel,      Arial, {fs_recap_lbl},   {C_GOLD_BRIGHT}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 4, 0, 1, 2, 1, 5, 0, 0, 0, 1\n"
        f"Style: SetRecap,      Arial, {fs_recap_score}, {C_WHITE},       &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 6, 0, 1, 4, 2, 5, 0, 0, 0, 1\n"
        f"Style: SetTransition, Arial, {fs_transition},  {C_GOLD_BRIGHT}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 8, 0, 1, 5, 3, 5, 0, 0, 0, 1\n"
        f"Style: GamePoint,     Arial, {fs_gp},          {C_GP_RED},      &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 3, 0, 1, 3, 1, 6, 0, 0, 0, 1\n"
        f"Style: MatchPoint,    Arial, {int(fs_gp * 1.15)}, {C_MP_RED},   &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 4, 0, 1, 4, 2, 6, 0, 0, 0, 1\n"
        f"Style: Deuce,         Arial, {fs_gp},          {C_DEUCE},       &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 3, 0, 1, 3, 1, 6, 0, 0, 0, 1\n"
        f"Style: PtsNum, Arial, {fs_pts},    {C_WHITE}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 0, 0, 1, 1.5, 0, 5, 0, 0, 0, 1\n"
        f"Style: Box,    Arial, 1,           {C_WHITE}, &H000000FF, &H00000000, &H80000000,  0, 0, 0, 0, 100, 100, 0, 0, 1, 0,   0, 7, 0, 0, 0, 1"
    )
    return _ass_skeleton(video_w, video_h, styles)


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


def _emit_recap_cards(lines: list[str], g: _Geometry,
                      events: list[ScoreFrame],
                      end_ts: float, sets_to_win: int) -> None:
    """SET N recap (label + final score) followed by a SET N+1 transition
    card after each completed (non-match-ending) set."""
    RECAP_DUR = 4.0
    TRANS_DUR = 4.5

    def _recap_score_text(p1_final: int, p2_final: int, winner: int) -> str:
        if winner == 1:
            return (f"{{\\c{C_GOLD_BRIGHT}}}{p1_final}{{\\c{C_GREY}}}  —  "
                    f"{{\\c{C_WHITE}}}{p2_final}")
        return (f"{{\\c{C_WHITE}}}{p1_final}{{\\c{C_GREY}}}  —  "
                f"{{\\c{C_GOLD_BRIGHT}}}{p2_final}")

    for i in range(1, len(events)):
        prev = events[i - 1]
        cur = events[i]
        if cur.p1_set > prev.p1_set:
            won_by = 1
        elif cur.p2_set > prev.p2_set:
            won_by = 2
        else:
            continue
        # Skip recap for the match-ending set — the final scoreboard
        # takes the centre of the screen at that moment.
        if max(cur.p1_set, cur.p2_set) >= sets_to_win:
            continue
        p1_final, p2_final = _set_final_score(prev.p1_score, prev.p2_score, won_by)

        ended_set_n = cur.p1_set + cur.p2_set
        next_set_n  = ended_set_n + 1

        T = max(0.0, cur.timestamp)
        recap_start = T
        recap_end   = min(end_ts, T + RECAP_DUR)
        trans_start = recap_end
        trans_end   = min(end_ts, trans_start + TRANS_DUR)

        recap_lbl_y   = g.cy - int(110 * g.scale)
        recap_score_y = g.cy + int(40  * g.scale)

        lines.append(
            f"Dialogue: 5,{_fmt_time(recap_start)},{_fmt_time(recap_end)},SetLabel,,0,0,0,,"
            f"{{\\an5\\pos({g.cx},{recap_lbl_y})\\fad(300,400)}}SET {ended_set_n}"
        )
        score_text = _recap_score_text(p1_final, p2_final, won_by)
        lines.append(
            f"Dialogue: 5,{_fmt_time(recap_start)},{_fmt_time(recap_end)},SetRecap,,0,0,0,,"
            f"{{\\an5\\pos({g.cx},{recap_score_y})\\fad(300,400)}}{score_text}"
        )

        if trans_end > trans_start:
            lines.append(
                f"Dialogue: 5,{_fmt_time(trans_start)},{_fmt_time(trans_end)},SetTransition,,0,0,0,,"
                f"{{\\an5\\pos({g.cx},{g.cy})\\fad(300,300)"
                f"\\fscx80\\fscy80\\t(0,400,\\fscx100\\fscy100)}}SET {next_set_n}"
            )


def _emit_flag_overlays(lines: list[str], g: _Geometry,
                        events: list[ScoreFrame],
                        scoreboard_end_t: float, end_ts: float,
                        sets_to_win: int) -> None:
    """GAME POINT / MATCH POINT / DEUCE flag, anchored just above the
    panel and pulsing for the duration of the qualifying state.

    Conditions evaluated on the event that introduced the state:
      DEUCE     : both >= 10 and equal
      GAME POINT: leader >= 10, lead >= 1
    Game point upgrades to match point when winning this set also wins
    the match (`leader_sets + 1 >= sets_to_win`).
    """
    flag_x = g.x2
    flag_y = g.y1 - max(12, int(14 * g.scale))
    GP_PERIOD_MS = 600

    def _build_pulse(duration_s: float) -> str:
        ms = int(duration_s * 1000)
        cycles = max(1, ms // GP_PERIOD_MS + 1)
        out = []
        for c in range(cycles):
            t0 = c * GP_PERIOD_MS
            t_mid = t0 + GP_PERIOD_MS // 2
            t_end = t0 + GP_PERIOD_MS
            out.append(f"\\t({t0},{t_mid},\\1a&H78&)\\t({t_mid},{t_end},\\1a&H00&)")
        return "".join(out)

    for i, ev in enumerate(events):
        start_t = max(0.0, ev.timestamp)
        end_t = events[i + 1].timestamp if i + 1 < len(events) else end_ts
        end_t = min(end_t, scoreboard_end_t)
        if end_t <= start_t:
            continue

        leader = 0  # 1 / 2 / 0 = none
        if ev.p1_score >= 10 and ev.p2_score >= 10 and ev.p1_score == ev.p2_score:
            style = "Deuce"
            text = "DEUCE"
        elif ev.p1_score >= 10 and ev.p1_score - ev.p2_score >= 1:
            leader = 1
        elif ev.p2_score >= 10 and ev.p2_score - ev.p1_score >= 1:
            leader = 2
        else:
            continue

        if leader:
            leader_sets = ev.p1_set if leader == 1 else ev.p2_set
            if leader_sets + 1 >= sets_to_win:
                style = "MatchPoint"
                text = "MATCH POINT"
            else:
                style = "GamePoint"
                text = "GAME POINT"

        pulse = _build_pulse(end_t - start_t)
        # \an3 = bottom-right anchor: text extends leftward from flag_x
        # so it never overruns the screen edge.
        lines.append(
            f"Dialogue: 4,{_fmt_time(start_t)},{_fmt_time(end_t)},{style},,0,0,0,,"
            f"{{\\an3\\pos({flag_x},{flag_y}){pulse}}}{text}"
        )


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


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_scoreboard_ass_text(
    *,
    video_w: int,
    video_h: int,
    total_duration: float,
    tournament: str,
    p1_name: str,
    p2_name: str,
    score_events: Iterable[ScoreFrame],
    best_of: int = 5,
    p1_team: str = "",
    p2_team: str = "",
    match_type: str = "single",
    p3_name: str = "",
    p4_name: str = "",
) -> str:
    """Build the scoreboard ASS content as a string. Six sections:

      1. Static live panel (header, rows, accents, dividers, names)
      2. Per-event live numbers (sets / pts updates)
      3. SET N recap + SET N+1 transition cards
      4. GAME POINT / MATCH POINT / DEUCE flag pulses
      5. End-of-match final scoreboard with per-set columns

    Each section emits Dialogue lines with non-overlapping layer + time
    ranges, so libass composites them in a deterministic z-order.

    Two consumers share this function: the render pipeline (via
    `build_scoreboard_ass`, which writes to disk for ffmpeg's `ass=`
    filter) and the live preview endpoint (which streams the text
    straight to the JASSUB-in-browser renderer). Sharing this single
    source guarantees the preview overlay is byte-identical to the
    burned-in scoreboard.
    """
    events = sorted(score_events, key=lambda e: e.timestamp)
    if not events or events[0].timestamp > 0.0:
        events = [ScoreFrame(0.0, 0, 0, 0, 0), *events]

    has_team = bool((p1_team or "").strip() or (p2_team or "").strip())
    g = _compute_geometry(video_w, video_h, has_team, tournament)

    end_ts = total_duration + 1
    # Resolve doubles row labels here so every downstream emitter
    # (_emit_live_panel, _emit_final_scoreboard) just sees one name per
    # row and doesn't need a separate doubles branch.
    row_top, row_bot = resolve_row_names(match_type, p1_name, p2_name, p3_name, p4_name)
    # Tournament gets a generous 45-char cap when no team column is
    # drawn — the wider singles layout has visible room for a longer
    # title. With a team column the word cap (14) is enough.
    title_trimmed = _trim_title(tournament, max_chars=None if has_team else 45)
    text = _AssetText(
        p1=_ass_escape(_trim_name(row_top)),
        p2=_ass_escape(_trim_name(row_bot)),
        p1_team=_ass_escape(_trim_team(p1_team)),
        p2_team=_ass_escape(_trim_team(p2_team)),
        tournament=title_trimmed,
    )

    sets_to_win = (best_of + 1) // 2
    set_history, match_end_t = _walk_events(events, sets_to_win)
    scoreboard_end_t = match_end_t if match_end_t is not None else end_ts

    lines: list[str] = [_scoreboard_header(
        video_w, video_h,
        g.fs_header, g.fs_name, g.fs_sets, g.fs_pts,
        g.fs_recap_lbl, g.fs_recap_score, g.fs_transition, g.fs_gp,
    )]

    _emit_live_panel(lines, g, text, scoreboard_end_t)
    _emit_dynamic_numbers(lines, g, events, scoreboard_end_t, end_ts)
    _emit_recap_cards(lines, g, events, end_ts, sets_to_win)
    _emit_flag_overlays(lines, g, events, scoreboard_end_t, end_ts, sets_to_win)

    if match_end_t is not None and set_history:
        last_ev = events[-1]
        _emit_final_scoreboard(
            lines, g, text, set_history,
            last_ev.p1_set, last_ev.p2_set,
            match_end_t, end_ts,
        )

    return "\n".join(lines)


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
    best_of: int = 5,
    p1_team: str = "",
    p2_team: str = "",
    match_type: str = "single",
    p3_name: str = "",
    p4_name: str = "",
) -> Path:
    """Write the scoreboard ASS to disk. Thin wrapper around
    `build_scoreboard_ass_text` — kept for the render pipeline which
    needs an on-disk file for ffmpeg's `ass=` filter."""
    output_path.write_text(
        build_scoreboard_ass_text(
            video_w=video_w, video_h=video_h,
            total_duration=total_duration,
            tournament=tournament,
            p1_name=p1_name, p2_name=p2_name,
            score_events=score_events,
            best_of=best_of,
            p1_team=p1_team, p2_team=p2_team,
            match_type=match_type,
            p3_name=p3_name, p4_name=p4_name,
        ),
        encoding="utf-8",
    )
    return output_path
