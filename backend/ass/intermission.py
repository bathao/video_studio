"""
Typography intermission card — 3-second bridge between highlight reel
and main match. Built entirely as a libass overlay so it composites at
NVENC speed and stays sharp at any resolution.

Layout (anchored at frame centre):

    ┌───────────────────────────────────────────┐
    │                                            │
    │         [TOURNAMENT NAME]  (fade in)       │
    │                                            │
    │           ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓                   │
    │           FULL  MATCH       (fade + zoom)  │
    │           ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓                   │
    │           — gold line —     (wipes out)    │
    │                                            │
    │           Player 1 vs Player 2             │
    │                                            │
    └───────────────────────────────────────────┘

Animation rhythm (300 / 300 / 3000 timing budget, per-element delays
keep the eye moving for the full 3 s):

  0.00 s  background fades up (handled by the renderer's filter graph)
  0.20 s  tournament fades in (400 ms fade)
  0.30 s  headline fades in (300 ms fade) + starts a slow 1.0 → 1.1 zoom
  0.50 s  gold accent line wipes outward from centre
  0.60 s  player names fade in (400 ms fade)
  2.70 s  everything fades out (300 ms)

Font: Arial Black on Windows ships with the OS; libass + DirectWrite
falls back to the closest available bold if it's missing. Avoids
bundling a font file just for one 3-second card.
"""

from __future__ import annotations

from pathlib import Path

from .common import (
    C_GOLD, C_GOLD_BRIGHT, C_WHITE,
    _ass_escape, _ass_skeleton, _bgr, _combine_doubles_name, _fmt_time,
    _trim_title,
)


def build_intermission_card_ass(
    *,
    output_path: Path,
    video_w: int,
    video_h: int,
    duration: float,
    headline: str,
    tournament: str,
    p1_label: str,
    p2_label: str,
    p1_team: str = "",
    p2_team: str = "",
) -> Path:
    """Write the intermission card .ass. `headline` is the big centre
    text (e.g. 'FULL MATCH'); `p1_label` / `p2_label` are already
    resolved row labels (singles → p1/p2 names, doubles → combined
    'A + B' / 'C + D' pairs — caller does the resolution so this
    builder stays format-agnostic).

    `p1_team` / `p2_team` produce a small "Team A vs Team B" line above
    the players when both are non-empty (same has_team gating the
    scoreboard uses). Empty in singles → line is skipped entirely.

    The literal "vs" separator on both lines is coloured gold inline so
    Team A / Team B (and the corresponding player pair) read as the two
    competing sides at a glance.
    """
    scale = max(0.6, video_h / 1080.0)
    cx = video_w // 2
    cy = video_h // 2

    # Inline override for the gold "vs" separator: switch primary
    # colour, render `vs`, switch back to white. Used by both the team
    # and players lines. The styles below ship white as primary so the
    # second `\1c` restores the default; we don't use `\r` because it
    # would also reset bold/italic and any other inline tweaks.
    gold_inline = _bgr(C_GOLD_BRIGHT)
    white_inline = _bgr(C_WHITE)
    vs_span = (
        f"{{\\1c&H{gold_inline}&}}vs"
        f"{{\\1c&H{white_inline}&}}"
    )

    # Vertical layout — generous spacing so each line breathes. The
    # headline is the visual anchor; sub-texts sit a comfortable ~280 px
    # above / below at 1080p. team_y slots between the gold accent and
    # the players line when both teams are set; falls out of the layout
    # otherwise so the players line keeps its singles position.
    has_team = bool(p1_team.strip() and p2_team.strip())
    tournament_y  = int(cy - 320 * scale)
    headline_y    = cy
    line_y        = int(cy + 110 * scale)
    if has_team:
        team_y    = int(cy + 250 * scale)
        players_y = int(cy + 340 * scale)
    else:
        team_y    = 0
        players_y = int(cy + 320 * scale)

    # Headline dominates — sized so a 10-char word like 'FULL MATCH' has
    # presence without overflowing the frame at 1080p. Arial Black at
    # 180 px renders ≈ 1100 px wide for 'FULL MATCH', well inside 1920.
    fs_headline   = max(96,  int(180 * scale))
    fs_tournament = max(28,  int(48 * scale))
    fs_team       = max(24,  int(38 * scale))   # smaller than players, reads as "label"
    fs_players    = max(28,  int(48 * scale))

    # Accent line under the headline — wipes outward from centre, same
    # idiom as the cinematic intro / text intro for visual continuity.
    line_w = int(video_w * 0.42)
    line_h = max(3, int(5 * scale))

    end_time = _fmt_time(duration)
    # Cap tournament length — wider visible band than the scoreboard but
    # we still don't want runaway titles wrecking the layout.
    title_safe = _ass_escape(_trim_title(tournament, max_chars=45)) if tournament.strip() else ""
    headline_safe = _ass_escape((headline or "FULL MATCH").strip() or "FULL MATCH")

    def _vs_line(left: str, right: str) -> str:
        """Compose 'left vs right' with the literal 'vs' rendered gold
        between two white halves. Single-side input (other half empty)
        falls back to plain text — no orphan separator."""
        left_safe = _ass_escape(left.strip())
        right_safe = _ass_escape(right.strip())
        if left_safe and right_safe:
            return f"{left_safe} {vs_span} {right_safe}"
        return left_safe or right_safe

    players_safe = _vs_line(p1_label, p2_label)
    team_safe = _vs_line(p1_team, p2_team) if has_team else ""

    gold_bgr = _bgr(C_GOLD)

    # Arial Black for the headline (bold broadcast feel). Sub-text uses
    # plain Arial with bold so libass on systems without Arial Black
    # still gets a heavy weight. TeamLine and Players share the same
    # white primary; the gold inline-override on the "vs" separator is
    # what makes the two sides pop. Only the smaller font on TeamLine
    # signals "label" vs "content" relative to Players.
    styles = (
        f"Style: Headline,   Arial Black, {fs_headline},   {C_WHITE}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 6, 0, 1, 4, 3, 5, 0, 0, 0, 1\n"
        f"Style: Tournament, Arial,       {fs_tournament}, {C_GOLD_BRIGHT}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 4, 0, 1, 2, 2, 5, 0, 0, 0, 1\n"
        f"Style: TeamLine,   Arial,       {fs_team},       {C_WHITE}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 3, 0, 1, 2, 2, 5, 0, 0, 0, 1\n"
        f"Style: Players,    Arial,       {fs_players},    {C_WHITE}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 3, 0, 1, 2, 2, 5, 0, 0, 0, 1\n"
        f"Style: Box,        Arial,       1,               {C_WHITE}, &H000000FF, &H00000000, &H80000000,  0, 0, 0, 0, 100, 100, 0, 0, 1, 0, 0, 7, 0, 0, 0, 1"
    )
    lines: list[str] = [_ass_skeleton(video_w, video_h, styles)]

    # Tournament — first to fade in, sits well above the headline.
    if title_safe:
        lines.append(
            f"Dialogue: 1,0:00:00.20,{end_time},Tournament,,0,0,0,,"
            f"{{\\an5\\pos({cx},{tournament_y})\\fad(400,300)}}{title_safe}"
        )

    # Headline — the visual anchor. \fad fades in 300 ms / out 300 ms;
    # the \t scale ramp runs for the full duration so the type breathes
    # outward over the 3 s instead of sitting frozen.
    duration_ms = int(duration * 1000)
    lines.append(
        f"Dialogue: 2,0:00:00.30,{end_time},Headline,,0,0,0,,"
        f"{{\\an5\\pos({cx},{headline_y})\\fad(300,300)"
        f"\\fscx100\\fscy100\\t(0,{duration_ms},\\fscx110\\fscy110)}}{headline_safe}"
    )

    # Gold accent line — wipes out from centre. Same `\fscx 0 → 100`
    # idiom used by the cinematic / text intros' accent lines.
    line_x_left = cx - line_w // 2
    lines.append(
        f"Dialogue: 1,0:00:00.50,{end_time},Box,,0,0,0,,"
        f"{{\\an7\\pos({line_x_left},{line_y})\\org({cx},{line_y})"
        f"\\fad(400,300)\\bord0\\shad0"
        f"\\1c&H{gold_bgr}&\\1a&H00&\\fscx0\\t(0,600,\\fscx100)\\p1}}"
        f"m 0 0 l {line_w} 0 l {line_w} {line_h} l 0 {line_h}{{\\p0}}"
    )

    # Team labelling line — only when both sides have a team set. Same
    # fade rhythm as the players line but starts a hair earlier so the
    # eye reads the team labels first, then drops to the player names.
    if team_safe:
        lines.append(
            f"Dialogue: 1,0:00:00.55,{end_time},TeamLine,,0,0,0,,"
            f"{{\\an5\\pos({cx},{team_y})\\fad(400,300)}}{team_safe}"
        )

    # Player names — last in, mirrors the tournament line's rhythm but
    # on the opposite side of the headline.
    if players_safe:
        lines.append(
            f"Dialogue: 1,0:00:00.60,{end_time},Players,,0,0,0,,"
            f"{{\\an5\\pos({cx},{players_y})\\fad(400,300)}}{players_safe}"
        )

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path
