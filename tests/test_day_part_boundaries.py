"""`day_part` must mean the same thing everywhere it is computed.

Three places bucket a timestamp into morning/afternoon/evening/night:

1. `app.db.todays_doses()` — the Today checklist's grouping, Postgres.
2. `app.db._adherence_summary_local()` — the Dashboard's local aggregates,
   Postgres.
3. `pipelines/medallion_pipeline.py`'s `_day_part_expr()` — `gold.adherence_facts`,
   Spark.

(1) and (2) are now generated from one `DAY_PART_BANDS` table by
`_day_part_case()`, so they cannot disagree; the tests below pin that table to
`DATA_CONTRACTS.md` §1 and check the emitted SQL is actually built from it.
They were previously two hand-copied CASE blocks differing only in table
alias, held together by a docstring — the same shape of duplication that let
`current_streak_days` drift (`tests/test_adherence_streak.py`).

(3) is a genuinely separate copy and stays one: a Lakeflow pipeline cannot
import a module that pulls in streamlit and psycopg. It is pinned here by
reading its source, which is weaker than sharing code but strong enough to
fail loudly if someone moves a boundary in one file and not the other.

The §1 boundary table is transcribed literally below rather than derived from
anything, so a change to `DAY_PART_BANDS` has to be made deliberately in two
places — the contract says these numbers are frozen.

No Postgres, no Spark, no workspace: `_day_part_case` and `day_part_for_hour`
are pure, and the Spark check reads text.
"""

import pathlib
import re

import pytest

from app.db import (
    DAY_PART_BANDS,
    DAY_PART_WRAPPING,
    _day_part_case,
    day_part_for_hour,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PIPELINE_SRC = REPO_ROOT / "pipelines" / "medallion_pipeline.py"
DB_SRC = REPO_ROOT / "app" / "db.py"

# DATA_CONTRACTS.md §1, transcribed: hour-of-day -> day_part. night wraps
# midnight, which is why it is written out here as two runs rather than one.
CONTRACT = {
    **{h: "night" for h in range(0, 5)},        # 00:00-04:59
    **{h: "morning" for h in range(5, 12)},     # 05:00-11:59
    **{h: "afternoon" for h in range(12, 17)},  # 12:00-16:59
    **{h: "evening" for h in range(17, 21)},    # 17:00-20:59
    **{h: "night" for h in range(21, 24)},      # 21:00-23:59
}


# ---------------------------------------------------------------------------
# The Python twin matches the contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hour", sorted(CONTRACT))
def test_day_part_for_hour_matches_contract(hour):
    assert day_part_for_hour(hour) == CONTRACT[hour]


def test_day_part_for_hour_rejects_impossible_hours():
    for bad in (-1, 24, 99):
        with pytest.raises(ValueError):
            day_part_for_hour(bad)


def test_night_is_the_wrapping_bucket_not_a_band():
    """night must be the ELSE branch. Written as a band it would need two
    rows (21-24 and 0-5), and the first implementation to forget the second
    row buckets 02:00 as whatever comes last."""
    assert DAY_PART_WRAPPING == "night"
    assert "night" not in {label for _, _, label in DAY_PART_BANDS}
    assert day_part_for_hour(23) == day_part_for_hour(2) == "night"


def test_bands_are_ordered_and_contiguous():
    """Gaps or overlaps would make bucketing depend on branch order."""
    for (_, end, _), (next_start, _, _) in zip(DAY_PART_BANDS, DAY_PART_BANDS[1:]):
        assert end == next_start


# ---------------------------------------------------------------------------
# The generated SQL matches the same table
# ---------------------------------------------------------------------------


def _bands_in_sql(sql: str) -> list[tuple[int, int, str]]:
    """Recover (start, end, label) triples from a generated CASE."""
    pattern = re.compile(
        r">=\s*(\d+)\s*\n\s*AND EXTRACT\(HOUR FROM [\w.]+\)\s*<\s*(\d+)\s*THEN '(\w+)'"
    )
    return [(int(a), int(b), label) for a, b, label in pattern.findall(sql)]


def test_generated_case_carries_the_band_table():
    sql = _day_part_case("de.planned_ts")
    assert _bands_in_sql(sql) == [tuple(b) for b in DAY_PART_BANDS]
    assert sql.rstrip().endswith("END")
    assert f"ELSE '{DAY_PART_WRAPPING}'" in sql


def test_generated_case_uses_the_column_it_was_given():
    sql = _day_part_case("t.planned_ts")
    assert "t.planned_ts" in sql
    assert "de.planned_ts" not in sql


@pytest.mark.parametrize("bad", [
    "de.planned_ts; DROP TABLE dose_events",
    "planned_ts",              # unqualified
    "de.planned_ts::date",     # not a bare column
    "DE.PLANNED_TS",           # bands are lowercase snake_case, §1
    "",
])
def test_day_part_case_rejects_anything_but_a_bare_qualified_column(bad):
    with pytest.raises(ValueError):
        _day_part_case(bad)


def test_db_module_has_no_hand_written_day_part_case_left():
    """The point of the refactor: every day_part CASE in app/db.py comes from
    the generator. A new hand-copied EXTRACT(HOUR ...) block would reintroduce
    exactly the drift this file exists to prevent."""
    src = DB_SRC.read_text()
    # The only EXTRACT(HOUR occurrences may be inside _day_part_case's own
    # f-string template.
    generator = src.split("def _day_part_case", 1)[1].split("\ndef ", 1)[0]
    outside = src.replace(generator, "")
    assert "EXTRACT(HOUR" not in outside
    assert src.count('_day_part_case("') == 2  # todays_doses + dashboard


# ---------------------------------------------------------------------------
# The Spark copy in the Lakeflow pipeline agrees
# ---------------------------------------------------------------------------


def test_spark_day_part_expr_uses_the_same_boundaries():
    """`_day_part_expr()` is read, not imported — pyspark is not a test
    dependency and a pipeline module must not import the app package."""
    src = PIPELINE_SRC.read_text()
    body = src.split("def _day_part_expr", 1)[1].split("\n\n\n", 1)[0]

    pattern = re.compile(
        r"\(hour >= (\d+)\) & \(hour < (\d+)\), F\.lit\(\"(\w+)\"\)"
    )
    spark_bands = [(int(a), int(b), label) for a, b, label in pattern.findall(body)]

    assert spark_bands == [tuple(b) for b in DAY_PART_BANDS]

    otherwise = re.search(r"\.otherwise\(F\.lit\(\"(\w+)\"\)\)", body)
    assert otherwise and otherwise.group(1) == DAY_PART_WRAPPING


def test_spark_valid_day_parts_covers_every_label():
    """VALID_DAY_PARTS drives an expect_or_drop — a label missing from it
    would silently drop every row in that bucket."""
    src = PIPELINE_SRC.read_text()
    declared = re.search(r"VALID_DAY_PARTS = \[(.*?)\]", src, re.S).group(1)
    labels = set(re.findall(r'"(\w+)"', declared))

    assert labels == {label for _, _, label in DAY_PART_BANDS} | {DAY_PART_WRAPPING}
