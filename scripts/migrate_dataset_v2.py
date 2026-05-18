"""
One-shot migration: groundtruth schema v1 → v2.

v1 used misleading field names (`rally_segments` for what were actually
post-trim kept chunks, `stats.rally_count` for kept-segment count, etc.).
v2 renames those to `kept_segments` / `kept_segment_count` and adds
real-rally estimates derived from score events.

Walks `dataset/`:
- Reads each entry's `project.json` (verbatim snapshot taken at render
  time — does not require source video to still be on disk).
- Reads existing `groundtruth.json` for source_video metadata + the
  original `exported_at` timestamp.
- Re-derives groundtruth via `build_groundtruth()`.
- Re-derives `notes.md` via `build_notes_md()`.
- Rebuilds `manifest.json` from scratch, preserving the original
  `archived_at` / `source_link_method` / `output_link_method` per entry
  (those are not derivable from groundtruth and only live in the old
  manifest).

Idempotent: re-running on already-v2 entries is a no-op rewrite (the
builder produces deterministic output from the same project data).

Usage:
    python scripts/migrate_dataset_v2.py [--dry-run]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.dataset import build_notes_md
from backend.groundtruth import build_groundtruth
from backend.models import ProjectData


def migrate_entry(entry_dir: Path, old_manifest_entry: dict | None) -> dict:
    """Migrate one dataset entry in place. Returns the rebuilt manifest
    entry (caller appends to fresh manifest)."""
    project_path = entry_dir / "project.json"
    gt_path = entry_dir / "groundtruth.json"
    notes_path = entry_dir / "notes.md"

    if not project_path.exists():
        raise RuntimeError(f"{entry_dir}: project.json missing — cannot migrate")
    if not gt_path.exists():
        raise RuntimeError(f"{entry_dir}: groundtruth.json missing — cannot migrate")

    project_dict = json.loads(project_path.read_text(encoding="utf-8"))
    project = ProjectData.model_validate(project_dict)

    old_gt = json.loads(gt_path.read_text(encoding="utf-8"))
    source_video = old_gt.get("source_video") or {}
    exported_at = old_gt.get("exported_at") or ""

    # Rebuild groundtruth with v2 schema.
    new_gt = build_groundtruth(project, source_video, exported_at=exported_at)
    gt_path.write_text(
        json.dumps(new_gt, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Rebuild notes.md. archived_at + project_name come from the old
    # manifest entry when available, fall back to slug parsing.
    if old_manifest_entry:
        archived_at = (old_manifest_entry.get("archived_at") or "").replace("T", " ")
        project_name = old_manifest_entry.get("project_name") or _parse_project_name(entry_dir.name)
    else:
        archived_at = _parse_archived_at(entry_dir.name)
        project_name = _parse_project_name(entry_dir.name)

    notes_md = build_notes_md(
        project, new_gt,
        archived_at=archived_at,
        project_name=project_name,
    )
    notes_path.write_text(notes_md, encoding="utf-8")

    # Rebuild manifest entry from new groundtruth stats + preserved
    # link methods (those can't be re-derived).
    stats = new_gt["stats"]
    sv = new_gt["source_video"]
    source_name = (old_manifest_entry or {}).get("source_video_name") or _guess_source_name(entry_dir)

    return {
        "slug": entry_dir.name,
        "archived_at": (old_manifest_entry or {}).get("archived_at") or _parse_archived_iso(entry_dir.name),
        "project_name": project_name,
        "source_video_name": source_name,
        "source_duration_sec": float(sv.get("duration_sec", 0.0)),
        "kept_segment_count": int(stats["kept_segment_count"]),
        "real_rally_count": int(stats["real_rally_count"]),
        "avg_real_rally_seconds": float(stats["avg_real_rally_seconds"]),
        "dead_time_pct": float(stats["dead_time_pct"]),
        "source_link_method": (old_manifest_entry or {}).get("source_link_method", "unknown"),
        "output_link_method": (old_manifest_entry or {}).get("output_link_method", "unknown"),
    }


def _parse_project_name(slug: str) -> str:
    """Slug = `<project>_YYYYMMDD_HHMMSS`. Strip trailing timestamp."""
    parts = slug.rsplit("_", 2)
    if len(parts) == 3 and len(parts[1]) == 8 and len(parts[2]) == 6:
        return parts[0]
    return slug


def _parse_archived_at(slug: str) -> str:
    parts = slug.rsplit("_", 2)
    if len(parts) == 3 and len(parts[1]) == 8 and len(parts[2]) == 6:
        d, t = parts[1], parts[2]
        return f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]}"
    return ""


def _parse_archived_iso(slug: str) -> str:
    parts = slug.rsplit("_", 2)
    if len(parts) == 3 and len(parts[1]) == 8 and len(parts[2]) == 6:
        d, t = parts[1], parts[2]
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}T{t[:2]}:{t[2:4]}:{t[4:6]}"
    return ""


def _guess_source_name(entry_dir: Path) -> str:
    """Fallback when manifest didn't record the original filename — read
    the entry's `source.*` symlink/copy and report its real name (which
    we lost as we renamed it to `source<ext>`)."""
    for f in entry_dir.glob("source.*"):
        return f.name
    return ""


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    dataset_root = ROOT / "dataset"
    if not dataset_root.exists():
        print(f"No dataset/ at {dataset_root} — nothing to migrate.")
        return 0

    old_manifest_path = dataset_root / "manifest.json"
    old_manifest: dict = {"version": 1, "entries": []}
    if old_manifest_path.exists():
        try:
            old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"Old manifest unparseable; proceeding without it.")
    old_entries_by_slug = {
        e.get("slug"): e for e in old_manifest.get("entries", []) if e.get("slug")
    }

    entry_dirs = sorted(p for p in dataset_root.iterdir() if p.is_dir())
    print(f"Found {len(entry_dirs)} entries in {dataset_root}")
    if dry_run:
        for d in entry_dirs:
            print(f"  - {d.name}  (would migrate)")
        return 0

    new_entries: list[dict] = []
    for entry_dir in entry_dirs:
        try:
            new_entry = migrate_entry(entry_dir, old_entries_by_slug.get(entry_dir.name))
            new_entries.append(new_entry)
            stats = json.loads((entry_dir / "groundtruth.json").read_text(encoding="utf-8"))["stats"]
            print(
                f"  OK {entry_dir.name}: "
                f"kept={stats['kept_segment_count']}, "
                f"rallies={stats['real_rally_count']}, "
                f"avg={stats['avg_real_rally_seconds']:.1f}s"
            )
        except Exception as e:
            print(f"  FAIL {entry_dir.name}: {e}")

    new_manifest = {"version": 1, "entries": new_entries}
    tmp = old_manifest_path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(new_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(old_manifest_path)
    print(f"Wrote new manifest with {len(new_entries)} entries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
