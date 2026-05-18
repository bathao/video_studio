"""Tests for the dataset archive.

`archive_to_dataset` mirrors a successful render into
`dataset/<slug>/`. Pure-logic tests: we mock `config.dataset_dir` to a
tmp path and provide stand-in source / output / sidecar files. The
hardlink path is the same as on-disk Windows behaviour — `os.link()` on
the same volume — and we fall through to copy when it fails."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from backend import dataset
from backend.models import (
    Highlight,
    ProjectData,
    ProjectInfo,
    ScoreEvent,
    TrimSegment,
)


def _make_fake_render(tmp_path: Path, project_name: str = "match01"):
    """Build a stand-in for everything `archive_to_dataset` reads from
    disk: source video, rendered output, groundtruth sidecar, refframe."""
    src = tmp_path / "match01_source.mp4"
    src.write_bytes(b"FAKE-VIDEO-BYTES")
    output_mp4 = tmp_path / "out.mp4"
    output_mp4.write_bytes(b"FAKE-OUTPUT-BYTES")

    # Sidecars next to output_mp4 (same naming convention export_groundtruth uses)
    gt = {
        "version": 2,
        "source_video": {"duration_sec": 100.0, "fps": 30.0},
        "kept_segments": [
            {"start": 0.0, "end": 40.0, "duration": 40.0, "rally_count_estimate": 3},
            {"start": 60.0, "end": 100.0, "duration": 40.0, "rally_count_estimate": 2},
        ],
        "stats": {
            "kept_segment_count": 2,
            "total_kept_duration": 80.0,
            "total_dead_time": 20.0,
            "dead_time_pct": 0.2,
            "real_rally_count": 5,
            "avg_real_rally_seconds": 16.0,
        },
    }
    (tmp_path / "out.groundtruth.json").write_text(
        json.dumps(gt), encoding="utf-8",
    )
    (tmp_path / "out.refframe.png").write_bytes(b"PNG")

    project = ProjectData(
        info=ProjectInfo(p1="Alice", p2="Bob"),
        trim_segments=[TrimSegment(start=10.0, end=20.0)],
        highlights=[Highlight(start=30.0, end=35.0)],
        score_events=[ScoreEvent(timestamp=25.0, who=1)],
    )
    ctx = SimpleNamespace(
        src=src,
        plan=SimpleNamespace(project=project, project_name=project_name),
        state=SimpleNamespace(message=""),
    )
    return ctx, output_mp4


def _setup_dataset_dir(monkeypatch, tmp_path: Path) -> Path:
    """Redirect `dataset.config.dataset_dir` → tmp_path/dataset for the
    test. `archive_to_dataset` only reads that one attribute, so we
    swap the whole `config` reference in the module for a stub instead
    of trying to override a read-only @property."""
    dataset_root = tmp_path / "dataset"
    dataset_root.mkdir()
    monkeypatch.setattr(
        dataset, "config", SimpleNamespace(dataset_dir=dataset_root),
    )
    return dataset_root


def test_archive_creates_entry_with_all_files(tmp_path, monkeypatch):
    dataset_root = _setup_dataset_dir(monkeypatch, tmp_path)
    ctx, output_mp4 = _make_fake_render(tmp_path)

    dataset.archive_to_dataset(ctx, output_mp4)

    entries = list(dataset_root.iterdir())
    # manifest.json + 1 entry folder
    entry_dirs = [p for p in entries if p.is_dir()]
    assert len(entry_dirs) == 1
    entry = entry_dirs[0]

    assert (entry / "source.mp4").exists()
    assert (entry / "output.mp4").exists()
    assert (entry / "project.json").exists()
    assert (entry / "groundtruth.json").exists()
    assert (entry / "refframe.png").exists()
    assert (entry / "notes.md").exists()

    # source.mp4 content matches original (hardlink or copy — both work)
    assert (entry / "source.mp4").read_bytes() == b"FAKE-VIDEO-BYTES"
    assert (entry / "output.mp4").read_bytes() == b"FAKE-OUTPUT-BYTES"

    # project.json is the verbatim snapshot
    pj = json.loads((entry / "project.json").read_text(encoding="utf-8"))
    assert pj["info"]["p1"] == "Alice"
    assert pj["trim_segments"][0]["start"] == 10.0


def test_manifest_entry_appended(tmp_path, monkeypatch):
    dataset_root = _setup_dataset_dir(monkeypatch, tmp_path)
    ctx, output_mp4 = _make_fake_render(tmp_path, project_name="match01")

    dataset.archive_to_dataset(ctx, output_mp4)

    manifest = json.loads(
        (dataset_root / "manifest.json").read_text(encoding="utf-8"),
    )
    assert manifest["version"] == dataset.DATASET_MANIFEST_VERSION
    assert len(manifest["entries"]) == 1
    e = manifest["entries"][0]
    assert e["project_name"] == "match01"
    assert e["source_video_name"] == "match01_source.mp4"
    # Stats pulled from groundtruth.json (v2 schema)
    assert e["kept_segment_count"] == 2
    assert e["real_rally_count"] == 5
    assert e["avg_real_rally_seconds"] == 16.0
    assert e["dead_time_pct"] == 0.2
    assert e["source_duration_sec"] == 100.0
    # Slug starts with project name and has timestamp suffix
    assert e["slug"].startswith("match01_")


def test_manifest_appends_across_calls(tmp_path, monkeypatch):
    """Two archives in sequence → manifest has 2 entries, not 1 (or 0
    if read-modify-write was broken)."""
    dataset_root = _setup_dataset_dir(monkeypatch, tmp_path)
    ctx1, output1 = _make_fake_render(tmp_path, "match01")
    dataset.archive_to_dataset(ctx1, output1)

    # Second render — different source / output files
    src2 = tmp_path / "match02_source.mp4"
    src2.write_bytes(b"v2")
    output2 = tmp_path / "out2.mp4"
    output2.write_bytes(b"o2")
    (tmp_path / "out2.groundtruth.json").write_text("{}", encoding="utf-8")
    (tmp_path / "out2.refframe.png").write_bytes(b"PNG")
    ctx2 = SimpleNamespace(
        src=src2,
        plan=SimpleNamespace(project=ProjectData(), project_name="match02"),
        state=SimpleNamespace(message=""),
    )
    dataset.archive_to_dataset(ctx2, output2)

    manifest = json.loads(
        (dataset_root / "manifest.json").read_text(encoding="utf-8"),
    )
    assert len(manifest["entries"]) == 2
    names = {e["project_name"] for e in manifest["entries"]}
    assert names == {"match01", "match02"}


def test_slug_sanitises_vietnamese_and_spaces(tmp_path, monkeypatch):
    """Project names with spaces / diacritics / punctuation collapse to
    ASCII-safe slugs so the folder name is fs-portable."""
    dataset_root = _setup_dataset_dir(monkeypatch, tmp_path)
    ctx, output_mp4 = _make_fake_render(tmp_path, project_name="Trận Nguyễn vs Trần (chung kết)")

    dataset.archive_to_dataset(ctx, output_mp4)

    entry_dirs = [p for p in dataset_root.iterdir() if p.is_dir()]
    assert len(entry_dirs) == 1
    name = entry_dirs[0].name
    # No raw diacritics / spaces / parens leak through
    assert " " not in name
    assert "(" not in name
    assert ")" not in name
    # Sanitiser collapses consecutive bad chars to a single _
    assert "__" not in name.split("_20")[0]  # before the timestamp


def test_slug_collision_appends_counter(tmp_path, monkeypatch):
    """Two renders that resolve to the same timestamp slug (forced by
    pre-creating the target dir) — the second one becomes <slug>_2."""
    dataset_root = _setup_dataset_dir(monkeypatch, tmp_path)
    ctx, output_mp4 = _make_fake_render(tmp_path)

    dataset.archive_to_dataset(ctx, output_mp4)
    # Second archive in the same second would otherwise collide.
    dataset.archive_to_dataset(ctx, output_mp4)

    entry_dirs = sorted(
        p for p in dataset_root.iterdir() if p.is_dir()
    )
    assert len(entry_dirs) == 2
    # One of them ends with "_2"
    assert any(p.name.endswith("_2") for p in entry_dirs)


def test_archive_does_not_raise_on_missing_source(tmp_path, monkeypatch):
    """Source video deleted between render and archive: must NOT raise.
    The error message accumulates in RenderState.message."""
    _setup_dataset_dir(monkeypatch, tmp_path)
    ctx, output_mp4 = _make_fake_render(tmp_path)
    ctx.src.unlink()  # source gone

    dataset.archive_to_dataset(ctx, output_mp4)

    assert "Dataset archive failed" in ctx.state.message


def test_archive_does_not_raise_on_corrupted_manifest(tmp_path, monkeypatch):
    """A garbled manifest.json must NOT crash the next archive — the
    bad file is moved aside to .bak and a fresh one is created."""
    dataset_root = _setup_dataset_dir(monkeypatch, tmp_path)
    (dataset_root / "manifest.json").write_text(
        "this is not json {{{", encoding="utf-8",
    )

    ctx, output_mp4 = _make_fake_render(tmp_path)
    dataset.archive_to_dataset(ctx, output_mp4)

    # Fresh manifest valid + 1 entry, bad one preserved
    fresh = json.loads(
        (dataset_root / "manifest.json").read_text(encoding="utf-8"),
    )
    assert len(fresh["entries"]) == 1
    assert (dataset_root / "manifest.bak").exists()


def test_hardlink_falls_back_to_copy(tmp_path, monkeypatch):
    """When os.link raises (cross-volume / unsupported FS) the archive
    falls back to shutil.copy2 transparently. Manifest records which
    branch was taken so downstream tools can tell."""
    dataset_root = _setup_dataset_dir(monkeypatch, tmp_path)
    ctx, output_mp4 = _make_fake_render(tmp_path)

    def _no_link(_src, _dst):
        raise OSError("cross-volume link not supported (simulated)")

    monkeypatch.setattr(dataset.os, "link", _no_link)

    dataset.archive_to_dataset(ctx, output_mp4)

    manifest = json.loads(
        (dataset_root / "manifest.json").read_text(encoding="utf-8"),
    )
    assert manifest["entries"][0]["source_link_method"] == "copy"
    assert manifest["entries"][0]["output_link_method"] == "copy"


def test_notes_md_is_auto_filled(tmp_path, monkeypatch):
    """notes.md must contain match identity, manual-label counts, and
    derived distributions — never empty. The operator should not have
    to touch it by hand."""
    _setup_dataset_dir(monkeypatch, tmp_path)
    ctx, output_mp4 = _make_fake_render(tmp_path, project_name="Spring Open R1")
    # Bump up the score so the final-score line surfaces something
    # specific to assert against.
    ctx.plan.project.score_events = [
        ScoreEvent(timestamp=10.0, who=1, p1_score=1, p2_score=0,
                   p1_set=0, p2_set=0),
        ScoreEvent(timestamp=300.0, who=2, p1_score=8, p2_score=11,
                   p1_set=1, p2_set=2),
    ]

    dataset.archive_to_dataset(ctx, output_mp4)

    entry = next(p for p in (tmp_path / "dataset").iterdir() if p.is_dir())
    notes = (entry / "notes.md").read_text(encoding="utf-8")

    assert notes.strip()  # never empty on success
    assert "Spring Open R1" in notes
    assert "Alice" in notes and "Bob" in notes
    assert "singles" in notes  # format line
    assert "Final: Alice 1–2 Bob" in notes  # set-level final
    assert "Manual labels" in notes
    assert "Trim segments: 1" in notes
    assert "Highlights: 1" in notes
    assert "Score events: 2" in notes


# ---------- pure build_notes_md tests -----------------------------------


def _basic_project(**kw):
    info_defaults = dict(p1="Alice", p2="Bob", best_of=5)
    info_defaults.update(kw.pop("info", {}))
    return ProjectData(
        info=ProjectInfo(**info_defaults),
        trim_segments=kw.get("trim_segments", []),
        highlights=kw.get("highlights", []),
        score_events=kw.get("score_events", []),
    )


def test_notes_singles_minimal():
    """Empty groundtruth + no trims/highlights/events: notes still
    builds with match identity and zero-count manual labels."""
    project = _basic_project()
    md = dataset.build_notes_md(project, {}, project_name="match01")
    assert md.startswith("# match01")
    assert "Players: Alice vs Bob" in md
    assert "Format: singles, best of 5" in md
    assert "Trim segments: 0" in md
    assert "Highlights: 0" in md


def test_notes_always_includes_caveats_section():
    """Caveat about manual trims being a subjective / noisy subset must
    be present in EVERY entry — even when there are no trims at all."""
    md = dataset.build_notes_md(_basic_project(), {}, project_name="m")
    assert "## Caveats" in md
    assert "subjective" in md.lower()
    assert "noisy" in md.lower()
    # No fixed-threshold language — earlier framing of ">15s" was wrong.
    assert "15 s" not in md
    assert "NOT a reliable ground truth" in md


def test_notes_doubles_uses_team_format():
    project = _basic_project(info={
        "match_type": "double",
        "p3": "Carol", "p4": "Dan",
        "p1_team": "Red", "p2_team": "Blue",
    })
    md = dataset.build_notes_md(project, {}, project_name="m")
    assert "Teams: Alice & Carol (Red) vs Bob & Dan (Blue)" in md
    assert "doubles" in md


def test_notes_final_score_from_last_event():
    project = _basic_project(score_events=[
        ScoreEvent(timestamp=10.0, who=1, p1_score=1, p1_set=0, p2_set=0),
        ScoreEvent(timestamp=200.0, who=1, p1_score=11, p1_set=3, p2_set=1),
    ])
    md = dataset.build_notes_md(project, {}, project_name="m")
    assert "Final: Alice 3–1 Bob" in md
    assert "last game 11–0" in md


def test_notes_includes_source_video_section():
    gt = {
        "source_video": {
            "path": "videos/match01.mp4",
            "duration_sec": 3725.0,  # 1:02:05
            "fps": 29.97,
            "width": 1920,
            "height": 1080,
            "has_audio": True,
        }
    }
    md = dataset.build_notes_md(_basic_project(), gt, project_name="m")
    assert "## Source video" in md
    assert "match01.mp4" in md
    assert "1:02:05" in md
    assert "1920×1080" in md
    assert "29.97fps" in md
    assert "Audio: yes" in md


def test_notes_kept_segment_breakdown_and_real_rallies():
    """Kept segments are post-trim chunks containing multiple real
    rallies. Notes.md must surface BOTH: the breakdown table (per chunk
    with rally_count_estimate) AND the real rally aggregate."""
    gt = {
        "kept_segments": [
            {"start": 0, "end": 10, "duration": 10.0, "rally_count_estimate": 1},
            {"start": 20, "end": 35, "duration": 15.0, "rally_count_estimate": 2},
            {"start": 40, "end": 100, "duration": 60.0, "rally_count_estimate": 5},
        ],
        "stats": {
            "kept_segment_count": 3,
            "dead_time_pct": 0.15,
            "total_kept_duration": 85.0,
            "total_dead_time": 15.0,
            "real_rally_count": 8,
            "avg_real_rally_seconds": 10.625,
        },
    }
    project = _basic_project(
        trim_segments=[
            TrimSegment(start=10, end=20),
            TrimSegment(start=35, end=40),
        ],
        score_events=[ScoreEvent(timestamp=t * 1.0, who=1) for t in range(8)],
    )
    md = dataset.build_notes_md(project, gt, project_name="m")

    # Misleading "Rally duration" heading must NOT appear.
    assert "## Rally duration" not in md

    # Real rallies section is the headline insight for spike tuning.
    assert "## Real rallies" in md
    assert "Count: 8" in md
    assert "Average duration: 10.6s/rally" in md

    # Kept segment breakdown table shows per-chunk rally count estimate.
    assert "## Kept segments" in md
    assert "est. rallies inside" in md
    assert "NOT real rallies" in md  # in the heading caveat
    # Row 3: start 0:40, end 1:40, duration 1:00, 5 rallies
    assert "| 3 | 0:40 | 1:40 |" in md
    assert "| 5 |" in md  # 5 rallies in row 3

    # Dead-gap section still present (renamed to "operator-marked only").
    assert "## Dead time gap" in md
    assert "operator-marked only" in md
    assert "| 2 |" in md  # 2 trim gaps


def test_notes_highlight_list_with_timestamps():
    project = _basic_project(highlights=[
        Highlight(start=65.0, end=78.0, label="great rally"),
        Highlight(start=3600.0, end=3612.0),  # >1h → h:mm:ss format
    ])
    md = dataset.build_notes_md(project, {}, project_name="m")
    assert "## Highlights (operator-marked)" in md
    assert "1:05 → 1:18 (13.0s) — great rally" in md
    assert "1:00:00 → 1:00:12 (12.0s)" in md


def test_fmt_hms_breaks_at_one_hour():
    assert dataset._fmt_hms(0) == "0:00"
    assert dataset._fmt_hms(65) == "1:05"
    assert dataset._fmt_hms(3599) == "59:59"
    assert dataset._fmt_hms(3600) == "1:00:00"
    assert dataset._fmt_hms(3725) == "1:02:05"


def test_dist_table_percentiles():
    # 5 values: min=1, p25=2, median=3, p75=4, max=5
    table = dataset._dist_table([3.0, 1.0, 4.0, 5.0, 2.0])
    assert "| 5 | 1.0 | 2.0 | 3.0 | 4.0 | 5.0 | 3.0 |" in table


def test_dist_table_single_value():
    """One-element list: every percentile is the same value, no
    division-by-zero or index error."""
    table = dataset._dist_table([7.5])
    assert "| 1 | 7.5 | 7.5 | 7.5 | 7.5 | 7.5 | 7.5 |" in table


def test_archive_runs_after_groundtruth_export_missing_sidecars(tmp_path, monkeypatch):
    """If groundtruth.json or refframe.png is missing in output/ the
    archive still succeeds (warnings surface in message) — the source +
    output + project.json half is still useful."""
    dataset_root = _setup_dataset_dir(monkeypatch, tmp_path)
    ctx, output_mp4 = _make_fake_render(tmp_path)
    (tmp_path / "out.groundtruth.json").unlink()
    (tmp_path / "out.refframe.png").unlink()

    dataset.archive_to_dataset(ctx, output_mp4)

    entry_dirs = [p for p in dataset_root.iterdir() if p.is_dir()]
    assert len(entry_dirs) == 1
    entry = entry_dirs[0]
    assert (entry / "source.mp4").exists()
    assert (entry / "output.mp4").exists()
    assert (entry / "project.json").exists()
    assert not (entry / "groundtruth.json").exists()
    assert not (entry / "refframe.png").exists()
    assert "missing in output/" in ctx.state.message
