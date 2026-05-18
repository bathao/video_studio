"""
Auto-archive every successful render into a structured dataset entry.

Operator works manually in the GUI as usual; every successful render
gets mirrored into `dataset/<slug>/` so the same activity that produces
the final video also accumulates a labelled corpus for the auto-trim
CV pipeline. Zero workflow change required.

Folder layout:

  dataset/
    manifest.json                 ← index appended per render (used by
                                    downstream spike / eval scripts to
                                    enumerate entries without scanning)
    <slug>/                       ← slug = "<sanitised_project>_<YYYYMMDD_HHMMSS>"
      source<.ext>                ← hardlink to the project's source
                                    video; copy fallback when hardlink
                                    fails (cross-volume, ReFS, etc.).
                                    Extension preserved (.mp4 / .mov /
                                    .avi) so downstream tools see the
                                    real container.
      output.mp4                  ← hardlink to the rendered output.mp4
      project.json                ← verbatim snapshot of project state
                                    at render time (re-loadable in the
                                    GUI after fixing video_file path)
      groundtruth.json            ← copied from output/ (derived rally
                                    segments + stats; see groundtruth.py)
      refframe.png                ← copied from output/ (mid-rally frame)
      notes.md                    ← empty stub for operator-written
                                    context: setup quirks, lighting, etc.

Best-effort: failures append to RenderState.message but never abort
the render. The mp4 + sidecar in output/ are canonical regardless of
whether the dataset archive succeeds.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .config import config

if TYPE_CHECKING:
    from .renderer import RenderContext

DATASET_MANIFEST_VERSION = 1


def archive_to_dataset(ctx: "RenderContext", output_mp4: Path) -> None:
    """Mirror the just-finished render into `dataset/<slug>/`.

    Reads the sidecar files already written into `output/` by
    `export_groundtruth`, so this MUST be called after that. Appends a
    summary entry to `dataset/manifest.json`. Any failure lands in
    `RenderState.message`; never raises.
    """
    try:
        dataset_root = config.dataset_dir
    except Exception as e:
        _append_msg(ctx, f"Dataset archive skipped (config): {e}")
        return

    project_name = ctx.plan.project_name or "match"
    when = time.time()
    slug = _make_slug(project_name, when)
    entry_dir = _unique_dir(dataset_root, slug)

    try:
        entry_dir.mkdir(parents=True, exist_ok=False)
    except OSError as e:
        _append_msg(ctx, f"Dataset archive failed (mkdir): {e}")
        return

    # source.<ext> — preserve original container extension. Hardlink
    # when possible (same-volume NTFS), copy fallback otherwise.
    src_ext = ctx.src.suffix or ".mp4"
    src_dst = entry_dir / f"source{src_ext}"
    src_link_method = _link_or_copy(ctx.src, src_dst)
    if src_link_method is None:
        _append_msg(ctx, f"Dataset archive failed (link source): {ctx.src}")
        _rmtree_quietly(entry_dir)
        return

    # output.mp4 — same hardlink-or-copy strategy.
    out_dst = entry_dir / "output.mp4"
    out_link_method = _link_or_copy(output_mp4, out_dst)
    if out_link_method is None:
        _append_msg(ctx, f"Dataset archive failed (link output): {output_mp4}")
        _rmtree_quietly(entry_dir)
        return

    # Copy the sidecar files produced by export_groundtruth. Small JSON
    # + PNG — copy2 keeps mtime so downstream scripts can tell which
    # entry the manifest line refers to.
    base = output_mp4.with_suffix("")
    gt_src = base.with_suffix(".groundtruth.json")
    rf_src = base.with_suffix(".refframe.png")
    sidecar_warnings: list[str] = []
    if gt_src.exists():
        try:
            shutil.copy2(gt_src, entry_dir / "groundtruth.json")
        except OSError as e:
            sidecar_warnings.append(f"groundtruth.json copy failed: {e}")
    else:
        sidecar_warnings.append("groundtruth.json missing in output/")
    if rf_src.exists():
        try:
            shutil.copy2(rf_src, entry_dir / "refframe.png")
        except OSError as e:
            sidecar_warnings.append(f"refframe.png copy failed: {e}")
    else:
        sidecar_warnings.append("refframe.png missing in output/")

    # Verbatim project snapshot. Separate from groundtruth.json's
    # embedded project field — this one is directly re-loadable in the
    # GUI (after the operator updates the video_file path to point at
    # source<ext>).
    try:
        project_json = ctx.plan.project.model_dump()
        (entry_dir / "project.json").write_text(
            json.dumps(project_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        sidecar_warnings.append(f"project.json write failed: {e}")

    # Read groundtruth once — feeds both notes.md generation and the
    # manifest stats below. Missing / corrupt groundtruth → notes.md
    # still builds from project state alone, manifest stats default to 0.
    gt_data: dict = {}
    if gt_src.exists():
        try:
            gt_data = json.loads(gt_src.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            gt_data = {}

    # Auto-fill notes.md from project state + groundtruth so the operator
    # never has to write it by hand. Rich enough that a developer (or AI
    # assistant) opening the dataset entry can grok the match, the manual
    # labels, and the rally / dead-gap distributions without running any
    # script.
    archived_at_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))
    try:
        notes_md = build_notes_md(
            ctx.plan.project,
            gt_data,
            archived_at=archived_at_str,
            project_name=project_name,
        )
        (entry_dir / "notes.md").write_text(notes_md, encoding="utf-8")
    except OSError as e:
        sidecar_warnings.append(f"notes.md write failed: {e}")

    # Manifest stats — reuse the gt_data we already parsed.
    kept_segment_count = 0
    real_rally_count = 0
    avg_real_rally_seconds = 0.0
    source_duration = 0.0
    dead_pct = 0.0
    if gt_data:
        try:
            stats = gt_data.get("stats", {})
            kept_segment_count = int(stats.get("kept_segment_count", 0))
            real_rally_count = int(stats.get("real_rally_count", 0))
            avg_real_rally_seconds = float(stats.get("avg_real_rally_seconds", 0.0))
            source_duration = float(gt_data.get("source_video", {}).get("duration_sec", 0.0))
            dead_pct = float(stats.get("dead_time_pct", 0.0))
        except (TypeError, ValueError):
            pass

    entry = {
        "slug": entry_dir.name,
        "archived_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(when)),
        "project_name": project_name,
        "source_video_name": ctx.src.name,
        "source_duration_sec": round(source_duration, 3),
        "kept_segment_count": kept_segment_count,
        "real_rally_count": real_rally_count,
        "avg_real_rally_seconds": round(avg_real_rally_seconds, 3),
        "dead_time_pct": round(dead_pct, 4),
        "source_link_method": src_link_method,
        "output_link_method": out_link_method,
    }
    manifest_warning = _append_manifest(dataset_root / "manifest.json", entry)
    if manifest_warning:
        sidecar_warnings.append(manifest_warning)

    # Surface success + any non-fatal warnings in the status message.
    suffix = f"Archived to dataset/{entry_dir.name}"
    if sidecar_warnings:
        suffix += " (warnings: " + "; ".join(sidecar_warnings) + ")"
    _append_msg(ctx, suffix)


# ---------- notes.md builder ------------------------------------------------


def build_notes_md(
    project,
    gt_data: dict,
    *,
    archived_at: str = "",
    project_name: str = "",
) -> str:
    """Render an auto-filled notes.md from project state + groundtruth.

    Pure: no I/O, no clock reads (caller supplies `archived_at`). Aimed
    at a developer / AI assistant grokking a dataset entry at a glance:
    match identity, manual labels, rally + dead-gap distributions, and
    operator-marked highlights as high-confidence rally examples to
    cross-check against any auto-detected segmentation."""
    info = project.info
    lines: list[str] = []

    title = project_name.strip() or info.tournament.strip() or "match"
    lines.append(f"# {title}")
    if archived_at:
        lines.append(f"_archived {archived_at}_")
    lines.append("")

    # ----- Match -----
    lines.append("## Match")
    if info.tournament and (title != info.tournament):
        lines.append(f"- Tournament: {info.tournament}")
    is_doubles = (info.match_type or "single").lower() == "double"
    if is_doubles:
        team1 = f"{info.p1} & {info.p3}"
        if info.p1_team:
            team1 += f" ({info.p1_team})"
        team2 = f"{info.p2} & {info.p4}"
        if info.p2_team:
            team2 += f" ({info.p2_team})"
        lines.append(f"- Teams: {team1} vs {team2}")
        lines.append(f"- Format: doubles, best of {info.best_of}")
    else:
        p1 = info.p1 + (f" ({info.p1_team})" if info.p1_team else "")
        p2 = info.p2 + (f" ({info.p2_team})" if info.p2_team else "")
        lines.append(f"- Players: {p1} vs {p2}")
        lines.append(f"- Format: singles, best of {info.best_of}")

    # Final score derived from the last score event. Score events are
    # actions, so the last one's cached p1_set/p2_set is the match score
    # and p1_score/p2_score is the final game's running score.
    if project.score_events:
        last = project.score_events[-1]
        lines.append(
            f"- Final: {info.p1} {last.p1_set}–{last.p2_set} {info.p2} "
            f"(last game {last.p1_score}–{last.p2_score})"
        )
    lines.append("")

    # ----- Source video -----
    sv = (gt_data or {}).get("source_video") or {}
    if sv:
        dur = float(sv.get("duration_sec", 0.0))
        lines.append("## Source video")
        if sv.get("path"):
            lines.append(f"- File: {Path(sv['path']).name}")
        lines.append(f"- Duration: {_fmt_hms(dur)} ({dur:.1f}s)")
        if sv.get("width") and sv.get("height"):
            fps = float(sv.get("fps", 0.0))
            lines.append(
                f"- Resolution: {sv['width']}×{sv['height']} @ {fps:.2f}fps"
            )
        lines.append(f"- Audio: {'yes' if sv.get('has_audio') else 'no'}")
        lines.append("")

    # ----- Manual labels -----
    stats = (gt_data or {}).get("stats") or {}
    dead_pct = float(stats.get("dead_time_pct", 0.0))
    dead_total = float(stats.get("total_dead_time", 0.0))
    kept_total = float(stats.get("total_kept_duration", 0.0))
    kept_count = int(stats.get("kept_segment_count", len(project.trim_segments) + 1))
    real_rally_count = int(stats.get("real_rally_count", len(project.score_events)))
    avg_real_rally = float(stats.get("avg_real_rally_seconds", 0.0))

    lines.append("## Manual labels")
    lines.append(
        f"- Trim segments: {len(project.trim_segments)} "
        f"(dead time = {dead_pct * 100:.1f}%, {_fmt_hms(dead_total)} total)"
    )
    lines.append(
        f"- Kept segments: {kept_count} (post-trim chunks; each may "
        f"contain multiple real rallies — {_fmt_hms(kept_total)} total)"
    )
    lines.append(f"- Highlights: {len(project.highlights)}")
    lines.append(f"- Score events: {len(project.score_events)}")
    lines.append("")

    # ----- Real rallies (estimated from score events) -----
    # Each score event marks one rally ending (server / receiver scored).
    # The real rally count is therefore len(score_events), which is what
    # the auto-trim spike's `min_rally_duration` should sit well below.
    if real_rally_count > 0:
        lines.append("## Real rallies (estimated from score events)")
        lines.append(
            f"- Count: {real_rally_count} "
            f"(1 score event ≈ 1 rally ending)"
        )
        lines.append(
            f"- Average duration: {avg_real_rally:.1f}s/rally "
            f"(= total kept / event count)"
        )
        lines.append("")

    # ----- Kept segment breakdown -----
    # Per-chunk rally count is the data the spike actually needs: it
    # tells you how many rallies a chunk contains and therefore how
    # aggressively the detector should split it.
    kept_segments = (gt_data or {}).get("kept_segments") or []
    if kept_segments:
        lines.append("## Kept segments (post-trim chunks; NOT real rallies)")
        lines.append("| # | start | end | duration | est. rallies inside |")
        lines.append("|---|-------|-----|----------|---------------------|")
        for i, seg in enumerate(kept_segments, 1):
            s = float(seg.get("start", 0.0))
            e = float(seg.get("end", 0.0))
            d = float(seg.get("duration", e - s))
            r = int(seg.get("rally_count_estimate", 0))
            lines.append(
                f"| {i} | {_fmt_hms(s)} | {_fmt_hms(e)} | "
                f"{_fmt_hms(d)} ({d:.1f}s) | {r} |"
            )
        lines.append("")

    # ----- Dead time gap distribution -----
    # Long tail here indicates set breaks / timeouts — useful when
    # tuning the auto-trim padding (a 2 min break shouldn't get padded
    # like a 5 s between-point pause).
    dead_durations = [
        float(t.end - t.start)
        for t in project.trim_segments
        if t.end > t.start
    ]
    if dead_durations:
        lines.append("## Dead time gap (seconds — operator-marked only)")
        lines.append(_dist_table(dead_durations))
        lines.append("")

    # ----- Highlights -----
    if project.highlights:
        lines.append("## Highlights (operator-marked)")
        for i, h in enumerate(project.highlights, 1):
            duration = max(0.0, float(h.end) - float(h.start))
            range_str = f"{_fmt_hms(h.start)} → {_fmt_hms(h.end)} ({duration:.1f}s)"
            label = f" — {h.label}" if h.label else ""
            lines.append(f"{i}. {range_str}{label}")
        lines.append("")

    # ----- Caveats (always present) -----
    # Permanent reminder for anyone reading this dataset entry: manual
    # trims are NOT a reliable ground truth in either direction —
    # they're a subjective, non-systematic subset of true dead time, no
    # duration threshold. Operator may include short gaps and miss long
    # ones at will. Don't tune auto-trim toward matching this label set.
    lines.append("## Caveats")
    lines.append(
        "- Manual trims here are a **subjective, noisy** partial label "
        "set: the operator visually scans the recording and trims what "
        "they happen to notice as obviously dead time — no fixed "
        "duration threshold. They can include short gaps and they can "
        "miss long ones (didn't scrub past, distracted, lazy)."
    )
    lines.append(
        "- These trims are therefore NOT a reliable ground truth in "
        "either direction. Auto-trim is expected to be strictly more "
        "thorough; it should NOT be tuned to match this label set. "
        "Use eyeball QA on rendered output, or collect one exhaustive "
        "(every-gap-marked) reference video, for real precision/recall."
    )
    lines.append("")

    return "\n".join(lines)


def _fmt_hms(seconds: float) -> str:
    """Format seconds as h:mm:ss for ≥ 1 hour, m:ss otherwise."""
    s = max(0, int(round(float(seconds))))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _dist_table(values: list[float]) -> str:
    """One-line markdown table: count / min / p25 / median / p75 / max /
    mean. Linear-interpolated percentiles (numpy-free)."""
    if not values:
        return ""
    vs = sorted(values)
    n = len(vs)

    def q(p: float) -> float:
        if n == 1:
            return vs[0]
        k = (n - 1) * p
        f = int(k)
        c = min(f + 1, n - 1)
        return vs[f] + (vs[c] - vs[f]) * (k - f)

    mean = sum(vs) / n
    return (
        "| count | min | p25 | median | p75 | max | mean |\n"
        "|-------|-----|-----|--------|-----|-----|------|\n"
        f"| {n} | {vs[0]:.1f} | {q(0.25):.1f} | {q(0.5):.1f} "
        f"| {q(0.75):.1f} | {vs[-1]:.1f} | {mean:.1f} |"
    )


# ---------- helpers ---------------------------------------------------------


_SLUG_BAD = re.compile(r"[^A-Za-z0-9_-]+")


def _make_slug(project_name: str, when: float) -> str:
    """Filesystem-safe `<sanitised>_<timestamp>` slug. Vietnamese
    diacritics and spaces collapse to `_`; consecutive bad chars merge
    into one separator so slugs stay readable."""
    safe = _SLUG_BAD.sub("_", project_name).strip("_") or "match"
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(when))
    return f"{safe}_{ts}"


def _unique_dir(root: Path, slug: str) -> Path:
    """Resolve a non-existing entry path under `root`. Two renders that
    finish within the same second land on the same timestamp slug; the
    second one becomes `<slug>_2`, third is `<slug>_3`, and so on."""
    target = root / slug
    if not target.exists():
        return target
    counter = 2
    while True:
        candidate = root / f"{slug}_{counter}"
        if not candidate.exists():
            return candidate
        counter += 1


def _link_or_copy(src: Path, dst: Path) -> str | None:
    """Hardlink `src` → `dst` when supported (same-volume NTFS), copy
    fallback when not. Returns "hardlink" / "copy" on success, None on
    failure. copy2 preserves mtime which the manifest stats reader
    relies on for ordering."""
    try:
        os.link(src, dst)
        return "hardlink"
    except OSError:
        pass
    try:
        shutil.copy2(src, dst)
        return "copy"
    except OSError:
        return None


def _append_manifest(manifest_path: Path, entry: dict) -> str:
    """Read-modify-write the manifest atomically (write to .tmp, then
    rename). Returns "" on success, an error-message string on failure
    so the caller can fold it into RenderState.message."""
    data: dict = {"version": DATASET_MANIFEST_VERSION, "entries": []}
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "entries" not in data:
                data = {"version": DATASET_MANIFEST_VERSION, "entries": []}
        except (OSError, json.JSONDecodeError):
            # Corrupted manifest — back up to .bak so we don't silently
            # destroy data the operator may want to inspect.
            try:
                manifest_path.replace(manifest_path.with_suffix(".bak"))
            except OSError:
                pass
            data = {"version": DATASET_MANIFEST_VERSION, "entries": []}
    data.setdefault("entries", []).append(entry)

    tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(manifest_path)
    except OSError as e:
        return f"manifest write failed: {e}"
    return ""


def _rmtree_quietly(p: Path) -> None:
    try:
        shutil.rmtree(p)
    except OSError:
        pass


def _append_msg(ctx: "RenderContext", msg: str) -> None:
    prev = ctx.state.message
    ctx.state.message = f"{prev} | {msg}" if prev else msg
