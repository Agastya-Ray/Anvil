"""Deterministic vocabularies: role clusters, skills/tools, safe synonym evidence.

Everything is plain keyword matching so tailoring costs 0 tokens.
"""
from __future__ import annotations

import re

CLUSTERS = ["ba_pa", "commodity", "consulting", "general", "revops_ops", "implementation"]

# Keyword -> weight per cluster. Used both to tag genes and to classify a JD.
CLUSTER_KEYWORDS: dict[str, dict[str, float]] = {
    "ba_pa": {
        "business analyst": 4, "product analyst": 4, "business analysis": 3, "requirement": 2,
        "requirements": 2, "uat": 2, "user acceptance": 2, "testing": 1, "defect": 1.5, "bug": 1,
        "stakeholder": 1, "brd": 2, "brds": 2, "frd": 2, "user stories": 2, "jira": 1, "product": 1,
        "analytics": 1, "functional": 1.5, "documentation": 1, "apm": 3, "associate product manager": 4,
        "product manager": 2,
    },
    "commodity": {
        "ctrm": 4, "etrm": 4, "commodity": 3, "commodities": 3, "trading": 2, "trade": 1.5,
        "energy": 1.5, "agri": 2, "agriculture": 2, "oil": 1.5, "petronas": 1, "cbh": 1,
        "pricing": 1, "risk": 1, "incoterms": 2, "freight": 1.5, "shipping": 1, "botany": 1,
        "physical": 1, "hedging": 1.5, "derivatives": 1,
    },
    "consulting": {
        "consulting": 4, "consultant": 3, "advisory": 3, "strategy": 2.5, "case": 1,
        "research": 1.5, "benchmark": 2, "benchmarking": 2, "revenue": 1.5, "scenario": 1.5,
        "market sizing": 2, "chief of staff": 3, "ey": 1, "client advisory": 3, "financial modelling": 2,
        "financial modeling": 2, "due diligence": 2,
    },
    "revops_ops": {
        "operations": 2.5, "ops": 2, "revops": 4, "revenue operations": 4, "process": 1.5,
        "workflow": 2, "backlog": 1.5, "excel": 1, "kpi": 1.5, "dashboard": 1.5, "reporting": 1.5,
        "crm": 2, "hubspot": 2, "salesforce": 1.5, "approval": 1, "sop": 1.5, "data quality": 2,
        "reconciliation": 2, "finance operations": 3, "strategy & operations": 3, "bizops": 3,
        "customer success": 2, "cs ops": 3, "liveops": 3,
    },
    "implementation": {
        "implementation": 4, "onboarding": 3, "solutions": 2, "solution": 1.5, "functional consultant": 4,
        "migration": 2.5, "data migration": 3, "go-live": 2, "configuration": 1.5, "client delivery": 2,
        "deployment": 1.5, "demo": 1.5, "pre-sales": 2, "presales": 2, "customer": 1, "integration": 1.5,
        "saas": 1, "rollout": 1.5,
    },
    "general": {
        "ai": 2, "llm": 3, "prompt": 3, "genai": 3, "generative": 2, "automation": 1.5,
        "chatgpt": 2, "claude": 2, "machine learning": 1.5, "python": 1, "startup": 1, "analyst": 1,
    },
}

# Skills/tools vocabulary for the 3-bucket JD diff. canonical -> aliases (lowercase).
SKILLS: dict[str, list[str]] = {
    "SQL": ["sql", "mysql", "postgres", "postgresql", "bigquery", "snowflake"],
    "Excel": ["excel", "spreadsheet", "spreadsheets", "vlookup", "pivot"],
    "Advanced Excel": ["advanced excel", "pivottables", "pivot tables", "index-match"],
    "Excel macros / VBA": ["vba", "macros", "macro"],
    "Power BI": ["power bi", "powerbi"],
    "Tableau": ["tableau"],
    "Looker": ["looker", "looker studio"],
    "Python": ["python", "pandas"],
    "R": [" r ", "rstudio"],
    "Jira": ["jira"],
    "Confluence": ["confluence"],
    "SAP": ["sap", "s/4hana", "sap fico", "sap mm"],
    "Salesforce": ["salesforce", "sfdc"],
    "HubSpot": ["hubspot"],
    "CRM": ["crm", "crm hygiene"],
    "SaaS metrics": ["saas metrics", "arr", "mrr", "ndr", "gdr", "churn", "net revenue retention"],
    "Customer success": ["customer success", "renewals", "retention"],
    "Google Analytics": ["google analytics", "ga4"],
    "Mixpanel / Amplitude": ["mixpanel", "amplitude"],
    "Figma": ["figma"],
    "A/B testing": ["a/b test", "ab test", "experimentation", "experiments"],
    "Agile / Scrum": ["agile", "scrum", "sprint"],
    "Requirement analysis": ["requirement analysis", "requirements analysis", "requirement gathering",
                             "requirements gathering", "gather requirements", "gathering requirements",
                             "requirements elicitation"],
    "BRD / FRD": ["brd", "brds", "frd", "frds", "business requirement document", "functional specification",
                  "functional spec", "requirements document", "requirement documents", "prd"],
    "User stories": ["user stories", "user story", "acceptance criteria"],
    "UAT / functional testing": ["uat", "user acceptance", "functional testing", "test cases",
                                 "testing", "qa ", "quality assurance", "regression"],
    "Defect management": ["defect", "defects", "bug", "bugs", "triage", "issue tracking"],
    "Impact analysis": ["impact analysis", "impact assessment", "gap analysis"],
    "Process optimisation": ["process improvement", "process optimization", "process optimisation",
                             "workflow", "streamline"],
    "Stakeholder management": ["stakeholder", "stakeholders", "cross-functional", "cross functional"],
    "Data migration": ["data migration", "migration", "migrate"],
    "Data quality / reconciliation": ["reconciliation", "reconcile", "data quality", "data hygiene", "data cleansing",
                                      "data cleaning", "data validation", "data integrity"],
    "Documentation": ["documentation", "document processes", "sops", "sop"],
    "Dashboards / KPI reporting": ["dashboard", "dashboards", "kpi", "kpis", "reporting", "metrics"],
    "Financial modelling": ["financial model", "financial modelling", "financial modeling",
                            "scenario analysis", "forecasting", "valuation"],
    "Market research / benchmarking": ["market research", "benchmarking", "competitive analysis",
                                       "industry research", "research"],
    "Product demos / pre-sales": ["demo", "demos", "pre-sales", "presales", "solution consulting"],
    "Client onboarding / implementation": ["implementation", "onboarding", "go-live", "rollout",
                                           "client delivery"],
    "CTRM / ETRM": ["ctrm", "etrm", "commodity trading", "trade lifecycle", "trade capture"],
    "Commodity pricing / risk": ["pricing", "price risk", "hedging", "mark-to-market", "mtm",
                                 "exposure", "credit risk"],
    "Incoterms / shipping": ["incoterms", "freight", "shipping", "logistics", "vessel"],
    "Prompt engineering / LLMs": ["prompt engineering", "prompt", "llm", "llms", "genai",
                                  "generative ai", "chatgpt", "ai tools"],
    "API / integrations": ["api", "apis", "rest", "integration", "integrations", "postman"],
    "Accounting / ERP": ["accounting", "erp", "netsuite", "oracle", "quickbooks", "tally"],
    "Statistics": ["statistics", "statistical", "regression analysis", "hypothesis"],
    "Project management": ["project management", "pmp", "timelines", "deliverables"],
}

# Bucket 2 evidence: JD skill -> phrases that MUST exist in his real CV text for the
# skill to count as an "implied safe swap". The swap phrase is what we may say in the
# diff, never added to the CV unless it is also in vault/swaps.json.
IMPLIED_EVIDENCE: dict[str, list[str]] = {
    "Requirement analysis": ["requirement analysis"],
    "UAT / functional testing": ["functional/uat testing", "functional and workflow testing"],
    "Defect management": ["defects", "bug reproduction"],
    "Documentation": ["documentation"],
    "Data quality / reconciliation": ["data quality discrepancies", "corrected 2,300+ rows"],
    "Data migration": ["data migration"],
    "Process optimisation": ["process optimization", "workflow changes"],
    "Stakeholder management": ["stakeholder management", "single point of contact"],
    "Dashboards / KPI reporting": ["kpi dashboards"],
    "Financial modelling": ["scenario-analysis modelling", "financial modelling"],
    "Market research / benchmarking": ["benchmarking", "researched global best practices"],
    "Product demos / pre-sales": ["demo environments", "pre-sales"],
    "Client onboarding / implementation": ["client work across the portfolio", "implementation"],
    "Prompt engineering / LLMs": ["prompt engineering", "engineered prompts"],
    "Excel": ["excel"],
    "Advanced Excel": ["advanced excel"],
    "CTRM / ETRM": ["ctrm"],
    "Commodity pricing / risk": ["pricing (indexes", "risk across credit"],
    "Incoterms / shipping": ["incoterms", "shipping insurance"],
    "Project management": ["ahead of the timeline", "holding the delivery timeline"],
}

STOPWORDS = set("""
a an and are as at be been but by can could did do does for from had has have he her his i if in into
is it its may might more most must no not of on or our out over she should so such than that the their
them then there these they this those through to under up us very was we were what when where which while
who will with would you your yours about across after all also any because before being both each etc
few further here how just like many much new now only other own same some still too via within without
work working role team teams experience years year strong ability skills skill including include using use
across well good great plus join help across able based within per make made making ensure ensuring
""".split())


def tokenize(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9][a-z0-9+/#&.-]*", text.lower())
    return [t.strip(".-") for t in toks if t.strip(".-") and t.strip(".-") not in STOPWORDS and len(t) > 1]


def phrase_in(phrase: str, text_low: str) -> bool:
    p = phrase.lower()
    if p.startswith(" ") or p.endswith(" "):
        return p in f" {text_low} "
    return re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", text_low) is not None


def cluster_scores(text: str, title: str = "") -> dict[str, float]:
    body = text.lower()
    head = title.lower()
    scores = {}
    for c, kws in CLUSTER_KEYWORDS.items():
        s = 0.0
        for kw, w in kws.items():
            if phrase_in(kw, body):
                s += w
            if head and phrase_in(kw, head):
                s += 3 * w
        scores[c] = s
    return scores


def classify(text: str, title: str = "") -> str:
    scores = cluster_scores(text, title)
    best = max(scores.items(), key=lambda kv: kv[1])
    return best[0] if best[1] > 0 else "general"


def tag_clusters(text: str, threshold: float = 2.0) -> list[str]:
    scores = cluster_scores(text)
    tags = [c for c, s in scores.items() if s >= threshold]
    return tags or ["general"]
