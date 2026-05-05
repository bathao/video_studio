"""
Intro title cards. Two flavours sit side-by-side here:

  - `build_intro_ass`           — the original 3 s text-only card
                                   (used as a fallback when avatars are
                                   missing).
  - `build_cinematic_intro_ass` — text overlay accompaniment for the
                                   ffmpeg-driven cinematic intro
                                   (avatars sliding in over a blurred
                                   source frame; see
                                   `backend/intro_builder.py`).

Both share the palette + helpers in `common.py`.
"""

from __future__ import annotations

from pathlib import Path

from .common import (
    C_GOLD, C_GOLD_BRIGHT, C_WHITE,
    _ass_escape, _ass_rgb, _ass_skeleton, _bgr, _fmt_time,
    _trim_team, _trim_title,
)


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

    styles = (
        f"Style: Title, Arial, {fs_title}, {C_GOLD},  &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 3, 0, 1, 2, 1, 5, 0, 0, 0, 1\n"
        f"Style: VS,    Arial, {fs_vs},    {vs_dim},  &H000000FF, &H00000000, &H80000000,  0, 1, 0, 0, 100, 100, 4, 0, 1, 2, 0, 5, 0, 0, 0, 1\n"
        f"Style: Name,  Arial, {fs_name},  {C_WHITE}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 2, 0, 1, 2, 2, 5, 0, 0, 0, 1\n"
        f"Style: Box,   Arial, 1,           {C_WHITE}, &H000000FF, &H00000000, &H80000000,  0, 0, 0, 0, 100, 100, 0, 0, 1, 0, 0, 7, 0, 0, 0, 1"
    )
    lines: list[str] = [_ass_skeleton(video_w, video_h, styles)]

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

    title_safe = _ass_escape(_trim_title(tournament)) if tournament.strip() else ""
    p1_safe    = _ass_escape(p1_name.strip() or "Player 1")
    p2_safe    = _ass_escape(p2_name.strip() or "Player 2")

    styles = (
        f"Style: Tournament, Arial, {fs_tournament}, {C_GOLD_BRIGHT}, &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 4, 0, 1, 3, 2, 5, 0, 0, 0, 1\n"
        f"Style: Name,       Arial, {fs_name},       {C_WHITE},       &H000000FF, &H00000000, &H80000000, -1, 0, 0, 0, 100, 100, 2, 0, 1, 2, 2, 5, 0, 0, 0, 1\n"
        f"Style: VS,         Arial, {fs_vs},         {C_GOLD_BRIGHT}, &H000000FF, &H00000000, &H80000000, -1, 1, 0, 0, 100, 100, 6, 0, 1, 4, 3, 5, 0, 0, 0, 1\n"
        f"Style: Box,        Arial, 1,               {C_WHITE},       &H000000FF, &H00000000, &H80000000,  0, 0, 0, 0, 100, 100, 0, 0, 1, 0, 0, 7, 0, 0, 0, 1"
    )
    lines: list[str] = [_ass_skeleton(video_w, video_h, styles)]

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
