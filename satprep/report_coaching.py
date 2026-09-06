"""Render incremental wrong-answer coaching reports."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import REPO_ROOT
from .report_coaching_stats import build_context


def render_report(
    conn, rows: list[dict], *, after_attempt_id: int, through_attempt_id: int, generated_at: str
) -> str:
    env = Environment(
        loader=FileSystemLoader(Path(REPO_ROOT) / "satprep" / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    return env.get_template("coaching_report.html").render(
        **build_context(
            conn,
            rows,
            after_attempt_id=after_attempt_id,
            through_attempt_id=through_attempt_id,
            generated_at=generated_at,
        )
    )
