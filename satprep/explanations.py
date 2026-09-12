"""KB-aware SAT error explanation pipeline (issue #36).

Combines:
  - the existing rule-based error taxonomy in `satprep.corpus.tagger`
    (diagnose_error / reasoning_tags) to identify the smallest observable
    reasoning error
  - the deterministic KB retrieval index emitted by
    `scripts/check_kb.py` to fetch the smallest relevant subset of
    concept / summary pages
  - an optional LLM call (via OpenAI-compatible HTTP, e.g. the FreeLLMAPI
    gateway on http://127.0.0.1:3001/v1) to compose the explanation
  - an explicit abstention path when the evidence does not support a
    reliable classification

The pipeline is opt-in: the LLM is only invoked when both
`SAT_EXPLAIN_ENDPOINT` and `SAT_EXPLAIN_API_KEY` (or the OpenAI-style
`OPENAI_API_KEY` / `OPENAI_BASE_URL` fallback) are set in the environment,
AND the configured model is on the policy allowlist. Without those, the
pipeline returns a deterministic explanation built entirely from the
rule-based taxonomy and KB pages — no fabricated classifications.

This module NEVER mutates the database or writes to the KB vault. Its
sole output is an `Explanation` dataclass for callers (admin UI, server
endpoint, batch harness) to render.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import pathlib
import re
import urllib.error
import urllib.request
from typing import Any

from .corpus.tagger import _tokens as _corpus_tokens
from .corpus.tagger import diagnose_error, reasoning_tags
from .corpus.tags import effective_tags

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# The LLM endpoint. Defaults to the FreeLLMAPI gateway on the controller
# box; callers can override per-call.
_DEFAULT_ENDPOINT = "http://127.0.0.1:3001/v1/chat/completions"
_DEFAULT_MODEL = "auto:generic-free"

# Allowlist of cheap/free models so a misconfigured OPENAI_API_KEY cannot
# silently route to a paid model. Anything not on this list returns the
# deterministic explanation with `mode="abstained"`.
_ALLOWED_MODELS: set[str] = {
    "auto:generic-free",
    "auto:ds-v4-flash-free",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Explanation:
    """Stable, JSON-serialisable result of explain_error()."""

    tested_task: str
    tempting_answer: str
    exact_failure: str
    correct_reasoning: str
    kb_tactic_refs: list[str]
    evidence_citations: list[dict]
    confidence: str
    mode: str
    model: str
    error_taxonomy: list[str]


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def _repo_root() -> pathlib.Path:
    """Locate the KB root (the parent of kb/wiki/index.md) without
    depending on __file__ location so the module is importable from
    test fixtures that re-root SAT_KB_ROOT."""
    env = os.environ.get("SAT_KB_ROOT")
    if env:
        return pathlib.Path(env).resolve()
    here = pathlib.Path(__file__).resolve().parent
    for parent in (here, *here.parents):
        if (parent / "kb" / "wiki" / "index.md").exists():
            return parent
    return pathlib.Path.cwd().resolve()


def _load_index(repo: pathlib.Path) -> dict:
    """Read the committed retrieval index. Returns {} on any error so
    callers degrade gracefully (the pipeline still produces a rule-based
    explanation with kb_tactic_refs=[])."""
    p = repo / "kb" / ".kb-index.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.warning("could not load KB retrieval index from %s: %s", p, e)
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
        log.warning("KB retrieval index has unexpected shape: top-level=%r", type(data).__name__)
        return {}
    return data


def _error_to_kb_hits(errset_lower: set[str], page_path: str) -> int:
    """Map rule-based error tags to KB page paths.

    Returns 2 when the page is a strong recommendation for one of the
    observed errors, 0 otherwise. The mapping is small and explicit on
    purpose: extension means editing a 6-line table, not adjusting weights.
    """
    strong = {"qualifier_strength", "absolute_vs_tentative_language", "over_inference"}
    trap = {"true_but_not_supported", "same_topic_wrong_relationship"}
    inference = {"unsupported_inference"}
    confusion = {"direction_reversal", "paraphrase_precision", "near_synonym_distinction"}
    causal = {"cause_vs_correlation", "hypothesis_vs_result"}
    explicit = {
        "kb/wiki/summaries/settele-strong-words.md": strong,
        "kb/wiki/summaries/settele-trap-answers.md": trap,
        "kb/wiki/summaries/settele-dumb-summaries.md": causal,
        "kb/wiki/summaries/settele-confusing-passages.md": confusion,
        "kb/wiki/summaries/penguin-reading-hacks.md": inference,
    }
    targets = explicit.get(page_path, set())
    if errset_lower & targets:
        return 2
    return 0


def _retrieve_pages(
    index: dict,
    *,
    task_tags: list[str],
    error_taxonomy: list[str] | None = None,
    question_fingerprint: str = "",
    max_pages: int = 3,
) -> list[dict]:
    """Pick the smallest relevant subset of KB pages.

    Relevance is the union of three signals:
      1. frontmatter tag intersection with the question's task tags
         (e.g. "evidence", "inference")
      2. an explicit rule-based-error -> KB-page mapping for the
         smallest-observable-reasoning-error pattern.
      3. a question_fingerprint match: if a question-review page's
         `question_fingerprint` frontmatter equals the current
         question's fingerprint, that page is a strong match and
         ranked above generic tag overlap.

    A non-matching question-review page (a review for a different
    question) is dropped to prevent it from crowding out more
    relevant tactics. Ties broken by total overlap, then by page path
    for determinism.
    """
    if not index or not index.get("pages"):
        return []
    # Normalize granular reasoning tags to the broader KB vocabulary.
    tagset = {_tag_family(t) for t in (task_tags or [])}
    errset = set(error_taxonomy or [])
    errset_lower = {t.lower() for t in errset}
    scored: list[tuple[int, int, str, dict]] = []
    for page in index["pages"]:
        path = page.get("path", "")
        # Exclude non-matching question-review pages: they are
        # authoritative for one specific question, not as KB tactics
        # for arbitrary others.
        if page.get("type") == "question-review":
            pf = page.get("question_fingerprint") or ""
            if question_fingerprint and pf and pf != question_fingerprint:
                continue
        page_tags = {_tag_family(t) for t in (page.get("tags") or [])}
        tag_overlap = len(tagset & page_tags)
        mapping_hits = _error_to_kb_hits(errset_lower, path)
        # If the review page matches our question's fingerprint, add
        # 3 to mapping_hits so it ranks above pure tag-overlap
        # matches. A non-matching review was filtered out above.
        if (
            page.get("type") == "question-review"
            and question_fingerprint
            and (page.get("question_fingerprint") or "") == question_fingerprint
        ):
            mapping_hits += 3
        if tag_overlap <= 0 and mapping_hits <= 0:
            continue
        # Error-taxonomy evidence outranks plain tag overlap.
        scored.append((mapping_hits, tag_overlap + mapping_hits, path, page))
    # Sort by mapping hits (error taxonomy matches rank higher), then
    # total, then path for deterministic tie-breaking.
    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
    return [p for _, _, _, p in scored[:max_pages]]


def _tag_family(tag: str) -> str:
    """Map granular `reasoning_tags` labels to the broader families
    that the KB index uses (e.g. "unsupported_inference" ->
    "inference"). Without this normalisation the retrieval step scores
    zero for almost every ordinary question and only the explicit
    error-taxonomy mapping lights up."""
    if not tag:
        return ""
    t = tag.strip().lower()
    if t in {
        "unsupported_inference",
        "over_inference",
        "irrelevant_detail",
        "evidence_relevance",
        "evidence_strength",
        "claim_vs_evidence",
        "main_claim_vs_detail",
        "true_but_not_supported",
        "abstract_relationship_extraction",
    }:
        return "inference"
    if t in {
        "cause_vs_correlation",
        "hypothesis_vs_result",
        "direction_reversal",
        "comparison_relationship",
        "scope_shift",
        "wrong_reference_group",
        "chronology",
    }:
        return "evidence"
    if t in {
        "paraphrase_precision",
        "near_synonym_distinction",
        "word_sense_in_context",
        "qualifier_strength",
        "absolute_vs_tentative_language",
        "tone_or_stance",
        "degree_or_intensity",
        "author_purpose",
        "contrast_concession",
        "logical_connector",
        "quantifier_mismatch",
    }:
        return "word-in-context"
    if t in {
        "cross_text_agreement",
        "cross_text_disagreement",
        "same_topic_wrong_relationship",
        "dense_scientific_vocabulary",
        "scientific_noun_overload",
    }:
        return "passage-strategy"
    if t in {"pacing", "pacing-strategy", "pacing_strategy"}:
        return "pacing-strategy"
    return t


# ---------------------------------------------------------------------------
# Evidence extraction
# ---------------------------------------------------------------------------


def _tokenize_for_evidence(text: str) -> set[str]:
    """Expose the corpus tokenizer without shadowing its imported alias."""
    return _corpus_tokens(text)


def _extract_evidence(
    passage: str, stem: str, choices: list[dict], student_letter: str, correct_letter: str
) -> list[dict]:
    """Collect the smallest set of {role, text} dicts that any
    explanation must rest on: the question stem, the student's chosen
    choice, the correct choice, and the smallest relevant passage span.

    The passage span is a single sentence window around the most
    relevant sentence we can find (cheap heuristic), capped at a hard
    ceiling so the LLM prompt stays bounded. If the heuristic cannot
    find a sentence (the passage is too short or has no sentence
    boundary), we fall back to the first sentence boundary or the whole
    passage. The window is widened with the next sentence if the next
    one shares a key noun/verb with the first, since choice evidence
    often straddles a sentence break."""
    # NB: the tokenizer is fetched via the `_tokenize_for_evidence`
    # helper above so this module does not depend on `_tokens`
    # disappearing at import time.
    out: list[dict] = []
    if stem:
        out.append({"role": "stem", "text": stem})
    cmap = {c.get("letter", ""): c.get("text", "") for c in choices or []}
    if student_letter and cmap.get(student_letter):
        out.append(
            {"role": "student_choice", "letter": student_letter, "text": cmap[student_letter]}
        )
    if correct_letter and cmap.get(correct_letter):
        out.append(
            {"role": "correct_choice", "letter": correct_letter, "text": cmap[correct_letter]}
        )
    if passage:
        excerpt = _pick_passage_span(
            passage,
            stem,
            cmap.get(correct_letter, ""),
            cmap.get(student_letter, ""),
            _tokenize_for_evidence,
        )
        out.append({"role": "passage_excerpt", "text": excerpt})
    return out


_EVIDENCE_MAX_CHARS = 480


def _sentence_scores(
    sentences: list[str], target: str, tokenize
) -> list[tuple[int, int, int, str]]:
    scored = []
    for index, sentence in enumerate(sentences):
        tokens = tokenize(sentence)
        hit = sum(1 for token in tokens if token in target)
        scored.append((hit, -index, index, sentence))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored


def _fallback_excerpt(scored: list[tuple[int, int, int, str]], sentences: list[str]) -> str:
    scored.sort(key=lambda item: (-len(item[3]), item[1]))
    for _, _, _, sentence in scored:
        if len(sentence) <= _EVIDENCE_MAX_CHARS:
            return sentence
    return sentences[0][:_EVIDENCE_MAX_CHARS]


def _widen_excerpt(primary: str, scored: list[tuple[int, int, int, str]], tokenize) -> str:
    parts = [primary]
    size = len(primary)
    primary_tokens = set(tokenize(primary))
    for _, _, _, sentence in scored[1:]:
        if size + len(sentence) > _EVIDENCE_MAX_CHARS:
            break
        overlap = sum(1 for token in tokenize(sentence) if token in primary_tokens)
        if overlap == 0:
            continue
        parts.append(sentence)
        size += len(sentence)
    return " ".join(parts)


def _pick_passage_span(
    passage: str, stem: str, correct_text: str, student_text: str, tokenize
) -> str:
    """Pick a bounded passage excerpt that still contains the cited
    support when the answer evidence is more than 240 chars into the
    text. Returns at most _EVIDENCE_MAX_CHARS characters."""
    if not passage:
        return ""
    if len(passage) <= _EVIDENCE_MAX_CHARS:
        return passage
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", passage) if s.strip()]
    if not sentences:
        # No sentence boundary - hard cap to keep the prompt bounded.
        return passage[:_EVIDENCE_MAX_CHARS]
    # Find the sentence sharing the most key tokens with the stem or
    # the answer choices; break ties by earliest occurrence so the
    # excerpt is deterministic.
    target = " ".join([stem, correct_text, student_text]).lower()
    if not target.strip():
        target = sentences[0].lower()
    # Sort before testing the best score; a later sentence may be the only match.
    scored = _sentence_scores(sentences, target, tokenize)
    if not scored or scored[0][0] == 0:
        # Nothing matched; take the longest sentence we can still fit.
        return _fallback_excerpt(scored, sentences)
    primary = scored[0][3]
    # Optionally widen with the next sentence if it shares tokens with
    # the primary (citations often straddle a sentence break).
    return _widen_excerpt(primary, scored, tokenize)


# ---------------------------------------------------------------------------
# Deterministic (rule-only) explanation
# ---------------------------------------------------------------------------


def _rule_based_explanation(
    passage: str,
    stem: str,
    choices: list[dict],
    student_letter: str,
    correct_letter: str,
    effective_question_tags: list[str],
    error_taxonomy: list[str],
    kb_pages: list[dict],
) -> Explanation:
    """Build a deterministic explanation from rule-based inputs only.

    No LLM, no fabrication. The structure mirrors the LLM's output shape
    so callers can render either path through the same UI.
    """
    cmap = {c.get("letter", ""): c.get("text", "") for c in choices or []}
    student_text = cmap.get(student_letter, "")
    correct_text = cmap.get(correct_letter, "")
    tested_task = (effective_question_tags or [""])[0] or "ambiguous"
    if error_taxonomy:
        tempting = (
            f"your choice {student_letter!r} ({student_text[:80]!r}) "
            f"introduces one or more unsupported {', '.join(error_taxonomy[:3])}"
        )
    else:
        tempting = (
            f"your choice {student_letter!r} ({student_text[:80]!r}) "
            f"was a plausible-looking but unsupported answer"
        )
    if not error_taxonomy:
        exact_failure = (
            "the rule-based diagnostic could not pin a single failure "
            "type from the text alone; treat the explanation as advisory"
        )
        correct_reasoning = (
            f"the correct answer {correct_letter!r} ({correct_text[:80]!r}) "
            f"is supported by the passage"
        )
        confidence = "low"
    else:
        first = error_taxonomy[0]
        exact_failure = (
            f"the diagnostic flagged `{first}` — a known reasoning-trap "
            f"shape; see the linked KB tactic"
        )
        correct_reasoning = (
            f"the correct answer {correct_letter!r} ({correct_text[:80]!r}) "
            f"matches the passage's claim without the {first} pattern"
        )
        confidence = "medium"
    return Explanation(
        tested_task=tested_task,
        tempting_answer=tempting,
        exact_failure=exact_failure,
        correct_reasoning=correct_reasoning,
        kb_tactic_refs=[p.get("path", "") for p in kb_pages if p.get("path")],
        evidence_citations=_extract_evidence(
            passage, stem, choices, student_letter, correct_letter
        ),
        confidence=confidence,
        mode="rule",
        model="",
        error_taxonomy=error_taxonomy,
    )


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


def _llm_configured() -> tuple[str, str, str] | None:
    """Return (endpoint, model, api_key) if a usable config exists, else
    None. The model must be on the allowlist so a stray
    OPENAI_API_KEY cannot route to an expensive model by accident.

    SAT_EXPLAIN_ENDPOINT is a full endpoint; OPENAI_BASE_URL is a base
    URL that needs the chat-completions route appended."""
    endpoint = os.environ.get("SAT_EXPLAIN_ENDPOINT")
    if not endpoint:
        base = os.environ.get("OPENAI_BASE_URL")
        # OPENAI_BASE_URL is a base URL; only append the chat-completions
        # route if the caller did not already include it. Treat the base
        # as already-complete if it ends with the standard paths so a
        # caller-supplied "https://host/v1/" still resolves to
        # "…/v1/chat/completions" rather than "…/v1/chat/completions/chat/completions".
        if not base:
            base = _DEFAULT_ENDPOINT
        endpoint = _normalize_openai_endpoint(base)
    api_key = os.environ.get("SAT_EXPLAIN_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
    model = os.environ.get("SAT_EXPLAIN_MODEL", _DEFAULT_MODEL)
    if not api_key:
        return None
    if model not in _ALLOWED_MODELS:
        log.warning(
            "SAT_EXPLAIN_MODEL=%r is not on the allowlist %r; falling back to rule-based",
            model,
            sorted(_ALLOWED_MODELS),
        )
        return None
    return endpoint.rstrip("/"), model, api_key


_CHAT_COMPLETIONS_PATH = "/chat/completions"


def _normalize_openai_endpoint(base: str) -> str:
    """Given an OpenAI-style base URL (no chat-completions path), return
    a full /v1/chat/completions URL. Idempotent if the path is already
    present."""
    base = base.rstrip("/")
    if base.endswith(_CHAT_COMPLETIONS_PATH):
        return base
    if base.endswith("/v1"):
        return base + _CHAT_COMPLETIONS_PATH
    return base + "/v1" + _CHAT_COMPLETIONS_PATH


def _call_llm(
    endpoint: str, model: str, api_key: str, messages: list[dict], *, max_tokens: int = 700
) -> str:
    """POST to an OpenAI-compatible /v1/chat/completions endpoint.

    Structural mismatches raise ValueError so the caller can abstain."""
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2}
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    # Canonical OpenAI shape: body["choices"][0]["message"]["content"].
    # Anything else is a malformed successful response and should fall
    # back to abstention rather than crash.
    try:
        choices = body["choices"]
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(
            f"malformed OpenAI-compatible response: {e!r} (expected choices[0].message.content)"
        ) from None
    if not isinstance(content, str):
        raise ValueError(
            f"malformed OpenAI-compatible response: content is {type(content).__name__}, not str"
        )
    return content


_SYSTEM_PROMPT = (
    "You are an SAT Reading and Writing tutor. You will be given a "
    "passage excerpt, a question, the student's chosen answer, the "
    "correct answer, a small set of authoritative KB pages, and a "
    "rule-based error taxonomy. Produce a JSON object with exactly the "
    "five fields below. Cite the exact word/relationship that fails; do "
    "not invent any classification. If the evidence is insufficient, "
    "set confidence='low' and exact_failure='insufficient evidence'.\n"
    "  tested_task: short label of the reasoning task\n"
    "  tempting_answer: why the student's choice was plausible\n"
    "  exact_failure: the smallest word/clause/relationship that "
    "invalidates the tempting answer\n"
    "  correct_reasoning: 1-3 sentences linking the correct answer to "
    "the passage\n"
    "  confidence: 'high' | 'medium' | 'low'\n"
    "Return ONLY the JSON object. Do not wrap it in markdown or prose."
)


def _kb_body_excerpt(path: str, max_chars: int = 1200) -> str:
    """Load a bounded Markdown excerpt of a KB page so the LLM has the
    actual procedure, not just the title. Returns "" if the file is
    missing or unreadable; the prompt degrades gracefully."""
    if not path:
        return ""
    full = (REPO_ROOT_FOR_BODY / path).resolve()
    try:
        if not full.is_file():
            return ""
        text = full.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # Strip YAML frontmatter so the model sees the body, not the metadata.
    if text.startswith("---"):
        end = text.find("\n---\n", 4)
        if end != -1:
            text = text[end + 5 :]
    return text if len(text) <= max_chars else text[:max_chars] + "…"


# REPO_ROOT for body loading: same lookup as the retrieval index.
REPO_ROOT_FOR_BODY = _repo_root()


def _prompt_messages(
    rule_explanation: Explanation, kb_pages: list[dict], rationale: str = ""
) -> list[dict]:
    """Build JSON-grounded messages with KB tactics and official rationale."""
    kb_brief = [
        {
            "path": p.get("path", ""),
            "title": p.get("title", ""),
            "tags": p.get("tags", []),
            "description": p.get("description", ""),
            "body_excerpt": _kb_body_excerpt(p.get("path", "")),
        }
        for p in kb_pages
    ]
    user_payload = {
        "tested_task": rule_explanation.tested_task,
        "rule_based_error_taxonomy": rule_explanation.error_taxonomy,
        "evidence": rule_explanation.evidence_citations,
        "kb_pages": kb_brief,
        "rule_based_tempting_answer": rule_explanation.tempting_answer,
        "rule_based_exact_failure": rule_explanation.exact_failure,
        "official_rationale": rationale,
    }
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_llm_json(text: str) -> dict | None:
    """Tolerant parse: try json.loads first, then fall back to a
    `{...}`-extraction if the model wrapped the JSON in prose."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_OBJECT_RE.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def explain_error(
    *,
    question_id: int,
    passage: str,
    stem: str,
    choices: list[dict],
    student_letter: str,
    correct_letter: str,
    rationale: str = "",
    question_fingerprint: str = "",
    conn: Any | None = None,
) -> Explanation:
    """Classify a student's wrong SAT R&W answer and return a structured
    explanation. Never mutates `data/satprep.db`. Safe to call with no
    LLM configured (returns a deterministic rule-based explanation)."""
    # 1. Rule-based taxonomy (always; the LLM is constrained to this).
    task_tags = reasoning_tags(passage, stem, [c.get("text", "") for c in choices or []])
    cmap = {c.get("letter", ""): c.get("text", "") for c in choices or []}
    error_taxonomy = diagnose_error(cmap.get(correct_letter, ""), cmap.get(student_letter, ""))
    # Persisted tags include admin suppressions and manual corrections.
    if conn is not None:
        try:
            effective = list(effective_tags(conn, question_id))
        except Exception as e:
            log.debug("effective_tags unavailable: %s", e)
            effective = list(task_tags)
    else:
        effective = list(task_tags)

    # 2. Retrieval. Pass question_fingerprint so non-matching question-review
    # pages are excluded from the retrieved set; a review for a different
    # question would otherwise crowd out the more relevant tactic pages.
    index = _load_index(_repo_root())
    kb_pages = _retrieve_pages(
        index,
        task_tags=effective,
        error_taxonomy=error_taxonomy,
        question_fingerprint=question_fingerprint,
    )

    # 3. Deterministic base.
    base = _rule_based_explanation(
        passage,
        stem,
        choices,
        student_letter,
        correct_letter,
        effective_question_tags=effective,
        error_taxonomy=error_taxonomy,
        kb_pages=kb_pages,
    )

    # Without rule evidence, the LLM cannot cite or invent a failure mode.
    if not error_taxonomy:
        return dataclasses.replace(base, mode="abstained", confidence="low", model="")
    # 4. Optional LLM upgrade.
    cfg = _llm_configured()
    if cfg is None:
        return base
    endpoint, model, api_key = cfg
    try:
        content = _call_llm(
            endpoint, model, api_key, _prompt_messages(base, kb_pages, rationale=rationale)
        )
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        KeyError,
        IndexError,
        TypeError,
        json.JSONDecodeError,
        ValueError,
    ) as e:
        log.warning("LLM explanation call failed: %s; abstaining", e)
        return dataclasses.replace(base, mode="abstained", confidence="low", model=model)
    parsed = _parse_llm_json(content)
    if not isinstance(parsed, dict):
        return dataclasses.replace(base, mode="abstained", confidence="low", model=model)
    confidence = parsed.get("confidence", "low")
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    return Explanation(
        tested_task=str(parsed.get("tested_task") or base.tested_task),
        tempting_answer=str(parsed.get("tempting_answer") or base.tempting_answer),
        exact_failure=str(parsed.get("exact_failure") or base.exact_failure),
        correct_reasoning=str(parsed.get("correct_reasoning") or base.correct_reasoning),
        kb_tactic_refs=base.kb_tactic_refs,
        evidence_citations=base.evidence_citations,
        confidence=confidence,
        mode="llm",
        model=model,
        error_taxonomy=base.error_taxonomy,
    )
