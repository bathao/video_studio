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
import {
  syncHighlights, toggleHighlightMark, toggleSlowmoOnLastHighlight,
} from './highlights.js';
import { syncTrims, markTrimStart, markTrimEnd } from './trims.js';
import { setSyncAllUI, setSyncInfoFromInputs as setProjectIoSync } from './project_io.js';
import { setSyncInfoFromInputs as setRenderSync } from './render.js';
import { toast } from './toast.js';


// ---------- orchestration --------------------------------------------------

function syncInfoFromInputs() {
  project.info.tournament = $('in-tournament').value;
  project.info.p1 = $('in-p1').value;
  project.info.p2 = $('in-p2').value;
  project.info.p1_team = $('in-p1-team').value;
  project.info.p2_team = $('in-p2-team').value;
  project.info.best_of = parseInt($('in-best-of').value, 10) || 5;
  $('lbl-p1').textContent = (project.info.p1 || 'P1').toUpperCase();
  $('lbl-p2').textContent = (project.info.p2 || 'P2').toUpperCase();
}

function syncAllUI() {
  $('in-tournament').value = project.info.tournament || '';
  $('in-p1').value = project.info.p1 || '';
  $('in-p2').value = project.info.p2 || '';
  $('in-p1-team').value = project.info.p1_team || '';
  $('in-p2-team').value = project.info.p2_team || '';
  $('in-best-of').value = String(project.info.best_of || 5);
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
  refreshAvatarThumb('p1');
  refreshAvatarThumb('p2');
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
['in-tournament', 'in-p1', 'in-p2', 'in-p1-team', 'in-p2-team'].forEach((id) =>
  $(id).addEventListener('input', syncInfoFromInputs)
);
$('in-best-of').addEventListener('change', syncInfoFromInputs);

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
    case 's': case 'S': e.preventDefault(); toggleSlowmoOnLastHighlight(); break;
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
  await loadVideoList();
  syncAllUI();
})();
