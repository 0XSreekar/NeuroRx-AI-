"""`current_streak_days` must mean the same thing on both code paths.

The dashboard reads this number from one of two implementations depending on
whether `NEURORX_LOCAL_PG` is set: the `neurorx.app.get_adherence_stats` UC
function (`agent/tools/get_adherence_stats.sql`), or `app/db.py`'s local
Python equivalent. The SQL is the contract — its own COMMENT is what the agent
relays to a patient — so these tests pin the Python side to the SQL's
documented behaviour.

The two rules worth having a test for are the ones a hand-written
walk-backwards loop gets wrong, and did get wrong here before this file
existed: the streak is measured to *yesterday* rather than to the newest day
with rows, and a calendar day with no rows is counted *through* rather than
treated as a break. Both are stated outright in the SQL header ("A day with no
planned doses does not break the streak — it is vacuously adherent") and both
were verified by running that CTE's logic in DuckDB against the same fixtures
used below.

No Postgres and no workspace needed: `_streak_from_daily_totals` is pure.
"""

from datetime import date, timedelta

from app.db import _streak_from_daily_totals

TODAY = date(2026, 8, 4)
YESTERDAY = TODAY - timedelta(days=1)
WINDOW = 30


def _days_back(n: int) -> date:
    """`n` days before yesterday. `_days_back(0)` is yesterday itself."""
    return YESTERDAY - timedelta(days=n)


def _clean(n_days: int, *, ending_days_back: int = 0) -> dict[date, list[int]]:
    """`n_days` consecutive fully-adherent days, newest `ending_days_back`
    days before yesterday."""
    return {
        _days_back(ending_days_back + i): [2, 2]  # [taken, planned]
        for i in range(n_days)
    }


def _streak(day_totals, window_days: int = WINDOW) -> int:
    return _streak_from_daily_totals(day_totals, window_days, today=TODAY)


# --- The straightforward cases both implementations always agreed on --------


def test_no_history_is_not_a_broken_streak_but_zero():
    """An empty window means no data. The caller's `empty` dict is what
    distinguishes "no history" from "streak of 0" for the view; this function
    only promises not to invent a number."""
    assert _streak({}) == 0


def test_counts_consecutive_clean_days_ending_yesterday():
    assert _streak(_clean(5)) == 5


def test_a_missed_dose_yesterday_ends_the_streak_at_zero():
    day_totals = _clean(4, ending_days_back=1)
    day_totals[_days_back(0)] = [1, 2]
    assert _streak(day_totals) == 0


def test_streak_stops_at_the_most_recent_bad_day():
    day_totals = _clean(6)
    day_totals[_days_back(2)] = [0, 2]
    assert _streak(day_totals) == 2


def test_a_skipped_dose_breaks_the_streak_like_a_missed_one():
    """`_adherence_summary_local` counts only `taken` toward `taken_doses`, so
    a skip arrives here as taken < planned. F4 (DATA_CONTRACTS.md §2) is still
    unsigned-off; if skips stop counting against adherence, that changes the
    pipeline formula, not this function."""
    day_totals = _clean(3, ending_days_back=1)
    day_totals[_days_back(0)] = [1, 2]  # one taken, one skipped
    assert _streak(day_totals) == 0


# --- The two rules this file exists for ------------------------------------


def test_a_day_with_no_rows_does_not_break_the_streak():
    """SQL header: a day with no planned doses is vacuously adherent and is
    counted through. `datediff` measures calendar distance, so the gap is
    spanned; a `while day in day_totals` loop stops dead at it instead."""
    day_totals = _clean(5)
    del day_totals[_days_back(2)]
    assert _streak(day_totals) == 5


def test_the_streak_is_measured_to_yesterday_not_to_the_newest_row():
    """Both SQL branches anchor at `date_sub(current_date(), 1)`. Three days of
    stale data still count toward the streak — the Lakebase→Delta sync lag in
    F9 is exactly how this arises in the demo."""
    assert _streak(_clean(3, ending_days_back=3)) == 6


def test_a_gap_between_the_bad_day_and_yesterday_is_counted_through():
    """The two rules interacting: streak = datediff(yesterday, last_bad_day),
    which spans the empty day in between."""
    day_totals = {
        _days_back(0): [2, 2],
        _days_back(1): [2, 2],
        # _days_back(2) has no rows at all
        _days_back(3): [0, 2],  # the bad day
        _days_back(4): [2, 2],
    }
    assert _streak(day_totals) == 3


# --- Window clamping --------------------------------------------------------


def test_a_clean_window_counts_only_days_actually_covered():
    """A history that starts five days ago reports 5, not the full window.
    This is the SQL's `MIN(event_date)` branch, not a `window_days` default."""
    assert _streak(_clean(5), window_days=30) == 5


def test_the_streak_is_capped_by_the_window():
    """"Reported streak == window_days" means *at least* that long, never
    exactly — the days before the window were never read."""
    assert _streak(_clean(30), window_days=7) == 7


def test_a_short_window_still_reports_a_short_streak_honestly():
    assert _streak(_clean(3), window_days=7) == 3
