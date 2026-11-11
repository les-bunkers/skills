# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Convert a transcript to the shared Words format (*.words.json) the edit engine reads.

in:  a studio transcript (schema 1: segments[].words[{w, start_ms, end_ms}]), a raw Grok STT response, a raw
     ElevenLabs Scribe response (type word/spacing/audio_event), OpenAI verbose_json with word timestamps, a Words file,
     or an SRT (word times spread over each cue by length: approximate, fine for anchors, too coarse for tighten)
out: <stem>.words.json beside the input (or --out): {"source", "language", "model", "time_base": "source", "words":
     [{"text", "start", "end", "type": word|filler|event, "speaker"?}]}. Empty tokens dropped, so word id = index.
     Fillers get type "filler" from the language's list (fr: euh heu hum hmm euhm; en: um uh er erm hmm mm uhm).
usage: python import_words.py <transcript> [--out X.words.json] [--source <media file>] [--language fr] [--model M]
                              [--no-fillers]
Times stay on the transcript's own clock: put the offset to the program file in edit.json words_offset_s (sync.py).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editlib as E  # noqa: E402

E.setup_stdout()


def from_srt(path):
    words = []
    for a, b, text in E.parse_srt(path):
        toks = text.split()
        if not toks:
            continue
        total = sum(len(t) + 1 for t in toks)
        t = a
        for tok in toks:
            d = (b - a) * (len(tok) + 1) / total
            words.append({"text": tok, "start": round(t, 3), "end": round(t + d * 0.9, 3), "type": "word"})
            t += d
    return {"source": "", "language": "", "model": "srt (approximate word times)", "time_base": "source", "words": words}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("transcript")
    ap.add_argument("--out")
    ap.add_argument("--source", help="media file the transcript was made on (stored as \"source\")")
    ap.add_argument("--language", help="language code (fr, en, ...) when the transcript does not say")
    ap.add_argument("--model", help="model name when the transcript does not say")
    ap.add_argument("--no-fillers", action="store_true", help="do not mark fillers by name")
    a = ap.parse_args()
    src = Path(a.transcript)
    d = from_srt(src) if src.suffix.lower() == ".srt" else E.read_words(src)
    if a.source:
        d["source"] = a.source
    if a.language:
        d["language"] = a.language
    if a.model:
        d["model"] = a.model
    lang = (d.get("language") or "")[:2]
    for w in d["words"]:
        w.setdefault("type", "word")
    if not a.no_fillers and lang in E.FILLERS:
        E.words_with_types(d["words"], set(E.FILLERS[lang]))
    out = Path(a.out) if a.out else src.with_name(src.name.split(".")[0] + ".words.json")
    if out.resolve() == src.resolve():
        sys.exit("refusing to overwrite the input; pass --out")
    E.write_json(out, d, indent=None)
    n = len(d["words"])
    fil = sum(w["type"] == "filler" for w in d["words"])
    print(f"{out}: {n} words ({fil} fillers), language {d['language'] or '?'}, model {d['model'] or '?'}"
          + (f", {d['words'][0]['start']:.2f}-{d['words'][-1]['end']:.2f} s" if n else ""))


if __name__ == "__main__":
    main()
