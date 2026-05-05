// Tiny DOM helper. Exported so every other module can grab elements
// by id without each one re-declaring its own `$`.
export const $ = (id) => document.getElementById(id);
