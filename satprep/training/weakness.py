"""Personal weakness model (spec sections 5, 8, 12).

weakness(entity) in [0, 100], built from a recency-weighted Bayesian error
rate over ALL historical attempts (not just errors), adjusted by difficulty
and confidence signals:

    posterior_error = (alpha_wrong + prior_strength * prior_p) / n_total
      where each attempt contributes weight w:
        wrong + conf3  -> 1.6   (confidently wrong: strongest signal)
        wrong + other  -> 1.0
        correct+conf3  -> LOW_CONF_CORRECT_WEIGHT * mastery discount
        correct+conf12 -> 0.55  (lucky/guessing correct proves little)
      and decays by half-life WEAKNESS_RECENCY_HALF_LIFE_DAYS.

A tag with 1/1 wrong cannot outrank 8/20 wrong: smoothing + the fact that
correct-but-low-confidence questions keep contributing partial evidence.
Recent confident-correct streaks subtract a mastery discount.
"""

import json
import math
from collections import defaultdict
from datetime import datetime

from .. import config


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        return ts.astimezone()
    return ts


def _weight_for(correct: int, confidence: int) -> float:
    """Evidence weight of one attempt.

    Wrong attempts push alpha_wrong (confidently wrong pushes hardest).
    Correct answers push alpha_right - i.e. credit toward mastery. A
    proven-correct answer earns full credit; a guessed-correct answer only
    partial, so shaky categories remain trainable.
    """
    if not correct:
        return config.CONFIDENT_WRONG_MULTIPLIER if confidence >= 3 else 1.0
    return 1.0 if confidence >= 3 else config.LOW_CONF_CORRECT_WEIGHT


def _recency(attempted_at: str | None, now: datetime) -> float:
    ts = _parse_ts(attempted_at)
    if ts is None:
        return 0.55  # old data counts partially
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    return math.pow(0.5, age_days / config.WEAKNESS_RECENCY_HALF_LIFE_DAYS)


def compute_weakness(conn, now: datetime | None = None) -> dict:  # noqa: C901 (legacy: per-entity recency/multiplication math)
    """Compute weakness scores for skills, reasoning tags, error tags.

    Returns {entity_type: {entity: {'score': float, 'stats': {...}}}}
    and persists to weakness_cache.
    """
    now = now or datetime.now().astimezone()

    base_sql = """
        SELECT a.correct AS correct, a.confidence AS confidence,
               a.attempted_at AS attempted_at, a.time_ms AS time_ms,
               q.difficulty AS difficulty
        FROM attempts a JOIN questions q ON q.id=a.question_id
        WHERE q.active=1 AND ({join}) = ?
    """

    SLOW_CORRECT_MS = config.SLOW_CORRECT_THRESHOLD_S * 1000

    def collect(join_clause: str, entity_value: str):
        rows = conn.execute(base_sql.format(join=join_clause), (entity_value,)).fetchall()
        alpha_wrong = alpha_right = 0.0
        slow_penalty = 0.0
        n = wrong = correct = 0.0
        times = []
        for r in rows:
            w = _weight_for(r["correct"], r["confidence"] or 0)
            decay = _recency(r["attempted_at"], now)
            weighted = w * decay
            n += decay if r["correct"] else weighted
            if r["correct"]:
                alpha_right += weighted
                correct += 1
                # spec section 12: a slow correct answer still indicates friction
                t = r["time_ms"] or 0
                if t >= SLOW_CORRECT_MS:
                    slow_penalty += 0.35
                    times.append(t)
            else:
                alpha_wrong += weighted
                wrong += 1
        return (
            {
                "n": round(n, 3),
                "n_raw": len(rows),
                "wrong": int(wrong),
                "correct": int(correct),
                "alpha_wrong": round(alpha_wrong, 3),
                "alpha_right": round(alpha_right, 3),
                "slow_correct": len(times),
            },
            rows,
            slow_penalty,
        )

    def score_from(stats: dict, slow_penalty: float = 0.0) -> tuple[float, dict]:
        denom = stats["alpha_wrong"] + stats["alpha_right"] + k
        post_err = (stats["alpha_wrong"] + k * prior_p) / denom if denom else prior_p
        score = post_err * 100.0 + min(6.0, slow_penalty)
        if stats["correct"] >= 4 and stats["wrong"] == 0:
            score *= 1 - config.MASTERY_RECENT_CORRECT_DISCOUNT
        baseline = prior_p * 100.0
        vol = max(stats.get("n_raw", stats["n"]), 1)
        shrink = min(1.0, vol / (vol + config.WEAKNESS_SHRINK_N))
        score = baseline + (score - baseline) * shrink
        return round(min(100.0, score), 1), stats

    out = {"skill": {}, "tag": {}, "error_tag": {}}
    k = config.WEAKNESS_PRIOR_STRENGTH
    prior_p = 0.25  # expected error rate for a strong student

    # official skills
    for skill_row in conn.execute(
        """SELECT DISTINCT q.official_skill AS s FROM questions q
           WHERE q.active=1 AND q.official_skill != ''"""
    ).fetchall():
        skill = skill_row["s"]
        stats, _rows, slow_pen = collect("q.official_skill", skill)
        if stats["n"] == 0 and stats["n_raw"] == 0:
            continue
        sc, st = score_from(stats, slow_pen)
        out["skill"][skill] = {"score": sc, **st}

    # reasoning tags (demand tags)
    tag_rows = conn.execute(
        """SELECT qt.tag AS tag, a.correct AS correct, a.confidence AS confidence,
                  a.attempted_at AS attempted_at, a.time_ms AS time_ms,
                  q.difficulty AS difficulty
           FROM effective_question_tags qt
           JOIN questions q ON q.id=qt.question_id AND q.active=1
           LEFT JOIN attempts a ON a.question_id=q.id"""
    ).fetchall()
    per_tag: dict[str, list] = {}
    for r in tag_rows:
        per_tag.setdefault(r["tag"], []).append(r)
    for tag, trows in per_tag.items():
        alpha_wrong = alpha_right = 0.0
        wrong = correct = hard_wrong = slow_correct = 0
        now_decayed_n = 0.0
        for r in trows:
            if r["correct"] is None:
                continue
            w = _weight_for(r["correct"], r["confidence"] or 0)
            decay = _recency(r["attempted_at"], now)
            now_decayed_n += decay
            if r["correct"]:
                alpha_right += w * decay
                correct += 1
                if (r["time_ms"] or 0) >= config.SLOW_CORRECT_THRESHOLD_S * 1000:
                    slow_correct += 1
            else:
                alpha_wrong += w * decay
                wrong += 1
                if (r["difficulty"] or "") == "hard":
                    hard_wrong += 1
        stats = {
            "n": round(now_decayed_n, 3),
            "wrong": wrong,
            "correct": correct,
            "hard_questions_wrong": hard_wrong,
            "slow_correct": slow_correct,
            "alpha_wrong": round(alpha_wrong, 3),
            "alpha_right": round(alpha_right, 3),
        }
        if wrong + correct == 0:
            continue
        sc, st = score_from(stats, min(6.0, 1.2 * hard_wrong + 0.3 * slow_correct))
        out["tag"][tag] = {"score": sc, **stats}

    # diagnosed student error tags
    err_rows = conn.execute(
        """SELECT set_.tag AS tag, set_.qid AS qid FROM (
               SELECT setag.tag AS tag, setag.question_id AS qid
               FROM student_error_tags setag
           ) set_ GROUP BY set_.tag, set_.qid"""
    ).fetchall()
    # Score error tags from actual attempt outcomes on questions that
    # carry the diagnosis. This makes the error-tag weakness signal
    # real (not a constant 0.0), so remediation can differentiate
    # candidates by the student's actual error pattern.
    tag_qids: dict[str, list[int]] = defaultdict(list)
    for r in err_rows:
        tag_qids[r["tag"]].append(r["qid"])
    for tag, qids in tag_qids.items():
        alpha_wrong = alpha_right = 0.0
        wrong = correct = 0
        now_decayed_n = 0.0
        for r in conn.execute(
            """SELECT a.correct AS correct, a.confidence AS confidence,
                      a.attempted_at AS attempted_at, a.time_ms AS time_ms
               FROM attempts a
               JOIN questions q ON q.id=a.question_id AND q.active=1
               WHERE a.question_id IN ({})""".format(",".join("?" * len(qids))),
            qids,
        ).fetchall():
            w = _weight_for(r["correct"], r["confidence"] or 0)
            decay = _recency(r["attempted_at"], now)
            now_decayed_n += decay
            if r["correct"]:
                alpha_right += w * decay
                correct += 1
            else:
                alpha_wrong += w * decay
                wrong += 1
        stats = {
            "n": round(now_decayed_n, 3),
            "wrong": wrong,
            "correct": correct,
            "alpha_wrong": round(alpha_wrong, 3),
            "alpha_right": round(alpha_right, 3),
        }
        if wrong + correct == 0:
            continue
        sc, st = score_from(stats, 0.0)
        out["error_tag"][tag] = {"score": sc, **stats, "question_ids": qids}

    # persist cache
    conn.execute("DELETE FROM weakness_cache")
    ts = now.isoformat()
    for etype, entities in out.items():
        for entity, payload in entities.items():
            conn.execute(
                """INSERT OR REPLACE INTO weakness_cache (entity_type, entity, score, stats_json, computed_at)
                   VALUES (?,?,?,?,?)""",
                (etype, entity, payload.get("score", 0.0), json.dumps(payload), ts),
            )
    return out


_ENTITIES_WITH_EVIDENCE = {
    "skill": """SELECT DISTINCT q.official_skill AS entity
                FROM attempts a JOIN questions q ON q.id=a.question_id
                WHERE q.active=1 AND q.official_skill != ''""",
    "tag": """SELECT DISTINCT qt.tag AS entity
              FROM attempts a
              JOIN questions q ON q.id=a.question_id AND q.active=1
              JOIN effective_question_tags qt ON qt.question_id=q.id""",
}


def ensure_current(conn) -> None:
    """Refresh the cache up front if it does not cover everything with evidence.

    Callers that assemble several sections from one profile call this before
    reading any of them. Without it, a lazy refresh triggered partway through
    - say a newly ingested tag missing from the cache - rewrites scores the
    earlier sections have already read, and one response ends up showing two
    different model snapshots.
    """
    for entity_type, sql in _ENTITIES_WITH_EVIDENCE.items():
        expected = {r["entity"] for r in conn.execute(sql)}
        cached = {
            r["entity"]
            for r in conn.execute(
                "SELECT entity FROM weakness_cache WHERE entity_type=?", (entity_type,)
            )
        }
        if expected - cached:
            compute_weakness(conn)
            return


def risk_scores(conn, entity_type: str, entities=None) -> dict[str, float]:
    """Weakness score per entity of one type - the single source of "how risky".

    `weakness_cache` is a cache, not a second model: when `entities` names
    something the cache has never seen, this recomputes rather than handing
    the caller a hole to paper over with its own smoothing. Callers used to
    fall back to a locally-defined prior on a cache miss, which quietly put
    two differently-computed numbers in the same column.

    Recomputes at most once per call. An entity with no attempts at all is
    legitimately absent from the result; ask only about entities you have
    evidence for.
    """
    scores = {
        r["entity"]: r["score"]
        for r in conn.execute(
            "SELECT entity, score FROM weakness_cache WHERE entity_type=?", (entity_type,)
        )
    }
    if entities is not None and set(entities) - scores.keys():
        computed = compute_weakness(conn).get(entity_type, {})
        scores = {entity: payload["score"] for entity, payload in computed.items()}
    return scores


def cached_profile(conn, entity_type: str | None = None) -> dict:
    """Last computed profile with full stats, ranked most-at-risk first.

    Callers that want to render the profile - the weakness screen, the drill
    picker's weak-tag list - read it here rather than querying weakness_cache,
    so the cache stays an implementation detail of this module.
    """
    sql = "SELECT entity_type, entity, score, stats_json FROM weakness_cache"
    params: tuple = ()
    if entity_type is not None:
        sql += " WHERE entity_type=?"
        params = (entity_type,)
    result: dict[str, dict] = {}
    for row in conn.execute(sql + " ORDER BY score DESC", params):
        result.setdefault(row["entity_type"], {})[row["entity"]] = {
            "score": row["score"],
            **json.loads(row["stats_json"] or "{}"),
        }
    return result.get(entity_type, {}) if entity_type is not None else result


if __name__ == "__main__":
    from ..db import db_context

    with db_context() as _conn:
        scores = compute_weakness(_conn)
    for etype in ("skill", "tag"):
        ranked = sorted(scores[etype].items(), key=lambda kv: -kv[1]["score"])
        print(f"--- top {etype} weaknesses ---")
        for name, payload in ranked[:10]:
            print(
                f"{payload['score']:5.1f}  {name}  ({payload.get('wrong', 0)}w/{payload.get('wrong', 0) + payload.get('correct', 0)}n)"
            )
