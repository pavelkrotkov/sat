"""Tests for the KB-aware error explanation pipeline (issue #36)."""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from satprep.explanations import (
    Explanation,
    _load_index,
    _parse_llm_json,
    _retrieve_pages,
    _rule_based_explanation,
    explain_error,
)

REPO = pathlib.Path(__file__).resolve().parent.parent


def _sample_choices():
    return [
        {"letter": "A", "text": "The graph proves that rainfall directly causes yield.",
         "is_correct": False},
        {"letter": "B", "text": "The graph shows a positive correlation between rainfall and yield.",
         "is_correct": True},
        {"letter": "C", "text": "Rainfall always increases yield.",
         "is_correct": False},
        {"letter": "D", "text": "Some rainfall data was recorded.",
         "is_correct": False},
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
        choices=[{"letter": "A", "text": "", "is_correct": False},
                 {"letter": "B", "text": "X", "is_correct": True}],
        student_letter="A",
        correct_letter="B",
    )
    assert ex.confidence == "low"
    assert ex.mode == "rule"
    assert ex.error_taxonomy == []


def test_explain_error_trap_answer_pattern():
    ex = explain_error(
        question_id=3,
        passage="Researchers tracked 50 birds and found migration patterns.",
        stem="What can most reasonably be inferred from the study?",
        choices=[
            {"letter": "A", "text": "All birds migrate.", "is_correct": False},
            {"letter": "B", "text": "Some tracked birds migrated.",
             "is_correct": True},
        ],
        student_letter="A",
        correct_letter="B",
    )
    assert ex.error_taxonomy
    assert any("absolute_vs_tentative_language" in t or "qualifier_strength" in t
               for t in ex.error_taxonomy)
    paths = " ".join(ex.kb_tactic_refs)
    assert "settele-strong-words" in paths or "settele-trap-answers" in paths


def test_explain_error_evidence_extraction_includes_stem_and_choices():
    ex = explain_error(
        question_id=4,
        passage="P",
        stem="Which supports the claim?",
        choices=[{"letter": "A", "text": "alpha", "is_correct": False},
                 {"letter": "B", "text": "beta", "is_correct": True}],
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
    index = {"pages": [
        {"path": "kb/wiki/summaries/a.md", "tags": ["t1"]},
        {"path": "kb/wiki/summaries/b.md", "tags": ["t1", "t2"]},
        {"path": "kb/wiki/summaries/c.md", "tags": ["t2"]},
    ]}
    out1 = _retrieve_pages(index, task_tags=["t1", "t2"])
    out2 = _retrieve_pages(index, task_tags=["t1", "t2"])
    assert [p["path"] for p in out1] == [p["path"] for p in out2]
    assert out1[0]["path"] == "kb/wiki/summaries/b.md"


def test_retrieve_pages_error_taxonomy_mapping():
    index = {"pages": [
        {"path": "kb/wiki/summaries/settele-strong-words.md",
         "tags": ["inference", "evidence", "passage-strategy"]},
        {"path": "kb/wiki/summaries/settele-trap-answers.md",
         "tags": ["inference", "evidence", "passage-strategy"]},
        {"path": "kb/wiki/summaries/penguin-reading-hacks.md",
         "tags": ["inference", "evidence", "passage-strategy"]},
    ]}
    out = _retrieve_pages(index, task_tags=[], error_taxonomy=["qualifier_strength"])
    paths = [p["path"] for p in out]
    assert "kb/wiki/summaries/settele-strong-words.md" in paths


def test_load_index_missing_file_returns_empty():
    assert _load_index(pathlib.Path("/nonexistent")) == {}


def test_parse_llm_json_handles_prose_wrapping():
    assert _parse_llm_json('{"a": 1, "b": 2}') == {"a": 1, "b": 2}
    assert _parse_llm_json(
        "Here you go:\n```json\n{\"a\": 3}\n```\nEnjoy!"
    ) == {"a": 3}
    assert _parse_llm_json("not json at all") is None
    assert _parse_llm_json("prefix then {\"k\": \"v\"} suffix") == {"k": "v"}


def test_explain_error_unconfigured_endpoint_abstains(monkeypatch):
    monkeypatch.setenv("SAT_EXPLAIN_API_KEY", "fake-key")
    monkeypatch.setenv("SAT_EXPLAIN_MODEL", "auto:generic-free")
    monkeypatch.setenv("SAT_EXPLAIN_ENDPOINT", "http://127.0.0.1:1/v1/chat/completions")
    ex = explain_error(
        question_id=5,
        passage="P",
        stem="S",
        choices=[{"letter": "A", "text": "x", "is_correct": False},
                 {"letter": "B", "text": "y", "is_correct": True}],
        student_letter="A",
        correct_letter="B",
    )
    assert ex.mode in {"rule", "abstained"}
    if ex.mode == "abstained":
        assert ex.confidence == "low"
        assert ex.model == "auto:generic-free"
    assert isinstance(ex.error_taxonomy, list)


def test_explain_error_disallowed_model_falls_back(monkeypatch):
    monkeypatch.setenv("SAT_EXPLAIN_API_KEY", "fake-key")
    monkeypatch.setenv("SAT_EXPLAIN_MODEL", "gpt-4o")
    ex = explain_error(
        question_id=6,
        passage="P",
        stem="S",
        choices=[{"letter": "A", "text": "x", "is_correct": False},
                 {"letter": "B", "text": "y", "is_correct": True}],
        student_letter="A",
        correct_letter="B",
    )
    assert ex.mode == "rule"
    assert ex.model == ""


def test_explain_error_llm_response_unparseable_abstains(monkeypatch):
    monkeypatch.setenv("SAT_EXPLAIN_API_KEY", "fake-key")
    monkeypatch.setenv("SAT_EXPLAIN_MODEL", "auto:generic-free")
    monkeypatch.setenv("SAT_EXPLAIN_ENDPOINT", "http://127.0.0.1:1/v1/chat/completions")
    import satprep.explanations as ex_mod
    monkeypatch.setattr(ex_mod, "_call_llm",
                        lambda *a, **kw: "Sorry, I cannot answer that.")
    ex = explain_error(
        question_id=7,
        passage="P",
        stem="S",
        choices=[{"letter": "A", "text": "x", "is_correct": False},
                 {"letter": "B", "text": "y", "is_correct": True}],
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
    for field in ("tested_task", "tempting_answer", "exact_failure",
                  "correct_reasoning", "kb_tactic_refs", "evidence_citations",
                  "confidence", "mode", "model", "error_taxonomy"):
        assert hasattr(ex, field), field
    assert ex.mode in {"rule", "llm", "abstained"}
    assert ex.confidence in {"low", "medium", "high"}