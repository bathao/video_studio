# Avatar images for the cinematic intro

The renderer looks up player photos here. If both players have a photo,
the intro becomes a cinematic 6-second card with both players sliding
in from the sides. Otherwise it falls back to the existing text-only
title card and the renderer logs a hint to add the missing photos.

## Naming convention

Drop one image per player directly into this folder. The filename's stem
is matched (case-insensitively, NFC-normalised) against what's typed in
the **Player 1** / **Player 2** inputs.

```
assets/avatars/
  Ma Long.jpg
  Fan Zhendong.png
  Nguyễn Bá Thảo.jpg          ← Vietnamese diacritics fully OK
  Quách Thị Lan.jpeg
```

Matching is case-insensitive, so `Ma Long`, `MA LONG`, `ma long` all
resolve to `Ma Long.jpg`.

## Image rules

- Any aspect ratio works — the renderer center-crops to a square then
  applies a circular mask, so a typical action shot from a match
  (player + court background) ends up as a clean round portrait. **No
  transparency / background removal needed.**
- Accepted extensions, in priority order: `.png`, `.jpg`, `.jpeg`, `.webp`.
  If both `Ma Long.png` and `Ma Long.jpg` exist, the .png wins.
- Larger source = better. The avatar is shown at ~420 px in the intro;
  anything ≥600 px square equivalent looks great.
- Try to centre the player's face/torso in the source image — the
  square crop is taken from the centre of the photo, so anything off-
  frame on the edges will be cut.

## Default placeholder

`_default.jpg` (filenames starting with `_` are reserved) ships with
the app as a generic head-and-shoulders silhouette. It's used **only at
render time** when one or both players have no specific photo, so the
cinematic intro still works for the player who does have a real photo.

The UI thumbnail next to each name input intentionally does **not**
fall back to `_default.jpg` — a missing photo stays visibly missing
(faded thumbnail) so you remember to add a real one when you have it.

## Quick test workflow

1. Drop `Test Player.jpg` into this folder.
2. In the web UI, type `Test Player` (or `test player` — matching is
   case-insensitive) into a player name input.
3. A circular thumbnail should appear next to the name within ~400 ms.
4. If nothing appears, the file isn't being found — check spelling
   and that the file is directly under `assets/avatars/`, not in a
   subfolder.
