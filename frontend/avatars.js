// Debounced thumbnail loader. When the player name input changes, look
// up `assets/avatars/<name>/preview` after a short pause and update
// the corresponding <img> next to the input. The cache-buster in the
// URL forces a refresh when the user replaces the file on disk
// without restarting the server.
import { $ } from './dom.js';

const _thumbDebounce = { p1: 0, p2: 0 };

export function refreshAvatarThumb(slot) {
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
