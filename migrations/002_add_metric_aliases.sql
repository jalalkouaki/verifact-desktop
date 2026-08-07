-- ===========================================================================
--  Veri-Fact Desktop - migration 002
--  Adds: metric_aliases
--  Safe to run repeatedly. Purely ADDITIVE - no existing table is altered,
--  no row is modified, nothing is dropped.
-- ===========================================================================
--
--  WHAT THIS DOES
--  --------------
--  Creates the table behind the smart-suggest feature. When Veri-Fact cannot
--  recognise a metric name it fuzzy-matches and asks:
--
--      'custmer count' is not a known metric.
--      Did you mean 'customer_count'?   [Yes] [No] [Ignore]
--
--  The answer is stored here so you are never asked twice - the mapping
--  survives across sessions.
--
--  COLUMNS
--  -------
--    source_text  the text exactly as it appeared in the document
--    metric_name  the database metric it maps to
--    verdict      'accepted' -> always rewrite source_text to metric_name
--                 'rejected' -> never suggest THIS pairing again, keep trying
--                               other candidates
--                 'ignored'  -> never prompt for this source_text at all
--    hit_count    how many times a learned alias saved you a prompt
--
--  The UNIQUE(source_text, metric_name) constraint lets the application use
--  an upsert, so changing your mind about a pairing simply overwrites it.
--
--  MANAGING THE CONTENTS
--  ---------------------
--  In the app: Edit > Learned Metric Corrections...
--  To forget everything and be asked afresh:
--      DELETE FROM metric_aliases;
--
--  HOW TO RUN IT MANUALLY
--  ----------------------
--      sqlite3 verifact.db < migrations/002_add_metric_aliases.sql
-- ===========================================================================

BEGIN TRANSACTION;

CREATE TABLE IF NOT EXISTS metric_aliases (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_text  TEXT    NOT NULL,
    metric_name  TEXT    NOT NULL,
    verdict      TEXT    NOT NULL DEFAULT 'accepted',
    created_at   TEXT    NOT NULL,
    hit_count    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source_text, metric_name)
);

-- Lookups are always by the text found in the document.
CREATE INDEX IF NOT EXISTS idx_metric_aliases_source
    ON metric_aliases (source_text);

COMMIT;
