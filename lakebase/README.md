# Lakebase schema — `neurorx-oltp` (Task 3.1)

`schema.sql` is the DDL for the four tables in `DATA_CONTRACTS.md` §6 — `patients`,
`schedules`, `dose_events`, `guardrail_blocks` — plus their indexes and the
`schedules.updated_at` trigger. Idempotent: safe to re-run against the same
database any number of times (verified — see "What was verified" below).

**Lakebase is the sole source of truth for patient state**
(`DATA_CONTRACTS.md` §7.3 / F8). Nothing here is ever backfilled from bronze
or gold; sync runs one way, Lakebase → Delta, covered by a separate
Phase 3 task, not this one.

⚠️ **`guardrail_blocks`'s home is still contested** (`DATA_CONTRACTS.md` F2 —
three sources say Delta, the original task spec says Lakebase). This file
follows `DATA_CONTRACTS.md` itself, which specifies it here — but that is not
a resolution of F2. See the comment above the table in `schema.sql`.

---

## What was verified before writing this (not assumed)

Per this project's standing rule (`CLAUDE.md` §6): verify live, don't guess.
The previous `.env.example` comments for the four `LAKEBASE_*` vars said
"typically" / "usually" — this file replaces that guesswork with what current
Databricks docs actually state, confirmed this session:

- **Postgres version**: Lakebase (Autoscaling) supports Postgres **16, 17,
  and 18**, with **17 as the default**. `gen_random_uuid()` has been a
  **core** Postgres function (no `pgcrypto` extension needed) since **PG13**
  — so `schema.sql` deliberately does not run `CREATE EXTENSION pgcrypto`.
- **Default database name**: every Lakebase project has a database named
  **`databricks_postgres`** — this is what `LAKEBASE_DB` should be, not a
  project-chosen name (confirmed against current connection-string docs; also
  independently confirmed in Task 2.3's own research, see `CLAUDE.md` §4).
- **Host format**: a per-project Databricks endpoint, shaped like
  `ep-abc-123.databricks.com` — not a generic hostname you choose.
- **Port**: the standard Postgres port, **5432**.
- **TLS**: Lakebase requires `sslmode=require` on every connection — not
  optional, not a tuning knob.
- **Two distinct auth mechanisms**, with a real tradeoff between them (see
  below) — confirmed against current Databricks docs, not inferred from
  generic Postgres knowledge.
- **`schema.sql` was actually executed**, not just parsed: against a real
  local Postgres 18 instance (Homebrew, the same technique Task 2.3 used for
  `manage_schedule`'s local test harness — hit and worked around the same two
  known snags, a Unix-socket path-length limit and a "postmaster became
  multithreaded" locale issue fixed with `LC_ALL=C`). Verified, not just
  read: the file applies cleanly twice in a row with zero errors (true
  idempotency, not merely `IF NOT EXISTS` syntax that happens to parse); the
  `schedules_frequency_match` CHECK genuinely rejects a `times_per_day=2`
  row carrying 3 `dose_times`; the `schedules_status_valid` and
  `dose_events_actioned_consistent` CHECKs genuinely reject `'paused'` and a
  `missed` dose carrying an `actioned_ts`; the `updated_at` trigger genuinely
  bumps on `UPDATE` and leaves `created_at` untouched; and — the one that
  actually needed a live database to prove, not just read from the DDL —
  deleting a patient cascades to `schedules` and `dose_events` (both emptied)
  while `guardrail_blocks` **survives** with `patient_id` set to `NULL`,
  confirming the append-only evidence log is never destroyed by a patient
  purge.
- **`sqlglot` caught one real narrow parser gap, isolated before working
  around it** — same lesson as Tasks 2.1 and 2.3: `COMMENT ON TABLE ... IS`
  with a **multi-line, adjacent-string-literal-concatenated** value (valid
  Postgres syntax) failed to parse under `sqlglot`'s Postgres dialect, while
  the identical concatenation pattern parsed fine inside a plain `SELECT`
  isolated with a 3-line repro. Reworded to single string literals per
  `COMMENT` (free fix, same call made in Task 2.1) rather than investigate
  further.

## Two ways to authenticate — and which one this project uses

| | OAuth token | Native Postgres password |
|---|---|---|
| How | `WorkspaceClient().database.generate_database_credential(request_id=<uuid>, instance_names=["neurorx-oltp"])`, or `databricks database generate-database-credential --request-id $(uuidgen) --json '{"instance_names": ["neurorx-oltp"]}'` | Toggle **"Enable Postgres Native Role Login"** on the instance, then `CREATE ROLE <name> LOGIN PASSWORD '<password>';` |
| Lifetime | **1 hour** — expiration enforced at login only, so a session started just before expiry keeps working, but any *new* connection after the hour fails | Indefinite, until manually rotated |
| Used as | The Postgres password field, over a mandatory SSL connection | The Postgres password field, same as any Postgres role |

**This project uses native password auth for local development** (the
`LAKEBASE_*` vars in `.env` / `app/config.py`) — the same choice
`CLAUDE.md`'s Task 2.3 notes already made for the **local test harness**
(never the *deployed* `manage_schedule`, which is blocked from raw `psycopg`
entirely by the port-5432 sandbox restriction and uses the Lakebase Data API
instead). An hourly-expiring OAuth token is the wrong shape for a `.env` file
a developer edits once and forgets about; a stable role password is.

## Step-by-step: apply `schema.sql` to a real Lakebase instance

1. **Create the instance** (if not already done in Phase 0): Databricks
   workspace → **Compute → OLTP Databases** (or the Lakebase app) → create a
   project named `neurorx-oltp`. Free Edition allows exactly one Lakebase
   project (`CLAUDE.md` §4) — don't create a second one.
2. **Open connection details**: in the Lakebase project, click **Connect**,
   then select the branch, compute, and database (`databricks_postgres`,
   the default). This page shows the host and offers a **copy psql connection
   snippet** button — use that snippet directly rather than hand-assembling
   one; it already has the right host/port/database/sslmode filled in.
3. **Enable native password auth** (one-time, for local dev use — see the
   table above): in the same project, find **"Enable Postgres Native Role
   Login"** and turn it on, then connect once with an OAuth token (Step 2's
   snippet, or `databricks database generate-database-credential`) and run:
   ```sql
   CREATE ROLE neurorx_dev LOGIN PASSWORD '<pick a strong password>';
   GRANT ALL PRIVILEGES ON DATABASE databricks_postgres TO neurorx_dev;
   ```
4. **Apply the schema**:
   ```bash
   psql "postgresql://neurorx_dev:<password>@<host-from-step-2>/databricks_postgres?sslmode=require" \
     -f lakebase/schema.sql
   ```
   Safe to re-run — every statement is idempotent (verified above).
5. **Populate `.env`** for `app/config.py` (see `.env.example`):
   ```
   LAKEBASE_HOST=<host-from-step-2, e.g. ep-abc-123.databricks.com>
   LAKEBASE_DB=databricks_postgres
   LAKEBASE_USER=neurorx_dev
   LAKEBASE_PASSWORD=<the password from step 3>
   ```

## Verify it applied correctly

```sql
\d patients
\d schedules
\d dose_events
\d guardrail_blocks
\di   -- lists idx_schedules_patient_active, idx_dose_events_patient_planned,
      --   idx_dose_events_schedule, idx_guardrail_blocks_ts
```

## Table summary

| Table | PK | FKs | Notes |
|---|---|---|---|
| `patients` | `patient_id` | — | Sole source of truth for patient identity. |
| `schedules` | `schedule_id` | `patient_id` → `patients`, `ON DELETE CASCADE` | `updated_at` trigger; `schedules_frequency_match` CHECK ties `times_per_day` to `cardinality(dose_times)`. |
| `dose_events` | `event_id` | `schedule_id` → `schedules` CASCADE; `patient_id` → `patients` CASCADE (denormalized, kept consistent with `schedule_id`'s cascade — see `schema.sql` comment) | The adherence ledger. |
| `guardrail_blocks` | `block_id` | `patient_id` → `patients`, `ON DELETE SET NULL` | Append-only evidence log — `SET NULL`, not `CASCADE`, so a patient purge never destroys the record that a block fired. Home contested, see F2. |
