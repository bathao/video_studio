// Highlight reel ops + list UI. Highlights store source-time ranges
// with optional slow-mo flags; the renderer pulls them in order to
// build the highlight reel mp4.
import { $ } from './dom.js';
import { fmt, parseTimecode } from './timecode.js';
import { mut, project, snapshot } from './state.js';
import { player } from './player.js';
import { toast } from './toast.js';

export function toggleHighlightMark() {
  if (!player.duration) {
    toast('Load a video first');
    return;
  }
  if (mut.pendingHighlightStart === null) {
    snapshot();
    mut.pendingHighlightStart = player.currentTime;
    $('hud-hl').classList.remove('hidden');
    toast(`Highlight start @ ${fmt(mut.pendingHighlightStart)}`);
  } else {
    const start = mut.pendingHighlightStart;
    const end = player.currentTime;
    mut.pendingHighlightStart = null;
    $('hud-hl').classList.add('hidden');
    if (end <= start + 0.2) {
      toast('Highlight too short, ignored');
      return;
    }
    project.highlights.push({ start, end, slow_mo: false, label: '' });
    syncHighlights();
    toast(`Highlight ${fmt(start)} → ${fmt(end)}`);
  }
}

export function toggleSlowmoOnLastHighlight() {
  if (!project.highlights.length) {
    toast('No highlights yet');
    return;
  }
  snapshot();
  const last = project.highlights[project.highlights.length - 1];
  last.slow_mo = !last.slow_mo;
  syncHighlights();
  toast(`Last highlight slow-mo: ${last.slow_mo ? 'ON' : 'OFF'}`);
}

function addManualHighlight() {
  const t = player.currentTime;
  const startStr = prompt('Start time (m:ss.xx)', fmt(t));
  if (startStr === null) return;
  const endStr = prompt('End time (m:ss.xx)', fmt(t + 6));
  if (endStr === null) return;
  const start = parseTimecode(startStr);
  const end = parseTimecode(endStr);
  if (!isFinite(start) || !isFinite(end) || end <= start) {
    toast('Invalid range');
    return;
  }
  snapshot();
  project.highlights.push({ start, end, slow_mo: false, label: '' });
  syncHighlights();
}

function removeHighlight(idx) {
  snapshot();
  project.highlights.splice(idx, 1);
  syncHighlights();
}

function setHighlightField(idx, field, value) {
  snapshot();
  if (field === 'slow_mo') project.highlights[idx].slow_mo = !!value;
  else project.highlights[idx][field] = parseFloat(value);
  syncHighlights();
}

export function syncHighlights() {
  $('hl-count').textContent = `(${project.highlights.length})`;
  const ul = $('hl-list');
  ul.innerHTML = '';
  project.highlights.forEach((h, i) => {
    const li = document.createElement('li');
    li.className = 'list-row';
    li.innerHTML = `
      <span class="font-mono text-accent-400 text-[11px]">#${i + 1}</span>
      <input type="text" value="${fmt(h.start)}" class="ipt w-20 text-[11px] py-0.5 font-mono" data-field="start" title="m:ss.xx" />
      <span class="text-slate-500">→</span>
      <input type="text" value="${fmt(h.end)}" class="ipt w-20 text-[11px] py-0.5 font-mono" data-field="end" title="m:ss.xx" />
      <label class="flex items-center gap-1 ml-1"><input type="checkbox" data-field="slow_mo" ${h.slow_mo ? 'checked' : ''}/>slow</label>
      <button class="text-slate-400 hover:text-accent-400 ml-auto" title="Jump to start" data-jump>↦</button>
      <button class="text-slate-400 hover:text-danger-500" title="Delete" data-del>✕</button>
    `;
    li.querySelectorAll('input').forEach((el) => {
      el.addEventListener('change', () => {
        if (el.type === 'checkbox') {
          setHighlightField(i, el.dataset.field, el.checked);
          return;
        }
        const v = parseTimecode(el.value);
        if (isFinite(v) && v >= 0) {
          setHighlightField(i, el.dataset.field, v);
        } else {
          syncHighlights();  // bad input — revert displayed value
        }
      });
    });
    li.querySelector('[data-jump]').addEventListener('click', () => {
      player.currentTime = h.start;
      player.play().catch(() => {});
    });
    li.querySelector('[data-del]').addEventListener('click', () => removeHighlight(i));
    ul.appendChild(li);
  });
}

$('btn-mark-hl').addEventListener('click', toggleHighlightMark);
$('btn-add-hl-manual').addEventListener('click', addManualHighlight);
