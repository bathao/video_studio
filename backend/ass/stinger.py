"""
libass overlay for the auto-stinger clip.

Burned on top of the blurred-source background + semi-transparent
brand-colour wash in `stinger_builder._render_stinger_forward`.
Provides:

  - A diagonal "light streak" that sweeps across the frame right after
    the wipe — `\\blur` + `\\frz` give the soft rotated bar, `\\move`
    drives it from off-screen left to off-screen right. Reads as a
    light flash, lights up the logo area without making the logo
    itself glow.
  - The channel name centred below the logo. libass handles Vietnamese
    diacritics reliably; drawtext doesn't without an explicit fontfile.
  - A small "REPLAY" label below the channel name, accent-gold so it
    pops against the warm bar.

Timings are ABSOLUTE seconds, not percentages of duration: wipe always
finishes at 0.20 s, logo + text fade in by 0.35 s, then hold until the
end. Increasing `duration` extends the readable hold at the end — no
need to retune the animation when slowing the clip down.

With duration = 1.5 s the hold is ~1.15 s, plenty of time to read
"Nguyễn Bá Thảo · REPLAY" without rushing.
"""

from __future__ import annotations

from pathlib import Path

from .common import C_GOLD_BRIGHT, C_WHITE, _ass_escape, _ass_skeleton, _bgr, _fmt_time


def build_stinger_ass(
    *,
    output_path: Path,
    video_w: int,
    video_h: int,
    duration: float,
    channel_name: str,
    replay_label: str = "REPLAY",
) -> Path:
    """Write the libass overlay used by the forward stinger clip.

    Animation timeline (ABSOLUTE seconds, not percentages, so extending
    `duration` extends the trailing hold without retuning the entry
    animation):
      0.00 – 0.20 s : nothing drawn (brand bar is still wiping below).
      0.05 – 0.30 s : light streak sweeps diagonally across.
      0.20 – 0.35 s : channel name + REPLAY fade in.
      0.35 s – end  : everything holds. Hold = duration − 0.35 s.

    Reverse playback produces the OUT stinger so the same .ass works
    backwards too — the streak then sweeps the OTHER way as the stinger
    leaves.
    """
    scale = max(0.6, video_h / 1080.0)
    fs_channel = max(36, int(58 * scale))
    fs_replay = max(26, int(38 * scale))

    # Vertical layout: logo sits ~40 % down the frame (set by the
    # ffmpeg overlay in stinger_builder), channel name ~66 %, REPLAY
    # label ~80 %.
    cy_channel = int(video_h * 0.66)
    cy_replay = int(video_h * 0.80)

    # ABSOLUTE timings (with safety caps for very short durations).
    # Increasing `duration` lengthens the trailing hold; entry / fade
    # phases are unchanged.
    text_show_s = min(0.20, duration * 0.30)
    text_fade_in_ms = int(min(0.15, duration * 0.20) * 1000)

    text_show_start = _fmt_time(text_show_s)
    end_t = _fmt_time(duration)

    # Light streak: 0.05 → 0.30 s, absolute. The \\fad softens the
    # entry/exit so the streak reads as a passing light rather than a
    # hard rectangle popping in.
    streak_show_s = min(0.05, duration * 0.10)
    streak_hide_s = min(0.30, duration * 0.40)
    streak_show_start = _fmt_time(streak_show_s)
    streak_show_end = _fmt_time(streak_hide_s)
    streak_move_ms = int((streak_hide_s - streak_show_s) * 1000)

    # Streak geometry: a thin tall rectangle, rotated 25° clockwise via
    # \\frz, blurred via \\blur. Width ~4 % of frame width, height 140 %
    # of frame height so rotation can't expose its top/bottom edges
    # inside the visible canvas.
    streak_w = max(40, int(video_w * 0.04))
    streak_hw = streak_w // 2
    streak_hh = int(video_h * 0.70)
    streak_y = int(video_h * 0.40)         # roughly the logo's vertical centre
    streak_x_start = -int(video_w * 0.2)
    streak_x_end = video_w + int(video_w * 0.2)

    styles = (
        f"Style: ChannelName, Arial, {fs_channel}, {C_WHITE}, &H000000FF, &H00000000, &HA0000000, -1, 0, 0, 0, 100, 100, 2, 0, 1, 2, 2, 5, 0, 0, 0, 1\n"
        f"Style: ReplayLabel, Arial, {fs_replay}, {C_GOLD_BRIGHT}, &H000000FF, &H00000000, &HA0000000, -1, 0, 0, 0, 100, 100, 3, 0, 1, 2, 2, 5, 0, 0, 0, 1\n"
        f"Style: Streak,      Arial, 1,           {C_WHITE},        &H000000FF, &H00000000, &H00000000, 0,  0, 0, 0, 100, 100, 0, 0, 1, 0, 0, 7, 0, 0, 0, 1"
    )

    lines: list[str] = [_ass_skeleton(video_w, video_h, styles)]

    # Light streak. \\1a&H80& = 50 % opacity so it reads as light, not
    # a solid bar — the blurred white + brand-orange beneath it should
    # combine into a warm flash. Layered 0 (lowest) so any future ASS
    # primitives appear in front.
    lines.append(
        f"Dialogue: 0,{streak_show_start},{streak_show_end},Streak,,0,0,0,,"
        f"{{\\an5"
        f"\\move({streak_x_start},{streak_y},{streak_x_end},{streak_y},0,{streak_move_ms})"
        f"\\frz25"
        f"\\1c&H{_bgr(C_WHITE)}&\\1a&H80&"
        f"\\bord0\\shad0\\blur30"
        f"\\fad(100,120)"
        f"\\p1}}"
        f"m -{streak_hw} -{streak_hh} l {streak_hw} -{streak_hh} "
        f"l {streak_hw} {streak_hh} l -{streak_hw} {streak_hh}"
        f"{{\\p0}}"
    )

    # Channel name — handles Vietnamese diacritics via libass shaping.
    name_safe = _ass_escape((channel_name or "").strip())
    if name_safe:
        lines.append(
            f"Dialogue: 1,{text_show_start},{end_t},ChannelName,,0,0,0,,"
            f"{{\\an5\\pos({video_w // 2},{cy_channel})"
            f"\\fad({text_fade_in_ms},0)}}"
            f"{name_safe}"
        )

    # REPLAY label — accent-gold, slightly delayed so the eye reads
    # name → REPLAY in sequence rather than as a mass.
    #
    # Position: sits below the channel name when one is set. When the
    # channel name is BLANK we hoist REPLAY up into the channel-name
    # row instead, so there isn't a 30 % vertical gap between the logo
    # and REPLAY label.
    replay_safe = _ass_escape((replay_label or "").strip())
    if replay_safe:
        if name_safe:
            replay_y = cy_replay
            replay_delay_ms = 80  # short stagger after the name
        else:
            replay_y = cy_channel
            replay_delay_ms = 0   # no name to sequence after — appear with logo
        replay_start_s = text_show_s + replay_delay_ms / 1000.0
        replay_show_start = _fmt_time(replay_start_s)
        lines.append(
            f"Dialogue: 1,{replay_show_start},{end_t},ReplayLabel,,0,0,0,,"
            f"{{\\an5\\pos({video_w // 2},{replay_y})"
            f"\\fad({text_fade_in_ms},0)}}"
            f"▶  {replay_safe}"
        )

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path
