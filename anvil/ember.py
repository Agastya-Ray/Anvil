"""Talks to Ember (Agastya-Ray/FireAlt): reads leads from the leads-data branch and
calls the live /api/v1/scrape endpoint. Never uses Ember search."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

BASE = os.environ.get("EMBER_BASE_URL", "https://fire-alt.vercel.app").rstrip("/")
LEADS_URL = os.environ.get(
    "ANVIL_LEADS_URL", "https://raw.githubusercontent.com/Agastya-Ray/FireAlt/leads-data/open-leads.csv")
ACTIONABLE = ("PASS", "PARK", "HIGHPAY")

ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "smartrecruiters.com",
             "workable.com", "teamtailor.com", "jobvite.com", "remotive.com", "weworkremotely.com",
             "linkedin.com", "naukri.com", "wellfound.com", "instahyre.com", "successfactors.com",
             "icims.com", "bamboohr.com", "recruitee.com", "breezy.hr", "rippling.com", "talentbrew",
             "indeed.com", "glassdoor", "cutshort.io", "hirist", "foundit")


class EmberError(Exception):
    pass


def _key() -> str:
    k = os.environ.get("EMBER_API_KEY", "").strip()
    if not k:
        raise EmberError("EMBER_API_KEY is not set (the emb_... key from Ember's Connect page).")
    return k


def scrape(url: str, max_tokens: int = 2500, timeout: int = 60) -> dict:
    """POST /api/v1/scrape -> {url, title, markdown, text, links, ...}."""
    body = json.dumps({"url": url, "maxTokens": max_tokens, "onlyMainContent": True}).encode()
    req = urllib.request.Request(f"{BASE}/api/v1/scrape", data=body, method="POST", headers={
        "content-type": "application/json", "authorization": f"Bearer {_key()}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise EmberError(f"Ember scrape {e.code} for {url}: {detail}") from e
    except urllib.error.URLError as e:
        raise EmberError(f"Ember unreachable: {e.reason}") from e
    if not data.get("success"):
        raise EmberError(f"Ember scrape failed for {url}: {str(data)[:300]}")
    return data["data"]


def load_leads(path: str | None = None) -> list[dict]:
    """Read open-leads.csv from a local path, or from the leads-data branch."""
    if path:
        text = Path(path).read_text(encoding="utf-8")
    else:
        try:
            req = urllib.request.Request(LEADS_URL, headers={"user-agent": "anvil"})
            token = os.environ.get("GITHUB_TOKEN")
            if token:
                req.add_header("authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise EmberError("open-leads.csv not found on FireAlt's leads-data branch yet "
                                 "(the daily sweep has not pushed it). Use --leads-file.") from e
            raise EmberError(f"could not read leads: HTTP {e.code}") from e
    return list(csv.DictReader(io.StringIO(text)))


def actionable(leads: list[dict], verdicts=ACTIONABLE) -> list[dict]:
    return [r for r in leads if (r.get("verdict") or "").upper() in verdicts]


def find_lead(leads: list[dict], canonical_id: str) -> dict | None:
    for r in leads:
        if r.get("canonical_id") == canonical_id:
            return r
    return None


def is_ats(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(h in host for h in ATS_HOSTS)


def company_url_candidates(lead: dict, jd_markdown: str = "") -> list[str]:
    """Best guesses for the company's own site, most likely first."""
    out = []
    url = lead.get("url") or ""
    if url and not is_ats(url):
        p = urlparse(url)
        host = re.sub(r"^(careers?|jobs|apply|work)\.", "www.", p.netloc.lower())
        out.append(f"{p.scheme}://{host}")
    for m in re.findall(r"https?://[^\s)\]>\"']+", jd_markdown or ""):
        if not is_ats(m) and not re.search(r"(google|facebook|twitter|x\.com|youtube|instagram|glassdoor|"
                                           r"apple\.com|microsoft|cloudflare|gstatic|w3\.org)", m):
            p = urlparse(m)
            root = f"{p.scheme}://{p.netloc}"
            if root not in out:
                out.append(root)
    slug = (lead.get("canonical_id") or "::").split(":")[1] if ":" in (lead.get("canonical_id") or "") else ""
    slug = re.sub(r"[^a-z0-9]", "", (slug or lead.get("company", "")).lower())
    if slug:
        for tld in (".com", ".io", ".ai", ".co"):
            out.append(f"https://www.{slug}{tld}")
    return out[:6]
