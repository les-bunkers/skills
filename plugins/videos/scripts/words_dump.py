# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Words file for the phrase-trim pass: the kept main segments of a rough cut as id:word tokens, one block per segment.

Header per block: segment number, source, timeline time, word id range. "/ " before a token = pause >= 0.4 s before
that word. [brackets] = words tighten drops anyway (fillers by name, spare words a previous tighten pass removed).
The phrase-trim agent reads it and proposes spans; phrase_trims.py parses it back.
in:  --project edit.json, --cuts rough.cuts.json, optional --tight tight.cuts.json (brackets its removed spare words)
out: --out words.txt (default <cuts folder>/phrase-trims/words.txt)
usage: python words_dump.py --project cut/edit.json --cuts cut/rough.cuts.json [--tight cut/tight.cuts.json] [--out F]
       python words_dump.py --project cut/edit.json --src A [--out F]   whole transcript, one line per cue with its
       [mm:ss.s] on the words clock (the times rough.json anchors use), "/ " = pause >= 0.4 s
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editlib as E  # noqa: E402

E.setup_stdout()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--cuts")
    ap.add_argument("--src", action="append", help="dump whole sources (for writing rough.json anchors)")
    ap.add_argument("--tight")
    ap.add_argument("--out")
    a = ap.parse_args()
    P = E.Project(a.project)
    if a.src:
        lines = []
        for sid in a.src:
            ws, off = P.words(sid), P.offset(sid)
            lines.append(f"## {sid} ({len(ws)} words, times = words clock)")
            cur, t0 = [], None
            for i, w in enumerate(ws):
                if not cur:
                    t0 = w["start"] - off
                gap = w["start"] - ws[i - 1]["end"] if i else 0
                cur.append(("/ " if gap >= 0.4 and cur else "") + w["text"])
                if w["text"].rstrip().endswith((".", "?", "!")) or len(cur) >= 14:
                    lines.append(f"[{int(t0 // 60):02d}:{t0 % 60:04.1f} = {t0:.1f}] " + " ".join(cur))
                    cur = []
            if cur:
                lines.append(f"[{int(t0 // 60):02d}:{t0 % 60:04.1f} = {t0:.1f}] " + " ".join(cur))
        text = "\n".join(lines) + "\n"
        if a.out:
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            Path(a.out).write_text(text, encoding="utf-8")
            print(a.out, len(lines), "lines")
        else:
            print(text)
        return
    if not a.cuts:
        ap.error("--cuts (or --src)")
    c = E.load_cuts(a.cuts)
    fps = c["fps"]
    t = E.read_json(a.tight) if a.tight else None
    dropped = {(d["src"], d["id"]) for d in (t or {}).get("spare", []) if d["removed"]}
    out, n = [], 0
    for k, s in enumerate(c["segments"]):
        if s.get("alt"):
            continue
        src = s["src"]
        ws = P.words(src)
        fillers = set(E.FILLERS.get((P.language(src) or "")[:2], []))
        lo, hi = s["in_f"] / fps, s["out_f"] / fps
        ids = [i for i, w in enumerate(ws) if lo <= (w["start"] + w["end"]) / 2 <= hi]
        if not ids:
            continue
        tl = s["rec_f"] / fps
        out.append(f"\n## seg {k} | take {src} | timeline {int(tl // 60):02d}:{tl % 60:04.1f} | words {ids[0]}-{ids[-1]}")
        toks = []
        for i in ids:
            w = ws[i]["text"]
            if (src, i) in dropped or E.norm(w) in fillers or ws[i].get("type") == "filler":
                w = f"[{w}]"
            gap = ws[i]["start"] - ws[i - 1]["end"] if i > ids[0] else 0
            toks.append(("/ " if gap >= 0.4 else "") + f"{i}:{w}")
            n += 1
        out.append(" ".join(toks))
    p = Path(a.out) if a.out else Path(a.cuts).resolve().parent / "phrase-trims" / "words.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# {c.get('timeline', '')} — {n} words, main cut only\n" + "\n".join(out) + "\n", encoding="utf-8")
    print(p, n, "words,", sum(1 for s in c["segments"] if not s.get("alt")), "segments",
          "" if t else "(no --tight: spare words not bracketed yet)")


if __name__ == "__main__":
    main()
