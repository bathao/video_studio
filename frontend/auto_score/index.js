// Auto Score tab — public entry. Importing this module (from app.js)
// arms the whole interaction layer: tab switching, ROI-gate button,
// detect/cancel/apply/discard, delegated list clicks, keyboard review.
//
// Phase 1 scope (Gate G0a): rally segmentation proposals + operator
// enters winners. Winner detection (VLM/solver) is Gate G0b — see
// docs/AUTO_SCORE_PLAN.md.

import { openAutoTrimModal } from '../auto_trim/index.js';
import { els, state } from './state.js';
import { abortJob, onCancelClick, onDetectClick } from './api.js';
import {
  onApplyClick, onDiscardClick, onListClick, onReviewKeydown,
  renderReviewList, syncAutoScoreUI,
} from './review.js';

export { syncAutoScoreUI, renderReviewList };


const ACTIVE = 'flex-1 px-2 py-1 text-xs rounded transition-colors bg-accent-500 text-white';
const INACTIVE = 'flex-1 px-2 py-1 text-xs rounded transition-colors text-slate-300 hover:text-white';

function setTab(auto) {
  state.autoTabActive = auto;
  els.tabManual.classList.toggle('hidden', auto);
  els.tabAuto.classList.toggle('hidden', !auto);
  els.tabManualBtn.className = auto ? INACTIVE : ACTIVE;
  els.tabAutoBtn.className = auto ? ACTIVE : INACTIVE;
  if (auto) {
    // ROI may have been confirmed (or the draft restored by a project
    // load) since the last visit — refresh everything on entry.
    renderReviewList();
  }
}

els.tabManualBtn.addEventListener('click', () => setTab(false));
els.tabAutoBtn.addEventListener('click', () => setTab(true));

// ROI gate: reuse the Auto Trim modal verbatim — its Confirm writes
// project.info.roi_quadrilateral AND feeds dataset/roi_groundtruth.
els.btnRoi.addEventListener('click', () => {
  openAutoTrimModal();
});

els.btnDetect.addEventListener('click', onDetectClick);
els.btnCancel.addEventListener('click', onCancelClick);
els.btnApply.addEventListener('click', onApplyClick);
els.btnDiscard.addEventListener('click', onDiscardClick);
els.reviewList.addEventListener('click', onListClick);

// Keyboard review — registered at import time, i.e. BEFORE app.js's
// global hotkey listener (module graph order), so accepted keys can
// stopImmediatePropagation ahead of Space=play / A/D=score.
window.addEventListener('keydown', onReviewKeydown);

// Don't leak a running EventSource across a page unload.
window.addEventListener('beforeunload', abortJob);
