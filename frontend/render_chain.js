// One-click render chain — Render always goes through Auto Trim.
//
// Operator rule: every clip needs auto trims (recent production: 6/6
// matches, ~45-60% of the source is dead time). Rendering an untrimmed
// video doubles the render time and ships a bad output, so the Render
// button routes through this chain instead of starting ffmpeg blindly:
//
//   auto trims present            → render immediately (no change).
//   trims missing, ROI confirmed  → headless rally detection via the
//                                   existing /api/auto_trim/* job
//                                   (progress in the Render panel),
//                                   auto-apply, then render.
//   trims missing, ROI missing    → open the Auto Trim modal at the
//                                   ROI step (the one step that needs
//                                   operator eyes); Confirm resumes the
//                                   chain automatically, closing the
//                                   modal counts as cancel.
//   <10 score events              → confirm dialog (detection needs the
//                                   events as anchors) — the only path
//                                   that can still render untrimmed.
//
// Detection failure is fail-loud: toast + abort, never a silent
// full-length render. The Auto Trim modal's "Detect + Apply + Render"
// button enters here too (with forceDetect so a re-run replaces the
// previous auto trims — cache makes that near-free).
//
// Cycle-breaking: render.js and auto_trim/index.js register their
// entry points via setters (same pattern as setSyncAllUI).
import { $ } from './dom.js';
import { mut, project, snapshot } from './state.js';
import { toast } from './toast.js';
import { syncTrims } from './trims.js';
import { closeModal } from './auto_trim/modal.js';

let _startRender = () => toast('Render entry not wired');
export function setStartRender(fn) { _startRender = fn; }

let _openAutoTrimModal = () => toast('Auto Trim modal not wired');
export function setOpenAutoTrimModal(fn) { _openAutoTrimModal = fn; }

let _running = false;
let _cancelActive = null; // set while a headless detection is in flight

function hasAutoTrims() {
  return (project.trim_segments || []).some((t) => t.source === 'auto');
}

function hasRoi() {
  const q = project.info?.roi_quadrilateral;
  return Array.isArray(q) && q.length === 4;
}

// Mirror of openAutoTrimModal's source resolution: the session token
// (external file) wins over a bare videos/ name.
function videoIdent() {
  const out = {};
  if (mut.externalToken) out.token = mut.externalToken;
  else if (project.info?.video_file?.trim()) out.name = project.info.video_file.trim();
  return out;
}

function waitForRoiConfirm() {
  return new Promise((resolve) => {
    const onConfirm = () => { cleanup(); resolve(true); };
    const onClosed = () => { cleanup(); resolve(false); };
    function cleanup() {
      window.removeEventListener('roi-confirmed', onConfirm);
      window.removeEventListener('auto-trim-closed', onClosed);
    }
    window.addEventListener('roi-confirmed', onConfirm);
    window.addEventListener('auto-trim-closed', onClosed);
  });
}

// The detect stage borrows the Render panel's status block so the
// operator watches one place; startRender() re-initialises it when the
// real render takes over.
function showDetectProgress() {
  $('render-status').classList.remove('hidden');
  $('rs-output').classList.add('hidden');
  $('rs-stage').textContent = 'auto trim';
  $('rs-pct').textContent = '0%';
  $('rs-bar').style.width = '0%';
  $('rs-msg').textContent = 'detecting dead time…';
  const btn = $('rs-cancel');
  btn.classList.remove('hidden');
  btn.disabled = false;
  btn.textContent = 'Cancel';
  btn.onclick = () => { if (_cancelActive) _cancelActive(); };
}

function hideDetectProgress() {
  $('render-status').classList.add('hidden');
  $('rs-cancel').classList.add('hidden');
}

function paintDetectProgress(frac, msg) {
  const pct = Math.round(frac * 100);
  $('rs-pct').textContent = `${pct}%`;
  $('rs-bar').style.width = `${pct}%`;
  if (msg) $('rs-msg').textContent = msg;
}

async function runDetectionHeadless() {
  const r = await fetch('/api/auto_trim/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ...videoIdent(),
      roi: project.info.roi_quadrilateral,
      score_events: project.score_events || [],
    }),
  });
  if (!r.ok) throw new Error(`start ${r.status}: ${await r.text()}`);
  const startResp = await r.json();
  const jobId = startResp.job_id;

  return new Promise((resolve, reject) => {
    const src = new EventSource(`/api/auto_trim/events/${jobId}`);
    const trims = [];
    let cancelled = false;
    _cancelActive = () => {
      cancelled = true;
      try { src.close(); } catch (_e) { /* ignore */ }
      fetch(`/api/auto_trim/cancel/${jobId}`, { method: 'POST' }).catch(() => {});
      reject(new Error('cancelled'));
    };
    src.addEventListener('stage', (ev) => {
      try { $('rs-msg').textContent = `auto trim: ${JSON.parse(ev.data).name || ''}`; } catch (_e) { /* ignore */ }
    });
    src.addEventListener('progress', (ev) => {
      try {
        const d = JSON.parse(ev.data);
        paintDetectProgress(Math.min(1, (d.frame_n || 0) / Math.max(1, d.frame_total || 1)));
      } catch (_e) { /* ignore */ }
    });
    src.addEventListener('trim', (ev) => {
      try {
        const t = JSON.parse(ev.data);
        trims.push({ start: t.start, end: t.end, source: 'auto' });
      } catch (_e) { /* ignore */ }
    });
    src.addEventListener('error', (ev) => {
      // No .data = EventSource transport blip → the browser reconnects
      // on its own; only an explicit server error event is terminal.
      if (!ev.data) return;
      let msg = 'unknown error';
      try { msg = JSON.parse(ev.data).msg || msg; } catch (_e) { /* ignore */ }
      try { src.close(); } catch (_e) { /* ignore */ }
      reject(new Error(msg));
    });
    src.addEventListener('close', (ev) => {
      try { src.close(); } catch (_e) { /* ignore */ }
      let d = {};
      try { d = JSON.parse(ev.data); } catch (_e) { /* ignore */ }
      if (d.status === 'done') resolve(trims);
      else if (!cancelled) reject(new Error(d.error || d.status || 'detection did not finish'));
    });
  });
}

function applyAutoTrims(trims) {
  snapshot();
  // Replace-own-output rule (same as the modal's Apply): drop previous
  // auto trims, keep manual ones.
  project.trim_segments = (project.trim_segments || []).filter(
    (t) => t.source !== 'auto',
  );
  for (const t of trims) project.trim_segments.push(t);
  syncTrims();
}

export async function ensureTrimsAndRender({ forceDetect = false } = {}) {
  if (_running) return;
  if (!project.info.video_file) { _startRender(); return; } // its own toast
  if (hasAutoTrims() && !forceDetect) { _startRender(); return; }

  const nEvents = (project.score_events || []).length;
  if (nEvents < 10) {
    // Detection anchors on score events — without them there is
    // nothing to trim against. The ONLY escape hatch to an untrimmed
    // render lives here, behind an explicit dialog.
    const renderFull = window.confirm(
      `Auto Trim needs >=10 score events (have ${nEvents}).\n`
      + 'Render the FULL untrimmed video anyway?',
    );
    if (renderFull) _startRender();
    return;
  }

  _running = true;
  $('btn-render').disabled = true;
  let ready = false;
  try {
    if (!hasRoi()) {
      toast('Auto Trim first — confirm the table ROI');
      // Listen BEFORE opening: openAutoTrimModal spends seconds in the
      // refframe + ROI detect fetch, and a close during that window
      // would otherwise fire before the listener exists and hang the
      // chain with the Render button disabled.
      const confirmedPromise = waitForRoiConfirm();
      _openAutoTrimModal();
      const confirmed = await confirmedPromise;
      if (!confirmed) {
        toast('Render cancelled — ROI not confirmed');
        return;
      }
      closeModal();
    }
    showDetectProgress();
    let trims;
    try {
      trims = await runDetectionHeadless();
    } catch (e) {
      const msg = (e && e.message) || String(e);
      toast(msg === 'cancelled'
        ? 'Auto Trim cancelled — render aborted'
        : `Auto Trim failed: ${msg} — render aborted`);
      return;
    }
    if (trims.length) {
      applyAutoTrims(trims);
      toast(`Applied ${trims.length} auto trims`);
    } else {
      toast('Auto Trim found no dead time — rendering as-is');
    }
    ready = true;
  } finally {
    _running = false;
    _cancelActive = null;
    $('btn-render').disabled = false; // startRender re-disables once its POST lands
    if (!ready) hideDetectProgress();
  }
  if (ready) _startRender();
}
