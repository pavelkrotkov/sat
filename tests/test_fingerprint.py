from satprep.corpus.fingerprint import fingerprint, normalize_text, pool_for_fingerprint


def test_stable_across_unicode_and_whitespace():
    a = fingerprint("The cat \u2014 sat.  It ran.", "What  happened?", ["One", "Two"])
    b = fingerprint("The cat — sat.\nIt   ran.", "what happened? ", [" one", "two "])
    assert a == b


def test_different_choices_differ():
    base = ("passage", "stem", ["a", "b", "c", "d"])
    other = ("passage", "stem", ["a", "b", "c", "x"])
    assert fingerprint(*base) != fingerprint(*other)


def test_order_sensitive_choices_and_none_for_empty():
    assert fingerprint("", "", ["a", "b"]) != fingerprint("", "", ["b", "a"])
    assert fingerprint("", "", []) != fingerprint("", "", ["x"])


def test_pool_split_is_deterministic_and_roughly_quarter():
    fps = [fingerprint(f"p{i}", f"s{i}", [f"c{j}" for j in range(3)]) for i in range(400)]
    protected = sum(1 for fp in fps if pool_for_fingerprint(fp) == "protected_benchmark")
    # deterministic: same call twice identical
    assert all(pool_for_fingerprint(fp) == pool_for_fingerprint(fp) for fp in fps)
    assert 60 <= protected <= 140  # target 100 (25%)


def test_normalize_collapses_punct_spacing():
    assert normalize_text("word , next") == normalize_text("word, next")
