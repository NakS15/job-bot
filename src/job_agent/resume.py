import re
import shutil
from pathlib import Path
from xml.sax.saxutils import escape

from pypdf import PdfReader
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer

from .config import BASE_RESUME_PDF, BASE_RESUME_TXT, TAILORED_DIR, ensure_dirs


def import_resume(pdf_path: str) -> str:
    ensure_dirs()
    src = Path(pdf_path).expanduser()
    reader = PdfReader(str(src))
    text = "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    if not text:
        raise ValueError("No text could be extracted from the PDF (scanned image?).")
    shutil.copyfile(src, BASE_RESUME_PDF)
    BASE_RESUME_TXT.write_text(text)
    return text


def base_resume_text() -> str:
    if not BASE_RESUME_TXT.exists():
        raise FileNotFoundError("No base resume stored. Run import_base_resume first.")
    return BASE_RESUME_TXT.read_text()


def coverage(text: str, keywords: list[str]) -> tuple[float, list[str]]:
    """Percent of keywords found in text, plus the missing ones."""
    if not keywords:
        return 0.0, []
    low = text.lower()
    missing = [
        k for k in keywords
        if not re.search(r"(?<![\w+#])" + re.escape(k.lower()) + r"(?![\w+#])", low)
    ]
    return round(100 * (len(keywords) - len(missing)) / len(keywords), 1), missing


def _inline(s: str) -> str:
    s = escape(s)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)


def render_pdf(markdown: str, job_id: str) -> Path:
    """Render a simple markdown resume (#, ##, -, **bold**) to PDF."""
    ensure_dirs()
    out = TAILORED_DIR / f"resume_{re.sub(r'[^A-Za-z0-9_-]', '_', job_id)}.pdf"
    ss = getSampleStyleSheet()
    name = ParagraphStyle("name", parent=ss["Title"], fontSize=18, spaceAfter=4)
    h = ParagraphStyle("h", parent=ss["Heading2"], fontSize=12, spaceBefore=8, spaceAfter=3)
    body = ParagraphStyle("b", parent=ss["BodyText"], fontSize=10, leading=13)

    story, bullets = [], []

    def flush():
        nonlocal bullets
        if bullets:
            story.append(ListFlowable(
                [ListItem(Paragraph(_inline(b), body)) for b in bullets],
                bulletType="bullet", leftIndent=14,
            ))
            bullets = []

    for line in markdown.splitlines():
        line = line.rstrip()
        if line.startswith(("- ", "* ")):
            bullets.append(line[2:])
            continue
        flush()
        if not line.strip():
            story.append(Spacer(1, 3))
        elif line.startswith("# "):
            story.append(Paragraph(_inline(line[2:]), name))
        elif line.startswith("#"):
            story.append(Paragraph(_inline(line.lstrip("# ")), h))
        else:
            story.append(Paragraph(_inline(line), body))
    flush()

    SimpleDocTemplate(
        str(out), pagesize=LETTER,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
    ).build(story)
    return out
