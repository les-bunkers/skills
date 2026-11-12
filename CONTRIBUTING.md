# Conventions

Rules every plugin in this marketplace follows. Read this before adding or changing a skill or script.

## Layout

```
.claude-plugin/marketplace.json        one entry per plugin
plugins/<plugin>/
  .claude-plugin/plugin.json           name, version, description, author, license, keywords
  skills/<skill>/SKILL.md              one folder per skill; supporting docs beside it
  scripts/                             code the skills run (shared by the plugin's skills)
  references/                          longer docs a skill loads on demand (setup, formats, measurements)
tools/                                 repo checks (leak_check.py)
```

A skill named `tighten` in plugin `videos` is `/videos:tighten`, or `/tighten` when no other installed skill has that name. A skill named like its plugin (`videos/skills/videos`) answers to `/videos`.

## SKILL.md

- Frontmatter: `name` (= folder name), `description` (what it does + when to use it, in the words a request would contain: it is the router), optional `argument-hint`. Add `disable-model-invocation: true` only where a human must control timing (publishing, paid batch jobs).
- Body: steps an agent can follow without asking questions. Commands are copy-pasteable. Every default carries its reason in a few words (the measurement or failure that set it).
- Refer to bundled files with `${CLAUDE_PLUGIN_ROOT}/scripts/...` (plugin-wide) or `${CLAUDE_SKILL_DIR}/...` (files inside the skill folder).
- Keep SKILL.md under ~500 lines; move setup, file formats, measurements and history to `references/*.md` and link them.
- English. Speech-specific rules (French fillers, Québec spoken forms) stay as they are and say which language they cover.

## Scripts

- Python 3.11+. Every script starts with a docstring: what it does, inputs, outputs, usage line.
- Dependencies as PEP 723 inline metadata so `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/x.py` works with nothing installed:
  ```python
  # /// script
  # requires-python = ">=3.11"
  # dependencies = ["numpy>=2", "soundfile"]
  # ///
  ```
  Stdlib-only scripts run with plain `python` too. GPU stacks (torch, pyannote, model weights) get a documented venv in `references/setup.md` with exact `uv` commands, including the CUDA wheel index.
- Portable: `pathlib`, no drive letters or home paths, `sys.stdout.reconfigure(encoding="utf-8")` at the top, no Windows-only calls unless guarded. ffmpeg / ffprobe are found on PATH (env `FFMPEG`, `FFPROBE` override).
- Never modify source media. Outputs go to a job or project folder the user names.
- Long jobs print progress and write a log next to their outputs; reruns skip finished steps (outputs newer than inputs).

## Configuration and secrets

- Per project: a JSON file next to the work (`videos.json`, `episodes.json`, `<job>/job.json`, ...), documented in the skill. Machine-specific values (media drive, venv paths) go in a `*.local.json` beside it, which overrides it and is never committed.
- API keys come from environment variables only. Each provider also reads a base URL, so a local key proxy can inject auth and the script sends a placeholder key:

  | Provider | Key | Base URL (default) |
  |---|---|---|
  | xAI (Grok speech-to-text) | `XAI_API_KEY` | `XAI_BASE_URL` (`https://api.x.ai/v1`) |
  | ElevenLabs (Scribe) | `ELEVENLABS_API_KEY` | `ELEVENLABS_BASE_URL` (`https://api.elevenlabs.io`) |
  | OpenAI (transcription) | `OPENAI_API_KEY` | `OPENAI_BASE_URL` (`https://api.openai.com/v1`) |
  | Google Gemini (Decisions API, visual checks) | `GEMINI_API_KEY` | `GEMINI_BASE_URL` (`https://generativelanguage.googleapis.com`) |
  | Hugging Face (gated weights) | `HF_TOKEN` | |

  Base URLs follow each official SDK's own convention (OpenAI and xAI include `/v1`, ElevenLabs and Gemini don't), so the same variables work for the SDKs and for these scripts. Paths are appended to them: `$OPENAI_BASE_URL/audio/transcriptions`, `$XAI_BASE_URL/stt`, `$ELEVENLABS_BASE_URL/v1/speech-to-text`, `$GEMINI_BASE_URL/v1beta/decisions`. Strip one trailing `/`.

- Paid calls: the skill says what costs money and asks before the first paid call of a job. Results are cached next to the job and never re-requested when the cache exists.

## Shared file formats

Plugins hand files to each other. Keep these shapes; add fields, never rename them.

- **Words** (`*.words.json`), every transcript after import:
  ```json
  {"source": "<media file>", "language": "fr", "model": "grok-voice-transcribe-2.0",
   "time_base": "source", "words": [{"text": "Bonjour", "start": 1.02, "end": 1.31, "speaker": 0, "type": "word"}]}
  ```
  Seconds from the start of `source`. `type` is `word`, `filler` (« euh », "um") or `event` (laughter, cough). `speaker` optional.
- **Ears events** (`*.ears.json`) from agent-ears:
  ```json
  {"audio": "<file>", "model": "facebook/pe-a-frame-large", "prompt": "a person inhaling sharply", "thr": 0.95, "hop_s": 0.04,
   "events": [{"t0": 3.16, "t1": 3.48, "type": "breath", "conf": 0.998}], "prob": [0.01, 0.02]}
  ```
  `prob` = one value per `hop_s`, so a new threshold needs no rerun.
- **Cut list** (`*.cuts.json`) from the videos rough/tighten stages: ordered kept ranges of named sources, in source seconds:
  ```json
  {"fps": 30, "sources": {"A": "<file>"}, "segments": [{"src": "A", "in": 12.40, "out": 31.85, "why": "intro"}]}
  ```

## Before a pull request

- `python tools/leak_check.py` passes (no drive paths, home folders, emails, keys or tokens).
- `claude plugin validate plugins/<plugin>` passes.
- Each changed script ran once on real material; say what you ran in the PR.
