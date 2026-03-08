#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "playwright>=1.52.0",
# ]
# ///

from __future__ import annotations

import argparse
import base64
import csv
import html
import io
import json
import logging
import os
import re
import shutil
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import MISSING, asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Frame,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


LOG = logging.getLogger("sat_wrong_questions")

DEFAULT_START_URL = "https://mypractice.collegeboard.org/"
DEFAULT_TIMEOUT_MS = 15_000
MAX_BACK_ATTEMPTS = 3
_JS_NORMALIZE = 'const normalize = (text) => (text || "").replace(/\\\\s+/g, " ").trim();'
STOP_WORDS = {
    "about",
    "after",
    "again",
    "against",
    "also",
    "answer",
    "because",
    "before",
    "being",
    "between",
    "both",
    "correct",
    "could",
    "does",
    "each",
    "explanation",
    "from",
    "have",
    "incorrect",
    "into",
    "itself",
    "module",
    "question",
    "review",
    "section",
    "should",
    "show",
    "that",
    "their",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "very",
    "what",
    "when",
    "which",
    "with",
    "would",
    "your",
}
MATH_HINTS = {
    "algebra",
    "equation",
    "expression",
    "function",
    "geometry",
    "trigonometry",
    "circle",
    "triangle",
    "linear",
    "quadratic",
    "ratio",
    "percent",
    "probability",
    "statistics",
    "advanced math",
}
RW_HINTS = {
    "reading",
    "writing",
    "rhetorical",
    "punctuation",
    "grammar",
    "transition",
    "inference",
    "evidence",
    "boundaries",
    "craft",
    "structure",
    "command of evidence",
    "words in context",
}

PANDOC_REPORT_CSS = """\
html {
  line-height: 1.5;
  -webkit-text-size-adjust: 100%;
}

body {
  margin: 0 !important;
  max-width: none !important;
  padding: 32px 40px 56px !important;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif !important;
  color: #17212b;
  background: #ffffff;
}

main {
  max-width: none !important;
}

h1, h2, h3, h4 {
  line-height: 1.2;
  color: #0f172a;
}

h1 {
  margin: 0 0 1rem;
  font-size: 2rem;
}

h2 {
  margin-top: 2rem;
  padding-bottom: 0.25rem;
  border-bottom: 1px solid #e5e7eb;
}

h3 {
  margin-top: 1.5rem;
}

p, li {
  max-width: 92ch;
}

.sat-rich-block,
.sat-rich-block p,
.sat-rich-block li {
  max-width: none;
}

.sat-rich-block mjx-assistive-mml {
  position: absolute !important;
  width: 1px !important;
  height: 1px !important;
  padding: 0 !important;
  margin: -1px !important;
  overflow: hidden !important;
  clip: rect(0, 0, 0, 0) !important;
  clip-path: inset(50%) !important;
  white-space: nowrap !important;
  border: 0 !important;
}

.sat-rich-block mjx-container[jax="SVG"] {
  display: inline-block;
  max-width: 100%;
}

.sat-rich-block mjx-container[jax="SVG"] > svg {
  max-width: 100%;
  height: auto;
}

.sat-answer-choices {
  padding-left: 1.6rem;
}

.sat-answer-choices li {
  margin: 0.35rem 0;
}

a {
  color: #0b63ce;
}

img, svg {
  max-width: 100%;
  height: auto;
}

figure {
  margin: 1rem 0 1.25rem;
}

pre, code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

pre {
  overflow-x: auto;
  padding: 0.9rem 1rem;
  border-radius: 10px;
  background: #f6f8fa;
}

code {
  padding: 0.12rem 0.3rem;
  border-radius: 4px;
  background: #f6f8fa;
}

pre code {
  padding: 0;
  background: transparent;
}

table {
  border-collapse: collapse;
}

th, td {
  padding: 0.4rem 0.6rem;
  border: 1px solid #d7dce2;
}

@media (max-width: 900px) {
  body {
    padding: 20px 16px 40px !important;
  }

  p, li {
    max-width: none;
  }
}
"""


@dataclass
class WrongQuestionRecord:
    uid: str
    scraped_at: str
    test_name: str
    test_number: str = ""
    section: str = ""
    subject_bucket: str = ""
    module: str = ""
    question_number: str = ""
    domain: str = ""
    skill: str = ""
    my_answer: str = ""
    correct_answer: str = ""
    question_text: str = ""
    question_html: str = ""
    answer_choices: list[str] = field(default_factory=list)
    answer_choices_html: list[str] = field(default_factory=list)
    explanation: str = ""
    explanation_html: str = ""
    images: list[str] = field(default_factory=list)
    screenshot_path: str = ""
    html_snapshot_path: str = ""
    source_row_text: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape incorrect SAT Bluebook questions from College Board My Practice.",
    )
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--profile-dir", default="playwright_profile")
    parser.add_argument("--outputs-dir", default="outputs")
    parser.add_argument("--artifacts-dir", default="artifacts")
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    parser.add_argument("--slow-mo", type=int, default=100)
    parser.add_argument("--headless", action="store_true", help="Run headless instead of headed.")
    parser.add_argument("--max-tests", type=int, default=0, help="Limit how many tests to scrape.")
    parser.add_argument(
        "--max-questions-per-test",
        type=int,
        default=0,
        help="Limit how many incorrect questions to scrape per test.",
    )
    parser.add_argument(
        "--force-login-prompt",
        action="store_true",
        help="Always pause for manual login confirmation before scraping.",
    )
    parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="Re-scrape questions even when the UID already exists in outputs/wrong_questions.json.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore any existing outputs and build a fresh dataset for this run.",
    )
    parser.add_argument(
        "--rebuild-from-json",
        default="",
        help="Rebuild outputs from an existing JSON file and saved HTML snapshots; skips browser automation.",
    )
    parser.add_argument(
        "--save-page-visits",
        action="store_true",
        help="Save full-page visit snapshots under artifacts/page_visits for debugging.",
    )
    parser.add_argument(
        "--save-question-screenshots",
        action="store_true",
        help="Save per-question review screenshots under artifacts/screenshots.",
    )
    parser.add_argument(
        "--save-error-screenshots",
        action="store_true",
        help="Save failure screenshots under artifacts/errors.",
    )
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def normalize_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def normalize_lines(value: str | None) -> list[str]:
    if not value:
        return []
    lines = [normalize_space(line) for line in value.splitlines()]
    return [line for line in lines if line]


def slugify(value: str, fallback: str = "item", max_len: int = 80) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    slug = slug[:max_len].strip("-")
    return slug or fallback


def natural_sort_key(value: str) -> list[Any]:
    return [int(chunk) if chunk.isdigit() else chunk.lower() for chunk in re.split(r"(\d+)", value)]


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, content: str) -> None:
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_bytes(content)
    tmp.replace(path)


def json_dump_pretty(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def relative_markdown_path(from_dir: Path, path_str: str) -> str:
    if not path_str:
        return ""
    return os.path.relpath(path_str, start=from_dir).replace("\\", "/")


def ensure_list_of_strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def extract_visual_assets_from_html(uid: str, html: str, image_dir: Path) -> list[str]:
    figure_blocks = re.findall(r"<figure\b.*?</figure>", html, flags=re.IGNORECASE | re.DOTALL)
    if not figure_blocks:
        return []

    ensure_dir(image_dir)
    assets: list[str] = []
    for figure_index, figure_html in enumerate(figure_blocks, start=1):
        svg_match = re.search(r"<svg\b.*?</svg>", figure_html, flags=re.IGNORECASE | re.DOTALL)
        if svg_match:
            svg_markup = svg_match.group(0).strip()
            if "xmlns=" not in svg_markup[:256]:
                svg_markup = svg_markup.replace(
                    "<svg",
                    '<svg xmlns="http://www.w3.org/2000/svg"',
                    1,
                )
            svg_path = image_dir / f"{uid}-figure-{figure_index}.svg"
            atomic_write_text(svg_path, svg_markup + "\n")
            assets.append(str(svg_path))
            continue

        for image_index, img_match in enumerate(
            re.finditer(r"<img\b[^>]*?\bsrc=(['\"])(.*?)\1", figure_html, flags=re.IGNORECASE | re.DOTALL),
            start=1,
        ):
            src = normalize_space(img_match.group(2))
            data_match = re.match(
                r"data:(image/(?:png|jpe?g|gif|webp|svg\+xml));base64,(.+)",
                src,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if not data_match:
                continue
            mime_type = data_match.group(1).lower()
            encoded = re.sub(r"\s+", "", data_match.group(2))
            extension = {
                "image/png": "png",
                "image/jpeg": "jpg",
                "image/jpg": "jpg",
                "image/gif": "gif",
                "image/webp": "webp",
                "image/svg+xml": "svg",
            }.get(mime_type, "bin")
            try:
                payload = base64.b64decode(encoded)
            except ValueError:
                continue
            suffix = f"-img-{image_index}" if image_index > 1 else ""
            image_path = image_dir / f"{uid}-figure-{figure_index}{suffix}.{extension}"
            atomic_write_bytes(image_path, payload)
            assets.append(str(image_path))
    return dedupe_preserve_order(assets)


def detect_test_number(test_name: str) -> str:
    match = re.search(r"(?:practice\s*test|test)\s*#?\s*(\d+)", test_name, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"\bpractice\s+(\d+)\b", test_name, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def make_uid(
    test_name: str,
    section: str,
    module: str,
    question_number: str,
    fallback: str,
) -> str:
    parts = [
        slugify(test_name, "test"),
        slugify(section, "section"),
        slugify(module, "module") if module else "",
        slugify(question_number, "question") if question_number else "",
        slugify(fallback, "wrong-question", max_len=30),
    ]
    return "-".join(part for part in parts if part)


def detect_subject(record: dict[str, Any]) -> str:
    section = normalize_space(record.get("section", "")).lower()
    if "reading and writing" in section:
        return "Reading and Writing"
    if section == "math" or " math" in f" {section} ":
        return "Math"
    haystack = " ".join(
        normalize_space(str(record.get(key, "")))
        for key in ("section", "domain", "skill", "question_text")
    ).lower()
    if any(hint in haystack for hint in MATH_HINTS) or " math" in f" {haystack} ":
        return "Math"
    if any(hint in haystack for hint in RW_HINTS) or "reading and writing" in haystack:
        return "Reading and Writing"
    return normalize_space(record.get("section", "")) or "Unspecified"


def looks_like_choice(line: str) -> bool:
    return bool(
        re.match(r"^[A-H][\).\:\-]\s+\S", line)
        or re.match(r"^(Choice|Option)\s+[A-H]\b", line, flags=re.IGNORECASE)
    )


def tokenize_keywords(text: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z][a-zA-Z\-]{2,}", text.lower())
    return [token for token in tokens if token not in STOP_WORDS]


def pull_label(lines: list[str], *labels: str) -> str:
    lowered = [line.lower() for line in lines]
    for label in labels:
        label_lower = label.lower()
        for idx, line in enumerate(lines):
            lowered_line = lowered[idx]
            if lowered_line.startswith(label_lower + ":"):
                return normalize_space(line.split(":", 1)[1])
            if lowered_line == label_lower and idx + 1 < len(lines):
                return normalize_space(lines[idx + 1])
    return ""


def split_explanation(lines: list[str]) -> tuple[list[str], list[str]]:
    for idx, line in enumerate(lines):
        lowered = line.lower()
        if lowered.startswith("explanation") or "correct answer and explanation" in lowered:
            if ":" in line:
                head, tail = line.split(":", 1)
                remainder = [normalize_space(tail)] if normalize_space(tail) else []
                return lines[:idx], remainder + lines[idx + 1 :]
            return lines[:idx], lines[idx + 1 :]
    return lines, []


def parse_review_content(raw_text: str) -> dict[str, Any]:
    lines = normalize_lines(raw_text)
    before_expl, explanation_lines = split_explanation(lines)
    choices = [line for line in before_expl if looks_like_choice(line)]
    question_number = ""
    for line in lines:
        match = re.search(r"\bquestion\s*(\d+)\b", line, flags=re.IGNORECASE)
        if match:
            question_number = match.group(1)
            break
    metadata_lines = []
    for label in (
        "question",
        "section",
        "module",
        "domain",
        "skill",
        "your answer",
        "correct answer",
        "show correct answer and explanation",
    ):
        metadata_lines.extend(
            line for line in before_expl if line.lower().startswith(label)
        )
    question_lines = [
        line
        for line in before_expl
        if line not in choices and line not in metadata_lines and "review" not in line.lower()
    ]
    explanation = "\n".join(explanation_lines).strip()
    if explanation.replace("\n", " ").strip() in {"Previous Next", "Next Previous", "Previous", "Next"}:
        explanation = ""
    return {
        "question_number": question_number or pull_label(lines, "Question"),
        "section": pull_label(lines, "Section"),
        "module": pull_label(lines, "Module"),
        "domain": pull_label(lines, "Domain"),
        "skill": pull_label(lines, "Skill"),
        "my_answer": pull_label(lines, "Your answer"),
        "correct_answer": pull_label(lines, "Correct answer"),
        "question_text": "\n".join(question_lines).strip(),
        "answer_choices": choices,
        "explanation": explanation,
    }


def summarize_group(items: list[dict[str, Any]], subject: str, label: str) -> str:
    keyword_counts = Counter()
    for item in items:
        keyword_counts.update(tokenize_keywords(f"{item.get('question_text', '')} {item.get('explanation', '')}"))
    common = [term for term, count in keyword_counts.most_common(4) if count >= 2]
    if common:
        return (
            f"This {subject.lower()} cluster centers on {label.lower()}. "
            f"Recurring themes include {', '.join(common[:3])}."
        )
    return f"This {subject.lower()} cluster centers on {label.lower()} and needs targeted repetition."


def review_first(items: list[dict[str, Any]], subject: str, label: str) -> str:
    refs = ", ".join(question_ref(item) for item in items[:4])
    if subject == "Math":
        return f"Redo {refs} untimed, then solve one fresh {label.lower()} set under time pressure."
    if subject == "Reading and Writing":
        return f"Review {refs} slowly, justify each answer choice, then do one timed {label.lower()} passage set."
    return f"Start with {refs}, then create two fresh drills for the same pattern."


def prompt_suggestions(items: list[dict[str, Any]], subject: str, label: str) -> list[str]:
    refs = ", ".join(question_ref(item) for item in items[:5])
    return [
        (
            f"Act as an SAT {subject} tutor. Build a 15-minute drill on {label} based on these misses: "
            f"{refs}. Teach the pattern first, then give 5 original questions with answer explanations."
        ),
        (
            f"I missed these SAT {subject} questions: {refs}. Diagnose the likely misconception behind them, "
            f"rank the causes, and give a compact study plan for the next 3 practice sessions."
        ),
        (
            f"Create a targeted SAT {subject} warm-up for {label}. Use the mistakes from {refs} as examples, "
            f"include one worked example, three guided problems, and three timed problems."
        ),
    ]


def question_ref(item: dict[str, Any]) -> str:
    test_name = item.get("test_name", "Unknown test")
    question_number = item.get("question_number", "").strip()
    suffix = f" Q{question_number}" if question_number else ""
    return f"{test_name}{suffix}"


class ReviewParser:
    def parse_container(self, container: Locator, row_meta: dict[str, Any]) -> tuple[dict[str, Any], str]:
        structured = self.extract_review_structured_data(container)
        merged = {**row_meta, **structured}
        text_payload = ""
        if self.review_parse_needs_fallback(merged):
            text_payload = self.extract_visible_text(container)
            parsed = parse_review_content(text_payload)
            for key, value in parsed.items():
                if value and not merged.get(key):
                    merged[key] = value
        return merged, text_payload

    def review_parse_needs_fallback(self, merged: dict[str, Any]) -> bool:
        required_groups = (
            ("question_html", "question_text"),
            ("explanation_html", "explanation"),
            ("correct_answer",),
        )
        for group in required_groups:
            if any(normalize_space(str(merged.get(key, ""))) for key in group):
                continue
            return True
        return False

    def extract_review_structured_data(self, container: Locator) -> dict[str, Any]:
        try:
            data = container.evaluate(
                """
                (root) => {
                  __JS_NORMALIZE__
                  const heading = normalize(root.querySelector(".question-panel h3")?.innerText);
                  const questionParts = Array.from(root.querySelectorAll(".question-panel p"))
                    .map((node) => normalize(node.innerText))
                    .filter(Boolean);
                  const questionBody = root.querySelector(".question-panel > div") || root.querySelector(".question-panel");
                  const questionHtml = (questionBody?.innerHTML || "").trim();
                  const answerItems = Array.from(root.querySelectorAll(".answer-panel ol li"));
                  const answerChoices = answerItems.map((item, index) => {
                    const label = String.fromCharCode(65 + index);
                    return `${label}. ${normalize(item.innerText)}`;
                  });
                  const answerChoicesHtml = answerItems.map((item) => (item.innerHTML || "").trim()).filter(Boolean);
                  const correctChoiceIndex = answerItems.findIndex((item) =>
                    item.classList.contains("correct") || item.querySelector(".correct")
                  );
                  const statusText = normalize(
                    root.querySelector(".answer-panel p.incorrect, .answer-panel p.correct, .answer-panel p.response")?.innerText
                  );
                  const rationaleHeader = Array.from(root.querySelectorAll(".answer-panel h3"))
                    .find((node) => /rationale/i.test(normalize(node.innerText)));
                  let explanation = "";
                  const explanationHtmlParts = [];
                  if (rationaleHeader) {
                    const parts = [];
                    let sibling = rationaleHeader.nextElementSibling;
                    while (sibling) {
                      const value = normalize(sibling.innerText);
                      if (value) parts.push(value);
                      const html = (sibling.outerHTML || "").trim();
                      if (html) explanationHtmlParts.push(html);
                      sibling = sibling.nextElementSibling;
                    }
                    explanation = parts.join("\\n\\n");
                  }
                  const domain = normalize(
                    root.querySelector(".header-with-ksd .ksd-title p span:last-child")?.innerText
                    || root.querySelector(".header-with-ksd .ksd-title p")?.innerText
                  ).replace(/^Knowledge and Skills:\\s*/i, "");
                  return {
                    heading,
                    question_parts: questionParts,
                    question_html: questionHtml,
                    answer_choices: answerChoices,
                    answer_choices_html: answerChoicesHtml,
                    explanation,
                    explanation_html: explanationHtmlParts.join("\\n"),
                    status_text: statusText,
                    domain,
                    correct_choice_letter: correctChoiceIndex >= 0 ? String.fromCharCode(65 + correctChoiceIndex) : "",
                  };
                }
                """.replace("__JS_NORMALIZE__", _JS_NORMALIZE)
            )
        except PlaywrightError:
            return {}

        structured: dict[str, Any] = {}
        heading = normalize_space(data.get("heading"))
        if heading:
            match = re.search(r"^(.*?):\s*Question\s*(\d+)\s*$", heading, flags=re.IGNORECASE)
            if match:
                structured["section"] = normalize_space(match.group(1))
                structured["question_number"] = match.group(2)
        question_parts = [normalize_space(part) for part in data.get("question_parts", []) if normalize_space(part)]
        if question_parts:
            structured["question_text"] = "\n".join(question_parts)
        question_html = (data.get("question_html") or "").strip()
        if question_html:
            structured["question_html"] = question_html
        answer_choices = [normalize_space(choice) for choice in data.get("answer_choices", []) if normalize_space(choice)]
        if answer_choices:
            structured["answer_choices"] = answer_choices
        answer_choices_html = [fragment.strip() for fragment in data.get("answer_choices_html", []) if fragment and fragment.strip()]
        if answer_choices_html:
            structured["answer_choices_html"] = answer_choices_html
        explanation = normalize_space(data.get("explanation"))
        if explanation:
            structured["explanation"] = explanation
        explanation_html = (data.get("explanation_html") or "").strip()
        if explanation_html:
            structured["explanation_html"] = explanation_html
        domain = normalize_space(data.get("domain"))
        if domain:
            structured["domain"] = domain
        status_text = normalize_space(data.get("status_text"))
        if status_text:
            selected_match = re.search(r"You selected answer\s+([A-H])", status_text, flags=re.IGNORECASE)
            correct_match = re.search(r"correct answer is\s+([A-H])", status_text, flags=re.IGNORECASE)
            if selected_match:
                structured["my_answer"] = f"{selected_match.group(1).upper()}; Incorrect"
            if correct_match:
                structured["correct_answer"] = correct_match.group(1).upper()
        correct_choice = normalize_space(data.get("correct_choice_letter"))
        if correct_choice and not structured.get("correct_answer"):
            structured["correct_answer"] = correct_choice
        return structured

    def extract_visible_text(self, container: Locator) -> str:
        try:
            text = container.evaluate(
                """
                (root) => {
                  const isVisible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
                  };
                  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                  const pieces = [];
                  while (walker.nextNode()) {
                    const node = walker.currentNode;
                    const parent = node.parentElement;
                    if (!parent || !isVisible(parent)) continue;
                    const value = (node.textContent || "").replace(/\\s+/g, " ").trim();
                    if (!value) continue;
                    pieces.push(value);
                  }
                  return pieces.join("\\n");
                }
                """
            )
            return text
        except PlaywrightError:
            return normalize_space(container.inner_text(timeout=1_500))


class OutputManager:
    def __init__(self, outputs_dir: Path, *, fresh: bool = False) -> None:
        self.outputs_dir = ensure_dir(outputs_dir)
        self.json_path = self.outputs_dir / "wrong_questions.json"
        self.csv_path = self.outputs_dir / "wrong_questions.csv"
        self.md_path = self.outputs_dir / "wrong_questions.md"
        self.llm_md_path = self.outputs_dir / "wrong_questions.llm.md"
        self.html_path = self.outputs_dir / "wrong_questions.html"
        self.drill_path = self.outputs_dir / "drill_pack.md"
        self.drill_html_path = self.outputs_dir / "drill_pack.html"
        self.css_path = self.outputs_dir / "pandoc-report.css"
        self.pandoc_path = shutil.which("pandoc")
        self.fragment_conversion_cache: dict[tuple[str, str, bool], str] = {}
        self.records: dict[str, dict[str, Any]] = {} if fresh else self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.json_path.exists():
            return {}
        try:
            payload = json.loads(self.json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            LOG.warning("Could not parse %s; starting with an empty dataset.", self.json_path)
            return {}
        if isinstance(payload, list):
            return {item["uid"]: item for item in payload if isinstance(item, dict) and item.get("uid")}
        return {}

    def has_uid(self, uid: str) -> bool:
        return uid in self.records

    def upsert(self, record: WrongQuestionRecord) -> None:
        payload = self._prepare_record_payload(asdict(record))
        self.records[payload["uid"]] = payload

    def ordered_records(self) -> list[dict[str, Any]]:
        return sorted(
            self.records.values(),
            key=lambda item: (
                natural_sort_key(item.get("test_name", "")),
                natural_sort_key(item.get("section", "")),
                natural_sort_key(item.get("module", "")),
                natural_sort_key(item.get("question_number", "")),
                item.get("uid", ""),
            ),
        )

    def checkpoint_json(self) -> None:
        ordered = self.ordered_records()
        atomic_write_text(self.json_path, json_dump_pretty(ordered))

    def save_reports(self) -> None:
        ordered = self.ordered_records()
        atomic_write_text(self.csv_path, self._render_csv(ordered))
        atomic_write_text(self.md_path, self._render_markdown(ordered))
        atomic_write_text(self.llm_md_path, self._render_llm_markdown(ordered))
        atomic_write_text(self.drill_path, self._render_drill_pack(ordered))
        atomic_write_text(self.css_path, PANDOC_REPORT_CSS)

    def finalize(self) -> None:
        self._refresh_record_assets()
        self.checkpoint_json()
        self.save_reports()
        self._render_html_reports()

    def _render_html_reports(self) -> None:
        pandoc = self.pandoc_path
        if not pandoc:
            LOG.warning("Pandoc is not installed; skipping standalone HTML generation.")
            return

        jobs = [
            (self.md_path, self.html_path),
            (self.drill_path, self.drill_html_path),
        ]
        for markdown_path, html_path in jobs:
            try:
                subprocess.run(
                    [
                        pandoc,
                        "-s",
                        "--embed-resources",
                        "--mathml",
                        "--css",
                        str(self.css_path.resolve()),
                        str(markdown_path),
                        "-o",
                        str(html_path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as exc:
                stderr = normalize_space(exc.stderr)
                LOG.warning("Pandoc HTML export failed for %s: %s", markdown_path.name, stderr or exc)

    def _refresh_record_assets(self) -> None:
        for uid, record in self.records.items():
            record["uid"] = uid
            self.records[uid] = self._prepare_record_payload(record)

    def _prepare_record_payload(self, record: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for field_def in fields(WrongQuestionRecord):
            field_name = field_def.name
            if field_name in record:
                payload[field_name] = record[field_name]
                continue
            if field_def.default_factory is not MISSING:
                payload[field_name] = field_def.default_factory()
            elif field_def.default is not MISSING:
                payload[field_name] = field_def.default
        payload["subject_bucket"] = payload.get("subject_bucket") or detect_subject(payload)
        payload["images"] = self._ensure_embeddable_images(payload)
        return payload

    def _ensure_embeddable_images(self, record: dict[str, Any]) -> list[str]:
        existing = dedupe_preserve_order(ensure_list_of_strings(record.get("images", [])))
        html_snapshot = normalize_space(record.get("html_snapshot_path", ""))
        if not html_snapshot:
            return existing

        html_path = Path(html_snapshot)
        if not html_path.exists():
            return existing

        try:
            html = html_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return existing

        image_dir = ensure_dir(html_path.parent.parent / "images")
        derived = extract_visual_assets_from_html(record.get("uid", "record"), html, image_dir)
        return dedupe_preserve_order(derived + existing)

    def _render_csv(self, records: list[dict[str, Any]]) -> str:
        fieldnames = [field_def.name for field_def in fields(WrongQuestionRecord)]
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            row = record.copy()
            row["answer_choices"] = json.dumps(row.get("answer_choices", []), ensure_ascii=False)
            row["answer_choices_html"] = json.dumps(row.get("answer_choices_html", []), ensure_ascii=False)
            row["images"] = json.dumps(row.get("images", []), ensure_ascii=False)
            writer.writerow(row)
        return output.getvalue()

    def _render_markdown(self, records: list[dict[str, Any]]) -> str:
        return self._render_question_report(
            records,
            title="# Wrong SAT Bluebook Questions",
            intro="",
            include_images=True,
            include_visual_context=False,
        )

    def _render_llm_markdown(self, records: list[dict[str, Any]]) -> str:
        intro = (
            "This file is optimized for LLM analysis. Math is preserved as Markdown math, "
            "and visuals are converted to text descriptions or table-like plain text. "
            "External image links are intentionally omitted."
        )
        return self._render_question_report(
            records,
            title="# Wrong SAT Bluebook Questions (LLM Analysis Edition)",
            intro=intro,
            include_images=False,
            include_visual_context=True,
        )

    def _render_question_report(
        self,
        records: list[dict[str, Any]],
        *,
        title: str,
        intro: str,
        include_images: bool,
        include_visual_context: bool,
    ) -> str:
        lines = [
            title,
            "",
            f"Generated {utc_now()}",
            "",
        ]
        if intro:
            lines.extend([intro, ""])
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            grouped[record.get("test_name") or "Unknown Test"].append(record)
        for test_name in sorted(grouped, key=natural_sort_key):
            lines.extend([f"## {test_name}", ""])
            for item in grouped[test_name]:
                self._append_question_report_section(
                    lines,
                    item,
                    include_images=include_images,
                    include_visual_context=include_visual_context,
                )
            lines.append("")
        return self._clean_report_markdown("\n".join(lines))

    def _append_question_report_section(
        self,
        lines: list[str],
        item: dict[str, Any],
        *,
        include_images: bool,
        include_visual_context: bool,
    ) -> None:
        q_label = item.get("question_number") or item.get("uid")
        lines.append(f"### Question {q_label}")
        lines.append("")
        lines.append(f"- Section: {item.get('section') or 'Unknown'}")
        lines.append(f"- Subject: {item.get('subject_bucket') or 'Unknown'}")
        lines.append(f"- Module: {item.get('module') or 'Unknown'}")
        lines.append(f"- Domain / Skill: {item.get('domain') or 'Unknown'} / {item.get('skill') or 'Unknown'}")
        lines.append(f"- My answer: {item.get('my_answer') or 'Unknown'}")
        lines.append(f"- Correct answer: {item.get('correct_answer') or 'Unknown'}")
        if include_images:
            self._append_record_images(lines, item)
        if item.get("screenshot_path"):
            lines.append(f"- Screenshot: {relative_markdown_path(self.outputs_dir, item['screenshot_path'])}")
        if item.get("html_snapshot_path"):
            lines.append(f"- HTML snapshot: {relative_markdown_path(self.outputs_dir, item['html_snapshot_path'])}")
        lines.extend(["", "#### Question", ""])
        question_markdown = self._record_fragment_markdown(item, "question_html", item.get("question_text", ""))
        lines.extend([question_markdown or "_Not parsed cleanly._", ""])
        if include_visual_context:
            self._append_visual_context(lines, item)
        self._append_answer_choices(lines, item)
        lines.extend(["#### Explanation", ""])
        explanation_markdown = self._record_fragment_markdown(item, "explanation_html", item.get("explanation", ""))
        lines.extend([explanation_markdown or "_Not found._", ""])

    def _append_record_images(self, lines: list[str], item: dict[str, Any]) -> None:
        image_paths = [str(path) for path in item.get("images", []) if path]
        if not image_paths:
            return
        lines.extend(["", "#### Figure", ""])
        for image_path in image_paths:
            image_uri = Path(image_path).resolve().as_uri()
            if image_path.lower().endswith(".svg"):
                lines.append(f"![Figure]({image_uri}){{.inline-svg}}")
            else:
                lines.append(f"![Figure]({image_uri})")
            lines.append("")

    def _append_visual_context(self, lines: list[str], item: dict[str, Any]) -> None:
        visual_contexts = self._extract_llm_visual_contexts(item)
        if not visual_contexts:
            return
        lines.extend(["#### Visual Context", ""])
        for index, context in enumerate(visual_contexts, start=1):
            lines.extend([f"##### Visual {index}", ""])
            lines.extend(context.splitlines())
            lines.append("")

    def _append_answer_choices(self, lines: list[str], item: dict[str, Any]) -> None:
        if item.get("answer_choices_html"):
            lines.extend(["#### Answer Choices", ""])
            for index, choice_html in enumerate(item["answer_choices_html"], start=1):
                label = chr(64 + index)
                converted = self._convert_html_fragment(
                    choice_html,
                    target_format="commonmark_x",
                    strip_figures=True,
                ).strip()
                if not converted:
                    continue
                choice_lines = converted.splitlines()
                lines.append(f"- {label}. {choice_lines[0]}")
                for line in choice_lines[1:]:
                    lines.append(f"  {line}" if line else "")
            lines.append("")
            return
        if item.get("answer_choices"):
            lines.extend(["#### Answer Choices", ""])
            lines.extend(f"- {choice}" for choice in item["answer_choices"])
            lines.append("")

    def _record_fragment_markdown(self, item: dict[str, Any], html_key: str, plain_text: str) -> str:
        html_fragment = item.get(html_key) or ""
        if html_fragment:
            converted = self._convert_html_fragment(
                html_fragment,
                target_format="commonmark_x",
                strip_figures=True,
            ).strip()
            if converted:
                return converted
        return plain_text or ""

    def _convert_html_fragment(
        self,
        html_fragment: str,
        *,
        target_format: str,
        strip_figures: bool,
    ) -> str:
        fragment = (html_fragment or "").strip()
        if not fragment:
            return ""
        cache_key = (fragment, target_format, strip_figures)
        cached = self.fragment_conversion_cache.get(cache_key)
        if cached is not None:
            return cached
        if not self.pandoc_path:
            self.fragment_conversion_cache[cache_key] = ""
            return ""

        prepared = self._prepare_fragment_for_markdown(fragment, strip_figures=strip_figures)
        try:
            result = subprocess.run(
                [self.pandoc_path, "-f", "html", "-t", target_format],
                input=prepared,
                capture_output=True,
                text=True,
                check=True,
            )
            if target_format == "plain":
                converted = self._postprocess_plain_fragment(result.stdout)
            else:
                converted = self._postprocess_markdown_fragment(result.stdout)
        except subprocess.CalledProcessError as exc:
            stderr = normalize_space(exc.stderr)
            LOG.debug("Pandoc fragment conversion failed: %s", stderr or exc)
            converted = ""
        self.fragment_conversion_cache[cache_key] = converted
        return converted

    def _prepare_fragment_for_markdown(self, html_fragment: str, *, strip_figures: bool) -> str:
        fragment = html_fragment
        if strip_figures:
            fragment = re.sub(
                r"<([a-z0-9]+)\b[^>]*class=(['\"])[^'\"]*\b(?:sr-only|visually-hidden)\b[^'\"]*\2[^>]*>.*?</\1>",
                "",
                fragment,
                flags=re.IGNORECASE | re.DOTALL,
            )
        if strip_figures:
            fragment = re.sub(r"<figure\b.*?</figure>", "", fragment, flags=re.IGNORECASE | re.DOTALL)
        fragment = re.sub(
            r"<mjx-container\b[^>]*>.*?<mjx-assistive-mml[^>]*>(.*?)</mjx-assistive-mml>.*?</mjx-container>",
            r"\1",
            fragment,
            flags=re.IGNORECASE | re.DOTALL,
        )
        fragment = re.sub(
            r"<mjx-assistive-mml\b[^>]*>(.*?)</mjx-assistive-mml>",
            r"\1",
            fragment,
            flags=re.IGNORECASE | re.DOTALL,
        )
        fragment = fragment.replace("&nbsp;", " ")
        while True:
            unwrapped = re.sub(
                r"^\s*<div\b[^>]*>(.*)</div>\s*$",
                r"\1",
                fragment,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if unwrapped == fragment:
                break
            fragment = unwrapped
        return fragment.strip()

    def _postprocess_markdown_fragment(self, markdown: str) -> str:
        cleaned: list[str] = []
        for line in markdown.replace("\u00a0", " ").splitlines():
            stripped = line.strip()
            if stripped in {"::: {}", ":::"} or stripped.startswith(":::"):
                continue
            cleaned.append(line.rstrip())
        text = "\n".join(cleaned).replace("\\'", "'")
        text = re.sub(
            r"\[(?:\\_)+\]\{[^{}]*\}\s*\[blank\]\{[^{}]*\}",
            "[blank]",
            text,
            flags=re.DOTALL,
        )
        text = re.sub(r"\[([^\]]+)\]\{[^{}]*\}", r"\1", text, flags=re.DOTALL)
        return text.strip()

    def _postprocess_plain_fragment(self, plain_text: str) -> str:
        cleaned: list[str] = []
        skipping_attr_block = False
        for line in plain_text.replace("\u00a0", " ").splitlines():
            line = line.replace("[]", "").rstrip()
            stripped = normalize_space(line)
            if skipping_attr_block:
                if stripped.endswith("}"):
                    skipping_attr_block = False
                continue
            if stripped.startswith(": "):
                stripped = stripped[2:]
                line = stripped
            if not stripped:
                if cleaned and cleaned[-1]:
                    cleaned.append("")
                continue
            if stripped.startswith("{"):
                if not stripped.endswith("}"):
                    skipping_attr_block = True
                continue
            cleaned.append(line if line else stripped)
        while cleaned and not cleaned[-1]:
            cleaned.pop()
        return "\n".join(cleaned).replace("\\'", "'").strip()

    def _extract_llm_visual_contexts(self, item: dict[str, Any]) -> list[str]:
        contexts: list[str] = []
        seen: set[str] = set()
        for fragment_key in ("question_html", "explanation_html"):
            fragment = (item.get(fragment_key) or "").strip()
            if not fragment:
                continue
            fragment_added = False
            for block in self._extract_visual_blocks(fragment):
                rendered = self._convert_html_fragment(
                    block,
                    target_format="plain",
                    strip_figures=False,
                )
                if self._is_meaningful_visual_context(rendered):
                    normalized = rendered.strip()
                    if normalized not in seen:
                        seen.add(normalized)
                        contexts.append(normalized)
                        fragment_added = True
            fallback_label = self._extract_visual_aria_label(fragment)
            if not fragment_added and fallback_label and fallback_label not in seen:
                seen.add(fallback_label)
                contexts.append(fallback_label)
        return contexts

    def _extract_visual_blocks(self, html_fragment: str) -> list[str]:
        fragment = (html_fragment or "").strip()
        if not fragment:
            return []

        blocks: list[str] = []
        sr_only_pattern = re.compile(
            r"<([a-z0-9]+)\b[^>]*class=(['\"])[^'\"]*\b(?:sr-only|visually-hidden)\b[^'\"]*\2[^>]*>.*?</\1>",
            flags=re.IGNORECASE | re.DOTALL,
        )
        figure_pattern = re.compile(r"<figure\b.*?</figure>", flags=re.IGNORECASE | re.DOTALL)
        table_pattern = re.compile(r"<table\b.*?</table>", flags=re.IGNORECASE | re.DOTALL)

        sr_only_blocks = sr_only_pattern.findall(fragment)
        if sr_only_blocks:
            for match in sr_only_pattern.finditer(fragment):
                block = match.group(0).strip()
                if block:
                    blocks.append(block)

        figure_blocks = figure_pattern.findall(fragment)
        blocks.extend(block.strip() for block in figure_blocks if block.strip())

        fragment_without_figures = figure_pattern.sub("", fragment)
        table_blocks = table_pattern.findall(fragment_without_figures)
        blocks.extend(table.strip() for table in table_blocks if table.strip())
        return blocks

    def _extract_visual_aria_label(self, html_fragment: str) -> str:
        fragment = re.sub(
            r"<mjx-container\b.*?</mjx-container>",
            "",
            html_fragment or "",
            flags=re.IGNORECASE | re.DOTALL,
        )
        for match in re.finditer(
            r"<(?:svg|img)\b[^>]*\b(?:aria-label|alt)=(['\"])(.*?)\1",
            fragment,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            label = html.unescape(normalize_space(match.group(2)))
            lowered = label.lower()
            if not label or lowered in {"scrollable content"}:
                continue
            if len(label) < 12:
                continue
            return re.sub(r"\s*Refer to long description\.?\s*$", "", label, flags=re.IGNORECASE)
        return ""

    def _is_meaningful_visual_context(self, text: str) -> bool:
        normalized = normalize_space(text.replace("[]", ""))
        if not normalized:
            return False
        if len(normalized) < 12:
            return False
        if normalized.lower() in {"figure", "table", "visual"}:
            return False
        return True

    def _clean_report_markdown(self, markdown: str) -> str:
        text = markdown.strip().replace("\\'", "'")
        text = re.sub(
            r"\[(?:\\_)+\]\{[^{}]*\}\s*\[blank\]\{[^{}]*\}",
            "[blank]",
            text,
            flags=re.DOTALL,
        )
        text = re.sub(r"\[([^\]]+)\]\{[^{}]*\}", r"\1", text, flags=re.DOTALL)
        cleaned: list[str] = []
        for line in text.splitlines():
            if line.strip().startswith(":::"):
                continue
            cleaned.append(line.rstrip())
        return "\n".join(cleaned).strip() + "\n"

    def _render_drill_pack(self, records: list[dict[str, Any]]) -> str:
        lines = [
            "# SAT Wrong Question Drill Pack",
            "",
            f"Generated {utc_now()}",
            "",
        ]
        subject_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            subject_groups[record.get("subject_bucket") or detect_subject(record)].append(record)
        for subject in sorted(subject_groups, key=natural_sort_key):
            lines.extend([f"## {subject}", ""])
            domain_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for item in subject_groups[subject]:
                label = item.get("domain") or item.get("skill") or "Unlabeled pattern"
                domain_groups[label].append(item)
            for label, items in sorted(
                domain_groups.items(),
                key=lambda entry: (-len(entry[1]), natural_sort_key(entry[0])),
            ):
                lines.extend([f"### {label}", ""])
                lines.append(f"- Affected tests/questions: {', '.join(question_ref(item) for item in items)}")
                lines.append(f"- Weakness summary: {summarize_group(items, subject, label)}")
                lines.append(f"- Review first: {review_first(items, subject, label)}")
                lines.append("- Ready-to-paste prompts:")
                for prompt in prompt_suggestions(items, subject, label):
                    lines.append(f"  - {prompt}")
                lines.append("")
        return "\n".join(lines).strip() + "\n"


class SatBluebookScraper:
    def __init__(self, args: argparse.Namespace, outputs: OutputManager) -> None:
        self.args = args
        self.outputs = outputs
        self.profile_dir = ensure_dir(Path(args.profile_dir))
        self.artifacts_dir = ensure_dir(Path(args.artifacts_dir))
        self.html_dir = ensure_dir(self.artifacts_dir / "html")
        self.image_dir = ensure_dir(self.artifacts_dir / "images")
        self.error_dir = self.artifacts_dir / "errors"
        self.screenshot_dir = self.artifacts_dir / "screenshots"
        self.page_visit_dir = self.artifacts_dir / "page_visits"
        self.context: BrowserContext | None = None
        self.main_page: Page | None = None
        self.page_visit_counter = 0
        self.attached_page_ids: set[int] = set()
        self.review_parser = ReviewParser()

    def run(self) -> None:
        with sync_playwright() as playwright:
            self.context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=self.args.headless,
                slow_mo=self.args.slow_mo,
                viewport=None,
                args=["--start-maximized"],
            )
            self.context.set_default_timeout(self.args.timeout_ms)
            if self.args.save_page_visits:
                self.install_debug_hooks()
            self.main_page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.main_page.set_default_timeout(self.args.timeout_ms)
            self.main_page.goto(self.args.start_url, wait_until="domcontentloaded")
            self.wait_for_ready_state(self.main_page)
            self.ensure_ready_to_scrape(self.main_page)
            LOG.info("Detected page state: %s", self.describe_page_state(self.main_page))

            if self.on_questions_overview(self.main_page):
                test_name = self.current_test_name(self.main_page) or "Current Test"
                self.process_questions_overview(self.main_page, test_name)
                return

            if self.on_test_list(self.main_page):
                test_cards = self.discover_test_cards(self.main_page)
                if not test_cards:
                    raise RuntimeError(
                        "Could not discover any SAT Practice Tests. Log in and navigate to the My Practice page, "
                        "then rerun the script."
                    )
                if self.args.max_tests > 0:
                    test_cards = test_cards[: self.args.max_tests]
                LOG.info("Discovered %d tests.", len(test_cards))
                for index, card in enumerate(test_cards, start=1):
                    test_name = card["test_name"]
                    LOG.info("Processing test %d/%d: %s", index, len(test_cards), test_name)
                    self.main_page.goto(self.args.start_url, wait_until="domcontentloaded")
                    self.wait_for_ready_state(self.main_page)
                    self.ensure_ready_to_scrape(self.main_page, reprompt=False)
                    try:
                        self.process_test_card(self.main_page, card)
                    except Exception as exc:  # noqa: BLE001
                        self.capture_error(self.main_page, f"test-failure-{slugify(test_name)}")
                        LOG.exception("Failed while scraping %s: %s", test_name, exc)
                return

            if self.on_test_page(self.main_page):
                test_name = self.current_test_name(self.main_page) or "Current Test"
                self.process_test_from_current_page(self.main_page, test_name)
                return

            raise RuntimeError("The current page did not match a supported My Practice workflow.")

    def wait_for_ready_state(self, page: Page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=4_000)
        except PlaywrightTimeoutError:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=2_000)
            except PlaywrightTimeoutError:
                pass
        page.wait_for_timeout(750)
        if self.args.save_page_visits:
            self.snapshot_page(page, "wait_for_ready_state")

    def install_debug_hooks(self) -> None:
        assert self.context is not None
        for page in self.context.pages:
            self.attach_page_debug_hooks(page)
        self.context.on("page", self.attach_page_debug_hooks)

    def attach_page_debug_hooks(self, page: Page) -> None:
        page_id = id(page)
        if page_id in self.attached_page_ids:
            return
        self.attached_page_ids.add(page_id)
        page.on("domcontentloaded", lambda: self.snapshot_page(page, "domcontentloaded"))
        page.on("load", lambda: self.snapshot_page(page, "load"))
        page.on("framenavigated", lambda frame: self.on_frame_navigated(page, frame))
        self.snapshot_page(page, "page_attached")

    def on_frame_navigated(self, page: Page, frame: Frame) -> None:
        try:
            if frame != page.main_frame:
                return
        except PlaywrightError:
            return
        self.snapshot_page(page, "framenavigated")

    def snapshot_page(self, page: Page, reason: str) -> str:
        if not self.args.save_page_visits:
            return ""
        if page.is_closed():
            return ""
        try:
            url = normalize_space(page.url) or "about-blank"
        except PlaywrightError:
            url = "unavailable-url"
        try:
            html = page.content()
        except PlaywrightError:
            return ""
        try:
            title = normalize_space(page.title())
        except PlaywrightError:
            title = ""
        self.page_visit_counter += 1
        prefix = f"{self.page_visit_counter:05d}-{slugify(reason, 'event')}-{slugify(url, 'page', max_len=50)}"
        ensure_dir(self.page_visit_dir)
        html_path = self.page_visit_dir / f"{prefix}.html"
        meta_path = self.page_visit_dir / f"{prefix}.json"
        atomic_write_text(html_path, html)
        atomic_write_text(
            meta_path,
            json_dump_pretty(
                {
                    "snapshot_id": self.page_visit_counter,
                    "captured_at": utc_now(),
                    "reason": reason,
                    "url": url,
                    "title": title,
                    "html_path": str(html_path),
                }
            ),
        )
        LOG.info("Saved page snapshot %s for %s", html_path.name, url)
        return str(html_path)

    def ensure_ready_to_scrape(self, page: Page, reprompt: bool = True) -> None:
        ready = self.on_test_list(page) or self.on_test_page(page) or self.on_questions_overview(page)
        if not ready and not reprompt and not self.args.force_login_prompt:
            self.wait_for_ready_state(page)
            ready = self.on_test_list(page) or self.on_test_page(page) or self.on_questions_overview(page)
        if self.args.force_login_prompt or (reprompt and not ready):
            print(
                "\nManual step: if College Board asks you to sign in, do it in the opened browser window.\n"
                "Once you can see SAT Practice Tests, a test page, or Questions Overview, press Enter here.\n",
                flush=True,
            )
            input()
            self.wait_for_ready_state(page)
            ready = self.on_test_list(page) or self.on_test_page(page) or self.on_questions_overview(page)
        if not ready and reprompt:
            raise RuntimeError(
                "The browser is not on a recognizable My Practice screen after the login prompt."
            )

    def on_test_list(self, page: Page) -> bool:
        return self.any_text_visible(page, [r"SAT Practice Tests"]) and self.page_has_test_cards(page)

    def on_test_page(self, page: Page) -> bool:
        return (
            self.any_text_visible(page, [r"Score Details"])
            and not self.on_test_list(page)
            and not self.on_questions_overview(page)
        )

    def on_questions_overview(self, page: Page) -> bool:
        if self.any_text_visible(page, [r"Questions Overview"]):
            return True
        try:
            row_count = page.locator("tr, [role='row']").count()
        except PlaywrightError:
            row_count = 0
        if row_count == 0:
            return False
        body_text = self.safe_inner_text(page.locator("body"))
        lowered = body_text.lower()
        return "incorrect" in lowered and "review" in lowered and "your answer" in lowered

    def describe_page_state(self, page: Page) -> str:
        states = []
        if self.on_test_list(page):
            states.append("test_list")
        if self.on_test_page(page):
            states.append("test_page")
        if self.on_questions_overview(page):
            states.append("questions_overview")
        return ", ".join(states) or "unknown"

    def page_has_test_cards(self, page: Page) -> bool:
        try:
            return page.locator(".carousel-score-card").count() > 0
        except PlaywrightError:
            return False

    def any_text_visible(self, page: Page, patterns: list[str]) -> bool:
        for pattern in patterns:
            try:
                if page.get_by_text(re.compile(pattern, flags=re.IGNORECASE)).first.is_visible(timeout=1_000):
                    return True
            except (PlaywrightTimeoutError, PlaywrightError):
                continue
        return False

    def discover_test_cards(self, page: Page) -> list[dict[str, Any]]:
        self.lazy_load(page)
        try:
            raw_cards = page.evaluate(
                """
                () => {
                  const isVisible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
                  };
                  __JS_NORMALIZE__
                  const out = [];
                  const cards = Array.from(document.querySelectorAll(".carousel-score-card"));
                  cards.forEach((card, index) => {
                    const version = normalize(card.querySelector(".version")?.textContent);
                    const date = normalize(card.querySelector(".date")?.textContent);
                    const detailsButton = card.querySelector("button.details-button");
                    const numberMatch = version.match(/practice\\s*(\\d+)/i);
                    if (!numberMatch || !detailsButton) return;
                    const number = numberMatch[1];
                    out.push({
                      card_index: index,
                      version,
                      date,
                      test_name: `SAT Practice Test ${number}`,
                    });
                  });
                  return out;
                }
                """.replace("__JS_NORMALIZE__", _JS_NORMALIZE)
            )
        except PlaywrightError:
            raw_cards = []
        cards = []
        seen: set[str] = set()
        for card in raw_cards:
            test_name = normalize_space(card.get("test_name"))
            if not test_name or test_name in seen:
                continue
            seen.add(test_name)
            cards.append(
                {
                    "card_index": int(card["card_index"]),
                    "version": normalize_space(card.get("version")),
                    "date": normalize_space(card.get("date")),
                    "test_name": test_name,
                }
            )
        LOG.info("Discovered test cards: %s", [card["test_name"] for card in cards])
        return cards

    def lazy_load(self, page: Page) -> None:
        stable_passes = 0
        previous_height = 0
        for _ in range(8):
            height = page.evaluate("() => document.body.scrollHeight")
            page.evaluate("height => window.scrollTo(0, height)", height)
            page.wait_for_timeout(500)
            if height == previous_height:
                stable_passes += 1
            else:
                stable_passes = 0
            if stable_passes >= 2:
                break
            previous_height = height
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(400)

    def process_test_card(self, page: Page, card: dict[str, Any]) -> None:
        test_name = card["test_name"]
        score_page = self.click_with_possible_popup(
            page,
            lambda: self.click_score_details_for_card(page, int(card["card_index"]), test_name),
            description=f"open Score Details for {test_name}",
        )
        self.process_questions_overview(score_page, test_name)
        if score_page is not page and not score_page.is_closed():
            score_page.close()

    def process_test_from_current_page(self, page: Page, test_name: str) -> None:
        score_page = self.click_with_possible_popup(
            page,
            lambda: self.click_score_details(page),
            description=f"open Score Details for {test_name}",
        )
        self.process_questions_overview(score_page, test_name)
        if score_page is not page and not score_page.is_closed():
            score_page.close()

    def click_score_details(self, page: Page) -> None:
        patterns = [r"Score Details", r"View Score Details"]
        for pattern in patterns:
            if self.click_by_text(page, pattern, regex=True, required=False):
                return
        raise RuntimeError("Could not find the Score Details control.")

    def click_score_details_for_card(self, page: Page, card_index: int, test_name: str) -> None:
        self.dismiss_session_modal(page)
        card = page.locator(".carousel-score-card").nth(card_index)
        button = card.locator("button.details-button").first
        try:
            card.scroll_into_view_if_needed(timeout=2_000)
        except (PlaywrightError, PlaywrightTimeoutError):
            pass
        if self.click_first_visible(button, required=False):
            return
        try:
            clicked = page.evaluate(
                """
                ({ index }) => {
                  const cards = Array.from(document.querySelectorAll(".carousel-score-card"));
                  const card = cards[index];
                  const button = card?.querySelector("button.details-button");
                  if (!button) return false;
                  button.click();
                  return true;
                }
                """,
                {"index": card_index},
            )
            if clicked:
                return
        except PlaywrightError:
            pass
        raise RuntimeError(f"Could not click Score Details for {test_name}.")

    def dismiss_session_modal(self, page: Page) -> None:
        self.click_by_text(page, r"\bContinue\b", regex=True, required=False)

    def process_questions_overview(self, page: Page, test_name: str) -> None:
        self.wait_for_questions_overview(page)
        if not self.on_questions_overview(page):
            screenshot = self.capture_error(page, f"not-questions-overview-{slugify(test_name)}")
            raise RuntimeError(
                f"Did not reach Questions Overview for {test_name}. "
                f"Saved a diagnostic screenshot to {screenshot or 'artifacts/errors/'}."
            )
        self.set_view_all(page)
        row_targets = self.incorrect_row_targets(page)
        total_rows = len(row_targets)
        LOG.info("%s: found %d incorrect question rows.", test_name, total_rows)
        if self.args.max_questions_per_test > 0:
            row_targets = row_targets[: self.args.max_questions_per_test]
        for row_position, row_target in enumerate(row_targets):
            self.wait_for_questions_overview(page)
            row = self.find_row_for_target(page, row_target)
            if row is None:
                LOG.warning("Could not refind incorrect row %d for %s; stopping early.", row_position + 1, test_name)
                break
            row_meta = row_target["meta"]
            tentative_uid = make_uid(
                test_name=test_name,
                section=row_meta.get("section", ""),
                module=row_meta.get("module", ""),
                question_number=row_meta.get("question_number", ""),
                fallback=row_meta.get("source_row_text", "")[:30],
            )
            if self.outputs.has_uid(tentative_uid) and not self.args.overwrite_existing:
                LOG.info("Skipping already-scraped question %s.", tentative_uid)
                continue
            review_page = page
            try:
                review_page = self.click_with_possible_popup(
                    page,
                    lambda: self.click_review(row),
                    description=f"open review for {test_name} row {row_position + 1}",
                )
                record = self.scrape_review_page(review_page, test_name, row_meta)
                if self.outputs.has_uid(record.uid) and not self.args.overwrite_existing:
                    LOG.info("Skipping existing UID after review scrape: %s", record.uid)
                else:
                    self.outputs.upsert(record)
                    self.outputs.checkpoint_json()
                    LOG.info("Saved %s.", record.uid)
            except Exception as exc:  # noqa: BLE001
                self.capture_error(review_page, f"review-failure-{slugify(test_name)}-{row_position + 1}")
                LOG.exception("Failed to scrape review page for %s row %d: %s", test_name, row_position + 1, exc)
            finally:
                try:
                    self.return_to_questions_overview(page, review_page)
                except Exception as cleanup_exc:  # noqa: BLE001
                    self.capture_error(page, f"cleanup-failure-{slugify(test_name)}-{row_position + 1}")
                    LOG.exception(
                        "Failed to return to Questions Overview for %s row %d: %s",
                        test_name,
                        row_position + 1,
                        cleanup_exc,
                    )

    def wait_for_questions_overview(self, page: Page) -> None:
        if self.on_questions_overview(page):
            return
        try:
            page.get_by_text(re.compile(r"Questions Overview", re.IGNORECASE)).first.wait_for(timeout=5_000)
        except PlaywrightTimeoutError:
            page.wait_for_timeout(1_000)

    def set_view_all(self, page: Page) -> None:
        self.dismiss_session_modal(page)
        try:
            total_questions = self.total_questions_count(page)
        except Exception:  # noqa: BLE001
            total_questions = 0
        page_size_buttons = page.locator("#questions-table .page-size button")
        try:
            button_count = page_size_buttons.count()
        except PlaywrightError:
            button_count = 0
        for index in range(button_count):
            button = page_size_buttons.nth(index)
            label = self.safe_inner_text(button)
            if label != "All":
                continue
            aria_disabled = button.get_attribute("disabled")
            classes = button.get_attribute("class") or ""
            if aria_disabled is not None or "selected" in classes:
                return
            if self.click_first_visible(button, required=False):
                self.wait_for_table_row_count(page, minimum=max(total_questions, 11) if total_questions > 10 else 1)
                return
        try:
            labeled_select = page.get_by_label(re.compile(r"View", re.IGNORECASE))
            if labeled_select.count() > 0:
                try:
                    labeled_select.first.select_option(label="All")
                except PlaywrightError:
                    labeled_select.first.select_option(value="all")
                page.wait_for_timeout(600)
                return
        except (PlaywrightError, PlaywrightTimeoutError):
            pass

        if self.click_by_text(page, r"\bAll\b", regex=True, required=False):
            page.wait_for_timeout(600)

    def questions_table_rows(self, page: Page) -> Locator:
        return page.locator("tr, [role='row']")

    def total_questions_count(self, page: Page) -> int:
        candidates = [
            page.locator("#questions-overview-tallies .total-quetions .number"),
            page.locator("#questions-overview-tallies .total-questions .number"),
        ]
        for locator in candidates:
            try:
                if locator.count() == 0:
                    continue
                text = self.safe_inner_text(locator.first)
                match = re.search(r"\d+", text)
                if match:
                    return int(match.group(0))
            except PlaywrightError:
                continue
        return 0

    def wait_for_table_row_count(self, page: Page, minimum: int) -> None:
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                row_count = page.locator("#questions-table tbody tr").count()
            except PlaywrightError:
                row_count = 0
            if row_count >= minimum:
                return
            page.wait_for_timeout(300)

    def incorrect_row_targets(self, page: Page) -> list[dict[str, Any]]:
        rows = self.questions_table_rows(page)
        targets: list[dict[str, Any]] = []
        try:
            count = rows.count()
        except PlaywrightError:
            return targets
        for index in range(count):
            row = rows.nth(index)
            row_text = self.safe_inner_text(row)
            lowered = row_text.lower()
            if "incorrect" not in lowered:
                continue
            if "questions overview" in lowered or "your answer" in lowered:
                continue
            targets.append(
                {
                    "row_index": index,
                    "row_text": row_text,
                    "meta": self.read_row_metadata(row, row_text),
                }
            )
        return targets

    def find_row_for_target(self, page: Page, target: dict[str, Any]) -> Locator | None:
        rows = self.questions_table_rows(page)
        target_index = int(target["row_index"])
        target_text = target["row_text"]
        try:
            candidate = rows.nth(target_index)
            if self.safe_inner_text(candidate) == target_text:
                return candidate
        except PlaywrightError:
            pass

        try:
            count = rows.count()
        except PlaywrightError:
            return None
        for index in range(count):
            candidate = rows.nth(index)
            if self.safe_inner_text(candidate) == target_text:
                return candidate
        return None

    def read_row_metadata(self, row: Locator, row_text: str = "") -> dict[str, str]:
        row_text = row_text or self.safe_inner_text(row)
        header_cells = row.locator("th")
        cells = row.locator("td, [role='cell']")
        question_number = ""
        try:
            if header_cells.count() > 0:
                question_number = self.safe_inner_text(header_cells.first)
        except PlaywrightError:
            question_number = ""
        values: list[str] = []
        try:
            count = cells.count()
        except PlaywrightError:
            count = 0
        for index in range(count):
            text = self.safe_inner_text(cells.nth(index))
            if text:
                values.append(text)
        section = next(
            (value for value in values if "math" in value.lower() or "reading" in value.lower() or "writing" in value.lower()),
            "",
        )
        module = next((value for value in values if "module" in value.lower()), "")
        try:
            row_class = row.get_attribute("class") or ""
        except PlaywrightError:
            row_class = ""
        if not module:
            module_match = re.search(r"module-(\d+)", row_class, flags=re.IGNORECASE)
            if module_match:
                module = f"Module {module_match.group(1)}"
        if not question_number:
            for value in values:
                match = re.search(r"\bquestion\s*(\d+)\b|\b(\d+)\b", value, flags=re.IGNORECASE)
                if match:
                    question_number = match.group(1) or match.group(2)
                    break
        my_answer = next((value for value in values if "incorrect" in value.lower()), "")
        correct_answer = values[1] if len(values) >= 2 else ""
        domain = values[4] if len(values) >= 5 else ""
        return {
            "section": section,
            "module": module,
            "question_number": question_number,
            "domain": domain,
            "my_answer": my_answer,
            "correct_answer": correct_answer,
            "source_row_text": row_text,
        }

    def click_review(self, row: Locator) -> None:
        candidates = [
            row.get_by_role("button", name=re.compile(r"Review", re.IGNORECASE)),
            row.get_by_role("link", name=re.compile(r"Review", re.IGNORECASE)),
            row.get_by_text(re.compile(r"\bReview\b", re.IGNORECASE)),
        ]
        for locator in candidates:
            if self.click_first_visible(locator, required=False):
                return
        try:
            clicked = row.evaluate(
                """
                (node) => {
                  const button = node.querySelector("button, a, [role='button'], [role='link']");
                  if (!button) return false;
                  button.scrollIntoView({ block: "center", inline: "nearest" });
                  button.click();
                  return true;
                }
                """
            )
            if clicked:
                return
        except PlaywrightError:
            pass
        raise RuntimeError("Could not find the Review control in the incorrect-question row.")

    def scrape_review_page(self, page: Page, test_name: str, row_meta: dict[str, str]) -> WrongQuestionRecord:
        self.wait_for_review_screen(page)
        self.ensure_correct_answer_visible(page)
        container = self.review_container(page)
        merged, _text_payload = self.review_parser.parse_container(container, row_meta)
        uid = make_uid(
            test_name=test_name,
            section=merged.get("section", ""),
            module=merged.get("module", ""),
            question_number=merged.get("question_number", ""),
            fallback=merged.get("source_row_text", "")[:30] or page.url,
        )
        paths = self.save_artifacts(container, uid)
        record = WrongQuestionRecord(
            uid=uid,
            scraped_at=utc_now(),
            test_name=test_name,
            test_number=detect_test_number(test_name),
            section=merged.get("section", ""),
            subject_bucket=detect_subject(merged),
            module=merged.get("module", ""),
            question_number=merged.get("question_number", ""),
            domain=merged.get("domain", ""),
            skill=merged.get("skill", ""),
            my_answer=merged.get("my_answer", ""),
            correct_answer=merged.get("correct_answer", ""),
            question_text=merged.get("question_text", ""),
            question_html=merged.get("question_html", ""),
            answer_choices=merged.get("answer_choices", []),
            answer_choices_html=merged.get("answer_choices_html", []),
            explanation=merged.get("explanation", ""),
            explanation_html=merged.get("explanation_html", ""),
            images=paths["images"],
            screenshot_path=paths["screenshot"],
            html_snapshot_path=paths["html"],
            source_row_text=merged.get("source_row_text", ""),
        )
        return record

    def wait_for_review_screen(self, page: Page) -> None:
        modal = page.locator(".test-questions-modal[aria-hidden='false'] [role='dialog']").first
        try:
            modal.wait_for(state="visible", timeout=4_000)
        except PlaywrightTimeoutError:
            page.wait_for_timeout(1_000)

    def _first_visible(self, candidates: list[Locator]) -> Locator | None:
        for candidate in candidates:
            try:
                if candidate.count() > 0 and candidate.first.is_visible():
                    return candidate.first
            except (PlaywrightError, PlaywrightTimeoutError):
                continue
        return None

    def ensure_correct_answer_visible(self, page: Page) -> None:
        modal = self.review_modal(page)
        if self.review_answer_reveal_visible(modal):
            return

        label = modal.locator("label").filter(has_text=re.compile(r"Show correct answer and explanation", re.IGNORECASE)).first
        checkbox_input = modal.locator("input[type='checkbox']").first
        text_locator = modal.get_by_text(re.compile(r"Show correct answer and explanation", re.IGNORECASE)).first

        for _ in range(3):
            if self.review_answer_reveal_visible(modal):
                return
            clicked = False
            for locator in (label, checkbox_input, text_locator):
                if self.click_first_visible(locator, required=False):
                    clicked = True
                    break
            if not clicked:
                try:
                    clicked = modal.evaluate(
                        """
                        (root) => {
                          const label = Array.from(root.querySelectorAll('label')).find((node) =>
                            /show correct answer and explanation/i.test((node.textContent || '').replace(/\\s+/g, ' ').trim())
                          );
                          if (label) {
                            label.click();
                            return true;
                          }
                          const input = root.querySelector('input[type="checkbox"]');
                          if (input) {
                            input.click();
                            return true;
                          }
                          return false;
                        }
                        """
                    )
                except PlaywrightError:
                    clicked = False
            page.wait_for_timeout(600 if clicked else 300)
        if not self.review_answer_reveal_visible(modal):
            LOG.warning("Could not reveal the correct answer/explanation before scraping this review modal.")

    def review_answer_reveal_visible(self, modal: Locator) -> bool:
        candidates = [
            modal.locator(".answer-panel p.incorrect, .answer-panel p.correct, .answer-panel p.response"),
            modal.locator(".answer-panel h3").filter(has_text=re.compile(r"Rationale", re.IGNORECASE)),
            modal.locator(".answer-panel li.correct"),
        ]
        return self._first_visible(candidates) is not None

    def review_container(self, page: Page) -> Locator:
        candidates = [
            page.locator(".test-questions-modal[aria-hidden='false'] .cb-modal-container"),
            page.locator(".test-questions-modal .cb-modal-container"),
            page.locator(".test-questions-modal[aria-hidden='false'] .question-content"),
            page.locator(".test-questions-modal .question-content"),
            page.locator("main"),
            page.locator("[role='main']"),
            page.locator("article"),
            page.locator("body"),
        ]
        return self._first_visible(candidates) or page.locator("body")

    def save_artifacts(self, container: Locator, uid: str) -> dict[str, Any]:
        screenshot_path = self.screenshot_dir / f"{uid}.png"
        html_path = self.html_dir / f"{uid}.html"
        screenshot_value = str(screenshot_path)
        html_value = str(html_path)
        html_markup = ""
        if self.args.save_question_screenshots:
            ensure_dir(self.screenshot_dir)
            try:
                container.screenshot(path=str(screenshot_path))
            except PlaywrightError as exc:
                LOG.warning("Question screenshot failed for %s: %s", uid, exc)
                self.main_page and self.capture_error(self.main_page, f"screenshot-failure-{uid}")
                screenshot_value = ""
        else:
            screenshot_value = ""
        try:
            html_markup = container.inner_html()
            atomic_write_text(html_path, html_markup)
        except PlaywrightError as exc:
            LOG.warning("HTML snapshot failed for %s: %s", uid, exc)
            html_value = ""

        images: list[str] = []
        if html_markup:
            try:
                images.extend(extract_visual_assets_from_html(uid, html_markup, self.image_dir))
            except OSError as exc:
                LOG.warning("Figure extraction failed for %s: %s", uid, exc)

        images.extend(self.capture_non_svg_figures(container, uid))

        img_locator = container.locator("img")
        try:
            img_count = min(img_locator.count(), 12)
        except PlaywrightError:
            img_count = 0
        for index in range(img_count):
            image = img_locator.nth(index)
            try:
                if not image.is_visible():
                    continue
                path = self.image_dir / f"{uid}-img-{index + 1}.png"
                image.screenshot(path=str(path))
                images.append(str(path))
            except PlaywrightError:
                continue

        return {
            "screenshot": screenshot_value,
            "html": html_value,
            "images": dedupe_preserve_order(images),
        }

    def capture_non_svg_figures(self, container: Locator, uid: str) -> list[str]:
        figure_paths: list[str] = []
        figure_locator = container.locator("figure")
        try:
            figure_count = min(figure_locator.count(), 8)
        except PlaywrightError:
            return figure_paths

        for index in range(figure_count):
            figure = figure_locator.nth(index)
            try:
                if not figure.is_visible():
                    continue
                if figure.locator("svg").count() > 0:
                    continue
                if figure.locator("img").count() > 0:
                    continue
                path = self.image_dir / f"{uid}-figure-{index + 1}.png"
                figure.screenshot(path=str(path))
                figure_paths.append(str(path))
            except PlaywrightError:
                continue
        return figure_paths

    def return_to_questions_overview(self, score_page: Page, review_page: Page) -> None:
        if review_page is not score_page:
            if not review_page.is_closed():
                review_page.close()
            self.wait_for_ready_state(score_page)
            return
        self.close_review_modal(score_page)
        for _ in range(MAX_BACK_ATTEMPTS):
            if self.on_questions_overview(score_page):
                return
            if self.click_by_text(score_page, r"\bBack\b", regex=True, required=False):
                self.wait_for_ready_state(score_page)
                if self.on_questions_overview(score_page):
                    return
            try:
                score_page.go_back(wait_until="domcontentloaded", timeout=4_000)
            except PlaywrightTimeoutError:
                pass
            self.wait_for_ready_state(score_page)
            if self.on_questions_overview(score_page):
                return
        raise RuntimeError("Could not return to Questions Overview after scraping a review page.")

    def current_test_name(self, page: Page) -> str:
        try:
            heading = page.evaluate(
                """
                () => {
                  __JS_NORMALIZE__
                  for (const el of document.querySelectorAll("h1, h2, h3, [role='heading']")) {
                    const text = normalize(el.innerText);
                    if (/practice test/i.test(text)) {
                      const match = text.match(/(?:SAT\\s+)?Practice Test\\s*#?\\s*\\d+/i);
                      return normalize(match ? match[0] : text);
                    }
                  }
                  return "";
                }
                """.replace("__JS_NORMALIZE__", _JS_NORMALIZE)
            )
        except PlaywrightError:
            heading = ""
        return normalize_space(heading) or normalize_space(page.title())

    def click_with_possible_popup(
        self,
        page: Page,
        action: Any,
        description: str,
    ) -> Page:
        assert self.context is not None
        try:
            with self.context.expect_page(timeout=2_500) as popup_info:
                action()
            popup = popup_info.value
            popup.set_default_timeout(self.args.timeout_ms)
            self.wait_for_ready_state(popup)
            return popup
        except PlaywrightTimeoutError:
            LOG.debug("No popup opened for %s; assuming same-tab navigation.", description)
            self.wait_for_ready_state(page)
            return page

    def review_modal(self, page: Page) -> Locator:
        visible_modal = page.locator(".test-questions-modal[aria-hidden='false']").first
        try:
            if visible_modal.count() > 0:
                return visible_modal
        except PlaywrightError:
            pass
        return page.locator(".test-questions-modal").first

    def close_review_modal(self, page: Page) -> None:
        closed = False
        candidates = [
            page.locator(".test-questions-modal [data-cb-modal-close='true']"),
            page.get_by_role("button", name=re.compile(r"Close", re.IGNORECASE)),
        ]
        for locator in candidates:
            if self.click_first_visible(locator, required=False):
                closed = True
                break
        if not closed:
            try:
                page.evaluate(
                    """
                    () => {
                      const buttons = Array.from(document.querySelectorAll(".test-questions-modal [data-cb-modal-close='true']"));
                      for (const button of buttons) {
                        const style = window.getComputedStyle(button);
                        const rect = button.getBoundingClientRect();
                        if (style.display === "none" || style.visibility === "hidden" || rect.width === 0 || rect.height === 0) continue;
                        button.click();
                        return true;
                      }
                      return false;
                    }
                    """
                )
            except PlaywrightError:
                pass
            try:
                page.keyboard.press("Escape")
            except PlaywrightError:
                pass
        try:
            page.locator(".test-questions-modal[aria-hidden='false']").first.wait_for(
                state="hidden",
                timeout=2_000,
            )
        except PlaywrightTimeoutError:
            page.wait_for_timeout(300)

    def click_by_text(
        self,
        page: Page,
        text: str,
        *,
        regex: bool = False,
        required: bool = True,
    ) -> bool:
        pattern = re.compile(text, flags=re.IGNORECASE) if regex else re.compile(re.escape(text), flags=re.IGNORECASE)
        locators = [
            page.get_by_role("button", name=pattern),
            page.get_by_role("link", name=pattern),
            page.get_by_text(pattern),
        ]
        for locator in locators:
            if self.click_first_visible(locator, required=False):
                return True
        try:
            clicked = page.evaluate(
                """
                ({ needle, useRegex }) => {
                  __JS_NORMALIZE__
                  const matcher = useRegex ? new RegExp(needle, "i") : null;
                  const candidates = Array.from(document.querySelectorAll("a, button, [role='button'], [role='link']"));
                  for (const el of candidates) {
                    const text = normalize(el.innerText);
                    if (!text) continue;
                    const matches = useRegex ? matcher.test(text) : text.toLowerCase().includes(needle.toLowerCase());
                    if (!matches) continue;
                    el.click();
                    return true;
                  }
                  return false;
                }
                """.replace("__JS_NORMALIZE__", _JS_NORMALIZE),
                {"needle": text, "useRegex": regex},
            )
            if clicked:
                return True
        except PlaywrightError:
            pass
        if required:
            raise RuntimeError(f"Could not click text matching {text!r}.")
        return False

    def click_first_visible(self, locator: Locator, *, required: bool) -> bool:
        try:
            count = locator.count()
        except PlaywrightError:
            if required:
                raise
            return False
        for index in range(min(count, 6)):
            candidate = locator.nth(index)
            try:
                if not candidate.is_visible():
                    continue
                candidate.scroll_into_view_if_needed(timeout=2_000)
                candidate.click(timeout=2_500)
                return True
            except (PlaywrightError, PlaywrightTimeoutError):
                continue
        if required:
            raise RuntimeError("Could not click a visible locator.")
        return False

    def safe_inner_text(self, locator: Locator) -> str:
        try:
            return normalize_space(locator.inner_text(timeout=1_500))
        except (PlaywrightError, PlaywrightTimeoutError):
            return ""

    def capture_error(self, page: Page, label: str) -> str:
        if not self.args.save_error_screenshots:
            return ""
        ensure_dir(self.error_dir)
        path = self.error_dir / f"{slugify(label)}-{int(time.time())}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
        except PlaywrightError:
            return ""
        return str(path)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def load_records_from_json(json_path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{json_path} does not contain a JSON list.")
    return {item["uid"]: item for item in payload if isinstance(item, dict) and item.get("uid")}


def rehydrate_records_from_snapshots(args: argparse.Namespace, outputs: OutputManager) -> None:
    pending = [
        record
        for record in outputs.records.values()
        if normalize_space(record.get("html_snapshot_path", ""))
    ]
    if not pending:
        return

    parser = ReviewParser()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            for record in pending:
                html_path = Path(record["html_snapshot_path"])
                if not html_path.exists():
                    continue
                html_fragment = html_path.read_text(encoding="utf-8", errors="ignore")
                page.set_content(f"<!DOCTYPE html><html><body>{html_fragment}</body></html>", wait_until="domcontentloaded")
                merged, _text_payload = parser.parse_container(page.locator("body"), record)
                for key, value in merged.items():
                    if value:
                        record[key] = value
        finally:
            page.close()
            browser.close()


def main() -> int:
    configure_logging()
    args = parse_args()

    if args.rebuild_from_json:
        json_path = Path(args.rebuild_from_json)
        if not json_path.exists():
            LOG.error("Rebuild source %s does not exist.", json_path)
            return 1
        try:
            outputs = OutputManager(Path(args.outputs_dir), fresh=True)
            outputs.records = load_records_from_json(json_path)
            rehydrate_records_from_snapshots(args, outputs)
            outputs.finalize()
        except Exception as exc:  # noqa: BLE001
            LOG.exception("Rebuild failed: %s", exc)
            return 1
        LOG.info(
            "Rebuilt outputs from %s. Wrote %d records to %s.",
            json_path,
            len(outputs.records),
            outputs.json_path,
        )
        return 0

    outputs = OutputManager(Path(args.outputs_dir), fresh=args.fresh)
    scraper = SatBluebookScraper(args, outputs)
    try:
        scraper.run()
        outputs.finalize()
    except KeyboardInterrupt:
        LOG.warning("Interrupted by user.")
        return 130
    except Exception as exc:  # noqa: BLE001
        LOG.exception("Scrape failed: %s", exc)
        return 1
    LOG.info(
        "Finished. Wrote %d records to %s.",
        len(outputs.records),
        outputs.json_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
