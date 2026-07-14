// Phase B — rally detection client.
//
// Wires up the "Rally detection" panel in the Auto-Trim modal:
//   ① POST /api/auto_trim/start with the project's ROI + score events
//   ② open EventSource('/api/auto_trim/events/{job_id}') and consume
//      the SSE stream (stage / progress / trim / done / error events)
//   ③ on done: surface results + enable Apply/Discard
//   ④ Apply: append the auto trims into project.trim_segments with
//      `source: "auto"` and trigger the Trim panel's syncTrims()
//
// State machine ('idle' | 'running' | 'done' | 'cancelled' | 'error')
// drives which buttons + readouts are visible. closeModal() must call
// `abortDetection()` so we don't leak EventSource connections when the
// operator closes mid-run.

import { project, snapshot } from '../state.js';
import { fmt } from '../timecode.js';
import { toast } from '../toast.js';
import { syncTrims } from '../trims.js';
// Cyclic with api.js (which imports syncDetectionUI from here) — safe:
// both modules only export function declarations, and the calls happen
// at event time, long after both have evaluated.
import { videoIdentBody } from './api.js';
import { log } from './log.js';
import { els, state } from './state.js';


function setStatusUI() {
  const d = state.detection;
  els.detStatus.textContent = d.status;
  els.detStage.textContent = d.stage || '—';
  // Color the status word.
  els.detStatus.className = {
    idle: 'text-slate-400',
    running: 'text-accent-400',
    done: 'text-success-400',
    cancelled: 'text-warn-400',
    error: 'text-danger-400',
  }[d.status] || 'text-slate-200';

  // Progress bar visible only while running.
  const showProg = d.status === 'running';
  els.detProgressWrap.classList.toggle('hidden', !showProg);
  els.detProgress.style.width = `${Math.round(d.progress * 100)}%`;

  // Buttons: Detect+Render / Run / Cancel / (Apply + Discard).
  const enoughScores = (project.score_events?.length || 0) >= 10;
  const runsHidden = d.status === 'running' || d.status === 'done';
  els.detRun.classList.toggle('hidden', runsHidden);
  els.detRunRender.classList.toggle('hidden', runsHidden);
  els.detCancel.classList.toggle('hidden', d.status !== 'running');
  els.detApplyRow.classList.toggle('hidden', d.status !== 'done');

  // Re-run after done/error/cancelled: button is re-shown above; relabel.
  if (d.status === 'idle' || d.status === 'cancelled' || d.status === 'error') {
    els.detRun.textContent = state.confirmed
      ? '▶ Run detection only'
      : '▶ Run detection (confirm ROI first)';
    const blocked = !state.confirmed || !enoughScores;
    const blockedTitle = !state.confirmed
      ? 'Confirm ROI first'
      : (!enoughScores
        ? `Need ≥10 score events (have ${project.score_events?.length || 0})`
        : '');
    els.detRun.disabled = blocked;
    els.detRun.title = blockedTitle || 'Run rally detection';
    // The one-click chain button shares the same gate: it closes the
    // modal and runs detect → apply → render via render_chain.js.
    els.detRunRender.disabled = blocked;
    els.detRunRender.title = blockedTitle
      || 'Detect dead time, apply trims, and start the render';
  }

  // Results panel
  els.detResults.classList.toggle('hidden', d.status !== 'done');
  if (d.status === 'done' && d.done) {
    els.detTrimCount.textContent = String(d.done.trims ?? d.trims.length);
    els.detTrimTotal.textContent = fmt(d.done.total_trimmed_s ?? 0);
    els.detCache.textContent = d.cacheHit ? 'hit ⚡' : 'miss (cached for next run)';
  }
}


export function syncDetectionUI() {
  // Score event count text.
  const n = project.score_events?.length || 0;
  els.detScoreCount.textContent = String(n);
  els.detScoreCount.className = n >= 10
    ? 'text-success-400'
    : 'text-warn-400';
  setStatusUI();
}


// Exported so openAutoTrimModal (index.js) resets Phase B state through
// the same function instead of a hand-copied field list — the two had
// already drifted (the copy missed `_lastLoggedPct`).
export function resetDetection() {
  state.detection.status = 'idle';
  state.detection.jobId = null;
  state.detection.eventSource = null;
  state.detection.stage = '';
  state.detection.progress = 0;
  state.detection.trims = [];
  state.detection.done = null;
  state.detection.error = null;
  state.detection.cacheHit = false;
  state.detection.cacheKey = null;
  // Wipe the throttle marker too — a second run in the same session
  // would otherwise skip the first 10 % log line because the previous
  // run already advanced this past 0.
  state.detection._lastLoggedPct = -1;
}


export function abortDetection() {
  const d = state.detection;
  if (d.eventSource) {
    try { d.eventSource.close(); } catch (_e) { /* ignore */ }
    d.eventSource = null;
  }
  if (d.status === 'running' && d.jobId) {
    // Fire-and-forget cancel — server will flip the flag, detector
    // notices on next progress tick.
    fetch(`/api/auto_trim/cancel/${d.jobId}`, { method: 'POST' }).catch(() => {});
  }
}


// ---- SSE event handlers -----------------------------------------------
// One named function per event type; attachSse() wires them onto a new
// EventSource. Previously all seven were inlined in onRunDetectionClick
// (~160 lines) which made the state machine hard to follow.

function onSseHello(ev) {
  try {
    const d = JSON.parse(ev.data);
    log(`hello — job ${d.job_id}`);
  } catch (_e) { /* ignore */ }
}

function onSseStage(ev) {
  try {
    const d = JSON.parse(ev.data);
    state.detection.stage = d.name || state.detection.stage;
    log(`stage: ${JSON.stringify(d)}`);
    syncDetectionUI();
  } catch (_e) { /* ignore */ }
}

function onSseProgress(ev) {
  try {
    const d = JSON.parse(ev.data);
    const total = Math.max(1, d.frame_total || 1);
    state.detection.progress = Math.min(1, (d.frame_n || 0) / total);
    // Only log every 10% so the log doesn't drown in progress lines.
    const pct = Math.floor((state.detection.progress * 100) / 10) * 10;
    if (pct !== state.detection._lastLoggedPct) {
      log(`${d.stage || 'decode'} ${pct}%  (${d.frame_n}/${d.frame_total})`);
      state.detection._lastLoggedPct = pct;
    }
    syncDetectionUI();
  } catch (_e) { /* ignore */ }
}

function onSseTrim(ev) {
  try {
    const t = JSON.parse(ev.data);
    state.detection.trims.push({
      start: t.start,
      end: t.end,
      source: 'auto',
    });
  } catch (_e) { /* ignore */ }
}

function onSseLog(ev) {
  try {
    const d = JSON.parse(ev.data);
    log(`server: ${d.msg}`);
  } catch (_e) { /* ignore */ }
}

function onSseDone(ev) {
  try {
    const d = JSON.parse(ev.data);
    state.detection.done = d;
    log(`done: ${d.trims} trims, ${d.total_trimmed_s}s dead time`);
  } catch (_e) { /* ignore */ }
}

function onSseError(ev, src) {
  // Two cases: explicit `error` SSE event, OR EventSource transport
  // failure (network drop). The latter has no .data.
  if (ev.data) {
    try {
      const d = JSON.parse(ev.data);
      state.detection.status = 'error';
      state.detection.error = d.msg || 'unknown error';
      log(`ERROR: ${state.detection.error}`);
      toast(`Detection failed: ${state.detection.error}`);
    } catch (_e) {
      state.detection.status = 'error';
      state.detection.error = 'malformed error event';
    }
  } else if (state.detection.status === 'running') {
    // Transport hiccup — let it auto-reconnect for a bit.
    log('(EventSource transport blip — browser will reconnect)');
    return;
  }
  try { src.close(); } catch (_e) { /* ignore */ }
  state.detection.eventSource = null;
  syncDetectionUI();
}

function onSseClose(ev, src, willCacheHit) {
  try {
    const d = JSON.parse(ev.data);
    // Server-side terminal status — promote to UI state.
    if (d.status === 'done') {
      state.detection.status = 'done';
      state.detection.progress = 1;
      // Echo cache_hit info from the start response (server doesn't
      // re-send it here).
      state.detection.cacheHit = !!willCacheHit;
    } else if (d.status === 'cancelled') {
      state.detection.status = 'cancelled';
      log('detection cancelled');
      toast('Detection cancelled');
    } else if (d.status === 'error') {
      state.detection.status = 'error';
      state.detection.error = d.error || state.detection.error || 'unknown';
    }
  } catch (_e) { /* ignore */ }
  try { src.close(); } catch (_e) { /* ignore */ }
  state.detection.eventSource = null;
  syncDetectionUI();
}

function attachSse(jobId, willCacheHit) {
  const src = new EventSource(`/api/auto_trim/events/${jobId}`);
  state.detection.eventSource = src;
  src.addEventListener('hello', onSseHello);
  src.addEventListener('stage', onSseStage);
  src.addEventListener('progress', onSseProgress);
  src.addEventListener('trim', onSseTrim);
  src.addEventListener('log', onSseLog);
  src.addEventListener('done', onSseDone);
  src.addEventListener('error', (ev) => onSseError(ev, src));
  src.addEventListener('close', (ev) => onSseClose(ev, src, willCacheHit));
}


export async function onRunDetectionClick() {
  if (!state.confirmed && !project.info.roi_quadrilateral) {
    toast('Confirm ROI first');
    return;
  }
  const roi = state.corners || project.info.roi_quadrilateral;
  if (!roi || roi.length !== 4) {
    toast('No ROI to run against');
    return;
  }
  const events = project.score_events || [];
  if (events.length < 10) {
    toast(`Need ≥10 score events (have ${events.length})`);
    return;
  }

  resetDetection();
  state.detection.status = 'running';
  state.detection.stage = 'starting';
  syncDetectionUI();
  log(`POST /api/auto_trim/start (${events.length} score events)...`);

  let startResp;
  try {
    const r = await fetch('/api/auto_trim/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...videoIdentBody(),
        roi,
        score_events: events,
      }),
    });
    if (!r.ok) throw new Error(`start ${r.status}: ${await r.text()}`);
    startResp = await r.json();
  } catch (e) {
    state.detection.status = 'error';
    state.detection.error = e.message || String(e);
    log(`ERROR start: ${state.detection.error}`);
    toast(`Auto-trim start failed: ${state.detection.error}`);
    syncDetectionUI();
    return;
  }

  state.detection.jobId = startResp.job_id;
  state.detection.cacheKey = startResp.cache_key;
  log(
    `job ${startResp.job_id} cache_key=${(startResp.cache_key || '').slice(0, 8)}... `
    + `cache=${startResp.will_cache_hit ? 'HIT' : 'miss'}`,
  );

  attachSse(startResp.job_id, startResp.will_cache_hit);
}


export function onCancelDetectionClick() {
  if (state.detection.status !== 'running') return;
  log('cancel requested');
  abortDetection();
  state.detection.status = 'cancelled';
  syncDetectionUI();
}


export function onApplyClick() {
  const d = state.detection;
  if (d.status !== 'done' || !d.trims.length) {
    toast('Nothing to apply');
    return;
  }
  snapshot();
  // Re-run policy: drop any pre-existing auto trims, keep manual ones.
  project.trim_segments = (project.trim_segments || []).filter(
    (t) => t.source !== 'auto',
  );
  for (const t of d.trims) {
    project.trim_segments.push({
      start: t.start,
      end: t.end,
      source: 'auto',
    });
  }
  // syncTrims sorts in place + redraws the Trim panel list + timeline.
  syncTrims();
  toast(`Applied ${d.trims.length} auto trims`);
  log(`applied ${d.trims.length} auto trims into project.trim_segments`);
  // Stay in modal so operator can re-run with different params or
  // close manually. Reset detection state so the button reads "Run
  // detection" again instead of "Apply".
  resetDetection();
  syncDetectionUI();
}


export function onDiscardClick() {
  log(`discarded ${state.detection.trims.length} auto trims`);
  toast('Auto trims discarded');
  resetDetection();
  syncDetectionUI();
}
