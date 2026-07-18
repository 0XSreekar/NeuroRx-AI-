-- NeuroRx AI — Lakebase -> Delta sync (Task 3.2)
--
-- Two systems of SQL in this one file, clearly separated below:
--   PART A runs in the Lakebase SQL editor (Postgres) — enables CDC capture
--          on the source tables.
--   PART B runs in a Databricks SQL warehouse (Unity Catalog) — reconstructs
--          "current state" tables from the CDC history Delta tables that
--          Lakebase Change Data Feed produces automatically.
--
-- See lakebase/sync_setup.md for the full runbook, the feature-name
-- verification, and why this is two parts rather than one.

-- =============================================================================
-- PART A — run in the Lakebase SQL editor (neurorx-oltp, database
-- databricks_postgres), BEFORE starting Lakebase CDF in the UI.
-- =============================================================================

-- Lakebase Change Data Feed requires REPLICA IDENTITY FULL on every
-- participating table (confirmed against current docs) — it tells Postgres
-- to record the full before-and-after row in the WAL, which CDC needs to
-- capture a delete or an update's old values, not just the primary key.
--
-- CDF is enabled per SCHEMA, not per table (confirmed): once started on the
-- `public` schema, every current and future table in it is included — BUT
-- only tables with REPLICA IDENTITY FULL actually participate; the others
-- are silently skipped. All four of this project's tables live in `public`
-- (lakebase/schema.sql never set a schema), so all four need this statement
-- run, even though this task only asks about two — leaving `patients` or
-- `guardrail_blocks` without REPLICA IDENTITY FULL would silently exclude
-- them once CDF starts, which is easy to miss.
ALTER TABLE schedules        REPLICA IDENTITY FULL;
ALTER TABLE dose_events      REPLICA IDENTITY FULL;
ALTER TABLE patients         REPLICA IDENTITY FULL;
ALTER TABLE guardrail_blocks REPLICA IDENTITY FULL;

-- After running the above, start Lakebase CDF from the UI — see
-- sync_setup.md Step 2. There is no SQL equivalent for starting the feed
-- itself; it is a UI-only action (confirmed against current docs — no
-- `CREATE SYNCED TABLE`-style DDL exists for this feature).

-- =============================================================================
-- PART B — run in a Databricks SQL warehouse (Serverless — see
-- sync_setup.md's permission note), AFTER Lakebase CDF is running and has
-- produced its initial snapshot.
--
-- Lakebase CDF's own output (lb_schedules_history, lb_dose_events_history)
-- is an append-only SCD Type 2 CHANGE LOG, not a current-state mirror — one
-- row per insert, two rows per update (a `update_preimage` / old-values row
-- and an `update_postimage` / new-values row), one row per delete. Reading
-- it directly as "the current schedules table" would double- and triple-
-- count rows. These two views reduce the history log to current state:
-- latest event per primary key, excluding keys whose latest event is a
-- delete. See sync_setup.md for why this is a materialized view, not a
-- Lakeflow pipeline table, and the staleness this implies.
-- =============================================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS neurorx.gold.schedules_synced AS
WITH ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY schedule_id ORDER BY _sort_by DESC
        ) AS _rn
    FROM neurorx.bronze.lb_schedules_history
    -- update_preimage rows carry the OLD values before an update — never the
    -- current state for any key, so they must never win the "latest row"
    -- pick even if their _sort_by happens to be highest for some reason.
    WHERE _pg_change_type != 'update_preimage'
)
SELECT
    schedule_id,
    patient_id,
    rxcui,
    drug_name,
    dose_text,
    times_per_day,
    dose_times,
    timing_notes,
    status,
    created_at,
    updated_at
FROM ranked
WHERE _rn = 1
  -- if the latest event for this key is a delete, the record no longer
  -- exists in Lakebase — exclude it, don't show its last-known values.
  AND _pg_change_type != 'delete';

CREATE MATERIALIZED VIEW IF NOT EXISTS neurorx.gold.dose_events_synced AS
WITH ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY event_id ORDER BY _sort_by DESC
        ) AS _rn
    FROM neurorx.bronze.lb_dose_events_history
    WHERE _pg_change_type != 'update_preimage'
)
SELECT
    event_id,
    schedule_id,
    patient_id,
    planned_ts,
    actioned_ts,
    status
FROM ranked
WHERE _rn = 1
  AND _pg_change_type != 'delete';

-- Both materialized views auto-refresh on Databricks' own schedule once
-- created this way (see sync_setup.md for the exact refresh cadence chosen
-- and why). To force an immediate refresh (e.g. right before a demo):
--
-- REFRESH MATERIALIZED VIEW neurorx.gold.schedules_synced;
-- REFRESH MATERIALIZED VIEW neurorx.gold.dose_events_synced;

-- =============================================================================
-- Verification — row counts should match between Lakebase and the
-- reconstructed current-state view. See sync_setup.md for why this
-- deliberately does NOT compare against lb_*_history's raw row count
-- (that count is expected to be much larger and growing, by design).
-- =============================================================================

-- Run in Databricks SQL:
-- SELECT count(*) AS synced_schedule_count FROM neurorx.gold.schedules_synced;
-- SELECT count(*) AS synced_dose_event_count FROM neurorx.gold.dose_events_synced;

-- Run in the Lakebase SQL editor (Postgres), for comparison:
-- SELECT count(*) AS live_schedule_count FROM schedules;
-- SELECT count(*) AS live_dose_event_count FROM dose_events;

-- The two schedule counts and the two dose_event counts should be equal
-- (allowing for a refresh-cycle's worth of lag — see sync_setup.md's
-- staleness note). A persistent mismatch after two refresh cycles indicates
-- either CDF stalled, a table missing REPLICA IDENTITY FULL, or the
-- materialized view's "latest row per key" logic has a bug — not
-- normal staleness.
