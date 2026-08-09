"""The reminder banner must not show yesterday's reminders.

`notifications.acknowledged` starts false and only a human pressing "Dismiss"
in `app/views/today.py`'s banner ever flips it. Nothing expires a row, and
`app/jobs/reminders_job.py` writes one per dose slot (~5/day for the demo
cohort's 4 drugs, metformin twice daily). Before the `due_ts::date =
CURRENT_DATE` bound in `db.UNACKNOWLEDGED_REMINDERS_SQL`, the query returned
every undismissed notification ever written for the patient, and the banner
renders one `st.info` block per row.

The messages are why that is a safety problem and not a layout one.
`reminders_job.build_message()` emits "Time for your warfarin (5 mg) at
07:00 PM." with **no date**, so a four-day-old row is indistinguishable from
the one for the dose due in 20 minutes — a patient reading the banner is told
to take a dose that isn't due.

These tests run `db.UNACKNOWLEDGED_REMINDERS_SQL` itself, not a paraphrase of
it, so editing the real query without updating the bound fails here. Same
reason Task 3.7 exercised `reminders_job`'s real functions rather than
hand-copied SQL.

DuckDB, not Postgres: this file needs no `psycopg`, no local Postgres, and no
workspace, so it runs in CI and on a machine that never followed
`docs/local_dev.md`. The one Postgres-ism in the query is the `::date` cast,
which DuckDB spells identically.

The query string is read out of `app/db.py` with `ast`, deliberately **not**
`import app.db`. Importing it pulls in `streamlit`, `psycopg`, `psycopg_pool`
and `app.config` (which reads `.env`) at module scope — none of which this
query needs, and any one of them missing turns a real assertion into a silent
skip. Parsing the file gets the same literal with no runtime dependencies, so
this test cannot pass by not running. It also still fails if the constant is
renamed or deleted.
"""

import ast
import pathlib
import re

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb is a requirements-dev.txt dep")

DB_PY = pathlib.Path(__file__).resolve().parent.parent / "app" / "db.py"
SQL_CONST = "UNACKNOWLEDGED_REMINDERS_SQL"


def _sql_from_source() -> str:
    """The value of `app.db.UNACKNOWLEDGED_REMINDERS_SQL`, read without importing."""
    tree = ast.parse(DB_PY.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == SQL_CONST for t in node.targets
        ):
            assert isinstance(node.value, ast.Constant), (
                f"{SQL_CONST} is no longer a plain string literal — this test "
                "reads it statically and needs it to stay one."
            )
            return node.value.value
    raise AssertionError(f"{SQL_CONST} not found at module level in {DB_PY}")


PATIENT = "11111111-1111-1111-1111-111111111111"
OTHER_PATIENT = "22222222-2222-2222-2222-222222222222"


def _duckdb_sql() -> str:
    """`UNACKNOWLEDGED_REMINDERS_SQL` with psycopg's named placeholder swapped
    for DuckDB's positional one. The only translation applied — everything
    else, including the date bound under test, runs verbatim."""
    sql, n = re.subn(r"%\(patient_id\)s", "?", _sql_from_source())
    assert n == 1, f"expected exactly one patient_id placeholder, found {n}"
    return sql


def _con(rows: list[tuple[str, int, int, str]]):
    """DuckDB holding a `notifications` table shaped like `lakebase/schema.sql`.

    Each row is `(patient_id, days_ago, hour, message)`; `days_ago=0` is today.
    Every row is left unacknowledged, which is the state the bug lives in.
    """
    con = duckdb.connect()
    con.execute(
        """
        CREATE TABLE notifications(
            notification_id UUID DEFAULT uuid(),
            patient_id      TEXT,
            schedule_id     TEXT,
            due_ts          TIMESTAMP,
            message         TEXT,
            acknowledged    BOOLEAN DEFAULT false,
            created_at      TIMESTAMP DEFAULT now()
        )
        """
    )
    for i, (patient, days_ago, hour, message) in enumerate(rows):
        con.execute(
            "INSERT INTO notifications "
            "(notification_id, patient_id, schedule_id, due_ts, message, acknowledged, created_at) "
            "VALUES (uuid(), ?, ?, "
            f"(current_date - {days_ago}) + INTERVAL {hour} HOUR, ?, false, now())",
            [patient, f"sched-{i}", message],
        )
    return con


def _messages(con) -> list[str]:
    return [r[4] for r in con.execute(_duckdb_sql(), [PATIENT]).fetchall()]


def test_yesterdays_undismissed_reminder_is_not_returned():
    """The regression itself: a lapsed slot nobody dismissed stays out."""
    con = _con(
        [
            (PATIENT, 1, 19, "Time for your warfarin (5 mg) at 07:00 PM."),
            (PATIENT, 0, 7, "Time for your metformin (500 mg) at 07:00 AM."),
        ]
    )

    assert _messages(con) == ["Time for your metformin (500 mg) at 07:00 AM."]


def test_a_week_of_undismissed_reminders_collapses_to_today():
    """Volume is the visible half of the bug.

    Seven days x 2 daily metformin slots is 14 undismissed rows, every one of
    which the banner would render as its own block above the checklist.
    """
    rows = [
        (PATIENT, d, h, f"Time for your metformin (500 mg) at {h:02d}:00.")
        for d in range(7)
        for h in (7, 19)
    ]
    con = _con(rows)

    returned = _messages(con)
    assert len(returned) == 2, (
        f"Only today's two slots should reach the banner, got {len(returned)} "
        "— the unbounded query returned all 14."
    )


def test_ordering_and_patient_scoping_are_unchanged():
    """The bound is the only behaviour change: still most-recently-due first,
    still one patient only."""
    con = _con(
        [
            (PATIENT, 0, 7, "morning"),
            (PATIENT, 0, 19, "evening"),
            (OTHER_PATIENT, 0, 12, "someone else's dose"),
        ]
    )

    assert _messages(con) == ["evening", "morning"]


def test_acknowledged_rows_stay_excluded():
    """Dismissing still works — the date bound is ANDed with it, not a
    replacement for it."""
    con = _con([(PATIENT, 0, 7, "morning"), (PATIENT, 0, 19, "evening")])
    con.execute("UPDATE notifications SET acknowledged = true WHERE message = 'morning'")

    assert _messages(con) == ["evening"]


def test_tomorrows_reminder_is_not_shown_early():
    """`reminders_job.find_doses_due_soon()` deliberately generates tomorrow's
    slots too, to close a midnight blind spot — so a row for 00:15 tomorrow can
    legitimately exist before midnight. It belongs on tomorrow's Today screen,
    not tonight's, and the same `CURRENT_DATE` bound handles it.
    """
    con = _con(
        [
            (PATIENT, -1, 0, "Time for your metformin (500 mg) at 12:15 AM."),
            (PATIENT, 0, 19, "Time for your metformin (500 mg) at 07:00 PM."),
        ]
    )

    assert _messages(con) == ["Time for your metformin (500 mg) at 07:00 PM."]
