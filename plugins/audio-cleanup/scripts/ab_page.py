# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=2", "soundfile>=0.12", "faster-whisper>=1.1"]
# ///
"""A/B listening page for a cleanup job (cleanup.py): per dropout spot inside the cut, every step as its own clip
(original, per-speaker split + RE-USE when split, RE-USE result, master), with synced switching in the browser.

Spots = in-cut events of <job>/scan.json, deepest first, at least 2 s apart per source, capped by --max-spots.
Each spot = event +-2 s. Intermediate steps keep their real level next to the original; RE-USE and master results
are RMS-matched to the original so the ear compares content, not volume. Waveform peaks on a dB scale
(-48..-1 dBFS). Whisper (faster-whisper, default large-v3 on CPU int8, ~10 s per transcription) on original,
RE-USE and master marks the words the cleaned result lost; cached in <out>/whisper_cache.json. The model downloads
from Hugging Face on first use (~3 GB). Language: --lang, else job.json "language", else Whisper auto-detect.
Template = ab_page_template.html next to this file. ffmpeg on PATH (env FFMPEG overrides).

The page loads its clips with fetch(): serve the folder over http, never open index.html as a file:
    python -m http.server 8790 --bind 127.0.0.1 --directory <out>        -> http://127.0.0.1:8790/

usage: uv run ab_page.py JOB [--max-spots 60] [--out JOB/ab-page] [--lang fr] [--no-asr]
"""
import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.stdout.reconfigure(encoding="utf-8")
SR, NB = 48000, 200
HERE = Path(__file__).resolve().parent
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")

ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("job", type=Path)
ap.add_argument("--max-spots", type=int, default=60)
ap.add_argument("--out", type=Path, default=None)
ap.add_argument("--lang", default=None, help="speech language for Whisper (default: job.json language, else auto)")
ap.add_argument("--whisper-model", default="large-v3")
ap.add_argument("--device", default="cpu", help="cpu (default) or cuda (needs cuBLAS 12 + cuDNN 9 on PATH)")
ap.add_argument("--compute-type", default="int8")
ap.add_argument("--no-asr", action="store_true", help="skip Whisper (no text, no missing-word marks)")
args = ap.parse_args()
JOB = args.job.resolve()
OUT = (args.out or JOB / "ab-page").resolve()
WORK = JOB / "work"
cfg = {}
for name in ("job.json", "job.local.json"):
    if (JOB / name).exists():
        cfg.update(json.loads((JOB / name).read_text(encoding="utf-8")))
LANG = args.lang or cfg.get("language") or None
jobs = {j["key"]: j for j in json.loads((WORK / "jobs.json").read_text(encoding="utf-8"))}
man_doc = json.loads((JOB / "manifest.json").read_text(encoding="utf-8"))
man = {s["key"]: s for s in man_doc["sources"]}
scan = json.loads((JOB / "scan.json").read_text(encoding="utf-8"))
quality = json.loads((JOB / "split_quality.json").read_text(encoding="utf-8")) \
    if (JOB / "split_quality.json").exists() else {}
plan = json.loads((JOB / "plan.json").read_text(encoding="utf-8"))
OUT.mkdir(parents=True, exist_ok=True)
clips = OUT / "clips"
cache_f = OUT / "whisper_cache.json"
cache = json.loads(cache_f.read_text(encoding="utf-8")) if cache_f.exists() else {}

events = [dict(e, key=k) for k, v in scan.items() for e in v["events"] if e["in_cut"]]
events.sort(key=lambda e: -e["drop_db"])
spots = []
for e in events:
    if len(spots) < args.max_spots and not any(s["key"] == e["key"] and abs(s["src_s"] - e["src_s"]) < 2.0
                                               for s in spots):
        spots.append(e)
order = [t["name"] for t in plan["timelines"]]
spots.sort(key=lambda s: (order.index(s["in_cut"][0]["timeline"]) if s["in_cut"][0]["timeline"] in order else 99,
                          s["in_cut"][0]["tc"]))
print(len(spots), "spots", flush=True)

model = None


def asr(x):
    global model
    if model is None:
        from faster_whisper import WhisperModel
        model = WhisperModel(args.whisper_model, device=args.device, compute_type=args.compute_type,
                             cpu_threads=os.cpu_count() or 4)
    sf.write(str(OUT / "_t.wav"), x, SR, subtype="FLOAT")
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(OUT / "_t.wav"), "-ar", "16000",
                    "-ac", "1", str(OUT / "_t16.wav")], check=True)
    segs, _ = model.transcribe(str(OUT / "_t16.wav"), language=LANG, beam_size=5, condition_on_previous_text=False)
    return " ".join(s.text.strip() for s in segs)


def src_read(path, a, b):
    with sf.SoundFile(str(path)) as f:
        a0 = max(0, a)
        f.seek(a0)
        y = f.read(b - a0, dtype="float32", always_2d=True).mean(axis=1)
    return np.pad(y, (a0 - a, max(0, (b - a) - (a0 - a) - len(y))))


def cat_read(path, j, a, b):
    out = np.zeros(b - a, np.float32)
    with sf.SoundFile(str(path)) as f:
        for r in j["layout"]:
            lo, hi = max(a, r["src_start"]), min(b, r["src_end"])
            if hi > lo:
                f.seek(r["cat_start"] + lo - r["src_start"])
                out[lo - a:hi - a] = f.read(hi - lo, dtype="float32", always_2d=True).mean(axis=1)
    return out


def toks(t):
    return [w for w in re.split(r"[\s'’\-]+", re.sub(r"[^\w\s'’\-]", " ", t.lower())) if w]


def missing(ref, hyp):
    words = ref.split()
    flat, owner = [], []
    for i, w in enumerate(words):
        for t in toks(w):
            flat.append(t)
            owner.append(i)
    miss = set()
    for op, a0, a1, _, _ in difflib.SequenceMatcher(a=flat, b=toks(hyp), autojunk=False).get_opcodes():
        if op in ("delete", "replace"):
            miss.update(owner[a0:a1])
    return sorted(miss)


def rms(x):
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) + 1e-12


shutil.rmtree(clips, ignore_errors=True)
clips.mkdir(parents=True)
data = []
for n, s in enumerate(spots):
    j, m = jobs[s["key"]], man[s["key"]]
    a, b = int((s["src_s"] - 2.0) * SR), int((s["src_s"] + s["dur_s"] + 2.0) * SR)
    sid = f"{n:03d}-{s['key'][-6:]}"
    t = {"orig": src_read(j["src_wav"], a, b)}
    if j["split"]:
        for i in range(2):
            t[f"split.{i}"] = cat_read(WORK / f"{j['key']}.s{i}.regions.wav", j, a, b)
            t[f"spk.{i}"] = cat_read(WORK / f"{j['key']}.s{i}.regions.reuse.wav", j, a, b)
    t["reuse"] = src_read(m["outputs"]["reuse"]["wav"], a, b)
    if m["outputs"].get("master"):
        t["master"] = src_read(m["outputs"]["master"]["wav"], a, b)
    text = {}
    if not args.no_asr:
        for k in ("orig", "reuse", "master"):
            if k in t:
                ck = f"{s['key']}|{s['src_s']:.2f}|{s['dur_s']:.2f}|{k}|{args.whisper_model}|{LANG}"
                if ck not in cache:
                    cache[ck] = asr(t[k])
                    cache_f.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
                text[k] = cache[ck]
    ref = rms(t["orig"])
    for k in ("reuse", "master"):
        if k in t:
            t[k] = t[k] * np.float32(ref / rms(t[k]))
    g = min(1.0, 0.89 / max(float(np.abs(y).max()) for y in t.values()))
    tracks = {}
    for k, y in t.items():
        y = (y * g).astype(np.float32)
        edges = np.linspace(0, len(y), NB + 1).astype(int)
        pk = []
        for e0, e1 in zip(edges[:-1], edges[1:]):
            mx = float(np.abs(y[e0:e1]).max()) if e1 > e0 else 0.0
            pk.append(int(round(99 * min(1.0, max(0.0, (20 * np.log10(mx / 0.89 + 1e-9) + 48) / 48)))))
        sf.write(str(OUT / "_c.wav"), y, SR, subtype="FLOAT")
        name = f"{sid}-{k.replace('.', '_')}.mp3"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(OUT / "_c.wav"), "-c:a",
                        "libmp3lame", "-q:a", "2", str(clips / name)], check=True)
        tracks[k] = {"src": f"clips/{name}", "peaks": pk}
    where = s["in_cut"][0]
    data.append({"id": sid, "pod": where["timeline"], "timeline": where["tc"], "dur": round((b - a) / SR, 3),
                 "drop_at": 2.0, "drop_dur": s["dur_s"], "kind": f"−{s['drop_db']:.0f} dB",
                 "lanes": j.get("speakers") or None, "text": text,
                 "missing": {k: missing(text["orig"], text[k]) for k in text if k != "orig"}, "tracks": tracks})
    print(n + 1, sid, where["timeline"], where["tc"], flush=True)
for f in ("_t.wav", "_t16.wav", "_c.wav"):
    (OUT / f).unlink(missing_ok=True)

target = man_doc.get("target_lufs", -14.0)
master_label = f"Master {target:g} LUFS".replace("-", "−")
master_track = man_doc.get("master_track", "MASTER −14 LUFS")
ORIG = {"id": "orig", "label": "Original", "sub": "Source audio, no cleanup", "lanes": ["orig"], "who": "Original"}
pipes = [
    {"k": "reuse", "label": "RE-USE", "desc": "RE-USE speech enhancement; two-speaker sources are split first and "
                                              "each speaker is cleaned alone, then summed.",
     "steps": [ORIG,
               {"id": "split", "label": "Split", "sub": "One track per speaker; the other voice stays in 30 dB down",
                "lanes": ["split.0", "split.1"]},
               {"id": "spk", "label": "RE-USE per speaker", "sub": "This speaker's track alone",
                "lanes": ["spk.0", "spk.1"]},
               {"id": "final", "label": "RE-USE result", "sub": "Track “RE-USE” in Resolve", "lanes": ["reuse"],
                "who": "RE-USE"}]},
    {"k": "master", "label": master_label, "desc": f"RE-USE result, then gain + compressor + limiter so the cut "
                                                   f"reads {target:g} LUFS. Track “{master_track}” in Resolve.",
     "steps": [ORIG, {"id": "reuse", "label": "RE-USE result", "sub": "Before mastering", "lanes": ["reuse"],
                      "who": "RE-USE"},
               {"id": "final", "label": "Master", "sub": "Gain, compressor 3:1, limiter −2 dBFS", "lanes": ["master"],
                "who": "Master"}]}]
score = "; ".join(f"{jobs[k]['name']}: {v['wrong_share']:.1%} of words in the wrong speaker's track"
                  for k, v in quality.items() if "wrong_share" in v)
meta = {"title": f"Audio cleanup A/B — {JOB.name}", "pipelines": pipes, "spots": data,
        "score": f"Speaker split check (words where voice pitch and STT agree): {score}." if score else ""}
html = (HERE / "ab_page_template.html").read_text(encoding="utf-8").replace(
    "__DATA__", json.dumps(meta, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"))
(OUT / "index.html").write_text(html, encoding="utf-8")
print(f"{OUT / 'index.html'}: {len(data)} spots, clips {sum(f.stat().st_size for f in clips.glob('*.mp3')) / 1e6:.1f} MB")
