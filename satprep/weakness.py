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
from datetime import datetime

from . import config
from .db import connect


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        from datetime import timezone as _tz
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


def _difficulty_bonus(rows) -> float:
    hard_seen = sum(1 for r in rows if (r["difficulty"] or "") == "hard")
    return min(6.0, 1.2 * hard_seen)


def compute_weakness(conn=None, now: datetime | None = None) -> dict:
    """Compute weakness scores for skills, reasoning tags, error tags.

    Returns {entity_type: {entity: {'score': float, 'stats': {...}}}}
    and persists to weakness_cache.
    """
    own = conn is None
    conn = conn or connect()
    now = now or datetime.now().astimezone()

    base_sql = """
        SELECT a.correct AS correct, a.confidence AS confidence,
               a.attempted_at AS attempted_at, q.difficulty AS difficulty
        FROM attempts a JOIN questions q ON q.id=a.question_id
        WHERE q.active=1 AND q.official_skill != 'Math'
          AND ({join}) = ? AND ({cond})
    """

    def collect(join_clause: str, cond: str, entity_value: str):
        sql = base_sql.format(join=join_clause, cond=cond)
        rows = conn.execute(sql, (entity_value,)).fetchall()
        alpha_wrong = 0.0
        alpha_right = 0.0
        n = 0.0
        wrong = correct = 0
        recent_wrong = 0.0
        for r in rows:
            w = _weight_for(r["correct"], r["confidence"] or 0)
            decay = _recency(r["attempted_at"], now)
            weighted = w * decay
            n += decay if r["correct"] else weighted
            if r["correct"]:
                # correct answers shrink toward mastery via their own weight
                alpha_right += weighted
                correct += 1
                if decay > 0.8:
                    pass
            else:
                alpha_wrong += weighted
                wrong += 1
                recent_wrong += weighted
        return {
            "n": round(n, 3),
            "n_raw": len(rows),
            "wrong": wrong,
            "correct": correct,
            "alpha_wrong": round(alpha_wrong, 3),
            "alpha_right": round(alpha_right, 3),
            "recent_wrong": round(recent_wrong, 3),
        }, rows

    out = {"skill": {}, "tag": {}, "error_tag": {}}
    k = config.WEAKNESS_PRIOR_STRENGTH
    prior_p = 0.25  # expected error rate for a strong student

    def score_from(stats: dict, rows) -> tuple[float, dict]:
        denom = stats["alpha_wrong"] + stats["alpha_right"] + k
        post_err = (stats["alpha_wrong"] + k * prior_p) / denom if denom else prior_p
        score = post_err * 100.0
        score += _difficulty_bonus(rows)
        # mastery discount when recent history is confidently correct-heavy
        if stats["correct"] >= 4 and stats["wrong"] == 0:
            score *= (1 - config.MASTERY_RECENT_CORRECT_DISCOUNT)
        # shrink excess-over-baseline by evidence volume so a 2-question bucket
        # cannot outrank a 40-question signal scraped from the same source mix
        baseline = prior_p * 100.0
        vol = max(stats.get("n_raw", stats["n"]), 1)
        shrink = min(1.0, vol / (vol + config.WEAKNESS_SHRINK_N))
        score = baseline + (score - baseline) * shrink
        return round(min(100.0, score), 1), stats

    # official skills
    for skill_row in conn.execute(
        """SELECT DISTINCT q.official_skill AS s FROM questions q
           WHERE q.active=1 AND q.official_skill != ''"""
    ).fetchall():
        skill = skill_row["s"]
        stats, rows = collect("q.official_skill", "a.mode='historical' OR 1=1", skill)
        if stats["n"] == 0:
            continue
        sc, st = score_from(stats, rows)
        out["skill"][skill] = {"score": sc, **st}

    # reasoning tags (demand tags)
    tag_rows = conn.execute(
        """SELECT qt.tag AS tag, a.correct AS correct, a.confidence AS confidence,
                  a.attempted_at AS attempted_at, q.difficulty AS difficulty
           FROM question_tags qt
           JOIN questions q ON q.id=qt.question_id AND q.active=1
           LEFT JOIN attempts a ON a.question_id=q.id"""
    ).fetchall()
    per_tag: dict[str, list] = {}
    for r in tag_rows:
        per_tag.setdefault(r["tag"], []).append(r)
    for tag, trows in per_tag.items():
        seen_ids = set()
        alpha_wrong = alpha_right = 0.0
        wrong = correct = 0
        hard_wrong = 0
        now_decayed_n = 0.0
        for r in trows:
            if r["correct"] is None:
                continue
            key = id(r)
            w = _weight_for(r["correct"], r["confidence"] or 0)
            decay = _recency(r["attempted_at"], now)
            if r["correct"]:
                alpha_right += w * decay
                correct += 1
            else:
                alpha_wrong += w * decay
                wrong += 1
                if (r["difficulty"] or "") == "hard":
                    hard_wrong += 1
            now_decayed_n += decay
            seen_ids.add(key)
        stats = {"n": round(now_decayed_n, 3), "wrong": wrong, "correct": correct,
                 "hard_questions_wrong": hard_wrong,
                 "alpha_wrong": round(alpha_wrong, 3)}
        if now_decayed_n == 0:
            continue
        denom = alpha_wrong + alpha_right + k
        post_err = (alpha_wrong + k * prior_p) / denom
        score = post_err * 100.0 + min(6.0, 1.2 * hard_wrong)
        if correct >= 4 and wrong == 0:
            score *= (1 - config.MASTERY_RECENT_CORRECT_DISCOUNT)
        baseline = prior_p * 100.0
        vol = max(wrong + correct, 1)
        shrink = min(1.0, vol / (vol + config.WEAKNESS_SHRINK_N))
        score = baseline + (score - baseline) * shrink
        out["tag"][tag] = {"score": round(min(100.0, score), 1), **stats}

    # diagnosed student error tags
    err_rows = conn.execute(
        """SELECT set_.tag AS tag, COUNT(*) AS cnt FROM (
               SELECT setag.tag AS tag, setag.question_id AS qid
               FROM student_error_tags setag
           ) set_ GROUP BY set_.tag"""
    ).fetchall()
    for r in err_rows:
        out["error_tag"][r["tag"]] = {"score": 0.0, "occurrences": r["cnt"]}

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
    conn.commit()
    if own:
        conn.close()
    return out


def get_weakness(db_path=None) -> dict:
    conn = connect(db_path)
    cached = conn.execute("SELECT entity_type, entity, score, stats_json FROM weakness_cache").fetchall()
    result: dict[str, dict] = {}
    for row in cached:
        result.setdefault(row["entity_type"], {})[row["entity"]] = {
            "score": row["score"],
            **json.loads(row["stats_json"]),
        }
    conn.close()
    return result


if __name__ == "__main__":
    scores = compute_weakness()
    for etype in ("skill", "tag"):
        ranked = sorted(scores[etype].items(), key=lambda kv: -kv[1]["score"])
        print(f"--- top {etype} weaknesses ---")
        for name, payload in ranked[:10]:
            print(f"{payload['score']:5.1f}  {name}  ({payload.get('wrong', 0)}w/{payload.get('wrong', 0)+payload.get('correct', 0)}n)")
