// Auto-Trim modal (Phase 1a).
//
// Flow: operator presses "Auto Trim" → modal opens → backend extracts a
// refframe at the video midpoint → backend runs naive ROI auto-detect →
// modal renders the refframe + proposed ROI overlay → operator toggles
// edit-mode and drags the 4 corners if needed → Confirm saves to
// project.info.roi_quadrilateral AND POSTs to /api/auto_trim/confirm_roi
// (which appends to a growing groundtruth dataset for improving the
// detector over time).
//
// This module deliberately does NOT touch the trim list. Trim detection
// happens in a later phase after ROI auto-detect proves reliable across
// many inputs.

import { $ } from './dom.js';
import { mut, project, snapshot } from './state.js';
import { toast } from './toast.js';

const els = {
  modal: $('modal-auto-trim'),
  canvas: $('at-canvas'),
  canvasWrap: $('at-canvas-wrap'),
  canvasEmpty: $('at-canvas-empty'),
  canvasLoading: $('at-canvas-loading'),
  editMode: $('at-edit-mode'),
  cornerReadout: $('at-corner-readout'),
  zoomTL: $('at-zoom-tl'),
  zoomTR: $('at-zoom-tr'),
  zoomBR: $('at-zoom-br'),
  zoomBL: $('at-zoom-bl'),
  status: $('at-roi-status'),
  infoVideo: $('at-info-video'),
  infoMethod: $('at-info-method'),
  infoConf: $('at-info-conf'),
  infoSource: $('at-info-source'),
  infoNn: $('at-info-nn'),
  infoNnList: $('at-info-nn-list'),
  log: $('at-log'),
  gtCount: $('at-gt-count'),
  redetect: $('at-redetect'),
  resetDefault: $('at-reset-default'),
  clearLog: $('at-clear-log'),
  confirm: $('at-confirm'),
  cancel: $('at-cancel'),
  close: $('at-close'),
};

// State for the open modal session.
const state = {
  open: false,
  videoName: null,    // basename inside videos_dir, OR null when using token
  videoToken: null,   // external-video token, OR null when using name
  refframeUrl: null,
  imgEl: null,         // loaded HTMLImageElement
  imgW: 0,
  imgH: 0,
  corners: null,       // [[x, y], ...] normalized 0-1, TL TR BR BL
  detectorResult: null,  // most recent /api/auto_trim/detect_roi response
  wasEdited: false,
  dragging: -1,        // index of corner being dragged, or -1
  confirmed: false,    // true after a successful confirm POST in this session
};

const DEFAULT_CORNERS = [
  [0.25, 0.55],
  [0.65, 0.55],
  [0.67, 0.85],
  [0.23, 0.85],
];

const CORNER_LABELS = ['TL', 'TR', 'BR', 'BL'];

// ----- modal open/close -----------------------------------------------------

export async function openAutoTrimModal() {
  // Resolve source video. We accept either an in-videos/ basename
  // (project.info.video_file is a bare filename) or an external token
  // already registered with the backend (mut.externalToken set by player.js).
  const videoName = project.info?.video_file?.trim() || '';
  const token = mut.externalToken;
  if (!videoName && !token) {
    toast('Load a video first');
    return;
  }
  state.open = true;
  state.videoName = (token ? null : videoName) || null;
  state.videoToken = token || null;
  state.refframeUrl = null;
  state.imgEl = null;
  state.corners = null;
  state.detectorResult = null;
  state.wasEdited = false;
  state.dragging = -1;
  state.confirmed = false;
  els.modal.classList.remove('hidden');
  els.modal.classList.add('flex');
  els.canvasEmpty.classList.add('hidden');
  els.editMode.checked = false;
  els.confirm.disabled = true;
  els.confirm.textContent = '✓ Confirm ROI';
  clearLog();
  log(`opened modal — video=${state.videoName || `<token:${state.videoToken}>`}`);
  await loadRefframeAndDetect();
  await refreshGroundtruthCount();
}

function closeModal() {
  state.open = false;
  els.modal.classList.add('hidden');
  els.modal.classList.remove('flex');
}

// ----- backend calls --------------------------------------------------------

function buildQuery() {
  const p = new URLSearchParams();
  if (state.videoName) p.set('name', state.videoName);
  if (state.videoToken) p.set('token', state.videoToken);
  return p.toString();
}

function videoIdentBody() {
  const out = {};
  if (state.videoName) out.name = state.videoName;
  if (state.videoToken) out.token = state.videoToken;
  return out;
}

async function loadRefframeAndDetect() {
  setLoading(true);
  try {
    // 1) refframe URL (server extracts + caches the JPEG)
    state.refframeUrl = `/api/auto_trim/refframe?${buildQuery()}&_ts=${Date.now()}`;
    log('fetching refframe...');
    const img = await loadImage(state.refframeUrl);
    state.imgEl = img;
    state.imgW = img.naturalWidth;
    state.imgH = img.naturalHeight;
    log(`refframe ${state.imgW}×${state.imgH}`);

    // 2) auto-detect proposal
    log('POST /api/auto_trim/detect_roi ...');
    const r = await fetch('/api/auto_trim/detect_roi', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(videoIdentBody()),
    });
    if (!r.ok) throw new Error(`detect_roi ${r.status}: ${await r.text()}`);
    const det = await r.json();
    state.detectorResult = det;
    // Always trust the fresh detector output. The previous behaviour was to
    // prefer project.info.roi_quadrilateral when present, but that field is
    // project-level, not video-level — when the operator switched videos
    // without restarting the page, video 1's confirmed corners would
    // override video 2's correct detection. saved corners are still
    // written on confirm (kept for forward compatibility with Phase 1b
    // render which may consume them) but no longer drive the modal.
    state.corners = det.corners.map((p) => [p[0], p[1]]);
    state.wasEdited = false;
    log(`detector: method=${det.method} conf=${det.confidence.toFixed(2)}`);
    if (det.debug && Object.keys(det.debug).length) {
      log(`debug: ${JSON.stringify(det.debug)}`);
    }
    updateInfoPanel();
    redraw();
    els.confirm.disabled = false;
  } catch (e) {
    log(`ERROR: ${e.message || e}`);
    toast(`Auto-trim error: ${e.message || e}`);
  } finally {
    setLoading(false);
  }
}

async function refreshGroundtruthCount() {
  try {
    const r = await fetch('/api/auto_trim/groundtruth_count');
    if (!r.ok) throw new Error(r.status);
    const j = await r.json();
    els.gtCount.textContent = `${j.count} / 10`;
    els.gtCount.classList.toggle('text-success-400', j.count >= 10);
  } catch (e) {
    els.gtCount.textContent = '—';
  }
}

async function onConfirmClick() {
  if (state.confirmed) {
    closeModal();
    return;
  }
  if (!state.corners) return;
  els.confirm.disabled = true;
  try {
    snapshot();
    project.info.roi_quadrilateral = state.corners.map((p) => [p[0], p[1]]);

    const body = {
      ...videoIdentBody(),
      corners: state.corners,
      was_edited: state.wasEdited,
    };
    log('POST /api/auto_trim/confirm_roi ...');
    const r = await fetch('/api/auto_trim/confirm_roi', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`confirm_roi ${r.status}: ${await r.text()}`);
    const j = await r.json();
    log(`saved → ${j.saved_to} (history: ${j.history_count})`);
    log('Modal stays open. Trim detection is NOT yet wired — see banner above.');
    toast(state.wasEdited
      ? 'ROI corrected — saved as groundtruth'
      : 'ROI confirmed as-detected');
    await refreshGroundtruthCount();
    // Toggle button so a 2nd click closes (instead of re-POSTing).
    state.confirmed = true;
    els.confirm.textContent = '✓ Saved — close modal';
    els.confirm.disabled = false;
  } catch (e) {
    log(`ERROR: ${e.message || e}`);
    toast(`Save failed: ${e.message || e}`);
    els.confirm.disabled = false;
  }
}

// ----- canvas rendering -----------------------------------------------------

function redraw() {
  const cv = els.canvas;
  const wrap = els.canvasWrap;
  if (!state.imgEl || !state.corners) {
    const ctx = cv.getContext('2d');
    ctx.clearRect(0, 0, cv.width, cv.height);
    return;
  }
  // Fit canvas to wrapper while preserving image aspect
  const wrapRect = wrap.getBoundingClientRect();
  const aspect = state.imgW / state.imgH;
  let dispW, dispH;
  if (wrapRect.width / wrapRect.height > aspect) {
    dispH = wrapRect.height;
    dispW = dispH * aspect;
  } else {
    dispW = wrapRect.width;
    dispH = dispW / aspect;
  }
  const dpr = window.devicePixelRatio || 1;
  cv.width  = Math.round(dispW * dpr);
  cv.height = Math.round(dispH * dpr);
  cv.style.width  = `${dispW}px`;
  cv.style.height = `${dispH}px`;

  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.imageSmoothingEnabled = true;

  // background image
  ctx.drawImage(state.imgEl, 0, 0, dispW, dispH);

  // polygon overlay
  const pts = state.corners.map(([x, y]) => [x * dispW, y * dispH]);
  ctx.save();
  ctx.fillStyle = 'rgba(60, 220, 60, 0.18)';
  ctx.strokeStyle = 'rgba(60, 220, 60, 0.95)';
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);
  for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
  ctx.closePath();
  ctx.fill();
  ctx.stroke();
  ctx.restore();

  // corner handles + labels
  const editMode = els.editMode.checked;
  for (let i = 0; i < 4; i++) {
    const [x, y] = pts[i];
    ctx.save();
    ctx.fillStyle = editMode ? 'rgba(255, 220, 60, 1)' : 'rgba(120, 220, 255, 1)';
    ctx.strokeStyle = 'rgba(20, 20, 20, 0.9)';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(x, y, editMode ? 9 : 6, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = 'rgba(255, 255, 255, 0.95)';
    ctx.font = '12px monospace';
    ctx.textBaseline = 'middle';
    ctx.fillText(CORNER_LABELS[i], x + 12, y);
    ctx.restore();
  }

  // readout
  els.cornerReadout.textContent =
    'corners: ' + state.corners
      .map(([x, y], i) => `${CORNER_LABELS[i]}(${(x * 100).toFixed(1)}%, ${(y * 100).toFixed(1)}%)`)
      .join('  ');

  drawCornerZooms();
}

// Per-corner zoom insets. 3× magnification of the area around each corner,
// crosshair at the exact corner position. Lets the operator judge alignment
// against the physical table edge at near-pixel precision without needing a
// GT overlay (which they don't have in production).
const ZOOM_FACTOR = 3;
function drawCornerZooms() {
  const panels = [els.zoomTL, els.zoomTR, els.zoomBR, els.zoomBL];
  if (!state.imgEl || !state.corners) {
    panels.forEach((cv) => {
      const ctx = cv.getContext('2d');
      ctx.clearRect(0, 0, cv.width, cv.height);
    });
    return;
  }
  for (let i = 0; i < 4; i++) {
    const cv = panels[i];
    const W = cv.width;
    const H = cv.height;
    const ctx = cv.getContext('2d');
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, W, H);
    const [nx, ny] = state.corners[i];
    // Pixel coordinates in the source image
    const px = nx * state.imgW;
    const py = ny * state.imgH;
    const cropW = W / ZOOM_FACTOR;
    const cropH = H / ZOOM_FACTOR;
    const sx = px - cropW / 2;
    const sy = py - cropH / 2;
    ctx.imageSmoothingEnabled = false;  // crisp pixel zoom
    try {
      ctx.drawImage(state.imgEl, sx, sy, cropW, cropH, 0, 0, W, H);
    } catch (e) { /* off-edge crops fall back to black bg */ }
    // crosshair at center pointing to the exact corner pixel
    const cx = W / 2;
    const cy = H / 2;
    ctx.strokeStyle = 'rgba(60, 255, 60, 0.95)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    // gap in the middle so the crosshair doesn't obscure the pixel itself
    ctx.moveTo(cx - 18, cy); ctx.lineTo(cx - 5, cy);
    ctx.moveTo(cx + 5, cy);  ctx.lineTo(cx + 18, cy);
    ctx.moveTo(cx, cy - 18); ctx.lineTo(cx, cy - 5);
    ctx.moveTo(cx, cy + 5);  ctx.lineTo(cx, cy + 18);
    ctx.stroke();
    // 1-pixel dot exactly on the corner
    ctx.fillStyle = 'rgba(60, 255, 60, 1)';
    ctx.fillRect(cx - 1, cy - 1, 2, 2);
  }
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`image load failed: ${url}`));
    img.src = url;
  });
}

// ----- corner dragging ------------------------------------------------------

function canvasPointToNormalized(ev) {
  const rect = els.canvas.getBoundingClientRect();
  const x = (ev.clientX - rect.left) / rect.width;
  const y = (ev.clientY - rect.top) / rect.height;
  return [Math.max(0, Math.min(1, x)), Math.max(0, Math.min(1, y))];
}

function findCornerAt(nx, ny) {
  if (!state.corners) return -1;
  const rect = els.canvas.getBoundingClientRect();
  const rEdit = 14;  // pixels
  for (let i = 0; i < 4; i++) {
    const [cx, cy] = state.corners[i];
    const dx = (cx - nx) * rect.width;
    const dy = (cy - ny) * rect.height;
    if (dx * dx + dy * dy <= rEdit * rEdit) return i;
  }
  return -1;
}

els.canvas.addEventListener('mousedown', (ev) => {
  if (!els.editMode.checked) return;
  const [nx, ny] = canvasPointToNormalized(ev);
  const idx = findCornerAt(nx, ny);
  if (idx >= 0) {
    state.dragging = idx;
    state.wasEdited = true;
    ev.preventDefault();
  }
});

window.addEventListener('mousemove', (ev) => {
  if (state.dragging < 0) return;
  const [nx, ny] = canvasPointToNormalized(ev);
  state.corners[state.dragging] = [nx, ny];
  redraw();
});

window.addEventListener('mouseup', () => {
  state.dragging = -1;
});

// ----- info panel + log -----------------------------------------------------

function updateInfoPanel() {
  const det = state.detectorResult;
  els.infoVideo.textContent = det?.video_name || '—';
  els.infoVideo.title = det?.video_name || '';

  const method = det?.method || '—';
  els.infoMethod.textContent = method;
  els.infoMethod.title = method;
  // Color-code the method tag so it's obvious whether we got a learned
  // (operator-trained) result or a naive heuristic fallback.
  els.infoMethod.classList.remove('text-success-400', 'text-warn-300', 'text-slate-200');
  if (method.startsWith('learned_nn:')) {
    els.infoMethod.classList.add('text-success-400');
  } else if (method.startsWith('color_')) {
    els.infoMethod.classList.add('text-warn-300');
  } else {
    els.infoMethod.classList.add('text-slate-200');
  }

  els.infoConf.textContent = det
    ? `${(det.confidence * 100).toFixed(0)}%`
    : '—';
  els.infoSource.textContent = state.videoToken ? 'external' : 'videos/';
  els.status.textContent = state.wasEdited
    ? '[edited]'
    : (det && det.method === 'default' ? '[fallback]' : '[auto]');

  // Top-3 nearest groundtruth examples (always shown when known)
  const top3 = det?.debug?.nn_top3;
  if (Array.isArray(top3) && top3.length) {
    els.infoNn.classList.remove('hidden');
    els.infoNnList.innerHTML = top3.map((row, i) => {
      const sim = (row.sim * 100).toFixed(0);
      const used = method.startsWith('learned_nn:') && i === 0;
      const label = `${used ? '→' : ' '} ${row.video}`;
      return `<li class="${used ? 'text-success-400' : 'text-slate-400'}">${escapeHtml(label)} <span class="text-slate-500">sim=${sim}%</span></li>`;
    }).join('');
  } else {
    els.infoNn.classList.add('hidden');
    els.infoNnList.innerHTML = '';
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function log(msg) {
  const t = new Date().toISOString().slice(11, 19);
  els.log.textContent += `[${t}] ${msg}\n`;
  els.log.scrollTop = els.log.scrollHeight;
}

function clearLog() {
  els.log.textContent = '';
}

function setLoading(on) {
  els.canvasLoading.classList.toggle('hidden', !on);
  els.canvasLoading.classList.toggle('flex', on);
}

// ----- buttons --------------------------------------------------------------

els.close.addEventListener('click', closeModal);
els.cancel.addEventListener('click', closeModal);
els.confirm.addEventListener('click', onConfirmClick);

els.redetect.addEventListener('click', async () => {
  state.wasEdited = false;
  state.confirmed = false;
  els.confirm.textContent = '✓ Confirm ROI';
  await loadRefframeAndDetect();
});

els.resetDefault.addEventListener('click', () => {
  state.corners = DEFAULT_CORNERS.map((p) => [p[0], p[1]]);
  state.wasEdited = true;
  updateInfoPanel();
  redraw();
});

els.editMode.addEventListener('change', redraw);
els.clearLog.addEventListener('click', clearLog);

window.addEventListener('resize', () => {
  if (state.open) redraw();
});

// Keyboard escape
window.addEventListener('keydown', (ev) => {
  if (!state.open) return;
  if (ev.key === 'Escape') closeModal();
});
