-- ===========================================================================
--  Veri-Fact Desktop - migration 003  (OPTIONAL / COSMETIC)
--  Renames the old demo region labels to neutral placeholders.
-- ===========================================================================
--
--  *** READ THIS BEFORE RUNNING ***
--
--  Unlike migrations 001 and 002, this one MODIFIES EXISTING ROWS.
--
--  DO NOT RUN IT if 'Europe' and 'North America' are your REAL region labels.
--  Your own labels work perfectly well - Veri-Fact learns its region
--  vocabulary from the data via SELECT DISTINCT region, so it adapts to
--  whatever you use. This rename is purely cosmetic.
--
--  Only run it if you are still carrying the v1 DEMO rows and would like them
--  to match the neutral placeholders used in the current documentation.
--
--  THE EASIER, SAFER WAY
--  ---------------------
--  Use the application instead:
--      Help > Migrate Legacy Region Labels...
--  It shows you exactly which rows would change and asks for confirmation
--  before touching anything. That is the recommended route.
--
--  WHY NEUTRAL PLACEHOLDERS
--  ------------------------
--  Shipping 'Europe' / 'North America' as demo data implied Veri-Fact cared
--  about those particular places. It does not - regions are read from your
--  database. Region_A / Region_B / Global make it obvious at a glance that
--  these are sample rows rather than somebody's real figures.
--
--  BACK UP FIRST
--  -------------
--      copy verifact.db verifact.db.backup      (Windows)
--      cp   verifact.db verifact.db.backup      (macOS / Linux)
--
--  HOW TO RUN IT MANUALLY
--  ----------------------
--      sqlite3 verifact.db < migrations/003_neutral_region_labels.sql
--
--  Idempotent: running it again is a harmless no-op once there is nothing
--  left to rename.
-- ===========================================================================

BEGIN TRANSACTION;

-- Preview what WOULD change (uncomment to inspect before committing):
-- SELECT id, metric_name, region, quarter, year, value
--   FROM financial_metrics
--  WHERE region IN ('Europe', 'North America');

UPDATE financial_metrics
   SET region = 'Region_A'
 WHERE region = 'Europe';

UPDATE financial_metrics
   SET region = 'Region_B'
 WHERE region = 'North America';

COMMIT;

-- Verify the result:
--   SELECT DISTINCT region FROM financial_metrics ORDER BY region;
