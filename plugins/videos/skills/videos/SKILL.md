---
name: videos
description: Video production board and entry point for any project. With no argument it shows every video in progress (stage × status) and the next move for each; with a video name it opens that video's record, says where it stands and runs or proposes the next stage (/videos:new, ingest, transcribe, rough, tighten, audio, grade, formats, master, wide, clips, qc, publish). Holds the order every talking-head video follows: a multicamera shoot is cut once in the NLE, then laid out in HyperFrames from one all-angles master (16:9 first, the 9:16 from the same layout spans); a single-camera edit is cut as the long 9:16, then the 16:9 and the shorts are made from it. Use when the user types /videos, names a video to continue, asks to run a video end to end (every stage in a row, stopping only for the human's reviews), asks what video work is open or where a video stands, or makes any video request that doesn't name a stage.
argument-hint: "[video name or slug]"
---

`videos.py` below = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"` (stdlib only; `python` / `python3` also work). Record format, folders and status words: [records](${CLAUDE_PLUGIN_ROOT}/references/records.md).

## Do

1. No argument → `videos.py board` (`--ws <W>` one workspace, `--wide` every stage column, `--all` include published). Show it, name the next move per active video, stop. "No video records" → offer `/videos:new`; `videos.py where` shows which folder was searched.
2. A name → `videos.py show <query>` (exact slug, else fuzzy on slug and title; several hits → list them and ask). Read the record it prints: frontmatter = state, body = decisions, notes, log. Read the files listed in `style:` before any stage that cuts or styles.
3. Pick the stage: the user's verb (« tighten X » → tighten), else the stage someone is on (`doing` / `review` / `blocked`), else the first one left to do. Status `review …` = waiting on a human: show what to review (timeline, markers, page) and stop, unless the user is giving that review now → record it under `## Decisions`, then continue. NLE route: once master is `done`, wide and clips are both open: propose both (two agents can run them, one Resolve writer at a time). Comp route: wide is `skip`, clips opens once the comps are approved.
4. Load only that stage's skill (table) and the skills or docs it names; run it.
5. After each stage: `videos.py set <slug> <stage> "<status> <date or what to look at>" --next "<next step>"` (master, formats, wide, clips, generate, qc and publish need video CI first: next section). Doubts, skipped items and things to check go in `videos.py note <slug> "<text>"`. Never leave the record stale: the next session starts from it.
6. End to end (the user asks to run the whole video, « end to end », « run it through », « keep going », or starts with `/videos:new` and asks for the rest): after step 5, a stage that ended `done` or `skip` → back to step 3 and run the next stage in the same session. Stop at the first stage left in `review`, `blocked` or `doing` while the human adjusts something, or at a question only the human can answer (a paid model, a hand-edited timeline, the human active in the NLE): say what to look at and which stage runs after their go. Their answer in the chat → record it (step 3) and keep chaining. After master on the NLE route: run wide, then clips, in this session, or offer a second agent for clips (one Resolve writer at a time). A stage never skips its own preview or review gate because the run is end to end.

## Stages

| Stage | Skill | Covers |
|---|---|---|
| brief | `/videos:new` | create the record, kind, campaign / content-hub link, sources, style files |
| generate | your generation pipeline | AI video (kind `generative`): scenes → storyboard → references → voices → keyframes → animatic → shot clips; no skill in this plugin yet, paid models need a go-ahead |
| ingest | `/videos:ingest` | probe sources, sync offsets, camera angles, NLE import, multicam |
| transcribe | `/videos:transcribe` | word-level transcript with fillers kept, caption text |
| rough | `/videos:rough` | take picker on retakes, text anchors → cut list → the cut timeline + markers (comp route `<Video> 16x9`, NLE route `<Video> 9x16`) |
| tighten | `/videos:tighten` | breaths, fillers, dead air, tight pauses, J-cuts, phrase trims, camera switches; then the human fine-tunes and approves the cut |
| audio | `/videos:audio` | speech cleanup, speaker split, −14 LUFS master (→ `audio-cleanup` plugin); one master serves every format |
| grade | `/videos:grade` | one look per set, LUT export |
| formats | `/videos:formats` | comp route: the all-angles master, then the HyperFrames layout comp (layout + show-don't-tell passes, captions, sound), 16:9 first, then the 9:16 from the same spans; NLE route: the same passes as alpha layers on the 9x16; other kinds: their formats |
| master | `/videos:master` | the finals the human approved: comp route = the comp renders checked and filed; NLE route = the full 9x16 frozen in Published, rendered, cuts read back, file checked |
| wide | `/videos:wide` | NLE route: the 16:9 made from the approved 9x16 (same cut, sound and grade, wide framing, 16:9 layers, render); comp route: skip |
| clips | `/videos:clips` | shorts cut from the approved vertical (comp route: the 9:16 comp; NLE route: the 9x16 in the NLE); podcasts without a vertical edit → `vertical-clips` plugin |
| qc | `/videos:qc` | one gate before anything ships |
| publish | `/videos:publish` | finals into `artifacts/videos/<slug>/`, post text, covers, schedule |

## Order: cut once, then the formats

A talking-head video is cut once, then turned into its deliverables. The route goes in the record's decisions at brief (`route: comp` or `route: nle`).

**Comp route** (default when the shoot has several cameras or a screen feed): the NLE decides the cut, HyperFrames decides what the viewer sees. [comp](${CLAUDE_PLUGIN_ROOT}/references/comp.md)

1. ingest → transcribe → rough (take picker; the agent's rough: dead air out, the last take of a passage by default, stumbles and repeated restarts out) → tighten on `<Video> 16x9`; the human fine-tunes the cut in the NLE and approves it. The camera of each piece does not matter there: the comp picks it per span.
2. audio (one master for every format) → grade (optional, on the camera clips, per set).
3. formats: the all-angles master of the approved cut (every feed in one 8K frame, H.265), then the layout comp in HyperFrames: 16:9 first (layout spans cut on the approved cut, show-don't-tell, captions, sound), reviewed in HyperFrames Studio, then the 9:16 from the same spans. Each render passes video CI.
4. master: the approved comp renders checked and filed. wide: `skip`. clips: shorts from the approved 9:16.
5. qc, then publish.

**NLE route** (one camera, or the human finishes in the NLE): the long 9:16 first.

1. ingest → transcribe → rough → tighten → audio → grade → formats, all on `<Video> 9x16` (1080×1920). Each stage keeps its own review gate.
2. master: the human watches the whole 9x16 with its layers and approves it. It is frozen in `01 Timelines/Published` and rendered. Nothing after this point changes the approved cut.
3. Then side by side, both made from the approved 9x16: wide builds `<Video> 16x9`, clips builds the shorts `<Video> 9x16 S01 <topic>` (ranges the human picks, Green markers from the rough as candidates). Each has its own review, render and video CI.
4. qc, then publish: the long 9x16, the 16x9 and the shorts.

A change the human asks for after approval is made on the cut first (comp route: the cut timeline, then a new all-angles master and both comps; NLE route: the 9x16, re-approved and re-rendered), then carried to every format and short it touches. Kinds without a long edit (clip, motion, generative) keep their own formats stage. Timeline names: `/videos:resolve` § Where things go.

Not stages: `/videos:resolve` = rules and failure log for driving DaVinci Resolve through its MCP, used by every stage that touches Resolve; `/videos:learn` = folds past mistakes (record lessons + past agent sessions) back into the skills or the project's notes.

Companion plugins from the same marketplace: `audio-cleanup`, `vertical-clips`, `agent-ears` (breath / filler detection used by tighten). Missing → `/plugin install <name>@les-bunkers`.

## Video CI (mandatory)

Nothing reaches a human's eyes or a platform before video CI: before a review page, a contact sheet, a file path in chat, a status `review`, or a delivery, run `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/video_ci.py" <files> --slug <video> --stage <stage> [--var hook="…" --var cta="…"]` on every file you are about to show.

- RED: do not show it. Fix the cause, re-render, re-run. Can't fix → tell the human what failed, with the frames, and set the stage `blocked <what>`.
- AMBER: show it with the flagged frames (`checks/ci/<stage>/<file>_frames/`) and one line per flag. Never call an amber file clean.
- GREEN: show it. CI covers the mechanical and visual defects; taste, claims and pacing stay with the human.
- `videos.py set` refuses `review` / `done` on master, formats, wide, clips, generate, qc and publish unless the stage's CI index is fresh (files unchanged since their run) and has no red file. A dropped file leaves the index with `video_ci.py <file> --slug <video> --stage <stage> --forget`; `--skip-ci "<why>"` passes the gate only when the human asked for the file as it is, and the reason is logged.
- Cost: the tech tier is local and free; the visual tier is a paid API call (~$0.005 per short, at most $0.50 per file). Ask the human once per project for a standing go-ahead, record it in the project's style or house notes, then run CI without asking. Without it: `--no-visual "no go-ahead yet"` (the file is at best amber).

Profiles, checks, thresholds, where each stage runs it: [ci](${CLAUDE_PLUGIN_ROOT}/references/ci.md).

## House rules (every stage)

- Taste lives in the project: a stage that cuts or styles reads the record's `style:` files (edit style, caption look, brand). None → ask once, write the answer into a style file and add it to `style:`.
- Preview before full builds: one video, clip or look approved before a batch. Gates = status `review` until the human says go.
- Mistakes are lessons: when the human corrects or rejects something, a call fails or silently does nothing, or work has to be redone, log it once fixed: `videos.py lesson <slug> <stage> "<tried> → <what happened> → <do instead>"`. `/videos:learn` folds them into the skills; after a video ships, or when `videos.py lessons` lists several, offer it.
- Paid calls (speech-to-text APIs, generation, dubbing) need a go-ahead per job; cache every result next to the job and never request it twice.
- Local GPU generation (video or image models running on your own card) costs nothing per call but holds the GPU and a lot of RAM: run only what was asked and say the expected minutes first.
- Never cut voice the transcript lacks: words a speech-to-text model missed are listed for the human, not cut.
- Grade per set: a look never crosses shoots or sets (lighting differs).
- One render chain at a time; check free RAM first (< 4 GB → wait; ≥ 10 GB for heavy stages when browser renders, speech models or local generation share the machine). Background jobs longer than ~10 min get a periodic check that they are still moving, and an expected finish time at launch.
- Other agents, sessions and the human share the project: check who is on a video before writing, never edit a script a running job uses, hold the render lock for every GPU-heavy job, brief sub-agents with the CI rule, and tell the human where everything is: [working](${CLAUDE_PLUGIN_ROOT}/references/working.md). ffmpeg traps: [ffmpeg](${CLAUDE_PLUGIN_ROOT}/references/ffmpeg.md); install: [setup](${CLAUDE_PLUGIN_ROOT}/references/setup.md).
- NLE timelines: before rebuilding one, compare it with the last build (hand edits → ask); build new timelines beside old ones, never delete the human's. A timeline's name carries its format (`<Video> 9x16`, `<Video> 16x9`, `<Video> 9x16 S01 <topic>`); on the NLE route the 16x9 and the shorts come from the approved 9x16, never cut apart from it. In Resolve the bin says who has a timeline: `WIP/Processing` while an agent changes it (`B.claim`), `Waiting for review` when the agent is done and the stage's status turns `review` (`B.release`), `WIP/Latest` otherwise, approved copies in `Published` (`/videos:resolve`).
- DaVinci Resolve: load `/videos:resolve` before the first MCP call of a session. A timed-out script keeps running, one Resolve is shared with the human and other sessions, and many calls fail silently; its log ([resolve](${CLAUDE_PLUGIN_ROOT}/references/resolve.md)) lists every failed attempt with what to do instead.
- Camera changes on a multicam edit = the multicam clip's active angle, never another camera's clip on a track above it (Resolve: `scripts/resolve_multicam_angles.py`).
- Comp route: everything the viewer sees is built in HyperFrames from the all-angles master and `comp.json` (camera per span, layouts, screen crops and zooms, overlays, captions, sound); the human reviews and retouches in HyperFrames Studio, and `comp.py html` never overwrites their edits. [comp](${CLAUDE_PLUGIN_ROOT}/references/comp.md)
- NLE route: graphics are layers; compositing happens in the NLE. Anything drawn over footage (motion graphics, cards, labels, highlight boxes, hook and end cards, designed captions) renders alone with alpha (HyperFrames `--format mov` = ProRes 4444 + alpha, transparent root), on a canvas the size of the target timeline with the element at its default place (Resolve scales a smaller clip up to fit the frame), and goes on its own named track above the picture, where the human can move, scale, trim or drop it without a re-render. Changes to the footage itself (zooms, punch-ins, reframes, blurs and redactions, PiP of another camera, transitions, grade) are made on the clips in the NLE, never baked into a rendered copy of the cut. Bake graphics into one rendered video only for motion-only pieces with no footage under them, or a hands-off batch the human asked for. [layers](${CLAUDE_PLUGIN_ROOT}/references/layers.md)
- Going over a clip includes a layout pass and a show-don't-tell pass, without being asked. Any request to go over, do a pass on, polish or improve a clip or video runs both (stage formats, `/videos:formats` § Layout pass) next to whatever else was asked. Layout pass: read the cut sentence by sentence and mark the spans where framed panels of real footage on a moving wallpaper show the moment better than one full-frame camera (screen share in a window with the face in a corner bubble, two panels for an action and its effect, one big portrait + two small landscape, three cameras for a multi-camera result, a vertical slice beside its source), one radius, gutter and shadow, hard cuts between layouts. Show, don't tell: every sentence that names something visible (a control, a camera, a product, a result, a before / after) shows that thing (real footage, a crop of the screen recording, B-roll, a real photo, a drawn diagram); text cards stay only where nothing can be shown, and cards a span replaces are disabled, not deleted. A product named → its own footage, downloaded from its site without waiting to be asked; an action → shown (typing → the real screen; asking an AI → a generated UI of that app with the request typed and sent); nothing to film → generated B-roll (paid: a go-ahead and a credit cap) or a diagram. Preview stills before the build. [layers](${CLAUDE_PLUGIN_ROOT}/references/layers.md) § Layout sections; comp route: [comp](${CLAUDE_PLUGIN_ROOT}/references/comp.md) § Show, don't tell
- Review pages for the human run on localhost: `python "${CLAUDE_PLUGIN_ROOT}/scripts/serve.py" <folder> --port <p> --detach` (answers Range requests, so players can seek: `python -m http.server` sends whole files and videos will not scrub; binds 127.0.0.1; refuses a taken port; `--detach` = its own process, so it survives the session and a memory-pressure reap of session jobs). Give the human the URL and the PID; stop it by that PID.
- Never modify source media. Heavy media (rushes, masters, renders, HyperFrames projects) goes under `media_root`; the project keeps records, cut data, reports and upload-ready finals.
- Before publishing, check the picture for readable secrets (passwords, Wi-Fi keys, emails, phone numbers, client screens): video CI scans finals for them, `/videos:qc` has the procedure for screen recordings.
