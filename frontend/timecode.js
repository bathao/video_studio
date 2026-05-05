// Timecode formatting + parsing. Used everywhere we display a video
// timestamp (HUD, list rows, manual prompts) so a single source of
// truth keeps the format consistent.

export function fmt(s) {
  if (!isFinite(s) || s < 0) s = 0;
  const m = Math.floor(s / 60);
  const sec = s - m * 60;
  return `${m}:${sec.toFixed(2).padStart(5, '0')}`;
}

// Inverse of fmt(): "1:23.45" -> 83.45, "23.45" -> 23.45, "1:23" -> 83.
// Returns NaN for invalid input so callers can detect parse failure
// and revert the displayed value.
export function parseTimecode(str) {
  if (str == null) return NaN;
  const s = String(str).trim();
  if (!s) return NaN;
  const parts = s.split(':');
  if (parts.length === 1) return parseFloat(parts[0]);
  if (parts.length === 2) {
    const m = parseInt(parts[0], 10);
    const sec = parseFloat(parts[1]);
    if (!isFinite(m) || !isFinite(sec)) return NaN;
    return m * 60 + sec;
  }
  return NaN;
}
