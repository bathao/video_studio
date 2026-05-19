"""Slow-mo replay plumbing.

Pure logic: given the operator's highlights, compute which slow-mo
replays to splice into the main timeline + the per-entry playlist that
the ffmpeg main-render stage consumes. Also handles the score-event
shift each replay (and its optional stinger bracket) introduces.

No ffmpeg, no I/O. Re-exported from the package root for tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..ass import ScoreFrame
from ..models import Highlight
from .segments import remap_score_event_to_trimmed


# Speed factor for slow-mo replays inserted into main. 0.5 → 2× duration,
# atempo=0.5 audio. Picked to match what's visually readable for a table-
# tennis rally — fast enough that the replay doesn't drag, slow enough
# to show the rally's geometry clearly.
REPLAY_SPEED = 0.5

# Volume scale applied to the replay clip's audio. atempo=0.5 leaves the
# pitch intact but smears the rally noises (ball-hits, crowd) into a
# muddy drone that's worse than helpful — half the volume keeps the
# slow-mo feeling immersive without making it the loudest thing on the
# track.
REPLAY_VOLUME = 0.0

# Linear fade window (seconds) applied to BOTH ends of the replay audio.
# Smooths the snap between real-time main slice (full volume) and the
# slow-mo audio (REPLAY_VOLUME) at the concat boundaries — the dip to
# silence reads as a deliberate "wow moment" beat before/after the
# slow-mo, instead of an audible level cut. Capped to 1/4 of the
# replay's final duration so very short replays don't overlap the
# two fades into each other.
REPLAY_FADE_SECONDS = 0.3


@dataclass(frozen=True)
class ReplayInsert:
    """A slow-mo replay of a highlight, scheduled to play in the main
    render right after the highlight's real-time occurrence.

    `insert_at_main` is the moment (in TRIMMED-main coords, before any
    replay is added to the timeline) where the replay drops in. Built
    by remapping the highlight's source `end` time through the trims.

    `src_start` / `src_end` is the source range to slow down. The final
    replay clip plays for `(src_end - src_start) / REPLAY_SPEED` seconds.
    """
    insert_at_main: float
    src_start: float
    src_end: float

    @property
    def replay_duration(self) -> float:
        return (self.src_end - self.src_start) / REPLAY_SPEED


def build_replay_plan(
    highlights: list[Highlight],
    kept: list[tuple[float, float]],
) -> list[ReplayInsert]:
    """For each highlight, compute the trimmed-main insert point right
    after its real-time playback. Highlights whose `end` lies inside a
    trim get snapped forward to the start of the next kept segment, so
    the replay still plays once the main video resumes (which still
    reads as 'just after we saw the action live')."""
    plan: list[ReplayInsert] = []
    for h in highlights:
        if h.end <= h.start:
            continue
        t_main = remap_score_event_to_trimmed(h.end, kept)
        if t_main is None:
            continue
        plan.append(ReplayInsert(
            insert_at_main=t_main,
            src_start=float(h.start),
            src_end=float(h.end),
        ))
    plan.sort(key=lambda r: r.insert_at_main)
    return plan


def remap_events_with_replays(
    events: list[ScoreFrame],
    replays: list[ReplayInsert],
    *,
    stinger_total_duration: float = 0.0,
) -> list[ScoreFrame]:
    """Shift each score event by the cumulative replay (+ optional
    stinger bracket) duration of replays that occur strictly before
    the event. Events that fire AT a replay's insert point stay put
    so the post-highlight score is visible during the replay too.

    `stinger_total_duration` is `in_duration + out_duration` (the
    asymmetric bracket: long IN before, short OUT after). When > 0
    each replay adds this on top of `replay_duration` to the timeline;
    subsequent events shift by the combined amount."""
    if not replays:
        return events
    out: list[ScoreFrame] = []
    for ev in events:
        shift = 0.0
        for r in replays:
            if r.insert_at_main < ev.timestamp:
                shift += r.replay_duration + stinger_total_duration
            else:
                break
        out.append(ScoreFrame(
            timestamp=ev.timestamp + shift,
            p1_score=ev.p1_score, p2_score=ev.p2_score,
            p1_set=ev.p1_set, p2_set=ev.p2_set,
        ))
    return out


@dataclass(frozen=True)
class _PlaylistEntry:
    """One ffmpeg input slot for the main render.

    `kind` discriminates:
      - "slice"      : a normal-speed slice of the source video
      - "replay"     : a slow-mo replay (needs setpts*2 + atempo=0.5)
      - "stinger_in" : pre-rendered branded transition before a replay
      - "stinger_out": pre-rendered branded transition after a replay
                       (same content as stinger_in, played reversed)

    For "slice" / "replay" the source is the main project video and we
    use input-side `-ss src_start -t (src_end-src_start) -i <src>`. For
    stinger entries, `src_path` points to the cached stinger mp4 and the
    whole file is consumed (no -ss/-t needed).

    `final_start` / `final_end` are the entry's position in the final
    main timeline — used to time the SLOW MOTION badge during replay
    entries and to remap score events past every entry's contribution.
    """
    kind: str
    src_start: float
    src_end: float
    final_start: float
    final_end: float
    src_path: Optional[Path] = None   # only set for stinger entries


def build_main_playlist(
    kept: list[tuple[float, float]],
    replays: list[ReplayInsert],
    *,
    stinger_in_path: Optional[Path] = None,
    stinger_out_path: Optional[Path] = None,
    stinger_in_duration: float = 0.0,
    stinger_out_duration: float = 0.0,
) -> list[_PlaylistEntry]:
    """Walk the kept segments and splice each replay in at its insert
    point. Kept segments that contain insert points get split into
    sub-slices; replays drop in between. When stinger paths are supplied
    each replay is bracketed with a sting-in (before) and sting-out
    (after). IN and OUT have INDEPENDENT durations — the IN clip is
    the long readable hold, OUT is the quick wipe-out back to live.

    When a replay's insert point lands exactly at a kept-segment
    boundary, the splice falls between two existing slices — no extra
    split is generated.

    Stinger entries always have `src_path` set; slice / replay entries
    leave it None so the caller knows to use the main source with
    input-side `-ss` / `-t`.
    """
    has_stinger = (
        stinger_in_path is not None
        and stinger_out_path is not None
        and stinger_in_duration > 0.0
        and stinger_out_duration > 0.0
    )

    entries: list[_PlaylistEntry] = []
    accumulated_main = 0.0          # trimmed-main offset at start of current kept
    accumulated_final = 0.0         # final-timeline cursor (incl. replay + sting)
    replay_idx = 0

    def _append_replay_with_stingers(r: ReplayInsert) -> None:
        nonlocal accumulated_final
        if has_stinger:
            entries.append(_PlaylistEntry(
                kind="stinger_in",
                src_start=0.0, src_end=stinger_in_duration,
                final_start=accumulated_final,
                final_end=accumulated_final + stinger_in_duration,
                src_path=stinger_in_path,
            ))
            accumulated_final += stinger_in_duration
        r_len = r.replay_duration
        entries.append(_PlaylistEntry(
            kind="replay",
            src_start=r.src_start, src_end=r.src_end,
            final_start=accumulated_final,
            final_end=accumulated_final + r_len,
        ))
        accumulated_final += r_len
        if has_stinger:
            entries.append(_PlaylistEntry(
                kind="stinger_out",
                src_start=0.0, src_end=stinger_out_duration,
                final_start=accumulated_final,
                final_end=accumulated_final + stinger_out_duration,
                src_path=stinger_out_path,
            ))
            accumulated_final += stinger_out_duration

    for a, b in kept:
        seg_len = b - a
        kept_end_main = accumulated_main + seg_len
        current_src = a

        # Consume any replays whose insert point falls inside this kept
        # segment's trimmed-main range. Equal-to-end is consumed here so
        # the replay slots in before moving to the next kept.
        while replay_idx < len(replays) and replays[replay_idx].insert_at_main <= kept_end_main:
            r = replays[replay_idx]
            offset_in_kept = max(0.0, r.insert_at_main - accumulated_main)
            split_src = a + offset_in_kept
            # Pre-split slice (may be empty if replay lands at the segment start).
            if split_src > current_src:
                slice_len = split_src - current_src
                entries.append(_PlaylistEntry(
                    kind="slice",
                    src_start=current_src, src_end=split_src,
                    final_start=accumulated_final,
                    final_end=accumulated_final + slice_len,
                ))
                accumulated_final += slice_len
            _append_replay_with_stingers(r)
            current_src = split_src
            replay_idx += 1

        # Trailing slice of this kept segment.
        if b > current_src:
            slice_len = b - current_src
            entries.append(_PlaylistEntry(
                kind="slice",
                src_start=current_src, src_end=b,
                final_start=accumulated_final,
                final_end=accumulated_final + slice_len,
            ))
            accumulated_final += slice_len

        accumulated_main = kept_end_main

    # Any replays whose insert point lands past every kept segment land
    # at the very end (insert_at_main was snapped to past-end by remap).
    while replay_idx < len(replays):
        _append_replay_with_stingers(replays[replay_idx])
        replay_idx += 1

    return entries
