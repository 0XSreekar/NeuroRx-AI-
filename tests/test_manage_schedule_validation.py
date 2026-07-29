"""update_timing payload validation — the checks that used to fall through to Postgres.

`validate_payload` promises "an error message string, or None". Three
update_timing payloads broke that promise: a non-list `dose_times` raised
TypeError out of it, and `times_per_day: 0` or a lone half of the
dose_times/times_per_day pair returned None and failed later against the
schedules_frequency_match / schedules_times_positive CHECK constraints — as a
Postgres error the model cannot act on.

manage_schedule.py ends by calling `spark.sql(...)` at import time, so the
module is loaded here by AST-filtering to its function and constant
definitions. That is also what keeps this test workspace-free.
"""

import ast
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "agent" / "tools" / "manage_schedule.py"


def load_pure_definitions():
    tree = ast.parse(MODULE_PATH.read_text())
    keep = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.Assign, ast.Import, ast.ImportFrom))
    ]
    namespace: dict = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(MODULE_PATH), "exec"), namespace)
    return namespace


@pytest.fixture(scope="module")
def validate():
    return load_pure_definitions()["validate_payload"]


def test_non_list_dose_times_returns_an_error_not_a_crash(validate):
    """Used to raise TypeError: object of type 'int' has no len()."""
    error = validate("update_timing", {"schedule_id": "s", "dose_times": 3, "times_per_day": 3})
    assert isinstance(error, str)
    assert "dose_times" in error


def test_zero_times_per_day_is_rejected_here(validate):
    """schedules_times_positive would otherwise reject it, DB-side."""
    error = validate("update_timing", {"schedule_id": "s", "dose_times": [], "times_per_day": 0})
    assert isinstance(error, str)
    assert "positive" in error


@pytest.mark.parametrize(
    "payload",
    [
        {"schedule_id": "s", "dose_times": ["08:00", "20:00"]},
        {"schedule_id": "s", "times_per_day": 2},
    ],
    ids=["dose_times_alone", "times_per_day_alone"],
)
def test_half_the_pair_is_rejected_with_an_actionable_message(validate, payload):
    error = validate("update_timing", payload)
    assert isinstance(error, str)
    assert "together" in error, "the message must tell the model what to do differently"


def test_mismatched_pair_still_rejected(validate):
    error = validate("update_timing", {
        "schedule_id": "s", "dose_times": ["08:00"], "times_per_day": 2,
    })
    assert isinstance(error, str)


def test_consistent_pair_passes(validate):
    assert validate("update_timing", {
        "schedule_id": "s", "dose_times": ["08:00", "20:00"], "times_per_day": 2,
    }) is None


def test_timing_notes_only_update_still_allowed(validate):
    """Neither half present is a legitimate partial update, not an error."""
    assert validate("update_timing", {
        "schedule_id": "s", "timing_notes": "with food",
    }) is None


def test_missing_schedule_id_unchanged(validate):
    assert "schedule_id" in validate("update_timing", {"dose_times": ["08:00"], "times_per_day": 1})


def test_both_validator_copies_stay_in_sync():
    """The module-level validator is duplicated inside the CREATE FUNCTION body.

    They are separate text, so a fix applied to one silently diverges from the
    deployed UC function. This counts the new pairing rule in both.
    """
    source = MODULE_PATH.read_text()
    assert source.count("update_timing must send dose_times and times_per_day together") == 2, (
        "validate_payload exists twice (module-level and embedded SQL body) — patch both"
    )
