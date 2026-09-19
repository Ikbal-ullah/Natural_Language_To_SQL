-- Ten queries that are slow on an unindexed schema.
--
-- Format: each block starts with "-- name:" then "-- lesson:" then the SQL, terminated
-- by a semicolon. db/indexing/run_exercise.py parses this file, so keep the shape.
--
-- These are not artificial. Every one is a question the NL-to-SQL system generates from
-- an ordinary user question, which is the point: the index set in migration 0009 exists
-- because of these access patterns, not the other way round.

-- name: q01_email_lookup
-- lesson: Expression index. A plain index on (email) cannot serve lower(email) = ...,
-- lesson: because the index stores the raw value and lower() is not order-preserving.
SELECT id, full_name, status
FROM learners
WHERE lower(email) = 'dara.zhang48211@example.com';

-- name: q02_enrollments_in_cohort
-- lesson: Two lessons at once. (a) PostgreSQL does not index foreign key columns for you.
-- lesson: (b) The LEADING COLUMN rule: enrollments already has a unique index on
-- lesson: (learner_id, cohort_id), and it is useless here, because cohort_id is its
-- lesson: SECOND column. An index is sorted by its first column; asking for one value of
-- lesson: the second column means the matching entries are scattered through the whole
-- lesson: index. Filtering by learner_id instead would have been fast all along.
SELECT id, learner_id, course_id, status, enrolled_at
FROM enrollments
WHERE cohort_id = 73;

-- name: q03_latest_submissions
-- lesson: An index can supply ORDER BY, not just filtering. This one is deliberately the
-- lesson: WEAKEST result in the set, and it is the most useful. submissions already has a
-- lesson: unique index on (enrollment_id, assignment_id, attempt_no), whose leading column
-- lesson: already answers "which rows belong to this enrollment" -- so the new
-- lesson: (enrollment_id, submitted_at DESC) index adds nothing to the FILTER. All it adds
-- lesson: is the ordering: the rows come out sorted, so LIMIT 10 stops after ten index
-- lesson: entries instead of fetching all 800 and sorting them. A near-duplicate of an
-- lesson: index you already have earns a small win, not a large one, and still costs full
-- lesson: price on every INSERT. Measure before you add one.
SELECT id, assignment_id, attempt_no, score, submitted_at
FROM submissions
WHERE enrollment_id = 171300
ORDER BY submitted_at DESC
LIMIT 10;

-- name: q04_course_completions
-- lesson: Covering index. INCLUDE (learner_id) lets this be answered from the index alone
-- lesson: -- an Index Only Scan with no heap fetch at all.
SELECT count(DISTINCT learner_id) AS completed_learners
FROM enrollments
WHERE course_id = 12 AND status = 'completed';

-- name: q05_weekly_submission_counts
-- lesson: BRIN. submitted_at is physically correlated with row order (0.9999), so a
-- lesson: 32 kB block-range index -- 4,700x smaller than the equivalent B-tree -- lets the
-- lesson: scan skip almost the whole 772 MB table. Keep the window narrow when you read
-- lesson: this result: widen it to two months and the win drops to 1.3x, because the cost
-- lesson: moves from the scan to sorting 344,000 rows on disk. The index still did its
-- lesson: job; the bottleneck simply moved. LEARN.md annotates that plan line by line.
SELECT date_trunc('day', submitted_at) AS day, count(*) AS submissions
FROM submissions
WHERE submitted_at >= TIMESTAMPTZ '2024-02-01'
  AND submitted_at <  TIMESTAMPTZ '2024-02-08'
GROUP BY day
ORDER BY day;

-- name: q06_refund_report
-- lesson: Partial index. Only ~6% of payments are refunded, so indexing only those rows
-- lesson: gives an index a fraction of the size that is also cheaper to maintain.
SELECT count(*) AS refunds,
       sum(refunded_amount_cents) / 100.0 AS refunded_usd
FROM payments
WHERE status = 'refunded'
  AND created_at >= TIMESTAMPTZ '2024-01-01';

-- name: q07_mentor_workload
-- lesson: Another unindexed foreign key, this time on a self-referencing relationship.
SELECT ms.status, count(*) AS sessions, avg(ms.duration_min) AS avg_minutes
FROM mentor_sessions ms
WHERE ms.mentor_id = 98916
GROUP BY ms.status;

-- name: q08_assignment_scores
-- lesson: Equality column first, then the secondary filter. Answers a question about one
-- lesson: assignment without reading the other 4,990,000 submissions.
SELECT count(*) AS graded, round(avg(score), 2) AS avg_score
FROM submissions
WHERE assignment_id = 220 AND status = 'graded';

-- name: q09_course_submission_stats
-- lesson: THE HONEST NEGATIVE RESULT. Indexes exist that could serve this join, and the
-- lesson: planner refuses them -- correctly. Course 7 has 5,390 enrollments and 133,492
-- lesson: submissions. A nested loop would do 5,062 index descents and then ~125k random
-- lesson: heap fetches; submissions has a physical correlation of 0.006 on enrollment_id,
-- lesson: so those fetches really are random. One sequential 5M-row scan reads pages in
-- lesson: order and is genuinely cheaper. "Add an index" is not a fix for every slow
-- lesson: query, and a planner that ignores your index is often smarter than you.
SELECT count(*) AS submissions,
       round(avg(s.score), 2) AS avg_score,
       count(DISTINCT s.enrollment_id) AS enrollments
FROM submissions s
JOIN enrollments e ON e.id = s.enrollment_id
WHERE e.course_id = 7;

-- name: q10_recent_active_signups
-- lesson: An index can supply ORDER BY for free. With (status, signup_ts DESC) the LIMIT
-- lesson: stops after 20 index entries instead of sorting 62,000 rows.
SELECT id, full_name, signup_ts
FROM learners
WHERE status = 'active'
ORDER BY signup_ts DESC
LIMIT 20;
