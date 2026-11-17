# Claims lint: on-screen numbers and claims before rendering

`claims_check.py` reads every string that can reach the screen (HyperFrames HTML, graphics specs, captions, `--text`) and checks it against the workspace's claims file. Run it before every render: video CI checks the picture, this checks what the words promise. A production system rejects a claim nobody approved before it turns into a beautiful video.

```
uv run "${CLAUDE_PLUGIN_ROOT}/scripts/claims_check.py" <inputs...> [--text "..."] (--claims F | --slug S)
    [--words F]... [--words-key KEY --words-root DIR] [--captions CLASS] [--keys a,b] [--skip-keys a,b]
    [--today YYYY-MM-DD] [--json out.json] [--quiet]
```

Inputs, in any mix: HyperFrames project folders, `.html`, `.json` specs, `.srt` / `.vtt` (burned-in captions), `.txt`; globs work. Exit 0 green, 2 amber, 1 red, 3 could not run (claims file missing or invalid, unknown slug, missing input).

## Claims file

One per workspace: `claims.json` next to the records folder (`<workspace>/.agents/claims.json`), or `claims: <path>` in a record. Lookup: `--claims`, else the record's `claims:`, else `claims.json` beside the records folder.

- `approved[]`: `claim`, `numbers` (as written on screen; a trailing noun makes it strict: "6 jetons" approves 6 only before that noun, so an approved 31 does not approve every 31), `match` (phrases that state the claim; a claim word inside them is cleared), `source` (where the human approved it), `expires` (last valid day; after it the numbers and phrases turn red).
- `allow_numbers`: numbers that are never a claim (a year).
- `banned[]`: `text` and/or `numbers`, `why`.
- `claim_words`: superlatives and comparisons; replaces the built-in fr/en list.

Numbers are normalized (spaces, NBSP, `,` / `.` decimals, `$ € %`, `x10`, « fois plus »); phrases match without case or accents. When unsure, leave a claim out: a flag a human clears beats a wrong approval. Never add an approval without the place the human approved it.

## Verdicts

- RED: a price, percentage or multiplier not approved; a banned phrase or number; an approved claim past `expires`.
- AMBER: any other number not approved; a claim word with no approved claim around it.
- Said on camera = already in the audio: numbers and claim words in the transcript pass (`--words <file.words.json>`, `--words-key timing_source` to find each spec's own transcript, or `--captions <class>` to use a project's word-timed captions as its transcript). Spoken numbers are read with `text2num` (« mille quatre cent quatre-vingt-quinze », « trois fois » = 3×). Words inside a banned phrase clear nothing.
- Skipped: keycaps, list markers (« étape 2 », « 1 · Cadrage »), metadata keys (file, path, why, anchor, `*_note`…), paths, URLs, colours, icon ids, timestamps, aspect ratios (9:16). Plans full of notes: `--keys hook,label,text`.

## Measured

44 graphics specs (1,551 texts) with each spec's transcript: 0 red, 2 amber, both genuine (a number the clip never says, a paraphrase turned superlative), about 2 s. 20 HyperFrames builds (3,084 texts) with `--captions cap-line`: green; without a transcript, 50 ambers, all caption words the speaker says. False alarms fixed while tuning: the « 16 » of 9:16, `x 60-400` coordinates read as multipliers, timestamped folder names, bare approved numbers approving the same value anywhere.
