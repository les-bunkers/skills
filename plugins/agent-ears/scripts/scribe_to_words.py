# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""ElevenLabs Scribe response (raw JSON) -> Words file (*.words.json, the shared transcript format). Stdlib only.

Scribe tokens -> Words entries:
  type "word"         -> "word", or "filler" when it is a filled pause of the transcript's language
                         fr: euh heu hum hmm euhm · en: um umm uh uhm er erm hmm hm · other: fr + en minus « hum »
  type "audio_event"  -> "event" (text as Scribe wrote it: "[rire]", "[toux]", "(laughter)")
  type "spacing"      -> dropped
French: « eh », « ben », « hein » (Québec tag word) and « eu » (avoir) stay words. Scribe spells them apart from
« euh », and an editor keeps them.
Each entry: text (Scribe's, punctuation kept), start, end (s, 3 decimals), type, logprob, speaker (int, diarized only).
Top level: source, language (ISO 639-1 when known), model, time_base "source", raw (the Scribe file's name),
duration_s. Times are seconds of the audio Scribe heard; run_scribe.py uploads the source from t=0, so they are
source-file seconds (same base as ears.py).

Prints counts (words, fillers, events) and a timestamp sanity check (order, end >= start, last end <= duration).

usage: python scribe_to_words.py <x.scribe.json ...> [--out FILE ...] [--source MEDIA] [--fillers euh,heu,...]
       default output: x.scribe.json -> x.scribe.words.json beside it
"""
import argparse
import json
import os
import re
import statistics
import sys
import unicodedata
from collections import Counter
from pathlib import Path

FILLERS = {
    "fr": {"euh", "heu", "hum", "hmm", "euhm"},
    "en": {"um", "umm", "uh", "uhm", "er", "erm", "hmm", "hm"},
}
ISO3 = {"fra": "fr", "eng": "en", "spa": "es", "deu": "de", "ita": "it", "por": "pt", "nld": "nl"}


def norm(s):
    s = "".join(ch for ch in unicodedata.normalize("NFD", s.lower()) if not unicodedata.combining(ch))
    return re.sub(r"[^\w]", "", s)


def fillers_for(lang):
    if lang in FILLERS:
        return FILLERS[lang]
    return (FILLERS["fr"] | FILLERS["en"]) - {"hum"}  # « hum » is an English word


def speaker(w):
    s = w.get("speaker_id")
    if s is None:
        return None
    m = re.search(r"(\d+)$", str(s))
    return int(m.group(1)) if m else s


def convert(raw, raw_name="", source=None, fillers=None):
    req = raw.get("_request", {})
    lang3 = raw.get("language_code") or req.get("language_code") or ""
    lang = ISO3.get(lang3, lang3)
    fil = fillers or fillers_for(lang)
    out = []
    for w in raw.get("words", []):
        t = w.get("type")
        if t == "spacing" or not str(w.get("text", "")).strip():
            continue
        kind = "event" if t == "audio_event" else "filler" if norm(w["text"]) in fil else "word"
        e = {"text": w["text"], "start": round(float(w["start"]), 3), "end": round(float(w["end"]), 3), "type": kind}
        if w.get("logprob") is not None:
            e["logprob"] = round(float(w["logprob"]), 4)
        sp = speaker(w)
        if sp is not None:
            e["speaker"] = sp
        out.append(e)
    return {"source": source or req.get("source", ""), "language": lang, "model": req.get("model_id", "scribe"),
            "time_base": "source", "raw": raw_name, "duration_s": raw.get("audio_duration_secs"), "words": out}


def check(d):
    """Counts + timestamp sanity -> printable lines."""
    ws = d["words"]
    kinds = Counter(w["type"] for w in ws)
    fil = [w for w in ws if w["type"] == "filler"]
    ev = [w for w in ws if w["type"] == "event"]
    runs = sum(1 for i, w in enumerate(ws) if w["type"] == "filler" and (i == 0 or ws[i - 1]["type"] != "filler"))
    back = sum(1 for a, b in zip(ws, ws[1:]) if b["start"] < a["start"] - 1e-6)
    neg = sum(1 for w in ws if w["end"] < w["start"])
    last = max((w["end"] for w in ws), default=0.0)
    dur = d.get("duration_s")
    lines = [f"{kinds.get('word', 0)} words, {len(fil)} fillers ({runs} runs), {len(ev)} events; "
             f"{ws[0]['start'] if ws else 0:.2f}-{last:.2f} s of {dur if dur is not None else '?'} s"]
    if fil:
        lines.append("fillers: " + ", ".join(f"{k} {v}" for k, v in Counter(norm(w["text"]) for w in fil).most_common())
                     + f"; median length {statistics.median(w['end'] - w['start'] for w in fil):.2f} s")
    if ev:
        lines.append("events: " + ", ".join(f"{k} {v}" for k, v in Counter(w["text"] for w in ev).most_common()))
    bad = []
    if back:
        bad.append(f"{back} starts out of order")
    if neg:
        bad.append(f"{neg} entries end before they start")
    if dur is not None and last > float(dur) + 0.05:
        bad.append(f"last end {last:.2f} s past the audio duration {dur} s")
    lines.append("timestamps: " + ("; ".join(bad) if bad else "ordered, end >= start, within the audio"))
    return lines


def words_path(raw_path):
    p = Path(raw_path)
    name = p.name[:-len(".scribe.json")] if p.name.endswith(".scribe.json") else p.stem
    return p.with_name(f"{name}.scribe.words.json")


def write(raw_path, dst=None, source=None, fillers=None):
    raw_path = Path(raw_path)
    with open(raw_path, encoding="utf-8") as f:
        raw = json.load(f)
    d = convert(raw, raw_path.name, source, fillers)
    dst = Path(dst) if dst else words_path(raw_path)
    tmp = dst.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, dst)
    return dst, d


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Scribe raw JSON -> Words file with fillers and events typed.")
    ap.add_argument("raw", nargs="+", help="Scribe responses (<stem>.scribe.json)")
    ap.add_argument("--out", nargs="*", default=None, help="one output path per input (default <stem>.scribe.words.json)")
    ap.add_argument("--source", help="media file the transcript belongs to (default: the request's recorded source)")
    ap.add_argument("--fillers", help="comma-separated filler words, replaces the language default")
    a = ap.parse_args()
    if a.out and len(a.out) != len(a.raw):
        ap.error("--out needs one path per input")
    fil = {norm(x) for x in a.fillers.split(",") if x.strip()} if a.fillers else None
    for i, r in enumerate(a.raw):
        dst, d = write(r, a.out[i] if a.out else None, a.source, fil)
        print(dst)
        for line in check(d):
            print("  " + line)


if __name__ == "__main__":
    main()
