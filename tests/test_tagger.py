from satprep.corpus.tagger import derive_official_skill, diagnose_error, reasoning_tags


def test_wic_stem():
    skill, _domain = derive_official_skill(
        "As used in the text, what does the word 'grave' most nearly mean?"
    )
    assert skill == "Words in Context"


def test_inference_stem():
    skill, _ = derive_official_skill(
        "What does the text most strongly suggest about the migration?"
    )
    assert skill == "Inferences"


def test_cross_text_stem():
    skill, _ = derive_official_skill(
        "Based on the texts, how would the author of Text 1 most likely respond to Text 2?"
    )
    assert skill == "Cross-Text Connections"


def test_generic_conventions_gets_domain_only():
    stem = "Which choice completes the text so that it conforms to the conventions of Standard English?"
    skill, _domain = derive_official_skill(stem, choices=["one", "two; three", "four, five", "six"])
    assert skill in ("Boundaries", "Form, Structure, and Sense", "")


def test_qualifier_trap_detected():
    tags = reasoning_tags(
        "The drug may reduce symptoms in some patients.",
        "Which choice best states what the text suggests about the drug?",
        [
            "The drug eliminates symptoms in all patients.",
            "The drug may reduce symptoms for some patients.",
            "Doctors avoid prescribing the drug.",
            "The drug was designed for children.",
        ],
    )
    assert "unsupported_inference" in tags
    assert "absolute_vs_tentative_language" in tags or "qualifier_strength" in tags


def test_hypothesis_vs_result():
    tags = reasoning_tags(
        "Researchers hypothesized that X causes Y. The experiment found no effect on Y.",
        "Which choice best reflects the relationship between the hypothesis and the findings?",
        ["h supports f", "f contradicts h", "unrelated", "none"],
    )
    assert "hypothesis_vs_result" in tags


def test_diagnose_overreach_to_absolute():
    tags = diagnose_error(
        "The pesticide may contribute to the decline.",
        "The pesticide is the primary cause of the decline.",
    )
    assert "qualifier_strength" in tags
    assert "over_inference" in tags


def test_no_diagnosis_when_choice_unknown():
    assert diagnose_error("", "") == []
