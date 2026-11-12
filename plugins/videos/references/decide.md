# decide.py: assertions and judgments on a video

`decide.py` asks closed questions about a slice of a video and gets calibrated probabilities back in about a second. Agents use it as unit tests after an edit (did the change land, did anything else break), as a gate before a paid or human step (stray text in a generated frame, a password on screen), and as a judge (which take, which cover frame).

Engine: the Gemini Decisions API, model `gemini-decision-1`. One request = one shared input (video slice, still frames, audio, reference images, text) plus up to 1000 questions, answered in one forward pass with no generated text. Every answer carries a `confidence`; yes/no answers are a probability between 0.01 and 0.99.

## Setup

- Key from Google AI Studio (API keys page) in `GEMINI_API_KEY` (or `GOOGLE_API_KEY`).
- `GEMINI_BASE_URL` (default `https://generativelanguage.googleapis.com`, no version, same convention as the google-genai SDK). Point it at a local key proxy and send any placeholder key; give that proxy a request cap of ~100 MB (long spans are sent inline as base64).
- ffmpeg and ffprobe on PATH. Stdlib only: `python decide.py` or `uv run decide.py`.
- Cache: `DECIDE_CACHE` (default `~/.cache/videos-decide`) holds prepared media, answers keyed by the exact request, and `log.jsonl` (every live answer). Reruns of unchanged checks are free and instant.

## Cost and speed (measured)

$0.10 per million input tokens, output free. Video ≈ 120 tokens/s at `low`; a still ≈ 260 tokens at `low` (≤ 512 px) up to ~1100 at `high`.

| Job | Requests | Cost | Wall time |
|---|---|---|---|
| `run short` on a 48 s 9:16 short (6 checks, stills every 2–3 s) | ~45 | $0.002–0.005 | 2–10 s |
| 24 finished shorts, same pack | ~1000 | $0.08 | 141 s |
| `scan --every 1 --res high` over 48 s | 48 | $0.006 | 6 s |
| `pick` over 8 frames, or 4 video takes | 4–8 | < $0.001 | 1–3 s |
| `calibrate`, 30 labelled frames × 6 wordings | 30 | $0.003 | 3 s |

`--dry-run` prepares the media and prints the request count and a token estimate without calling the API. It is a paid API: say the estimate before the first live run of a job unless the project sets a standing cap.

## Commands

```
# ad hoc: yes/no questions are positional; statements work best
decide.py ask final.mp4 --at 0.4,1.2 --res high "The headline on screen reads «SHOOT WITHOUT A CREW»."
decide.py ask final.mp4 --from -3 --choice "What closes the video?" endcard="a logo end card" talk="the speaker talking" other=""
decide.py ask new.mp4 --baseline old.mp4 --at 28.5 "A new line of text was added on top of the speaker's face in the After image."

# a check file or a pack (scripts/decide_packs/<name>.json), pass / fail / unsure per check, failing frames saved
decide.py run short --media final/x_9x16.mp4 --var hook="SHOOT WITHOUT A CREW" --var cta="example.com" --json checks/decide-9x16.json

# rank candidates: files, or frames of one file; gates drop a candidate when P(yes) reaches --gate-max
decide.py pick master.mp4 --at 2.5,8,11,26,29,40 --res medium --score "How well does this frame work as a cover thumbnail?" unusable weak good excellent --gate "The person's eyes are closed or half-closed."
decide.py pick takes/*.mp4 --with storyboard.png:"Storyboard keyframe" --label "Generated take:" --score "How closely does the take follow the storyboard?" unrelated loose same-scene close faithful --gate "A large dark object passes in front of the camera." --gate-max 0.3

# timeline: one question set per still or window, flagged spans merged
decide.py scan screen-recording.mp4 --every 1 --res high "Private information is readable on screen: a password, Wi-Fi key, email, phone number or API key." --flag ">=0.4"

# choose the wording and the threshold on labelled examples (every wording asked in the same request)
decide.py calibrate calib.json --json checks/calib.json
```

Exit codes: 0 pass, 1 a check failed (or `scan` flagged a span), 2 only unsure answers (show the frames to a human), 3 usage, media or API error.

## Samples

A sample says what the questions look at. Times in seconds; negative = from the end.

| Key | Meaning |
|---|---|
| `at` / `frames` | still frame(s); several in one input, labelled "Frame at 12.40 s:" |
| `every` (+ `offset`, `from`, `to`) | a still every N s (first at N/2) |
| `each: true` | one request per still or window, verdict per input (use for "anywhere in the video" checks) |
| `from` / `to` | a video span (default the whole file) |
| `window` (+ `hop`) | split the span into clips, one request each |
| `audio: true` | audio of the span only |
| `res` | `low` / `medium` / `high` / `ultra_high` (stills only); stills default `high`, video `low` |
| `label`, `context` | text before / after the media (expected text, transcript, rules) |
| `with` | reference files placed first: `["ref.png:Reference sheet"]` or `[{"file", "label", "at"}]` |
| `compare: true` | input = "Before:" baseline + "After:" media at the same times (`--baseline`) |
| `media` | another file than the run's media |

Video is sampled by the API at about 1 frame per second, so anything frame-accurate (a cut frame, a 3-frame glitch) goes in as stills.

## Check files and packs

```json
{
 "min_confidence": 0.6,
 "vars": {"cta": "example.com"},
 "samples": {"opening": {"at": [0.4, 1.2], "res": "high"}, "spread": {"every": 3, "each": true, "res": "medium"}},
 "checks": [
  {"id": "hook_text", "sample": "opening", "ask": "The large headline text on screen reads «{hook}».", "expect": ">=0.6"},
  {"id": "caption_off_face", "sample": "spread",
   "ask": "A line of on-screen text is placed over the middle of the speaker's face, hiding part of it.",
   "yes": "letters sit directly on the face (eyes, nose, mouth or beard)",
   "no": "all text is below the chin, above the head, or there is no face",
   "expect": "<0.4"},
  {"id": "graded", "sample": "grade", "agg": "majority", "ask": "...", "expect": ">=0.42"},
  {"id": "layout", "sample": "spread", "ask": "Which layout is on screen?",
   "options": {"talking": "the speaker alone", "screen": "a software screen with the speaker small", "other": ""}, "expect": "talking|screen"}
 ]
}
```

- Question shorthand: `ask` (+ `yes` / `no` descriptions) = yes/no; `options` {id: description} = choice (2–255, keep an `other`); `levels` [low … high] = score (linear steps). Checks sharing a sample go in the same request.
- `expect`: yes/no → `yes`, `no`, `>=0.6`, `<0.4`; choice → `id`, `a|b`, `!id`; score → `>=2`. No `expect` = information only.
- Verdicts: a yes/no or score within `band` of its threshold (default 0.08, score 0.25) is `unsure`; a choice under `min_confidence` (default 0.6) is `unsure`. `agg` over `each` inputs: `all` (default: one failing frame fails the check), `any`, `majority`.
- `{name}` placeholders come from `vars`, `--var name=value`, `--vars name=file.json` (nested keys → `{name.key}`). A missing var stops the run.
- `extends: "<pack>"` merges a pack by check id. Packs: `short` (finished short: hook, CTA, caption off the face, face framing, grade, secrets), `secrets` (screen leaks), `generated` (AI stills and takes: stray text, wipes, hands).
- `run --json out.json` writes the report and copies the stills behind every fail or unsure answer to `out_frames/`: a fail alone tells the human nothing.

## Calibrate before you trust a threshold

Probabilities are calibrated, but each question's best threshold depends on its wording and on the footage. Do this once per new check:

1. Make labelled examples. Mutations give labels by construction: copy a good render with ffmpeg and break one thing (cut the hook, drop the grade with `eq=contrast=0.55:saturation=0.35`, move a caption over the face with `drawtext`, shift the reframe, draw a password card). Pair each with the untouched file.
2. Label from the exact stills decide extracts (`ffmpeg -ss <t> -i <file> -frames:v 1`), never from a contact sheet built with a `select` filter: one such sheet showed other frames and produced 4 wrong labels that looked like model errors.
3. Write 4–6 wordings as `variants` and run `calibrate`. All variants share one request per labelled input, so 6 wordings cost the same as one.
4. Keep the variant with the widest margin (lowest yes score minus highest no score) and AUC 1.00; set `expect` between them.
5. For `each` checks, judge at video level: the clean file's worst frame must pass, the broken file's worst frame must fail. Per-frame scores can overlap while per-video maxima separate cleanly (caption on face: clean shorts peak at 0.28, the broken one at 0.76).

What the calibrations showed:
- Wording matters more than resolution. "There is text in this image." beat two longer wordings (margin +0.66). For "caption on face" only the wording with `yes`/`no` descriptions separated (margin +0.20, the others overlapped). For the grade, "strong contrast with deep black shadows" separated (margin +0.32) where a three-way graded/flat/other choice never flipped.
- Inverted wordings ("the face is fully visible") were the weakest every time. Ask about the defect.

## What it is good at (measured on finished 9:16 talking-head shorts)

| Check | Result |
|---|---|
| Hook text present and correct, CTA text in the last second | 0.83–0.88 on correct text, 0.12–0.16 when cut or when the expected text changed |
| Caption over a face | caught a caption moved onto the face on 2 shorts; on 24 finished shorts it flagged 1 caption sitting on the chin and 2 touching the beard (borderline, for the human) |
| Face cut by the frame edge | reframe off by 250 px: 0.85 vs at most 0.42 on whole faces |
| Grade lost | 0.09–0.16 vs 0.55–0.80 graded |
| Password on screen | flagged the exact 20.0–26.0 s window (0.93–0.96), no false alarm on 24 shorts including a prompter screen full of text |
| Stray text in generated frames | 0.85–0.92 with text vs at most 0.19 without |
| Object wiping across a generated take | invented pillar 0.77 vs 0.12–0.18 on five clean takes |
| Before/after compare | "text added on the face": 0.88; "identical": 0.09 |
| Held out (thresholds frozen, a second short, 7 breaks) | 7 of 7 caught, 0 false fails |

## Limits

- Audio edits: whether a join clips a syllable is not separable (AUC 0.55–0.69 on 14 constructed joins). Keep the dB join check as the authority.
- Small details in wide shots: "hands deformed" sat at 0.43–0.50 on every wide take, good or bad. Calibrate on close shots before it blocks anything.
- Taste: ranking cover frames or takes by "how good" gives low confidence (0.3–0.5) and debatable orders. Use objective gates (eyes closed, text present, wipe) to drop candidates and leave the taste call to the human, or calibrate on 30+ human picks first.
- 1 fps video sampling: frame-level defects need stills; lip sync finer than seconds is out of reach.
- No reasons come back. Keep each question about one defect so a fail points at one fix, and look at the saved frames.
- Input is untrusted: on-screen text can argue for its own answer. Keep a human on anything that publishes, deletes or pays.
- Media leaves the machine (Google). Follow the project's rules on whose footage may be sent.
- `gemini-decision-1` is a v1beta model: the model id is part of every cached request; re-run `calibrate` when it changes.
