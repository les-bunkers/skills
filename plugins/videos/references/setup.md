# Setup

Tested on Windows 11 with ffmpeg 8.0.1, uv 0.12, Python 3.13, Node 24, HyperFrames 0.9.19 and DaVinci Resolve Studio 21.2. Nothing here is Windows-only. Install only what the stages you run need.

| Need | For | Install |
|---|---|---|
| Python 3.11+ | every script | stdlib scripts run with `python`; scripts with a PEP 723 header (`# /// script`, numpy and friends) run with `uv run` |
| uv | numpy scripts (tighten, join check, sync, finals check, angle scan) | <https://docs.astral.sh/uv/>; the first `uv run` builds a cached env |
| ffmpeg + ffprobe | everything that reads media | on PATH, or env `FFMPEG` / `FFPROBE`. A full build: `lut3d`, `ebur128`, `loudnorm`, `alimiter`, `scdet`, `blackdetect`, `freezedetect`; NVENC is optional. Known traps: [ffmpeg](ffmpeg.md) |
| DaVinci Resolve Studio + Blackmagic's DaVinci Resolve MCP | ingest, rough, tighten, grade, master in Resolve | Resolve Studio running; the MCP server registered in the agent; load `/videos:resolve` before the first call ([resolve](resolve.md)) |
| HyperFrames CLI + Chrome | formats, layers, variants | `npm install -g hyperframes` (env `HYPERFRAMES` may hold a full command such as `node /path/to/cli.js`); its own skills: `/hyperframes` |
| yt-dlp | online sources | update before the first download of a project (`yt-dlp -U`); old builds get HTTP 403 on the media |
| xAI key | speech-to-text (`stt_grok.py`, paid per audio minute) | env `XAI_API_KEY`; `XAI_BASE_URL` (default `https://api.x.ai/v1`) may point at a local key proxy with a placeholder key |
| Gemini key | video CI visual tier and `decide.py` (paid per token, cents per video) | env `GEMINI_API_KEY` (or `GOOGLE_API_KEY`), `GEMINI_BASE_URL` (proxy, ~100 MB request cap), cache `DECIDE_CACHE`; details in [decide](decide.md) |
| Companion plugins | breaths and timed fillers, speech cleanup and mastering, 9:16 clips | `agent-ears@les-bunkers`, `audio-cleanup@les-bunkers`, `vertical-clips@les-bunkers`; each has its own `references/setup.md` |

## Project configuration

`videos.json` at the project root (optional) and `videos.local.json` beside it for machine paths (keep that one out of git):

```json
{"records": ".agents/videos", "published": "artifacts/videos", "media_root": "/mnt/media", "workspaces": "clients/*", "skills_repo": "~/src/skills"}
```

- Project root = `$VIDEOS_ROOT`, else the nearest folder at or above the current one holding `videos.json`, else the current folder.
- `workspaces`: glob of sub-projects that each hold their own records (several clients or brands in one tree). `python videos.py init` writes a starter file; `videos.py where` prints the resolved paths.
- `media_root` (or `$VIDEOS_MEDIA_ROOT`): rushes, masters, renders and HyperFrames projects go under it, never into the project.
- `skills_repo` (or `$VIDEOS_SKILLS_REPO`): a git clone of this plugin repo, where `/videos:learn` writes the lessons that hold for every project (without it, they stay in the project's notes).
- `videos.py where` prints which config files it read.

## Checks after install

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py" --help
ffmpeg -hide_banner -filters | grep -E "lut3d|ebur128|scdet"
uv run "${CLAUDE_PLUGIN_ROOT}/scripts/finals_check.py" --help
hyperframes --version
```

Resolve: the MCP's `get_resolve_status` answers and `run_script` with `result = project.GetName()` returns the open project.

## Windows notes

- Scripts reconfigure stdout and stderr to UTF-8 themselves; `python -I` ignores `PYTHONIOENCODING`, so a one-liner that prints paths with « — » or « ？ » needs `-X utf8`.
- `python -I` hides user site-packages: a numpy one-liner runs as `uv run --no-project --with numpy python -I …`.
- A long render keeps big temp files: point `TEMP` / `TMP` at the media drive for HyperFrames batches ([layers](layers.md) § HyperFrames traps).
