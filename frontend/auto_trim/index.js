// Auto-Trim modal (Phase 1a) — public entry.
//
// Flow: operator presses "Auto Trim" → modal opens → backend extracts a
// refframe at the video midpoint → backend runs naive ROI auto-detect →
// modal renders the refframe + proposed ROI overlay → operator toggles
// edit-mode and drags the 4 corners if needed → Confirm saves to
// project.info.roi_quadrilateral AND POSTs to /api/auto_trim/confirm_roi
// (which appends to a growing groundtruth dataset for improving the
// detector over time).
//
// This module deliberately does NOT touch the trim list. Trim detection
// happens in a later phase after ROI auto-detect proves reliable across
// many inputs.
//
// Module layout under frontend/auto_trim/:
//   state.js      — `els` DOM refs + `state` session object + constants
//   modal.js      — closeModal() (extracted so api.js can call without cycle)
//   log.js        — log / clearLog / setLoading
//   canvas.js     — redraw + drawCornerZooms + loadImage + corner-drag
//                   handlers (mousedown/move/up listeners wired on import)
//   info_panel.js — updateInfoPanel + escapeHtml
//   api.js        — backend HTTP calls + onConfirmClick
//   index.js      — openAutoTrimModal + button bindings + window listeners

import { mut, project } from '../state.js';
import { toast } from '../toast.js';
import {
  loadRefframeAndDetect,
  onConfirmClick,
  refreshGroundtruthCount,
} from './api.js';
import { redraw } from './canvas.js';
import {
  onApplyClick,
  onCancelDetectionClick,
  onDiscardClick,
  onRunDetectionClick,
  resetDetection,
  syncDetectionUI,
} from './detection.js';
import { clearLog, log } from './log.js';
import { closeModal } from './modal.js';
import { updateInfoPanel } from './info_panel.js';
import { DEFAULT_CORNERS, els, state } from './state.js';


export async function openAutoTrimModal() {
  // Resolve source video. We accept either an in-videos/ basename
  // (project.info.video_file is a bare filename) or an external token
  // already registered with the backend (mut.externalToken set by player.js).
  const videoName = project.info?.video_file?.trim() || '';
  const token = mut.externalToken;
  if (!videoName && !token) {
    toast('Load a video first');
    return;
  }
  state.open = true;
  state.videoName = (token ? null : videoName) || null;
  state.videoToken = token || null;
  state.refframeUrl = null;
  state.imgEl = null;
  state.corners = null;
  state.detectorResult = null;
  state.wasEdited = false;
  state.dragging = -1;
  state.confirmed = false;
  // Reset Phase B detection state too — a stale jobId from a prior
  // open would otherwise let the Cancel button POST against a job
  // belonging to the previous video. Shared with detection.js so the
  // field list can't drift again.
  resetDetection();
  els.modal.classList.remove('hidden');
  els.modal.classList.add('flex');
  els.canvasEmpty.classList.add('hidden');
  els.editMode.checked = false;
  els.confirm.disabled = true;
  els.confirm.textContent = '✓ Confirm ROI';
  clearLog();
  log(`opened modal — video=${state.videoName || `<token:${state.videoToken}>`}`);
  syncDetectionUI();
  await loadRefframeAndDetect();
  await refreshGroundtruthCount();
}


// ----- button + window event bindings ---------------------------------------

els.close.addEventListener('click', closeModal);
els.cancel.addEventListener('click', closeModal);
els.confirm.addEventListener('click', onConfirmClick);

els.redetect.addEventListener('click', async () => {
  state.wasEdited = false;
  state.confirmed = false;
  els.confirm.textContent = '✓ Confirm ROI';
  syncDetectionUI();
  await loadRefframeAndDetect();
});

els.resetDefault.addEventListener('click', () => {
  state.corners = DEFAULT_CORNERS.map((p) => [p[0], p[1]]);
  state.wasEdited = true;
  updateInfoPanel();
  redraw();
});

els.editMode.addEventListener('change', redraw);
els.clearLog.addEventListener('click', clearLog);

// Phase B — rally detection panel buttons.
els.detRun.addEventListener('click', onRunDetectionClick);
els.detCancel.addEventListener('click', onCancelDetectionClick);
els.detApply.addEventListener('click', onApplyClick);
els.detDiscard.addEventListener('click', onDiscardClick);

window.addEventListener('resize', () => {
  if (state.open) redraw();
});

// Keyboard escape
window.addEventListener('keydown', (ev) => {
  if (!state.open) return;
  if (ev.key === 'Escape') closeModal();
});
