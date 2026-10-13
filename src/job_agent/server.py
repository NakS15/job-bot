from mcp.server.fastmcp import FastMCP

from . import applier, db, resume, sheet
from .config import load_profile

mcp = FastMCP("job-agent")


@mcp.tool()
def get_search_preferences() -> dict:
    """Return target titles, locations, min match score and search limits from profile.yaml."""
    return load_profile().get("preferences", {})


@mcp.tool()
def import_base_resume(pdf_path: str) -> str:
    """Store the base resume from a PDF path. Returns the extracted text."""
    return resume.import_resume(pdf_path)


@mcp.tool()
def get_base_resume() -> str:
    """Return the stored base resume text (the source of truth for tailoring)."""
    return resume.base_resume_text()


@mcp.tool()
def seen_job_ids() -> list[str]:
    """Job IDs already evaluated, so they can be skipped."""
    return [j["job_id"] for j in db.list_jobs()]


@mcp.tool()
def record_job(
    job_id: str,
    title: str,
    company: str,
    url: str,
    match_score: float,
    keywords: list[str],
    location: str = "",
    apply_type: str = "",
    description: str = "",
) -> dict:
    """Save a matched job. match_score is 0-100 vs the base resume; keywords are the
    job's key required skills/terms (used to compute keyword coverage of the resumes)."""
    cov, missing = resume.coverage(resume.base_resume_text(), keywords)
    db.upsert_job(
        job_id, title=title, company=company, url=url, location=location,
        apply_type=apply_type, description=description, keywords=keywords,
        match_score=match_score, original_coverage=cov, missing=missing,
        status="matched",
    )
    return {"original_keyword_coverage": cov, "missing_keywords": missing}


@mcp.tool()
def save_tailored_resume(job_id: str, resume_markdown: str, changes_summary: str) -> dict:
    """Render a tailored resume (markdown: '# Name', '## Section', '- bullet', **bold**) to PDF
    for a recorded job. Must only contain truthful content from the base resume."""
    job = db.get_job(job_id)
    if not job:
        raise ValueError(f"Unknown job_id {job_id}; call record_job first.")
    pdf = resume.render_pdf(resume_markdown, job_id)
    cov, missing = resume.coverage(resume_markdown, job["keywords"])
    db.upsert_job(
        job_id, tailored_pdf=str(pdf), tailored_coverage=cov, missing=missing,
        changes=changes_summary, status="tailored",
    )
    return {"pdf": str(pdf), "tailored_keyword_coverage": cov, "missing_keywords": missing}


@mcp.tool()
def list_jobs(status: str | None = None) -> list[dict]:
    """List tracked jobs (without descriptions), optionally filtered by status."""
    return [{k: v for k, v in j.items() if k != "description"} for j in db.list_jobs(status)]


@mcp.tool()
def build_spreadsheet() -> str:
    """Write/refresh the review spreadsheet. The user's Approve (YES/NO) edits are preserved."""
    return sheet.build_sheet()


@mcp.tool()
async def apply_approved(dry_run: bool = True) -> list[dict]:
    """Apply via LinkedIn Easy Apply to rows the USER marked Approve=YES in the spreadsheet.
    Only call when the user explicitly asks. Use dry_run=True first (does not submit)."""
    return await applier.apply_approved(dry_run=dry_run)


def serve() -> None:
    mcp.run()
