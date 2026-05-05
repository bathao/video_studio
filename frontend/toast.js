// Floating toast at bottom-right. Auto-hides after ~1.8 s.
import { $ } from './dom.js';

let toastTimer = null;

export function toast(msg) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 1800);
}
