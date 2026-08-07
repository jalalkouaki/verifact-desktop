-- ===========================================================================
--  Veri-Fact Desktop - migration 001
--  Adds: schema_meta
--  Safe to run repeatedly. Purely ADDITIVE - no existing table is altered,
--  no row is modified, nothing is dropped.
-- ===========================================================================
--
--  WHAT THIS DOES
--  --------------
--  Creates a tiny key/value table that records which schema version the
--  database is at. Veri-Fact reads it on launch to decide whether any data
--  migration is needed. Without it, an upgraded app cannot tell a brand-new
--  database from a v1 one.
--
--  WHEN TO RUN IT
--  --------------
--  You normally do NOT need to: the application runs the equivalent
--  statements itself at startup. This file exists so DBAs can review or apply
--  the change manually, and so the schema history is visible in the repo.
--
--  HOW TO RUN IT MANUALLY
--  ----------------------
--      sqlite3 verifact.db < migrations/001_add_schema_meta.sql
--
--  Back up verifact.db first if it holds data you care about:
--      copy verifact.db verifact.db.backup      (Windows)
--      cp   verifact.db verifact.db.backup      (macOS / Linux)
-- ===========================================================================

BEGIN TRANSACTION;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Record the version. ON CONFLICT makes this idempotent: running the script
-- twice updates the value rather than raising a UNIQUE constraint error.
INSERT INTO schema_meta (key, value)
VALUES ('schema_version', '2')
ON CONFLICT(key) DO UPDATE SET value = excluded.value;

COMMIT;
