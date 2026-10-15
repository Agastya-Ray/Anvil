"""anvil: evolutionary resume bandit and recon engine.

  anvil ingest [files or folders]          build vault/gene-pool.json from real CVs
  anvil leads [--all]                      list actionable leads from Ember's open-leads.csv
  anvil tailor <canonical_id> [...]        build CV + JD diff + recon for one lead
  anvil feedback <canonical_id> --outcome callback|interview|offer|rejected|ghosted
  anvil ghost-sweep [--days 14]            mark silent applications as ghosted
  anvil stats                              gene weights, hooks, golden variants
  anvil check <file>                       run the guardrails on any CV text (e.g. Gemini output)
  anvil serve [--host 0.0.0.0 --port 8787] web page + REST + MCP (same API as the hosted one)
  anvil mcp                                MCP over stdio for Claude Code / Desktop / any MCP client
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import bandit, ember, guards, ingest, store


def cmd_ingest(a):
    pool = ingest.run(a.paths or None)
    g = pool["genes"].values()
    kinds = {}
    for x in g:
        kinds[x["kind"]] = kinds.get(x["kind"], 0) + 1
    print(f"Gene pool: {len(pool['genes'])} genes from {len(pool['sources'])} CVs: "
          + ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())))
    for x in g:
        if x["blocked"]:
            print(f"  BLOCKED {x['id']} ({x['kind']}): {'; '.join(x['block_reasons'])}")
    for w in pool["warnings"]:
        print(f"  warning: {w}")


def cmd_leads(a):
    leads = ember.load_leads(a.leads_file)
    rows = leads if a.all else ember.actionable(leads)
    apps = {x["canonical_id"]: x["outcome"] for x in bandit.load_apps()}
    print(f"{len(rows)} lead(s)")
    for r in rows:
        st = apps.get(r.get("canonical_id"), "")
        print(f"{r.get('verdict',''):8} {st:10} {r.get('canonical_id','')}\n         {r.get('role','')} | "
              f"{r.get('company','')} | {r.get('pay','')} | {r.get('url','')}")


def cmd_tailor(a):
    from . import tailor
    ids = a.canonical_ids or [None]
    if a.all_new:
        leads = ember.actionable(ember.load_leads(a.leads_file))
        done = {x["canonical_id"] for x in bandit.load_apps()}
        ids = [r["canonical_id"] for r in leads if r["canonical_id"] not in done][: a.all_new]
    failed = []
    for cid in ids:
        try:
            tailor.run(cid, a)
        except (ember.EmberError, SystemExit) as e:
            if len(ids) == 1:
                raise
            failed.append(cid)
            print(f"  skipped {cid}: {e}")
    if failed:
        print(f"{len(failed)} failed: {', '.join(failed)}")


def cmd_feedback(a):
    app = bandit.apply_outcome(a.canonical_id, a.outcome, a.note or "")
    print(f"{app['canonical_id']}: {app['outcome']} (cluster {app['cluster']}, dna {app['dna_hash']}, "
          f"{len(app['genes_used'])} genes updated)")


def cmd_ghost(a):
    g = bandit.ghost_sweep(a.days)
    print(f"Ghosted {len(g)}: {', '.join(g)}" if g else "Nothing to ghost.")


def cmd_stats(a):
    pool = store.load(store.vault() / "gene-pool.json") or {"genes": {}}
    log = bandit.load_log()
    apps = bandit.load_apps()
    by = {}
    for x in apps:
        by.setdefault(x["cluster"], {}).setdefault(x["outcome"], 0)
        by[x["cluster"]][x["outcome"]] += 1
    print("Applications by cluster:")
    for c, d in sorted(by.items()):
        print(f"  {c:15} " + ", ".join(f"{k} {v}" for k, v in sorted(d.items())))
    for c, arms in sorted(log["genes"].items()):
        moved = sorted(((v["alpha"] / (v["alpha"] + v["beta"]), gid, v) for gid, v in arms.items()
                        if v["alpha"] > 1 or v["beta"] > 1), reverse=True)
        if not moved:
            continue
        print(f"\n[{c}] genes with feedback (mean, alpha/beta):")
        for m, gid, v in moved[: a.top]:
            text = pool["genes"].get(gid, {}).get("text", "?")[:70]
            print(f"  {m:.2f}  {v['alpha']}/{v['beta']}  {gid}  {text}")
    for c, hooks in sorted(log["hooks"].items()):
        dep = [h for h, v in hooks.items() if v["deprecated"]]
        if dep:
            print(f"\n[{c}] deprecated hooks: {', '.join(dep)}")
    for c, gl in sorted(log["golden"].items()):
        print(f"\n[{c}] golden variants: " + ", ".join(f"{r['dna_hash']} ({r['company']}, {r['outcome']})" for r in gl))


def cmd_check(a):
    sk = store.skeleton()
    pool = store.load(store.vault() / "gene-pool.json")
    if not pool:
        pool = ingest.run()
    lines = [ln.strip("-*• #\t") for ln in ingest.read_lines(Path(a.file)) if ln.strip()]
    rep = guards.check_document(lines, sk, set(pool["known_nouns"]), set(pool["known_numbers"]))
    if rep.ok:
        print("OK: no guardrail violations")
        return
    for v in rep.violations:
        print(v)
    sys.exit(1)


def main(argv=None):
    p = argparse.ArgumentParser(prog="anvil", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="parse real CVs into the gene pool")
    s.add_argument("paths", nargs="*")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("leads", help="list actionable leads")
    s.add_argument("--leads-file")
    s.add_argument("--all", action="store_true", help="include every verdict")
    s.set_defaults(fn=cmd_leads)

    s = sub.add_parser("tailor", help="build a tailored CV for a lead")
    s.add_argument("canonical_ids", nargs="*", help="<ats>:<company>:<job_id> from open-leads.csv")
    s.add_argument("--all-new", type=int, metavar="N", help="tailor the first N actionable leads not yet done")
    s.add_argument("--leads-file", help="local open-leads.csv instead of the leads-data branch")
    s.add_argument("--jd-file", help="JD text file (skips scraping the job URL)")
    s.add_argument("--company", help="company name (for a job not on the board)")
    s.add_argument("--role", help="role title override")
    s.add_argument("--url", help="job URL (for a job not on the board)")
    s.add_argument("--company-url", help="company homepage, if the guess is wrong")
    s.add_argument("--notes", help="extra recon, e.g. 2 lines from a hiring manager's post")
    s.add_argument("--cluster", choices=["ba_pa", "commodity", "consulting", "general", "revops_ops", "implementation"])
    s.add_argument("--polish", action="store_true", help="optional ~300-token LLM synonym pass (guarded)")
    s.add_argument("--golden", action="store_true", help="replay the cluster's latest winning recipe")
    s.add_argument("--formats", default="pdf,docx,html")
    s.add_argument("--seed", type=int)
    s.add_argument("--refresh", action="store_true", help="re-scrape even if recon is cached")
    s.add_argument("--dry-run", action="store_true", help="do not log the application")
    s.add_argument("--force", action="store_true", help="allow leads whose verdict is not actionable")
    s.add_argument("--copy-to", help="also copy the PDF/DOCX to this folder")
    s.set_defaults(fn=cmd_tailor)

    s = sub.add_parser("feedback", help="record a company's reply")
    s.add_argument("canonical_id")
    s.add_argument("--outcome", required=True, choices=["callback", "interview", "offer", "rejected", "ghosted"])
    s.add_argument("--note")
    s.set_defaults(fn=cmd_feedback)

    s = sub.add_parser("ghost-sweep", help="mark silent applications as ghosted")
    s.add_argument("--days", type=int, default=bandit.GHOST_DAYS)
    s.set_defaults(fn=cmd_ghost)

    s = sub.add_parser("stats", help="show what the bandit has learned")
    s.add_argument("--top", type=int, default=8)
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("check", help="run guardrails on a CV text/markdown file")
    s.add_argument("file")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("serve", help="run the web page + REST + MCP server locally")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.set_defaults(fn=lambda a: __import__("anvil.server", fromlist=["serve"]).serve(a.host, a.port))

    s = sub.add_parser("mcp", help="MCP server over stdio")
    s.set_defaults(fn=lambda a: __import__("anvil.server", fromlist=["stdio"]).stdio())

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except ember.EmberError as e:
        raise SystemExit(f"Ember: {e}")


if __name__ == "__main__":
    main()
