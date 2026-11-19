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
- Canvas = the target timeline's size and aspect, the element at its default place on it. Author at 1920×1080 and render `--resolution landscape-4k` for a 3840×2160 timeline (`portrait-4k` for 2160×3840): Chrome renders at device scale factor 2, so text and SVG stay sharp; the aspect must match and the scale must be a whole multiple. Why a full canvas and not a tight crop: Resolve's default for mismatched resolution ("Scale entire image to fit") blows a small clip up to fill the frame; a full-size canvas lands 1:1 at its intended place, and the Inspector's Position / Zoom move it from there.
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

1. Bin: `mp = project.GetMediaPool()`; `bin = B.path_bin(mp, "03 Assets/Videos/<Video>/<set>")` (`scripts/resolve_bins.py`; layout: [resolve](resolve.md) § Organization), `mp.SetCurrentFolder(bin)`, `mp.ImportMedia([path, ...])` (plain strings), `B.tag(clip, "<Video>", "graphic")` on each, then put the current folder back. Check each clip's `GetClipProperty("Frames")` against the placement list.
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

## After compositing

The render comes from the NLE: the 16:9 master renders with the layers track enabled (`/videos:master`), each other format from its own timeline. Loudness and video CI run on those NLE renders, as for any final ([ci](ci.md)).
