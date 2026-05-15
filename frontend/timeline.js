// Live timeline summary under the Live Score panel.
//
// Shows three derived totals that update on every highlight / trim
// edit and every change to the render-stage checkboxes:
//
//   - Highlights total (sum of all `(end - start)` ranges).
//   - Trimmed total (sum of all `(end - start)` ranges removed from
//     the source).
//   - Output estimate: intro + main (kept source - trims) + per-replay
//     slow-mo stretch + per-replay stinger brackets + outro.
//
// Constants mirror the backend defaults — if the operator changes
// these in config.json the estimate drifts. Accepted because this is
// a status display, not a contract.
//
//   - INTRO_DUR_CINEMATIC  → config.intro_duration_seconds (4.0)
//   - INTRO_DUR_TEXT       → render_intro hard-codes 3.0 s
//   - REPLAY_SPEED         → renderer.REPLAY_SPEED (0.5 → 2× duration)
//   - STINGER_IN_DUR       → config.stinger_duration_seconds (2.0)
//   - STINGER_OUT_DUR      → config.stinger_out_duration_seconds (0.6)
//   - OUTRO_DUR            → config.outro_duration_seconds (5.0)

import { $ } from './dom.js';
import { fmt } from './timecode.js';
import { project } from './state.js';
import { player } from './player.js';

const INTRO_DUR_CINEMATIC = 4.0;
const INTRO_DUR_TEXT = 3.0;
const REPLAY_SPEED = 0.5;
const STINGER_IN_DUR = 2.0;
const STINGER_OUT_DUR = 0.6;
const OUTRO_DUR = 5.0;


export function syncTimeline() {
  const hlCount = project.highlights.length;
  const hlTotal = project.highlights.reduce(
    (sum, h) => sum + Math.max(0, h.end - h.start), 0,
  );
  const trimTotal = project.trim_segments.reduce(
    (sum, t) => sum + Math.max(0, t.end - t.start), 0,
  );

  // Render-options gate each stage's contribution to the output.
  const cinematic = $('opt-intro-cinematic').checked;
  const textIntro = $('opt-intro-text').checked;
  const includeReplays = $('opt-replays').checked && hlCount > 0;
  const includeMain = $('opt-main').checked;

  const introDur = cinematic ? INTRO_DUR_CINEMATIC
                 : textIntro ? INTRO_DUR_TEXT
                 : 0;
  const baseMain = includeMain ? Math.max(0, (player.duration || 0) - trimTotal) : 0;
  // Each highlight gets a 50%-speed replay spliced into main, so the
  // main contribution stretches by `sum(highlight_dur) / REPLAY_SPEED`.
  // Each replay is also bracketed by a sting-in + sting-out clip.
  const replayDur = (includeReplays && includeMain) ? hlTotal / REPLAY_SPEED : 0;
  const stingerDur = (includeReplays && includeMain)
    ? hlCount * (STINGER_IN_DUR + STINGER_OUT_DUR)
    : 0;
  // Outro tags onto main; only contributes when main is on.
  const outroDur = includeMain ? OUTRO_DUR : 0;

  const outputDur = introDur + baseMain + replayDur + stingerDur + outroDur;

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
