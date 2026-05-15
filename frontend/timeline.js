// Live timeline summary under the Live Score panel.
//
// Shows three derived totals that update on every highlight / trim
// edit and every change to the render-stage checkboxes:
//
//   - Highlights total (sum of all `(end - start)` ranges).
//   - Trimmed total (sum of all `(end - start)` ranges removed from
//     the source).
//   - Output estimate (intro + main + the per-highlight slow-mo replays
//     spliced into main).
//
// Constants mirror the backend defaults:
//   - INTRO_DUR_CINEMATIC  → config.intro_duration_seconds (default 4.0)
//   - INTRO_DUR_TEXT       → render_intro hard-codes 3.0 s
//   - REPLAY_SPEED         → renderer.REPLAY_SPEED (0.5 → 2× duration)
// If any of those defaults change in the backend, the estimate will
// drift; accepted because this is a status display, not a contract.

import { $ } from './dom.js';
import { fmt } from './timecode.js';
import { project } from './state.js';
import { player } from './player.js';

const INTRO_DUR_CINEMATIC = 4.0;
const INTRO_DUR_TEXT = 3.0;
const REPLAY_SPEED = 0.5;


export function syncTimeline() {
  const hlTotal = project.highlights.reduce(
    (sum, h) => sum + Math.max(0, h.end - h.start), 0,
  );
  const trimTotal = project.trim_segments.reduce(
    (sum, t) => sum + Math.max(0, t.end - t.start), 0,
  );

  // Render-options gate each stage's contribution to the output.
  const cinematic = $('opt-intro-cinematic').checked;
  const textIntro = $('opt-intro-text').checked;
  const includeReplays = $('opt-replays').checked && project.highlights.length > 0;
  const includeMain = $('opt-main').checked;

  const introDur = cinematic ? INTRO_DUR_CINEMATIC
                 : textIntro ? INTRO_DUR_TEXT
                 : 0;
  const baseMain = includeMain ? Math.max(0, (player.duration || 0) - trimTotal) : 0;
  // Each highlight gets a 50%-speed replay spliced into main, so the
  // main contribution stretches by `sum(highlight_dur) / REPLAY_SPEED`.
  const replayDur = (includeReplays && includeMain) ? hlTotal / REPLAY_SPEED : 0;
  const outputDur = introDur + baseMain + replayDur;

  $('tl-hl').textContent = fmt(hlTotal);
  $('tl-trim').textContent = fmt(trimTotal);
  $('tl-output').textContent = fmt(outputDur);
}


// Auto-refresh whenever any input that feeds the calculation changes.
// (Highlight / trim list edits already call syncTimeline directly from
// their sync* functions, so we only need listeners for the render
// checkboxes + video duration here.)
['opt-intro-cinematic', 'opt-intro-text', 'opt-replays', 'opt-main'].forEach((id) => {
  $(id).addEventListener('change', syncTimeline);
});
player.addEventListener('durationchange', syncTimeline);
player.addEventListener('loadedmetadata', syncTimeline);
