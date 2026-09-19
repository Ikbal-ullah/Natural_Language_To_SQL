-- 0009_performance_indexes.sql
--
-- This migration is DELIBERATELY NOT APPLIED BY DEFAULT.
--
-- `make migrate` stops at 0008, which leaves the database in the state real systems are
-- in on day one: primary keys and unique constraints are indexed (PostgreSQL builds
-- those automatically), and nothing else is.  Foreign key columns in particular have no
-- index -- PostgreSQL does not create one for you, which surprises people coming from
-- MySQL/InnoDB, and is the single most common cause of a slow join in the wild.
--
-- db/indexing/run_exercise.py measures the ten queries in db/indexing/queries.sql against
-- that bare state, applies this file, and measures again.  Run it before reading further:
--     make index-exercise
-- The before/after EXPLAIN plans and timings land in BENCHMARKS.md.
--
-- Each index below states what it is for and why its columns are in that order.  An index
-- whose justification you cannot write down in one sentence is an index you should not
-- have: every one of them slows down every INSERT and UPDATE to the table and occupies
-- storage forever.

-- ---------------------------------------------------------------------------
-- Foreign key columns.  Needed for two separate reasons:
--   (a) joins in the child -> parent direction,
--   (b) DELETEs on the parent, which must scan the child to enforce the FK.  Without an
--       index that scan is a full sequential scan of a 5M-row table per deleted parent row.
-- ---------------------------------------------------------------------------
CREATE INDEX ix_enrollments_learner       ON enrollments (learner_id);
CREATE INDEX ix_enrollments_cohort        ON enrollments (cohort_id);
CREATE INDEX ix_cohorts_course            ON cohorts (course_id);
CREATE INDEX ix_assignments_course        ON assignments (course_id);
CREATE INDEX ix_mentor_sessions_enrollment ON mentor_sessions (enrollment_id);
CREATE INDEX ix_mentor_sessions_mentor    ON mentor_sessions (mentor_id);
CREATE INDEX ix_payments_learner          ON payments (learner_id);
CREATE INDEX ix_payments_enrollment       ON payments (enrollment_id);

-- ---------------------------------------------------------------------------
-- submissions: the 5M-row fact table.  Column order is the whole lesson here.
--
-- (enrollment_id, submitted_at) -- NOT (submitted_at, enrollment_id).
-- A B-tree is sorted by the first column, then within equal first-column values by the
-- second.  Think of a phone book sorted by surname then first name: "everyone called
-- Patel, most recent first" is one contiguous slice you can jump straight to.  "Everyone
-- called Amit, any surname" is not contiguous at all -- you would read the whole book.
-- Our access pattern is always "this enrollment, over this period", so the equality
-- column goes first and the range column second.  Reverse them and the index still gets
-- used for date-only queries but stops helping the per-enrollment ones, which are the
-- common case.  Exercise D1 in drills/ makes you measure exactly this.
-- ---------------------------------------------------------------------------
CREATE INDEX ix_submissions_enrollment_time ON submissions (enrollment_id, submitted_at DESC);

-- Assignment-level analytics ("average score on assignment 7").  status is second because
-- questions always name the assignment and only sometimes filter the status.
CREATE INDEX ix_submissions_assignment_status ON submissions (assignment_id, status);

-- Time-series scans over the whole table ("submissions per week").  BRIN, not B-tree:
-- submissions are inserted roughly in submitted_at order, so physically adjacent rows
-- have adjacent timestamps.  BRIN stores one min/max summary per 128-page range, which
-- makes it about 1/400th the size of the equivalent B-tree here (~25 kB vs ~110 MB) at
-- the cost of being useless if the physical order is ever shuffled.  This is the one
-- index in the file whose tradeoff is genuinely load-bearing -- see LEARN.md.
CREATE INDEX ix_submissions_submitted_brin ON submissions USING brin (submitted_at) WITH (pages_per_range = 128);

-- ---------------------------------------------------------------------------
-- COVERING index.  INCLUDE puts learner_id in the index leaf without making it part of
-- the sort key.  "How many learners completed course 12" is then answered by walking one
-- contiguous slice of this index and never opening the table itself -- an Index Only Scan.
-- Without INCLUDE, PostgreSQL finds the right index entries and then does one random heap
-- read per matching row purely to fetch learner_id, which is where the time goes.
-- Cost: an extra 4 bytes per index entry, and the index has to be rewritten when
-- learner_id changes (it never does -- it is part of a unique key).
-- ---------------------------------------------------------------------------
CREATE INDEX ix_enrollments_course_status_cover ON enrollments (course_id, status) INCLUDE (learner_id);

-- ---------------------------------------------------------------------------
-- PARTIAL index.  Only ~6% of payments are refunded, so an index over the whole table to
-- answer refund questions is 94% dead weight.  The WHERE clause both shrinks the index
-- and makes it cheaper to maintain: rows that do not match are never touched by it.
-- The catch: the planner can only use it when it can PROVE your query's WHERE implies the
-- index's WHERE.  `status = 'refunded'` matches; `status <> 'succeeded'` does not.
-- ---------------------------------------------------------------------------
CREATE INDEX ix_payments_refunded ON payments (learner_id, created_at) WHERE status = 'refunded';

-- Revenue by time, restricted to money that actually moved.
CREATE INDEX ix_payments_succeeded_created ON payments (created_at) WHERE status = 'succeeded';

-- ---------------------------------------------------------------------------
-- Case-insensitive email lookup.  A plain index on (email) CANNOT serve
-- `WHERE lower(email) = ...`: the index stores the raw value, and the planner will not
-- assume lower() is order-preserving (it is not).  Indexing the EXPRESSION stores the
-- already-lowered value, and now the lookup is a single B-tree descent.
-- ---------------------------------------------------------------------------
CREATE INDEX ix_learners_lower_email ON learners (lower(email));

-- Sparse index: only mentors, which are ~2% of learners.
CREATE INDEX ix_learners_mentors ON learners (id) WHERE is_mentor;

-- Signup vintage analysis, and the status filter that always accompanies it.
CREATE INDEX ix_learners_status_signup ON learners (status, signup_ts DESC);

ANALYZE;
