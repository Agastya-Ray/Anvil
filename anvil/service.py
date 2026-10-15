"""One set of operations shared by the CLI, the HTTP API, the MCP endpoint and the phone page.
Every function takes plain JSON-able arguments and returns JSON-able dicts."""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from . import bandit, ember, engine, guards, ingest, recon, render, store

CLUSTERS = ["ba_pa", "commodity", "consulting", "general", "revops_ops", "implementation"]
OUTCOMES = ["callback", "interview", "offer", "rejected", "ghosted"]


class UserError(Exception):
    """Bad input; shown to the caller as-is."""


def _pool() -> dict:
    pool = store.load(store.vault() / "gene-pool.json")
    if not pool:
        raise UserError("No gene pool. Run `python -m anvil ingest` and commit vault/gene-pool.json.")
    return pool


def leads(include_all: bool = False) -> dict:
    rows = ember.load_leads()
    rows = rows if include_all else ember.actionable(rows)
    status = {a["canonical_id"]: a["outcome"] for a in bandit.load_apps()}
    return {"count": len(rows), "leads": [
        {k: r.get(k, "") for k in ("canonical_id", "verdict", "company", "role", "location", "pay", "url", "posted")}
        | {"anvil_status": status.get(r.get("canonical_id"), "")} for r in rows]}


def tailor(canonical_id: str | None = None, jd_text: str | None = None, company: str | None = None,
           role: str | None = None, url: str | None = None, notes: str | None = None,
           company_url: str | None = None, cluster: str | None = None, polish: bool = False,
           golden: bool = False, dry_run: bool = False, refresh: bool = False, force: bool = False,
           base_url: str = "") -> dict:
    from . import tailor as tailor_mod
    if not canonical_id and not jd_text:
        raise UserError("Give canonical_id (from leads) or jd_text + company + role.")
    if cluster and cluster not in CLUSTERS:
        raise UserError(f"cluster must be one of {CLUSTERS}")
    args = SimpleNamespace(jd_file=None, jd_text=jd_text, company=company, role=role, url=url, leads_file=None,
                           company_url=company_url, notes=notes, cluster=cluster, polish=bool(polish),
                           golden=bool(golden), formats="pdf,docx,html", seed=None, refresh=bool(refresh),
                           dry_run=bool(dry_run), force=bool(force), copy_to=None)
    logs: list[str] = []
    try:
        res = tailor_mod.run(canonical_id, args, log=logs.append)
    except SystemExit as e:
        raise UserError(str(e)) from e
    app = res["application"]
    sid = store.safe_id(app["canonical_id"])
    links = {f: f"{base_url}/api/v1/cv/{sid}.{f}" for f in ("pdf", "docx", "html")}
    return {
        "canonical_id": app["canonical_id"], "company": app["company"], "role": app["role"],
        "cluster": app["cluster"], "dna_hash": app["dna_hash"], "logged": not res["dry_run"],
        "pages": res["pages"], "downloads": links, "jd_diff": res["jd_diff"],
        "jd_diff_markdown": _diff_md(res), "changes": res["changes"], "log": logs,
        "cv_text": (Path(res["out_dir"]) / f"{res['base']}.txt").read_text(encoding="utf-8"),
    }


def _diff_md(res: dict) -> str:
    from . import diff
    a = res["application"]
    return diff.to_markdown(res["jd_diff"], a["company"], a["role"])


def cv_file(safe_canonical_id: str, fmt: str) -> tuple[bytes, str, str]:
    """Re-render a saved CV recipe. Returns (bytes, content_type, filename)."""
    saved = store.read_state(f"cv/{store.safe_id(safe_canonical_id)}.json")
    if not saved:
        raise UserError("No CV saved for that id. Run tailor first.")
    pool, sk = _pool(), store.skeleton()
    missing = [g for g in _recipe_genes(saved["recipe"]) if g not in pool["genes"]]
    if missing:
        raise UserError("The gene pool changed since this CV was made; run tailor again.")
    plan = engine.Plan(cluster=saved["recipe"]["cluster"], recipe=saved["recipe"], texts=saved.get("texts", {}))
    blocks = render.build_doc(pool, sk, plan)
    base = f"Swastik_Kumar_Ray_{store.file_company(saved['company'])}"
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        if fmt == "pdf":
            render.to_pdf_fpdf(blocks, t / "cv.pdf")
            return (t / "cv.pdf").read_bytes(), "application/pdf", base + ".pdf"
        if fmt == "docx":
            render.to_docx(blocks, t / "cv.docx")
            return ((t / "cv.docx").read_bytes(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document", base + ".docx")
        if fmt == "html":
            return render.to_html(blocks, base).encode(), "text/html; charset=utf-8", base + ".html"
        if fmt == "txt":
            return render.to_text(blocks).encode(), "text/plain; charset=utf-8", base + ".txt"
    raise UserError("format must be pdf, docx, html or txt")


def _recipe_genes(r: dict) -> list[str]:
    ids = [r["summary"]] + ([r["headline"]] if r.get("headline") else []) + r["projects"] + r["skills"]
    return ids + [i for v in r["bullets"].values() for i in v]


def feedback(canonical_id: str, outcome: str, note: str = "") -> dict:
    if outcome not in OUTCOMES:
        raise UserError(f"outcome must be one of {OUTCOMES}")
    try:
        app = bandit.apply_outcome(canonical_id, outcome, note or "")
    except SystemExit as e:
        raise UserError(str(e)) from e
    return {"canonical_id": app["canonical_id"], "outcome": app["outcome"], "cluster": app["cluster"],
            "dna_hash": app["dna_hash"], "genes_updated": len(app["genes_used"])}


def applications(limit: int = 50) -> dict:
    apps = bandit.load_apps()[-limit:]
    return {"count": len(apps), "applications": [
        {k: a.get(k) for k in ("canonical_id", "company", "role", "cluster", "dna_hash", "applied_at", "outcome")}
        for a in reversed(apps)]}


def stats() -> dict:
    log = bandit.load_log()
    pool = store.load(store.vault() / "gene-pool.json") or {"genes": {}}
    apps = bandit.load_apps()
    by: dict = {}
    for a in apps:
        by.setdefault(a["cluster"], {}).setdefault(a["outcome"], 0)
        by[a["cluster"]][a["outcome"]] += 1
    learned = {}
    for c, arms in log["genes"].items():
        moved = [(round(v["alpha"] / (v["alpha"] + v["beta"]), 2), gid, v) for gid, v in arms.items()
                 if v["alpha"] > 1 or v["beta"] > 1]
        if moved:
            learned[c] = [{"mean": m, "alpha": v["alpha"], "beta": v["beta"], "gene": gid,
                           "text": pool["genes"].get(gid, {}).get("text", "")[:100]}
                          for m, gid, v in sorted(moved, reverse=True)[:8]]
    return {"applications_by_cluster": by, "learned": learned,
            "deprecated_hooks": {c: [h for h, v in hs.items() if v["deprecated"]] for c, hs in log["hooks"].items()},
            "golden": {c: [{"dna_hash": r["dna_hash"], "company": r["company"], "outcome": r["outcome"]} for r in g]
                       for c, g in log["golden"].items()}}


def get_recon(canonical_id: str) -> dict:
    r = recon.load_recon(canonical_id)
    if not r:
        raise UserError("No recon saved for that id. Run tailor first.")
    r = dict(r)
    r.pop("company_text", None)
    r["jd"] = (r.get("jd") or "")[:6000]
    return r


def ghost_sweep(days: int = bandit.GHOST_DAYS) -> dict:
    return {"ghosted": bandit.ghost_sweep(days)}


def check(text: str) -> dict:
    pool, sk = _pool(), store.skeleton()
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(text)
    try:
        lines = [ln.strip("-*• #\t") for ln in ingest.read_lines(Path(f.name)) if ln.strip()]
    finally:
        Path(f.name).unlink(missing_ok=True)
    rep = guards.check_document(lines, sk, set(pool["known_nouns"]), set(pool["known_numbers"]))
    return {"ok": rep.ok, "violations": [str(v) for v in rep.violations]}


def status() -> dict:
    import os
    return {"ok": True, "service": "anvil", "state_backend": store.backend(),
            "ember_key": bool(os.environ.get("EMBER_API_KEY")),
            "llm_keys": bool(os.environ.get("GROQ_API_KEY") or os.environ.get("GEMINI_API_KEY")
                             or os.environ.get("EMBER_LLM_KEYS")),
            "genes": len(_pool()["genes"])}
