"""Honest 3-bucket JD diff. No ATS score, no percentages."""
from __future__ import annotations

import re
from collections import Counter

from . import guards
from .taxonomy import IMPLIED_EVIDENCE, SKILLS, phrase_in

COMMON_CAPS = set("""
we you our the a an and or in on for to of with as at by from this that it is are be will can about who what
if your their they all any team role job remote india hybrid full time part apply company us about benefits
requirements responsibilities qualifications preferred required must nice bonus plus experience skills
monday friday january february march april may june july august september october november december
equal opportunity employer english salary location key what's why how you'll we're you're
""".split())


def cv_corpus(pool: dict) -> str:
    parts = []
    for g in pool["genes"].values():
        if g.get("blocked"):
            continue
        if g["kind"] == "project":
            parts.append(g["meta"]["title"] + " " + " ".join(g["meta"]["bullets"]))
        else:
            parts.append(g["text"])
    return "\n".join(parts)


def three_buckets(jd: str, pool: dict, swaps_cfg: dict, company: str = "") -> dict:
    jd_low = jd.lower()
    cv_low = cv_corpus(pool).lower()
    exact, implied, gaps = [], [], []
    verified_extra = {c["skill"]: c for c in swaps_cfg.get("unverified_claims", []) if c.get("verified")}
    unverified = {c["skill"]: c for c in swaps_cfg.get("unverified_claims", []) if not c.get("verified")}
    for canon, aliases in SKILLS.items():
        jd_hits = [a.strip() for a in aliases if phrase_in(a, jd_low)]
        if not jd_hits:
            continue
        cv_hits = [a.strip() for a in aliases if phrase_in(a, cv_low)]
        same = [a for a in jd_hits if a in cv_hits]
        if same:
            exact.append({"skill": canon, "jd_term": same[0]})
            continue
        ev = [e for e in IMPLIED_EVIDENCE.get(canon, []) if phrase_in(e.lower(), cv_low)]
        if not ev and cv_hits:
            ev = cv_hits[:1]
        if not ev and canon in verified_extra:
            ev = [verified_extra[canon]["evidence"] + " (confirmed by Swastik)"]
        if ev:
            implied.append({"skill": canon, "jd_term": jd_hits[0], "cv_evidence": ev[0]})
        else:
            item = {"skill": canon, "jd_term": jd_hits[0]}
            if canon in unverified or canon.split(" ")[0] in {k.split(" ")[0] for k in unverified}:
                item["note"] = "spec says he did this, but no real CV shows it; confirm before using"
            gaps.append(item)
    # Capitalised JD terms (tools, certifications) the vocabulary does not know and the CV lacks.
    known = set(pool.get("known_nouns", [])) | guards.cap_tokens(company)
    caps = Counter()
    for m in re.findall(r"\b(?:[A-Z]{2,6}\d?|[A-Z][a-z]+[A-Z][A-Za-z]+|[A-Z][a-z]{2,}(?:\s[A-Z][a-z]{2,})?)\b", jd):
        low = m.lower()
        if re.search(r"(?<![A-Za-z])" + re.escape(low) + r"(?![A-Za-z])", jd):  # also used as a plain word
            continue
        if low in COMMON_CAPS or low in known or any(low in s.lower() for s in SKILLS):
            continue
        caps[m] += 1
    other = [t for t, c in caps.most_common(40) if c >= 2 or (t.isupper() and len(t) <= 6)][:12]
    return {"exact": exact, "implied": implied, "gaps": gaps, "other_terms_to_check": other}


def to_markdown(d: dict, company: str, role: str) -> str:
    lines = [f"# JD diff: {role} at {company}", "",
             "No ATS score. Three honest buckets. Bucket 3 is never added to the CV.", ""]
    lines.append(f"## 1. Exact matches ({len(d['exact'])})")
    lines += [f"- {x['skill']} (JD says \"{x['jd_term']}\")" for x in d["exact"]] or ["- none"]
    lines += ["", f"## 2. Implied safe swaps ({len(d['implied'])})",
              "Work he really did, under another name. Fine to say in interviews using the JD's word."]
    lines += [f"- {x['skill']}: JD says \"{x['jd_term']}\", CV shows \"{x['cv_evidence']}\"" for x in d["implied"]] or ["- none"]
    lines += ["", f"## 3. True gaps ({len(d['gaps'])}), interview prep only"]
    lines += [f"- {x['skill']} (JD says \"{x['jd_term']}\")" + (f". Note: {x['note']}" if x.get("note") else "")
              for x in d["gaps"]] or ["- none"]
    if d.get("other_terms_to_check"):
        lines += ["", "## Other JD terms not on the CV (check by eye)",
                  ", ".join(d["other_terms_to_check"])]
    return "\n".join(lines) + "\n"
