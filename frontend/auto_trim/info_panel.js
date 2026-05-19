// Info-panel rendering: video name, detection method tag, confidence,
// source kind, edit-state badge, top-3 nearest groundtruth examples.
//
// Pure read-only side: consumes `state.detectorResult` + `state` flags
// and writes them into the DOM nodes captured in `els`. Called from
// api.js after every detect call and from index.js after the
// "Reset to default" button.

import { els, state } from './state.js';


export function updateInfoPanel() {
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
