-- NeuroRx AI — Lakebase (Postgres OLTP) schema (Task 3.1)
--
-- Instance: neurorx-oltp. Source of truth: DATA_CONTRACTS.md §6 (frozen —
-- column names/types here must match that file exactly; if they ever diverge,
-- fix DATA_CONTRACTS.md first, then this file, per CLAUDE.md §6).
--
-- Lakebase is the sole source of truth for patient state (DATA_CONTRACTS.md
-- §7.3 / F8): nothing here is ever backfilled from bronze or gold — the sync
-- direction is Lakebase -> Delta only (see lakebase/README.md).
--
-- ---------------------------------------------------------------------------
-- Verified live before writing this file (not assumed):
--
-- 1. Postgres version: Lakebase (Autoscaling) supports Postgres 16, 17, and
--    18, with 17 as the default (confirmed against current Databricks docs).
-- 2. gen_random_uuid() needs NO extension on any of those versions: the
--    function moved into Postgres core in PG13 (previously pgcrypto-only).
--    This file deliberately does NOT run `CREATE EXTENSION pgcrypto` — it
--    would be a no-op for this function and an unnecessary privilege ask on
--    a workspace that may restrict CREATE EXTENSION.
-- 3. Idempotency mechanics used below are all confirmed current Postgres
--    syntax: `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, and
--    `CREATE OR REPLACE FUNCTION` are natively idempotent. Postgres has no
--    `CREATE TRIGGER IF NOT EXISTS` — idempotent trigger (re-)creation uses
--    `DROP TRIGGER IF EXISTS` immediately before `CREATE TRIGGER`, which is
--    the standard, safe pattern (not a Lakebase-specific quirk).
-- ---------------------------------------------------------------------------

-- =============================================================================
-- patients (DATA_CONTRACTS.md §6.1)
-- =============================================================================

CREATE TABLE IF NOT EXISTS patients (
    patient_id      UUID        NOT NULL DEFAULT gen_random_uuid(),
    display_name    TEXT        NOT NULL,
    caregiver_name  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT patients_pkey PRIMARY KEY (patient_id),
    CONSTRAINT patients_display_name_present CHECK (length(trim(display_name)) > 0)
);

COMMENT ON TABLE patients IS
    'Synthetic patient records only — no PHI, ever (ARCHITECTURE.md §5). Sole source of truth for patient identity; mirrored read-only to neurorx.gold.patients (DATA_CONTRACTS.md §9).';

-- =============================================================================
-- schedules (DATA_CONTRACTS.md §6.2)
-- =============================================================================

CREATE TABLE IF NOT EXISTS schedules (
    schedule_id     UUID        NOT NULL DEFAULT gen_random_uuid(),
    patient_id      UUID        NOT NULL,
    rxcui           TEXT        NOT NULL,
    drug_name       TEXT        NOT NULL,
    dose_text       TEXT        NOT NULL,
    times_per_day   INTEGER     NOT NULL,
    dose_times      TIME[]      NOT NULL,
    timing_notes    TEXT,
    status          TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT schedules_pkey PRIMARY KEY (schedule_id),

    -- ON DELETE CASCADE per DATA_CONTRACTS.md §6.2 verbatim: a schedule has no
    -- meaning once its patient is gone, and this is the only FK direction
    -- that keeps dose_events' own cascade (below) from leaving orphans.
    CONSTRAINT schedules_patient_fkey FOREIGN KEY (patient_id)
        REFERENCES patients (patient_id) ON DELETE CASCADE,

    CONSTRAINT schedules_status_valid    CHECK (status IN ('active','stopped')),
    CONSTRAINT schedules_rxcui_numeric   CHECK (rxcui ~ '^[0-9]+$'),
    CONSTRAINT schedules_times_positive  CHECK (times_per_day > 0),
    -- times_per_day and dose_times are two representations of one fact and
    -- must never disagree — see DATA_CONTRACTS.md §6.2's own note: a mismatch
    -- here would generate the wrong number of dose_events and silently
    -- corrupt every adherence number downstream.
    CONSTRAINT schedules_frequency_match CHECK (cardinality(dose_times) = times_per_day),
    CONSTRAINT schedules_updated_after   CHECK (updated_at >= created_at)
);

COMMENT ON TABLE schedules IS
    'A patient prescription list, active and stopped. Written only by manage_schedule, and only after explicit user confirmation (ARCHITECTURE.md §5(d)) — never write this table directly.';

-- updated_at trigger: bump on every UPDATE, per DATA_CONTRACTS.md §6.2's
-- "bump on every write" and the schedules_updated_after CHECK above, which
-- would otherwise be trivially violated by any hand-written UPDATE that
-- forgets to touch updated_at itself.
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Postgres has no `CREATE TRIGGER IF NOT EXISTS` — drop-then-create is the
-- standard idempotent pattern, safe to re-run this whole file any number of
-- times against the same database.
DROP TRIGGER IF EXISTS schedules_set_updated_at ON schedules;
CREATE TRIGGER schedules_set_updated_at
    BEFORE UPDATE ON schedules
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- =============================================================================
-- dose_events (DATA_CONTRACTS.md §6.3)
-- =============================================================================

CREATE TABLE IF NOT EXISTS dose_events (
    event_id        UUID        NOT NULL DEFAULT gen_random_uuid(),
    schedule_id     UUID        NOT NULL,
    patient_id      UUID        NOT NULL,
    planned_ts      TIMESTAMPTZ NOT NULL,
    actioned_ts     TIMESTAMPTZ,
    status          TEXT        NOT NULL,

    CONSTRAINT dose_events_pkey PRIMARY KEY (event_id),

    -- ON DELETE CASCADE per DATA_CONTRACTS.md §6.3 verbatim: a dose event has
    -- no meaning once its schedule is gone.
    CONSTRAINT dose_events_schedule_fkey FOREIGN KEY (schedule_id)
        REFERENCES schedules (schedule_id) ON DELETE CASCADE,

    -- DATA_CONTRACTS.md §6.3 states this column is "denormalized ... kept for
    -- query performance" but does not state an ON DELETE action. Set to
    -- CASCADE here to agree with schedule_id's cascade above: patient_id is
    -- redundant with schedules.patient_id by construction (see the
    -- denormalization invariant below), so the two FKs must resolve deletes
    -- identically — a patient purge must not leave this table in a state
    -- where one FK path deletes a row and the other would have blocked it.
    CONSTRAINT dose_events_patient_fkey FOREIGN KEY (patient_id)
        REFERENCES patients (patient_id) ON DELETE CASCADE,

    CONSTRAINT dose_events_status_valid CHECK (status IN ('planned','taken','skipped','missed')),
    -- planned/missed carry no action timestamp; taken/skipped require one.
    -- Enforcing this as a biconditional (not two separate one-way checks)
    -- prevents the ambiguous state of a `missed` dose carrying a timestamp.
    CONSTRAINT dose_events_actioned_consistent
        CHECK ((actioned_ts IS NOT NULL) = (status IN ('taken','skipped'))),
    CONSTRAINT dose_events_actioned_after_planned
        CHECK (actioned_ts IS NULL OR actioned_ts >= planned_ts),

    -- Added for Task 3.3 (app/db.py's mark_dose): DATA_CONTRACTS.md §6.3
    -- doesn't specify this, but mark_dose needs a genuine INSERT ... ON
    -- CONFLICT upsert — "create the planned row if the reminders job hasn't
    -- yet" — and Postgres upsert requires a real unique constraint to
    -- conflict on. One (schedule_id, planned_ts) pair is exactly one dose
    -- slot; without this constraint, a double-submitted mark_dose call (a
    -- double-click, a retried request) could insert two dose_events rows
    -- for the same slot, silently double-counting that dose in every
    -- adherence aggregate downstream.
    CONSTRAINT dose_events_slot_unique UNIQUE (schedule_id, planned_ts)
);

COMMENT ON TABLE dose_events IS
    'The adherence ledger. Written by the Today view when a dose is marked, and by the reminders job when it materializes upcoming doses. Substrate for neurorx.gold.adherence_facts via sync plus Lakeflow (DATA_CONTRACTS.md §5.3, §9) — never a synced table itself.';

-- Denormalization invariant from DATA_CONTRACTS.md §6.3: dose_events.patient_id
-- must always equal the patient_id of its own schedule_id. Not expressible as
-- a Postgres CHECK (it spans two tables) — enforce in manage_schedule's write
-- path, and run this query after every Lakeflow sync; it must return zero rows.
--
-- SELECT e.event_id FROM dose_events e
-- JOIN schedules s ON e.schedule_id = s.schedule_id
-- WHERE e.patient_id <> s.patient_id;

-- =============================================================================
-- guardrail_blocks (DATA_CONTRACTS.md §6.4)
--
-- Home is contested per DATA_CONTRACTS.md F2 (Lakebase per the original task
-- spec vs. a Delta table per ARCHITECTURE.md §2/§5(e) and the build plan).
-- Written here per the Task 0.5 spec DATA_CONTRACTS.md itself follows —
-- **this is still an open decision, not a resolution of F2.** If the project
-- settles on Delta instead, this table should be dropped from Lakebase and
-- written directly to neurorx.evals (DATA_CONTRACTS.md F2's own recommended
-- schema, if Delta wins) rather than kept in both places.
-- =============================================================================

CREATE TABLE IF NOT EXISTS guardrail_blocks (
    block_id                UUID        NOT NULL DEFAULT gen_random_uuid(),
    ts                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    patient_id              UUID,
    model_output_excerpt    TEXT        NOT NULL,
    rule_triggered          TEXT        NOT NULL,
    judge_verdict           TEXT,

    CONSTRAINT guardrail_blocks_pkey PRIMARY KEY (block_id),

    -- patient_id is nullable per DATA_CONTRACTS.md §6.4 ("NULL for anonymous
    -- or pre-auth sessions"). ON DELETE SET NULL, not CASCADE: this table is
    -- an append-only evidence log ("only credible if nothing can quietly
    -- edit it after the fact" — DATA_CONTRACTS.md §6.4's own words). Deleting
    -- a patient must never delete the evidence that the guardrail once
    -- blocked a response for them; it only invalidates which patient the
    -- (still-nullable) reference points at.
    CONSTRAINT guardrail_blocks_patient_fkey FOREIGN KEY (patient_id)
        REFERENCES patients (patient_id) ON DELETE SET NULL,

    CONSTRAINT guardrail_blocks_excerpt_present CHECK (length(trim(model_output_excerpt)) > 0),
    CONSTRAINT guardrail_blocks_rule_present    CHECK (length(trim(rule_triggered)) > 0)
);

COMMENT ON TABLE guardrail_blocks IS
    'Append-only log of every response the output guardrail blocked. Never UPDATE or DELETE a row (DATA_CONTRACTS.md §6.4) — this table is the evidence the safety net fires, credible only if nothing can quietly edit it after the fact. Home contested, see F2 above: may move to a Delta table in neurorx.evals if that recommendation is accepted.';

-- =============================================================================
-- notifications (Task 3.7)
--
-- Not in DATA_CONTRACTS.md's frozen §6 table list — that file predates this
-- table. ARCHITECTURE.md §2/§7 names it only in prose ("a scheduled
-- Lakeflow Job computes upcoming doses and writes a notifications table the
-- app polls"), with no column list anywhere. This is the first concrete
-- schema for it; app/db.py's Task 3.5 version (list_unacknowledged_reminders/
-- acknowledge_reminder) was explicitly flagged there as provisional against
-- an assumed, different shape (acknowledged_at TIMESTAMPTZ, no schedule_id,
-- no due_ts) — updated in this same task to match the real columns below,
-- not left disagreeing with them.
-- =============================================================================

CREATE TABLE IF NOT EXISTS notifications (
    notification_id UUID        NOT NULL DEFAULT gen_random_uuid(),
    patient_id      UUID        NOT NULL,
    schedule_id     UUID        NOT NULL,
    due_ts          TIMESTAMPTZ NOT NULL,
    message         TEXT        NOT NULL,
    acknowledged    BOOLEAN     NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT notifications_pkey PRIMARY KEY (notification_id),

    -- ON DELETE CASCADE for both, matching dose_events' own choice
    -- (lakebase/schema.sql, Task 3.1): a notification about a dose slot
    -- has no meaning once the schedule or patient it belongs to is gone.
    -- Unlike guardrail_blocks (an append-only evidence log, deliberately
    -- ON DELETE SET NULL), a notification is disposable operational state,
    -- not evidence anything needs to survive its subject's deletion.
    CONSTRAINT notifications_patient_fkey FOREIGN KEY (patient_id)
        REFERENCES patients (patient_id) ON DELETE CASCADE,
    CONSTRAINT notifications_schedule_fkey FOREIGN KEY (schedule_id)
        REFERENCES schedules (schedule_id) ON DELETE CASCADE,

    CONSTRAINT notifications_message_present CHECK (length(trim(message)) > 0),

    -- The reminders job's own idempotency key (Task 3.7 requirement #2:
    -- "idempotent on schedule_id+due_ts"). Running the job twice inside the
    -- same 15-minute window (a retried job run, an overlapping manual
    -- trigger) must not duplicate a reminder for the same dose slot —
    -- this constraint is what an INSERT ... ON CONFLICT DO NOTHING has to
    -- conflict on; without it, every run would insert a fresh row.
    CONSTRAINT notifications_slot_unique UNIQUE (schedule_id, due_ts)
);

COMMENT ON TABLE notifications IS
    'Upcoming-dose reminders computed by the scheduled reminders job (Task 3.7, app/jobs/reminders_job.py) and polled by the Today view banner (Task 3.5). Not append-only — acknowledged flips true when a user dismisses one.';

-- =============================================================================
-- Indexes (Task 3.1 requirement #2)
-- =============================================================================

-- The Today view's dominant query: "this patient's active prescriptions."
-- Partial index — only active schedules are ever queried this way; stopped
-- schedules are historical and read rarely, via schedule_id directly.
CREATE INDEX IF NOT EXISTS idx_schedules_patient_active
    ON schedules (patient_id)
    WHERE status = 'active';

-- The Today view's and get_adherence_stats' dominant dose_events query:
-- "this patient's doses in a time window," ordered by planned_ts.
CREATE INDEX IF NOT EXISTS idx_dose_events_patient_planned
    ON dose_events (patient_id, planned_ts);

-- Postgres does not auto-index a foreign-key column. Without this, deleting
-- a schedule (which cascades to dose_events via schedule_id) requires a full
-- table scan of dose_events to find the rows to cascade-delete.
CREATE INDEX IF NOT EXISTS idx_dose_events_schedule
    ON dose_events (schedule_id);

-- The demo's "show the guardrail firing" query reads this table in time
-- order; also the natural index for the append-only evidence log.
CREATE INDEX IF NOT EXISTS idx_guardrail_blocks_ts
    ON guardrail_blocks (ts);

-- The Today view's reminder-banner poll (app/db.py's
-- list_unacknowledged_reminders): "this patient's unacknowledged
-- reminders." Partial index — acknowledged reminders are never polled
-- again, so indexing them would be pure waste.
CREATE INDEX IF NOT EXISTS idx_notifications_patient_unacknowledged
    ON notifications (patient_id)
    WHERE acknowledged = false;
