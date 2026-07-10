// YOLO retrain flow for the Auto-Trim modal.
//
// Owns everything retrain-related in the UI — the staleness line
// (#at-gt-stale), the "Retrain now" button (#at-retrain), the
// after-confirm popup offer, and the status polling while a retrain
// runs on the backend. Single-writer rule: no other module touches
// those two elements.
//
// Flow: every Confirm makes the classical tiers smarter immediately,
// but the YOLO tier only learns from a retrain. When enough confirms
// (PROMPT_AT) have accumulated since the model file was trained,
// confirming offers a one-click retrain (popup, once per modal
// session); the button stays available for manual triggering at any
// pending count. Backend job: POST /api/auto_trim/retrain_yolo +
// GET /api/auto_trim/retrain_status (backend/server/retrain.py).
import { toast } from '../toast.js';
import { log } from './log.js';
import { els } from './state.js';

// Offer threshold — matches the "retrain suggested" wording; the
// backend has no opinion (any pending count may be trained).
const PROMPT_AT = 5;
const POLL_MS = 5000;

let pollTimer = null;
let offeredThisSession = false;

export function isRetrainActive() {
  return pollTimer !== null;
}

function showLine(text) {
  els.gtStale.textContent = text;
  els.gtStale.classList.remove('hidden');
}

// Single entry point for reflecting backend state into the UI.
// `gt` = /groundtruth_count payload, `st` = /retrain_status payload
// (either may be null on fetch failure).
export function updateRetrainUI(gt, st) {
  if (st && st.status === 'running') {
    beginPolling();
    return;
  }
  if (!gt) {
    els.gtStale.classList.add('hidden');
    els.retrain.classList.add('hidden');
    return;
  }
  const stale = gt.confirms_since_yolo_train | 0;
  const missing = !gt.yolo_model_exists;
  els.retrain.classList.toggle('hidden', !(missing || stale > 0));
  if (missing) {
    showLine('YOLO model missing — train it from your confirms');
  } else if (stale >= PROMPT_AT) {
    showLine(`${stale} confirms not in the YOLO model yet — retrain suggested (~2 min)`);
  } else if (stale > 0) {
    showLine(`${stale} confirms newer than the YOLO model`);
  } else {
    els.gtStale.classList.add('hidden');
  }
}

// Called right after a successful Confirm. Pops the yes/no offer once
// per modal session when the pending count crosses the threshold.
export function offerRetrainIfDue(gt, st) {
  if (offeredThisSession || !gt) return;
  if (st && st.status === 'running') return;
  const stale = gt.confirms_since_yolo_train | 0;
  if (gt.yolo_model_exists && stale < PROMPT_AT) return;
  offeredThisSession = true;
  const what = gt.yolo_model_exists
    ? `${stale} ROI confirms are not in the YOLO model yet.`
    : 'No YOLO model has been trained yet.';
  const go = window.confirm(
    `${what}\nRetrain now? (~2 min, runs in the background — you can keep working)`,
  );
  if (go) startRetrain();
}

export async function startRetrain() {
  if (isRetrainActive()) return;
  try {
    const r = await fetch('/api/auto_trim/retrain_yolo', { method: 'POST' });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      toast(`Retrain not started: ${j.detail || r.status}`);
      return;
    }
    toast('YOLO retrain started (~2 min)');
    log('YOLO retrain started (dataset build → train)…');
    beginPolling();
  } catch {
    toast('Retrain not started: backend unreachable');
  }
}

function beginPolling() {
  if (pollTimer) return;
  showLine('YOLO retrain running…');
  els.retrain.classList.add('hidden');
  pollTimer = setInterval(pollOnce, POLL_MS);
  // Let the top-bar Training chip (training_status.js) track this run.
  window.dispatchEvent(new CustomEvent('retrain-active'));
}

async function pollOnce() {
  let st;
  try {
    const r = await fetch('/api/auto_trim/retrain_status');
    if (!r.ok) return; // transient — keep polling
    st = await r.json();
  } catch {
    return;
  }
  if (st.status === 'running') {
    const pct = Math.round((st.progress || 0) * 100);
    showLine(`YOLO retrain running — ${pct}% — ${st.message}`);
    return;
  }
  clearInterval(pollTimer);
  pollTimer = null;
  if (st.status === 'done') {
    // Message carries the auto-comparison verdict (old vs new model on
    // the confirmed refframes) — show it, don't summarize it away.
    toast(`✅ ${st.message}`);
    log(`YOLO retrain done — ${st.message}`);
    log('New weights active on the next detect.');
  } else {
    toast(`Retrain failed: ${st.message}`);
    log(`YOLO retrain FAILED: ${st.message}`);
  }
  // Re-sync the staleness line/button from fresh backend truth.
  try {
    const gt = await (await fetch('/api/auto_trim/groundtruth_count')).json();
    updateRetrainUI(gt, st);
  } catch {
    els.gtStale.classList.add('hidden');
  }
}

// Manual trigger — wired at import time (same convention as canvas.js).
els.retrain.addEventListener('click', () => startRetrain());
