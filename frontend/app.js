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
import {
  syncLiveFromTime, syncScore, syncEvents, scorePoint, recomputeAllEvents,
} from './score.js';
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
import './training_status.js'; // top-bar Training button + status modal


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
  // Side-info block is singles-only: doubles matches are excluded from
  // auto-score training, so the labels would be meaningless there.
  for (const el of document.querySelectorAll('.setup-singles')) {
    el.classList.toggle('hidden', mt === 'double');
  }
  // Player-1 / Player-2 row labels — in doubles each row is a pair.
  $('lbl-in-p1').firstChild.nodeValue = mt === 'double'
    ? 'Player 1 (Team 1, key A)'
    : 'Player 1 (Left, key A)';
  $('lbl-in-p2').firstChild.nodeValue = mt === 'double'
    ? 'Player 2 (Team 2, key D)'
    : 'Player 2 (Right, key D)';
}

// P1-side axis depends on the camera angle: near/far (distance from
// the tripod) for the standard behind-player family, left/right (of
// the video frame) for side-on; "other" has no defined axis. Mirrors
// backend ProjectInfo semantics.
const P1_SIDE_OPTIONS = {
  standard: [
    ['near', 'P1 near camera (default)'],
    ['far', 'P1 far from camera'],
    ['', '— unknown —'],
  ],
  side: [
    ['', '— unknown —'],
    ['left', 'P1 on the LEFT of frame'],
    ['right', 'P1 on the RIGHT of frame'],
  ],
  other: [['', '— n/a (unusual angle) —']],
};
function syncP1SideOptions(angle, value) {
  const sel = $('in-p1-side');
  const opts = P1_SIDE_OPTIONS[angle] || P1_SIDE_OPTIONS.other;
  sel.innerHTML = opts
    .map(([v, label]) => `<option value="${v}">${label}</option>`)
    .join('');
  sel.disabled = !(angle in P1_SIDE_OPTIONS) || angle === 'other';
  // Stale values from another axis (e.g. 'near' after switching to
  // side-on) fall back to unknown.
  sel.value = opts.some(([v]) => v === (value || '')) ? (value || '') : '';
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
  // Handicap — receiver 0 means none; pattern is digits-only (the
  // input listener sanitises as the operator types).
  project.info.handicap_receiver = parseInt($('in-hcp-receiver').value, 10) || 0;
  project.info.handicap_pattern = $('in-hcp-pattern').value.replace(/\D/g, '');
  $('in-hcp-pattern').disabled = project.info.handicap_receiver === 0;
  // Auto Score training labels ('' in the selects means "not
  // confirmed" / "unknown" and maps to null in the project schema).
  // The P1-side option set follows the camera angle (near/far vs
  // left/right) — rebuilt by syncP1SideOptions at the angle-change
  // listener and in syncAllUI, so here we only read.
  project.info.camera_angle = $('in-camera-angle').value || 'standard';
  project.info.p1_side_set1 = $('in-p1-side').value || null;
  project.info.swap_sides_each_set = $('in-swap-sides').checked;
  const s5 = $('in-set5-swap').value;
  project.info.set5_mid_swap = s5 === '' ? null : s5 === 'yes';
  const isDoubles = project.info.match_type === 'double';
  const top = isDoubles
    ? combineDoublesName(project.info.p1, project.info.p3) || 'P1'
    : (project.info.p1 || 'P1');
  const bot = isDoubles
    ? combineDoublesName(project.info.p2, project.info.p4) || 'P2'
    : (project.info.p2 || 'P2');
  // Mirror of the scoreboard's gold "+<pattern>" badge after the
  // receiving side's name (the burned-in version lives in
  // backend/ass/scoreboard/builder.py).
  const hcpBadge = (project.info.handicap_receiver && project.info.handicap_pattern)
    ? ` +${project.info.handicap_pattern}` : '';
  $('lbl-p1').textContent = top.toUpperCase() + (project.info.handicap_receiver === 1 ? hcpBadge : '');
  $('lbl-p2').textContent = bot.toUpperCase() + (project.info.handicap_receiver === 2 ? hcpBadge : '');
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
  $('in-hcp-receiver').value = String(project.info.handicap_receiver || 0);
  $('in-hcp-pattern').value = project.info.handicap_pattern || '';
  $('in-hcp-pattern').disabled = !(project.info.handicap_receiver || 0);
  $('in-camera-angle').value = project.info.camera_angle || 'standard';
  syncP1SideOptions(
    project.info.camera_angle || 'standard',
    project.info.p1_side_set1 || '',
  );
  $('in-swap-sides').checked = project.info.swap_sides_each_set !== false;
  $('in-set5-swap').value = project.info.set5_mid_swap == null
    ? '' : (project.info.set5_mid_swap ? 'yes' : 'no');
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
[
  'in-best-of', 'in-p1-side', 'in-swap-sides', 'in-set5-swap',
].forEach((id) => $(id).addEventListener('change', () => {
  snapshotInfoBurst();
  syncInfoFromInputs();
}));

// Changing the angle switches the P1-side AXIS (near/far vs
// left/right), so the option set rebuilds and the value resets to
// that family's convention default ('near' for standard, unknown for
// side-on/other) instead of carrying a stale cross-axis value. Loads
// of saved projects keep their explicit value — this only fires on an
// operator click.
$('in-camera-angle').addEventListener('change', () => {
  snapshotInfoBurst();
  const angle = $('in-camera-angle').value || 'standard';
  syncP1SideOptions(angle, angle === 'standard' ? 'near' : '');
  syncInfoFromInputs();
});

// Handicap edits change how EXISTING score events replay (each set's
// start score moves), so beyond the usual info sync they recompute the
// derived score cache + refresh the score panel and events list.
function onHandicapChange() {
  snapshotInfoBurst();
  // Sanitise in place so the operator sees digits-only immediately.
  const pat = $('in-hcp-pattern');
  if (pat.value !== pat.value.replace(/\D/g, '')) {
    pat.value = pat.value.replace(/\D/g, '');
  }
  syncInfoFromInputs();
  recomputeAllEvents();
  syncLiveFromTime(player.currentTime);
  syncScore();
  syncEvents();
}
$('in-hcp-receiver').addEventListener('change', onHandicapChange);
$('in-hcp-pattern').addEventListener('input', onHandicapChange);

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
