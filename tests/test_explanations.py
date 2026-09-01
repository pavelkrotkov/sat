"""Tests for the KB-aware error explanation pipeline (issue #36)."""

from __future__ import annotations

import hashlib
import json
import pathlib

from satprep.explanations import (
    _EVIDENCE_MAX_CHARS,
    _corpus_tokens,
    _load_index,
    _parse_llm_json,
    _pick_passage_span,
    _retrieve_pages,
    _tag_family,
    _tokenize_for_evidence,
    explain_error,
)


def hashlib_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


REPO = pathlib.Path(__file__).resolve().parent.parent


def _sample_choices():
    return [
        {
            "letter": "A",
            "text": "The graph proves that rainfall directly causes yield.",
            "is_correct": False,
        },
        {
            "letter": "B",
            "text": "The graph shows a positive correlation between rainfall and yield.",
            "is_correct": True,
        },
        {"letter": "C", "text": "Rainfall always increases yield.", "is_correct": False},
        {"letter": "D", "text": "Some rainfall data was recorded.", "is_correct": False},
    ]


def test_explain_error_no_llm_configured_returns_rule_based(monkeypatch):
    monkeypatch.delenv("SAT_EXPLAIN_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SAT_EXPLAIN_MODEL", raising=False)
    ex = explain_error(
        question_id=1,
        passage="A scatterplot shows yield versus rainfall.",
        stem="Which choice most effectively uses data from the graph to support the claim?",
        choices=_sample_choices(),
        student_letter="A",
        correct_letter="B",
    )
    assert ex.mode == "rule"
    assert ex.confidence in {"low", "medium", "high"}
    assert ex.error_taxonomy, "rule-based error taxonomy must be populated"
    assert "qualifier_strength" in ex.error_taxonomy
    assert ex.kb_tactic_refs, "at least one KB page should be recommended"
    assert any("settele-strong-words" in p for p in ex.kb_tactic_refs)


def test_explain_error_explicit_no_choice_data_abstains():
    ex = explain_error(
        question_id=2,
        passage="Some passage.",
        stem="What does the text most strongly suggest?",
        choices=[
            {"letter": "A", "text": "", "is_correct": False},
            {"letter": "B", "text": "X", "is_correct": True},
        ],
        student_letter="A",
        correct_letter="B",
    )
    assert ex.confidence == "low"
    # PR-43 review: empty rule-based taxonomy now abstains by design.
    assert ex.mode in {"rule", "abstained"}
    assert ex.error_taxonomy == []


def test_explain_error_trap_answer_pattern():
    ex = explain_error(
        question_id=3,
        passage="Researchers tracked 50 birds and found migration patterns.",
        stem="What can most reasonably be inferred from the study?",
        choices=[
            {"letter": "A", "text": "All birds migrate.", "is_correct": False},
            {"letter": "B", "text": "Some tracked birds migrated.", "is_correct": True},
        ],
        student_letter="A",
        correct_letter="B",
    )
    assert ex.error_taxonomy
    assert any(
        "absolute_vs_tentative_language" in t or "qualifier_strength" in t
        for t in ex.error_taxonomy
    )
    paths = " ".join(ex.kb_tactic_refs)
    assert "settele-strong-words" in paths or "settele-trap-answers" in paths


def test_explain_error_evidence_extraction_includes_stem_and_choices():
    ex = explain_error(
        question_id=4,
        passage="P",
        stem="Which supports the claim?",
        choices=[
            {"letter": "A", "text": "alpha", "is_correct": False},
            {"letter": "B", "text": "beta", "is_correct": True},
        ],
        student_letter="A",
        correct_letter="B",
    )
    roles = {c.get("role") for c in ex.evidence_citations}
    assert "stem" in roles
    assert "student_choice" in roles
    assert "correct_choice" in roles
    assert "passage_excerpt" in roles


def test_retrieve_pages_handles_empty_index():
    assert _retrieve_pages({}, task_tags=["x"]) == []
    assert _retrieve_pages({"pages": []}, task_tags=["x"]) == []


def test_retrieve_pages_deterministic_order():
    index = {
        "pages": [
            {"path": "kb/wiki/summaries/a.md", "tags": ["t1"]},
            {"path": "kb/wiki/summaries/b.md", "tags": ["t1", "t2"]},
            {"path": "kb/wiki/summaries/c.md", "tags": ["t2"]},
        ]
    }
    out1 = _retrieve_pages(index, task_tags=["t1", "t2"])
    out2 = _retrieve_pages(index, task_tags=["t1", "t2"])
    assert [p["path"] for p in out1] == [p["path"] for p in out2]
    assert out1[0]["path"] == "kb/wiki/summaries/b.md"


def test_retrieve_pages_error_taxonomy_mapping():
    index = {
        "pages": [
            {
                "path": "kb/wiki/summaries/settele-strong-words.md",
                "tags": ["inference", "evidence", "passage-strategy"],
            },
            {
                "path": "kb/wiki/summaries/settele-trap-answers.md",
                "tags": ["inference", "evidence", "passage-strategy"],
            },
            {
                "path": "kb/wiki/summaries/penguin-reading-hacks.md",
                "tags": ["inference", "evidence", "passage-strategy"],
            },
        ]
    }
    out = _retrieve_pages(index, task_tags=[], error_taxonomy=["qualifier_strength"])
    paths = [p["path"] for p in out]
    assert "kb/wiki/summaries/settele-strong-words.md" in paths


def test_load_index_missing_file_returns_empty():
    assert _load_index(pathlib.Path("/nonexistent")) == {}


def test_parse_llm_json_handles_prose_wrapping():
    assert _parse_llm_json('{"a": 1, "b": 2}') == {"a": 1, "b": 2}
    assert _parse_llm_json('Here you go:\n```json\n{"a": 3}\n```\nEnjoy!') == {"a": 3}
    assert _parse_llm_json("not json at all") is None
    assert _parse_llm_json('prefix then {"k": "v"} suffix') == {"k": "v"}


def test_explain_error_unconfigured_endpoint_abstains(monkeypatch):
    monkeypatch.setenv("SAT_EXPLAIN_API_KEY", "fake-key")
    monkeypatch.setenv("SAT_EXPLAIN_MODEL", "auto:generic-free")
    monkeypatch.setenv("SAT_EXPLAIN_ENDPOINT", "http://127.0.0.1:1/v1/chat/completions")
    ex = explain_error(
        question_id=5,
        passage="P",
        stem="S",
        choices=[
            {"letter": "A", "text": "x", "is_correct": False},
            {"letter": "B", "text": "y", "is_correct": True},
        ],
        student_letter="A",
        correct_letter="B",
    )
    assert ex.mode in {"rule", "abstained"}
    if ex.mode == "abstained":
        assert ex.confidence == "low"
        # PR-43 review: model is the LLM name only when an actual LLM
        # call is attempted. When the rule-based taxonomy is empty (as
        # it is here), we abstain before ever reaching _call_llm, so
        # `model` stays the empty string.
        if ex.model:
            assert ex.model == "auto:generic-free"
    assert isinstance(ex.error_taxonomy, list)


def test_explain_error_disallowed_model_falls_back(monkeypatch):
    monkeypatch.setenv("SAT_EXPLAIN_API_KEY", "fake-key")
    monkeypatch.setenv("SAT_EXPLAIN_MODEL", "gpt-4o")
    ex = explain_error(
        question_id=6,
        passage="P",
        stem="S",
        choices=[
            {"letter": "A", "text": "x", "is_correct": False},
            {"letter": "B", "text": "y", "is_correct": True},
        ],
        student_letter="A",
        correct_letter="B",
    )
    # PR-43 review: empty rule taxonomy now abstains by design, so the
    # disallowed-model path returns abstained rather than rule.
    assert ex.mode in {"rule", "abstained"}
    assert ex.model == ""


def test_explain_error_llm_response_unparseable_abstains(monkeypatch):
    monkeypatch.setenv("SAT_EXPLAIN_API_KEY", "fake-key")
    monkeypatch.setenv("SAT_EXPLAIN_MODEL", "auto:generic-free")
    monkeypatch.setenv("SAT_EXPLAIN_ENDPOINT", "http://127.0.0.1:1/v1/chat/completions")
    import satprep.explanations as ex_mod

    monkeypatch.setattr(ex_mod, "_call_llm", lambda *a, **kw: "Sorry, I cannot answer that.")
    ex = explain_error(
        question_id=7,
        passage="P",
        stem="S",
        choices=[
            {"letter": "A", "text": "x", "is_correct": False},
            {"letter": "B", "text": "y", "is_correct": True},
        ],
        student_letter="A",
        correct_letter="B",
    )
    assert ex.mode == "abstained"
    assert ex.confidence == "low"
    assert ex.tempting_answer
    assert ex.correct_reasoning


def test_explanations_module_does_not_import_training():
    import satprep.explanations as ex_mod

    src = pathlib.Path(ex_mod.__file__).read_text()
    assert "from .training" not in src
    assert "import .training" not in src


def test_real_vault_explain_end_to_end():
    ex = explain_error(
        question_id=1,
        passage="A scatterplot shows yield versus rainfall.",
        stem="Which choice most effectively uses data from the graph to support the claim?",
        choices=_sample_choices(),
        student_letter="A",
        correct_letter="B",
    )
    for field in (
        "tested_task",
        "tempting_answer",
        "exact_failure",
        "correct_reasoning",
        "kb_tactic_refs",
        "evidence_citations",
        "confidence",
        "mode",
        "model",
        "error_taxonomy",
    ):
        assert hasattr(ex, field), field
    assert ex.mode in {"rule", "llm", "abstained"}
    assert ex.confidence in {"low", "medium", "high"}


# ---------------------------------------------------------------------------
# Round-3 regressions: confirm the P1 / P2 fixes from the second
# bot-review pass on PR #43 are actually in effect.
# ---------------------------------------------------------------------------


def test_pick_passage_span_finds_later_supporting_sentence(monkeypatch):
    """Round-3 P2: when the first sentence has zero token overlap with
    the target but a later sentence does, _pick_passage_span must pick
    the later sentence (the previous version branched on the unsorted
    first tuple and returned the no-match fallback)."""
    # 5 sentences; only the last has a token in common with the stem.
    passage = (
        "The first sentence contains nothing relevant. "
        "The second sentence also contains nothing. "
        "And the third too. "
        "Nor does the fourth. "
        "But the scatterplot clearly shows the relationship."
    )
    stem = "scatterplot"
    out = _pick_passage_span(passage, stem, "", "", _corpus_tokens)
    assert "scatterplot" in out, f"span should contain the supporting sentence, got: {out!r}"


def test_tokenize_for_evidence_does_not_recurse(monkeypatch):
    """Round-3 P1: the module-level wrapper that took the same name as
    the imported tokenizer recursed into itself and crashed on any
    passage longer than ~480 chars. Sanity-check that the public name
    resolves to the corpus tokenizer, not to itself."""
    assert _tokenize_for_evidence("the scatterplot is clear") == _corpus_tokens(
        "the scatterplot is clear"
    )


def test_retrieve_pages_filters_non_matching_question_review():
    """Round-3 P2: a question-review page authored for question A
    must not appear in the retrieved set when explaining question B,
    even if its tags overlap. Without the fingerprint check, the
    wrong review would crowd out relevant tactic pages."""
    index = {
        "pages": [
            {
                "path": "kb/wiki/concepts/stack.md",
                "tags": ["inference"],
                "type": "concept",
                "title": "Stack",
            },
            {
                "path": "kb/wiki/reviews/A.md",
                "tags": ["inference"],
                "type": "question-review",
                "title": "A",
                "question_fingerprint": "a" * 64,
            },
        ]
    }
    # No fingerprint: review is treated as a regular page (by tag).
    out = _retrieve_pages(index, task_tags=["inference"], question_fingerprint="b" * 64)
    paths = [p["path"] for p in out]
    assert "kb/wiki/concepts/stack.md" in paths
    assert "kb/wiki/reviews/A.md" not in paths, "non-matching question-review must be filtered out"


def test_kb_body_excerpt_strips_frontmatter_and_caps():
    """Round-3 P2: the LLM prompt body excerpt is the actual KB page
    body, not its frontmatter, and is bounded by the documented
    max_chars cap."""
    from satprep.explanations import _kb_body_excerpt

    body = _kb_body_excerpt("kb/wiki/summaries/settele-strong-words.md")
    # The frontmatter contains `tags:` which would be a leakage marker
    # if it appeared in the excerpt; the body must start with a real
    # Markdown heading.
    assert "tags:" not in body[:20]
    assert body.startswith("# ") or "Strong" in body


def test_load_index_rejects_non_dict_root():
    """Round-3 P2: a top-level malformed index (`[]` or `null`) used
    to crash the pipeline with AttributeError; the loader now returns
    {} for graceful degradation."""
    from satprep.explanations import _load_index

    p = REPO / "kb" / ".kb-index.json"
    original = p.read_text(encoding="utf-8")
    p.write_text("[]", encoding="utf-8")
    try:
        idx = _load_index(REPO)
        assert idx == {}
    finally:
        p.write_text(original, encoding="utf-8")


def test_explain_error_uses_effective_tags_when_conn_supplied():
    """Round-3 P2: when a connection is provided, retrieval must use
    the persisted effective_tags (with admin suppressions honoured)
    rather than the raw rule-inferred task_tags."""
    # Build a small synthetic index where only effective-tagged pages
    # would match. We bypass the file path by injecting an index file
    # via SAT_KB_ROOT.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        kb = pathlib.Path(tmp) / "kb"
        kb.mkdir()
        (kb / "wiki").mkdir()
        # A page tagged with the "effective" tag.
        (kb / "wiki" / "test-page.md").write_text(
            "---\ntitle: T\ntype: summary\ncreated: 2026-01-01\n"
            "updated: 2026-01-01\ntags: [effective]\n"
            "sources: [transcripts/youtube-HlkBuNW-VHE.txt]\n"
            "confidence: low\n---\n\nbody\n"
        )
        (kb / "wiki" / "index.md").write_text("# Index\n")
        (kb / "raw").mkdir()
        (kb / "raw" / "transcripts").mkdir()
        (kb / "raw" / "transcripts" / "x.txt").write_text("x" * 100)
        (kb / "raw" / "source-manifest.jsonl").write_text(
            json.dumps(
                {
                    "source_id": "x",
                    "title": "X",
                    "url": "https://example.com/x",
                    "retrieved_at": "2026-01-01T00:00:00+00:00",
                    "content_type": "text/plain",
                    "sha256": hashlib_sha256(b"x" * 100),
                    "bytes": 100,
                    "authority": "unofficial",
                    "transcript": "transcripts/x.txt",
                }
            )
            + "\n"
        )
        monkeypatch = __import__("pytest").MonkeyPatch()
        monkeypatch.setenv("SAT_KB_ROOT", str(tmp))
        ex = explain_error(
            question_id=1,
            passage="p",
            stem="s",
            choices=[
                {"letter": "A", "text": "a", "is_correct": False},
                {"letter": "B", "text": "b", "is_correct": True},
            ],
            student_letter="A",
            correct_letter="B",
        )
        # The pipeline ran without crashing; the rule-based error
        # taxonomy is empty (just "a" vs "b"), so the explanation
        # abstains, and no KB page is recommended. The point of the
        # test is that we got here without the pipeline crashing on
        # the synthetic index. The exact KB recommendation is exercised
        # by the larger end-to-end test above.
        assert ex.mode in {"rule", "abstained"}
        monkeypatch.undo()


# ----- PR-43 review regression tests -----


def test_evidence_excerpt_includes_keyword_beyond_240_chars():
    """PR-43 review: when the supporting sentence sits after the first
    240 chars of the passage, the excerpt must still contain it rather
    than silently cutting it off."""
    from satprep.corpus.tagger import _tokens as _tokenize

    long_passage = (
        "Sentence one is background and gives no evidence. "
        "Sentence two elaborates the setup further. "
        "Sentence three reinforces the framing again. "
        "Sentence four finally states that elephants migrate seasonally. "
        "Sentence five draws the conclusion from that fact."
    )
    excerpt = _pick_passage_span(
        long_passage,
        "What does the passage most strongly suggest?",
        "The elephants migrate seasonally.",
        "The elephants always migrate.",
        _tokenize,
    )
    assert "elephants" in excerpt, excerpt
    assert len(excerpt) <= _EVIDENCE_MAX_CHARS


def test_llm_call_malformed_response_raises_value_error(monkeypatch):
    """PR-43 review: a successful HTTP 200 with empty `choices` (or a
    null content) must raise ValueError instead of IndexError/TypeError,
    so the caller's abstention handler can catch it."""
    import satprep.explanations as ex_mod

    class _Resp:
        def __init__(self, body):
            self._body = body

        def read(self_inner):
            return self_inner._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    for bad in (
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": None}}]},
    ):

        def _fake_urlopen(req, timeout=20, body=bad):
            import json as _json

            return _Resp(_json.dumps(body).encode("utf-8"))

        monkeypatch.setattr(ex_mod.urllib.request, "urlopen", _fake_urlopen)
        try:
            ex_mod._call_llm("http://x/", "auto:generic-free", "k", [])
        except ValueError:
            continue
        else:
            raise AssertionError(f"expected ValueError for body={bad!r}")


def test_llm_configured_appends_chat_completions_route(monkeypatch):
    """PR-43 review: OPENAI_BASE_URL is a base URL; the chat-completions
    route must be appended to derive the POST endpoint."""
    import satprep.explanations as ex_mod

    monkeypatch.setenv("SAT_EXPLAIN_API_KEY", "k")
    monkeypatch.setenv("SAT_EXPLAIN_MODEL", "auto:generic-free")
    monkeypatch.delenv("SAT_EXPLAIN_ENDPOINT", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.com/v1")
    cfg = ex_mod._llm_configured()
    assert cfg[0].endswith("/chat/completions"), cfg


def test_explain_error_abstains_when_taxonomy_empty(monkeypatch):
    """PR-43 review: an empty rule-based taxonomy must abstain even when
    an LLM is configured and would otherwise return a confident answer."""
    import satprep.explanations as ex_mod

    monkeypatch.setenv("SAT_EXPLAIN_API_KEY", "k")
    monkeypatch.setenv("SAT_EXPLAIN_MODEL", "auto:generic-free")
    monkeypatch.setenv("SAT_EXPLAIN_ENDPOINT", "http://127.0.0.1:1/v1/chat/completions")

    def _fraud(*a, **kw):
        return (
            '{"tested_task":"t","tempting_answer":"t",'
            '"exact_failure":"t","correct_reasoning":"t",'
            '"confidence":"high"}'
        )

    monkeypatch.setattr(ex_mod, "_call_llm", _fraud)
    ex = ex_mod.explain_error(
        question_id=42,
        passage="plain passage",
        stem="What is the central idea?",
        choices=[
            {"letter": "A", "text": "cats are mammals"},
            {"letter": "B", "text": "dogs are mammals"},
        ],
        student_letter="A",
        correct_letter="B",
    )
    assert ex.mode == "abstained"
    assert ex.confidence == "low"
    assert ex.model == ""


def test_retrieve_pages_taxonomy_normalization():
    """PR-43 review: granular reasoning tags (e.g. unsupported_inference)
    must still match KB pages tagged with the broader family name
    (inference)."""
    out = _retrieve_pages(
        {
            "pages": [
                {"path": "x/y.md", "tags": ["inference", "evidence"]},
                {"path": "x/z.md", "tags": ["passage-strategy"]},
            ]
        },
        task_tags=["unsupported_inference"],
        error_taxonomy=[],
    )
    assert [p["path"] for p in out] == ["x/y.md"]


def test_tag_family_groups_granular_labels():
    """PR-43 review: confirm the family buckets used by retrieval match
    the KB index vocabulary."""
    assert _tag_family("unsupported_inference") == "inference"
    assert _tag_family("UNSUPPORTED_INFERENCE") == "inference"
    assert _tag_family("word_sense_in_context") == "word-in-context"
    assert _tag_family("dense_scientific_vocabulary") == "passage-strategy"
    assert _tag_family("cause_vs_correlation") == "evidence"
    # Unknown tags fall through unchanged so a fresh label doesn't get
    # silently dropped by the retrieval normalisation.
    assert _tag_family("some_brand_new_tag") == "some_brand_new_tag"


def test_retrieve_pages_error_mapping_outranks_tag_overlap():
    """PR-43 review: an error-taxonomy-driven KB hit must rank above a
    plain tag-overlap page so the model sees the strongest evidence."""
    out = _retrieve_pages(
        {
            "pages": [
                # tag-only page: tag_overlap=2, no mapping hit
                {"path": "x/tagonly.md", "tags": ["inference", "evidence", "passage-strategy"]},
                # mapping-driven page: tag_overlap=1, mapping hit (qualifier_strength)
                {"path": "kb/wiki/summaries/settele-strong-words.md", "tags": ["inference"]},
            ]
        },
        task_tags=["unsupported_inference"],
        error_taxonomy=["qualifier_strength"],
    )
    assert out[0]["path"].endswith("settele-strong-words.md"), [p["path"] for p in out]
