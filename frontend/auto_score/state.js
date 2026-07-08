// DOM refs + session state for the Live Score Auto tab.
//
// Review DATA (the proposal list + verdicts) lives in
// project.auto_score_draft so a half-finished review survives project
// save/load — modules must read it through draft() at use time, never
// cache the array (project_io may swap the field wholesale on load).
// Only transient session things (SSE handle, cursor, progress) live in
// the `state` object here.

import { $ } from '../dom.js';


export const els = {
  tabManualBtn: $('tab-score-manual'),
  tabAutoBtn: $('tab-score-auto'),
  tabManual: $('score-tab-manual'),
  tabAuto: $('score-tab-auto'),
  roiStatus: $('as-roi-status'),
  btnRoi: $('as-btn-roi'),
  btnDetect: $('as-btn-detect'),
  btnCancel: $('as-btn-cancel'),
  progressWrap: $('as-progress-wrap'),
  stage: $('as-stage'),
  status: $('as-status'),
  progress: $('as-progress'),
  reviewWrap: $('as-review-wrap'),
  reviewSummary: $('as-review-summary'),
  reviewList: $('as-review-list'),
  btnApply: $('as-btn-apply'),
  btnDiscard: $('as-btn-discard'),
  hint: $('as-hint'),
};

export const state = {
  autoTabActive: false,
  status: 'idle',      // 'idle' | 'running' | 'done' | 'cancelled' | 'error'
  jobId: null,
  eventSource: null,
  stage: '',
  progress: 0,
  cacheKey: null,
  cacheHit: false,
  cursor: 0,           // review-list row index (into visible rows)
  _lastLoggedPct: -1,
};

// A proposal = {id, t_start, t_end, who: 0|1|2, status: 'pending'|'accepted'|'deleted'}
// draft shape = {video_file, proposals: [...]}
