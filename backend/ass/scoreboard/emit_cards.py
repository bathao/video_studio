"""Inter-set recap panel + GP / MP / DEUCE flag overlay.

Two transient overlays riding on top of the live panel:
  - `_emit_recap_cards`: after each completed non-match-ending set,
    expand the bottom-right scoreboard panel with one column per set
    played so far. Same layout as the end-of-match summary — visually
    reads as "live panel grows" rather than a centred popup. Delegates
    to `_emit_scoreboard_panel` in `emit_final.py`.
  - `_emit_flag_overlays`: anchored just above the panel, pulsing flag
    for DEUCE / GAME POINT / MATCH POINT while the state holds.
"""

from __future__ import annotations

from ..common import _fmt_time
from .emit_final import _emit_scoreboard_panel
from .events import ScoreFrame, _set_final_score
from .geometry import _AssetText, _Geometry


def _emit_recap_cards(lines: list[str], g: _Geometry, t: _AssetText,
                      events: list[ScoreFrame],
                      end_ts: float, sets_to_win: int) -> None:
    """After each non-match-ending set, expand the bottom-right
    scoreboard panel with one column per set played so far. The
    match-ending set falls through to `_emit_final_scoreboard`, which
    renders the same panel with the full history at the match-end
    timestamp.

    Recap window starts at the winning-point event (which already
    carries the post-reset state: set += 1, score = 0–0) and runs for
    `RECAP_DUR` seconds, clipped to `NEXT_EVENT_GUARD` before the next
    score event so a fast-served next point isn't visually swallowed."""
    RECAP_DUR = 4.0
    NEXT_EVENT_GUARD = 0.3

    running_history: list[tuple[int, int, int]] = []
    for i in range(1, len(events)):
        prev = events[i - 1]
        cur = events[i]
        if cur.p1_set > prev.p1_set:
            won_by = 1
        elif cur.p2_set > prev.p2_set:
            won_by = 2
        else:
            continue
        p1_final, p2_final = _set_final_score(prev.p1_score, prev.p2_score, won_by)
        running_history.append((p1_final, p2_final, won_by))

        # Match-ending set: fall through. _emit_final_scoreboard owns
        # this window with a longer fade-in and runs to the credits.
        if max(cur.p1_set, cur.p2_set) >= sets_to_win:
            continue

        start_t = max(0.0, cur.timestamp)
        end_t = start_t + RECAP_DUR
        if i + 1 < len(events):
            end_t = min(end_t, events[i + 1].timestamp - NEXT_EVENT_GUARD)
        end_t = min(end_t, end_ts)
        if end_t <= start_t:
            continue

        _emit_scoreboard_panel(
            lines, g, t,
            running_history,
            cur.p1_set, cur.p2_set,
            start_t, end_t,
            fade_in_ms=350, fade_out_ms=300,
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
