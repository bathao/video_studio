// Intro preview — renders ONLY the ~4s intro clip through the exact
// production code path (POST /api/preview/intro calls the same
// `render_intro_clip` the render pipeline uses) and plays it in a
// modal. Long titles that overflow, missing avatars, wrong style —
// all visible in seconds instead of after a full render. The backend
// caches by content key, so re-opening an unchanged preview is
// instant.
//
// Single-writer rule: this module owns #modal-intro-preview internals.
import { $ } from './dom.js';
import { mut, project } from './state.js';
import { toast } from './toast.js';

let _syncInfoFromInputs = () => {};
export function setSyncInfoFromInputs(fn) { _syncInfoFromInputs = fn; }

function closeModal() {
  const vid = $('ip-video');
  vid.pause();
  vid.removeAttribute('src');
  vid.load(); // release the network stream now, not at GC time
  $('modal-intro-preview').classList.add('hidden');
  $('modal-intro-preview').classList.remove('flex');
}

function openModal(url, noteLines) {
  $('ip-note').textContent = noteLines.join(' ');
  $('modal-intro-preview').classList.remove('hidden');
  $('modal-intro-preview').classList.add('flex');
  const vid = $('ip-video');
  vid.src = url;
  vid.play().catch(() => {}); // autoplay may be blocked — controls remain
}

async function onPreviewClick() {
  _syncInfoFromInputs();
  if (!project.info.video_file) return toast('Pick a video first');
  const cinematic = $('opt-intro-cinematic').checked;
  const textIntro = $('opt-intro-text').checked;
  if (!cinematic && !textIntro) {
    return toast('Intro is disabled — tick one of the intro styles first');
  }

  const btn = $('btn-intro-preview');
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⏳ Rendering intro preview…';
  try {
    const r = await fetch('/api/preview/intro', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        project,
        intro_style: cinematic ? 'cinematic' : 'text',
        token: mut.externalToken || null,
      }),
    });
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch { /* not json */ }
      toast(`Preview failed: ${detail || `HTTP ${r.status}`}`);
      return;
    }
    const data = await r.json();
    const notes = [];
    if (cinematic && !data.used_cinematic) {
      notes.push('⚠ Fell back to the TEXT intro — at least one required player photo is missing (even the placeholder).');
    }
    if (data.placeholders && data.placeholders.length) {
      notes.push(`Placeholder avatar used for: ${data.placeholders.join(', ')} — drop a photo into assets/avatars/.`);
    }
    notes.push('Rendered by the same code as the real render — what you see is what you get.');
    openModal(data.url, notes);
  } catch {
    toast('Preview failed: cannot reach backend');
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

$('btn-intro-preview').addEventListener('click', onPreviewClick);
$('ip-close').addEventListener('click', closeModal);
$('modal-intro-preview').addEventListener('click', (e) => {
  if (e.target === $('modal-intro-preview')) closeModal();
});
window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('modal-intro-preview').classList.contains('hidden')) {
    closeModal();
  }
});
