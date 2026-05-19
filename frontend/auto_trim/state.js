// Module-level DOM refs + session state for the Auto-Trim modal.
//
// Everything that needs to be shared between the modal's sub-modules
// (canvas rendering, dragging, backend calls, info panel) lives here.
// Constants too — keeping them next to the state avoids circular imports.

import { $ } from '../dom.js';


export const els = {
  modal: $('modal-auto-trim'),
  canvas: $('at-canvas'),
  canvasWrap: $('at-canvas-wrap'),
  canvasEmpty: $('at-canvas-empty'),
  canvasLoading: $('at-canvas-loading'),
  editMode: $('at-edit-mode'),
  cornerReadout: $('at-corner-readout'),
  zoomTL: $('at-zoom-tl'),
  zoomTR: $('at-zoom-tr'),
  zoomBR: $('at-zoom-br'),
  zoomBL: $('at-zoom-bl'),
  status: $('at-roi-status'),
  infoVideo: $('at-info-video'),
  infoMethod: $('at-info-method'),
  infoConf: $('at-info-conf'),
  infoSource: $('at-info-source'),
  infoNn: $('at-info-nn'),
  infoNnList: $('at-info-nn-list'),
  log: $('at-log'),
  gtCount: $('at-gt-count'),
  redetect: $('at-redetect'),
  resetDefault: $('at-reset-default'),
  clearLog: $('at-clear-log'),
  confirm: $('at-confirm'),
  cancel: $('at-cancel'),
  close: $('at-close'),
};

// Mutable session state for the open modal. Reset by openAutoTrimModal()
// at every open so the modal never inherits stale state from a prior
// session.
export const state = {
  open: false,
  videoName: null,    // basename inside videos_dir, OR null when using token
  videoToken: null,   // external-video token, OR null when using name
  refframeUrl: null,
  imgEl: null,         // loaded HTMLImageElement
  imgW: 0,
  imgH: 0,
  corners: null,       // [[x, y], ...] normalized 0-1, TL TR BR BL
  detectorResult: null,  // most recent /api/auto_trim/detect_roi response
  wasEdited: false,
  dragging: -1,        // index of corner being dragged, or -1
  confirmed: false,    // true after a successful confirm POST in this session
};

export const DEFAULT_CORNERS = [
  [0.25, 0.55],
  [0.65, 0.55],
  [0.67, 0.85],
  [0.23, 0.85],
];

export const CORNER_LABELS = ['TL', 'TR', 'BR', 'BL'];
