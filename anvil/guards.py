"""Hard guardrails. These run in code on every generated line and on the final CV.

Nothing here asks an LLM. If a check fails, the caller must fall back to the
verbatim source gene (polish) or refuse to write the CV (final check).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# A number is digits (with optional commas/decimal) plus an optional magnitude
# or percent suffix. "2.5 million" and "2.5 billion" are different facts.
NUM_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?"
    r"(?:\s*(%|k|thousand|m|mn|million|bn|billion|lakhs?|crores?)(?![A-Za-z]))?",
    re.I,
)
SUFFIX_KEY = {"%": "%", "k": "k", "thousand": "k", "m": "m", "mn": "m", "million": "m",
              "bn": "b", "billion": "b", "lakh": "lakh", "lakhs": "lakh",
              "crore": "crore", "crores": "crore"}
NUM_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty".split())}
NUM_WORD_RE = re.compile(r"\b(" + "|".join(NUM_WORDS) + r")\b", re.I)

# A "proper noun" here is any token that starts with a capital letter or is an
# acronym/mixed token (Power BI, CTRM, LoD1, Fusion). Sentence-initial words are
# included on purpose: the check is "every capitalised token must already exist
# in an allowed source", so a capitalised common word that was in the source passes.
CAP_RE = re.compile(r"\b[A-Z][A-Za-z0-9&+./'’-]*")

# Characters that look like Latin letters but are Cyrillic (homoglyph evasion).
_CYR_TO_LATIN = str.maketrans({"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x",
                               "А": "A", "Е": "E", "О": "O", "Р": "P", "С": "C", "Х": "X"})
# Dash-like characters that are always banned (en dash "–" stays: dates use it).
EXTRA_BANNED_CHARS = ["―", "‒", "⸺", "－", "&mdash;", "--"]
DASH_BETWEEN_WORDS_RE = re.compile(r"(?<=\w)\s+-\s+(?=\w)")
# Client-scope stems, matched as word prefixes in the same text (bullet) as the client.
# Petronas is import/export work only: never pricing or price configuration.
CLIENT_STEMS = {"Petronas": ["pric", "tariff", "rate card", "configur"]}


class GuardError(Exception):
    pass


@dataclass
class Violation:
    rule: str
    detail: str
    text: str = ""

    def __str__(self) -> str:
        t = f" in: {self.text[:90]!r}" if self.text else ""
        return f"[{self.rule}] {self.detail}{t}"


@dataclass
class Report:
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def add(self, rule: str, detail: str, text: str = "") -> None:
        self.violations.append(Violation(rule, detail, text))

    def extend(self, other: "Report") -> None:
        self.violations.extend(other.violations)


def canon(text: str) -> str:
    """Normalise text for checking: NFKC, drop format characters (soft hyphen,
    zero-width space, BOM...), and map Cyrillic lookalikes to Latin."""
    t = unicodedata.normalize("NFKC", text)
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Cf")
    return t.translate(_CYR_TO_LATIN)


def _num_key(num: str, suffix: str | None) -> str:
    key = num.replace(",", "")
    if suffix:
        key += SUFFIX_KEY[suffix.lower()]
    return key


def numbers(text: str) -> set[str]:
    """Number keys in `text`. Digits keep their magnitude suffix ("2.5m", "50k").
    A number word (zero..twenty) yields its digit key plus a "w:<word>" marker,
    so "seven" is only accepted where the source also says "seven"."""
    t = canon(text)
    out = set()
    for m in NUM_RE.finditer(t):
        num = re.match(r"[\d,.]+", m.group(0)).group(0).rstrip(",.")
        suffix = m.group(1)
        out.add(_num_key(num, suffix))
    for m in NUM_WORD_RE.finditer(t):
        w = m.group(1).lower()
        out.add(str(NUM_WORDS[w]))
        out.add("w:" + w)
    return out


LEAD_VERB_RE = re.compile(r"^[A-Z][a-z]+(ed|ing)$")


def cap_tokens(text: str) -> set[str]:
    """Capitalised tokens, except a plain past-tense/-ing verb opening a sentence
    ("Resolved", "Managing"), which is how bullets start and is not a name."""
    t = canon(text)
    out = set()
    for m in CAP_RE.finditer(t):
        tok = m.group(0).strip(".,;:'’-/")
        if not tok:
            continue
        before = t[: m.start()].rstrip()
        sentence_start = not before or before[-1] in ".;:!?\n–-•"
        if sentence_start and LEAD_VERB_RE.match(tok):
            continue
        out.add(tok.lower())
    return out


_SUFFIXES = ("ies", "ing", "ed", "es", "ly", "e", "s")


def _stem(word: str) -> str:
    """Prefix stem of a banned word: leverage/leveraging/leveraged -> leverag,
    spearheaded -> spearhead, orchestrate -> orchestrat, synergies -> synerg."""
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            if suf == "s" and word.endswith("ss"):
                continue
            return word[: -len(suf)]
    return word


def _word_pattern(phrase: str, stem_last: bool) -> re.Pattern:
    """Regex for a phrase: hyphen and space are equivalent between words, the
    phrase must start at a word boundary, and the last word is matched as a prefix."""
    words = [w for w in re.split(r"[\s-]+", phrase.lower().strip()) if w]
    if stem_last:
        words[-1] = _stem(words[-1])
    body = r"[\s-]+".join(re.escape(w) for w in words)
    return re.compile(r"(?<![a-z])" + body)


def check_style(text: str, skeleton: dict) -> Report:
    """Anti-AI-detector filter: banned chars, dashes and AI-tell words."""
    r = Report()
    t = canon(text)
    chars = list(skeleton.get("banned_chars", [])) + EXTRA_BANNED_CHARS
    for ch in chars:
        if ch in text or ch in t:
            r.add("banned_char", f"contains {ch!r}", text)
    if DASH_BETWEEN_WORDS_RE.search(t):
        r.add("banned_char", "contains ' - ' used as a dash", text)
    low = t.lower()
    for w in skeleton.get("banned_phrases", []):
        if _word_pattern(w, stem_last=True).search(low):
            r.add("banned_word", f"uses {w!r}", text)
    return r


def check_client_scope(text: str, skeleton: dict) -> Report:
    """Petronas = import/export only, etc. Each client's window runs from the last ';' or
    other client name before it to the next client name (or the end of the bullet), so a
    later sentence about the same work is still checked, but "CBH: pricing checks;
    Petronas: import/export" is not pinned on Petronas."""
    r = Report()
    low = canon(text).lower()
    scope = skeleton.get("clients", {}).get("scope", {})
    names = list(scope) + [c for c in skeleton.get("clients", {}).get("named_whitelist", []) if c not in scope]
    spans = sorted((m.start(), m.end(), c) for c in names
                   for m in _word_pattern(c, stem_last=False).finditer(low))
    for i, (start, end, client) in enumerate(spans):
        rule = scope.get(client)
        if not rule:
            continue
        prev_end = spans[i - 1][1] if i else 0
        lo = max(prev_end, low.rfind(";", 0, start) + 1)
        hi = next((s0 for s0, _, c in spans[i + 1:] if c != client), len(low))
        window = low[lo:hi]
        terms = list(dict.fromkeys(list(rule.get("forbidden_terms", [])) + CLIENT_STEMS.get(client, [])))
        for bad in terms:
            if _word_pattern(bad, stem_last=False).search(window):
                r.add("client_scope", f"{client} named next to '{bad}'", text.strip())
                break
    return r


def check_client_whitelist(text: str, skeleton: dict, known_nouns: set[str]) -> Report:
    """Only whitelisted clients may be named. Any capitalised token that is not a
    known noun from the real CVs/skeleton is reported, which catches invented clients."""
    r = Report()
    unknown = cap_tokens(text) - known_nouns
    if unknown:
        r.add("unknown_noun", "unrecognised name(s): " + ", ".join(sorted(unknown)), text)
    return r


def assert_no_new_facts(source: str, output: str, allowed_extra: str = "",
                        allowed_terms: set[str] | None = None) -> Report:
    """Reject any number or capitalised token in `output` that is not present in
    `source`, `allowed_extra` (target company + role) or `allowed_terms`."""
    r = Report()
    allowed_nums = numbers(source) | numbers(allowed_extra)
    new_nums = numbers(output) - allowed_nums
    if new_nums:
        r.add("new_number", "introduced " + ", ".join(sorted(new_nums)), output)
    allowed_caps = cap_tokens(source) | cap_tokens(allowed_extra) | {t.lower() for t in (allowed_terms or set())}
    # allowed_terms may be multi-word ("BRD/FRD documentation"); add their capitalised tokens too
    for t in allowed_terms or set():
        allowed_caps |= cap_tokens(t)
    new_caps = cap_tokens(output) - allowed_caps
    if new_caps:
        r.add("new_noun", "introduced " + ", ".join(sorted(new_caps)), output)
    return r


def check_line(text: str, skeleton: dict, known_nouns: set[str]) -> Report:
    r = check_style(text, skeleton)
    r.extend(check_client_scope(text, skeleton))
    r.extend(check_client_whitelist(text, skeleton, known_nouns))
    return r


# Section headings are fixed text, not facts.
STRUCTURAL = set("professional summary experience work education projects ai personal skills and "
                 "certifications technical core profile".split())


def check_document(doc_lines: list[str], skeleton: dict, known_nouns: set[str],
                   known_numbers: set[str]) -> Report:
    """Final gate over every rendered line of the CV."""
    r = Report()
    known_nouns = known_nouns | STRUCTURAL
    for line in doc_lines:
        r.extend(check_line(line, skeleton, known_nouns))
        extra = numbers(line) - known_numbers
        if extra:
            r.add("new_number", "number not in any real CV: " + ", ".join(sorted(extra)), line)
    full = "\n".join(doc_lines)
    if skeleton["title"] not in full:
        r.add("title_lock", f"title {skeleton['title']!r} missing")
    if skeleton["contact"]["location"] not in full:
        r.add("skeleton", "locked location missing")
    for exp in skeleton["experience"]:
        if exp["dates"] not in full:
            r.add("skeleton", f"dates for {exp['company']} missing")
    return r
