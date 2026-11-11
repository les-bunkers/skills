# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Word-level transcript with fillers kept, from xAI Grok speech-to-text (PAID: billed per audio minute by xAI).

in:  media files, or --project edit.json [--src A ...] (each source's words_media, else its file)
out: per input, in --out-dir (default: <project>/words/ or beside the media): <stem>.grok.json (raw response, the
     cache: never re-requested while it exists), <stem>.words.json (Words, fillers typed), <stem>.srt (cues end on
     . ? ! or after 14 words). Times are on the input file's clock: words_offset_s maps them to the program file.
usage: python stt_grok.py --project cut/edit.json [--src A] [--language fr] [--keyterm X ...] [--diarize] --yes
       python stt_grok.py <file.mp4> ... --language fr --yes
Without --yes it prints what it would send (files, minutes, endpoint) and exits 2: the skill asks the human first.
env: XAI_API_KEY (Bearer), XAI_BASE_URL (default https://api.x.ai/v1; the script appends /stt). With a local key
proxy as base URL, a placeholder key works. Request: model grok-voice-transcribe-2.0, filler_words=true (Grok drops
« euh » / "um" by default), keyterm repeated (<= 100, <= 50 chars each: names, products), language, mono 16 kHz MP3.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editlib as E  # noqa: E402

E.setup_stdout()
MODEL = "grok-voice-transcribe-2.0"


def base_url():
    return os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1").removesuffix("/")


def duration(path):
    r = subprocess.run([os.environ.get("FFPROBE", "ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of",
                        "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def stt(path, language, keyterms, diarize):
    key = os.environ.get("XAI_API_KEY")
    url = base_url() + "/stt"
    if not key:
        if "api.x.ai" in url:
            sys.exit("XAI_API_KEY is not set (or point XAI_BASE_URL at a key proxy)")
        key = "placeholder"
    with tempfile.TemporaryDirectory() as td:
        mp3 = os.path.join(td, "a.mp3")
        subprocess.run([E.need_ffmpeg(), "-v", "error", "-y", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", "16000",
                        "-b:a", "64k", mp3], check=True)
        boundary = uuid.uuid4().hex
        fields = [("model", MODEL), ("filler_words", "true")]
        if language:
            fields.append(("language", language))
        if diarize:
            fields.append(("diarize", "true"))
        fields += [("keyterm", k[:50]) for k in keyterms[:100]]
        body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8") for k, v in fields)
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.mp3"\r\nContent-Type: audio/mpeg\r\n\r\n'.encode("utf-8")
        body += Path(mp3).read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")  # file = LAST field (API rule)
    req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                                          "Authorization": f"Bearer {key}", "User-Agent": "videos-plugin/1"})
    host = urllib.parse.urlparse(url).hostname or ""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if host in ("localhost", "127.0.0.1", "::1") \
        else urllib.request.build_opener()
    with opener.open(req, timeout=900) as r:
        return json.loads(r.read().decode("utf-8"))


def to_words(raw, source, language):
    d = {"source": Path(source).name, "language": language or raw.get("language", ""), "model": MODEL, "time_base": "source",
         "words": []}
    for w in raw.get("words", []):
        if not str(w.get("text", "")).strip():
            continue
        x = {"text": w["text"], "start": float(w["start"]), "end": float(w["end"]), "type": "word"}
        if "speaker" in w:
            x["speaker"] = w["speaker"]
        d["words"].append(x)
    lang = (d["language"] or "")[:2]
    if lang in E.FILLERS:
        E.words_with_types(d["words"], set(E.FILLERS[lang]))
    return d


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("media", nargs="*")
    ap.add_argument("--project")
    ap.add_argument("--src", action="append", help="source id from edit.json (repeat; default all)")
    ap.add_argument("--out-dir")
    ap.add_argument("--language", help="fr, en, ... (default: edit.json language)")
    ap.add_argument("--keyterm", action="append", default=[], help="name or product to bias toward (repeat)")
    ap.add_argument("--diarize", action="store_true", help="speaker ids per word (two speakers, podcasts)")
    ap.add_argument("--yes", action="store_true", help="make the paid call (the human agreed)")
    a = ap.parse_args()
    jobs, keyterms, language = [], list(a.keyterm), a.language
    if a.project:
        P = E.Project(a.project)
        keyterms += [k for k in P.cfg.get("keyterms", []) if k not in keyterms]
        language = language or P.cfg.get("language")
        out_dir = Path(a.out_dir) if a.out_dir else P.dir / "words"
        for sid in a.src or list(P.sources):
            s = P.src(sid)
            jobs.append((P.p(s.get("words_media")) or P.file(sid), out_dir))
    for m in a.media:
        jobs.append((m, Path(a.out_dir) if a.out_dir else Path(m).resolve().parent))
    if not jobs:
        ap.error("give media files or --project")
    todo = []
    for path, od in jobs:
        stem = Path(path).stem
        if (od / f"{stem}.grok.json").is_file():
            print(f"cached {od / (stem + '.grok.json')} (no call)")
            raw = E.read_json(od / f"{stem}.grok.json")
            E.write_json(od / f"{stem}.words.json", to_words(raw, path, language), indent=None)
            continue
        todo.append((path, od, stem))
    if not todo:
        return
    mins = sum(duration(p) for p, _, _ in todo) / 60
    print(f"{len(todo)} file(s), {mins:.1f} min of audio -> POST {base_url()}/stt ({MODEL}, filler_words=true, "
          f"language {language or 'auto'}, {len(keyterms)} keyterms{', diarize' if a.diarize else ''})")
    for p, od, stem in todo:
        print(f"  {p} -> {od / (stem + '.words.json')}")
    if not a.yes:
        print("PAID call not made: rerun with --yes once the human agreed.")
        sys.exit(2)
    for path, od, stem in todo:
        od.mkdir(parents=True, exist_ok=True)
        raw = stt(path, language, keyterms, a.diarize)
        E.write_json(od / f"{stem}.grok.json", raw)
        d = to_words(raw, path, language)
        E.write_json(od / f"{stem}.words.json", d, indent=None)
        cues = [(a_, b_, E.wrap(t)) for a_, b_, t in E.words_to_cues(d["words"])]
        E.write_srt(od / f"{stem}.srt", cues)
        print(f"{stem}: {len(d['words'])} words ({sum(w['type'] == 'filler' for w in d['words'])} fillers), {len(cues)} cues")


if __name__ == "__main__":
    main()
