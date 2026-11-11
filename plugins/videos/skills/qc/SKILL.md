---
name: qc
description: QC gate before anything ships — which checker for which file (loudness, true peak, frames vs timeline, black frames, captions in the safe area, join loudness, sync), pass marks, the secrets-on-screen scan, and what a human still has to eyeball. Use when checking a render, master, short or clip before delivery, judging loudness/captions/frames, signing a video off as ready to publish, or running /videos:qc.
argument-hint: "[video name or file]"
---

`videos.py` = `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/videos.py"`. Results go to the record's `checks/` folder.

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

Black and frozen frames:

```
ffmpeg -i <file> -vf "blackdetect=d=0.2:pix_th=0.1,freezedetect=n=0.003:d=1.5" -an -f null - 2>&1 | grep -E "black_start|freeze_start"
```

"Sound stops dead" at a natural pause inside a take is a false alarm; at a cut, listen.

## Secrets on screen (agent does this before any publish)

Screen recordings, switcher outputs and monitors leak credentials. Scan the transcript for « mot de passe | password | wifi | wi-fi | code | clé | key | token | courriel | email » and list every stretch where an app, settings page, browser or phone screen is visible; pull a contact sheet at 1 fps over those stretches (`ffmpeg -ss <a> -to <b> -i <file> -vf "fps=1,scale=640:-1,tile=4x4" sheet_%02d.jpg`) and read it. Anything readable (network name + password, emails, API keys, phone numbers, client names, booking or admin screens) → blur in the NLE (tracked window + blur) or cut, then `videos.py note <slug> "secret on screen at <tc>: <what>, <fixed how>"`. One real case: a studio tour showed the Wi-Fi name and password on the switcher output for 40 s.

## Human eyeball (agents can't judge these)

Hook legible in the first second; no caption on a face; claims and prices correct (the project's claim rules); faces not cropped; typography of the language (French spacing before « ; : ! ? », quotes); the pacing. Show a contact sheet or review page; never call it good from checks alone.

## Record

`videos.py set <slug> qc "done <date>"` or `"blocked <what failed>"`.
