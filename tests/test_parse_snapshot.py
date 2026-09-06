from satprep.corpus.parse_snapshot import parse_snapshot

INCORRECT = """
<div class="question-panel">
  <h3>Reading and Writing: Question 7</h3>
  <div><p>A fabricated passage about lanterns.</p></div>
  <div><p>What does the text most strongly suggest?</p></div>
</div>
<div class="answer-panel">
  <ol type="A">
    <li>First synthetic choice</li>
    <li class="correct">Second synthetic choice</li>
    <li>Third synthetic choice</li>
    <li>Fourth synthetic choice</li>
  </ol>
  <p class="incorrect response">You selected answer A. The correct answer is B.</p>
  <h3>Rationale</h3><div><p>Choice B follows from the fabricated passage.</p></div>
</div>
"""

CORRECT = """
<div class="question-panel">
  <h3>Reading and Writing: Question 1</h3>
  <div><p>A fabricated passage about maps.</p></div>
  <div><p>Which choice best completes the text?</p></div>
  <ol class="answer-options" type="A">
    <li>First synthetic choice</li>
    <li>Second synthetic choice</li>
    <li class="correct">Third synthetic choice</li>
    <li>Fourth synthetic choice</li>
  </ol>
</div>
<div class="answer-panel">
  <p class="correct response">You selected answer C.</p>
  <h3>Rationale</h3><div><p>Choice C completes the fabricated text.</p></div>
</div>
"""


def test_parses_incorrect_review_snapshot():
    parsed = parse_snapshot(INCORRECT)
    assert parsed.section == "Reading and Writing"
    assert parsed.question_number == "7"
    assert [choice["letter"] for choice in parsed.choices] == list("ABCD")
    assert parsed.correct_letter == "B"
    assert parsed.student_letter == "A"
    assert parsed.passage == "A fabricated passage about lanterns."
    assert parsed.stem == "What does the text most strongly suggest?"
    assert parsed.rationale == "Choice B follows from the fabricated passage."


def test_parses_correct_review_snapshot_with_choices():
    parsed = parse_snapshot(CORRECT)
    assert parsed.question_number == "1"
    assert parsed.student_letter == parsed.correct_letter == "C"
    assert [choice["letter"] for choice in parsed.choices] == list("ABCD")
    assert parsed.stem == "Which choice best completes the text?"
    assert parsed.rationale == "Choice C completes the fabricated text."


def test_snapshot_parse_is_deterministic():
    assert parse_snapshot(INCORRECT).__dict__ == parse_snapshot(INCORRECT).__dict__
