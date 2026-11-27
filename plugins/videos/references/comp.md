# Layout comp: one all-angles master, laid out in HyperFrames

The route for a talking-head video shot with several cameras (and often a screen feed). The NLE decides the cut. HyperFrames decides what the viewer sees at every moment: which camera, which layout, which screen crop, what gets shown instead of told. Everything comes from one file, the all-angles master: every camera of the approved cut side by side in one big frame, the same cut on every feed.

Scripts: `scripts/comp_faces.py` (face measurements per camera), `scripts/comp.py` (plan, tracks, sound, `index.html`, checks). One spec per video, `comp.json`.

## Order

1. rough → tighten on the multicam in the NLE: take picker, the agent's rough (dead air and silences out, the last take of a passage by default, stumbles and repeated restarts out), then the human fine-tunes the cut and approves it (`/videos:rough`, `/videos:tighten`). The camera of each piece no longer matters there: the comp picks it per span.
2. audio: cleanup and the −14 LUFS master on the approved cut (`/videos:audio`). One master serves every format.
3. grade (optional): on the camera clips in the NLE, one look per set (`/videos:grade`).
4. The all-angles master of the approved cut (below).
5. 16:9 first: faces → spec → plan → preview stills → build → the human reviews in HyperFrames Studio → render → checks → video CI.
6. 9:16 from the same spans (`--fmt 9x16`): stills → render → checks → video CI.
7. Shorts (ranges of the approved comps), qc, publish.

Why 16:9 first: the rough cut does not change the layout, and the layout spans are cut once the cut is approved. The wide frame holds every layout kind; the vertical reuses the same spans, so it costs one more plan and render.

## The all-angles master

- Content: every feed of the approved cut (each camera, the screen recording, the program feed if there is one), each in a fixed cell of one frame, the same cut on every feed, the cut's sound on A1.
- Grid: 7680×4320 holds two 3840×2160 cells on top and three 2560×1440 cells below (5 feeds). Each feed sits at its native resolution: screen spans need the screen recording at its own resolution, else small UI text is unreadable after the crop.
- Build in Resolve, from the approved cut: a copy of the cut timeline at 7680×4320, one video track per feed, the cut's picture items copied onto every track with that track's angle as the active angle (multicam: the `.drt` angle rewrite of `scripts/resolve_multicam_angles.py`), each track moved into its cell (per-track Pan / Tilt, Zoom 1), the grade on, A1 = the cut's sound. Name it `<Video> 16x9 all angles`. Not scripted in this plugin yet: read every step back and log what works in [resolve](resolve.md) § Timelines.
- No grade wanted: the same can come straight from the camera files with ffmpeg (trimmed feeds through `xstack`), without Resolve. Not scripted yet either.
- Codec: H.265 on the NVIDIA encoder (Resolve: QuickTime or MP4, H.265 NVIDIA; ffmpeg: `-c:v hevc_nvenc -preset p5 -rc vbr -cq 21 -b:v 0 -maxrate 400M -bufsize 800M -pix_fmt yuv420p -tag:v hvc1`). Measured on a 5-feed 8K master: ~0.85 GB per minute (ProRes 422 LT: ~9 GB per minute), the same as ProRes at 100 % on faces (SSIM Y 0.91–0.97; the difference is sensor grain the encoder smooths), 20 s of 8K encoded in 15 s. H.264 on NVIDIA stops at 4096 px wide (an 8K job writes nothing); H.264 in software works but is slow. The price: comp.py's crops decode H.265 ~4× slower than ProRes (minutes on a short video).
- The cells, as rendered, go in the spec (`cells`).

## Faces

`uv run "$S/comp_faces.py" --spec comp.json` (YuNet, downloaded once; `--to 20` = a 20 s test first) → the spec's `faces` file: per camera, 2 samples per second of nose, eye centre, face size and yaw, in cell px. Cameras with role `top` are skipped. Framing, contain and the face bubble all read it.

## The spec (`comp.json`)

Paths are relative to the spec's folder.

| Field | What |
|---|---|
| `title`, `lang`, `fps` (`"24000/1001"`), `end_f` | `end_f` = the master's frame count |
| `master` | the all-angles master |
| `out` | output root: `<out>/16x9/`, `<out>/9x16/` (HyperFrames projects), `<out>/.cache/` (tracks shared by both) |
| `cells` | `{cam: [x, y, w, h]}` in master px |
| `cams` | per camera: `role` `front` (nose centred on the upper third) / `side` (head on the third behind it, looking room in front) / `top` (hands, desk: a fixed `anchor` [x, y] in cell fractions); `faces_from` (a program feed that mirrors another camera's framing), `min_face_h` (ignore smaller faces: a preview window inside a screen) |
| `faces` | output of `comp_faces.py` |
| `screen` | `cam` (the screen feed), `crops` {name: [x, y, w, h]} in that cell (the app below its title bar and above the taskbar; a browser page without tabs or bookmarks), `blur` [{`rect`, `frames`: [[f0, f1]]}] (URLs, addresses) |
| `bubble_cam` | the face in the screen bubble (default: the first front camera) |
| `words` | Words file of the cut, record seconds, already fixed against a second transcript |
| `captions` | `font` (woff2), `family`, `weight`, `accent` (the spoken word), optional `size`, `max_chars`, `max_width`, `drop` (filler regex), `shadow` |
| `audio` | `src` (the mastered sound of the cut), optional `af` (ffmpeg filter) |
| `style` | `shadow`, `wall` {`base`, `mid`, `edge`, `band`, `band2`} (wallpaper colours; bands as `r,g,b`) |
| `spans` | the layout plan, below |
| `overlays` | clips or generated UIs on a slot, below |

### Spans

One entry per layout span, `{"f": <start frame>, "k": <kind>, …, "note": "<the words it covers>"}`; a span ends where the next starts. Start frames sit on the cut's jumps (every jump of the cut is a jump in every feed) or on a content change of the screen feed.

| Kind | Fields | 16:9 | 9:16 |
|---|---|---|---|
| `full` | `cam`, `punch` (static zoom), `push` [start s, scale, duration s] | one camera, full frame | one card 992×1832, captions over the chest (punch capped at 1.25) |
| `side2` | `cams` [a, b] | two panels side by side | two panels stacked |
| `three16` | `cams` [big, small, small] | one big + two small | three 16:9 panels stacked |
| `quad` | `cams` [4] | 2×2 | 2×2, captions in the middle |
| `screen` | `src` (crop name) or `crop`, `views` [[t, x, y, zoom]], `lens` [[t, x, y, mag]] | rounded window at the crop's aspect + face bubble | window on top, camera panel below |
| `duo` | `src` / `crop`, `cam`, `views`, `lens` | screen in the big panel + one camera in a narrow panel | window on top, camera below |
| `hero` | `cam`, `da` | the slot for a reel or overlay (big panel) + one camera in the narrow panel | slot on top, camera below |

`views` and `lens`: times in record seconds, x / y in fractions of the screen cell; a view `[t, null, null, 1]` zooms back out. `da` = a per-span anchor for a `top` camera (the hands on the keyboard rather than the mouse).

### Overlays

On the `hero` slot by default (`slot` + per-format `rect` otherwise), between `f0` and `f1`, in list order (later = on top):

- `video`: `src` (a downloaded reel, a generated clip), looped and scaled to the slot. Without `f0` / `f1` it covers every run of `hero` spans: the product stays on screen the whole time it is discussed, whatever camera the narrow panel shows.
- `master`: a `crop` of one cell (`cam`, default the screen feed), frozen from `hold` on (a page that starts loading its bookmarks bar).
- `html`: a generated UI. `html`, `css`, `js` files (per format: `{"16x9": …, "9x16": …}`); the fragment is drawn for the slot rect in `plan.json` (`hero.rect`), the `js` file holds `tl.set` / `tl.to` lines on the main timeline (typing, a button press, a send). `bg` = the panel colour.

## What `plan` does (framing rules)

- Front cameras: the nose on the upper third, centred. Side cameras: the head on the third behind it, looking room in front. In 9:16: the eyes on the upper third of the panel's part inside the platform safe zone (y 220–1436), side cameras at 0.38 / 0.62 (0.40 / 0.60 on the single card), and headroom so the hair clears the panel top: target y ≥ 0.05 + 0.8 × face height / panel height.
- Sticky: one framing per camera and panel size for the whole video. A new one only when the speaker moved more than 10 % of the window width or 8 % of its height during the span (`reframed` in the table).
- Contain: in narrow panels (`hero` and every 9:16 panel) every face sample of the span stays inside the window; the window shifts, then widens (`contain`, `contain z-…`).
- Punch-ins and pushes are the only camera moves: a static `punch` or one eased `push` per span, never a micro-reframe.
- Shots ≥ 1.5 s: `SHORT:` lists the spans under it.
- During a big gesture (reaching for a keyboard), pick the camera where the head moves least; a head that leaves a narrow panel shows up as `contain` or in the stills.

## Rules the spans follow

- Hard cuts only, no fades or morphs between layouts; motion lives inside a panel (push-ins, screen zooms).
- A jump of the cut lands on a layout or camera change, or inside a screen span or a split, never inside one full-frame camera.
- Spans start a quarter frame early (built in), else a frame at the cut shows an empty panel.
- Screen: the whole window stays visible ≥ 1 s before any zoom; zooms ≥ 1.35×, 0.8 s, eased; the crop is chosen per span so no text is cut at the window edge; small controls get the loupe and a pointer (`lens`).
- Privacy: browser tabs, the bookmarks bar and the taskbar never appear (crop them out, `hold` before a bar loads); URLs and addresses are blurred (`screen.blur`); a QR code meant to be scanned stays sharp; a generated UI shows no client names, private chats or real account data.

## Show, don't tell

Read the cut sentence by sentence and ask what each one names that a viewer could see.

- A product named: its own footage on screen the whole time it is discussed (`hero` spans + a `video` overlay). Be proactive: download the official reel or demo from its site before asking, keep the source URL in the record's notes, credit it if the platform needs it.
- An action: shown. The speaker types → the real screen, cropped (`master` overlay). "Ask an AI to do it" → a generated UI of that app with the request typed and sent (`html` overlay), drawn from a screenshot of the real app.
- Nothing to film: generated B-roll (paid image or video generation: a go-ahead and a credit cap per video) or a drawn diagram. Text only where nothing can be shown.

## Captions and sound

- Captions come from the Words file of the cut, fixed first against a second transcript (misheard phrases, stutters, brand names: `/videos:transcribe`). Chunks of ≤ 26 characters in 16:9 (≤ 24 in 9:16) that never cross a sentence and break at punctuation and pauses; the spoken word in the accent colour; a text shadow, no box; in the layout's clear band, never on a face. 9:16: a chunk never straddles a band change.
- The sound sits in the comp (`audio`), so Studio plays it and the render needs no mux.

## Build and check

```
S=<plugin>/scripts
uv run "$S/comp_faces.py" --spec comp.json
uv run "$S/comp.py" plan --spec comp.json              # read the table: SHORT none, every reframed / contain explained
uv run "$S/comp.py" all --spec comp.json               # plan + tracks + sound + index.html
uv run "$S/comp.py" verify --spec comp.json            # frame counts, frame 0 = master at f0
cd <out>/16x9 && hyperframes lint && hyperframes snapshot . --at <one time per span> --no-end -o <stills>
hyperframes render . --workers 3 --fps 24000/1001 --output <render>.mp4
uv run "$S/comp.py" gaps <render>.mp4                  # no frame with an empty panel
```

Then loudness (−14 LUFS ± 0.5, true peak ≤ −1 dBTP) and video CI `--stage formats` on the render before the human sees it. Stills to read at every span: heads inside panels, no text cut at a window edge, captions clear of faces, nothing private on screen. The 9:16: the same commands with `--fmt 9x16`; its tracks reuse the 16:9 ones where identical (screen crops).

Measured on a 2:46 video (5 feeds, 53 spans): ~72 tracks per format, encoded in a few minutes from an 8K ProRes master; a render ≈ 2.5 min per format with 3 workers; ~300 MB per final.

## Studio or regeneration

The human reviews and retouches the comp in HyperFrames Studio. `comp.py html` refuses to overwrite an `index.html` that changed since it wrote it: read `hyperframes history --json`, carry the edits back into the spec (span frames, cameras, push, views), then rerun with `--force`. Touch-ups the spec cannot hold are made in Studio after the last regeneration. Which side owns the comp in the long run (the spec, regenerated; or Studio, edited by hand) is the human's call: record it in the project's style notes once made.
