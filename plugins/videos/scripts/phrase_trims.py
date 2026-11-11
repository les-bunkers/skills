# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Phrase trims: check the phrase-trim agent's spans against the words file, show each cut as it would read, and
(--apply) write them into the overrides file's drop_spans, which tighten.py cuts only where both edges split cleanly.

in:  --words words.txt (words_dump.py), --spans spans.json (the agent):
       [{"seg": 3, "first": 117, "last": 118, "kind": "filler phrase", "conf": "high|med|low", "why": "...", "skip": false}]
     one span = contiguous words inside one seg. "skip": true = rejected after reading (kept out of drop_spans).
out: <spans stem>.proposals.json beside the spans (every span with removed text and the joined reading);
     --apply: overrides.json drop_spans {source: [[first, last, label, removed], ...]}, label = kind (+ " (conf)" when
     conf isn't high). Spans of a source outside this words file's segments stay; a backup goes beside the overrides.
usage: python phrase_trims.py --words cut/phrase-trims/words.txt --spans cut/phrase-trims/spans.json
                              [--apply --overrides cut/overrides.json]
"""
import argparse
import re
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editlib as E  # noqa: E402

E.setup_stdout()
PUNCT = (",", ".", "?", "!", ";", ":")


def parse_words(path):
    segs, cur = {}, None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        m = re.match(r"## seg (\d+) \| take (\S+) \|", line)
        if m:
            cur = int(m.group(1))
            segs[cur] = {"src": m.group(2), "toks": []}
            continue
        if cur is None or not line.strip() or line.startswith("#"):
            continue
        pause = False
        for t in line.split():
            if t == "/":
                pause = True
                continue
            i, w = t.split(":", 1)
            segs[cur]["toks"].append({"id": int(i), "w": w, "pause": pause, "br": w.startswith("[")})
            pause = False
    return segs


def words(ts):
    return " ".join(t["w"] for t in ts)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--words", required=True)
    ap.add_argument("--spans", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--overrides", help="overrides.json to write drop_spans into (with --apply)")
    a = ap.parse_args()
    segs = parse_words(a.words)
    spans = E.read_json(a.spans)
    out, errs, used = [], [], {}
    for sp in spans:
        seg, i0, i1 = int(sp["seg"]), int(sp["first"]), int(sp["last"])
        if seg not in segs:
            errs.append(f"seg {seg}: no such seg")
            continue
        toks = segs[seg]["toks"]
        pos = {t["id"]: k for k, t in enumerate(toks)}
        if i0 not in pos or i1 not in pos or pos[i0] > pos[i1]:
            errs.append(f"seg {seg}: span {i0}-{i1} not inside the seg")
            continue
        ia, ib = pos[i0], pos[i1]
        span = toks[ia:ib + 1]
        for i in range(ia, ib + 1):
            if (seg, i) in used:
                errs.append(f"seg {seg}: overlap {i0}-{i1} with {used[(seg, i)]}")
            used[(seg, i)] = f"{i0}-{i1}"
        prev = toks[ia - 1] if ia > 0 else None
        nxt = toks[ib + 1] if ib + 1 < len(toks) else None
        start_ok = prev is None or toks[ia]["pause"] or prev["br"] or span[0]["br"] or prev["w"].endswith(PUNCT)
        end_ok = nxt is None or nxt["pause"] or nxt["br"] or span[-1]["br"] or span[-1]["w"].endswith(PUNCT)
        before = [t for t in toks[:ia] if not t["br"]][-6:]
        after = [t for t in toks[ib + 1:] if not t["br"]][:6]
        joined = (words(before) or "[seg start]") + " ‖ " + (words(after) or "[seg end]")
        removed = " ".join(("/ " if (t["pause"] and k > 0) else "") + t["w"] for k, t in enumerate(span))
        out.append({"src": segs[seg]["src"], "seg": seg, "first": i0, "last": i1, "removed": removed, "joined": joined,
                    "kind": sp.get("kind", "phrase"), "conf": sp.get("conf", "high"), "why": sp.get("why", ""),
                    "skip": bool(sp.get("skip")), "edge_pause": bool(start_ok and end_ok)})
    if errs:
        print("ERRORS:\n" + "\n".join(errs))
    sp_path = Path(a.spans)
    E.write_json(sp_path.with_name(sp_path.stem.replace("spans", "proposals") + ".json"
                                   if "spans" in sp_path.stem else sp_path.stem + ".proposals.json"), out)
    for o in out:
        flag = "SKIP " if o["skip"] else ""
        print(f'{flag}{o["seg"]:>2} {o["src"]} {o["first"]}-{o["last"]} [{o["kind"]}/{o["conf"]}/{"P" if o["edge_pause"] else "-"}]'
              f' -{o["removed"]}-  ::  {o["joined"]}')
    print(len(out), "spans,", sum(o["skip"] for o in out), "skipped;", dict(Counter(o["kind"] for o in out)),
          dict(Counter(o["conf"] for o in out)))
    if not a.apply:
        return
    if errs:
        sys.exit("not applied: fix the errors first")
    if not a.overrides:
        sys.exit("--apply needs --overrides")
    ovp = Path(a.overrides)
    ovr = E.read_json(ovp) if ovp.is_file() else {}
    if ovp.is_file():
        shutil.copy(ovp, ovp.with_name(f"{ovp.stem}.before-trims-{time.strftime('%Y%m%d-%H%M%S')}.json"))
    ds = ovr.setdefault("drop_spans", {})
    for src in sorted({o["src"] for o in out}):
        here = {t["id"] for s in segs.values() if s["src"] == src for t in s["toks"]}
        keep = [x for x in ds.get(src, []) if int(x[0]) not in here]
        new = [[o["first"], o["last"], o["kind"] if o["conf"] == "high" else f'{o["kind"]} ({o["conf"]})', o["removed"]]
               for o in out if o["src"] == src and not o["skip"]]
        ds[src] = sorted(keep + new, key=lambda x: int(x[0]))
        print(f"drop_spans[{src}] = {len(new)} spans from this words file" + (f" + {len(keep)} kept from elsewhere" if keep else ""))
    E.write_json(ovp, ovr)
    print(f"written {ovp}. Next: tighten.py")


if __name__ == "__main__":
    main()
