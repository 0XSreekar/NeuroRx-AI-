"""NeuroRx AI — app data-access layer, Lakebase side (Task 3.3).

One of exactly two modules through which the app touches data (see also
`app/agent_client.py`). No business logic belongs in the Streamlit UI layer —
every function here returns a plain dict (or list of dicts), never an ORM
object or a database cursor, so the UI layer never needs to know this is
Postgres underneath.

**Every query is parameterized.** No *value* is ever interpolated into SQL in
this file — patient-supplied values (patient_id from a URL param, a marked
dose's timestamp) always go through psycopg's own parameter binding. This is
not a style preference: `patient_id` ultimately comes from a session/URL
value a user controls, and string-formatting it into SQL would be a real SQL
injection surface for a project whose whole safety story is "no clinical fact
reaches a user without a deterministic, auditable path."

The one exception is structural, not a value: `_day_part_case()` builds a
constant `CASE` fragment from `DAY_PART_BANDS` and a column name written
literally in this file. Nothing a user controls reaches it, and it validates
its argument as a bare `alias.column` identifier regardless. It is
interpolated rather than bound because a bind parameter cannot carry a column
reference — and single-sourcing that fragment is worth more than the purity
of the rule, since the alternative is what this file used to do: keep two
hand-copied CASE blocks in sync by comment.

## Two connections, two purposes, deliberately not interchangeable

- **Lakebase (psycopg, this file's `_pool`)** — live OLTP reads/writes:
  patients, schedules, today's doses, marking a dose, guardrail-block
  logging. Anything the Today view or Maintain flow needs *right now*.
- **Delta via the Databricks SQL connector (`sql_connect()`)** —
  `adherence_summary()` only. **Analytics reads go to Delta, never
  Lakebase — this is deliberate, not an oversight, and worth stating to a
  judge**: `get_adherence_stats` (the UC function tool, Task 2.4) already
  reads `gold.adherence_facts` rather than live `dose_events` for the exact
  same reason (`DATA_CONTRACTS.md` F9): a UC function doing a live Postgres
  round-trip on every chat turn is real latency for no benefit once
  `gold.adherence_facts` is fresh to ~15s (Task 3.2's Lakebase CDF). The app
  layer follows the same rule so the two surfaces (chat answers, dashboard)
  never disagree about which store is authoritative for which kind of read.

## What was verified before writing this (not assumed)

- **psycopg 3 (not psycopg2)** — already established project fact (`CLAUDE.md`
  §4, Task 2.3's local test harness). Pooling via `psycopg_pool.ConnectionPool`
  (current PyPI: `psycopg-pool==3.3.1`), confirmed against the actual package
  source this session: constructor takes `conninfo`, `min_size`, `max_size`;
  `.connection()` is a context-manager method yielding a live connection.
- **The Databricks SQL connector's parameter style is NOT psycopg's.**
  Confirmed against the actual `databricks-sql-connector==4.3.0` source:
  default mode (`use_inline_params=False`) uses PEP-249 `named` paramstyle —
  `:param_name` placeholders with a dict — not psycopg's `%(name)s`. Mixing
  the two up in this file would silently produce the wrong bind syntax for
  whichever connector didn't get it; each helper below uses its own
  connector's real style, not a copy-pasted guess.
- **The SQL warehouse's HTTP path is `warehouse.odbc_params.path`**, not a
  `.http_path` attribute directly on the endpoint object — confirmed against
  the real `databricks-sdk` source (`OdbcParams` dataclass), the same lesson
  Task 2.8 already learned about not trusting a plausible-sounding attribute
  name without checking.

## A real, undocumented gap this file does NOT paper over

`refill_estimates()` needs a fill quantity to subtract taken doses from.
**`DATA_CONTRACTS.md` §6.2's `schedules` columns have no such field** — no
`fill_quantity`, no `days_supply`, nothing refill-shaped anywhere in the
frozen contract. Per this task's own instruction, this file flags that gap
loudly (a distinct, documented "unavailable" result) rather than inventing a
column that doesn't exist or fabricating a number from `dose_text`'s free
text, which was explicitly never meant to be parsed for clinical logic
(`DATA_CONTRACTS.md` §6.2: "Never parsed for clinical logic — dosing is out
of scope").
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Optional

import streamlit as st
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import settings

# ---------------------------------------------------------------------------
# day_part bucketing — one definition, every call site
# ---------------------------------------------------------------------------
#
# `DATA_CONTRACTS.md` §1 fixes the boundaries in local time: morning
# 05:00–11:59, afternoon 12:00–16:59, evening 17:00–20:59, night 21:00–04:59.
# `night` wraps midnight, so it is the ELSE branch rather than a contiguous
# range — every implementation of this rule has to express it that way.
#
# These numbers appear exactly once on the app side. Both queries that bucket
# a timestamp — `todays_doses()` for the Today checklist and
# `_adherence_summary_local()` for the Dashboard — generate their CASE from
# `_day_part_case()` below rather than spelling it out. Until now they were
# two hand-copied CASE blocks differing only in table alias, kept in agreement
# by a docstring promising they were "never two silently-drifting
# reimplementations" — which is precisely what they were. `current_streak_days`
# had the same shape of duplication and did drift (see
# `tests/test_adherence_streak.py`); this removes the opportunity here instead
# of testing for it after the fact.
#
# `pipelines/medallion_pipeline.py`'s `_day_part_expr()` is a third
# implementation, in Spark, for `gold.adherence_facts`. It deliberately does
# NOT import this module — a Lakeflow pipeline must not pull in streamlit and
# psycopg — so it stays a separate copy, and
# `tests/test_day_part_boundaries.py` pins it to these same numbers by reading
# its source.
DAY_PART_BANDS: tuple[tuple[int, int, str], ...] = (
    (5, 12, "morning"),
    (12, 17, "afternoon"),
    (17, 21, "evening"),
)

#: The bucket for every hour no band claims — 21:00–04:59, wrapping midnight.
DAY_PART_WRAPPING = "night"

_QUALIFIED_COLUMN = re.compile(r"[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*")


def day_part_for_hour(hour: int) -> str:
    """The `day_part` for an hour-of-day, in Python.

    The pure twin of `_day_part_case()`: same bands, same wrapping ELSE. Used
    by tests to check the generated SQL against the contract table without
    needing a database, and available to any caller that already has an hour.
    """
    if not 0 <= hour <= 23:
        raise ValueError(f"hour out of range: {hour!r}")
    for start, end, label in DAY_PART_BANDS:
        if start <= hour < end:
            return label
    return DAY_PART_WRAPPING


def _day_part_case(ts_expr: str) -> str:
    """The `day_part` CASE expression over `ts_expr`.

    `ts_expr` is a qualified timestamp column such as `de.planned_ts`, always
    written literally at the call site — no user-controlled string reaches
    this function. It is validated as a bare `alias.column` identifier anyway;
    see the module docstring on why this fragment is interpolated rather than
    bound.
    """
    if not _QUALIFIED_COLUMN.fullmatch(ts_expr):
        raise ValueError(f"not a bare qualified column: {ts_expr!r}")
    branches = "\n".join(
        f"                        WHEN EXTRACT(HOUR FROM {ts_expr}) >= {start}\n"
        f"                         AND EXTRACT(HOUR FROM {ts_expr}) < {end}"
        f" THEN '{label}'"
        for start, end, label in DAY_PART_BANDS
    )
    return (
        "CASE\n"
        f"{branches}\n"
        f"                        ELSE '{DAY_PART_WRAPPING}'\n"
        "                    END"
    )


# ---------------------------------------------------------------------------
# Lakebase connection pool
# ---------------------------------------------------------------------------

# Sized for "a small Streamlit app" (Task 3.3's own framing), not a
# production web service: a handful of concurrent Today-view/chat sessions
# at hackathon-demo scale, not hundreds. min_size=1 avoids holding idle
# connections open against Lakebase Autoscaling when nobody's using the app;
# max_size=5 is comfortably above what one demo session needs while staying
# well under Free Edition's (already small) connection ceiling.
_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 5


def _conninfo() -> str:
    """The libpq conninfo string for Lakebase, or the local Postgres standing
    in for it.

    NEURORX_LOCAL_PG is used verbatim — it is a complete conninfo string for
    the off-workspace demo path (docs/local_dev.md), typically a Unix socket
    with no TLS. Appending to it would break that. Same switch the cohort
    loader (lakebase/07_load_cohort.py) honors, so both point at the same
    store from one env var.

    Extracted from _get_pool() so non-Streamlit callers (tests, jobs) can
    build the same connection without going through @st.cache_resource.
    """
    local = os.getenv("NEURORX_LOCAL_PG")
    if local:
        return local
    return (
        f"host={settings.lakebase_host} "
        f"dbname={settings.lakebase_db} "
        f"user={settings.lakebase_user} "
        f"password={settings.lakebase_password} "
        f"sslmode=require port=5432"
    )


@st.cache_resource
def _get_pool() -> ConnectionPool:
    """One pool per Streamlit process, memoized via st.cache_resource so a
    script rerun (Streamlit's normal execution model — the whole script
    re-executes on every interaction) reuses the same pool instead of
    leaking a new one on every rerun.
    """
    return ConnectionPool(
        _conninfo(),
        min_size=_POOL_MIN_SIZE,
        max_size=_POOL_MAX_SIZE,
        open=True,
    )


def _row_to_dict(row: Any) -> Optional[dict]:
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_patient(patient_id: str) -> Optional[dict]:
    """The patient record, or None if patient_id doesn't exist."""
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT patient_id, display_name, caregiver_name, created_at
                FROM patients
                WHERE patient_id = %(patient_id)s
                """,
                {"patient_id": patient_id},
            )
            return _row_to_dict(cur.fetchone())


def list_active_schedules(patient_id: str) -> list[dict]:
    """This patient's active prescriptions — the Today view / Maintain
    flow's primary read. Uses idx_schedules_patient_active (Task 3.1).
    """
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT schedule_id, patient_id, rxcui, drug_name, dose_text,
                       times_per_day, dose_times, timing_notes, status,
                       created_at, updated_at
                FROM schedules
                WHERE patient_id = %(patient_id)s AND status = 'active'
                ORDER BY drug_name
                """,
                {"patient_id": patient_id},
            )
            return [dict(row) for row in cur.fetchall()]


def todays_doses(patient_id: str) -> list[dict]:
    """Every dose slot due today for this patient's active schedules, each
    annotated with its actual status.

    Generates the day's expected slots by unnesting each active schedule's
    `dose_times` against today's date, then LEFT JOINs `dose_events` on the
    exact (schedule_id, planned_ts) pair — a slot with no matching row is
    reported as `status='planned'` with `event_id=None`, i.e. the reminders
    job (DATA_CONTRACTS.md §6.3) hasn't materialized it yet. This is exactly
    the case `mark_dose()` below handles by inserting the row itself rather
    than assuming one already exists.

    Returns `dose_text` and `day_part` alongside the original columns — both
    added for Task 3.5 (the Today view), which needs `dose_text` to actually
    display what's being taken, and `day_part` to group the checklist. Per
    Task 3.5's own "no new business logic in the view" instruction,
    `day_part` is classified here in SQL, not in the view — by
    `_day_part_case()`, the module-level generator that also builds the
    Dashboard's CASE in `_adherence_summary_local()`. Both come from one
    `DAY_PART_BANDS` table (`DATA_CONTRACTS.md` §1: morning 05:00–11:59,
    afternoon 12:00–16:59, evening 17:00–20:59, night 21:00–04:59 wrapping
    midnight as the `ELSE` branch), so a dose bucketed "evening" here and a
    dose bucketed "evening" in the adherence dashboard are the same rule by
    construction rather than by two copies agreeing today.

    ⚠️ **No per-patient timezone column exists anywhere in
    `DATA_CONTRACTS.md`.** "Today" here is the Lakebase server's own
    `CURRENT_DATE`, in whatever timezone that Postgres session is
    configured for (Lakebase's default, not chosen per-patient) — flagged as
    a real gap, not silently assumed correct. Fine for a single-timezone
    demo cohort; would need a `patients.timezone` column for a real
    multi-timezone deployment.
    """
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                WITH todays_slots AS (
                    SELECT
                        s.schedule_id,
                        s.patient_id,
                        s.rxcui,
                        s.drug_name,
                        s.dose_text,
                        (CURRENT_DATE + dt)::timestamptz AS planned_ts
                    FROM schedules s, unnest(s.dose_times) AS dt
                    WHERE s.patient_id = %(patient_id)s AND s.status = 'active'
                )
                SELECT
                    t.schedule_id,
                    t.patient_id,
                    t.rxcui,
                    t.drug_name,
                    t.dose_text,
                    t.planned_ts,
                    de.event_id,
                    de.actioned_ts,
                    COALESCE(de.status, 'planned') AS status,
                    {_day_part_case("t.planned_ts")} AS day_part
                FROM todays_slots t
                LEFT JOIN dose_events de
                    ON de.schedule_id = t.schedule_id
                   AND de.planned_ts  = t.planned_ts
                ORDER BY t.planned_ts
                """,
                {"patient_id": patient_id},
            )
            return [dict(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_schedules_local(patient_id: str, drugs: list[dict]) -> dict:
    """Insert confirmed extraction drugs as active schedules in local Postgres.

    **Local-demo path only** (`NEURORX_LOCAL_PG`). On a real workspace the app
    goes through the `neurorx.app.manage_schedule` UC function
    (`agent_client.call_manage_schedule`), which also runs the mandatory
    interaction-check gate before any drug is added (Task 2.3). That gate reads
    `gold.interaction_pairs`, which does not exist locally — so this local
    insert does **not** perform an interaction check, and callers/UI should not
    imply it did. It exists purely so the Create flow (paste → extract →
    confirm → appears in Today) is demonstrable off-workspace.

    Each drug must already be resolved: `rxcui` a numeric string,
    `times_per_day` an int, `dose_times` a list of ``HH:MM:SS`` strings whose
    length equals `times_per_day` (the same shape `manage_schedule`'s
    `create_from_extraction` requires). The `schedules` table's own CHECK
    constraints (numeric rxcui, times_per_day == cardinality(dose_times))
    enforce this server-side — a bad row raises rather than corrupting Today.
    """
    inserted = []
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            for d in drugs:
                cur.execute(
                    """
                    INSERT INTO schedules
                        (patient_id, rxcui, drug_name, dose_text,
                         times_per_day, dose_times, timing_notes, status)
                    VALUES
                        (%(patient_id)s, %(rxcui)s, %(drug_name)s, %(dose_text)s,
                         %(times_per_day)s, %(dose_times)s::time[],
                         %(timing_notes)s, 'active')
                    RETURNING schedule_id, drug_name
                    """,
                    {
                        "patient_id": patient_id,
                        "rxcui": str(d.get("rxcui")),
                        "drug_name": d.get("drug_name"),
                        "dose_text": d.get("dose_text") or "",
                        "times_per_day": d.get("times_per_day"),
                        "dose_times": list(d.get("dose_times") or []),
                        "timing_notes": d.get("timing_notes"),
                    },
                )
                inserted.append(dict(cur.fetchone()))
    return {"status": "created", "count": len(inserted), "schedules": inserted}


def mark_dose(
    *,
    schedule_id: str,
    planned_ts: datetime,
    status: str,
    ts: Optional[datetime] = None,
    event_id: Optional[str] = None,
) -> dict:
    """Mark a dose taken/skipped/missed, creating the planned row first if
    the reminders job hasn't materialized it yet.

    Callers normally have `schedule_id` + `planned_ts` from `todays_doses()`
    (not necessarily an `event_id`, since a not-yet-materialized slot has
    none) — this is why the upsert keys on `(schedule_id, planned_ts)`
    (`dose_events_slot_unique`, added to `lakebase/schema.sql` in this same
    task specifically so this upsert has a real constraint to conflict on)
    rather than requiring the caller to already know an `event_id`.
    `event_id` is accepted too, for a caller that already has it, but is not
    required.

    `status='taken'` or `'skipped'` requires `ts` (the action timestamp) per
    `dose_events_actioned_consistent` (`lakebase/schema.sql`) — passed
    through as `actioned_ts`; `status='planned'`/`'missed'` must leave it
    NULL, same constraint, same reason.
    """
    if status not in ("planned", "taken", "skipped", "missed"):
        raise ValueError(f"Invalid dose status: {status!r}")
    actioned_ts = ts if status in ("taken", "skipped") else None

    # A dose can legitimately be actioned EARLY — taking the 18:00 dose at
    # 17:40, or skipping tonight's dose in advance ("I know I won't take
    # it"). But DATA_CONTRACTS.md §6.3's frozen `actioned_after_planned`
    # CHECK requires actioned_ts >= planned_ts, so an early action's true
    # wall-clock time is unrepresentable; writing it raises CheckViolation
    # and (before this fix) crashed the whole Today tab on the first
    # early Skip click — found by a user actually clicking, not by the
    # test suite, which only ever marked a past-due dose. Clamp to
    # planned_ts: the action is recorded against its slot, the (small)
    # loss of the true early timestamp is the price of the frozen contract.
    if actioned_ts is not None and actioned_ts < planned_ts:
        actioned_ts = planned_ts

    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            # patient_id is looked up from schedules rather than trusted from
            # the caller — dose_events.patient_id is denormalized (Task 3.1's
            # schema note) specifically so it must always agree with the
            # owning schedule's patient_id, never be independently supplied.
            cur.execute(
                """
                INSERT INTO dose_events (event_id, schedule_id, patient_id, planned_ts, actioned_ts, status)
                SELECT
                    COALESCE(%(event_id)s, gen_random_uuid()),
                    %(schedule_id)s,
                    s.patient_id,
                    %(planned_ts)s,
                    %(actioned_ts)s,
                    %(status)s
                FROM schedules s
                WHERE s.schedule_id = %(schedule_id)s
                ON CONFLICT (schedule_id, planned_ts)
                DO UPDATE SET actioned_ts = EXCLUDED.actioned_ts,
                              status      = EXCLUDED.status
                RETURNING event_id, schedule_id, patient_id, planned_ts, actioned_ts, status
                """,
                {
                    "event_id": event_id,
                    "schedule_id": schedule_id,
                    "planned_ts": planned_ts,
                    "actioned_ts": actioned_ts,
                    "status": status,
                },
            )
            row = cur.fetchone()
            conn.commit()
            if row is None:
                raise ValueError(f"No schedule found with schedule_id={schedule_id!r}")
            return dict(row)


# Hoisted to module level so tests can execute this exact string rather than a
# hand-copied paraphrase of it — the same reason Task 3.7 ran reminders_job's
# real functions instead of extracted SQL. `tests/test_reminder_banner_window.py`
# runs it in DuckDB.
UNACKNOWLEDGED_REMINDERS_SQL = """
SELECT notification_id, patient_id, schedule_id, due_ts, message, acknowledged, created_at
FROM notifications
WHERE patient_id = %(patient_id)s
  AND acknowledged = false
  AND due_ts::date = CURRENT_DATE
ORDER BY due_ts DESC
"""


def list_unacknowledged_reminders(patient_id: str) -> list[dict]:
    """This patient's unacknowledged dose reminders **for today**,
    most-recently-due first.

    Reads the real `notifications` table (`lakebase/schema.sql`, Task 3.7),
    populated by the scheduled reminders job (`app/jobs/reminders_job.py`).
    **Supersedes this function's own earlier provisional version** (Task
    3.5, written before Task 3.7 existed): that version assumed a
    `(notification_id, patient_id, message, created_at, acknowledged_at)`
    shape with a nullable `acknowledged_at` timestamp; the real table
    Task 3.7 defines instead has `schedule_id`, `due_ts`, and a plain
    `acknowledged BOOLEAN` — updated here to match, rather than left
    disagreeing with the table that actually exists now.

    ## Why the `due_ts::date = CURRENT_DATE` bound exists

    `acknowledged` starts false and only a human pressing "Dismiss" in the
    Today view's banner ever flips it. Nothing expires a row. Without a date
    bound this query returned **every notification ever written for this
    patient that nobody dismissed**, and `today.py`'s banner renders one
    `st.info` block per returned row: the reminders job writes ~5 rows/day
    for the demo cohort's 4 drugs, so a patient who simply doesn't press
    Dismiss accumulates a growing wall of banners above their checklist,
    unbounded in the number of days the job has been running.

    The messages are what makes that a safety problem rather than a layout
    one. `reminders_job.build_message()` produces "Time for your warfarin
    (5 mg) at 07:00 PM." — **no date**. A four-day-old reminder is therefore
    indistinguishable from the one for the dose actually due in 20 minutes,
    and the persona this view is written for (`today.py`'s docstring: a
    60-year-old managing several chronic prescriptions) is being told to take
    a dose that isn't due.

    Bounded to `CURRENT_DATE` to match `todays_doses()` above, which scopes
    its slots the same way — the banner and the checklist beneath it now
    describe the same day instead of the banner silently spanning all of
    history. This is not a new policy invented here: it is the scope the view
    the banner lives in already had.

    Still degrades gracefully if `notifications` somehow doesn't exist
    (catches `psycopg.errors.UndefinedTable` specifically, not a bare
    `except`, which would mask a real bug) — belt-and-suspenders now that
    the table is real, not the load-bearing case it was in Task 3.5.
    """
    import psycopg

    with _get_pool().connection() as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    UNACKNOWLEDGED_REMINDERS_SQL,
                    {"patient_id": patient_id},
                )
                return [dict(row) for row in cur.fetchall()]
        except psycopg.errors.UndefinedTable:
            conn.rollback()  # the failed statement poisons the transaction otherwise
            return []


def acknowledge_reminder(notification_id: str) -> None:
    """Mark one reminder acknowledged (Today view's banner dismiss action,
    Task 3.5). Flips the real `acknowledged` boolean (Task 3.7's schema),
    not a timestamp column — updated from this function's own earlier
    provisional version for the same reason as
    `list_unacknowledged_reminders()` above. Same graceful no-op if the
    table is somehow absent.
    """
    import psycopg

    with _get_pool().connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE notifications SET acknowledged = true WHERE notification_id = %(id)s",
                    {"id": notification_id},
                )
                conn.commit()
        except psycopg.errors.UndefinedTable:
            conn.rollback()


def log_guardrail_block(
    *,
    model_output_excerpt: str,
    rule_triggered: str,
    patient_id: Optional[str] = None,
    judge_verdict: Optional[str] = None,
) -> dict:
    """Append a guardrail-block record. Append-only by convention — this
    function never updates or deletes a row (DATA_CONTRACTS.md §6.4: the
    table is only credible as evidence if nothing quietly edits it after the
    fact). `patient_id=None` is the documented case for an anonymous or
    pre-auth session, not an error.
    """
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO guardrail_blocks (patient_id, model_output_excerpt, rule_triggered, judge_verdict)
                VALUES (%(patient_id)s, %(model_output_excerpt)s, %(rule_triggered)s, %(judge_verdict)s)
                RETURNING block_id, ts, patient_id, model_output_excerpt, rule_triggered, judge_verdict
                """,
                {
                    "patient_id": patient_id,
                    "model_output_excerpt": model_output_excerpt,
                    "rule_triggered": rule_triggered,
                    "judge_verdict": judge_verdict,
                },
            )
            row = cur.fetchone()
            conn.commit()
            return dict(row)


# ---------------------------------------------------------------------------
# Analytics — Delta, not Lakebase (see module docstring)
# ---------------------------------------------------------------------------


@st.cache_resource
def get_warehouse_http_path() -> str:
    """Free Edition provides exactly one pre-created SQL warehouse
    (CLAUDE.md §4) — discovered live via the SDK rather than hardcoded,
    mirroring the identical pattern `agent/log_agent.py` already established
    for Task 2.8's `build_resources()`. `.odbc_params.path` is the verified
    attribute name (see module docstring) — not a guessed `.http_path`.
    """
    # On the local demo path there is no Databricks workspace at all, so warehouse
    # discovery cannot succeed — and against a placeholder host the SDK retries for
    # minutes before finally raising, which reads as a hung dashboard. Fail fast
    # and clearly instead. Delta-backed analytics simply are not available without
    # a workspace; the caller (dashboard) degrades gracefully on this error.
    import os

    if os.getenv("NEURORX_LOCAL_PG"):
        raise RuntimeError(
            "Delta/SQL-warehouse analytics are unavailable on the local demo path "
            "(NEURORX_LOCAL_PG is set). Connect a Databricks workspace to enable them."
        )

    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient(host=settings.databricks_host, token=settings.databricks_token)
    warehouse = next(iter(w.warehouses.list()), None)
    if warehouse is None:
        raise RuntimeError("No SQL warehouse found in this workspace.")
    return warehouse.odbc_params.path


def sql_connect():
    """A fresh Databricks SQL connector connection per call, not pooled.

    Deliberately not memoized like the Lakebase pool above: a Databricks SQL
    warehouse (especially Free Edition's single small one) can auto-suspend
    after idle time, and a long-held cached connection object would go stale
    across a suspend/resume cycle in a way that's easy to misdiagnose in a
    demo. `adherence_summary()`/`resolve_citations()` are called
    infrequently enough (once per dashboard load or chat citation) that the
    per-call connection overhead is not worth trading away that robustness.
    """
    from databricks import sql as databricks_sql

    server_hostname = settings.databricks_host.removeprefix("https://").removeprefix("http://")
    return databricks_sql.connect(
        server_hostname=server_hostname,
        http_path=get_warehouse_http_path(),
        access_token=settings.databricks_token,
    )


# ---------------------------------------------------------------------------
# Local-demo analytics (computed from local Postgres dose_events, not Delta)
# ---------------------------------------------------------------------------
#
# On a real workspace the Dashboard reads pre-aggregated `gold.adherence_facts`
# (Delta) via the SQL warehouse — the F9 OLTP-vs-analytics split. That table
# does not exist locally. These functions recompute the same aggregates
# directly from local `dose_events` (joined to `schedules` for drug_name) so the
# Dashboard is demonstrable off-workspace. Same window rule as the UC function
# (Task 2.4): whole days ending yesterday, today excluded; the day-part CASE is
# generated by `_day_part_case()` at the top of this file, the same call
# `todays_doses()` makes, so the Today checklist and the Dashboard cannot
# bucket the same timestamp differently; adherence = taken/planned (skips count
# against it, per F4). The streak is a local reimplementation of the UC
# function's "consecutive fully-taken days ending yesterday" — acceptable here
# because there is no UC function to call locally, flagged as such.


def get_patient(patient_id: str) -> Optional[dict]:
    """The patient's display name + their active medication list, from local
    Postgres. Used by the Dashboard header so it shows a real name and drugs
    rather than a bare UUID. Returns None if the patient row does not exist."""
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT patient_id, display_name, caregiver_name "
                "FROM patients WHERE patient_id = %(pid)s",
                {"pid": patient_id},
            )
            patient = cur.fetchone()
            if patient is None:
                return None
            cur.execute(
                """
                SELECT drug_name, rxcui, dose_text, times_per_day, dose_times,
                       timing_notes
                FROM schedules
                WHERE patient_id = %(pid)s AND status = 'active'
                ORDER BY drug_name
                """,
                {"pid": patient_id},
            )
            meds = [dict(r) for r in cur.fetchall()]
    return {
        "patient_id": str(patient["patient_id"]),
        "display_name": patient["display_name"],
        "caregiver_name": patient["caregiver_name"],
        "medications": meds,
    }


def _adherence_summary_local(patient_id: str, days: int = 30) -> list[dict]:
    """Local `adherence_summary` equivalent: per (drug, day, day_part) counts,
    computed from `dose_events` joined to `schedules`. Same columns/shape the
    workspace version returns from `gold.adherence_facts`."""
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                WITH ev AS (
                    SELECT de.patient_id, s.rxcui, s.drug_name,
                           de.planned_ts::date AS event_date,
                           {_day_part_case("de.planned_ts")} AS day_part,
                           de.status
                    FROM dose_events de
                    JOIN schedules s ON s.schedule_id = de.schedule_id
                    WHERE de.patient_id = %(pid)s
                      AND de.planned_ts::date >= CURRENT_DATE - %(days)s
                      AND de.planned_ts::date <= CURRENT_DATE - 1
                )
                SELECT patient_id, rxcui, drug_name, event_date, day_part,
                       count(*)                                    AS planned_doses,
                       count(*) FILTER (WHERE status = 'taken')    AS taken_doses,
                       count(*) FILTER (WHERE status = 'skipped')  AS skipped_doses,
                       count(*) FILTER (WHERE status = 'missed')   AS missed_doses,
                       round(100.0 * count(*) FILTER (WHERE status = 'taken')
                             / NULLIF(count(*), 0), 1)             AS adherence_pct
                FROM ev
                GROUP BY patient_id, rxcui, drug_name, event_date, day_part
                ORDER BY event_date DESC, day_part
                """,
                {"pid": patient_id, "days": days},
            )
            return [dict(r) for r in cur.fetchall()]


def _streak_from_daily_totals(
    day_totals: dict[date, list[int]],
    window_days: int,
    today: Optional[date] = None,
) -> int:
    """Consecutive fully-adherent days ending yesterday, per the semantics
    `agent/tools/get_adherence_stats.sql`'s `streak_calc` CTE defines and the
    UC function's own COMMENT promises to the agent.

    Split out as a pure function on purpose: this is the one metric
    `_get_adherence_stats_local` genuinely re-implements rather than reads, and
    `get_adherence_stats`'s docstring already warns that a second streak
    implementation risks the two-silently-diverging-implementations problem
    this project has hit before. A pure function is testable against the SQL's
    documented behaviour without a live workspace or a Postgres fixture — see
    `tests/test_adherence_streak.py`.

    Two rules that a naive "walk back while the day is clean" loop gets wrong,
    both of them documented SQL behaviour, not choices made here:

    1. **The streak is measured to yesterday, not to the newest day with
       data.** SQL anchors both branches at `date_sub(current_date(), 1)`. If
       the freshest rows are three days old, those three days are still part
       of the count.
    2. **A calendar day with no rows does not break the streak.** The SQL
       header states it outright: such a day is vacuously adherent — nothing
       was due, so nothing was missed — and is counted through. `datediff`
       spans it because it measures calendar distance, not row count.

    `today` is injectable for deterministic tests; it defaults to the host
    clock. Note the small assumption that comes with that: the window filter in
    `_adherence_summary_local` is evaluated by Postgres (`CURRENT_DATE`) while
    this boundary is evaluated by Python, so the two can disagree if the app
    host and the database sit in different timezones. They do not in the local
    demo path (`docs/local_dev.md` — same machine); if that ever stops being
    true, take the reference day from the same connection instead of here.
    """
    if not day_totals:
        return 0

    reference_day = (today or date.today()) - timedelta(days=1)

    # Mirrors the SQL's `WHERE d.taken_doses < d.planned_doses` exactly — a day
    # with zero planned doses is not a bad day under either spelling.
    last_bad_day = max(
        (day for day, (taken, planned) in day_totals.items() if taken < planned),
        default=None,
    )

    if last_bad_day is None:
        # Clean window: count from the earliest day actually covered, so a
        # history that only starts five days ago reports 5, not a fabricated
        # `window_days`.
        streak = (reference_day - min(day_totals)).days + 1
    else:
        # The gap between yesterday and the last bad day. Zero when yesterday
        # itself was the bad day, which is correct.
        streak = (reference_day - last_bad_day).days

    # The window filter already bounds this; clamping makes the "capped by
    # window_days" half of the function COMMENT true by construction rather
    # than by assumption about the caller.
    return max(0, min(streak, window_days))


def _get_adherence_stats_local(patient_id: str, window_days: int = 30) -> dict:
    """Local `get_adherence_stats` equivalent — aggregates the same five metrics
    the UC function returns, from `_adherence_summary_local`'s rows in Python."""
    empty = {
        "overall_adherence_pct": None,
        "current_streak_days": None,
        "most_missed_drug": None,
        "most_missed_daypart": None,
        "adherence_by_drug": [],
    }
    rows = _adherence_summary_local(patient_id, window_days)
    if not rows:
        return empty

    total_taken = sum(r["taken_doses"] for r in rows)
    total_planned = sum(r["planned_doses"] for r in rows)

    by_drug_taken: dict[str, int] = {}
    by_drug_planned: dict[str, int] = {}
    missed_by_drug: dict[str, int] = {}
    missed_by_daypart: dict[str, int] = {}
    day_totals: dict[date, list[int]] = {}
    for r in rows:
        d = r["drug_name"]
        by_drug_taken[d] = by_drug_taken.get(d, 0) + r["taken_doses"]
        by_drug_planned[d] = by_drug_planned.get(d, 0) + r["planned_doses"]
        if r["missed_doses"]:
            missed_by_drug[d] = missed_by_drug.get(d, 0) + r["missed_doses"]
            missed_by_daypart[r["day_part"]] = (
                missed_by_daypart.get(r["day_part"], 0) + r["missed_doses"]
            )
        tot = day_totals.setdefault(r["event_date"], [0, 0])
        tot[0] += r["taken_doses"]
        tot[1] += r["planned_doses"]

    adherence_by_drug = [
        {
            "drug_name": name,
            "adherence_pct": round(100.0 * by_drug_taken[name] / by_drug_planned[name], 1),
        }
        for name in sorted(by_drug_planned)
        if by_drug_planned[name]
    ]

    most_missed_drug = None
    if missed_by_drug:
        name, cnt = sorted(missed_by_drug.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        most_missed_drug = {"drug_name": name, "missed_count": float(cnt)}

    most_missed_daypart = None
    if missed_by_daypart:
        part, cnt = sorted(missed_by_daypart.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        most_missed_daypart = {"daypart": part, "missed_count": float(cnt)}

    streak = _streak_from_daily_totals(day_totals, window_days)

    return {
        "overall_adherence_pct": (
            round(100.0 * total_taken / total_planned, 1) if total_planned else None
        ),
        "current_streak_days": streak,
        "most_missed_drug": most_missed_drug,
        "most_missed_daypart": most_missed_daypart,
        "adherence_by_drug": adherence_by_drug,
    }


def adherence_summary(patient_id: str, days: int = 30) -> list[dict]:
    """Adherence aggregates for the dashboard, read from
    `neurorx.gold.adherence_facts` via the Databricks SQL connector.

    **Analytics reads go to Delta, never Lakebase — see this module's own
    docstring for why.** This function does not open a Lakebase connection
    at all; it is the one function in this file that never touches
    `_get_pool()`.

    Uses the Databricks SQL connector's own named paramstyle (`:patient_id`,
    a dict of params) — NOT psycopg's `%(name)s` style used everywhere else
    in this file. Confirmed against the connector's real source this
    session; see module docstring.

    **Local-demo path** (`NEURORX_LOCAL_PG`): computed from local Postgres
    `dose_events` instead of the Delta gold table — see
    `_adherence_summary_local`.
    """
    if os.getenv("NEURORX_LOCAL_PG"):
        return _adherence_summary_local(patient_id, days)

    with sql_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT patient_id, rxcui, drug_name, event_date, day_part,
                       planned_doses, taken_doses, skipped_doses, missed_doses,
                       adherence_pct
                FROM neurorx.gold.adherence_facts
                WHERE patient_id = :patient_id
                  AND event_date >= current_date - :days
                ORDER BY event_date DESC, day_part
                """,
                {"patient_id": patient_id, "days": days},
            )
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_adherence_stats(patient_id: str, window_days: int = 30) -> dict:
    """Header-stat metrics for the Dashboard view (Task 3.6 Requirement 1):
    overall adherence %, current streak, most-missed drug, most-missed
    time of day, and adherence % by drug.

    **Calls the existing `neurorx.app.get_adherence_stats` UC function
    (Task 2.4) directly, via SQL, rather than recomputing these metrics
    from `adherence_summary()`'s raw rows.** The streak calculation in
    particular is genuinely non-trivial (consecutive-days-ending-yesterday,
    capped by the window, with its own DuckDB-verified edge cases — see
    that file's own history: an empty-history bug that silently produced
    `current_streak_days=0` instead of no rows at all, caught only by
    running it) — reimplementing it here in Python would risk exactly the
    kind of two-silently-diverging-implementations problem this project
    has already caught and fixed once (Task 3.5's day-part boundaries).
    One correct implementation, called from both the chat agent and the
    dashboard.

    `get_adherence_stats` is a table-valued UC function (`RETURNS TABLE`),
    invoked here as `SELECT * FROM ...(...)` — different call shape from
    `agent_client.call_manage_schedule()`'s scalar `SELECT ...(...)` (that
    one `RETURNS STRING`); confirmed against the function's own
    `CREATE FUNCTION` signature in `agent/tools/get_adherence_stats.sql`
    before assuming either shape.

    Returns:
        {
          "overall_adherence_pct": float | None,
          "current_streak_days": int | None,
          "most_missed_drug": {"drug_name": str, "missed_count": float} | None,
          "most_missed_daypart": {"daypart": str, "missed_count": float} | None,
          "adherence_by_drug": [{"drug_name": str, "adherence_pct": float}, ...],
        }

    Per the UC function's own contract: an absent `most_missed_drug` or
    `most_missed_daypart` row means nothing was missed in the window, not
    missing data — reported here as `None`, which the view must render as
    "nothing missed," not as "no data." A wholly empty result (no rows at
    all) means no dose history exists for this patient/window — every field
    below is `None`/`[]` in that case, and the view must not read that as
    perfect adherence.

    **Local-demo path** (`NEURORX_LOCAL_PG`): computed from local Postgres
    `dose_events` instead of calling the UC function — see
    `_get_adherence_stats_local`.
    """
    if os.getenv("NEURORX_LOCAL_PG"):
        return _get_adherence_stats_local(patient_id, window_days)

    with sql_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT metric, drug_name, value_num, value_text "
                "FROM neurorx.app.get_adherence_stats(:patient_id, :window_days)",
                {"patient_id": patient_id, "window_days": window_days},
            )
            rows = cur.fetchall()

    result = {
        "overall_adherence_pct": None,
        "current_streak_days": None,
        "most_missed_drug": None,
        "most_missed_daypart": None,
        "adherence_by_drug": [],
    }
    for metric, drug_name, value_num, value_text in rows:
        if metric == "overall_adherence_pct":
            result["overall_adherence_pct"] = value_num
        elif metric == "current_streak_days":
            result["current_streak_days"] = int(value_num) if value_num is not None else None
        elif metric == "most_missed_drug":
            result["most_missed_drug"] = {"drug_name": value_text, "missed_count": value_num}
        elif metric == "most_missed_daypart":
            result["most_missed_daypart"] = {"daypart": value_text, "missed_count": value_num}
        elif metric == "adherence_pct":
            result["adherence_by_drug"].append({"drug_name": drug_name, "adherence_pct": value_num})

    return result


def refill_estimates(patient_id: str) -> list[dict]:
    """⚠️ Not implemented — flagged gap, not a fabricated answer.

    `DATA_CONTRACTS.md` §6.2's `schedules` columns have no fill-quantity,
    days-supply, or refill-date field of any kind — there is nothing in the
    frozen contract this function could honestly compute "pills remaining"
    from. `dose_text` (e.g. "500 mg") is explicitly documented as "never
    parsed for clinical logic" (§6.2) and doesn't carry a quantity dispensed
    in any case.

    Returns one dict per active schedule with `pills_remaining=None`,
    `days_remaining=None`, and an explicit `unavailable_reason`, rather than
    either raising (which would break a dashboard that expects a list) or
    inventing a number — the UI layer is expected to render "refill
    tracking not available" per schedule, not silently omit the row.

    `days_remaining` is included (always `None` today) specifically so a
    caller — Task 3.5's Today view — can implement its "<7 days" refill
    badge as a plain field check (`if days_remaining is not None and
    days_remaining < 7`) rather than computing pills-to-days conversion
    itself; that division (`pills_remaining / doses_per_day`) is exactly
    the kind of derived-value computation that belongs in the data layer,
    not the view, once `pills_remaining` has a real source to divide.
    """
    schedules = list_active_schedules(patient_id)
    return [
        {
            "schedule_id": s["schedule_id"],
            "drug_name": s["drug_name"],
            "pills_remaining": None,
            "days_remaining": None,
            "unavailable_reason": (
                "DATA_CONTRACTS.md schedules has no fill-quantity/days-supply "
                "column — refill tracking needs a schema change, not a "
                "computation, before this can return a real number."
            ),
        }
        for s in schedules
    ]


# ---------------------------------------------------------------------------
# Accounts (DATA_CONTRACTS.md §6.5)
#
# Sign-in identity. NOT an access boundary — the demo patient switcher stays,
# so a signed-in account can still read any patient. See app/auth.py.
# ---------------------------------------------------------------------------


def _normalize_email(email: str) -> str:
    """Lowercase and trim. The accounts_email_normalized CHECK enforces this
    database-side too — this is the convenience, not the guarantee."""
    return email.strip().lower()


@contextmanager
def _conn_or_pooled(conn):
    """Yield the caller's connection, or borrow one from the pool.

    Tests pass an explicit connection so their writes stay inside a single
    rolled-back transaction. The app passes nothing: db's pool is
    @st.cache_resource-decorated and would otherwise open a second connection
    that cannot see uncommitted rows.
    """
    if conn is not None:
        yield conn
    else:
        with _get_pool().connection() as pooled:
            yield pooled


def create_account_with_patient(
    email: str, display_name: str, password_hash: str, conn=None
) -> dict:
    """Create the patient row and its account in ONE transaction.

    Both inserts share a transaction so a duplicate-email failure cannot leave
    an orphan patient behind. Raises psycopg.errors.UniqueViolation when the
    email already exists; the caller decides how to phrase that.
    """
    normalized = _normalize_email(email)
    with _conn_or_pooled(conn) as active:
        with active.transaction():
            with active.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    INSERT INTO patients (display_name)
                    VALUES (%(name)s)
                    RETURNING patient_id
                    """,
                    {"name": display_name},
                )
                patient_id = cur.fetchone()["patient_id"]

                cur.execute(
                    """
                    INSERT INTO accounts (email, display_name, password_hash, patient_id)
                    VALUES (%(email)s, %(name)s, %(hash)s, %(patient_id)s)
                    RETURNING account_id, email, display_name, patient_id
                    """,
                    {
                        "email": normalized,
                        "name": display_name,
                        "hash": password_hash,
                        "patient_id": patient_id,
                    },
                )
                row = cur.fetchone()

    return {
        "account_id": str(row["account_id"]),
        "email": row["email"],
        "display_name": row["display_name"],
        "patient_id": str(row["patient_id"]),
    }


def find_account_by_email(email: str, conn=None) -> Optional[dict]:
    """The account for this email, or None. Includes password_hash — the only
    function that returns it, and only auth.authenticate() should call it."""
    with _conn_or_pooled(conn) as active:
        with active.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT account_id, email, display_name, patient_id, password_hash
                FROM accounts
                WHERE email = %(email)s
                """,
                {"email": _normalize_email(email)},
            )
            row = cur.fetchone()

    if row is None:
        return None
    return {
        "account_id": str(row["account_id"]),
        "email": row["email"],
        "display_name": row["display_name"],
        "patient_id": str(row["patient_id"]),
        "password_hash": row["password_hash"],
    }


def touch_last_login(account_id: str, conn=None) -> None:
    """Record a successful sign-in."""
    with _conn_or_pooled(conn) as active:
        with active.cursor() as cur:
            cur.execute(
                "UPDATE accounts SET last_login_at = now() WHERE account_id = %(a)s",
                {"a": account_id},
            )
