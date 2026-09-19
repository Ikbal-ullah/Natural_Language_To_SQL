"""Few-shot example bank and similarity-based selection.

WHY FEW-SHOT EXAMPLES AT ALL
----------------------------
The schema tells the model what exists. The examples tell it what good SQL *for this
database* looks like: that revenue means `amount_cents` filtered to succeeded payments,
that "top N per group" is a window function and not a correlated subquery, that dates are
bucketed with date_trunc. These are conventions, and conventions cannot be deduced from a
CREATE TABLE statement.

WHY SELECT THEM BY SIMILARITY RATHER THAN SENDING ALL OF THEM
-------------------------------------------------------------
Two reasons, and the second matters more than the first:
  1. Tokens. Every example is ~80 tokens of prompt on every request forever.
  2. Interference. A prompt containing twenty examples of every shape gives the model
     twenty competing templates. Sending the four nearest ones biases it toward the right
     shape. eval/sweep.py measures the curve; more is not monotonically better.

WHY LEXICAL SIMILARITY AND NOT EMBEDDINGS
-----------------------------------------
An embedding model is another network dependency, another failure mode in the request
path, and another thing to keep in sync. For a bank of a few dozen examples over a fixed
vocabulary of domain nouns -- learner, cohort, submission, refund -- IDF-weighted token
overlap is competitive and it is deterministic, which means the evaluation harness
measures the pipeline rather than the weather. If the bank grew to thousands of examples
this tradeoff would flip, and the selector interface is narrow enough to swap.

CONTAMINATION
-------------
These examples must never overlap the golden evaluation set. If a question in the eval
set also appears here, the score measures retrieval, not generation. tests/ asserts the
two sets are disjoint on every run.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_STOP = frozenset(
    {
        "the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "is", "are", "was",
        "were", "be", "by", "with", "how", "what", "which", "who", "whom", "that", "this",
        "show", "list", "give", "me", "get", "find", "many", "much", "do", "does", "did",
        "have", "has", "had", "there", "their", "from", "at", "as", "it", "its", "all",
        "each", "per", "any", "we", "our", "you", "your", "i",
    }
)


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens with stop words removed and simple plural folding."""
    out = []
    for raw in _TOKEN_RE.findall(text.lower()):
        if raw in _STOP or len(raw) < 2:
            continue
        # "submissions" and "submission" must match; a stemmer would be overkill and
        # would mangle domain nouns like "status".
        if len(raw) > 4 and raw.endswith("s") and not raw.endswith("ss"):
            raw = raw[:-1]
        out.append(raw)
    return out


@dataclass(frozen=True)
class FewShotExample:
    """One (question, SQL) pair, plus why the SQL is shaped that way."""

    question: str
    sql: str
    tags: tuple[str, ...] = ()
    note: str = ""


BANK: tuple[FewShotExample, ...] = (
    FewShotExample(
        question="How many learners signed up from Germany?",
        sql=(
            "SELECT count(*) AS learner_count\n"
            "FROM learners\n"
            "WHERE country_code = 'DE'\n"
            "LIMIT 100"
        ),
        tags=("filter", "count"),
        note="Country is an ISO alpha-2 code, not a country name.",
    ),
    FewShotExample(
        question="List the 10 most recently published courses with their price in dollars.",
        sql=(
            "SELECT title, published_at, list_price_cents / 100.0 AS price_usd\n"
            "FROM courses\n"
            "ORDER BY published_at DESC\n"
            "LIMIT 10"
        ),
        tags=("filter", "order", "money"),
        note="Money is stored in cents; divide by 100.0 to report dollars.",
    ),
    FewShotExample(
        question="Which courses have the most enrollments?",
        sql=(
            "SELECT c.title, count(*) AS enrollment_count\n"
            "FROM enrollments e\n"
            "JOIN courses c ON c.id = e.course_id\n"
            "GROUP BY c.title\n"
            "ORDER BY enrollment_count DESC\n"
            "LIMIT 10"
        ),
        tags=("join", "group_by", "count"),
        note="Join enrollments straight to courses via the denormalised course_id.",
    ),
    FewShotExample(
        question="What is the completion rate for each course?",
        sql=(
            "SELECT c.title,\n"
            "       count(*) FILTER (WHERE e.status = 'completed') AS completed,\n"
            "       count(*) AS total,\n"
            "       round(100.0 * count(*) FILTER (WHERE e.status = 'completed')"
            " / count(*), 2) AS completion_pct\n"
            "FROM enrollments e\n"
            "JOIN courses c ON c.id = e.course_id\n"
            "GROUP BY c.title\n"
            "ORDER BY completion_pct DESC\n"
            "LIMIT 50"
        ),
        tags=("join", "group_by", "ratio", "filter_clause"),
        note="FILTER (WHERE ...) computes a conditional count in one pass over the group.",
    ),
    FewShotExample(
        question="Show total revenue by month.",
        sql=(
            "SELECT date_trunc('month', created_at) AS month,\n"
            "       sum(amount_cents - refunded_amount_cents) / 100.0 AS net_revenue_usd\n"
            "FROM payments\n"
            "WHERE status IN ('succeeded', 'refunded')\n"
            "GROUP BY month\n"
            "ORDER BY month\n"
            "LIMIT 100"
        ),
        tags=("aggregate", "time_series", "money"),
        note="Revenue must exclude failed and pending payments and subtract refunds.",
    ),
    FewShotExample(
        question="Which mentors have the highest average session rating?",
        sql=(
            "SELECT m.full_name,\n"
            "       round(avg(ms.learner_rating), 2) AS avg_rating,\n"
            "       count(*) AS rated_sessions\n"
            "FROM mentor_sessions ms\n"
            "JOIN learners m ON m.id = ms.mentor_id\n"
            "WHERE ms.learner_rating IS NOT NULL\n"
            "GROUP BY m.full_name\n"
            "HAVING count(*) >= 20\n"
            "ORDER BY avg_rating DESC\n"
            "LIMIT 10"
        ),
        tags=("join", "group_by", "having", "self_reference"),
        note="HAVING filters groups after aggregation; a minimum count avoids a mentor "
        "with one 5-star session topping the list.",
    ),
    FewShotExample(
        question="For each course, who are the top 3 learners by final grade?",
        sql=(
            "WITH ranked AS (\n"
            "  SELECT e.course_id, e.learner_id, e.final_grade,\n"
            "         row_number() OVER (PARTITION BY e.course_id"
            " ORDER BY e.final_grade DESC) AS rn\n"
            "  FROM enrollments e\n"
            "  WHERE e.status = 'completed' AND e.final_grade IS NOT NULL\n"
            ")\n"
            "SELECT c.title, l.full_name, r.final_grade\n"
            "FROM ranked r\n"
            "JOIN courses c ON c.id = r.course_id\n"
            "JOIN learners l ON l.id = r.learner_id\n"
            "WHERE r.rn <= 3\n"
            "ORDER BY c.title, r.final_grade DESC\n"
            "LIMIT 200"
        ),
        tags=("window", "cte", "join", "top_n_per_group"),
        note="Top-N-per-group is a window function in a CTE, filtered outside it -- "
        "window functions cannot be used in WHERE on the same query level.",
    ),
    FewShotExample(
        question="What is the average number of submissions per enrollment?",
        sql=(
            "SELECT round(avg(n), 2) AS avg_submissions_per_enrollment\n"
            "FROM (\n"
            "  SELECT enrollment_id, count(*) AS n\n"
            "  FROM submissions\n"
            "  GROUP BY enrollment_id\n"
            ") per_enrollment\n"
            "LIMIT 1"
        ),
        tags=("aggregate", "subquery"),
        note="Averaging a per-group count needs two levels of aggregation.",
    ),
    FewShotExample(
        question="Which learners have never submitted anything?",
        sql=(
            "SELECT l.id, l.full_name\n"
            "FROM learners l\n"
            "JOIN enrollments e ON e.learner_id = l.id\n"
            "LEFT JOIN submissions s ON s.enrollment_id = e.id\n"
            "WHERE s.id IS NULL\n"
            "LIMIT 100"
        ),
        tags=("left_join", "anti_join"),
        note="LEFT JOIN plus IS NULL on the right side is an anti-join: rows with no match.",
    ),
    FewShotExample(
        question="How many submissions were made each week in 2024?",
        sql=(
            "SELECT date_trunc('week', submitted_at) AS week, count(*) AS submissions\n"
            "FROM submissions\n"
            "WHERE submitted_at >= '2024-01-01' AND submitted_at < '2025-01-01'\n"
            "GROUP BY week\n"
            "ORDER BY week\n"
            "LIMIT 100"
        ),
        tags=("time_series", "aggregate", "range"),
        note="A half-open range [start, end) avoids the boundary bugs BETWEEN causes with "
        "timestamps.",
    ),
    FewShotExample(
        question="What percentage of submissions on each assignment were late?",
        sql=(
            "SELECT a.title,\n"
            "       round(100.0 * count(*) FILTER (WHERE s.status = 'late')"
            " / count(*), 2) AS late_pct\n"
            "FROM submissions s\n"
            "JOIN assignments a ON a.id = s.assignment_id\n"
            "GROUP BY a.title\n"
            "ORDER BY late_pct DESC\n"
            "LIMIT 20"
        ),
        tags=("join", "group_by", "ratio"),
        note="Multiply by 100.0 not 100 -- integer division would floor the result to 0.",
    ),
    FewShotExample(
        question="Show each learner's running total of submissions over time.",
        sql=(
            "SELECT e.learner_id, s.submitted_at,\n"
            "       count(*) OVER (PARTITION BY e.learner_id"
            " ORDER BY s.submitted_at ROWS UNBOUNDED PRECEDING) AS running_total\n"
            "FROM submissions s\n"
            "JOIN enrollments e ON e.id = s.enrollment_id\n"
            "WHERE e.learner_id = 1\n"
            "ORDER BY s.submitted_at\n"
            "LIMIT 100"
        ),
        tags=("window", "running_total", "join"),
        note="A running total is a window frame, not a GROUP BY.",
    ),
    FewShotExample(
        question="Which cohorts are over 90% full?",
        sql=(
            "SELECT co.name, c.title, count(e.id) AS enrolled, co.capacity\n"
            "FROM cohorts co\n"
            "JOIN courses c ON c.id = co.course_id\n"
            "LEFT JOIN enrollments e ON e.cohort_id = co.id\n"
            "GROUP BY co.id, co.name, c.title, co.capacity\n"
            "HAVING count(e.id) > 0.9 * co.capacity\n"
            "ORDER BY enrolled DESC\n"
            "LIMIT 50"
        ),
        tags=("join", "group_by", "having", "left_join"),
        note="LEFT JOIN keeps empty cohorts; count(e.id) counts matches, count(*) would "
        "count the placeholder row too.",
    ),
    FewShotExample(
        question="What is the median final grade for completed enrollments?",
        sql=(
            "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY final_grade)"
            " AS median_grade\n"
            "FROM enrollments\n"
            "WHERE status = 'completed' AND final_grade IS NOT NULL\n"
            "LIMIT 1"
        ),
        tags=("aggregate", "percentile"),
        note="There is no median() in PostgreSQL; percentile_cont is the ordered-set "
        "aggregate that does it.",
    ),
    FewShotExample(
        question="Compare each learner's grade to their cohort average.",
        sql=(
            "SELECT e.learner_id, e.cohort_id, e.final_grade,\n"
            "       round(avg(e.final_grade) OVER (PARTITION BY e.cohort_id), 2)"
            " AS cohort_avg,\n"
            "       round(e.final_grade - avg(e.final_grade)"
            " OVER (PARTITION BY e.cohort_id), 2) AS delta\n"
            "FROM enrollments e\n"
            "WHERE e.final_grade IS NOT NULL\n"
            "ORDER BY delta DESC\n"
            "LIMIT 100"
        ),
        tags=("window", "comparison"),
        note="A window aggregate keeps every row while adding the group statistic; "
        "GROUP BY would collapse the rows you want to compare.",
    ),
    FewShotExample(
        question="How many distinct learners submitted work in March 2024?",
        sql=(
            "SELECT count(DISTINCT e.learner_id) AS active_learners\n"
            "FROM submissions s\n"
            "JOIN enrollments e ON e.id = s.enrollment_id\n"
            "WHERE s.submitted_at >= '2024-03-01' AND s.submitted_at < '2024-04-01'\n"
            "LIMIT 1"
        ),
        tags=("join", "distinct", "range"),
        note="A learner with several enrollments would be counted twice without DISTINCT.",
    ),
    FewShotExample(
        question="Which payment methods are most likely to fail?",
        sql=(
            "SELECT method,\n"
            "       count(*) FILTER (WHERE status = 'failed') AS failed,\n"
            "       count(*) AS attempted,\n"
            "       round(100.0 * count(*) FILTER (WHERE status = 'failed')"
            " / count(*), 2) AS failure_pct\n"
            "FROM payments\n"
            "GROUP BY method\n"
            "ORDER BY failure_pct DESC\n"
            "LIMIT 20"
        ),
        tags=("group_by", "ratio"),
        note="Failure rate is a ratio over all attempts, so no status filter in WHERE.",
    ),
    FewShotExample(
        question="Show the average score on each assignment as a percentage of the maximum.",
        sql=(
            "SELECT a.title,\n"
            "       round(avg(100.0 * s.score / a.max_score), 2) AS avg_pct\n"
            "FROM submissions s\n"
            "JOIN assignments a ON a.id = s.assignment_id\n"
            "WHERE s.status = 'graded'\n"
            "GROUP BY a.title\n"
            "ORDER BY avg_pct\n"
            "LIMIT 20"
        ),
        tags=("join", "group_by", "ratio"),
        note="Scores are out of assignments.max_score (20 for quizzes), never out of 100.",
    ),
    FewShotExample(
        question="Find learners enrolled in more than three courses.",
        sql=(
            "SELECT l.full_name, count(DISTINCT e.course_id) AS courses\n"
            "FROM learners l\n"
            "JOIN enrollments e ON e.learner_id = l.id\n"
            "GROUP BY l.id, l.full_name\n"
            "HAVING count(DISTINCT e.course_id) > 3\n"
            "ORDER BY courses DESC\n"
            "LIMIT 100"
        ),
        tags=("join", "group_by", "having", "distinct"),
        note="Group by the primary key as well as the name -- two learners can share a name.",
    ),
    FewShotExample(
        question="What is the average grading turnaround time in hours?",
        sql=(
            "SELECT round(avg(EXTRACT(EPOCH FROM (graded_at - submitted_at)) / 3600.0), 2)"
            " AS avg_hours\n"
            "FROM submissions\n"
            "WHERE graded_at IS NOT NULL\n"
            "LIMIT 1"
        ),
        tags=("aggregate", "interval"),
        note="Subtracting timestamps gives an interval; EXTRACT(EPOCH ...) turns it into "
        "seconds you can average.",
    ),
    FewShotExample(
        question="Which learners paid the most in total?",
        sql=(
            "SELECT l.full_name, sum(p.amount_cents) / 100.0 AS total_usd\n"
            "FROM payments p\n"
            "JOIN learners l ON l.id = p.learner_id\n"
            "WHERE p.status = 'succeeded'\n"
            "GROUP BY l.id, l.full_name\n"
            "ORDER BY total_usd DESC\n"
            "LIMIT 10"
        ),
        tags=("join", "group_by", "money"),
        note="",
    ),
    FewShotExample(
        question="How many learners are in each status?",
        sql=(
            "SELECT status, count(*) AS learners\n"
            "FROM learners\n"
            "GROUP BY status\n"
            "ORDER BY learners DESC\n"
            "LIMIT 10"
        ),
        tags=("group_by", "count"),
        note="",
    ),
    FewShotExample(
        question="Show the latest attempt for each assignment by enrollment 500.",
        sql=(
            "SELECT DISTINCT ON (s.assignment_id)\n"
            "       s.assignment_id, s.attempt_no, s.score, s.submitted_at\n"
            "FROM submissions s\n"
            "WHERE s.enrollment_id = 500\n"
            "ORDER BY s.assignment_id, s.attempt_no DESC\n"
            "LIMIT 100"
        ),
        tags=("distinct_on", "latest_per_group"),
        note="DISTINCT ON is the PostgreSQL shortcut for latest-row-per-group; the ORDER "
        "BY must start with the DISTINCT ON columns.",
    ),
    FewShotExample(
        question="Which subjects generate the most revenue per enrollment?",
        sql=(
            "SELECT c.subject,\n"
            "       sum(p.amount_cents) / 100.0 AS revenue_usd,\n"
            "       count(DISTINCT e.id) AS enrollments,\n"
            "       round(sum(p.amount_cents) / 100.0 / count(DISTINCT e.id), 2)"
            " AS revenue_per_enrollment\n"
            "FROM payments p\n"
            "JOIN enrollments e ON e.id = p.enrollment_id\n"
            "JOIN courses c ON c.id = e.course_id\n"
            "WHERE p.status = 'succeeded'\n"
            "GROUP BY c.subject\n"
            "ORDER BY revenue_per_enrollment DESC\n"
            "LIMIT 20"
        ),
        tags=("join", "group_by", "money", "three_table"),
        note="count(DISTINCT e.id) because one enrollment can have several payments.",
    ),
)


@lru_cache(maxsize=1)
def _idf() -> dict[str, float]:
    """Inverse document frequency over the example bank.

    Without IDF, a question containing 'course' matches every example equally well,
    because 'course' appears everywhere. IDF makes rare, discriminating words --
    'refund', 'median', 'running' -- carry the weight.
    """
    n = len(BANK)
    df: Counter[str] = Counter()
    for ex in BANK:
        df.update(set(tokenize(ex.question)) | set(ex.tags))
    return {term: math.log((n + 1) / (count + 1)) + 1.0 for term, count in df.items()}


def score_example(question_tokens: set[str], example: FewShotExample) -> float:
    """IDF-weighted overlap, normalised so long examples are not favoured."""
    idf = _idf()
    ex_tokens = set(tokenize(example.question)) | set(example.tags)
    shared = question_tokens & ex_tokens
    if not shared:
        return 0.0
    numerator = sum(idf.get(t, 1.0) for t in shared)
    denominator = math.sqrt(sum(idf.get(t, 1.0) for t in ex_tokens)) or 1.0
    return numerator / denominator


def select_examples(
    question: str, k: int = 4, bank: tuple[FewShotExample, ...] = BANK
) -> list[FewShotExample]:
    """Return the k examples most similar to the question, most similar first.

    Ties break on the example's position in the bank so the selection is deterministic:
    an evaluation that silently reorders its own prompt is measuring noise.
    """
    if k <= 0:
        return []
    tokens = set(tokenize(question))
    scored = [
        (score_example(tokens, ex), -i, ex) for i, ex in enumerate(bank)
    ]
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    # Always return k, even when some score zero. A question with no lexical overlap
    # still benefits from seeing what well-formed SQL for this schema looks like, and a
    # variable-length prompt makes the sweep's few-shot-count axis meaningless.
    return [ex for _, _, ex in scored[:k]]
