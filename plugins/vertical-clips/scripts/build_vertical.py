"""HyperFrames 9:16 project(s) for one clip: vertical master + hook + word-timed captions, one project per look.

usage: python build_vertical.py [-c clips.json] ID [LOOK…]     (looks default: clip "looks", else config "looks")
       python build_vertical.py --list                          (look presets)
in:  <clip>/assets/master.mp4 (clip_master.py), words.json (stt_reconcile.py --apply), plan.json, master.json;
     clip "hook" in clips.json ("Lead *tone-2 phrase*"; no hook = captions from 0 s)
out: <clip>/9x16-<look>/index.html + hyperframes.json + assets (hard-linked master and fonts), rewritten only when
     the content changed (keeps render freshness); <clip>/build-<look>.json (caption groups, band warnings). Render:
     cd <clip>/9x16-<look> && hyperframes check --json && hyperframes render . --fps <fps> --output ../renders/<ID>_<look>.mp4

Layout (1080x1920): stacks = main speaker in the top panel 1080 x seam, the other person below; hook and captions sit
in ONE band centred on y = seam on every shot, so on stacks the text covers the hard edge between panels (plus a soft
dark gradient across the seam while a stack is on screen) and it never jumps between shots. Singles' chins end above
the band (clip_plan lifts the low ones); a +24 px face overlap with the text block prints as BAND WARNING.
Hook = frame 0 (the thumbnail), held hook_s, then captions take the band.
"""
import argparse
import html
import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vc_config import OUT_H as H, OUT_W as W, SCRIPTS, Batch  # noqa: E402
from vc_lang import table  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
SEAM_FADE = 300  # px: dark gradient across the seam while a stack is on screen (blends the panel edge)
FONT_DIR = SCRIPTS / "fonts"
FONTS = {  # bundled, SIL Open Font License (OFL.txt beside each file)
    "Bricolage Grotesque": {"file": FONT_DIR / "BricolageGrotesque-latin.woff2", "weight": "200 800"},
    "Unlock": {"file": FONT_DIR / "Unlock-Regular.ttf", "weight": "400"},
}
HF_CONFIG = {"$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
             "paths": {"blocks": "compositions", "components": "compositions/components", "assets": "assets"},
             "media": {"autoProxy": True}, "authoringSkill": "hyperframes"}
SOFT_SHADOW = "text-shadow: 0 0 26px rgba(0,0,0,.6), 0 3px 10px rgba(0,0,0,.65), 0 1px 2px rgba(0,0,0,.7);"

# Look presets. max_chars / min_s = caption group limits, size/weight = caption type, mode "active" = the spoken word
# turns `accent` (others `ink`), "fill" = words appear as spoken over `unread` opacity; hook_* = the frame-0 hook.
PRESETS = {
    "signal": dict(label="Signal", note="Bold white Bricolage, yellow active word, pop",
                   max_chars=17, min_s=1.0, size=88, weight=800, ink="#FFFFFF", accent="#FFD23F",
                   mode="active", hook_size=104, hook_weight=800, band=None, pop=True),
    "signal-unlock": dict(label="Signal Unlock", note="Signal with captions in Unlock (one heavy 400 weight)",
                          max_chars=17, min_s=1.0, size=88, weight=400, ink="#FFFFFF", accent="#FFD23F",
                          mode="active", hook_size=104, hook_weight=800, band=None, pop=True, cap_font="Unlock",
                          cap_spacing="-0.01em"),
    "drench": dict(label="Drench", note="White text on a solid colour slab, words fill in",
                   max_chars=20, min_s=1.3, size=86, weight=500, ink="#FFFFFF", accent="#FFFFFF",
                   mode="fill", unread=0.55, hook_size=100, hook_weight=300, band="#D9431C",
                   hook_tone2_color="#FFFFFF", hook_tone2_weight=700),
    # unread 0.38 vanished over a pink jacket in the first prototype -> 0.5 + heavier shadow
    "editorial": dict(label="Editorial", note="Light large type, orange accent, words fill in",
                      max_chars=19, min_s=1.3, size=98, weight=300, ink="#FFFFFF", accent="#FF8A50",
                      mode="fill", unread=0.5, hook_size=118, hook_weight=300, band=None, shadow=SOFT_SHADOW),
    "hybrid": dict(label="Hybrid", note="Editorial hook over Signal captions",
                   max_chars=17, min_s=1.0, size=88, weight=800, ink="#FFFFFF", accent="#FFD23F",
                   mode="active", hook_size=118, hook_weight=300, hook_accent="#FF8A50", band=None, pop=True,
                   hook_shadow=SOFT_SHADOW),
}


def presets(b=None):
    """Built-in presets + config "look_presets" ({"name": {"base": "signal", …overrides}})."""
    out = dict(PRESETS)
    for name, p in ((b.cfg.get("look_presets") if b else None) or {}).items():
        out[name] = dict(out[p.get("base", "signal")], **{k: v for k, v in p.items() if k != "base"})
    return out


def fonts(b=None):
    out = dict(FONTS)
    for fam, f in ((b.cfg.get("fonts") if b else None) or {}).items():
        out[fam] = {"file": b.resolve(f["file"]), "weight": str(f.get("weight", "400"))}
    return out


def clean_caption(text):
    """Keep ? and !, drop trailing , . ; :"""
    return re.sub(r"[,.;:]+$", "", text)


def group_words(words, min_s=1.3, max_chars=34, pause=0.4):
    """Speech-paced groups: a group closes at a sentence end or a long pause (hard), at a comma once it has run 0.6 s,
    or once it has run min_s / would exceed max_chars. A short tail (one word under 0.4 s) joins the previous group
    when it fits."""
    def chars(g):
        return len(" ".join(clean_caption(w["text"]) for w in g))

    groups, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        dur = w["end"] - cur[0]["start"]
        hard = nxt is None or w["text"][-1] in ".?!" or (nxt["start"] - w["end"] >= pause)
        soft = w["text"][-1] in ",;:" and dur >= 0.6
        full = dur >= min_s or (nxt is not None and chars(cur + [nxt]) > max_chars)
        if hard or soft or full:
            if (groups and len(cur) == 1 and dur < 0.4 and chars(groups[-1] + cur) <= max_chars
                    and cur[0]["start"] - groups[-1][-1]["end"] < pause):
                groups[-1] = groups[-1] + cur
            else:
                groups.append(cur)
            cur = []
    return groups


def faces_out(plan):
    """Face boxes per shot in output pixels."""
    out = []
    for s in plan["shots"]:
        boxes = []
        if s["layout"] == "single":
            c = s["crop"]
            k = H / c["h"]
            boxes = [((p["x"] - c["x"]) * k, (p["y"] - c["y"]) * k, p["w"] * k, p["h"] * k) for p in s["people"]
                     if c["x"] <= p["x"] + p["w"] / 2 <= c["x"] + c["w"]]
        else:
            ppl = sorted(s["people"], key=lambda p: p["x"])
            seam = s.get("seam", H // 2)
            for c, p, y0, ph in zip(s["panels"], s.get("panel_people", [ppl[0], ppl[-1]]), (0, seam), (seam, H - seam)):
                k = ph / c["h"]
                boxes.append(((p["x"] - c["x"]) * k, y0 + (p["y"] - c["y"]) * k, p["w"] * k, p["h"] * k))
        out.append((s, boxes))
    return out


def shot_faces(plan):
    """[(t0, t1, layout, face boxes)] in output time/pixels."""
    fps, t, out = plan["fps"], 0.0, []
    for s, boxes in faces_out(plan):
        n = (s["out_f"] - s["in_f"]) / fps
        out.append((t, t + n, s["layout"], boxes))
        t += n
    return out


def pick_band(bands, sf, t0, t1, text_h=None):
    """First band clear of every face (+24 px) in the shots overlapping [t0, t1]; else the least-bad one.
    text_h = the text block's height: overlap is measured on it (centred in the band) instead of the whole band."""
    boxes = [b for a, z, _, bs in sf if a < t1 and z > t0 for b in bs]

    def overlap(band):
        top, bot = band
        if text_h:
            mid = (top + bot) / 2
            top, bot = mid - text_h / 2, mid + text_h / 2
        return sum(max(0, min(bot, y + h + 24) - max(top, y - 24)) for (x, y, w, h) in boxes)

    for band in bands:
        if overlap(band) == 0:
            return band, 0
    best = min(bands, key=overlap)
    return best, overlap(best)


def phrase_groups(words, min_s, max_chars, weak, pause=0.4):
    """group_words, then move trailing weak words into the next group (same breath only: gap < pause and no
    punctuation after them). The next group may then run up to max_chars + 8 (text-wrap: balance -> 2 lines)."""
    groups = group_words(words, min_s=min_s, max_chars=max_chars, pause=pause)
    for i in range(len(groups) - 1):
        g, n = groups[i], groups[i + 1]
        while (len(g) > 1 and re.sub(r"[^\w]", "", g[-1]["text"].lower().replace("’", "'").split("'")[0]) in weak
               and not re.search(r"[,.;:?!]$", g[-1]["text"]) and n[0]["start"] - g[-1]["end"] < pause
               and len(" ".join(clean_caption(w["text"]) for w in [g[-1]] + n)) <= max_chars + 8):
            n.insert(0, g.pop())
    groups = [g for g in groups if g]
    # a lone word without punctuation joins the next group when the speaker pauses < 0.8 s (one-word flashes)
    out = []
    for g in groups:
        if out and len(out[-1]) == 1 and not re.search(r"[,.;:?!]$", out[-1][-1]["text"]) \
                and g[0]["start"] - out[-1][-1]["end"] < 0.8 \
                and len(" ".join(clean_caption(w["text"]) for w in out[-1] + g)) <= max_chars + 8:
            out[-1] = out[-1] + g
        else:
            out.append(g)
    # still alone (next line full): join the previous line when it's the same breath (a 0.4 s one-word flash)
    merged = []
    for g in out:
        if (merged and len(g) == 1 and g[0]["start"] - merged[-1][-1]["end"] < 0.8
                and not re.search(r"[.?!]$", merged[-1][-1]["text"])
                and len(" ".join(clean_caption(w["text"]) for w in merged[-1] + g)) <= max_chars + 8):
            merged[-1] = merged[-1] + g
        else:
            merged.append(g)
    return merged


def lines(g, st, width=W - 140):
    """Caption lines a group wraps to (~0.52 em per character incl. spaces, measured on Bricolage 800 / Unlock 400)."""
    n = len(" ".join(clean_caption(w["text"]) for w in g))
    return 1 if n * 0.52 * st["size"] <= width else 2


def ap(text_html):
    """Bricolage's ’ nearly closes up at 800 weight (« L’IA » read « LIA »): give it side bearing."""
    return text_html.replace("’", '<span class="ap">’</span>').replace("'", '<span class="ap">’</span>')


def hook_layout(hook, st, width=W - 140):
    """(font px, break before tone 2?) for a 2-line hook. Bricolage advance ~0.50 em at weight 300, ~0.58 em at 800.
    Break mode = lead on line 1, the *tone-2* phrase alone (nowrap) on line 2, sized so the longer line fits; used
    unless it costs > 15 % of size vs a balanced 2-line wrap (then no break, tone 2 may wrap)."""
    em = 0.50 + (st["hook_weight"] - 300) / 500 * 0.08
    parts = [p.strip() for p in re.split(r"\*(.+?)\*", hook)]
    bal = min(st["hook_size"], width * 2 / (max(1, len("".join(parts))) * 1.08 * em))
    if len(parts) >= 2 and parts[0] and parts[1]:
        brk = min(st["hook_size"], width / (max(len(parts[0]), len(parts[1])) * 1.06 * em))
        if brk >= 0.85 * bal:
            return int(brk), True
    return int(bal), False


def hook_html(hook, brk=False):
    parts = re.split(r"\*(.+?)\*", html.escape(hook, quote=False))
    cls = "tone2 nw" if brk else "tone2"
    out = "".join(f'<span class="{cls}">{ap(p)}</span>' if i % 2 else ap(p) for i, p in enumerate(parts))
    return out.replace(' <span class="tone2 nw">', '<br><span class="tone2 nw">', 1) if brk else out


def hook_css(st, size):
    """CSS for #hook-line (shared with covers.py)."""
    tone2 = st.get("hook_tone2_color", st.get("hook_accent", st["accent"]))
    tw = f"font-weight: {st['hook_tone2_weight']};" if st.get("hook_tone2_weight") else ""
    return (f"#hook-line {{ color: {st['ink']}; font-weight: {st['hook_weight']}; font-size: {size}px; line-height: 1.0; "
            f"letter-spacing: -0.03em; {st.get('hook_shadow', '')} }}\n"
            f"#hook-line .tone2 {{ color: {tone2}; {tw} }}\n#hook-line .nw {{ white-space: nowrap; }}\n"
            f".ap {{ margin: 0 0.05em 0 0.03em; }}\n")


def text_shadow(st):
    return "" if st["band"] else st.get("shadow", "text-shadow: 0 4px 18px rgba(0,0,0,.55), 0 2px 4px rgba(0,0,0,.55);")


def slab_css(st):
    return (f".slab {{ background: {st['band']}; padding: 40px 72px 46px; width: 100%; box-sizing: border-box; }}"
            if st["band"] else "")


def font_faces(fams, reg, prefix="assets/fonts/"):
    css = []
    for fam in fams:
        f = reg[fam]
        fmt = {"woff2": "woff2", "woff": "woff", "ttf": "truetype", "otf": "opentype"}[Path(f["file"]).suffix[1:].lower()]
        css.append(f"@font-face {{ font-family: '{fam}'; src: url('{prefix}{Path(f['file']).name}') format('{fmt}'); "
                   f"font-weight: {f['weight']}; font-display: block; }}")
    return "\n".join(css)


def link(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if dst.stat().st_size == src.stat().st_size and dst.stat().st_mtime >= src.stat().st_mtime:
            return
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def write_if_changed(path, text):
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def load_clip_words(path):
    data = json.load(open(path, encoding="utf-8"))
    return data["words"] if isinstance(data, dict) else data


def build(b, clip_id, looks):
    d = b.clip_dir(clip_id)
    ep, clip = b.episode_of(clip_id), b.clip(clip_id)
    lang = ep["language"]
    plan = json.load(open(d / "plan.json", encoding="utf-8"))
    hook = clip.get("hook", "")
    hook_s = b.hook_s if hook else 0.0
    D = json.load(open(d / "master.json", encoding="utf-8"))["duration_s"]
    drop, weak = table("CAPTION_DROP", lang), table("WEAK", lang)
    words = [w for w in load_clip_words(d / "words.json")
             if re.sub(r"[^\w]", "", w["text"].lower()) not in drop and w.get("type") != "filler" and w["start"] < D]
    words = [dict(w, text=w["text"].replace("'", "’")) for w in words]
    sfaces = shot_faces(plan)
    bands = [(b.seam - 110, b.seam + 110)]  # 2 caption lines ~185 px, 2 hook lines ~210 px
    seam = b.seam
    reg, pre = fonts(b), presets(b)
    for key in looks:
        if key not in pre:
            raise SystemExit(f"unknown look {key!r}; presets: {', '.join(pre)}")
        st = pre[key]
        warns = []

        def slot(t0, t1, what, text_h=None):
            (top, bot), bad = pick_band(bands, sfaces, t0, t1, text_h)
            if bad:
                warns.append({"what": what, "t0": round(t0, 2), "t1": round(t1, 2), "overlap_px": round(bad), "band": [top, bot]})
                print(f"  BAND WARNING {what} {t0:.1f}-{t1:.1f}s: {bad:.0f} px of face overlap at best band {top}-{bot}")
            return f'<div class="band-slot" style="top: {top}px; height: {bot - top}px">'

        groups = [g for g in phrase_groups(words, st["min_s"], st["max_chars"], weak) if g[-1]["end"] > hook_s]
        parts, js = [], []
        stacks = []  # stack shots on screen (adjacent ones merged) -> seam gradient
        for a, z, lay, _ in sfaces:
            if lay == "stack":
                if stacks and abs(stacks[-1][1] - a) < 0.01:
                    stacks[-1][1] = z
                else:
                    stacks.append([a, z])
        for si, (a, z) in enumerate(stacks):
            parts.append(f'<div id="seam-{si}" class="clip seam" data-start="{round(a, 3)}" '
                         f'data-duration="{round(min(z, D) - a, 3)}" data-track-index="1"></div>')
        hsize, hbrk = hook_layout(hook, st) if hook else (st["hook_size"], False)
        if hook:
            parts.append(f'<div id="hook" class="clip" data-start="0" data-duration="{hook_s}" data-track-index="3">'
                         f'{slot(0, hook_s, "hook", hsize * (2 if hbrk or len(hook) > 18 else 1))}'
                         f'<div id="hook-line" class="{"slab" if st["band"] else ""}">'
                         f'<span id="hook-text">{hook_html(hook, hbrk)}</span></div></div></div>')
            js.append(f'tl.to("#hook-line", {{opacity: 0, duration: 0.12, ease: "power2.in"}}, {hook_s - 0.12});')
            js.append(f'tl.set("#hook-line", {{opacity: 0, visibility: "hidden"}}, {hook_s});')
        for gi, g in enumerate(groups):
            gs = round(max(hook_s, g[0]["start"] - 0.05), 3)
            nxt = groups[gi + 1][0]["start"] - 0.05 if gi + 1 < len(groups) else D
            ge = round(min(max(g[-1]["end"] + 0.25, gs + 0.4), nxt, D), 3)
            if ge - gs < 0.2:
                continue
            gid = f"cap-{gi}"
            spans = []
            for wi, w in enumerate(g):
                wid = f"{gid}-w{wi}"
                spans.append(f'<span class="w" id="{wid}">{ap(html.escape(clean_caption(w["text"]), quote=False))}</span>')
                on = round(max(gs, w["start"]), 3)
                if st["mode"] == "active":
                    off = round(max(on + 0.06, g[wi + 1]["start"] if wi + 1 < len(g) else ge), 3)
                    js.append(f'tl.set("#{wid}", {{color: "{st["accent"]}"}}, {on});')
                    if off < ge:
                        js.append(f'tl.set("#{wid}", {{color: "{st["ink"]}"}}, {off});')
                else:
                    js.append(f'tl.set("#{wid}", {{opacity: 1}}, {on});')
            parts.append(f'<div id="{gid}" class="clip cap" data-start="{gs}" data-duration="{round(ge - gs, 3)}" '
                         f'data-track-index="2">{slot(gs, ge, gid, lines(g, st) * st["size"] * 1.04)}<div class="cap-line{" slab" if st["band"] else ""}" '
                         f'id="{gid}-line">{" ".join(spans)}</div></div></div>')
            if st.get("pop"):
                js.append(f'tl.fromTo("#{gid}-line", {{scale: 0.94}}, {{scale: 1, duration: 0.1, ease: "power2.out"}}, {gs});')
            js.append(f'tl.set("#{gid}-line", {{opacity: 0, visibility: "hidden"}}, {ge});')
        unread = f"opacity: {st['unread']};" if st["mode"] == "fill" else ""
        font = st.get("font", "Bricolage Grotesque")
        cap_font = st.get("cap_font", font)
        fams = list(dict.fromkeys([font, cap_font]))
        css = f"""
{font_faces(fams, reg)}
#root {{ position: relative; width: {W}px; height: {H}px; overflow: hidden; background: #000;
  font-family: '{font}', sans-serif; font-optical-sizing: auto; }}
#footage {{ position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }}
.seam {{ position: absolute; left: 0; right: 0; top: {seam - SEAM_FADE // 2}px; height: {SEAM_FADE}px; pointer-events: none;
  background: linear-gradient(to bottom, rgba(0,0,0,0) 0%, rgba(0,0,0,.42) 38%, rgba(0,0,0,.5) 50%, rgba(0,0,0,.42) 62%, rgba(0,0,0,0) 100%); }}
.band-slot {{ position: absolute; left: 0; right: 0; display: flex; align-items: center; justify-content: center; overflow: visible; }}
.cap-line, #hook-line {{ text-align: center; padding: 0 70px; text-wrap: balance; {text_shadow(st)} }}
.cap-line {{ color: {st['ink']}; font-family: '{cap_font}', sans-serif; font-synthesis: none; font-weight: {st['weight']};
  font-size: {st['size']}px; line-height: 1.04; letter-spacing: {st.get('cap_spacing', '-0.02em')}; }}
.cap-line .w {{ display: inline-block; {unread} }}
{hook_css(st, hsize)}{slab_css(st)}
"""
        doc = f"""<!doctype html>
<html lang="{html.escape(lang)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width={W}, height={H}">
<script src="https://cdn.jsdelivr.net/npm/gsap@3.15.0/dist/gsap.min.js"></script>
<title>{html.escape(clip_id)} 9x16 {key}</title>
<style>{css}</style>
</head>
<body>
<div id="root" data-composition-id="main" data-start="0" data-duration="{D}" data-width="{W}" data-height="{H}">
  <video id="footage" class="clip" src="assets/master.mp4" data-start="0" data-duration="{D}" data-track-index="0" data-has-audio="true" playsinline></video>
  {chr(10).join("  " + p for p in parts)}
</div>
<script>
const tl = gsap.timeline({{ paused: true }});
{chr(10).join(js)}
window.__timelines["main"] = tl;
</script>
</body>
</html>
"""
        out = d / f"9x16-{key}"
        link(d / "assets" / "master.mp4", out / "assets" / "master.mp4")
        for fam in fams:
            src = Path(reg[fam]["file"])
            link(src, out / "assets" / "fonts" / src.name)
        out.mkdir(parents=True, exist_ok=True)
        write_if_changed(out / "hyperframes.json", json.dumps(HF_CONFIG, indent=2))
        changed = write_if_changed(out / "index.html", doc)
        caps = [" ".join(clean_caption(w["text"]) for w in g) for g in groups]
        write_if_changed(d / f"build-{key}.json", json.dumps({"look": key, "hook": hook, "hook_px": hsize,
                                                               "hook_break": hbrk, "groups": caps,
                                                               "band_warnings": warns}, ensure_ascii=False, indent=1))
        print(f"{clip_id} {key}: {len(groups)} caption groups, {D}s -> {out / 'index.html'}"
              + ("" if changed else " (unchanged)"))
        print("   ", " | ".join(caps[:8]), "…")


if __name__ == "__main__":
    ap_ = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap_.add_argument("-c", "--config", help="batch config (default $VC_CONFIG or ./clips.json)")
    ap_.add_argument("--list", action="store_true", help="print the look presets")
    ap_.add_argument("id", nargs="?", help="clip id")
    ap_.add_argument("looks", nargs="*", help="look preset names")
    a = ap_.parse_args()
    if a.list:
        batch = Batch(a.config) if (a.config or Path("clips.json").exists()) else None
        for name, p in presets(batch).items():
            print(f"{name:14s} {p['label']}: {p['note']}" if "note" in p else name)
        sys.exit(0)
    if not a.id:
        ap_.error("clip id required")
    batch = Batch(a.config)
    build(batch, a.id, a.looks or batch.looks_for(a.id))
