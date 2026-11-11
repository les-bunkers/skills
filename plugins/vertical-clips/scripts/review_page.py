"""Local review page for a clip batch: <out>/index.html (open it from disk, nothing is uploaded).

usage: python review_page.py [-c clips.json] [--start YYYY-MM-DD]
Per clip: player with look tabs (poster = hook frame), hook, topic, tags, QC (loudness, shots, raea flags, caption-band
warnings), words the agent marked CHECK first, every text fix, post text with a copy button, links to the framing sheet
and the clip folder. Top: one look switch for every clip, filters, posting calendar.
Calendar (also <out>/_review/schedule.json, read by export_finals.py): one lane per clip "account" (default "main"),
config schedule.per_day (int or {"lane": n}, default 2) clips a day from --start (or schedule.start, else today);
episodes alternate by rank and a tag never repeats within the previous schedule.tag_gap (default 3) slots of a lane.
A look shows for a clip only when its render is newer than the clip's master (a stale render = old layout, hidden).
Clip "schedule": false keeps a clip off the calendar (an alternate cut).
"""
import argparse
import datetime as dt
import html
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_vertical as BV  # noqa: E402
from vc_config import FFMPEG, Batch, jload  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


def poster(mp4, jpg):
    if mp4.exists() and (not jpg.exists() or jpg.stat().st_mtime < mp4.stat().st_mtime):
        subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", "0.5", "-i", str(mp4), "-frames:v", "1", "-vf", "scale=540:-1",
                        "-q:v", "4", str(jpg)], check=False)
    return jpg.exists()


def clip_info(b, cid, looks):
    c, d = b.clip(cid), b.out / cid
    info = {}
    master = d / "assets" / "master.mp4"
    for s in looks:
        mp4 = d / "renders" / f"{cid}_{s}.mp4"
        if mp4.exists() and master.exists() and mp4.stat().st_mtime >= master.stat().st_mtime:
            jpg = d / "renders" / f"{cid}_{s}.jpg"
            raea = jload(d / "renders" / f"raea-{s}.json", {}) or {}
            build = jload(d / f"build-{s}.json", {}) or {}
            chk = jload(d / f"check-{s}.json", {}) or {}
            info[s] = {"mp4": f"{cid}/renders/{mp4.name}", "poster": f"{cid}/renders/{jpg.name}" if poster(mp4, jpg) else "",
                       "raea": [f"{i['p']} {i['rule']} {i['at']:.1f}s: {i['says']}" for i in raea.get("issues", [])],
                       "bands": build.get("band_warnings", []), "check_ok": chk.get("ok") if chk else None}
    fixes = jload(d / "stt_fixes.json", []) or []
    checks = [f for f in fixes if str(f.get("why", "")).startswith("CHECK")]
    tf = (d / "text_fixes.md").read_text(encoding="utf-8").splitlines() if (d / "text_fixes.md").exists() else []
    m = jload(d / "master.json", {}) or {}
    shots = (jload(d / "plan.json", {}) or {}).get("shots", [])
    words = jload(d / "words.json", []) or []
    words = words["words"] if isinstance(words, dict) else words
    return {
        "id": cid, "episode": c.get("episode") or cid.split("-")[0], "rank": c.get("rank"), "hook": c.get("hook", ""),
        "topic": c.get("topic", ""), "notes": c.get("notes", ""), "tags": c.get("tags", []),
        "lane": c.get("account", "main"), "scheduled": c.get("schedule", True) is not False,
        "dur": m.get("duration_s"), "lufs": m.get("I"), "tp": m.get("TP"),
        "shots": len(shots), "stacks": sum(1 for s in shots if s.get("layout") == "stack"),
        "review_shots": sum(1 for s in shots if s.get("review")), "looks": info,
        "fix_count": len(fixes), "checks": checks, "fixes_log": [x[2:] for x in tf if x.startswith("- ")],
        "text": " ".join(w["text"] for w in words), "post": c.get("post", ""),
        "plan_sheet": f"{cid}/plan_sheet.jpg" if (d / "plan_sheet.jpg").exists() else "",
        "covers": [s for s in looks if (d / "renders" / f"{cid}_{s}_cover.jpg").exists()],
    }


def schedule(b, clips, start):
    sch = b.cfg.get("schedule", {})
    per_day, gap = sch.get("per_day", 2), int(sch.get("tag_gap", 3))
    lanes = list(dict.fromkeys(c["lane"] for c in clips if c["scheduled"]))
    tags = {c["id"]: set(c["tags"]) for c in clips}
    orders = {}
    for lane in lanes:
        mine = [c for c in clips if c["lane"] == lane and c["scheduled"]]
        eps = {}
        for c in sorted(mine, key=lambda c: (c["rank"] if c["rank"] is not None else 999, c["id"])):
            eps.setdefault(c["episode"], []).append(c["id"])
        queue, qs = [], list(eps.values())
        while any(qs):  # episodes alternate, each in rank order
            for q in qs:
                if q:
                    queue.append(q.pop(0))
        order = []
        while queue:
            recent = set().union(*(tags[i] for i in order[-gap:])) if order and gap else set()
            pick = next((i for i in queue if not tags[i] & recent), queue[0])
            queue.remove(pick)
            order.append(pick)
        orders[lane] = order
    n = {lane: (per_day.get(lane, 1) if isinstance(per_day, dict) else int(per_day)) for lane in lanes}
    ndays = max([math.ceil(len(orders[lane]) / n[lane]) for lane in lanes] or [0])
    days = []
    for k in range(ndays):
        day = start + dt.timedelta(days=k)
        days.append({"date": day.isoformat(), "label": day.strftime("%a %d %b"),
                     "lanes": {lane: orders[lane][n[lane] * k:n[lane] * k + n[lane]] for lane in lanes}})
    return days, lanes, n


def esc(s):
    return html.escape(str(s), quote=True)


def hook_html(h):
    return re.sub(r"\*(.+?)\*", r'<em>\1</em>', esc(h))


def render(b, clips, days, lanes, per, looks, out):
    pre = BV.presets(b)
    n_looks = sum(len(c["looks"]) for c in clips)
    n_checks = sum(len(c["checks"]) for c in clips)
    rows = []
    for c in clips:
        tabs = "".join(f'<button class="tab" data-look="{esc(s)}" {"disabled" if s not in c["looks"] else ""} '
                       f'title="{esc(pre[s]["label"] + " — " + pre[s].get("note", ""))}">{esc(s)}</button>' for s in looks)
        first = next(iter(c["looks"]), None)
        vid = (f'<video controls preload="none" playsinline poster="{esc(c["looks"][first]["poster"])}" '
               f'src="{esc(c["looks"][first]["mp4"])}"></video>' if first else '<div class="novid">Not rendered yet</div>')
        chips = "".join(f'<span class="chip">{esc(t)}</span>' for t in c["tags"])
        checks = "".join(f'<li><b>{esc("hunk " + str(f["hunk"]) if "hunk" in f else "word " + str(f.get("word", f.get("words", ""))))}</b> '
                         f'« {esc(f["text"])} » <span class="why">{esc(f["why"][6:].strip(" :"))}</span></li>'
                         for f in c["checks"])
        qc = []
        if c["lufs"] is not None:
            qc.append(f'{c["lufs"]:.1f} LUFS · {c["tp"]:.1f} dBTP')
        qc.append(f'{c["shots"]} shots, {c["stacks"]} stacked' + (f', {c["review_shots"]} without a face' if c["review_shots"] else ""))
        if any(lk.get("check_ok") is False for lk in c["looks"].values()):
            qc.append("hyperframes check not ok")
        flags = sorted({f for lk in c["looks"].values() for f in lk["raea"]})
        flag_html = "".join(f"<li>{esc(f)}</li>" for f in flags)
        band_html = "".join(f'<li>{esc(s)}: {esc(bw["what"])} {bw["t0"]}–{bw["t1"]} s, {bw["overlap_px"]} px on a face</li>'
                            for s, lk in c["looks"].items() for bw in lk["bands"])
        fixes = "".join(f"<li>{esc(x)}</li>" for x in c["fixes_log"])
        post = (f'<div class="post"><div class="label">Post text</div><p>{esc(c["post"])}</p>'
                f'<button class="copy" data-copy="{esc(c["post"])}">Copy</button></div>' if c["post"] else "")
        covers = ("Covers: " + " ".join(f'<a href="{esc(c["id"])}/renders/{esc(c["id"])}_{esc(k)}_cover.jpg">{esc(k)}</a>'
                                        for k in c["covers"]) + " · ") if c["covers"] else ""
        rows.append(f"""
<article class="clip" id="{esc(c['id'])}" data-looks="{esc(json.dumps(c['looks']))}" data-check="{1 if c['checks'] else 0}"
  data-rendered="{1 if c['looks'] else 0}">
  <div class="player">
    <div class="tabs" role="group" aria-label="Look">{tabs}</div>
    {vid}
  </div>
  <div class="info">
    <div class="meta"><span class="id">{esc(c['id'])}</span><span>{esc(c['episode'])}</span>
      <span>{(c['dur'] or 0):.0f} s</span><span class="acct">{esc(c['lane'])}</span></div>
    <h2 class="hook">{hook_html(c['hook']) or esc(c['id'])}</h2>
    <p class="claim">{esc(c['topic'])}</p>
    <div class="chips">{chips}</div>
    {'<section class="checks"><div class="label">Check these words (' + str(len(c['checks'])) + ')</div><ul>' + checks + '</ul></section>' if checks else ''}
    <div class="qc"><span>{' · '.join(qc)}</span></div>
    {'<ul class="flags">' + flag_html + band_html + '</ul>' if flag_html or band_html else ''}
    {post}
    <details><summary>Caption text and {c['fix_count']} corrections</summary>
      <p class="fulltext">{esc(c['text'])}</p><ul class="fixes">{fixes}</ul>
      <p class="notes">{esc(c['notes'])}</p>
      <p class="paths">{'<a href="' + esc(c['plan_sheet']) + '">Framing sheet</a> · ' if c['plan_sheet'] else ''}{covers}<a href="{esc(c['id'])}/">Clip folder</a></p>
    </details>
  </div>
</article>""")
    by_id = {c["id"]: c for c in clips}

    def slot(i):
        if not i:
            return '<span class="slot empty"></span>'
        return f'<a href="#{esc(i)}" class="slot"><span class="sid">{esc(i)}</span>{hook_html(by_id[i]["hook"])}</a>'
    ncol = sum(per[lane] for lane in lanes)
    cols = f"92px repeat({max(ncol, 1)}, minmax(0, 1fr))"
    head = '<li class="calhead" style="grid-template-columns: ' + cols + '"><span></span>' + "".join(
        f'<span style="grid-column: span {per[lane]}">{esc(lane)}</span>' for lane in lanes) + "</li>"
    cal = head + "".join(f'<li style="grid-template-columns: {cols}"><span class="day">{esc(d["label"])}</span>'
                         + "".join(slot(i) for lane in lanes for i in (d["lanes"][lane] + [None] * per[lane])[:per[lane]])
                         + "</li>" for d in days)
    look_btns = "".join(f'<button class="seg" data-look="{esc(s)}"><b>{esc(s)}</b></button>' for s in looks)
    title = esc(b.cfg.get("title", "Vertical clips"))
    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
@font-face {{ font-family: 'Bricolage Grotesque'; src: url('_review/BricolageGrotesque-latin.woff2') format('woff2'); font-weight: 200 800; }}
:root {{
  --bg: #121110; --surface: #1B1A18; --raise: #252320; --line: #2F2C28; --ink: #EDE9E2; --muted: #A9A298;
  --accent: #FF8A50; --accent-ink: #1A0E07; --warn: #FFB25C;
  --r: 10px; --ease: cubic-bezier(.22,1,.36,1);
  color-scheme: dark;
}}
* {{ box-sizing: border-box; }}
html {{ background: var(--bg); }}
body {{ margin: 0; color: var(--ink); font: 15px/1.5 'Bricolage Grotesque', system-ui, sans-serif; font-optical-sizing: auto; }}
::selection {{ background: var(--accent); color: var(--accent-ink); }}
a {{ color: var(--ink); text-underline-offset: 3px; text-decoration-color: var(--accent); }}
:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
button {{ font: inherit; color: inherit; cursor: pointer; transition: background-color .24s var(--ease), color .24s var(--ease), border-color .24s var(--ease), scale .24s var(--ease); }}
button:hover {{ transition-duration: .16s; }}
button:active {{ scale: .96; transition-duration: .08s; }}
button:disabled {{ cursor: default; opacity: .35; }}
button:disabled:active {{ scale: 1; }}
header {{ position: sticky; top: 0; z-index: 5; background: color-mix(in srgb, var(--bg) 92%, transparent); backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--line); padding: 14px 24px; display: flex; flex-wrap: wrap; gap: 12px 24px; align-items: center; }}
h1 {{ margin: 0; font-size: 22px; font-weight: 300; letter-spacing: -0.02em; }}
.count {{ color: var(--muted); font-variant-numeric: tabular-nums; }}
.segs, .filters {{ display: flex; gap: 4px; background: var(--surface); padding: 4px; border-radius: 999px; }}
.seg, .flt {{ border: 0; background: transparent; padding: 6px 14px; border-radius: 999px; color: var(--muted); }}
.seg:hover, .flt:hover {{ background: var(--raise); color: var(--ink); }}
.seg[aria-pressed=true], .flt[aria-pressed=true] {{ background: var(--accent); color: var(--accent-ink); }}
main {{ max-width: 1240px; margin: 0 auto; padding: 24px; }}
.intro {{ margin: 8px 0 40px; }}
.intro h2.sec {{ margin: 0 0 12px; font-size: 13px; font-weight: 600; color: var(--muted); }}
.cal {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }}
.cal li {{ display: grid; gap: 6px; align-items: stretch; }}
.calhead span {{ font-size: 12px; color: var(--muted); padding: 0 12px; }}
.slot.empty {{ background: transparent; }}
.day {{ color: var(--muted); font-size: 13px; padding: 10px 0; font-variant-numeric: tabular-nums; }}
.slot {{ display: block; text-decoration: none; padding: 8px 12px; border-radius: 8px; background: var(--surface); font-size: 13px; line-height: 1.3;
  transition: background-color .24s var(--ease), scale .24s var(--ease); }}
.slot:hover {{ background: var(--raise); transition-duration: .16s; }}
.slot:active {{ scale: .975; transition-duration: .08s; }}
.slot em, .hook em {{ font-style: normal; color: var(--accent); }}
.sid {{ display: block; font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }}
.clip {{ display: grid; grid-template-columns: 300px minmax(0, 1fr); gap: 32px; padding: 36px 0; border-top: 1px solid var(--line); scroll-margin-top: 90px; }}
.clip[hidden] {{ display: none; }}
.player video, .novid {{ width: 300px; aspect-ratio: 9 / 16; border-radius: var(--r); background: #000; display: block; }}
.novid {{ display: grid; place-items: center; color: var(--muted); }}
.tabs {{ display: flex; gap: 4px; margin-bottom: 8px; }}
.tab {{ flex: 1; border: 1px solid var(--line); background: var(--surface); border-radius: 8px; padding: 6px 8px; font-weight: 600; font-size: 12px; }}
.tab:not(:disabled):hover {{ background: var(--raise); }}
.tab[aria-pressed=true] {{ background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }}
.meta {{ display: flex; flex-wrap: wrap; gap: 6px 14px; color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }}
.meta .id {{ color: var(--ink); font-weight: 700; }}
.hook {{ margin: 10px 0 12px; font-size: 40px; line-height: 1.04; font-weight: 300; letter-spacing: -0.03em; text-wrap: balance; }}
.claim {{ margin: 0 0 12px; max-width: 70ch; }}
.chips {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 16px; }}
.chip {{ font-size: 12px; padding: 3px 10px; border-radius: 999px; background: var(--raise); color: var(--muted); }}
.label {{ font-size: 13px; font-weight: 600; color: var(--muted); margin-bottom: 6px; }}
.checks {{ background: color-mix(in srgb, var(--accent) 12%, var(--surface)); border-radius: var(--r); padding: 14px 18px; margin-bottom: 14px; }}
.checks .label {{ color: var(--accent); }}
.checks ul, .flags, .fixes {{ margin: 0; padding-left: 18px; }}
.checks li {{ margin: 4px 0; }}
.why {{ color: var(--muted); }}
.qc {{ font-size: 13px; color: var(--muted); font-variant-numeric: tabular-nums; margin-bottom: 6px; }}
.flags {{ font-size: 13px; color: var(--warn); margin-bottom: 12px; }}
.post {{ background: var(--surface); border-radius: var(--r); padding: 14px 18px; margin: 12px 0; position: relative; }}
.post p {{ margin: 0; white-space: pre-wrap; max-width: 70ch; padding-right: 72px; }}
.copy {{ position: absolute; top: 12px; right: 12px; border: 1px solid var(--line); background: var(--raise); border-radius: 8px; padding: 4px 12px; font-size: 13px; }}
.copy:hover {{ border-color: var(--accent); }}
details {{ margin-top: 10px; }}
summary {{ cursor: pointer; color: var(--muted); font-size: 13px; padding: 6px 0; transition: color .24s var(--ease); }}
summary:hover {{ color: var(--ink); transition-duration: .16s; }}
.fulltext {{ max-width: 70ch; background: var(--surface); padding: 14px 18px; border-radius: var(--r); }}
.fixes, .notes, .paths {{ font-size: 13px; color: var(--muted); max-width: 80ch; }}
@media (max-width: 760px) {{
  header {{ padding: 12px 16px; position: static; }} main {{ padding: 16px; }}
  .clip {{ grid-template-columns: 1fr; }}
  .player video, .novid {{ width: 100%; max-width: 360px; }}
  .cal li {{ grid-template-columns: 1fr 1fr !important; }} .day {{ grid-column: 1 / -1; padding: 4px 0 0; }}
  .slot.empty, .calhead {{ display: none; }}
  .hook {{ font-size: 32px; }}
}}
@media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; }} }}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <span class="count">{len(clips)} clips · {n_looks} renders · {n_checks} words to check</span>
  <div class="segs" role="group" aria-label="Look for every clip">{look_btns}</div>
  <div class="filters" role="group" aria-label="Filter">
    <button class="flt" data-f="all" aria-pressed="true">All</button>
    <button class="flt" data-f="check">Words to check</button>
    <button class="flt" data-f="rendered">Rendered</button>
  </div>
</header>
<main>
  <div class="intro"><h2 class="sec">Posting plan</h2><ol class="cal">{cal}</ol></div>
  {''.join(rows)}
</main>
<script>
const clips = [...document.querySelectorAll('.clip')];
function setLook(el, look) {{
  const looks = JSON.parse(el.dataset.looks);
  if (!looks[look]) return;
  const v = el.querySelector('video');
  if (v) {{ const t = v.currentTime, playing = !v.paused; v.poster = looks[look].poster; v.src = looks[look].mp4;
    if (t) {{ v.currentTime = t; }} if (playing) v.play(); }}
  el.querySelectorAll('.tab').forEach(b => b.setAttribute('aria-pressed', b.dataset.look === look));
}}
clips.forEach(el => {{
  const first = Object.keys(JSON.parse(el.dataset.looks))[0];
  if (first) setLook(el, first);
  el.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => setLook(el, b.dataset.look)));
}});
document.querySelectorAll('.seg').forEach(b => b.addEventListener('click', () => {{
  document.querySelectorAll('.seg').forEach(x => x.setAttribute('aria-pressed', x === b));
  clips.forEach(el => setLook(el, b.dataset.look));
  try {{ localStorage.setItem('look', b.dataset.look); }} catch (e) {{}}
}}));
document.querySelectorAll('.flt').forEach(b => b.addEventListener('click', () => {{
  document.querySelectorAll('.flt').forEach(x => x.setAttribute('aria-pressed', x === b));
  clips.forEach(el => el.hidden = (b.dataset.f === 'check' && el.dataset.check !== '1') || (b.dataset.f === 'rendered' && el.dataset.rendered !== '1'));
}}));
document.querySelectorAll('.copy').forEach(b => b.addEventListener('click', async () => {{
  try {{ await navigator.clipboard.writeText(b.dataset.copy); b.textContent = 'Copied'; setTimeout(() => b.textContent = 'Copy', 1400); }} catch (e) {{}}
}}));
document.addEventListener('play', e => document.querySelectorAll('video').forEach(v => {{ if (v !== e.target) v.pause(); }}), true);
try {{ const l = localStorage.getItem('look'); if (l) document.querySelector(`.seg[data-look="${{l}}"]`)?.click(); }} catch (e) {{}}
</script>
</body>
</html>
"""
    out.write_text(doc, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-c", "--config", help="batch config (default $VC_CONFIG or ./clips.json)")
    ap.add_argument("--start", help="first posting day, YYYY-MM-DD (default: config schedule.start, else today)")
    a = ap.parse_args()
    b = Batch(a.config)
    start = dt.date.fromisoformat(a.start or b.cfg.get("schedule", {}).get("start") or dt.date.today().isoformat())
    ids = [c for c in b.clip_ids() if (b.out / c / "plan.json").exists()]
    looks = list(dict.fromkeys(s for cid in ids for s in b.looks_for(cid)))
    clips = [clip_info(b, cid, b.looks_for(cid)) for cid in ids]
    days, lanes, per = schedule(b, clips, start)
    (b.out / "_review").mkdir(parents=True, exist_ok=True)
    shutil.copy2(BV.FONTS["Bricolage Grotesque"]["file"], b.out / "_review" / "BricolageGrotesque-latin.woff2")
    order = {i: k for k, d in enumerate(days) for ids_ in d["lanes"].values() for i in ids_}
    clips.sort(key=lambda c: (order.get(c["id"], 999), c["id"]))
    (b.out / "_review" / "schedule.json").write_text(json.dumps(days, ensure_ascii=False, indent=1), encoding="utf-8")
    render(b, clips, days, lanes, per, looks, b.out / "index.html")
    span = f"{days[0]['date']} → {days[-1]['date']}" if days else "no days"
    print(f"{len(clips)} clips, {sum(len(c['looks']) for c in clips)} renders, {len(days)} days ({span}) -> "
          f"{b.out / 'index.html'}")


if __name__ == "__main__":
    main()
