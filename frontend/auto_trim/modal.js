// Modal show/hide primitives.
//
// Split out from index.js so api.js can call closeModal() without
// pulling in index.js's button-binding side effects (which would form
// a cycle: index → api → index).

import { abortDetection } from './detection.js';
import { els, state } from './state.js';


export function closeModal() {
  // Abort any in-flight detection job + close EventSource so we don't
  // leak streams when the operator closes the modal mid-run.
  abortDetection();
  state.open = false;
  els.modal.classList.add('hidden');
  els.modal.classList.remove('flex');
}
