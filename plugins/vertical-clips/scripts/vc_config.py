"""Batch config shared by every vertical-clips script: reads clips.json (+ clips.local.json beside it, which overrides
it), resolves episode paths, each clip's kept source ranges, per-clip edge fixes and tool paths.

Kept ranges per clip, first match wins: "cuts" (a Cut list file, shared format) · "ranges" ([[in, out], …] source
seconds) · "in"/"out" (or text anchors "from_text"/"to_text") minus "trims", after "prepend" ranges. Frames =
floor(in * fps), ceil(out * fps). <clip>/edges.json then shrinks, drops or splits ranges (see SKILL.md).

Stdlib only. Imported by the other scripts; also a CLI to check a config and pick clips:
  python vc_config.py [-c clips.json] ranges [IDS…]            kept ranges + first/last words of each clip
  python vc_config.py [-c clips.json] transcript EP [FROM TO]  an episode's words as timed lines (to choose clips)
Config path: -c, else $VC_CONFIG, else ./clips.json. Format: references/config.md.
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
OUT_W, OUT_H = 1080, 1920
# Reels / Shorts / TikTok overlay UI leaves y 168-1330 px clear on a 1080x1920 frame (account bar on top, post text,
# buttons and audio row below).
SAFE_Y = (168, 1330)
# Caption centre line = top-panel height of a stacked shot. 1040 measured on a 2-person podcast batch: singles' chins
# end ~850-910 px (p50-p90 of 279 shots); 1000 put captions on the host's mouth.
DEFAULT_SEAM = 1040
YUNET_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
SCRIPTS = Path(__file__).resolve().parent


def tool(name, env):
    """Executable for an optional CLI: $ENV, else PATH, else None."""
    return os.environ.get(env) or shutil.which(name)


def raea_bin():
    found = tool("raea", "RAEA")
    if found:
        return found
    home = Path.home() / ".raea" / "bin"
    for n in ("raea.exe", "raea"):
        if (home / n).exists():
            return str(home / n)
    return None


def merge(a, b):
    """b over a; dicts merge key by key, everything else is replaced."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = merge(a.get(k), v) if k in a else v
        return out
    return b


def norm(t):
    t = t.lower().replace("’", "'").replace("œ", "oe")
    return re.sub(r"[^\w']+", "", t).strip("'")


def jload(p, default=None):
    try:
        return json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return default


def fps_arg(fps):
    """ffmpeg/HyperFrames rate string: 30 -> "30", 29.97 -> "30000/1001"."""
    f = float(fps)
    if abs(f - round(f)) < 1e-6:
        return str(int(round(f)))
    for n in (24, 30, 48, 60, 120):
        if abs(f - n * 1000 / 1001) < 2e-3:
            return f"{n * 1000}/1001"
    return f"{f:.6f}"


def probe_fps(video):
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                        "-of", "csv=p=0", str(video)], capture_output=True, text=True, check=True).stdout.strip()
    a, _, b = r.partition("/")
    f = float(a) / float(b or 1)
    return int(round(f)) if abs(f - round(f)) < 1e-6 else round(f, 5)


def load_words(path):
    """Words file (shared format: {"words": [...]}) or a bare list -> words with text, start, end (+ type, speaker).
    Events (laughter, cough) are dropped: they are not caption text."""
    data = json.load(open(path, encoding="utf-8"))
    words = data["words"] if isinstance(data, dict) else data
    return [w for w in words if str(w.get("text", "")).strip() and w.get("type", "word") != "event"]


class Batch:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("VC_CONFIG") or "clips.json").resolve()
        if not self.path.exists():
            raise SystemExit(f"config not found: {self.path} (write clips.json, see references/config.md)")
        self.root = self.path.parent
        cfg = json.load(open(self.path, encoding="utf-8"))
        local = self.path.with_name(self.path.stem + ".local.json")
        if local.exists():
            cfg = merge(cfg, json.load(open(local, encoding="utf-8")))
        clips = cfg.get("clips", [])
        if isinstance(clips, str):  # sibling clip-list file
            data = json.load(open(self.resolve(clips), encoding="utf-8"))
            clips = data["clips"] if isinstance(data, dict) else data
        self.cfg = cfg
        self.clips = {c["id"]: c for c in clips}
        self.episodes = cfg.get("episodes", {})
        self.out = self.resolve(cfg.get("out", "clips"))
        self.language = cfg.get("language", "en")
        lay = cfg.get("layout", {})
        self.seam = int(lay.get("seam", DEFAULT_SEAM))
        self.low_face = int(lay.get("low_face", self.seam - 124))  # 2-line caption top ~ seam - 92, minus margin
        self.looks = cfg.get("looks", ["signal"])
        self.hook_s = float(cfg.get("hook_s", 2.8))
        self.cache_dir = Path(os.environ.get("VERTICAL_CLIPS_CACHE") or cfg.get("cache_dir")
                              or Path.home() / ".cache" / "vertical-clips")
        self._fps = {}

    def resolve(self, p):
        if p in (None, ""):
            return None
        p = Path(os.path.expandvars(str(p))).expanduser()
        return p if p.is_absolute() else (self.root / p)

    def clip(self, cid):
        if cid not in self.clips:
            raise SystemExit(f"{cid}: not in {self.path.name} clips")
        return self.clips[cid]

    def episode(self, key):
        if key not in self.episodes:
            raise SystemExit(f"episode {key!r} not in {self.path.name} episodes")
        e = dict(self.episodes[key], key=key)
        for k in ("video", "words", "captions", "audio", "lut"):
            e[k] = self.resolve(e.get(k))
        if not e.get("video") or not e.get("words"):
            raise SystemExit(f"episode {key}: 'video' and 'words' are required")
        e["splices"] = [dict(s, file=self.resolve(s["file"])) for s in e.get("words_splices", [])]
        if not e.get("fps"):
            if key not in self._fps:
                self._fps[key] = probe_fps(e["video"])
            e["fps"] = self._fps[key]
        e["audio_src"] = e["audio"] or e["video"]  # speech-cleaned track, else the video's own audio
        e["language"] = e.get("language", self.language)
        e.setdefault("main_side", "left")
        e.setdefault("people", "")
        return e

    def episode_of(self, cid):
        c = self.clip(cid)
        return self.episode(c.get("episode") or cid.split("-")[0])

    def clip_dir(self, cid):
        d = self.out / cid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def clip_ids(self):
        return list(self.clips)

    def looks_for(self, cid):
        env = os.environ.get("LOOKS")
        return env.split() if env else self.clip(cid).get("looks", self.looks)

    def words(self, ep):
        """Episode words (source seconds), dropout splices applied: main-pass words starting in [from, to) are
        replaced by the splice file's words whose shifted start falls in that window."""
        words = load_words(ep["words"])
        for s in ep["splices"]:
            lo, hi, off = s["from"], s["to"], s.get("offset", 0.0)
            words = [w for w in words if not lo <= w["start"] < hi]
            words += [dict(w, start=w["start"] + off, end=w["end"] + off)
                      for w in load_words(s["file"]) if lo <= w["start"] + off < hi]
        return sorted(words, key=lambda w: w["start"])

    def anchor(self, cid, ep, phrase, after=0.0, near=None, end=False, pad=0.0):
        """Source time of a phrase in the episode words: start of its first word minus pad, or end of its last word
        plus pad (end=True); the pad never reaches into the neighbouring word (another speaker's onset).
        near = pick the occurrence closest to this time; else the first one at or after `after`."""
        words = self.words(ep)
        toks = [norm(w["text"]) for w in words]
        want = [t for t in (norm(x) for x in phrase.split()) if t]
        hits = [i for i in range(len(toks) - len(want) + 1) if toks[i:i + len(want)] == want]
        if near is not None:
            hits.sort(key=lambda i: abs(words[i]["start"] - near))
        else:
            hits = [i for i in hits if words[i]["start"] >= after - 0.01]
        if not hits:
            raise SystemExit(f"{cid}: text anchor not found in {ep['words'].name}: « {phrase} »")
        i = hits[0]
        if end:
            j = i + len(want) - 1
            t = words[j]["end"] + pad
            return min(t, words[j + 1]["start"] - 0.05) if j + 1 < len(words) else t
        t = words[i]["start"] - pad
        return max(t, words[i - 1]["end"] + 0.02) if i else max(0.0, t)

    def clip_ranges(self, cid):
        """Kept source ranges in clip order (seconds), before edges.json."""
        c, ep = self.clip(cid), self.episode_of(cid)
        if c.get("cuts"):
            cl = json.load(open(self.resolve(c["cuts"]), encoding="utf-8"))
            return [(s["in"], s["out"]) for s in cl["segments"]]
        if c.get("ranges"):
            return [tuple(r) for r in c["ranges"]]
        a, b = c.get("in"), c.get("out")
        if a is None:
            a = self.anchor(cid, ep, c["from_text"], near=c.get("near"), pad=c.get("lead", 0.15))
        if b is None:
            b = self.anchor(cid, ep, c["to_text"], after=a, end=True, pad=c.get("tail", 0.3))
        out = [tuple(r) for r in (c.get("prepend") or [])]
        for t0, t1 in sorted(c.get("trims") or []):
            if t0 > a:
                out.append((a, t0))
            a = max(a, t1)
        if b > a:
            out.append((a, b))
        return out

    def clip_segments(self, cid):
        """Kept ranges as frames at the episode fps, in clip order: [{in_f, out_f, rec_f (clip-relative), idx}].
        <clip>/edges.json (written by the agent, see SKILL.md) is applied on top: [{"seg": i, "in_s"|"out_s": s} |
        {"seg": i, "drop": true} | {"seg": i, "cut": [a_s, b_s]}, "why": …], seg = index in the configured order."""
        fps = self.episode_of(cid)["fps"]
        segs = [{"in_f": math.floor(a * fps), "out_f": math.ceil(b * fps), "idx": k}
                for k, (a, b) in enumerate(self.clip_ranges(cid))]
        ef = self.out / cid / "edges.json"
        if ef.exists():
            cuts = []
            for e in json.load(open(ef, encoding="utf-8")):
                s = segs[e["seg"]]
                if e.get("drop"):
                    s["drop"] = True
                if "in_s" in e:
                    s["in_f"] = round(e["in_s"] * fps)
                if "out_s" in e:
                    s["out_f"] = round(e["out_s"] * fps)
                if "cut" in e:
                    cuts.append((e["seg"], round(e["cut"][0] * fps), round(e["cut"][1] * fps)))
                assert s["out_f"] > s["in_f"], (cid, e)
            segs = [s for s in segs if not s.get("drop")]
            for k, a, b in sorted(cuts, key=lambda c: -c[1]):
                i = next(n for n, s in enumerate(segs) if s["idx"] == k and s["in_f"] < a < b < s["out_f"])
                s = segs[i]
                segs[i:i + 1] = [dict(s, out_f=a), dict(s, in_f=b)]
        rec, out = 0, []
        for s in segs:
            out.append({"in_f": s["in_f"], "out_f": s["out_f"], "rec_f": rec, "idx": s["idx"]})
            rec += s["out_f"] - s["in_f"]
        return out

    def write_cuts(self, cid, segs):
        """<clip>/<id>.cuts.json in the shared Cut list format (other plugins can rebuild the clip from it)."""
        ep = self.episode_of(cid)
        fps = ep["fps"]
        cl = {"fps": fps, "sources": {"A": str(ep["video"])},
              "segments": [{"src": "A", "in": round(s["in_f"] / fps, 4), "out": round(s["out_f"] / fps, 4),
                            "why": f"{cid} range {s['idx']}"} for s in segs]}
        (self.clip_dir(cid) / f"{cid}.cuts.json").write_text(json.dumps(cl, indent=1, ensure_ascii=False), encoding="utf-8")


def cli():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("ranges", help="kept ranges + first/last words of each clip")
    r.add_argument("ids", nargs="*")
    t = sub.add_parser("transcript", help="an episode's words as timed lines")
    t.add_argument("episode")
    t.add_argument("start", nargs="?", type=float, default=0.0)
    t.add_argument("end", nargs="?", type=float, default=1e9)
    a = ap.parse_args()
    b = Batch(a.config)
    if a.cmd == "ranges":
        for cid in a.ids or b.clip_ids():
            ep = b.episode_of(cid)
            fps, words = ep["fps"], b.words(ep)
            segs = b.clip_segments(cid)
            total = sum(s["out_f"] - s["in_f"] for s in segs) / fps
            print(f"{cid} ({ep['key']}, {total:.1f} s)")
            for s in segs:
                t0, t1 = s["in_f"] / fps, s["out_f"] / fps
                inside = [w["text"] for w in words if t0 - 0.02 <= w["start"] < t1]
                print(f"  {t0:9.3f}-{t1:9.3f}  « {' '.join(inside[:6])} … {' '.join(inside[-6:])} »")
    else:
        ep = b.episode(a.episode)
        line, t0 = [], None
        for w in b.words(ep):
            if not a.start <= w["start"] < a.end:
                continue
            t0 = w["start"] if t0 is None else t0
            line.append(w["text"])
            if re.search(r"[.?!]$", w["text"]) or w["end"] - t0 > 12:
                print(f"[{t0:8.2f}] {' '.join(line)}")
                line, t0 = [], None
        if line:
            print(f"[{t0:8.2f}] {' '.join(line)}")


if __name__ == "__main__":
    cli()
