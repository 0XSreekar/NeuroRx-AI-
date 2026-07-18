"""RxNorm client — maps a drug name string to a canonical RxCUI via the free
NLM RxNav REST API (rxnav.nlm.nih.gov).

Used by `data/ingestion/02_rxnorm_ingest.py` today, and intended for reuse by
the agent's prescription-extraction flow later (Phase 2) — the module has no
Databricks or Spark dependency so it works in both contexts.

┌─────────────────────────────────────────────────────────────────────────┐
│ SAFETY INVARIANT — READ BEFORE CALLING get_rxcui()                      │
│                                                                           │
│ This module must NEVER silently substitute a different drug for the one │
│ the caller asked about. Any input whose correct RxCUI cannot be         │
│ determined with confidence — no match, multiple equally-plausible       │
│ matches, or a low-confidence fuzzy match — returns match_type="none"    │
│ and rxcui=None. It does not guess. The agent's prescription             │
│ confirmation screen (ARCHITECTURE.md §2, "human confirmation screen")   │
│ depends on this: a wrong silent guess here becomes a wrong drug on a    │
│ patient's schedule with no human ever having been asked to confirm it.  │
│ The caller — not this module — decides what happens on a `none` result. │
└─────────────────────────────────────────────────────────────────────────┘

Verified live against the RxNav API before this module was written (not
assumed from memory or documentation alone):

1. Endpoint paths and the `search` parameter's exact semantics, confirmed
   against `rxcui.json?name=metformin` under all three values:
     - search=0 → exact match only.       metformin -> ['6809']
     - search=1 → normalized match only.  metformin -> ['235743', '6809']
     - search=2 → exact match, falling back to normalized only if the exact
       search found nothing.              metformin -> ['6809']
   search=2 (used here for the "exact" tier) is NOT the union of exact and
   normalized results — it's exact-first, normalized-as-fallback. For
   "metformin" this correctly isolates the single ingredient-level concept
   (rxcui 6809, tty=IN, name="metformin") rather than also surfacing the
   salt-form concept (235743, tty=PIN, "metformin hydrochloride") that a
   normalized-only search pulls in. This resolves what looked like a name
   ambiguity in earlier ad hoc testing (DATA_CONTRACTS.md F11, which used
   search=1) — for this specific name, search=2 is unambiguous.
2. `GET /REST/rxcui/{rxcui}/properties.json` returns the canonical
   `name` and `tty` for a known RxCUI — confirmed shape:
   `{"properties": {"rxcui", "name", "tty", ...}}`.
3. `GET /REST/approximateTerm.json?term=...&maxEntries=N` — confirmed the
   response nests candidates as `{"approximateGroup": {"candidate": [...]}}`,
   and that this array is NOT one row per distinct drug: the same rxcui
   appears multiple times, once per source vocabulary (RXNORM, VANDF, ATC,
   DRUGBANK, ...), all sharing the same score and rank. Verified with
   "metforminn" (a misspelling) — 6 rows returned, all rxcui=6809. Code
   here dedupes by rxcui, keeping the max score seen, before ranking
   candidates — naively taking "the first N rows" would just return N
   copies of the same top hit for common drugs and silently truncate real
   alternates for others.
4. `score` in each candidate is a JSON STRING (e.g. `"8.331008911132812"`),
   not a number — must be cast with `float()`, never compared as text.
5. A query with zero exact matches returns `{"idGroup": {}}` — no
   `rxnormId` key at all, not an empty list. A fully nonsense
   `approximateTerm` query returns `{"approximateGroup": {"inputTerm": null}}`
   — no `candidate` key. Both are handled via `.get(..., default)`, never
   assumed present.
6. Rate limit, confirmed against `lhncbc.nlm.nih.gov/RxNav/TermsofService.html`:
   "send no more than 20 requests per second per IP address." No API key
   required. This module self-limits to one request per
   `MIN_REQUEST_INTERVAL_SECONDS` (0.5s, i.e. 2/sec) — 10x under the
   published ceiling — and caches every response in-process, which also
   follows NLM's stated preference ("caching results for a 12-24 hour
   period") in spirit, though the cache here is in-memory/process-lifetime
   only, not persisted to disk.

Score threshold calibration (honest disclosure, not false precision): ad hoc
testing against real misspellings ("lisinoprol", "ibuprofin", "predisone",
"warfarn", "metforminn") consistently scored in the 6.4–9.7 range for the
correct drug. Testing could not produce a clean *low-but-nonzero* negative
example — RxNav's own candidate generation appears to already return zero
candidates for strings with no meaningful overlap (confirmed with a random
character string), rather than returning a poor match with a low score. So
APPROXIMATE_SCORE_THRESHOLD below is a conservative floor set well under the
observed genuine-match range, not a boundary tuned against real negatives.
Revisit if the eval set (DATA_CONTRACTS.md / ARCHITECTURE.md §6) surfaces a
false-positive approximate match.
"""

import time
from dataclasses import dataclass
from typing import Any, Literal, Optional
from urllib.parse import quote

import requests

RXNAV_BASE = "https://rxnav.nlm.nih.gov/REST"

# Self-imposed politeness delay between actual network calls (cache misses
# only). RxNav's published hard limit is 20 req/sec (see module docstring,
# point 6) — this keeps us at 2 req/sec, 10x under that ceiling.
MIN_REQUEST_INTERVAL_SECONDS = 0.5

# Conservative floor for approximateTerm scores; see calibration note above.
APPROXIMATE_SCORE_THRESHOLD = 4.0

# Candidate pool size for the approximate tier, per Task 1.2 requirement #1.
DEFAULT_MAX_APPROXIMATE_ENTRIES = 4

MatchType = Literal["exact", "approximate", "none"]


@dataclass(frozen=True)
class RxNormResult:
    input_name: str
    rxcui: Optional[str]
    matched_name: Optional[str]
    match_type: MatchType
    score: Optional[float]


# In-memory cache, keyed by request URL. Process-lifetime only — not
# persisted across runs. Satisfies Task 1.2 requirement #2 ("in-memory
# cache"); see module docstring point 6 for why a heavier persistent cache
# wasn't built.
_cache: dict[str, Any] = {}
_last_request_time: float = 0.0


def _get_json(url: str) -> Any:
    """GET url with the shared cache and politeness delay applied.

    Every public function in this module funnels through here, so the cache
    and rate limit are enforced exactly once, in one place, regardless of
    which endpoint is being called.
    """
    global _last_request_time

    if url in _cache:
        return _cache[url]

    elapsed = time.monotonic() - _last_request_time
    if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
        time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)

    response = requests.get(url, timeout=10)
    _last_request_time = time.monotonic()
    response.raise_for_status()

    data = response.json()
    _cache[url] = data
    return data


def search_exact(name: str) -> list[str]:
    """Exact-tier RxCUI search (search=2: exact, falling back to normalized
    only if exact finds nothing — see module docstring point 1).

    Returns every RxCUI string found — normally zero or one, but the API can
    in principle return more than one for a single name (see get_rxcui's
    handling of that case). Never guesses among them itself.
    """
    url = f"{RXNAV_BASE}/rxcui.json?name={quote(name)}&search=2"
    data = _get_json(url)
    return data.get("idGroup", {}).get("rxnormId", [])


def search_approximate(term: str, max_entries: int = DEFAULT_MAX_APPROXIMATE_ENTRIES) -> list[dict]:
    """Approximate (fuzzy) RxCUI search, deduped and ranked by score.

    The raw API response contains multiple rows per distinct RxCUI — one per
    source vocabulary that independently indexed the same concept — all
    sharing that RxCUI's score and rank (verified; see module docstring
    point 3). This function collapses those into one entry per RxCUI,
    keeping the highest score seen, and returns them sorted best-first.

    Each returned dict has: rxcui (str), score (float), rank (str | None) —
    rank is RxNav's own field, passed through verbatim, not recomputed from
    this function's own ordering.
    """
    url = f"{RXNAV_BASE}/approximateTerm.json?term={quote(term)}&maxEntries={max_entries}"
    data = _get_json(url)
    raw_candidates = data.get("approximateGroup", {}).get("candidate") or []

    best_by_rxcui: dict[str, dict] = {}
    for candidate in raw_candidates:
        rxcui = candidate["rxcui"]
        score = float(candidate["score"])  # confirmed: API returns this as a string
        if rxcui not in best_by_rxcui or score > best_by_rxcui[rxcui]["score"]:
            best_by_rxcui[rxcui] = {"rxcui": rxcui, "score": score, "rank": candidate.get("rank")}

    return sorted(best_by_rxcui.values(), key=lambda c: -c["score"])


def get_properties(rxcui: str) -> dict:
    """Canonical name + term type (tty) for a known RxCUI.

    Returns {} if the RxCUI is somehow unknown to RxNav (shouldn't happen for
    an RxCUI this module just received from RxNav itself, but not assumed).
    """
    url = f"{RXNAV_BASE}/rxcui/{rxcui}/properties.json"
    data = _get_json(url)
    return data.get("properties", {})


def get_rxcui(name: str) -> RxNormResult:
    """Resolve a drug name to a single canonical RxCUI, or refuse to guess.

    See the safety invariant at the top of this module: a `none` result
    means exactly that — this function found no confident single answer —
    and the caller must not treat it as equivalent to any particular drug.

    Resolution order:
      1. Exact tier (search=2). Exactly one hit -> that's the answer.
         Zero or more than one hit is NOT automatically resolved here (a
         `>1` case is a genuine ambiguity at the exact-match level — treated
         identically to zero, i.e. escalated to the caller, never guessed).
      2. Approximate tier, only if the exact tier found nothing. The top
         (highest-score) candidate wins only if:
           - its score clears APPROXIMATE_SCORE_THRESHOLD, and
           - no other distinct RxCUI ties it for the top score (a tie is
             exactly the ambiguous case this module must not silently
             resolve).
      3. Otherwise: match_type="none".
    """
    name = name.strip()
    if not name:
        return RxNormResult(input_name=name, rxcui=None, matched_name=None, match_type="none", score=None)

    exact_ids = search_exact(name)

    if len(exact_ids) == 1:
        rxcui = exact_ids[0]
        props = get_properties(rxcui)
        return RxNormResult(
            input_name=name,
            rxcui=rxcui,
            matched_name=props.get("name"),
            match_type="exact",
            score=1.0,
        )

    if len(exact_ids) > 1:
        # More than one exact-tier RxCUI for one name: a real ambiguity, not
        # a bug. Never silently pick the first one — see safety invariant.
        return RxNormResult(input_name=name, rxcui=None, matched_name=None, match_type="none", score=None)

    # Exact tier found nothing (len(exact_ids) == 0) — try approximate.
    candidates = search_approximate(name, max_entries=DEFAULT_MAX_APPROXIMATE_ENTRIES)
    if not candidates:
        return RxNormResult(input_name=name, rxcui=None, matched_name=None, match_type="none", score=None)

    top = candidates[0]

    if len(candidates) > 1 and candidates[1]["score"] == top["score"]:
        # Two distinct drugs tied for best fuzzy match — cannot pick one.
        return RxNormResult(input_name=name, rxcui=None, matched_name=None, match_type="none", score=top["score"])

    if top["score"] < APPROXIMATE_SCORE_THRESHOLD:
        return RxNormResult(input_name=name, rxcui=None, matched_name=None, match_type="none", score=top["score"])

    props = get_properties(top["rxcui"])
    return RxNormResult(
        input_name=name,
        rxcui=top["rxcui"],
        matched_name=props.get("name"),
        match_type="approximate",
        score=top["score"],
    )
