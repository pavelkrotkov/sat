"""The Question value: one hydration point for stored corpus rows.

Five modules used to decode `choices_json` independently, and a `sqlite3.Row`
travelled all the way from the database into the Jinja templates. A missing
column then surfaced as an `IndexError` deep inside a comprehension rather
than at the seam - which is exactly how the dashboard crash in #10 stayed
hidden.

Deliberately NOT the same type as `parse_snapshot.ParsedQuestion`, despite
the overlap in passage/stem/choices. That one is a parse result from a
single source format: it carries `section`, `question_number` and
`student_letter`, has no id, fingerprint or pool, and exists only until
ingestion turns it into a Question plus an attempt. `student_letter` in
particular is training data, and folding it in here would drag a training
concept into the corpus entity that satprep.training was just separated
from. The conversion lives in `ingest`, which is where the two source
shapes are already reconciled.
"""

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Choice:
    letter: str
    text: str
    is_correct: bool = False

    @classmethod
    def from_dict(cls, raw: dict) -> "Choice":
        return cls(
            letter=str(raw.get("letter", "")),
            text=str(raw.get("text", "")),
            is_correct=bool(raw.get("is_correct", False)),
        )

    def as_dict(self) -> dict:
        return {"letter": self.letter, "text": self.text, "is_correct": self.is_correct}


@dataclass(frozen=True)
class Question:
    """A stored question, with its JSON columns already decoded."""

    id: int
    fingerprint: str
    passage: str
    stem: str
    choices: tuple[Choice, ...]
    correct_letter: str
    pool: str
    official_skill: str = ""
    official_domain: str = ""
    difficulty: str = ""
    rationale: str = ""
    source: str = ""
    source_test: str = ""
    source_question_number: str = ""
    module: str = ""
    skill_source: str = "unknown"
    import_batch: str = ""
    images: tuple[str, ...] = ()
    provenance: dict = field(default_factory=dict)
    is_new_bank: int = 0
    seen_benchmark: int = 0
    active: int = 1

    # ------------------------------------------------------------ derived --

    @property
    def choice_texts(self) -> list[str]:
        return [c.text for c in self.choices]

    @property
    def is_displayable(self) -> bool:
        """Bluebook omits options on correctly-answered reviews, so some
        historical questions are statistics-only and can never be shown."""
        return bool(self.choices)

    def choice(self, letter: str) -> Choice | None:
        letter = (letter or "").strip().upper()[:1]
        return next((c for c in self.choices if c.letter == letter), None)

    def text_of(self, letter: str) -> str:
        found = self.choice(letter)
        return found.text if found else ""

    @property
    def key(self) -> Choice | None:
        return self.choice(self.correct_letter)

    def is_correct_answer(self, letter: str) -> bool:
        return bool(letter) and self.correct_letter.upper() == letter.strip().upper()[:1]

    # ------------------------------------------------------- construction --

    @classmethod
    def from_row(cls, row) -> "Question":
        """Build from a `SELECT * FROM questions` row.

        Missing columns fail here, at the seam, naming the column - rather
        than several modules downstream inside a comprehension.
        """
        def get(name, default=None):
            try:
                value = row[name]
            except (KeyError, IndexError):
                raise KeyError(
                    f"Question.from_row: row has no column {name!r}; "
                    f"select the full questions row, not a projection."
                ) from None
            return default if value is None else value

        return cls(
            id=get("id"),
            fingerprint=get("fingerprint", ""),
            passage=get("passage", ""),
            stem=get("stem", ""),
            choices=tuple(Choice.from_dict(c) for c in json.loads(get("choices_json", "[]") or "[]")),
            correct_letter=get("correct_letter", ""),
            pool=get("pool", ""),
            official_skill=get("official_skill", ""),
            official_domain=get("official_domain", ""),
            difficulty=get("difficulty", ""),
            rationale=get("rationale", ""),
            source=get("source", ""),
            source_test=get("source_test", ""),
            source_question_number=str(get("source_question_number", "")),
            module=get("module", ""),
            skill_source=get("skill_source", "unknown"),
            import_batch=get("import_batch", ""),
            images=tuple(json.loads(get("images_json", "[]") or "[]")),
            provenance=json.loads(get("provenance_json", "{}") or "{}"),
            is_new_bank=int(get("is_new_bank", 0)),
            seen_benchmark=int(get("seen_benchmark", 0)),
            active=int(get("active", 1)),
        )


# ------------------------------------------------------------------ reads --

def load(conn, question_id: int) -> Question | None:
    row = conn.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
    return Question.from_row(row) if row else None


def load_many(conn, question_ids) -> dict[int, Question]:
    """Hydrate a set of ids in one query, keyed by id."""
    ids = list(question_ids)
    if not ids:
        return {}
    qmarks = ",".join("?" for _ in ids)
    rows = conn.execute(f"SELECT * FROM questions WHERE id IN ({qmarks})", ids)
    return {q.id: q for q in (Question.from_row(r) for r in rows)}


def iter_active(conn, pools: tuple[str, ...] | None = None, displayable_only: bool = False):
    """Every active question, optionally restricted to certain pools.

    `displayable_only` filters in SQL rather than in Python so the sampler
    keeps its query-level pool filter - the guarantee that a protected
    benchmark item is never even loaded outside benchmark mode.
    """
    clauses = ["active=1"]
    params: list = []
    if pools is not None:
        clauses.append(f"pool IN ({','.join('?' for _ in pools)})")
        params.extend(pools)
    if displayable_only:
        clauses.append("choices_json != '[]'")
    sql = f"SELECT * FROM questions WHERE {' AND '.join(clauses)}"
    for row in conn.execute(sql, params):
        yield Question.from_row(row)
