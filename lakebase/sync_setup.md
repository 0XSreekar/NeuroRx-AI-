# Lakebase → Delta sync runbook (Task 3.2)

Goal: `schedules` and `dose_events` (Lakebase Postgres, OLTP) become queryable
as `neurorx.gold.schedules_synced` / `neurorx.gold.dose_events_synced` (Delta,
Unity Catalog) — so that analytics, the dashboard, Genie, and
`get_adherence_stats` never touch OLTP directly (`DATA_CONTRACTS.md` F9's own
recommendation, already accepted per `CLAUDE.md`'s Task 2.4 notes).

---

## ⚠️ Read this before doing anything — the feature name in the task brief is the wrong direction

The task brief says "configure **synced tables**." Databricks' current docs do
have a feature literally named **"synced tables"** — but verified live this
session, it goes the **opposite** direction from what this task needs:

> "Synced tables let you serve lakehouse data through Lakebase Postgres...
> Unity Catalog tables sync into Postgres so applications can query lakehouse
> data directly with low latency." — this is **Delta → Postgres**, also
> called **Reverse ETL**, for serving analytics data *to* an operational app.

That is backwards for this task, which needs **Postgres (OLTP) → Delta**. The
correct current mechanism, confirmed against current docs, is a **different,
separately-named feature: Lakebase Change Data Feed (CDF)**:

> "Lakehouse Sync enables continuous, low-latency replication of your
> Lakebase Postgres tables into Unity Catalog managed Delta tables by
> capturing row-level changes... powered by the `wal2delta` Postgres
> extension, which runs inside Lakebase compute, using logical decoding to
> capture write-ahead log (WAL) changes."

**Do not build against "synced tables" for this task** — that would sync the
wrong direction and require an existing Delta gold table to already contain
live OLTP data, which is circular. Everything below uses Lakebase CDF.

## What Lakebase CDF actually produces — read before writing a verification query

CDF does not produce a simple current-state mirror. It produces an
**append-only SCD Type 2 change log**, one Delta table per source table,
named `lb_<table_name>_history`, with these columns added on top of the
source table's own columns (verified against current docs):

| Column | Type | Meaning |
|---|---|---|
| `_pg_change_type` | `TEXT` | `insert`, `delete`, `update_preimage` (old values), or `update_postimage` (new values) |
| `_pg_lsn` | `BIGINT` | Postgres WAL log sequence number |
| `_pg_xid` | `INTEGER` | Postgres transaction id |
| `_timestamp` | `TIMESTAMP` | When the change was processed |
| `_sort_by` | `BIGINT` | Monotonic key — the correct column to order all changes by |

**An `UPDATE` produces two rows** (`update_preimage` + `update_postimage`), a
`DELETE` one row (the pre-delete values, `_pg_change_type='delete'`), an
`INSERT` one row. A raw row count on `lb_schedules_history` will be **larger
than**, and grow independently of, the live `schedules` row count — that is
correct behavior, not a sync bug. **Requirement #4's row-count verification
must run against a reconstructed current-state view, never against the raw
history table directly** — see `sync.sql`'s two materialized views and the
verification section below.

## Sync mode — there isn't a choice, and that's different from the (wrong-direction) feature

The task asks to "choose and justify the sync mode (continuous vs
triggered/snapshot)." That three-way choice is real, but it belongs to the
**synced tables** (Reverse ETL) feature this task isn't using — its docs
state Snapshot, Triggered, and Continuous as selectable modes. **Lakebase
CDF, the feature this task actually needs, has exactly one mode**: continuous
WAL-based streaming, with changes "batched and flushed every ~15 seconds."
There is nothing to configure here beyond turning it on.

**Expected staleness: ~15 seconds**, plus whatever lag the downstream
materialized view (below) adds on its own refresh cycle. For a hackathon demo
— mark a dose in the Today view, then ask the dashboard or Genie about it —
this is indistinguishable from live to a human, and dramatically better than
the alternative this project already ruled out for `get_adherence_stats`
(F9): reading Lakebase directly from a UC function on every request, which
costs a live Postgres round-trip per query instead of a warm Delta read.

---

## Step-by-step setup

### 1. Enable replica identity on the Lakebase side (Postgres)

Run in the **Lakebase SQL editor** (not Databricks SQL) against
`neurorx-oltp`, database `databricks_postgres` — see `sync.sql` Part A:

```sql
ALTER TABLE schedules        REPLICA IDENTITY FULL;
ALTER TABLE dose_events      REPLICA IDENTITY FULL;
ALTER TABLE patients         REPLICA IDENTITY FULL;
ALTER TABLE guardrail_blocks REPLICA IDENTITY FULL;
```

Required before a table can participate in CDF at all — it tells Postgres to
record full before/after row images in the WAL. **CDF is enabled per Postgres
*schema*, not per table** (confirmed: "when you start CDF on a Lakebase
schema, every current and future table in that schema is included") — all
four of this project's tables live in the default `public` schema
(`lakebase/schema.sql` never set one), so enabling CDF on `public` will pick
up all four once each has `REPLICA IDENTITY FULL` set. This task only needs
`schedules` and `dose_events` synced onward into gold views, but the other
two get `REPLICA IDENTITY FULL` here too — skipping it on `guardrail_blocks`
or `patients` would silently exclude them from CDF the moment it starts,
which is easy to miss later if either is needed.

### 2. Start Lakebase CDF (UI only — no SQL equivalent exists)

1. Open **Lakebase Postgres** from the app switcher (top right).
2. Select the `neurorx-oltp` project and its branch.
3. Open **Branch overview** (click the branch name in the top breadcrumb),
   then click the **Lakebase CDF** tab.
4. Click **Start**. In the configuration dialog:
   - **Database:** `databricks_postgres` (default — leave as-is)
   - **Schema:** `public` (the source Postgres schema)
   - **To Catalog:** `neurorx` (destination Unity Catalog catalog)
   - **Schema:** `bronze` (destination UC schema — see naming note below)
5. Click **Start** to begin the feed.

Requires: `USE CATALOG` / `USE SCHEMA` / `CREATE TABLE` on `neurorx.bronze`
for the identity configuring this, and `CAN MANAGE` on the `neurorx-oltp`
Lakebase project for the Postgres side.

**Destination naming choice**: docs confirm the raw output lands as
`lb_<table_name>_history` in whatever catalog/schema you pick — this runbook
puts it in `neurorx.bronze` (not `gold`), consistent with this project's own
medallion convention (`CLAUDE.md`: bronze is raw/as-ingested, no business
logic). The append-only change log genuinely is that: raw, unreduced,
untransformed. The two materialized views below, which *do* apply logic
(latest-row-per-key, delete filtering), are what land in `gold`.

### 3. Reconstruct current state (Databricks SQL — `sync.sql` Part B)

Run in a **Serverless SQL Warehouse** — confirmed as a hard requirement for
querying anything Lakebase-backed ("You can only query a registered Lakebase
catalog using a Serverless SQL Warehouse. Pro and Classic warehouses return
permission errors" — applies to the destination history tables' underlying
storage layer too, not just federated Lakebase catalogs).

`sync.sql` creates `neurorx.gold.schedules_synced` and
`neurorx.gold.dose_events_synced` as materialized views: for each primary
key, take the row with the highest `_sort_by` among non-`update_preimage`
rows, and exclude it entirely if that latest row is a `delete`. **Verified by
actually running this logic against DuckDB**, not just reading the SQL: a
3-row fixture (one schedule inserted-then-updated, one inserted-then-deleted,
one inserted-only) confirms the view shows the updated dose_times for the
first, omits the deleted second entirely, and passes the untouched third
through unchanged.

A materialized view, not a Lakeflow `@dp.materialized_view` inside
`pipelines/medallion_pipeline.py`: Lakeflow pipelines batch-transform
existing Delta tables on their own run schedule, which is the right shape for
the bronze→silver→gold transforms already in that file, but `lb_*_history`
already **is** a continuously-updating Delta table produced by a separate,
independently-managed feed — a plain Databricks SQL materialized view with
its own refresh schedule is the simpler, correct tool here, and matches the
pattern shown in Databricks' own CDF documentation example.

---

## ⚠️ Naming conflict with `DATA_CONTRACTS.md` §9 — flagged, not resolved

`DATA_CONTRACTS.md` §9's sync map names the two mirrors `neurorx.gold.schedules`
and `neurorx.gold.dose_events` (no suffix). This task's own brief asks for
`gold.schedules_synced` / `gold.dose_events_synced` — this runbook and
`sync.sql` follow the task brief's `_synced` names, since they are also more
honest about what these views actually are (a reconstruction from a CDC
history log, not a naive drop-in mirror the bare name implies). **This is a
real naming disagreement between two project documents, not a typo** —
`DATA_CONTRACTS.md` §9 and `pipelines/medallion_pipeline.py`'s existing
Phase-3 TODO comment (which said `gold.dose_events`, no suffix) should both
be amended to the `_synced` names if this is accepted, per this project's own
rule (`CLAUDE.md` §6: "flag document conflicts; never silently resolve
them").

---

## Update instruction for `pipelines/medallion_pipeline.py`

The existing Phase 1 TODO (lines 518–525) is:

```python
# TODO(Phase 3): flip this constant to the Lakebase-synced gold table once
# the Lakebase -> Delta sync is live (ARCHITECTURE.md §2, "synced table").
SOURCE_TABLE = f"{CATALOG}.bronze.synthetic_dose_events_raw"  # Phase 1
# SOURCE_TABLE = f"{CATALOG}.gold.dose_events"  # Phase 3
```

Flip it to:

```python
SOURCE_TABLE = f"{CATALOG}.gold.dose_events_synced"  # Phase 3 — live, Task 3.2/3.8
# SOURCE_TABLE = f"{CATALOG}.bronze.synthetic_dose_events_raw"  # Phase 1, historical
```

This is genuinely the one-line change the existing comment promised: the
aggregation logic in `adherence_facts()` reads `SOURCE_TABLE` expecting
exactly `(event_id, schedule_id, patient_id, rxcui, planned_ts, actioned_ts,
status)` — but **`dose_events_synced` has no `rxcui` column**
(`DATA_CONTRACTS.md` §6.3's `dose_events` never had one either; the bronze
synthetic table denormalized it in for audit convenience only, per §3.5).
`adherence_facts()` will need a join to `schedules` (on `schedule_id`) to
recover `rxcui` once this flips — **not** a one-line change after all, and
worth fixing in the same commit that flips `SOURCE_TABLE`, not discovered
later when the pipeline fails on a missing column.

**Do not flip this constant yet** — it depends on Task 3.8 (loading the
synthetic cohort *into* Lakebase, superseding the direct-to-bronze path
Task 1.4 currently takes) actually landing data in `schedules`/`dose_events`
first. Flow once both land: **generator → Lakebase → CDF → `bronze.lb_*_history`
→ `gold.*_synced` (this task) → `gold.adherence_facts`** — the bronze
synthetic tables (`04_synthetic_cohort.py`) become terminal audit-only
records once Task 3.8 ships, per `DATA_CONTRACTS.md` F8's existing "Lakebase
is the sole source of truth" invariant.

---

## Verification: row counts match between Lakebase and the synced view

**Never compare against `lb_schedules_history`'s or `lb_dose_events_history`'s
raw row count** — those grow with every insert/update/delete and are
expected to exceed the live table's row count, by design (see above).
Compare the live Postgres table against the reconstructed materialized view:

```sql
-- Databricks SQL (Serverless warehouse):
SELECT count(*) AS synced_schedule_count   FROM neurorx.gold.schedules_synced;
SELECT count(*) AS synced_dose_event_count FROM neurorx.gold.dose_events_synced;
```

```sql
-- Lakebase SQL editor (Postgres):
SELECT count(*) AS live_schedule_count   FROM schedules;
SELECT count(*) AS live_dose_event_count FROM dose_events;
```

The two schedule counts and the two dose-event counts should match, modulo
one materialized-view refresh cycle's worth of lag. A persistent mismatch
after two refreshes means one of: CDF stalled, a table is missing `REPLICA
IDENTITY FULL` (Step 1), or the "latest row per key" reconstruction has a
bug — not ordinary staleness. To force an immediate refresh before checking
(e.g. right before a demo):

```sql
REFRESH MATERIALIZED VIEW neurorx.gold.schedules_synced;
REFRESH MATERIALIZED VIEW neurorx.gold.dose_events_synced;
```

---

## What was verified live this session, and how

Per `CLAUDE.md` §6 ("verify external API facts live... this is not
pedantry"): every claim above about CDF's mechanics, schema, setup steps, and
permissions was pulled from current Databricks docs fetched this session, not
recalled from general Postgres/CDC knowledge or an older tutorial — this
feature name and shape is new enough that guessing from familiarity with
generic Postgres logical replication would have produced a plausible-looking
but wrong runbook (starting with the "synced tables" name collision above,
which is the kind of mistake that would have sent this whole task in the
wrong direction from the first sentence). The reconstruction query in
`sync.sql` was additionally verified by actually running it against DuckDB
with a fixture exercising all three change types (insert-only,
insert-then-update, insert-then-delete) — not just read for plausibility.
