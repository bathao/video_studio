"""ScoreFrame dataclass + event-walking helpers + doubles row-name rule.

Kept tier-agnostic: this module imports nothing from the emit_* modules,
so any consumer (renderer, server preview endpoint, tests) can pull
just the public types/helpers without dragging the layout code in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..common import _combine_doubles_name


@dataclass
class ScoreFrame:
    timestamp: float
    p1_score: int
    p2_score: int
    p1_set: int
    p2_set: int


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


def _set_final_score(prev_p1: int, prev_p2: int, won_by: int) -> tuple[int, int]:
    """
    Recover the final score of a set from the event immediately BEFORE
    the winning point. Just adds 1 to whoever won — no rule enforcement.
    What's in the events is what gets shown.
    """
    if won_by == 1:
        return (prev_p1 + 1, prev_p2)
    return (prev_p1, prev_p2 + 1)


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
