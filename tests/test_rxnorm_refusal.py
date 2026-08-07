"""Pin `data/ingestion/rxnorm_client.py`'s refusal branches — the module's
stated safety invariant, which currently has no coverage at all.

That module opens with a boxed invariant: it "must NEVER silently substitute a
different drug for the one the caller asked about." That promise is not one
check; it is four separate branches inside `get_rxcui()`, each returning
`match_type="none"` for a different reason (no exact hit and no fuzzy
candidate, *more than one* exact hit, a tie for best fuzzy score, and a top
fuzzy score under `APPROXIMATE_SCORE_THRESHOLD`). Nothing pins any of them.

Why that matters more here than for typical uncovered code: every one of these
branches fails *silently and plausibly* if it regresses. Deleting the `>1`
exact-match branch, or flipping `<` to `<=` on the threshold, does not raise —
it returns a confident-looking `RxNormResult` carrying a real RxCUI for a drug
nobody confirmed. Per ARCHITECTURE.md §2 the extraction flow puts that value on
a human confirmation screen, so a wrong guess here is pre-filled as if it were
resolved. The refusals are the safety property; a returned answer is not.

No network. Every test monkeypatches `rxnorm._get_json`, the single choke point
all four public functions funnel through, so the response shapes under test are
the ones the module docstring recorded from live RxNav — `{"idGroup": {}}` with
no `rxnormId` key for a zero-hit exact search, `{"approximateGroup":
{"inputTerm": null}}` with no `candidate` key for a nonsense fuzzy search, and
`score` as a JSON *string*. Fake responses that quietly "fixed" those shapes
(an empty list instead of a missing key) would test a friendlier API than the
real one.
"""

import pytest

from data.ingestion import rxnorm_client as rxnorm


# --- fake transport ---------------------------------------------------------
#
# Routes on the URL substring each public function builds. Anything unrouted
# raises rather than returning a default: a test that silently received {} for
# a call it did not intend to make would still pass, for the wrong reason.


def install_fake_json(monkeypatch, routes: dict[str, object]):
    """Replace the module's one network choke point. Returns the call log."""
    calls: list[str] = []

    def fake_get_json(url: str):
        calls.append(url)
        for fragment, payload in routes.items():
            if fragment in url:
                return payload
        raise AssertionError(f"unrouted request: {url}")

    monkeypatch.setattr(rxnorm, "_get_json", fake_get_json)
    return calls


def exact_hits(*rxcuis: str) -> dict:
    """Real zero-hit shape is a missing key, not an empty list (docstring 5)."""
    return {"idGroup": {"rxnormId": list(rxcuis)} if rxcuis else {}}


def properties(rxcui: str, name: str, tty: str = "IN") -> dict:
    return {"properties": {"rxcui": rxcui, "name": name, "tty": tty}}


def approximate(*rows: tuple[str, str]) -> dict:
    """rows are (rxcui, score-as-string) — score really is a string (docstring 4).

    Real zero-candidate shape omits `candidate` entirely (docstring 5).
    """
    if not rows:
        return {"approximateGroup": {"inputTerm": None}}
    return {
        "approximateGroup": {
            "candidate": [
                {"rxcui": rxcui, "score": score, "rank": "1", "source": "RXNORM"}
                for rxcui, score in rows
            ]
        }
    }


# --- the happy paths, pinned only so the refusals below mean something -------


def test_single_exact_hit_resolves(monkeypatch):
    install_fake_json(
        monkeypatch,
        {"rxcui.json": exact_hits("6809"), "properties.json": properties("6809", "metformin")},
    )

    result = rxnorm.get_rxcui("metformin")

    assert result.match_type == "exact"
    assert result.rxcui == "6809"
    assert result.matched_name == "metformin", "canonical name comes from properties, not the input"


def test_clean_approximate_winner_resolves(monkeypatch):
    install_fake_json(
        monkeypatch,
        {
            "rxcui.json": exact_hits(),
            "approximateTerm.json": approximate(("6809", "8.33"), ("29046", "5.10")),
            "properties.json": properties("6809", "metformin"),
        },
    )

    result = rxnorm.get_rxcui("metforminn")

    assert result.match_type == "approximate"
    assert result.rxcui == "6809"
    assert result.score == pytest.approx(8.33), "score is float(), never compared as text"


# --- the four refusal branches ----------------------------------------------


def test_multiple_exact_hits_refuse_rather_than_take_the_first(monkeypatch):
    """Two exact-tier RxCUIs for one name is ambiguity, not a tie to break.

    The specific regression this guards: returning `exact_ids[0]`. That reads
    as reasonable and would pass any test that only asserted "resolves 6809",
    because RxNav lists the ingredient concept first for common drugs. It is
    wrong for exactly the inputs where it matters — the ambiguous ones.
    """
    calls = install_fake_json(monkeypatch, {"rxcui.json": exact_hits("6809", "235743")})

    result = rxnorm.get_rxcui("metformin")

    assert result.match_type == "none"
    assert result.rxcui is None
    assert not any("properties.json" in url for url in calls), (
        "an ambiguous name must not be resolved far enough to look up a name"
    )


def test_no_exact_and_no_candidates_refuses(monkeypatch):
    install_fake_json(
        monkeypatch,
        {"rxcui.json": exact_hits(), "approximateTerm.json": approximate()},
    )

    result = rxnorm.get_rxcui("qqzzxx")

    assert result.match_type == "none"
    assert result.rxcui is None
    assert result.score is None


def test_tie_for_best_fuzzy_score_refuses(monkeypatch):
    """Two *distinct* drugs at the same top score cannot be chosen between.

    Sorting is stable, so whichever tied row the API happened to list first
    would win silently if this branch were dropped.
    """
    install_fake_json(
        monkeypatch,
        {
            "rxcui.json": exact_hits(),
            "approximateTerm.json": approximate(("6809", "8.33"), ("29046", "8.33")),
        },
    )

    result = rxnorm.get_rxcui("metformi")

    assert result.match_type == "none"
    assert result.rxcui is None
    assert result.score == pytest.approx(8.33), (
        "the refusal still reports what it saw — a caller logging this "
        "should be able to tell a tie from no candidates at all"
    )


def test_top_score_under_threshold_refuses(monkeypatch):
    install_fake_json(
        monkeypatch,
        {
            "rxcui.json": exact_hits(),
            "approximateTerm.json": approximate(("6809", "3.90")),
        },
    )

    result = rxnorm.get_rxcui("mtfn")

    assert result.match_type == "none"
    assert result.rxcui is None


def test_threshold_is_inclusive_at_the_floor(monkeypatch):
    """A score exactly at APPROXIMATE_SCORE_THRESHOLD is accepted (`<` refuses
    strictly below). Pinned because `<` vs `<=` is a one-character edit with no
    failing symptom — it silently moves the boundary of the only numeric
    safety gate in the module.
    """
    floor = str(rxnorm.APPROXIMATE_SCORE_THRESHOLD)
    install_fake_json(
        monkeypatch,
        {
            "rxcui.json": exact_hits(),
            "approximateTerm.json": approximate(("6809", floor)),
            "properties.json": properties("6809", "metformin"),
        },
    )

    result = rxnorm.get_rxcui("mtfn")

    assert result.match_type == "approximate"
    assert result.score == pytest.approx(rxnorm.APPROXIMATE_SCORE_THRESHOLD)


def test_blank_name_refuses_without_calling_the_api(monkeypatch):
    calls = install_fake_json(monkeypatch, {})

    result = rxnorm.get_rxcui("   ")

    assert result.match_type == "none"
    assert calls == [], "a blank name is answerable locally"


def test_input_name_is_stripped_and_echoed(monkeypatch):
    install_fake_json(
        monkeypatch,
        {"rxcui.json": exact_hits("6809"), "properties.json": properties("6809", "metformin")},
    )

    assert rxnorm.get_rxcui("  metformin  ").input_name == "metformin"


# --- dedupe, and the limitation it does not remove --------------------------


def test_multi_vocabulary_rows_collapse_to_one_candidate(monkeypatch):
    """One drug indexed by several source vocabularies is one candidate.

    Docstring point 3: the raw array is not one row per distinct drug — the
    same rxcui repeats once per vocabulary (RXNORM, VANDF, ATC, DRUGBANK...),
    all sharing its score.
    """
    install_fake_json(
        monkeypatch,
        {"approximateTerm.json": approximate(("6809", "8.33"), ("6809", "8.33"), ("6809", "8.33"))},
    )

    candidates = rxnorm.search_approximate("metforminn")

    assert [c["rxcui"] for c in candidates] == ["6809"]


def test_dedupe_keeps_the_highest_score_seen(monkeypatch):
    """The best row must win regardless of where it sits in the response.

    Row order is deliberately descending: with the higher score listed *last*,
    a "keep whatever came last" dedupe returns the same answer as "keep the
    max" and the test proves nothing. Written the other way round, only a real
    max comparison passes.
    """
    install_fake_json(
        monkeypatch,
        {"approximateTerm.json": approximate(("6809", "8.33"), ("6809", "5.00"))},
    )

    assert rxnorm.search_approximate("metforminn")[0]["score"] == pytest.approx(8.33)


def test_candidates_are_sorted_best_first(monkeypatch):
    install_fake_json(
        monkeypatch,
        {"approximateTerm.json": approximate(("29046", "5.10"), ("6809", "8.33"))},
    )

    assert [c["rxcui"] for c in rxnorm.search_approximate("x")] == ["6809", "29046"]


def test_tie_detection_cannot_see_alternates_the_api_never_returned(monkeypatch):
    """Documented limitation, pinned as behaviour rather than left as a comment.

    Dedupe runs on rows the API has *already* truncated to `maxEntries`. So the
    module docstring's point 3 is only half-delivered: dedupe does stop N copies
    of one hit from filling the candidate list, but it cannot recover an
    alternate that never made it into those N rows. When every returned row
    collapses to one rxcui, `get_rxcui` sees a single candidate and the tie
    branch — one of the four refusals above — is unreachable by construction,
    not because the input was unambiguous.

    This is not asserted to be correct; it is asserted to be *current*, so that
    raising `DEFAULT_MAX_APPROXIMATE_ENTRIES` or moving dedupe API-side has a
    test that notices. Verifying which alternates real RxNav drops at a given
    maxEntries needs live calls and is out of scope for this network-free file.
    """
    install_fake_json(
        monkeypatch,
        {
            "rxcui.json": exact_hits(),
            "approximateTerm.json": approximate(*[("6809", "8.33")] * 4),
            "properties.json": properties("6809", "metformin"),
        },
    )

    result = rxnorm.get_rxcui("metforminn")

    assert result.match_type == "approximate", (
        "single surviving candidate — the tie branch never runs, even though "
        "the four raw rows are all the API was willing to return"
    )
    assert result.rxcui == "6809"


# --- the choke point itself -------------------------------------------------


def test_repeat_url_is_served_from_cache(monkeypatch):
    """Task 1.2 requirement #2, and NLM's stated preference for caching.

    `_get_json` is the real function here — this is the one test that exercises
    it — so `requests.get` is what gets replaced.
    """
    monkeypatch.setattr(rxnorm, "_cache", {})
    monkeypatch.setattr(rxnorm, "MIN_REQUEST_INTERVAL_SECONDS", 0)

    hits = []

    class FakeResponse:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"ok": True}

    def fake_get(url, timeout=None):
        hits.append(url)
        return FakeResponse()

    monkeypatch.setattr(rxnorm.requests, "get", fake_get)

    first = rxnorm._get_json("https://example.invalid/a")
    second = rxnorm._get_json("https://example.invalid/a")

    assert first == second == {"ok": True}
    assert len(hits) == 1, "second call must not reach the network"
