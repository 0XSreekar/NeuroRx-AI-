"""`missed_doses` counts a status the app never writes.

`get_adherence_stats`'s `most_missed_drug`/`most_missed_daypart` rank on
`adherence_facts.missed_doses`, which counts only rows where
`dose_events.status = 'missed'` literally (`pipelines/medallion_pipeline.py`'s
`adherence_facts` agg, mirrored by `app/db.py`'s `_adherence_summary_local`).

Nothing in the application ever writes that status:

- `app/views/today.py` calls an overdue un-actioned dose "Missed" at **render
  time only** — its own module docstring says the row "genuinely stays
  `status='planned'` in the database until an explicit `mark_dose()` call".
- `app/jobs/reminders_job.py` pre-creates every upcoming slot as `'planned'`.
- `app/db.py`'s `mark_dose()` accepts `'missed'`, but no caller passes it.
- Only `data/ingestion/04_synthetic_cohort.py` writes `'missed'`, and only for
  the Phase-1 synthetic cohort (`verify_cohort.py` asserts the generated
  statuses are exactly `{taken, missed, skipped}` — no `planned` at all).

So on the live Lakebase path — the one Phase 3's `SOURCE_TABLE` flip points
`adherence_facts` at (`pipelines/medallion_pipeline.py`'s TODO) — a genuinely
missed dose sits at `'planned'` forever. It lands in `planned_doses`, so it
correctly depresses `adherence_pct`, but it is invisible to `missed_doses`.

The window is what makes this unambiguous rather than a timing artifact.
`get_adherence_stats.sql` scopes to `[current_date() - window_days,
current_date() - 1]` and says why: *"today is still in progress, so its
unactioned doses are `planned`, not `missed`"*. That reasoning covers today,
and today is already excluded. Every `'planned'` row **inside** the window is
a past slot that was never actioned — exactly what the Today view renders as
"Missed".

Two consequences, both in the agent's mouth:

1. `most_missed_drug` can name the wrong drug — the function COMMENT tells the
   agent these are "facts to be relayed, not estimates to be refined".
2. When no row ever carried a literal `'missed'`, the metric is omitted, and
   the COMMENT instructs the agent: *"Absence of a most_missed_drug row means
   nothing was missed in the window — say that rather than naming a drug."*
   The agent then tells a patient who took none of their doses that nothing
   was missed.

`medallion_pipeline.py`'s `counts_reconcile` expectation is written
`taken + skipped + missed <= planned_doses` — the `<=` absorbs the gap, so no
expectation fires and nothing surfaces at pipeline runtime.

**These tests pin the CURRENT behaviour, which is the wrong behaviour.** They
exist so the gap is stated in an executable place instead of living only in a
reader's head, and so closing it has to be a deliberate edit to this file
rather than a silent change in a number on the dashboard. Widening
`missed_doses` is a change to `DATA_CONTRACTS.md` §5.3's frozen column
semantics, so it is filed as **F16** for sign-off (CLAUDE.md §5/§6: flag
document conflicts, never silently resolve them) rather than fixed here.

No Postgres and no workspace needed: the aggregate and the ranking are run in
DuckDB, the same "transpile-and-run" path `requirements-dev.txt` documents and
Task 2.4 already used to catch a real bug that reading and parsing both missed.
"""

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb is a requirements-dev.txt dep")

# `_day_part_case` in app/db.py and `_day_part_expr` in
# pipelines/medallion_pipeline.py, spelled as the plain SQL both compile to.
# Boundaries per DATA_CONTRACTS.md §1 (F13); test_day_part_boundaries.py owns
# pinning them, this file only needs bucketing to happen at all.
_DAY_PART = """
    CASE WHEN extract(hour from planned_ts) < 12 THEN 'morning'
         WHEN extract(hour from planned_ts) < 17 THEN 'afternoon'
         WHEN extract(hour from planned_ts) < 21 THEN 'evening'
         ELSE 'night' END
"""

# The four count expressions verbatim from app/db.py's `_adherence_summary_local`,
# which is documented there as returning the "same columns/shape the workspace
# version returns from gold.adherence_facts". The window bound is
# get_adherence_stats.sql's: whole days ending yesterday, today excluded.
_FACTS_VIEW = f"""
CREATE VIEW facts AS
WITH ev AS (
    SELECT drug_name,
           planned_ts::date AS event_date,
           {_DAY_PART} AS day_part,
           status
    FROM dose_events
    WHERE planned_ts::date >= current_date - 30
      AND planned_ts::date <= current_date - 1
)
SELECT drug_name, event_date, day_part,
       count(*)                                   AS planned_doses,
       count(*) FILTER (WHERE status = 'taken')    AS taken_doses,
       count(*) FILTER (WHERE status = 'skipped')  AS skipped_doses,
       count(*) FILTER (WHERE status = 'missed')   AS missed_doses
FROM ev
GROUP BY drug_name, event_date, day_part
"""

# get_adherence_stats.sql's `worst_drug` CTE, tiebreak included.
_MOST_MISSED_DRUG = """
SELECT f.drug_name, SUM(f.missed_doses) AS missed_doses
FROM facts f
GROUP BY f.drug_name
HAVING SUM(f.missed_doses) > 0
ORDER BY SUM(f.missed_doses) DESC, f.drug_name ASC
LIMIT 1
"""

# rows_out metric 1: dose-weighted, per design note 2 in the SQL header.
_OVERALL_PCT = """
SELECT round(100.0 * SUM(f.taken_doses) / NULLIF(SUM(f.planned_doses), 0), 1)
FROM facts f
"""

_PER_DRUG_PCT = """
SELECT f.drug_name,
       round(100.0 * SUM(f.taken_doses) / NULLIF(SUM(f.planned_doses), 0), 1)
FROM facts f
GROUP BY f.drug_name
"""


def _con(rows: list[tuple[str, int, int, str]]):
    """A DuckDB connection holding `dose_events` + the `facts` view.

    Each row is `(drug_name, days_ago, hour, status)`. `days_ago=1` is
    yesterday — the newest day the window includes.
    """
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE dose_events(drug_name TEXT, planned_ts TIMESTAMP, status TEXT)"
    )
    for drug, days_ago, hour, status in rows:
        con.execute(
            "INSERT INTO dose_events VALUES "
            f"(?, (current_date - {days_ago}) + INTERVAL {hour} HOUR, ?)",
            [drug, status],
        )
    con.execute(_FACTS_VIEW)
    return con


def _never_actioned(drug: str, days: int, hours: tuple[int, ...]):
    """`days` past days of slots the reminders job created and nobody touched."""
    return [(drug, d, h, "planned") for d in range(1, days + 1) for h in hours]


def test_planned_rows_inflate_the_denominator_but_no_count_explains_them():
    """`taken + skipped + missed` does not add up to `planned_doses`.

    A caregiver reading the breakdown sees 10 doses due, 0 taken, 0 skipped,
    0 missed, and no account of the other 10.
    """
    con = _con(_never_actioned("metformin", 5, (7, 19)))

    planned, taken, skipped, missed = con.execute(
        "SELECT sum(planned_doses), sum(taken_doses), sum(skipped_doses),"
        " sum(missed_doses) FROM facts"
    ).fetchone()

    assert planned == 10, "5 past days x 2 slots should all be in the window"
    assert (taken, skipped, missed) == (0, 0, 0)
    # The gap medallion_pipeline.py's `counts_reconcile` expectation permits by
    # being written `<=` rather than `=`.
    assert taken + skipped + missed < planned


def test_zero_adherence_reports_that_nothing_was_missed():
    """The sharpest form: every dose un-actioned, `most_missed_drug` absent.

    `get_adherence_stats`' COMMENT tells the agent absence means "nothing was
    missed in the window — say that rather than naming a drug", so the agent
    says exactly that to a patient at 0%.
    """
    con = _con(_never_actioned("metformin", 5, (7, 19)))

    assert con.execute(_OVERALL_PCT).fetchone()[0] == 0.0
    assert con.execute(_MOST_MISSED_DRUG).fetchall() == [], (
        "A most_missed_drug row would mean the metric noticed these doses. "
        "It does not — which is F16."
    )


def test_most_missed_drug_names_the_better_adhered_drug():
    """The metric and the percentages in the same result set disagree.

    metformin: 10 past doses, none actioned -> 0% adherence, 0 missed_doses.
    warfarin:  5 past doses, one explicit 'missed' -> 80% adherence, 1 missed.

    `most_missed_drug` ranks on `missed_doses`, so it returns warfarin.
    """
    rows = _never_actioned("metformin", 5, (7, 19))
    rows += [("warfarin", d, 9, "missed" if d == 3 else "taken") for d in range(1, 6)]
    con = _con(rows)

    per_drug = dict(con.execute(_PER_DRUG_PCT).fetchall())
    assert per_drug == {"metformin": 0.0, "warfarin": 80.0}

    worst = con.execute(_MOST_MISSED_DRUG).fetchall()
    assert worst == [("warfarin", 1)], (
        "F16: the drug named as most-missed is the one with the HIGHER "
        f"adherence. Per-drug percentages in the same result set: {per_drug}"
    )


def test_todays_planned_doses_are_correctly_excluded():
    """The window bound is not the problem — this is the control.

    Today's un-actioned doses legitimately are `planned` rather than missed,
    and `get_adherence_stats.sql` already excludes today for exactly that
    reason. So F16 is not "the window is too wide": with only today's slots
    present the aggregate is empty, and every row the other tests see is a
    strictly past slot.
    """
    con = _con([("metformin", 0, 7, "planned"), ("metformin", 0, 19, "planned")])

    assert con.execute("SELECT count(*) FROM facts").fetchone()[0] == 0
    assert con.execute(_OVERALL_PCT).fetchone()[0] is None
