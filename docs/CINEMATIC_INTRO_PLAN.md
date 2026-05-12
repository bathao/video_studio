# Cinematic Avatar Intro — Design & Plan

A 5–8 second broadcast-style intro card that replaces the existing
text-only title card when both players have an avatar photo on disk.
Shows tournament name, both player photos sliding in from the sides,
"VS" centred, and player names — all over a blurred frame from the
source video.

Status legend:  ✅ done · 🔧 in progress · ⏳ pending

## 1. Scope (v1 — keep this small)

The plan is deliberately cut into a *minimal v1* that ships fast, plus
follow-up phases for polish. Anything fancy (rotate, bouncy easing,
particles, auto background-removal) is explicitly v2+.

| Aspect       | v1 — ship now                                       | v2+ — polish later                                  |
| ---          | ---                                                 | ---                                                 |
| Background   | One frame from mid-source, gblur, brightness −20 %  | Live moving segment + slow zoom                     |
| Avatar       | Center-cropped square + circular alpha mask         | Glow ring, drop shadow, slight rotate / parallax    |
| BG removal   | Not needed (circular mask hides corners)            | Auto rembg / MediaPipe                              |
| Text         | libass (existing pipeline, Vietnamese-safe)         | Same — already broadcast-quality                    |
| Music        | None (silent intro)                                 | Optional MP3 mix with fade in/out + voice ducking   |
| Duration     | 6 s default, configurable in `config.json`          | Auto-scale to player-name length                    |
| Fallback     | Missing player photo → `_default.jpg` placeholder; both placeholder *and* a player photo missing → existing text intro | Per-player placeholder library (silhouette by gender / club / country) |

Decision: with `_default.jpg` shipped, **a render only falls back to
the text intro if the operator deletes the placeholder.** Normal
operation: real photo when available, generic silhouette otherwise,
with a hint message in the render state asking the operator to drop a
real photo when they have one.

## 2. Folder & naming convention  ✅ implemented

Player photos sit as flat files directly inside `assets/avatars/`:

```
assets/avatars/
  _default.jpg              ← shipped placeholder, used when a player has no photo
  Ma Long.jpg
  Fan Zhendong.png
  Nguyễn Bá Thảo.jpg        ← Vietnamese diacritics fully OK
  Quách Thị Lan.jpeg
```

Rules:

- One file per player, name = exactly what's typed in the Player 1 /
  Player 2 input.
- Matching is **case-insensitive + NFC-normalised** — `Ma Long`,
  `MA LONG`, `ma long` all hit the same file.
- Accepted extensions, in priority order: `.png`, `.jpg`, `.jpeg`,
  `.webp`. PNG wins when both `Ma Long.png` and `Ma Long.jpg` exist.
- Filenames starting with `_` are **reserved** — they never match a
  player name. `_default.<ext>` is the only one currently used.

Implementation: [backend/avatars.py](../backend/avatars.py)
- `find_avatar(name)` — strict; UI uses this so missing photos stay
  visibly missing.
- `find_default_avatar()` — returns `_default.<ext>` or `None`.
- `find_avatar_or_default(name) -> (path, used_default)` — for the
  renderer; lets the caller log a hint when the placeholder is used.

## 3. Backend API  ✅ implemented

Two endpoints in [backend/server.py](../backend/server.py):

- `GET /api/avatars/{name}` → `{name, exists, path}` JSON; tells the
  UI whether to show or fade the thumbnail.
- `GET /api/avatars/{name}/preview` → serves the file with the right
  MIME type, or 404. Used for the live thumbnail next to each name
  input.

Both use the **strict** `find_avatar` so the operator sees a faded
thumbnail when no specific photo exists.

## 4. Frontend  ✅ implemented

[frontend/index.html](../frontend/index.html) — each player input
becomes a flex row with a 40×40 rounded thumbnail beside it:

```html
<div class="flex items-center gap-2">
  <input id="in-p1" class="ipt flex-1 min-w-0" />
  <img id="thumb-p1" class="w-10 h-10 rounded-full object-cover bg-ink-800
       border border-ink-700 opacity-30 shrink-0" />
</div>
```

[frontend/app.js](../frontend/app.js) — `refreshAvatarThumb(slot)`:

- 350 ms debounce on the player name `input` event.
- URL: `/api/avatars/<encoded>/preview?t=<now>` — the cache-buster
  forces a refetch when you replace the file on disk without restarting
  the server.
- `onerror` → faded placeholder; `onload` → fully opaque.
- Also called once on project load via `syncAllUI` so the UI rehydrates
  thumbnails for the saved player names.

## 5. Image processing pipeline  ⏳ Phase B

The intro renderer takes any photo (action shot from a match, with
court background) and produces a clean broadcast-style portrait — no
manual background removal required.

```
input photo (any aspect, any size)
  ↓ crop=min(iw,ih):min(iw,ih)               (center crop to square)
  ↓ scale=420:420                            (target avatar size)
  ↓ format=yuva420p                          (alpha channel)
  ↓ geq apply circular alpha mask:
      a='255*lt(hypot(X-W/2,Y-H/2),W/2-2)'
  ↓ overlay onto blurred background
```

The circular mask hides the rectangular crop edges and the original
background — that's why no rembg/MediaPipe dependency is needed.

## 6. ffmpeg filter graph  ⏳ Phase B

Single `ffmpeg` call with multiple inputs, no intermediate files:

```
inputs:
  0: source video       -ss <mid> -t <DUR>      (raw blurred bg)
  1: player 1 photo     -loop 1  -t <DUR>       (PNG / JPG)
  2: player 2 photo     -loop 1  -t <DUR>
  3: silent audio       -f lavfi -i anullsrc    (no music in v1)

filter_complex:
  [0:v] scale=W:H,gblur=sigma=30,eq=brightness=-0.20         [bg]
  [1:v] crop=min(iw\,ih):min(iw\,ih),scale=420:420,
        format=yuva420p,
        geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':
            a='255*lt(hypot(X-W/2\,Y-H/2)\,W/2-2)'           [av1]
  [2:v] (same chain as av1)                                   [av2]

  [bg ][av1] overlay=x='<slide_in_left>' :y='(H-h)/2+80'     [s1]
  [s1 ][av2] overlay=x='<slide_in_right>':y='(H-h)/2+80'     [s2]
  [s2] ass=intro.ass,format=yuv420p                           [vout]
```

The `+80` y-offset leaves room above for the tournament name and below
for player names rendered by libass.

## 7. Animation timing

Slide-in uses **ease-out cubic** so avatars decelerate naturally into
their resting position — broadcast feel, no bouncing.

```
ease_out_cubic(t) = 1 - (1-t)^3
```

For ffmpeg's overlay `x` expression (no `pow`, so we expand the cube):

```
P1 (target = W*0.27 - w/2, off-screen start = -w):
  x = if(lt(t,1.0),
         (W*0.27-w/2) * (1 - (1-t)*(1-t)*(1-t))
         + (-w)       *      (1-t)*(1-t)*(1-t),
         W*0.27 - w/2)

P2 (mirrored, target = W*0.73 - w/2, start = W):
  x = if(lt(t,1.0),
         (W*0.73-w/2) * (1 - (1-t)*(1-t)*(1-t))
         + W          *      (1-t)*(1-t)*(1-t),
         W*0.73 - w/2)
```

Timeline (DUR = 6 s default):

| Time   | Event                                                      |
| ---    | ---                                                        |
| 0.0 s  | Tournament name fades in (libass `\fad(500,0)`)            |
| 0.0 s  | Avatars start sliding in from off-screen                   |
| 0.7 s  | Player names fade in below the still-moving avatars        |
| 1.0 s  | Avatars settle at ¼ / ¾ of screen width                    |
| 1.1 s  | "VS" fades in + scales 80→100 % over 0.4 s                 |
| 5.5 s  | All elements start fading out (0.5 s tail)                 |
| 6.0 s  | End                                                        |

## 8. Renderer integration  ⏳ Phase B

[backend/renderer.py](../backend/renderer.py)'s `run_render` will branch
when `plan.include_intro` is true:

```python
p1_photo, p1_default = find_avatar_or_default(plan.project.info.p1)
p2_photo, p2_default = find_avatar_or_default(plan.project.info.p2)

if p1_photo and p2_photo:
    render_cinematic_intro(
        out_path=intro_path, src=src,
        duration=config.intro_duration_seconds,
        width=width, height=height, fps=fps,
        tournament=plan.project.info.tournament,
        p1_name=plan.project.info.p1, p1_avatar=p1_photo,
        p2_name=plan.project.info.p2, p2_avatar=p2_photo,
        on_progress=make_progress("intro", weight_lookup["intro"]),
    )
    if p1_default or p2_default:
        s.message = (
            f"Cinematic intro used the default placeholder for "
            f"{'P1' if p1_default else ''}"
            f"{' & ' if p1_default and p2_default else ''}"
            f"{'P2' if p2_default else ''} "
            f"— drop a real photo into assets/avatars/ when you have one."
        )
else:
    # Both placeholder *and* a player photo missing → text intro.
    render_intro(...)  # existing libass-only path
```

New file: [backend/intro_builder.py](../backend/intro_builder.py)
(separate from the `backend/ass/` package — different responsibility:
builds the ffmpeg filter graph rather than the .ass file). The .ass for
the cinematic intro's text overlays lives in
[backend/ass/intro.py](../backend/ass/intro.py) as
`build_cinematic_intro_ass`, with layout adapted (text below avatars
instead of stacked in the centre).

## 9. Config additions

Add to [config.json](../config.json) and corresponding properties in
[backend/config.py](../backend/config.py):

```json
{
  "intro_duration_seconds": 4.0,
  "intro_avatar_size_px": 420,
  "intro_blur_sigma": 30
}
```

Default duration locked at 4 s after iterating with the operator —
shorter felt rushed, longer left a boring tail. `intro_music_path`
reserved for v2+; not in config until then.

## 10. Phases & current status

### Phase A — Avatar foundation  ✅ done

- [x] `assets/avatars/` folder + `README.md`
- [x] [backend/avatars.py](../backend/avatars.py) with `find_avatar`,
      `find_default_avatar`, `find_avatar_or_default`
- [x] `/api/avatars/{name}` + `/api/avatars/{name}/preview` endpoints
- [x] Frontend thumbnails next to P1/P2 inputs, debounced refresh
- [x] `_default.jpg` shipped (head + trapezoid silhouette, generated
      via ffmpeg `geq` filter at setup time)

### Phase B — Cinematic intro renderer  ✅ done

- [x] [backend/intro_builder.py](../backend/intro_builder.py) with
      `render_cinematic_intro(...)` (single-pass ffmpeg, NVENC encode,
      silent audio for concat compatibility)
- [x] Filter graph: bg blur + Ken-Burns zoom (zoompan, `on/fps`) + 2
      avatars centre-cropped + circular alpha mask via `geq` + sin-bobbing
      y-position + libass text burn
- [x] [backend/ass/intro.py](../backend/ass/intro.py)
      `build_cinematic_intro_ass`: tournament name + gold underline that
      wipes in @ t=2 s + player names with slow fade-out (~half duration)
      + "VS" with periodic 100 → 105 → 100 % pulse
- [x] Hook in [backend/renderer.py](../backend/renderer.py) `run_render`:
      both avatars resolved → cinematic; either missing → text fallback
      with `s.message` hint to drop a real photo in
- [x] Config keys (§9): `intro_duration_seconds=4.0`,
      `intro_avatar_size_px=420`, `intro_blur_sigma=30`

### Phase C — Polish  ⏳ later

- [ ] Animation timing tuning (overshoot? subtle camera shake?)
- [ ] Brightness / blur sigma tuning per real footage
- [ ] Edge cases: long tournament name, very narrow / very wide source
      photos, source video shorter than `DUR`

### Phase D — Optional v2  ⏳ later

- [ ] Music mix with fade in/out
- [ ] Bo-tròn glow ring around avatars
- [ ] Slight rotate / parallax during slide-in
- [ ] Auto bg-removal via rembg

## 11. Risks & mitigations

| Risk                                                              | Mitigation                                                                              |
| ---                                                               | ---                                                                                     |
| Photo without transparent bg looks like a square cutout           | Circular mask hides corners → broadcast-style portrait. v1 design.                      |
| `gblur` slow on 2K/4K                                             | Scale to 1280×720 before blur, then upscale. Visually identical, ~4× faster.            |
| NVENC has no alpha support → avatar overlay must be CPU-side      | Filters run on CPU, only encode is NVENC. No round-trip waste; NV12 throughout.         |
| Player name has chars that break filter graph escaping            | Already escape via `_ass_escape`; for filter complex we'd never inject names raw.       |
| `_default.jpg` accidentally collides with a player named `_default`| Reserved-prefix rule in `find_avatar` returns `None` for `_*`.                          |
| Source shorter than `DUR`                                         | `-ss` clamped to `min(midpoint, duration - DUR)`; if still short, use frame at t=0.    |
| Music `.mp3` shorter than intro                                   | `-stream_loop -1` loops; afade trims gracefully. (Deferred to v2.)                      |

## 12. Test plan

1. **Unit-ish** — `find_avatar` / `find_default_avatar` /
   `find_avatar_or_default` against 6+ cases:
   empty input · missing player · case variants · NFC vs NFD · reserved
   `_default` · default fallback when both missing.  ✅ already verified.
2. **Render — happy path** — both players have real photos, render a
   short BO5 sample, manually verify intro looks broadcast-quality and
   transitions smoothly into the highlight reel.
3. **Render — one default** — drop avatar for P1 only, render. Confirm
   intro uses real P1 photo + `_default.jpg` for P2; render state's
   `message` field surfaces the hint.
4. **Render — fallback** — delete `_default.jpg`, render with no player
   photos. Confirm renderer falls back to existing text intro and
   doesn't crash.
5. **Edge** — extremely tall/wide source photo; very long player name;
   `intro_duration_seconds = 4.0`. Layout should remain stable.

## 13. Open questions resolved

| Question                          | Answer                                                                  |
| ---                               | ---                                                                     |
| Avatar match — exact or fuzzy?    | Exact (NFC-normalised, case-insensitive). Fuzzy is a v2+ ergonomics nice-to-have. |
| Music                             | Skip in v1. `intro_music_path = ""` in config; reserved.                |
| Animation feel                    | Ease-out cubic, 1.0 s slide; no overshoot in v1.                        |
| Avatar size                       | 420 px (≈ 40 % of 1080p height).                                        |
| Fallback policy                   | Both photo missing → text intro; one missing → default placeholder + warning message. |
| Default placeholder               | Shipped: `_default.jpg`, generated at setup with ffmpeg `geq`.          |
