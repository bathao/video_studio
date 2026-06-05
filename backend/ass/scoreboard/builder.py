"""Public scoreboard builders.

`build_scoreboard_ass_text` is the single source of truth — both the
render pipeline (writes the result to disk for ffmpeg's `ass=` filter)
and the live preview endpoint (streams the text straight to JASSUB)
call this function. That sharing guarantees the in-browser preview
overlay is byte-identical to the burned-in scoreboard.

`build_scoreboard_ass` is a thin file-write wrapper for the renderer's
on-disk needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ..common import _ass_escape, _trim_name, _trim_team, _trim_title
from .emit_cards import _emit_flag_overlays, _emit_recap_cards
from .emit_final import _emit_final_scoreboard
from .emit_live import _emit_dynamic_numbers, _emit_live_panel
from .events import ScoreFrame, _walk_events, resolve_row_names
from .geometry import _AssetText, _compute_geometry, _scoreboard_header


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
    """Build the scoreboard ASS content as a string. Five sections:

      1. Static live panel (header, rows, accents, dividers, names)
      2. Per-event live numbers (sets / pts updates)
      3. Inter-set recap panels — the bottom-right scoreboard expanded
         with one column per set played so far, ~4 s per set
      4. GAME POINT / MATCH POINT / DEUCE flag pulses
      5. End-of-match final scoreboard with per-set columns (same
         panel layout as the recap, called with the full history at
         the match-end timestamp)

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
        g.fs_header, g.fs_name, g.fs_sets, g.fs_pts, g.fs_gp,
    )]

    _emit_live_panel(lines, g, text, scoreboard_end_t)
    _emit_dynamic_numbers(lines, g, events, scoreboard_end_t, end_ts)
    _emit_recap_cards(lines, g, text, events, end_ts, sets_to_win)
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
