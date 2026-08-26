"""Central configuration: paths, taxonomy, and tunable constants."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "satprep.db"

# Raw sources (read-only; never modified by satprep)
BLUEBOOK_JSON = REPO_ROOT / "outputs" / "wrong_questions.json"
SNAPSHOT_DIR = REPO_ROOT / "artifacts" / "html"
IMAGES_DIR = REPO_ROOT / "artifacts" / "images"

# Drop zone for official College Board Question Bank exports (gitignored).
IMPORT_DIR = REPO_ROOT / "imports"

# Only Reading & Writing is trained by this system.
SUBJECT = "Reading and Writing"

# Deterministic fresh-question split (§3): sha256(fingerprint) % 4 == PROTECTED_MOD -> protected.
PROTECTED_MOD = 4
PROTECTED_TARGET = 3  # => 25% protected

# ---------------------------------------------------------------- taxonomy --

OFFICIAL_DOMAINS = [
    "Information and Ideas",
    "Craft and Structure",
    "Expression of Ideas",
    "Standard English Conventions",
]

# Official R&W skill vocabulary (College Board naming), grouped by domain.
OFFICIAL_SKILLS: dict[str, list[str]] = {
    "Information and Ideas": [
        "Central Ideas and Details",
        "Command of Evidence",
        "Inferences",
    ],
    "Craft and Structure": [
        "Words in Context",
        "Text Structure and Purpose",
        "Cross-Text Connections",
    ],
    "Expression of Ideas": [
        "Rhetorical Synthesis",
        "Transitions",
    ],
    "Standard English Conventions": [
        "Boundaries",
        "Form, Structure, and Sense",
    ],
}
SKILL_TO_DOMAIN = {s: d for d, ss in OFFICIAL_SKILLS.items() for s in ss}

# Granular reasoning/error taxonomy (§4). These describe the *reasoning
# operation* a question demands or the *failure mode* that could cause a
# strong student to miss it. A question may carry several.
REASONING_TAGS: list[str] = [
    "unsupported_inference",
    "over_inference",
    "qualifier_strength",
    "absolute_vs_tentative_language",
    "scope_shift",
    "wrong_reference_group",
    "cause_vs_correlation",
    "direction_reversal",
    "contrast_concession",
    "chronology",
    "comparison_relationship",
    "hypothesis_vs_result",
    "claim_vs_evidence",
    "main_claim_vs_detail",
    "evidence_relevance",
    "evidence_strength",
    "quantifier_mismatch",
    "paraphrase_precision",
    "word_sense_in_context",
    "near_synonym_distinction",
    "tone_or_stance",
    "degree_or_intensity",
    "author_purpose",
    "cross_text_agreement",
    "cross_text_disagreement",
    "same_topic_wrong_relationship",
    "true_but_not_supported",
    "irrelevant_detail",
    "logical_connector",
    "dense_scientific_vocabulary",
    "scientific_noun_overload",
    "abstract_relationship_extraction",
]

TAG_DESCRIPTIONS: dict[str, str] = {
    "unsupported_inference": "Answer goes beyond what the text forces.",
    "over_inference": "Conclusion is stronger than the evidence warrants.",
    "qualifier_strength": "Hedges like 'may/might/some' vs strong claims.",
    "absolute_vs_tentative_language": "'All/never/always' vs tentative wording.",
    "scope_shift": "Answer quietly widens or narrows the passage's scope.",
    "wrong_reference_group": "Right claim about the wrong group/agent.",
    "cause_vs_correlation": "Co-occurrence mistaken for causation.",
    "direction_reversal": "Relationship flipped (helps <-> harms).",
    "contrast_concession": "Although/however/yet pivots misread.",
    "chronology": "Sequence or anachronism confusion.",
    "comparison_relationship": "Comparative form or direction mishandled.",
    "hypothesis_vs_result": "Prediction vs observed finding conflated.",
    "claim_vs_evidence": "Supporting data confused with the claim itself.",
    "main_claim_vs_detail": "True detail chosen over the central claim.",
    "evidence_relevance": "Quote that exists but does not answer the ask.",
    "evidence_strength": "Weak support chosen over decisive support.",
    "quantifier_mismatch": "some/several/all/most mismatched to text.",
    "paraphrase_precision": "Near-paraphrase subtly off from original.",
    "word_sense_in_context": "Secondary meaning of a common word.",
    "near_synonym_distinction": "Two close synonyms with different force.",
    "tone_or_stance": "Attitude signaled but missed.",
    "degree_or_intensity": "Intensity of praise/criticism misjudged.",
    "author_purpose": "Why the author includes a detail/structure.",
    "cross_text_agreement": "Where two texts align (P1/P2 items).",
    "cross_text_disagreement": "Where two texts conflict (P1/P2 items).",
    "same_topic_wrong_relationship": "True topic, wrong relation asserted.",
    "true_but_not_supported": "Plausibly true externally, not in passage.",
    "irrelevant_detail": "Detail present in text but unresponsive to ask.",
    "logical_connector": "Transition/discourse marker function.",
    "dense_scientific_vocabulary": "Heavy technical lexicon load.",
    "scientific_noun_overload": "Many noun phrases per clause.",
    "abstract_relationship_extraction": "Relations between abstract entities.",
}

# Rule-based lesson templates for the review screen (one-sentence lessons).
TAG_LESSONS: dict[str, str] = {
    "qualifier_strength": "Prefer the weakest conclusion fully forced by the passage; hedge words are constraints, not decoration.",
    "over_inference": "Stop at the last claim the text compels you to accept - one step past the evidence is a trap.",
    "unsupported_inference": "Every part of the answer must be anchored to specific passage language.",
    "absolute_vs_tentative_language": "Match the passage's certainty level exactly; absolutes are rarely supported.",
    "scope_shift": "Check whether the answer's subject group matches the passage's group word-for-word.",
    "cause_vs_correlation": "Do not upgrade 'happens with' into 'causes' unless the text says so.",
    "direction_reversal": "Verify the direction of each relationship against the passage verb by verb.",
    "contrast_concession": "The sentence after 'however/but/yet' carries the author's real point.",
    "hypothesis_vs_result": "Keep predictions and findings separate; which one is the passage asserting?",
    "claim_vs_evidence": "Ask: is this choice the claim, or the support for the claim?",
    "main_claim_vs_detail": "A true detail is still wrong if the question asks what the text as a whole does.",
    "evidence_relevance": "A good evidence quote must contain the relationship the question asks about.",
    "evidence_strength": "Choose the quote where the link is explicit, not merely compatible.",
    "quantifier_mismatch": "Audit every quantifier in the choices against the passage.",
    "paraphrase_precision": "Map each phrase of the winning paraphrase back to passage language.",
    "word_sense_in_context": "Test the ordinary meaning first; if it fails, take the contextual meaning.",
    "near_synonym_distinction": "Between close synonyms, decide on connotation and strength, not topic.",
    "tone_or_stance": "List the passage's attitude words before reading the choices.",
    "degree_or_intensity": "Calibrate praise/blame intensity; mild and strong stances are different answers.",
    "author_purpose": "Connect the detail to the author's next move or overall goal.",
    "cross_text_agreement": "Find the proposition both texts could each endorse verbatim.",
    "cross_text_disagreement": "Locate the exact proposition where the second text pushes back.",
    "same_topic_wrong_relationship": "Topic match is not enough; verify the claimed relationship.",
    "true_but_not_supported": "Outside knowledge makes a choice tempting and wrong.",
    "irrelevant_detail": "Confirm the answer responds to the actual question asked.",
    "logical_connector": "Define the logical job the blank must do before looking at options.",
    "chronology": "Order events as the text gives them before evaluating sequences.",
    "comparison_relationship": "Track who is compared to whom and on which dimension.",
    "dense_scientific_vocabulary": "Strip nouns to roles (agent, target, measure) before reasoning.",
    "scientific_noun_overload": "Break overloaded sentences into who-did-what-to-whom chunks.",
    "abstract_relationship_extraction": "Name each abstract entity's role explicitly, then test relationships.",
}

# ------------------------------------------------------------- weakness ----

WEAKNESS_PRIOR_STRENGTH = 6.0      # beta prior pseudo-count (smoothing)
WEAKNESS_RECENCY_HALF_LIFE_DAYS = 120.0
WEAKNESS_SHRINK_N = 10.0           # evidence shrinkage toward baseline
SLOW_CORRECT_THRESHOLD_S = 90.0    # spec section 12: slow-correct friction signal
CONFIDENT_WRONG_MULTIPLIER = 1.6   # wrong + confidence 3 counts extra
LOW_CONF_CORRECT_WEIGHT = 0.55     # correct-but-guessing proves less
MASTERY_RECENT_CORRECT_DISCOUNT = 0.25

# A tag counts as "weak enough to train against" above this model score.
# Used by the dashboard's transfer panel to decide which fresh questions
# count as transfer material rather than incidental practice.
WEAK_TAG_THRESHOLD = 55.0

# ------------------------------------------------------------- sampler -----
# Relative weights for the additive selection score (see sampler.py).
W_WEAK_TAG_MATCH = 2.2
W_WEAK_SECONDARY_TAG = 0.8
W_SKILL_WEAKNESS = 1.1
W_FRESH_MATCHING_WEAK = 2.5
W_FRESH_NEIGHBOR = 1.0
W_DUE_INCORRECT = 2.0
W_TRANSFER_CORRECT = 0.9
W_SEMANTIC_BIAS = 0.6
W_HARD_DIFFICULTY = 1.2
W_NOT_SEEN_LONG_AGO = 1.2
PENALTY_EXPOSURE_PER_SEEN = 0.55
PENALTY_EXPOSURE_CAP = 2.75
PENALTY_RECENT_MASTERED = 1.0

DEFAULT_DRILL_SIZE = 12
HARD_MIXED_SIZE = 27

# Skills representing hard semantic/reasoning work (spec section 16).
SEMANTIC_SKILLS = {
    "Inferences",
    "Command of Evidence",
    "Words in Context",
    "Text Structure and Purpose",
    "Cross-Text Connections",
    "Central Ideas and Details",
}
