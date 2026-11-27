# Edit engine: files, formats, measurements

The scripts behind `/videos:ingest`, `/videos:transcribe`, `/videos:rough`, `/videos:tighten` and `/videos:master`. They cut talking-head footage from transcripts: a rough cut written as text anchors, then a tighten pass that removes breaths, fillers, dead air and approved phrase trims, places J-cuts and camera switches, and builds the timeline in DaVinci Resolve.

All paths below are relative to the video's record folder (`<project>/.agents/videos/<slug>/`, see [records](records.md)); `S` = `${CLAUDE_PLUGIN_ROOT}/scripts`. Heavy media never moves: `edit.json` points at it.

## Files

```
cut/
  edit.json            project: fps, language, sources (files, transcripts, detector outputs, angles)
  edit.local.json      machine paths overriding edit.json (deep merge, never committed)
  words/<src>.words.json   transcript per source (Words format)            import_words.py / stt_grok.py
  ears/<stem>.ears.json, <stem>.scribe.words.json   breaths + timed fillers  agent-ears plugin
  scans/<src>.scan.json, sheet_<src>.jpg            camera scan + contact sheet  angles_scan.py / angle_sheet.py
  rough.json           keep ranges as text anchors (the editor's decisions)
  rough.cuts.json/.srt rough Cut list + retimed captions                    rough.py
  phrase-trims/words.txt, spans.json, proposals.json                       words_dump.py / agent / phrase_trims.py
  overrides.json       per-word decisions for tighten (spare words, phrase spans)
  style.json           optional: the tighten style when it differs from the house style
  tight.cuts.json/.srt/.report.md   tightened Cut list, captions, report  tighten.py
checks/joins.json, checks/master.json                                     join_check.py / finals_check.py
```

| Script | Does | Cost |
|---|---|---|
| `import_words.py` | any transcript (studio JSON, Grok, Scribe, OpenAI verbose, Words, SRT) → Words | free |
| `stt_grok.py` | Grok speech-to-text with fillers kept → Words + SRT, cached | **paid** (xAI, per audio minute) |
| `sync.py` | audio offsets: transcript media → program, each angle → program; `--write` stores them | free |
| `angles_scan.py` | per source, 2 samples/s: program↔angle correlation, screen activity, green share | GPU decode, ~1 min per 10 min of 4K per angle |
| `angle_sheet.py` | contact sheet: program + angles × 6 moments | free |
| `rough.py` | text anchors → rough Cut list + SRT, prints every seam; `--check` lists anchor failures | free |
| `words_dump.py` | words file for the phrase-trim agent | free |
| `phrase_trims.py` | checks the agent's spans, prints each cut as it reads, `--apply` → overrides | free |
| `tighten.py` | the tighten pass → tight Cut list + SRT + report | ~10 s for 20 min of cut |
| `join_check.py` | level at every join (pass: none > 35 dB over floor) | light |
| `resolve_build.py` | Cut list → Resolve: import, multicam, build, take picker + read picks, review markers, inspect, read cuts; `--dry-run` plan | runs inside Resolve |
| `render_finals.py` | queue + start renders from approved timelines (stock preset) | runs inside Resolve |
| `comp_faces.py` | comp route: face position, size and yaw per camera cell of the all-angles master (YuNet) | CPU, minutes |
| `comp.py` | comp route: spans → framing plan, panel tracks, sound, HyperFrames `index.html` (16:9 / 9:16), `verify`, `gaps`; [comp](comp.md) | ffmpeg, minutes |
| `finals_check.py` | master file: frames, loudness, content vs the cut list at lag | light |
| `sync_check.py` | lag between two renders, several windows | light |

`editlib.py` holds the shared helpers (config, Words loading, anchors, SRT retiming).

## edit.json

```json
{
  "fps": 23.976,
  "language": "fr",
  "keyterms": ["Product name", "brand.com"],
  "overrides": "overrides.json",
  "style": "style.json",
  "resolve": {"shoot": "2026-03-02 Studio"},
  "sources": {
    "A": {
      "file": "$MEDIA/shoot/take1/Program.mp4",
      "words": "words/A.words.json",
      "words_media": "$MEDIA/shoot/take1/Camera Face.mp4",
      "words_offset_s": 0.144,
      "captions": "$MEDIA/shoot/take1/Camera Face.srt",
      "ears": "ears/Program.ears.json",
      "fillers": "ears/Program.scribe.words.json",
      "scan": "scans/A.scan.json",
      "face": "Face",
      "angles": {
        "Face":  {"file": "$MEDIA/shoot/take1/Camera Face.mp4",  "offset_s": 0.144},
        "Right": {"file": "$MEDIA/shoot/take1/Camera Right.mp4", "offset_s": 0.084},
        "Left":  {"file": "$MEDIA/shoot/take1/Camera Left.mp4",  "offset_s": 0.144}
      },
      "multicam": {"name": "MC A"}
    }
  }
}
```

| Field | Meaning |
|---|---|
| `fps` | timeline rate; 23.976 / 29.97 / 59.94 are read as the exact NTSC ratios |
| `language` | picks the filler and spare-word lists in the style (a Words file's own `language` wins per source) |
| `sources.<id>` | one recording the cut is made from. Id = short name used everywhere (cut lists, overrides, reports) |
| `file` | program file: the picture and sound the cut uses (a live-switched program out, or the only camera). Paths: absolute, relative to edit.json, or with `$VARS` / `~` expanded |
| `audio` | optional: a separate file for the sound analysis, on the same clock as `file` |
| `words` | Words file. Word id = index in its `words` list (import drops empty tokens) |
| `words_media`, `words_offset_s` | file the transcript was made on, and seconds to add to its times to land on `file` (sync.py measures it; + = the program hears it later) |
| `captions` | optional source SRT on the words clock; its text (hand fixes) survives retiming. None → captions built from the words |
| `ears`, `fillers` | agent-ears outputs on the `file` clock (`ears_offset_s`, `fillers_offset_s` if not) |
| `scan` | angles_scan.py output |
| `face`, `angles` | angle names → file + offset (+ = the program hears it later; angle frame = program frame − round(offset × fps)). `face` = the angle shown on face segments and first in the camera rotation (both = the multicam's active angle on V1) |
| `multicam` | Resolve multicam clip name (default `MC <id>` when angles exist; `false` = cut the program file directly); `out_offset_f` overrides the computed program position inside it |
| `audio_files` | optional extra audio (per-mic WAVs) imported beside the source |

## rough.json (anchors)

```json
{"timeline": "Studio tour",
 "segments": [["A", [27.2, "faire une vidéo de quinze minutes"], [74, "pas avoir etc"], {"face": true, "why": "hook"}],
              ["A", [77, "Donc essentiellement quand tu arrives"], [360, "on va se le dire"]],
              ["B", [57.5, "Ça me prenait trois heures"], [78.3, "j'arrive et j'enregistre"], {"alt": "short hook"}]],
 "markers": [["A", 77, "Donc essentiellement", "Blue", "1. Framing", "chapter note"]],
 "end_note": "what was cut and why"}
```

Anchor = (approx second on the words clock, phrase). Matching is on accent-stripped letters, starting on a word boundary, nearest to the approx time within ±12 s (an elided token is ONE word: « qu'on a des » matches, « on a des » inside « qu'on » does not; start the anchor at the whole token); `*` = any one word. Pads 0.12 s in / 0.25 s out, clamped to the midpoint with the neighbour word, then floor/ceil to frames. Options: `face`, `cam` (an angle name of the source, or `program`: shown for the whole segment, set by picks), `takes` (the same passage in other recordings, `[[src, [s, "phrase"], [s, "phrase"]], …]`: the segment's own range = take 1, the default; a segment with takes never merges with its neighbour), `alt` (placed after a 2 s gap at the end, Purple marker), `head` / `tail` (pads), `why`.

Take picker: `resolve_build.py --mode takes` builds `<name> - takes` from the rough cut list (one V1 clip per main segment, its takes = every camera of every take, plain camera files, all padded to the segment's longest take, take 1 = the segment's range on its `cam` / face angle); `--mode read_takes --out picks.json` reads what each clip plays (by file, in clip order) → `{"timeline", "picks": [{"seg", "specs", "src", "cam", "take", "changed", "first"}], "unknown"}`; `rough.py --picks picks.json --out …` writes them into rough.json (picked take ↔ the segment's own range, the others stay in `takes`; `cam` replaces `face`; previous file kept as `<spec>.before-picks.json`). A marker anchored in a take that is no longer played lands at the same fraction of the picked take. Marker colours used: Blue chapters · Green short candidates · Yellow claims to check · Red end of cut (+ what was cut) · Purple alternates.

## Cut lists

Shared format (CONTRIBUTING): `{"fps", "sources": {id: file}, "segments": [{"src", "in", "out", "why"}]}`. The engine adds fields:

- top level: `timeline`, `alt_gap_s`, `markers` ([{frame, color, name, note, kind?}], frames from the timeline start; `kind: "end"` = moves to the new end on tighten), `main_s`, `total_f`, `srt`.
- rough segment: `in_f`, `out_f` (source frames, exact), `rec_f` (timeline frame), `face`, `cam`, `alt`, `specs` (indexes of the rough.json segments merged into it), `takes` ([{src, in_f, out_f, first, last}], take 1 = this segment), `first` / `last` / `cut_before` / `cut_after` (the seam text).
- tight part (one per kept stretch): `in_f`/`out_f`/`rec_f` = sound on whole frames; `v_in_f`/`v_out_f`/`v_rec_f` = picture (J-cut: the next part's picture starts `J` frames after its sound, the outgoing picture runs on over removed silence); `a_in_f`/`a_out_f`/`a_rec_f` = exact sound edges in fractional frames (cuts inside the voice); `xf` = 1-frame cross fade at its end; `sync_f` = picture/sound offset; `fade_in`; `angle` = V3 camera piece {name, file, delay_f, max_f}; a part of a `cam` segment keeps that camera (no rotation, no cutaway).
- tight extras: `cuts` (every removal: timeline s, source s on the words clock, length, kinds, words around), `spare`, `phrases`, `fillers_timed` (with why kept), `flags` (voice the transcript lacks: listen), `angles` (counts), `before_main_s`.

Cut kinds: `breath` (heard by the DSP) · `breath (ears)` · `breath glued (ears)` (inhale running into a word) · `filler` (written in the transcript) · `filler (audio)` (held vowel the DSP heard) · `filler (timed)` / `(timed, tight)` / `(timed, in the voice)` (timed-fillers file) · `dead air` · `spare word` · `phrase` / `phrase (tight)`.

## overrides.json

```json
{"keep_spare": {"A": [802, 1537]},
 "drop_spare": {},
 "drop_spans": {"A": [[117, 118, "filler phrase (med)", "en fait."], [121, 126, "transition", "ce que je fais, c'est que"]]}}
```

Word ids from the report / words file. `keep_spare` = a spare word that carries meaning (a real "so"); `drop_spare` = cut one the rule keeps (still needs a clean gap); `drop_spans` = [first, last, label, removed text] from phrase_trims.py. Tighten cuts a span only where both edges split cleanly; the rest is listed as kept.

## Style (tighten.py --dump-style)

House defaults, measured on 448 jump cuts of an editor's own past project (single-file sources, 23.976) and set by review notes on the first tightened cut:

| Key | Default | Why |
|---|---|---|
| `dead_air_s` | 0.4 | pauses the editor keeps: p50 0.15 / p90 0.33 / p95 0.48 s; 0.9 was judged too slow |
| `keep_out_s` / `keep_in_s` | 0.03 / 0.02 | measured pads past the floor+10 dB crossing: out 0.03, in 0.00; ~0.12 s pause across a join |
| `j_frames` | 2 | 30 % of the editor's cuts are J (sound first) of 1–4 frames, J2 most common (69 of 136). Placed only where the held outgoing frames are silent and breath-free |
| `min_cut_s` | 0.15 | shorter removals are not worth a jump |
| `phrase_max_db` | 30 | a soft-dip edge louder than floor + 30 once frame-snapped stays (frame snapping put 22 of 94 joins 35–49 dB over floor before) |
| `spare_word_gap_s` | 0.25 | a spare word after this pause counts as sentence-initial |
| `cameras` | gap 20 s, min 2.5 s, hold 3 s, max 8 s | "switch the cameras a bit": one cutaway per 20 s from a calm screen part, back after 8 s; face parts rotate after 3 s |
| `languages.fr` | fillers euh heu hum hmm euhm; aliases ah eh; spare words donc | **French** (Québec speech). « eh », « ben » are words there. « donc » is cut only sentence-initial, not essential, and with a clean gap |
| `languages.en` | um uh er erm hmm mm uhm; no spare word | add "so" if the speaker leans on it |

Never cut: voice the transcript has no words for (`speech?`): listed under "Kept" in the report for a human to hear.

## How tighten hears (detector, not taste)

Source audio high-passed at 80 Hz (chair rumble read as voice otherwise), 16 kHz, 30 ms Hann windows every 10 ms → dB, pitch periodicity (window-corrected autocorrelation, 70–300 Hz), spectral centroid. Floor = 5th percentile; silence < floor + 10 dB. A mix gated only part of the time (a podcast where a gate or an editor muted some pauses) puts the 5th percentile in the muted stretches, 15–30 dB under the room tone of the other pauses: nothing else counts as silence and tighten cuts no pause or breath there (0 cuts on three clips of such a mix). Check the report's floor against the level of a real pause; when they differ, cut those pauses by hand or tighten the ungated per-speaker tracks.

- Between two words, the word's tail and the next onset never count. Each other sound between silences: **filler** = one held voiced vowel (loud core unbroken ≥ 0.18 s, centroid sd < 200 Hz, ≤ 1.2 s); **speech?** = voiced with syllable dips (kept); **breath** = aperiodic, centroid > 700 Hz, ≥ 80 ms, ≥ 8 dB under speech; **noise** = the rest (mouth clicks count as dead air). An energy + zero-crossing v1 took rumble for voice and word-final « s » for breath: 327 cuts on the test video, many wrong; this detector: 117.
- Ears breath spans (PE-A-Frame "a person inhaling sharply" ≥ 0.95; precision 0.98 / recall 0.93 against 116 hand labels vs 0.91 / 0.21 for the DSP alone) relabel noise as breath, add breaths under the silence threshold, cut word→breath→word with no silence between near-silent dips, and cut an inhale glued to the next onset when the stretch is unvoiced. Exhales into a word tail are never cut.
- Timed fillers: the words transcript (Grok) wrote 12 of 20 hand-labelled « euh » and times its fillers up to 0.34 s off; Scribe heard 19 of 20. Each timed filler is cut from the previous word's end to the next word's start: on a clean dip (≤ floor + 18), else a soft dip (≤ floor + 30 and ≥ 12 dB under the local peak), else inside the voice (`glide`): splice at the level valley (≥ 6 dB) or the strongest spectral change near the word boundary, sample-exact audio edges and a 1-frame "Cross Fade +3 dB" (preferred in a blind listening test). Skipped when the words transcript has a real word there or a `speech?` sits beside it.
- Cut edges snap to the quieter of the two frames around them (level centred on the boundary). A gap shorter than a frame (a 30 ms break before a false start) averages into voice at frame level, so a phrase trim there is refused; cut it by hand at the sample-level dip and run the join check on it (a hand cut at such a gap measured 32 dB, under the 35 dB mark).

## Results on the test video (regression)

A 21-min French studio walkthrough (3 sources, 20 rough segments): rough 20.81 min → tight **16.84 min, 300 cuts, 320 parts, 57 J-cuts, 46 cuts inside the voice, 33 camera cutaways + 11 face-angle changes**; 15 of 44 « donc » removed; 82 of 183 proposed phrase spans cut (both edges clean or on a soft dip). With `--no-glide`: 17.14 min, 267 cuts. Join check: 254 joins, p50 8 / 7 dB over floor, none > 35 (max 31), speech ≈ 55; the 46 in-the-voice joins sit at p50 44 by design. The port reproduces the original engine field for field (cut list, captions byte-identical; the Resolve build call for call on a recording mock).

## Resolve (Studio 21.2, verified)

Rules for every Resolve call and the log of what failed (imports, appends, subtitles, markers, renders, frames, Fusion, hangs): `/videos:resolve` and [resolve](resolve.md). Below: how this engine uses Resolve.

- `run_script_unsafe` with `timeout` 60, one mode per call. Keep logic in a file and `exec` it (`resolve_build.py --snippet` prints the call). A timed-out call keeps running inside Resolve: read the result back before rerunning a mode.
- Bins and names: one layout for every project ([resolve](resolve.md) § Organization, code in `scripts/resolve_bins.py`). Footage in `02 Footage/<resolve.shoot>/<source id>`, the build in `01 Timelines/WIP/Processing` under the video's plain name (released to `Waiting for review` once checked), its SRT in `03 Assets/Subtitles/<name>`; `replace` files the old timeline in `01 Timelines/WIP/Archive` as `<name> vNN YYYY-MM-DD <state>`. Media already in the pool is found wherever it is filed (the whole pool is indexed by file path).
- Multicam: angle order = clip NAME sort (list order ignored), so the clips carry `1 <id> program`, `2 <id> <angle>` while the multicam is created and get their file names back right after (the multicam keeps those angle names; the camera stays in each clip's `Angle` metadata). With full extents the multicam starts at the earliest angle: program frame 0 sits at −min(0, min(round(offset × fps))).
- Camera changes = the multicam clip's ACTIVE ANGLE on V1, never another camera's clip on a track above (an editor switches angles on the multicam; flat clips on V2 / V3 break that workflow). No API call sets the active angle (`PerformMulticamSmartSwitch` lets Resolve pick, ~77 s per 40 s of timeline) → `scripts/resolve_multicam_angles.py` does it through a `.drt` round trip: `Timeline.Export(path, resolve.EXPORT_DRT, resolve.EXPORT_NONE)` = zip of XML; a V1 multicam item's `<FieldsBlob>` (hex: u32 2, u32 length, 0x80 raw / 0x81 zstd, protobuf-like payload) holds `"Camera N"`; rewrite N, `MediaPool.ImportTimelineFromFile(path)` (named after the FILE, lands in the current bin, links the existing media, becomes current; ImportOptions ignored for .drt). Round trip checked equal: items, sub-frame audio edges, transitions, subtitles, markers, track names. Angle N = the Nth video track of the multicam's own sequence (`<MediaRef>` → `<Sm2MpMulticamClip>` → `<Sm2Sequence>`). `GetName()` of a multicam item = `<multicam> - <angle clip name>` = readback. `build` places face / camera pieces on V2 / V3, then `switch_angles(from_tracks=True, inplace=True)` turns them into V1 angle switches; `sync_frames` compares each old clip's frame with the angle's (0, or ±1 where Resolve's audio sync rounded an angle offset differently). zstd: stdlib on Python ≥ 3.14; older → `zstandard` installed in the Python that runs Resolve's scripts. Missing, or any other failure in the switch → the build still finishes, clips stay on V2 / V3, `angles.ok` false and `angles.error` says why (fix, or build with `--camera-tracks`).
- Switching angles on any timeline (a human's edit, an older project): from `run_script_unsafe`, `sys.path.insert(0, "<plugin>/scripts"); import resolve_multicam_angles as A; importlib.reload(A); A.switch_angles(resolve, project, "<timeline>", switches=[("01:00:12:00", "01:00:18:00", "Left")], from_tracks=False, out_name=None, inplace=False, dry=False)`. Works on a duplicate, cuts V1 where needed (re-append the same source frames), keeps the original until every check passed, refuses a camera the multicam lacks. V1 clips with linked audio or a transform are not cut (reported). A 17-min timeline with 324 V1 clips and 71 camera clips converts in ~6 s.
- Sub-frame audio needs the user preference "Align audio edits to frame boundaries" OFF; then `SetLeftOffset/SetRightOffset(x, True, False)` take fractional frames. A 1-frame audio transition lands at [join − 1, join], so both edges of an in-the-voice cut sit half a frame past the splice.
- Review markers: `AddMarker` fails on an occupied frame (and on every frame while a render runs) → the build retries ±1, ±2 and reports the count.
- Read cuts back from a timeline (for captions or graphics after a human moved a cut): A1 items `GetSourceStartFrame/EndFrame` (end exclusive) minus the multicam offset; `GetStart/GetEnd` minus the timeline start = record frames. 1-frame audio cross fades come back as items without a source (`GetSourceStartFrame()` None) and are skipped.
