"""Parser for saved Bluebook review-page HTML snapshots.

Each snapshot under artifacts/html/ contains a self-contained review modal.
Bluebook renders two DOM layouts depending on whether the question was
answered correctly (issue #49):

    .question-panel  -> h3 heading ("Reading and Writing: Question 7"),
                        passage div(s), a final div holding the stem, and —
                        on correct-answer reviews — the choices
                        (<ol class="answer-options">) with li.correct
                        marking the key
    .answer-panel    -> on incorrect reviews, the <ol type="A"> choices
                        (li.correct marks the key), an optional status line
                        ("You selected answer B. ..."), and a Rationale
                        section

This is the richest available representation of each historical question;
the scraped JSON often lacks answer choices, so ingestion rebuilds from
these files whenever present and falls back to JSON fields otherwise.
"""

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag


@dataclass
class ParsedQuestion:
    section: str = ""
    question_number: str = ""
    passage: str = ""
    stem: str = ""
    choices: list[dict] = field(default_factory=list)  # {letter,text,is_correct}
    correct_letter: str = ""
    student_letter: str = ""
    rationale: str = ""

    @property
    def choice_texts(self) -> list[str]:
        return [c["text"] for c in self.choices]


def _clean(text: str | None) -> str:
    return re.sub(r"[ \t]+", " ", text or "").strip()


def _node_text(node) -> str:
    parts = [re.sub(r"\s+", " ", p.get_text(" ")) for p in node.find_all(["p", "li", "blockquote"])]
    parts = [_clean(p) for p in parts if _clean(p)]
    if parts:
        return "\n".join(parts)
    return _clean(node.get_text(" "))


_SELECTED_RE = re.compile(r"You selected answer\s+([A-H])", re.IGNORECASE)
_CORRECT_IS_RE = re.compile(r"correct answer is\s+([A-H])", re.IGNORECASE)


def _find_choice_list(soup) -> Tag | None:
    """Locate the answer <ol> in either Bluebook modal layout (issue #49).

    Correct reviews keep the choices inside .question-panel in a dedicated
    <ol class="answer-options">; incorrect reviews put a plain <ol> in
    .answer-panel. Only the specifically identified question-panel answer
    list is preferred, since a generic question-panel <ol> often belongs to
    an ordered list inside the passage, not to the choices (T7).
    """
    question_panel = soup.select_one(".question-panel")
    if question_panel is not None:
        found = question_panel.select_one("ol.answer-options")
        if found is not None:
            return found
    answer_panel = soup.select_one(".answer-panel")
    if answer_panel is not None:
        found = answer_panel.find("ol")
        if isinstance(found, Tag):
            return found
    return None


def parse_snapshot(html: str) -> ParsedQuestion:
    soup = BeautifulSoup(html, "html.parser")
    out = ParsedQuestion()

    panel = soup.select_one(".question-panel") or soup
    heading = panel.find("h3")
    if heading:
        m = re.search(r"^(.*?):\s*Question\s*(\d+)", _clean(heading.get_text(" ")), re.IGNORECASE)
        if m:
            out.section = _clean(m.group(1))
            out.question_number = m.group(2)

    # Passage vs stem: every direct child div after the heading; the stem is
    # the last one that looks like a question (ends with ? or starts with a
    # typical directive). Everything earlier is passage material.
    body_divs = [
        d for d in panel.find_all("div", recursive=False) if d is not None and not d.find("h3")
    ]
    stem_idx = -1
    for idx in range(len(body_divs) - 1, -1, -1):
        text = _clean(body_divs[idx].get_text(" "))
        if text:
            stem_idx = idx
            break
    if stem_idx >= 0:
        out.stem = _node_text(body_divs[stem_idx])
        passage_parts = [_node_text(d) for d in body_divs[:stem_idx]]
        out.passage = "\n".join(p for p in passage_parts if p)

    # Choices live in two possible places (issue #49):
    #  - correct reviews: .question-panel ol.answer-options
    #  - incorrect reviews: .answer-panel ol
    # Prefer the question-panel list when present (it is the fuller one on
    # correct reviews); fall back to the answer panel otherwise.
    choice_ol = _find_choice_list(soup)
    if choice_ol is not None:
        start = choice_ol.get("type")
        start_letter = start.upper() if isinstance(start, str) and len(start) == 1 else "A"
        for offset, li in enumerate(choice_ol.find_all("li", recursive=False)):
            classes = li.get("class") or []
            out.choices.append(
                {
                    "letter": chr(ord(start_letter) + offset),
                    "text": _node_text(li),
                    "is_correct": "correct" in classes,
                }
            )
    for c in out.choices:
        if c["is_correct"]:
            out.correct_letter = c["letter"]
            break

    answer_panel = soup.select_one(".answer-panel")
    if answer_panel is None:
        return out

    status_p = answer_panel.find(
        "p",
        class_=lambda c: bool(c) and ("response" in c or "incorrect" in c or "correct" in c),
    )
    if status_p:
        status_text = _clean(status_p.get_text(" "))
        m_sel = _SELECTED_RE.search(status_text)
        if m_sel:
            out.student_letter = m_sel.group(1).upper()
        if not out.correct_letter:
            m_key = _CORRECT_IS_RE.search(status_text)
            if m_key:
                out.correct_letter = m_key.group(1).upper()

    rationale_h3 = next(
        (
            h
            for h in answer_panel.find_all("h3")
            if re.search(r"rationale", _clean(h.get_text(" ")), re.I)
        ),
        None,
    )
    if rationale_h3:
        chunks = []
        for sib in rationale_h3.next_siblings:
            if isinstance(sib, Tag):
                txt = _node_text(sib)
                if txt:
                    chunks.append(txt)
        out.rationale = "\n".join(chunks)

    return out
