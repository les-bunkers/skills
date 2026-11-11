# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31"]
# ///
"""ElevenLabs Scribe speech-to-text (PAID) on speech files -> <stem>.scribe.json (raw response, cached) +
<stem>.scribe.words.json (Words format: fillers as type "filler", audio events as type "event"; scribe_to_words.py).

Request: model scribe_v3, verbatim (fillers and false starts kept; never send no_verbatim, it strips fillers AND
audio events), tag_audio_events, character timestamps, diarize off, temperature 0, seed 1. Upload = FLAC 16 kHz mono
extracted from t=0 of the input (~1.6 MiB per minute of audio), so times are input-file seconds, the same time base
as ears.py. Measured: a 10-min take came back in ~9 s.

Paid: without --yes it only prints the audio length and estimated upload, then exits 3. The skill asks the user
first. Cached: an existing <stem>.scribe.json is never re-requested (--force does); the Words file is rebuilt from
it when missing or older.
Key: ELEVENLABS_API_KEY, sent as the xi-api-key header. Base URL: ELEVENLABS_BASE_URL (default
https://api.elevenlabs.io, no /v1: the script appends /v1/speech-to-text; one trailing slash is stripped).
Upload guard --max-mib (default 49): ElevenLabs takes files up to 5 GB, but many API gateways cap a request body at
50 MiB, so a FLAC above 49 MiB (~30 min of audio) stops with a message. Split the file, or raise --max-mib when the
request goes straight to ElevenLabs.

usage: uv run run_scribe.py <audio ...> --out-dir DIR [--lang fra] [--model scribe_v3] [--yes] [--force]
                            [--max-mib 49] [--diarize]
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scribe_to_words as S  # noqa: E402

UA = "agent-ears/0.1 (+https://github.com/les-bunkers/skills)"
MIB_PER_MIN = 1.6


def ffmpeg():
    return os.environ.get("FFMPEG", "ffmpeg")


def ffprobe():
    return os.environ.get("FFPROBE", "ffprobe")


def duration(path):
    out = subprocess.run([ffprobe(), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def base_url():
    b = os.environ.get("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io")
    return b[:-1] if b.endswith("/") else b


def transcribe(src, raw_path, a):
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        sys.exit("ELEVENLABS_API_KEY is not set (when ELEVENLABS_BASE_URL points at a gateway that adds the key, "
                 "any placeholder value works)")
    with tempfile.TemporaryDirectory() as td:
        flac = Path(td) / "audio.flac"
        subprocess.run([ffmpeg(), "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "flac",
                        str(flac)], check=True)
        size = flac.stat().st_size / 2**20
        print(f"  FLAC 16 kHz mono {size:.1f} MiB", flush=True)
        if size > a.max_mib:
            sys.exit(f"{src}: upload is {size:.1f} MiB, over the {a.max_mib:g} MiB guard (many gateways cap request bodies "
                     "at 50 MiB). Split the audio into parts under ~30 min, or pass --max-mib 4000 when the request "
                     "goes straight to ElevenLabs (its own limit is 5 GB).")
        form = {"model_id": a.model, "tag_audio_events": "true", "timestamps_granularity": "character",
                "diarize": "true" if a.diarize else "false", "temperature": "0", "seed": "1", "file_format": "other"}
        if a.lang:
            form["language_code"] = a.lang
        t0 = time.time()
        with open(flac, "rb") as f:
            r = requests.post(f"{base_url()}/v1/speech-to-text", data=form,
                              files={"file": ("audio.flac", f, "audio/flac")},
                              headers={"xi-api-key": key, "User-Agent": UA}, timeout=1800)
    print(f"  HTTP {r.status_code} in {time.time() - t0:.0f} s", flush=True)
    if r.status_code != 200:
        print(r.text[:2000])
        sys.exit(1)
    d = r.json()
    d["_request"] = dict(form) | {"source": str(src)}
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = raw_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, raw_path)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="ElevenLabs Scribe (paid) -> <stem>.scribe.json + <stem>.scribe.words.json.")
    ap.add_argument("audio", nargs="+", help="audio or video files (anything ffmpeg reads)")
    ap.add_argument("--out-dir", required=True, help="project or job folder for the outputs (not the media folder)")
    ap.add_argument("--model", default="scribe_v3")
    ap.add_argument("--lang", default=None, help="ISO 639-1/3 code (fra, eng...); default: Scribe detects it")
    ap.add_argument("--diarize", action="store_true", help="speaker ids (several speakers)")
    ap.add_argument("--yes", action="store_true", help="the user agreed to the paid call")
    ap.add_argument("--force", action="store_true", help="request again even when <stem>.scribe.json exists")
    ap.add_argument("--max-mib", type=float, default=49, help="upload guard in MiB (default 49)")
    a = ap.parse_args()
    out = Path(a.out_dir)
    jobs = []
    for s in map(Path, a.audio):
        if not s.is_file():
            sys.exit(f"missing input: {s}")
        raw = out / f"{s.stem}.scribe.json"
        jobs.append((s, raw, a.force or not raw.is_file()))
    paid = [(s, duration(s)) for s, _, call in jobs if call]
    if paid and not a.yes:
        mins = sum(d for _, d in paid) / 60
        for s, d in paid:
            print(f"{s}: {d / 60:.1f} min, upload ~{d / 60 * MIB_PER_MIN:.1f} MiB")
        print(f"PAID: {len(paid)} file(s), {mins:.1f} min of audio billed by ElevenLabs Scribe ({a.model}). "
              "Ask the user, then rerun with --yes. Cached files are never re-requested.")
        sys.exit(3)
    for s, raw, call in jobs:
        print(s, flush=True)
        if call:
            transcribe(s, raw, a)
        else:
            print(f"  cached {raw}")
        words = S.words_path(raw)
        if a.force or call or not words.is_file() or words.stat().st_mtime < raw.stat().st_mtime:
            words, d = S.write(raw)
        else:
            with open(words, encoding="utf-8") as f:
                d = json.load(f)
        print(f"  {words}")
        for line in S.check(d):
            print("    " + line)


if __name__ == "__main__":
    main()
