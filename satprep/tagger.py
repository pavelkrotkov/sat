"""Semantic tagging and official-skill derivation.

Hierarchy (spec section 7):
  1. official metadata            (already stored at ingest time)
  2. deterministic/rule-based     (this module - always available, cached in DB)
  3. cached LLM classification    (llm_tag_cache table; populated out-of-band,
                                   never required for normal operation)
  4. manual correction            (admin UI overrides everything)

Question tagging NEVER looks at whether the student answered correctly.
Error diagnosis (comparing the chosen wrong letter to the key) is separate
and writes to student_error_tags.
"""

import json
import re

from .config import SKILL_TO_DOMAIN

# ------------------------------------------------------- official skills ---

_SKILL_STEM_RULES: list[tuple[str, list[str]]] = [
    # (skill, regexes tested against the stem)
    ("Cross-Text Connections", [
        r"\btext\s*1\b.*\btext\s*2\b", r"\bboth\s+(passages|texts|authors)\b",
        r"author of text (1|2)", r"how would the author of",
        r"\bpassage\s*(1|2)\b.*\bpassage\s*(1|2)\b",
    ]),
    ("Command of Evidence", [
        r"which (quotation|choice .*)? ?(?:from the text|finding|detail).*?(best )?(supports|support|provides)",
        r"best supports the (claim|inference|hypothesis|argument)",
        r"most relevant piece of evidence",
        r"which (finding|result), if true",
        r"would most directly (support|weaken|challenge)",
        r"most effectively illustrates the claim",
        r"quotation from .+ (illustrates|supports|provides)",
    ]),
    ("Inferences", [
        r"what does the text most strongly suggest",
        r"can most reasonably be inferred",
        r"which choice (most )?(logically completes|can be inferred)",
        r"the text suggests? (that|which)",
        r"what can best be inferred",
        r"it can most reasonably be inferred",
    ]),
    ("Central Ideas and Details", [
        r"(main|central) (idea|theme|claim)",
        r"best states the main",
        r"text is primarily about",
        r"^according to the text\b",
        r"according to the text, (why|what|how|which)",
    ]),
    ("Text Structure and Purpose", [
        r"(function|purpose) of the (underlined|noted|final|first|last)?\s*(sentence|portion|text)",
        r"why does the author (mention|include|note|quote|provide)",
        r"the author (includes|mentions).*(in order to|to)",
        r"best describes the (text.s|author.s) (approach|structure|purpose)",
    ]),
    ("Words in Context", [
        r"as used in the text, what does",
        r"most nearly means?",
        r"most logical and precise word or phrase",
        r"which choice best (states|describes) the meaning of",
    ]),
    ("Rhetorical Synthesis", [
        r"most effectively uses? .*(quotations|information|data)",
        r"goal.*(would best|best accomplish)",
        r"succinctly|concisely summariz",
        r"relevant and sufficient",
    ]),
    ("Transitions", [
        r"which choice completes the text with the most logical transition",
        r"most logical and effective transition",
    ]),
]

_CONVENTIONS_RE = re.compile(r"conventions?\s+(of|for)\s+(standard\s+)?english", re.I)
_BOUNDARY_SHAPE = re.compile(r"[,;:]|\s—|\s--")
_FORM_SHAPE = re.compile(
    r"\b(is|are|was|were|has|have|had|does|do|its|it's|their|they're|there|being|having|than)\b", re.I
)


def _split_conventions(stem: str, passage: str, choices: list[str]) -> tuple[str, str]:
    """Generic 'conforms to conventions' stems: distinguish via answer shape."""
    blob = " ".join(choices)
    boundary_votes = sum(1 for c in choices if _BOUNDARY_SHAPE.search(c))
    if len(choices) >= 3 and boundary_votes >= 3:
        return "Boundaries", SKILL_TO_DOMAIN["Boundaries"]
    if _FORM_SHAPE.search(blob) and boundary_votes < 3:
        return "Form, Structure, and Sense", SKILL_TO_DOMAIN["Form, Structure, and Sense"]
    if _CONVENTIONS_RE.search(f"{stem} {passage[:200]}"):
        # still ambiguous; report honestly as unknown rather than guess
        return "", ""
    return "", ""


def derive_official_skill(stem: str, passage: str = "", choices: list[str] | None = None) -> tuple[str, str]:
    """Return (skill, domain). Deterministic stem-pattern matching."""
    low = f"{stem}\n{passage[:400]}".lower()
    for skill, patterns in _SKILL_STEM_RULES:
        if any(re.search(p, low) for p in patterns):
            return skill, SKILL_TO_DOMAIN.get(skill, "")
    if re.search(r"transition", low):
        return "Transitions", SKILL_TO_DOMAIN["Transitions"]
    if _CONVENTIONS_RE.search(low):
        skill, domain = _split_conventions(stem, passage, choices or [])
        if skill:
            return skill, domain
        # known domain, unresolvable sub-skill: keep it honest
        return "", "Standard English Conventions"
    if re.search(r"\bas used in\b|\bmost nearly mean", low):
        return "Words in Context", SKILL_TO_DOMAIN["Words in Context"]
    return "", ""


# ------------------------------------------------------------ reasoning ----

_ABSOLUTES = re.compile(
    r"\b(all|none|never|always|every|entirely|completely|impossible|proves?|definitely|undoubtedly|certainly)\b", re.I
)
_HEDGES = re.compile(
    r"\b(may|might|could|possibly|perhaps|likely|unlikely|some|several|suggests?|appears?|tends?)\b", re.I
)
_CAUSAL = re.compile(
    r"\b(caus(e|es|ed|ing)|because|due to|lead[s]? to|led to|results? in|resulted in|therefore|thus|consequently|drives?|produces?|responsible for)\b", re.I
)
_CONTRAST = re.compile(
    r"\b(however|but|yet|although|though|while|whereas|despite|nevertheless|nonetheless|instead|rather|in contrast|on the other hand|even so|still)\b", re.I
)
_COMPARATIVE = re.compile(
    r"\b(more|less|fewer|greater|higher|lower|larger|smaller|(?:\w+)er)\b.{0,24}\bthan\b|\bcompared (to|with)\b|\bas .{1,20} as\b", re.I
)
_CHRONOLOGY = re.compile(
    r"\b(before|after|earlier|later|prior to|subsequently|eventually|by \d{3,4}|until|once|when)\b|\b(1[0-9]{3}|20[0-9]{2})\b"
)
_HYPOTHESIS = re.compile(r"\b(hypothes[ia]z|theoriz|predict|expect(ed)?|assumed|propose[sd]?|postulat)\w*", re.I)
_RESULT_WORDS = re.compile(r"\b(found|observed|showed|revealed|measured|recorded|reported|demonstrated|data show)\w*", re.I)
_EVIDENCE_STEM = re.compile(r"which (quotation|finding|choice).*?(best )?(supports?|illustrates?|provides?|strengthens?|weakens?|challenges?)", re.I)
_MAIN_IDEA_STEM = re.compile(r"(main|central) (idea|claim|theme|point)|best states the main|primarily about", re.I)
_PURPOSE_STEM = re.compile(r"(function|purpose) of|why does the author|in order to", re.I)
_WORD_SENSE_STEM = re.compile(r"as used in the text|most nearly means?", re.I)
_CROSS_TEXT_STEM = re.compile(r"text\s*(1|2)|both (texts|passages|authors)|author of text", re.I)
_QUOTED_CHOICE = re.compile(r"[\"“”]")
_TECH_SUFFIX = re.compile(r"\w+(tion|sion|ology|ography|ometry|itis|genesis|metry|pathy|esis|osis|ase|ine)\b", re.I)

_DIRECTION_PAIRS = [
    ("increase", "decrease"), ("more", "less"), ("higher", "lower"),
    ("greater", "smaller"), ("help", "harm"), ("benefit", "damage"),
    ("strengthen", "weaken"), ("attract", "repel"), ("accelerate", "slow"),
    ("positive", "negative"), ("gain", "loss"),
]
_STANCE_WORDS = re.compile(
    r"\b(critical|skeptic\w*|enthusiast\w*|support\w*|oppos\w*|favor\w*|object\w*|endorse\w*|caution\w*|dismiss\w*|ambivalent|neutral)\b", re.I
)


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z']+", text.lower()))


def _long_word_ratio(text: str) -> float:
    words = re.findall(r"[A-Za-z']+", text)
    return (sum(1 for w in words if len(w) >= 12) / len(words)) if words else 0.0


def reasoning_tags(passage: str, stem: str, choices: list[str]) -> list[str]:
    """Rule-based demand/trap tagging. Deterministic; never uses outcomes."""
    tags: list[str] = []
    add = lambda t: tags.append(t) if t not in tags else None  # noqa: E731

    full = "\n".join([passage, stem])
    choice_blob = " \n ".join(choices)
    stem_low = stem.lower()

    # ---- question-demand tags ------------------------------------------
    if _EVIDENCE_STEM.search(stem):
        add("claim_vs_evidence")
        add("evidence_strength")
        if sum(1 for c in choices if _QUOTED_CHOICE.search(c)) >= 3:
            add("evidence_relevance")
    if _MAIN_IDEA_STEM.search(stem):
        add("main_claim_vs_detail")
        add("paraphrase_precision")
    if _PURPOSE_STEM.search(stem):
        add("author_purpose")
    if _WORD_SENSE_STEM.search(stem):
        add("word_sense_in_context")
        add("near_synonym_distinction")
    if _CROSS_TEXT_STEM.search(stem):
        add("cross_text_agreement" if re.search(r"agree|similar|shared|both", stem_low) else "cross_text_disagreement")
        add("same_topic_wrong_relationship")
    if re.search(r"suggest|infer", stem_low):
        add("unsupported_inference")
        add("true_but_not_supported")
    if _COMPARATIVE.search(choice_blob) or _COMPARATIVE.search(passage[-600:]):
        add("comparison_relationship")

    # ---- trap-shape tags (how strong students get fooled) ---------------
    abs_hits = [bool(_ABSOLUTES.search(c)) for c in choices]
    hedge_hits = [bool(_HEDGES.search(c)) for c in choices]
    if any(abs_hits) and any(hedge_hits):
        add("absolute_vs_tentative_language")
        add("qualifier_strength")
    elif sum(hedge_hits) >= 2:
        add("qualifier_strength")
    if _CAUSAL.search(choice_blob) and _CAUSAL.search(full):
        add("cause_vs_correlation")
    if _CONTRAST.search(full):
        add("contrast_concession")
    if _CHRONOLOGY.search(full):
        add("chronology")
    if _HYPOTHESIS.search(full) and _RESULT_WORDS.search(full):
        add("hypothesis_vs_result")
    if re.search(r"(attitude|tone|stance|perspective)", stem_low):
        add("tone_or_stance")
    if sum(1 for c in choices if _STANCE_WORDS.search(c)) >= 2:
        add("tone_or_stance")
        add("degree_or_intensity")
    low_words = ["increase", "decrease", "reduce", "expand"]
    if len({w for w in low_words for c in choices if w in c.lower()}) >= 2 or \
       any(a in c.lower() and b in c2.lower() for a, b in _DIRECTION_PAIRS for c in choices for c2 in choices if c is not c2):
        add("direction_reversal")

    quantifiers = {"all", "none", "some", "several", "most", "many", "few", "only", "both"}
    q_per_choice = [{m.group(0).lower() for m in re.finditer(r"\b(" + "|".join(quantifiers) + r")\b", c, re.I)} for c in choices]
    union_q = set().union(*q_per_choice) if q_per_choice else set()
    if len(union_q) >= 2 and any(len(q) > 0 for q in q_per_choice):
        add("quantifier_mismatch")

    # lexical overlap among choices => fine paraphrase distinctions
    tok_sets = [_tokens(c) for c in choices if len(c.split()) >= 4]
    overlaps = []
    for i in range(len(tok_sets)):
        for j in range(i + 1, len(tok_sets)):
            inter = tok_sets[i] & tok_sets[j]
            if inter:
                overlaps.append(len(inter) / min(len(tok_sets[i]), len(tok_sets[j])))
    if overlaps and max(overlaps) >= 0.55:
        add("paraphrase_precision")
        add("near_synonym_distinction")

    # dense scientific passage load (spec section 17)
    sentences = [s for s in re.split(r"[.!?]+", passage) if s.strip()]
    avg_len = (sum(len(s.split()) for s in sentences) / len(sentences)) if sentences else 0
    tech_ratio = len(_TECH_SUFFIX.findall(passage)) / max(1, len(sentences))
    if _long_word_ratio(passage) >= 0.055 or tech_ratio >= 1.2:
        add("dense_scientific_vocabulary")
    if avg_len >= 30:
        add("scientific_noun_overload")
    if ("dense_scientific_vocabulary" in tags or "scientific_noun_overload" in tags) and \
       (_HYPOTHESIS.search(full) or _COMPARATIVE.search(full)):
        add("abstract_relationship_extraction")

    # inference stems on abstract relations
    if re.search(r"suggest|infer", stem_low) and _CONTRAST.search(full) and len(sentences) <= 6:
        add("abstract_relationship_extraction")

    return tags


# ---------------------------------------------------------- diagnostics ---

def diagnose_error(correct_text: str, student_text: str) -> list[str]:
    """Compare the chosen WRONG choice against the key. Returns error tags.

    Deliberately conservative: only fires on clear textual contrasts so we
    don't fabricate failure modes the data cannot support (spec section 6).
    """
    tags: list[str] = []
    add = lambda t: tags.append(t) if t not in tags else None  # noqa: E731
    if not correct_text.strip() or not student_text.strip():
        return []
    c, s = correct_text.lower(), student_text.lower()

    s_abs, s_hedge = bool(_ABSOLUTES.search(s)), bool(_HEDGES.search(s))
    c_abs, c_hedge = bool(_ABSOLUTES.search(c)), bool(_HEDGES.search(c))
    s_intensifier = bool(re.search(r"\b(primary|main|sole(ly)?|direct(ly)?|chief|foremost)\b", s))
    c_hedged = bool(re.search(r"\b(may|might|could|contribute|suggests?|appears?)\b", c))
    if s_abs and not c_abs:
        add("qualifier_strength")
        add("over_inference")
        if re.search(r"prove|definitely|undoubtedly|always|never", s):
            add("absolute_vs_tentative_language")
    elif s_intensifier and c_hedged and not _ABSOLUTES.search(c):
        # "is the primary cause" vs "may contribute": strength upgrade, no absolutes
        add("qualifier_strength")
        add("over_inference")
    if _CAUSAL.search(s) and not _CAUSAL.search(c):
        add("cause_vs_correlation")
    for a, b in _DIRECTION_PAIRS:
        if ((a in s and b in c) or (b in s and a in c)) and not (a in c and b in c):
            add("direction_reversal")
            break
    if _CONTRAST.search(c) and not _CONTRAST.search(s) and re.search(r"\bhowever|but|although\b", s) is None:
        add("contrast_concession")
    st, ct = _tokens(student_text), _tokens(correct_text)
    if len(st) >= 4 and len(ct) >= 4:
        overlap = len(st & ct) / max(1, min(len(st), len(ct)))
        if overlap < 0.18:
            add("same_topic_wrong_relationship")
    return tags


# ------------------------------------------------------------ persistence --

def tag_question_row(conn, qid: int, passage: str, stem: str, choices: list[str],
                     correct_letter: str = "") -> list[str]:
    from . import config
    from .ingest import utc_now

    tags = reasoning_tags(passage, stem, choices)
    conn.execute("DELETE FROM question_tags WHERE question_id=? AND origin='rule'", (qid,))
    for t in tags:
        conn.execute(
            "INSERT OR IGNORE INTO question_tags (question_id, tag, origin, created_at) VALUES (?,?,'rule',?)",
            (qid, t, utc_now()),
        )
    return tags


def diagnose_attempt(conn, qid: int, choices: list[dict], correct_letter: str, student_letter: str) -> list[str]:
    from .ingest import utc_now

    if not student_letter or student_letter == correct_letter:
        return []
    cmap = {c["letter"]: c["text"] for c in choices}
    correct_text = cmap.get(correct_letter, "")
    student_text = cmap.get(student_letter, "")
    if not correct_text or not student_text:
        return []
    tags = diagnose_error(correct_text, student_text)
    conn.execute("DELETE FROM student_error_tags WHERE question_id=?", (qid,))
    for t in tags:
        conn.execute(
            "INSERT OR IGNORE INTO student_error_tags (question_id, tag, diagnosis_source, created_at) VALUES (?,?,'rule',?)",
            (qid, t, utc_now()),
        )
    return tags


def run_full_tagging(db_path=None) -> dict:
    """Tag every active question lacking rule tags; derive missing skills."""
    from .db import connect

    conn = connect(db_path)
    rows = conn.execute(
        """SELECT id, passage, stem, choices_json, official_skill FROM questions WHERE active=1"""
    ).fetchall()
    stats = {"questions": len(rows), "tagged": 0, "skills_derived": 0}
    for row in rows:
        choices = [c["text"] for c in json_load(row["choices_json"])]
        # choice-less questions (Bluebook omits options on correct reviews)
        # are still tagged from passage+stem so they inform the weakness model
        tag_question_row(conn, row["id"], row["passage"], row["stem"], choices)
        stats["tagged"] += 1
        if not row["official_skill"]:
            skill, domain = derive_official_skill(row["stem"], row["passage"],
                                                  [c["text"] for c in json_load(row["choices_json"])])
            if skill or domain:
                conn.execute(
                    "UPDATE questions SET official_skill=?, official_domain=?, skill_source=? WHERE id=?",
                    (skill, domain, "derived" if skill else "derived-domain-only", row["id"]),
                )
                stats["skills_derived"] += 1
    conn.commit()
    conn.close()
    return stats


def json_load(value: str):
    return json.loads(value) if value else []
