# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Bake-off job folder for agent-ears: config, Words loading, strata. Stdlib only (imported by every bake-off script).

A job folder holds ONE take under test and everything the bake-off writes about it:
  bakeoff.json        {"name": "a 10-min French take", "order": ["dsp", "respiro", "peaframe"],
                       "weak_euh": ["peaframe"], "strata": {...}}            (committable: no machine paths)
  bakeoff.local.json  {"audio": "/media/take.mp4", "words": "/project/take.scribe.words.json"}   (overrides; never commit)
  probs_<det>.npz     per-hop probabilities of a detector you can re-threshold (run_peaframe.py, run_respiro.py)
  spans_<det>.json    fixed spans of any other detector (format: references/bakeoff_files.md)
  candidates.json     union.py · key.json (labels) key_server.py · sample.json · score.md / score.json score.py
  work/               decoded audio caches
Relative paths resolve against the job folder. "order" = detector order (union members, votes, table rows);
default = spans_* files, then probs_* files, alphabetical. Words fillers (type "filler") join as detector "words".

usage: python ears_job.py init <job> --audio FILE --words FILE [--name TEXT]
       python ears_job.py show <job>
"""
import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

# name -> how many to label (None = all). Scoring weights each labelled candidate by its stratum's full size.
STRATA = {
    "euh_claimed": None,    # « euh » claimed by a detector you trust for fillers (not in weak_euh): rare, label all
    "speech?": None,        # a detector heard voice with no transcript word: the safety check, label all
    "weak_euh_only": 15,    # only weak euh sources (default: PE-A-Frame's euh prompt)
    "breath_3": 10,         # breath flagged by 3 or more detector families
    "breath_2": 30,
    "breath_1": 20,
}
WEAK_EUH = ["peaframe"]  # PE-A-Frame's "someone saying uh" prompt fires on breaths: P <= 0.42 in the bake-off


def ffmpeg():
    return os.environ.get("FFMPEG", "ffmpeg")


def ffprobe():
    return os.environ.get("FFPROBE", "ffprobe")


def duration(path):
    out = subprocess.run([ffprobe(), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def extract_wav(src, dst, sr, codec="pcm_f32le", highpass=None):
    """Mono wav of src at sr (optionally high-passed), written once; reused while newer than src."""
    dst = Path(dst)
    if dst.is_file() and dst.stat().st_mtime >= Path(src).stat().st_mtime:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    af = ["-af", f"highpass=f={highpass}"] if highpass else []
    subprocess.run([ffmpeg(), "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(sr), *af,
                    "-c:a", codec, str(dst)], check=True)
    return dst


def norm(s):
    """Lower case, accents and punctuation stripped: « Euh... » -> euh."""
    s = "".join(ch for ch in unicodedata.normalize("NFD", s.lower()) if not unicodedata.combining(ch))
    return re.sub(r"[^\w]", "", s)


def family(src):
    """Detector family of a span source: "respiro@0.5" -> "respiro"."""
    return str(src).split("@")[0]


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj, indent=1):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)
    return path


def load_job(job):
    """bakeoff.json + bakeoff.local.json of a job folder -> dict with absolute "audio"/"words" Paths and "dir"."""
    d = Path(job).resolve()
    cfg = {"name": d.name, "strata": dict(STRATA), "weak_euh": list(WEAK_EUH), "order": None}
    for name in ("bakeoff.json", "bakeoff.local.json"):
        p = d / name
        if p.is_file():
            cfg.update(read_json(p))
    for k in ("audio", "words"):
        if cfg.get(k):
            p = Path(cfg[k]).expanduser()
            cfg[k] = p if p.is_absolute() else (d / p).resolve()
    if not cfg.get("audio"):
        sys.exit(f"{d}: no audio in bakeoff.json / bakeoff.local.json (run: ears_job.py init {d} --audio FILE --words FILE)")
    cfg["dir"] = d
    (d / "work").mkdir(parents=True, exist_ok=True)
    return cfg


def detectors(cfg):
    """Detector names in table order: cfg["order"] first, then the remaining spans_* and probs_* files."""
    d = cfg["dir"]
    fixed = sorted(p.stem[len("spans_"):] for p in d.glob("spans_*.json"))
    probs = sorted(p.stem[len("probs_"):] for p in d.glob("probs_*.npz"))
    found = {n: "spans" for n in fixed} | {n: "probs" for n in probs}  # a probs file wins over a spans file of the same name
    names = [n for n in (cfg.get("order") or []) if n in found]
    names += [n for n in fixed + probs if n not in names]
    return [(n, found[n]) for n in dict.fromkeys(names)]


def words(path):
    """Words file -> [{text, start, end, type, speaker}] sorted by start (types word | filler | event)."""
    d = read_json(path)
    out = [{"text": w["text"], "start": float(w["start"]), "end": float(w["end"]), "type": w.get("type", "word"),
            "speaker": w.get("speaker")} for w in d["words"] if str(w.get("text", "")).strip()]
    out.sort(key=lambda w: w["start"])
    return out, d


def speech_words(ws):
    """Words and fillers (anything spoken), events left out: what the word mask and the context line use."""
    return [w for w in ws if w["type"] != "event"]


def stratum(c, weak_euh=WEAK_EUH):
    seed = [m for m in c["members"] if m["type"] in ("breath", "euh", "speech?")]
    types = {m["type"] for m in seed}
    if "speech?" in types:
        return "speech?"
    if any(m["type"] == "euh" and family(m["src"]) not in weak_euh for m in seed):
        return "euh_claimed"
    if types == {"euh"}:
        return "weak_euh_only"
    return f"breath_{min(3, len({family(m['src']) for m in seed}))}"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Create or show a bake-off job folder.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="write bakeoff.json (name, strata) + bakeoff.local.json (audio, words paths)")
    i.add_argument("job")
    i.add_argument("--audio", required=True, help="the take: any file ffmpeg reads")
    i.add_argument("--words", required=True, help="its Words transcript (*.words.json)")
    i.add_argument("--name", default=None, help='how reports name the take, e.g. "a 10-min French take"')
    s = sub.add_parser("show", help="print the resolved config and the detector files found")
    s.add_argument("job")
    a = ap.parse_args()
    d = Path(a.job).resolve()
    if a.cmd == "init":
        d.mkdir(parents=True, exist_ok=True)
        base = read_json(d / "bakeoff.json") if (d / "bakeoff.json").is_file() else {}
        base.setdefault("name", a.name or d.name)
        if a.name:
            base["name"] = a.name
        base.setdefault("weak_euh", list(WEAK_EUH))
        base.setdefault("strata", dict(STRATA))
        save_json(d / "bakeoff.json", base)
        local = read_json(d / "bakeoff.local.json") if (d / "bakeoff.local.json").is_file() else {}
        local.update({"audio": str(Path(a.audio).resolve()), "words": str(Path(a.words).resolve())})
        save_json(d / "bakeoff.local.json", local)
        print(f"{d / 'bakeoff.json'}\n{d / 'bakeoff.local.json'}")
    cfg = load_job(d)
    print(json.dumps({k: str(v) if isinstance(v, Path) else v for k, v in cfg.items()}, ensure_ascii=False, indent=1))
    print("detectors:", ", ".join(f"{n} ({k})" for n, k in detectors(cfg)) or "none yet")


if __name__ == "__main__":
    main()
