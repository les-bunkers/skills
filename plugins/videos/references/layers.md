# Graphics as layers: alpha renders composited in the NLE

Default for anything drawn over footage. Each graphic renders alone with a transparent background and sits on its own track above the picture in the NLE (DaVinci Resolve here). Why: the human keeps control where they edit (move, scale, trim, retime or drop a layer without a re-render), the cut, camera angles and reframes underneath stay theirs, and a moved cut drags its layers along instead of invalidating a baked render. A fix re-renders one layer, not the whole video.

## What goes where

| Kind of change | Where | Examples |
|---|---|---|
| Drawn over the picture | layer = alpha render, own track | motion graphics, cards, labels, highlight boxes and outlines, arrows, callouts, hook card, end card, lower thirds, designed word captions, full-frame cutaway graphics |
| Changes the footage itself | on the clips in the NLE | punch-ins and zooms, reframes, blurs and redactions, picture-in-picture of another camera, transitions, grade |
| Baked into one rendered video | only when there is no footage to composite, or the human asked for a hands-off batch | bumper or motion-only piece, a clip factory batch, ad variants of an already approved baked short |

Never bake a footage change into a rendered copy of the cut (a zoom made by re-rendering the cut with a scale): it freezes the human's edit under the graphic, and any trim later leaves the copy out of sync.

## Render a layer (HyperFrames)

- Transparent root: no background on `html`, `body` or the root element. Only a full-frame cutaway is opaque, and it is still its own layer.
- Canvas = the target timeline's size and aspect, the element at its default place on it. Author at 1920×1080 and render `--resolution landscape-4k` for a 3840×2160 timeline (`portrait-4k` for 2160×3840; a 1080×1920 vertical timeline: author and render at 1080×1920): Chrome renders at device scale factor 2, so text and SVG stay sharp; the aspect must match and the scale must be a whole multiple. Why a full canvas and not a tight crop: Resolve's default for mismatched resolution ("Scale entire image to fit") blows a small clip up to fill the frame; a full-size canvas lands 1:1 at its intended place, and the Inspector's Position / Zoom move it from there.
- `hyperframes render . --format mov --fps <timeline fps> --resolution landscape-4k --output <renders>/<id>.mov --quiet` → ProRes 4444 with alpha (`yuva444p12le`). `--prores-profile hq|422|lt|proxy` drops the alpha. The output path must sit outside the project folder (the render fails inside it). WebM alpha exists but ProRes is what NLEs read natively.
- Alpha is straight (not premultiplied). Measured on a HyperFrames ProRes 4444 render: 102 semi-transparent edge pixels (median alpha 121 of 255) keep their opaque neighbour's colour (edge/neighbour RGB ratio median 1.00, p10 0.81, p90 1.22; premultiplied would read ≈ 0.47). In Resolve, if edges show a light halo or soft shadows and translucent panels look washed out (straight alpha read as premultiplied), set Clip Attributes → Video → Alpha mode = Straight on those clips. Check on a composite still over a bright and a dark frame.
- Glass: a transparent layer has nothing behind it to blur, so `backdrop-filter` renders as a flat tint. Make panels ~.88–.92 opaque with a soft shadow, or render the panel shape as a second matte layer (white shape on transparent) and blur the footage under it in the NLE.
- Contrast: `hyperframes check` can't see the footage under a transparent layer. Text carries its own backing (panel, chip, shadow) and is judged on a composite still.
- Many short layers: each standalone render carries ~25 s of fixed overhead. Put ~10 graphics back to back on one composition, render once, then cut it apart with stream copy at exact frame timestamps (ProRes is intra-frame, so any frame is a clean cut point): ~5 s per graphic instead of ~25.
- Claims check on every text before rendering ([claims](claims.md)); `hyperframes check --at-transitions` per layer.

### HyperFrames traps (seen on insert batches)

- **One frame too long.** HyperFrames renders ceil(duration × fps) frames: 29.2793 s at 23.976 = 702.0006 frames → 703. Write `data-duration` as frames / fps rounded DOWN at the last decimal, and after every render compare ffprobe's frame count (`-count_packets -show_entries stream=nb_read_packets`) with the slot it fills (f1 − f0); the placing script refuses a mismatch. Check the first and last frame of each card against the cut frame it must land on: a card one frame early shows on the wrong camera.
- **Disk.** Renders write uncompressed frames to the temp folder (~16 GB per minute of 1080p) and the extract cache under it is never pruned (57 GB on one machine); at low space the render refuses ("Low disk space, 0.0 GB free"). Check free space before a batch, give each render process `TEMP` / `TMP` / `TMPDIR` on the media drive, delete that folder after, and clear the extract cache only when no render runs.
- **Re-render a placed layer** into a new folder, every time: overwriting sometimes fails with EPERM and sometimes succeeds silently, changing a clip already in the edit (resolve § One Resolve).
- **Lint.** Initial hidden states go in CSS or a `gsap.set` before the timeline (`gsap_timeline_set_initial_hide`). Element ids never look like a media uid (`v2` as a span id collided with video `v2` → `gsap_animates_clip_element`): prefix media uids (`vid…`). Position and size motion goes through transforms and a clip-path inset (`gsap_non_transform_motion`); tween clip-path with an explicit `fromTo`. Swap colours with `tl.set` on backgroundColor / color, not className toggles (applied unreliably). A hidden label still takes flex space under `visibility:hidden` → position it absolutely. Every font family named in the CSS has its `@font-face` in the project, including the families of looks not shown (`font_family_without_font_face`; system fonts have none → serve a font). Word spans flush against a zero-padding box → `text_box_overflow`: inset the text zone. Three `<video>` tags of one file with the same start and duration → `duplicate_media_discovery_risk`: give each its own alias (a hard link per tag).
- **Snapshots.** `hyperframes snapshot --at … -o <dir>` writes `frame-NN-at-<t>s.png` (NN = index in the list, shifts when the times change) plus its own `contact-sheet.jpg`: list the folder, never guess a name.
- **Folder busy after a check.** `hyperframes check` leaves a warm-check helper (`warmCheck.js`, 5-minute idle timeout) with the project as its working folder: deleting or renaming that folder fails ("Device or resource busy", WinError 32) until it exits. Check from a copy you can leave behind, or wait 5 minutes before cleaning up.
- **Thin strips.** NVENC refuses frames under its minimum size (a 30 px strip: `Frame Dimension less than the minimum supported value`) → render wider and mask in the composition.
- **Size on a 4K master.** Cards authored at what looked right in the preview read small and dim at 4K: judge on a full-size still of the master, not the preview.

## Placement list

Next to the renders, `layers.json`: one entry per layer, `{"id", "file", "record_frame" (frames from the timeline start), "timecode", "frames", "track", "anchor" (the words it illustrates), "note"}`. Time layers on the words of the CUT (the Words file shifted through the cuts read back from the timeline, `/videos:master` step 2), never on planned or source times; after the human moves a cut, read the cuts again and re-place.

## Place in Resolve (scripting API, `run_script_unsafe`)

1. Bin: `mp = project.GetMediaPool()`; `bin = B.path_bin(mp, "03 Assets/Videos/<Video>/<fmt>[/<set>]")` (layers are drawn per format: the 9x16 ones first, the 16x9 ones at the wide stage) (`scripts/resolve_bins.py`; layout: [resolve](resolve.md) § Organization), `mp.SetCurrentFolder(bin)`, `mp.ImportMedia([path, ...])` (plain strings), `B.tag(clip, "<Video>", "graphic")` on each, then put the current folder back. Check each clip's `GetClipProperty("Frames")` against the placement list.
2. Timeline: `AppendToTimeline` writes only to the CURRENT timeline. If the human is working in another timeline, ask before switching; switch back to their timeline when done.
3. Track: `tl.AddTrack("video")`, `tl.SetTrackName("video", vt, "<layers track name>")`; on reruns find the track by name. V1, A1 and every other track stay untouched.
4. Place: `mp.AppendToTimeline([{"mediaPoolItem": clip, "startFrame": 0, "endFrame": n, "trackIndex": vt, "recordFrame": tl.GetStartFrame() + f0, "mediaType": 1}])`. `endFrame` is exclusive (n = clip frames); `mediaType` 1 = picture only, so no empty audio items land on the audio tracks. Skip a layer whose record frame is already occupied on that track (reruns stay idempotent).
5. Marker per layer: `tl.AddMarker(f0, "<colour>", "<id>", "<anchor / note>", 1)` (frame relative to the timeline start; occupied → retry ±1, ±2).
6. Look: a composite still at 2–3 layer frames (Resolve MCP `get_image`, or `ExportCurrentFrameAsStill` for slow frames): edges, position, nothing over a face or under the caption band.
7. Re-render a placed layer: Resolve holds placed files open, so overwriting fails. Render to a new file, then `MediaPoolItem.ReplaceClip(<new path>)` (same frame count keeps the item's in/out).
8. Undo: disable or delete the layers track.

## Footage-side changes in the NLE

- Punch-ins and zooms: Zoom and Position keyframes on the V1 clip (or an installed zoom template on an adjustment clip), so they follow trims.
- Blurs and redactions: a tracked window with a blur on the clip (Resolve Color page), not a baked copy.
- Picture-in-picture of another camera: that camera's clip on a track above, cropped and scaled, as a PiP, never as a camera change (camera changes stay the multicam's active angle). The frame or card around the PiP is a layer.

## Split section: the speaker moved aside, a side panel for the cards

A run of cards on one topic (a list, a price breakdown, steps) reads better beside the speaker than over them. For the length of the run the picture moves to one side and an opaque panel takes the other third of the frame; the cards appear in it one after the other. Single cards between two runs stay floating layers, so the edit keeps two rhythms. The human's verdict on the first one (a 16:9 sales video), from a settled frame: "I really like this layout".

```
┌──────────────────────────────┬─────────────────┐
│                              │ ▬▬▬▬▬ ───── ─── │  story bar: one segment per card of the run
│          speaker             │ ┌─────────────┐ │
│   nose at the centre of      │ │    card     │ │  the cards, one after the other
│   this part of the frame     │ └─────────────┘ │
│                              │ brand  campaign │  footer line
└──────────────────────────────┴─────────────────┘
            1216 px                   704 px          on a 1920 canvas
```

- Panel: 704 of 1920 px (37 %), full height, opaque, on the right. It carries its own wallpaper, a story bar at the top (one segment per card of the run, the current one filling over its clip), the card column (56 px padding) and a footer line (brand on the left, campaign on the right).
- Picture: `pan = (W - PW) / 2 / W - nose_x`, in fractions of the frame width (`nose_x` = where the speaker's nose sits on the frontal camera, 0.555 on the first one), puts the nose at the centre of the part left visible. Keep `|pan| * W < PW`: the panel then always covers the strip the pan uncovers.
- Clips: one alpha clip per card, abutting (card k runs from its first frame to the first frame of card k+1; the last one runs 8 frames past its card). The panel background is drawn in every clip, so it stays continuous across a handoff and only the card content changes.
- Motion: the panel pushes in over 0.70 s on the first clip and out over 0.56 s on the last (`cubic-bezier(.7,0,.2,1)`, a little blur while it moves, a cast shadow on the picture). The picture pan runs on the same clock and the same curve, one key per frame while moving, so picture and panel move as one piece.
- Timeline: a variant of the working timeline (`<Video> 16x9 - panels`; [resolve](resolve.md) § Organization), never the working timeline itself. The layout is a 16:9 one: on a video cut vertical first it belongs to the wide stage. Two tracks on top: adjustment clips carrying the pan above every picture track, the panel clips above them. Inside a section, the floating cards with the same ids come off the variant.
- The scripting API can't keyframe an adjustment clip: the pan keys go in through a DRT rewrite ([resolve](resolve.md) § Fusion, effects and templates).

Checks: every shot inside a section is the frontal camera with the face near the centre (thumbnails across each span; a side camera inside a section → shorten the section or key another pan for that shot). No full-frame cutaway overlaps a section: the adjustment clip pans everything under it. After the import, composite stills at mid push-in, settled, a handoff, mid push-out and 2 frames after: the face on its part, no uncovered strip, normal framing back.

Traps: a panel flush to the frame edge fails the title-safe lint → lint panel pages with the safe margin set to the panel padding, on the settled layout. A zoom on the card column multiplies slide distances (a 42 px entry at zoom 1.3 = 55 px, the whole padding) → cap slides at 40 px on screen. Prefix the panel's CSS classes and ids: card builders already use the short names. A subtitle track centred on the full frame runs under the panel → switch it off over sections, or give burned-in captions a position per section.

## Layout sections: framed panels that show the thing

On the comp route these kinds are built in HyperFrames from one all-angles master instead of in the NLE: [comp](comp.md). The layout pass and the show-don't-tell pass are part of every pass over a clip (`/videos` § House rules, `/videos:formats` § Layout pass). A talking head with text cards over it tells; framed panels of the real thing show. For a span of the cut, the full-frame camera gives way to one or more rounded panels of real footage on a moving wallpaper; between spans the plain camera cut comes back.

### Kinds

| Kind | Panels | Use when |
|---|---|---|
| `screen` | the screen recording cropped to the app window, in a rounded window with a margin on the wallpaper + the speaker's face in a circle bubble in a lower corner | the speaker works in an app or a page: the viewer must read the screen |
| `side2` | two equal panels | one action and its effect (a click ↔ the camera that moves), or two angles of the same moment; never two unrelated pictures |
| `tri3` | one big portrait panel + two small landscape panels stacked on the other side (mirrorable) | the speaker (tall) beside the thing and its detail (a device + its control, a product + a close-up) |
| `three16` | three 16:9 cameras, one big + two small stacked | a multi-camera result (all cameras reframed at once, before / after on each) |
| `vslice` | the landscape source with the 9:16 slice outlined + a portrait panel showing that slice | explaining a vertical crop, a zoom headroom, a resolution |
| `single` | one framed panel | one B-roll or one camera that needs the frame (in-panel hard cuts or one eased zoom) |

On a 9:16 canvas the same kinds stack top to bottom (`side2` → two panels one above the other); the caption band stays clear in every kind.

Shared by every kind:
- One corner radius, one gutter, one outer margin, one drop shadow, one wallpaper for the whole video (the project's style file holds the numbers; first use → write them there once approved).
- Panels show real footage: multicam angles (the camera is the multicam clip's active angle, the same rule as camera changes) or B-roll and screen-recording files, framed with the NLE's crop / zoom / pan. Nothing in a panel is a rendered copy of the cut.
- Layout changes are hard cuts on a word boundary, ideally on a cut of the camera track; no morphs, no fades between layouts. Motion lives inside a panel (one eased zoom, in-panel cuts).
- No panel overlaps another panel or the bubble: the NLE has no circle matte over another panel, so each panel and the bubble keep their own area.
- Overlays drawn over the panels (a highlight box on the control the speaker names, a thirds grid, a lead-room arrow, a slice outline, a click ripple) are timed to the words that name them.

### Show, don't tell

For each sentence of the cut, ask what it names that a viewer could see: a control, a camera, a product, a result, a before / after. Look in every source of the take (all camera ISOs, not only the active angle; the screen recording; B-roll; real product photos; the project's assets). Show it in a layout span or as a real visual. A drawn diagram explains a concept nothing films. A text card stays only where nothing can be shown (a price, a quote, a list of reasons). Cards that only restated what a span now shows are disabled in the NLE (not deleted) and listed in the record's note.

### The frame layer: wallpaper with holes

One alpha layer per layout span, on a track above the panel tracks:
- Canvas = the timeline's size, rendered ProRes 4444 with alpha like any layer (§ Render a layer); in Resolve set Alpha mode = Straight.
- Wallpaper = an opaque full-frame element (gradient + blurred bands, a slow linear drift over the span) masked with `mask-image: url("data:image/svg+xml,…")`: an SVG with one full-frame rect and one rounded rect (or circle) per panel in a single path, `fill-rule="evenodd"`, so each panel area is a transparent hole.
- Shadows: one empty `div` per panel at the panel rect with the same radius, `background: transparent`, and an outer `box-shadow`. Browsers paint an outer box-shadow only outside the border box, so the hole stays clear and the shadow falls on the wallpaper. Put these divs after the masked wallpaper (same for the bubble: `border-radius: 50%`, a thin light ring + a soft shadow).
- Overlays in the same composition above the holes, timed to words. One composition per span; carry the drift phase over so the wallpaper does not jump at a cut between two spans.

### Placing the panels (NLE)

One track per panel slot under the frame layer, named for the slot; the camera track (V1) stays as it was, so turning the layout tracks off gives the old edit back. Per panel, from the source rect `(sx, sy, sw, sh)` (source px, same aspect as the panel) to the panel rect `(px, py, pw, ph)` (timeline px) on a `W × H` timeline from an `SW × SH` source:

```
z    = pw / sw                                    screen px per source px  → Zoom = z / b
Pan  = px + pw/2 − W/2 − z·(sx + sw/2 − SW/2)     timeline px, + = right
Tilt = −(py + ph/2 − H/2 − z·(sy + sh/2 − SH/2))  timeline px, + = up
Crop = per edge, the source px outside (sx, sy, sw, sh) minus bleed / z, × b  (CropLeft = b·(sx − bleed/z), …)
```

`b` = the timeline's input scale (1 when source and timeline sizes match). Convert px to the Inspector's Pan / Tilt units with [resolve](resolve.md) § Timelines (Inspector transform units; 1 px per unit when the sizes match). Bleed = 2 px past each hole edge, so the anti-aliased rounded edge sits on picture. Crop works the same on a multicam item as on a file clip when the aspects match. Each panel's camera item is cropped to its own panel with cropping switched on ([resolve](resolve.md) § Tracks and clips: `CroppingEnabled`); the frame layer and the item below never serve as the mask, because the upper camera shows through the lower panel as soon as the item below is missing, black or off (the human's verdict on such a frame: a face cut by a caption band = a big no). Prove it per stacked span: hide the lower item (`Opacity` 0) and export the frame → the lower panel must render empty. Check on exported frames: every panel edge inside its hole, no picture showing at a rounded corner outside the hole, no face cut by a panel edge, captions clear.

### Order of work

1. Plan spans on the cut (`layout.json`: span frames, kind, panels with source and source rect, overlays with anchor words, the cards each span replaces).
2. Preview stills: real frames placed in the panel rects + the frame layer, 1–2 per span. The human sees them before the build.
3. Render the frame layers, place panels and layers on the working timeline (claimed), disable the replaced cards, one marker per span.
4. Composite stills from the NLE at every camera-track item's start, start + 1, middle and end − 1 and at every span edge, as contact sheets with the frame number on each tile (`Timeline.ExportFrame` grabs any timeline without switching the human's view; 90 frames at 300 px ≈ 13 s): one camera per panel, captions clear. Span start / middle / end alone misses an item that straddles a layout change or lost its crop. Caption coverage: the captions layer's alpha every 4th frame; empty runs only on pauses in speech. Then video CI on the review render.

## The human re-cuts a layered timeline

Layers are rendered for one cut. When the human re-cuts the working timeline (inserts, trims, a source span played twice), everything keyed to record frames goes stale; the plan is keyed on source time, so it rebuilds from the new cut.

- ✗ **Slicing the old layer renders to follow the new cut** (the human did it by hand, then disabled the slices that no longer fitted) → no captions and no frame over the inserted shots → dump the new cut, make one plan window per camera-track item (source range and angle from the picture item, sound range from the audio item so J/L cuts stay), render the layers again at the timeline's length and swap them whole on their tracks.
- Pair picture and sound items by overlap, never by index: a picture-only cut inside one audio item adds a picture item.
- Span edges from the old plan miss the new cut points (seen 3 frames to 1.3 s off) → a panel track runs past its layout change (two cameras in one panel) or a few frames of another layout flash before a screen → snap each span edge to the nearest cut of the camera tracks before rendering.
- A layout section that falls inside one long shot of the new cut (a screen, then stacked panels, over a single camera item) → one item cannot fill several panels → rebuild the camera tracks of that span only, between the human's own cut points: delete their items inside it, place one item per panel from the plan, cropping on. Where they chose an angle and the plan chose another, keep theirs.
- Cards and other cues timed in record frames → re-time them from the new cut's words, taken from the new plan (mapping source words onto the timeline places a span played twice only once).
- A reframe the human set by hand (a zoom on one shot) stays; the plan's automatic push-in on that shot is dropped.
- Snapshot the human's new cut before writing anything ([resolve](resolve.md) § One Resolve, many callers).

## After compositing

The render comes from the NLE, each format from its own timeline with its own layers tracks enabled: the approved long 9x16 at `/videos:master`, the 16x9 at `/videos:wide`, each short at `/videos:clips`. `layers.json` holds the cues (record frames, anchor words) for every format; the renders sit in `<media>/layers/<fmt>/renders/`. Loudness and video CI run on those NLE renders, as for any final ([ci](ci.md)).
