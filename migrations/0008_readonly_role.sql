-- 0008_readonly_role.sql
-- Defence in depth, layer 3.
--
-- Layer 1 is the prompt ("generate SELECT only") -- advisory, and an attacker talks to
-- the model, so it is not a control at all.
-- Layer 2 is the validator, which parses the SQL and rejects anything that is not a
-- read.  That is a real control, but it is our code, and our code has bugs.
-- Layer 3 is this file.  Even if layers 1 and 2 both fail completely and a DELETE
-- reaches the server, the server itself refuses it.  This is the layer that has to hold,
-- because it is the only one an attacker cannot reason their way around.
--
-- Three independent mechanisms, any one of which is sufficient:
--   1. The role is granted SELECT and nothing else -- no INSERT/UPDATE/DELETE/TRUNCATE.
--   2. default_transaction_read_only forces every transaction it opens to be read-only,
--      so even a table it somehow gained write rights on is still untouchable.
--   3. NOCREATEDB / NOCREATEROLE / NOSUPERUSER / no CREATE on the schema, so it cannot
--      make itself a sandbox to write into.
-- A statement_timeout is attached to the role as well: a runaway query is killed by the
-- server after 5 seconds regardless of what the client asked for or forgot to ask for.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nl2sql_ro') THEN
        CREATE ROLE nl2sql_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END
$$;

GRANT CONNECT ON DATABASE nl2sql TO nl2sql_ro;
GRANT USAGE  ON SCHEMA public   TO nl2sql_ro;

-- Explicitly take away the ability to create objects in the schema.  In PostgreSQL 15+
-- PUBLIC no longer has CREATE on `public` by default, but saying so is free and this
-- migration must be correct on 13 and 14 too.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM nl2sql_ro;

GRANT SELECT ON ALL TABLES    IN SCHEMA public TO nl2sql_ro;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO nl2sql_ro;

-- Tables created by future migrations are covered automatically; otherwise the day
-- someone adds a table, the read-only role silently loses access to it and the fix people
-- reach for under time pressure is a blanket GRANT ALL.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES    TO nl2sql_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO nl2sql_ro;

ALTER ROLE nl2sql_ro SET default_transaction_read_only = on;
ALTER ROLE nl2sql_ro SET statement_timeout = '5s';
-- Cap how long it may sit inside an open transaction holding resources.
ALTER ROLE nl2sql_ro SET idle_in_transaction_session_timeout = '10s';
-- Keep the planner honest for this role: we want plan costs that reflect real work.
ALTER ROLE nl2sql_ro SET jit = off;
