"""Tests for the score-event-walking logic that sits between the score
events array and the rendered scoreboard. Specifically:

  - `_set_final_score`: recover the final per-set score from the event
    BEFORE the winning point.
  - `_walk_events`: derive the per-set history + match-end timestamp.

Both are pure functions; getting them wrong silently mistimes the
recap cards and final scoreboard."""

from backend.ass.scoreboard import (
    ScoreFrame,
    _set_final_score,
    _walk_events,
)


# ---------- _set_final_score -----------------------------------------------


def test_final_score_when_p1_won():
    # Pre-winning event: P1 at 10, P2 at 7. P1 scores → ends 11–7.
    assert _set_final_score(10, 7, won_by=1) == (11, 7)


def test_final_score_when_p2_won():
    assert _set_final_score(10, 11, won_by=2) == (10, 12)


def test_final_score_at_deuce_extreme():
    # Deuce can drag past 11; no rule enforcement.
    assert _set_final_score(15, 14, won_by=1) == (16, 14)


# ---------- _walk_events ----------------------------------------------------


def _ev(t: float, p1: int, p2: int, s1: int = 0, s2: int = 0) -> ScoreFrame:
    return ScoreFrame(timestamp=t, p1_score=p1, p2_score=p2, p1_set=s1, p2_set=s2)


def test_walk_events_no_sets_won():
    events = [_ev(0, 0, 0), _ev(5, 1, 0), _ev(10, 1, 1)]
    history, match_end = _walk_events(events, sets_to_win=3)
    assert history == []
    assert match_end is None


def test_walk_events_one_set_won_by_p1():
    # IMPORTANT: the live scorer stores the winning point's event with
    # score 0/0 + set++ (recomputeAllEvents resets on rule trigger).
    # So the event BEFORE the set-transition has the pre-winning score
    # (10/7), not 11/7. _set_final_score then adds 1 to recover 11/7.
    events = [
        _ev(0, 0, 0, 0, 0),
        _ev(10, 10, 7, 0, 0),     # pre-winning state
        _ev(15, 0, 0, 1, 0),       # set 1 won by P1 (winning point + reset)
    ]
    history, match_end = _walk_events(events, sets_to_win=3)
    assert history == [(11, 7, 1)]
    assert match_end is None  # match still in progress (need 3 sets)


def test_walk_events_match_end_detected_at_correct_event():
    events = [
        _ev(0, 0, 0, 0, 0),
        _ev(10, 10, 7, 0, 0),
        _ev(20, 0, 0, 1, 0),       # set 1 → P1
        _ev(30, 10, 8, 1, 0),
        _ev(40, 0, 0, 2, 0),       # set 2 → P1
        _ev(50, 10, 9, 2, 0),
        _ev(60, 0, 0, 3, 0),       # set 3 → P1, match ends here at t=60
    ]
    history, match_end = _walk_events(events, sets_to_win=3)
    assert len(history) == 3
    assert all(won_by == 1 for _, _, won_by in history)
    assert match_end == 60.0


def test_walk_events_alternating_set_winners():
    events = [
        _ev(0, 0, 0, 0, 0),
        _ev(10, 10, 7, 0, 0),
        _ev(20, 0, 0, 1, 0),       # set 1 → P1
        _ev(30, 9, 10, 1, 0),
        _ev(40, 0, 0, 1, 1),       # set 2 → P2
    ]
    history, match_end = _walk_events(events, sets_to_win=3)
    assert history == [(11, 7, 1), (9, 11, 2)]
    assert match_end is None


def test_walk_events_match_end_in_best_of_5():
    # First to 3 sets wins (BO5).
    events = [
        _ev(0, 0, 0, 0, 0),
        _ev(10, 10, 9, 0, 0),
        _ev(20, 0, 0, 1, 0),
        _ev(30, 8, 10, 1, 0),
        _ev(40, 0, 0, 1, 1),
        _ev(50, 10, 7, 1, 1),
        _ev(60, 0, 0, 2, 1),
        _ev(70, 10, 5, 2, 1),
        _ev(80, 0, 0, 3, 1),       # P1 reaches 3, match ends here.
    ]
    _, match_end = _walk_events(events, sets_to_win=3)
    assert match_end == 80.0


def test_walk_events_detects_first_match_end_only():
    # If somehow more events follow after match end (shouldn't normally),
    # match_end stays at the first crossing.
    events = [
        _ev(0, 0, 0, 0, 0),
        _ev(10, 10, 7, 0, 0),
        _ev(20, 0, 0, 1, 0),
        _ev(30, 10, 8, 1, 0),
        _ev(40, 0, 0, 2, 0),
        _ev(50, 10, 9, 2, 0),
        _ev(60, 0, 0, 3, 0),       # FIRST match end
        _ev(70, 1, 0, 3, 0),       # weird trailing event
    ]
    _, match_end = _walk_events(events, sets_to_win=3)
    assert match_end == 60.0


def test_walk_events_handles_empty_list():
    history, match_end = _walk_events([], sets_to_win=3)
    assert history == []
    assert match_end is None


def test_walk_events_handles_single_event():
    history, match_end = _walk_events([_ev(0, 0, 0)], sets_to_win=3)
    assert history == []
    assert match_end is None
