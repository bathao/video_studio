// Trim segment ops + list UI. Trims describe ranges to REMOVE from
// the main render — the inverse, "kept segments", is computed in
// backend/renderer.py at render time.
import { $ } from './dom.js';
import { fmt, parseTimecode } from './timecode.js';
import { mut, project, snapshot } from './state.js';
import { player } from './player.js';
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
  const startStr = prompt('Trim start (m:ss.xx)', fmt(0));
  if (startStr === null) return;
  const endStr = prompt('Trim end (m:ss.xx)', fmt(60));
  if (endStr === null) return;
  const start = parseTimecode(startStr);
  const end = parseTimecode(endStr);
  if (!isFinite(start) || !isFinite(end) || end <= start) {
    toast('Invalid range');
    return;
  }
  snapshot();
  project.trim_segments.push({ start, end });
  syncTrims();
}

function removeTrim(idx) {
  snapshot();
  project.trim_segments.splice(idx, 1);
  syncTrims();
}

export function syncTrims() {
  // Always render in chronological order, regardless of when each
  // trim was added.
  project.trim_segments.sort((a, b) => a.start - b.start);
  $('tr-count').textContent = `(${project.trim_segments.length})`;
  const ul = $('tr-list');
  ul.innerHTML = '';
  project.trim_segments.forEach((t, i) => {
    const li = document.createElement('li');
    li.className = 'list-row';
    const badge = t.source === 'auto'
      ? '<span class="font-mono text-[9px] px-1 py-0.5 rounded bg-accent-900/40 text-accent-300 border border-accent-700/50" title="Auto-detected by rally detector">AUTO</span>'
      : '';
    li.innerHTML = `
      <span class="font-mono text-warn-400 text-[11px]">#${i + 1}</span>
      ${badge}
      <input type="text" value="${fmt(t.start)}" class="ipt w-20 text-[11px] py-0.5 font-mono" data-field="start" title="m:ss.xx" />
      <span class="text-slate-500">→</span>
      <input type="text" value="${fmt(t.end)}" class="ipt w-20 text-[11px] py-0.5 font-mono" data-field="end" title="m:ss.xx" />
      <button class="text-slate-400 hover:text-accent-400 ml-auto" title="Jump to start" data-jump>↦</button>
      <button class="text-slate-400 hover:text-danger-500" title="Delete" data-del>✕</button>
    `;
    li.querySelectorAll('input').forEach((el) => {
      el.addEventListener('change', () => {
        const v = parseTimecode(el.value);
        if (!isFinite(v) || v < 0) {
          syncTrims();  // bad input — revert displayed value
          return;
        }
        snapshot();
        project.trim_segments[i][el.dataset.field] = v;
        syncTrims();
      });
    });
    li.querySelector('[data-jump]').addEventListener('click', () => {
      player.currentTime = t.start;
    });
    li.querySelector('[data-del]').addEventListener('click', () => removeTrim(i));
    ul.appendChild(li);
  });
  syncTimeline();
}

$('btn-trim-start').addEventListener('click', markTrimStart);
$('btn-trim-end').addEventListener('click', markTrimEnd);
$('btn-add-tr-manual').addEventListener('click', addManualTrim);
$('btn-auto-trim').addEventListener('click', openAutoTrimModal);
