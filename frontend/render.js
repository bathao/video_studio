// Start a render job + poll its progress. Renders run in a worker
// thread on the backend; the frontend posts the project + render
// flags, gets a job_id back, and polls /api/render/{id} every 600 ms
// until status is "done" or "error".
import { $ } from './dom.js';
import { ensureTrimsAndRender, setStartRender } from './render_chain.js';
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
    output_name: $('in-output').value || null,
  };
  let r;
  try {
    r = await fetch('/api/render', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch {
    toast('Render failed: cannot reach backend');
    return;
  }
  if (!r.ok) {
    const err = await r.text();
    toast(`Render failed: ${err}`);
    return;
  }
  const data = await r.json();
  $('render-status').classList.remove('hidden');
  $('rs-output').classList.add('hidden');
  // Reveal Cancel for the duration of the run; pollRender hides it on
  // terminal status.
  const cancelBtn = $('rs-cancel');
  cancelBtn.classList.remove('hidden');
  cancelBtn.disabled = false;
  cancelBtn.textContent = 'Cancel render';
  cancelBtn.onclick = () => cancelRender(data.job_id);
  $('btn-render').disabled = true;
  pollRender(data.job_id);
}

async function cancelRender(jobId) {
  const btn = $('rs-cancel');
  btn.disabled = true;
  btn.textContent = 'Cancelling…';
  try {
    const r = await fetch(`/api/render/${jobId}/cancel`, { method: 'POST' });
    if (!r.ok) {
      const err = await r.text();
      toast(`Cancel failed: ${err}`);
      btn.disabled = false;
      btn.textContent = 'Cancel render';
    }
  } catch (e) {
    toast('Cancel error');
    btn.disabled = false;
    btn.textContent = 'Cancel render';
  }
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
      const terminal = (j.status === 'done' || j.status === 'error' || j.status === 'cancelled');
      if (terminal) {
        clearInterval(mut.pollTimer);
        $('rs-cancel').classList.add('hidden');
        $('btn-render').disabled = false;
      }
      if (j.status === 'done') {
        const out = j.output_path || '';
        const fname = out.replace(/\\/g, '/').split('/').pop();
        if (fname) {
          $('rs-output').classList.remove('hidden');
          $('rs-output-path').textContent = out;
          $('rs-output-link').href = `/api/output/${encodeURIComponent(fname)}`;
          $('rs-output-reveal').onclick = async () => {
            try {
              await fetch(`/api/output/${encodeURIComponent(fname)}/reveal`, { method: 'POST' });
            } catch {
              toast('Reveal failed: cannot reach backend');
            }
          };
        }
        toast('Render done');
      } else if (j.status === 'error') {
        toast('Render error');
      } else if (j.status === 'cancelled') {
        toast('Render cancelled');
      }
    } catch (e) {
      console.error(e);
    }
  }, 600);
}

// The Render button goes through the auto-trim chain (render_chain.js):
// it guarantees auto trims exist — detecting them first when missing —
// before startRender is invoked. startRender itself stays the raw
// "post the plan, poll the job" entry and is handed to the chain here.
setStartRender(startRender);
$('btn-render').addEventListener('click', () => ensureTrimsAndRender());

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
  try {
    await fetch('/api/output-folder/open', { method: 'POST' });
  } catch {
    toast('Cannot open output folder: backend unreachable');
  }
});
