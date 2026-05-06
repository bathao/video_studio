// Live timeline summary under the Live Score panel.
//
// Shows three derived totals that update on every highlight / trim
// edit and every change to the render-stage checkboxes:
//
//   - Highlights total (with slow-mo expansion factored in — the
//     last 2.5 s of any slow_mo highlight runs at half speed in the
//     renderer, so the rendered duration is +2.5 s).
//   - Trimmed total (sum of all `(end - start)` ranges removed from
//     the source).
//   - Output estimate (intro + highlight reel + bridge + main, where
//     each contribution is gated on the matching checkbox).
//
// All constants here mirror the backend defaults:
//   - SLOWMO_TAIL          → renderer.SLOWMO_TAIL_SECONDS
//   - INTRO_DUR_CINEMATIC  → config.intro_duration_seconds (default 4.0)
//   - INTRO_DUR_TEXT       → render_intro hard-codes 3.0 s
//   - BRIDGE_DUR           → render_transition hard-codes 0.8 s
// If any of those defaults change in the backend, the estimate will
// drift; that's accepted because this is a status display, not a
// contract.

import { $ } from './dom.js';
import { fmt } from './timecode.js';
import { project } from './state.js';
import { player } from './player.js';

const SLOWMO_TAIL = 2.5;
const INTRO_DUR_CINEMATIC = 4.0;
const INTRO_DUR_TEXT = 3.0;
const BRIDGE_DUR = 0.8;


function expandedHighlightDuration(h) {
  const d = Math.max(0, h.end - h.start);
  // Matches `apply_slowmo` in renderer._render_one_highlight: only
  // applies when the source range is meaningfully longer than the
  // tail, otherwise the slow-mo flag is silently ignored.
  if (h.slow_mo && d > SLOWMO_TAIL + 0.2) {
    return d + SLOWMO_TAIL;   // tail of 2.5 s plays at 2× → +2.5 s
  }
  return d;
}


export function syncTimeline() {
  const hlTotal = project.highlights.reduce(
    (sum, h) => sum + expandedHighlightDuration(h), 0,
  );
  const trimTotal = project.trim_segments.reduce(
    (sum, t) => sum + Math.max(0, t.end - t.start), 0,
  );

  // Render-options gate each stage's contribution to the output.
  const cinematic = $('opt-intro-cinematic').checked;
  const textIntro = $('opt-intro-text').checked;
  const includeHl = $('opt-hl').checked && project.highlights.length > 0;
  const includeMain = $('opt-main').checked;

  const introDur = cinematic ? INTRO_DUR_CINEMATIC
                 : textIntro ? INTRO_DUR_TEXT
                 : 0;
  const bridgeDur = (includeHl && includeMain) ? BRIDGE_DUR : 0;
  const mainDur = includeMain ? Math.max(0, (player.duration || 0) - trimTotal) : 0;
  const hlDur = includeHl ? hlTotal : 0;
  const outputDur = introDur + hlDur + bridgeDur + mainDur;

  $('tl-hl').textContent = fmt(hlTotal);
  $('tl-trim').textContent = fmt(trimTotal);
  $('tl-output').textContent = fmt(outputDur);
}


// Auto-refresh whenever any input that feeds the calculation changes.
// (Highlight / trim list edits already call syncTimeline directly from
// their sync* functions, so we only need listeners for the render
// checkboxes + video duration here.)
['opt-intro-cinematic', 'opt-intro-text', 'opt-hl', 'opt-main'].forEach((id) => {
  $(id).addEventListener('change', syncTimeline);
});
player.addEventListener('durationchange', syncTimeline);
player.addEventListener('loadedmetadata', syncTimeline);
