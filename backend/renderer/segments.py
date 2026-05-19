"""Segment math: invert trim-segments → kept-segments and remap source
timestamps into the trimmed-main timeline.

Pure logic — no ffmpeg, no I/O. Both functions are also re-exported
from the package root for external callers (tests, groundtruth sidecar).
"""

from __future__ import annotations

from typing import Optional

from ..models import TrimSegment


def kept_segments_from_trims(
    duration: float,
    trims: list[TrimSegment],
) -> list[tuple[float, float]]:
    """Invert a list of remove-segments into the list of keep-segments."""
    if duration <= 0:
        return []
    if not trims:
        return [(0.0, duration)]
    cleaned: list[tuple[float, float]] = []
    for t in trims:
        a = max(0.0, min(duration, float(t.start)))
        b = max(0.0, min(duration, float(t.end)))
        if b > a:
            cleaned.append((a, b))
    cleaned.sort()
    # Merge overlaps
    merged: list[list[float]] = []
    for a, b in cleaned:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    kept: list[tuple[float, float]] = []
    cursor = 0.0
    for a, b in merged:
        if a > cursor:
            kept.append((cursor, a))
        cursor = b
    if cursor < duration:
        kept.append((cursor, duration))
    return kept


def remap_score_event_to_trimmed(
    t_source: float,
    kept: list[tuple[float, float]],
) -> Optional[float]:
    """
    Convert a source-time score timestamp to its position inside the
    trimmed main video. Returns None if there are no kept segments.
    Events that fall inside a removed gap are snapped forward to the
    start of the next kept segment so the score-change still appears.
    """
    if not kept:
        return None
    accumulated = 0.0
    for a, b in kept:
        if t_source < a:
            return accumulated  # snap forward to start of this kept segment
        if t_source <= b:
            return accumulated + (t_source - a)
        accumulated += b - a
    return accumulated  # past end → end of trimmed video
