"""Render a plan to an ATS-clean one-column CV: DOCX (python-docx), HTML, and PDF.

PDF comes from LibreOffice (docx -> pdf) when installed, else headless Chromium
(html -> pdf). ATS-clean = one column, no tables, no images, real text, standard
section names.
"""
from __future__ import annotations

import html
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .engine import headline_text

SECTION_TITLES = {"summary": "PROFESSIONAL SUMMARY", "experience": "EXPERIENCE", "projects": "AI PROJECTS",
                  "education": "EDUCATION", "skills": "SKILLS AND CERTIFICATIONS"}


def build_doc(pool: dict, skeleton: dict, plan) -> list[tuple]:
    """Flat list of (kind, payload) blocks, shared by every renderer and the final guard."""
    g = pool["genes"]
    r = plan.recipe
    t = lambda gid: plan.texts.get(gid, g[gid]["text"])  # noqa: E731
    c = skeleton["contact"]
    blocks = [("name", skeleton["name"]),
              ("headline", headline_text(pool, skeleton, r)),
              ("contact", [c["location"], c["phone"], c["email"], (c["linkedin"], c.get("linkedin_url"))])]
    for sec in r["section_order"]:
        if sec == "projects" and not r["projects"]:
            continue
        blocks.append(("section", SECTION_TITLES[sec]))
        if sec == "summary":
            blocks.append(("para", t(r["summary"])))
        elif sec == "experience":
            for exp in skeleton["experience"]:
                ids = r["bullets"].get(exp["key"], [])
                if not ids:
                    continue
                where = f", {exp['location']}" if exp.get("location") else ""
                blocks.append(("role", f"{exp['title']} – {exp['company']}{where}", exp["dates"]))
                blocks += [("bullet", t(i)) for i in ids]
        elif sec == "projects":
            for pid in r["projects"]:
                m = g[pid]["meta"]
                blocks.append(("role", m["title"], m["year"]))
                blocks += [("bullet", b) for b in m["bullets"]]
        elif sec == "education":
            for e in skeleton["education"]:
                blocks.append(("role", e["degree"], e["dates"]))
                blocks.append(("line", e["school"]))
                if e.get("detail"):
                    blocks.append(("line", e["detail"]))
        elif sec == "skills":
            for sid in r["skills"]:
                group, _, items = t(sid).partition(":")
                blocks.append(("skill", group.strip(), items.strip()))
    return blocks


def doc_lines(blocks: list[tuple]) -> list[str]:
    out = []
    for b in blocks:
        if b[0] == "contact":
            out.append(" | ".join(x[0] if isinstance(x, tuple) else x for x in b[1]))
        elif b[0] in ("role",):
            out.append(f"{b[1]} | {b[2]}")
        elif b[0] == "skill":
            out.append(f"{b[1]}: {b[2]}")
        else:
            out.append(b[1])
    return out


def to_text(blocks: list[tuple]) -> str:
    return "\n".join(doc_lines(blocks)) + "\n"


# ---------------------------------------------------------------- HTML

CSS = """
@page { size: A4; margin: 10mm 12mm; }
body { font-family: Calibri, Carlito, Arial, sans-serif; font-size: 10pt; line-height: 1.22; color: #111;
       max-width: 182mm; margin: 0 auto; }
h1 { font-size: 17pt; margin: 0; letter-spacing: .5px; }
.headline { font-weight: 600; margin: 1px 0; }
.contact { margin: 0 0 4px; }
h2 { font-size: 10.5pt; border-bottom: 1px solid #444; margin: 7px 0 3px; padding-bottom: 1px; letter-spacing: .4px; }
.role { display: flex; justify-content: space-between; font-weight: 700; margin-top: 4px; gap: 8px; }
ul { margin: 1px 0 2px 16px; padding: 0; }
li { margin: 0 0 1px; }
p { margin: 1px 0; }
a { color: inherit; text-decoration: none; }
"""


def to_html(blocks: list[tuple], title: str) -> str:
    e = html.escape
    out = ["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
           f"<title>{e(title)}</title><style>{CSS}</style></head><body>"]
    in_list = False
    for b in blocks:
        if b[0] != "bullet" and in_list:
            out.append("</ul>")
            in_list = False
        if b[0] == "name":
            out.append(f"<h1>{e(b[1])}</h1>")
        elif b[0] == "headline":
            out.append(f"<div class='headline'>{e(b[1])}</div>")
        elif b[0] == "contact":
            parts = []
            for x in b[1]:
                if isinstance(x, tuple):
                    parts.append(f"<a href='{e(x[1] or 'https://' + x[0])}'>{e(x[0])}</a>")
                elif "@" in x:
                    parts.append(f"<a href='mailto:{e(x)}'>{e(x)}</a>")
                else:
                    parts.append(e(x))
            out.append("<div class='contact'>" + " | ".join(parts) + "</div>")
        elif b[0] == "section":
            out.append(f"<h2>{e(b[1])}</h2>")
        elif b[0] == "role":
            out.append(f"<div class='role'><span>{e(b[1])}</span><span>{e(b[2])}</span></div>")
        elif b[0] == "bullet":
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{e(b[1])}</li>")
        elif b[0] in ("para", "line"):
            out.append(f"<p>{e(b[1])}</p>")
        elif b[0] == "skill":
            out.append(f"<p><b>{e(b[1])}:</b> {e(b[2])}</p>")
    if in_list:
        out.append("</ul>")
    out.append("</body></html>")
    return "\n".join(out)


# ---------------------------------------------------------------- DOCX

def to_docx(blocks: list[tuple], path: Path) -> None:
    import docx
    from docx.enum.text import WD_TAB_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt

    d = docx.Document()
    sec = d.sections[0]
    sec.page_height, sec.page_width = Mm(297), Mm(210)
    sec.left_margin = sec.right_margin = Mm(12)
    sec.top_margin = sec.bottom_margin = Mm(10)
    usable = sec.page_width - sec.left_margin - sec.right_margin
    st = d.styles["Normal"]
    st.font.name = "Calibri"
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
    st.font.size = Pt(10)
    st.paragraph_format.space_after = Pt(0)
    st.paragraph_format.space_before = Pt(0)
    st.paragraph_format.line_spacing = 1.0

    def para(text="", bold=False, size=None, before=0, after=0):
        p = d.add_paragraph()
        p.paragraph_format.space_before = Pt(before)
        p.paragraph_format.space_after = Pt(after)
        if text:
            run = p.add_run(text)
            run.bold = bold
            if size:
                run.font.size = Pt(size)
        return p

    def rule(p):
        pPr = p._p.get_or_add_pPr()
        bdr = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        for k, v in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"), ("w:color", "444444")):
            bottom.set(qn(k), v)
        bdr.append(bottom)
        pPr.append(bdr)

    for b in blocks:
        kind = b[0]
        if kind == "name":
            para(b[1], bold=True, size=17)
        elif kind == "headline":
            para(b[1], bold=True)
        elif kind == "contact":
            items = [x[0] if isinstance(x, tuple) else x for x in b[1]]
            para(" | ".join(items), after=2)
        elif kind == "section":
            rule(para(b[1], bold=True, before=5, after=1))
        elif kind == "role":
            p = para(before=3)
            p.paragraph_format.tab_stops.add_tab_stop(usable, WD_TAB_ALIGNMENT.RIGHT)
            p.add_run(b[1]).bold = True
            p.add_run("\t" + b[2]).bold = True
        elif kind == "bullet":
            p = d.add_paragraph(style="List Bullet")
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.left_indent = Mm(5)
            p.add_run(b[1])
        elif kind in ("para", "line"):
            para(b[1])
        elif kind == "skill":
            p = para(after=1)
            p.add_run(b[1] + ": ").bold = True
            p.add_run(b[2])
    d.core_properties.author = "Swastik Kumar Ray"
    d.core_properties.title = "Resume"
    d.save(str(path))


# ---------------------------------------------------------------- PDF

def _chromium() -> str | None:
    for c in (os.environ.get("ANVIL_CHROMIUM"), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
              shutil.which("chromium"), shutil.which("chromium-browser"), shutil.which("google-chrome"),
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        if c and Path(c).exists():
            return c
    return None


FONT_DIR = Path(__file__).resolve().parent / "fonts"


def to_pdf_fpdf(blocks: list[tuple], pdf_path: Path) -> int:
    """Pure-Python PDF (fpdf2 + bundled Carlito). Same output on PC, phone-hosted API, anywhere.
    Returns the page count."""
    from fpdf import FPDF

    pdf = FPDF(format="A4", unit="mm")
    pdf.set_margins(12, 10, 12)
    pdf.set_auto_page_break(True, margin=10)
    pdf.add_font("Carlito", "", str(FONT_DIR / "Carlito-Regular.ttf"))
    pdf.add_font("Carlito", "B", str(FONT_DIR / "Carlito-Bold.ttf"))
    pdf.set_title("Resume")
    pdf.set_author("Swastik Kumar Ray")
    pdf.add_page()
    W = pdf.w - pdf.l_margin - pdf.r_margin
    LH = 4.15

    def font(style="", size=10):
        pdf.set_font("Carlito", style, size)

    for b in blocks:
        kind = b[0]
        if kind == "name":
            font("B", 17)
            pdf.cell(W, 7, b[1], new_x="LMARGIN", new_y="NEXT")
        elif kind == "headline":
            font("B", 10)
            pdf.cell(W, LH, b[1], new_x="LMARGIN", new_y="NEXT")
        elif kind == "contact":
            font("", 10)
            for i, x in enumerate(b[1]):
                sep = " | " if i < len(b[1]) - 1 else ""
                if isinstance(x, tuple):
                    pdf.write(LH, x[0], link=x[1] or "https://" + x[0])
                elif "@" in x:
                    pdf.write(LH, x, link="mailto:" + x)
                else:
                    pdf.write(LH, x)
                pdf.write(LH, sep)
            pdf.ln(LH + 0.6)
        elif kind == "section":
            pdf.ln(1.6)
            font("B", 10.5)
            pdf.cell(W, 4.6, b[1], new_x="LMARGIN", new_y="NEXT")
            y = pdf.get_y()
            pdf.set_line_width(0.25)
            pdf.line(pdf.l_margin, y, pdf.l_margin + W, y)
            pdf.ln(0.8)
        elif kind == "role":
            pdf.ln(0.6)
            font("B", 10)
            dw = min(pdf.get_string_width(b[2]) + 1, W * 0.4)
            y = pdf.get_y()
            pdf.multi_cell(W - dw - 2, LH, b[1], align="L", new_x="LMARGIN", new_y="NEXT")
            y2 = pdf.get_y()
            pdf.set_xy(pdf.l_margin + W - dw, y)
            pdf.cell(dw, LH, b[2], align="R")
            pdf.set_xy(pdf.l_margin, y2)
        elif kind == "bullet":
            font("", 10)
            pdf.set_x(pdf.l_margin + 1.5)
            pdf.cell(4, LH, "•")
            pdf.multi_cell(W - 5.5, LH, b[1], align="L", new_x="LMARGIN", new_y="NEXT")
        elif kind in ("para", "line"):
            font("", 10)
            pdf.multi_cell(W, LH, b[1], align="L", new_x="LMARGIN", new_y="NEXT")
        elif kind == "skill":
            font("", 10)
            font("B", 10)
            pdf.write(LH, f"{b[1]}: ")
            font("", 10)
            pdf.write(LH, b[2])
            pdf.ln(LH)
    pdf.output(str(pdf_path))
    return pdf.pages_count


def to_pdf(docx_path: Path, html_path: Path, pdf_path: Path, blocks: list[tuple] | None = None) -> str | None:
    """Returns the engine used, or None if no PDF engine is available.
    ANVIL_PDF_ENGINE = fpdf (default, works everywhere) | libreoffice | chromium."""
    engine = os.environ.get("ANVIL_PDF_ENGINE", "fpdf").lower()
    if engine == "fpdf" and blocks is not None:
        try:
            to_pdf_fpdf(blocks, pdf_path)
            return "fpdf"
        except Exception as e:  # noqa: BLE001 - fall back to another engine
            print(f"fpdf failed ({type(e).__name__}: {e}); trying another PDF engine")
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice and docx_path.exists():
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(docx_path)],
                               capture_output=True, timeout=120)
            produced = Path(tmp) / (docx_path.stem + ".pdf")
            if r.returncode == 0 and produced.exists():
                shutil.move(str(produced), pdf_path)
                return "libreoffice"
    chrome = _chromium()
    if chrome and html_path.exists():
        pdf_path.unlink(missing_ok=True)
        r = subprocess.run([chrome, "--headless", "--disable-gpu", "--no-sandbox", "--no-pdf-header-footer",
                            f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()],
                           capture_output=True, timeout=120)
        if r.returncode == 0 and pdf_path.exists():
            return "chromium"
    return None


def page_count(pdf_path: Path) -> int | None:
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(pdf_path)).pages)
    except Exception:
        return None
