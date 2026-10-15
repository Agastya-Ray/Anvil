# Anvil

Evolutionary resume bandit and recon engine for Swastik's job hunt. Reads leads from Ember
(`Agastya-Ray/FireAlt`, branch `leads-data`), scrapes the JD and company site through Ember's
`/api/v1/scrape`, and builds a one-page, ATS-clean CV from his **real** CV text only.

Tailoring costs 0 LLM tokens. An optional `--polish` pass (~300 tokens, Groq/Gemini) may only swap
synonyms, and code throws away any line that gains a number or a name.

## Setup

```bash
pip install -r requirements.txt          # python-docx, pypdf
export EMBER_API_KEY=emb_...             # Ember's Connect page
export GROQ_API_KEY=...                  # optional, only for --polish (GEMINI_API_KEY / EMBER_LLM_KEYS also work)
python -m anvil ingest                   # vault/sources/*.docx|.pdf|.md -> vault/gene-pool.json
```

PDF output needs LibreOffice (`soffice`) or Chrome/Chromium. Without either you still get DOCX and HTML.

## Daily use

```bash
python -m anvil leads                                    # PASS / PARK / HIGHPAY leads from Ember
python -m anvil tailor lever:sprinto:56d536c2-...        # one lead
python -m anvil tailor --all-new 5                       # next 5 leads not yet tailored
python -m anvil tailor --jd-file jd.txt --company "Acme" --role "Business Analyst"   # job not on the board
python -m anvil feedback lever:sprinto:56d536c2-... --outcome callback   # or interview / offer / rejected
python -m anvil stats                                    # what the bandit has learned
python -m anvil check gemini-output.md                   # run the guardrails on any CV text
```

Useful `tailor` flags: `--notes "2 lines from the hiring manager's post"`, `--company-url`, `--cluster`,
`--golden` (replay the last CV that got a callback in that cluster), `--polish`, `--dry-run`,
`--copy-to ~/Desktop/CVs`.

Each run writes `out/<canonical_id>/`:

| File | What |
| --- | --- |
| `Swastik_Kumar_Ray_<Company>.pdf/.docx/.html` | The CV (one page, checked) |
| `jd-diff.md` | Honest 3-bucket diff: exact matches, implied safe swaps, true gaps (never added to the CV) |
| `<canonical_id>-recon.json` | Product summary, product terms, pain points, matched hooks, JD diff. Issue 3 (cold email) reads this instead of re-scraping. Full JD is cached in `state/recon/`. |

`:` in a canonical_id becomes `_` in file names so they work on Windows.

## How the bandit works

- **Genes**: every real summary, headline, bullet, project and skill line, deduplicated across CVs and
  tagged with clusters (`ba_pa`, `commodity`, `consulting`, `general`, `revops_ops`, `implementation`).
- **Tailor**: the JD picks a cluster. Each gene's score = 0.55 x BM25 relevance to JD + recon
  + 0.35 x Thompson sample from Beta(alpha, beta) for that cluster + 0.10 if tagged for the cluster.
  Best summary, best bullets per role (in score order), skill groups by score, section order by
  cluster. Lowest-scoring bullets are trimmed until the PDF is one page.
- **Log**: `state/applications.json` gets `{canonical_id, company, role, cluster, dna_hash, genes_used, applied_at, outcome: PENDING}`.
- **Feedback**: callback / interview / offer adds +2 alpha to every gene used and saves the recipe as a
  golden variant. Rejected adds +1 beta. Applications with no reply after 14 days are auto-ghosted
  (+1 beta) on the next run.
- **Mutation**: a hook (summary + headline + first 2 Fendahl bullets) sent 8+ times with 0 replies is
  deprecated for that cluster; the next CV is forced onto a different summary or bullet order.

State lives in `state/`. Commit it so learning survives between machines. Keep this repo **private**:
it holds his contact details and where he applied.

## Guardrails (in code, not prompts)

`vault/skeleton.json` holds locked facts: name, title `Product Analyst`, contact line, company names,
dates, education. These are copied verbatim, never generated.

| Rule | Where |
| --- | --- |
| No em dash, no AI-tell words (spearheaded, leveraged, cutting-edge, ...) | `guards.check_style` |
| Only Petronas and CBH named; Petronas never next to pricing/config (per clause) | `guards.check_client_scope` |
| No new numbers or names vs the source gene (polish + swaps) | `guards.assert_no_new_facts` |
| Every number and capitalised name in the final CV must exist in his real CVs or the skeleton | `guards.check_document` |
| JD gaps (Tableau, SAP, ...) are listed in the diff, never put on the CV | `diff.py` |

A gene that breaks a rule at ingest is kept but **blocked**. Unblock or block by id in
`vault/overrides.json`: `{"unblock": ["g_..."], "block": ["g_..."]}`.

Wording swaps live in `vault/swaps.json` (e.g. "bug reproduction" -> "defect reproduction" when the JD
says defect). BRD/FRD and impact analysis are there but `verified: false`, because no real CV mentions
them. Flip to `true` only if Swastik confirms.

## Adding CV versions

Drop more real `.docx` / `.pdf` / `.md` CVs into `vault/sources/` and run `python -m anvil ingest`.
New phrasings become new genes; existing bandit weights are kept (gene ids are content hashes).

## Tests

```bash
python -m unittest discover tests
```
