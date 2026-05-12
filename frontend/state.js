// Mutable in-memory state shared by every other module. Object/array
// references are exported directly so importers can mutate them in
// place; scalars live on `mut` because ES module bindings are
// read-only from the importer side.
//
// State model:
//
//   project = {
//     info: {
//       tournament, match_type, p1, p2, p3, p4,
//       p1_team, p2_team, video_file, best_of,
//     },
//     trim_segments: [{ start, end }],
//     highlights:    [{ start, end, slow_mo, label }],
//     score_events:  [{ timestamp, who, p1_score, p2_score, p1_set, p2_set }],
//   }
//
//   match_type = "single" | "double"
//     single: P1 vs P2 (P3/P4 unused). Scoreboard rows = P1, P2.
//     double: P1+P3 (team 1) vs P2+P4 (team 2). Scoreboard rows show
//             combined names "lastTwo(P1) + lastTwo(P3)" and likewise
//             for team 2. The combine is done on the backend so the
//             preview and the burned-in render produce identical text.
//
//   live = { p1, p2, p1_set, p2_set }   // current live score (derived
//                                         // from the most recent score
//                                         // event, or 0s)
//
//   mut.pendingHighlightStart  number|null   set on first H, cleared on second H
//   mut.pendingTrimStart       number|null   set on T, cleared on Y
//   mut.externalToken          string|null   session token for the active external video
//   mut.lastSourcedFile        string        memo to avoid redundant <video> reloads
//   mut.pollTimer              number|null   render-progress polling interval id

export const project = {
  info: {
    tournament: '',
    match_type: 'single',
    p1: 'Player 1',
    p2: 'Player 2',
    p3: '',
    p4: '',
    p1_team: '',
    p2_team: '',
    video_file: '',
    best_of: 5,
  },
  trim_segments: [],
  highlights: [],
  score_events: [],
};

export const live = { p1: 0, p2: 0, p1_set: 0, p2_set: 0 };

export const undoStack = [];

export const mut = {
  pendingHighlightStart: null,
  pendingTrimStart: null,
  externalToken: null,
  lastSourcedFile: '',
  pollTimer: null,
};

export function snapshot() {
  undoStack.push({
    project: JSON.parse(JSON.stringify(project)),
    live: { ...live },
    pendingHighlightStart: mut.pendingHighlightStart,
  });
  if (undoStack.length > 100) undoStack.shift();
}
