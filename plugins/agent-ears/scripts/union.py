# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=2"]
# ///
"""Bake-off step 2: candidate union -> <job>/candidates.json, the stretches a human labels.

Seeds = every span of type breath / euh / speech? from every detector of the job, each at its low union threshold:
  probs_<det>.npz   spans above each meta.union_thrs value (src "<det>" or "<det>@<thr>"), every variant
  spans_<det>.json  fixed rows (any other detector: a DSP pass, another model, your own code)
  Words fillers     type "filler" entries of the job's Words file (src "words", type euh)
Other types (mouth, handling, laugh, throat, noise) never seed a candidate: they ride along as members of one they
overlap. "speech" (a control prompt) is ignored. Spans overlapping or within 30 ms of each other merge; clusters
longer than 2 s split at their quietest 10 ms hop (one breath or euh per snippet).
Each candidate: id, t0, t1, members [{t0, t1, type, label, conf, src, variant, free}], free (part outside the word
mask, or null), in_word, ctx ("prev … next" word), srcs, types. Recall in score.py is relative to this union:
a breath no detector flagged is not in the key.

usage: uv run union.py <job>
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ears_dsp as D  # noqa: E402
import ears_job as J  # noqa: E402

JOIN = 0.03
MAX_LEN = 2.0
SEED_TYPES = {"breath", "euh", "speech?"}


def load_probs(path):
    z = np.load(path, allow_pickle=False)
    meta = json.loads(str(z["meta"]))
    return meta, {v: z[v] for v in meta["variants"]}


def detector_rows(cfg, name, kind, mask):
    rows = []
    if kind == "spans":
        d = J.read_json(cfg["dir"] / f"spans_{name}.json")
        for r in d["rows"]:
            t0, t1 = float(r["t0"]), float(r["t1"])
            rows.append({"t0": t0, "t1": t1, "type": r["type"], "label": r.get("label", r["type"]),
                         "conf": r.get("conf"), "src": r.get("src", name), "variant": r.get("variant", ""),
                         "free": r["free"] if "free" in r else D.free_part(mask, t0, t1)})
        return rows
    meta, arrays = load_probs(cfg["dir"] / f"probs_{name}.npz")
    hop, mf, thrs = meta["hop_s"], meta.get("min_frames", 1), meta["union_thrs"]
    for v in meta["variants"]:
        for thr in thrs:
            src = name if len(thrs) == 1 else f"{name}@{thr}"
            for i, p in enumerate(meta["prompts"]):
                if p["type"] == "speech":
                    continue
                for a, b, c in D.spans(arrays[v][i], thr, hop, mf):
                    rows.append({"t0": a, "t1": b, "type": p["type"], "label": p["name"], "conf": c, "src": src,
                                 "variant": v, "free": D.free_part(mask, a, b)})
    return rows


def filler_rows(ws, model):
    return [{"t0": round(w["start"], 3), "t1": round(w["end"], 3), "type": "euh", "label": "filler", "conf": None,
             "src": "words", "variant": model, "free": None, "text": w["text"]} for w in ws if w["type"] == "filler"]


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    import argparse

    ap = argparse.ArgumentParser(description="Union of every detector's spans -> candidates.json for labelling.")
    ap.add_argument("job", help="bake-off job folder (ears_job.py init)")
    ap.add_argument("--no-words-fillers", action="store_true", help="don't seed from the Words file's fillers")
    a = ap.parse_args()
    cfg = J.load_job(a.job)
    ws_all, wd = J.words(cfg["words"])
    ws = J.speech_words(ws_all)
    level = D.Level(cfg["audio"])
    mask = D.attached_mask(level, ws)

    spans = []
    dets = J.detectors(cfg)
    if not dets:
        sys.exit(f"{cfg['dir']}: no probs_*.npz or spans_*.json yet (run run_peaframe.py / run_respiro.py first)")
    fillers = [] if a.no_words_fillers else filler_rows(ws_all, wd.get("model", ""))
    names = [n for n, _ in dets]
    for name, kind in dets:
        rows = detector_rows(cfg, name, kind, mask)
        print(f"{name} ({kind}): {len(rows)} spans", {t: sum(r['type'] == t for r in rows) for t in sorted({r['type'] for r in rows})})
        spans += rows
    if fillers:
        print(f"words fillers: {len(fillers)}")
        spans += fillers
        names.append("words")

    spans.sort(key=lambda r: r["t0"])
    seeds = [r for r in spans if r["type"] in SEED_TYPES]
    riders = [r for r in spans if r["type"] not in SEED_TYPES]
    clusters = []
    for r in seeds:
        if clusters and r["t0"] <= clusters[-1]["t1"] + JOIN:
            c = clusters[-1]
            c["t1"] = max(c["t1"], r["t1"])
            c["members"].append(r)
        else:
            clusters.append({"t0": r["t0"], "t1": r["t1"], "members": [r]})
    out = []
    for c in clusters:
        stack = [c]
        while stack:
            c = stack.pop()
            if c["t1"] - c["t0"] <= MAX_LEN:
                out.append(c)
                continue
            i0, i1 = level.i(c["t0"] + 0.3), level.i(c["t1"] - 0.3)
            k = i0 + int(np.argmin(level.db[i0:i1]))
            t = k * D.HOP
            left = [m for m in c["members"] if (m["t0"] + m["t1"]) / 2 < t]
            right = [m for m in c["members"] if (m["t0"] + m["t1"]) / 2 >= t]
            if not left or not right:
                out.append(c)
                continue
            stack.append({"t0": c["t0"], "t1": t, "members": left})
            stack.append({"t0": t, "t1": c["t1"], "members": right})
    out.sort(key=lambda c: c["t0"])
    for r in riders:
        for c in out:
            if r["t0"] < c["t1"] and r["t1"] > c["t0"]:
                c["members"].append(r)
                break
    starts = np.array([w["start"] for w in ws])
    for n, c in enumerate(out):
        c["id"] = n
        c["t0"], c["t1"] = round(c["t0"], 3), round(c["t1"], 3)
        c["free"] = D.free_part(mask, c["t0"], c["t1"])
        c["in_word"] = any(w["start"] <= (c["t0"] + c["t1"]) / 2 <= w["end"] for w in ws)
        j = int(np.searchsorted(starts, c["t0"]))
        prev, nxt = (ws[j - 1] if j > 0 else None), (ws[j] if j < len(ws) else None)
        c["ctx"] = f"{prev['text'] if prev else ''} … {nxt['text'] if nxt else ''}"
        c["srcs"] = sorted({m["src"] for m in c["members"]})
        c["types"] = sorted({m["type"] for m in c["members"]})
    dst = J.save_json(cfg["dir"] / "candidates.json",
                      {"name": cfg["name"], "detectors": names, "n": len(out), "candidates": out})
    lens = np.array([c["t1"] - c["t0"] for c in out]) if out else np.zeros(1)
    print(f"{len(seeds)} seed spans -> {len(out)} candidates; free {sum(c['free'] is not None for c in out)}, "
          f"in-word {sum(c['in_word'] for c in out)}; length median {np.median(lens):.2f} s, p90 {np.percentile(lens, 90):.2f} s")
    by = {}
    for c in out:
        k = "+".join(c["srcs"])
        by[k] = by.get(k, 0) + 1
    for k, v in sorted(by.items(), key=lambda x: -x[1])[:15]:
        print(f"  {v:4d}  {k}")
    strata = {}
    for c in out:
        s = J.stratum(c, cfg["weak_euh"])
        strata[s] = strata.get(s, 0) + 1
    print("strata:", ", ".join(f"{s} {strata.get(s, 0)}" for s in cfg["strata"]))
    print(dst)


if __name__ == "__main__":
    main()
