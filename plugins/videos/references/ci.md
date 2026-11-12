# Video CI: automated QA before a human sees a render

Every file an agent is about to show a human (a review page, a contact sheet, a path in chat, a status `review`) or to ship goes through `video_ci.py` first. Like CI on code: the agent never asks a human to look at something a machine could have rejected, and the record refuses `review` / `done` without a fresh run.

```
uv run "${CLAUDE_PLUGIN_ROOT}/scripts/video_ci.py" <files...> --slug <video> --stage <stage> [--profile P] [--var hook="..." --var cta="..."]
```

Exit 0 = green, 2 = amber, 1 = red, 3 = could not run. Reports: `<record>/checks/ci/<stage>/<file>.json`, failing frames beside them in `<file>_frames/`, stage index `<record>/checks/ci/<stage>.json`. Without `--slug`: `<file folder>/ci/` (or `--out`).

## Verdicts and what the agent does

| Verdict | Means | Agent |
|---|---|---|
| RED | a blocking check failed | do not show the file. Fix the cause, re-render, re-run CI. Can't fix → tell the human what failed and how, with the frames, and leave the stage `blocked <what>` |
| AMBER | something a human should look at, or a check that could not decide | show the file with the flagged frames and one line per flag ("caption close to the chin at 7.5 s"). Never call an amber file clean |
| GREEN | nothing found | show it. Checks don't replace the human's eye on taste, claims and pacing |

## The gate

`videos.py set <slug> <stage> "review …" | "done …"` on `master`, `formats`, `clips`, `generate`, `qc` and `publish` reads the stage index and refuses unless:
- the index has at least one file (`qc` and `publish` read the master, formats, clips and qc indexes together),
- every file still has the size and modification time it had when CI ran (a re-render after CI = stale = run CI again),
- no file is red.

The log line records the CI tally (`(CI: 3 green, 1 amber, 0 red)`). A file the human will not see any more (rejected take, dropped format) leaves the index with `video_ci.py <file> --slug <video> --stage <stage> --forget`. `--skip-ci "<why>"` passes the gate anyway and writes the reason in the log; use it only when the human asked for the file as it is.

## Profiles

Auto: an image → `generated`; portrait or under 3 min → `short`; else `master`. Pass `--profile` when it is something else.

| Profile | For | Tech tier | Visual tier (pack) |
|---|---|---|---|
| `short` | finished short, any aspect | H.264/HEVC yuv420p + AAC; −14 ±0.5 LUFS, TP ≤ −1 dBTP; black frames inside the video fail; frozen picture and dead air > 1.5 s flag | `short`: hook text (`--var hook`), closing text (`--var cta`), caption on a face, face cut by the frame edge, grade, secrets |
| `clip` | vertical clip cut from a long video | same | same; without `--var cta` the closing-text check is skipped (clips often end without one) |
| `master` | long master | codec/pix/audio present; loudness fails; black, frozen, dead air flag | `master`: face cut by the frame edge, grade, secrets; at most 150 stills per check (~$0.03 per master whatever its length) |
| `generated` | AI keyframe, storyboard, take | readable, no audio needed; black flags | `generated`: stray text, object wiping across the frame; hands as information |
| `preview` | rough cut, timeline export, low-res review render | everything flags, nothing fails | `secrets` |

Tech checks ignore the first and last 0.5 s (fades). A missing var turns that check amber ("not checked: pass --var hook"), never green.

Visual-tier cost: `short` ≈ 45 requests, ~$0.005, a few seconds; reruns of unchanged frames come from the decide cache for free. Above `--max-cost` (default $0.50 per file) the visual tier is skipped and the file goes amber. The visual tier failing to run (no key, API down) makes the file red: fix it, or `--no-visual "<why>"` (recorded, at best amber). Methods, thresholds and limits of each visual check: [decide](decide.md).

## Where each stage runs it

| Stage | Files | Command |
|---|---|---|
| master | the rendered master | `video_ci.py <master> --slug <v> --stage master` (after `finals_check.py`) |
| formats | every rendered format | `video_ci.py <finals> --slug <v> --stage formats --var hook=... --var cta=...` |
| clips | every clip of the batch × the look shown | `video_ci.py <clips> --slug <batch> --stage clips --profile clip --var hook=...` (per clip hook: run per clip) |
| generate | every keyframe and take before a review page or a paid render | `video_ci.py <takes> --slug <v> --stage generate --profile generated` |
| rough / tighten | a review render, when one is made | `video_ci.py <render> --profile preview` (no gate: the human reviews these in the NLE) |
| qc | the finals of master / formats / clips | already in their indexes; `qc done` passes when they are fresh and none is red |

## Measured

On a 48 s 9:16 short and six broken copies (hook cut, end cut, grade removed, caption moved onto the face, reframe off by 250 px, a Wi-Fi password card): the untouched file green, each broken file red on its own check, 5 s for four files from cache. A generated take where the model invented a pillar crossing the frame: red; a clean take of the same shot and its storyboard: green. Gate: refused with no report, refused with a red file, passed after `--forget`, refused after the file changed, passed with `--skip-ci` and logged.
