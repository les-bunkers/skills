---
name: transcribe
description: Transcribe stage of a video — word-level transcripts with fillers kept for cutting (any existing transcript imported, else xAI Grok speech-to-text, paid), speaker ids for two voices, timed fillers and breaths from agent-ears for the tighten pass, and the caption text check. Use when footage needs a transcript with word timestamps, a take has no subtitles, captions carry misheard names, speakers must be labelled, or when running /videos:transcribe.
argument-hint: "[video name]"
---

`videos.py` = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"`. `S` = `${CLAUDE_PLUGIN_ROOT}/scripts`. Work from the record folder (`videos.py show <video>` → `record:`); ingest made `cut/edit.json`. Words format and fields: [engine](${CLAUDE_PLUGIN_ROOT}/references/engine.md).

## Cutting transcript (one Words file per source)

1. A transcript already exists (studio subtitles JSON, a Grok / Scribe / OpenAI response, an SRT)? Import it, free:
   `python "$S/import_words.py" <transcript> --out cut/words/<id>.words.json --language fr`
   An SRT gives evenly spread word times: good for anchors, too coarse for tighten → transcribe properly before tightening.
2. None → Grok speech-to-text. **Paid** (xAI bills per audio minute): run without `--yes` first, it prints files and minutes; ask the human with that number; then
   `python "$S/stt_grok.py" --project cut/edit.json [--src A] --yes` (two voices: `--diarize`)
   → `cut/words/<stem>.grok.json` (raw response, the cache: never requested twice), `<stem>.words.json`, `<stem>.srt`. Env: `XAI_API_KEY`, `XAI_BASE_URL` (default `https://api.x.ai/v1`; a local key proxy works with a placeholder key). It sends `filler_words=true`: Grok drops « euh » / "um" by default, and tighten needs them.
3. Point each source at its file in edit.json: `"words": "words/<…>.words.json"`, `"words_media"` = the file it was made on, `"captions"` = a hand-fixed SRT if one exists (its text survives every retime). Run `uv run "$S/sync.py" --project cut/edit.json --write` if `words_media` is not the program file.
4. Read it: `python "$S/words_dump.py" --project cut/edit.json --src A --out cut/words/A.txt` (one line per sentence with its time). This is what `/videos:rough` anchors on.

## Fillers and breaths for tighten (agent-ears plugin)

Grok wrote only 12 of 20 hand-labelled « euh » and times its fillers up to 0.34 s off; breaths are not in any transcript. `/agent-ears` (install `agent-ears@les-bunkers`) runs on each source's **program file** and writes `<stem>.ears.json` (breaths) and `<stem>.scribe.words.json` (timed fillers; ElevenLabs Scribe, paid: ask first). Set `"ears"` and `"fillers"` per source in edit.json. Without them tighten still runs (DSP breaths, transcript fillers) and says so.

## Caption text

The words transcript mishears rare names and products. Read the dump, list suspected misses with their times in the record (`videos.py note <slug> "captions: 'Ocean' at 04:12 = Notion"`) and fix them in the source SRT given as `captions` (hand fixes survive retiming), or on the subtitle track later. Add recurring names to `keyterms` for the next takes.

## Rule

Voice the transcript has no words for may be words it missed: tighten never cuts it and lists it for the human.

## Record

`videos.py set <slug> transcribe "done <date> <n> words, <model>" --next "rough"`.
