// Canvas rendering + corner dragging for the Auto-Trim modal.
//
// `redraw()` paints the refframe + ROI polygon + per-corner zoom panels
// on every change (initial detect, drag, edit-mode toggle, window
// resize). `drawCornerZooms()` is a sub-step but lives here because it
// shares the source image + corner state with the main canvas.
//
// Corner dragging: in edit mode, mousedown on a corner handle starts a
// drag, mousemove updates the corner, mouseup ends it. The 3 event
// listeners are wired at module load time — importing this module
// arms them. `state.dragging` is the index of the corner currently
// being dragged, or -1.

import { CORNER_LABELS, els, state } from './state.js';


export function redraw() {
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

export function loadImage(url) {
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
