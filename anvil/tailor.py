"""`anvil tailor`: one lead in, one guarded one-page CV + JD diff + recon out."""
from __future__ import annotations

import hashlib
import random
import shutil
from pathlib import Path

from . import bandit, diff, engine, ember, guards, ingest, recon, render, store
from .taxonomy import classify, cluster_scores


def _rel(p: Path) -> str:
    """Path relative to ANVIL_HOME when inside it (on Vercel the output lives in /tmp)."""
    try:
        return str(p.relative_to(store.home()))
    except ValueError:
        return str(p)


def load_pool(log=print) -> dict:
    pool = store.load(store.vault() / "gene-pool.json")
    if not pool:
        log("No gene pool yet, running ingest first.")
        pool = ingest.run()
    return pool


def resolve_lead(canonical_id: str | None, args) -> dict:
    jd_text = getattr(args, "jd_text", None)
    if (args.jd_file or jd_text) and (args.company or not canonical_id):
        company = args.company or "Company"
        raw = jd_text.encode() if jd_text else Path(args.jd_file).read_bytes()
        cid = canonical_id or f"manual:{store.file_company(company).lower()}:" + hashlib.sha1(raw).hexdigest()[:8]
        return {"canonical_id": cid, "company": company, "role": args.role or "", "url": args.url or "",
                "verdict": "MANUAL"}
    leads = ember.load_leads(args.leads_file)
    lead = ember.find_lead(leads, canonical_id)
    if not lead:
        raise SystemExit(f"{canonical_id} is not in open-leads.csv. Use `anvil leads` to list ids, "
                         "or pass --jd-file --company --role for a job that is not on the board.")
    if ember.normalize_verdict(lead.get("verdict")) not in ember.ACTIONABLE and not args.force:
        raise SystemExit(f"{canonical_id} has verdict {lead.get('verdict')}; Anvil only tailors "
                         f"{', '.join(ember.ACTIONABLE)}. Use --force to override.")
    if args.role:
        lead["role"] = args.role
    return lead


def run(canonical_id: str | None, args, log=print) -> dict:
    skeleton = store.skeleton()
    pool = load_pool(log)
    swaps_cfg = store.load(store.vault() / "swaps.json", {}) or {}
    ghosted = bandit.ghost_sweep()
    if ghosted:
        log(f"Auto-ghosted (no reply in {bandit.GHOST_DAYS} days): {', '.join(ghosted)}")

    lead = resolve_lead(canonical_id, args)
    cid, company, role = lead["canonical_id"], lead.get("company", ""), lead.get("role", "")
    log(f"Tailoring for {role} at {company} ({cid})")
    rec = recon.gather(lead, jd_file=args.jd_file, company_url=args.company_url, refresh=args.refresh, log=log,
                       jd_text=getattr(args, "jd_text", None))
    jd = rec["jd"]
    notes = recon.notes_for(cid, company, args.notes)
    query = "\n".join([role, role, jd, notes])  # role title counts extra
    extra = " ".join(rec.get("product_terms", [])) + "\n" + rec.get("company_text", "")[:2500]

    cluster = args.cluster or classify(jd + "\n" + notes, role)
    rel = engine.relevance(pool, query, extra)
    blog = bandit.load_log()
    rng = random.Random(args.seed)
    golden = None
    if args.golden:
        g = blog["golden"].get(cluster, [])
        golden = g[-1] if g else None
        if not golden:
            log(f"No golden variant for {cluster} yet; sampling normally.")

    plan = engine.choose(pool, skeleton, cluster, rel, blog, rng, golden=golden)
    engine.apply_swaps(plan, pool, swaps_cfg, jd.lower())
    buckets = diff.three_buckets(jd, pool, swaps_cfg, company)
    if args.polish:
        jd_terms = [x["jd_term"] for x in buckets["exact"] + buckets["implied"]]
        engine.apply_polish(plan, pool, skeleton, swaps_cfg, jd_terms, company, role)

    # Final hard gate over every rendered line.
    known_nouns = set(pool["known_nouns"])
    for t in engine.approved_terms(swaps_cfg):
        known_nouns |= guards.cap_tokens(t)
    known_numbers = set(pool["known_numbers"])

    out = store.out_dir() / store.safe_id(cid)
    out.mkdir(parents=True, exist_ok=True)
    base = f"Swastik_Kumar_Ray_{store.file_company(company)}"
    formats = [f.strip() for f in args.formats.split(",") if f.strip()]
    mins = {e["key"]: e["min_bullets"] for e in skeleton["experience"]}
    pdf_engine, pages = None, None
    for attempt in range(12):
        blocks = render.build_doc(pool, skeleton, plan)
        rep = guards.check_document(render.doc_lines(blocks), skeleton, known_nouns, known_numbers)
        if not rep.ok:
            msg = "\n  ".join(str(v) for v in rep.violations)
            raise SystemExit(f"Guardrail check failed, no CV written:\n  {msg}")
        html_p, docx_p, pdf_p = out / f"{base}.html", out / f"{base}.docx", out / f"{base}.pdf"
        html_p.write_text(render.to_html(blocks, f"{skeleton['name'].title()} - {company}"), encoding="utf-8")
        if "docx" in formats or "pdf" in formats:
            render.to_docx(blocks, docx_p)
        if "pdf" not in formats:
            break
        pdf_engine = render.to_pdf(docx_p, html_p, pdf_p, blocks)
        pages = render.page_count(pdf_p) if pdf_engine else None
        if not pages or pages <= 1:
            break
        log(f"  {pages} pages, trimming one item")
        if not engine.drop_one(plan, mins):
            break
    if pages and pages > 1:
        raise SystemExit(f"CV is {pages} pages even at the minimum bullets per role; not logged as sent.")
    for f in ("html", "docx"):
        if f not in formats:
            (out / f"{base}.{f}").unlink(missing_ok=True)
    (out / f"{base}.txt").write_text(render.to_text(blocks), encoding="utf-8")

    recipe = plan.recipe
    dna = engine.dna_hash(recipe)
    hook = engine.hook_id(recipe)
    genes_used = ([recipe["summary"]] + ([recipe["headline"]] if recipe["headline"] else [])
                  + [i for ids in recipe["bullets"].values() for i in ids] + recipe["projects"] + recipe["skills"])

    (out / "jd-diff.md").write_text(diff.to_markdown(buckets, company, role), encoding="utf-8")
    top = sorted((gid for gid in genes_used if pool["genes"][gid]["kind"] == "bullet"),
                 key=lambda g: rel.get(g, 0), reverse=True)[:5]
    rec_out = {k: rec[k] for k in ("canonical_id", "company", "role", "url", "company_pages", "product_summary",
                                   "product_terms", "pain_points", "scraped_at")}
    rec_out.update({
        "cluster": cluster,
        "cluster_scores": cluster_scores(jd, role),
        "dna_hash": dna,
        "matched_hooks": [{"gene": g, "text": plan.texts.get(g, pool["genes"][g]["text"])} for g in top],
        "jd_diff": buckets,
        "notes": notes,
        "cv_files": [_rel(p) for p in sorted(out.glob(base + ".*"))],
    })
    full = recon.load_recon(cid) or {}
    full.update(rec_out)
    store.write_state(recon.recon_key(cid), full)
    store.save(out / f"{store.safe_id(cid)}-recon.json", rec_out)

    app = {"canonical_id": cid, "company": company, "role": role, "cluster": cluster, "dna_hash": dna,
           "hook_id": hook, "genes_used": genes_used, "recipe": recipe, "applied_at": bandit.now(),
           "outcome": "PENDING", "out_dir": _rel(out)}
    # Saved recipe so any client (phone page, MCP, another AI) can re-download this exact CV later.
    store.write_state(f"cv/{store.safe_id(cid)}.json", {
        "canonical_id": cid, "company": company, "role": role, "dna_hash": dna, "recipe": recipe,
        "texts": plan.texts, "created_at": bandit.now(), "pages": pages, "dry_run": bool(args.dry_run)})
    app["texts"] = plan.texts
    if args.dry_run:
        log("Dry run: not logged to state/applications.json")
    else:
        bandit.record_application(app)

    log(f"  cluster: {cluster}   dna: {dna}   hook: {hook}")
    log(f"  summary from: {pool['genes'][recipe['summary']]['sources'][0]['file']}")
    for c in dict.fromkeys(plan.changes):
        log(f"  {c}")
    log(f"  JD diff: {len(buckets['exact'])} exact, {len(buckets['implied'])} implied, {len(buckets['gaps'])} gaps")
    if pdf_engine:
        log(f"  PDF via {pdf_engine}: {pages} page(s)")
    elif "pdf" in formats:
        log("  PDF skipped: install LibreOffice or Chrome (DOCX and HTML are written)")
    log(f"  wrote {out}")
    if args.copy_to:
        dest = Path(args.copy_to)
        dest.mkdir(parents=True, exist_ok=True)
        for p in out.glob(base + ".*"):
            if p.suffix in (".pdf", ".docx"):
                shutil.copy2(p, dest / p.name)
    return {"application": app, "jd_diff": buckets, "changes": list(dict.fromkeys(plan.changes)),
            "pages": pages, "pdf_engine": pdf_engine, "out_dir": str(out), "base": base,
            "recon": rec_out, "dry_run": bool(args.dry_run)}
