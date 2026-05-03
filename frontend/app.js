/* Table Tennis Studio — frontend logic.
 *
 * State model:
 *
 *   project = {
 *     info: { tournament, p1, p2, video_file },
 *     trim_segments: [{ start, end }],
 *     highlights:    [{ start, end, slow_mo, label }],
 *     score_events:  [{ timestamp, p1_score, p2_score, p1_set, p2_set }],
 *   }
 *
 *   live = { p1, p2, p1_set, p2_set }   // current live score (derived from
 *                                         // the most recent score_event, or 0s)
 *   pendingHighlightStart = number|null
 *   undoStack = [snapshot, ...]         // up to 100 deep
 */

const $ = (id) => document.getElementById(id);
const fmt = (s) => {
  if (!isFinite(s) || s < 0) s = 0;
  const m = Math.floor(s / 60);
  const sec = s - m * 60;
  return `${m}:${sec.toFixed(2).padStart(5, '0')}`;
};

// Inverse of fmt(): "1:23.45" -> 83.45, "23.45" -> 23.45, "1:23" -> 83.
// Returns NaN for invalid input so callers can detect parse failure and
// revert the displayed value.
function parseTimecode(str) {
  if (str == null) return NaN;
  const s = String(str).trim();
  if (!s) return NaN;
  const parts = s.split(':');
  if (parts.length === 1) return parseFloat(parts[0]);
  if (parts.length === 2) {
    const m = parseInt(parts[0], 10);
    const sec = parseFloat(parts[1]);
    if (!isFinite(m) || !isFinite(sec)) return NaN;
    return m * 60 + sec;
  }
  return NaN;
}

const project = {
  info: { tournament: '', p1: 'Player 1', p2: 'Player 2', p1_team: '', p2_team: '', video_file: '', best_of: 5 },
  trim_segments: [],
  highlights: [],
  score_events: [],
};
const live = { p1: 0, p2: 0, p1_set: 0, p2_set: 0 };
let pendingHighlightStart = null;
const undoStack = [];
let pollTimer = null;

const player = $('player');

// ---------- snapshots / undo ------------------------------------------------

function snapshot() {
  undoStack.push({
    project: JSON.parse(JSON.stringify(project)),
    live: { ...live },
    pendingHighlightStart,
  });
  if (undoStack.length > 100) undoStack.shift();
}

function undo() {
  if (!undoStack.length) {
    toast('Nothing to undo');
    return;
  }
  const snap = undoStack.pop();
  Object.assign(project, snap.project);
  Object.assign(live, snap.live);
  pendingHighlightStart = snap.pendingHighlightStart;
  // After restoring the events array, refresh live state from the
  // current playback position so the panel matches what's on screen.
  syncLiveFromTime(player.currentTime);
  syncAllUI();
  toast('Undo');
}

// ---------- toast ----------------------------------------------------------

let toastTimer = null;
function toast(msg) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 1800);
}

// ---------- video & player -------------------------------------------------

// Token returned by /api/videos/browse (or /external/register) for the
// currently-loaded external video. Session-scoped on the backend, so
// loading a project re-registers the path to get a fresh token.
let externalToken = null;
// Memo of the video_file that's currently wired to the <video> element.
// Lets syncAllUI() avoid redundant reloads (which cause a brief flicker).
let lastSourcedFile = '';

function isAbsolutePath(p) {
  if (!p) return false;
  if (/^[a-zA-Z]:[\\/]/.test(p)) return true;   // C:\... or C:/...
  if (/^\\\\/.test(p))           return true;   // UNC \\server\share
  if (p.startsWith('/'))         return true;   // POSIX /...
  return false;
}

async function loadVideoList() {
  const r = await fetch('/api/videos');
  const data = await r.json();
  const sel = $('in-video');
  const current = sel.value;
  // Preserve any external (Browse-picked) options so refreshing the
  // backend list doesn't drop the user's manually-chosen file.
  const externals = Array.from(sel.querySelectorAll('option[data-external="1"]'));
  sel.innerHTML = '<option value="">— select —</option>';
  for (const o of externals) sel.appendChild(o);
  for (const v of data.videos) {
    const opt = document.createElement('option');
    opt.value = v.name;
    const mb = (v.size / (1024 * 1024)).toFixed(1);
    opt.textContent = `${v.name} (${mb} MB)`;
    sel.appendChild(opt);
  }
  if (current) sel.value = current;
}

function addExternalOption(absPath, name, sizeBytes) {
  const sel = $('in-video');
  // Only one external entry visible at a time — replace any prior one.
  sel.querySelectorAll('option[data-external="1"]').forEach((o) => o.remove());
  const opt = document.createElement('option');
  opt.value = absPath;
  opt.dataset.external = '1';
  const mb = (sizeBytes / (1024 * 1024)).toFixed(1);
  opt.textContent = `${name} (${mb} MB)`;
  // Insert just after the placeholder so it's prominent at the top.
  sel.insertBefore(opt, sel.children[1] || null);
}

async function registerExternalPath(absPath) {
  let r;
  try {
    r = await fetch('/api/videos/external/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: absPath }),
    });
  } catch (e) {
    toast('Cannot reach server');
    return null;
  }
  if (!r.ok) {
    const err = await r.text().catch(() => '');
    toast(`Cannot use file: ${err || r.status}`);
    return null;
  }
  return await r.json();
}

// Wire the player to a registered external file: stash the token, add
// the dropdown option, set src, update project state. Used by both the
// Browse picker and (re-)hydration on project load.
function useExternalVideo(info) {
  externalToken = info.token;
  addExternalOption(info.path, info.name, info.size);
  $('in-video').value = info.path;
  player.src = `/api/videos/external/${info.token}/stream`;
  player.load();
  project.info.video_file = info.path;
  lastSourcedFile = info.path;
}

async function setVideoSource(nameOrPath) {
  if (!nameOrPath) {
    player.removeAttribute('src');
    player.load();
    project.info.video_file = '';
    externalToken = null;
    lastSourcedFile = '';
    return;
  }
  if (nameOrPath === lastSourcedFile) return;  // nothing to do
  if (isAbsolutePath(nameOrPath)) {
    // External file — register every time so reload-after-restart works
    // (tokens are session-scoped on the backend).
    const info = await registerExternalPath(nameOrPath);
    if (info) useExternalVideo(info);
    return;
  }
  externalToken = null;
  player.src = `/api/videos/${encodeURIComponent(nameOrPath)}/stream`;
  player.load();
  project.info.video_file = nameOrPath;
  lastSourcedFile = nameOrPath;
}

async function browseForVideo() {
  toast('Opening file picker…');
  let r;
  try {
    r = await fetch('/api/videos/browse', { method: 'POST' });
  } catch (e) {
    toast('Browse error');
    return;
  }
  if (!r.ok) {
    const err = await r.text().catch(() => '');
    toast(`Browse failed: ${err || r.status}`);
    return;
  }
  const data = await r.json();
  if (data.cancelled) {
    toast('Cancelled');
    return;
  }
  if (data.kind === 'local') {
    // File lives inside videos/ — treat exactly like picking from the
    // dropdown. Refresh the list if the file was added since last load.
    const sel = $('in-video');
    if (!Array.from(sel.options).some((o) => o.value === data.name)) {
      await loadVideoList();
    }
    sel.value = data.name;
    await setVideoSource(data.name);
  } else {
    useExternalVideo(data);
  }
  toast(`Selected ${data.name}`);
}

function togglePlay() {
  if (player.paused) player.play();
  else player.pause();
}

function seekBy(seconds) {
  if (!player.duration) return;
  player.currentTime = Math.max(0, Math.min(player.duration, player.currentTime + seconds));
}

function updateHud() {
  $('hud-time').textContent = `${fmt(player.currentTime)} / ${fmt(player.duration || 0)}`;
  if (player.duration) {
    const seek = $('seek');
    seek.max = player.duration.toFixed(2);
    seek.value = player.currentTime.toFixed(2);
  }
  $('btn-play').textContent = player.paused ? 'Play' : 'Pause';
}

player.addEventListener('timeupdate', () => {
  updateHud();
  // Live panel reflects the score AT the current playback position,
  // so scrubbing the timeline shows you "what was the score here?".
  syncLiveFromTime(player.currentTime);
  syncScore();
});
player.addEventListener('durationchange', updateHud);
player.addEventListener('play', updateHud);
player.addEventListener('pause', updateHud);

$('seek').addEventListener('input', (e) => {
  const t = parseFloat(e.target.value);
  if (isFinite(t)) player.currentTime = t;
});

document.querySelectorAll('button[data-skip]').forEach((b) => {
  b.addEventListener('click', () => seekBy(parseFloat(b.dataset.skip)));
});
$('btn-play').addEventListener('click', togglePlay);
$('in-rate').addEventListener('change', (e) => {
  player.playbackRate = parseFloat(e.target.value);
});

// ---------- score logic ----------------------------------------------------
//
// Events are ACTIONS, not absolute states. Each event records who scored
// (`who`) at a specific video timestamp. The p1_score / p2_score / set
// fields are a derived cache, recomputed by replaying all actions in
// chronological order. This way, scoring at any playback position
// (including after seeking back) inserts at the right place in time
// and every later event's score gets recomputed automatically.

const POINTS_TO_WIN = 11;
const MIN_LEAD = 2;

/**
 * Sort events by timestamp and replay all actions to fill in the
 * derived score / set fields. Mutates the input array.
 */
function recomputeAllEvents() {
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

/**
 * Update the live score panel to reflect the score state at time `t`
 * — i.e. the latest event with timestamp ≤ t. Lets the operator scrub
 * the timeline and see "what was the score here?" without pressing
 * anything.
 */
function syncLiveFromTime(t) {
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

/**
 * Migrate legacy projects (pre-action schema) by deriving the `who`
 * field from the score diff with the previous event. Assumes events
 * are already in press-order (which is how legacy projects stored them).
 */
function migrateLegacyEvents() {
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

function scorePoint(who) {
  if (!player.duration && player.readyState < 1) {
    toast('Load a video first');
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

  // Toast on set win — detect by checking if the latest event reset to 0,0.
  const latest = project.score_events[project.score_events.length - 1];
  if (latest && latest.p1_score === 0 && latest.p2_score === 0
      && (latest.p1_set + latest.p2_set) > 0) {
    toast(`Set won! ${latest.p1_set}-${latest.p2_set}`);
  }
}

function deleteScoreEvent(idx) {
  if (idx < 0 || idx >= project.score_events.length) return;
  snapshot();
  project.score_events.splice(idx, 1);
  recomputeAllEvents();
  syncLiveFromTime(player.currentTime);
  syncEvents();
  syncScore();
  toast('Event deleted');
}

function syncScore() {
  $('score-p1').textContent = live.p1;
  $('score-p2').textContent = live.p2;
  $('set-p1').textContent = live.p1_set;
  $('set-p2').textContent = live.p2_set;
}

$('btn-p1').addEventListener('click', () => scorePoint(1));
$('btn-p2').addEventListener('click', () => scorePoint(2));
$('btn-undo').addEventListener('click', undo);

// ---------- highlights -----------------------------------------------------

function toggleHighlightMark() {
  if (!player.duration) {
    toast('Load a video first');
    return;
  }
  if (pendingHighlightStart === null) {
    snapshot();
    pendingHighlightStart = player.currentTime;
    $('hud-hl').classList.remove('hidden');
    toast(`Highlight start @ ${fmt(pendingHighlightStart)}`);
  } else {
    const start = pendingHighlightStart;
    const end = player.currentTime;
    pendingHighlightStart = null;
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

function toggleSlowmoOnLastHighlight() {
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

$('btn-mark-hl').addEventListener('click', toggleHighlightMark);
$('btn-add-hl-manual').addEventListener('click', addManualHighlight);

// ---------- trim segments --------------------------------------------------

let pendingTrimStart = null;

function markTrimStart() {
  if (!player.duration) return toast('Load a video first');
  pendingTrimStart = player.currentTime;
  $('hud-trim').classList.remove('hidden');
  toast(`Trim start @ ${fmt(pendingTrimStart)}`);
}

function markTrimEnd() {
  if (pendingTrimStart === null) return toast('Mark a trim start first');
  const start = pendingTrimStart;
  const end = player.currentTime;
  pendingTrimStart = null;
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

$('btn-trim-start').addEventListener('click', markTrimStart);
$('btn-trim-end').addEventListener('click', markTrimEnd);
$('btn-add-tr-manual').addEventListener('click', addManualTrim);

// ---------- list rendering -------------------------------------------------

function syncHighlights() {
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

function syncTrims() {
  $('tr-count').textContent = `(${project.trim_segments.length})`;
  const ul = $('tr-list');
  ul.innerHTML = '';
  project.trim_segments.forEach((t, i) => {
    const li = document.createElement('li');
    li.className = 'list-row';
    li.innerHTML = `
      <span class="font-mono text-warn-400 text-[11px]">#${i + 1}</span>
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
}

function syncEvents() {
  $('ev-count').textContent = `(${project.score_events.length})`;
  const ul = $('ev-list');
  ul.innerHTML = '';
  // Display newest at top, but track each event's index in the sorted
  // (chronological) array so delete acts on the right one.
  const total = project.score_events.length;
  project.score_events.slice().reverse().forEach((e, revIdx) => {
    const idx = total - 1 - revIdx;
    const whoMark = e.who === 1 ? 'P1' : e.who === 2 ? 'P2' : '··';
    const whoColor = e.who === 1 ? 'text-orange-400'
                   : e.who === 2 ? 'text-accent-400'
                   : 'text-slate-500';
    const li = document.createElement('li');
    li.className = 'list-row';
    li.innerHTML = `
      <span class="font-mono text-[11px] w-14">${fmt(e.timestamp)}</span>
      <span class="text-[11px] font-bold ${whoColor}">${whoMark}</span>
      <span class="text-[11px]">[${e.p1_set}] ${e.p1_score}-${e.p2_score} [${e.p2_set}]</span>
      <button class="text-slate-400 hover:text-accent-400 ml-auto px-1" title="Jump" data-jump>↦</button>
      <button class="text-slate-400 hover:text-danger-500 px-1" title="Delete" data-del>✕</button>
    `;
    li.querySelector('[data-jump]').addEventListener('click', (ev) => {
      ev.stopPropagation();
      player.currentTime = e.timestamp;
    });
    li.querySelector('[data-del]').addEventListener('click', (ev) => {
      ev.stopPropagation();
      deleteScoreEvent(idx);
    });
    ul.appendChild(li);
  });
}

function syncInfoFromInputs() {
  project.info.tournament = $('in-tournament').value;
  project.info.p1 = $('in-p1').value;
  project.info.p2 = $('in-p2').value;
  project.info.p1_team = $('in-p1-team').value;
  project.info.p2_team = $('in-p2-team').value;
  project.info.best_of = parseInt($('in-best-of').value, 10) || 5;
  $('lbl-p1').textContent = (project.info.p1 || 'P1').toUpperCase();
  $('lbl-p2').textContent = (project.info.p2 || 'P2').toUpperCase();
}

// Debounced thumbnail loader: when the player name changes, look up
// `assets/avatars/<name>/`. The cache-buster in the URL forces a refresh
// when the user replaces the file on disk without restarting the server.
const _thumbDebounce = { p1: 0, p2: 0 };
function refreshAvatarThumb(slot) {
  const input = $(`in-${slot}`);
  const img = $(`thumb-${slot}`);
  const name = (input.value || '').trim();
  clearTimeout(_thumbDebounce[slot]);
  _thumbDebounce[slot] = setTimeout(() => {
    if (!name) {
      img.removeAttribute('src');
      img.classList.add('opacity-30');
      return;
    }
    img.onload  = () => img.classList.remove('opacity-30');
    img.onerror = () => { img.removeAttribute('src'); img.classList.add('opacity-30'); };
    img.src = `/api/avatars/${encodeURIComponent(name)}/preview?t=${Date.now()}`;
  }, 350);
}

function syncAllUI() {
  $('in-tournament').value = project.info.tournament || '';
  $('in-p1').value = project.info.p1 || '';
  $('in-p2').value = project.info.p2 || '';
  $('in-p1-team').value = project.info.p1_team || '';
  $('in-p2-team').value = project.info.p2_team || '';
  $('in-best-of').value = String(project.info.best_of || 5);
  const vf = project.info.video_file || '';
  if (vf) {
    if (vf !== lastSourcedFile) {
      // setVideoSource handles bare-name vs. absolute-path branching,
      // including (re)registering external paths and adding the option.
      setVideoSource(vf);
    } else {
      $('in-video').value = vf;
    }
  }
  syncScore();
  syncHighlights();
  syncTrims();
  syncEvents();
  syncInfoFromInputs();
  refreshAvatarThumb('p1');
  refreshAvatarThumb('p2');
}

$('in-p1').addEventListener('input', () => refreshAvatarThumb('p1'));
$('in-p2').addEventListener('input', () => refreshAvatarThumb('p2'));
['in-tournament', 'in-p1', 'in-p2', 'in-p1-team', 'in-p2-team'].forEach((id) =>
  $(id).addEventListener('input', syncInfoFromInputs)
);
$('in-best-of').addEventListener('change', syncInfoFromInputs);
$('in-video').addEventListener('change', (e) => {
  setVideoSource(e.target.value);
});
$('btn-refresh-videos').addEventListener('click', loadVideoList);
$('btn-browse-video').addEventListener('click', browseForVideo);

// ---------- save / load ---------------------------------------------------

async function saveProject() {
  syncInfoFromInputs();
  const name = ($('in-project').value || '').trim();
  if (!name) return toast('Set a project name');
  const r = await fetch(`/api/projects/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(project),
  });
  if (!r.ok) {
    toast(`Save failed: ${r.status}`);
    return;
  }
  toast('Saved');
}

async function openLoadModal() {
  const r = await fetch('/api/projects');
  const data = await r.json();
  const ul = $('proj-list');
  ul.innerHTML = '';
  if (!data.projects.length) {
    ul.innerHTML = '<li class="text-slate-400 text-sm">No projects</li>';
  }
  for (const p of data.projects) {
    const li = document.createElement('li');
    li.className = 'list-row';
    const date = new Date(p.modified * 1000).toLocaleString();
    li.innerHTML = `<span class="flex-1">${p.name}</span><span class="text-slate-500">${date}</span>`;
    li.style.cursor = 'pointer';
    li.addEventListener('click', () => loadProject(p.name));
    ul.appendChild(li);
  }
  $('modal-load').classList.remove('hidden');
  $('modal-load').classList.add('flex');
}

async function loadProject(name) {
  const r = await fetch(`/api/projects/${encodeURIComponent(name)}`);
  if (!r.ok) return toast('Load failed');
  const data = await r.json();
  Object.assign(project.info, data.info || {});
  project.trim_segments = data.trim_segments || [];
  project.highlights = data.highlights || [];
  project.score_events = data.score_events || [];
  // Legacy projects may not have `who` on each event — derive it from
  // the score diff with the previous event, then sort and recompute so
  // the events list is internally consistent.
  migrateLegacyEvents();
  syncLiveFromTime(player.currentTime);
  $('in-project').value = name;
  syncAllUI();
  $('modal-load').classList.add('hidden');
  $('modal-load').classList.remove('flex');
  toast(`Loaded ${name}`);
}

$('btn-save').addEventListener('click', saveProject);
$('btn-load').addEventListener('click', openLoadModal);
$('btn-close-load').addEventListener('click', () => {
  $('modal-load').classList.add('hidden');
  $('modal-load').classList.remove('flex');
});

// ---------- render --------------------------------------------------------

async function startRender() {
  syncInfoFromInputs();
  if (!project.info.video_file) return toast('Pick a video first');
  const name = ($('in-project').value || 'match').trim();
  const cinematic = $('opt-intro-cinematic').checked;
  const textIntro = $('opt-intro-text').checked;
  const body = {
    project_name: name,
    project,
    include_intro: cinematic || textIntro,
    intro_style: cinematic ? 'cinematic' : 'text',
    include_highlights: $('opt-hl').checked,
    include_main: $('opt-main').checked,
    output_name: $('in-output').value || null,
  };
  const r = await fetch('/api/render', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const err = await r.text();
    toast(`Render failed: ${err}`);
    return;
  }
  const data = await r.json();
  $('render-status').classList.remove('hidden');
  $('rs-output').classList.add('hidden');
  pollRender(data.job_id);
}

function pollRender(jobId) {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const r = await fetch(`/api/render/${jobId}`);
      if (!r.ok) return;
      const j = await r.json();
      const pct = Math.round((j.progress || 0) * 100);
      $('rs-stage').textContent = j.stage || j.status;
      $('rs-pct').textContent = `${pct}%`;
      $('rs-bar').style.width = `${pct}%`;
      $('rs-msg').textContent = j.message || '';
      if (j.status === 'done') {
        clearInterval(pollTimer);
        const out = j.output_path || '';
        const fname = out.replace(/\\/g, '/').split('/').pop();
        if (fname) {
          $('rs-output').classList.remove('hidden');
          $('rs-output-path').textContent = out;
          $('rs-output-link').href = `/api/output/${encodeURIComponent(fname)}`;
          $('rs-output-reveal').onclick = async () => {
            await fetch(`/api/output/${encodeURIComponent(fname)}/reveal`, { method: 'POST' });
          };
        }
        toast('Render done');
      } else if (j.status === 'error') {
        clearInterval(pollTimer);
        toast('Render error');
      }
    } catch (e) {
      console.error(e);
    }
  }, 600);
}

$('btn-render').addEventListener('click', startRender);

// Intro style — radio-like behaviour: ticking one auto-unticks the
// other so cinematic and text never run simultaneously. Both can
// remain unticked → no intro at all.
$('opt-intro-cinematic').addEventListener('change', (e) => {
  if (e.target.checked) $('opt-intro-text').checked = false;
});
$('opt-intro-text').addEventListener('change', (e) => {
  if (e.target.checked) $('opt-intro-cinematic').checked = false;
});
$('btn-open-output').addEventListener('click', async () => {
  await fetch('/api/output-folder/open', { method: 'POST' });
});

// ---------- keyboard ------------------------------------------------------

function isFormFocused() {
  const a = document.activeElement;
  if (!a) return false;
  if (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA' || a.tagName === 'SELECT') return true;
  return false;
}

window.addEventListener('keydown', (e) => {
  // Allow Ctrl+Z everywhere; the rest only when no input is focused.
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
    e.preventDefault();
    undo();
    return;
  }
  if (isFormFocused()) return;

  switch (e.key) {
    case ' ': e.preventDefault(); togglePlay(); break;
    case 'ArrowLeft':  e.preventDefault(); seekBy(e.shiftKey ? -1 : -5); break;
    case 'ArrowRight': e.preventDefault(); seekBy(e.shiftKey ?  1 :  5); break;
    case 'a': case 'A': e.preventDefault(); scorePoint(1); break;
    case 'd': case 'D': e.preventDefault(); scorePoint(2); break;
    case 'h': case 'H': e.preventDefault(); toggleHighlightMark(); break;
    case 's': case 'S': e.preventDefault(); toggleSlowmoOnLastHighlight(); break;
    case 't': case 'T': e.preventDefault(); markTrimStart(); break;
    case 'y': case 'Y': e.preventDefault(); markTrimEnd(); break;
    default: break;
  }
});

// ---------- boot ----------------------------------------------------------

(async function init() {
  try {
    const r = await fetch('/api/health');
    const data = await r.json();
    $('health-line').textContent = `OK · encoder: ${data.encoder} · preset: ${data.preset}`;
  } catch {
    $('health-line').textContent = 'Backend offline';
  }
  await loadVideoList();
  syncAllUI();
})();
