"""JD + company recon through Ember's /api/v1/scrape. Results are cached in
state/recon/<safe_id>-recon.json so Issue 3 (cold email) never re-scrapes."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import ember, store
from .taxonomy import STOPWORDS, tokenize

PAIN_SIGNALS = ("you will", "you'll", "help us", "challenge", "problem", "scale", "improve", "own ",
                "responsible for", "build", "streamline", "automate", "reduce", "fix", "drive", "manage")
GENERIC = set("""
cookie cookies privacy policy terms contact login sign careers blog news press home menu learn more get started
started request demo book free trial pricing resources solutions products company customers partners support
copyright rights reserved english language read click here linkedin twitter youtube facebook instagram
""".split())


def recon_key(canonical_id: str) -> str:
    return f"recon/{store.safe_id(canonical_id)}-recon.json"


def load_recon(canonical_id: str) -> dict | None:
    return store.read_state(recon_key(canonical_id))


def sentences(text: str) -> list[str]:
    text = re.sub(r"[#*_`>\[\]]", " ", text)
    text = re.sub(r"\(https?://[^)]+\)", " ", text)
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [re.sub(r"\s+", " ", p).strip() for p in parts if 40 <= len(p.strip()) <= 300]


def product_summary(pages: list[str], company: str) -> list[str]:
    co = company.lower().split()[0] if company else ""
    out = []
    for s in (s for p in pages for s in sentences(p)):
        low = s.lower()
        if any(g in low for g in ("cookie", "privacy", "javascript", "sign in", "log in", "©")):
            continue
        if (co and co in low) or re.search(r"\b(we|our)\b", low) or any(
                w in low for w in ("platform", "software", "helps", "provides", "solution", "product")):
            out.append(s)
        if len(out) >= 3:
            break
    return out


def product_terms(pages: list[str], jd: str) -> list[str]:
    toks = Counter()
    for p in pages:
        toks.update(t for t in tokenize(p) if t not in GENERIC and not t.isdigit() and len(t) > 3)
    jd_toks = set(tokenize(jd))
    ranked = sorted(toks.items(), key=lambda kv: (kv[0] in jd_toks, kv[1]), reverse=True)
    return [t for t, c in ranked if c >= 2][:15]


BOILERPLATE = ("backed by", "trusted by", "founded", "raised", "funding", "equal opportunity", "benefits",
               "insurance", "leave", "allowance", "we may use", "privacy")


def pain_points(jd: str) -> list[str]:
    """Sentences about the work they need done; 'you'-sentences first, company boilerplate dropped."""
    hits = []
    for i, s in enumerate(sentences(jd)):
        low = s.lower()
        if "$" in s or any(b in low for b in BOILERPLATE):
            continue
        if any(sig in low for sig in PAIN_SIGNALS):
            hits.append((0 if re.search(r"\byou('ll| will)?\b|\bown\b", low) else 1, i, s))
    return [s for _, _, s in sorted(hits)[:6]]


def notes_for(canonical_id: str, company: str, inline: str | None) -> str:
    """--notes text, plus any `## <canonical_id or company>` section in recon-notes.md."""
    chunks = [inline] if inline else []
    f = store.home() / "recon-notes.md"
    if f.exists():
        cur, buf = None, {}
        for ln in f.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^##\s+(.+)$", ln)
            if m:
                cur = m.group(1).strip().lower()
                buf[cur] = []
            elif cur:
                buf[cur].append(ln)
        for key in (canonical_id.lower(), company.lower()):
            if key in buf:
                chunks.append("\n".join(buf[key]).strip())
    return "\n".join(c for c in chunks if c)


def gather(lead: dict, jd_file: str | None = None, company_url: str | None = None,
           refresh: bool = False, log=print, jd_text: str | None = None) -> dict:
    """Returns the recon dict (cached). Raises ember.EmberError only if no JD can be had at all."""
    cid = lead["canonical_id"]
    cached = load_recon(cid) if not refresh else None
    if cached and not jd_file and not jd_text and not company_url:
        return cached
    jd = ""
    source = ""
    if jd_text:
        jd, source = jd_text, "pasted"
    elif jd_file:
        jd = Path(jd_file).read_text(encoding="utf-8")
        source = f"file:{jd_file}"
    elif cached and cached.get("jd"):
        jd, source = cached["jd"], cached.get("jd_source", "cache")
    elif lead.get("url"):
        page = ember.scrape(lead["url"], max_tokens=3000)
        jd = page.get("markdown") or page.get("text") or ""
        source = f"ember:{lead['url']}"
    if len(jd.strip()) < 200:
        raise ember.EmberError(f"JD text for {cid} is too short ({len(jd.strip())} chars). Pass --jd-file.")

    pages, used_urls = [], []
    tries = [company_url] if company_url else ember.company_url_candidates(lead, jd)
    for root in tries:
        if not root:
            continue
        try:
            home = ember.scrape(root, max_tokens=1500)
        except ember.EmberError as e:
            log(f"  recon: {root} unreadable ({str(e)[:80]})")
            continue
        title = (home.get("title") or "") + " " + (home.get("markdown") or "")[:600]
        if re.search(r"just a moment|checking your browser|access denied|attention required", title, re.I):
            log(f"  recon: {root} is behind a bot wall, skipped")
            continue
        first = (lead.get("company") or "").split()[0].lower() if lead.get("company") else ""
        if not company_url and first and first not in title.lower():
            log(f"  recon: {root} does not look like {lead.get('company')}, skipped")
            continue
        pages.append(home.get("markdown") or "")
        used_urls.append(root)
        for about in ("/about", "/about-us", "/company"):
            try:
                a = ember.scrape(root.rstrip("/") + about, max_tokens=1500)
                if len(a.get("markdown") or "") > 300:
                    pages.append(a["markdown"])
                    used_urls.append(root.rstrip("/") + about)
                    break
            except ember.EmberError:
                continue
        break

    # No readable company site (bot wall, guess failed): the JD's own "about us" text is the fallback.
    summary_src = pages or [jd]
    data = {
        "canonical_id": cid,
        "company": lead.get("company", ""),
        "role": lead.get("role", ""),
        "url": lead.get("url", ""),
        "jd_source": source,
        "jd": jd,
        "company_pages": used_urls,
        "product_summary": product_summary(summary_src, lead.get("company", "")),
        "product_terms": product_terms(summary_src, jd),
        "pain_points": pain_points(jd),
        "company_text": "\n\n".join(p[:4000] for p in pages),
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    store.write_state(recon_key(cid), data)
    return data
