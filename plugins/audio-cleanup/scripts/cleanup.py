# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=2", "soundfile>=0.12", "praat-parselmouth>=0.4.3"]
# ///
"""Speech cleanup for a DaVinci Resolve edit: RE-USE speech enhancement -> optional two-speaker split -> loudness
master (default -14 LUFS: compressor + oversampled limiter), built from exactly what the timelines play.
Source media is never modified; everything is written inside the job folder.

  1. Resolve MCP: resolve_cleanup.py ACTION="plan" -> <job>/plan.json (timelines, items, source in/out)
  2. uv run cleanup.py <job> all [--split SUBSTR ...]      (or one step at a time, same order)
       prepare  every source file used: first audio stream, 48 kHz float -> work/<key>.src.wav; used ranges (+2 s
                handles, merged within 5 s) glued with 5 s digital silence -> work/<key>.mix.regions.wav;
                work/jobs.json. Split sources = path contains a --split / job.json "split" substring ("all" = all).
       diarize  split sources: diarize.py (GPU venv) on the WHOLE source, 2 speakers, cached in work/<key>.diar.json;
                turns cut to the regions. Names from job.json "stt" (words with speaker ids) when given, else
                "speaker A" (most talk) / "speaker B".
       split    lossless gain-gated split into one track per speaker (other voice ducked -30 dB, never muted).
       quality  split sources with STT words: share of words (voice pitch AND STT agree on the speaker) that sit
                in the wrong track -> split_quality.json.
       reuse    reuse_file.py (GPU venv) on each region file (per speaker when split); skips outputs that exist at
                full length and are newer than their input.
       finish   RMS-match to the source (per speaker to its own share), common gain to <= -2 dBTP, sum speakers;
                reuse/<name> RE-USE.wav (+ per speaker) full length, silence outside the used ranges, same sample
                count and time origin as the source; mov wrapper at the clip fps for Resolve; manifest.json.
       master   per source: gain solved so its audio AS PLAYED on the timelines reads the target (dual mono on a
                stereo track), chain highpass 60 -> gain -> compressor 3:1 -> 4x oversampled limiter -2 dBFS; AAC
                256k round-trip check; per-timeline loudness -> master/<name> master <target> LUFS.wav (+ mov).
       scan     dropouts RE-USE vs original (50 ms frames, original > -30 dBFS, RE-USE >= 15 dB under) with
                timeline timecodes -> scan.json.
  3. Resolve MCP: resolve_cleanup.py ACTION="place" -> tracks "RE-USE" + "MASTER -14 LUFS" mirroring the plan.
  4. uv run ab_page.py <job> -> <job>/ab-page (listening page).

Settings: <job>/job.json, overridden by <job>/job.local.json (machine paths, never shared), then env vars, then
--home defaults. Keys (all optional): split [substr|"all"], stt [{"match", "words", "names"}], target_lufs,
reuse_python, diar_python, reuse_dir, pyannote. Env: AUDIO_CLEANUP_HOME (folder with venv-gpu/ and models/),
AUDIO_CLEANUP_REUSE_PYTHON, AUDIO_CLEANUP_DIAR_PYTHON, AUDIO_CLEANUP_REUSE_DIR, AUDIO_CLEANUP_PYANNOTE,
AUDIO_CLEANUP_GPU_LOCK, FFMPEG. File formats: references/job-files.md. Log: <job>/run.log (appended).

usage: uv run cleanup.py JOB STEP [--split SUBSTR ...] [--home DIR]     STEP = one of the steps above, or all
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
STEPS = ["prepare", "diarize", "split", "quality", "reuse", "finish", "master", "scan"]
SR = 48000
FRAME = SR // 100
HANDLE_S, GAP_S = 2.0, 5.0
REUSE_TP_DB = -2.0                      # RE-USE output: scalar gain only, capped here
TP_MAX_DB, LIMIT_DB = -1.5, -2.0
MAX_EXTRA_DB = 6.0                      # solved master gain may exceed plain gain-to-target by at most this
DUCK_DB, MIN_TURN_S, XF_S, SEARCH_S = -30.0, 0.12, 0.020, 0.050
PCT = re.compile(r"[^A-Za-z0-9]+")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
REPO_ID = "pyannote/speaker-diarization-community-1"

ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("job", type=Path, help="job folder (holds plan.json)")
ap.add_argument("step", help=" | ".join(STEPS) + " | all")
ap.add_argument("--split", nargs="*", default=None, help="path substrings of two-speaker sources ('all' = every)")
ap.add_argument("--home", type=Path, default=None, help="folder with venv-gpu/ and models/ (env AUDIO_CLEANUP_HOME)")
args = ap.parse_args()
JOB = args.job.resolve()
WORK = JOB / "work"
TMP = WORK / "tmp"


def load_settings():
    cfg = {}
    for name in ("job.json", "job.local.json"):
        p = JOB / name
        if p.exists():
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
    return cfg


CFG = load_settings()
TARGET_I = float(CFG.get("target_lufs", -14.0))
# threshold sits on the loudest ~10 % of syllables AT the target level (-10 dBFS at -14 LUFS). A -20 dBFS threshold
# crushed all speech and the solver ran the gain to +51 dB, lifting pauses ~50 dB; MAX_EXTRA_DB stops that.
COMP = (f"acompressor=threshold={10 ** ((TARGET_I + 4.0) / 20):.4f}:ratio=3:knee=4:attack=5:release=120:"
        "detection=peak")
MASTER_TRACK = f"MASTER {TARGET_I:g} LUFS".replace("-", "−")


def jpath(v):
    """Path from a settings value: relative = relative to the job folder."""
    p = Path(v).expanduser()
    return p if p.is_absolute() else (JOB / p)


def venv_python(root):
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def tool(key, env, default=None):
    v = CFG.get(key) or os.environ.get(env)
    return jpath(v) if v else default


def home():
    h = args.home or CFG.get("home") or os.environ.get("AUDIO_CLEANUP_HOME")
    return Path(h).expanduser() if h else None


def reuse_python():
    h = home()
    p = tool("reuse_python", "AUDIO_CLEANUP_REUSE_PYTHON", venv_python(h / "venv-gpu") if h else None)
    if not p or not p.exists():
        raise SystemExit(f"GPU venv python not found ({p}): set AUDIO_CLEANUP_REUSE_PYTHON or --home "
                         "(references/setup.md)")
    return p


def diar_python():
    p = tool("diar_python", "AUDIO_CLEANUP_DIAR_PYTHON")
    return p if p else reuse_python()


def reuse_dir():
    h = home()
    p = tool("reuse_dir", "AUDIO_CLEANUP_REUSE_DIR", h / "models" / "RE-USE" if h else None)
    if not p or not (p / "models").is_dir():
        raise SystemExit(f"RE-USE download not found ({p}): set AUDIO_CLEANUP_REUSE_DIR or --home "
                         "(hf download nvidia/NVIDIA-NemotronLabs-RE-USE, references/setup.md)")
    return p


def pyannote_model():
    v = CFG.get("pyannote") or os.environ.get("AUDIO_CLEANUP_PYANNOTE")
    if v:
        return str(jpath(v)) if (jpath(v)).is_dir() else v
    h = home()
    d = h / "models" / "speaker-diarization-community-1" if h else None
    return str(d) if d and d.is_dir() else REPO_ID


class Tee:
    encoding = "utf-8"

    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8")
        self.out = sys.stdout

    def isatty(self):
        return False

    def write(self, s):
        self.out.write(s)
        self.f.write(s)

    def flush(self):
        self.out.flush()
        self.f.flush()


# ---------------------------------------------------------------- helpers
def load_plan():
    return json.loads((JOB / "plan.json").read_text(encoding="utf-8"))


def load_jobs():
    return json.loads((WORK / "jobs.json").read_text(encoding="utf-8"))


def save_jobs(jobs):
    (WORK / "jobs.json").write_text(json.dumps(jobs, indent=1, ensure_ascii=False), encoding="utf-8")


def fresh(out, inputs):
    """out exists and is at least as new as every input."""
    out = Path(out)
    return out.exists() and all(out.stat().st_mtime >= Path(i).stat().st_mtime for i in inputs)


def rms(x):
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) + 1e-12


def fps_expr(f):
    for num, den in ((24000, 1001), (30000, 1001), (48000, 1001), (60000, 1001), (120000, 1001)):
        if abs(f - num / den) < 1e-6:
            return f"{num}/{den}"
    return f"{f:g}"


def ffmpeg(*a):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *a], check=True)


def run_logged(cmd):
    """Run a helper in another venv, stream its output into this log."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    p = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", env=env)
    for line in p.stdout:
        print("  | " + line.rstrip(), flush=True)
    if p.wait():
        raise SystemExit(f"{Path(str(cmd[1])).name} failed with exit code {p.returncode}")


def loudnorm_json(path):
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path), "-af", "loudnorm=print_format=json", "-f", "null",
                        "-"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    return json.JSONDecoder().raw_decode(r.stderr[r.stderr.rfind("{"):])[0]


def loudness(x, dual=True):
    """(integrated LUFS, true peak dBTP, LRA). dual = as Resolve plays a mono clip as dual mono on a stereo track."""
    TMP.mkdir(parents=True, exist_ok=True)
    p = TMP / "measure.wav"
    sf.write(str(p), np.stack([x, x], 1) if dual else x, SR, subtype="FLOAT")
    m = loudnorm_json(p)
    return float(m["input_i"]), float(m["input_tp"]), float(m["input_lra"])


def ffmpeg_filter(x, filt):
    TMP.mkdir(parents=True, exist_ok=True)
    a, b = TMP / "in.wav", TMP / "out.wav"
    sf.write(str(a), x, SR, subtype="FLOAT")
    ffmpeg("-i", str(a), "-af", filt, "-ar", str(SR), "-c:a", "pcm_f32le", str(b))
    y, _ = sf.read(str(b), dtype="float32")
    if len(y) != len(x):  # resampler edge: pad/trim the tail only, never the head (time origin)
        y = np.pad(y, (0, max(0, len(x) - len(y))))[:len(x)]
    return y


def lag_samples(a, b, center, width=SR * 4, search=SR // 10):
    """lag of b vs a around `center` (positive = b late), FFT cross-correlation on a 4 s window."""
    a = a[center:center + width].astype(np.float64)
    b = b[max(0, center - search):center + width + search].astype(np.float64)
    if len(a) < SR or len(b) < len(a) + 2 * search:
        return None
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    c = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)[:2 * search + 1]
    return int(np.argmax(c)) - search


def region_len(r):
    return r["src_end"] - r["src_start"]


def used_mask(j):
    m = np.zeros(j["cat_samples"], bool)
    for r in j["layout"]:
        m[r["cat_start"]:r["cat_start"] + region_len(r)] = True
    return m


def cat_index(j, s):
    for r in j["layout"]:
        if r["src_start"] <= s < r["src_end"]:
            return r["cat_start"] + s - r["src_start"]
    return None


def write_scattered(j, y_cat, path, block=SR * 60):
    """Full-length mono float WAV: regions back at their source positions, digital silence elsewhere (streamed)."""
    zeros = np.zeros(block, np.float32)

    def silence(f, n):
        while n > 0:
            k = min(n, block)
            f.write(zeros[:k])
            n -= k

    with sf.SoundFile(str(path), "w", SR, 1, subtype="FLOAT") as f:
        pos = 0
        for r in j["layout"]:
            silence(f, r["src_start"] - pos)
            f.write(y_cat[r["cat_start"]:r["cat_start"] + region_len(r)])
            pos = r["src_end"]
        silence(f, j["src_samples"] - pos)


def wrapper(wav, fps):
    """Resolve media: mov with a 64x36 black video at the clip fps (Resolve reads WAV frame numbers at the project
    rate and refuses SetClipProperty("FPS")) + dual-mono 24-bit PCM (a mono clip on a stereo track plays -3 dB)."""
    out = wav.with_suffix(f".{fps:.3f}fps.mov".replace(".000fps", "fps"))
    n = sf.info(str(wav)).frames
    ffmpeg("-f", "lavfi", "-i", f"color=black:s=64x36:r={fps_expr(fps)}", "-i", str(wav), "-map", "0:v", "-map",
           "1:a", "-t", f"{n / SR:.6f}", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage", "-crf",
           "51", "-pix_fmt", "yuv420p", "-af", "pan=stereo|c0=c0|c1=c0", "-c:a", "pcm_s24le", str(out))
    return out


def played_spans(j):
    """Concat-sample spans of this source in timeline order (every timeline, every item), as Resolve plays them."""
    out = []
    for t in load_plan()["timelines"]:
        for x in sorted(t["items"], key=lambda x: x["rec_f"]):
            if os.path.normcase(x["path"]) != os.path.normcase(j["path"]):
                continue
            a, b = int(round(x["src_in_s"] * SR)), int(round(x["src_out_s"] * SR))
            c = cat_index(j, a)
            if c is not None:
                out.append((c, c + min(b - a, j["cat_samples"] - c)))
    return out


def stt_entries():
    rows = CFG.get("stt")
    if rows is None and (JOB / "stt.json").exists():
        rows = json.loads((JOB / "stt.json").read_text(encoding="utf-8"))
    return [dict(r, words=str(jpath(r.get("words") or r["stt"]))) for r in (rows or [])]


# ---------------------------------------------------------------- steps
def prepare():
    WORK.mkdir(parents=True, exist_ok=True)
    plan = load_plan()
    old = {j["key"]: j for j in load_jobs()} if (WORK / "jobs.json").exists() else {}
    split_subs = args.split if args.split is not None else CFG.get("split", [])
    if isinstance(split_subs, str):
        split_subs = [split_subs]
    stt = stt_entries()
    srcs = {}
    for t in plan["timelines"]:
        for x in t["items"]:
            s = srcs.setdefault(os.path.normcase(x["path"]), {"path": x["path"], "clip_fps": x["clip_fps"], "spans": []})
            s["spans"].append((x["src_in_s"], x["src_out_s"]))
    stems = [Path(s["path"]).stem.strip()[:90] for s in srcs.values()]
    jobs = []
    for s in srcs.values():
        stem = Path(s["path"]).stem.strip()
        h6 = hashlib.sha1(s["path"].encode()).hexdigest()[:6]
        key = PCT.sub("-", stem).strip("-")[:40] + "-" + h6
        name = stem[:90] if stems.count(stem[:90]) == 1 else f"{stem[:83]} {h6}"  # unique output names
        if not Path(s["path"]).exists():
            raise SystemExit(f"source missing: {s['path']}")
        src_wav = WORK / f"{key}.src.wav"
        if not fresh(src_wav, [s["path"]]):
            print("extract", stem, flush=True)
            # first audio stream only; sample 0 = audio stream time 0 (no -ss/-t)
            ffmpeg("-i", s["path"], "-map", "0:a:0", "-af", "aresample=48000:resampler=soxr", "-c:a", "pcm_f32le",
                   str(src_wav))
        info = sf.info(str(src_wav))
        n = info.frames
        spans = sorted((max(0, int((a - HANDLE_S) * SR)), min(n, int(np.ceil((b + HANDLE_S) * SR))))
                       for a, b in s["spans"])
        regions = []
        for a, b in spans:
            if regions and a <= regions[-1][1] + GAP_S * SR:
                regions[-1][1] = max(regions[-1][1], b)
            else:
                regions.append([a, b])
        gap = np.zeros(int(GAP_S * SR), np.float32)
        layout, pos = [], 0
        for a, b in regions:
            layout.append({"src_start": a, "src_end": b, "cat_start": pos})
            pos += b - a + len(gap)
        mix = WORK / f"{key}.mix.regions.wav"
        if key in old and old[key]["layout"] == layout and fresh(mix, [src_wav]):
            print(f"{key}: regions unchanged", flush=True)
        else:
            with sf.SoundFile(str(src_wav)) as fi, sf.SoundFile(str(mix), "w", SR, 1, subtype="FLOAT") as fo:
                for r in layout:
                    fi.seek(r["src_start"])
                    fo.write(fi.read(region_len(r), dtype="float32", always_2d=True).mean(axis=1))
                    fo.write(gap)
        split = any(sub == "all" or sub in s["path"] for sub in split_subs)
        st = next((e for e in stt if e["match"] in s["path"]), None)
        jobs.append({"key": key, "path": s["path"], "name": name, "clip_fps": s["clip_fps"],
                     "src_wav": str(src_wav), "src_samples": n, "src_channels": info.channels, "layout": layout,
                     "cat_samples": pos, "mix": str(mix), "split": split,
                     "stt": st["words"] if st else None, "stt_names": st.get("names", {}) if st else {}})
        print(f"{key}: {len(regions)} regions, {pos / SR / 60:.1f} min to process"
              f"{' (two-speaker split)' if split else ''}", flush=True)
    save_jobs(jobs)


def to_concat(j, turns):
    out = []
    for a, b, lab in turns:
        for r in j["layout"]:
            lo, hi = max(a * SR, r["src_start"]), min(b * SR, r["src_end"])
            if hi > lo:
                off = r["cat_start"] - r["src_start"]
                out.append([round((lo + off) / SR, 3), round((hi + off) / SR, 3), lab])
    return sorted(out)


def stt_words(j):
    return [(w["start"], w["end"], str(w["speaker"]))
            for w in json.loads(Path(j["stt"]).read_text(encoding="utf-8"))["words"]
            if w.get("speaker") is not None and w.get("type", "word") != "event"]


def diarize():
    jobs = load_jobs()
    if not any(j["split"] for j in jobs):
        return print("no split sources", flush=True)
    model = pyannote_model()
    for j in jobs:
        if not j["split"]:
            continue
        raw = WORK / f"{j['key']}.diar.json"
        if fresh(raw, [j["src_wav"]]) and json.loads(raw.read_text(encoding="utf-8")).get("num_speakers") == 2:
            print(f"{j['key']}: diarization cached", flush=True)
        else:
            run_logged([diar_python(), HERE / "diarize.py", j["src_wav"], "--out", raw, "--model", model,
                        "--speakers", "2"])
        d = json.loads(raw.read_text(encoding="utf-8"))
        excl, full = d["exclusive"], d["full"]
        excl_c, full_c = to_concat(j, excl), to_concat(j, full)
        talk = {lab: 0.0 for _, _, lab in excl}
        for a, b, lab in excl_c:
            talk[lab] += b - a
        order = sorted(talk, key=lambda lb: -talk[lb])
        if len(order) != 2:
            raise SystemExit(f"{j['key']}: diarization found {len(order)} speaker(s) in the source; the split needs "
                             "two (drop --split for this source)")
        names = {lab: f"speaker {'AB'[i]}" for i, lab in enumerate(order)}
        agree = None
        if j.get("stt"):
            words = stt_words(j)
            ov = {}
            for a, b, lab in excl:
                for wa, wb, s in words:
                    o = min(b, wb) - max(a, wa)
                    if o > 0:
                        ov[(lab, s)] = ov.get((lab, s), 0.0) + o
            best = {lab: max({s for _, s in ov} or {"?"}, key=lambda s: ov.get((lab, s), 0.0)) for lab in order}
            if len(set(best.values())) == len(order):
                names = {lab: j["stt_names"].get(best[lab], f"speaker {best[lab]}") for lab in order}
                tot = sum(ov.values())
                agree = round(sum(ov.get((lab, best[lab]), 0.0) for lab in order) / tot, 4) if tot else None
        ovl = 0.0
        for i, (a, b, la) in enumerate(full_c):
            for a2, b2, lb in full_c[i + 1:]:
                if a2 >= b:
                    break
                if lb != la:
                    ovl += min(b, b2) - a2
        j.update(diar_excl=excl_c, diar_full=full_c, label_names=names, speakers=[names[lb] for lb in order],
                 stt_agreement=agree, overlap_speech_s=round(ovl, 1), diar_seconds=d.get("seconds"))
        print(f"{j['key']}: speakers {j['speakers']} talk {[round(talk[lb]) for lb in order]} s, overlap {ovl:.1f} s,"
              f" STT agreement {agree}", flush=True)
    save_jobs(jobs)


def frame_energy(x, step=6000):
    nf = len(x) // FRAME
    out = np.empty(nf)
    for i in range(0, nf, step):
        blk = x[i * FRAME:min(nf, i + step) * FRAME].astype(np.float64).reshape(-1, FRAME)
        out[i:i + len(blk)] = np.mean(blk * blk, axis=1)
    return out


def owners_for_region(turns, r0, r1, energy, nf):
    """[(start_sample, speaker index)] covering [r0, r1); cuts at the quietest 10 ms frame between turns."""
    ts = [(max(a, r0), min(b, r1), s) for a, b, s in turns if b > r0 and a < r1]
    merged = []
    for t in (t for t in ts if t[1] - t[0] >= MIN_TURN_S * SR):
        if merged and merged[-1][2] == t[2]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], t[1]), t[2])
        else:
            merged.append(t)
    if not merged:
        return None, merged
    owner = [(r0, merged[0][2])]
    for (a0, b0, _), (a1, b1, s1) in zip(merged, merged[1:]):
        lo, hi = (b0, a1) if a1 > b0 else (max(a0, a1 - int(SEARCH_S * SR)), min(b1, b0 + int(SEARCH_S * SR)))
        f0, f1 = lo // FRAME, min(nf, max(lo // FRAME + 1, hi // FRAME))
        k = f0 + int(np.argmin(energy[f0:f1])) if f1 > f0 else lo // FRAME
        owner.append((min(max(k * FRAME + FRAME // 2, owner[-1][0] + 1), r1 - 1), s1))
    return owner, merged


def split():
    jobs = load_jobs()
    for j in jobs:
        if not j["split"]:
            continue
        outs = [WORK / f"{j['key']}.s{i}.regions.wav" for i in range(2)]
        side = WORK / f"{j['key']}.split.json"
        sig = hashlib.sha1(json.dumps([j["diar_excl"], j["label_names"], j["speakers"], j["layout"], DUCK_DB,
                                       MIN_TURN_S, XF_S, SEARCH_S]).encode()).hexdigest()
        if side.exists() and all(fresh(o, [j["mix"]]) for o in outs):
            prev = json.loads(side.read_text(encoding="utf-8"))
            if prev.get("sig") == sig:
                j.update({k: prev[k] for k in ("turns", "split_cuts", "split_sum_error")})
                print(f"{j['key']}: split unchanged", flush=True)
                continue
        x = sf.read(j["mix"], dtype="float32")[0]
        n, nf = len(x), len(x) // FRAME
        energy = frame_energy(x)
        idx = {name: i for i, name in enumerate(j["speakers"])}
        turns = sorted((int(round(a * SR)), int(round(b * SR)), idx[j["label_names"][lab]])
                       for a, b, lab in j["diar_excl"])
        g = np.zeros(n, np.float64)  # gain of speaker 0; speaker 1 = 1 - g
        val, kept, cuts = (1.0, 0.0), [], 0
        for r in j["layout"]:
            r0 = r["cat_start"]
            r1 = r0 + region_len(r)
            owner, merged = owners_for_region(turns, r0, r1, energy, nf)
            kept += merged
            if owner is None:  # no speech found: nearest turn's speaker owns the region
                owner = [(r0, min(turns, key=lambda t: min(abs(t[0] - r0), abs(t[1] - r1)))[2])]
            edges = [o[0] for o in owner] + [r1]
            for (_, s), a, b in zip(owner, edges[:-1], edges[1:]):
                g[a:b] = val[s]
            for i in range(1, len(owner)):
                c = owner[i][0]
                h = int(min(XF_S * SR, edges[i] - edges[i - 1], edges[i + 1] - edges[i])) // 2
                if h >= 1:
                    tt = (np.arange(2 * h) + 0.5) / (2 * h)
                    g[c - h:c + h] = val[owner[i - 1][1]] + (val[owner[i][1]] - val[owner[i - 1][1]]) * (
                        0.5 - 0.5 * np.cos(np.pi * tt))
                cuts += 1
        e = 10 ** (DUCK_DB / 20)
        gd = e + (1 - 2 * e) * g  # duck, never mute: silence before a turn makes RE-USE overshoot the onset
        xd = x.astype(np.float64)
        for o, gi in zip(outs, (gd, 1.0 - gd)):
            sf.write(str(o), (xd * gi).astype(np.float32), SR, subtype="FLOAT")
        err = float(np.max(np.abs(xd * gd + xd * (1 - gd) - xd)))
        res = {"turns": [[a / SR, b / SR, j["speakers"][s]] for a, b, s in kept], "split_cuts": cuts,
               "split_sum_error": err}
        j.update(res)
        side.write_text(json.dumps(dict(res, sig=sig)), encoding="utf-8")
        print(f"{j['key']}: {cuts} cuts, {len(kept)} turns, max |sum - mix| {err:.1e}", flush=True)
    save_jobs(jobs)


def quality(min_gap_hz=40.0):
    jobs = [j for j in load_jobs() if j["split"]]
    if not jobs:
        return
    import parselmouth

    rep = {}
    for j in jobs:
        if not j.get("stt"):
            rep[j["key"]] = {"skipped": "no STT words for this source: judge by ear on the A/B page"}
            continue
        mix = sf.read(j["mix"], dtype="float32")[0]
        nf = len(mix) // FRAME
        f0 = np.zeros(nf)
        for r in j["layout"]:
            a = r["cat_start"]
            p = parselmouth.Sound(mix[a:a + region_len(r)].astype(np.float64), sampling_frequency=SR).resample(
                16000).to_pitch_ac(time_step=0.01, pitch_floor=60.0, pitch_ceiling=400.0)
            k = ((a / SR + p.xs()) * 100).astype(int)
            ok = (k >= 0) & (k < nf)
            f0[k[ok]] = p.selected_array["frequency"][ok]
        f0[10 * np.log10(frame_energy(mix)[:nf] + 1e-20) < -45] = 0
        del mix
        inv = {v: k for k, v in j["label_names"].items()}
        words = []
        for a, b, s in stt_words(j):
            name = j["stt_names"].get(s, f"speaker {s}")
            for cw in to_concat(j, [(a, b, name)]):
                k0, k1 = int(cw[0] * 100), int(np.ceil(cw[1] * 100))
                v = f0[k0:k1][f0[k0:k1] > 0]
                if len(v) >= 8:
                    words.append((k0, k1, name, float(np.median(v))))
        spk = [s for s in j["speakers"] if s in inv]
        med = {s: float(np.median([m for _, _, w, m in words if w == s] or [0])) for s in spk}
        if len(spk) < 2 or abs(med[spk[0]] - med[spk[1]]) < min_gap_hz:
            rep[j["key"]] = {"skipped": f"speaker pitch medians {med} too close (or names do not match the STT "
                                        "speakers): judge by ear"}
            continue
        lo, hi = sorted(spk, key=lambda s: med[s])
        gap = med[hi] - med[lo]
        cut_lo, cut_hi = med[lo] + 0.3 * gap, med[hi] - 0.3 * gap
        agreed = [(k0, k1, s) for k0, k1, s, m in words if s == (lo if m < cut_lo else hi if m > cut_hi else None)]
        lab = np.zeros(nf, np.int8)
        for a, b, s in j["turns"]:
            lab[int(a * 100):int(np.ceil(b * 100))] = j["speakers"].index(s) + 1
        e = n = 0
        for k0, k1, s in agreed:
            seg = lab[k0:k1]
            seg = seg[seg > 0]
            if len(seg):
                n += 1
                e += int((1 if np.mean(seg == 1) >= 0.5 else 2) != j["speakers"].index(s) + 1)
        rep[j["key"]] = {"pitch_median_hz": {s: round(v) for s, v in med.items()}, "agreed_words": n,
                         "wrong_words": e, "wrong_share": round(e / max(1, n), 4)}
        print(f"{j['key']}: {e / max(1, n):.1%} of {n} agreed words in the wrong track", flush=True)
    (JOB / "split_quality.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")


def reuse_inputs(j):
    return [WORK / f"{j['key']}.s{i}.regions.wav" for i in range(2)] if j["split"] else [Path(j["mix"])]


def reuse():
    todo = []
    for j in load_jobs():
        for inp in reuse_inputs(j):
            out = inp.with_suffix(".reuse.wav")
            if fresh(out, [inp]) and sf.info(str(out)).frames == sf.info(str(inp)).frames:
                print("skip", out.name, flush=True)
            else:
                todo.append((inp, out))
    if not todo:
        return
    py, rd = reuse_python(), reuse_dir()
    for inp, out in todo:
        t = time.time()
        print(f"RE-USE {inp.name} ({sf.info(str(inp)).duration / 60:.1f} min)", flush=True)
        run_logged([py, HERE / "reuse_file.py", inp, "--out", out, "--reuse-dir", rd])
        print(f"  done in {(time.time() - t) / 60:.1f} min", flush=True)


def finish():
    jobs = load_jobs()
    (JOB / "reuse").mkdir(parents=True, exist_ok=True)
    man = {"made_by": "audio-cleanup cleanup.py", "target_lufs": TARGET_I, "master_track": MASTER_TRACK,
           "sources": []}
    for j in jobs:
        mix = sf.read(j["mix"], dtype="float32")[0]
        used = used_mask(j)
        ins = reuse_inputs(j)
        if j["split"]:
            parts = []
            for inp in ins:
                share = sf.read(str(inp), dtype="float32")[0]
                y = sf.read(str(inp.with_suffix(".reuse.wav")), dtype="float32")[0]
                parts.append(y * np.float32(rms(share[used]) / rms(y[used])))
            total = parts[0] + parts[1]
        else:
            y = sf.read(str(ins[0].with_suffix(".reuse.wav")), dtype="float32")[0]
            parts, total = [], y * np.float32(rms(mix[used]) / rms(y[used]))
        assert len(total) == len(mix), j["key"]
        tp = max(loudness(v, dual=False)[1] for v in parts + [total])
        k = np.float32(10 ** (min(0.0, REUSE_TP_DB - tp) / 20))
        total *= k
        parts = [p * k for p in parts]
        sf.write(str(WORK / f"{j['key']}.reuse.final.regions.wav"), total, SR, subtype="FLOAT")
        idx = np.flatnonzero(used)
        probes = [int(idx[int(len(idx) * f)]) for f in (0.2, 0.5, 0.8)]
        lags = [lag_samples(mix, total, max(SR // 10, p)) for p in probes]
        outs = {}
        wav = JOB / "reuse" / f"{j['name']} RE-USE.wav"
        write_scattered(j, total, wav)
        outs["reuse"] = {"wav": str(wav), "resolve_media": str(wrapper(wav, j["clip_fps"]))}
        outs["speakers"] = {}
        for name, p in zip(j.get("speakers", []), parts):
            w = JOB / "reuse" / f"{j['name']} RE-USE {name}.wav"
            write_scattered(j, p, w)
            outs["speakers"][name] = {"wav": str(w), "resolve_media": str(wrapper(w, j["clip_fps"]))}
        man["sources"].append({"key": j["key"], "path": j["path"], "name": j["name"], "clip_fps": j["clip_fps"],
                               "samples": j["src_samples"], "split": j["split"], "speakers": j.get("speakers", []),
                               "stt_agreement": j.get("stt_agreement"), "overlap_speech_s": j.get("overlap_speech_s"),
                               "reuse_gain_tp_cap_db": round(float(20 * np.log10(k)), 2), "lag_samples": lags,
                               "outputs": outs})
        print(f"{j['key']}: RE-USE done, TP cap {20 * np.log10(k):+.2f} dB, lags {lags}", flush=True)
        del mix, total, parts
    (JOB / "manifest.json").write_text(json.dumps(man, indent=1, ensure_ascii=False), encoding="utf-8")


def chain(g_db):
    return (f"highpass=f=60,volume={g_db:.4f}dB,{COMP},aresample=192000,"
            f"alimiter=limit={10 ** (LIMIT_DB / 20):.5f}:attack=2:release=60:level=false:latency=true,"
            f"aresample=48000:resampler=soxr")


def master():
    jobs = load_jobs()
    mpath = JOB / "manifest.json"
    man = json.loads(mpath.read_text(encoding="utf-8"))
    by_key = {s["key"]: s for s in man["sources"]}
    (JOB / "master").mkdir(parents=True, exist_ok=True)
    for j in jobs:
        x = sf.read(str(WORK / f"{j['key']}.reuse.final.regions.wav"), dtype="float32")[0]
        spans = played_spans(j)

        def played(y):
            return np.concatenate([y[a:b] for a, b in spans])

        i_src, tp_src, lra_src = loudness(played(x))
        hist = []

        def f(g):
            i = loudness(played(ffmpeg_filter(x, chain(g))))[0]
            hist.append((round(g, 3), i))
            return i

        g_plain = TARGET_I - i_src
        g0, i0 = g_plain, f(g_plain)
        g1 = g0 + (TARGET_I - i0)
        i1 = f(g1)
        for _ in range(6):
            if abs(i1 - TARGET_I) < 0.05 or i1 == i0:
                break
            g0, i0, g1 = g1, i1, g1 + (TARGET_I - i1) * (g1 - g0) / (i1 - i0)
            if g1 > g_plain + MAX_EXTRA_DB:
                raise SystemExit(f"{j['key']}: master gain {g1:.1f} dB runs > {MAX_EXTRA_DB} dB past plain gain "
                                 f"{g_plain:.1f} dB -> compressor too hard for this material, review COMP")
            i1 = f(g1)
        y = ffmpeg_filter(x, chain(g1))
        i_fin, tp_fin, lra_fin = loudness(played(y))
        TMP.mkdir(parents=True, exist_ok=True)
        pw, aac = TMP / "played.wav", TMP / "played.m4a"
        sf.write(str(pw), np.stack([played(y)] * 2, 1), SR, subtype="FLOAT")
        ffmpeg("-i", str(pw), "-c:a", "aac", "-b:a", "256k", str(aac))
        m = loudnorm_json(aac)
        sf.write(str(WORK / f"{j['key']}.master.regions.wav"), y, SR, subtype="FLOAT")
        wav = JOB / "master" / f"{j['name']} master {TARGET_I:g} LUFS.wav"
        write_scattered(j, y, wav)
        del x, y
        by_key[j["key"]]["outputs"]["master"] = {"wav": str(wav), "resolve_media": str(wrapper(wav, j["clip_fps"]))}
        by_key[j["key"]]["master"] = {
            "gain_db": round(g1, 2), "plain_gain_db": round(g_plain, 2), "chain": chain(g1), "solve": hist,
            "played_before": {"I": i_src, "TP": tp_src, "LRA": lra_src},
            "played_after": {"I": i_fin, "TP": tp_fin, "LRA": lra_fin, "tp_ok": tp_fin <= TP_MAX_DB},
            "aac_256k": {"I": float(m["input_i"]), "TP": float(m["input_tp"])}}
        print(f"{j['key']}: as played {i_src:.1f} -> {i_fin:.2f} LUFS, TP {tp_src:.1f} -> {tp_fin:.2f} dBTP "
              f"(AAC {float(m['input_tp']):.2f}), LRA {lra_src:.1f} -> {lra_fin:.1f}, gain {g1:+.1f} dB", flush=True)
    # every timeline as it will play from the masters (several sources -> one reading)
    jk = {os.path.normcase(j["path"]): j for j in jobs}
    man["timelines"] = {}
    for t in load_plan()["timelines"]:
        if not t["items"]:
            continue
        n = int(max(x["rec_f"] + x["dur_f"] for x in t["items"]) / t["fps"] * SR) + 1
        out = np.zeros(n, np.float32)
        for x in t["items"]:
            j = jk[os.path.normcase(x["path"])]
            a, b = int(round(x["src_in_s"] * SR)), int(round(x["src_out_s"] * SR))
            c = cat_index(j, a)
            r0 = int(round(x["rec_f"] / t["fps"] * SR))
            with sf.SoundFile(str(WORK / f"{j['key']}.master.regions.wav")) as fm:  # seek: never all masters in RAM
                fm.seek(c)
                seg = fm.read(b - a, dtype="float32")
            out[r0:r0 + len(seg)] = seg[:max(0, n - r0)]
        i, tp, lra = loudness(out)
        man["timelines"][t["name"]] = {"I": i, "TP": tp, "LRA": lra}
        print(f"timeline {t['name']}: {i:.2f} LUFS, TP {tp:.2f} dBTP, LRA {lra:.1f}", flush=True)
    mpath.write_text(json.dumps(man, indent=1, ensure_ascii=False), encoding="utf-8")


def tc(seconds):
    return f"{int(seconds // 60):02d}:{seconds % 60:05.2f}"


def scan(loud_db=-30.0, drop_db=15.0):
    f = SR // 20
    plan = load_plan()
    rep = {}
    for j in load_jobs():
        mix = sf.read(j["mix"], dtype="float32")[0]
        y = sf.read(str(WORK / f"{j['key']}.reuse.final.regions.wav"), dtype="float32")[0]
        used = used_mask(j)
        y = y * np.float32(rms(mix[used]) / rms(y[used]))
        nfr = len(mix) // f

        def env(v):
            return 10 * np.log10(np.mean(np.square(v[:nfr * f].astype(np.float64)).reshape(nfr, f), 1) + 1e-20)

        md, yd = env(mix), env(y)
        ev = []
        for k in np.flatnonzero((md > loud_db) & (yd < md - drop_db)):
            if ev and k - ev[-1][1] <= 6:
                ev[-1][1] = k
            else:
                ev.append([k, k])
        rows = []
        for a, b in ev:
            r = next(r for r in j["layout"] if r["cat_start"] <= a * f < r["cat_start"] + region_len(r))
            src_s = (r["src_start"] + a * f - r["cat_start"]) / SR
            where = [{"timeline": t["name"], "tc": tc(x["rec_f"] / t["fps"] + src_s - x["src_in_s"])}
                     for t in plan["timelines"] for x in t["items"]
                     if os.path.normcase(x["path"]) == os.path.normcase(j["path"])
                     and x["src_in_s"] <= src_s < x["src_out_s"]]
            rows.append({"src_s": round(src_s, 2), "dur_s": round((b - a + 1) * f / SR, 2),
                         "orig_db": round(float(md[a:b + 1].max()), 1),
                         "drop_db": round(float((md[a:b + 1] - yd[a:b + 1]).max()), 1), "in_cut": where})
        rep[j["key"]] = {"name": j["name"], "events": rows}
        print(f"{j['key']}: {sum(1 for r in rows if r['in_cut'])} dropouts inside the cut, {len(rows)} in the regions",
              flush=True)
    (JOB / "scan.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    if not (JOB / "plan.json").exists():
        raise SystemExit(f"{JOB / 'plan.json'} missing: run resolve_cleanup.py ACTION='plan' first")
    names = STEPS if args.step == "all" else [args.step]
    for name in names:
        if name not in STEPS:
            raise SystemExit(f"unknown step {name}; steps: {', '.join(STEPS)} or all")
    sys.stdout = Tee(JOB / "run.log")
    t0 = time.time()
    print(f"\n### {time.strftime('%Y-%m-%d %H:%M:%S')} cleanup.py {args.step}", flush=True)
    for name in names:
        t = time.time()
        print(f"== {name}", flush=True)
        globals()[name]()
        print(f"   {name} {time.time() - t:.0f} s", flush=True)
    print(f"done in {(time.time() - t0) / 60:.1f} min", flush=True)
