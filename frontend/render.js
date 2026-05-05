// Start a render job + poll its progress. Renders run in a worker
// thread on the backend; the frontend posts the project + render
// flags, gets a job_id back, and polls /api/render/{id} every 600 ms
// until status is "done" or "error".
import { $ } from './dom.js';
import { mut, project } from './state.js';
import { toast } from './toast.js';

let _syncInfoFromInputs = () => {};
export function setSyncInfoFromInputs(fn) { _syncInfoFromInputs = fn; }

async function startRender() {
  _syncInfoFromInputs();
  if (!project.info.video_file) return toast('Pick a video first');
  const name = ($('in-project').value || 'match').trim();
  const cinematic = $('opt-intro-cinematic').checked;
  const textIntro = $('opt-intro-text').checked;
  const body = {
    project_name: name,
    project,
    include_intro: cinematic || textIntro,
    intro_style: cinematic ? 'cinematic' : 'text',
    include_highlights: $('opt-hl').checked,
    include_main: $('opt-main').checked,
    output_name: $('in-output').value || null,
  };
  const r = await fetch('/api/render', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const err = await r.text();
    toast(`Render failed: ${err}`);
    return;
  }
  const data = await r.json();
  $('render-status').classList.remove('hidden');
  $('rs-output').classList.add('hidden');
  pollRender(data.job_id);
}

function pollRender(jobId) {
  if (mut.pollTimer) clearInterval(mut.pollTimer);
  mut.pollTimer = setInterval(async () => {
    try {
      const r = await fetch(`/api/render/${jobId}`);
      if (!r.ok) return;
      const j = await r.json();
      const pct = Math.round((j.progress || 0) * 100);
      $('rs-stage').textContent = j.stage || j.status;
      $('rs-pct').textContent = `${pct}%`;
      $('rs-bar').style.width = `${pct}%`;
      $('rs-msg').textContent = j.message || '';
      if (j.status === 'done') {
        clearInterval(mut.pollTimer);
        const out = j.output_path || '';
        const fname = out.replace(/\\/g, '/').split('/').pop();
        if (fname) {
          $('rs-output').classList.remove('hidden');
          $('rs-output-path').textContent = out;
          $('rs-output-link').href = `/api/output/${encodeURIComponent(fname)}`;
          $('rs-output-reveal').onclick = async () => {
            await fetch(`/api/output/${encodeURIComponent(fname)}/reveal`, { method: 'POST' });
          };
        }
        toast('Render done');
      } else if (j.status === 'error') {
        clearInterval(mut.pollTimer);
        toast('Render error');
      }
    } catch (e) {
      console.error(e);
    }
  }, 600);
}

$('btn-render').addEventListener('click', startRender);

// Intro style — radio-like behaviour: ticking one auto-unticks the
// other so cinematic and text never run simultaneously. Both can
// remain unticked → no intro at all.
$('opt-intro-cinematic').addEventListener('change', (e) => {
  if (e.target.checked) $('opt-intro-text').checked = false;
});
$('opt-intro-text').addEventListener('change', (e) => {
  if (e.target.checked) $('opt-intro-cinematic').checked = false;
});
$('btn-open-output').addEventListener('click', async () => {
  await fetch('/api/output-folder/open', { method: 'POST' });
});
