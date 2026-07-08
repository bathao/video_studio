// Backend calls + SSE client for the Auto Score tab.
//
// Sibling of auto_trim/detection.js: POST /api/auto_score/start, then
// EventSource('/api/auto_score/events/{job_id}') with one named handler
// per event type. `done` installs the proposal list into
// project.auto_score_draft (the persisted review session).

import { mut, project } from '../state.js';
import { toast } from '../toast.js';
import { els, state } from './state.js';
import { renderReviewList, syncAutoScoreUI } from './review.js';


function videoIdentBody() {
  const out = {};
  if (mut.externalToken) out.token = mut.externalToken;
  else if (project.info?.video_file?.trim()) out.name = project.info.video_file.trim();
  return out;
}


export function resetJob() {
  state.status = 'idle';
  state.jobId = null;
  state.eventSource = null;
  state.stage = '';
  state.progress = 0;
  state.cacheKey = null;
  state.cacheHit = false;
  state._lastLoggedPct = -1;
}


export function abortJob() {
  if (state.eventSource) {
    try { state.eventSource.close(); } catch (_e) { /* ignore */ }
    state.eventSource = null;
  }
  if (state.status === 'running' && state.jobId) {
    fetch(`/api/auto_score/cancel/${state.jobId}`, { method: 'POST' }).catch(() => {});
  }
}


// ---- SSE handlers -------------------------------------------------------

function onSseStage(ev) {
  try {
    const d = JSON.parse(ev.data);
    state.stage = d.name || state.stage;
    syncAutoScoreUI();
  } catch (_e) { /* ignore */ }
}

function onSseProgress(ev) {
  try {
    const d = JSON.parse(ev.data);
    const total = Math.max(1, d.frame_total || 1);
    state.progress = Math.min(1, (d.frame_n || 0) / total);
    syncAutoScoreUI();
  } catch (_e) { /* ignore */ }
}

function onSseDone(ev) {
  try {
    const d = JSON.parse(ev.data);
    // A fresh detection replaces any previous draft for this video.
    project.auto_score_draft = {
      video_file: project.info?.video_file || '',
      proposals: (d.proposals || []).map((p) => ({
        id: p.id,
        t_start: p.t_start,
        t_end: p.t_end,
        who: p.who || 0,
        status: p.status || 'pending',
      })),
    };
    state.cursor = 0;
  } catch (_e) { /* ignore */ }
}

function onSseError(ev, src) {
  if (ev.data) {
    try {
      const d = JSON.parse(ev.data);
      state.status = 'error';
      toast(`Auto Score failed: ${d.msg || 'unknown error'}`);
    } catch (_e) {
      state.status = 'error';
    }
  } else if (state.status === 'running') {
    return; // transport blip — EventSource auto-reconnects
  }
  try { src.close(); } catch (_e) { /* ignore */ }
  state.eventSource = null;
  syncAutoScoreUI();
}

function onSseClose(ev, src, willCacheHit) {
  try {
    const d = JSON.parse(ev.data);
    if (d.status === 'done') {
      state.status = 'done';
      state.progress = 1;
      state.cacheHit = !!willCacheHit;
      const n = project.auto_score_draft?.proposals?.length || 0;
      toast(`${n} rally proposals ready for review`);
    } else if (d.status === 'cancelled') {
      state.status = 'cancelled';
      toast('Detection cancelled');
    } else if (d.status === 'error') {
      state.status = 'error';
    }
  } catch (_e) { /* ignore */ }
  try { src.close(); } catch (_e) { /* ignore */ }
  state.eventSource = null;
  renderReviewList();
  syncAutoScoreUI();
}

function attachSse(jobId, willCacheHit) {
  const src = new EventSource(`/api/auto_score/events/${jobId}`);
  state.eventSource = src;
  src.addEventListener('stage', onSseStage);
  src.addEventListener('progress', onSseProgress);
  src.addEventListener('done', onSseDone);
  src.addEventListener('error', (ev) => onSseError(ev, src));
  src.addEventListener('close', (ev) => onSseClose(ev, src, willCacheHit));
}


export async function onDetectClick() {
  const roi = project.info?.roi_quadrilateral;
  if (!roi || roi.length !== 4) {
    toast('Confirm the table ROI first (mandatory)');
    return;
  }
  const ident = videoIdentBody();
  if (!ident.name && !ident.token) {
    toast('Load a video first');
    return;
  }
  if (project.auto_score_draft?.proposals?.length
      && !window.confirm('Replace the current proposal list with a fresh detection?')) {
    return;
  }

  resetJob();
  state.status = 'running';
  state.stage = 'starting';
  syncAutoScoreUI();

  let startResp;
  try {
    const r = await fetch('/api/auto_score/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...ident, roi }),
    });
    if (!r.ok) throw new Error(`start ${r.status}: ${await r.text()}`);
    startResp = await r.json();
  } catch (e) {
    state.status = 'error';
    toast(`Auto Score start failed: ${e.message || e}`);
    syncAutoScoreUI();
    return;
  }

  state.jobId = startResp.job_id;
  state.cacheKey = startResp.cache_key;
  attachSse(startResp.job_id, startResp.will_cache_hit);
}


export function onCancelClick() {
  if (state.status !== 'running') return;
  abortJob();
  state.status = 'cancelled';
  syncAutoScoreUI();
}
