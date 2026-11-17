# /// script
# requires-python = ">=3.11"
# dependencies = ["text2num>=2.5"]
# ///
"""claims_check.py: before rendering, reject on-screen numbers, prices, percentages and claims nobody approved.

Reads every string that can reach the screen and checks it against a claims file (approved claims with their
numbers and sources, numbers that are fine anywhere, banned phrases, claim words). Numbers the speaker says count
as approved when a transcript is given, so a b-roll stat that repeats what he said passes.

Inputs (any mix, globs allowed):
  <dir>             HyperFrames project: index.html + compositions/**/*.html. Visible text per block element
                    (<br> = space, inline tags joined as rendered); <script>, <style> and <head> are ignored.
  <file>.html       one HTML file, same extraction
  <file>.json       a spec: every string value that can reach the screen. Keys that hold no on-screen text are
                    skipped (file, path, src, id, why, anchor, type, zone, skin, fmt, kind, timing_source, out_dir,
                    *_note, *_why, ...), so are strings that look like paths, URLs, colours, icon ids or ISO dates.
                    An object with a numeric "value" is read as one displayed number: prefix + value + suffix + unit
                    + label (stat and bar graphics). A plan that mixes notes with on-screen text: --keys hook,label.
  <file>.srt|.vtt   burned-in captions: one text per cue
  <file>.txt        one on-screen text per line
  --text "..."      a literal string (repeatable)

Claims file (JSON): --claims <file>; else, with --slug, the record's `claims:` frontmatter (relative to the
workspace, the record folder or the project root); else claims.json next to the records folder
(<workspace>/.agents/claims.json with the default layout). No claims file = exit 3.
  {"language": "fr",
   "approved": [{"claim": "Le forfait annuel coûte 1 495 $", "numbers": ["1 495 $", "6 jetons"],
                 "match": ["clause grand-père"], "source": "pricing page, 2026-11-04", "expires": "2026-11-30"}],
   "allow_numbers": ["2026", "4K"],
   "banned": [{"text": "le moins cher au pays", "numbers": ["5,55 $"], "why": "unsourced"}],
   "claim_words": ["le moins cher", "meilleur", "le seul"]}
  numbers: written as on screen. Spaces, NBSP, thin spaces, `,` or `.` decimals, `$` `€` `%` and `x`/`fois plus`
           are normalized ("1495$" = "1 495 $"). A trailing word makes it stricter: "6 jetons" approves 6 only when
           "jeton(s)" follows it; "6" approves any 6. A price approves the same number shown without its sign.
  match:   phrases that state the claim (besides the claim text itself); a claim word inside one of them is cleared.
  expires: last valid day (YYYY-MM-DD). After it the claim's numbers and phrases turn red.
  claim_words: replaces the built-in list for the language (superlatives, comparisons, "only", "free", ...).
  All phrase matching ignores case and accents.

Verdicts:
  RED    price ($, €) or percentage or multiplier (10x, x10, « 3 fois plus ») not approved; a banned phrase or
         number; an approved claim used after it expired.
  AMBER  any other number not approved, not in allow_numbers and not said in the transcript; a claim word with
         no approved claim around it that the speaker does not say.
  Skipped: single-digit strings (keycaps, list markers), numbers after an enumerator word (« étape 2 », "step 2",
         "angle 3", "preset 1") or at the head of a numbered label (« 1 · Cadrage »), and the numbers inside a
         banned phrase (the ban is the finding).

Transcript: what the speaker says on camera is already in the audio, so its numbers (same value; a price,
percentage or multiplier said as one, « trois fois » = 3×) and its claim words pass on screen. Words said inside
a banned phrase clear nothing: « 10 à 20 fois moins cher » on camera approves no 20×.
  --words <file>       Words JSON (CONTRIBUTING format, or a bare list of {"text": ...}), .srt, .vtt or .txt;
                       repeatable, applies to every input. Spoken number words are converted with text2num
                       (« mille quatre cent quatre-vingt-quinze » -> 1495).
  --words-key KEY      for JSON specs: the string at top-level KEY names that spec's own transcript
                       (e.g. timing_source), resolved against --words-root, then the spec's folder and its parents.
  --captions CLASS     for HTML: elements with this class hold word-timed captions (cap-line in the HyperFrames
                       builds of vertical-clips): their text is the transcript of that project.

usage:
  uv run claims_check.py hf/launch-9x16/ --claims .agents/claims.json --captions cap-line
  uv run claims_check.py "specs/*.json" --claims claims.json --words-key timing_source --words-root /media/project
  uv run claims_check.py "plans/*.json" --claims claims.json --keys hook,label,text
  uv run claims_check.py --slug launch-03 hf/9x16 --words cut/launch-03.words.json --json checks/claims.json
  uv run claims_check.py --claims claims.json --text "Le studio le moins cher au pays" --text "1 495 $ par année"
Exit: 0 green, 2 amber, 1 red, 3 could not run.
Reference: ${CLAUDE_PLUGIN_ROOT}/references/claims.md
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path

try:
    from text_to_num import alpha2digit   # spoken / spelled-out numbers -> digits
except ImportError:                       # plain python without the dependency: digits only, main() warns
    alpha2digit = None

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------- what is not on-screen text

SKIP_KEYS = {
    "file", "files", "path", "paths", "src", "source", "sources", "id", "ids", "slug", "why", "anchor", "type",
    "zone", "skin", "fmt", "format", "kind", "timing_source", "out_dir", "dir", "master", "stt", "model", "status",
    "note", "notes", "comment", "comments", "icon", "color", "colour", "font", "class", "style", "layout", "mode",
    "url", "href", "lang", "language", "coords", "cut", "enter", "exit", "side", "view", "pick", "heavier", "mark",
    "who", "curve", "metric", "frame", "angle", "ease", "easing", "track", "voice", "seed", "prompt",
    "negative_prompt", "transition", "codec", "preset_id", "lut", "hash", "version", "created", "updated", "date",
    "concept", "plate", "review", "draft", "label_pos", "align", "anchor_words", "words", "timing", "audio",
    "video_file", "image", "images", "thumbnail", "poster", "render", "renders", "proxy", "crop", "fit",
}
SKIP_SUFFIXES = ("_note", "_notes", "_why", "_path", "_paths", "_file", "_files", "_dir", "_src", "_url", "_id",
                 "_ids", "_color", "_colour", "_angle", "_rect", "_mode", "_layout", "_source", "_key")
SKIP_PREFIXES = ("why", "_")
VALUE_KEYS = ("value",)              # a numeric value under these keys is a displayed number (stat, bars)
VALUE_PARTS = ("prefix", "suffix", "unit", "label", "decimals")

PATHLIKE = re.compile(r"^(?:[A-Za-z]:[\\/]|\.{0,2}/|~[\\/]|\\\\)|^[^\s]+\.[A-Za-z0-9]{2,4}$|^[\w.-]+(?:/[\w. -]+)+/?$")
URLLIKE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|www\.|mailto:)", re.I)
COLOURLIKE = re.compile(r"^(?:#[0-9a-fA-F]{3,8}|rgba?\(.*\)|hsla?\(.*\))$")
ICONLIKE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*:[a-z0-9]+(?:[-_][a-z0-9]+)*$")
ISODATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+Z?)?$")
CLOCK = re.compile(r"^\d{1,2}[:h]\d{2}(?:[: ]\d{2}s?)?$")   # 9:00, 18h48 31s (a take folder named by its time)
SLUGLIKE = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)+$")

BLOCK_TAGS = {
    "html", "body", "div", "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol", "dl", "dt", "dd", "section",
    "article", "header", "footer", "main", "aside", "nav", "figure", "figcaption", "blockquote", "pre", "table",
    "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "form", "fieldset", "legend", "button", "label",
    "option", "summary", "details", "template", "svg", "text", "foreignobject", "g",
}
SKIP_TAGS = {"script", "style", "head", "noscript", "title"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr",
             "param"}

# ---------------------------------------------------------------- claim words (used when the claims file has none)

DEFAULT_CLAIM_WORDS = {
    "fr": ["le moins cher", "la moins chère", "les moins chers", "moins cher", "pas cher", "meilleur", "meilleure",
           "meilleurs", "meilleures", "le seul", "la seule", "les seuls", "les seules", "unique au", "le premier",
           "la première", "les premiers", "les premières", "numéro 1", "numéro un", "no 1", "#1", "imbattable",
           "garanti", "garantie", "révolutionnaire", "jamais vu", "aucun autre", "aucune autre", "partout",
           "illimité", "illimitée", "gratuit", "gratuite", "gratuitement", "sans frais", "fois plus", "fois moins",
           "le plus rapide", "le plus simple", "le plus facile"],
    "en": ["cheapest", "lowest price", "best", "only", "first", "number one", "#1", "no. 1", "fastest", "easiest",
           "unbeatable", "guaranteed", "guarantee", "revolutionary", "never seen", "unlimited", "free",
           "times more", "times less", "times faster", "times cheaper", "anywhere", "everywhere", "most affordable"],
}
ENUM_WORDS = {
    "etape", "etapes", "step", "steps", "angle", "angles", "preset", "presets", "slot", "piste", "pistes", "track",
    "tracks", "camera", "cameras", "cam", "ndi", "page", "partie", "part", "video", "prise", "take", "chapitre",
    "chapter", "no", "n", "numero", "number", "option", "scene", "bloc", "block", "episode", "ep", "saison",
    "season", "question", "jour", "day", "semaine", "week", "niveau", "level", "#",
}
UNIT_WORDS = {
    "h": "h", "hr": "h", "hrs": "h", "heure": "h", "heures": "h", "hour": "h", "hours": "h",
    "min": "min", "mins": "min", "minute": "min", "minutes": "min",
    "s": "s", "sec": "s", "secs": "s", "seconde": "s", "secondes": "s", "second": "s", "seconds": "s",
    "jour": "jour", "jours": "jour", "day": "jour", "days": "jour",
    "mois": "mois", "month": "mois", "months": "mois",
    "an": "an", "ans": "an", "annee": "an", "annees": "an", "year": "an", "years": "an",
    "go": "go", "gb": "go", "gigs": "go", "giga": "go", "gigas": "go", "mo": "mo", "mb": "mo", "tb": "to",
}
ORDINAL = re.compile(r"^(?:er|re|ère|ere|e|è|ème|eme|es|nd|st|rd|th)(?![^\W\d_])")
RED_CLASSES = {"$": "price", "€": "price", "%": "percentage", "x": "multiplier"}

# ---------------------------------------------------------------- text normalization

SPACES = dict.fromkeys(map(ord, "\u00a0\u202f\u2009\u2007\u200a\u2002\u2003\u2004\u2005\u2006\u2008\u205f\u3000"), " ")
APOS = dict.fromkeys(map(ord, "\u2019\u2018\u02bc\u00b4\u0060"), "'")


def prep(s: str) -> str:
    """Unify spaces and apostrophes, collapse whitespace."""
    s = s.translate(SPACES).translate(APOS)
    return re.sub(r"\s+", " ", s).strip()


def fold(s: str) -> str:
    """Lowercase, no accents, hyphens as spaces: the form all phrase matching uses."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"(?<=[^\W\d_])-(?=[^\W\d_])", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def singular(w: str) -> str:
    w = fold(w).strip("'-")
    return w[:-1] if len(w) > 3 and w[-1] in "sx" else w


def dec(s: str) -> str:
    try:
        return format(Decimal(s).normalize(), "f")
    except InvalidOperation:
        return s


# ---------------------------------------------------------------- numbers


@dataclass
class Num:
    start: int
    end: int
    raw: str
    value: str          # canonical: "1495", "12.48", "12:30", "9:16", "4k"
    cls: str            # "$", "€", "%", "x", "time", "ratio", "code" or "" (plain)
    word: str = ""      # normalized word right after it ("jeton", "h", "novembre")
    canon: str = ""     # rendering inside folded text for phrase matching
    enum: bool = False  # enumerator / list marker: not a claim

    def show(self):
        return self.raw.strip()


LEAD = r"(?<![\w.,])(?<![^\W\d_]-)"
NUM_RE = {
    "fr": re.compile(LEAD + r"(\d{1,3}(?: \d{3})+(?:[.,]\d+)?(?!\d)|\d+(?:[.,]\d+)?)"),
    "en": re.compile(LEAD + r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?(?!\d)|\d{1,3}(?: \d{3})+(?:\.\d+)?(?!\d)"
                            r"|\d+(?:\.\d+)?(?:,\d{1,2}(?!\d))?)"),
}
PRE_MULT = re.compile(r"(?<![\w])(?:x|× ?)(\d+(?:[.,]\d+)?)(?![\d\w])")   # x10, ×10; « x 60 » is a coordinate
MONEY_AFTER = re.compile(r"^\s?([kKM])?\s?(\$\s?CA\b|\$|€|CAD\b|USD\b|EUR\b|dollars?\b|piasses?\b|piastres?\b|"
                         r"euros?\b|bucks\b)", re.I)
PCT_AFTER = re.compile(r"^\s?(%|pour ?cents?\b|pour ?100\b|pourcents?\b|percent\b|per ?cent\b)", re.I)
MULT_AFTER = re.compile(r"^\s?(?:[x×](?![\w])|fois\b(?=\s+(?:plus|moins|mieux|pire)\b)|"
                        r"times\b(?=\s+(?:more|less|faster|cheaper|better|bigger|higher|lower|smaller|as)\b))", re.I)
TIME_AFTER = re.compile(r"^(?:h|:)(\d{2})(?!\d)")
RATIO_AFTER = re.compile(r"^\s?[x×:/]\s?(\d+)(?![\d\w])")
LETTERS_AFTER = re.compile(r"^([^\W\d_]+)")
WORD_AFTER = re.compile(r"^\s?([^\W\d_][\w'-]*)")
WORD_BEFORE = re.compile(r"([^\W\d_]+|#)\s?$")


def _value(raw: str, lang: str) -> str:
    s = raw.replace(" ", "")
    if lang == "en" and re.match(r"^\d{1,3}(?:,\d{3})+", s):
        s = s.replace(",", "")
    elif "." in s and "," in s:
        s = s.replace(".", "")
    return dec(s.replace(",", "."))


def scan(text: str, lang: str = "fr") -> list[Num]:
    """Every number in an already prep()-ed text, with its class, unit word and canonical rendering."""
    nums, taken = [], []
    for m in PRE_MULT.finditer(text):                            # x10, ×10
        v = _value(m.group(1), lang)
        nums.append(Num(m.start(), m.end(), m.group(0), v, "x", canon=f"{v}x"))
        taken.append((m.start(), m.end()))
    rx = NUM_RE.get(lang, NUM_RE["fr"])
    for m in rx.finditer(text):
        if any(a <= m.start() < b for a, b in taken):
            continue                                             # inside x10, or the 16 of 9:16
        raw, start, end = m.group(1), m.start(), m.end()
        value, cls, word, unit = _value(raw, lang), "", "", ""
        rest = text[end:]
        before = text[:start]
        if (t := TIME_AFTER.match(rest)) and "." not in value and int(value) <= 24:
            value, cls, end = f"{int(value)}:{t.group(1)}", "time", end + t.end()
        elif (r := RATIO_AFTER.match(rest)):                     # 16:9, 9x16, 1920x1080, 24/7
            value, cls, end = f"{value}:{dec(r.group(1))}", "ratio", end + r.end()
        elif (mo := MONEY_AFTER.match(rest)):
            mult = {"k": 1000, "K": 1000, "M": 1000000}.get(mo.group(1) or "", 1)
            value = dec(str(Decimal(value) * mult))
            cls = "€" if re.match(r"€|eur", mo.group(2), re.I) else "$"
            end += mo.end()
        elif re.search(r"(?<![\w])\$\s?$", before) or re.search(r"(?<![\w])€\s?$", before):
            cls = "€" if before.rstrip().endswith("€") else "$"
            start = before.rstrip().rfind(cls[-1])
        elif (p := PCT_AFTER.match(rest)):
            cls, end = "%", end + p.end()
        elif (x := MULT_AFTER.match(rest)):
            cls, end = "x", end + x.end()
        elif re.search(r"(?<![\w])[x×]\s?$", before):
            cls = "x"
        elif (lt := LETTERS_AFTER.match(rest)):
            letters = lt.group(1)
            if ORDINAL.match(letters):
                end += lt.end()
            elif fold(letters) in UNIT_WORDS:
                unit, end = UNIT_WORDS[fold(letters)], end + lt.end()
            else:
                value, cls, end = f"{value}{fold(letters)}", "code", end + lt.end()
        if not unit and cls not in ("time", "ratio") and (w := WORD_AFTER.match(text[end:])):
            fw = fold(w.group(1))
            if fw in UNIT_WORDS and cls == "":
                unit, end = UNIT_WORDS[fw], end + w.end()
            else:
                word = singular(w.group(1))
        if unit:
            word = unit
        canon = {"$": f"{value}$", "€": f"{value}€", "%": f"{value}%", "x": f"{value}x"}.get(cls, value)
        if unit:
            canon += f" {unit}"
        n = Num(start, end, text[start:end], value, cls, word, canon)
        taken.append((start, end))
        wb = WORD_BEFORE.search(text[:start])
        prev = max((x for x in nums if x.end <= start), key=lambda x: x.end, default=None)
        if cls == "" and "." not in value and int(value) < 100 and (
                (wb and fold(wb.group(1)) in ENUM_WORDS) or
                (not text[:start].strip() and re.match(r"^\s?[.)·:–-]\s", text[end:]) and int(value) < 20) or
                (prev and prev.enum and re.fullmatch(r"\s?(?:à|a|-|–|to|et|and|,)\s?", text[prev.end:start]))):
            n.enum = True                                        # étape 2, angle 3, « 1 · Cadrage », étapes 1 à 5
        nums.append(n)
    nums.sort(key=lambda n: n.start)
    return nums


def _fold_keep(s: str) -> str:
    """fold() without trimming or collapsing, so positions in a prep()-ed text carry over."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"(?<=[^\W\d_])-(?=[^\W\d_])", " ", s)


def canon_text(text: str, nums: list[Num], spans: list | None = None) -> str:
    """Folded text with each number in its canonical form; fills `spans` with each number's (start, end) in it."""
    out, pos, at = [], 0, 0
    for n in sorted(nums, key=lambda n: n.start):
        if n.start < pos:
            continue
        piece = _fold_keep(text[pos:n.start])
        out += [piece, n.canon]
        at += len(piece)
        if spans is not None:
            spans.append((n, (at, at + len(n.canon))))
        at += len(n.canon)
        pos = n.end
    out.append(_fold_keep(text[pos:]))
    return "".join(out)


def phrase_spans(hay: str, phrase: str) -> list[tuple[int, int]]:
    if not phrase:
        return []
    rx = re.compile(r"(?<![\w])" + re.escape(phrase) + r"(?![\w])")
    return [(m.start(), m.end()) for m in rx.finditer(hay)]


def canon_phrase(s: str, lang: str) -> str:
    p = prep(s)
    return re.sub(r"\s+", " ", canon_text(p, scan(p, lang))).strip()


# ---------------------------------------------------------------- claims file


@dataclass
class Spec:
    value: str
    cls: str
    word: str

    def allows(self, n: Num) -> bool:
        return (self.value == n.value and (self.cls == n.cls or n.cls == "")
                and (not self.word or self.word == n.word))


@dataclass
class Claims:
    path: Path
    lang: str
    approved: list = field(default_factory=list)   # [{claim, source, specs, phrases, expires, expired}]
    allow: list = field(default_factory=list)      # [Spec]
    banned: list = field(default_factory=list)     # [{text, why, phrase, specs}]
    claim_words: list = field(default_factory=list)  # [(word, folded)]


class CheckError(Exception):
    pass


def parse_specs(items, lang, where):
    specs = []
    for s in items or []:
        p = prep(str(s))
        found = [n for n in scan(p, lang)]
        if not found:
            raise CheckError(f"{where}: no number in {s!r}")
        specs += [Spec(n.value, n.cls, n.word) for n in found]
    return specs


def load_claims(path: Path, today: datetime.date) -> Claims:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CheckError(f"claims file {path}: {e}") from None
    lang = (d.get("language") or "en").lower()[:2]
    c = Claims(path, lang)
    for i, a in enumerate(d.get("approved", [])):
        if not a.get("claim"):
            raise CheckError(f"{path}: approved[{i}] has no \"claim\"")
        exp = None
        if a.get("expires"):
            try:
                exp = datetime.date.fromisoformat(a["expires"])
            except ValueError:
                raise CheckError(f"{path}: approved[{i}].expires {a['expires']!r} is not YYYY-MM-DD") from None
        phrases = [canon_phrase(x, lang) for x in [a["claim"], *a.get("match", [])]]
        c.approved.append({"claim": a["claim"], "source": a.get("source", ""), "expires": a.get("expires"),
                           "expired": bool(exp and today > exp),
                           "specs": parse_specs(a.get("numbers"), lang, f"{path}: approved[{i}]"),
                           "phrases": [p for p in phrases if p]})
    c.allow = parse_specs(d.get("allow_numbers"), lang, f"{path}: allow_numbers")
    for i, b in enumerate(d.get("banned", [])):
        if not (b.get("text") or b.get("numbers")):
            raise CheckError(f"{path}: banned[{i}] needs \"text\" or \"numbers\"")
        c.banned.append({"text": b.get("text") or ", ".join(b["numbers"]), "why": b.get("why", ""),
                         "phrase": canon_phrase(b["text"], lang) if b.get("text") else "",
                         "specs": parse_specs(b.get("numbers"), lang, f"{path}: banned[{i}]")})
    words = d.get("claim_words")
    if words is None:
        words = DEFAULT_CLAIM_WORDS.get(lang, DEFAULT_CLAIM_WORDS["en"])
    c.claim_words = [(w, canon_phrase(w, lang)) for w in words if w.strip()]
    return c


def claims_from_record(slug: str) -> Path:
    sys.path.insert(0, str(HERE))
    import videos as V  # same folder; resolves the project from the current directory or $VIDEOS_ROOT
    vm, meta, _ws, wsdir = V.one(slug)
    rec = vm.parent
    p = (meta.get("claims") or "").strip().strip("\"'")
    if p:
        pp = Path(p)
        cands = [pp] if pp.is_absolute() else list(dict.fromkeys([wsdir / pp, rec / pp, V.ROOT / pp, Path.cwd() / pp]))
        for c in cands:
            if c.is_file():
                return c
        raise CheckError(f"record {slug}: claims: {p} not found (looked in {', '.join(str(c.parent) for c in cands)})")
    default = wsdir / Path(V.CFG["records"]).parent / "claims.json"
    if default.is_file():
        return default
    raise CheckError(f"record {slug} has no claims: field and {default} does not exist. Write a claims file "
                     f"(references/claims.md) and add `claims: <path>` to {vm}, or pass --claims <file>.")


# ---------------------------------------------------------------- transcripts


def transcript_text(path: Path) -> tuple[str, str]:
    """-> (text, language or "")."""
    suf = path.suffix.lower()
    raw = path.read_text(encoding="utf-8", errors="replace")
    if suf == ".json":
        d = json.loads(raw)
        words = d.get("words", []) if isinstance(d, dict) else d
        lang = d.get("language", "") if isinstance(d, dict) else ""
        return " ".join(str(w.get("text", "")) for w in words if isinstance(w, dict) and w.get("type") != "event"), lang
    if suf in (".srt", ".vtt"):
        return " ".join(t for _, _, t in read_cues(raw)), ""
    return raw, ""


@dataclass
class Spoken:
    """What the speaker says: its numbers (value + class) and its folded text, for claim words."""
    name: str
    specs: list
    hay: str
    bans: list | None = None    # banned-phrase spans in hay, filled on first use


def spoken_from(path: Path, c: Claims) -> Spoken:
    text, wlang = transcript_text(path)
    return spoken_text(text, c, path.name, (wlang or c.lang or "en")[:2])


def spoken_text(text: str, c: Claims, name: str, lang: str = "") -> Spoken:
    """Numbers said outside banned phrases (« 10 à 20 fois moins cher » approves no 20×), and the folded text."""
    lang = lang or c.lang
    texts = [text]
    if alpha2digit:
        try:
            texts.append(alpha2digit(text, lang, threshold=0))   # every number word, « un » included
        except ValueError:                                       # language text2num does not know
            pass
    specs, hays = {}, []
    for t in texts:
        p = prep(t)
        nums, at = scan(p, lang), []
        hay = canon_text(p, nums, at)
        hays.append(hay)
        bans = [sp for b in c.banned for sp in phrase_spans(hay, b["phrase"])]
        for n, span in at:
            if overlaps(span, bans):
                continue
            specs[(n.value, n.cls)] = Spec(n.value, n.cls, "")
            if n.cls == "" and re.match(r"^\s?(?:fois|times)\b", p[n.end:], re.I):
                specs[(n.value, "x")] = Spec(n.value, "x", "")  # « trois fois les profits » = 3×
    return Spoken(name, list(specs.values()), " \n ".join(hays))


def find_transcript(name: str, spec_path: Path, roots: list[Path]) -> Path | None:
    p = Path(name)
    if p.is_absolute():
        return p if p.is_file() else None
    for base in [*roots, spec_path.parent, *spec_path.parent.parents]:
        if (base / p).is_file():
            return base / p
    return None


# ---------------------------------------------------------------- input extraction


@dataclass
class Text:
    source: str
    where: str
    text: str
    spoken: list = field(default_factory=list)   # this source's own transcript(s): [Spoken]
    classes: frozenset = frozenset()             # HTML: classes of the element and its ancestors


class Blocks(HTMLParser):
    """Visible text per block element, inline tags joined as rendered."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.bufs, self.out, self.skip = [], [], [], 0

    def _ctx(self):
        el_id = next((e[1] for e in reversed(self.stack) if e[1]), "")
        start = next((e[2] for e in reversed(self.stack) if e[2]), "")
        classes = frozenset(c for e in self.stack for c in e[4])
        return el_id, start, classes

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in VOID_TAGS:
            if tag == "br" and self.bufs:
                self.bufs[-1]["parts"].append(" ")
            return
        a = dict(attrs)
        self.stack.append((tag, a.get("id") or "", a.get("data-start") or "", tag in BLOCK_TAGS,
                           (a.get("class") or "").split()))
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag in BLOCK_TAGS:
            el_id, start, classes = self._ctx()
            self.bufs.append({"id": el_id, "start": start, "tag": tag, "line": self.getpos()[0], "parts": [],
                              "classes": classes})

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in VOID_TAGS:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if not any(e[0] == tag for e in self.stack):
            return
        while self.stack:
            t, _, _, is_block, _ = self.stack.pop()
            if t in SKIP_TAGS:
                self.skip -= 1
            elif is_block:
                self._flush()
            if t == tag:
                break

    def handle_data(self, data):
        if not self.skip and self.bufs:
            self.bufs[-1]["parts"].append(data)

    def _flush(self):
        b = self.bufs.pop()
        text = prep("".join(b["parts"]))
        if text:
            where = f"line {b['line']}" + (f" #{b['id']}" if b["id"] else f" <{b['tag']}>") + (
                f" @{b['start']}s" if b["start"] else "")
            self.out.append((where, text, b["classes"]))

    def close(self):
        super().close()
        while self.bufs:
            self._flush()


def html_texts(path: Path, label: str) -> list[Text]:
    p = Blocks()
    p.feed(path.read_text(encoding="utf-8", errors="replace"))
    p.close()
    return [Text(label, w, t, classes=cl) for w, t, cl in p.out]


def skip_key(k: str) -> bool:
    k = k.lower()
    return k in SKIP_KEYS or k.endswith(SKIP_SUFFIXES) or k.startswith(SKIP_PREFIXES)


def skip_value(s: str) -> bool:
    s = s.strip()
    return (not s or bool(URLLIKE.match(s) or COLOURLIKE.match(s) or ICONLIKE.match(s) or ISODATE.match(s)
                          or CLOCK.match(s) or SLUGLIKE.match(s) or PATHLIKE.match(s)))


def fmt_value(v, decimals=None):
    if isinstance(v, float) and decimals is not None:
        return f"{v:.{int(decimals)}f}"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def json_texts(obj, label: str, only_keys: set | None, extra_skip: set) -> list[Text]:
    out = []

    def is_num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def walk(o, path, key):
        if isinstance(o, dict):
            consumed = set()
            for vk in VALUE_KEYS:
                if is_num(o.get(vk)) and (only_keys is None or vk in only_keys):
                    s = f"{o.get('prefix') or ''}{fmt_value(o[vk], o.get('decimals'))}{o.get('suffix') or ''}"
                    if isinstance(o.get("unit"), str) and o["unit"].strip():
                        s += " " + o["unit"]
                    if isinstance(o.get("label"), str) and o["label"].strip():
                        s += " " + o["label"]
                    out.append(Text(label, f"{path}.{vk}" if path else vk, prep(s)))
                    consumed |= {vk, *VALUE_PARTS}
            for k, v in o.items():
                if k in consumed or skip_key(k) or k.lower() in extra_skip:
                    continue
                walk(v, f"{path}.{k}" if path else k, k)
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]", key)
        elif isinstance(o, str):
            if (only_keys is None or key in only_keys) and not skip_value(o):
                out.append(Text(label, path or "(root)", prep(o)))

    walk(obj, "", "")
    return out


def read_cues(raw: str):
    """-> [(index, start, text)] from SRT or VTT."""
    cues = []
    for block in re.split(r"\n\s*\n", raw.replace("\r\n", "\n").strip()):
        lines = [x for x in block.split("\n") if x.strip()]
        tc = next((i for i, x in enumerate(lines) if "-->" in x), None)
        if tc is None:
            continue
        idx = lines[0].strip() if tc > 0 else str(len(cues) + 1)
        start = lines[tc].split("-->")[0].strip()
        text = re.sub(r"<[^>]+>", "", " ".join(lines[tc + 1:]))
        cues.append((idx, start, text))
    return cues


def expand(inputs: list[str]) -> list[Path]:
    out = []
    for s in inputs:
        if any(ch in s for ch in "*?[") and not Path(s).exists():
            hits = sorted(glob.glob(s, recursive=True))
            if not hits:
                raise CheckError(f"no file matches {s}")
            out += [Path(h) for h in hits]
        else:
            out.append(Path(s))
    return out


def rel(p: Path) -> str:
    try:
        return p.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def collect(path: Path, a, claims: Claims) -> list[Text]:
    if not path.exists():
        raise CheckError(f"not found: {path}")

    def with_captions(texts, name):
        # captions are what the speaker says: they act as this project's transcript
        caps = [t.text for t in texts if a.captions and a.captions & t.classes]
        if caps:
            sp = spoken_text(" ".join(caps), claims, f"{name} captions")
            for t in texts:
                t.spoken.append(sp)
        return texts

    if path.is_dir():
        files = ([path / "index.html"] if (path / "index.html").is_file() else sorted(path.glob("*.html")))
        files += sorted((path / "compositions").rglob("*.html")) if (path / "compositions").is_dir() else []
        if not files:
            raise CheckError(f"{path}: no index.html or compositions/*.html (not a HyperFrames project)")
        return with_captions([t for f in files for t in html_texts(f, rel(f))], rel(path))
    suf = path.suffix.lower()
    if suf in (".html", ".htm"):
        return with_captions(html_texts(path, rel(path)), rel(path))
    if suf == ".json":
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise CheckError(f"{path}: invalid JSON ({e})") from None
        texts = json_texts(obj, rel(path), a.only_keys, a.extra_skip)
        if a.words_key and isinstance(obj, dict) and isinstance(obj.get(a.words_key), str):
            tp = find_transcript(obj[a.words_key], path, a.words_root)
            if tp is None:
                print(f"claims_check: {rel(path)}: transcript {obj[a.words_key]} not found "
                      f"(--words-root?); its numbers are not counted as spoken", file=sys.stderr)
            else:
                try:
                    sp = spoken_from(tp, claims)
                except (OSError, ValueError, AttributeError) as e:
                    raise CheckError(f"{rel(path)}: transcript {tp} unreadable ({e})") from None
                for t in texts:
                    t.spoken = [sp]
        return texts
    if suf in (".srt", ".vtt"):
        raw = path.read_text(encoding="utf-8", errors="replace")
        return [Text(rel(path), f"cue {i} @{st}", prep(t)) for i, st, t in read_cues(raw) if prep(t)]
    if suf in (".txt", ".md"):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return [Text(rel(path), f"line {i}", prep(x)) for i, x in enumerate(lines, 1) if prep(x)]
    raise CheckError(f"{path}: unsupported input (want a HyperFrames folder, .html, .json, .srt, .vtt or .txt)")


# ---------------------------------------------------------------- the check


def overlaps(span, spans):
    return any(span[0] < b and a < span[1] for a, b in spans)


def said_outside_bans(s: Spoken, phrase: str, c: Claims) -> bool:
    """The phrase is in the transcript, outside any banned phrase (« le moins cher au Canada » clears nothing)."""
    if s.bans is None:
        s.bans = [sp for b in c.banned for sp in phrase_spans(s.hay, b["phrase"])]
    return any(not overlaps(sp, s.bans) for sp in phrase_spans(s.hay, phrase))


def check_text(t: Text, c: Claims, spoken_all: list[Spoken]) -> list[dict]:
    text = conv = t.text
    if alpha2digit:  # spelled-out numbers on screen (« vingt dollars », « dix fois moins »); « un », « deux » stay
        try:
            conv = prep(alpha2digit(text, c.lang))
        except ValueError:
            pass
    nums = scan(conv, c.lang)
    num_spans = []
    hay = canon_text(conv, nums, num_spans)
    where_in_hay = {id(n): sp for n, sp in num_spans}
    found = []

    def add(level, token, reason):
        found.append({"level": level, "source": t.source, "where": t.where, "text": t.text, "token": token,
                      "reason": reason})

    if len(nums) == 1 and nums[0].cls == "" and re.fullmatch(r"\d", conv.strip()):
        nums = []                                                # keycap / single-digit label
    banned_spans = []
    for b in c.banned:
        for sp in phrase_spans(hay, b["phrase"]):
            banned_spans.append(sp)
            add("red", b["text"], f"banned «{b['text']}»" + (f": {b['why']}" if b["why"] else ""))
    live = [a for a in c.approved if not a["expired"]]
    dead = [a for a in c.approved if a["expired"]]
    live_spans = [sp for a in live for p in a["phrases"] for sp in phrase_spans(hay, p)]
    for a in dead:
        if any(phrase_spans(hay, p) for p in a["phrases"]):
            add("red", a["claim"], f"approved claim expired {a['expires']}: «{a['claim']}»")
    said = [*spoken_all, *t.spoken]
    spoken = [s for sp in said for s in sp.specs]
    for n in nums:
        if n.enum or overlaps(where_in_hay.get(id(n), (-1, -1)), banned_spans):
            continue                                             # list marker, or already reported with its ban
        ban = next((b for b in c.banned if any(s.allows(n) for s in b["specs"])), None)
        if ban:
            add("red", n.raw, f"{n.show()}: banned number" + (f": {ban['why']}" if ban["why"] else ""))
            continue
        if any(s.allows(n) for a in live for s in a["specs"]) or any(s.allows(n) for s in c.allow):
            continue
        # said in the transcript: same value; a price, percentage or multiplier must be said as one
        if any(s.value == n.value and (s.cls == n.cls or n.cls == "" or (s.cls == "" and n.cls not in RED_CLASSES))
               for s in spoken):
            continue
        exp = next((a for a in dead if any(s.allows(n) for s in a["specs"])), None)
        if exp and any(f["token"] == exp["claim"] for f in found):
            continue                                             # its phrase is already reported as expired
        if exp:
            add("red", n.raw, f"{n.show()}: approved until {exp['expires']} («{exp['claim']}»)")
        elif n.cls in RED_CLASSES:
            add("red", n.raw, f"{n.show()}: {RED_CLASSES[n.cls]} not approved")
        else:
            add("amber", n.raw, f"{n.show()}: number not approved"
                + (", not said in the transcript" if said else " (no transcript given)"))
    hits = sorted(((sp, w, cw) for w, cw in c.claim_words for sp in phrase_spans(hay, cw)),
                  key=lambda h: h[0][0] - h[0][1])                # longest first: « meilleur prix », then « meilleur »
    shown = []
    for sp, w, cw in hits:
        if overlaps(sp, banned_spans) or overlaps(sp, live_spans) or overlaps(sp, shown):
            continue
        if any(said_outside_bans(s, cw, c) for s in said):
            continue                                             # his own words on camera: the audio carries it
        shown.append(sp)
        add("amber", w, f"claim word «{w}» with no approved claim around it"
            + (", not said in the transcript" if said else ""))
    seen, uniq = set(), []
    for f in found:
        k = (f["level"], f["token"], f["reason"])
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def clip(s, n=90):
    return s if len(s) <= n else s[:n - 1] + "…"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], epilog="\n".join(__doc__.split("\n")[2:]),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="*", help="HyperFrames folders, .html, .json specs, .srt/.vtt, .txt")
    ap.add_argument("--text", action="append", default=[], help="a literal on-screen string (repeatable)")
    ap.add_argument("--claims", help="claims file (default: the record's claims: field with --slug)")
    ap.add_argument("--slug", help="video record: its claims file")
    ap.add_argument("--words", action="append", default=[], help="transcript: numbers said here are approved")
    ap.add_argument("--words-key", help="JSON specs: top-level key naming the spec's own transcript")
    ap.add_argument("--words-root", action="append", default=[], help="base folder for relative --words-key paths")
    ap.add_argument("--captions", help="HTML: class of word-timed caption elements (comma list, e.g. cap-line); "
                                       "their text is the speaker's, so it counts as the transcript")
    ap.add_argument("--keys", help="JSON specs: check only these keys (comma list), e.g. hook,label,text")
    ap.add_argument("--skip-keys", help="JSON specs: more keys to skip (comma list)")
    ap.add_argument("--today", help="date for expiry checks (YYYY-MM-DD, default today)")
    ap.add_argument("--json", dest="json_out", help="write the findings here")
    ap.add_argument("--quiet", action="store_true", help="print findings and the verdict only")
    a = ap.parse_args(argv)
    if not a.inputs and not a.text:
        ap.error("give at least one input or --text")
    a.only_keys = {k.strip() for k in a.keys.split(",")} if a.keys else None
    a.extra_skip = {k.strip().lower() for k in a.skip_keys.split(",")} if a.skip_keys else set()
    a.captions = {k.strip() for k in a.captions.split(",") if k.strip()} if a.captions else set()
    a.words_root = [Path(p) for p in a.words_root]
    if alpha2digit is None:
        print("claims_check: text2num is not installed, so spelled-out numbers (« vingt dollars », « quinze ») are "
              "not read; run with `uv run` or pip install text2num", file=sys.stderr)
    try:
        today = datetime.date.fromisoformat(a.today) if a.today else datetime.date.today()
        if a.claims:
            cpath = Path(a.claims)
        elif a.slug:
            try:
                cpath = claims_from_record(a.slug)
            except SystemExit as e:
                raise CheckError(str(e)) from None
        else:
            raise CheckError("no claims file: pass --claims <file>, or --slug <record> whose frontmatter has "
                             "`claims: <path>` (references/claims.md)")
        if not cpath.is_file():
            raise CheckError(f"claims file not found: {cpath}")
        claims = load_claims(cpath, today)
        spoken_all = []
        for w in a.words:
            if not Path(w).is_file():
                raise CheckError(f"transcript not found: {w}")
            try:
                spoken_all.append(spoken_from(Path(w), claims))
            except (OSError, ValueError, AttributeError) as e:
                raise CheckError(f"transcript {w} unreadable ({e})") from None
    except (CheckError, ValueError) as e:
        print(f"claims_check: {e}", file=sys.stderr)
        return 3
    if not a.quiet:
        dead = [x for x in claims.approved if x["expired"]]
        print(f"claims: {rel(cpath)}" + (f" (from record {a.slug})" if a.slug and not a.claims else "")
              + f": {len(claims.approved)} approved" + (f" ({len(dead)} expired)" if dead else "")
              + f", {len(claims.allow)} allowed numbers, {len(claims.banned)} banned,"
              f" {len(claims.claim_words)} claim words, language {claims.lang}")
        for w, sp in zip(a.words, spoken_all, strict=True):
            print(f"spoken: {w} ({len(sp.specs)} numbers)")
    texts, errors = [], []
    for t in a.text:
        texts.append(Text("--text", "", prep(t)))
    try:
        paths = expand(a.inputs)
    except CheckError as e:
        print(f"claims_check: {e}", file=sys.stderr)
        return 3
    per_source = {}
    for p in paths:
        try:
            got = collect(p, a, claims)
        except (CheckError, OSError) as e:
            print(f"claims_check: {e}", file=sys.stderr)
            errors.append(str(e))
            continue
        texts += got
        per_source[rel(p)] = len(got)
    findings = []
    for t in texts:
        findings += check_text(t, claims, spoken_all)
    for f in findings:
        print(f"{f['level'].upper():<6}{f['source']}  {f['where']}  «{clip(f['text'])}»  -> {f['reason']}")
    red = sum(f["level"] == "red" for f in findings)
    amber = sum(f["level"] == "amber" for f in findings)
    verdict = "red" if red else "error" if errors else "amber" if amber else "green"
    print(f"claims_check: {len(texts)} texts from {len(per_source) + (1 if a.text else 0)} input(s): "
          f"{red} red, {amber} amber" + (f", {len(errors)} input(s) could not be read" if errors else "")
          + f" -> {verdict.upper()}")
    if a.json_out:
        out = {"claims": str(cpath.resolve()), "today": today.isoformat(), "verdict": verdict, "texts": len(texts),
               "inputs": per_source, "errors": errors, "findings": findings}
        Path(a.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        if not a.quiet:
            print(f"report: {a.json_out}")
    return 1 if red else 3 if errors else 2 if amber else 0


if __name__ == "__main__":
    sys.exit(main())
