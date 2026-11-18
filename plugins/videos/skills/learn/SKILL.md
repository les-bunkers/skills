---
name: learn
description: Learn from past mistakes in the video pipeline and fold them back into the skills — gathers the lessons logged in video records (videos.py lessons) and digests past agent sessions (failed or silent tool calls, the human's corrections, rejections and interruptions), sorts each mistake into what was tried, what happened, why, and what to do instead, checks what the skills already say, then updates the stage skills and references (general method) or the project's style and house notes (taste, local facts), fixes scripts the mistakes expose, and releases the plugin after the human's go-ahead. Use when asked to learn from mistakes, update the video skills from experience, run a retrospective or post-mortem on a video or a stage, after a stage needed several tries or a human correction, or when running /videos:learn.
argument-hint: "[since YYYY-MM-DD | video name | resolve]"
---

`videos.py` = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"`. `S` = `${CLAUDE_PLUGIN_ROOT}/scripts`. Reader brief, findings format, routing table and cost: [learn](${CLAUDE_PLUGIN_ROOT}/references/learn.md).

## Capture while working (every stage)

The moment a mistake is caught and fixed (the human corrects or rejects something, a call fails or silently does nothing, a render or build has to be redone, CI goes red because of the pipeline), log it in the video's record:

`videos.py lesson <slug> <stage> "<what was tried> → <what happened, exact error or numbers> → <what works instead>"`

One line, written when the details are fresh. These open lessons are the first input of a learn run; a session transcript is the second, for what nobody logged.

## Do

1. Scope from the argument: a date (`since 2026-03-01`), a video (its record's lessons and the sessions in its date span), or a focus (`resolve` = the DaVinci Resolve MCP only). No argument: everything since the last run (`videos.py where` → records root; state in `<records>/_learn/state.json`). First run with no date → ask how far back.
2. Open lessons: `videos.py lessons` (`--ws W` for one workspace).
3. Sessions: `python "$S/learn_digest.py" --out <scratch>/learn-digests --project <project root> --since <date> --state <records>/_learn/state.json [--focus resolve]`. It writes one digest per session with pipeline activity (flagged calls with input, result and the agent's next words; every human message; interruptions), `index.tsv` and `groups.json`. Digests quote private work: keep them in a scratch folder, never in a repo.
4. Read. Digests ≤ ~250 KB in total → read them yourself. More → one reader agent per group in `groups.json`, all in one message, each with the brief in the reference (they write findings files; you read those, not the digests). Several groups cost real tokens: give the human the count and ask before a fan-out of more than 3 groups.
5. Merge the findings and the open lessons: one entry per distinct mistake, repeats counted. Each entry: tried → got (exact error, numbers) → cause → what works instead → when → already documented? (yes, partly, no, or contradicts a doc line).
6. Route each new, partial or contradicting entry with the table in the reference: general method goes in the plugin (the stage's SKILL.md as a rule with its reason, or the reference that stage links), taste goes in the project's style files, local facts go in the project's notes. A lesson never goes in two places.
7. Write the changes:
   - A rule a stage must follow: one line in that stage's SKILL.md with its reason. A failed attempt: an entry in the area's log (Resolve: `references/resolve.md`, format `✗ tried → what happened → what to do instead`).
   - A doc line the evidence contradicts gets corrected in place, with the date and what showed it. When two sessions disagree and neither explains it, write both results and "retest".
   - A mistake caused by a script bug: fix the script and run it once on real material (read-only when it touches Resolve), then say so in the report.
8. Release plugin changes. `videos.py where` → `skills_repo` = a git clone of the plugin repo (never edit the installed plugin cache). In the clone: the repo's leak check with your private terms list, `claude plugin validate plugins/videos` and `.`, bump `version` in `plugin.json`, commit. Push and `claude plugin update videos@<marketplace>` only after the human says go: the repo may be public. No `skills_repo` → put the general lessons in the project's notes under a "for the plugin" heading and tell the human.
9. Close: tick each folded lesson in its record (`- [ ]` → `- [x] … → <file it went to>`). Report: sessions and lessons read, entries new / known / contradicted, every file changed with one line on what changed, what waits for the human (push, retests, decisions).

## Rules

- A lesson names the attempt, the result and the fix. "Be careful with X" or "check Y" without a reason is not a lesson: find what failed or drop it.
- The human's taste (pacing, framing, captions, colour, music, wording) belongs to the project's style files, never to the plugin.
- Nothing in the plugin names people, clients, projects, drives, accounts or machines.
- A choice recorded under a record's `## Decisions` is the human's; don't turn it into a "mistake".
- Check before adding: most findings are already known. Known ones count but change nothing.
