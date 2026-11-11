"""Caption text for one clip from up to three independent transcripts; disagreements are decided by an agent.

usage: python stt_reconcile.py [-c clips.json] ID [--paid | --two-sources]   -> <clip>/stt_review.json + stt_review.md
       (agent writes <clip>/stt_fixes.json, see SKILL.md)
       python stt_reconcile.py [-c clips.json] ID --apply                    -> <clip>/words.json + text_fixes.md

Sources:
- reference = the episode Words file ("words", shared Words format): its word timings drive the captions;
- captions  = optional second transcript ("captions"): YouTube auto-captions .vtt (word-timed tags), any .vtt/.srt
  (cue text spread over the cue), or another Words file;
- OpenAI gpt-4o-transcribe on the clip's own audio: text only, one PAID call per clip, cached forever in
  <clip>/openai_stt.json (never deleted, never re-requested). Needs --paid the first time; env OPENAI_API_KEY,
  OPENAI_BASE_URL (default https://api.openai.com/v1). --two-sources skips it.
Hunks = spans where the reference differs from the captions (or from OpenAI when there are no captions). Comparison
ignores case, punctuation and fillers (vc_lang.COMPARE_IGNORE). Do not re-run a review once stt_fixes.json exists:
it renumbers hunks (run_batch skips it on existence).
"""
import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vc_config import FFMPEG, Batch, load_words, norm  # noqa: E402
from vc_lang import table  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
OPENAI_MODEL = "gpt-4o-transcribe"


def ts(s):
    s = s.replace(",", ".")
    parts = s.split(":")
    if len(parts) == 2:
        parts = ["0"] + parts
    h, m, rest = parts
    return int(h) * 3600 + int(m) * 60 + float(rest)


def youtube_words(vtt):
    """Word start times from YouTube auto-caption VTT (<hh:mm:ss.mmm><c> word</c> tags); first word = cue start."""
    out, seen = [], set()
    cue_start = None
    for line in open(vtt, encoding="utf-8"):
        line = line.rstrip("\n")
        m = re.match(r"([0-9]{2}:[0-9]{2}:[0-9]{2}[.][0-9]+) --> ", line)
        if m:
            cue_start = ts(m.group(1))
            continue
        if "<c>" not in line or cue_start is None:
            continue
        first = line.split("<", 1)[0].strip()
        items = [(cue_start, first)] if first else []
        items += [(ts(a), b.strip()) for a, b in re.findall(r"<([0-9]{2}:[0-9]{2}:[0-9]{2}[.][0-9]+)><c>([^<]*)</c>", line)]
        for t, w in items:
            if w and (round(t, 2), w) not in seen:
                seen.add((round(t, 2), w))
                out.append({"text": w, "start": t})
    return sorted(out, key=lambda w: w["start"])


def cue_words(path):
    """Plain .srt/.vtt cues -> words, each cue's time shared by character count."""
    out, cue = [], None
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        m = re.match(r"([\d:.,]+)\s+-->\s+([\d:.,]+)", line)
        if m:
            cue = [ts(m.group(1)), ts(m.group(2)), []]
            out.append(cue)
        elif line and cue is not None and not line.isdigit():
            cue[2] += re.sub(r"<[^>]+>", "", line).split()
    words = []
    for a, b, toks in out:
        total = sum(len(t) for t in toks) or 1
        acc = a
        for t in toks:
            words.append({"text": t, "start": round(acc, 3)})
            acc += (b - a) * len(t) / total
    return sorted(words, key=lambda w: w["start"])


def caption_words(path):
    if path.suffix.lower() == ".json":
        return [{"text": w["text"], "start": w["start"]} for w in load_words(path)]
    text = path.read_text(encoding="utf-8")
    return youtube_words(path) if "<c>" in text else cue_words(path)


def clip_words(words, segs, fps, end_key=True):
    """Words inside the kept segments, mapped to clip time."""
    out = []
    for s in segs:
        a, b, r = s["in_f"] / fps, s["out_f"] / fps, s["rec_f"] / fps
        for w in words:
            ok = w["start"] >= a - 0.02 and (w["end"] <= b + 0.05 if end_key else w["start"] < b - 0.05)
            if ok:
                cw = dict(w, start=round(w["start"] - a + r, 3))
                if "end" in w:
                    cw["end"] = round(min(w["end"], b) - a + r, 3)
                out.append(cw)
    return out


def clip_audio(ep, segs, fps, path):
    """Clip audio (cleaned track, else the video's audio) as 16 kHz mono WAV for transcription."""
    parts = "".join(f"[0:a]atrim=start={s['in_f'] / fps:.4f}:end={s['out_f'] / fps:.4f},asetpts=PTS-STARTPTS[a{i}];"
                    for i, s in enumerate(segs))
    cat = "".join(f"[a{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=0:a=1[o]"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(ep["audio_src"]), "-filter_complex", parts + cat,
                    "-map", "[o]", "-ar", "16000", "-ac", "1", str(path)], check=True)


def openai_text(wav, prompt, language, model=OPENAI_MODEL):
    """One paid OpenAI transcription call: $OPENAI_BASE_URL/audio/transcriptions."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise SystemExit("OPENAI_API_KEY is not set")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    base = base[:-1] if base.endswith("/") else base
    b = "----" + uuid.uuid4().hex
    fields = {"model": model, "response_format": "json", "prompt": prompt}
    if language:
        fields["language"] = language.split("-")[0]
    body = b"".join(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode() for k, v in fields.items())
    body += (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"clip.wav\"\r\n"
             f"Content-Type: audio/wav\r\n\r\n").encode() + Path(wav).read_bytes() + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(base + "/audio/transcriptions", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}",
                                          "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)["text"]


def prompt_for(batch, ep, clip):
    """Transcription prompt: config transcription.prompt template ({people} {topic} {vocabulary}) or a plain default."""
    tr = batch.cfg.get("transcription", {})
    vocab = ", ".join(tr.get("vocabulary", []) + ep.get("vocabulary", []))
    tpl = ep.get("prompt") or tr.get("prompt")
    if tpl:
        return tpl.format(people=ep.get("people", ""), topic=clip.get("topic", ""), vocabulary=vocab).strip()
    parts = [tr.get("context", ""), ep.get("people", ""), clip.get("topic", ""), f"Vocabulary: {vocab}." if vocab else ""]
    return " ".join(p.strip() for p in parts if p and p.strip())


def tokens_keep(words, ignore):
    """(index, normalized) for comparable tokens (fillers dropped)."""
    return [(i, norm(w["text"])) for i, w in enumerate(words) if norm(w["text"]) and norm(w["text"]) not in ignore]


def align(a, b):
    return difflib.SequenceMatcher(None, [x for _, x in a], [x for _, x in b], autojunk=False).get_opcodes()


def review(batch, clip_id, paid=False, two_sources=False):
    ep, clip = batch.episode_of(clip_id), batch.clip(clip_id)
    fps, segs, d = ep["fps"], batch.clip_segments(clip_id), batch.clip_dir(clip_id)
    ignore = table("COMPARE_IGNORE", ep["language"])
    g = clip_words(batch.words(ep), segs, fps)
    y = clip_words(caption_words(ep["captions"]), segs, fps, end_key=False) if ep.get("captions") else []
    cache = d / "openai_stt.json"
    o_text = ""
    if cache.exists():
        o_text = json.load(open(cache, encoding="utf-8"))["text"]
    elif not two_sources:
        if not paid:
            print(f"{clip_id}: needs one paid OpenAI transcription call (cached afterwards). Ask, then rerun with "
                  "--paid, or use --two-sources to skip it.")
            sys.exit(3)
        wav = d / "clip_16k.wav"
        clip_audio(ep, segs, fps, wav)
        vol = subprocess.run([FFMPEG, "-hide_banner", "-i", str(wav), "-af", "volumedetect", "-f", "null", "-"],
                             capture_output=True, text=True).stderr
        mean_db = float(vol.split("mean_volume:")[1].split("dB")[0]) if "mean_volume:" in vol else -99.0
        # a cleaned track that covers only some ranges is digital silence elsewhere: 10 paid calls once came back as
        # the prompt echoed over silence
        assert mean_db > -60, (f"{clip_id}: clip audio is silent ({mean_db} dB): the audio file does not cover these "
                               "ranges. Clean them first; no paid call made.")
        prompt = prompt_for(batch, ep, clip)
        cache.write_text(json.dumps({"model": OPENAI_MODEL, "prompt": prompt,
                                     "text": openai_text(wav, prompt, ep["language"])},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
        o_text = json.load(open(cache, encoding="utf-8"))["text"]
    if not y and not o_text:
        raise SystemExit(f"{clip_id}: only one transcript (no captions file, no OpenAI text): nothing to compare")
    o = [{"text": t} for t in o_text.split()]
    gk, yk, ok = tokens_keep(g, ignore), tokens_keep(y, ignore), tokens_keep(o, ignore)
    o_map = {}  # reference token pos -> OpenAI text over the aligned span
    for tag, i1, i2, j1, j2 in align(gk, ok):
        for i in range(i1, i2):
            o_map[i] = " ".join(o[ok[j][0]]["text"] for j in range(j1, j2)) if tag != "equal" else o[ok[j1 + i - i1][0]]["text"]
    other, okeys = (y, yk) if y else (o, ok)
    hunks = []
    for tag, i1, i2, j1, j2 in align(gk, okeys):
        if tag == "equal":
            continue
        gi0 = gk[i1][0] if i1 < len(gk) else len(g)
        gi1 = gk[i2 - 1][0] + 1 if i2 > i1 else gi0
        hunks.append({"hunk": len(hunks), "t": g[min(gi0, len(g) - 1)]["start"], "ref_idx": [gi0, gi1],
                      "ref": " ".join(w["text"] for w in g[gi0:gi1]) or "∅",
                      "captions": " ".join(other[okeys[j][0]]["text"] for j in range(j1, j2)) or "∅" if y else "—",
                      "openai": (" ".join(dict.fromkeys(o_map.get(i, "") for i in range(i1, i2) if o_map.get(i))) or "?")
                      if y else (" ".join(o[ok[j][0]]["text"] for j in range(j1, j2)) or "∅"),
                      "before": " ".join(w["text"] for w in g[max(0, gi0 - 8):gi0]),
                      "after": " ".join(w["text"] for w in g[gi1:gi1 + 8])})
    out = {"clip": clip_id, "segments": segs, "ref_words": g, "captions_text": " ".join(w["text"] for w in y),
           "openai_text": o_text, "hunks": hunks}
    (d / "stt_review.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    md = [f"# {clip_id} transcript review ({len(hunks)} hunks)", "",
          "| # | t | reference | captions | OpenAI | context |", "|---|---|---|---|---|---|"]
    for h in hunks:
        md.append(f"| {h['hunk']} | {h['t']:.1f} | {h['ref']} | {h['captions']} | {h['openai']} | "
                  f"…{h['before']} **[ ]** {h['after']}… |")
    (d / "stt_review.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))


def spread(new, t0, t1):
    """New words over [t0, t1], time shared by character count."""
    total = sum(len(w) for w in new) or 1
    out, acc = [], t0
    for w in new:
        dur = (t1 - t0) * len(w) / total
        out.append({"text": w, "start": round(acc, 3), "end": round(acc + dur, 3)})
        acc += dur
    return out


def remap(batch, clip_id, rv, words, log):
    """Words timed on the segments the review was made on -> current segments (edges.json changed a cut): back to
    source time, forward to the new clip time; words outside every new segment are dropped and logged."""
    old, new, fps = rv.get("segments"), batch.clip_segments(clip_id), batch.episode_of(clip_id)["fps"]
    strip = [{k: v for k, v in s.items() if k != "idx"} for s in new]
    if not old or [{k: v for k, v in s.items() if k != "idx"} for s in old] == strip:
        return words
    old = [dict(s, idx=s.get("idx", k)) for k, s in enumerate(old)]

    def to_src(t):
        """clip time -> (range index, source time); a word belongs to the range it was heard in, so a cold open
        replaying a later range maps back to its own range."""
        for s in old:
            if s["rec_f"] / fps - 1e-6 <= t <= (s["rec_f"] + s["out_f"] - s["in_f"]) / fps + 1e-6:
                return s["idx"], t - s["rec_f"] / fps + s["in_f"] / fps, s
        return None, None, None

    def to_clip(idx, src):
        for s in new:
            if s["idx"] == idx and s["in_f"] / fps - 1e-6 <= src < s["out_f"] / fps:
                return s, src - s["in_f"] / fps + s["rec_f"] / fps
        return None, None

    out = []
    for w in words:
        idx, a, oseg = to_src(w["start"])
        seg, t0 = to_clip(idx, a) if a is not None else (None, None)
        if seg is None:
            log.append(f"- edge fix: « {w['text']} » ({w['start']:.2f} s) now outside the cut, dropped")
            continue
        e_idx, e_src, _ = to_src(w["end"])
        e_src = e_src if e_idx == idx and e_src is not None else oseg["out_f"] / fps  # word runs to its cut
        end = min(e_src, seg["out_f"] / fps) - seg["in_f"] / fps + seg["rec_f"] / fps
        out.append(dict(w, start=round(t0, 3), end=round(max(end, t0 + 0.04), 3)))
    log.append(f"- edge fix: words remapped onto {len(new)} segments (edges.json)")
    return out


def apply(batch, clip_id, clean_repeats=True):
    """Reference words + agent fixes -> words.json (clip time, shared Words format, source = assets/master.mp4).
    Insertions take time from the previous word's second half when there is no gap. clean_repeats: immediate repeats
    ("la la la tarte", "un un un pour", "je pense je pense") collapse to one, time merged, each logged; not across
    . ? ! and exact doubles of vc_lang.KEEP_DOUBLE words stay."""
    ep, d = batch.episode_of(clip_id), batch.clip_dir(clip_id)
    keep_double = table("KEEP_DOUBLE", ep["language"])
    rv = json.load(open(d / "stt_review.json", encoding="utf-8"))
    raw = json.load(open(d / "stt_fixes.json", encoding="utf-8"))
    fixes = {f["hunk"]: f["text"] for f in raw if "hunk" in f}
    g = [{k: w[k] for k in ("text", "start", "end", "type") if k in w} for w in rv["ref_words"]]
    log = ["# Text fixes " + clip_id, "", "Sources: reference words (timing), captions, OpenAI.", ""]
    edits = [h for h in rv["hunks"] if h["hunk"] in fixes]
    for k, f in enumerate(x for x in raw if "word" in x or "words" in x):
        i0, i1 = (f["word"], f["word"] + 1) if "word" in f else tuple(f["words"])
        key = f"w{k}"
        fixes[key] = f["text"]
        edits.append({"hunk": key, "t": g[i0]["start"], "ref_idx": [i0, i1], "ref": " ".join(w["text"] for w in g[i0:i1]),
                      "captions": "(word fix)", "openai": f.get("why", "")})
    spans = sorted((h["ref_idx"] for h in edits), key=lambda x: x[0])
    for a, b in zip(spans, spans[1:]):
        assert a[1] <= b[0] and a != b, f"overlapping fixes {a} {b}"
    for h in sorted(edits, key=lambda h: -h["ref_idx"][0]):  # right to left so indices stay valid
        i0, i1 = h["ref_idx"]
        new = fixes[h["hunk"]].split()
        if i1 > i0:
            g[i0:i1] = spread(new, g[i0]["start"], g[i1 - 1]["end"])
        else:
            prev = g[i0 - 1] if i0 else None
            t1 = g[i0]["start"] if i0 < len(g) else (prev["end"] + 0.3 if prev else 0.3)
            t0 = prev["end"] if prev else max(0.0, t1 - 0.3)
            if t1 - t0 < 0.1 and prev:
                t0 = round((prev["start"] + prev["end"]) / 2, 3)
                prev["end"] = t0
            g[i0:i0] = spread(new, t0, t1)
        log.append(f"- {h['t']:.1f} s: « {h['ref']} » → « {fixes[h['hunk']]} » (captions « {h['captions']} », "
                   f"OpenAI « {h['openai']} »)")
    out = []
    if clean_repeats:
        i = 0
        while i < len(g):
            j, n = i, 1
            for n in (3, 2, 1):  # n-word phrase repeated right after itself
                a = [norm(w["text"]) for w in g[i:i + n]]
                # not across a sentence end: « …parachute, tu sais. Tu sais, lui… » is two uses, not a stutter
                if (len(a) == n and [norm(w["text"]) for w in g[i + n:i + 2 * n]] == a and all(a)
                        and not re.search(r"[.?!]$", g[i + n - 1]["text"])
                        and not (n == 1 and a[0] in keep_double
                                 and (i + 2 >= len(g) or norm(g[i + 2]["text"]) != a[0])
                                 and (i == 0 or norm(g[i - 1]["text"]) != a[0]))):
                    j = i + n
                    while [norm(w["text"]) for w in g[j + n:j + 2 * n]] == a:
                        j += n
                    break
            if j > i:  # keep the last occurrence, start it at the first one
                kept = [dict(w) for w in g[j:j + n]]
                kept[0]["start"] = g[i]["start"]
                log.append(f"- {g[i]['start']:.1f} s: stutter « {' '.join(w['text'] for w in g[i:j + n])} » → "
                           f"« {' '.join(w['text'] for w in kept)} »")
                out += kept
                i = j + n
            else:
                out.append(g[i])
                i += 1
    else:
        out = g
    out = remap(batch, clip_id, rv, out, log)
    doc = {"source": "assets/master.mp4", "language": ep["language"], "model": "reconciled", "time_base": "source",
           "words": [dict(w, type=w.get("type", "word")) for w in out]}
    (d / "words.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "text_fixes.md").write_text("\n".join(log) + "\n", encoding="utf-8")
    print("\n".join(log[4:]))
    print(f"{clip_id}: {len(fixes)} fixes applied, {len(out)} words -> {d / 'words.json'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-c", "--config", help="batch config (default $VC_CONFIG or ./clips.json)")
    ap.add_argument("id", help="clip id")
    ap.add_argument("--apply", action="store_true", help="stt_fixes.json -> words.json")
    ap.add_argument("--paid", action="store_true", help="allow the one paid OpenAI call when not cached")
    ap.add_argument("--two-sources", action="store_true", help="skip OpenAI (reference + captions only)")
    a = ap.parse_args()
    batch = Batch(a.config)
    if a.apply:
        apply(batch, a.id)
    else:
        review(batch, a.id, paid=a.paid, two_sources=a.two_sources)
