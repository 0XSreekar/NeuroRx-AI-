# Running NeuroRx AI locally (off-workspace demo path)

The full stack needs a Databricks workspace. But the **Today** tab — the
extract→confirm→schedule→mark-dose loop — runs entirely against a local Postgres
standing in for Lakebase, with **no workspace at all**. This is the fastest way
to see real data flowing through the app before provisioning anything.

What works locally vs. what needs the workspace:

| Surface | Local (this doc) | Needs workspace |
|---|---|---|
| **Today** tab (doses, mark taken/skipped, countdown, refills) | ✅ full | — |
| **Dashboard** tab (adherence analytics) | degrades to a clear notice | ✅ reads `gold.adherence_facts` (Delta) via SQL warehouse |
| **Chat** tab (agent Q&A, interaction checks, citations) | degrades to a clear notice | ✅ the deployed `neurorx-agent` serving endpoint |

The split is by design: Today is live OLTP (Lakebase), Dashboard/Chat are
analytics/agent surfaces that only exist on the workspace (`app/db.py`'s
OLTP-vs-analytics F9 split).

## Prerequisites

- Python 3.14 venv with `pip install -r requirements.txt` (see
  `requirements-dev.txt` for ingestion/verification extras).
- Homebrew Postgres (`postgresql@18`).

## 1. Start a local Postgres

A short socket path avoids the Unix-socket length limit; `LC_ALL=C` avoids the
"postmaster became multithreaded" locale error. **Create the database as UTF8**
— the default under `LC_ALL=C initdb` is `SQL_ASCII`, and psycopg3 then returns
`bytes` for text columns, which breaks readback. Real Lakebase is UTF8.

```bash
export LC_ALL=C
initdb -D /tmp/nrx_pgdata -U postgres --auth=trust
pg_ctl -D /tmp/nrx_pgdata -o "-k /tmp/nrx_pg -p 5439 -c listen_addresses=127.0.0.1" -l /tmp/nrx_pg.log start
psql -h /tmp/nrx_pg -p 5439 -U postgres -d postgres \
  -c "CREATE DATABASE databricks_postgres ENCODING 'UTF8' TEMPLATE template0;"
```

## 2. Apply the schema

```bash
psql -h /tmp/nrx_pg -p 5439 -U postgres -d databricks_postgres \
  -v ON_ERROR_STOP=1 -f lakebase/schema.sql
```

## 3. Generate and load the synthetic cohort

The generator writes local Parquet when no SparkSession is present; the loader
reads it when `NEURORX_COHORT_PARQUET` is set.

```bash
export LOCAL_PG="host=/tmp/nrx_pg port=5439 user=postgres dbname=databricks_postgres"

NEURORX_COHORT_OUTPUT_DIR=/tmp/nrx_cohort python data/ingestion/04_synthetic_cohort.py

PYTHONPATH="$PWD" NEURORX_LOCAL_PG="$LOCAL_PG" \
  NEURORX_COHORT_PARQUET=/tmp/nrx_cohort python lakebase/07_load_cohort.py
```

The loader asserts Margaret Demo's UUID/drugs and reconciles row counts
(50 patients / 190 schedules / 66,060 dose events). Re-running is idempotent.

## 4. Run the app

`NEURORX_LOCAL_PG` points both `app/db.py` and the loader at the local Postgres
and short-circuits every workspace-only path (Delta warehouse, agent endpoint)
so they fail fast with a clear message instead of retrying a placeholder host.
`.env` still needs its nine vars present (placeholders are fine — `app/config.py`
fails at import otherwise).

```bash
NEURORX_LOCAL_PG="$LOCAL_PG" streamlit run app/app.py
```

Open the Today tab. You should see Margaret's 5 doses for today, grouped by
day-part, with a next-dose countdown and working "Taken ✓ / Skip" buttons that
write back to Postgres.
