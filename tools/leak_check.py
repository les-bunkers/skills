"""Scan the repo for things that must never be published: drive paths, home folders, emails, API keys, tokens.

usage: python tools/leak_check.py [--terms FILE] [PATH ...]
  --terms FILE   extra case-insensitive literals, one per line (# comments), e.g. a private list of
                 names and hostnames kept outside the repo
Exit 1 when anything is found. Stdlib only.
"""
import argparse
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}
TEXT = {".py", ".md", ".json", ".js", ".mjs", ".ts", ".tsx", ".html", ".css", ".sh", ".ps1", ".txt", ".yml", ".yaml", ".toml", ".cfg", ".srt", ".vtt", ".csv"}
PATTERNS = [
    ("windows drive path", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/](?![\\/])(?!\.\.\.)")),
    ("home folder", re.compile(r"(?i)(/Users/[a-z0-9_.-]+|/home/[a-z0-9_.-]+|\\Users\\[a-z0-9_.-]+)")),
    ("email address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("api key", re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|sk_live_[A-Za-z0-9]{10,}|xai-[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b")),
    ("bearer token", re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._-]{20,}")),
    ("localhost key proxy", re.compile(r"127\.0\.0\.1:8765")),
]
ALLOW = [
    re.compile(r"noreply@anthropic\.com"),
    re.compile(r"[A-Za-z0-9._%+-]+@example\.(com|org)"),
    re.compile(r"git@github\.com"),
    re.compile(r"[a-z0-9_-]+@les-bunkers\b"),          # plugin@marketplace install ids
    re.compile(r"[a-z0-9_-]+@[0-9][0-9a-z.~^-]*"),   # package@version
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--terms")
    ap.add_argument("paths", nargs="*")
    a = ap.parse_args()
    terms = []
    if a.terms:
        terms = [t.strip() for t in Path(a.terms).read_text(encoding="utf-8").splitlines() if t.strip() and not t.startswith("#")]
    roots = [Path(p) for p in a.paths] or [ROOT]
    hits = 0
    for root in roots:
        files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file() and not (set(p.parts) & SKIP_DIRS)]
        for f in files:
            if f.suffix.lower() not in TEXT or f.resolve() == Path(__file__).resolve():
                continue
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for n, line in enumerate(lines, 1):
                clean = line
                for al in ALLOW:
                    clean = al.sub("", clean)
                found = [label for label, rx in PATTERNS if rx.search(clean)]
                low = clean.lower()
                found += [f"term '{t}'" for t in terms if t.lower() in low]
                for label in found:
                    hits += 1
                    print(f"{f.relative_to(ROOT) if f.is_relative_to(ROOT) else f}:{n}: {label}: {line.strip()[:160]}")
    print(f"{hits} finding(s)")
    sys.exit(1 if hits else 0)


if __name__ == "__main__":
    main()
