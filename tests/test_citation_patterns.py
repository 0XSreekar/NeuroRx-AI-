"""The two citation regexes have one definition each, and both are load-bearing.

Citations are not decoration in this project — CLAUDE.md §1's spine is that a
clinical fact reaching the user without a deterministic lookup *and a citation*
is a defect. Everything that decides "is this cited?" therefore has to agree on
what a citation looks like. Four files had opinions; two of them were wrong.

These tests pin the two properties that were actually violated, not the regexes'
syntax:

1. `CHUNK_ID_PATTERN` captures, so `.findall()` yields a bare chunk_id directly
   comparable to a tool result's `chunk_id` field. The no-capture-group variant
   (`agent/07_smoke_tests.py`, `evals/02_run_evaluation.py` before it was fixed)
   returns `[bracketed]` strings, which never equal a bare chunk_id — so every
   correctly-cited response scores as *fabricated*. `evals/02_run_evaluation.py`'s
   own comment records hitting exactly that.

2. `INTERACTION_CITATION_PATTERN` matches the dual-attested source list that
   `agent/tools/check_interactions.sql` actually emits, and rejects a source this
   project never cites. The variant it replaced, `\\[source:\\s*\\w+\\]`, was wrong
   in both directions at once: `\\w+` cannot span the ", " in
   `[source: ddinter, fda_label]` (a false negative on a *correct* citation),
   while happily matching `[source: made_up]` (a false positive on an invented
   one).

Both failure modes are silent — they produce a wrong verdict, not an error — so
nothing but an assertion catches them.
"""

import pathlib
import re

import pytest

from agent.guardrail import INTERACTION_CITATION_PATTERN
from app.agent_client import CHUNK_ID_PATTERN

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Shape per DATA_CONTRACTS.md §4.2's concat_ws formula: doc uuid, section, 4-digit
# ordinal. This is the string a `search_drug_labels` result carries in its
# `chunk_id` field, and the string the agent is expected to cite in brackets.
CHUNK_ID = "12345678-1234-1234-1234-123456789012:warnings:0001"


# ---------------------------------------------------------------------------
# CHUNK_ID_PATTERN — the capture group is the whole point
# ---------------------------------------------------------------------------

def test_findall_yields_bare_chunk_ids_not_bracketed_matches():
    """The exact comparison every groundedness check makes must succeed."""
    response = f"Take it with food [{CHUNK_ID}]."

    found = CHUNK_ID_PATTERN.findall(response)

    assert found == [CHUNK_ID]
    # The bug direction, pinned explicitly: a bracketed match would compare
    # unequal to the tool result's chunk_id and read as a fabricated citation.
    assert f"[{CHUNK_ID}]" not in found


def test_cited_chunk_ids_intersect_a_tool_result_chunk_id():
    """The set operation `guardrail.check` and the eval scorer both perform."""
    tool_returned = {CHUNK_ID}
    cited = set(CHUNK_ID_PATTERN.findall(f"... [{CHUNK_ID}]"))

    assert cited & tool_returned == {CHUNK_ID}
    assert not (cited - tool_returned), "a real citation must not read as fabricated"


def test_multiple_citations_are_each_captured():
    other = "87654321-4321-4321-4321-210987654321:dosage_and_administration:0012"
    response = f"One [{CHUNK_ID}]. Two [{other}]."

    assert CHUNK_ID_PATTERN.findall(response) == [CHUNK_ID, other]


@pytest.mark.parametrize(
    "text",
    [
        f"{CHUNK_ID}",                       # unbracketed
        "[not-a-uuid:warnings:0001]",        # malformed uuid
        f"[{CHUNK_ID[:-1]}]",                # 3-digit ordinal
        "[12345678-1234-1234-1234-123456789012:Warnings:0001]",  # uppercase section
    ],
)
def test_non_citations_do_not_match(text):
    assert CHUNK_ID_PATTERN.findall(text) == []


# ---------------------------------------------------------------------------
# INTERACTION_CITATION_PATTERN — wrong in both directions before this
# ---------------------------------------------------------------------------

def test_matches_the_dual_attested_source_list():
    """`array_join(sources, ', ')` really does emit two sources in one tag.

    The false negative that matters most: this is a *correctly* cited
    interaction warning, and the old `\\w+` form rejected it.
    """
    assert INTERACTION_CITATION_PATTERN.search(
        "Warfarin and ibuprofen interact [source: ddinter, fda_label]."
    )


def test_matches_the_single_source_form():
    assert INTERACTION_CITATION_PATTERN.search("... [source: ddinter]")


def test_is_case_insensitive():
    assert INTERACTION_CITATION_PATTERN.search("... [source: DDInter]")


def test_rejects_a_source_this_project_never_cites():
    """The false positive: an invented attribution must not read as grounded."""
    assert not INTERACTION_CITATION_PATTERN.search("... [source: made_up]")
    assert not INTERACTION_CITATION_PATTERN.search("... [source: my_training_data]")


def test_the_two_patterns_do_not_match_each_other():
    """Layer 1 accepts either form; neither may stand in for the other."""
    assert not CHUNK_ID_PATTERN.findall("[source: ddinter]")
    assert not INTERACTION_CITATION_PATTERN.search(f"[{CHUNK_ID}]")


# ---------------------------------------------------------------------------
# Drift guard — the copies are what caused both bugs
# ---------------------------------------------------------------------------

# Any re.compile whose body looks like the chunk_id citation shape, in any form.
_REDERIVED_CHUNK_ID_RE = re.compile(r"re\.compile\(\s*r?[\"']\\\[\(?\[0-9a-f-\]\{36\}")

# app/agent_client.py owns the canonical definition. agent/06_deploy_agent.py
# keeps a private copy on purpose: it never imports app.config, and importing the
# canonical module would drag that file's import-time validation of nine required
# env vars into a deploy notebook that needs no Lakebase credentials to run. It
# only counts and prints matches, so the no-capture-group form is not a defect
# there — but if it ever compares a match to a chunk_id, it must import instead.
_ALLOWED_TO_DEFINE = {
    pathlib.Path("app/agent_client.py"),
    pathlib.Path("agent/06_deploy_agent.py"),
}


def test_no_module_rederives_the_chunk_id_pattern():
    offenders = []
    for directory in ("app", "agent", "evals", "pipelines"):
        for path in (REPO_ROOT / directory).rglob("*.py"):
            relative = path.relative_to(REPO_ROOT)
            if relative in _ALLOWED_TO_DEFINE:
                continue
            if _REDERIVED_CHUNK_ID_RE.search(path.read_text(encoding="utf-8")):
                offenders.append(str(relative))

    assert not offenders, (
        "these files re-derive the chunk_id citation regex instead of importing "
        f"CHUNK_ID_PATTERN from app.agent_client: {sorted(offenders)}. A private "
        "copy without the capture group scores every correct citation as "
        "fabricated — see this module's docstring."
    )
