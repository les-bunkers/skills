# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=2"]
# ///
"""Tighten a talking-head rough cut in the house edit style: breaths, fillers (« euh », "um") and dead air out, a spare
sentence-initial word (French « donc ») out where it comes off cleanly, approved phrase trims cut on clean edges, tight
pauses, J-cuts (sound 1-4 frames before picture) where the held picture stays silent, camera switches on V3.

in:  --project edit.json (sources: file, words, ears, fillers, scan, angles), --cuts rough.cuts.json (Cut list),
     overrides.json (keep_spare / drop_spare / drop_spans, word ids), style JSON (defaults = the house style below)
out: <out>.cuts.json (tight Cut list: parts with sound in/out + picture in/out, every cut, logs), <out>.srt (captions
     minus removed words, retimed), <out>.report.md (every cut with timeline time, spare-word, phrase-trim and filler
     tables, camera counts, voice kept because the transcript has no words there)
usage: uv run tighten.py --project cut/edit.json --cuts cut/rough.cuts.json --out cut/tight [--style S] [--overrides O]
       [--no-ears] [--no-fillers] [--no-glide] [--no-cameras] [--name "Timeline name"]
       uv run tighten.py --dump-style > cut/style.json      (write the defaults, then edit)

How it hears (engine.md has the measurements): the source audio, high-passed 80 Hz, 16 kHz, 30 ms windows every 10 ms.
Silence = under the take's floor (5th percentile) + 10 dB. Between two words, the sound running on from the word's
end (its tail, a final « s ») and into the next onset never counts; every other sound between silences is an event:
filler = one held voiced vowel; speech? = voiced with syllable dips (words the transcript missed: kept and listed);
breath = aperiodic, bright, >= 80 ms, quieter than speech; noise = the rest. A gap is cut when it holds a breath or a
filler, or runs silent past dead_air_s; the cut keeps keep_out_s / keep_in_s of silence after the tail / before the
onset. Breath spans from agent-ears (*.ears.json) relabel "noise", add quiet breaths and glued inhales. Timed fillers
(a second transcript that hears and times fillers, e.g. ElevenLabs Scribe via agent-ears) cut each filler between its
neighbours, on a clean dip, a soft dip, or inside the voice (sample-exact audio + 1-frame cross fade) when it glides
into the next word.
"""
# Ported from a private engine; behaviour is regression-tested on a 21-min French studio tour (engine.md).
import argparse
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import editlib as E  # noqa: E402

E.setup_stdout()
SR, HOP = 16000, 0.01
GLIDE_KIND = "filler (timed, in the voice)"

# House style. Every value carries the measurement or complaint that set it (engine.md § Style, measured).
DEFAULT_STYLE = {
    "_note": "Tighten style. Values = the house style; copy (tighten.py --dump-style), edit, pass with --style or "
             "edit.json \"style\". Language blocks hold speech-specific word lists.",
    "keep_out_s": 0.03,      # silence kept after the outgoing word's tail (editor's own cuts: median 0.03 s past floor+18)
    "keep_in_s": 0.02,       # silence kept before the incoming onset (median ~0 at floor+18)
    "dead_air_s": 0.4,       # silent gap longer than this is cut (editor's kept pauses: p95 0.48 s; "even more aggressive")
    "min_cut_s": 0.15,       # removals shorter than this are not worth a jump cut
    "j_frames": 2,           # J-cut length: next part's sound this many frames before its picture (editor: 30 % of cuts J 1-4, J2 most common)
    "spare_word_gap_s": 0.25,  # a spare word after a pause this long counts as sentence-initial
    "phrase_max_db": 30,     # a phrase-trim / tight filler edge still louder than floor + this once frame-snapped is kept
    "ears": True,            # use *.ears.json breath spans when the source has one
    "timed_fillers": True,   # use the timed-fillers transcript when the source has one
    "glide": True,           # timed filler running voiced into the next word: cut inside the voice + 1-frame cross fade
    "cameras": {
        "enabled": True,
        "gap_s": 20.0,       # body (program shows the screen): at most one camera cutaway per this much timeline
        "min_s": 2.5,        # a cutaway part lasts at least this long
        "hold_s": 3.0,       # face parts (hooks/outros): hold an angle at least this long before switching
        "max_s": 8.0,        # a cutaway returns to the screen after this long
        "face_min_part_s": 1.5,  # a face part shorter than this never switches angle
        "screen_corr": 0.5,  # program correlates under this with every camera = it shows a screen, not a camera
        "calm_max": 0.15,    # screen activity max inside the part (no page change)
        "calm_median": 0.03, # screen activity median inside the part
        "green_max": 0.05,   # never cut to a camera showing raw green screen (share of green pixels)
        "cycle": None        # angle rotation; null = [face, other1, face, other2, ...] from the source's angles
    },
    "languages": {
        "fr": {
            "_note": "French (Québec speech measured). « eh » / « ben » are real words there: never fillers.",
            "fillers": list(E.FILLERS["fr"]),
            "filler_aliases": ["ah", "eh"],   # the words transcript's spelling of a held vowel the timed-fillers file hears as « euh »
            "spare_words": ["donc"]           # sentence-initial discourse word cut when not essential and clean
        },
        "en": {
            "_note": "English: filler list only; no spare word by default (add \"so\" if the speaker leans on it).",
            "fillers": list(E.FILLERS["en"]),
            "filler_aliases": [],
            "spare_words": []
        }
    }
}

# set by configure()
KEEP_OUT = KEEP_IN = DEAD_AIR = MIN_CUT = PHRASE_MAX_DB = SPARE_GAP = 0.0
J_F = 0
FILLERS, EUH_ALIAS, SPARE = set(), set(), set()
USE_EARS = USE_FILL = USE_GLIDE = USE_CAMS = True
CAM = {}
FPS = 24000 / 1001
OVR = {}
P = None  # editlib.Project
# Resolve 21.2 puts a 1-frame audio transition at [join - 1, join] (alignment "center" or "left"; a true centre needs an
# even duration) -> both audio edges of an in-the-voice cut sit half a frame past the splice, centring the fade on it
GLIDE_H = 0.5


def configure(style, fps, language):
    global KEEP_OUT, KEEP_IN, DEAD_AIR, MIN_CUT, PHRASE_MAX_DB, SPARE_GAP, J_F, FILLERS, EUH_ALIAS, SPARE
    global USE_EARS, USE_FILL, USE_GLIDE, USE_CAMS, CAM, FPS
    KEEP_OUT, KEEP_IN, DEAD_AIR = style["keep_out_s"], style["keep_in_s"], style["dead_air_s"]
    MIN_CUT, PHRASE_MAX_DB, SPARE_GAP = style["min_cut_s"], style["phrase_max_db"], style["spare_word_gap_s"]
    J_F = int(style["j_frames"])
    lang = style["languages"].get(language) or style["languages"].get((language or "")[:2]) or {}
    if not lang:
        print(f"WARNING no language block for {language!r} in the style: no fillers / spare words by name", file=sys.stderr)
    FILLERS = {E.norm(x) for x in lang.get("fillers", [])}
    EUH_ALIAS = {E.norm(x) for x in lang.get("filler_aliases", [])}
    SPARE = {E.norm(x) for x in lang.get("spare_words", [])}
    USE_EARS, USE_FILL, USE_GLIDE = style["ears"], style["timed_fillers"], style["glide"]
    CAM = style["cameras"]
    USE_CAMS = CAM.get("enabled", True)
    FPS = fps


def norm(s):
    return E.norm(s)


def ears_breaths(src):
    """[(t0, t1)] breath spans of one source from its *.ears.json (source seconds), or None."""
    s = P.src(src)
    p = P.p(s.get("ears"))
    if not USE_EARS or not p or not os.path.isfile(p):
        return None
    off = float(s.get("ears_offset_s", 0.0))
    return [(e["t0"] + off, e["t1"] + off) for e in E.read_json(p)["events"] if e["type"] == "breath"]


def scribe_euh(src):
    """Filler runs heard by the timed-fillers transcript (consecutive fillers merged) with the words around them:
    [{t0, t1, pe = previous word's end, ns = next word's start, prev, next, ctx}] (source seconds), or []."""
    s = P.src(src)
    p = P.p(s.get("fillers"))
    if not USE_FILL:
        return []
    if not p or not os.path.isfile(p):
        print(f"WARNING no timed-fillers file for {src}: only fillers the words transcript wrote get cut "
              "(agent-ears makes one: <stem>.scribe.words.json)", file=sys.stderr)
        return []
    off = float(s.get("fillers_offset_s", 0.0))
    ws = [dict(w, start=w["start"] + off, end=w["end"] + off) for w in E.read_words(p)["words"]
          if w.get("type", "word") in ("word", "filler")]
    fil = [w.get("type") == "filler" or norm(w["text"]) in FILLERS for w in ws]
    out, n = [], 0
    while n < len(ws):
        if not fil[n]:
            n += 1
            continue
        m = n
        while m + 1 < len(ws) and fil[m + 1]:
            m += 1
        if n > 0 and m + 1 < len(ws):
            pv, nx = ws[n - 1], ws[m + 1]
            out.append({"t0": ws[n]["start"], "t1": ws[m]["end"], "pe": pv["end"], "ns": nx["start"],
                        "prev": pv["text"], "next": nx["text"],
                        "ctx": " ".join(w["text"] for w in ws[max(0, n - 6):n]) + " [" + " ".join(w["text"] for w in ws[n:m + 1])
                        + "] " + " ".join(w["text"] for w in ws[m + 1:m + 7])})
        n = m + 1
    return out


class Take:
    """Audio of one source (high-passed 80 Hz: chair/handling rumble read as voice otherwise) as 10 ms hops of a 30 ms
    window: dB, periodicity (normalised autocorrelation peak at 70-300 Hz pitch), spectral centroid (Hz). Computed in
    chunks of hops so a 25-min source stays under ~1 GB."""

    CHUNK = 20000

    def __init__(self, path, ext=None):
        self.ext = ext or []
        pcm = subprocess.run([E.need_ffmpeg(), "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-af",
                              "highpass=f=80", "-f", "f32le", "-"], capture_output=True, check=True).stdout
        x = np.frombuffer(pcm, np.float32)
        self.x = x  # splice_point (in-the-voice cuts) reads the samples
        hop, win = int(HOP * SR), int(0.03 * SR)
        n = (len(x) - win) // hop
        han = np.hanning(win)[None, :].astype(np.float32)
        f = np.fft.rfftfreq(1024, 1 / SR)
        lo, hi = SR // 300, SR // 70
        # divide out the Hann window's own autocorrelation, else a 100 Hz voice (lag = 1/3 window) can't pass 0.45
        wac = np.fft.irfft(np.abs(np.fft.rfft(np.hanning(win), 1024)) ** 2, 1024)
        wn = (wac[lo:hi] / wac[0])[None, :]
        dbs, cens, pers = [], [], []
        for c0 in range(0, n, self.CHUNK):
            c1 = min(n, c0 + self.CHUNK)
            idx = np.arange(win)[None, :] + hop * np.arange(c0, c1)[:, None]
            fr = x[idx] * han
            dbs.append(20 * np.log10(np.sqrt((fr ** 2).mean(1)) + 1e-9))
            mag = np.abs(np.fft.rfft(fr, 1024))
            cens.append((mag * f).sum(1) / (mag.sum(1) + 1e-9))
            ac = np.fft.irfft(mag ** 2, 1024)
            pers.append(np.clip((ac[:, lo:hi] / (ac[:, :1] + 1e-12)) / wn, 0, 1.2).max(1))
        self.db = np.concatenate(dbs)
        self.centroid = np.concatenate(cens)
        self.period = np.concatenate(pers)
        self.floor = float(np.percentile(self.db, 5))
        self.speech = float(np.percentile(self.db, 90))
        self.sil = self.db < self.floor + 10

    def i(self, t):
        return max(0, min(len(self.db) - 1, int(round(t / HOP))))

    def events(self, a, b):
        """Non-silent runs strictly inside (a, b) that are separated from both words by silence: the word's own
        tail (contiguous sound after a, incl. a final « s ») and the next onset (contiguous sound before b, incl. a
        breath that runs into it) never count. Each event: kind breath | euh | speech? | noise, start, end (s)."""
        ia, ib = self.i(a), self.i(b)
        while ia < ib and not self.sil[ia]:
            ia += 1
        while ib > ia and not self.sil[ib - 1]:
            ib -= 1
        if self.ext and ia >= ib:  # word -> breath -> word with no silence at all: cut between near-silent dips
            for p0, p1 in self.ext:
                if p1 - p0 < 0.08 or p0 < a - 0.03 or p1 > b + 0.08:
                    continue
                j0, j1 = self.dip(max(a - 0.03, p0 - 0.08), p0 + 0.08), self.dip(p1 - 0.08, min(b + 0.03, p1 + 0.08))
                if j0 is None or j1 is None or j1 - j0 < 8:
                    continue
                voiced = (self.period[j0:j1] > 0.55) & (self.db[j0:j1] > self.speech - 30)
                if voiced.mean() < 0.2:
                    return [{"kind": "breath", "a": j0 * HOP, "b": j1 * HOP, "dur": (j1 - j0) * HOP,
                             "src": "ears_bridge"}], j0 * HOP, j1 * HOP
        out, k = [], ia
        while k < ib:
            if self.sil[k]:
                k += 1
                continue
            e = k
            while e < ib and (not self.sil[e] or (e + 2 < ib and not self.sil[e + 1] and not self.sil[e + 2])):
                e += 1  # bridge a single silent hop inside an event
            dur = (e - k) * HOP
            db, per, cen = self.db[k:e], self.period[k:e], self.centroid[k:e]
            loud = db > self.speech - 30
            voiced = (per > 0.55) & loud
            if dur >= 0.1 and voiced.mean() >= 0.5:
                # filler = ONE held vowel: the loud core (within 10 dB of the peak, 3-hop smoothed) is one unbroken
                # stretch >= 0.18 s with a steady spectrum (centroid sd < 200 Hz). Several syllables (a valley in the
                # core) or a moving spectrum = words the transcript missed -> "speech?", never cut. Calibrated on a
                # held « euh » (core 0.27 s, periodicity 0.84-0.99, centroid 1450 +- 60 Hz) vs two misheard words
                # (15 dB valley, centroid 650-1370 Hz).
                sm = np.convolve(db, np.ones(3) / 3, mode="same")
                core = np.where(sm > sm.max() - 10)[0]
                one = core[-1] - core[0] + 1 == len(core)
                steady = np.std(cen[core]) < 200
                kind = "euh" if one and steady and len(core) * HOP >= 0.18 and dur <= 1.2 else "speech?"
            elif dur >= 0.08 and np.median(per) < 0.45 and np.median(cen) > 700 and db.max() < self.speech - 8:
                kind = "breath"
            else:
                kind = "noise"
            src = "dsp"
            if kind == "noise" and any(min(p1, e * HOP) - max(p0, k * HOP) >= 0.5 * min(p1 - p0, dur) for p0, p1 in self.ext):
                kind, src = "breath", "ears"
            out.append({"kind": kind, "a": k * HOP, "b": e * HOP, "dur": dur, "src": src})
            k = e
        if self.ext and ia < ib:  # a breath too quiet to rise above the silence floor
            f0, f1 = ia * HOP, ib * HOP
            for p0, p1 in self.ext:
                q0, q1 = max(p0, f0), min(p1, f1)
                if q1 - q0 < 0.06 or any(min(q1, x["b"]) - max(q0, x["a"]) >= 0.5 * (q1 - q0) for x in out):
                    continue
                out.append({"kind": "breath", "a": q0, "b": q1, "dur": q1 - q0, "src": "ears_quiet"})
        if self.ext and ia < ib:  # an inhale running straight into the next word: cut to its end on a near-silent dip
            on0 = ib * HOP        # when the removed stretch is unvoiced; exhales into a word tail are never cut (« s »)
            for p0, p1 in self.ext:
                if p1 <= on0 + 0.05 or p0 >= b - 0.02 or p1 > b + 0.08:
                    continue
                lo, hi = self.i(max(on0 + 0.05, p1 - 0.08)), self.i(min(p1 + 0.08, b + 0.03)) + 1
                if hi <= lo:
                    break
                j = lo + int(np.argmin(self.db[lo:hi]))
                seg = slice(ib, j)
                voiced = (self.period[seg] > 0.55) & (self.db[seg] > self.speech - 30)
                if self.db[j] < self.floor + 18 and j - ib >= 8 and voiced.mean() < 0.2:
                    out.append({"kind": "breath", "a": on0, "b": j * HOP, "dur": (j - ib) * HOP, "src": "ears_onset"})
                    ib = j
                break
        out.sort(key=lambda x: x["a"])
        return out, ia * HOP, ib * HOP

    def dip(self, t0, t1):
        """Quietest hop in [t0, t1] if it sits near silence (floor + 18 dB, the clean_split rule), else None."""
        lo, hi = self.i(t0), self.i(t1) + 1
        if hi <= lo:
            return None
        k = lo + int(np.argmin(self.db[lo:hi]))
        return k if self.db[k] < self.floor + 18 else None


def clean_split(tk, t0, t1):
    """Quietest hop between t0 and t1 (+-40 ms) if it dips near silence (a stop closure / micro-pause): where a word
    comes off its neighbour cleanly; None when the two run together."""
    lo, hi = tk.i(min(t0, t1) - 0.04), tk.i(max(t0, t1) + 0.04) + 1
    k = lo + int(np.argmin(tk.db[lo:hi]))
    return k * HOP if tk.db[k] < tk.floor + 18 else None


def soft_split(tk, t0, t1):
    """Looser dip for phrase trims and tight filler cuts: quietest hop within +-40 ms under floor + 30 dB AND >= 12 dB
    below the local peak (+-150 ms). Admitted 86 of 183 proposed phrase trims vs 56 with clean_split on the test cut.
    Splice inside running speech: every such cut gets a review marker."""
    lo, hi = tk.i(min(t0, t1) - 0.04), tk.i(max(t0, t1) + 0.04) + 1
    k = lo + int(np.argmin(tk.db[lo:hi]))
    m = (t0 + t1) / 2
    peak = float(tk.db[tk.i(m - 0.15):tk.i(m + 0.15) + 1].max())
    return k * HOP if tk.db[k] < tk.floor + 30 and peak - tk.db[k] >= 12 else None


def splice_point(tk, t0, t1):
    """Where to splice inside running voice in [t0, t1] (s): the level valley (20 ms window, 5 ms hop) when it sits
    >= 6 dB under the peaks 50 ms either side (the filler -> word boundary on a spectrogram), else the strongest
    spectral change (log-spectrum flux 100-6000 Hz, 3-frame smoothed)."""
    x, win, hop = tk.x, int(0.02 * SR), int(0.005 * SR)
    a, b = max(0, int((t0 - 0.06) * SR)), min(len(x), int((t1 + 0.06) * SR) + win)
    seg = x[a:b]
    n = (len(seg) - win) // hop
    if n < 3:
        return (t0 + t1) / 2
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    fr = seg[idx]
    tf = (a + hop * np.arange(n) + win / 2) / SR
    lvl = 20 * np.log10(np.sqrt((fr ** 2).mean(1)) + 1e-9)
    inw = np.where((tf >= t0) & (tf <= t1))[0]
    if len(inw):
        k = inw[int(np.argmin(lvl[inw]))]
        side = 10  # 50 ms
        # interior minimum only: the window edge sitting lowest = the level still falling into the next word
        if k not in (inw[0], inw[-1]) and min(lvl[max(0, k - side):k + 1].max(), lvl[k:k + side + 1].max()) - lvl[k] >= 6:
            return float(tf[k])
    mag = np.log(np.abs(np.fft.rfft(fr * np.hanning(win)[None, :], 1024)) + 1e-6)
    f = np.fft.rfftfreq(1024, 1 / SR)
    band = (f > 100) & (f < 6000)
    d = np.convolve(np.abs(np.diff(mag[:, band], axis=0)).mean(1), np.ones(3) / 3, mode="same")
    tc = (a + hop * np.arange(1, n) + win / 2) / SR  # boundary between frames k-1 and k
    ok = np.where((tc >= t0) & (tc <= t1))[0]
    return float(tc[ok[int(np.argmax(d[ok]))]]) if len(ok) else (t0 + t1) / 2


def _split(tk, a, b, soft):
    x = clean_split(tk, a, b)
    return x if x is not None or not soft else soft_split(tk, a, b)


def left_cut(tk, a, b, soft=False):
    """Cut start after the word ending at a, before what starts at b: up to KEEP_OUT s of the silence after the
    word's tail stays; no silence between them -> a clean dip (soft: or a soft_split dip) or None."""
    ev, t_end, o_start = tk.events(a, b)
    if o_start - t_end < 0.02:
        return _split(tk, a, b, soft), ev
    first = ev[0]["a"] if ev else o_start
    return t_end + min(KEEP_OUT, first - t_end), ev


def right_cut(tk, a, b, soft=False):
    """Cut end before the word starting at b, after what ends at a: up to KEEP_IN s of the silence before the onset."""
    ev, t_end, o_start = tk.events(a, b)
    if o_start - t_end < 0.02:
        return _split(tk, a, b, soft), ev
    last = ev[-1]["b"] if ev else t_end
    return o_start - min(KEEP_IN, o_start - last), ev


def breath_label(bsrc):
    return "breath" if "dsp" in bsrc else "breath glued (ears)" if bsrc & {"ears_onset", "ears_bridge"} else "breath (ears)"


def removals(src, seg, words, tk, donc_log, flags, phrase_log, sc_euh=(), euh_log=None):
    """Removal ranges (source seconds) inside one kept segment."""
    a_s, b_s = seg["in_f"] / FPS, seg["out_f"] / FPS
    idx = [i for i, w in enumerate(words) if a_s <= (w["start"] + w["end"]) / 2 <= b_s]
    pos = {i: p for p, i in enumerate(idx)}
    keep_d = set(OVR.get("keep_spare", {}).get(src, []))
    drop_d = set(OVR.get("drop_spare", {}).get(src, []))
    spans = {int(x[0]): x for x in OVR.get("drop_spans", {}).get(src, [])}  # first id -> [first, last, kind, removed]
    out = []
    k = 0
    while k < len(idx) - 1:
        i = idx[k]
        # words to drop after word i: transcribed fillers, a sentence-initial spare word that comes off cleanly,
        # an approved phrase span (drop_spans) whose both edges cut cleanly
        j, why = k + 1, []
        while j < len(idx) - 1:
            w = words[idx[j]]
            t = norm(w["text"])
            sp = spans.get(idx[j])
            if sp is not None and pos.get(int(sp[1]), len(idx)) < len(idx) - 1:
                last = int(sp[1])
                l_hard = left_cut(tk, words[i]["end"], w["start"])[0] is not None
                r_hard = right_cut(tk, words[last]["end"], words[last + 1]["start"])[0] is not None
                lt = left_cut(tk, words[i]["end"], w["start"], soft=True)[0]
                rt = right_cut(tk, words[last]["end"], words[last + 1]["start"], soft=True)[0]
                # soft edges must still be quiet once snapped to a frame (the dip can be narrower than a frame)
                l_ok = l_hard or (lt is not None and edge_level(tk, lt, 1) <= PHRASE_MAX_DB)
                r_ok = r_hard or (rt is not None and edge_level(tk, rt, -1) <= PHRASE_MAX_DB)
                phrase_log.append({"src": src, "first": idx[j], "last": last, "kind": sp[2] if len(sp) > 2 else "",
                                   "removed": " ".join(x["text"] for x in words[idx[j]:last + 1]), "clean": l_ok and r_ok,
                                   "tight": not (l_hard and r_hard),
                                   "edges": ("" if l_ok else "start ") + ("" if r_ok else "end"),
                                   "ctx": " ".join(x["text"] for x in words[max(0, idx[j] - 6):idx[j]]) + " ‖ "
                                   + " ".join(x["text"] for x in words[last + 1:last + 7])})
                if l_ok and r_ok:
                    why.append("phrase" if l_hard and r_hard else "phrase (tight)")
                    j = pos[last] + 1
                    continue
            if t in FILLERS or w.get("type") == "filler":
                why.append("filler")
                j += 1
                continue
            if t in SPARE:
                prev, nxt = words[idx[j - 1]], words[idx[j + 1]]
                initial = bool(re.search(r"[.?!,:]$", prev["text"])) or w["start"] - prev["end"] >= SPARE_GAP or bool(why)
                ok = idx[j] in drop_d or (initial and idx[j] not in keep_d)
                clean = right_cut(tk, w["end"], nxt["start"])[0] is not None
                donc_log.append({"src": src, "id": idx[j], "word": w["text"], "t": round(w["start"], 2), "initial": initial,
                                 "clean": clean, "removed": bool(ok and clean),
                                 "ctx": " ".join(x["text"] for x in words[max(0, idx[j] - 8):idx[j]]) + " [" + w["text"]
                                 + "] " + " ".join(x["text"] for x in words[idx[j] + 1:idx[j] + 9])})
                if ok and clean:
                    why.append("spare word")
                    j += 1
                    continue
            break
        a, b = words[i]["end"], words[idx[j]]["start"]
        dropped = [words[idx[x]] for x in range(k + 1, j)]
        soft = any(x.startswith("phrase") for x in why)
        ra, ev1 = left_cut(tk, a, dropped[0]["start"] if dropped else b, soft)
        rb, ev2 = right_cut(tk, dropped[-1]["end"] if dropped else a, b, soft)
        ev = ev1 + (ev2 if dropped else [])
        inner = []
        for x, y in zip(dropped, dropped[1:]):
            inner += tk.events(x["end"], y["start"])[0]
        kinds_ev = [e["kind"] for e in ev + inner]
        silent = not any(k in ("breath", "euh", "speech?") for k in kinds_ev) and b - a >= DEAD_AIR  # mouth clicks = dead air
        if "speech?" in kinds_ev:  # voice with several syllables where the transcript has nothing: maybe words -> keep
            flags.append({"src": src, "t": round(a, 2), "between": f"{words[i]['text']} … {words[idx[j]]['text']}",
                          "events": [(e["kind"], round(e["a"], 2), round(e["dur"], 2)) for e in ev + inner]})
        elif (why or "breath" in kinds_ev or "euh" in kinds_ev or silent) and ra is not None and rb is not None \
                and rb - ra >= MIN_CUT:
            bsrc = {e.get("src", "dsp") for e in ev + inner if e["kind"] == "breath"}
            souffle = [breath_label(bsrc)] if bsrc else []
            kinds = why + souffle + (["filler (audio)"] if "euh" in kinds_ev else []) + (["dead air"] if silent else [])
            out.append({"a": ra, "b": rb, "kinds": kinds, "before": words[i]["text"], "after": words[idx[j]]["text"],
                        "dropped": [w["text"] for w in dropped]})
        k = j
    for e in sc_euh:  # fillers the timed-fillers transcript hears and times
        if not a_s <= (e["t0"] + e["t1"]) / 2 <= b_s:
            continue
        log = {"src": src, "t": round(e["t0"], 2), "ctx": e["ctx"], "cut": "", "why": ""}
        euh_log.append(log)
        if any(min(x["b"], e["t1"]) - max(x["a"], e["t0"]) >= 0.5 * (e["t1"] - e["t0"]) for x in out):
            log["cut"] = "already"
            continue
        if not (a_s <= e["pe"] and e["ns"] <= b_s):
            log["why"] = "segment edge"
            continue
        clash = [words[i]["text"] for i in idx if norm(words[i]["text"]) not in FILLERS | EUH_ALIAS
                 and words[i].get("type") != "filler"
                 and min(words[i]["end"], e["ns"]) - max(words[i]["start"], e["pe"]) > 0.5 * (words[i]["end"] - words[i]["start"])]
        if clash:
            log["why"] = "the words transcript has a word there: " + " ".join(clash)
            continue
        ev = tk.events(e["pe"], e["ns"])[0]
        if any(x["kind"] == "speech?" and min(x["b"], e["t1"]) - max(x["a"], e["t0"]) < 0.5 * x["dur"] for x in ev):
            log["why"] = "speech? beside the filler"
            continue
        ra, rb, tight = left_cut(tk, e["pe"], e["t0"])[0], right_cut(tk, e["t1"], e["ns"])[0], []
        if ra is None:
            x = left_cut(tk, e["pe"], e["t0"], soft=True)[0]
            if x is not None and edge_level(tk, x, 1) <= PHRASE_MAX_DB:
                ra = x
                tight.append("start")
        if rb is None:
            x = right_cut(tk, e["t1"], e["ns"], soft=True)[0]
            if x is not None and edge_level(tk, x, -1) <= PHRASE_MAX_DB:
                rb = x
                tight.append("end")
        glide = [side for side, r in (("start", ra), ("end", rb)) if r is None]
        if glide and not USE_GLIDE:
            log["why"] = "no dip at the " + " and ".join(glide) + " (the filler runs into the word)"
            continue
        xa = splice_point(tk, e["pe"] - 0.02, e["pe"] + 0.06) if ra is None else None
        xb = splice_point(tk, e["ns"] - 0.06, e["ns"] + 0.02) if rb is None else None
        ra, rb = ra if xa is None else xa, rb if xb is None else xb
        if rb - ra < MIN_CUT:
            log["why"] = f"removal {rb - ra:.2f} s < min_cut_s"
            continue
        kinds = [GLIDE_KIND if glide else "filler (timed, tight)" if tight else "filler (timed)"]
        bsrc = {x.get("src", "dsp") for x in ev if x["kind"] == "breath" and ra <= x["a"] and x["b"] <= rb + 0.02}
        if bsrc:
            kinds.append(breath_label(bsrc))
        # touching = within 3 frames: split() drops a part shorter than that
        hit = [x for x in out if x["a"] < rb + 3 / FPS and ra < x["b"] + 3 / FPS]
        na, nb = min([ra] + [x["a"] for x in hit]), max([rb] + [x["b"] for x in hit])
        out = [x for x in out if x not in hit] + [{
            "a": na, "b": nb, "kinds": [k for x in hit for k in x["kinds"]] + kinds,
            "before": hit[0]["before"] if hit else e["prev"],
            "after": hit[-1]["after"] if hit else e["next"], "dropped": [w for x in hit for w in x["dropped"]] + ["euh" if "euh" in FILLERS else "(filler)"],
            # sample-exact edges inside the voice (split + resolve_build): only while they stay the outer edges
            "xa": xa if xa is not None and na == xa else None, "xb": xb if xb is not None and nb == xb else None}]
        out.sort(key=lambda x: x["a"])
        log["cut"] = "in the voice " + "+".join(glide) if glide else "tight " + "+".join(tight) if tight else "clean"
    return out


def edge_frame(tk, t, order):
    """Frame for a cut edge at t: the quieter of the two frames around it (level centred on the boundary); ties ->
    inward (order 1 = out edge -> frame up, -1 = in edge -> frame down). Plain inward rounding put 22 of 94 phrase
    joins 35-49 dB over floor (a 42 ms frame is wider than the dip)."""
    fs = [math.ceil(t * FPS), math.floor(t * FPS)][::order]
    return min(fs, key=lambda f: float(tk.db[tk.i(f / FPS - 0.015)]))


def edge_level(tk, t, order):
    """dB over floor at the frame edge_frame picks."""
    return float(tk.db[tk.i(edge_frame(tk, t, order) / FPS - 0.015)]) - tk.floor


def j_clean(tk, f0, n):
    """True when the outgoing source frames f0..f0+n (shown under the next part's sound) are silent and hold no ears
    breath: the held picture then shows a closed-mouth pause, never a breath or a filler starting."""
    t0, t1 = f0 / FPS, (f0 + n) / FPS
    if not tk.sil[tk.i(t0):tk.i(t1) + 1].all():
        return False
    return not any(min(p1, t1) > max(p0, t0) for p0, p1 in tk.ext)


def split(seg, rem, tk):
    """Sub-segments of one seg around its removals (frames), with J-cut picture ranges. In-the-voice cuts: picture
    edge = frame nearest the splice; audio edges sample-exact + GLIDE_H, carried as a_in_f / a_out_f (source frames,
    fractional) and drift = audio record start - picture record start (frames), absorbed at the next clean join."""
    cuts = sorted((max(seg["in_f"], round(r["xa"] * FPS) if r.get("xa") is not None else edge_frame(tk, r["a"], 1)),
                   min(seg["out_f"], edge_frame(tk, r["b"], -1)), r) for r in rem)
    parts, cur, cur_a, drift = [], seg["in_f"], None, 0.0
    head, fade_in = None, False
    for fa, fb, r in cuts:
        if fa - cur < 3 and not parts and head is None and r.get("xb") is not None and fb > cur:
            # in-the-voice filler right at the segment's head: start the part at the splice, 1-frame fade-in over
            # [splice - 0.5, splice + 0.5] frame (no outgoing sound of its own to cross fade with)
            head, fade_in = r, True
            cur_a = r["xb"] * FPS - GLIDE_H
            cur, drift = round(cur_a), 0.0
            continue
        if fa - cur >= 3 and fb > fa:
            a_in = cur if cur_a is None else cur_a
            if r.get("xa") is not None or r.get("xb") is not None:
                a_out = (r["xa"] * FPS if r.get("xa") is not None else fa) + GLIDE_H
                b_in = (r["xb"] * FPS if r.get("xb") is not None else fb) + GLIDE_H
                nd = drift + (a_out - a_in) - (fa - cur)
                fb = max(fa + 1, round(b_in - nd))  # next picture in-point = frame nearest its sound
                parts.append([cur, fa, r, a_in, a_out, drift, True])
                cur, cur_a, drift = fb, b_in, nd
            else:  # clean join: this part's sound ends with its picture (drift absorbed in the silence pad)
                parts.append([cur, fa, r, a_in, a_in + (fa - cur) - drift, drift, False])
                cur, cur_a, drift = fb, None, 0.0
    a_in = cur if cur_a is None else cur_a
    parts.append([cur, seg["out_f"], None, a_in, a_in + (seg["out_f"] - cur) - drift, drift, False])
    subs = []
    for n, (fa, fb, r, a_in, a_out, dr, xf) in enumerate(parts):
        s = {k: v for k, v in seg.items() if k not in ("in_f", "out_f", "rec_f", "in_s", "out_s", "rec_s", "in", "out")}
        s.update(in_f=fa, out_f=fb, cut_after=r, a_in_f=round(a_in, 4), a_out_f=round(a_out, 4), a_drift_f=round(dr, 4),
                 xf=xf, sync_f=round(a_in - dr - fa, 4))
        if n == 0 and head is not None:
            s.update(head_cut=head, fade_in=True)
        subs.append(s)
    for a, b in zip(subs, subs[1:]):  # J: next part's sound on time, its picture J frames later; meanwhile the
        removed = b["in_f"] - a["out_f"]  # outgoing picture runs on into the removed (silent) source
        j = min(J_F, removed, (b["out_f"] - b["in_f"]) // 2)
        a["J"] = j if j > 0 and j_clean(tk, a["out_f"], j) else 0
    for n, s in enumerate(subs):
        jp = subs[n - 1]["J"] if n else 0
        s["v_in_f"], s["v_out_f"] = s["in_f"] + jp, s["out_f"] + s.get("J", 0)
    return subs


def cam_cycle(sid, face, cams):
    if CAM.get("cycle"):
        return list(CAM["cycle"])
    others = [c for c in P.angles(sid) if c != face] or [c for c in cams if c != face]
    cyc = []
    for o in others:
        cyc += [face, o]
    return cyc or [face]


def plan_angles(segs):
    """Camera switches -> s["angle"] per part, placed on V3 by resolve_build (V1 multicam program and V2 face stay
    underneath: deleting a V3 piece goes back to them). No NLE API sets a multicam item's angle, hence V3 clips.
      - face parts (hooks/outros): at a jump cut, after >= hold_s on one angle, rotate face / other / face / other
        (face = no V3 piece, V2 shows it);
      - body where the program shows a screen (no camera correlates > screen_corr): a part >= min_s whose screen does
        not change page becomes a camera cutaway, at most one per gap_s, back to the screen after max_s;
      - program already on a camera (live switcher, keyed backgrounds): left alone;
      - never a camera showing raw green screen anywhere in the part."""
    scans, stats = {}, {"cutaway": 0, "face_switch": 0}
    last_cut, face_i, face_since = -1e9, 0, -1e9
    for s in segs:
        s["angle"] = None
        if not USE_CAMS:
            continue
        if s["src"] not in scans:
            p = P.p(P.src(s["src"]).get("scan"))
            scans[s["src"]] = E.read_json(p) if p and os.path.isfile(p) else None
        sc = scans[s["src"]]
        if not sc:
            continue
        k0, k1 = int(s["in_f"] / FPS / sc["step"]), min(sc["n"], int(math.ceil(s["out_f"] / FPS / sc["step"])))
        if k1 - k0 < 1:
            continue
        cams = list(sc["out_corr"])
        face = P.src(s["src"]).get("face")
        cycle = cam_cycle(s["src"], face, cams)
        ok = {c for c in cams if max(sc["green"][c][k0:k1]) < CAM["green_max"]}
        dur, t = (s["v_out_f"] - s["v_in_f"]) / FPS, s["v_rec_f"] / FPS

        def pick(name):
            ang = P.angles(s["src"]).get(name, {})
            f = (sc.get("file") or {}).get(name) or ang.get("file")
            off = (sc.get("offset_s") or {}).get(name)
            off = ang.get("offset_s") if off is None else off
            return {"name": name, "file": f, "delay_f": round((off or 0.0) * FPS)}
        if s.get("face"):
            if face_since < -1e8:  # a face run opens on the face angle
                face_i, face_since = 0, t
            elif t - face_since >= CAM["hold_s"] and dur >= CAM["face_min_part_s"]:
                for _ in range(len(cycle)):
                    face_i = (face_i + 1) % len(cycle)
                    if cycle[face_i] in ok:
                        break
                face_since = t
                stats["face_switch"] += 1
            name = cycle[face_i % len(cycle)]
            s["angle"] = pick(name) if name != face and name in ok else None
            continue
        face_since = -1e9
        corr = max(float(np.median(sc["out_corr"][c][k0:k1])) for c in cams)
        act = sc["act"][k0 + 1:k1] or [0]
        if corr < CAM["screen_corr"] and dur >= CAM["min_s"] and max(act) < CAM["calm_max"] \
                and float(np.median(act)) < CAM["calm_median"] and ok and t - last_cut >= CAM["gap_s"]:
            # ok may hold only cameras outside the cycle (all cycle cameras show raw green) -> no cutaway
            name = next((c for c in cycle[stats["cutaway"] % len(cycle):] + cycle if c in ok), None)
            if name is None:
                continue
            s["angle"] = dict(pick(name), max_f=round(CAM["max_s"] * FPS))
            last_cut = t
            stats["cutaway"] += 1
    return stats


def tighten(c, name):
    takes, words_c, donc_log, segs, report, flags, phrase_log = {}, {}, [], [], [], [], []
    sc_c, euh_log = {}, []
    rec = 0
    alt_gap_done = False
    gap_f = round(float(c.get("alt_gap_s", 2.0)) * FPS)
    for seg in c["segments"]:
        src = seg["src"]
        if src not in takes:
            s = P.src(src)
            audio = P.p(s.get("audio")) or P.file(src)
            print(f"  hearing {src}: {Path(audio).name}", flush=True)
            takes[src] = Take(audio, ears_breaths(src))
            words_c[src] = P.words(src)
            sc_c[src] = scribe_euh(src)
        rem = removals(src, seg, words_c[src], takes[src], donc_log, flags, phrase_log, sc_c[src], euh_log)
        if seg.get("alt") and not alt_gap_done:
            alt_gap_done = True
            rec += gap_f
        for s in split(seg, rem, takes[src]):
            lp = rec - (s["in_f"] - s["v_in_f"])
            s.update(rec_f=rec, v_rec_f=lp, in_s=s["in_f"] / FPS, out_s=s["out_f"] / FPS, rec_s=rec / FPS,
                     a_rec_f=round(rec + s.pop("a_drift_f"), 4))
            r = s.pop("cut_after")
            hd = s.pop("head_cut", None)
            off = P.offset(src)
            if hd:  # head trim (in-the-voice filler at the segment start): the join is this part's start
                report.append({"rec_s": rec / FPS, "src": src, "src_s": hd["a"] - off, "len": (hd["b"] - hd["a"]),
                               "kinds": hd["kinds"], "before": hd["before"], "after": hd["after"], "dropped": hd["dropped"]})
            rec += s["out_f"] - s["in_f"]
            if r:
                report.append({"rec_s": rec / FPS, "src": src, "src_s": r["a"] - off, "len": (r["b"] - r["a"]),
                               "kinds": r["kinds"], "before": r["before"], "after": r["after"], "dropped": r["dropped"]})
            segs.append(s)
    for x in euh_log:  # a removal split() could not place (too close to another cut / the segment edge) = still in
        if x["cut"]:
            off = P.offset(x["src"])
            if not any(c2["src"] == x["src"] and c2["src_s"] + off <= x["t"] + 0.05 <= c2["src_s"] + off + c2["len"] for c2 in report):
                x["why"], x["cut"] = f"dropped by split ({x['cut']}): too close to another cut or the segment edge", ""
    angles = plan_angles(segs)
    old = c["segments"]

    def remap(f):  # rough record frame -> source frame -> tight record frame
        o = next((s for s in old if s["rec_f"] <= f < s["rec_f"] + s["out_f"] - s["in_f"]), None)
        if o is None:
            return None
        sf = o["in_f"] + f - o["rec_f"]
        n = next((s for s in segs if s["src"] == o["src"] and s.get("alt") == o.get("alt") and s["in_f"] <= sf < s["out_f"]), None)
        if n is None:  # inside a removed stretch -> start of the next kept part
            n = min((s for s in segs if s["src"] == o["src"] and s.get("alt") == o.get("alt") and s["in_f"] >= sf),
                    key=lambda s: s["in_f"], default=None)
            return n["rec_f"] if n else None
        return n["rec_f"] + sf - n["in_f"]
    main_end = E.main_end_f(segs)
    markers = []
    for m in c.get("markers", []):
        if m.get("kind") == "end":
            m = dict(m, frame=main_end - 1)
        else:
            f = remap(m["frame"])
            if f is None:
                continue
            m = dict(m, frame=f)
        markers.append(m)
    # captions: drop removed words from the source cues (capital moves to the next word), retime on the parts
    cues = []
    for src in dict.fromkeys(s["src"] for s in segs):
        words, off = words_c[src], P.offset(src)
        # timed-filler cuts: the words transcript's own filler may sit up to ~0.35 s off the removed audio
        gone = [(r["src_s"] + off, r["src_s"] + off + r["len"], any(k.startswith("phrase") for k in r["kinds"]),
                 0.35 if any(k.startswith("filler (timed") for k in r["kinds"]) else 0.0) for r in report if r["src"] == src]
        src_cues = []
        for a, b, t in P.captions(src):
            cue_w = [w for w in words if a - 0.05 <= (w["start"] + w["end"]) / 2 <= b + 0.05]
            dead = [n for n, w in enumerate(cue_w) if any(
                (x <= (w["start"] + w["end"]) / 2 <= y and (ph or norm(w["text"]) in FILLERS | SPARE or w.get("type") == "filler"))
                or (pad and x - pad <= (w["start"] + w["end"]) / 2 <= y + pad and norm(w["text"]) in FILLERS | EUH_ALIAS)
                for x, y, ph, pad in gone)]
            toks = t.split()
            if dead and len(toks) == len(cue_w):  # same tokens as words: drop by position (repeated words stay right)
                cap = [n for n in dead if toks[n][:1].isupper()]
                keep = [n for n in range(len(toks)) if n not in dead]
                for n in cap:  # capital moves to the next kept word
                    nxt = next((m for m in keep if m > n), None)
                    if nxt is not None:
                        toks[nxt] = toks[nxt][:1].upper() + toks[nxt][1:]
                for n in dead:  # a sentence end on a dropped word moves to the previous kept word
                    prv = max((m for m in keep if m < n), default=None)
                    if toks[n][-1:] in ".?!" and prv is not None and toks[prv][-1:] not in ".?!,:;":
                        toks[prv] += toks[n][-1]
                toks = [toks[n] for n in keep]
            else:
                for w in (cue_w[n] for n in dead):
                    for q, tok in enumerate(toks):
                        if norm(tok) == norm(w["text"]):
                            cap = tok[:1].isupper()
                            del toks[q]
                            if cap and q < len(toks):
                                toks[q] = toks[q][:1].upper() + toks[q][1:]
                            break
            if toks:
                src_cues.append((a, b, " ".join(toks)))
        cues += E.retime_srt(src_cues, words, [s for s in segs if s["src"] == src], FPS)
    cues = E.finish_cues(cues, segs, FPS)
    for s in segs:
        s["in"], s["out"] = round(s["in_f"] / FPS, 6), round(s["out_f"] / FPS, 6)
    return {"fps": FPS, "timeline": name, "sources": {k: P.file(k) for k in dict.fromkeys(s["src"] for s in segs)},
            "alt_gap_s": c.get("alt_gap_s", 2.0), "segments": segs, "markers": markers, "main_s": main_end / FPS,
            "total_f": E.total_f(segs), "before_main_s": E.main_end_f(c["segments"]) / FPS, "cuts": report,
            "spare": donc_log, "flags": flags, "phrases": phrase_log, "angles": angles, "fillers_timed": euh_log,
            "style": {"j_frames": J_F, "keep_out_s": KEEP_OUT, "keep_in_s": KEEP_IN, "dead_air_s": DEAD_AIR}}, cues


def write_report(r, path):
    kinds = {}
    for x in r["cuts"]:
        for k in x["kinds"]:
            kinds[k] = kinds.get(k, 0) + 1
    st = r["style"]
    lines = [f"# {r['timeline']}", "",
             f"Main cut {r['before_main_s'] / 60:.2f} -> {r['main_s'] / 60:.2f} min ({r['before_main_s'] - r['main_s']:.0f} s removed), "
             f"{len(r['cuts'])} cuts: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items()))
             + f". J-cut (sound {st['j_frames']} fr before picture) on {sum(1 for s in r['segments'] if s.get('J'))} of them, the rest "
             f"straight (outgoing source not silent). Pause kept: {st['keep_out_s']:.2f} s after the tail, {st['keep_in_s']:.2f} s "
             "before the onset.", "",
             "| Timeline | Source | Removed | What | Around the cut |", "|---|---|---|---|---|"]
    for x in r["cuts"]:
        lines.append(f"| {E.tc(x['rec_s'])} | {x['src']} {x['src_s']:.2f} | {x['len']:.2f} s | {', '.join(x['kinds'])} | "
                     f"…{x['before']} ✂ {' '.join(x['dropped']) + ' ✂ ' if x['dropped'] else ''}{x['after']}… |")
    d = r["spare"]
    lines += ["", f"## Spare words: {sum(x['removed'] for x in d)} removed of {len(d)} (overrides keep_spare / drop_spare by id)", "",
              "| id | Words s | Initial | Clean gap | Removed | Context |", "|---|---|---|---|---|---|"]
    for x in d:
        lines.append(f"| {x['src']}:{x['id']} | {x['t']:.2f} | {'y' if x['initial'] else ''} | {'y' if x['clean'] else ''} | "
                     f"{'**yes**' if x['removed'] else ''} | {x['ctx']} |")
    ph = r.get("phrases", [])
    lines += ["", f"## Phrase trims (overrides drop_spans): {sum(x['clean'] for x in ph)} cut of {len(ph)} "
              "(the rest kept: no clean edge to cut on)", "",
              "| Source ids | Kind | Cut | Removed | Context (before ‖ after) |", "|---|---|---|---|---|"]
    for x in ph:
        lines.append(f"| {x['src']}:{x['first']}-{x['last']} | {x['kind']} | {'**yes**' if x['clean'] else 'no (' + x['edges'].strip() + ')'} | "
                     f"{x['removed']} | {x['ctx']} |")
    eu = r.get("fillers_timed", [])
    if eu:
        lines += ["", f"## Fillers heard by the timed-fillers transcript: {len(eu)} in the kept segments — "
                  f"{sum(x['cut'] == 'already' for x in eu)} already cut, {sum(x['cut'] == 'clean' for x in eu)} cut clean, "
                  f"{sum(x['cut'].startswith('tight') for x in eu)} cut on a soft dip (listen), "
                  f"{sum(x['cut'].startswith('in the voice') for x in eu)} cut inside the voice (1-frame cross fade: listen), "
                  f"{sum(not x['cut'] for x in eu)} kept", "",
                  "| Source | Words s | Cut | Why kept | Context |", "|---|---|---|---|---|"]
        for x in eu:
            lines.append(f"| {x['src']} | {x['t'] - P.offset(x['src']):.2f} | {x['cut'] or 'no'} | {x['why']} | {x['ctx']} |")
    xs = [s for s in r["segments"] if s.get("xf")]
    if xs:
        sy = [abs(s["sync_f"]) for s in r["segments"]]
        lines += ["", f"## Cuts inside the voice: {len(xs)} joins with sub-frame audio edges + a 1-frame Cross Fade +3 dB "
                  f"(A1); worst picture/sound offset {max(sy):.2f} frame ({max(sy) / FPS * 1000:.0f} ms)", ""]
    a = r.get("angles", {})
    lines += ["", f"## Cameras (V3): {a.get('cutaway', 0)} cutaways from the screen, {a.get('face_switch', 0)} angle changes on face parts", ""]
    lines += ["", f"## Kept: voice where the transcript has no words ({len(r['flags'])}), maybe real words: listen", ""]
    for x in r["flags"]:
        lines.append(f"- {x['src']} {x['t']:.2f}: {x['between']} — {x['events']}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_style(path):
    st = E.deep_merge(DEFAULT_STYLE, E.read_json(path)) if path else E.deep_merge(DEFAULT_STYLE, {})
    return st


def main():
    global P, OVR
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", help="edit.json")
    ap.add_argument("--cuts", help="rough cut list (*.cuts.json)")
    ap.add_argument("--out", help="output prefix: writes <out>.cuts.json, <out>.srt, <out>.report.md")
    ap.add_argument("--style", help="style JSON (default: edit.json \"style\", else the house style)")
    ap.add_argument("--overrides", help="overrides JSON (default: edit.json \"overrides\")")
    ap.add_argument("--name", help="timeline name (default: the rough name with rough -> tight)")
    ap.add_argument("--no-ears", action="store_true", help="ignore *.ears.json breath spans (DSP breaths only)")
    ap.add_argument("--no-fillers", action="store_true", help="ignore the timed-fillers transcript")
    ap.add_argument("--no-glide", action="store_true", help="never cut inside the voice")
    ap.add_argument("--no-cameras", action="store_true", help="no camera switches")
    ap.add_argument("--dump-style", action="store_true", help="print the default style JSON and exit")
    a = ap.parse_args()
    if a.dump_style:
        print(json.dumps(DEFAULT_STYLE, ensure_ascii=False, indent=2))
        return
    if not (a.project and a.cuts and a.out):
        ap.error("--project, --cuts and --out are required")
    P = E.Project(a.project)
    style = load_style(a.style or P.p(P.cfg.get("style")))
    for flag, key in ((a.no_ears, "ears"), (a.no_fillers, "timed_fillers"), (a.no_glide, "glide")):
        if flag:
            style[key] = False
    if a.no_cameras:
        style["cameras"]["enabled"] = False
    ov = a.overrides or P.p(P.cfg.get("overrides"))
    OVR = E.read_json(ov) if ov and os.path.isfile(ov) else {}
    c = E.load_cuts(a.cuts)
    if abs(c["fps"] - P.fps) > 1e-6:
        print(f"WARNING cut list fps {c['fps']:.3f} != project fps {P.fps:.3f}: using the cut list's", file=sys.stderr)
    first = c["segments"][0]["src"]
    configure(style, c["fps"], P.language(first))
    rough = c.get("timeline") or Path(a.cuts).stem
    name = a.name or (re.sub(r"rough( v\d+)?", "tight", rough, count=1) if "rough" in rough else rough + " — tight")
    r, cues = tighten(c, name)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    r["srt"] = str(out.resolve().with_name(out.name + ".srt"))
    E.write_srt(r["srt"], cues)
    E.write_json(out.with_name(out.name + ".cuts.json"), r)
    write_report(r, out.with_name(out.name + ".report.md"))
    sp = r["spare"]
    print(f"{name}: main {r['before_main_s'] / 60:.2f} -> {r['main_s'] / 60:.2f} min, {len(r['cuts'])} cuts, "
          f"{len(r['segments'])} parts, J {sum(1 for s in r['segments'] if s.get('J'))}, spare words {sum(x['removed'] for x in sp)}/{len(sp)} "
          f"removed, cameras {r['angles']} -> {out}.cuts.json / .srt / .report.md")


if __name__ == "__main__":
    main()
