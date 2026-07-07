// Trim segment ops + list UI. Trims describe ranges to REMOVE from
// the main render — the inverse, "kept segments", is computed in
// backend/renderer.py at render time.
import { $ } from './dom.js';
import { fmt, parseTimecode } from './timecode.js';
import { mut, project, snapshot } from './state.js';
import { invalidateKeptSegments, player } from './player.js';
import { syncTimeline } from './timeline.js';
import { toast } from './toast.js';
import { openAutoTrimModal } from './auto_trim/index.js';

export function markTrimStart() {
  if (!player.duration) return toast('Load a video first');
  mut.pendingTrimStart = player.currentTime;
  $('hud-trim').classList.remove('hidden');
  toast(`Trim start @ ${fmt(mut.pendingTrimStart)}`);
}

export function markTrimEnd() {
  if (mut.pendingTrimStart === null) return toast('Mark a trim start first');
  const start = mut.pendingTrimStart;
  const end = player.currentTime;
  mut.pendingTrimStart = null;
  $('hud-trim').classList.add('hidden');
  if (end <= start) {
    toast('Trim end must be after start');
    return;
  }
  snapshot();
  project.trim_segments.push({ start, end });
  syncTrims();
  toast(`Trim ${fmt(start)} → ${fmt(end)}`);
}

function addManualTrim() {
  // Adds an editable row at the playhead (default 10 s long) instead
  // of the old blocking prompt() pair — the start/end cells in the
  // list are already inline-editable.
  if (!player.duration) {
    toast('Load a video first');
    return;
  }
  const end = Math.min(player.currentTime + 10, player.duration);
  const start = Math.min(player.currentTime, Math.max(0, end - 1));
  snapshot();
  project.trim_segments.push({ start, end });
  syncTrims();
  toast('Trim added — adjust start/end inline');
}

function removeTrim(idx) {
  snapshot();
  project.trim_segments.splice(idx, 1);
  syncTrims();
}

export function syncTrims() {
  // Always render in chronological order, regardless of when each
  // trim was added. One HTML string + single innerHTML write with
  // delegated interaction (see listeners below); also invalidates the
  // player's kept-segment cache so Preview Cut sees the new trim set.
  project.trim_segments.sort((a, b) => a.start - b.start);
  invalidateKeptSegments();
  $('tr-count').textContent = `(${project.trim_segments.length})`;
  $('tr-list').innerHTML = project.trim_segments.map((t, i) => {
    const badge = t.source === 'auto'
      ? '<span class="font-mono text-[9px] px-1 py-0.5 rounded bg-accent-900/40 text-accent-300 border border-accent-700/50" title="Auto-detected by rally detector">AUTO</span>'
      : '';
    return `
    <li class="list-row" data-idx="${i}">
      <span class="font-mono text-warn-400 text-[11px]">#${i + 1}</span>
      ${badge}
      <input type="text" value="${fmt(t.start)}" class="ipt w-20 text-[11px] py-0.5 font-mono" data-field="start" title="m:ss.xx" />
      <span class="text-slate-500">→</span>
      <input type="text" value="${fmt(t.end)}" class="ipt w-20 text-[11px] py-0.5 font-mono" data-field="end" title="m:ss.xx" />
      <button class="text-slate-400 hover:text-accent-400 ml-auto" title="Jump to start" data-jump>↦</button>
      <button class="text-slate-400 hover:text-danger-500" title="Delete" data-del>✕</button>
    </li>`;
  }).join('');
  syncTimeline();
}

// Delegated once at module load — see syncTrims().
$('tr-list').addEventListener('click', (e) => {
  const li = e.target.closest('li[data-idx]');
  if (!li) return;
  const i = parseInt(li.dataset.idx, 10);
  const t = project.trim_segments[i];
  if (!t) return;
  if (e.target.closest('[data-jump]')) {
    player.currentTime = t.start;
  } else if (e.target.closest('[data-del]')) {
    removeTrim(i);
  }
});
$('tr-list').addEventListener('change', (e) => {
  const input = e.target.closest('input[data-field]');
  if (!input) return;
  const li = input.closest('li[data-idx]');
  if (!li) return;
  const i = parseInt(li.dataset.idx, 10);
  const v = parseTimecode(input.value);
  if (!isFinite(v) || v < 0 || !project.trim_segments[i]) {
    syncTrims();  // bad input — revert displayed value
    return;
  }
  snapshot();
  project.trim_segments[i][input.dataset.field] = v;
  syncTrims();
});

$('btn-trim-start').addEventListener('click', markTrimStart);
$('btn-trim-end').addEventListener('click', markTrimEnd);
$('btn-add-tr-manual').addEventListener('click', addManualTrim);
$('btn-auto-trim').addEventListener('click', openAutoTrimModal);
