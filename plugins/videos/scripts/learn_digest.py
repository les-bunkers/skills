# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Digest past agent sessions for /videos:learn: the pipeline calls that failed or looked wrong, what the agent said
right after, the human's messages (corrections, rejections, interruptions), one Markdown file per session.

Inputs: Claude Code transcripts of one project. Claude Code keeps them as JSONL in
<config dir>/projects/<project path with every non-alphanumeric character replaced by "-">/<session>.jsonl, subagents
in <session>/subagents/*.jsonl (config dir = $CLAUDE_CONFIG_DIR, else ~/.claude).
Outputs in --out: <session>.md for every session with pipeline activity in the window, index.tsv (one row per
digest) and groups.json (digests packed into reader groups of at most --group-kb, for parallel readers).
--state FILE remembers digested transcripts; unchanged ones are skipped next time (written after the run).

Focus: "pipeline" (default) = the DaVinci Resolve MCP tools, the video skills and shell commands running the
pipeline's scripts and tools (videos.py, video_ci, resolve_build, tighten, ffmpeg, hyperframes, ...); "resolve" =
the Resolve MCP only; "all" = every tool call. --match REGEX adds tool names or commands.

Usage:
  python learn_digest.py --out DIR [--project PATH] [--projects-dir DIR] [--since YYYY-MM-DD] [--until YYYY-MM-DD]
                         [--focus pipeline|resolve|all] [--match REGEX] [--state FILE] [--ok] [--include-active]
                         [--group-kb 250]
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

RESOLVE_TOOL = "mcp__DaVinci_Resolve__"
PIPE_CMD = re.compile(
    r"videos\.py|video_ci|decide\.py|claims_check|variants\.py|beats\.py|import_words|stt_grok|sync(_check)?\.py|"
    r"angles_scan|angle_sheet|rough\.py|words_dump|phrase_trims|tighten\.py|join_check|resolve_build|"
    r"resolve_multicam|render_finals|finals_check|loudness\.py|editlib|audio.?cleanup|reuse|ears|vertical.?clips|"
    r"ffmpeg|ffprobe|hyperframes|raea|DaVinci|fusionscript",
    re.I,
)
PIPE_SKILL = re.compile(r"^(videos|audio-cleanup|vertical-clips|agent-ears)\b", re.I)
FAIL_MCP = re.compile(
    r"Traceback|Error|error|[Ee]xception|\bFalse\b|\bNone\b|\bnull\b|failed|Failed|timed out|[Tt]imeout|not found|"
    r"cannot|can't|[Uu]nable|[Oo]ffline|[Ii]nvalid|refused|denied|\[\]|\{\}|missing|Missing|not running"
)
FAIL_SHELL = re.compile(
    r"Traceback|^\w*Error\b|Error:|error:|ERROR|FAILED|[Ff]ailed|timed out|No such file|not recognized|"
    r"command not found|[Ee]xit code [1-9]|Permission denied|EPERM|EBUSY|Killed|RED\b",
    re.M,
)
SAID_WRONG = re.compile(
    r"didn't|did not|doesn't|does not|silently|fail|broke|wrong|trap|bug|instead|workaround|no effect|ignored|"
    r"not work|won't|can't|cannot|unreliable|returned (False|None)|timed out|stuck|crash|hang|mismatch|off by|"
    r"lost|turns out|actually|my mistake|I was wrong|retry|rejected",
    re.I,
)
VIDEO_CTX = re.compile(
    r"resolve|timeline|render|multicam|marker|subtitle|caption|fusion|clip|cut|tighten|rough|master|loudness|LUFS|"
    r"audio|frame|fps|ffmpeg|hyperframes|transcript|stt|angle|grade|LUT|video|short|hook|CI\b",
    re.I,
)
SKIP_HUMAN = re.compile(r"^<(command-|local-command|system-reminder|task-notification)|^Caveat:", re.I)
DENIED = re.compile(r"user doesn't want to proceed|user rejected|permission.*denied by user", re.I)


def text_of(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    out = []
    for b in content:
        if isinstance(b, dict):
            if b.get("type") == "text":
                out.append(b.get("text", ""))
            elif b.get("type") == "image":
                out.append("[image]")
        else:
            out.append(str(b))
    return "\n".join(out)


def clip(s, head, tail=0):
    s = (s or "").strip()
    if len(s) <= head + tail + 20:
        return s
    if tail:
        return s[:head] + f"\n…[{len(s) - head - tail} chars cut]…\n" + s[-tail:]
    return s[:head] + f" …[+{len(s) - head} chars]"


def project_dir(args):
    root = Path(args.projects_dir) if args.projects_dir else (
        Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" if os.environ.get("CLAUDE_CONFIG_DIR")
        else Path.home() / ".claude" / "projects")
    proj = Path(args.project or os.getcwd()).resolve()
    d = root / re.sub(r"[^A-Za-z0-9]", "-", str(proj))
    if not d.is_dir():
        near = [p.name for p in root.glob("*") if proj.name.lower() in p.name.lower()] if root.is_dir() else []
        sys.exit(f"no transcripts at {d}" + (f"; similar: {', '.join(near[:8])} (pass --projects-dir/--project)" if near else ""))
    return d


def focus_of(args):
    extra = re.compile(args.match, re.I) if args.match else None

    def is_focus(name, inp):
        if args.focus == "all":
            return True
        if name.startswith(RESOLVE_TOOL):
            return True
        if extra and (extra.search(name) or extra.search(json.dumps(inp, ensure_ascii=False)[:4000])):
            return True
        if args.focus == "resolve":
            return False
        if name == "Skill":
            return bool(PIPE_SKILL.search(str(inp.get("skill", ""))))
        if name in ("Bash", "PowerShell"):
            return bool(PIPE_CMD.search(inp.get("command", "")))
        return False
    return is_focus


def load_events(path, since, until):
    events, pending = [], {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            ts = (rec.get("timestamp") or "")[:19]
            if ts and ((since and ts[:10] < since) or (until and ts[:10] > until)):
                continue
            typ, content = rec.get("type"), (rec.get("message") or {}).get("content")
            if typ == "assistant" and isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text", "").strip():
                        events.append({"k": "said", "ts": ts, "text": b["text"]})
                    elif b.get("type") == "tool_use":
                        events.append({"k": "tool", "ts": ts, "name": b.get("name", ""), "input": b.get("input") or {}})
                        pending[b.get("id")] = len(events) - 1
            elif typ == "user" and not rec.get("isMeta") and not rec.get("isCompactSummary"):
                blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_result":
                        i = pending.get(b.get("tool_use_id"))
                        if i is not None:
                            events[i]["result"] = text_of(b.get("content"))
                            events[i]["is_error"] = bool(b.get("is_error"))
                    elif b.get("type") == "text":
                        t = b.get("text", "").strip()
                        if t.startswith("[Request interrupted"):
                            events.append({"k": "interrupt", "ts": ts})
                        elif t and not SKIP_HUMAN.match(t):
                            events.append({"k": "human", "ts": ts, "text": t})
    return events


def show_input(name, inp):
    if "script" in inp:
        return clip(inp["script"], 1400, 500)
    if "command" in inp:
        return clip(inp["command"], 900, 300)
    return clip(json.dumps(inp, ensure_ascii=False), 600)


def digest(path, args, is_focus):
    ev = load_events(path, args.since, args.until)
    focus = [e for e in ev if e["k"] == "tool" and is_focus(e["name"], e["input"])]
    if not focus:
        return None
    out, n_flag = [], 0
    for i, e in enumerate(ev):
        if e["k"] == "tool" and is_focus(e["name"], e["input"]):
            res = e.get("result", "<no result>")
            shell = e["name"] in ("Bash", "PowerShell")
            pattern = FAIL_SHELL if shell else FAIL_MCP
            follow = []
            for e2 in ev[i + 1: i + 8]:
                if e2["k"] == "said":
                    follow.append(e2["text"])
                    if len(follow) == 2:
                        break
                elif e2["k"] == "tool" and follow:
                    break
            after = "\n".join(follow)
            # shell calls: the result decides (agents comment on every command); tool calls: the agent's next words too
            flagged = e.get("is_error") or DENIED.search(res) or pattern.search(res[:6000] if shell else res) \
                or (not shell and SAID_WRONG.search(after[:600]))
            short = e["name"].replace(RESOLVE_TOOL, "resolve:")
            if not flagged:
                if args.ok:
                    out.append(f"- ok {e['ts']} {short}: {clip(res.replace(chr(10), ' '), 140)}")
                continue
            n_flag += 1
            tag = " DENIED" if DENIED.search(res) else (" IS_ERROR" if e.get("is_error") else "")
            out.append(f"\n### [{e['ts']}] {short}{tag}")
            out.append("INPUT:\n```\n" + show_input(e["name"], e["input"]) + "\n```")
            out.append("RESULT:\n```\n" + clip(res, 1200, 300) + "\n```")
            if after:
                out.append("AGENT AFTER:\n> " + clip(after, 1200).replace("\n", "\n> "))
        elif e["k"] == "human":
            out.append(f"\n**USER [{e['ts']}]:** " + clip(e["text"], 900))
        elif e["k"] == "interrupt":
            out.append(f"\n**USER INTERRUPTED [{e['ts']}]**")
        elif e["k"] == "said":
            prev = next((x for x in reversed(ev[max(0, i - 6): i]) if x["k"] == "tool"), None)
            if prev and is_focus(prev["name"], prev["input"]):
                continue  # already printed as AGENT AFTER
            if VIDEO_CTX.search(e["text"]) and SAID_WRONG.search(e["text"]):
                out.append(f"\n_agent [{e['ts']}]:_ " + clip(e["text"], 700))
    ts = [e["ts"] for e in ev if e.get("ts")]
    head = (f"# Session {path.stem}\n\nTranscript: `{path}`\nSpan: {ts[0] if ts else '?'} → {ts[-1] if ts else '?'}; "
            f"pipeline calls {len(focus)}, flagged {n_flag}, human messages {sum(1 for e in ev if e['k'] == 'human')}\n")
    return head + "\n".join(out) + "\n", len(focus), n_flag, (ts[0] if ts else ""), (ts[-1] if ts else "")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="\n".join(__doc__.splitlines()[1:]))
    p.add_argument("--out", required=True)
    p.add_argument("--project", help="project folder the sessions ran in (default: current folder)")
    p.add_argument("--projects-dir", help="Claude Code projects folder (default: $CLAUDE_CONFIG_DIR/projects or ~/.claude/projects)")
    p.add_argument("--since", help="YYYY-MM-DD: only events on or after this day")
    p.add_argument("--until", help="YYYY-MM-DD: only events on or before this day")
    p.add_argument("--focus", choices=["pipeline", "resolve", "all"], default="pipeline")
    p.add_argument("--match", help="regex: extra tool names or commands to treat as pipeline")
    p.add_argument("--state", help="JSON file of digested transcripts; unchanged ones are skipped")
    p.add_argument("--ok", action="store_true", help="also list pipeline calls that looked fine, one line each")
    p.add_argument("--include-active", action="store_true", help="include transcripts written in the last 10 minutes")
    p.add_argument("--group-kb", type=int, default=250)
    args = p.parse_args()

    src = project_dir(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    state = {}
    if args.state and Path(args.state).is_file():
        state = json.loads(Path(args.state).read_text(encoding="utf-8")).get("transcripts", {})
    is_focus = focus_of(args)
    files = sorted(list(src.glob("*.jsonl")) + list(src.glob("*/subagents/*.jsonl")), key=lambda f: f.stat().st_mtime)
    now, rows, skipped = time.time(), [], 0
    for f in files:
        mt = f.stat().st_mtime
        if not args.include_active and now - mt < 600:
            continue
        if args.since and time.strftime("%Y-%m-%d", time.localtime(mt)) < args.since:
            continue
        key = str(f)
        if state.get(key) == mt:
            skipped += 1
            continue
        r = digest(f, args, is_focus)
        state[key] = mt
        if not r:
            continue
        text, n_calls, n_flag, t0, t1 = r
        name = (f.parent.parent.name[:8] + "_" + f.stem.replace("agent-", "sub-")[:12]) if f.parent.name == "subagents" else f.stem[:8]
        dest = out / f"{name}.md"
        dest.write_text(text, encoding="utf-8")
        rows.append({"digest": dest.name, "from": t0, "to": t1, "calls": n_calls, "flagged": n_flag,
                     "kb": round(dest.stat().st_size / 1024), "transcript": key})
    with open(out / "index.tsv", "w", encoding="utf-8") as fh:
        fh.write("digest\tfrom\tto\tcalls\tflagged\tkb\ttranscript\n")
        for r in rows:
            fh.write("\t".join(str(r[k]) for k in ("digest", "from", "to", "calls", "flagged", "kb", "transcript")) + "\n")
    groups, cur, size = [], [], 0
    for r in sorted(rows, key=lambda r: -r["kb"]):
        if cur and size + r["kb"] > args.group_kb:
            groups.append(cur)
            cur, size = [], 0
        cur.append(r["digest"])
        size += r["kb"]
    if cur:
        groups.append(cur)
    (out / "groups.json").write_text(json.dumps(groups, indent=1), encoding="utf-8")
    if args.state:
        Path(args.state).parent.mkdir(parents=True, exist_ok=True)
        Path(args.state).write_text(json.dumps({"updated": time.strftime("%Y-%m-%dT%H:%M:%S"), "transcripts": state},
                                               indent=1), encoding="utf-8")
    total = sum(r["kb"] for r in rows)
    print(f"transcripts: {src} ({len(files)} files, {skipped} unchanged since the last run)")
    print(f"digests: {len(rows)} sessions, {sum(r['calls'] for r in rows)} pipeline calls, "
          f"{sum(r['flagged'] for r in rows)} flagged, {total} KB → {out}")
    print(f"reader groups (≤ {args.group_kb} KB): {len(groups)} → groups.json")


if __name__ == "__main__":
    main()
