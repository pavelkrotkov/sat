"""Stable content fingerprints for duplicate detection across sources.

The fingerprint is a sha256 over a normalized concatenation of passage,
question stem, and the answer-choice texts (order preserved, letters
stripped). Normalization collapses whitespace and unicode variants so the
same question arriving via PDF text extraction, HTML snapshots, or CSV
exports collides as intended.
"""

import hashlib
import re
import unicodedata

from . import config

_WS = re.compile(r"\s+")
_PUNCT_SPACE = re.compile(r"\s([,.!?;:])")


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value)
    # unify typographic quotes/dashes that vary between exports
    table = str.maketrans({
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
        "\u2013": "-", "\u2014": "-", "\u2212": "-",
        "\u00a0": " ",
    })
    text = text.translate(table)
    text = _WS.sub(" ", text).strip().lower()
    text = _PUNCT_SPACE.sub(r"\1", text)
    return text


def fingerprint(passage: str | None, stem: str | None, choices: list[str]) -> str:
    payload_parts = [
        "P:" + normalize_text(passage),
        "Q:" + normalize_text(stem),
        "C:" + "|".join(normalize_text(c) for c in choices),
    ]
    payload = "\n".join(payload_parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def pool_for_fingerprint(fp: str) -> str:
    """Deterministic 75/25 fresh split (spec section 3).

    Same fingerprint always lands in the same pool regardless of import
    order or reruns.
    """
    digest = int(hashlib.sha256(("pool:" + fp).encode()).hexdigest(), 16)
    return "protected_benchmark" if digest % config.PROTECTED_MOD == config.PROTECTED_TARGET else "fresh_training"


def fingerprint_loose(passage: str | None, stem: str | None) -> str:
    """Content hash ignoring answer choices.

    Used ONLY to reconcile choice-less records (Bluebook omits options on
    correct-answer reviews) against full official bank items. Same normalized
    passage + stem is treated as the same question.
    """
    payload = "\n".join([
        "P:" + normalize_text(passage),
        "Q:" + normalize_text(stem),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
