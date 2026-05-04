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


def _trim_team(text: str, max_len: int = 14) -> str:
    """Like `_trim_name` but returns an empty string for empty input
    instead of a placeholder, so an unset team renders as a blank cell
    rather than the literal word PLAYER."""
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


def _set_final_score(prev_p1: int, prev_p2: int, won_by: int) -> tuple[int, int]:
    """
    Recover the final score of a set from the event immediately BEFORE
    the winning point. Just adds 1 to whoever won — no rule enforcement.
    What's in the events is what gets shown.
    """
    if won_by == 1:
        return (prev_p1 + 1, prev_p2)
    return (prev_p1, prev_p2 + 1)


def _trim_title(text: str, max_words: int = 10) -> str:
    """Tournament title: cap at 10 whitespace-separated tokens. Anything
    longer gets truncated with an ellipsis. Keeps the panel layout
    predictable regardless of how chatty the user is."""
    text = (text or "").strip()
    if not text:
        return ""
    tokens = text.split()
    if len(tokens) <= max_words:
        return text
    return " ".join(tokens[:max_words]) + "…"


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


def _bgr(c: str) -> str:
    """Strip the &H...& wrapping from an ASS colour. Used when a colour
    has to be embedded in a raw f-string (drawing primitives, inline
    \\1c overrides) instead of as a Style: column."""
    return c.strip("&H&")


# Palette (R, G, B). Each comment names the on-screen colour.
C_WHITE       = _ass_rgb(255, 255, 255)
C_GREY        = _ass_rgb(185, 185, 185)
C_GOLD        = _ass_rgb(180, 140,  40)   # broadcast amber accent
C_GOLD_BRIGHT = _ass_rgb(255, 195,  60)   # brighter gold for emphasis
C_SEP         = _ass_rgb( 75,  75,  75)   # divider lines
C_BG_HEADER   = _ass_rgb( 35,  35,  35)   # near-black header strip
C_BG_ROWS     = _ass_rgb( 18,  18,  18)   # near-black player rows
C_BG_SETS     = _ass_rgb(102,  76,  24)   # gold-tinted dark for the sets (set-point) column
C_BG_TEAM     = _ass_rgb( 70,  58,  32)   # muted gold for the optional team column (sibling of C_BG_SETS, desaturated)
C_ACCENT_HDR  = _ass_rgb(180, 140,  40)   # gold accent for tournament header
C_ACCENT_P1   = _ass_rgb(165, 100, 220)   # purple (player A)
C_ACCENT_P2   = _ass_rgb( 50, 140, 220)   # sky blue (player B)
C_GP_RED      = _ass_rgb(255,  85,  85)   # game-point flag red
C_MP_RED      = _ass_rgb(255,  40,  40)   # match-point flag (deeper red)
C_DEUCE       = _ass_rgb(255, 195,  60)   # deuce flag amber


def _ass_header(video_w: int, video_h: int, fs_header: int,
                fs_name: int, fs_sets: int, fs_pts: int,
                fs_recap_lbl: int, fs_recap_score: int,
                fs_transition: int, fs_gp: int) -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Header, Arial, {fs_header}, {C_WHITE}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 1, 0, 1, 0,   0, 5, 0, 0, 0, 1
Style: Name,   Arial, {fs_name},   {C_WHITE}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 0, 0, 1, 1,   0, 4, 0, 0, 0, 1
Style: SetNum, Arial, {fs_sets},   {C_GREY},  &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 0, 0, 1, 1,   0, 5, 0, 0, 0, 1
Style: SetLabel,      Arial, {fs_recap_lbl},   {C_GOLD_BRIGHT}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 4, 0, 1, 2, 1, 5, 0, 0, 0, 1
Style: SetRecap,      Arial, {fs_recap_score}, {C_WHITE},       &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 6, 0, 1, 4, 2, 5, 0, 0, 0, 1
Style: SetTransition, Arial, {fs_transition},  {C_GOLD_BRIGHT}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 8, 0, 1, 5, 3, 5, 0, 0, 0, 1
Style: GamePoint,     Arial, {fs_gp},          {C_GP_RED},      &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 3, 0, 1, 3, 1, 6, 0, 0, 0, 1
Style: MatchPoint,    Arial, {int(fs_gp * 1.15)}, {C_MP_RED},   &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 4, 0, 1, 4, 2, 6, 0, 0, 0, 1
Style: Deuce,         Arial, {fs_gp},          {C_DEUCE},       &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 3, 0, 1, 3, 1, 6, 0, 0, 0, 1
Style: PtsNum, Arial, {fs_pts},    {C_WHITE}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 0, 0, 1, 1.5, 0, 5, 0, 0, 0, 1
Style: Box,    Arial, 1,           {C_WHITE}, &H000000FF, &H00000000, &H80000000,  0, 0, 0, 0, 100, 100, 0, 0, 1, 0,   0, 7, 0, 0, 0, 1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _rect(x: int, y: int, w: int, h: int, color: str, alpha_hex: str = "00", layer: int = 0) -> str:
    """ASS drawing primitive: filled rectangle anchored top-left at (x,y)."""
    return (
        f"{{\\an7\\pos({x},{y})\\bord0\\shad0"
        f"\\1c&H{_bgr(color)}&\\1a&H{alpha_hex}&\\p1}}"
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
    best_of: int = 5,
    p1_team: str = "",
    p2_team: str = "",
) -> Path:
    events = sorted(score_events, key=lambda e: e.timestamp)
    if not events or events[0].timestamp > 0.0:
        events = [ScoreFrame(0.0, 0, 0, 0, 0), *events]

    # All geometry is tuned for 1080p; everything scales linearly with
    # video_h so the panel keeps the same on-screen footprint at 2K/4K.
    scale = max(0.6, video_h / 1080.0)

    # Team column appears only when at least one player has a team
    # affiliation set — singles matches keep the compact layout.
    has_team = bool((p1_team or "").strip() or (p2_team or "").strip())

    # Wider columns so the panel feels broadcast-sized, not minimap-sized.
    PAD_X      = int(14 * scale)
    TEAM_COL   = int(130 * scale) if has_team else 0
    NAME_COL   = int(290 * scale)
    # Sets and points share the same column width — number cells should
    # match visually (only colour distinguishes them).
    SETS_COL   = int(56  * scale)
    PTS_COL    = int(56  * scale)
    # No trailing PAD_X: the panel's right edge ends flush with the
    # right edge of the points cell so there's no "dead" strip after
    # the last column.
    BAR_W      = PAD_X + TEAM_COL + NAME_COL + SETS_COL + PTS_COL
    ROW_H      = int(42 * scale)
    HEADER_PAD = int(6 * scale)
    HEADER_H   = (int(24 * scale) + HEADER_PAD * 2) if tournament.strip() else 0
    MARGIN     = int(30 * scale)
    ACCENT_W   = max(5, int(6 * scale))
    GOLD_LINE  = max(3, int(4 * scale))
    SEP_COL    = max(2, int(2 * scale))   # column dividers between cells
    SEP_MID    = max(2, int(3 * scale))   # divider between the two player rows
    GAP_ROWS   = 2

    fs_header = max(16, int(22 * scale))
    fs_name   = max(16, int(22 * scale))
    # Set count and points use the same size so neither visually dominates;
    # colour alone carries the hierarchy (grey sets vs. white points).
    fs_sets   = max(20, int(26 * scale))
    fs_pts    = max(20, int(26 * scale))
    # Broadcast overlay sizes (set transition cards, game-point flag).
    fs_recap_lbl   = max(28, int(40  * scale))
    fs_recap_score = max(80, int(140 * scale))
    fs_transition  = max(110, int(200 * scale))
    fs_gp          = max(28, int(40  * scale))

    total_h = HEADER_H + ROW_H * 2 + GAP_ROWS
    x1 = video_w - BAR_W - MARGIN
    y1 = video_h - total_h - MARGIN
    x2 = x1 + BAR_W
    y2 = y1 + total_h
    hdr_y2 = y1 + HEADER_H        # bottom of header strip = top of player rows
    mid_y = hdr_y2 + ROW_H        # divider between the two player rows

    col_team_x = x1 + PAD_X
    col_name_x = col_team_x + TEAM_COL          # name origin shifts when team is shown
    col_sets_x = col_name_x + NAME_COL
    col_pts_x  = col_sets_x + SETS_COL

    # vertical centre of each row, used for middle-anchored text
    row1_cy = hdr_y2 + ROW_H // 2
    row2_cy = mid_y + GAP_ROWS + ROW_H // 2

    end_ts = total_duration + 1
    p1_safe = _ass_escape(_trim_name(p1_name))
    p2_safe = _ass_escape(_trim_name(p2_name))
    p1_team_safe = _ass_escape(_trim_team(p1_team))
    p2_team_safe = _ass_escape(_trim_team(p2_team))

    # Walk events to recover the per-set final scores and the moment the
    # match ended (when one player reaches sets_to_win). The main
    # scoreboard stays visible until that moment; after it, the final
    # scoreboard takes the centre of the screen for the rest of the video.
    sets_to_win = (best_of + 1) // 2
    set_history: list[tuple[int, int, int]] = []  # (p1_final, p2_final, won_by)
    match_end_t: float | None = None
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

    scoreboard_end_t = match_end_t if match_end_t is not None else end_ts

    lines: list[str] = [_ass_header(
        video_w, video_h, fs_header, fs_name, fs_sets, fs_pts,
        fs_recap_lbl, fs_recap_score, fs_transition, fs_gp,
    )]

    def add_static(payload: str, layer: int = 0) -> None:
        lines.append(f"Dialogue: {layer},{_fmt_time(0)},{_fmt_time(scoreboard_end_t)},Box,,0,0,0,,{payload}")

    # ------------------------------------------------------------------ static layer
    # Header strip — a darker, more opaque slab that visually anchors the
    # panel and makes the title prominent. Spans the full panel width.
    if HEADER_H > 0:
        add_static(_rect(x1, y1, BAR_W, HEADER_H, C_BG_HEADER, alpha_hex="08"))
        # Gold accent line at the very top, full width.
        add_static(_rect(x1, y1, BAR_W, GOLD_LINE, C_GOLD, alpha_hex="00"))
        # Solid divider under the header.
        add_static(_rect(x1, hdr_y2 - SEP_COL, BAR_W, SEP_COL, C_GOLD, alpha_hex="40"))
        # Header left accent bar — same idiom as the player rows below
        # so the panel reads as one consistent broadcast graphic.
        add_static(_rect(x1, y1 + GOLD_LINE, ACCENT_W, HEADER_H - GOLD_LINE, C_ACCENT_HDR, alpha_hex="00"))

    # Player rows background.
    add_static(_rect(x1, hdr_y2, BAR_W, ROW_H * 2 + GAP_ROWS, C_BG_ROWS, alpha_hex="0C"))

    # Team column tint — subtle steel-blue panel that visually anchors
    # the optional team / club affiliation without competing with the
    # gold sets column.
    if has_team:
        add_static(_rect(col_team_x, hdr_y2, TEAM_COL, ROW_H * 2 + GAP_ROWS, C_BG_TEAM, alpha_hex="0C"))

    # Set-point column tint — distinguishes the sets cell from the
    # points cell at a glance. Same opacity as the row bg so it reads
    # as a solid coloured cell, not a translucent overlay.
    add_static(_rect(col_sets_x, hdr_y2, SETS_COL, ROW_H * 2 + GAP_ROWS, C_BG_SETS, alpha_hex="0C"))

    # Left edge accent bars (one per player).
    add_static(_rect(x1, hdr_y2,                ACCENT_W, ROW_H, C_ACCENT_P1, alpha_hex="00"))
    add_static(_rect(x1, mid_y + GAP_ROWS,      ACCENT_W, ROW_H, C_ACCENT_P2, alpha_hex="00"))

    # Mid separator between the two player rows.
    add_static(_rect(x1, mid_y, BAR_W, SEP_MID, C_SEP, alpha_hex="00"))

    # Vertical column dividers — fully opaque so cells read clearly.
    # The panel's right edge itself (x2) closes the points cell, so we
    # only need dividers between cells, not after the last one.
    div_y_top = hdr_y2 + 6
    div_h     = ROW_H * 2 + GAP_ROWS - 12
    if has_team:
        add_static(_rect(col_name_x, div_y_top, SEP_COL, div_h, C_SEP, alpha_hex="00"))
    add_static(_rect(col_sets_x, div_y_top, SEP_COL, div_h, C_SEP, alpha_hex="00"))
    add_static(_rect(col_pts_x,  div_y_top, SEP_COL, div_h, C_SEP, alpha_hex="00"))

    # Header text (tournament name). Capped at 8 words by _trim_title so
    # the layout stays predictable. Left-aligned with the same padding
    # as the player names below; \clip clips any residual overflow to
    # the header strip rectangle.
    if tournament.strip():
        tag = _ass_escape(_trim_title(tournament))
        clip = f"\\clip({x1},{y1 + GOLD_LINE},{x2 - PAD_X // 2},{hdr_y2})"
        lines.append(
            f"Dialogue: 1,{_fmt_time(0)},{_fmt_time(scoreboard_end_t)},Header,,0,0,0,,"
            f"{{\\an4\\pos({x1 + PAD_X},{y1 + HEADER_H // 2 + GOLD_LINE // 2})\\q2{clip}}}{tag}"
        )

    # Team names (static, until match end). White + non-bold over the
    # steel-blue tint cell — readable but visually subordinate to the
    # bold white player name on the right. Skipped entirely when neither
    # player has a team set, preserving the compact singles layout.
    if has_team:
        team_text_x = col_team_x + ACCENT_W + 6
        for cy_row, txt in ((row1_cy, p1_team_safe), (row2_cy, p2_team_safe)):
            if not txt:
                continue
            lines.append(
                f"Dialogue: 1,{_fmt_time(0)},{_fmt_time(scoreboard_end_t)},Name,,0,0,0,,"
                f"{{\\an4\\pos({team_text_x},{cy_row})\\q2\\b0}}{txt}"
            )

    # Player names (static, until match end). When the team column is
    # present, names anchor just inside it (no extra accent-bar offset);
    # otherwise they sit flush with the left accent bar.
    name_text_x = (col_name_x + 6) if has_team else (col_team_x + ACCENT_W + 6)
    lines.append(
        f"Dialogue: 1,{_fmt_time(0)},{_fmt_time(scoreboard_end_t)},Name,,0,0,0,,"
        f"{{\\an4\\pos({name_text_x},{row1_cy})\\q2}}{p1_safe}"
    )
    lines.append(
        f"Dialogue: 1,{_fmt_time(0)},{_fmt_time(scoreboard_end_t)},Name,,0,0,0,,"
        f"{{\\an4\\pos({name_text_x},{row2_cy})\\q2}}{p2_safe}"
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
        end = min(end, scoreboard_end_t)
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

    # ------------------------------------------------------------------ broadcast overlays
    cx = video_w // 2
    cy = video_h // 2
    flag_x = x2                           # right edge of scoreboard panel
    flag_y = y1 - max(12, int(14 * scale))   # just above the panel

    # SET TRANSITION + RECAP CARDS
    # When a set is won, we emit two stacked cards:
    #   T → T+2.0s : recap "SET N" + final score (winner highlighted)
    #   T+2.0 → T+3.5s : transition card "SET N+1"
    #
    # The winning point itself isn't recorded as its own event (the live
    # scorer pushes the post-reset event with set++ and score = 0,0). So
    # we recover the set's final score from the previous event:
    #   prev (10, 7) → P1 scored to 11, won → cur (0, 0) with set+1
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
        # Skip recap + transition for the match-ending set: the final
        # scoreboard takes the centre of the screen at that moment.
        if max(cur.p1_set, cur.p2_set) >= sets_to_win:
            continue
        p1_final, p2_final = _set_final_score(prev.p1_score, prev.p2_score, won_by)

        ended_set_n = cur.p1_set + cur.p2_set     # the set that just ended
        next_set_n  = ended_set_n + 1

        T = max(0.0, cur.timestamp)
        recap_start = T
        recap_end   = min(end_ts, T + RECAP_DUR)
        trans_start = recap_end
        trans_end   = min(end_ts, trans_start + TRANS_DUR)

        # --- Recap card: "SET N" label + big score "11 — 7" ---
        recap_lbl_y   = cy - int(110 * scale)
        recap_score_y = cy + int(40  * scale)

        lines.append(
            f"Dialogue: 5,{_fmt_time(recap_start)},{_fmt_time(recap_end)},SetLabel,,0,0,0,,"
            f"{{\\an5\\pos({cx},{recap_lbl_y})\\fad(300,400)}}SET {ended_set_n}"
        )
        score_text = _recap_score_text(p1_final, p2_final, won_by)
        lines.append(
            f"Dialogue: 5,{_fmt_time(recap_start)},{_fmt_time(recap_end)},SetRecap,,0,0,0,,"
            f"{{\\an5\\pos({cx},{recap_score_y})\\fad(300,400)}}{score_text}"
        )

        # --- Transition card: huge "SET N+1" centred ---
        if trans_end > trans_start:
            lines.append(
                f"Dialogue: 5,{_fmt_time(trans_start)},{_fmt_time(trans_end)},SetTransition,,0,0,0,,"
                f"{{\\an5\\pos({cx},{cy})\\fad(300,300)"
                f"\\fscx80\\fscy80\\t(0,400,\\fscx100\\fscy100)}}SET {next_set_n}"
            )

    # GAME POINT / DEUCE FLAG
    # Pulses above the scoreboard for the duration of the qualifying state.
    # Conditions evaluated on the event that introduced the state:
    #   DEUCE     : both >= 10 and equal
    #   GAME POINT: leader >= 10, lead >= 1
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
        start = max(0.0, ev.timestamp)
        end = events[i + 1].timestamp if i + 1 < len(events) else end_ts
        end = min(end, scoreboard_end_t)
        if end <= start:
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
            # If winning this set also wins the match, upgrade to MATCH POINT.
            leader_sets = ev.p1_set if leader == 1 else ev.p2_set
            if leader_sets + 1 >= sets_to_win:
                style = "MatchPoint"
                text = "MATCH POINT"
            else:
                style = "GamePoint"
                text = "GAME POINT"

        pulse = _build_pulse(end - start)
        # \an3 = bottom-right anchor: text extends leftward from flag_x so
        # it never overruns the screen edge.
        lines.append(
            f"Dialogue: 4,{_fmt_time(start)},{_fmt_time(end)},{style},,0,0,0,,"
            f"{{\\an3\\pos({flag_x},{flag_y}){pulse}}}{text}"
        )

    # ------------------------------------------------------------------ final scoreboard
    # Anchored at the same bottom-right corner as the live panel, with
    # IDENTICAL fonts / colours / opacities — just one extra column per
    # played set on the right. Replaces the live scoreboard from match-
    # end to video-end.
    if match_end_t is not None and set_history:
        last_ev = events[-1]
        final_p1_sets = last_ev.p1_set
        final_p2_sets = last_ev.p2_set
        n_sets = len(set_history)

        # Reuse every constant from the live panel. The only new column
        # widths are derived from those: total sets uses the same width
        # as the live SETS column, per-set scores use the live PTS width.
        # The team column (when present) sits to the LEFT of the name
        # column and shifts every downstream origin by TEAM_COL.
        F_TOTAL_COL = SETS_COL
        F_SET_COL   = PTS_COL
        F_BAR_W     = PAD_X + TEAM_COL + NAME_COL + F_TOTAL_COL + n_sets * F_SET_COL
        F_total_h   = HEADER_H + ROW_H * 2 + GAP_ROWS

        F_x1 = video_w - F_BAR_W - MARGIN
        F_y1 = video_h - F_total_h - MARGIN
        F_x2 = F_x1 + F_BAR_W
        F_hdr_y2 = F_y1 + HEADER_H
        F_mid_y  = F_hdr_y2 + ROW_H
        F_row1_cy = F_hdr_y2 + ROW_H // 2
        F_row2_cy = F_mid_y + GAP_ROWS + ROW_H // 2

        F_team_x   = F_x1 + PAD_X
        F_name_col_x = F_team_x + TEAM_COL
        F_team_text_x = F_team_x + ACCENT_W + 6
        F_name_text_x = (F_name_col_x + 6) if has_team else (F_team_x + ACCENT_W + 6)
        F_total_x  = F_name_col_x + NAME_COL
        F_total_cx = F_total_x + F_TOTAL_COL // 2
        F_set_cxs  = [
            F_total_x + F_TOTAL_COL + i * F_SET_COL + F_SET_COL // 2
            for i in range(n_sets)
        ]
        # Column dividers: [team|]name|total, total|s1, s1|s2, ..., s(n-1)|sn.
        # Right edge of the panel itself closes the last column.
        F_div_xs = ([F_name_col_x] if has_team else []) + [F_total_x] + [
            F_total_x + F_TOTAL_COL + i * F_SET_COL for i in range(n_sets)
        ]

        F_start = match_end_t
        F_end   = end_ts
        fade = "\\fad(500,300)"

        def fbox(x: int, y: int, w: int, h: int, color: str, alpha: str = "00", layer: int = 6) -> str:
            # Compose `_rect`'s payload with a Dialogue wrapper + a \fad
            # tag injected after the opening brace of the tag block.
            payload = "{" + fade + _rect(x, y, w, h, color, alpha_hex=alpha)[1:]
            return (
                f"Dialogue: {layer},{_fmt_time(F_start)},{_fmt_time(F_end)},Box,,0,0,0,,"
                f"{payload}"
            )

        # Header strip + player rows + accent bars + dividers — same
        # idiom as the live panel above.
        if HEADER_H > 0:
            lines.append(fbox(F_x1, F_y1, F_BAR_W, HEADER_H, C_BG_HEADER, alpha="08"))
            lines.append(fbox(F_x1, F_y1, F_BAR_W, GOLD_LINE, C_GOLD))
            lines.append(fbox(F_x1, F_hdr_y2 - SEP_COL, F_BAR_W, SEP_COL, C_GOLD, alpha="40"))
            lines.append(fbox(F_x1, F_y1 + GOLD_LINE, ACCENT_W, HEADER_H - GOLD_LINE, C_ACCENT_HDR))

        lines.append(fbox(F_x1, F_hdr_y2, F_BAR_W, ROW_H * 2 + GAP_ROWS, C_BG_ROWS, alpha="0C"))

        # Team column tint — steel-blue panel matching the live board.
        if has_team:
            lines.append(fbox(F_team_x, F_hdr_y2, TEAM_COL, ROW_H * 2 + GAP_ROWS, C_BG_TEAM, alpha="0C"))

        # Tint just the totals column to mirror the live SETS-column
        # highlight; per-set columns stay on the plain row bg.
        lines.append(fbox(F_total_x, F_hdr_y2, F_TOTAL_COL, ROW_H * 2 + GAP_ROWS, C_BG_SETS, alpha="0C"))

        lines.append(fbox(F_x1, F_hdr_y2,           ACCENT_W, ROW_H, C_ACCENT_P1))
        lines.append(fbox(F_x1, F_mid_y + GAP_ROWS, ACCENT_W, ROW_H, C_ACCENT_P2))
        lines.append(fbox(F_x1, F_mid_y, F_BAR_W, SEP_MID, C_SEP))

        F_div_y_top = F_hdr_y2 + 6
        F_div_h     = ROW_H * 2 + GAP_ROWS - 12
        for dx in F_div_xs:
            lines.append(fbox(dx, F_div_y_top, SEP_COL, F_div_h, C_SEP))

        if tournament.strip():
            tag = _ass_escape(_trim_title(tournament))
            clip = f"\\clip({F_x1},{F_y1 + GOLD_LINE},{F_x2 - PAD_X // 2},{F_hdr_y2})"
            lines.append(
                f"Dialogue: 7,{_fmt_time(F_start)},{_fmt_time(F_end)},Header,,0,0,0,,"
                f"{{\\an4\\pos({F_x1 + PAD_X},{F_y1 + HEADER_H // 2 + GOLD_LINE // 2})"
                f"\\q2{clip}{fade}}}{tag}"
            )

        if has_team:
            for cy_row, txt in ((F_row1_cy, p1_team_safe), (F_row2_cy, p2_team_safe)):
                if not txt:
                    continue
                lines.append(
                    f"Dialogue: 7,{_fmt_time(F_start)},{_fmt_time(F_end)},Name,,0,0,0,,"
                    f"{{\\an4\\pos({F_team_text_x},{cy_row})\\q2{fade}\\b0}}{txt}"
                )

        for cy_row, name in ((F_row1_cy, p1_safe), (F_row2_cy, p2_safe)):
            lines.append(
                f"Dialogue: 7,{_fmt_time(F_start)},{_fmt_time(F_end)},Name,,0,0,0,,"
                f"{{\\an4\\pos({F_name_text_x},{cy_row})\\q2{fade}}}{name}"
            )

        for cy_row, total in ((F_row1_cy, final_p1_sets), (F_row2_cy, final_p2_sets)):
            lines.append(
                f"Dialogue: 7,{_fmt_time(F_start)},{_fmt_time(F_end)},SetNum,,0,0,0,,"
                f"{{\\an5\\pos({F_total_cx},{cy_row}){fade}}}{total}"
            )

        # Per-set point columns — winner of each set highlighted gold.
        for i, (p1_s, p2_s, w) in enumerate(set_history):
            cx_set = F_set_cxs[i]
            p1_c = C_GOLD_BRIGHT if w == 1 else C_WHITE
            p2_c = C_GOLD_BRIGHT if w == 2 else C_WHITE
            lines.append(
                f"Dialogue: 7,{_fmt_time(F_start)},{_fmt_time(F_end)},PtsNum,,0,0,0,,"
                f"{{\\an5\\pos({cx_set},{F_row1_cy}){fade}\\c{p1_c}}}{p1_s}"
            )
            lines.append(
                f"Dialogue: 7,{_fmt_time(F_start)},{_fmt_time(F_end)},PtsNum,,0,0,0,,"
                f"{{\\an5\\pos({cx_set},{F_row2_cy}){fade}\\c{p2_c}}}{p2_s}"
            )

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


# ---------------------------------------------------------------------------
# Mode badges (top-left corner)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Highlight → Main transition (0.8 s bridge)
# ---------------------------------------------------------------------------

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

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Box, Arial, 1, &H00FFFFFF, &H000000FF, &H00000000, &H80000000, 0, 0, 0, 0, 100, 100, 0, 0, 1, 0, 0, 7, 0, 0, 0, 1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines: list[str] = [header]

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


# ---------------------------------------------------------------------------
# Intro card (3-second title)
# ---------------------------------------------------------------------------

def build_intro_ass(
    *,
    output_path: Path,
    video_w: int,
    video_h: int,
    duration: float,
    tournament: str,
    p1_name: str,
    p2_name: str,
) -> Path:
    """
    libass-driven intro card with a vertical broadcast layout:

        ┌───────────────────────────────────────────┐
        │       TOURNAMENT NAME (fade in)           │
        │       ─────── (line wipes outwards) ───── │
        │                                           │
        │       PLAYER A   (drops down)             │
        │            vs    (fades + scales)         │
        │       PLAYER B   (rises up)               │
        │                                           │
        │       ─────── (line wipes outwards) ───── │
        └───────────────────────────────────────────┘

    libass + DirectWrite handles Vietnamese diacritics natively, so the
    drawtext-based intro's font corruption is gone.
    """
    scale = max(0.6, video_h / 1080.0)

    fs_title = max(40, int(60  * scale))
    fs_vs    = max(28, int(46  * scale))   # smaller — secondary to the names
    fs_name  = max(48, int(78  * scale))   # the dominant element

    cx = video_w // 2
    cy = video_h // 2

    title_y      = int(cy - 280 * scale)
    top_line_y   = int(cy - 200 * scale)
    p1_y         = int(cy - 70  * scale)
    vs_y         = int(cy)
    p2_y         = int(cy + 80  * scale)
    bot_line_y   = int(cy + 200 * scale)

    line_w       = int(900 * scale)
    line_h       = max(3, int(4 * scale))

    # P1 starts off-screen above its target and drops down.
    p1_drop_from_y = p1_y - int(80 * scale)
    # P2 starts off-screen below and rises up.
    p2_rise_from_y = p2_y + int(80 * scale)

    end_time   = _fmt_time(duration)
    title_safe = _ass_escape(_trim_title(tournament)) if tournament.strip() else ""
    p1_safe    = _ass_escape(p1_name.strip() or "Player 1")
    p2_safe    = _ass_escape(p2_name.strip() or "Player 2")

    gold_bgr = _bgr(C_GOLD)
    vs_dim   = _ass_rgb(120, 120, 120)  # mid-grey "vs" — quiet vs. the names

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title, Arial, {fs_title}, {C_GOLD},  &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 3, 0, 1, 2, 1, 5, 0, 0, 0, 1
Style: VS,    Arial, {fs_vs},    {vs_dim},  &H000000FF, &H00000000, &H80000000,  0, 1, 0, 0, 100, 100, 4, 0, 1, 2, 0, 5, 0, 0, 0, 1
Style: Name,  Arial, {fs_name},  {C_WHITE}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 2, 0, 1, 2, 2, 5, 0, 0, 0, 1
Style: Box,   Arial, 1,           {C_WHITE}, &H000000FF, &H00000000, &H80000000,  0, 0, 0, 0, 100, 100, 0, 0, 1, 0, 0, 7, 0, 0, 0, 1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines: list[str] = [header]

    def _line_dialogue(start_ms: int, fade_in: int, fade_out: int, y: int) -> str:
        """Gold accent line. Anchored top-left at (cx - line_w/2, y) with
        positive drawing coords so libass anchors it correctly. We animate
        \\fscx 0 → 100 to wipe outward from centre."""
        x_left = cx - line_w // 2
        return (
            f"Dialogue: 0,{_fmt_time(start_ms / 1000.0)},{end_time},Box,,0,0,0,,"
            f"{{\\an7\\pos({x_left + line_w // 2},{y})\\org({x_left + line_w // 2},{y})"
            f"\\fad({fade_in},{fade_out})\\bord0\\shad0"
            f"\\1c&H{gold_bgr}&\\1a&H00&\\fscx0\\t(0,{600},\\fscx100)\\p1}}"
            f"m {-line_w // 2} 0 l {line_w // 2} 0 l {line_w // 2} {line_h} l {-line_w // 2} {line_h}"
            f"{{\\p0}}"
        )

    # Tournament title — first thing on screen.
    if title_safe:
        lines.append(
            f"Dialogue: 1,0:00:00.00,{end_time},Title,,0,0,0,,"
            f"{{\\an5\\pos({cx},{title_y})\\fad(400,500)}}{title_safe}"
        )

    # Top accent line — wipes outward from centre.
    lines.append(_line_dialogue(start_ms=200, fade_in=300, fade_out=500, y=top_line_y))

    # P1 name — drops in from above, fades in, locks into target.
    lines.append(
        f"Dialogue: 1,0:00:00.40,{end_time},Name,,0,0,0,,"
        f"{{\\an5\\fad(400,500)"
        f"\\move({cx},{p1_drop_from_y},{cx},{p1_y},0,500)}}{p1_safe}"
    )

    # "vs" — fades + scales in between the names.
    lines.append(
        f"Dialogue: 1,0:00:00.70,{end_time},VS,,0,0,0,,"
        f"{{\\an5\\pos({cx},{vs_y})\\fad(300,500)"
        f"\\fscx70\\fscy70\\t(0,400,\\fscx100\\fscy100)}}vs"
    )

    # P2 name — rises in from below.
    lines.append(
        f"Dialogue: 1,0:00:00.60,{end_time},Name,,0,0,0,,"
        f"{{\\an5\\fad(400,500)"
        f"\\move({cx},{p2_rise_from_y},{cx},{p2_y},0,500)}}{p2_safe}"
    )

    # Bottom accent line — wipes outward from centre after the names.
    lines.append(_line_dialogue(start_ms=900, fade_in=300, fade_out=500, y=bot_line_y))

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


# ---------------------------------------------------------------------------
# Cinematic intro (avatar slide-in companion)
# ---------------------------------------------------------------------------

def build_cinematic_intro_ass(
    *,
    output_path: Path,
    video_w: int,
    video_h: int,
    duration: float,
    tournament: str,
    p1_name: str,
    p2_name: str,
    avatar_size_px: int,
    p1_team: str = "",
    p2_team: str = "",
) -> Path:
    """
    Text overlays for the cinematic intro. Layout assumes the caller's
    ffmpeg filter graph is rendering two avatars at x = W*0.27 and W*0.73,
    centred vertically with a +80 px y-offset (matches `intro_builder.py`).

        ┌────────────────────────────────────────────┐
        │                                            │
        │            TOURNAMENT NAME                 │ ← top
        │                                            │
        │     ⬤  Avatar1   VS   Avatar2 ⬤           │
        │                                            │
        │     P1 NAME              P2 NAME           │
        │                                            │
        └────────────────────────────────────────────┘

    Timing — chosen to cooperate with the slide-in (settles at t=1.0):
      0.0s  Tournament fades in
      0.7s  Player names fade in below the still-moving avatars
      1.1s  "VS" fades in + scales 80 → 100% over 0.4s
      DUR-0.5  Everything fades out
    """
    scale = max(0.6, video_h / 1080.0)
    fs_tournament = max(36, int(64 * scale))
    fs_name       = max(28, int(52 * scale))
    fs_vs         = max(48, int(96 * scale))

    cx = video_w // 2
    # Avatar centre Y = (H-h)/2 + 80 + h/2 = H/2 + 80. The text rows sit
    # above and below that.
    avatar_cy = video_h // 2 + 80
    avatar_half = avatar_size_px // 2

    tournament_y = max(int(80 * scale), avatar_cy - avatar_half - int(70 * scale))
    name_y       = avatar_cy + avatar_half + int(50 * scale)
    p1_cx        = int(video_w * 0.27)
    p2_cx        = int(video_w * 0.73)
    vs_cy        = avatar_cy

    end_time = _fmt_time(duration)
    out_fade_start_ms = int(max(0.0, duration - 0.5) * 1000)

    title_safe = _ass_escape(_trim_title(tournament)) if tournament.strip() else ""
    p1_safe    = _ass_escape(p1_name.strip() or "Player 1")
    p2_safe    = _ass_escape(p2_name.strip() or "Player 2")

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Tournament, Arial, {fs_tournament}, {C_GOLD_BRIGHT}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 4, 0, 1, 3, 2, 5, 0, 0, 0, 1
Style: Name,       Arial, {fs_name},       {C_WHITE},       &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 2, 0, 1, 2, 2, 5, 0, 0, 0, 1
Style: VS,         Arial, {fs_vs},         {C_GOLD_BRIGHT}, &H000000FF, &H00000000, &H80000000, -1, 1, 0, 0, 100, 100, 6, 0, 1, 4, 3, 5, 0, 0, 0, 1
Style: Box,        Arial, 1,               {C_WHITE},       &H000000FF, &H00000000, &H80000000,  0, 0, 0, 0, 100, 100, 0, 0, 1, 0, 0, 7, 0, 0, 0, 1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines: list[str] = [header]

    if title_safe:
        lines.append(
            f"Dialogue: 1,0:00:00.00,{end_time},Tournament,,0,0,0,,"
            f"{{\\an5\\pos({cx},{tournament_y})\\fad(500,500)}}{title_safe}"
        )

        # Gold underline that wipes in below the tournament name at t=2 s.
        # This is the deliberate "beat" that breaks up the otherwise
        # static hold from t≈1.5 s onward.
        gold_bgr = _bgr(C_GOLD)
        line_w = max(280, int(520 * scale))
        line_h = max(2, int(3 * scale))
        line_y = tournament_y + int(48 * scale)
        lines.append(
            f"Dialogue: 0,0:00:02.00,{end_time},Box,,0,0,0,,"
            f"{{\\an5\\pos({cx},{line_y})\\fad(200,500)"
            f"\\fscx0\\t(0,600,\\fscx100)\\bord0\\shad0"
            f"\\1c&H{gold_bgr}&\\1a&H00&\\p1}}"
            f"m {-line_w // 2} 0 l {line_w // 2} 0 "
            f"l {line_w // 2} {line_h} l {-line_w // 2} {line_h}{{\\p0}}"
        )

    # Player names — fade in @ 0.7 s while avatars are still sliding,
    # locking in by the time avatars settle at t=1.0 s. Fade-out spans
    # the last ~3 s (capped at half the intro) so the names ease out
    # slowly instead of snapping off — gives the wind-down a graceful
    # feel rather than a hard cut.
    name_fadeout_ms = max(800, min(3000, int(duration * 500)))
    lines.append(
        f"Dialogue: 1,0:00:00.70,{end_time},Name,,0,0,0,,"
        f"{{\\an5\\pos({p1_cx},{name_y})\\fad(400,{name_fadeout_ms})}}{p1_safe}"
    )
    lines.append(
        f"Dialogue: 1,0:00:00.70,{end_time},Name,,0,0,0,,"
        f"{{\\an5\\pos({p2_cx},{name_y})\\fad(400,{name_fadeout_ms})}}{p2_safe}"
    )

    # Team labels — only when BOTH players have a team affiliation set.
    # Renders gold-italic + non-bold just above each player name so the
    # team reads as elegant context, subordinate to the player. Skipped
    # entirely for singles to keep the intro uncluttered.
    p1_team_safe = _trim_team(p1_team, max_len=20)
    p2_team_safe = _trim_team(p2_team, max_len=20)
    if p1_team_safe and p2_team_safe:
        fs_team = max(22, int(36 * scale))
        team_y = name_y - int(38 * scale)
        for cx_team, txt in (
            (p1_cx, _ass_escape(p1_team_safe)),
            (p2_cx, _ass_escape(p2_team_safe)),
        ):
            lines.append(
                f"Dialogue: 1,0:00:00.70,{end_time},Name,,0,0,0,,"
                f"{{\\an5\\pos({cx_team},{team_y})\\fad(400,{name_fadeout_ms})"
                f"\\fs{fs_team}\\c{C_GOLD_BRIGHT}\\i1\\b0}}{txt}"
            )

    # "VS" centred between avatars — fades in just after both avatars
    # settle, scales 80→100 % to pop into place, then gentle 100→105→100
    # pulses every 2 s through the hold phase so the centre of the card
    # never feels frozen.
    vs_dialog_start = 1.1
    pulse_chain = ""
    pulse_t = 3.0
    while pulse_t < duration - 0.8:
        offset_ms = int((pulse_t - vs_dialog_start) * 1000)
        pulse_chain += (
            f"\\t({offset_ms},{offset_ms + 250},\\fscx105\\fscy105)"
            f"\\t({offset_ms + 250},{offset_ms + 550},\\fscx100\\fscy100)"
        )
        pulse_t += 2.0
    lines.append(
        f"Dialogue: 2,0:00:01.10,{end_time},VS,,0,0,0,,"
        f"{{\\an5\\pos({cx},{vs_cy})\\fad(300,500)"
        f"\\fscx80\\fscy80\\t(0,400,\\fscx100\\fscy100){pulse_chain}}}vs"
    )

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path
