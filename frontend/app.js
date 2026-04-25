/* Pingpong Studio — frontend logic.
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

const project = {
  info: { tournament: '', p1: 'Player 1', p2: 'Player 2', video_file: '' },
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

async function loadVideoList() {
  const r = await fetch('/api/videos');
  const data = await r.json();
  const sel = $('in-video');
  const current = sel.value;
  sel.innerHTML = '<option value="">— select —</option>';
  for (const v of data.videos) {
    const opt = document.createElement('option');
    opt.value = v.name;
    const mb = (v.size / (1024 * 1024)).toFixed(1);
    opt.textContent = `${v.name} (${mb} MB)`;
    sel.appendChild(opt);
  }
  if (current) sel.value = current;
}

function setVideoSource(name) {
  if (!name) {
    player.removeAttribute('src');
    player.load();
    return;
  }
  player.src = `/api/videos/${encodeURIComponent(name)}/stream`;
  player.load();
  project.info.video_file = name;
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

player.addEventListener('timeupdate', updateHud);
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

const POINTS_TO_WIN = 11;
const MIN_LEAD = 2;

function pushScoreEvent() {
  project.score_events.push({
    timestamp: player.currentTime,
    p1_score: live.p1,
    p2_score: live.p2,
    p1_set: live.p1_set,
    p2_set: live.p2_set,
  });
  syncEvents();
}

function scorePoint(who) {
  if (!player.duration && player.readyState < 1) {
    toast('Load a video first');
    return;
  }
  snapshot();
  if (who === 1) live.p1 += 1;
  else live.p2 += 1;

  // Did this point win the set?
  const max = Math.max(live.p1, live.p2);
  const lead = Math.abs(live.p1 - live.p2);
  if (max >= POINTS_TO_WIN && lead >= MIN_LEAD) {
    if (live.p1 > live.p2) live.p1_set += 1;
    else live.p2_set += 1;
    live.p1 = 0;
    live.p2 = 0;
    toast(`Set won! ${live.p1_set}-${live.p2_set}`);
  }
  pushScoreEvent();
  syncScore();
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
  const startStr = prompt('Start time (s)', t.toFixed(2));
  if (startStr === null) return;
  const endStr = prompt('End time (s)', (t + 6).toFixed(2));
  if (endStr === null) return;
  const start = parseFloat(startStr);
  const end = parseFloat(endStr);
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
  toast(`Trim start @ ${fmt(pendingTrimStart)}`);
}

function markTrimEnd() {
  if (pendingTrimStart === null) return toast('Mark a trim start first');
  const start = pendingTrimStart;
  const end = player.currentTime;
  pendingTrimStart = null;
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
  const startStr = prompt('Trim start (s)', '0');
  if (startStr === null) return;
  const endStr = prompt('Trim end (s)', '60');
  if (endStr === null) return;
  const start = parseFloat(startStr);
  const end = parseFloat(endStr);
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
      <input type="number" step="0.05" value="${h.start.toFixed(2)}" class="ipt w-20 text-[11px] py-0.5" data-field="start" />
      <span class="text-slate-500">→</span>
      <input type="number" step="0.05" value="${h.end.toFixed(2)}" class="ipt w-20 text-[11px] py-0.5" data-field="end" />
      <label class="flex items-center gap-1 ml-1"><input type="checkbox" data-field="slow_mo" ${h.slow_mo ? 'checked' : ''}/>slow</label>
      <button class="text-slate-400 hover:text-accent-400 ml-auto" title="Jump to start" data-jump>↦</button>
      <button class="text-slate-400 hover:text-danger-500" title="Delete" data-del>✕</button>
    `;
    li.querySelectorAll('input').forEach((el) => {
      el.addEventListener('change', () => setHighlightField(i, el.dataset.field,
        el.type === 'checkbox' ? el.checked : el.value));
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
      <input type="number" step="0.05" value="${t.start.toFixed(2)}" class="ipt w-20 text-[11px] py-0.5" data-field="start" />
      <span class="text-slate-500">→</span>
      <input type="number" step="0.05" value="${t.end.toFixed(2)}" class="ipt w-20 text-[11px] py-0.5" data-field="end" />
      <button class="text-slate-400 hover:text-accent-400 ml-auto" title="Jump to start" data-jump>↦</button>
      <button class="text-slate-400 hover:text-danger-500" title="Delete" data-del>✕</button>
    `;
    li.querySelectorAll('input').forEach((el) => {
      el.addEventListener('change', () => {
        snapshot();
        project.trim_segments[i][el.dataset.field] = parseFloat(el.value);
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
  project.score_events.slice().reverse().forEach((e, idx) => {
    const li = document.createElement('li');
    li.className = 'list-row';
    li.textContent = `${fmt(e.timestamp)}  [${e.p1_set}] ${e.p1_score} - ${e.p2_score} [${e.p2_set}]`;
    li.addEventListener('click', () => { player.currentTime = e.timestamp; });
    ul.appendChild(li);
  });
}

function syncInfoFromInputs() {
  project.info.tournament = $('in-tournament').value;
  project.info.p1 = $('in-p1').value;
  project.info.p2 = $('in-p2').value;
  $('lbl-p1').textContent = (project.info.p1 || 'P1').toUpperCase();
  $('lbl-p2').textContent = (project.info.p2 || 'P2').toUpperCase();
}

function syncAllUI() {
  $('in-tournament').value = project.info.tournament || '';
  $('in-p1').value = project.info.p1 || '';
  $('in-p2').value = project.info.p2 || '';
  if (project.info.video_file) {
    $('in-video').value = project.info.video_file;
    if (player.src && !player.src.includes(encodeURIComponent(project.info.video_file))) {
      setVideoSource(project.info.video_file);
    } else if (!player.src) {
      setVideoSource(project.info.video_file);
    }
  }
  syncScore();
  syncHighlights();
  syncTrims();
  syncEvents();
  syncInfoFromInputs();
}

['in-tournament', 'in-p1', 'in-p2'].forEach((id) =>
  $(id).addEventListener('input', syncInfoFromInputs)
);
$('in-video').addEventListener('change', (e) => {
  setVideoSource(e.target.value);
});
$('btn-refresh-videos').addEventListener('click', loadVideoList);

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
  // derive live score from last event
  if (project.score_events.length) {
    const last = project.score_events[project.score_events.length - 1];
    Object.assign(live, {
      p1: last.p1_score, p2: last.p2_score,
      p1_set: last.p1_set, p2_set: last.p2_set,
    });
  } else {
    Object.assign(live, { p1: 0, p2: 0, p1_set: 0, p2_set: 0 });
  }
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
  const body = {
    project_name: name,
    project,
    include_intro: $('opt-intro').checked,
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
