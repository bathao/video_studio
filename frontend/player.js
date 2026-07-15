// Video element wiring + source switching. Owns the <video> element,
// the HUD, the seek/scrub controls, the speed selector, and the
// machinery for picking a source video (videos folder dropdown, native
// browse picker, external-token registration for files outside videos/).
import { $ } from './dom.js';
import { fmt } from './timecode.js';
import { mut, project, snapshot } from './state.js';
import { syncLiveFromTime, syncScore } from './score.js';
import { toast } from './toast.js';

export const player = $('player');


// ---------- source switching -----------------------------------------------

function isAbsolutePath(p) {
  if (!p) return false;
  if (/^[a-zA-Z]:[\\/]/.test(p)) return true;   // C:\... or C:/...
  if (/^\\\\/.test(p))           return true;   // UNC \\server\share
  if (p.startsWith('/'))         return true;   // POSIX /...
  return false;
}

export async function loadVideoList() {
  // Validate BEFORE touching the DOM — the old code wiped the dropdown
  // first, so a backend error left it empty (indistinguishable from an
  // empty videos/ folder) with no message.
  let data;
  try {
    const r = await fetch('/api/videos');
    if (!r.ok) throw new Error(`backend answered ${r.status}`);
    data = await r.json();
  } catch (e) {
    toast(`Cannot load video list: ${e.message || e}`);
    throw e;
  }
  if (!Array.isArray(data.videos)) {
    toast('Cannot load video list: unexpected response');
    throw new Error('unexpected /api/videos payload');
  }
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
  mut.externalToken = info.token;
  addExternalOption(info.path, info.name, info.size);
  $('in-video').value = info.path;
  player.src = `/api/videos/external/${info.token}/stream`;
  player.load();
  project.info.video_file = info.path;
  mut.lastSourcedFile = info.path;
}

export async function setVideoSource(nameOrPath) {
  if (!nameOrPath) {
    player.removeAttribute('src');
    player.load();
    project.info.video_file = '';
    mut.externalToken = null;
    mut.lastSourcedFile = '';
    return;
  }
  if (nameOrPath === mut.lastSourcedFile) return;  // nothing to do
  if (isAbsolutePath(nameOrPath)) {
    // External file — register every time so reload-after-restart works
    // (tokens are session-scoped on the backend).
    const info = await registerExternalPath(nameOrPath);
    if (info) useExternalVideo(info);
    return;
  }
  mut.externalToken = null;
  player.src = `/api/videos/${encodeURIComponent(nameOrPath)}/stream`;
  player.load();
  project.info.video_file = nameOrPath;
  mut.lastSourcedFile = nameOrPath;
}

export async function browseForVideo() {
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
  snapshot();  // user-driven source change — same rationale as the dropdown
  if (data.kind === 'local') {
    // File lives inside videos/ — treat exactly like picking from the
    // dropdown. Refresh the list if the file was added since last load.
    const sel = $('in-video');
    if (!Array.from(sel.options).some((o) => o.value === data.name)) {
      // loadVideoList toasts its own failure; the picked file is still
      // usable without a refreshed list, so keep going.
      await loadVideoList().catch(() => {});
    }
    sel.value = data.name;
    await setVideoSource(data.name);
  } else {
    useExternalVideo(data);
  }
  toast(`Selected ${data.name}`);
}


// ---------- playback controls ----------------------------------------------

export function togglePlay() {
  if (player.paused) player.play();
  else player.pause();
}

export function seekBy(seconds) {
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

// ---------- preview cut: play the trimmed timeline ---------------------------
// When preview mode is on, playback (and manual seeks) jump past every
// trim_segment so the operator watches only what the render would keep —
// the same set the render's kept_segments use, both manual + auto trims.
// The dedicated "preview-bar" transport seeks on KEPT time (trims removed),
// so ⏪/⏩ stay inside the cut and the readout shows kept-position / total.
// The scoreboard overlay (JASSUB on source time) stays correct because
// score events live inside kept segments, not the skipped dead-time.

// Kept segments = complement of trim_segments within [0, duration].
// Cached — this used to be recomputed (sort + complement) on every
// timeupdate tick while preview mode was on. syncTrims() invalidates
// on every trim mutation; durationchange covers source switches.
let keptCache = null;
export function invalidateKeptSegments() { keptCache = null; }

function keptSegments() {
  if (keptCache) return keptCache;
  const dur = player.duration || 0;
  const trims = [...(project.trim_segments || [])]
    .map((t) => [Math.max(0, Math.min(dur, t.start)), Math.max(0, Math.min(dur, t.end))])
    .filter(([s, e]) => e > s)
    .sort((a, b) => a[0] - b[0]);
  const kept = [];
  let cursor = 0;
  for (const [s, e] of trims) {
    if (s > cursor) kept.push([cursor, s]);
    cursor = Math.max(cursor, e);
  }
  if (cursor < dur) kept.push([cursor, dur]);
  keptCache = kept;
  return kept;
}

const keptTotal = (kept) => kept.reduce((a, [s, e]) => a + (e - s), 0);

function sourceToKept(src, kept) {
  let acc = 0;
  for (const [s, e] of kept) {
    if (src < s) break;            // src sits in a trimmed gap before this kept seg
    if (src <= e) return acc + (src - s);
    acc += e - s;
  }
  return acc;
}

function keptToSource(k, kept) {
  let acc = 0;
  for (const [s, e] of kept) {
    const len = e - s;
    if (k <= acc + len) return s + (k - acc);
    acc += len;
  }
  return kept.length ? kept[kept.length - 1][1] : 0;
}

function jumpPastTrims() {
  if (!mut.previewSkipTrims) return;
  const segs = project.trim_segments;
  if (!segs || !segs.length) return;
  let t = player.currentTime;
  let moved = false;
  // Collapse consecutive/adjacent trims so we land on the next kept frame
  // in a single jump instead of flickering through tiny gaps.
  for (let guard = 0; guard <= segs.length; guard++) {
    const seg = segs.find((s) => t >= s.start - 0.05 && t < s.end - 0.03);
    if (!seg) break;
    t = seg.end + 0.03;
    moved = true;
  }
  if (!moved) return;
  const dur = player.duration || Infinity;
  if (t >= dur - 0.1) {
    player.pause();
  } else if (Math.abs(t - player.currentTime) > 0.01) {
    player.currentTime = t;
  }
}

// Seek by `deltaKept` seconds along the kept timeline (trims skipped).
function previewSeek(deltaKept) {
  const kept = keptSegments();
  if (!kept.length) { seekBy(deltaKept); return; }
  const total = keptTotal(kept);
  const k = Math.max(0, Math.min(total, sourceToKept(player.currentTime, kept) + deltaKept));
  player.currentTime = keptToSource(k, kept);
}

function updatePreviewTime() {
  if (!mut.previewSkipTrims) return;
  const kept = keptSegments();
  $('pv-time').textContent = `${fmt(sourceToKept(player.currentTime, kept))} / ${fmt(keptTotal(kept))}`;
}

function updatePvPlay() {
  const b = $('pv-play');
  if (b) b.textContent = player.paused ? 'Play' : 'Pause';
}

function syncPvRate() {
  document.querySelectorAll('.pv-rate').forEach((b) => {
    const active = parseFloat(b.dataset.pvRate) === player.playbackRate;
    b.classList.toggle('bg-accent-500', active);
    b.classList.toggle('text-ink-950', active);
    b.classList.toggle('text-slate-300', !active);
  });
}

function setPvRate(r) {
  player.playbackRate = r;
  $('in-rate').value = String(r);   // keep the main speed selector in sync
  syncPvRate();
}

function setPreviewSkip(on) {
  mut.previewSkipTrims = on;
  $('btn-preview-trims').classList.toggle('btn-primary', on);
  $('btn-preview-trims').classList.toggle('btn-ctl', !on);
  $('preview-bar').classList.toggle('hidden', !on);
  if (on) {
    const n = project.trim_segments?.length || 0;
    toast(n ? `Preview: skipping ${n} trimmed segment(s)` : 'Preview on (no trims to skip)');
    syncPvRate();
    updatePvPlay();
    jumpPastTrims();
    updatePreviewTime();
    player.play().catch(() => {});
  } else {
    toast('Preview off');
  }
}

player.addEventListener('timeupdate', () => {
  jumpPastTrims();
  updateHud();
  updatePreviewTime();
  // Live panel reflects the score AT the current playback position,
  // so scrubbing the timeline shows you "what was the score here?".
  syncLiveFromTime(player.currentTime);
  syncScore();
});
player.addEventListener('seeked', () => { jumpPastTrims(); updatePreviewTime(); });
player.addEventListener('durationchange', updateHud);
player.addEventListener('durationchange', invalidateKeptSegments);
player.addEventListener('play', () => { updateHud(); updatePvPlay(); });
player.addEventListener('pause', () => { updateHud(); updatePvPlay(); });

$('btn-preview-trims').addEventListener('click', () => setPreviewSkip(!mut.previewSkipTrims));
$('pv-play').addEventListener('click', togglePlay);
$('pv-exit').addEventListener('click', () => setPreviewSkip(false));
$('preview-bar').querySelectorAll('[data-pv-seek]').forEach((b) =>
  b.addEventListener('click', () => previewSeek(parseFloat(b.dataset.pvSeek))));
$('preview-bar').querySelectorAll('[data-pv-rate]').forEach((b) =>
  b.addEventListener('click', () => setPvRate(parseFloat(b.dataset.pvRate))));

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
  syncPvRate();
});

$('in-video').addEventListener('change', (e) => {
  // Snapshot at the user-driven entry point, NOT inside setVideoSource
  // — that function is also called from syncAllUI during undo/load
  // restore, where a snapshot would corrupt the undo stack.
  snapshot();
  setVideoSource(e.target.value);
});
$('btn-refresh-videos').addEventListener('click', () => {
  loadVideoList().catch(() => {});  // failure already toasted inside
});
$('btn-browse-video').addEventListener('click', browseForVideo);
