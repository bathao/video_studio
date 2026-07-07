// Highlight ops + list UI. Highlights store source-time ranges; the
// renderer splices a 50%-speed replay of each into the main render at
// the matching real-time point.
import { $ } from './dom.js';
import { fmt, parseTimecode } from './timecode.js';
import { mut, project, snapshot } from './state.js';
import { player } from './player.js';
import { syncTimeline } from './timeline.js';
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
    project.highlights.push({ start, end, label: '' });
    syncHighlights();
    toast(`Highlight ${fmt(start)} → ${fmt(end)}`);
  }
}

function addManualHighlight() {
  // Adds an editable row at the playhead (default 6 s long) instead of
  // the old blocking prompt() pair — the start/end cells in the list
  // are already inline-editable.
  if (!player.duration) {
    toast('Load a video first');
    return;
  }
  const end = Math.min(player.currentTime + 6, player.duration);
  const start = Math.min(player.currentTime, Math.max(0, end - 1));
  snapshot();
  project.highlights.push({ start, end, label: '' });
  syncHighlights();
  toast('Highlight added — adjust start/end inline');
}

function removeHighlight(idx) {
  snapshot();
  project.highlights.splice(idx, 1);
  syncHighlights();
}

// Cut the raw source-video segment for one highlight. Backend saves it
// to output/ AND we trigger a browser download of the same file.
async function exportHighlight(idx, btn) {
  const h = project.highlights[idx];
  if (!h) return;
  const token = mut.externalToken;
  const videoFile = (project.info?.video_file || '').trim();
  if (!token && !videoFile) {
    toast('Load a video first');
    return;
  }
  const projName = ($('in-project')?.value || 'match').trim();
  const body = {
    token: token || null,
    video_file: videoFile || null,  // durable fallback if the token is stale
    start: h.start,
    end: h.end,
    project_name: projName,
    index: idx + 1,
  };
  const prev = btn ? btn.textContent : null;
  if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
  toast(`Exporting highlight #${idx + 1}…`);
  try {
    const r = await fetch('/api/highlights/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      let detail = await r.text();
      try { detail = JSON.parse(detail).detail ?? detail; } catch {}
      toast(`Export failed: ${detail}`);
      return;
    }
    const data = await r.json();
    toast(`Saved to output/${data.name}`);
  } catch (e) {
    toast(`Export error: ${e}`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = prev; }
  }
}

function setHighlightField(idx, field, value) {
  snapshot();
  project.highlights[idx][field] = parseFloat(value);
  syncHighlights();
}

export function syncHighlights() {
  // Always render in chronological order, regardless of when each
  // highlight was added. Sorting in place means subsequent edits +
  // the index used by click handlers stay consistent with display.
  // One HTML string + single innerHTML write; interaction is delegated
  // to the <ul> below instead of 5 listeners per row per re-render.
  project.highlights.sort((a, b) => a.start - b.start);
  $('hl-count').textContent = `(${project.highlights.length})`;
  $('hl-list').innerHTML = project.highlights.map((h, i) => `
    <li class="list-row" data-idx="${i}">
      <span class="font-mono text-accent-400 text-[11px]">#${i + 1}</span>
      <input type="text" value="${fmt(h.start)}" class="ipt w-20 text-[11px] py-0.5 font-mono" data-field="start" title="m:ss.xx" />
      <span class="text-slate-500">→</span>
      <input type="text" value="${fmt(h.end)}" class="ipt w-20 text-[11px] py-0.5 font-mono" data-field="end" title="m:ss.xx" />
      <span class="flex items-center gap-4 ml-auto pl-2">
        <button class="text-slate-400 hover:text-accent-400 px-1" title="Export clip to output/" data-export>⤓</button>
        <button class="text-slate-400 hover:text-accent-400 px-1" title="Jump to start" data-jump>↦</button>
        <button class="text-slate-400 hover:text-danger-500 px-1" title="Delete" data-del>✕</button>
      </span>
    </li>`).join('');
  syncTimeline();
}

// Delegated once at module load — see syncHighlights().
$('hl-list').addEventListener('click', (e) => {
  const li = e.target.closest('li[data-idx]');
  if (!li) return;
  const i = parseInt(li.dataset.idx, 10);
  const h = project.highlights[i];
  if (!h) return;
  const exportBtn = e.target.closest('[data-export]');
  if (exportBtn) {
    exportHighlight(i, exportBtn);
  } else if (e.target.closest('[data-jump]')) {
    player.currentTime = h.start;
    player.play().catch(() => {});
  } else if (e.target.closest('[data-del]')) {
    removeHighlight(i);
  }
});
$('hl-list').addEventListener('change', (e) => {
  const input = e.target.closest('input[data-field]');
  if (!input) return;
  const li = input.closest('li[data-idx]');
  if (!li) return;
  const v = parseTimecode(input.value);
  if (isFinite(v) && v >= 0) {
    setHighlightField(parseInt(li.dataset.idx, 10), input.dataset.field, v);
  } else {
    syncHighlights();  // bad input — revert displayed value
  }
});

$('btn-mark-hl').addEventListener('click', toggleHighlightMark);
$('btn-add-hl-manual').addEventListener('click', addManualHighlight);
