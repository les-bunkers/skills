"""Check a finished cleanup job against its pass marks; prints one line per check, exit 1 when any check fails.

Reads <job>/manifest.json (+ split_quality.json, scan.json). Pass marks (references/job-files.md):
  lag        sources[].lag_samples all 0 (RE-USE vs original, 3 probes; None = window too short, ignored)
  loudness   sources[].master.played_after.I within 0.2 LU of the target; TP <= -1.5 dBTP (tp_ok)
  aac        sources[].master.aac_256k.TP <= -1.0 dBTP
  timelines  each timeline as it will play from the masters within 0.5 LU of the target, TP <= -1.0 dBTP
  split      split_quality.json wrong_share <= 4 % (reference runs 0.5-2.6 %)
  dropouts   scan.json in-cut dropouts: count only (listen to them on the A/B page)
Stdlib only.

usage: python check_job.py JOB
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip().splitlines()[-1])
    job = Path(sys.argv[1])
    man = json.loads((job / "manifest.json").read_text(encoding="utf-8"))
    target = man.get("target_lufs", -14.0)
    fails = 0

    def line(ok, what, detail):
        nonlocal fails
        fails += ok is False
        print(f"{'PASS' if ok else 'INFO' if ok is None else 'FAIL'}  {what:<58} {detail}")

    for s in man["sources"]:
        lags = [x for x in s.get("lag_samples", []) if x is not None]
        k = s["key"]
        line(all(x == 0 for x in lags), f"lag {k}", f"{s.get('lag_samples')}")
        m = s.get("master")
        if not m:
            line(False, f"master {k}", "missing: run the master step")
            continue
        pa, aac = m["played_after"], m["aac_256k"]
        line(abs(pa["I"] - target) <= 0.2 and pa["TP"] <= -1.5, f"loudness {k}",
             f"I {pa['I']:.2f} LUFS, TP {pa['TP']:.2f} dBTP, LRA {pa['LRA']:.1f} (gain {m['gain_db']:+.1f} dB, "
             f"plain {m['plain_gain_db']:+.1f})")
        line(aac["TP"] <= -1.0, f"aac 256k {k}", f"I {aac['I']:.2f} LUFS, TP {aac['TP']:.2f} dBTP")
    for name, t in (man.get("timelines") or {}).items():
        line(abs(t["I"] - target) <= 0.5 and t["TP"] <= -1.0, f"timeline {name[:48]}",
             f"I {t['I']:.2f} LUFS, TP {t['TP']:.2f} dBTP, LRA {t['LRA']:.1f}")
    q = job / "split_quality.json"
    if q.exists():
        for key, v in json.loads(q.read_text(encoding="utf-8")).items():
            if "wrong_share" in v:
                line(v["wrong_share"] <= 0.04, f"split {key}",
                     f"{v['wrong_share']:.1%} of {v['agreed_words']} agreed words in the wrong track, pitch "
                     f"{v['pitch_median_hz']}")
            else:
                line(None, f"split {key}", v.get("skipped", ""))
    sc = job / "scan.json"
    if sc.exists():
        for key, v in json.loads(sc.read_text(encoding="utf-8")).items():
            inc = [e for e in v["events"] if e["in_cut"]]
            line(None, f"dropouts {key}", f"{len(inc)} inside the cut, {len(v['events'])} in the regions"
                 + (f", deepest -{max(e['drop_db'] for e in inc):.0f} dB" if inc else ""))
    print(f"{fails} failed check(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
