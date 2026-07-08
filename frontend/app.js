// Boot module. The actual UI logic lives in the per-feature modules
// imported below; this file glues them together by:
//
//   1. Forcing each module to evaluate (so its top-level DOM event
//      wiring runs) just by importing it.
//   2. Defining the two cross-cutting orchestration functions
//      (`syncInfoFromInputs`, `syncAllUI`) and `undo`, which need to
//      reach into too many modules to live cleanly inside any one of
//      them.
//   3. Wiring the global keyboard shortcuts.
//   4. Running the initial `health` ping + video list load + UI sync.
//
// Mutable state lives in state.js (project, live, undoStack, mut.*).

import { $ } from './dom.js';
import { refreshAvatarThumb } from './avatars.js';
import { live, mut, project, snapshot, undoStack } from './state.js';
import { syncLiveFromTime, syncScore, syncEvents, scorePoint } from './score.js';
import {
  player, setVideoSource, togglePlay, seekBy, loadVideoList,
} from './player.js';
import { syncHighlights, toggleHighlightMark } from './highlights.js';
import { syncTrims, markTrimStart, markTrimEnd } from './trims.js';
import { setSyncAllUI, setSyncInfoFromInputs as setProjectIoSync } from './project_io.js';
import { setSyncInfoFromInputs as setRenderSync } from './render.js';
import { renderReviewList } from './auto_score/index.js';
import { syncScoreboardPreview } from './scoreboard_preview.js';
import { syncTimeline } from './timeline.js';
import { toast } from './toast.js';


// ---------- orchestration --------------------------------------------------

// Mirror of the backend's `_last_two_words` / `_combine_doubles_name`
// in backend/ass/common.py. Kept here so the Live Score panel labels
// reflect the same combined-name convention the scoreboard burns in —
// without an extra round-trip to the server for every keystroke. The
// scoreboard preview itself still goes through the backend, so this
// stays cosmetic (the labels above the score counters).
function lastTwoWords(name) {
  const text = (name || '').trim();
  if (!text) return '';
  const tokens = text.split(/\s+/);
  if (tokens.length <= 2) return text;
  return tokens.slice(-2).join(' ');
}
function combineDoublesName(a, b) {
  return [lastTwoWords(a), lastTwoWords(b)].filter(Boolean).join(' + ');
}

function applyMatchTypeUI() {
  const mt = project.info.match_type === 'double' ? 'double' : 'single';
  // Toggle active state on the tab buttons.
  for (const id of ['tab-single', 'tab-double']) {
    const btn = $(id);
    const active = btn.dataset.matchType === mt;
    btn.classList.toggle('bg-accent-500', active);
    btn.classList.toggle('text-white', active);
    btn.classList.toggle('text-slate-300', !active);
  }
  // Show / hide P3 + P4 rows (and their thumbnail blocks).
  for (const el of document.querySelectorAll('.setup-doubles')) {
    el.classList.toggle('hidden', mt !== 'double');
  }
  // Player-1 / Player-2 row labels — in doubles each row is a pair.
  $('lbl-in-p1').firstChild.nodeValue = mt === 'double'
    ? 'Player 1 (Team 1, key A)'
    : 'Player 1 (Left, key A)';
  $('lbl-in-p2').firstChild.nodeValue = mt === 'double'
    ? 'Player 2 (Team 2, key D)'
    : 'Player 2 (Right, key D)';
}

function syncInfoFromInputs() {
  project.info.tournament = $('in-tournament').value;
  project.info.p1 = $('in-p1').value;
  project.info.p2 = $('in-p2').value;
  project.info.p3 = $('in-p3').value;
  project.info.p4 = $('in-p4').value;
  project.info.p1_team = $('in-p1-team').value;
  project.info.p2_team = $('in-p2-team').value;
  project.info.best_of = parseInt($('in-best-of').value, 10) || 5;
  const isDoubles = project.info.match_type === 'double';
  const top = isDoubles
    ? combineDoublesName(project.info.p1, project.info.p3) || 'P1'
    : (project.info.p1 || 'P1');
  const bot = isDoubles
    ? combineDoublesName(project.info.p2, project.info.p4) || 'P2'
    : (project.info.p2 || 'P2');
  $('lbl-p1').textContent = top.toUpperCase();
  $('lbl-p2').textContent = bot.toUpperCase();
  syncScoreboardPreview();
}

function syncAllUI() {
  $('in-tournament').value = project.info.tournament || '';
  $('in-p1').value = project.info.p1 || '';
  $('in-p2').value = project.info.p2 || '';
  $('in-p3').value = project.info.p3 || '';
  $('in-p4').value = project.info.p4 || '';
  $('in-p1-team').value = project.info.p1_team || '';
  $('in-p2-team').value = project.info.p2_team || '';
  $('in-best-of').value = String(project.info.best_of || 5);
  applyMatchTypeUI();
  const vf = project.info.video_file || '';
  if (vf) {
    if (vf !== mut.lastSourcedFile) {
      // setVideoSource handles bare-name vs. absolute-path branching,
      // including (re)registering external paths and adding the option.
      setVideoSource(vf);
    } else {
      $('in-video').value = vf;
    }
  }
  syncScore();
  syncHighlights();
  syncTrims();
  syncEvents();
  syncInfoFromInputs();
  syncTimeline();
  // Auto Score tab: ROI-gate status + restore a saved review draft
  // after project load / undo.
  renderReviewList();
  refreshAvatarThumb('p1');
  refreshAvatarThumb('p2');
  refreshAvatarThumb('p3');
  refreshAvatarThumb('p4');
}

// Info edits (name / tournament / best-of typing) get undo coverage
// too — otherwise a later Ctrl+Z restores a pre-edit `project.info`
// snapshot and silently wipes whatever the operator typed since. One
// snapshot per editing burst, not per keystroke: only take a fresh one
// when the previous info snapshot is older than 1.5 s.
let lastInfoSnapshotAt = 0;
function snapshotInfoBurst() {
  const now = Date.now();
  if (now - lastInfoSnapshotAt > 1500) snapshot();
  lastInfoSnapshotAt = now;
}

function undo() {
  if (!undoStack.length) {
    toast('Nothing to undo');
    return;
  }
  const snap = undoStack.pop();
  Object.assign(project, snap.project);
  Object.assign(live, snap.live);
  mut.pendingHighlightStart = snap.pendingHighlightStart;
  mut.pendingTrimStart = snap.pendingTrimStart ?? null;
  // The HUD badges mirror the pending markers — keep them in sync or
  // an undone T/H press leaves a stale "TRIM…" / "HL…" chip on screen.
  $('hud-hl').classList.toggle('hidden', mut.pendingHighlightStart === null);
  $('hud-trim').classList.toggle('hidden', mut.pendingTrimStart === null);
  lastInfoSnapshotAt = 0;  // typing right after undo must snapshot again
  // After restoring the events array, refresh live state from the
  // current playback position so the panel matches what's on screen.
  syncLiveFromTime(player.currentTime);
  syncAllUI();
  toast('Undo');
}


// ---------- DOM event wiring (cross-module) --------------------------------

$('btn-undo').addEventListener('click', undo);

$('in-p1').addEventListener('input', () => refreshAvatarThumb('p1'));
$('in-p2').addEventListener('input', () => refreshAvatarThumb('p2'));
$('in-p3').addEventListener('input', () => refreshAvatarThumb('p3'));
$('in-p4').addEventListener('input', () => refreshAvatarThumb('p4'));
[
  'in-tournament', 'in-p1', 'in-p2', 'in-p3', 'in-p4',
  'in-p1-team', 'in-p2-team',
].forEach((id) => $(id).addEventListener('input', () => {
  snapshotInfoBurst();
  syncInfoFromInputs();
}));
$('in-best-of').addEventListener('change', () => {
  snapshotInfoBurst();
  syncInfoFromInputs();
});

// Match-type tabs. Clicking either tab updates state, re-renders the
// setup UI (which hides / shows the partner inputs), and re-fetches
// the scoreboard preview so the overlay flips between solo names and
// the combined doubles labels immediately.
for (const id of ['tab-single', 'tab-double']) {
  $(id).addEventListener('click', () => {
    const mt = $(id).dataset.matchType;
    if (project.info.match_type === mt) return;
    snapshot();
    project.info.match_type = mt;
    applyMatchTypeUI();
    syncInfoFromInputs();
  });
}

// Hand the orchestration callbacks to modules that need them but
// can't import them directly (cycle-breaking).
setSyncAllUI(syncAllUI);
setProjectIoSync(syncInfoFromInputs);
setRenderSync(syncInfoFromInputs);


// ---------- keyboard -------------------------------------------------------

function isFormFocused() {
  const a = document.activeElement;
  if (!a) return false;
  if (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA' || a.tagName === 'SELECT') return true;
  return false;
}

window.addEventListener('keydown', (e) => {
  // Allow Ctrl+Z everywhere; the rest only when no input is focused.
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
    e.preventDefault();
    undo();
    return;
  }
  if (isFormFocused()) return;

  switch (e.key) {
    case ' ': e.preventDefault(); togglePlay(); break;
    case 'ArrowLeft':  e.preventDefault(); seekBy(e.shiftKey ? -1 : -5); break;
    case 'ArrowRight': e.preventDefault(); seekBy(e.shiftKey ?  1 :  5); break;
    case 'a': case 'A': e.preventDefault(); scorePoint(1); break;
    case 'd': case 'D': e.preventDefault(); scorePoint(2); break;
    case 'h': case 'H': e.preventDefault(); toggleHighlightMark(); break;
    case 't': case 'T': e.preventDefault(); markTrimStart(); break;
    case 'y': case 'Y': e.preventDefault(); markTrimEnd(); break;
    default: break;
  }
});


// ---------- boot ----------------------------------------------------------

(async function init() {
  try {
    const r = await fetch('/api/health');
    const data = await r.json();
    $('health-line').textContent = `OK · encoder: ${data.encoder} · preset: ${data.preset}`;
  } catch {
    $('health-line').textContent = 'Backend offline';
  }
  // A failed list load must not abort boot — without this, a backend
  // that's still starting (or down) left the whole UI uninitialized
  // because syncAllUI() never ran.
  try {
    await loadVideoList();
  } catch {
    toast('Cannot load video list — is the backend running?');
  }
  syncAllUI();
})();
