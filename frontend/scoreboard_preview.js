// Scoreboard preview overlay — renders the same .ass file the render
// pipeline burns into the final MP4, but in the browser via libass-WASM
// (JASSUB). Single source of truth: `build_scoreboard_ass_text` on the
// backend; this module just fetches it and hands it to JASSUB, which
// attaches a canvas to the `<video>` element and composites the
// scoreboard frame-by-frame in sync with playback.
//
// Lifecycle: the overlay auto-enables as soon as the player's source
// video reports `loadedmetadata` (so JASSUB has valid videoWidth /
// videoHeight to scale against — without them it produces NaN CSS
// dimensions). After that, every change to project info or score
// events refreshes the .ass via `syncScoreboardPreview`, debounced
// 250 ms so a keystroke-stream of name edits collapses to one fetch.
import { $ } from './dom.js';
import { project } from './state.js';
import { player } from './player.js';
import { fmt } from './timecode.js';
import { toast } from './toast.js';

const DEBOUNCE_MS = 250;
const VENDOR = '/static/vendor/jassub';

let jassub = null;
let pendingTimer = null;
let JASSUB_CLASS = null;
let initInFlight = false;
let lastSignature = null;
let lastFailureAt = 0;

function stateSignature() {
  // What actually changes the .ass output: project info, score events,
  // and the source video's dimensions / duration (which feed the
  // scoreboard's geometry scaling). Everything else — player.currentTime,
  // playback rate, etc. — is libass's own domain and doesn't need a
  // re-fetch.
  return JSON.stringify({
    info: project.info,
    events: project.score_events,
    w: player.videoWidth || 0,
    h: player.videoHeight || 0,
    d: Math.round((player.duration || 0) * 10),
  });
}

async function loadJassubClass() {
  if (JASSUB_CLASS) return JASSUB_CLASS;
  const mod = await import(`${VENDOR}/jassub.es.js`);
  JASSUB_CLASS = mod.default;
  return JASSUB_CLASS;
}

async function fetchAss() {
  const body = {
    project,
    video_w: player.videoWidth || 1920,
    video_h: player.videoHeight || 1080,
    duration: (isFinite(player.duration) && player.duration > 0)
      ? player.duration
      : 3600.0,
  };
  const r = await fetch('/api/preview/scoreboard.ass', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`Preview .ass fetch failed: ${r.status}`);
  return await r.text();
}

async function initJassub() {
  if (jassub || initInFlight) return;
  if (!(player.videoWidth > 0 && player.videoHeight > 0)) return;
  initInFlight = true;
  try {
    console.log(`[scoreboard-preview] video ready: ${player.videoWidth}×${player.videoHeight} · ${fmt(player.duration || 0)}`);
    const JASSUB = await loadJassubClass();
    const initialAss = await fetchAss();
    jassub = new JASSUB({
      video: player,
      subContent: initialAss,
      workerUrl: `${VENDOR}/jassub-worker.js`,
      wasmUrl: `${VENDOR}/jassub-worker.wasm`,
      modernWasmUrl: `${VENDOR}/jassub-worker-modern.wasm`,
      // Bundle ships a Liberation-Sans variant under default.woff2 —
      // metric clone of Arial with full Vietnamese coverage, so the
      // preview matches the real render's font metrics even though
      // the browser can't reach the system Arial.
      availableFonts: {
        'liberation sans': `${VENDOR}/default.woff2`,
        'arial': `${VENDOR}/default.woff2`,
      },
      fallbackFont: 'liberation sans',
    });
    lastSignature = stateSignature();
    console.log('[scoreboard-preview] JASSUB attached');
  } catch (e) {
    console.error('[scoreboard-preview] init failed', e);
    toast(`Scoreboard preview failed: ${e?.message || e}`);
  } finally {
    initInFlight = false;
  }
}

export function syncScoreboardPreview() {
  if (!jassub) return;
  if (stateSignature() === lastSignature) return;
  // Brief backoff after a failed refresh — the dirty signature retries
  // on every score/info change (and timeupdate tick), so without this
  // a down backend gets hammered every DEBOUNCE_MS.
  if (Date.now() - lastFailureAt < 3000) return;
  if (pendingTimer) clearTimeout(pendingTimer);
  pendingTimer = setTimeout(async () => {
    pendingTimer = null;
    // Re-read at fetch time — state may have changed during the
    // debounce window; this signature matches what fetchAss() sends.
    const sig = stateSignature();
    try {
      const ass = await fetchAss();
      if (jassub) jassub.setTrack(ass);
      // Mark clean only AFTER success. Advancing the signature before
      // the fetch meant a failed refresh looked up-to-date and the
      // overlay stayed stale until the next unrelated state change.
      lastSignature = sig;
      lastFailureAt = 0;
    } catch (e) {
      lastFailureAt = Date.now();
      console.warn('[scoreboard-preview] refresh failed', e);
    }
  }, DEBOUNCE_MS);
}

// Auto-enable as soon as a source video has metadata. Subsequent
// metadata events (new video picked from the dropdown / Browse picker)
// just refresh the .ass via `syncScoreboardPreview` — JASSUB's own
// ResizeObserver handles the canvas resize.
function onMetadataReady() {
  if (jassub) {
    syncScoreboardPreview();
  } else {
    initJassub();
  }
}
player.addEventListener('loadedmetadata', onMetadataReady);

// Handle the case where the page boots with metadata already loaded
// (e.g., after a hot-reload while a video is selected). The event
// won't fire in that case, so check explicitly once.
if (player.videoWidth > 0) onMetadataReady();
