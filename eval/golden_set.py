"""The golden evaluation set: 60 questions with reference answers.

CATEGORIES
  simple_filter  10  one table, a WHERE clause, maybe an ORDER BY
  join           10  three or more tables joined
  aggregation    12  GROUP BY, HAVING, conditional aggregates
  window         10  OVER (...), ranking, running totals, per-group comparisons
  subquery_cte    8  CTEs, correlated subqueries, anti-joins, set operations
  ambiguous      10  deliberately under-specified; the correct answer is a QUESTION

WHY THE AMBIGUOUS TEN EXIST
  A text-to-SQL system that always returns SQL is not 100% useful, it is untrustworthy:
  when a question genuinely has two readings, silently choosing one and presenting a
  number is the worst possible behaviour, because the number looks exactly as confident as
  a correct one. These ten are scored inverted -- the system passes by REFUSING to answer
  and asking a clarifying question. A system that scores 50/50 on the other categories and
  0/10 here is worse in production than one that scores 40/50 and 10/10.

  Each ambiguous question names the specific fork the system is supposed to notice, so the
  scoring is not "did it get nervous" but "did it find the real ambiguity".

CONTAMINATION
  No question here appears in the few-shot bank in src/nl2sql/examples.py. Tests assert
  it on every run. Without that check the evaluation slowly turns into a measurement of
  how well we copied the answers into the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Category = Literal[
    "simple_filter", "join", "aggregation", "window", "subquery_cte", "ambiguous"
]
CATEGORIES: tuple[Category, ...] = (
    "simple_filter", "join", "aggregation", "window", "subquery_cte", "ambiguous",
)


@dataclass(frozen=True)
class GoldenQuestion:
    id: str
    question: str
    category: Category
    sql: str | None = None
    expects_clarification: bool = False
    ambiguity: str = ""
    order_matters: bool = False
    note: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


G = GoldenQuestion

SIMPLE_FILTER: tuple[GoldenQuestion, ...] = (
    G(
        "sf01", "How many learners have a banned account?", "simple_filter",
        "SELECT count(*) AS banned_learners FROM learners WHERE status = 'banned' LIMIT 100",
    ),
    G(
        "sf02", "List the slugs of all advanced courses that are no longer active.",
        "simple_filter",
        "SELECT slug FROM courses WHERE difficulty = 'advanced' AND is_active = false "
        "LIMIT 100",
    ),
    G(
        "sf03", "How many learners are based in India?", "simple_filter",
        "SELECT count(*) AS learners FROM learners WHERE country_code = 'IN' LIMIT 100",
    ),
    G(
        "sf04", "How many cohorts were cancelled?", "simple_filter",
        "SELECT count(*) AS cancelled FROM cohorts WHERE status = 'cancelled' LIMIT 100",
    ),
    G(
        "sf05", "What are the titles of the five most expensive courses?", "simple_filter",
        "SELECT title FROM courses ORDER BY list_price_cents DESC, title LIMIT 5",
        order_matters=True,
    ),
    G(
        "sf06", "How many payments failed?", "simple_filter",
        "SELECT count(*) AS failed FROM payments WHERE status = 'failed' LIMIT 100",
    ),
    G(
        "sf07", "How many mentor sessions ended in a no-show?", "simple_filter",
        "SELECT count(*) AS no_shows FROM mentor_sessions WHERE status = 'no_show' LIMIT 100",
    ),
    G(
        "sf08", "How many assignments are marked optional?", "simple_filter",
        "SELECT count(*) AS optional_assignments FROM assignments WHERE is_optional LIMIT 100",
    ),
    G(
        "sf09", "How many learners are also mentors?", "simple_filter",
        "SELECT count(*) AS mentors FROM learners WHERE is_mentor LIMIT 100",
    ),
    G(
        "sf10", "How many enrollments were deferred?", "simple_filter",
        "SELECT count(*) AS deferred FROM enrollments WHERE status = 'deferred' LIMIT 100",
    ),
)

JOIN: tuple[GoldenQuestion, ...] = (
    G(
        "jn01",
        "For each course subject, how many submissions have been made in total?",
        "join",
        "SELECT c.subject, count(*) AS submissions "
        "FROM submissions s "
        "JOIN enrollments e ON e.id = s.enrollment_id "
        "JOIN courses c ON c.id = e.course_id "
        "GROUP BY c.subject ORDER BY submissions DESC LIMIT 100",
        tags=("three_table",),
    ),
    G(
        "jn02",
        "Which course titles have learners from the most distinct countries?", "join",
        "SELECT c.title, count(DISTINCT l.country_code) AS countries "
        "FROM enrollments e "
        "JOIN learners l ON l.id = e.learner_id "
        "JOIN courses c ON c.id = e.course_id "
        "GROUP BY c.title ORDER BY countries DESC, c.title LIMIT 10",
        order_matters=True, tags=("three_table",),
    ),
    G(
        "jn03",
        "How much succeeded payment revenue in dollars has each course subject produced?",
        "join",
        "SELECT c.subject, sum(p.amount_cents) / 100.0 AS revenue_usd "
        "FROM payments p "
        "JOIN enrollments e ON e.id = p.enrollment_id "
        "JOIN courses c ON c.id = e.course_id "
        "WHERE p.status = 'succeeded' "
        "GROUP BY c.subject ORDER BY revenue_usd DESC LIMIT 100",
        tags=("three_table", "money"),
    ),
    G(
        "jn04",
        "For each cohort in the course titled 'SQL', how many learners enrolled?", "join",
        "SELECT co.name, count(e.id) AS enrolled "
        "FROM cohorts co "
        "JOIN courses c ON c.id = co.course_id "
        "LEFT JOIN enrollments e ON e.cohort_id = co.id "
        "WHERE c.title = 'SQL' "
        "GROUP BY co.id, co.name ORDER BY co.name LIMIT 100",
        order_matters=True, tags=("three_table", "left_join"),
    ),
    G(
        "jn05",
        "Which mentors ran sessions for learners in the Security subject, and how many?",
        "join",
        "SELECT m.full_name, count(*) AS sessions "
        "FROM mentor_sessions ms "
        "JOIN learners m ON m.id = ms.mentor_id "
        "JOIN enrollments e ON e.id = ms.enrollment_id "
        "JOIN courses c ON c.id = e.course_id "
        "WHERE c.subject = 'Security' "
        "GROUP BY m.id, m.full_name ORDER BY sessions DESC, m.full_name LIMIT 10",
        order_matters=True, tags=("four_table", "self_reference"),
    ),
    G(
        "jn06",
        "What is the average graded score percentage for each course difficulty level?",
        "join",
        "SELECT c.difficulty, round(avg(100.0 * s.score / a.max_score), 2) AS avg_pct "
        "FROM submissions s "
        "JOIN assignments a ON a.id = s.assignment_id "
        "JOIN courses c ON c.id = a.course_id "
        "WHERE s.status = 'graded' "
        "GROUP BY c.difficulty ORDER BY c.difficulty LIMIT 100",
        order_matters=True, tags=("three_table",),
    ),
    G(
        "jn07",
        "Which countries have the most completed enrollments?", "join",
        "SELECT l.country_code, count(*) AS completed "
        "FROM enrollments e JOIN learners l ON l.id = e.learner_id "
        "WHERE e.status = 'completed' "
        "GROUP BY l.country_code ORDER BY completed DESC, l.country_code LIMIT 10",
        order_matters=True,
    ),
    G(
        "jn08",
        "For each subject, how many distinct learners have ever enrolled?", "join",
        "SELECT c.subject, count(DISTINCT e.learner_id) AS learners "
        "FROM enrollments e JOIN courses c ON c.id = e.course_id "
        "GROUP BY c.subject ORDER BY learners DESC LIMIT 100",
    ),
    G(
        "jn09",
        "Which coding assignments in the Programming subject have the lowest average "
        "graded score percentage?",
        "join",
        "SELECT c.title AS course, a.title AS assignment, "
        "round(avg(100.0 * s.score / a.max_score), 2) AS avg_pct "
        "FROM submissions s "
        "JOIN assignments a ON a.id = s.assignment_id "
        "JOIN courses c ON c.id = a.course_id "
        "WHERE s.status = 'graded' AND a.kind = 'coding' AND c.subject = 'Programming' "
        "GROUP BY c.title, a.title ORDER BY avg_pct, c.title, a.title LIMIT 10",
        order_matters=True, tags=("three_table",),
    ),
    G(
        "jn10",
        "How many mentor sessions did learners in each course subject receive?", "join",
        "SELECT c.subject, count(*) AS sessions "
        "FROM mentor_sessions ms "
        "JOIN enrollments e ON e.id = ms.enrollment_id "
        "JOIN courses c ON c.id = e.course_id "
        "GROUP BY c.subject ORDER BY sessions DESC LIMIT 100",
        tags=("three_table",),
    ),
)

AGGREGATION: tuple[GoldenQuestion, ...] = (
    G(
        "ag01", "How many enrollments are there in each status?", "aggregation",
        "SELECT status, count(*) AS n FROM enrollments GROUP BY status "
        "ORDER BY n DESC LIMIT 100",
    ),
    G(
        "ag02",
        "Which countries have more than 2000 learners? Show the country and the count.",
        "aggregation",
        "SELECT country_code, count(*) AS learners FROM learners "
        "GROUP BY country_code HAVING count(*) > 2000 "
        "ORDER BY learners DESC, country_code LIMIT 100",
        order_matters=True, tags=("having",),
    ),
    G(
        "ag03", "What is the total net revenue in dollars across all payments?",
        "aggregation",
        "SELECT sum(amount_cents - refunded_amount_cents) / 100.0 AS net_revenue_usd "
        "FROM payments WHERE status IN ('succeeded', 'refunded') LIMIT 100",
        tags=("money",),
    ),
    G(
        "ag04", "How many submissions are there of each status?", "aggregation",
        "SELECT status, count(*) AS n FROM submissions GROUP BY status "
        "ORDER BY n DESC LIMIT 100",
    ),
    G(
        "ag05",
        "What is the average duration of completed mentor sessions, in minutes?",
        "aggregation",
        "SELECT round(avg(duration_min), 2) AS avg_minutes FROM mentor_sessions "
        "WHERE status = 'completed' LIMIT 100",
    ),
    G(
        "ag06",
        "For each assignment kind, how many submissions were plagiarised?", "aggregation",
        "SELECT a.kind, count(*) FILTER (WHERE s.status = 'plagiarised') AS plagiarised "
        "FROM submissions s JOIN assignments a ON a.id = s.assignment_id "
        "GROUP BY a.kind ORDER BY plagiarised DESC, a.kind LIMIT 100",
        order_matters=True, tags=("filter_clause",),
    ),
    G(
        "ag07",
        "What is the refund rate as a percentage for each payment method?", "aggregation",
        "SELECT method, "
        "round(100.0 * count(*) FILTER (WHERE status = 'refunded') / count(*), 2) AS refund_pct "
        "FROM payments GROUP BY method ORDER BY refund_pct DESC, method LIMIT 100",
        order_matters=True, tags=("ratio",),
    ),
    G(
        "ag08",
        "How many submissions were made in each calendar month of 2024?", "aggregation",
        "SELECT date_trunc('month', submitted_at) AS month, count(*) AS submissions "
        "FROM submissions "
        "WHERE submitted_at >= TIMESTAMPTZ '2024-01-01' "
        "AND submitted_at < TIMESTAMPTZ '2025-01-01' "
        "GROUP BY month ORDER BY month LIMIT 100",
        order_matters=True, tags=("time_series",),
    ),
    G(
        "ag09",
        "Which mentors have run more than 150 sessions in total?", "aggregation",
        "SELECT m.full_name, count(*) AS sessions "
        "FROM mentor_sessions ms JOIN learners m ON m.id = ms.mentor_id "
        "GROUP BY m.id, m.full_name HAVING count(*) > 150 "
        "ORDER BY sessions DESC, m.full_name LIMIT 100",
        order_matters=True, tags=("having",),
    ),
    G(
        "ag10",
        "What is the highest and lowest final grade awarded on completed enrollments?",
        "aggregation",
        "SELECT max(final_grade) AS highest, min(final_grade) AS lowest "
        "FROM enrollments WHERE status = 'completed' AND final_grade IS NOT NULL LIMIT 100",
    ),
    G(
        "ag11",
        "How many distinct learners submitted work in 2023?", "aggregation",
        "SELECT count(DISTINCT e.learner_id) AS learners "
        "FROM submissions s JOIN enrollments e ON e.id = s.enrollment_id "
        "WHERE s.submitted_at >= TIMESTAMPTZ '2023-01-01' "
        "AND s.submitted_at < TIMESTAMPTZ '2024-01-01' LIMIT 100",
        tags=("distinct",),
    ),
    G(
        "ag12",
        "What is the average number of assignments per course?", "aggregation",
        "SELECT round(avg(n), 2) AS avg_assignments FROM "
        "(SELECT course_id, count(*) AS n FROM assignments GROUP BY course_id) t LIMIT 100",
        tags=("nested_aggregate",),
    ),
)

WINDOW: tuple[GoldenQuestion, ...] = (
    G(
        "wn01",
        "For each course, list the top 2 cohorts by number of enrollments. Show course "
        "title, cohort name and the enrollment count.",
        "window",
        "WITH counts AS ("
        "  SELECT co.course_id, co.name, count(e.id) AS enrolled"
        "  FROM cohorts co LEFT JOIN enrollments e ON e.cohort_id = co.id"
        "  GROUP BY co.id, co.course_id, co.name"
        "), ranked AS ("
        "  SELECT course_id, name, enrolled,"
        "         row_number() OVER (PARTITION BY course_id ORDER BY enrolled DESC, name) AS rn"
        "  FROM counts"
        ") SELECT c.title, r.name, r.enrolled FROM ranked r "
        "JOIN courses c ON c.id = r.course_id WHERE r.rn <= 2 "
        "ORDER BY c.title, r.rn LIMIT 200",
        order_matters=True, tags=("top_n_per_group",),
    ),
    G(
        "wn02",
        "Rank course subjects by total succeeded revenue, showing the rank number.",
        "window",
        "SELECT c.subject, sum(p.amount_cents) / 100.0 AS revenue_usd, "
        "rank() OVER (ORDER BY sum(p.amount_cents) DESC) AS revenue_rank "
        "FROM payments p JOIN enrollments e ON e.id = p.enrollment_id "
        "JOIN courses c ON c.id = e.course_id WHERE p.status = 'succeeded' "
        "GROUP BY c.subject ORDER BY revenue_rank LIMIT 100",
        order_matters=True, tags=("rank", "aggregate_window"),
    ),
    G(
        "wn03",
        "For completed enrollments in course 12, show each learner's final grade and how "
        "far it is above or below the average grade for that course.",
        "window",
        "SELECT e.learner_id, e.final_grade, "
        "round(avg(e.final_grade) OVER (), 2) AS course_avg, "
        "round(e.final_grade - avg(e.final_grade) OVER (), 2) AS delta "
        "FROM enrollments e WHERE e.course_id = 12 AND e.status = 'completed' "
        "AND e.final_grade IS NOT NULL ORDER BY delta DESC, e.learner_id LIMIT 100",
        order_matters=True, tags=("window_aggregate",),
    ),
    G(
        "wn04",
        "Show the monthly count of succeeded payments in 2024 alongside a running "
        "cumulative total.",
        "window",
        "WITH m AS ("
        "  SELECT date_trunc('month', created_at) AS month, count(*) AS n"
        "  FROM payments WHERE status = 'succeeded'"
        "  AND created_at >= TIMESTAMPTZ '2024-01-01'"
        "  AND created_at < TIMESTAMPTZ '2025-01-01' GROUP BY month"
        ") SELECT month, n, sum(n) OVER (ORDER BY month ROWS UNBOUNDED PRECEDING) AS running "
        "FROM m ORDER BY month LIMIT 100",
        order_matters=True, tags=("running_total",),
    ),
    G(
        "wn05",
        "For each course subject, show the three most recently published courses with "
        "their publication order within the subject.",
        "window",
        "WITH r AS ("
        "  SELECT subject, title, published_at,"
        "         row_number() OVER (PARTITION BY subject ORDER BY published_at DESC, title) AS rn"
        "  FROM courses"
        ") SELECT subject, title, rn FROM r WHERE rn <= 3 ORDER BY subject, rn LIMIT 100",
        order_matters=True, tags=("top_n_per_group",),
    ),
    G(
        "wn06",
        "For assignment 307, show each graded submission's score and the difference from "
        "the previous attempt by the same enrollment, ordered by enrollment and attempt.",
        "window",
        "SELECT s.enrollment_id, s.attempt_no, s.score, "
        "s.score - lag(s.score) OVER (PARTITION BY s.enrollment_id ORDER BY s.attempt_no) "
        "AS delta_from_previous "
        "FROM submissions s WHERE s.assignment_id = 307 AND s.status = 'graded' "
        "ORDER BY s.enrollment_id, s.attempt_no LIMIT 200",
        order_matters=True, tags=("lag",),
    ),
    G(
        "wn07",
        "Split completed enrollments in course 5 into four quartiles by final grade and "
        "show the quartile for each learner.",
        "window",
        "SELECT learner_id, final_grade, "
        "ntile(4) OVER (ORDER BY final_grade) AS grade_quartile "
        "FROM enrollments WHERE course_id = 5 AND status = 'completed' "
        "AND final_grade IS NOT NULL ORDER BY final_grade, learner_id LIMIT 200",
        order_matters=True, tags=("ntile",),
    ),
    G(
        "wn08",
        "For each subject, show each course's enrollment count and that count as a "
        "percentage of the subject total.",
        "window",
        "WITH c AS ("
        "  SELECT co.subject, co.title, count(e.id) AS enrolled"
        "  FROM courses co LEFT JOIN enrollments e ON e.course_id = co.id"
        "  GROUP BY co.id, co.subject, co.title"
        ") SELECT subject, title, enrolled, "
        "round(100.0 * enrolled / sum(enrolled) OVER (PARTITION BY subject), 2) AS pct_of_subject "
        "FROM c ORDER BY subject, pct_of_subject DESC, title LIMIT 200",
        order_matters=True, tags=("window_aggregate", "ratio"),
    ),
    G(
        "wn09",
        "Show the 10 learners with the most enrollments, with a dense rank on the "
        "enrollment count.",
        "window",
        "WITH c AS ("
        "  SELECT learner_id, count(*) AS n FROM enrollments GROUP BY learner_id"
        ") SELECT learner_id, n, dense_rank() OVER (ORDER BY n DESC) AS rnk "
        "FROM c ORDER BY n DESC, learner_id LIMIT 10",
        order_matters=True, tags=("dense_rank",),
    ),
    G(
        "wn10",
        "For cohort 175, show each enrolled learner's final grade and the highest grade "
        "in that cohort.",
        "window",
        "SELECT learner_id, final_grade, "
        "max(final_grade) OVER () AS cohort_best "
        "FROM enrollments WHERE cohort_id = 175 "
        "ORDER BY final_grade DESC NULLS LAST, learner_id LIMIT 200",
        order_matters=True, tags=("window_aggregate",),
    ),
)

SUBQUERY_CTE: tuple[GoldenQuestion, ...] = (
    G(
        "sq01",
        "Which courses have never had a cancelled cohort? List their titles.",
        "subquery_cte",
        "SELECT c.title FROM courses c WHERE NOT EXISTS ("
        "  SELECT 1 FROM cohorts co WHERE co.course_id = c.id AND co.status = 'cancelled'"
        ") ORDER BY c.title LIMIT 100",
        order_matters=True, tags=("anti_join", "correlated"),
    ),
    G(
        "sq02",
        "Which learners have enrollments but have never made a payment? Give their ids.",
        "subquery_cte",
        "SELECT DISTINCT e.learner_id FROM enrollments e "
        "LEFT JOIN payments p ON p.learner_id = e.learner_id "
        "WHERE p.id IS NULL ORDER BY e.learner_id LIMIT 100",
        order_matters=True, tags=("anti_join",),
    ),
    G(
        "sq03",
        "Which courses have an above-average number of assignments? Show title and count.",
        "subquery_cte",
        "WITH per AS (SELECT course_id, count(*) AS n FROM assignments GROUP BY course_id) "
        "SELECT c.title, p.n FROM per p JOIN courses c ON c.id = p.course_id "
        "WHERE p.n > (SELECT avg(n) FROM per) ORDER BY p.n DESC, c.title LIMIT 100",
        order_matters=True, tags=("cte", "scalar_subquery"),
    ),
    G(
        "sq04",
        "How many learners have made more than five submissions in total?",
        "subquery_cte",
        "SELECT count(*) AS learners FROM ("
        "  SELECT e.learner_id FROM submissions s "
        "  JOIN enrollments e ON e.id = s.enrollment_id "
        "  GROUP BY e.learner_id HAVING count(*) > 5"
        ") t LIMIT 100",
        tags=("nested_aggregate",),
    ),
    G(
        "sq05",
        "List the titles of courses that have at least one cohort currently running.",
        "subquery_cte",
        "SELECT DISTINCT c.title FROM courses c "
        "WHERE EXISTS (SELECT 1 FROM cohorts co WHERE co.course_id = c.id "
        "AND co.status = 'running') ORDER BY c.title LIMIT 100",
        order_matters=True, tags=("semi_join", "correlated"),
    ),
    G(
        "sq06",
        "For the course with the most enrollments, what is its title and enrollment count?",
        "subquery_cte",
        "WITH counts AS ("
        "  SELECT course_id, count(*) AS n FROM enrollments GROUP BY course_id"
        ") SELECT c.title, counts.n FROM counts JOIN courses c ON c.id = counts.course_id "
        "WHERE counts.n = (SELECT max(n) FROM counts) LIMIT 100",
        tags=("cte", "scalar_subquery"),
    ),
    G(
        "sq07",
        "Which enrollments have more submissions than the overall average per enrollment? "
        "Return how many such enrollments there are.",
        "subquery_cte",
        "WITH per AS (SELECT enrollment_id, count(*) AS n FROM submissions GROUP BY enrollment_id) "
        "SELECT count(*) AS above_average FROM per WHERE n > (SELECT avg(n) FROM per) LIMIT 100",
        tags=("cte", "scalar_subquery"),
    ),
    G(
        "sq08",
        "List the titles of courses that have no cohort currently running.",
        "subquery_cte",
        "SELECT title FROM courses "
        "EXCEPT "
        "SELECT c.title FROM courses c JOIN cohorts co ON co.course_id = c.id "
        "WHERE co.status = 'running' "
        "ORDER BY title LIMIT 100",
        order_matters=True, tags=("set_operation",),
        note="Chosen over an EXCEPT that returns nothing: a reference answer of zero rows "
        "is passed by any query that also returns nothing, including a broken one.",
    ),
)

# ---------------------------------------------------------------------------
# The ambiguous ten. `sql` is None on purpose: there is no correct query, and writing
# one would mean asserting a reading the question does not support.
# ---------------------------------------------------------------------------
AMBIGUOUS: tuple[GoldenQuestion, ...] = (
    G(
        "am01", "Who are our best learners?", "ambiguous",
        expects_clarification=True,
        ambiguity="'Best' is undefined: highest final grade, most courses completed, "
        "most submissions, or most money paid. These rank learners differently.",
    ),
    G(
        "am02", "How many active users do we have?", "ambiguous",
        expects_clarification=True,
        ambiguity="'Active' collides with two things in the schema: learners.status = "
        "'active' (an account state) and 'submitted work recently' (behaviour). The two "
        "counts differ by an order of magnitude.",
    ),
    G(
        "am03", "What is our revenue?", "ambiguous",
        expects_clarification=True,
        ambiguity="Gross or net of refunds; all time or a period; succeeded only or "
        "including pending. Each choice changes the number materially.",
    ),
    G(
        "am04", "Show me the recent submissions.", "ambiguous",
        expects_clarification=True,
        ambiguity="'Recent' has no definition here -- last day, week, or month -- and the "
        "table has 5 million rows, so the window determines everything.",
    ),
    G(
        "am05", "How well is the Python course doing?", "ambiguous",
        expects_clarification=True,
        ambiguity="'Doing well' could mean completion rate, revenue, enrollment volume, "
        "average grade or mentor ratings. Also several course titles contain 'Python'.",
    ),
    G(
        "am06", "Which mentor is the worst?", "ambiguous",
        expects_clarification=True,
        ambiguity="'Worst' could be lowest average rating, highest no-show rate, or "
        "fewest sessions; and a mentor with two sessions should probably not be ranked "
        "against one with two hundred.",
    ),
    G(
        "am07", "How many people dropped out?", "ambiguous",
        expects_clarification=True,
        ambiguity="Count enrollments with status 'dropped', or distinct learners who "
        "dropped at least one, or learners whose account churned? Three different numbers.",
    ),
    G(
        "am08", "What is the average score?", "ambiguous",
        expects_clarification=True,
        ambiguity="Raw score across assignments with different max_score values is "
        "meaningless; it needs to be a percentage. It is also unclear whether ungraded "
        "and plagiarised submissions are included.",
    ),
    G(
        "am09", "Compare last month to the month before.", "ambiguous",
        expects_clarification=True,
        ambiguity="No metric is named at all, and 'last month' is relative to a date the "
        "question does not give.",
    ),
    G(
        "am10", "Which courses should we retire?", "ambiguous",
        expects_clarification=True,
        ambiguity="This asks for a business judgement, not a fact. There is no threshold "
        "in the schema for 'should retire'; low enrollment, low revenue and low "
        "completion all point at different courses.",
    ),
)

GOLDEN_SET: tuple[GoldenQuestion, ...] = (
    SIMPLE_FILTER + JOIN + AGGREGATION + WINDOW + SUBQUERY_CTE + AMBIGUOUS
)


def by_category() -> dict[str, list[GoldenQuestion]]:
    out: dict[str, list[GoldenQuestion]] = {c: [] for c in CATEGORIES}
    for q in GOLDEN_SET:
        out[q.category].append(q)
    return out


def get(question_id: str) -> GoldenQuestion:
    for q in GOLDEN_SET:
        if q.id == question_id:
            return q
    raise KeyError(question_id)
