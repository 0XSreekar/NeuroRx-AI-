"""NeuroRx AI — reminders job (Task 3.7).

Scheduled Lakeflow Job (`ARCHITECTURE.md` §2/§7: "a scheduled Lakeflow Job
computes upcoming doses and writes a notifications table the app polls").
Every run: find doses due in the next 60 minutes across all active
schedules, insert a `notifications` row for each (idempotent on
`schedule_id`+`due_ts` — see `lakebase/schema.sql`'s `notifications_slot_unique`
constraint, added in this same task), and pre-create the corresponding
`planned` `dose_events` row so the Today view's checklist (Task 3.5) has
something to show even before a human ever opens the app.

## Why this is a standalone script, not a reuse of app/db.py's connection pool

`app/db.py`'s Lakebase pool (`_get_pool()`) is decorated with
`@st.cache_resource` — a Streamlit-specific memoization decorator that
requires an active Streamlit script run context to work correctly. A
Lakeflow Job has no such context; importing `app.db` here and calling
`_get_pool()` would either fail outright or behave unpredictably outside the
runtime `st.cache_resource` assumes. This file opens its own plain
`psycopg` connection instead — the same pattern Task 2.3's local test
harness and this project's other standalone scripts already use, reusing
only `app.config.settings` for the Lakebase credentials (no Streamlit
dependency at all).

## Idempotency, both ways, verified against real Postgres

Both inserts in `_process_schedule()` are `INSERT ... ON CONFLICT DO
NOTHING`, keyed on the same real unique constraints Task 3.3
(`dose_events_slot_unique`) and Task 3.7 (`notifications_slot_unique`,
`lakebase/schema.sql`) added for exactly this purpose — a retried job run,
an overlapping manual trigger, or two schedule instances racing must never
duplicate a notification or a dose_events row for the same slot. Verified
by actually running this exact query pair twice in a row against real local
Postgres and confirming row counts stay at 1, not 2, for both tables.

## What this deliberately does NOT do

No SMS, no push notification, no external call of any kind — this job only
writes to Lakebase; the Today view (Task 3.5) polls it. **Twilio SMS/push
is the named production path and is explicitly out of scope for this
hackathon** — see `app/jobs/README.md`'s own note, restated here so anyone
reading just this file (not the README) sees it too: don't add a Twilio
call to this file without first reading why it isn't here.
"""

import os

import psycopg

from app.config import settings

# Task 3.7 requirement #2: "doses due in the next 60 minutes."
LOOKAHEAD_MINUTES = 60


def _connect() -> psycopg.Connection:
    """A single plain connection for one job run — no pooling needed here.
    This script runs to completion and exits once per scheduled trigger
    (every 15 minutes, per app/jobs/README.md); a Lakeflow Job invocation
    is not a long-lived process serving concurrent requests the way the
    Streamlit app is, so app/db.py's pool sizing rationale doesn't apply.
    """
    # NEURORX_LOCAL_PG: same local-Postgres override app/db.py and
    # lakebase/07_load_cohort.py honor — one env var points every component
    # at the same store on the off-workspace demo path (docs/local_dev.md).
    local = os.getenv("NEURORX_LOCAL_PG")
    if local:
        return psycopg.connect(local)

    return psycopg.connect(
        host=settings.lakebase_host,
        dbname=settings.lakebase_db,
        user=settings.lakebase_user,
        password=settings.lakebase_password,
        sslmode="require",
        port=5432,
    )


def find_doses_due_soon(conn: psycopg.Connection) -> list[dict]:
    """Every (schedule, dose_time) pair due in the next LOOKAHEAD_MINUTES,
    across all active schedules and all patients — one job run covers
    everyone, not per-patient invocations.

    Unnests `dose_times` against `CURRENT_DATE`, same construction
    `app/db.py`'s `todays_doses()` uses (Task 3.3/3.5) — the two queries
    are conceptually related (both turn a schedule's `dose_times` into
    concrete timestamps for "today") but serve different windows: this one
    looks at everyone's next hour; that one looks at one patient's whole
    day. Kept as separate queries rather than factored into one shared
    function, since the WHERE clauses differ in a way that would make a
    shared helper more confusing than two small, direct queries.
    """
    with conn.cursor() as cur:
        # Both today's AND tomorrow's slots are generated before windowing.
        # CURRENT_DATE alone has a midnight blind spot: a job run at 23:30
        # never sees a 00:15 dose, because that dose's timestamp belongs to
        # CURRENT_DATE + 1 — it would only get its notification after
        # midnight, i.e. 15 minutes' warning instead of the promised 60.
        # The BETWEEN window still bounds everything to the next
        # LOOKAHEAD_MINUTES, so adding tomorrow's slots to the candidate set
        # changes nothing except closing that boundary gap.
        cur.execute(
            """
            SELECT
                s.schedule_id,
                s.patient_id,
                s.drug_name,
                s.dose_text,
                (d + dt)::timestamptz AS due_ts
            FROM schedules s,
                 unnest(s.dose_times) AS dt,
                 unnest(ARRAY[CURRENT_DATE, CURRENT_DATE + 1]) AS d
            WHERE s.status = 'active'
              AND (d + dt)::timestamptz
                  BETWEEN now() AND now() + (%(lookahead)s * INTERVAL '1 minute')
            """,
            {"lookahead": LOOKAHEAD_MINUTES},
        )
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def build_message(drug_name: str, dose_text: str, due_ts) -> str:
    """Task 3.7 requirement #3's exact template — one function so the
    wording is defined once, not inlined at each call site.
    """
    return f"Time for your {drug_name} ({dose_text}) at {due_ts.strftime('%I:%M %p')}."


def process_dose(conn: psycopg.Connection, dose: dict) -> None:
    """Insert the notification and pre-create the planned dose_events row
    for one due-soon dose — both idempotent (see module docstring).
    """
    message = build_message(dose["drug_name"], dose["dose_text"], dose["due_ts"])

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO notifications (patient_id, schedule_id, due_ts, message)
            VALUES (%(patient_id)s, %(schedule_id)s, %(due_ts)s, %(message)s)
            ON CONFLICT (schedule_id, due_ts) DO NOTHING
            """,
            {
                "patient_id": dose["patient_id"],
                "schedule_id": dose["schedule_id"],
                "due_ts": dose["due_ts"],
                "message": message,
            },
        )
        # Pre-creates the row the Today view's todays_doses()/mark_dose()
        # (Task 3.3/3.5) expect to already exist once the reminders job has
        # run — those functions handle the case where it hasn't (LEFT JOIN
        # defaulting to 'planned', mark_dose's own upsert), but pre-creating
        # it here is exactly what this job exists to do per its own name.
        cur.execute(
            """
            INSERT INTO dose_events (schedule_id, patient_id, planned_ts, status)
            VALUES (%(schedule_id)s, %(patient_id)s, %(planned_ts)s, 'planned')
            ON CONFLICT (schedule_id, planned_ts) DO NOTHING
            """,
            {
                "schedule_id": dose["schedule_id"],
                "patient_id": dose["patient_id"],
                "planned_ts": dose["due_ts"],
            },
        )
    conn.commit()


def main() -> None:
    conn = _connect()
    try:
        due_soon = find_doses_due_soon(conn)
        print(f"reminders_job: {len(due_soon)} dose(s) due in the next {LOOKAHEAD_MINUTES} minutes")
        for dose in due_soon:
            process_dose(conn, dose)
        print(f"reminders_job: processed {len(due_soon)} dose(s) (idempotent — reruns are safe)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
