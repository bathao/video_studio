// Save / Load Project — file dialog + REST round-trip + project list
// modal. The actual UI sync happens via the syncAllUI callback the
// caller passes in (so this module doesn't need to import the entire
// orchestration layer).
import { $ } from './dom.js';
import { project } from './state.js';
import { migrateLegacyEvents, syncLiveFromTime } from './score.js';
import { player } from './player.js';
import { toast } from './toast.js';

// `syncInfoFromInputs` is the small bit of orchestration that copies
// form inputs back into project.info before saving. The boot module
// (app.js) registers it via setSyncInfoFromInputs() at init.
let _syncInfoFromInputs = () => {};
export function setSyncInfoFromInputs(fn) { _syncInfoFromInputs = fn; }

// `syncAllUI` is the orchestrator that re-renders every panel after a
// project load. Same pattern — registered by app.js at boot to break
// the circular dep with the modules syncAllUI itself drives.
let _syncAllUI = () => {};
export function setSyncAllUI(fn) { _syncAllUI = fn; }

async function saveProject() {
  _syncInfoFromInputs();
  const name = ($('in-project').value || '').trim();
  if (!name) return toast('Set a project name');
  const btn = $('btn-save');
  btn.disabled = true;
  try {
    const r = await fetch(`/api/projects/${encodeURIComponent(name)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(project),
    });
    if (!r.ok) {
      toast(`Save failed: ${r.status}`);
      return;
    }
    toast('Saved');
  } catch {
    toast('Save failed: cannot reach backend');
  } finally {
    btn.disabled = false;
  }
}

async function openLoadModal() {
  let data;
  try {
    const r = await fetch('/api/projects');
    if (!r.ok) return toast(`Cannot list projects: ${r.status}`);
    data = await r.json();
  } catch {
    return toast('Cannot list projects: backend unreachable');
  }
  const ul = $('proj-list');
  ul.innerHTML = '';
  if (!data.projects.length) {
    ul.innerHTML = '<li class="text-slate-400 text-sm">No projects</li>';
  }
  for (const p of data.projects) {
    const li = document.createElement('li');
    li.className = 'list-row';
    const date = new Date(p.modified * 1000).toLocaleString();
    li.innerHTML = `<span class="flex-1">${p.name}</span><span class="text-slate-500">${date}</span>`;
    li.style.cursor = 'pointer';
    li.addEventListener('click', () => loadProject(p.name));
    ul.appendChild(li);
  }
  $('modal-load').classList.remove('hidden');
  $('modal-load').classList.add('flex');
}

async function loadProject(name) {
  let data;
  try {
    const r = await fetch(`/api/projects/${encodeURIComponent(name)}`);
    if (!r.ok) return toast('Load failed');
    data = await r.json();
  } catch {
    return toast('Load failed: backend unreachable');
  }
  Object.assign(project.info, data.info || {});
  project.trim_segments = data.trim_segments || [];
  project.highlights = data.highlights || [];
  project.score_events = data.score_events || [];
  // Legacy projects may not have `who` on each event — derive it from
  // the score diff with the previous event, then sort and recompute so
  // the events list is internally consistent.
  migrateLegacyEvents();
  syncLiveFromTime(player.currentTime);
  $('in-project').value = name;
  _syncAllUI();
  $('modal-load').classList.add('hidden');
  $('modal-load').classList.remove('flex');
  toast(`Loaded ${name}`);
}

$('btn-save').addEventListener('click', saveProject);
$('btn-load').addEventListener('click', openLoadModal);
$('btn-close-load').addEventListener('click', () => {
  $('modal-load').classList.add('hidden');
  $('modal-load').classList.remove('flex');
});
