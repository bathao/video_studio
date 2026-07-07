"""Dry-run a render plan without touching ffmpeg's encoders.

Prints what a render WOULD produce for a saved project: source video
metadata, trim/kept segment breakdown, slow-mo replay inserts, the main
playlist composition (including whether the NVDEC pre-concat pass would
trigger), and the estimated final output duration. The only external
call is one read-only ffprobe on the source video.

Usage:
    venv/Scripts/python scripts/dry_run_render.py <project>
    venv/Scripts/python scripts/dry_run_render.py projects/match_001.json

<project> may be a bare project name (resolved to projects/<name>.json)
or a path to a project JSON file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backend.config import config                              # noqa: E402
from backend.ffmpeg_runner import probe_video                  # noqa: E402
from backend.models import ProjectData                         # noqa: E402
from backend.renderer import (                                 # noqa: E402
    build_main_playlist,
    build_replay_plan,
    kept_segments_from_trims,
)

# Mirrors renderer/orchestrator.py — keep in sync (a drift here only
# mislabels the dry-run output, it never affects a real render).
SLICE_PRECONCAT_THRESHOLD = 6


def _fmt(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def _resolve_project(arg: str) -> Path:
    p = Path(arg)
    if p.suffix == ".json" and p.exists():
        return p
    candidate = config.projects_dir / f"{arg}.json"
    if candidate.exists():
        return candidate
    sys.exit(f"Project not found: {arg} (tried {p} and {candidate})")


def _resolve_source(video_file: str) -> Path:
    if not video_file:
        sys.exit("Project has no video_file set - nothing to dry-run.")
    p = Path(video_file)
    if p.is_absolute():
        if not p.is_file():
            sys.exit(f"Source video missing: {p}")
        return p
    candidate = config.videos_dir / video_file
    if not candidate.is_file():
        sys.exit(f"Source video missing: {candidate}")
    return candidate


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("project", help="project name or path to project JSON")
    args = ap.parse_args()

    proj_path = _resolve_project(args.project)
    data = ProjectData.model_validate(
        json.loads(proj_path.read_text(encoding="utf-8"))
    )

    src = _resolve_source(data.info.video_file)
    info = probe_video(src)
    duration = float(info.get("duration", 0.0))

    print(f"Project : {proj_path}")
    print(f"Source  : {src}")
    print(f"          {info.get('width')}x{info.get('height')} @ "
          f"{info.get('fps'):.5g} fps | {_fmt(duration)} "
          f"| audio={'yes' if info.get('has_audio') else 'no'}")

    # --- trims / kept segments ------------------------------------------
    trims = data.trim_segments
    n_auto = sum(1 for t in trims if getattr(t, "source", "manual") == "auto")
    kept = kept_segments_from_trims(duration, trims)
    kept_total = sum(e - s for s, e in kept)
    print(f"\nTrims   : {len(trims)} ({n_auto} auto, {len(trims) - n_auto} manual)")
    print(f"Kept    : {len(kept)} segment(s), {_fmt(kept_total)} "
          f"({kept_total / duration * 100.0 if duration else 0:.1f}% of source; "
          f"{_fmt(duration - kept_total)} removed)")

    # --- replays + playlist ----------------------------------------------
    replays = build_replay_plan(data.highlights, kept)
    stinger_in = float(config.get("stinger_duration_seconds", 1.5))
    stinger_out = float(config.get("stinger_out_duration_seconds", 0.6))
    stinger_on = bool(config.get("stinger_enabled", True))
    playlist = build_main_playlist(
        kept, replays,
        # Dummy paths — the dry run never opens them; presence just
        # tells the playlist builder to include the stinger bracket.
        stinger_in_path=Path("stinger_in.mp4") if stinger_on else None,
        stinger_out_path=Path("stinger_out.mp4") if stinger_on else None,
        stinger_in_duration=stinger_in if stinger_on else 0.0,
        stinger_out_duration=stinger_out if stinger_on else 0.0,
    )
    kinds = {k: sum(1 for e in playlist if e.kind == k)
             for k in ("slice", "replay", "stinger_in", "stinger_out")}
    replay_total = sum(r.replay_duration for r in replays)
    print(f"\nReplays : {len(replays)} slow-mo insert(s), +{_fmt(replay_total)} "
          f"(+{_fmt((stinger_in + stinger_out) * len(replays)) if stinger_on else '0:00'} stingers)")
    for i, r in enumerate(replays, 1):
        print(f"  #{i}: src {_fmt(r.src_start)}-{_fmt(r.src_end)} "
              f"-> inserted at main {_fmt(r.insert_at_main)} "
              f"({r.replay_duration:.1f}s at 50%)")

    main_dur = playlist[-1].final_end if playlist else 0.0
    print(f"\nPlaylist: {len(playlist)} entries: "
          f"{kinds['slice']} slice / {kinds['replay']} replay / "
          f"{kinds['stinger_in'] + kinds['stinger_out']} stinger")
    if kinds["slice"] > SLICE_PRECONCAT_THRESHOLD:
        print(f"          NVDEC pre-concat WILL run "
              f"({kinds['slice']} slices > {SLICE_PRECONCAT_THRESHOLD} session budget)")
    else:
        print(f"          NVDEC pre-concat not needed "
              f"({kinds['slice']} slices <= {SLICE_PRECONCAT_THRESHOLD})")

    # --- final duration estimate ------------------------------------------
    intro_dur = float(config.get("intro_duration_seconds", 4.0))
    outro_on = bool(config.get("outro_enabled", True))
    outro_dur = float(config.get("outro_duration_seconds", 5.0)) if outro_on else 0.0
    total = intro_dur + main_dur + outro_dur
    print(f"\nEstimate: intro {_fmt(intro_dur)} + main {_fmt(main_dur)}"
          f" + outro {_fmt(outro_dur)} = {_fmt(total)} final output")
    print(f"Events  : {len(data.score_events)} score events "
          f"(scoreboard total_duration = {_fmt(main_dur)})")


if __name__ == "__main__":
    main()
