"""
ASS overlay builders.

Each submodule owns one kind of card / overlay:

  - `scoreboard` — live scoreboard + recap/transition cards + GP/MP flag
                   + end-of-match summary
  - `intro`      — text-only title card and the libass companion for the
                   ffmpeg-driven cinematic intro
  - `outro`      — closing card over a blurred freeze-frame of main's
                   last frame
  - `stinger`    — channel-name + REPLAY label + soft glow burned over
                   the auto-stinger transition clip
  - `badges`     — top-left SLOW MOTION badge
  - `common`     — palette, drawing primitives, and shared helpers
                   (everything imported by the builders above)

Public symbols are re-exported from this package so call sites can do
``from .ass import build_scoreboard_ass`` without picking which file
each builder lives in.
"""

from .badges import build_slow_motion_badge_ass
from .intro import build_cinematic_intro_ass, build_intro_ass
from .outro import build_outro_card_ass
from .scoreboard import ScoreFrame, build_scoreboard_ass, build_scoreboard_ass_text
from .stinger import build_stinger_ass

__all__ = [
    "ScoreFrame",
    "build_cinematic_intro_ass",
    "build_intro_ass",
    "build_outro_card_ass",
    "build_scoreboard_ass",
    "build_scoreboard_ass_text",
    "build_slow_motion_badge_ass",
    "build_stinger_ass",
]
