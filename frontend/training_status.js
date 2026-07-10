// Training-status dashboard — the "📊 Training" top-bar button.
//
// One popup answering "is my manual production paying off, and is any
// training action due?" without leaving the web UI:
//
//   · Auto-score corpus: labeled matches vs the G0b fine-tune target
//     (~15). The fine-tune itself is a milestone DECISION, not a
//     one-click job — so this section reports readiness only.
//   · ROI detector: groundtruth count + YOLO staleness, with a real
//     Start Retrain button (same backend job as the Auto Trim modal's).
//   · Live retrain progress: epoch-level bar fed by
//     GET /api/auto_trim/retrain_status, shown in the popup AND as a
//     percent chip on the top-bar button while running in background —
//     regardless of which UI started the retrain (the Auto Trim modal
//     dispatches 'retrain-active' on window when it starts one).
//
// Single-writer rule: this module owns #modal-training internals and
// #btn-training-prog. Data endpoint: GET /api/training/status
// (backend/server/routes_training.py).
import { $ } from './dom.js';
import { toast } from './toast.js';

const POLL_MS = 4000;

let pollTimer = null;
let lastRetrainStatus = 'idle'; // edge-detect running → done for the toast

function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function bar(frac, color = 'bg-accent-500') {
  const pct = Math.round(Math.max(0, Math.min(1, frac)) * 100);
  return `<div class="h-2 bg-ink-700 rounded overflow-hidden">
    <div class="h-full ${color}" style="width:${pct}%"></div></div>`;
}

const MATCH_BADGES = {
  labeled: '<span class="text-emerald-400">labeled</span>',
  unlabeled: '<span class="text-amber-400">no side info</span>',
  doubles: '<span class="text-slate-500">doubles — excluded</span>',
  no_events: '<span class="text-slate-500">no score events</span>',
};

function corpusSection(c) {
  const need = Math.max(0, c.target_matches - c.labeled_matches);
  const verdict = c.ready
    ? '<span class="text-emerald-400 font-semibold">✅ Target reached — time to decide the G0b winner-detection fine-tune (docs/AUTO_SCORE_PLAN.md). The fine-tune pipeline is built at that decision point, not from this popup.</span>'
    : `<span class="text-slate-300">Not yet — <span class="text-amber-400 font-semibold">${need} more labeled singles ${need === 1 ? 'match' : 'matches'}</span> needed. Every manual production with side info + score events counts automatically.</span>`;
  const rows = c.matches.slice(0, 12).map((m) => `
    <tr class="border-t border-ink-700/60">
      <td class="py-0.5 pr-2 truncate max-w-[14rem]" title="${esc(m.video)}">${esc(m.video)}</td>
      <td class="py-0.5 pr-2 text-slate-500 whitespace-nowrap">${esc(m.archived_at.slice(0, 10))}</td>
      <td class="py-0.5 pr-2 text-right">${m.rallies || '—'}</td>
      <td class="py-0.5">${MATCH_BADGES[m.status] || esc(m.status)}</td>
    </tr>`).join('');
  const more = c.matches.length > 12
    ? `<div class="text-[11px] text-slate-500 mt-1">+ ${c.matches.length - 12} more</div>` : '';
  return `
  <section class="bg-ink-800 border border-ink-700 rounded-md p-3">
    <h4 class="text-slate-300 font-semibold mb-2">Auto-score corpus (winner-detection fine-tune)</h4>
    <div class="flex items-center gap-3 mb-1">
      <div class="flex-1">${bar(c.labeled_matches / c.target_matches, 'bg-emerald-500')}</div>
      <div class="font-mono text-xs text-slate-300 whitespace-nowrap">${c.labeled_matches} / ${c.target_matches} matches</div>
    </div>
    <div class="text-xs mb-2">${verdict}</div>
    <table class="w-full text-[11px] font-mono text-slate-400">
      <tr class="text-slate-500"><th class="text-left font-normal">video</th><th class="text-left font-normal">date</th><th class="text-right font-normal pr-2">rallies</th><th class="text-left font-normal">status</th></tr>
      ${rows}
    </table>${more}
    ${c.unlabeled_matches > 0 ? `<div class="text-[11px] text-slate-500 mt-2">"no side info" = archived before the side-info GUI existed; they count again if re-rendered with the labels filled in.</div>` : ''}
  </section>`;
}

function roiSection(roi, rt) {
  const stale = roi.confirms_since_yolo_train | 0;
  const running = rt.status === 'running';
  let staleLine;
  if (!roi.yolo_model_exists) {
    staleLine = '<span class="text-amber-400">No YOLO model yet — train one from your confirms.</span>';
  } else if (stale > 0) {
    staleLine = `<span class="text-amber-400">${stale} confirm${stale === 1 ? '' : 's'} newer than the YOLO model</span> — classical tiers already use them; YOLO learns on retrain (~2 min).`;
  } else {
    staleLine = '<span class="text-emerald-400">YOLO model is up to date</span> with every confirm.';
  }
  let action;
  if (running) {
    action = `
      <div class="mt-2">${bar(rt.progress || 0)}</div>
      <div class="mt-1 font-mono text-[11px] text-slate-400">
        ${Math.round((rt.progress || 0) * 100)}% — ${esc(rt.message)}</div>`;
  } else {
    const enabled = !roi.yolo_model_exists || stale > 0;
    action = `
      <div class="mt-2 flex items-center gap-2">
        <button id="tr-start-retrain" class="btn-primary text-xs" ${enabled ? '' : 'disabled'}>Start Retrain</button>
        ${rt.status === 'done' && rt.message ? `<span class="text-[11px] text-slate-400">last run: ${esc(rt.message)}</span>` : ''}
        ${rt.status === 'error' ? `<span class="text-[11px] text-red-400">last run failed: ${esc(rt.message)}</span>` : ''}
      </div>`;
  }
  return `
  <section class="bg-ink-800 border border-ink-700 rounded-md p-3">
    <h4 class="text-slate-300 font-semibold mb-2">ROI detector (YOLO)</h4>
    <div class="text-xs text-slate-300 mb-1"><span class="font-mono">${roi.count}</span> videos with confirmed ROI groundtruth.</div>
    <div class="text-xs">${staleLine}</div>
    ${action}
  </section>`;
}

async function fetchStatus() {
  const r = await fetch('/api/training/status');
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function render(data) {
  $('tr-body').innerHTML =
    corpusSection(data.corpus) + roiSection(data.roi, data.retrain);
}

function modalOpen() {
  return !$('modal-training').classList.contains('hidden');
}

async function refresh() {
  let data;
  try {
    data = await fetchStatus();
  } catch {
    if (modalOpen()) {
      $('tr-body').innerHTML =
        '<div class="text-red-400 text-sm">Backend unreachable.</div>';
    }
    return;
  }
  const rt = data.retrain;
  if (modalOpen()) render(data);
  updateTopBar(rt);
  // Edge: running → terminal. Toast carries the A/B comparison verdict.
  if (lastRetrainStatus === 'running' && rt.status !== 'running') {
    toast(rt.status === 'done' ? `✅ ${rt.message}` : `Retrain failed: ${rt.message}`);
  }
  lastRetrainStatus = rt.status;
  if (rt.status === 'running') beginPolling();
  else if (!modalOpen()) stopPolling();
}

function updateTopBar(rt) {
  const chip = $('btn-training-prog');
  if (rt.status === 'running') {
    chip.textContent = `⟳ ${Math.round((rt.progress || 0) * 100)}%`;
    chip.classList.remove('hidden');
  } else {
    chip.classList.add('hidden');
  }
}

function beginPolling() {
  if (!pollTimer) pollTimer = setInterval(refresh, POLL_MS);
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

async function startRetrain() {
  try {
    const r = await fetch('/api/auto_trim/retrain_yolo', { method: 'POST' });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      toast(`Retrain not started: ${j.detail || r.status}`);
      return;
    }
    toast('YOLO retrain started (~2 min) — progress on the Training button');
    lastRetrainStatus = 'running';
    beginPolling();
    refresh();
  } catch {
    toast('Retrain not started: backend unreachable');
  }
}

function openModal() {
  $('modal-training').classList.remove('hidden');
  $('modal-training').classList.add('flex');
  $('tr-body').innerHTML = '<div class="text-slate-400">Loading…</div>';
  refresh();
  beginPolling();
}

function closeModal() {
  $('modal-training').classList.add('hidden');
  $('modal-training').classList.remove('flex');
  if (lastRetrainStatus !== 'running') stopPolling();
}

// ---------- wiring (at import time, repo convention) ------------------------

$('btn-training').addEventListener('click', openModal);
$('tr-close').addEventListener('click', closeModal);
$('modal-training').addEventListener('click', (e) => {
  if (e.target === $('modal-training')) closeModal();
});
window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && modalOpen()) closeModal();
});
// Delegated: the Start Retrain button lives inside re-rendered innerHTML.
$('tr-body').addEventListener('click', (e) => {
  if (e.target.closest('#tr-start-retrain')) startRetrain();
});
// A retrain started from the Auto Trim modal should light up the
// top-bar chip too.
window.addEventListener('retrain-active', () => {
  lastRetrainStatus = 'running';
  beginPolling();
});
// Catch a retrain already in flight when the page (re)loads.
refresh();
