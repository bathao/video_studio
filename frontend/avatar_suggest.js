// Type-ahead name suggestions for the Setup panel player inputs,
// backed by the avatar roster (GET /api/avatars — every player with a
// photo on disk). Typing "Thụy" (or accent-less "thuy") drops a list
// of matching names with their photos; picking one fills the input
// and fires a normal 'input' event so the existing pipeline (undo
// snapshot, info sync, thumbnail refresh) runs untouched.
//
// Matching is accent-insensitive (NFD fold + đ→d) so the operator
// doesn't have to remember exact diacritics, and prefix matches rank
// above mid-name ones. Suggestions never restrict input — a brand-new
// player without a photo is typed as usual and the list just stays
// empty.
import { $ } from './dom.js';

const SLOTS = ['p1', 'p2', 'p3', 'p4'];
const MAX_ITEMS = 8;
const ROSTER_TTL_MS = 60_000;

let _roster = null;          // [{name, folded}]
let _rosterAt = 0;
let _activeInput = null;     // the input the dropdown belongs to
let _activeIndex = -1;       // highlighted row

// Accent fold: NFD strips combining marks; đ/Đ are standalone letters
// so they need their own mapping. Mirrors nothing backend — this is
// UI-only convenience, the authoritative lookup stays exact+note-rule.
function fold(s) {
  return (s || '')
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .replace(/đ/g, 'd')
    .replace(/Đ/g, 'D')
    .toLowerCase();
}

async function roster() {
  const now = Date.now();
  if (_roster && now - _rosterAt < ROSTER_TTL_MS) return _roster;
  try {
    const r = await fetch('/api/avatars');
    if (!r.ok) return _roster || [];
    const data = await r.json();
    _roster = (data.names || []).map((name) => ({ name, folded: fold(name) }));
    _rosterAt = now;
  } catch {
    // Backend unreachable — keep whatever we had; suggestions are
    // best-effort and must never block typing.
  }
  return _roster || [];
}

// One shared dropdown element, repositioned under whichever input is
// active. Inline styles on purpose: the Tailwind CDN JIT + the
// styles.css fallbacks only cover classes present in index.html.
const box = document.createElement('div');
box.id = 'avatar-suggest';
Object.assign(box.style, {
  position: 'fixed',
  zIndex: '50',
  display: 'none',
  minWidth: '220px',
  maxHeight: '320px',
  overflowY: 'auto',
  background: '#111827',
  border: '1px solid #374151',
  borderRadius: '8px',
  boxShadow: '0 8px 24px rgba(0,0,0,.5)',
});
document.body.appendChild(box);

function hide() {
  box.style.display = 'none';
  box.innerHTML = '';
  _activeInput = null;
  _activeIndex = -1;
}

function highlight(idx) {
  const rows = box.children;
  if (!rows.length) return;
  _activeIndex = ((idx % rows.length) + rows.length) % rows.length;
  for (let i = 0; i < rows.length; i++) {
    rows[i].style.background = i === _activeIndex ? '#1f2937' : 'transparent';
  }
  rows[_activeIndex].scrollIntoView({ block: 'nearest' });
}

function pick(name) {
  const input = _activeInput;
  hide();
  if (!input) return;
  input.value = name;
  // Fire the standard pipeline: snapshot burst + info sync + thumb.
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.focus();
}

function render(matches, input) {
  if (!matches.length) return hide();
  _activeInput = input;
  _activeIndex = -1;
  box.innerHTML = '';
  for (const m of matches) {
    const row = document.createElement('div');
    Object.assign(row.style, {
      display: 'flex', alignItems: 'center', gap: '10px',
      padding: '6px 10px', cursor: 'pointer',
      color: '#e5e7eb', fontSize: '14px', whiteSpace: 'nowrap',
    });
    const img = document.createElement('img');
    Object.assign(img.style, {
      width: '28px', height: '28px', borderRadius: '50%',
      objectFit: 'cover', background: '#1f2937', flexShrink: '0',
    });
    img.loading = 'lazy';
    img.src = `/api/avatars/${encodeURIComponent(m.name)}/preview`;
    const span = document.createElement('span');
    span.textContent = m.name;
    row.append(img, span);
    // mousedown beats the input's blur, so the pick lands before hide.
    row.addEventListener('mousedown', (e) => { e.preventDefault(); pick(m.name); });
    row.addEventListener('mouseenter', () => highlight([...box.children].indexOf(row)));
    box.appendChild(row);
  }
  const r = input.getBoundingClientRect();
  box.style.left = `${Math.round(r.left)}px`;
  box.style.top = `${Math.round(r.bottom + 4)}px`;
  box.style.display = 'block';
}

async function onType(input) {
  const q = fold(input.value.trim());
  if (!q) return hide();
  const list = await roster();
  if (document.activeElement !== input) return; // focus moved mid-fetch
  const qNow = fold(input.value.trim());
  if (!qNow) return hide();
  const typedRaw = input.value.trim();
  const starts = [];
  const contains = [];
  for (const m of list) {
    // Skip only a RAW exact match — a fold-equal name with different
    // accents ("tuong thuy" vs "Tường Thụy") is exactly what the
    // operator wants suggested, so it must stay in the list.
    if (m.name === typedRaw) continue;
    if (m.folded.startsWith(qNow)) starts.push(m);
    else if (m.folded.includes(qNow)) contains.push(m);
  }
  render([...starts, ...contains].slice(0, MAX_ITEMS), input);
}

function onKeydown(e, input) {
  if (box.style.display === 'none' || _activeInput !== input) return;
  if (e.key === 'ArrowDown') { e.preventDefault(); highlight(_activeIndex + 1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); highlight(_activeIndex - 1); }
  else if (e.key === 'Enter' && _activeIndex >= 0) {
    e.preventDefault();
    pick(box.children[_activeIndex].lastChild.textContent);
  } else if (e.key === 'Escape') { e.preventDefault(); hide(); }
}

for (const slot of SLOTS) {
  const input = $(`in-${slot}`);
  input.setAttribute('autocomplete', 'off');
  input.addEventListener('input', () => onType(input));
  input.addEventListener('focus', () => onType(input));
  input.addEventListener('blur', () => setTimeout(hide, 150));
  input.addEventListener('keydown', (e) => onKeydown(e, input));
}
window.addEventListener('scroll', hide, true);
window.addEventListener('resize', hide);
