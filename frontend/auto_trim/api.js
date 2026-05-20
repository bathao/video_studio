// Backend HTTP calls for the Auto-Trim modal.
//
// `loadRefframeAndDetect` is the modal's main "do work" path: pulls the
// refframe JPEG (so it's loaded as an Image) and the detector result,
// then updates the info panel + canvas. `onConfirmClick` POSTs the
// (possibly edited) corners to /api/auto_trim/confirm_roi so they land
// in the groundtruth dataset. `refreshGroundtruthCount` updates the
// "N / 10" milestone counter shown in the modal header.

import { project, snapshot } from '../state.js';
import { toast } from '../toast.js';
import { redraw, loadImage } from './canvas.js';
import { log, setLoading } from './log.js';
import { updateInfoPanel } from './info_panel.js';
import { closeModal } from './modal.js';
import { els, state } from './state.js';


function buildQuery() {
  const p = new URLSearchParams();
  if (state.videoName) p.set('name', state.videoName);
  if (state.videoToken) p.set('token', state.videoToken);
  return p.toString();
}

export function videoIdentBody() {
  const out = {};
  if (state.videoName) out.name = state.videoName;
  if (state.videoToken) out.token = state.videoToken;
  return out;
}

export async function loadRefframeAndDetect() {
  setLoading(true);
  try {
    // 1) refframe URL (server extracts + caches the JPEG)
    state.refframeUrl = `/api/auto_trim/refframe?${buildQuery()}&_ts=${Date.now()}`;
    log('fetching refframe...');
    const img = await loadImage(state.refframeUrl);
    state.imgEl = img;
    state.imgW = img.naturalWidth;
    state.imgH = img.naturalHeight;
    log(`refframe ${state.imgW}×${state.imgH}`);

    // 2) auto-detect proposal
    log('POST /api/auto_trim/detect_roi ...');
    const r = await fetch('/api/auto_trim/detect_roi', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(videoIdentBody()),
    });
    if (!r.ok) throw new Error(`detect_roi ${r.status}: ${await r.text()}`);
    const det = await r.json();
    state.detectorResult = det;
    // Always trust the fresh detector output. The previous behaviour was to
    // prefer project.info.roi_quadrilateral when present, but that field is
    // project-level, not video-level — when the operator switched videos
    // without restarting the page, video 1's confirmed corners would
    // override video 2's correct detection. saved corners are still
    // written on confirm (kept for forward compatibility with Phase 1b
    // render which may consume them) but no longer drive the modal.
    state.corners = det.corners.map((p) => [p[0], p[1]]);
    state.wasEdited = false;
    log(`detector: method=${det.method} conf=${det.confidence.toFixed(2)}`);
    if (det.debug && Object.keys(det.debug).length) {
      log(`debug: ${JSON.stringify(det.debug)}`);
    }
    updateInfoPanel();
    redraw();
    els.confirm.disabled = false;
  } catch (e) {
    log(`ERROR: ${e.message || e}`);
    toast(`Auto-trim error: ${e.message || e}`);
  } finally {
    setLoading(false);
  }
}

export async function refreshGroundtruthCount() {
  try {
    const r = await fetch('/api/auto_trim/groundtruth_count');
    if (!r.ok) throw new Error(r.status);
    const j = await r.json();
    els.gtCount.textContent = `${j.count} / 10`;
    els.gtCount.classList.toggle('text-success-400', j.count >= 10);
  } catch (e) {
    els.gtCount.textContent = '—';
  }
}

export async function onConfirmClick() {
  if (state.confirmed) {
    closeModal();
    return;
  }
  if (!state.corners) return;
  els.confirm.disabled = true;
  try {
    snapshot();
    project.info.roi_quadrilateral = state.corners.map((p) => [p[0], p[1]]);

    // Pass the detector's most recent proposal so the backend doesn't
    // have to re-run detect_roi_multiframe just to log it. The result
    // is already in state.detectorResult from loadRefframeAndDetect.
    // Backend falls back to re-running only when these fields are
    // absent (defensive — should never happen in practice).
    const det = state.detectorResult;
    const body = {
      ...videoIdentBody(),
      corners: state.corners,
      was_edited: state.wasEdited,
      detector_proposed: det?.corners ?? null,
      detector_method: det?.method ?? null,
      detector_confidence: det?.confidence ?? null,
    };
    log('POST /api/auto_trim/confirm_roi ...');
    const r = await fetch('/api/auto_trim/confirm_roi', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`confirm_roi ${r.status}: ${await r.text()}`);
    const j = await r.json();
    log(`saved → ${j.saved_to} (history: ${j.history_count})`);
    log('Modal stays open. Trim detection is NOT yet wired — see banner above.');
    toast(state.wasEdited
      ? 'ROI corrected — saved as groundtruth'
      : 'ROI confirmed as-detected');
    await refreshGroundtruthCount();
    // Toggle button so a 2nd click closes (instead of re-POSTing).
    state.confirmed = true;
    els.confirm.textContent = '✓ Saved — close modal';
    els.confirm.disabled = false;
  } catch (e) {
    log(`ERROR: ${e.message || e}`);
    toast(`Save failed: ${e.message || e}`);
    els.confirm.disabled = false;
  }
}
