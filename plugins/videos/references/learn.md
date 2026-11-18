# Learning from past mistakes

How `/videos:learn` reads past work and where each lesson goes. The method was first used to build [resolve](resolve.md) from 18 sessions of Resolve work: about 830 tool calls, six parallel readers, ~210 findings, ~75 of them new or only partly documented, plus a script bug none of the docs had caught (cross fades read back as unknown clips). The first whole-pipeline run read 136 sessions (4,800 calls, 800 flagged) with nine readers: ~570 findings, ~150 of them new, partly documented or contradicting a doc, and seven script bugs (captions hidden at a float boundary, a content check that failed every dual-mono master, a camera rotation that never came back to the face camera, stale track names, missing stderr encoding in 30 scripts, a clip check that skipped transitions, a merge the plugin never got). Most new lessons were about coordination and the tools around the NLE (ffmpeg, the HTML renderer), not the NLE itself.

## Inputs

1. **Open lessons** in the records: `videos.py lesson <slug> <stage> "<tried> → <what happened> → <do instead>"` writes `- [ ] <date> <stage>: …` under `## Lessons`; `videos.py lessons` lists the open ones across workspaces (`--all` adds folded ones).
2. **Session digests** from `scripts/learn_digest.py`: the agent's own transcripts (Claude Code JSONL), filtered to pipeline activity.

| Digest line | Means |
|---|---|
| `### [time] tool IS_ERROR / DENIED` | the call errored, or the human refused it |
| `### [time] tool` | the result held an error, False / None / `[]`, a timeout, or (tool calls) the agent's next words said something went wrong |
| `INPUT` / `RESULT` / `AGENT AFTER` | what was run, what came back, what the agent said next (usually the diagnosis) |
| `**USER [time]:**` | every human message: corrections, rejections, taste |
| `**USER INTERRUPTED**` | the human stopped the agent: look at what it was doing just before |
| `_agent [time]:_` | the agent saying something failed, outside a flagged call |

Flags are wide on purpose: most flagged calls are noise (a `False` that was expected, an `error` word in a log). The readers sort them.

Size seen: three weeks of a busy project = 136 sessions, 4,800 pipeline calls, 800 flagged, 1.9 MB of digests, 9 reader groups of ≤ 250 KB; the Resolve focus alone = 16 sessions, 850 KB, 4 groups. The script takes ~10 s.

## Reader brief

One reader agent per group of `groups.json`, all launched in one message. Each reader costs ~250–320k tokens for a 250 KB group: ask the human before more than 3. Fill the brackets:

> Goal: list every mistake in these past sessions of our video pipeline (an approach, call, parameter, script or workflow that failed, silently did nothing, returned wrong data, was slow, or that the human corrected or rejected) and what fixed it, so we can update our video skills. Read-only, plus one output file.
>
> Digests (one per session) in `<dir>`: `<files>`. Line legend: `<paste the table above>`. For more context on one finding, search the raw transcript named at the top of a digest; never read a transcript whole.
>
> Already documented, read first: `<plugin>/skills/<stages touched>/SKILL.md`, `<plugin>/references/*.md` (resolve.md for Resolve), `<project style and house notes>`.
>
> For each distinct mistake (merge repeats, count them), write to `<out>/<group>.md`, grouped by stage or area:
> - **Tried:** exact call or approach · **Got:** symptom, exact error text, numbers · **Cause:** if established, else "unknown" · **Worked instead:** exact call or approach, or "unsolved" · **When/where:** date, session id · **Known:** yes (file + line) | partial (what is missing) | NO | CONTRADICTS (file + line)
>
> Include the human's corrections and rejections (quote them). Skip pure typos with no lesson unless repeated. Be exhaustive on NO / partial / CONTRADICTS; known ones get one line. Final reply ≤ 150 words: counts per area and the 5 most important new findings.

## Where a lesson goes

| The lesson is about | It goes to |
|---|---|
| DaVinci Resolve or its MCP (API calls, renders, pages, timeouts, hangs) | [resolve](resolve.md), its area, as `✗ tried → what happened → what to do instead` |
| how a stage works (order, defaults, gates, checks) | that stage's `skills/<stage>/SKILL.md`: one rule with its reason |
| edit engine formats, detectors, measurements | [engine](engine.md) |
| video CI checks and thresholds | [ci](ci.md) · visual asserts: [decide](decide.md) |
| graphics layers and compositing | [layers](layers.md) |
| ffmpeg commands and filters | [ffmpeg](ffmpeg.md) |
| several agents, sessions or the human on one project: claims, shared scripts, locks, briefs, reports | [working](working.md) |
| installing a tool, environment variables, project config | [setup](setup.md) |
| records, statuses, the board, publishing | [records](records.md) or the house rules in `skills/videos/SKILL.md` |
| audio cleanup, vertical clips, breath detection | the companion plugin's skill or reference |
| the human's taste: pacing, framing, captions, colour, music, wording | the project's style files (the record's `style:`) |
| local paths, machines, accounts, project scripts and their quirks | the project's own notes |
| a bug in a plugin script | the script (fixed and run once on real material) + the doc line that describes it |

Several sessions sharing one tool, or one human, produce lessons about coordination (who may change what, when): those are general method and go in the plugin, worded without names.

## State

`learn_digest.py --state <records>/_learn/state.json` stores each digested transcript with its modification time; the next run reads only new or changed ones. Folded lessons are ticked in their records. Digests and findings files stay in a scratch folder: they quote private work.
