"""Build the auto-score labeled corpus (AUTO_SCORE_PLAN Phase 0 step 1).

Merges two label sources into one JSONL index under
dataset/auto_score_corpus/corpus.jsonl:

1. dataset/<slug>/groundtruth.json score_events — operator manual
   scoring, GOLD winner labels (the burned-in scoreboard was reviewed
   in the rendered output).
2. dataset/attempt1/reviewed_matches/match_vinh_001 — 71 operator-
   reviewed rallies from the failed scoreboard_tool project (winner +
   11-class taxonomy + last_hitter; labels are near/far a|b, NOT
   P1/P2).

v1 is INDEX-ONLY by design: no clips are cut. Each record carries a
video path + time window; downstream spikes extract frames on demand.
This keeps the builder instant and idempotent, and avoids ~600 NVENC
encodes nobody consumes directly. (The plan text says "cut clips" —
deliberate deviation, recorded here and in TODO.md.)

Dedup: the same source video can be archived under several slugs
(re-renders of one match). All slugs sharing a source_video_name are
ONE match; we keep the slug with the most score events (tiebreak:
latest archived_at). Without this, identical rallies leak across the
train/held-out split.

Held-out: PINNED by explicit match id (never hash order — hash order
would rotate membership as new matches arrive). Held-out matches are
eval-only forever: never train, never tune.

Usage:
    python scripts/auto_score_spike/build_corpus.py          # build
    python scripts/auto_score_spike/build_corpus.py --check  # verify only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = ROOT / "dataset"
ATTEMPT1_MATCH = DATASET_DIR / "attempt1" / "reviewed_matches" / "match_vinh_001"
OUT_DIR = DATASET_DIR / "auto_score_corpus"
OUT_JSONL = OUT_DIR / "corpus.jsonl"

# Clip window around a score event (seconds). The event timestamp is
# the operator's key press ~0.5-2 s AFTER the rally ends; -6 reaches
# back into the rally's final exchanges, +6 covers post-rally
# behaviour (loser fetches the ball, walk-away) which may carry more
# winner signal than the rally itself (plan section 4).
WINDOW_BEFORE_S = 6.0
WINDOW_AFTER_S = 6.0

# Eval-only forever. One singles + one doubles so the held-out set
# represents both deployment modes. Never remove entries; only add.
HELD_OUT_MATCH_IDS = {
    "0510_HoangHuuHa_1-3.MP4",
    "0402_ThoiThao_vs_LoiPhuong_3-2.MP4",
}


def load_manifest() -> list[dict]:
    manifest = json.loads((DATASET_DIR / "manifest.json").read_text(encoding="utf-8"))
    return manifest["entries"]


def dedupe_entries(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """One entry per source video: most score events wins, then latest
    archived_at. Returns (kept, dropped)."""
    def event_count(entry: dict) -> int:
        gt = json.loads(
            (DATASET_DIR / entry["slug"] / "groundtruth.json").read_text(encoding="utf-8")
        )
        return len(gt["project"]["score_events"])

    by_video: dict[str, dict] = {}
    dropped: list[dict] = []
    counts = {e["slug"]: event_count(e) for e in entries}
    for entry in sorted(entries, key=lambda e: (counts[e["slug"]], e["archived_at"])):
        prev = by_video.get(entry["source_video_name"])
        if prev is not None:
            dropped.append(prev)
        by_video[entry["source_video_name"]] = entry
    return list(by_video.values()), dropped


def find_source_video(slug: str) -> Path:
    slug_dir = DATASET_DIR / slug
    hits = [p for p in slug_dir.iterdir() if p.stem == "source"]
    if len(hits) != 1:
        raise FileNotFoundError(f"{slug}: expected exactly one source.<ext>, got {hits}")
    return hits[0]


def dataset_records(entries: list[dict]) -> list[dict]:
    records = []
    for entry in entries:
        slug = entry["slug"]
        match_id = entry["source_video_name"]
        gt = json.loads((DATASET_DIR / slug / "groundtruth.json").read_text(encoding="utf-8"))
        info = gt["project"]["info"]
        duration = gt["source_video"]["duration_sec"]
        video = find_source_video(slug)
        split = "held_out" if match_id in HELD_OUT_MATCH_IDS else "train"
        events = sorted(gt["project"]["score_events"], key=lambda e: e["timestamp"])
        sets_before = [0, 0]
        for idx, ev in enumerate(events):
            t = ev["timestamp"]
            records.append({
                "id": f"{match_id}#e{idx:03d}",
                "label_source": "dataset",
                "match_id": match_id,
                "slug": slug,
                "video": video.relative_to(ROOT).as_posix(),
                "t_event": round(t, 3),
                "window_start": round(max(0.0, t - WINDOW_BEFORE_S), 3),
                "window_end": round(min(duration, t + WINDOW_AFTER_S), 3),
                "who": ev["who"],  # 1|2 == P1|P2 (NOT near/far; mapping per set unknown yet)
                "score_after": [ev["p1_score"], ev["p2_score"]],
                "sets_after": [ev["p1_set"], ev["p2_set"]],
                # 0-based index of the set this point was played IN —
                # derived from the state BEFORE the event (the after-
                # event cache of a match-ending point sums to the
                # final set total, which is not a set index).
                "set_index": sets_before[0] + sets_before[1],
                "match_type": info["match_type"],
                "best_of": info["best_of"],
                "split": split,
            })
            sets_before = [ev["p1_set"], ev["p2_set"]]
    return records


def attempt1_records() -> list[dict]:
    records = []
    for set_dir in sorted(ATTEMPT1_MATCH.glob("set_0*")):
        labels = set_dir / "labels.jsonl"
        if not labels.is_file():
            continue
        for line in labels.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue  # set_04 has one blank line (README_ATTEMPT1)
            row = json.loads(line)
            clip = set_dir / row["clip_relpath"]
            if not clip.is_file():
                print(f"  WARN missing clip {clip} -- skipped")
                continue
            records.append({
                "id": row["record_id"],
                "label_source": "attempt1",
                "match_id": "match_vinh_001",
                "video": clip.relative_to(ROOT).as_posix(),  # self-contained clip
                "t_event": None,  # per-set source video was deleted; clip IS the window
                "window_start": None,
                "window_end": None,
                # near/far convention (player_a/b as seen from tripod),
                # NOT P1/P2 -- see dataset/attempt1/README_ATTEMPT1.md
                "winner_ab": row["winner"],
                "taxonomy": row["taxonomy"],
                "last_hitter": row["last_hitter"],
                "set_index": int(row["set_id"].split("_")[1]) - 1,
                "match_type": "single",
                "best_of": 5,
                "split": "train",
            })
    return records


def build() -> dict:
    entries = load_manifest()
    kept, dropped = dedupe_entries(entries)
    ds = dataset_records(kept)
    a1 = attempt1_records()
    all_records = ds + a1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_JSONL.open("w", encoding="utf-8", newline="\n") as f:
        for rec in all_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    summary = {
        "total": len(all_records),
        "dataset_events": len(ds),
        "attempt1_rallies": len(a1),
        "unique_matches": len({r["match_id"] for r in all_records}),
        "dropped_duplicate_slugs": [d["slug"] for d in dropped],
        "per_split": {
            s: len([r for r in all_records if r["split"] == s])
            for s in ("train", "held_out")
        },
        "held_out_matches": sorted(HELD_OUT_MATCH_IDS),
    }
    return summary


def check() -> int:
    if not OUT_JSONL.is_file():
        print(f"MISSING {OUT_JSONL}")
        return 1
    bad = 0
    for n, line in enumerate(OUT_JSONL.read_text(encoding="utf-8").splitlines(), 1):
        rec = json.loads(line)
        if not (ROOT / rec["video"]).is_file():
            print(f"  line {n}: missing video {rec['video']}")
            bad += 1
    print(f"checked {n} records, {bad} broken video paths")
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify existing corpus only")
    args = parser.parse_args()

    if args.check:
        return check()

    summary = build()
    print(f"wrote {OUT_JSONL.relative_to(ROOT)}")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
