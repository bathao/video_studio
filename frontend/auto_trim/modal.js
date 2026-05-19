// Modal show/hide primitives.
//
// Split out from index.js so api.js can call closeModal() without
// pulling in index.js's button-binding side effects (which would form
// a cycle: index → api → index).

import { els, state } from './state.js';


export function closeModal() {
  state.open = false;
  els.modal.classList.add('hidden');
  els.modal.classList.remove('flex');
}
