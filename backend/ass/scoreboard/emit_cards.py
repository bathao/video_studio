"""Recap / transition cards + GP / MP / DEUCE flag overlay.

Two transient-overlay emitters that ride on top of the live panel:
  - `_emit_recap_cards`: after each completed (non-match-ending) set,
    fade in a "SET N" recap card showing the final score, then a
    "SET N+1" transition card before the next set's first point.
  - `_emit_flag_overlays`: anchored above the panel, pulsing flag for
    DEUCE / GAME POINT / MATCH POINT while the state holds.
"""

from __future__ import annotations

from ..common import C_GOLD_BRIGHT, C_GREY, C_WHITE, _fmt_time
from .events import ScoreFrame, _set_final_score
from .geometry import _Geometry


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
