# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=2"]
# ///
"""Bake-off step 4: score every detector setting against the human answer key -> <job>/score.md + score.json.

Unit = one labelled candidate (candidates.json + key.json). Truth: breath / euh present when the labeller marked it;
"speech" = voice that must never be cut. Claim rule: a setting claims a type on a candidate when one of its spans of
that type overlaps the band by >= half of the shorter of span and band (and >= 20 ms). A span that only grazes the
band (any overlap >= 20 ms, e.g. ending 50 ms inside the next word's band) is a "touch", reported apart. Where the
labeller dragged the band edges, the dragged band is the truth.

Settings swept:
  probs_<det>.npz   variant x prompt (each breath / euh prompt, plus "any_<type>" = max over a detector's prompts of
                    one type) x every meta.thrs threshold x word filter off/on (filt=1: only the span's part outside
                    the word mask counts, what an editor could cut between words)
  spans_<det>.json  as built: one setting per file, or the named subsets in its "settings" (references/bakeoff_files.md)
  Words fillers     the transcript's type "filler" entries as an euh detector
Thresholds stay >= each detector's union threshold: below it a setting would claim spans nobody labelled.

Weights: labels are a stratified sample, so each labelled candidate counts N_s / n_s for its stratum s (bakeoff.json
"strata"). P / R / F1 are stratum-weighted; tp / fp / fn / speech counts are raw. Recall = share of the breaths / euh
the union found. Holdout: 25 % of the labels per stratum (seeded) never take part in picking. Pick per family and type =
best dev F1 (then P) with ZERO speech claims on dev, reported on dev, holdout and all labels. Votes: k-of-N over the
picked breath settings. Edge error: median |start| / |end| between the claiming span and the labeller's dragged band,
breath-only candidates. "s-tail FP" = false claims starting within 80 ms of a word ending in s / x / z (a word-final
« s » hiss read as breath).

usage: uv run score.py <job>
"""
import json
import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ears_dsp as D  # noqa: E402
import ears_job as J  # noqa: E402
from union import load_probs  # noqa: E402

MIN_OV = 0.02
TYPES = ("breath", "euh")


def ov(a0, a1, b0, b1):
    return min(a1, b1) - max(a0, b0)


class Setting:
    def __init__(self, name, family, types, claims, meta):
        self.name, self.family, self.types, self.claims, self.meta = name, family, types, claims, meta

    def claim(self, c, typ, touch=False):
        """Best-overlapping span of this type that claims the band (touch=True: any overlap >= MIN_OV)."""
        def ok(s):
            o = ov(s[0], s[1], c["t0"], c["t1"])
            return o >= MIN_OV and (touch or o >= 0.5 * min(s[1] - s[0], c["t1"] - c["t0"]))
        hits = [s for s in self.claims if s[2] == typ and ok(s)]
        return max(hits, key=lambda s: ov(s[0], s[1], c["t0"], c["t1"])) if hits else None


class Vote:
    """Claims when >= need of the member settings claim."""

    def __init__(self, members, need):
        self.members, self.need = members, need

    def claim(self, c, typ, touch=False):
        hits = [h for h in (m.claim(c, typ, touch) for m in self.members) if h]
        return hits[0] if len(hits) >= self.need else None


def spans_to_claims(sp, typ, mask, filt):
    out = []
    for a, b, _ in sp:
        if filt:
            f = D.free_part(mask, a, b)
            if f is None:
                continue
            a, b = f
        out.append((a, b, typ))
    return out


def fixed_settings(name, d):
    rows = [r for r in d["rows"] if r["type"] in TYPES]
    subsets = d.get("settings") or {name: {}}
    out = []
    for sname, spec in subsets.items():
        keep = [r for r in rows
                if (not spec.get("labels") or r.get("label", r["type"]) in spec["labels"])
                and r.get("label", r["type"]) not in spec.get("exclude_labels", [])
                and (not spec.get("types") or r["type"] in spec["types"])]
        types = spec.get("types") or sorted({r["type"] for r in keep})
        out.append(Setting(sname, spec.get("family", name), types,
                           [(float(r["t0"]), float(r["t1"]), r["type"]) for r in keep], {"filt": 0}))
    return out


def settings(cfg, mask, ws_all, model):
    S = []
    for name, kind in J.detectors(cfg):
        if kind == "spans":
            S += fixed_settings(name, J.read_json(cfg["dir"] / f"spans_{name}.json"))
            continue
        meta, arrays = load_probs(cfg["dir"] / f"probs_{name}.npz")
        hop, mf = meta["hop_s"], meta.get("min_frames", 1)
        for v in meta["variants"]:
            p = arrays[v]
            curves = [(q["name"], q["type"], p[i]) for i, q in enumerate(meta["prompts"]) if q["type"] in TYPES]
            for typ in TYPES:
                idx = [i for i, q in enumerate(meta["prompts"]) if q["type"] == typ]
                if len(idx) > 1:
                    curves.append((f"any_{typ}", typ, p[idx].max(0)))
            for prompt, typ, curve in curves:
                for thr in meta["thrs"]:
                    sp = D.spans(curve, thr, hop, mf)
                    for filt in (0, 1):
                        S.append(Setting(f"{name} {v} {prompt} thr={thr} filt={filt}", f"{name}_{typ}", [typ],
                                         spans_to_claims(sp, typ, mask, filt),
                                         {"variant": v, "prompt": prompt, "thr": thr, "filt": filt}))
    fil = [(w["start"], w["end"], "euh") for w in ws_all if w["type"] == "filler"]
    if fil:
        S.append(Setting(f"words fillers ({model or 'transcript'})", "words", ["euh"], fil, {"filt": 0}))
    return S


def evaluate(st, cands, ids, w, labels, typ, edges):
    tp = fp = fn = 0.0
    n = {"tp": 0, "fp": 0, "fn": 0, "speech_cut": 0, "speech_touch": 0, "s_tail_fp": 0}
    for i in ids:
        c, lab = cands[i], labels[i]
        if edges.get(i):  # the labeller's adjusted band = what the marks describe
            c = dict(c, t0=edges[i][0], t1=edges[i][1])
        truth = typ in lab
        hit = st.claim(c, typ)
        if truth and hit:
            tp += w[i]; n["tp"] += 1
        elif truth:
            fn += w[i]; n["fn"] += 1
        elif hit:
            fp += w[i]; n["fp"] += 1
            if c.get("prev_s"):
                n["s_tail_fp"] += 1
        if "speech" in lab and hit:
            n["speech_cut"] += 1
        elif "speech" in lab and st.claim(c, typ, touch=True):
            n["speech_touch"] += 1
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"p": round(p, 3), "r": round(r, 3), "f1": round(f1, 3), **n}


def boundary(st, ids, key):
    err0, err1 = [], []
    for i in ids:
        e = key[str(i)].get("edge")
        if not e or key[str(i)]["labels"] != ["breath"]:
            continue
        hit = st.claim({"t0": e[0], "t1": e[1]}, "breath")
        if hit:
            err0.append(abs(hit[0] - e[0]))
            err1.append(abs(hit[1] - e[1]))
    if not err0:
        return None
    return {"n": len(err0), "start_ms": round(1000 * float(np.median(err0))), "end_ms": round(1000 * float(np.median(err1)))}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    import argparse

    ap = argparse.ArgumentParser(description="Score every detector setting against key.json -> score.md / score.json.")
    ap.add_argument("job", help="bake-off job folder with candidates.json and key.json")
    ap.add_argument("--holdout", type=float, default=0.25, help="share of labels per stratum kept out of picking")
    ap.add_argument("--seed", type=int, default=5, help="holdout draw seed")
    a = ap.parse_args()
    cfg = J.load_job(a.job)
    d = cfg["dir"]
    kd = J.read_json(d / "key.json")["labels"]
    cl = J.read_json(d / "candidates.json")["candidates"]
    cands = {c["id"]: c for c in cl}
    labels = {int(i): set(v["labels"]) for i, v in kd.items() if v.get("labels") and int(i) in cands}
    if not labels:
        sys.exit(f"{d / 'key.json'}: no labels matching candidates.json yet")
    edges = {int(i): v["edge"] for i, v in kd.items() if v.get("edge")}
    ws_all, wd = J.words(cfg["words"])
    ws = J.speech_words(ws_all)
    level = D.Level(cfg["audio"])
    mask = D.attached_mask(level, ws)
    for c in cl:  # word-final « s » trap
        prev = [w for w in ws if c["t0"] - 0.08 <= w["end"] <= c["t0"] + 0.08]
        c["prev_s"] = any(J.norm(w["text"])[-1:] in ("s", "x", "z") for w in prev if J.norm(w["text"]))
    strata = list(cfg["strata"])
    strat = {c["id"]: J.stratum(c, cfg["weak_euh"]) for c in cl}
    strata += sorted({s for s in strat.values()} - set(strata))
    size = {s: sum(v == s for v in strat.values()) for s in strata}
    nlab = {s: sum(strat[i] == s for i in labels) for s in strata}
    w = {i: size[strat[i]] / nlab[strat[i]] for i in labels}
    rnd = random.Random(a.seed)
    hold = set()
    for s in strata:
        ids = sorted(i for i in labels if strat[i] == s)
        rnd.shuffle(ids)
        hold |= set(ids[:round(len(ids) * a.holdout)])
    dev = sorted(set(labels) - hold)
    hold = sorted(hold)
    allids = sorted(labels)

    S = settings(cfg, mask, ws_all, wd.get("model", ""))
    rows = []
    for st in S:
        for typ in st.types:
            rows.append({"setting": st.name, "family": st.family, "type": typ, **st.meta,
                         "dev": evaluate(st, cands, dev, w, labels, typ, edges),
                         "hold": evaluate(st, cands, hold, w, labels, typ, edges),
                         "all": evaluate(st, cands, allids, w, labels, typ, edges),
                         "edges": boundary(st, allids, kd) if typ == "breath" else None})
    fams = list(dict.fromkeys((r["family"], r["type"]) for r in rows))

    def pick(fam, typ):
        ok = [r for r in rows if r["family"] == fam and r["type"] == typ and r["dev"]["speech_cut"] == 0]
        return max(ok, key=lambda r: (r["dev"]["f1"], r["dev"]["p"])) if ok else None

    def best_any(fam, typ):
        ok = [r for r in rows if r["family"] == fam and r["type"] == typ]
        return max(ok, key=lambda r: (r["dev"]["f1"], -r["dev"]["speech_cut"])) if ok else None

    picks = {f"{fam}/{typ}": pick(fam, typ) for fam, typ in fams}
    by = {st.name: st for st in S}
    breath = [(k.split("/")[0], by[v["setting"]]) for k, v in picks.items() if v and v["type"] == "breath"]
    if len(breath) > 1:
        fam_names = ", ".join(f for f, _ in breath)
        for need in range(1, len(breath) + 1):
            name = f"{need} of {len(breath)} ({fam_names})"
            v = Vote([s for _, s in breath], need)
            rows.append({"setting": name, "family": "vote", "type": "breath",
                         "dev": evaluate(v, cands, dev, w, labels, "breath", edges),
                         "hold": evaluate(v, cands, hold, w, labels, "breath", edges),
                         "all": evaluate(v, cands, allids, w, labels, "breath", edges), "edges": boundary(v, allids, kd)})
            picks[f"vote/{name}"] = rows[-1]

    ab = []  # raw vs high-passed, filter off / on, at each pick's threshold
    for k, r in picks.items():
        if not r or "thr" not in r:
            continue
        for x in rows:
            if (x["family"] == r["family"] and x["type"] == r["type"] and x.get("thr") == r["thr"]
                    and x.get("prompt") == r.get("prompt")):
                ab.append((k, x["variant"], x["filt"], x["all"]))

    pos = {t: sum(t in labels[i] for i in labels) for t in ("breath", "euh", "speech", "nothing", "noise")}
    J.save_json(d / "score.json", {"name": cfg["name"], "n_labels": len(labels), "dev": dev, "hold": hold,
                                   "weights": w, "size": size, "nlab": nlab, "positives": pos, "picks": picks,
                                   "rows": rows})
    L = [f"# Ears bake-off — {cfg['name']}", "",
         f"{len(labels)} labelled candidates ({len(dev)} dev / {len(hold)} holdout); marks: " +
         ", ".join(f"{k} {v}" for k, v in pos.items()) + ". Stratum sizes / labelled: " +
         ", ".join(f"{s} {size[s]}/{nlab[s]}" for s in strata) + ".", "",
         "Picked on dev (best F1 with zero speech claims), reported on dev, holdout and all labels. P/R/F1 "
         "stratum-weighted; tp/fp/fn/speech = raw counts on all labels. Recall is relative to what the union of "
         "detectors found.", "",
         "| Pick | Type | Setting | dev P / R / F1 | holdout P / R / F1 | all P / R / F1 | tp / fp / fn | speech cut / touch | s-tail FP | edge err start / end |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for k, r in picks.items():
        if not r:
            L.append(f"| {k} | | none passes the speech rule | | | | | | | |")
            continue
        dv, h, al, e = r["dev"], r["hold"], r["all"], r.get("edges")
        L.append(f"| {k} | {r['type']} | {r['setting']} | {dv['p']:.2f} / {dv['r']:.2f} / {dv['f1']:.2f} | "
                 f"{h['p']:.2f} / {h['r']:.2f} / {h['f1']:.2f} | {al['p']:.2f} / {al['r']:.2f} / {al['f1']:.2f} | "
                 f"{al['tp']} / {al['fp']} / {al['fn']} | {al['speech_cut']} / {al['speech_touch']} | {al['s_tail_fp']} | "
                 + (f"{e['start_ms']} / {e['end_ms']} ms (n={e['n']})" if e else "") + " |")
    L += ["", "## Best F1 per family ignoring the speech rule (what the rule costs)", "",
          "| Family | Type | Setting | all P / R / F1 | speech cut / touch |", "|---|---|---|---|---|"]
    for fam, typ in fams:
        r = best_any(fam, typ)
        if r:
            al = r["all"]
            L.append(f"| {fam} | {typ} | {r['setting']} | {al['p']:.2f} / {al['r']:.2f} / {al['f1']:.2f} | "
                     f"{al['speech_cut']} / {al['speech_touch']} |")
    if ab:
        L += ["", "## Raw vs high-passed 80 Hz, word filter off / on (all labels, picked threshold)", "",
              "| Pick | Audio | Filter | P / R / F1 | fp | speech cut | s-tail FP |", "|---|---|---|---|---|---|---|"]
        for k, v, filt, al in ab:
            L.append(f"| {k} | {v} | {'on' if filt else 'off'} | {al['p']:.2f} / {al['r']:.2f} / {al['f1']:.2f} | "
                     f"{al['fp']} | {al['speech_cut']} | {al['s_tail_fp']} |")
    p = d / "score.md"
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    print(p)


if __name__ == "__main__":
    main()
