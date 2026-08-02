"""Pin `agent/extraction.py`'s frequency table — the deterministic half of the
extraction flow, which currently has no coverage under pytest.

`normalize()` is where a printed sig ("1 tab po bid pc") becomes an actual
schedule (times_per_day=2, dose_times=08:00/20:00). Per the module docstring
and CLAUDE.md §1, this interpretation lives in code precisely so it is
auditable and testable rather than being the model's guess — but the only
checks on it lived in `extraction.py`'s `__main__` block, which nothing runs:
CI runs `pipelines/chunking.py` and `compileall`, not this file, and the rest
of that block needs live RxNav (`resolve()`) so it cannot simply be wired into
CI as-is.

These tests cover only the network-free stages. `FREQUENCY_RULES` ordering is
load-bearing and first-match-wins, as that list's own comment argues: "once
daily at night" must reach `qhs` (21:00) and not the generic `qd` (08:00), and
"twice daily" must reach `bid` and never fall through to the bare `\\bdaily\\b`
alternative in `qd`. Nothing in the ordering itself makes a regression loud —
reordering the list produces a wrong schedule silently, with no error — so the
property is pinned here rather than left as a comment.

No network, no Postgres, no Databricks: importing `agent.extraction` pulls in
`app.config` and `data.ingestion.rxnorm_client`, but neither performs I/O at
import time, and nothing below calls `extract()` or `resolve()`.
"""

import pytest

from agent.extraction import (
    FREQUENCY_RULES,
    _match_frequency,
    _match_modifiers,
    normalize,
)

# (frequency_text, expected rule key). Every schedulable rule appears at least
# once, in both abbreviated and spelled-out form where the pattern allows both.
# The diurnally-qualified "once daily ..." cases are the ordering regression:
# each shares the substring "once daily" with the generic qd rule and must be
# claimed by the more specific rule that precedes it.
ORDERING_CASES = [
    ("twice daily", "bid"),
    ("three times daily", "tid"),
    ("four times daily", "qid"),
    ("twice a day", "bid"),
    ("three times per day", "tid"),
    ("BID", "bid"),
    ("TID", "tid"),
    ("QID", "qid"),
    ("b.i.d.", "bid"),
    ("t.i.d.", "tid"),
    ("q.i.d.", "qid"),
    ("once daily at night", "qhs"),
    ("once daily in the morning", "qam"),
    ("once daily in the evening", "qpm"),
    ("once daily", "qd"),
    ("qd", "qd"),
    ("every 12 hours", "q12h"),
    ("every 8 hours", "q8h"),
    ("every 6 hours", "q6h"),
    ("every 4 hours", "q4h"),
    ("at bedtime", "qhs"),
    ("before bed", "qhs"),
    ("nightly", "qhs"),
    ("every morning", "qam"),
    ("every evening", "qpm"),
    ("every other day", "qod"),
    ("once weekly", "weekly"),
    ("as needed", "prn"),
    ("PRN", "prn"),
]


@pytest.mark.parametrize("frequency_text,expected_key", ORDERING_CASES)
def test_frequency_pattern_matches_expected_rule(frequency_text, expected_key):
    rule = _match_frequency(frequency_text)
    assert rule is not None, f"{frequency_text!r} matched no rule"
    assert rule.key == expected_key, (
        f"{frequency_text!r} matched rule {rule.key!r}, expected {expected_key!r} — "
        "the specific-before-generic ordering of FREQUENCY_RULES may be broken"
    )


def test_rule_keys_are_unique():
    keys = [rule.key for rule in FREQUENCY_RULES]
    assert len(keys) == len(set(keys)), f"duplicate rule keys: {keys}"


def test_every_registered_rule_is_reachable():
    """A rule shadowed entirely by an earlier one is dead code — and, worse,
    silently produces the earlier rule's schedule. Every key must be reached by
    at least one case above.
    """
    covered = {expected for _, expected in ORDERING_CASES}
    registered = {rule.key for rule in FREQUENCY_RULES}
    assert registered - covered == set(), (
        f"rules with no ordering case pinning them: {sorted(registered - covered)}"
    )


@pytest.mark.parametrize(
    "frequency_text,times_per_day,dose_times",
    [
        ("once daily", 1, ["08:00:00"]),
        ("once daily at night", 1, ["21:00:00"]),
        ("bid", 2, ["08:00:00", "20:00:00"]),
        ("tid", 3, ["08:00:00", "14:00:00", "20:00:00"]),
        ("every 8 hours", 3, ["06:00:00", "14:00:00", "22:00:00"]),
    ],
)
def test_schedulable_frequency_produces_expected_schedule(
    frequency_text, times_per_day, dose_times
):
    [drug] = normalize([_drug(frequency_text=frequency_text)])
    assert drug["times_per_day"] == times_per_day
    assert drug["dose_times"] == dose_times
    assert drug["needs_review"] is False, drug["review_reasons"]
    assert len(drug["dose_times"]) == drug["times_per_day"]


@pytest.mark.parametrize("frequency_text", ["every other day", "once weekly", "as needed"])
def test_recognized_but_unschedulable_frequency_needs_review(frequency_text):
    """qod/weekly/prn are understood, but the daily times_per_day/dose_times
    model (DATA_CONTRACTS.md §6.2) has no slot for them. They must flag for
    review with the rule's own specific reason — distinct from the
    "unrecognized text" reason — and must not invent a schedule.
    """
    [drug] = normalize([_drug(frequency_text=frequency_text)])
    assert drug["needs_review"] is True
    assert drug["times_per_day"] is None
    assert drug["dose_times"] is None
    [reason] = drug["review_reasons"]
    assert "did not match any known pattern" not in reason
    assert reason == _match_frequency(frequency_text).unschedulable_reason


def test_unrecognized_frequency_flags_review_without_fabricating_a_schedule():
    [drug] = normalize([_drug(frequency_text="purple elephant schedule")])
    assert drug["needs_review"] is True
    assert drug["times_per_day"] is None
    assert drug["dose_times"] is None
    assert "did not match any known pattern" in drug["review_reasons"][0]


def test_missing_frequency_flags_review():
    [drug] = normalize([_drug(frequency_text="")])
    assert drug["needs_review"] is True
    assert drug["times_per_day"] is None
    assert "No frequency information" in drug["review_reasons"][0]


@pytest.mark.parametrize(
    "frequency_text,expected_note",
    [
        ("1 tab po bid pc", "after meals"),
        ("bid ac", "before meals"),
        ("once daily with food", "with food"),
        ("once daily on an empty stomach", "on an empty stomach"),
    ],
)
def test_modifiers_land_in_timing_notes(frequency_text, expected_note):
    [drug] = normalize([_drug(frequency_text=frequency_text)])
    assert expected_note in drug["timing_notes"]


def test_modifier_does_not_change_the_schedule():
    """Modifiers are independent of frequency — a drug can be bid AND pc — so
    they must append to timing_notes without touching times_per_day/dose_times.
    """
    [plain] = normalize([_drug(frequency_text="bid")])
    [with_modifier] = normalize([_drug(frequency_text="bid pc")])
    assert with_modifier["times_per_day"] == plain["times_per_day"]
    assert with_modifier["dose_times"] == plain["dose_times"]
    assert with_modifier["needs_review"] is False


def test_existing_timing_notes_are_preserved_not_overwritten():
    [drug] = normalize(
        [_drug(frequency_text="bid pc", timing_notes="patient prefers late breakfast")]
    )
    assert "patient prefers late breakfast" in drug["timing_notes"]
    assert "after meals" in drug["timing_notes"]


def test_frequency_text_is_preserved_verbatim_apart_from_whitespace():
    """The raw sig is what the human sees on the confirmation card, so
    normalize() must not rewrite it into its own vocabulary.
    """
    [drug] = normalize([_drug(frequency_text="  1 tab po bid pc  ")])
    assert drug["frequency_text"] == "1 tab po bid pc"


def test_route_only_tokens_are_not_treated_as_modifiers():
    """"po" (by mouth) carries no timing constraint; echoing it into
    timing_notes would be noise. It stays in the raw frequency_text only.
    """
    assert _match_modifiers("1 tab po qd") == []


def test_normalize_preserves_other_fields_and_row_order():
    drugs = [
        _drug(drug_name="lisinopril", frequency_text="once daily"),
        _drug(drug_name="metformin", strength="500 mg", frequency_text="bid"),
    ]
    normalized = normalize(drugs)
    assert [d["drug_name"] for d in normalized] == ["lisinopril", "metformin"]
    assert normalized[1]["strength"] == "500 mg"


def _drug(drug_name="testdrug", strength="10 mg", frequency_text="", timing_notes=""):
    return {
        "drug_name": drug_name,
        "strength": strength,
        "frequency_text": frequency_text,
        "timing_notes": timing_notes,
    }
