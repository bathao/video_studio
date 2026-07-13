"""Pure helpers used across route modules.

These don't touch FastAPI's app object — they only take/return plain
values and raise HTTPException for bad input. Safe to import from any
route module.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from fastapi import HTTPException

from .state import (
    SAFE_NAME_RE,
    VIDEO_EXTS,
    _external_lock,
    _external_videos,
)

# Render job dirs are temp/<uuid4().hex[:12]> (see renderer.orchestrator);
# every named dir under temp/ (refframes, auto_trim_cache, …) fails this.
_JOB_DIR_RE = re.compile(r"^[0-9a-f]{12}$")
_STALE_JOB_DIR_AGE_S = 7 * 24 * 3600.0


def prune_stale_job_dirs(temp_dir: Path,
                         max_age_s: float = _STALE_JOB_DIR_AGE_S) -> list[str]:
    """Delete failed-render leftovers under temp/ at server start.

    `_finalize` keeps temp/<job_id> on failure so the broken ffmpeg
    inputs stay inspectable — but once the render has been retried the
    dir is just dead gigabytes (a long source leaves ~2 GB of
    intermediates). Job dirs untouched for `max_age_s` (default 7 days)
    are past any realistic debugging window; the age gate also protects
    a render that is mid-flight during a restart. Returns the deleted
    names for the caller to log."""
    deleted: list[str] = []
    if not temp_dir.is_dir():
        return deleted
    cutoff = time.time() - max_age_s
    for child in temp_dir.iterdir():
        if not child.is_dir() or not _JOB_DIR_RE.match(child.name):
            continue
        try:
            if child.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(child)
            deleted.append(child.name)
        except OSError:
            continue  # locked/in use — retried on the next startup
    return deleted


def _validate_video_path(path: Path) -> Path:
    """Resolve the path, ensure it points at an existing video file, or
    raise an appropriate HTTPException."""
    abs_path = path.resolve()
    if not abs_path.exists() or not abs_path.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {abs_path}")
    if abs_path.suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(status_code=400, detail=f"Not a supported video file: {abs_path.suffix}")
    return abs_path


def _register_validated_external(abs_path: Path) -> str:
    """Stash an already-validated absolute path under a stable token."""
    token = hashlib.sha1(str(abs_path).encode("utf-8")).hexdigest()[:16]
    with _external_lock:
        _external_videos[token] = abs_path
    return token


def _register_external_video(path: Path) -> tuple[str, Path]:
    abs_path = _validate_video_path(path)
    return _register_validated_external(abs_path), abs_path


def _resolve_external_video(token: str) -> Path:
    with _external_lock:
        p = _external_videos.get(token)
    if p is None or not p.exists():
        raise HTTPException(status_code=404, detail="External video not registered (re-pick the file)")
    return p


def _safe_name(name: str) -> str:
    if not name or not SAFE_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid name (alphanumerics, spaces, dot/dash/underscore only)")
    return name


def _resolve_inside(base: Path, name: str) -> Path:
    """Resolve `base / name` and refuse path-traversal."""
    candidate = (base / name).resolve()
    base_resolved = base.resolve()
    if base_resolved not in candidate.parents and candidate != base_resolved:
        raise HTTPException(status_code=400, detail="Path escapes base directory")
    return candidate


def _sanitize_for_json(obj):
    """Recursively convert numpy scalars/arrays to plain Python types so
    FastAPI/Pydantic can serialize the detector's debug dict. Detector
    pipeline does its own float() wrapping in hot paths, but the debug
    dict accumulates intermediate values (ratios, IoUs, statistics) where
    a numpy.float32 can slip through — this is the last line of defense."""
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    # numpy scalar (float32/int64/etc.) or 0-d array — has .item()
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        try:
            return obj.item()
        except (ValueError, TypeError):
            pass
    # numpy ndarray — convert to nested Python list
    if hasattr(obj, "tolist"):
        try:
            return obj.tolist()
        except Exception:
            pass
    return obj


# PowerShell payload for `_open_or_focus_explorer`: enumerate currently
# open Explorer windows via Shell.Application COM, compare each one's
# displayed folder to the target, and bring the match to the foreground
# instead of spawning a duplicate window. Falls through to a fresh
# explorer.exe launch (with `/select,` when a file is given) if nothing
# matches.
#
# Parameters are passed in via environment variables (VS_OPEN_TARGET +
# VS_OPEN_SELECT) so quoting is never an issue. The here-string for
# Add-Type is single-quoted (literal) — its closing '@ MUST stay at
# column 0 or PowerShell errors out on the parse.
_FOCUS_EXPLORER_PS = r"""
$ErrorActionPreference = 'Stop'
$target = $env:VS_OPEN_TARGET
$selectMode = ($env:VS_OPEN_SELECT -eq '1')
if (-not $target) { exit 2 }

$resolved = [System.IO.Path]::GetFullPath($target).TrimEnd('\')
$folderPath = if ($selectMode) { [System.IO.Path]::GetDirectoryName($resolved) } else { $resolved }
$folderPath = $folderPath.TrimEnd('\')

Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class VsWin {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
}
'@

$shell = New-Object -ComObject Shell.Application
$found = $null
foreach ($w in @($shell.Windows())) {
  try {
    if ($w.FullName -notlike '*\explorer.exe') { continue }
    $p = $w.Document.Folder.Self.Path
    if ([string]::IsNullOrEmpty($p)) { continue }
    $abs = [System.IO.Path]::GetFullPath($p).TrimEnd('\')
    if ([string]::Equals($abs, $folderPath, [System.StringComparison]::OrdinalIgnoreCase)) {
      $found = $w
      break
    }
  } catch { continue }
}

if ($found) {
  $hwnd = [IntPtr]$found.HWND
  if ([VsWin]::IsIconic($hwnd)) { [void][VsWin]::ShowWindow($hwnd, 9) }
  [void][VsWin]::SetForegroundWindow($hwnd)
  exit 0
}

if ($selectMode) {
  Start-Process explorer.exe -ArgumentList "/select,`"$resolved`""
} else {
  Start-Process explorer.exe -ArgumentList "`"$resolved`""
}
exit 0
"""


def _open_or_focus_explorer(target: Path, *, select: bool) -> None:
    """Bring an existing Explorer window for `target`'s folder to the
    foreground; only spawn a new window if none is already showing it.

    `select=True` mirrors `explorer /select,<file>` — the match is on
    the file's parent directory, and the fallback launch selects the
    file. Non-Windows hosts and PowerShell errors fall back to the
    plain Popen-based launch so behaviour never regresses.
    """
    abs_target = str(Path(target).absolute())
    if sys.platform != "win32":
        subprocess.Popen(["explorer", abs_target])
        return
    try:
        env = {
            **os.environ,
            "VS_OPEN_TARGET": abs_target,
            "VS_OPEN_SELECT": "1" if select else "0",
        }
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _FOCUS_EXPLORER_PS],
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        if select:
            subprocess.Popen(["explorer", f"/select,{abs_target}"])
        else:
            subprocess.Popen(["explorer", abs_target])
