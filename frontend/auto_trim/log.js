// Lightweight log + loading UI helpers for the Auto-Trim modal.

import { els } from './state.js';


export function log(msg) {
  const t = new Date().toISOString().slice(11, 19);
  els.log.textContent += `[${t}] ${msg}\n`;
  els.log.scrollTop = els.log.scrollHeight;
}

export function clearLog() {
  els.log.textContent = '';
}

export function setLoading(on) {
  els.canvasLoading.classList.toggle('hidden', !on);
  els.canvasLoading.classList.toggle('flex', on);
}
