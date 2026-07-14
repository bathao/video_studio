"""
Shared utilities and palette for the ASS overlay builders.

Every other module in `backend.ass` imports from here. Anything written
literally in raw f-strings — colour codes, drawing primitives, name
trimming, the time stamp formatter — lives in this file so callers can
trust a single source of truth.
"""

from __future__ import annotations

import re


def _fmt_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - (h * 3600 + m * 60)
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
    )


def _trim_name(text: str, max_len: int = 22) -> str:
    text = (text or "").strip()
    if not text:
        return "PLAYER"
    if len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


_NOTE_SUFFIX_RE = re.compile(r"\s*\([^()]*\)\s*$")


def strip_note_suffix(name: str) -> str:
    """Drop trailing parenthetical notes from a player name:
    'Lương Đức Tuấn (Gai Dài)' → 'Lương Đức Tuấn'. Operator convention:
    a '(note)' at the END of the typed name is a viewer-facing
    annotation (playing style, club, …), not part of the player's
    identity — scoreboard/intro display keep it verbatim, but avatar
    lookup and the doubles name-combine rule ignore it. Repeats so
    'X (a) (b)' also reduces; parentheses mid-name are untouched."""
    text = (name or "").strip()
    while True:
        stripped = _NOTE_SUFFIX_RE.sub("", text)
        if stripped == text:
            return text
        text = stripped


def _last_two_words(name: str) -> str:
    """Last 2 whitespace-separated tokens of a player's name. Used to
    compose the combined doubles label (e.g. 'Nguyễn Văn An' →
    'Văn An'). 1-token names pass through unchanged so 'Tommy' doesn't
    get padded. Empty input returns empty so the doubles join can
    short-circuit (no stray ' + ' for missing partner).

    Why: in doubles each row of the scoreboard fits ~22 chars (name
    column width). Combining two full Vietnamese 3-token names blows
    that budget; the last-2 convention keeps the personal-name part
    (which is what locals refer to each other by) and drops only the
    family prefix that all four players might share. A trailing
    '(note)' annotation is stripped first — otherwise it would BE the
    last two tokens and eat the whole label."""
    text = strip_note_suffix(name)
    if not text:
        return ""
    tokens = text.split()
    if len(tokens) <= 2:
        return text
    return " ".join(tokens[-2:])


def _combine_doubles_name(a: str, b: str) -> str:
    """Join two player names into the doubles row label. Empty halves
    are dropped, so the label gracefully degrades when only one partner
    is filled in."""
    parts = [_last_two_words(a), _last_two_words(b)]
    parts = [p for p in parts if p]
    if not parts:
        return ""
    return " + ".join(parts)


def _trim_team(text: str, max_len: int = 20) -> str:
    """Like `_trim_name` but returns an empty string for empty input
    instead of a placeholder, so an unset team renders as a blank cell
    rather than the literal word PLAYER. The 20-char cap fits the
    longest team-column width the scoreboard auto-sizes to."""
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


def _trim_title(text: str, max_words: int = 14, max_chars: int | None = None) -> str:
    """Tournament title: cap at `max_words` whitespace-separated tokens,
    plus an optional character cap via `max_chars`. Callers that have
    more horizontal room (e.g. singles layout with no team column)
    typically pass a generous `max_chars=45` to let longer titles
    through; the word cap stays as a safety net against pathological
    inputs (one comma-glued blob, etc.). Anything over either cap is
    truncated with an ellipsis."""
    text = (text or "").strip()
    if not text:
        return ""
    tokens = text.split()
    if len(tokens) > max_words:
        text = " ".join(tokens[:max_words]) + "…"
    if max_chars is not None and len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


# ASS colours are written &H00BBGGRR& (alpha BB GG RR). We pre-convert
# the RGB intent into that format.
def _ass_rgb(r: int, g: int, b: int) -> str:
    return f"&H00{b:02X}{g:02X}{r:02X}&"


def _bgr(c: str) -> str:
    """Strip the &H...& wrapping from an ASS colour. Used when a colour
    has to be embedded in a raw f-string (drawing primitives, inline
    \\1c overrides) instead of as a Style: column."""
    return c.strip("&H&")


# Palette (R, G, B). Each comment names the on-screen colour.
C_WHITE       = _ass_rgb(255, 255, 255)
C_GREY        = _ass_rgb(185, 185, 185)
C_GOLD        = _ass_rgb(180, 140,  40)   # broadcast amber accent
C_GOLD_BRIGHT = _ass_rgb(255, 195,  60)   # brighter gold for emphasis
C_SEP         = _ass_rgb( 75,  75,  75)   # divider lines
C_BG_HEADER   = _ass_rgb( 35,  35,  35)   # near-black header strip
C_BG_ROWS     = _ass_rgb( 18,  18,  18)   # near-black player rows
C_BG_SETS     = _ass_rgb(102,  76,  24)   # gold-tinted dark for the sets (set-point) column
C_ACCENT_HDR  = _ass_rgb(180, 140,  40)   # gold accent for tournament header
C_ACCENT_P1   = _ass_rgb(165, 100, 220)   # purple (player A)
C_ACCENT_P2   = _ass_rgb( 50, 140, 220)   # sky blue (player B)
C_GP_RED      = _ass_rgb(255,  85,  85)   # game-point flag red
C_MP_RED      = _ass_rgb(255,  40,  40)   # match-point flag (deeper red)
C_DEUCE       = _ass_rgb(255, 195,  60)   # deuce flag amber


def _rect(x: int, y: int, w: int, h: int, color: str, alpha_hex: str = "00", layer: int = 0) -> str:
    """ASS drawing primitive: filled rectangle anchored top-left at (x,y)."""
    return (
        f"{{\\an7\\pos({x},{y})\\bord0\\shad0"
        f"\\1c&H{_bgr(color)}&\\1a&H{alpha_hex}&\\p1}}"
        f"m 0 0 l {w} 0 l {w} {h} l 0 {h}{{\\p0}}"
    )


def _ass_skeleton(video_w: int, video_h: int, styles_block: str) -> str:
    """Wrap a per-builder list of `Style: …` rows with the standard ASS
    skeleton (`[Script Info]` header + `[V4+ Styles]` framing + `[Events]`
    footer). Every builder ended up writing the same boilerplate before
    its own Style rows; this helper is the single source of truth.

    `styles_block` is the joined `Style: …` lines (no leading newline,
    no trailing newline) that go between the Format line and the
    `[Events]` section.
    """
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{styles_block}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
