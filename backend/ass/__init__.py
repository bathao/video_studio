"""
ASS overlay builders.

Each submodule owns one kind of card / overlay:

  - `scoreboard` — live scoreboard + recap/transition cards + GP/MP flag
                   + end-of-match summary
  - `intro`      — text-only title card and the libass companion for the
                   ffmpeg-driven cinematic intro
  - `badges`     — top-left HIGHLIGHT and FULL MATCH badges
  - `transition` — sweep-line bridge between highlight reel and main match
  - `common`     — palette, drawing primitives, and shared helpers
                   (everything imported by the four builders above)

Public symbols are re-exported from this package so call sites can do
``from .ass import build_scoreboard_ass`` without picking which file
each builder lives in.
"""

from .badges import build_full_match_badge_ass, build_highlight_badge_ass
from .intro import build_cinematic_intro_ass, build_intro_ass
from .scoreboard import ScoreFrame, build_scoreboard_ass, build_scoreboard_ass_text
from .transition import build_transition_ass

__all__ = [
    "ScoreFrame",
    "build_cinematic_intro_ass",
    "build_full_match_badge_ass",
    "build_highlight_badge_ass",
    "build_intro_ass",
    "build_scoreboard_ass",
    "build_scoreboard_ass_text",
    "build_transition_ass",
]
