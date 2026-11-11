# Les Bunkers skills

[Français](README.fr.md)

Claude Code plugins for producing video, built while editing the talking-head videos, podcasts and short clips that come out of the Les Bunkers studio. They cover the whole job, from the first record of a video to the files you upload, and each step leaves a plain-text trail so the next session (or the next person) picks up where the last one stopped.

## Install

In Claude Code:

```
/plugin marketplace add les-bunkers/skills
/plugin install videos@les-bunkers
```

Then install the companions you need:

```
/plugin install audio-cleanup@les-bunkers
/plugin install vertical-clips@les-bunkers
/plugin install agent-ears@les-bunkers
```

Updates: `/plugin marketplace update les-bunkers`.

## Plugins

| Plugin | Skills | What it does |
|---|---|---|
| `videos` | `/videos`, `/videos:new`, `:ingest`, `:transcribe`, `:rough`, `:tighten`, `:audio`, `:grade`, `:master`, `:formats`, `:clips`, `:qc`, `:publish` | The pipeline. `/videos` shows a board of every video in progress; `/videos <name>` opens one and runs or proposes its next stage. Each stage has its own skill and writes its status back to the video's record. |
| `audio-cleanup` | `/audio-cleanup` | Speech cleanup for a DaVinci Resolve edit: plans from what the timeline actually plays, splits two speakers on one mic, enhances speech, masters to −14 LUFS, puts the new tracks back in Resolve and builds an A/B listening page. |
| `vertical-clips` | `/vertical-clips` | A 9:16 clip factory for podcasts and long videos: sticky face crops, stacked two-person layout, caption text fixed against three transcripts, word-timed captions and hook rendered with HyperFrames, QC, a review page and export. |
| `agent-ears` | `/agent-ears`, `/ears-bakeoff` | Lets an agent hear breaths and fillers (« euh », "um") so the tighten stage can cut them, and a blind labelling page plus scoring kit to test any detector against your own ears. |

Each plugin's `references/setup.md` lists what to install (ffmpeg, uv, Python environments, models, DaVinci Resolve Studio and its MCP server, HyperFrames).

## How a video moves

```
/videos:new ──► ingest ──► transcribe ──► rough ──► tighten ──► audio ──► grade ──► master ──► formats ──► qc ──► publish
                                                      ▲           ▲                              │
                                                 agent-ears  audio-cleanup        clip batches: vertical-clips
```

Every video gets one record, `.agents/videos/<slug>/video.md`: one line per stage (`todo`, `doing`, `review`, `done`, `skip`, `blocked`), the decisions the human made, notes and a dated log. Stages that wait on a person stop at `review` and say what to look at. Finished files land in `artifacts/videos/<slug>/` with a `release.md` holding the caption, platforms, post date, loudness and QC result. Heavy media stays on your media drive (`media_root`).

Put a `videos.json` at the root of your project (or of a folder holding several clients' workspaces) to set where those folders go; `uv run <plugin>/scripts/videos.py where` shows what it found. Details: [records](plugins/videos/references/records.md).

## What it assumes

- Claude Code with Bash, Python 3.11+, [uv](https://docs.astral.sh/uv/) and ffmpeg on PATH.
- DaVinci Resolve Studio with Blackmagic's MCP server for the stages that build timelines, place audio or export looks. The other stages work on files alone.
- An NVIDIA GPU for speech enhancement, speaker diarization and breath detection. Everything else runs on a CPU.
- Taste lives in your project, not in the plugin: point a video's `style:` at your own edit-style, caption and brand notes. The defaults are the Les Bunkers house style (breaths and « euh » out, pauses around 0.12 s, J-cuts of 1–4 frames), and every default says why it was chosen.

## Paid services

Some steps call paid speech-to-text APIs (xAI Grok, ElevenLabs Scribe, OpenAI). The skills ask before the first paid call of a job and cache every result next to the job, so a rerun never pays twice. Keys come from environment variables (`XAI_API_KEY`, `ELEVENLABS_API_KEY`, `OPENAI_API_KEY`, `HF_TOKEN`); each provider also reads a `*_BASE_URL`, so you can route calls through a local key proxy instead of exporting keys.

## Contributing

Conventions for skills, scripts, config and the files plugins hand to each other: [CONTRIBUTING.md](CONTRIBUTING.md). Run `python tools/leak_check.py` and `claude plugin validate plugins/<plugin>` before a pull request.

## License

[MIT](LICENSE). Model weights the plugins download (speech enhancement, diarization, breath detection) keep their own licenses, listed in each plugin's setup notes.
