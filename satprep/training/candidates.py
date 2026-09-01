"""The Candidate type: shared vocabulary between scoring and composition.

A leaf, so `sampler` (which builds and scores candidates) and `composition`
(which buckets them) can both depend on it without depending on each other.

`question` is a hydrated `corpus.questions.Question`, not a sqlite3.Row -
scoring and bucket rules read named attributes, so a typo is an
AttributeError here rather than a KeyError several frames away.
"""


def row_field(state, key: str, default=None):
    """Read a field from sqlite3.Row or a duck-typed state object."""
    if state is None:
        return default
    try:
        return state[key]
    except (KeyError, TypeError, IndexError):
        return getattr(state, key, default)


class Candidate:
    __slots__ = ("components", "hist_correct", "question", "score", "state", "tags")

    def __init__(self, question, tags):
        self.question = question
        self.tags = tags
        self.components: list[tuple[str, float]] = []
        self.score = 0.0
        self.state = None
        self.hist_correct = None  # True/False from scraped history, else None

    def add(self, label: str, value: float) -> None:
        if abs(value) > 1e-9:
            self.components.append((label, round(value, 2)))
            self.score += value
