# TODO

## RESUME POINTER 2026-06-05 — all in-flight work shipped; only (A) fresh-match test open

State of `v3-dev` (ahead of `origin` by 7 commits, not pushed):

- `14c5db7` Scoreboard inter-set recap — render-tested + committed.
- `22b5f08` 9 new player avatars.
- `4ec9012` Auto Trim detect speed 2.1× → 6.2× realtime (byte-identical
  `-hwaccel cuda` GPU-decode fix).
- `2f60a79` + `7a1363f` Docs updates (recap verified, this resume pointer).
- `9b1624e` Highlights per-row clip export + Preview Cut transport
  (details in [HISTORY.md](HISTORY.md)).

No feature is mid-implementation. `ROADMAP.md` has nothing new planned.

**Only open item — operator-driven:** (A) run Auto Trim on a fresh
match outside the 3 PHASE0_REPORT spike entries to measure real recall
on truly-unseen venue + audio. Needs a new recording; the assistant
can only analyse the result, not produce the input.

Lower-priority / deferred: Phase 5 (headless auto-trim inside the
Render button) and Phase 6 (YOLOv8-pose escalation) — both gated on (A)
producing enough confidence in detector reliability first.

If `verify_rally_detector.py` is ever used as a gate again, refresh its
`EXPECTED` recall targets — they predate the `post_match_keep_s=30`
handshake-keep feature and now read as false MISSes.

---

## Shipped history

All completed phase reports, SHIPPED/COMMITTED notes, and superseded
plan drafts moved to [HISTORY.md](HISTORY.md) on 2026-06-05 to keep
this file actionable. The lessons / "what NOT to do" notes live there.

---

## Backlog cũ chưa làm

- 🟡 Country/club flags cạnh tên player trong scoreboard
- 🟡 Tournament logo trên intro card
- 🟡 Theme presets scoreboard per-tournament
- 🔴 Audio leveling/ducking trong slow-mo (atempo=0.5 hơi robot)
- 🟢 Pin Python version trong pyproject.toml
- 🟡 `--dry-run` mode cho renderer
- 🟡 GitHub Actions lint
- 🟢 Bundle font trong assets/ cho intro drawtext fallback
- 🟢 Unit test apostrophe trong .ass escape
- 🟡 NVDEC session limit investigation
