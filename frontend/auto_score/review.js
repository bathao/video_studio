// Review list — the product of the Auto tab IS this list.
//
// Rendered as ONE innerHTML string with ONE delegated click listener
// (Phase-4 convention). Keyboard-first flow (the 5-8 min/match story):
//   1 / 2   set winner P1/P2 on the cursor row, accept, advance
//   Space   accept as-is (keeps current winner), advance
//   X       delete row, advance
//   ↑ / ↓   move cursor (seeks the video to the row's rally end)
// Row click seeks; the small P1/P2/✗ buttons work by mouse too.

import { fmt } from '../timecode.js';
import { player } from '../player.js';
import { project, snapshot } from '../state.js';
import {
  recomputeAllEvents, syncEvents, syncLiveFromTime, syncScore,
} from '../score.js';
import { syncScoreboardPreview } from '../scoreboard_preview.js';
import { toast } from '../toast.js';
import { els, state } from './state.js';


function draft() {
  return project.auto_score_draft;
}

function visibleRows() {
  return (draft()?.proposals || []).filter((p) => p.status !== 'deleted');
}


export function syncAutoScoreUI() {
  // ROI gate line.
  const roiOk = !!(project.info?.roi_quadrilateral?.length === 4);
  els.roiStatus.textContent = roiOk ? 'ROI: confirmed ✓' : 'ROI: not confirmed';
  els.roiStatus.className = `flex-1 ${roiOk ? 'text-success-400' : 'text-warn-400'}`;

  // Detect / Cancel buttons.
  const running = state.status === 'running';
  els.btnDetect.disabled = !roiOk || running;
  els.btnDetect.title = roiOk ? 'Segment the match into rally proposals'
    : 'Confirm the table ROI first — detection is gated on it';
  els.btnCancel.classList.toggle('hidden', !running);

  // Progress strip.
  els.progressWrap.classList.toggle('hidden', !running);
  els.stage.textContent = state.stage || '—';
  els.status.textContent = state.status;
  els.progress.style.width = `${Math.round(state.progress * 100)}%`;

  // Review section + Apply label.
  const rows = visibleRows();
  els.reviewWrap.classList.toggle('hidden', rows.length === 0);
  const withWinner = rows.filter((p) => p.who === 1 || p.who === 2);
  els.reviewSummary.textContent =
    `${withWinner.length}/${rows.length} winners set`;
  els.btnApply.textContent = `Apply ${withWinner.length} events`;
  els.btnApply.disabled = withWinner.length === 0;
}


export function renderReviewList() {
  const rows = visibleRows();
  if (state.cursor >= rows.length) state.cursor = Math.max(0, rows.length - 1);
  els.reviewList.innerHTML = rows.map((p, i) => {
    const cur = i === state.cursor;
    const who = p.who === 1 ? 'P1' : p.who === 2 ? 'P2' : '—';
    const whoCls = p.who === 1 ? 'text-accent-400 font-bold'
      : p.who === 2 ? 'text-warn-400 font-bold' : 'text-slate-500';
    return `
      <li data-i="${i}" class="flex items-center gap-2 px-2 py-1 cursor-pointer ${cur ? 'bg-ink-800' : ''}">
        <span class="text-slate-500 w-7 shrink-0">${i + 1}</span>
        <span class="text-slate-300 w-16 shrink-0">${fmt(p.t_end)}</span>
        <span class="text-slate-500 w-10 shrink-0">${(p.t_end - p.t_start).toFixed(1)}s</span>
        <span class="w-7 shrink-0 ${whoCls}">${who}</span>
        <span class="flex-1"></span>
        <button data-act="p1" data-i="${i}" class="px-1.5 rounded bg-ink-800 border border-ink-700 hover:border-accent-500">1</button>
        <button data-act="p2" data-i="${i}" class="px-1.5 rounded bg-ink-800 border border-ink-700 hover:border-warn-400">2</button>
        <button data-act="del" data-i="${i}" class="px-1.5 rounded bg-ink-800 border border-ink-700 hover:border-danger-500">✗</button>
      </li>`;
  }).join('');
  syncAutoScoreUI();
}


function seekToRow(i) {
  const rows = visibleRows();
  const p = rows[i];
  if (!p || !player.src) return;
  // Land ~3 s before the rally end so the operator sees the deciding
  // stroke + the aftermath.
  player.currentTime = Math.max(p.t_start, p.t_end - 3);
  player.play().catch(() => {});
}

function setCursor(i, { seek = true } = {}) {
  const rows = visibleRows();
  if (!rows.length) return;
  state.cursor = Math.min(Math.max(0, i), rows.length - 1);
  renderReviewList();
  const li = els.reviewList.querySelector(`li[data-i="${state.cursor}"]`);
  if (li) li.scrollIntoView({ block: 'nearest' });
  if (seek) seekToRow(state.cursor);
}

function verdict(i, act) {
  const rows = visibleRows();
  const p = rows[i];
  if (!p) return;
  if (act === 'p1') { p.who = 1; p.status = 'accepted'; }
  else if (act === 'p2') { p.who = 2; p.status = 'accepted'; }
  else if (act === 'accept') { p.status = 'accepted'; }
  else if (act === 'del') { p.status = 'deleted'; }
  // Advance to the next still-visible row (same index after delete).
  setCursor(act === 'del' ? i : i + 1);
}


// ---- Apply / Discard ----------------------------------------------------

export function onApplyClick() {
  const events = visibleRows().filter((p) => p.who === 1 || p.who === 2);
  if (!events.length) { toast('No winners set yet'); return; }
  snapshot();
  // Re-run policy mirrors auto-trim: replace only our own output.
  project.score_events = (project.score_events || []).filter(
    (e) => e.source !== 'auto',
  );
  for (const p of events) {
    project.score_events.push({
      timestamp: p.t_end,
      who: p.who,
      p1_score: 0, p2_score: 0, p1_set: 0, p2_set: 0,
      source: 'auto',
    });
  }
  recomputeAllEvents();
  syncLiveFromTime(player.currentTime || 0);
  syncEvents();
  syncScore();
  syncScoreboardPreview();
  project.auto_score_draft = null;
  renderReviewList();
  toast(`Applied ${events.length} auto score events`);
}

export function onDiscardClick() {
  if (!draft()) return;
  if (!window.confirm('Discard the whole proposal list?')) return;
  project.auto_score_draft = null;
  state.cursor = 0;
  renderReviewList();
  toast('Proposals discarded');
}


// ---- Delegated mouse + keyboard -----------------------------------------

export function onListClick(ev) {
  const btn = ev.target.closest('button[data-act]');
  if (btn) {
    verdict(Number(btn.dataset.i), btn.dataset.act);
    return;
  }
  const li = ev.target.closest('li[data-i]');
  if (li) setCursor(Number(li.dataset.i));
}

export function onReviewKeydown(ev) {
  if (!state.autoTabActive) return;
  const t = ev.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) return;
  if (!visibleRows().length) return;
  const key = ev.key;
  if (key === '1') verdict(state.cursor, 'p1');
  else if (key === '2') verdict(state.cursor, 'p2');
  else if (key === ' ') verdict(state.cursor, 'accept');
  else if (key === 'x' || key === 'X') verdict(state.cursor, 'del');
  else if (key === 'ArrowDown') setCursor(state.cursor + 1);
  else if (key === 'ArrowUp') setCursor(state.cursor - 1);
  else return;
  ev.preventDefault();
  // Registered before app.js's global hotkeys (module import order), so
  // this blocks Space=play / A/D=manual-score while reviewing.
  ev.stopImmediatePropagation();
}
