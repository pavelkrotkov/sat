"""Reports section: serve the generated wrong-answer review.

The heavy analysis (per-question diagnosis grounded in the SAT llm-wiki
framework) is generated offline into a self-contained HTML report under
data/reports/ (gitignored, a ReadWritePath of the satprep service). This module
only finds the newest report and hands it to the web UI.

Future work — self-serve ("review my mistakes since the last report") — is
tracked in the GitHub issues; this module deliberately contains no generation
logic yet.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from .config import REPO_ROOT

REPORTS_DIR = REPO_ROOT / "data" / "reports"


def latest_report() -> Path | None:
    """Newest weekly report HTML in data/reports/, or None."""
    if not REPORTS_DIR.is_dir():
        return None
    matches = sorted(REPORTS_DIR.glob("weekly-*.html"), key=lambda p: p.stat().st_mtime)
    return matches[-1] if matches else None


def report_meta() -> dict | None:
    """Display metadata for the latest report, or None."""
    p = latest_report()
    if p is None:
        return None
    try:
        generated = datetime.datetime.fromtimestamp(p.stat().st_mtime)
    except OSError:
        generated = None
    return {
        "name": p.name,
        "url": f"/reports/{p.name}",
        "generated": generated.strftime("%Y-%m-%d %H:%M") if generated else "unknown",
    }
