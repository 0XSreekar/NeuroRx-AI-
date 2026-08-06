"""A stat card's sublabel is a claim about the number above it.

`app/views/dashboard.py`'s four header cards each render a `delta` line that
tells the patient how the figure was computed. Those lines are the only
explanation of the metric anyone sees, and three of the four described a
different rule than `neurorx.app.get_adherence_stats` actually implements:

  - "Share of scheduled doses taken **on time**" — nothing records on-time-ness.
    `adherence_pct` is `taken_doses / planned_doses`, and Today's "I took it
    late" button writes `status='taken'` with the real late timestamp, so a
    late dose counts in full.
  - "Consecutive days ending yesterday at **≥90%**" — `streak_calc` breaks on
    `taken_doses < planned_doses`, any shortfall at all. A patient at 95% for
    one day sees the streak reset to 0, and the ≥90% label made the correct
    number look like a bug.
  - "**Lowest adherence** this month" — `worst_drug` ranks by
    `SUM(missed_doses) DESC`, a count, not a rate. `app/genie_assets.sql`'s own
    column comment says this question should rank by `missed_doses` and *not*
    by "the inverse of adherence_pct".

This is the same shape as the two divergences this project has already caught
(`tests/test_adherence_streak.py`, `tests/test_day_part_boundaries.py`): text
asserting a property that only a docstring, not the code, was holding up. The
difference is that those two were internal, and this one was on screen.

The tests pin each label against the **tool's own SQL and the frozen
contract**, not against `dashboard.py` — so changing the label alone cannot
make them pass, and changing the rule in SQL without revisiting the label
fails here rather than silently mislabelling the dashboard.

Read as text on purpose: importing `app.views.dashboard` pulls in streamlit and
plotly, and reading source is the pattern `test_day_part_boundaries.py` already
uses to pin a copy it cannot import. No Postgres, no Spark, no workspace.
"""

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DASHBOARD_SRC = REPO_ROOT / "app" / "views" / "dashboard.py"
TODAY_SRC = REPO_ROOT / "app" / "views" / "today.py"
STATS_SQL = REPO_ROOT / "agent" / "tools" / "get_adherence_stats.sql"
GENIE_SQL = REPO_ROOT / "app" / "genie_assets.sql"
CONTRACTS = REPO_ROOT / "DATA_CONTRACTS.md"


# ---------------------------------------------------------------------------
# Pull the four (eyebrow, delta) pairs out of _render_header_stats
# ---------------------------------------------------------------------------


def _card_labels() -> dict[str, str]:
    """Map each header card's eyebrow to its delta line.

    Sliced to the `cards = [...]` literal specifically, so the module docstring
    and this function's own prose (which quote the wrong labels verbatim, to
    record them) can never be mistaken for a live label.
    """
    src = DASHBOARD_SRC.read_text()

    start = src.index("def _render_header_stats")
    body = src[start:]
    literal_start = body.index("cards = [")
    literal_end = body.index("\n    ]", literal_start)
    literal = body[literal_start:literal_end]

    eyebrows = re.findall(r'theme\.stat_card\(\s*\n\s*"([^"]+)"', literal)
    deltas = re.findall(r'delta="([^"]*)"', literal)

    assert len(eyebrows) == 4, f"expected 4 stat cards, found {len(eyebrows)}"
    assert len(deltas) == 4, f"expected 4 delta lines, found {len(deltas)}"
    return dict(zip(eyebrows, deltas))


CARDS = _card_labels()

OVERALL = "OVERALL ADHERENCE · 30D"
STREAK = "CURRENT STREAK"
WORST_DRUG = "MOST-MISSED DRUG"
WORST_PART = "MOST-MISSED TIME"


def test_the_four_expected_cards_are_present():
    """If a card is renamed or added, every assertion below needs re-reading."""
    assert set(CARDS) == {OVERALL, STREAK, WORST_DRUG, WORST_PART}


# ---------------------------------------------------------------------------
# 1. Overall adherence: taken / planned, and a late dose counts
# ---------------------------------------------------------------------------


def test_contract_defines_adherence_as_taken_over_planned():
    """The premise. DATA_CONTRACTS.md §5.3 is frozen; if this line moves, the
    label below is describing a formula that no longer exists."""
    assert "taken_doses / NULLIF(planned_doses, 0) * 100" in CONTRACTS.read_text()


def test_a_late_dose_is_recorded_as_taken():
    """The reason "on time" was wrong: Today's late button writes `taken`, so
    lateness is never represented anywhere the percentage could see it."""
    src = TODAY_SRC.read_text()
    # Anchor on `st.button(`, not on the bare label: today.py's own module
    # docstring quotes "I took it late" several hundred characters earlier, and
    # matching that instead silently reads the wrong block.
    late_button = src.index('st.button("I took it late"')
    # The mark_dose call this button makes, within the same handler block.
    handler = src[late_button : late_button + 600]
    assert 'status="taken"' in handler


def test_overall_adherence_label_does_not_claim_on_time():
    label = CARDS[OVERALL].lower()
    assert "on time" not in label, (
        "adherence_pct cannot distinguish an on-time dose from a late one — "
        "app/views/today.py's late path writes status='taken' either way"
    )
    assert "late" in label, "the label should say lateness does not reduce the figure"


# ---------------------------------------------------------------------------
# 2. Streak: every planned dose, not a percentage threshold
# ---------------------------------------------------------------------------


def test_streak_sql_breaks_on_any_shortfall():
    """`streak_calc` has no tolerance band — strictly `taken < planned`."""
    sql = STATS_SQL.read_text()
    assert "d.taken_doses < d.planned_doses" in sql
    assert "every planned dose taken" in sql, "the tool COMMENT states the same rule"


def test_streak_sql_has_no_percentage_threshold():
    """Guards the other direction: if a tolerance is ever introduced in SQL,
    this fails and the label has to be revisited deliberately."""
    sql = STATS_SQL.read_text()
    streak_cte = sql[sql.index("streak_calc AS ("):sql.index("worst_drug AS (")]
    assert not re.search(r"0\.9|90\s*%|adherence_pct", streak_cte), (
        "streak_calc gained a percentage threshold — update the dashboard's "
        "CURRENT STREAK label to match before relaxing this test"
    )


def test_streak_label_does_not_claim_a_percentage_threshold():
    label = CARDS[STREAK]
    assert "90" not in label and "%" not in label, (
        "the streak breaks on any shortfall; a ≥90% label makes a correct "
        "reset-to-zero look like a defect in the number"
    )
    assert "every dose" in label.lower()
    assert "yesterday" in label.lower(), "the window ends yesterday, not today"


# ---------------------------------------------------------------------------
# 3. Most-missed drug: a count, not a rate
# ---------------------------------------------------------------------------


def test_worst_drug_sql_ranks_by_missed_count():
    sql = STATS_SQL.read_text()
    assert "ORDER BY SUM(f.missed_doses) DESC, f.drug_name ASC" in sql


def test_genie_comment_says_not_to_rank_by_adherence_pct():
    """Not this file's opinion — the shipped column comment says it."""
    assert "not skipped_doses or the inverse of adherence_pct" in GENIE_SQL.read_text()


def test_worst_drug_label_does_not_claim_lowest_adherence():
    label = CARDS[WORST_DRUG].lower()
    assert "lowest adherence" not in label, (
        "worst_drug ranks by SUM(missed_doses) DESC — a twice-daily drug can "
        "top it at a better percentage than a once-daily one"
    )
    assert "missed" in label


# ---------------------------------------------------------------------------
# 4. Most-missed day part: already correct, kept correct
# ---------------------------------------------------------------------------


def test_worst_daypart_sql_ranks_by_missed_count():
    sql = STATS_SQL.read_text()
    assert "ORDER BY SUM(f.missed_doses) DESC, f.day_part ASC" in sql


def test_worst_daypart_label_still_describes_a_missed_dose_count():
    label = CARDS[WORST_PART].lower()
    assert "most missed doses" in label


# ---------------------------------------------------------------------------
# No label may describe a rule none of the metrics implement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("eyebrow", [OVERALL, STREAK, WORST_DRUG, WORST_PART])
def test_no_label_reintroduces_a_retired_phrase(eyebrow):
    """The three exact phrases that were wrong, blocked everywhere at once so a
    future card cannot pick one back up."""
    label = CARDS[eyebrow].lower()
    for phrase in ("on time", "≥90", ">=90", "lowest adherence"):
        assert phrase not in label, f"{eyebrow!r} reintroduced {phrase!r}"
