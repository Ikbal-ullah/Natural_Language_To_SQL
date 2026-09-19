-- 0007_column_semantics.sql
-- Column semantics live in the database catalog, not in a Python dictionary.
--
-- The schema serialiser (src/nl2sql/schema.py) reads these COMMENTs straight out of
-- pg_description and puts them in the LLM prompt.  One source of truth: if you add a
-- column in a migration and comment it here, the model learns about it on the next
-- request with no code change and no risk of the prompt describing a schema that no
-- longer exists.  A hardcoded dict in the prompt file WILL drift from the real schema,
-- and a model reasoning about a stale schema generates confidently wrong SQL.

COMMENT ON TABLE  learners IS 'One row per human using the platform. Mentors are learners with is_mentor = true.';
COMMENT ON COLUMN learners.id IS 'Surrogate primary key.';
COMMENT ON COLUMN learners.email IS 'Login address, unique across the platform.';
COMMENT ON COLUMN learners.full_name IS 'Display name as entered by the learner.';
COMMENT ON COLUMN learners.country_code IS 'ISO 3166-1 alpha-2 country of residence, uppercase, e.g. US, IN, DE.';
COMMENT ON COLUMN learners.timezone IS 'IANA timezone name used to render schedules.';
COMMENT ON COLUMN learners.status IS 'Lifecycle state. active = currently learning; paused = temporarily stopped; churned = left the platform; banned = removed for conduct.';
COMMENT ON COLUMN learners.is_mentor IS 'True if this learner also mentors others. Join mentor_sessions.mentor_id to learners.id for mentor details.';
COMMENT ON COLUMN learners.signup_ts IS 'When the account was created. Use this for cohort/vintage analysis of signups.';
COMMENT ON COLUMN learners.last_active_ts IS 'Last time the learner opened the platform. NULL means never active since signup.';

COMMENT ON TABLE  courses IS 'The syllabus catalogue. A course is reusable; each scheduled run of it is a cohort.';
COMMENT ON COLUMN courses.slug IS 'URL-safe unique identifier, e.g. systems-programming-2.';
COMMENT ON COLUMN courses.title IS 'Human-readable course name. Match user phrases like "the Python course" against this.';
COMMENT ON COLUMN courses.subject IS 'Broad topic area, e.g. Programming, Data, Security, Design.';
COMMENT ON COLUMN courses.difficulty IS 'beginner | intermediate | advanced.';
COMMENT ON COLUMN courses.list_price_cents IS 'Sticker price in USD cents. Divide by 100.0 for dollars. Actual money paid is in payments.amount_cents.';
COMMENT ON COLUMN courses.duration_weeks IS 'Nominal length of the course in weeks.';
COMMENT ON COLUMN courses.is_active IS 'False means retired from the catalogue; existing cohorts still run.';
COMMENT ON COLUMN courses.published_at IS 'When the course first became available.';

COMMENT ON TABLE  cohorts IS 'One scheduled run of a course, with dates, a capacity and a mentor.';
COMMENT ON COLUMN cohorts.course_id IS 'The course being run. FK to courses.id.';
COMMENT ON COLUMN cohorts.name IS 'Intake label, unique within a course, e.g. "2025-W03".';
COMMENT ON COLUMN cohorts.starts_on IS 'First day of the cohort (date, not timestamp).';
COMMENT ON COLUMN cohorts.ends_on IS 'Last day of the cohort. Always later than starts_on.';
COMMENT ON COLUMN cohorts.capacity IS 'Maximum seats. Compare against a COUNT of enrollments to find full cohorts.';
COMMENT ON COLUMN cohorts.status IS 'scheduled | running | finished | cancelled.';
COMMENT ON COLUMN cohorts.mentor_id IS 'Lead mentor for the cohort. FK to learners.id, NULL if unassigned.';

COMMENT ON TABLE  enrollments IS 'A learner taking one cohort. The hub table: submissions, mentor_sessions and most payments hang off it.';
COMMENT ON COLUMN enrollments.learner_id IS 'FK to learners.id.';
COMMENT ON COLUMN enrollments.cohort_id IS 'FK to cohorts.id.';
COMMENT ON COLUMN enrollments.course_id IS 'DENORMALISED copy of cohorts.course_id, kept correct by a composite foreign key. Prefer joining enrollments.course_id directly to courses.id instead of going through cohorts.';
COMMENT ON COLUMN enrollments.status IS 'active | completed | dropped | deferred. Completion rate = completed / all.';
COMMENT ON COLUMN enrollments.enrolled_at IS 'When the learner joined the cohort.';
COMMENT ON COLUMN enrollments.completed_at IS 'Set only when status = completed; NULL otherwise.';
COMMENT ON COLUMN enrollments.final_grade IS 'Final percentage 0-100, NULL until awarded.';

COMMENT ON TABLE  assignments IS 'A piece of work belonging to a course. Shared by every cohort of that course.';
COMMENT ON COLUMN assignments.course_id IS 'FK to courses.id.';
COMMENT ON COLUMN assignments.position IS 'Order within the course, 1-based and unique per course.';
COMMENT ON COLUMN assignments.kind IS 'quiz | coding | essay | project.';
COMMENT ON COLUMN assignments.max_score IS 'Maximum attainable raw score. Percentage = submissions.score / assignments.max_score * 100.';
COMMENT ON COLUMN assignments.due_offset_days IS 'Days after the cohort start date that this assignment is due.';
COMMENT ON COLUMN assignments.is_optional IS 'True if it does not count towards completion.';

COMMENT ON TABLE  submissions IS 'Fact table, ~5 million rows: one row per attempt at an assignment. Highly skewed -- a few enrollments have hundreds of submissions and most have very few. Always constrain this table before aggregating it.';
COMMENT ON COLUMN submissions.enrollment_id IS 'FK to enrollments.id. Join through enrollments to reach the learner or the course.';
COMMENT ON COLUMN submissions.assignment_id IS 'FK to assignments.id.';
COMMENT ON COLUMN submissions.attempt_no IS 'Retry counter starting at 1. The latest attempt is the one with the highest attempt_no for an (enrollment_id, assignment_id) pair.';
COMMENT ON COLUMN submissions.status IS 'submitted | graded | late | plagiarised. Only graded rows are guaranteed to have a score.';
COMMENT ON COLUMN submissions.score IS 'Raw score, NULL until graded. Compare against assignments.max_score, not against 100.';
COMMENT ON COLUMN submissions.submitted_at IS 'When the learner submitted. Use for time-series questions about activity.';
COMMENT ON COLUMN submissions.graded_at IS 'When a grade was recorded. NULL if not yet graded. Turnaround = graded_at - submitted_at.';
COMMENT ON COLUMN submissions.runtime_ms IS 'For coding assignments, how long the submitted program ran in the autograder. NULL for non-coding kinds.';

COMMENT ON TABLE  mentor_sessions IS 'One-to-one mentoring calls attached to an enrollment.';
COMMENT ON COLUMN mentor_sessions.enrollment_id IS 'FK to enrollments.id -- identifies both the learner and the cohort.';
COMMENT ON COLUMN mentor_sessions.mentor_id IS 'FK to learners.id, the mentor who ran the session. Self-referencing: learners appear on both sides.';
COMMENT ON COLUMN mentor_sessions.scheduled_at IS 'Start time of the call.';
COMMENT ON COLUMN mentor_sessions.duration_min IS 'Booked length in minutes, 15 to 240.';
COMMENT ON COLUMN mentor_sessions.status IS 'scheduled | completed | no_show | cancelled.';
COMMENT ON COLUMN mentor_sessions.learner_rating IS 'Learner satisfaction 1-5. Only ever set on completed sessions, so an average rating needs no status filter -- but a session COUNT does.';

COMMENT ON TABLE  payments IS 'Money received. Amounts are integer USD cents.';
COMMENT ON COLUMN payments.learner_id IS 'Who paid. FK to learners.id.';
COMMENT ON COLUMN payments.enrollment_id IS 'What was paid for, if it maps to one enrollment. NULL for subscriptions and credit packs.';
COMMENT ON COLUMN payments.amount_cents IS 'Gross amount charged in cents. Net revenue = amount_cents - refunded_amount_cents.';
COMMENT ON COLUMN payments.currency IS 'ISO 4217 code. Seeded data is USD only.';
COMMENT ON COLUMN payments.status IS 'pending | succeeded | failed | refunded. Only succeeded and refunded rows represent money that actually moved; revenue questions must filter on this.';
COMMENT ON COLUMN payments.method IS 'card | bank_transfer | paypal | employer_invoice.';
COMMENT ON COLUMN payments.created_at IS 'When the charge was attempted.';
COMMENT ON COLUMN payments.settled_at IS 'When funds cleared. NULL if never settled.';
COMMENT ON COLUMN payments.refunded_amount_cents IS 'Partial or full refund, always <= amount_cents. Non-zero only when status = refunded.';
COMMENT ON COLUMN payments.external_ref IS 'Payment-processor reference, unique when present.';
