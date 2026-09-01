#!/usr/bin/env python3
"""Generate the SAT wrong-answer review report.

Renders a self-contained HTML report covering incorrect Reading & Writing
answers in the last REPORT_DAYS days, diagnosed with the SAT llm-wiki framework
(Analyze -> Predict -> Eliminate; dumb summaries; strong-word / trap-answer
checks). The report is a disposable presentation layer: the database owns the
canonical question and attempt data, and this script owns the rendering.

Output: data/reports/weekly-<timestamp>.html

Usage:
    uv run python scripts/gen_error_report.py            # last 7 days
    uv run python scripts/gen_error_report.py --days 14  # custom window
"""

from __future__ import annotations

import argparse
import collections
import datetime
import html
import json
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "data" / "satprep.db"
OUT_DIR = REPO / "data" / "reports"

REPORT_DAYS = 7

# Hand-curated per-question diagnoses, keyed by questions.fingerprint prefix
# (stable SHA-256 of passage+stem+choices). cat = trap category; trap = why it
# was tempting; key = mental shortcut / keywords to triage fast; fail = the
# exact word/relationship that disqualifies the choice; link = KB tactic page.
DIAGNOSES: dict[str, dict] = {
    "2e60b1be0108": dict(
        cat="Scope error",
        trap="Chose A imported 'traders from outside the regions the Maya controlled' — the passage is entirely internal to Maya lands.",
        key="Dumb-summarize: 'markets exist so people swap stuff they DON'T already have. If scholars thought all Maya regions grow the same crops, there is nothing different to swap.' Predict BEFORE choices: 'markets wouldn't have given Maya products different from their own.'",
        fail="A's 'outside the regions' adds an outside actor the passage never mentions; the blank must stay inside Maya lands. Correct C is precisely the prediction.",
        link="summaries/settele-trap-answers",
    ),
    "cf60262f7bd1": dict(
        cat="Failure to combine both findings (neighboring-detail answer)",
        trap="Chose A only negates the productivity finding; the stem says 'Together, these findings suggest' — the blank must fold in BOTH, especially the tax-code finding.",
        key="'Together' is the trigger word: name BOTH findings and ask what single conclusion they jointly force. The non-obvious finding (tax code = government incentive) is what drives adoption.",
        fail="A explains away finding 1 but ignores finding 2 entirely. Correct B uses the tax-code incentive as the real driver — the inference that 'Together' demands.",
        link="concepts/sat-hard-reading-strategy-stack",
    ),
    "57bbcb0425b8": dict(
        cat="Reversal of comparison direction",
        trap="Chose C attributes the benefit to the polymer-derived fibers; the finding favors the nitrogen-treated fiber (LOWER creep rate = deforms slower = lasts longer).",
        key="Keep the comparison's DIRECTION. 'Lower minimum creep rate' → 'withstands stress longer.' Predict the answer names the nitrogen-treated fiber, not its rivals.",
        fail="C reverses which fiber is superior and swaps 'creep resistance' for 'chemical properties' (a different property the passage doesn't test). Correct D preserves the direction and the right variable.",
        link="summaries/settele-trap-answers",
    ),
    "3c88801ed7bc": dict(
        cat="Over-applied premise / over-inference",
        trap="Chose A over-applied the 'if alone, a chicken stays silent' premise to BOTH conditions — but in condition 1 the mirror makes the bird think it is NOT alone.",
        key="The whole test turns on one word: 'lacked self-recognition.' If it lacks self-recognition, its reflection reads as ANOTHER CHICKEN → it warns. Second condition has a real chicken → it warns too. Both.",
        fail="'Neither condition' ignores that the reflection counts as another chicken under the no-self-recognition premise. The silence rule only applies when truly alone.",
        link="concepts/sat-hard-reading-strategy-stack",
    ),
    "8517954263f7": dict(
        cat="Chronology/ordering error",
        trap="Chose C: 'southern and northern tribes acquired maize at the same time from different sources.' The decisive clue is that maize vocabulary is NOT SHARED across branches.",
        key="Trigger: 'maize vocabulary isn't shared' + 'maize diffused northward.' Non-shared words for a universally-grown, south-origin crop → the branches DIVIDED BEFORE the crop arrived, each coining its own term.",
        fail="C invents 'same time, different sources' — an added claim. Correct D (division preceded acquisition) is the clean inference that uniquely explains why the vocabulary diverged.",
        link="summaries/settele-dumb-summaries",
    ),
    "fb31e927d253": dict(
        cat="Over-inference / scope overreach",
        trap="Chose C leaps 'forgo analyzing the painting in favor of analyzing political activity' — forgetting a historical caution does not mean abandoning the artwork.",
        key="'Who forget this fact' = who forgets that today's values may not fit a 19th-c. work = risks judging it by anachronistic standards. Stay proportional to the warning.",
        fail="C manufactures a dramatic switch the passage never endorses. Correct A is the soft, directly-supported consequence; the softer wording is actually the accurate one.",
        link="summaries/settele-strong-words",
    ),
    "add6029ae3f2": dict(
        cat="Main-idea: buried the study's controlling claim",
        trap="Chose B is true but states only 'spike waves can exceed limits' — it misses the mechanism the passage stresses (traveling waves that intersect).",
        key="Main idea = the controlling claim. Predict: 'spike waves, formed when traveling waves intersect, can be TALLER than the steepness limit.' Pick the choice carrying the cause AND the result.",
        fail="B drops 'intersect in specific ways' (the actual novel mechanism) and reads almost tautological. Correct D states cause + effect. Slow pacing — predict before choices; don't reread.",
        link="summaries/penguin-reading-hacks",
    ),
    "16b01af44bd7": dict(
        cat="Exclusive 'rather than' that drops part of the evidence (strong word)",
        trap="Chose C says social learning plays NO role ('rather than social learning'); the passage's 2-year maternal correlation IS social learning.",
        key="Evidence gives TWO things: (1) correlation with mothers fades by year 5 → social learning that diminishes; (2) correlation with unrelated in similar habitats is modest → environment matters too. Answer must include BOTH.",
        fail="C's 'rather than' is an exclusive strong-word — it deletes finding 1. Correct B is the weaker, fully-supported hedge ('...though environmental constraints cannot be ruled out').",
        link="summaries/settele-strong-words",
    ),
    "4ed02f7ae0d3": dict(
        cat="Words-in-context: word-picked by association, not sentence meaning",
        trap="Chose A 'inconspicuousness' by linking to 'too faint.' But the sentence needs 'preclude claims the event was [unclear / not truly detected]' → ambiguity.",
        key="Unfamiliar-words method — predict sentence meaning FIRST, ignore choices: the statistician precludes 'claims that the event [wasn't really real/clear].' The noun must describe a claim about validity, not visibility.",
        fail="'Inconspicuousness' = not noticeable (a physical property); 'ambiguity' = unclear whether the event is genuine. The 99%-confidence clause selects 'ambiguity.'",
        link="summaries/penguin-unfamiliar-words",
    ),
    "5171f11e9ee7": dict(
        cat="Over-inference + strong word / superlative",
        trap="Chose C 'ROMEO AND JULIET is the MOST thematically accessible of ALL Shakespeare's tragedies' — a superlative the passage never supports.",
        key="'Consequently' → conclusion from the contrast: tragedies have broad appeal, history plays demand historical background → today's readers find history plays less engaging. Predict the general claim.",
        fail="C's 'most ... of all' is an absolute strong-word not proven by the evidence (R&J is one example, not the maximum). Correct A is the safe, supported general claim.",
        link="summaries/settele-strong-words",
    ),
    "1a686e933f61": dict(
        cat="Direct contradiction of explicit passage fact",
        trap="The chosen answers both contradict the stated 'known bilateral origin' or flip which gene program matters; evidence is the ANTERIOR genes across the whole body.",
        key="Decisive sentence: 'activity only in ANTERIOR genes across P. miniata's ENTIRE body and some posterior genes at the edges.' → the 'head' program is everywhere → the head was NOT eliminated.",
        fail="One choice denies the passage's own 'known bilateral origin'; another stresses posterior genes when the finding foregrounds ANTERIOR genes. Correct mirrors 'rather than eliminating a head region... as previously assumed.'",
        link="summaries/settele-trap-answers",
    ),
    "fdac76fa19e3": dict(
        cat="Neighboring-detail answer / generalization",
        trap="Chose C restates the resilience-trait detail; the passage's point is that bumblebees are among the FEW genera with those traits, so insights from them may not generalize.",
        key="'As a result, ecologists gained much of their insight from bumblebees' + 'bumblebees are among the relatively few ... that display' → the conclusion is CAUTION about generalizing.",
        fail="C makes a factual claim about generalist diets instead of the stated conclusion. Correct D = 'responses not always comparable... exercise caution.' It matches the conclusion trigger ('therefore contends').",
        link="concepts/sat-hard-reading-strategy-stack",
    ),
    "84259e4f4c47": dict(
        cat="Same-topic, wrong relationship",
        trap="Chose C talks about where crabs are CONCENTRATED; the passage never says that — it describes the same burrowing having OPPOSITE effects by location.",
        key="Passage sets a two-sided CONTRAST: interior → pannes shrink (plants return); edge → erosion (plants lose). Blank must mirror it: the same action helps OR hurts depending on location.",
        fail="C invents a claim about crab density. Correct B ('may promote increases OR decreases ... depending on location') is the direct conclusion from the interior/edge contrast.",
        link="summaries/settele-dumb-summaries",
    ),
    "b428be2af3b1": dict(
        cat="Reversal of what each deposit indicates",
        trap="Chose A flips the evidence: fine mudstone = slow settling in LOW-flow water = a LONG-LIVED LAKE, not 'dryness prevented a lake.'",
        key="Map the two deposits: fine mudstone = settling in still water = LAKE; coarse sandstone = flowing water = stream. Desiccation cracks in the fine grains = the lake sometimes dried but persisted.",
        fail="A's 'prolonged dryness prevented a lake' reverses which rock means water. Correct B (a long-lived lake with episodic drying + some flowing-water intervals) matches mudstone-with-cracks + sandstone lenses.",
        link="summaries/settele-dumb-summaries",
    ),
    "3a07ea8dc96c": dict(
        cat="Words-in-context: wrong nuance (recant = retract one's own prior view)",
        trap="Chose D 'recants' — but recanting means publicly taking back something one PREVIOUSLY asserted; Harjo is rejecting a tendency, not retracting his own claim.",
        key="Predict: the director 'rejects / distances from' TV's past-situating tendency. The evidence word 'rejection' selects a verb meaning to reject → 'repudiates.'",
        fail="'Recants' requires a prior stance of his own; the sentence is a rejection of an industry tendency. Correct A 'repudiates' = rejects/disavows, matching 'rejection.'",
        link="summaries/penguin-unfamiliar-words",
    ),
    "77c9381401dc": dict(
        cat="Command of evidence: one link of an indirect causal chain",
        trap="Chose C covers only the voting link; the researchers posit an INDIRECT effect: NFM → (less knowledge/interest) → (less voting). Prove BOTH legs.",
        key="'Indirect effect' = two hops. Find the finding with BOTH: NFM lowers knowledge/interest AND knowledge/interest raises voting. B supplies the full chain.",
        fail="C gives the voting leg but says interest→knowledge is weak (undermines the chain). B (strong negative NFM effect + strong positive knowledge/interest↔voting) directly supports the indirect pathway.",
        link="concepts/sat-hard-reading-strategy-stack",
    ),
    "8ac51244c44d": dict(
        cat="Missed 'despite ... obligations' constraint",
        trap="Chose C 'mint HEAVIER coins (more silver)' — costly, but the passage says relief came 'despite Sidon's persistent oppressive financial obligations.'",
        key="Trigger word: 'despite ... financial obligations.' The fix had to be burden-minimizing. Keep fineness (silver ratio) steady but DROP weight = less silver outlay while reassuring traders.",
        fail="C's heavier coins cost MORE silver — the thing the obligation forbids. Correct B (consistent silver level, decreased weight) is the cost-constrained move that restores confidence.",
        link="summaries/settele-dumb-summaries",
    ),
    "d499bfd6b133": dict(
        cat="Command of evidence: choice that matches claim's key phrase (global, not Africa-only)",
        trap="Chose C 'The Short Century' is Africa-focused; the claim is showing African art in the larger context of GLOBAL modern art.",
        key="Anchor to the claim's key phrase: 'larger context of GLOBAL modern art and art history,' 'not focusing solely on modern African artists.' Predict the exhibition that crosses regions.",
        fail="C stays Africa-only (contradicts 'not solely'), then undoes the global point. Correct B crosses 'Pacific and Atlantic,' matching the global-context claim.",
        link="concepts/sat-hard-reading-strategy-stack",
    ),
    "cf2c083bd24c": dict(
        cat="Chose character-relational answer over the intellectual-distance point",
        trap="Chose A 'focus on characters' beliefs as revealed by actions' — but Brecht wanted engagement BEYOND the characters ('extend beyond the characters and events').",
        key="Repeat the Brecht doctrine: theater should elicit an INTELLECTUAL (not emotional) response, 'placing audiences at a distance.' Predict: 'be dispassionate and think critically about the social/political questions.'",
        fail="A re-focuses on characters (contradicts 'beyond the characters and events'). Correct D = dispassionate critical thinking = the stated aim of the distance device.",
        link="summaries/penguin-reading-hacks",
    ),
    "a3b0d4e8b427": dict(
        cat="Cross-text: factual misread of the method",
        trap="Chose D claims cosmogenic dating is applied 'to the fossils directly' — Text 2 says it analyzes the surrounding BRECCIA, precisely because relocated bones are unreliable.",
        key="Read the underlined problem ('unreliability of dating in this context') and match Text 2's assertion: 'this approach avoids the potential for misdating.' Answer = 'our technique beats others HERE.'",
        fail="D misdescribes the method (breccia, not the fossils). Correct C ('better suited than other methods to the unique challenges of Sterkfontein') is exactly Text 2's claim.",
        link="summaries/settele-trap-answers",
    ),
    "b207fff25e26": dict(
        cat="Command of evidence: confounded vs direct mechanism test",
        trap="Chose B (otters present, NO meadows) — but meadow ABSENCE is confounded (maybe no habitat) and you can't measure a meadow's health when none exists.",
        key="To undermine a causal hypothesis, find the finding that directly NEGATES the claimed mechanism. Hypothesis: otter damage → more sexual reproduction → healthier. Undermine: more/bigger otter populations ↔ WORSE eelgrass health.",
        fail="B is a confounded presence/absence observation. Correct C directly correlates (negatively) the health dimension the hypothesis claims to explain — a clean mechanism kill.",
        link="summaries/settele-trap-answers",
    ),
    "07de4e3cf1fd": dict(
        cat="Missed the 'why negligible' explanation question",
        trap="Chose C asserts a shallower/depth comparison the passage never provides; the question is WHY the two exposure conditions differed negligibly.",
        key="The stem is a 'fact that could be attributed to' question: answer WHY the pulse made no difference → ship sound already dominates the narwhals' acoustic environment.",
        fail="C manufactures an unsupported comparison. Correct B ('ship sounds contribute so much ... little effect') directly answers the 'why negligible' prompt.",
        link="concepts/sat-hard-reading-strategy-stack",
    ),
}

# Framework-derived fallback for questions without a hand diagnosis (per skill).
_FALLBACK: dict[str, dict] = {
    "Inferences": dict(
        cat="Over-inference / unsupported claim",
        key="Predict the claim the passage forces you to accept, then disqualify any choice that adds a commitment or strong word. One clause past the evidence is a trap.",
        fail="Weakest conclusion + exact scope: hedge words ('may/might/some') are constraints, and 'most/all/never' need explicit support.",
    ),
    "Command of Evidence": dict(
        cat="Evidence relevance / chain completeness",
        key="State the claim the evidence must CONTAIN, then match the choice whose finding directly addresses that exact relationship (not a merely-related detail).",
        fail="A supporting finding must carry the full causal chain the question posits; a single link is not enough.",
    ),
    "Central Ideas and Details": dict(
        cat="Main claim buried under a true detail",
        key="Distinguish the passage's controlling claim from an illustrative detail; choose the answer that states the overall claim, not an example of it.",
        fail="A true detail is still wrong when the question asks what the text as a whole does.",
    ),
    "Cross-Text Connections": dict(
        cat="Second text's exact disagreement",
        key="Locate the precise proposition each text asserts and the word that flips one; answer from the disagreement itself, not a paraphrase of a single text.",
        fail="Respond to what is underlined / claimed in the target text; re-typing your own paraphrase of one text misses the other's pushback.",
    ),
    "Words in Context": dict(
        cat="Word-picked by association, not sentence meaning",
        key="Predict the meaning the sentence REQUIRES from its contrast/cause/tone before reading the choices; choose the word that fits the logic, not the familiar attraction.",
        fail="Near-synonyms differ in force or require a prior stance — decide on context and connotation, not topic.",
    ),
    "Text Structure and Purpose": dict(
        cat="Function of the detail vs its content",
        key="Ask what the sentence/paragraph does in the argument (purpose), not only what information it contains.",
        fail="A structurally-correct-looking choice that only restates content misses the 'what is it doing' ask.",
    ),
}
_GENERIC = dict(
    cat="Relationship / proof-check miss",
    key="Name the relationship being tested, predict the answer before reading choices, and treat each choice as a compound claim — one unsupported part eliminates it.",
    fail="Locate the exact supporting sentence and penalize any unsupported strong word, reversal, scope overreach, or irrelevant-but-true claim.",
)

_DOMAINS_RW = ("Information and Ideas", "Craft and Structure")
_SKILLS_RW = (
    "Inferences",
    "Command of Evidence",
    "Central Ideas and Details",
    "Cross-Text Connections",
    "Words in Context",
    "Text Structure and Purpose",
)


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def _esc(s) -> str:
    return html.escape(str(s or ""))


def _when(iso) -> str:
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return f"{dt.strftime('%a %m/%d %H:%M')} UTC"
    except Exception:
        return str(iso)


def _time(ms) -> str:
    s = (ms or 0) // 1000
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def _diag_for(fingerprint: str) -> dict:
    for prefix, d in DIAGNOSES.items():
        if fingerprint.startswith(prefix):
            return d
    return {}


def _framework(skill: str) -> dict:
    return dict(_FALLBACK.get(skill, _GENERIC))


# ----------------------------------------------------------------------------
# rendering
# ----------------------------------------------------------------------------
def _agg_rows(counter) -> str:
    return "".join(
        f'<tr><td>{_esc(k)}</td><td class="num">{v}</td></tr>' for k, v in counter.most_common()
    )


def _render_cards(items, repeated) -> str:
    cards = []
    for q in items:
        d = _diag_for(q["fingerprint"]) or _framework(q["sk"])
        try:
            chs = json.loads(q["choices_json"])
        except Exception:
            chs = []
        choices = []
        for ch in chs:
            is_wrong = ch["letter"] == q["chosen_letter"]
            is_right = ch["letter"] == q["correct_letter"]
            cls = "wrong" if is_wrong else ("right" if is_right else "")
            tag = ""
            if is_wrong and is_right:
                tag = '<span class="pill wrong">your answer</span><span class="pill right">correct</span>'
            elif is_wrong:
                tag = '<span class="pill wrong">your incorrect choice</span>'
            elif is_right:
                tag = '<span class="pill right">correct</span>'
            choices.append(
                f'<div class="choice {cls}"><span class="choice-letter">{_esc(ch["letter"])}</span>'
                f"<span>{_esc(ch['text'])}</span>{tag}</div>"
            )
        repeat = (
            '<span class="pill warn">missed twice this period</span>'
            if q["fingerprint"] in repeated
            else ""
        )
        diff = f" · <b>difficulty {_esc(q['difficulty'])}</b>" if q["difficulty"] else ""
        conf = "" if not q["confidence"] else "●●●○"[: int(q["confidence"])]
        cards.append(f"""<article class="question">
  <header>
    <div><span class="q-skill">{_esc(q["sk"])}</span> {repeat}
      <span class="q-flag">src</span> {_esc(q["source_test"])} {_esc(q["qnum"])} {_esc(q["module"])}{diff}</div>
    <div class="q-metrics"><span>time <b>{_time(q["time_ms"])}</b></span>
      <span>{conf}</span><span>{_when(q["attempted_at"])}</span></div>
  </header>
  <div class="cols">
    <div class="col-passage"><h4>Passage</h4><p>{_esc(q["passage"])}</p></div>
    <div class="col-item">
      <h4>Question</h4><p class="stem">{_esc(q["stem"])}</p>
      <h4>Choices</h4>{"".join(choices)}
    </div>
  </div>
  <div class="diag">
    <div class="diag-grid">
      <div><h4>Category</h4><p>{_esc(d.get("cat"))}</p></div>
      <div><h4>Why it was tempting</h4><p>{_esc(d.get("trap", "—"))}</p></div>
      <div><h4>Mental shortcut · key words</h4><p>{_esc(d.get("key"))}</p></div>
      <div><h4>Exact failure of your choice</h4><p>{_esc(d.get("fail"))}</p></div>
    </div>
    <div class="rationale"><h4>Official rationale</h4><p>{_esc(q["rationale"]) or "—"}</p></div>
    <p class="tactic">KB tactic: <b>{_esc(d.get("link", "concepts/sat-hard-reading-strategy-stack"))}</b></p>
  </div>
</article>""")
    return "\n".join(cards)


_CSS = """
:root{--ink:#1a2332;--mut:#5b6676;--line:#e6e9ef;--paper:#fbfbfa;--card:#fff;--brand:#2f5b8f;
--wrong:#c0392b;--right:#1e8e5a;--warn:#b9770e;--code:#f4f6f9}
*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
color:var(--ink);background:var(--paper);margin:0;line-height:1.55}
.hero{background:linear-gradient(135deg,#1f3a5c,#2f5b8f);color:#fff;padding:34px 22px;margin-bottom:24px}
.hero .kicker{text-transform:uppercase;letter-spacing:.14em;font-size:12px;opacity:.85}
h1{margin:6px 0 4px;font-size:27px}h2{font-size:21px;margin:40px 0 10px;border-bottom:2px solid var(--brand);padding-bottom:6px}
h3{font-size:16px;margin:22px 0 8px}h4{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:0 0 6px}
.wrap{max-width:1060px;margin:0 auto;padding:0 20px 70px}.sub{opacity:.92;font-size:15px;max-width:840px}
.grid{display:grid;gap:12px}.g3{grid-template-columns:repeat(3,1fr)}.g2{grid-template-columns:repeat(2,1fr)}
@media(max-width:760px){.g3,.g2{grid-template-columns:1fr}}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:13px 15px}
.stat .k{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.06em}
.stat .v{font-size:25px;font-weight:700}.stat .d{font-size:13px;color:var(--mut)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);font-size:14px}
th{background:var(--code);text-transform:uppercase;letter-spacing:.05em;font-size:12px;color:var(--mut)}
td.num{text-align:right;font-variant-numeric:tabular-nums;font-weight:700}
.question{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:20px 0;overflow:hidden}
.question header{padding:13px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}
.q-skill{display:inline-block;background:var(--brand);color:#fff;border-radius:5px;padding:2px 8px;font-weight:600;font-size:12px;margin-right:6px}
.q-metrics{font-size:12px;color:var(--mut);display:flex;gap:12px;align-items:center}
.pill{display:inline-block;border-radius:20px;padding:1px 8px;font-size:11px;font-weight:600;margin:0 3px}
.pill.wrong{background:#fdecea;color:var(--wrong)}.pill.right{background:#e5f4ec;color:var(--right)}.pill.warn{background:#fdf1dc;color:var(--warn)}
.cols{display:grid;grid-template-columns:1.1fr 1fr}@media(max-width:820px){.cols{grid-template-columns:1fr}}
.col-passage,.col-item{padding:15px 16px}.col-passage{border-right:1px solid var(--line)}
@media(max-width:820px){.col-passage{border-right:none;border-bottom:1px solid var(--line)}}
.passage{font-size:14px;color:#2a3442}.stem{font-size:14px;font-weight:600}
.choice{padding:7px 10px;border-radius:7px;margin:6px 0;font-size:13.5px;border:1px solid var(--line);display:flex;gap:8px;align-items:flex-start}
.choice-letter{font-weight:700;color:var(--mut);min-width:14px}
.choice.wrong{background:#fdecea;border-color:#f2b8b0}.choice.right{background:#e5f4ec;border-color:#a8dcc2}
.diag{background:var(--code);border-top:1px solid var(--line);padding:15px 16px}
.diag-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:760px){.diag-grid{grid-template-columns:1fr}}
.diag-grid h4{color:var(--brand)}.diag-grid p{margin:0;font-size:13.5px}
.rationale{margin-top:13px;background:#fff;border:1px solid var(--line);border-radius:8px;padding:11px 13px;font-size:13.5px}
.rationale p{margin:0}.tactic{margin:11px 0 0;font-size:13px;color:var(--mut)}
.callout{background:#fff8ea;border:1px solid #f2dfa8;border-radius:10px;padding:13px 15px;font-size:14px;margin:14px 0}
footer{color:var(--mut);font-size:12px;margin-top:44px;border-top:1px solid var(--line);padding-top:12px}
ol.tight{padding-left:20px}ol.tight li{margin:6px 0}
"""


def build_report(since: str | None = None, out_name: str | None = None) -> Path:
    """Render the wrong-answer review; real (non-historical) attempts only."""
    since = (
        since
        or (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=REPORT_DAYS)).isoformat()
    )
    now = datetime.datetime.now(datetime.UTC)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        agg = conn.execute(
            """SELECT q.official_domain AS dom, q.official_skill AS sk, q.module
               FROM attempts a JOIN questions q ON q.id=a.question_id
               WHERE a.correct=0 AND a.mode != 'historical' AND a.attempted_at > ?""",
            (since,),
        ).fetchall()
        total = len(agg)
        dom = collections.Counter((r["dom"] or "Unspecified") for r in agg)
        sk = collections.Counter((r["sk"] or "Unspecified") for r in agg)
        mod = collections.Counter((r["module"] or "Unspecified") for r in agg)
        rw = sum(1 for r in agg if r["dom"] in _DOMAINS_RW)

        rows = conn.execute(
            """SELECT a.chosen_letter, a.time_ms, a.confidence, a.error_tags AS err, a.attempted_at,
                      q.fingerprint, q.source_test, q.source_question_number AS qnum, q.module,
                      q.passage, q.stem, q.choices_json, q.correct_letter, q.rationale,
                      q.official_domain AS dom, q.official_skill AS sk, q.difficulty
               FROM attempts a JOIN questions q ON q.id=a.question_id
               WHERE a.correct=0 AND a.mode != 'historical' AND a.attempted_at > ?
                 AND q.official_domain IN (?, ?) AND (a.time_ms OR 0) > 0
               ORDER BY q.official_skill, a.attempted_at""",
            (since, *_DOMAINS_RW),
        ).fetchall()
    finally:
        conn.close()

    uniq: dict[str, dict] = {}
    repeats: collections.Counter = collections.Counter()
    for r in rows:
        repeats[r["fingerprint"]] += 1
        uniq.setdefault(r["fingerprint"], dict(r))
    items = list(uniq.values())
    rank = {s: i for i, s in enumerate(_SKILLS_RW)}
    items.sort(key=lambda r: (rank.get(r["sk"], 99), r["attempted_at"]))
    repeated = {fp for fp, n in repeats.items() if n > 1}

    cats = collections.Counter()
    for r in items:
        d = _diag_for(r["fingerprint"]) or _framework(r["sk"])
        cats[d.get("cat", "Relationship / proof-check miss")] += 1

    cards = _render_cards(items, repeated)
    since_txt = since[:16]
    stamp = now.strftime("%Y%m%d-%H%M")
    name = out_name or f"weekly-{stamp}.html"
    path = OUT_DIR / name
    path.write_text(
        f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SAT Wrong-Answer Review</title><style>{_CSS}</style></head><body>
<div class="hero"><div class="wrap" style="padding:0">
  <div class="kicker">satprep · periodic review</div>
  <h1>Wrong-Answer Review</h1>
  <div class="sub">Diagnosed with the SAT llm-wiki framework (Analyze → Predict → Eliminate;
  dumb summaries; strong-word and trap-answer checks). Wrong answers since <b>{_esc(since_txt)}</b>.
  Generated {_esc(now.strftime("%Y-%m-%d %H:%M"))} UTC.</div>
</div></div>
<div class="wrap">
<h2>1 · At a glance</h2>
<div class="grid g3">
  <div class="stat"><div class="k">Wrong answers</div><div class="v">{total}</div><div class="d">all subjects since last report</div></div>
  <div class="stat"><div class="k">Reading &amp; Writing misses</div><div class="v">{rw}</div><div class="d">domains the framework teaches</div></div>
  <div class="stat"><div class="k">Deep-dived questions</div><div class="v">{len(items)}</div><div class="d">unique items with timing</div></div>
</div>
<div class="grid g3" style="margin-top:12px">
  <div><h3>By domain</h3><table><tr><th>Domain</th><th>#</th></tr>{_agg_rows(dom)}</table></div>
  <div><h3>By skill</h3><table><tr><th>Skill</th><th>#</th></tr>{_agg_rows(sk)}</table></div>
  <div><h3>By module</h3><table><tr><th>Module</th><th>#</th></tr>{_agg_rows(mod)}</table></div>
</div>
<div class="callout">The llm-wiki strategy stack is a reading &amp; writing framework. Reading
items are diagnosed in full below; math and grammar/conventions losses are counted above but have
no framework coverage and are not deep-dived.</div>

<h2>2 · How the framework catches a miss</h2>
<ol class="tight">
  <li><b>Analyze</b> — name the task and relationship being tested before reading choices.</li>
  <li><b>Dumb-summarize</b> — compress to a logical skeleton, preserving direction, scope, and negation.</li>
  <li><b>Predict before choices</b> — write the sentence the blank needs; most misses were predictable from the passage.</li>
  <li><b>Each choice is a compound claim</b> — one unsupported part is enough to eliminate it.</li>
  <li><b>Strong-word scan</b> — flag <i>most, all, never, rather than, exclusively</i>: wording that raises the required proof.</li>
</ol>

<h2>3 · Traps this period</h2>
<table><tr><th>Trap</th><th>#</th></tr>{_agg_rows(cats)}</table>

<h2>4 · Question-by-question</h2>
{cards}
</div>
<footer>Framework = medium-confidence instructor strategy (unofficial), not College Board policy.
Source: {_esc(str(OUT_DIR))}.</footer>
</body></html>""",
        encoding="utf-8",
    )
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--days",
        type=int,
        default=REPORT_DAYS,
        help=f"trailing window in days (default {REPORT_DAYS})",
    )
    ap.add_argument("--since", help="ISO cutoff; overrides --days")
    ap.add_argument("--out", help="output filename under data/reports/")
    args = ap.parse_args()

    since = args.since
    if since is None:
        since = (
            datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=args.days)
        ).isoformat()
    path = build_report(since=since, out_name=args.out)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
