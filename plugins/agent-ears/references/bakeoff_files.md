Files of a bake-off job folder (one take). All times = seconds from the start of the take's audio file.

## bakeoff.json + bakeoff.local.json

```json
{"name": "a 10-min French take",
 "order": ["dsp", "respiro", "peaframe"],
 "weak_euh": ["peaframe"],
 "strata": {"euh_claimed": null, "speech?": null, "weak_euh_only": 15, "breath_3": 10, "breath_2": 30, "breath_1": 20}}
```
`bakeoff.local.json` overrides it and holds the machine paths: `{"audio": "<take>", "words": "<take>.words.json"}`. Relative paths resolve against the job folder. `ears_job.py init` writes both.

- `order`: detector order in union members, votes and tables (optional; default spans files, then probs files, alphabetical).
- `weak_euh`: detector families whose euh claims don't make a candidate `euh_claimed` (PE-A-Frame's euh prompt fires on breaths).
- `strata`: name → how many to label (`null` = all). A candidate's stratum: any `speech?` seed → `speech?`; an euh seed from a non-weak family → `euh_claimed`; only weak euh seeds → `weak_euh_only`; else `breath_<n>` with n = detector families flagging it (3 = three or more). Keep the key order: the holdout draw walks strata in that order.

## probs_<det>.npz (re-thresholdable detector)

`np.load(path, allow_pickle=False)`: one float32 array per variant, shape `[prompt, frame]`, plus `meta` (a JSON string):
```json
{"det": "peaframe", "model": "facebook/pe-a-frame-large", "hop_s": 0.04, "min_frames": 1,
 "prompts": [{"name": "breath_in", "text": "a person inhaling sharply", "type": "breath"}],
 "thrs": [0.3, 0.5, 0.7, 0.8, 0.9, 0.95, 0.97, 0.98, 0.99], "union_thrs": [0.3],
 "variants": ["raw", "hp80"], "highpass": {"raw": null, "hp80": 80}}
```
Prompt `type`: `breath` / `euh` are scored; `mouth`, `handling`, `laugh`, `throat` ride along in candidates; `speech` (control) is ignored. A span = frames with prob > thr, more than `min_frames` long. union.py cuts spans at each `union_thrs` value (source `<det>` or `<det>@<thr>` when several); score.py sweeps `thrs`. Write one from your own model with the same keys and both scripts pick it up.

## spans_<det>.json (fixed detector)

```json
{"name": "dsp",
 "rows": [{"t0": 12.31, "t1": 12.58, "type": "breath", "label": "breath", "conf": null, "src": "dsp", "variant": "hp80"}],
 "settings": {"dsp": {"family": "dsp"},
              "dsp audio only": {"family": "dsp_audio", "exclude_labels": ["transcript_word"], "types": ["euh"]}}}
```
Row `type`: `breath`, `euh`, `speech?` seed candidates; anything else (`noise`, `mouth`) rides along. `label`, `src` (default = file name), `variant`, `conf` optional. `settings` optional: named subsets scored as separate settings (`labels` keep, `exclude_labels` drop, `types` limit the scored types, `family` groups picks); default = one setting with every breath / euh row.

## candidates.json (union.py)

```json
{"name": "...", "detectors": ["respiro", "peaframe", "words"], "n": 359,
 "candidates": [{"id": 0, "t0": 0.15, "t1": 0.99, "free": [0.15, 0.99], "in_word": false, "ctx": "prev … next",
                 "srcs": ["peaframe", "respiro@0.5"], "types": ["breath", "mouth"],
                 "members": [{"t0": 0.15, "t1": 0.99, "type": "breath", "label": "breath", "conf": 0.998,
                              "src": "respiro@0.064", "variant": "raw", "free": [0.15, 0.99]}]}]}
```
`free` = longest part outside the word mask (≥ 50 ms) or null; `in_word` = midpoint inside a word. Ids are positions: rebuilding candidates after labelling renumbers them, so keep candidates.json once labels exist (key.json stores t0 / t1 per label to re-match by hand if needed).

## key.json (key_server.py)

```json
{"name": "...", "labels": {"34": {"labels": ["breath", "euh"], "t0": 56.36, "t1": 57.47,
                                  "at": "2026-11-10T18:15:18Z", "edge": [56.5, 57.3]}}}
```
Labels: `breath`, `euh`, `noise`, `speech`, `nothing` (exclusive). `edge` only where the labeller dragged the band.

## sample.json (key_server.py --sample)

`{"<stratum>": {"size": 125, "quota": 30, "ids": [...]}}`: the drawn ids and each stratum's full size.

## score.json / score.md (score.py)

score.md = the tables. score.json: `n_labels`, `dev` / `hold` ids, `weights` (per labelled id), `size` / `nlab` per stratum, `positives`, `picks` (per `family/type`), `rows` (every setting: `setting`, `family`, `type`, `variant`, `prompt`, `thr`, `filt`, and `dev` / `hold` / `all` = `{p, r, f1, tp, fp, fn, speech_cut, speech_touch, s_tail_fp}`, `edges`).
