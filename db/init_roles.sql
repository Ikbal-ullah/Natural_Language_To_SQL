-- Defense-in-depth role for the FastAPI service. Mounted by docker-compose as
-- 02_roles.sql (runs once, as the bootstrap superuser, after 01_schema.sql).
--
-- The application connects ONLY as nl2sql_ro. Even if every other layer (prompt, AST
-- validator, EXPLAIN gate) were bypassed, this role cannot write, create or alter
-- anything, cannot run longer than 3 s, and cannot hold more than 16 MB of sort memory
-- per operation.
--
-- Password is a development default; override it in any real deployment.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nl2sql_ro') THEN
        CREATE ROLE nl2sql_ro LOGIN PASSWORD 'nl2sql_ro_dev'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO nl2sql_ro', current_database());
END
$$;

-- No ambient privileges: PUBLIC may not create objects in public, and the role gets
-- USAGE on the schema plus SELECT on tables -- nothing else.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT  USAGE  ON SCHEMA public TO nl2sql_ro;
GRANT  SELECT ON ALL TABLES IN SCHEMA public TO nl2sql_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO nl2sql_ro;

-- Server-side limits, applied at login. The pool also sets the transaction to READ ONLY.
ALTER ROLE nl2sql_ro SET default_transaction_read_only = on;
ALTER ROLE nl2sql_ro SET statement_timeout = '3000ms';
ALTER ROLE nl2sql_ro SET work_mem = '16MB';
ALTER ROLE nl2sql_ro SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE nl2sql_ro SET jit = off;
