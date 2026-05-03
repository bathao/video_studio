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
    so a missing photo stays visible to the operator."""
    if not name or not name.strip():
        return None
    target = _norm(name)
    if target.startswith(_RESERVED_PREFIX):
        return None  # reserved filenames cannot be claimed as a name
    return _scan(target)


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
