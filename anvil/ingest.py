"""`anvil ingest`: parse Swastik's real CV files into vault/gene-pool.json.

Supported inputs: .md, .txt, .docx, .pdf. Text is kept verbatim (only list
markers and Markdown markup are stripped). Every gene is checked by the
guardrails at ingest; a gene that breaks a rule is kept but marked blocked,
so it can never be selected until vault/overrides.json unblocks it.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from . import guards, store
from .taxonomy import tag_clusters

SECTION_PATTERNS = [
    ("summary", r"(professional\s+)?summary|profile|about( me)?|career objective|objective"),
    ("experience", r"(work\s+|professional\s+)?experience|employment( history)?"),
    ("projects", r"(ai\s+|personal\s+|side\s+|key\s+)?projects"),
    ("education", r"education|academics?"),
    ("skills", r"skills( and certifications| & certifications)?|technical skills|certifications|core skills"),
]
DATE_RE = re.compile(r"((jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{4}|present|\b(19|20)\d{2}\b)", re.I)
BULLET_RE = re.compile(r"^\s*([-*•▪●◦·]|\d+[.)])\s+")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
SENTENCE_END_RE = re.compile(r"[.!?…]$")
SKILL_LABEL_RE = re.compile(r"^[A-Za-z][\w &/()+,-]{0,40}:\s")
EMPTY_SKELETON = {"experience": []}


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def gene_id(kind: str, text: str) -> str:
    return "g_" + hashlib.sha1(f"{kind}|{norm(text)}".encode()).hexdigest()[:10]


def angle_from_name(name: str) -> str:
    n = name.lower()
    for key, cluster in [("consult", "consulting"), ("commodity", "commodity"), ("ctrm", "commodity"),
                         ("implementation", "implementation"), ("revops", "revops_ops"), ("ops", "revops_ops"),
                         ("business", "ba_pa"), ("product_analyst", "ba_pa"), ("product-analyst", "ba_pa"),
                         ("ba_", "ba_pa")]:
        if key in n:
            return cluster
    return "general"


# ---------------------------------------------------------------- readers

def _company_hit(line: str, skeleton: dict) -> bool:
    """True if the line names one of the locked experience companies (their `match` names)."""
    for exp in skeleton.get("experience", []):
        for m in exp["match"]:
            if re.search(r"(?<![A-Za-z])" + re.escape(m) + r"(?![A-Za-z])", line):
                return True
    return False


def _role_like(line: str, skeleton: dict) -> bool:
    """Strict role-header test used where bullet status is unknown (docx, pdf joins)."""
    if SENTENCE_END_RE.search(line.strip()):
        return False
    has_date_or_dash = bool(DATE_RE.search(line) or re.search(r"–| - |\|", line))
    return has_date_or_dash and (_company_hit(line, skeleton) or "|" in line)


def _has_numbering(p) -> bool:
    if p._p.pPr is not None and p._p.pPr.numPr is not None:
        return True
    st = p.style
    while st is not None:
        spPr = st.element.pPr
        if spPr is not None and spPr.numPr is not None:
            return True
        st = st.base_style
    return False


def _join_wrapped(raw: list[str], skeleton: dict) -> list[str]:
    """Undo pypdf's wrapped lines: continuation text joins the previous bullet or skill group."""
    out: list[str] = []
    section = None
    for r in raw:
        line = re.sub(r"\s+", " ", r).strip()
        if not line:
            continue
        sec = section_of(line)
        if sec:
            section = sec
            out.append(line)
            continue
        prev = out[-1] if out else None
        if prev is not None and not BULLET_RE.match(line) and not SKILL_LABEL_RE.match(line) \
                and not _role_like(line, skeleton):
            if section in ("experience", "projects") and BULLET_RE.match(prev) and not SENTENCE_END_RE.search(prev):
                out[-1] = prev + " " + line
                continue
            if section == "skills" and ":" in prev and ":" not in line:
                out[-1] = prev + " " + line
                continue
        out.append(line)
    return out


def read_lines(path: Path, skeleton: dict | None = None) -> list[str]:
    skeleton = skeleton or EMPTY_SKELETON
    ext = path.suffix.lower()
    if ext in (".md", ".txt"):
        raw = path.read_text(encoding="utf-8")
        raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
        lines = []
        for ln in raw.splitlines():
            ln = MD_LINK_RE.sub(lambda m: m.group(1), ln)
            ln = re.sub(r"^#{1,6}\s*", "", ln)
            ln = ln.replace("**", "").replace("__", "")
            lines.append(ln.rstrip())
        return lines
    if ext == ".docx":
        import docx  # python-docx
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        d = docx.Document(str(path))
        lines: list[str] = []

        def add_para(p) -> None:
            text = p.text.strip()
            if not text:
                return
            if _role_like(text, skeleton):  # a role header is never a bullet
                lines.append(BULLET_RE.sub("", text).strip())
                return
            is_list = _has_numbering(p) or bool(BULLET_RE.match(text))
            lines.append(("- " + text) if is_list and not BULLET_RE.match(text) else text)

        # Walk the body in document order, paragraphs and tables interleaved.
        for child in d.element.body.iterchildren():
            if child.tag == qn("w:p"):
                add_para(Paragraph(child, d))
            elif child.tag == qn("w:tbl"):  # some templates put the header in a table
                for row in Table(child, d).rows:
                    seen: list = []
                    for cell in row.cells:
                        if any(cell._tc is s for s in seen):  # merged cells repeat
                            continue
                        seen.append(cell._tc)
                        for p in cell.paragraphs:
                            add_para(p)
        return lines
    if ext == ".pdf":
        from pypdf import PdfReader
        raw: list[str] = []
        for page in PdfReader(str(path)).pages:
            raw.extend((page.extract_text() or "").splitlines())
        return _join_wrapped(raw, skeleton)
    raise ValueError(f"unsupported file type: {path.name}")


# ---------------------------------------------------------------- parser

def section_of(line: str) -> str | None:
    s = line.strip().strip(":").strip()
    if len(s) > 40:
        return None
    for key, pat in SECTION_PATTERNS:
        if re.fullmatch(pat, s, flags=re.I):
            return key
    return None


def parse_cv(lines: list[str], skeleton: dict) -> dict:
    cv = {"headline": None, "summary": [], "experience": {}, "projects": [], "skills": [],
          "section_order": [], "unknown_roles": []}
    section = "header"
    role_key = None
    project = None
    name_seen = False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        sec = section_of(line)
        if sec:
            section = sec
            if sec not in cv["section_order"]:
                cv["section_order"].append(sec)
            role_key, project = None, None
            continue
        is_bullet = bool(BULLET_RE.match(line))
        text = BULLET_RE.sub("", line).strip()
        if section == "header":
            if not name_seen:
                name_seen = True
                continue
            if "@" in line or re.search(r"\+?\d[\d\s]{8,}", line):
                continue  # contact line: locked, never taken from CVs
            if cv["headline"] is None:  # the first non-contact line is the headline
                cv["headline"] = text
        elif section == "summary":
            cv["summary"].append(text)
        elif section == "experience":
            if not is_bullet and (_company_hit(line, skeleton)
                                  or (DATE_RE.search(line) or "–" in line or " - " in line) and "|" in line):
                role_key = None
                for exp in skeleton["experience"]:
                    if any(re.search(r"(?<![A-Za-z])" + re.escape(m) + r"(?![A-Za-z])", line) for m in exp["match"]):
                        role_key = exp["key"]
                        break
                if role_key is None:
                    cv["unknown_roles"].append(line)
                    role_key = "unknown:" + line
                cv["experience"].setdefault(role_key, [])
            elif role_key:
                cv["experience"][role_key].append(text)
        elif section == "projects":
            if not is_bullet and "|" in line and re.search(r"\b(19|20)\d{2}\b", line):
                title, _, year = line.partition("|")
                project = {"title": title.strip(), "year": year.strip(), "bullets": []}
                cv["projects"].append(project)
            elif project is not None:
                project["bullets"].append(text)
        elif section == "skills":
            if ":" in text:
                name, _, items = text.partition(":")
                cv["skills"].append({"group": name.strip(), "items": items.strip()})
    cv["summary"] = " ".join(cv["summary"]).strip()
    return cv


# ---------------------------------------------------------------- gene pool

def _known_vocab(texts: list[str], skeleton: dict) -> tuple[set[str], set[str]]:
    import json
    sk_text = json.dumps({k: v for k, v in skeleton.items() if k not in ("banned_phrases", "banned_chars")},
                         ensure_ascii=False)
    blob = "\n".join(texts) + "\n" + sk_text
    return guards.cap_tokens(blob), guards.numbers(blob)


def build_pool(paths: list[Path], skeleton: dict, overrides: dict | None = None) -> dict:
    overrides = overrides or {}
    genes: dict[str, dict] = {}
    layouts = []
    all_text = []
    warnings = []

    def add(kind: str, text: str, src: str, pos: int, section: str, angle: str, meta: dict | None = None):
        key_text = text if kind != "project" else meta["title"] + " | " + " ".join(meta["bullets"])
        gid = gene_id(kind + ":" + section, key_text)
        g = genes.get(gid)
        if not g:
            tag_text = key_text if kind != "skill_group" else meta["group"] + " " + meta["items"]
            g = genes[gid] = {
                "id": gid, "kind": kind, "section": section, "text": text,
                "clusters": sorted(set(tag_clusters(tag_text)) | ({angle} if kind in ("summary", "headline") else set())),
                "sources": [], "blocked": False, "block_reasons": [],
            }
            if meta:
                g["meta"] = meta
        else:
            if kind in ("summary", "headline") and angle not in g["clusters"]:
                g["clusters"] = sorted(set(g["clusters"]) | {angle})
        g["sources"].append({"file": src, "pos": pos})
        all_text.append(key_text)
        return gid

    for path in paths:
        lines = read_lines(path, skeleton)
        cv = parse_cv(lines, skeleton)
        angle = angle_from_name(path.stem)
        src = path.name
        for u in cv["unknown_roles"]:
            warnings.append(f"{src}: role header does not match a locked company, ignored: {u!r}")
        layout = {"file": src, "angle": angle, "section_order": cv["section_order"], "bullets": {}, "skills": [],
                  "summary": None, "headline": None, "projects": []}
        if cv["headline"]:
            layout["headline"] = add("headline", cv["headline"], src, 0, "header", angle)
        if cv["summary"]:
            layout["summary"] = add("summary", cv["summary"], src, 0, "summary", angle)
        else:
            warnings.append(f"{src}: no summary found")
        for key, bullets in cv["experience"].items():
            if key.startswith("unknown:"):
                continue
            layout["bullets"][key] = [add("bullet", b, src, i, key, angle) for i, b in enumerate(bullets)]
        for i, p in enumerate(cv["projects"]):
            layout["projects"].append(add("project", p["title"], src, i, "projects", angle, meta=p))
        for i, s in enumerate(cv["skills"]):
            layout["skills"].append(add("skill_group", f"{s['group']}: {s['items']}", src, i, "skills", angle, meta=s))
        layouts.append(layout)
        if not cv["experience"]:
            warnings.append(f"{src}: no experience bullets parsed")

    known_nouns, known_numbers = _known_vocab(all_text, skeleton)

    # Guardrails at ingest: block anything that breaks a hard rule.
    hard_fail: set[str] = set()
    for gid, g in genes.items():
        # A project's title is checked along with its bullets (it is shown in the CV).
        if g["kind"] == "project":
            body = " ".join([g["meta"]["title"]] + g["meta"]["bullets"])
        else:
            body = g["text"]
        rep = guards.check_style(body, skeleton)
        rep.extend(guards.check_client_scope(body, skeleton))
        if g["kind"] == "headline" and skeleton["title"].lower() not in body.lower():
            # Headlines are only used as a tagline after the locked title, so this is a note, not a block.
            g["note"] = "headline lacks the locked title; rendered after 'Product Analyst' as a tagline"
        if not rep.ok:
            hard_fail.add(gid)
            g["blocked"] = True
            g["block_reasons"] = [str(v) for v in rep.violations]
    for gid in overrides.get("unblock", []):
        if gid not in genes:
            continue
        if gid in hard_fail:
            # Style and client-scope rules cannot be overridden by a config file.
            genes[gid]["block_reasons"].append("unblock refused by vault/overrides.json: fails style/client-scope rule")
            continue
        genes[gid]["blocked"] = False
        genes[gid]["block_reasons"].append("unblocked by vault/overrides.json")
    for gid in overrides.get("block", []):
        if gid in genes:
            genes[gid]["blocked"] = True
            genes[gid]["block_reasons"].append("blocked by vault/overrides.json")

    return {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": [p.name for p in paths],
        "genes": genes,
        "layouts": layouts,
        "known_nouns": sorted(known_nouns),
        "known_numbers": sorted(known_numbers),
        "warnings": warnings,
    }


def run(paths: list[str] | None = None) -> dict:
    skeleton = store.skeleton()
    src_dir = store.vault() / "sources"
    files: list[Path] = []
    for p in (paths or [str(src_dir)]):
        pp = Path(p)
        if pp.is_dir():
            files.extend(sorted(f for f in pp.iterdir() if f.suffix.lower() in (".md", ".txt", ".docx", ".pdf")
                                and not f.name.startswith(("00-", "ALL-", "."))))
        elif pp.exists():
            files.append(pp)
    if not files:
        raise SystemExit(f"No CV files found. Put .docx/.pdf/.md CVs in {src_dir}")
    overrides = store.load(store.vault() / "overrides.json", {}) or {}
    pool = build_pool(files, skeleton, overrides)
    store.save(store.vault() / "gene-pool.json", pool)
    return pool
