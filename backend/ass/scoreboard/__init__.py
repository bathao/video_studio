"""
Live scoreboard + inter-set recap panels (same layout, sliced history)
+ end-of-match summary + GAME POINT / MATCH POINT / DEUCE flag — all
generated as a single ASS file that ffmpeg burns over the main render.

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

Module layout under backend/ass/scoreboard/:
  __init__.py    — re-exports public API
  geometry.py    — `_Geometry`, `_AssetText` dataclasses,
                   `_compute_geometry`, `_scoreboard_header` style block,
                   `_title_fscx_tag`
  events.py      — `ScoreFrame` dataclass, `_walk_events`,
                   `_set_final_score`, `resolve_row_names` (doubles rule)
  emit_live.py   — `_emit_live_panel`, `_emit_dynamic_numbers`
  emit_cards.py  — `_emit_recap_cards`, `_emit_flag_overlays`
  emit_final.py  — `_emit_scoreboard_panel` (shared layout) +
                   `_emit_final_scoreboard` (end-of-match wrapper)
  builder.py     — `build_scoreboard_ass_text` + `build_scoreboard_ass`
"""

from .builder import build_scoreboard_ass, build_scoreboard_ass_text
from .events import ScoreFrame, _set_final_score, _walk_events, resolve_row_names

__all__ = [
    "ScoreFrame",
    "resolve_row_names",
    "build_scoreboard_ass",
    "build_scoreboard_ass_text",
    # Re-exported for the pure-logic test suite:
    "_set_final_score",
    "_walk_events",
]
