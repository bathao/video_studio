// Score logic + UI. Events are ACTIONS, not absolute states. Each
// event records who scored (`who`) at a specific video timestamp.
// The p1_score / p2_score / set fields are a derived cache,
// recomputed by replaying all actions in chronological order. This
// way, scoring at any playback position (including after seeking
// back) inserts at the right place in time and every later event's
// score gets recomputed automatically.
import { $ } from './dom.js';
import { fmt } from './timecode.js';
import { live, project, snapshot } from './state.js';
import { syncScoreboardPreview } from './scoreboard_preview.js';
import { toast } from './toast.js';

const POINTS_TO_WIN = 11;
const MIN_LEAD = 2;

// First to ceil(best_of / 2) sets takes the match (BO5 → 3 sets).
function setsToWin() {
  return Math.ceil((project.info.best_of || 5) / 2);
}

// Sort events by timestamp and replay all actions to fill in the
// derived score / set fields. Mutates the input array.
export function recomputeAllEvents() {
  project.score_events.sort((a, b) => a.timestamp - b.timestamp);
  let p1 = 0, p2 = 0, p1Set = 0, p2Set = 0;
  for (const ev of project.score_events) {
    if (ev.who === 1) p1 += 1;
    else if (ev.who === 2) p2 += 1;
    // (who === 0 is a legacy / placeholder and counts as no-op)

    const max = Math.max(p1, p2);
    const lead = Math.abs(p1 - p2);
    if (max >= POINTS_TO_WIN && lead >= MIN_LEAD) {
      if (p1 > p2) p1Set += 1;
      else p2Set += 1;
      p1 = 0;
      p2 = 0;
    }
    ev.p1_score = p1;
    ev.p2_score = p2;
    ev.p1_set = p1Set;
    ev.p2_set = p2Set;
  }
}

// Update the live score panel to reflect the score state at time `t`
// — i.e. the latest event with timestamp ≤ t. Lets the operator scrub
// the timeline and see "what was the score here?" without pressing
// anything.
export function syncLiveFromTime(t) {
  let latest = null;
  for (const ev of project.score_events) {
    if (ev.timestamp <= t) latest = ev;
    else break;  // events are sorted, can stop early
  }
  if (latest) {
    live.p1 = latest.p1_score;
    live.p2 = latest.p2_score;
    live.p1_set = latest.p1_set;
    live.p2_set = latest.p2_set;
  } else {
    live.p1 = live.p2 = live.p1_set = live.p2_set = 0;
  }
}

// Migrate legacy projects (pre-action schema) by deriving the `who`
// field from the score diff with the previous event. Assumes events
// are already in press-order (which is how legacy projects stored them).
export function migrateLegacyEvents() {
  let p1 = 0, p2 = 0, p1Set = 0, p2Set = 0;
  let dirty = false;
  for (const ev of project.score_events) {
    if (ev.who === undefined || ev.who === 0) {
      if (ev.p1_score > p1)        ev.who = 1;
      else if (ev.p2_score > p2)   ev.who = 2;
      else if (ev.p1_set > p1Set)  ev.who = 1;
      else if (ev.p2_set > p2Set)  ev.who = 2;
      else                         ev.who = 0;
      dirty = true;
    }
    p1 = ev.p1_score;
    p2 = ev.p2_score;
    p1Set = ev.p1_set;
    p2Set = ev.p2_set;
  }
  if (dirty) recomputeAllEvents();
}

export function scorePoint(who) {
  // Imported lazily to break the circular dep with player.js (player
  // imports syncLiveFromTime/syncScore from this module on its
  // `timeupdate` listener — pulling the player element here at load
  // time would deadlock that init).
  const player = $('player');
  if (!player.duration && player.readyState < 1) {
    toast('Load a video first');
    return;
  }
  // Block scoring after the match recording has ended — any event
  // pushed here would land at `timestamp = duration` and never appear
  // in the rendered scoreboard's time range. The operator should seek
  // back into the video first if they need to correct a late point.
  if (player.ended) {
    toast('Video ended — seek back to score');
    return;
  }
  // Block scoring once the match is already decided at the current
  // playback position — in a BO5, a point after 3 won sets is an
  // operator misclick. `live` tracks the playhead via timeupdate, so
  // seeking back before the final set re-enables scoring naturally.
  const need = setsToWin();
  if (live.p1_set >= need || live.p2_set >= need) {
    toast(`Match already decided ${live.p1_set}-${live.p2_set} (best of ${project.info.best_of || 5})`);
    return;
  }
  snapshot();
  // Insert action at current playback time.
  project.score_events.push({
    timestamp: player.currentTime,
    who,
    p1_score: 0,
    p2_score: 0,
    p1_set: 0,
    p2_set: 0,
  });
  recomputeAllEvents();
  syncLiveFromTime(player.currentTime);
  syncEvents();
  syncScore();
  syncScoreboardPreview();

  // Toast on set win — detect by checking if the latest event reset to
  // 0,0. A set that closes out the match gets the bigger announcement.
  const latest = project.score_events[project.score_events.length - 1];
  if (latest && latest.p1_score === 0 && latest.p2_score === 0
      && (latest.p1_set + latest.p2_set) > 0) {
    if (latest.p1_set >= need || latest.p2_set >= need) {
      toast(`🏆 Match won ${latest.p1_set}-${latest.p2_set}!`);
    } else {
      toast(`Set won! ${latest.p1_set}-${latest.p2_set}`);
    }
  }
}

// Remove the most recent scoring event for `who` at-or-before the
// current playback time. Lets the operator correct a misclick without
// hunting in the events list. Falls back to a toast when there's
// nothing to undo for that player in the visible timeline.
export function unscorePoint(who) {
  const player = $('player');
  let target = -1;
  for (let i = project.score_events.length - 1; i >= 0; i -= 1) {
    const ev = project.score_events[i];
    if (ev.timestamp > player.currentTime) continue;
    if (ev.who === who) { target = i; break; }
  }
  if (target < 0) {
    toast(`No P${who} points to remove`);
    return;
  }
  deleteScoreEvent(target);
}

export function deleteScoreEvent(idx) {
  if (idx < 0 || idx >= project.score_events.length) return;
  const player = $('player');
  snapshot();
  project.score_events.splice(idx, 1);
  recomputeAllEvents();
  syncLiveFromTime(player.currentTime);
  syncEvents();
  syncScore();
  syncScoreboardPreview();
  toast('Event deleted');
}

// NOTE: no syncScoreboardPreview() here — syncScore runs on every
// `timeupdate` tick (~4 Hz) purely to track the playhead, and the .ass
// only depends on info + score events, never on playback position.
// Mutation sites (scorePoint / deleteScoreEvent / syncInfoFromInputs)
// call the preview refresh explicitly.
export function syncScore() {
  $('score-p1').textContent = live.p1;
  $('score-p2').textContent = live.p2;
  $('set-p1').textContent = live.p1_set;
  $('set-p2').textContent = live.p2_set;
}

export function syncEvents() {
  $('ev-count').textContent = `(${project.score_events.length})`;
  // Display newest at top; each row carries its index in the sorted
  // (chronological) array so delete acts on the right one. The list is
  // built as one HTML string + a single innerHTML write, with click
  // handling delegated to the <ul> below — a long match used to rebuild
  // hundreds of elements AND rebind 2 listeners per row on every point.
  const total = project.score_events.length;
  const rows = project.score_events.slice().reverse().map((e, revIdx) => {
    const idx = total - 1 - revIdx;
    const whoMark = e.who === 1 ? 'P1' : e.who === 2 ? 'P2' : '··';
    const whoColor = e.who === 1 ? 'text-orange-400'
                   : e.who === 2 ? 'text-accent-400'
                   : 'text-slate-500';
    return `
    <li class="list-row" data-idx="${idx}">
      <span class="font-mono text-[11px] w-14">${fmt(e.timestamp)}</span>
      <span class="text-[11px] font-bold ${whoColor}">${whoMark}</span>
      <span class="text-[11px]">[${e.p1_set}] ${e.p1_score}-${e.p2_score} [${e.p2_set}]</span>
      <button class="text-slate-400 hover:text-accent-400 ml-auto px-1" title="Jump" data-jump>↦</button>
      <button class="text-slate-400 hover:text-danger-500 px-1" title="Delete" data-del>✕</button>
    </li>`;
  });
  $('ev-list').innerHTML = rows.join('');
}

// Delegated once at module load — rows are re-rendered wholesale by
// syncEvents(), so per-row listeners would be rebound on every change.
$('ev-list').addEventListener('click', (ev) => {
  const li = ev.target.closest('li[data-idx]');
  if (!li) return;
  const idx = parseInt(li.dataset.idx, 10);
  const e = project.score_events[idx];
  if (!e) return;
  if (ev.target.closest('[data-jump]')) {
    $('player').currentTime = e.timestamp;
  } else if (ev.target.closest('[data-del]')) {
    deleteScoreEvent(idx);
  }
});

// Wire score-panel buttons.
$('btn-p1').addEventListener('click', () => scorePoint(1));
$('btn-p2').addEventListener('click', () => scorePoint(2));
$('btn-p1-minus').addEventListener('click', () => unscorePoint(1));
$('btn-p2-minus').addEventListener('click', () => unscorePoint(2));

// Disable the score buttons once the video has played to the end —
// scoring there pushes an event at `timestamp = duration` which never
// appears in the burned-in scoreboard. The `seeked` / `play` events
// re-enable them once the cursor moves back inside the video. The
// keyboard A / D shortcuts are guarded inside `scorePoint` itself,
// so they stay covered without listening here.
(function wireEndedGuard() {
  const player = $('player');
  const refresh = () => {
    const blocked = player.ended;
    $('btn-p1').disabled = blocked;
    $('btn-p2').disabled = blocked;
  };
  ['ended', 'seeked', 'seeking', 'play', 'pause', 'loadeddata', 'emptied'].forEach((ev) =>
    player.addEventListener(ev, refresh)
  );
})();
