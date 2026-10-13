import os
from pathlib import Path

import yaml

HOME = Path(os.environ.get("JOB_AGENT_HOME", Path.home() / ".job-agent"))
DB_PATH = HOME / "jobs.db"
RESUME_DIR = HOME / "resumes"
TAILORED_DIR = RESUME_DIR / "tailored"
BASE_RESUME_TXT = RESUME_DIR / "base.txt"
BASE_RESUME_PDF = RESUME_DIR / "base.pdf"
PROFILE_PATH = HOME / "profile.yaml"


def _sheet_path() -> Path:
    """The review spreadsheet lives in HOME unless profile.yaml sets sheet_path."""
    if PROFILE_PATH.exists():
        custom = (yaml.safe_load(PROFILE_PATH.read_text()) or {}).get("sheet_path")
        if custom:
            return Path(custom).expanduser()
    return HOME / "jobs.xlsx"


SHEET_PATH = _sheet_path()
BROWSER_PROFILE = HOME / "browser-profile"

DEFAULT_PROFILE = {
    "preferences": {
        "titles": ["Software Engineer"],
        "locations": ["Remote"],
        "min_match": 70,
        "max_jobs_per_search": 10,
    },
    "apply": {
        "max_per_run": 10,
        "min_delay_seconds": 25,
        "max_delay_seconds": 60,
    },
    # Lowercase substring of the form question -> answer.
    "answers": {
        "phone": "",
        "years of experience": "",
        "authorized to work": "Yes",
        "sponsorship": "No",
    },
}


def ensure_dirs() -> None:
    for d in (HOME, RESUME_DIR, TAILORED_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_profile() -> dict:
    ensure_dirs()
    if not PROFILE_PATH.exists():
        PROFILE_PATH.write_text(yaml.safe_dump(DEFAULT_PROFILE, sort_keys=False))
    return yaml.safe_load(PROFILE_PATH.read_text()) or {}
