"""`anvil tailor`: one lead in, one guarded one-page CV + JD diff + recon out."""
from __future__ import annotations

import hashlib
import random
import shutil
from pathlib import Path

from . import bandit, diff, engine, ember, guards, ingest, recon, render, store
from .taxonomy import classify, cluster_scores


def load_pool() -> dict:
    pool = store.load(store.vault() / "gene-pool.json")
    if not pool:
        print("No gene pool yet, running ingest first.")
        pool = ingest.run()
    return pool


def resolve_lead(canonical_id: str | None, args) -> dict:
    if args.jd_file and (args.company or not canonical_id):
        company = args.company or "Company"
        cid = canonical_id or f"manual:{store.file_company(company).lower()}:" + hashlib.sha1(
            Path(args.jd_file).read_bytes()).hexdigest()[:8]
        return {"canonical_id": cid, "company": company, "role": args.role or "", "url": args.url or "",
                "verdict": "MANUAL"}
    leads = ember.load_leads(args.leads_file)
    lead = ember.find_lead(leads, canonical_id)
    if not lead:
        raise SystemExit(f"{canonical_id} is not in open-leads.csv. Use `anvil leads` to list ids, "
                         "or pass --jd-file --company --role for a job that is not on the board.")
    if (lead.get("verdict") or "").upper() not in ember.ACTIONABLE and not args.force:
        raise SystemExit(f"{canonical_id} has verdict {lead.get('verdict')}; Anvil only tailors "
                         f"{', '.join(ember.ACTIONABLE)}. Use --force to override.")
    if args.role:
        lead["role"] = args.role
    return lead


def run(canonical_id: str | None, args) -> dict:
    skeleton = store.skeleton()
    pool = load_pool()
    swaps_cfg = store.load(store.vault() / "swaps.json", {}) or {}
    ghosted = bandit.ghost_sweep()
    if ghosted:
        print(f"Auto-ghosted (no reply in {bandit.GHOST_DAYS} days): {', '.join(ghosted)}")

    lead = resolve_lead(canonical_id, args)
    cid, company, role = lead["canonical_id"], lead.get("company", ""), lead.get("role", "")
    print(f"Tailoring for {role} at {company} ({cid})")
    rec = recon.gather(lead, jd_file=args.jd_file, company_url=args.company_url, refresh=args.refresh)
    jd = rec["jd"]
    notes = recon.notes_for(cid, company, args.notes)
    query = "\n".join([role, role, jd, notes])  # role title counts extra
    extra = " ".join(rec.get("product_terms", [])) + "\n" + rec.get("company_text", "")[:2500]

    cluster = args.cluster or classify(jd + "\n" + notes, role)
    rel = engine.relevance(pool, query, extra)
    log = bandit.load_log()
    rng = random.Random(args.seed)
    golden = None
    if args.golden:
        g = log["golden"].get(cluster, [])
        golden = g[-1] if g else None
        if not golden:
            print(f"No golden variant for {cluster} yet; sampling normally.")

    plan = engine.choose(pool, skeleton, cluster, rel, log, rng, golden=golden)
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
    for attempt in range(6):
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
        pdf_engine = render.to_pdf(docx_p, html_p, pdf_p)
        pages = render.page_count(pdf_p) if pdf_engine else None
        if not pages or pages <= 1:
            break
        print(f"  {pages} pages, trimming one item")
        if not engine.drop_one(plan, mins):
            print("  WARNING: cannot trim further; CV is longer than one page")
            break
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
        "cv_files": [str(p.relative_to(store.home())) for p in sorted(out.glob(base + ".*"))],
    })
    full = store.load(recon.recon_path(cid)) or {}
    full.update(rec_out)
    store.save(recon.recon_path(cid), full)
    store.save(out / f"{store.safe_id(cid)}-recon.json", rec_out)

    app = {"canonical_id": cid, "company": company, "role": role, "cluster": cluster, "dna_hash": dna,
           "hook_id": hook, "genes_used": genes_used, "recipe": recipe, "applied_at": bandit.now(),
           "outcome": "PENDING", "out_dir": str(out.relative_to(store.home()))}
    if args.dry_run:
        print("Dry run: not logged to state/applications.json")
    else:
        bandit.record_application(app)

    print(f"  cluster: {cluster}   dna: {dna}   hook: {hook}")
    print(f"  summary from: {pool['genes'][recipe['summary']]['sources'][0]['file']}")
    for c in dict.fromkeys(plan.changes):
        print(f"  {c}")
    print(f"  JD diff: {len(buckets['exact'])} exact, {len(buckets['implied'])} implied, {len(buckets['gaps'])} gaps")
    if pdf_engine:
        print(f"  PDF via {pdf_engine}: {pages} page(s)")
    elif "pdf" in formats:
        print("  PDF skipped: install LibreOffice or Chrome (DOCX and HTML are written)")
    print(f"  wrote {out}")
    if args.copy_to:
        dest = Path(args.copy_to)
        dest.mkdir(parents=True, exist_ok=True)
        for p in out.glob(base + ".*"):
            if p.suffix in (".pdf", ".docx"):
                shutil.copy2(p, dest / p.name)
    return app
