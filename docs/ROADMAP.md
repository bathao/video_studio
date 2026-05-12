# Production Roadmap

This document plans the path from the current MVP toward a tool a
content creator could use to publish broadcast-quality table-tennis
recaps without external editing software.

## Current state (v0.1 — MVP)

A single operator on Windows + RTX 5060 Ti can:

1. Drop a tripod recording into `videos/`.
2. Watch the match, scoring with `A` / `D` and tagging highlights with `H`.
3. Hit **Render** and get back an MP4 with intro + slow-mo highlight reel
    + main match with a burned-in scoreboard.

This works end-to-end and has been validated on a synthetic 1080p sample
in 1.74 s (20 s input). Real 2K / multi-GB validation is the next gate.

## v0.2 — Production-ready single match (target: +1 week)

Goal: comfortable for daily filming of regional tournaments.

- Validate the pipeline on the user's actual 15 GB 2K source.
- Cancel renders (done) + resume from last completed stage.
- Auto-cleanup of `temp/` (done).
- Live scoreboard preview overlaid on the `<video>` element (done — JASSUB
   libass-WASM auto-attaches once the source video reports metadata).
- Timeline markers for highlights and trims on the seek bar.
- Bundled font + bundled fonts directory so `drawtext` and `ass` filters
   never fail because the system font lookup is misconfigured.

Exit criteria: the user can edit a 90-minute 2K match in <30 minutes of
operator time and render it in <30 minutes of wall-clock GPU time.

## v0.3 — Match awareness (target: +2 weeks)

Goal: the tool understands a *match*, not just a sequence of points.

- Best-of-N config (3 / 5 / 7 sets) with auto match-end detection.
- Serve tracking (alternates every 2 points; every 1 in deuce / 10–10).
- Deuce / match-point overlays on the scoreboard.
- Per-set summary card inserted between sets in the main render.
- Score events reflect the *transition* (who scored), not just the
   absolute counts.

## v0.4 — Broadcast polish (target: +1 month)

Goal: output looks like an amateur broadcast feed, not a screen recording.

- Country / club flag column in the scoreboard (PNGs in `assets/flags/`).
- Tournament logo on intro + bottom-corner watermark on main.
- Theme presets for the scoreboard (palette per tournament).
- Animated transitions: fade between sets, slide-in for the scoreboard
   panel, "POINT WON" flash on score change.
- Optional commentary track import (mix in user-supplied audio file).
- Optional intro music file in `assets/intro.mp3`.

## v0.5 — Multi-clip + asset library (target: +2 months)

Goal: support a tournament day with multiple matches.

- Project list becomes a tournament list: each tournament has many
   matches.
- Player roster stored once at the tournament level; auto-fill names.
- Cross-match highlight compilation (top 10 rallies of the day).
- Output naming convention: `<tournament>/<round>_<p1>_vs_<p2>.mp4`.

## v1.0 — Standalone distributable

Goal: somebody other than the original author can use this.

- Bundled FFmpeg + standalone Python via PyInstaller / pyoxidizer.
- Single `.exe` that opens the local web app on launch.
- Settings UI (replaces hand-editing `config.json`).
- Auto-update channel.
- First-run wizard: pick GPU, pick output folder, pick fonts.
- Localised UI (Vietnamese / English toggle).

## Stretch / research

These are ideas worth exploring once the core flow is stable. Not
committed.

- **Auto-rally detection.** Train a small audio classifier on the
   distinctive paddle / ball impact pattern to suggest highlight cuts
   automatically. Operator just confirms.
- **Auto-score from video.** OCR a real-world physical scoreboard if one
   is in frame. Reduces operator load to "watch and confirm".
- **Multi-camera composition.** Two angles + automatic cuts based on
   ball-side detection.
- **Live streaming output.** RTMP push of the rendered feed to YouTube /
   Facebook with the scoreboard burned in real-time.
- **Cloud render fallback.** If the local GPU is busy, queue jobs to a
   remote NVENC machine.

## Non-goals (deliberately out of scope)

- Multi-user / multi-tenant editing. This is a single-operator tool.
- General-purpose video editor. We optimise for one workflow:
   table-tennis match recap.
- Mobile app. Browser UI is enough; the heavy lifting is on the
   operator's desktop GPU.
- Cloud upload of source video. Files stay local; uploads are the user's
   choice via the OS share sheet.
