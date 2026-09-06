"""Canonical coaching taxonomy and reusable rules for report analysis."""

_GROUPS = (
    ("Unsupported addition / over-inference", frozenset("qualifier_strength over_inference absolute_vs_tentative_language unsupported_inference quantifier_mismatch scope_shift".split())),
    ("Wrong relationship / direction", frozenset("direction_reversal cause_vs_correlation wrong_reference_group comparison_relationship hypothesis_vs_result chronology".split())),
    ("Failed to combine all evidence", frozenset("failed_synthesis ignored_finding ignored_contrast incomplete_indirect_chain abstract_relationship_extraction".split())),
    ("Missed governing constraint / keyword", frozenset("contrast_concession logical_connector governing_constraint keyword".split())),
    ("Right topic, wrong job / neighboring answer", frozenset("true_but_not_supported same_topic_wrong_relationship irrelevant_detail main_claim_vs_detail evidence_relevance claim_vs_evidence".split())),
    ("Literal factual misread", frozenset("literal_misread misread_method misread_premise explicit_contradiction".split())),
    ("Vocabulary / semantic precision", frozenset("word_sense_in_context near_synonym_distinction paraphrase_precision collocation degree_or_intensity".split())),
)
RULES = {
    "Unsupported addition / over-inference": "Inference = minimum warranted conclusion. Audit every added actor, cause, comparison, and degree.",
    "Wrong relationship / direction": "Reduce the relationship to arrows before reading choices; preserve which variable does what.",
    "Failed to combine all evidence": "If the passage gives two findings, the answer must account for both.",
    "Missed governing constraint / keyword": "Circle the governing word—however, although, despite, together, indirect, rather than—and obey it.",
    "Right topic, wrong job / neighboring answer": "Ask what job the choice performs, not whether its topic appears in the passage.",
    "Literal factual misread": "Verify the exact premise or method against the text before inferring anything.",
    "Vocabulary / semantic precision": "Predict the sentence meaning first, then choose the word with the exact nuance and usage.",
}
_DEFAULTS = (
    "Predict before looking at the choices; do not shop among four answers.",
    "Audit the strongest word in the final two choices.",
    "Use the minimum conclusion the text actually warrants.",
)
_PREDICTION_YES = frozenset(tuple(RULES)[:5])
_PREDICTION_NO = frozenset(tuple(RULES)[5:])


def canonical_error(tags: list[str]) -> tuple[str, str]:
    for label, members in _GROUPS:
        for tag in tags:
            if tag in members:
                return label, tag
    return "", tags[0] if tags else ""


def prediction_preventable(label: str) -> str:
    if label in _PREDICTION_YES:
        return "yes"
    if label in _PREDICTION_NO:
        return "no"
    return "uncertain"


def rule_for(label: str) -> str:
    return RULES.get(label, "Explain the exact defect in the chosen answer before moving on.")


def coaching_rules(counter) -> list[str]:
    rules = [RULES[label] for label, _ in counter.most_common(5)]
    rules.extend(rule for rule in _DEFAULTS if rule not in rules)
    return rules[:5]
