"""
Avatar image lookup. Player photos live as flat files under
`assets/avatars/<Player Name>.<ext>` (no subfolder per player). Names
are matched case-insensitively after NFC-normalising the Unicode so
Vietnamese diacritics typed in any editor still match.

The cinematic intro renderer uses these photos; if either player has no
avatar the renderer falls back to the existing text-only title card and
the operator is asked to drop a photo in.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Optional

from .ass.common import strip_note_suffix
from .config import config

# Order encodes priority: when both `Ma Long.png` and `Ma Long.jpg`
# exist, the .png wins.
AVATAR_EXTS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp")

# Filenames starting with `_` are reserved (e.g. `_default.jpg`) and
# never match a player name, even if a player happens to be called "_x".
_RESERVED_PREFIX = "_"
_DEFAULT_STEM = "_default"


def _avatars_root() -> Path:
    return config.assets_dir / "avatars"


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s.strip()).lower()


def _scan(stem_target: str) -> Optional[Path]:
    """Find a file in the avatars folder whose stem (case + NFC
    normalised) matches `stem_target`. Returns the highest-priority
    extension if multiple files share the stem."""
    root = _avatars_root()
    if not root.exists():
        return None
    best: Optional[tuple[int, Path]] = None
    for entry in root.iterdir():
        if not entry.is_file():
            continue
        ext = entry.suffix.lower()
        if ext not in AVATAR_EXTS:
            continue
        if _norm(entry.stem) != stem_target:
            continue
        priority = AVATAR_EXTS.index(ext)
        if best is None or priority < best[0]:
            best = (priority, entry)
    return best[1] if best else None


def find_avatar(name: str) -> Optional[Path]:
    """Strict lookup: returns the path to <name>'s avatar image, or
    None. Never falls back to the default placeholder — used by the UI
    so a missing photo stays visible to the operator.

    A trailing '(note)' on the typed name is a viewer-facing annotation
    ('Lương Đức Tuấn (Gai Dài)'), not part of the player's identity —
    when the verbatim name has no photo we retry without the note, so
    annotating a name never loses the avatar. An exact filename match
    (including parentheses) still wins."""
    if not name or not name.strip():
        return None
    target = _norm(name)
    if target.startswith(_RESERVED_PREFIX):
        return None  # reserved filenames cannot be claimed as a name
    hit = _scan(target)
    if hit is not None:
        return hit
    stripped = _norm(strip_note_suffix(name))
    if stripped and stripped != target and not stripped.startswith(_RESERVED_PREFIX):
        return _scan(stripped)
    return None


def list_avatar_names() -> list[str]:
    """Every player name that has a photo on disk — the file stems
    (NFC-normalised, whitespace-trimmed), sorted case-insensitively.
    Reserved `_*` files are excluded; a stem present in several
    extensions counts once. Powers the type-ahead name suggestions in
    the Setup panel (`GET /api/avatars`)."""
    root = _avatars_root()
    if not root.exists():
        return []
    names: dict[str, str] = {}
    for entry in root.iterdir():
        if not entry.is_file() or entry.suffix.lower() not in AVATAR_EXTS:
            continue
        stem = unicodedata.normalize("NFC", entry.stem.strip())
        if not stem or stem.startswith(_RESERVED_PREFIX):
            continue
        names.setdefault(_norm(stem), stem)
    return sorted(names.values(), key=str.casefold)


def find_default_avatar() -> Optional[Path]:
    """Return `_default.<ext>` if the operator dropped one in, else None."""
    return _scan(_DEFAULT_STEM)


def find_avatar_or_default(name: str) -> tuple[Optional[Path], bool]:
    """Render-time lookup. Returns `(path, used_default)`:

      - `(player_photo, False)` when a player-specific photo exists,
      - `(default_photo, True)` when only the placeholder is available,
      - `(None, False)` when even the placeholder is missing — the
        renderer should then fall back to the text-only intro.
    """
    p = find_avatar(name)
    if p is not None:
        return (p, False)
    d = find_default_avatar()
    return (d, d is not None)
