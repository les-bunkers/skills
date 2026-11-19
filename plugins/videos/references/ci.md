# Video CI: automated QA before a human sees a render

Every file an agent is about to show a human (a review page, a contact sheet, a path in chat, a status `review`) or to ship goes through `video_ci.py` first. Like CI on code: the agent never asks a human to look at something a machine could have rejected, and the record refuses `review` / `done` without a fresh run.

```
uv run "${CLAUDE_PLUGIN_ROOT}/scripts/video_ci.py" <files...> --slug <video> --stage <stage> [--profile P] [--var hook="..." --var cta="..."]
    [--events <spec.json>] [--input <file or folder> ...] [--manifest <inputs.json>]
```

Exit 0 = green, 2 = amber, 1 = red, 3 = could not run. Reports: `<record>/checks/ci/<stage>/<file>.json`, failing frames beside them in `<file>_frames/`, stage index `<record>/checks/ci/<stage>.json`. Without `--slug`: `<file folder>/ci/` (or `--out`).

## Verdicts and what the agent does

| Verdict | Means | Agent |
|---|---|---|
| RED | a blocking check failed | do not show the file. Fix the cause, re-render, re-run CI. Can't fix → tell the human what failed and how, with the frames, and leave the stage `blocked <what>` |
| AMBER | something a human should look at, or a check that could not decide | show the file with the flagged frames and one line per flag ("caption close to the chin at 7.5 s"). Never call an amber file clean |
| GREEN | nothing found | show it. Checks don't replace the human's eye on taste, claims and pacing |

"Shown" includes every way a file leaves the agent: an upload to a video host or a social scheduler, a hub or review page tile, a path in a report, a file handed to another agent for posting. A report about a file quotes its CI verdict line (verdict + each flag); a brief to a subagent that renders or uploads carries this rule. Two red files went to a video host in one week because the gate only fired at `videos.py set`, and one red verdict never reached the human's report.

**Accepted red.** Only the human accepts a red, after seeing the flagged frames. Record it in the video record's `## Decisions` (file, check, time, who, date) and pass `--skip-ci "accepted by <human> <date>: <check> at <t> s"`; the next build re-runs CI and the decision says what was already accepted. An agent that calls a red a false alarm still shows the frames and asks: agents that shipped on their own call put a real framing defect on a platform.

## The gate

`videos.py set <slug> <stage> "review …" | "done …"` on `master`, `formats`, `wide`, `clips`, `generate`, `qc` and `publish` reads the stage index and refuses unless:
- the index has at least one file (`qc` and `publish` read the master, formats, wide, clips and qc indexes together),
- every file still has the size and modification time it had when CI ran (a re-render after CI = stale = run CI again),
- every input recorded with `--input` / `--manifest` is unchanged (a file: size + mtime; a folder: a fingerprint of every file's path, size and mtime): an edited spec, a new LUT, a re-cut master or a touched HyperFrames project makes the final stale even though the mp4 itself did not move,
- no file is red.

The log line records the CI tally (`(CI: 3 green, 1 amber, 0 red)`). A file the human will not see any more (rejected take, dropped format) leaves the index with `video_ci.py <file> --slug <video> --stage <stage> --forget`. `--skip-ci "<why>"` passes the gate anyway and writes the reason in the log; use it only when the human asked for the file as it is.

## Profiles

Auto: an image → `generated`; portrait or under 3 min → `short`; else `master`. Pass `--profile` when it is something else.

| Profile | For | Tech tier | Visual tier (pack) |
|---|---|---|---|
| `short` | finished short, any aspect | H.264/HEVC yuv420p + AAC; −14 ±0.5 LUFS, TP ≤ −1 dBTP; black frames inside the video fail; frozen picture and dead air > 1.5 s flag; scene changes found for the transitions sample | `short`: hook text (`--var hook`), closing text (`--var cta`), caption on a face, face cut by the frame edge, grade, secrets, text colliding or cut by the frame edge after each transition, overlay text too small for a phone (soft) |
| `clip` | vertical clip cut from a long video | same | same; without `--var cta` the closing-text check is skipped (clips often end without one) |
| `master` | long master | codec/pix/audio present; loudness fails; black, frozen, dead air flag; scene changes | `master`: face cut by the frame edge, grade, secrets, text colliding or cut off after transitions (cards, titles); at most 150 stills per check and 60 transitions (~$0.03-0.05 per master whatever its length) |
| `generated` | AI keyframe, storyboard, take | readable, no audio needed; black flags | `generated`: stray text, object wiping across the frame; hands as information |
| `preview` | rough cut, timeline export, low-res review render | everything flags, nothing fails | `secrets` |

Tech checks ignore the first and last 0.5 s (fades). A missing var turns that check amber ("not checked: pass --var hook"), never green. A video that by design has none of an element (a teaching video with no call to action) passes `--var cta=-`: the check is skipped and reported as info; say why in the record's `## Decisions`.

## Transitions, phone size, soft checks

- **Transitions.** Regular sampling (a still every 2–3 s) misses what happens in the half second after a cut or a graphic entrance: an outgoing label still on screen under the incoming one, a title that slid in past the edge. The tech pass runs ffmpeg `scdet` (threshold 6: layout switches, cuts and graphic entrances, not eased push-ins; 27 found in a 126 s teaching video) in the same decode as black/freeze; `--events <file>` adds known times that change no scene (a label over a static shot): a list of seconds, or `{t|start, dur|duration}` items (the exit at t+dur counts too), or an object holding such a list under `events` / `graphics` / `transitions` / `segments` (graphics specs, cut lists). Events closer than 0.4 s merge; at most 60 per file (given events first, then the strongest scene changes). The pack's `transitions` sample gets stills 0.1 s (mid-transition) and 0.6 s (landed) after each. Run alone (`decide.py run short`), the sample is empty and those checks report `info`.
- **Phone size.** `phone_small_text` looks at a still every 4 s shrunk to 360 px wide (decide sample key `px`), the size a phone shows. It asks whether overlay text is set much smaller than the captions: "too small to read" wordings failed (AUC 0.75) because the model reads 9 px text. HyperFrames `check` has its own type floor (`text_below_floor`: DOM text under 2.6 % of the short side held ≥ 0.5 s), which sees only HTML text; this check is for text baked into pictures (screenshots, UI captures, footage), like the pedal-board overlay below, made of PNGs.
- **Soft checks** (`"soft": true` in a pack check): a fail shows as AMBER with "(soft check: shown, not blocking)". For calls a human should see but that must not stop a delivery on their own (legibility at phone size).

## Provenance (stale inputs)

Pass the files a render was built from: `--input row.json --input formats/9x16 --input grade.cube` (repeat), or `--manifest inputs.json` (`{"inputs": [paths]}` or a list, relative to the manifest; builders can write one beside their output). The report and the stage index keep each input's print; `videos.py set … review|done` refuses once one of them changed ("built from inputs that changed after its CI run (row.json)"). Re-render, then re-run CI. Folders are fingerprinted from file names, sizes and mtimes (hidden folders and `__pycache__` skipped, at most 5000 files), so a big project costs a directory walk, not a hash of its media.

Visual-tier cost: `short` ≈ 70 requests for a 48 s short, ~$0.008, 30 s; a 2-min teaching video ≈ 200 requests, ~$0.02, 65 s; reruns of unchanged frames come from the decide cache for free. Above `--max-cost` (default $0.50 per file) the visual tier is skipped and the file goes amber. The visual tier failing to run (no key, API down, prepaid credits used up: HTTP 402 "prepayment credits are depleted") makes the file red: fix it, or `--no-visual "<why>"` (recorded, at best amber), check secrets yourself on stills and tell the human to top up. Methods, thresholds and limits of each visual check: [decide](decide.md).

## Where each stage runs it

| Stage | Files | Command |
|---|---|---|
| master | the rendered master (talking-head: the approved long 9x16) | `video_ci.py <master> --slug <v> --stage master` (after `finals_check.py`) |
| wide | the 16x9 rendered from the approved 9x16 | `video_ci.py <16x9 master> --slug <v> --stage wide` (after `finals_check.py`) |
| formats | every rendered format | `video_ci.py <finals> --slug <v> --stage formats --var hook=... --var cta=... --events <graphics spec> --input <spec> --input <project dir>` |
| clips | every short cut from the approved 9x16, or every clip of a batch × the look shown | `video_ci.py <clips> --slug <batch> --stage clips --profile clip --var hook=...` (per clip hook: run per clip) |
| generate | every keyframe and take before a review page or a paid render | `video_ci.py <takes> --slug <v> --stage generate --profile generated` |
| rough / tighten | a review render, when one is made | `video_ci.py <render> --profile preview` (no gate: the human reviews these in the NLE) |
| qc | the finals of master / formats / clips | already in their indexes; `qc done` passes when they are fresh and none is red |

A builder or batch script that calls `video_ci.py` passes `--slug`, `--stage` and the vars: a bare call (`video_ci.py final.mp4`) writes a report beside the file that the gate never reads and leaves hook and closing text unchecked (every file of a 9-video batch came back amber for it).

## Known false alarms

Look at the flagged frame before deciding; a known pattern is a reason to look, not to skip.

| Check | Fires on | Seen |
|---|---|---|
| `caption_off_face` | a caption under the mouth: on the beard, chin, neck or chest | 6+ reds at 0.50–0.58 on clips and teaching videos, all accepted by the human; 1 true red (a caption on the forehead). Pack wording changed 2026-11-18 (beard, chin, chest = no): recalibrate on such frames |
| `text_collision`, `text_cut_off` | a screen recording of an app whose own text scrolls under its controls or past its window (a teleprompter app) | 0.79–0.82 and 0.65–0.66 on a 4K master, above the calibrated clean maxima below |
| `text_cut_off` | an animated wipe caught 0.1 s into its move; a HUD set near a frame corner | 0.60–0.63 |
| `no_secrets` | redaction covers with blank fields, a blurred region, a QR code or network name the human chose to leave sharp | 0.48–0.58 |
| `face_in_frame` | stacked two-person layouts (each panel crops heads by design) | amber at the stack shot, twice |
| `black_frames`, `dead_air` | content: a lights-off or silhouette lighting preset, a gap before an alternate take in a review render, a silent demo pause | flags only |

## Measured

On a 48 s 9:16 short and six broken copies (hook cut, end cut, grade removed, caption moved onto the face, reframe off by 250 px, a Wi-Fi password card): the untouched file green, each broken file red on its own check, 5 s for four files from cache. A generated take where the model invented a pillar crossing the frame: red; a clean take of the same shot and its storyboard: green. Gate: refused with no report, refused with a red file, passed after `--forget`, refused after the file changed, passed with `--skip-ci` and logged; refused after a `--input` file changed and after a file was added to a `--input` folder.

Transition and phone checks, calibrated on 120 stills taken 0.1 s and 0.6 s after scene changes of 6 shipped renders (two 9:16 shorts, two 9:16 and two 16:9 teaching videos) plus mutants drawn on copies:

| Check | Separation |
|---|---|
| `text_collision` (two captions overlapping, one at 55 % = crossfade) | mutants 0.77–0.97, clean frames ≤ 0.57 (a prompter screen full of text); AUC 1.00, threshold 0.67. A 4K screen recording of the prompter app scrolling later reached 0.82 (Known false alarms) |
| `text_cut_off` (a caption running past the left or right edge) | mutants 0.39–0.90 (mean 0.62), clean ≤ 0.42 (prompter text clipped by its own window); AUC 1.00, borderline frames go amber (band 0.06) |
| `phone_small_text` (24–28 px labels on a 1080 frame, shrunk to 360) | small labels 0.52–0.97, captions-only and screen-recording frames ≤ 0.50; AUC 1.00 on 90 stills. It found a real case: a teaching video whose pedal-board overlay carries labels that are unreadable at phone size |

End to end on a 48 s short with three defects drawn in (collision after a cut, a caption cut by the right edge, a collision over a static shot given only through `--events`, a 26 px chip): each caught at its time; the untouched short passed all three checks.
