"""Hard guardrails. These run in code on every generated line and on the final CV.

Nothing here asks an LLM. If a check fails, the caller must fall back to the
verbatim source gene (polish) or refuse to write the CV (final check).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
# A "proper noun" here is any token that starts with a capital letter or is an
# acronym/mixed token (Power BI, CTRM, LoD1, Fusion). Sentence-initial words are
# included on purpose: the check is "every capitalised token must already exist
# in an allowed source", so a capitalised common word that was in the source passes.
CAP_RE = re.compile(r"\b[A-Z][A-Za-z0-9&+./'’-]*")


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


def _norm_num(n: str) -> str:
    return n.replace(",", "")


def numbers(text: str) -> set[str]:
    return {_norm_num(m) for m in NUM_RE.findall(text)}


LEAD_VERB_RE = re.compile(r"^[A-Z][a-z]+(ed|ing)$")


def cap_tokens(text: str) -> set[str]:
    """Capitalised tokens, except a plain past-tense/-ing verb opening a sentence
    ("Resolved", "Managing"), which is how bullets start and is not a name."""
    out = set()
    for m in CAP_RE.finditer(text):
        tok = m.group(0).strip(".,;:'’-/")
        if not tok:
            continue
        before = text[: m.start()].rstrip()
        sentence_start = not before or before[-1] in ".;:!?\n–-•"
        if sentence_start and LEAD_VERB_RE.match(tok):
            continue
        out.add(tok.lower())
    return out


def check_style(text: str, skeleton: dict) -> Report:
    """Anti-AI-detector filter: banned chars and AI-tell words."""
    r = Report()
    for ch in skeleton.get("banned_chars", []):
        if ch in text:
            r.add("banned_char", f"contains {ch!r}", text)
    low = text.lower()
    for w in skeleton.get("banned_phrases", []):
        if re.search(r"(?<![a-z])" + re.escape(w.lower()) + r"(?![a-z])", low):
            r.add("banned_word", f"uses {w!r}", text)
    return r


def _clauses(text: str) -> list[str]:
    # Clause-level so "CBH – testing; Petronas – import/export; ..." is judged per client.
    return [c for c in re.split(r"[;.\n]|\s–\s(?=[A-Z][a-z]+ –)", text) if c.strip()]


def check_client_scope(text: str, skeleton: dict) -> Report:
    """Petronas = import/export only, etc. Judged per clause."""
    r = Report()
    scope = skeleton.get("clients", {}).get("scope", {})
    for clause in _clauses(text):
        low = clause.lower()
        for client, rule in scope.items():
            if client.lower() not in low:
                continue
            for bad in rule.get("forbidden_terms", []):
                if re.search(r"(?<![a-z])" + re.escape(bad.lower()), low):
                    r.add("client_scope", f"{client} named next to '{bad}'", clause.strip())
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
