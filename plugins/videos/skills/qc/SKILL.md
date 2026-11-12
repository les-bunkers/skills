---
name: qc
description: QC gate before anything ships — which checker for which file (loudness, true peak, frames vs timeline, black frames, captions in the safe area, join loudness, sync), pass marks, the secrets-on-screen scan, and what a human still has to eyeball. Use when checking a render, master, short or clip before delivery, judging loudness/captions/frames, signing a video off as ready to publish, or running /videos:qc.
argument-hint: "[video name or file]"
---

`videos.py` = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"`; `decide.py` = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/decide.py"` (visual assertions, [decide](${CLAUDE_PLUGIN_ROOT}/references/decide.md); paid API, ~$0.005 per short, `--dry-run` prints the estimate). Results go to the record's `checks/` folder.

## Which checker

| File | Check | Pass |
|---|---|---|
| any shipped mp4 | `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/loudness.py" measure <file>` | −14.0 ±0.5 LUFS, true peak ≤ −1.0 dBTP on the AAC |
| any shipped mp4 | `ffprobe -v error -show_entries stream=codec_name,width,height,r_frame_rate,pix_fmt -of json <file>` | H.264 yuv420p (or the platform's spec), AAC, expected size and fps |
| black / frozen frames | blackdetect + freezedetect (command below) | none, except intended fades |
| master vs timeline | the master stage's frame count and cut read-back (`/videos:master`) | frames = timeline; content matches at lag 0 |
| tighten joins | the tighten stage's join check (`/videos:tighten`) | no join louder than the documented limit over the room floor |
| HyperFrames project | `hyperframes check --json` | `ok: true` |
| vertical clip batch | `/vertical-clips` QC report | prints problems only |
| finished short, each format | `decide.py run short --media <file> --var hook="<hook text>" --var cta="<closing text>" --json checks/decide-<fmt>.json` | exit 0; fail or unsure → open the saved frames in `checks/decide-<fmt>_frames/` |
| any file before publish | `decide.py run secrets --media <file> --json checks/secrets.json` | exit 0 (see below) |
| a fix you just made | `decide.py ask <new> --baseline <old> --at <t> "<what should have changed>"` | the change is seen, nothing else moved |

Black and frozen frames:

```
ffmpeg -i <file> -vf "blackdetect=d=0.2:pix_th=0.1,freezedetect=n=0.003:d=1.5" -an -f null - 2>&1 | grep -E "black_start|freeze_start"
```

"Sound stops dead" at a natural pause inside a take is a false alarm; at a cut, listen.

## Secrets on screen (agent does this before any publish)

Screen recordings, switcher outputs and monitors leak credentials. Run `decide.py scan <file> --every 1 --res high "Private information is readable on screen: a password, Wi-Fi network key, email address, phone number, access code or API key." --flag ">=0.4" --json checks/secrets-scan.json` (a planted Wi-Fi card was flagged at its exact 6 s window; 24 finished shorts gave no false alarm). Also scan the transcript for « mot de passe | password | wifi | wi-fi | code | clé | key | token | courriel | email » and list every stretch where an app, settings page, browser or phone screen is visible; pull a contact sheet at 1 fps over flagged and listed stretches (`ffmpeg -ss <a> -to <b> -i <file> -vf "fps=1,scale=640:-1,tile=4x4" sheet_%02d.jpg`) and read it. Anything readable (network name + password, emails, API keys, phone numbers, client names, booking or admin screens) → blur in the NLE (tracked window + blur) or cut, then `videos.py note <slug> "secret on screen at <tc>: <what>, <fixed how>"`. One real case: a studio tour showed the Wi-Fi name and password on the switcher output for 40 s.

## Human eyeball

`decide.py run short` covers the first pass on: hook present and correct at frame 0, closing text, caption on a face, face cut by the frame edge, grade lost, secrets on screen. Its fails and unsures come with frames: show them. Still only a human: claims and prices correct (the project's claim rules); typography of the language (French spacing before « ; : ! ? », quotes); pacing; taste (which cover, which take). Show a contact sheet or review page; never call it good from checks alone.

## Record

`videos.py set <slug> qc "done <date>"` or `"blocked <what failed>"`.
