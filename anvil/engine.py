"""Gene scoring and CV assembly. 0 tokens unless --polish is used."""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections import Counter
from dataclasses import dataclass, field

from . import bandit, guards
from .taxonomy import SKILLS, phrase_in, tokenize

W_REL, W_BANDIT, W_TAG = 0.55, 0.35, 0.10
# Rough one-page budget in "lines" (A4, 10.5pt, ~105 chars per bullet line). Tuned so
# every real CV fits; the PDF page check trims further if the renderer disagrees.
CHARS_PER_LINE = 108
PAGE_LINES = 62


# ---------------------------------------------------------------- relevance

def gene_text(g: dict) -> str:
    if g["kind"] == "project":
        return g["meta"]["title"] + " " + " ".join(g["meta"]["bullets"])
    return g["text"]


class BM25:
    def __init__(self, docs: dict[str, str], k1: float = 1.4, b: float = 0.6):
        self.k1, self.b = k1, b
        self.toks = {d: tokenize(t) for d, t in docs.items()}
        n = len(docs) or 1
        self.avg = sum(len(t) for t in self.toks.values()) / n or 1
        df = Counter()
        for t in self.toks.values():
            df.update(set(t))
        self.idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}

    def score(self, doc_id: str, query: Counter) -> float:
        toks = self.toks[doc_id]
        tf = Counter(toks)
        dl = len(toks) or 1
        s = 0.0
        for w, qf in query.items():
            if w not in tf:
                continue
            f = tf[w]
            s += self.idf.get(w, 0) * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / self.avg)) * min(qf, 3)
        return s


def skill_hits(text_low: str) -> set[str]:
    return {canon for canon, aliases in SKILLS.items() if any(phrase_in(a, text_low) for a in aliases)}


def relevance(pool: dict, query_text: str, extra_text: str = "") -> dict[str, float]:
    genes = {gid: g for gid, g in pool["genes"].items()}
    bm = BM25({gid: gene_text(g) for gid, g in genes.items()})
    q = Counter(tokenize(query_text))
    for w in tokenize(extra_text):  # recon/company terms count half
        q[w] += 0.5
    q_skills = skill_hits(query_text.lower())
    raw = {}
    for gid, g in genes.items():
        s = bm.score(gid, q)
        s += 1.0 * len(skill_hits(gene_text(g).lower()) & q_skills)
        raw[gid] = s
    # normalise within each (kind, section) group so summaries compete with summaries
    groups: dict[tuple, list[str]] = {}
    for gid, g in genes.items():
        groups.setdefault((g["kind"], g["section"]), []).append(gid)
    out = {}
    for ids in groups.values():
        mx = max(raw[i] for i in ids) or 1.0
        for i in ids:
            out[i] = raw[i] / mx if mx > 0 else 0.0
    return out


# ---------------------------------------------------------------- assembly

@dataclass
class Plan:
    cluster: str
    recipe: dict
    scores: dict = field(default_factory=dict)
    texts: dict = field(default_factory=dict)      # gene_id -> final text (after swaps/polish)
    changes: list = field(default_factory=list)    # human-readable list of swaps/polish applied
    dropped: list = field(default_factory=list)


def dna_hash(recipe: dict) -> str:
    core = {k: recipe[k] for k in ("cluster", "headline", "summary", "bullets", "projects", "skills",
                                   "section_order", "swaps", "polished")}
    return hashlib.sha1(json.dumps(core, sort_keys=True).encode()).hexdigest()[:12]


def hook_id(recipe: dict) -> str:
    top = recipe["bullets"].get("fendahl", [])[:2]
    core = [recipe["summary"], recipe["headline"], *top]
    return "h_" + hashlib.sha1("|".join(map(str, core)).encode()).hexdigest()[:10]


def _score(g: dict, rel: float, theta: float, cluster: str) -> float:
    return W_REL * rel + W_BANDIT * theta + W_TAG * (1.0 if cluster in g["clusters"] else 0.0)


def est_lines(text: str, width: int = CHARS_PER_LINE) -> int:
    return max(1, math.ceil(len(text) / width))


def estimate_page_lines(pool: dict, recipe: dict, skeleton: dict, texts: dict) -> int:
    g = pool["genes"]
    n = 4  # name, headline, contact, spacing
    n += 1 + est_lines(texts.get(recipe["summary"], g[recipe["summary"]]["text"]), 118)
    n += 1
    for key, ids in recipe["bullets"].items():
        n += 1.3 + sum(est_lines(texts.get(i, g[i]["text"])) for i in ids)
    if recipe["projects"]:
        n += 1
        for pid in recipe["projects"]:
            n += 1 + sum(est_lines(b) for b in g[pid]["meta"]["bullets"])
    n += 1 + len(skeleton["education"]) * 2.4
    n += 1 + sum(est_lines(texts.get(s, g[s]["text"]), 112) for s in recipe["skills"])
    return math.ceil(n)


def choose(pool: dict, skeleton: dict, cluster: str, rel: dict[str, float], log: dict,
           rng: random.Random, golden: dict | None = None, page_lines: int = PAGE_LINES) -> Plan:
    genes = pool["genes"]
    live = {gid: g for gid, g in genes.items() if not g["blocked"]}
    scores = {gid: _score(g, rel.get(gid, 0), bandit.sample(log, cluster, gid, rng), cluster)
              for gid, g in live.items()}
    for gid, g in live.items():
        # Projects are not explored: the one listed first in his real CVs is his main one, and the
        # second ("Earlier web app on the same problem") only reads right next to it.
        if g["kind"] == "project":
            scores[gid] = W_REL * rel.get(gid, 0) + (0.5 if any(s["pos"] == 0 for s in g["sources"]) else 0)

    def ranked(kind: str, section: str | None = None) -> list[str]:
        ids = [gid for gid, g in live.items() if g["kind"] == kind and (section is None or g["section"] == section)]
        return sorted(ids, key=lambda i: scores[i], reverse=True)

    summaries = ranked("summary")
    if not summaries:
        raise SystemExit("No usable summary in the gene pool (all blocked?). Check vault/gene-pool.json.")
    headlines = ranked("headline")
    bullets = {exp["key"]: ranked("bullet", exp["key"])[: exp["max_bullets"]] for exp in skeleton["experience"]}
    projects = ranked("project")
    skills = ranked("skill_group")

    if cluster == "commodity":
        order = ["summary", "education", "experience", "projects", "skills"]
    elif cluster == "general":
        order = ["summary", "experience", "projects", "education", "skills"]
    else:
        order = ["summary", "experience", "education", "projects", "skills"]

    recipe = {
        "cluster": cluster,
        "headline": headlines[0] if headlines else None,
        "summary": summaries[0],
        "bullets": bullets,
        "projects": projects,
        "skills": skills,
        "section_order": order,
        "swaps": [],
        "polished": [],
    }

    if golden:  # replay a recipe that already won a callback, keeping only genes that still exist
        r = golden["recipe"]
        if r.get("summary") in live:
            recipe["summary"] = r["summary"]
        if r.get("headline") in live or r.get("headline") is None:
            recipe["headline"] = r.get("headline")
        for k, ids in r.get("bullets", {}).items():
            ok = [i for i in ids if i in live]
            if ok:
                recipe["bullets"][k] = ok
        recipe["section_order"] = r.get("section_order", order)
        # Genes that left the pool are not backfilled by the replay: top each role up from the ranking
        # to its minimum, and cut it to its maximum.
        for exp in skeleton["experience"]:
            key = exp["key"]
            cur = [i for i in recipe["bullets"].get(key, []) if i in live]
            for gid in ranked("bullet", key):
                if len(cur) >= exp["min_bullets"]:
                    break
                if gid not in cur:
                    cur.append(gid)
            recipe["bullets"][key] = cur[: exp["max_bullets"]]

    # Escape deprecated hooks (8+ sends, 0 replies): mutate summary, then top-bullet order.
    # If no variant escapes, the original recipe is restored.
    if bandit.is_deprecated(log, cluster, hook_id(recipe)):
        orig_summary = recipe["summary"]
        orig_fendahl = list(recipe["bullets"].get("fendahl", []))
        fixed = False
        for s in summaries:
            recipe["summary"] = s
            if not bandit.is_deprecated(log, cluster, hook_id(recipe)):
                fixed = True
                break
        fb = orig_fendahl
        if not fixed and len(fb) >= 3:
            for j in range(2, len(fb)):
                trial = fb[:]
                trial[1], trial[j] = trial[j], trial[1]
                recipe["bullets"]["fendahl"] = trial
                if not bandit.is_deprecated(log, cluster, hook_id(recipe)):
                    fixed = True
                    break
        if not fixed:
            recipe["summary"] = orig_summary
            recipe["bullets"]["fendahl"] = orig_fendahl

    plan = Plan(cluster=cluster, recipe=recipe, scores=scores)
    _fit_page(plan, pool, skeleton, page_lines)
    assert_bullet_bounds(recipe, skeleton)
    return plan


def assert_bullet_bounds(recipe: dict, skeleton: dict) -> None:
    """Every experience block must stay within its min/max bullets (skeleton.json)."""
    for exp in skeleton["experience"]:
        n = len(recipe["bullets"].get(exp["key"], []))
        if not exp["min_bullets"] <= n <= exp["max_bullets"]:
            raise AssertionError(
                f"{exp['key']} has {n} bullets; skeleton allows {exp['min_bullets']}-{exp['max_bullets']}")


def _fit_page(plan: Plan, pool: dict, skeleton: dict, page_lines: int) -> None:
    r = plan.recipe
    mins = {e["key"]: e["min_bullets"] for e in skeleton["experience"]}
    while estimate_page_lines(pool, r, skeleton, plan.texts) > page_lines:
        if not drop_one(plan, mins):
            break


def drop_one(plan: Plan, mins: dict) -> bool:
    """Drop the lowest-scoring removable item: a project (keep at least 1), then a bullet above its role minimum."""
    r = plan.recipe
    cands = []
    if len(r["projects"]) > 1:
        cands.append((plan.scores.get(r["projects"][-1], 0) - 0.05, "project", r["projects"][-1]))
    for key, ids in r["bullets"].items():
        if len(ids) > mins.get(key, 1):
            worst = min(ids, key=lambda i: plan.scores.get(i, 0))
            cands.append((plan.scores.get(worst, 0), key, worst))
    if not cands:
        return False
    _, where, gid = min(cands)
    if where == "project":
        r["projects"].remove(gid)
    else:
        r["bullets"][where].remove(gid)
    plan.dropped.append(gid)
    return True


# ---------------------------------------------------------------- wording

def apply_swaps(plan: Plan, pool: dict, swaps_cfg: dict, jd_low: str) -> None:
    genes = pool["genes"]
    r = plan.recipe
    used = [r["summary"]] + [i for ids in r["bullets"].values() for i in ids] + r["skills"]
    for gid in used:
        text = plan.texts.get(gid, genes[gid]["text"])
        for idx, sw in enumerate(swaps_cfg.get("swaps", [])):
            if not sw.get("verified"):
                continue
            if sw["from"] in text and sw["to"] not in text and any(phrase_in(t, jd_low) for t in sw["when_jd_has"]):
                new = text.replace(sw["from"], sw["to"], 1)
                rep = guards.assert_no_new_facts(genes[gid]["text"], new, allowed_terms={sw["to"]})
                if rep.ok:
                    text = new
                    r["swaps"].append(f"{gid}:{idx}")
                    plan.changes.append(f"swap '{sw['from']}' -> '{sw['to']}'")
        if text != genes[gid]["text"]:
            plan.texts[gid] = text


def approved_terms(swaps_cfg: dict) -> set[str]:
    return {sw["to"] for sw in swaps_cfg.get("swaps", []) if sw.get("verified")}


def apply_polish(plan: Plan, pool: dict, skeleton: dict, swaps_cfg: dict, jd_terms: list[str],
                 company: str, role: str) -> None:
    """Optional LLM pass over the summary and the top 2 Fendahl bullets. Each line is
    accepted only if it passes assert_no_new_facts + style + client-scope checks."""
    from . import llm
    genes = pool["genes"]
    r = plan.recipe
    ids = [r["summary"]] + r["bullets"].get("fendahl", [])[:2]
    src = [plan.texts.get(i, genes[i]["text"]) for i in ids]
    out = llm.polish(src, jd_terms, sorted(approved_terms(swaps_cfg)))
    if out is None:
        plan.changes.append("polish skipped: no LLM key worked (verbatim text used)")
        return
    for gid, before, after in zip(ids, src, out):
        if after == before:
            continue
        rep = guards.assert_no_new_facts(genes[gid]["text"] + " " + before, after,
                                         allowed_extra=f"{company} {role}", allowed_terms=approved_terms(swaps_cfg))
        rep.extend(guards.check_style(after, skeleton))
        rep.extend(guards.check_client_scope(after, skeleton))
        ratio = len(after) / max(1, len(before))
        if not 0.85 <= ratio <= 1.15:
            rep.add("length", f"length changed by {ratio:.0%}")
        if rep.ok:
            plan.texts[gid] = after
            r["polished"].append(gid)
            plan.changes.append(f"polish accepted for {gid}")
        else:
            plan.changes.append(f"polish rejected for {gid}: " + "; ".join(str(v) for v in rep.violations))


def headline_text(pool: dict, skeleton: dict, recipe: dict) -> str:
    """Locked title first, then the real headline's non-title segments as a tagline."""
    title = skeleton["title"]
    hid = recipe.get("headline")
    if not hid:
        return title
    parts = [p.strip() for p in re.split(r"\s*\|\s*", pool["genes"][hid]["text"]) if p.strip()]
    tag = [p for p in parts if "analyst" not in p.lower()]
    return " | ".join([title] + tag)
