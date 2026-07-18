"""NeuroRx AI — prescription extraction flow (Task 2.7).

Photo/text -> structured, RxCUI-resolved, human-confirmable schedule proposal.

Per `ARCHITECTURE.md` §2 ("Prescription extraction flow"): this module runs
**outside** the chat agent — the app calls `extract_schedule()` (or the four
stages individually) directly, renders the returned `propose()` payload as the
confirmation card, and only *after* the user confirms does the app call
`manage_schedule` (Task 2.3) to actually write anything. **Nothing in this
file writes to Lakebase, Delta, or any other store — there is no database
client imported here, by construction, not by convention.** The explicit
human confirmation step this feeds is both the safety design and, per
`ARCHITECTURE.md` §2, "a great demo beat."

## Pipeline

    extract()   photo/text -> [{drug_name, strength, frequency_text, timing_notes}]
                (a transcription call to the Sonnet-tier FM endpoint — literal
                 reading of what's on the label/text, not clinical judgment)
    normalize() + times_per_day, dose_times, needs_review, review_reasons
                (deterministic regex mapping table — never the LLM's guess)
    resolve()   + rxcui, matched_name, match_type, candidates
                (RxNorm lookup via `data/ingestion/rxnorm_client.py`, Task 1.2 —
                 never overridden or second-guessed here)
    propose()   -> {"drugs": [...], "requires_user_confirmation": True}
                (pure assembly — no I/O, no side effects)

This mirrors the project's spine (CLAUDE.md §1): "clinical facts come from
deterministic SQL/table lookups... The LLM explains, it never originates."
Here the LLM's only job is OCR-like transcription of literal printed text; it
is explicitly instructed not to interpret or expand frequency abbreviations
itself (see `EXTRACTION_PROMPT`) — that interpretation happens in `normalize()`,
in code, where it is auditable and testable, not inside a model call.

## What was verified live this session, not assumed

`data/ingestion/rxnorm_client.py` (Task 1.2) is reused as-is, unmodified — its
own module docstring documents the safety invariant this file depends on:
`get_rxcui()` never silently substitutes a drug; a `none` result means exactly
that, and it is this module's job, as the caller, to build a candidate list
for the human to choose from. The three fixtures at the bottom of this file
were resolved against the **live** RxNav API this session to find genuinely
verified cases, not invented ones:

- `"Norvasc 5mg"` — a realistic mistake (strength appended to a brand name)
  that RxNorm resolves to a **genuine tie**: two distinct RxCUIs (572722,
  "amlodipine 5 MG [Norvasc]", tty=SBDC; and 212549, "amlodipine 5 MG Oral
  Tablet [Norvasc]", tty=SBD) score identically in the fuzzy search. This is
  real RxNorm behavior (different term-type granularity for the same product),
  not a fabricated example — plain `"Norvasc"` alone resolves cleanly exact.
- `"Lisinopril"` -> exact, rxcui 29046. `"metformin"` -> exact, rxcui 6809.
  Both confirmed live and consistent with prior tasks' verified RxCUI table
  in CLAUDE.md §4.

⚠️ **What was NOT verified**: the actual FM-endpoint call in
`_call_fm_extraction` (multimodal Claude transcription) has not been run
against a live Databricks workspace — there is no live endpoint reachable from
this environment. The three fixtures below inject a stub in place of the FM
call (`_fm_call=`) so that `normalize()`, `resolve()` (a real live RxNav call),
and `propose()` are genuinely exercised end to end; only the FM call itself is
faked. First thing to check on real deployment: does a real multimodal
response actually come back as clean JSON per `EXTRACTION_PROMPT`'s
instructions, or does the retry-once path get exercised in practice more than
the "defensive, shouldn't normally trigger" framing here assumes.

⚠️ Same `temperature` conflict as `agent/agent.py` (Task 2.6, see that file's
docstring): Claude Sonnet 5 on the Databricks FM API rejects
`temperature`/`top_p`/`top_k` with a 400. Not passed here either, for the same
reason.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.config import settings
from data.ingestion import rxnorm_client as rxnorm

# ---------------------------------------------------------------------------
# Step 1: extract()
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """You are a prescription-label transcription tool. You are not a \
medical professional and this is not medical advice — you only transcribe what is \
printed or written on the prescription image or text you are given.

Extract every distinct medication mentioned. Return ONLY a JSON array, nothing else — \
no markdown code fences, no explanation, no leading or trailing text before or after \
the array.

Each element of the array must be a JSON object with exactly these four string fields:

  "drug_name":      the medication name exactly as written (brand or generic name, \
                     either is fine — do not correct spelling or expand abbreviations)
  "strength":       the dose strength exactly as written, e.g. "500 mg" (empty string \
                     "" if no strength is shown)
  "frequency_text": the frequency/sig exactly as written, e.g. "1 tab po bid pc" — \
                     copy the literal text through verbatim. Do NOT expand \
                     abbreviations, do NOT compute specific clock times, and do NOT \
                     infer a frequency that is not stated. That normalization happens \
                     downstream, deterministically, not by you.
  "timing_notes":   any other timing or administration constraint not already captured \
                     in frequency_text, e.g. "avoid grapefruit", "take with a full \
                     glass of water" (empty string "" if none)

If you are not certain of a field's value, use your best literal reading of the source; \
never invent a value that is not present in the image or text, and never fill an \
uncertain field with a placeholder like "N/A" or a guess.

If the image or text contains no medication information at all, return an empty JSON \
array: []

Return nothing but the JSON array."""


class ExtractionError(Exception):
    """Raised when the FM endpoint's output could not be parsed as the required
    JSON array, even after one retry. The caller (the app) should show the user
    a "couldn't read this — try again or enter manually" message; this module
    never falls back to guessing a schedule from unparseable output."""


def _strip_code_fences(text: str) -> str:
    """Remove a leading/trailing ```json ... ``` or ``` ... ``` fence, if present.

    Models reliably ignore "no markdown fences" instructions often enough that
    this is standard defensive parsing, not a sign the prompt is wrong.
    """
    stripped = text.strip()
    fence_match = re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", stripped, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    return stripped


def _extract_bracketed_array(text: str) -> str:
    """Fall back to the substring between the first '[' and the last ']'.

    Handles a model prefacing the array with text despite instructions not to
    (e.g. "Here is the extracted JSON:\\n[...]"). This is still defensive
    client-side parsing, not a retry — the retry-once budget is reserved for
    output that fails even after this.
    """
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start : end + 1]


def _parse_json_array(raw_text: str) -> list[dict]:
    candidate = _strip_code_fences(raw_text)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        parsed = json.loads(_extract_bracketed_array(candidate))  # re-raises if still bad

    if not isinstance(parsed, list):
        raise ValueError(f"Expected a JSON array, got {type(parsed).__name__}: {parsed!r}")
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError(f"Expected each array element to be an object, got: {item!r}")
    return parsed


def _call_fm_extraction(input_data: bytes | str, is_image: bool) -> str:
    """The real call to the Sonnet-tier FM endpoint. Isolated behind this one
    function so `extract()` can be tested with a stub in place of it (see the
    fixtures at the bottom of this file) — this function itself has not been
    exercised against a live endpoint in this environment; see the module
    docstring's ⚠️ note.
    """
    from databricks_langchain import ChatDatabricks
    from langchain_core.messages import HumanMessage

    # Same endpoint the supervisor agent uses (agent/agent.py) — swapping
    # models is a config change (FM_CHAT_ENDPOINT), not a code change here either.
    # No `temperature=`: see this module's docstring.
    llm = ChatDatabricks(endpoint=settings.fm_chat_endpoint)

    if is_image:
        b64 = base64.b64encode(input_data).decode("ascii")
        content: Any = [
            {"type": "text", "text": EXTRACTION_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]
    else:
        content = f"{EXTRACTION_PROMPT}\n\n---\nPrescription text:\n{input_data}"

    response = llm.invoke([HumanMessage(content=content)])
    return response.content


def extract(
    input_data: bytes | str,
    is_image: bool = False,
    *,
    _fm_call: Optional[Callable[[bytes | str, bool], str]] = None,
) -> list[dict]:
    """Step 1. Photo bytes or pasted text -> list of literally-transcribed drug
    dicts. Retries the FM call exactly once on a parse failure (after client-side
    defensive parsing — fence-stripping and bracket-extraction — has already
    been tried and still failed), then raises `ExtractionError`.
    """
    call = _fm_call or _call_fm_extraction

    raw = call(input_data, is_image)
    try:
        return _parse_json_array(raw)
    except (json.JSONDecodeError, ValueError) as first_error:
        raw_retry = call(input_data, is_image)
        try:
            return _parse_json_array(raw_retry)
        except (json.JSONDecodeError, ValueError) as second_error:
            raise ExtractionError(
                "FM endpoint did not return a parseable JSON array after one retry. "
                f"First attempt error: {first_error}. Retry error: {second_error}. "
                f"Last raw output: {raw_retry!r}"
            ) from second_error


# ---------------------------------------------------------------------------
# Step 2: normalize()
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrequencyRule:
    key: str
    pattern: re.Pattern
    schedulable: bool
    times_per_day: Optional[int] = None
    dose_times: Optional[tuple[str, ...]] = None
    unschedulable_reason: Optional[str] = None


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


# Ordered deliberately: more specific / diurnally-qualified patterns are
# checked before their generic counterpart. "once daily at night" must match
# the qhs rule (-> 21:00), not the generic qd rule (-> 08:00) — this only
# works if qhs is checked first. Same reasoning for qam/qpm before qd, and for
# qid/tid/bid before the generic "daily" fallback (so "twice daily" is caught
# by bid, never falls through to a bare `\bdaily\b` match). First match wins;
# order is load-bearing, not cosmetic — see the ordering regression test in
# this file's __main__ block.
FREQUENCY_RULES: list[FrequencyRule] = [
    FrequencyRule(
        "qid", _rx(r"\bq\.?i\.?d\.?\b|four times (a|per) day|four times daily"),
        True, 4, ("08:00:00", "12:00:00", "16:00:00", "20:00:00"),
    ),
    FrequencyRule(
        "tid", _rx(r"\bt\.?i\.?d\.?\b|three times (a|per) day|three times daily"),
        True, 3, ("08:00:00", "14:00:00", "20:00:00"),
    ),
    FrequencyRule(
        "bid", _rx(r"\bb\.?i\.?d\.?\b|twice (a|per) day|twice daily"),
        True, 2, ("08:00:00", "20:00:00"),
    ),
    FrequencyRule(
        "q12h", _rx(r"\bq\.?12\.?h\.?\b|every 12 hours"),
        True, 2, ("08:00:00", "20:00:00"),
    ),
    FrequencyRule(
        "q8h", _rx(r"\bq\.?8\.?h\.?\b|every 8 hours"),
        True, 3, ("06:00:00", "14:00:00", "22:00:00"),
    ),
    FrequencyRule(
        "q6h", _rx(r"\bq\.?6\.?h\.?\b|every 6 hours"),
        True, 4, ("06:00:00", "12:00:00", "18:00:00", "00:00:00"),
    ),
    FrequencyRule(
        "q4h", _rx(r"\bq\.?4\.?h\.?\b|every 4 hours"),
        True, 6, ("02:00:00", "06:00:00", "10:00:00", "14:00:00", "18:00:00", "22:00:00"),
    ),
    FrequencyRule(
        "qhs",
        _rx(r"\bq\.?h\.?s\.?\b|at bedtime|before bed|\bnightly\b|once daily at night|\bat night\b"),
        True, 1, ("21:00:00",),
    ),
    FrequencyRule(
        "qam",
        _rx(r"\bq\.?a\.?m\.?\b|every morning|each morning|once daily in the morning"),
        True, 1, ("08:00:00",),
    ),
    FrequencyRule(
        "qpm",
        _rx(r"\bq\.?p\.?m\.?\b|every evening|each evening|once daily in the evening"),
        True, 1, ("18:00:00",),
    ),
    FrequencyRule(
        "qd",
        _rx(r"\bq\.?d\.?\b|once (a |per )?day\b|once daily|\bdaily\b"),
        True, 1, ("08:00:00",),
    ),
    # Recognized, but genuinely unrepresentable in the times_per_day/dose_times
    # model (DATA_CONTRACTS.md §6.2: dosing is modeled per calendar day). These
    # still count toward "known patterns" — the drug isn't sent to needs_review
    # because we failed to understand the text, but because the schema itself
    # has no slot for it yet.
    FrequencyRule(
        "qod", _rx(r"\bq\.?o\.?d\.?\b|every other day"),
        False, unschedulable_reason=(
            "Every-other-day dosing is not representable by the current daily "
            "times_per_day/dose_times model — needs manual scheduling."
        ),
    ),
    FrequencyRule(
        "weekly", _rx(r"\bonce (a |per )?week(ly)?\b"),
        False, unschedulable_reason=(
            "Weekly dosing is not representable by the current daily model — "
            "needs manual scheduling."
        ),
    ),
    FrequencyRule(
        "prn", _rx(r"\bprn\b|as needed|as necessary"),
        False, unschedulable_reason=(
            "As-needed dosing has no fixed times to schedule automatically — "
            "needs manual entry if the patient wants reminders."
        ),
    ),
]

# Independent of frequency (a drug can be "bid" AND "pc" at once) — appended to
# timing_notes, never affects times_per_day/dose_times. "po" (by mouth) and
# similar route-only tokens are deliberately NOT modifiers here: they carry no
# timing constraint, so they are left alone in the raw frequency_text (which is
# preserved verbatim for the human to see) rather than echoed into timing_notes
# as noise.
MODIFIER_RULES: list[tuple[str, re.Pattern]] = [
    ("after meals", _rx(r"\bpc\b|after meals|after eating")),
    ("before meals", _rx(r"\bac\b|before meals")),
    ("with food", _rx(r"with food|with meals")),
    ("on an empty stomach", _rx(r"empty stomach|without food")),
]


def _match_frequency(frequency_text: str) -> Optional[FrequencyRule]:
    for rule in FREQUENCY_RULES:
        if rule.pattern.search(frequency_text):
            return rule
    return None


def _match_modifiers(frequency_text: str) -> list[str]:
    return [note for note, pattern in MODIFIER_RULES if pattern.search(frequency_text)]


def normalize(extracted_drugs: list[dict]) -> list[dict]:
    """Step 2. Derives times_per_day/dose_times from each drug's frequency_text
    via the deterministic table above — never the LLM's own interpretation.
    Unknown or unrepresentable patterns keep the raw frequency_text untouched
    and set needs_review=True with a specific reason; nothing is guessed.
    """
    normalized = []
    for drug in extracted_drugs:
        frequency_text = (drug.get("frequency_text") or "").strip()
        timing_notes = (drug.get("timing_notes") or "").strip()
        needs_review = False
        review_reasons: list[str] = []
        times_per_day: Optional[int] = None
        dose_times: Optional[list[str]] = None

        if not frequency_text:
            needs_review = True
            review_reasons.append("No frequency information was extracted for this drug.")
        else:
            rule = _match_frequency(frequency_text)
            if rule is None:
                needs_review = True
                review_reasons.append(
                    f"Frequency text {frequency_text!r} did not match any known "
                    "pattern — please set the schedule manually."
                )
            elif not rule.schedulable:
                needs_review = True
                review_reasons.append(rule.unschedulable_reason)
            else:
                times_per_day = rule.times_per_day
                dose_times = list(rule.dose_times)

            modifier_notes = _match_modifiers(frequency_text)
            if modifier_notes:
                combined = [timing_notes] if timing_notes else []
                for note in modifier_notes:
                    if note not in combined:
                        combined.append(note)
                timing_notes = "; ".join(combined)

        normalized.append(
            {
                **drug,
                "frequency_text": frequency_text,
                "timing_notes": timing_notes,
                "times_per_day": times_per_day,
                "dose_times": dose_times,
                "needs_review": needs_review,
                "review_reasons": review_reasons,
            }
        )
    return normalized


# ---------------------------------------------------------------------------
# Step 3: resolve()
# ---------------------------------------------------------------------------


def _enrich_candidates(candidates: list[dict]) -> list[dict]:
    """Attach a human-readable name to each {"rxcui", "score"} candidate so the
    confirmation UI can show "did you mean X or Y?" rather than bare RxCUIs.
    `matched_name` can legitimately be None — RxNav's own properties lookup
    returns {} for at least one real RxCUI observed live this session
    (rxcui 285065, the top approximate hit for "Glucophage XR") — so this is
    not a defensive-programming hypothetical, it is an observed real case.
    """
    enriched = []
    for c in candidates:
        props = rxnorm.get_properties(c["rxcui"])
        enriched.append(
            {"rxcui": c["rxcui"], "score": c.get("score"), "matched_name": props.get("name")}
        )
    return enriched


def resolve(normalized_drugs: list[dict]) -> list[dict]:
    """Step 3. RxNorm resolution per drug via `rxnorm_client.get_rxcui` (Task
    1.2) — reused exactly as written, never second-guessed. Per that module's
    own safety invariant, a `none` or `approximate` match_type always sets
    needs_review=True; this function's job as "the caller" is building the
    candidate list a human can actually choose from, which requires a bit more
    than just relaying `get_rxcui`'s own top pick (see the "none" branch below,
    which distinguishes a genuine multi-way exact tie from a plain
    zero-match — `get_rxcui` collapses both to the same `match_type="none"`,
    but the right candidate list to show a human differs between the two).
    """
    resolved = []
    for drug in normalized_drugs:
        drug_name = (drug.get("drug_name") or "").strip()
        needs_review = drug.get("needs_review", False)
        review_reasons = list(drug.get("review_reasons", []))
        candidates: list[dict] = []

        if not drug_name:
            needs_review = True
            review_reasons.append("No drug name was extracted.")
            rxcui = matched_name = None
            match_type = "none"
            score = None
        else:
            result = rxnorm.get_rxcui(drug_name)
            rxcui, matched_name, match_type, score = (
                result.rxcui,
                result.matched_name,
                result.match_type,
                result.score,
            )

            if match_type == "approximate":
                needs_review = True
                review_reasons.append(
                    f"RxNorm match for {drug_name!r} is only approximate (matched "
                    f"{matched_name!r}, score {score:.1f}) — please confirm."
                )
                candidates = _enrich_candidates(rxnorm.search_approximate(drug_name))

            elif match_type == "none":
                needs_review = True
                # get_rxcui() collapses "multiple exact matches" and "zero
                # matches at all" to the same match_type="none" — distinguish
                # them here so the candidate list actually reflects which case
                # this is, rather than silently only ever showing fuzzy hits.
                exact_ids = rxnorm.search_exact(drug_name)
                if len(exact_ids) > 1:
                    review_reasons.append(
                        f"Multiple exact RxNorm matches found for {drug_name!r} — "
                        "please choose the correct one."
                    )
                    candidates = _enrich_candidates(
                        [{"rxcui": rid, "score": None} for rid in exact_ids]
                    )
                else:
                    approx = rxnorm.search_approximate(drug_name)
                    if approx:
                        review_reasons.append(
                            f"No confident RxNorm match found for {drug_name!r} — "
                            "here are the closest matches, please confirm or search "
                            "manually."
                        )
                        candidates = _enrich_candidates(approx)
                    else:
                        review_reasons.append(
                            f"No RxNorm match at all was found for {drug_name!r} — "
                            "please search manually."
                        )

        resolved.append(
            {
                **drug,
                "drug_name": drug_name,
                "rxcui": rxcui,
                "matched_name": matched_name,
                "match_type": match_type,
                "rxnorm_score": score,
                "candidates": candidates,
                "needs_review": needs_review,
                "review_reasons": review_reasons,
            }
        )
    return resolved


# ---------------------------------------------------------------------------
# Step 4: propose()
# ---------------------------------------------------------------------------


def _confidence(drug: dict) -> str:
    """A simple three-tier label, not a calibrated probability — this project
    does not have a real confidence model for extraction, and presenting one
    number as if it were calibrated would overstate what is actually known.
    "low" specifically means the RxNorm step found nothing usable at all;
    "medium" covers every other reason needs_review might be set (an
    approximate RxNorm match, or an unschedulable/unrecognized frequency);
    "high" means both steps succeeded cleanly.
    """
    if not drug.get("needs_review"):
        return "high"
    return "low" if drug.get("match_type") == "none" else "medium"


def propose(resolved_drugs: list[dict]) -> dict:
    """Step 4. Assembles the confirmation payload the UI renders. Pure
    function: no I/O, no side effects, no database client imported anywhere in
    this module. The only path from here to a persisted schedule is the user
    confirming this payload in the UI, which then calls `manage_schedule`
    (Task 2.3) directly — `manage_schedule` itself separately enforces
    `user_confirmed=true` in code, so this module's `requires_user_confirmation`
    flag is a UI contract, not the actual safety gate.
    """
    drugs = [
        {
            "drug_name": drug.get("drug_name"),
            "strength": drug.get("strength"),
            "frequency_text": drug.get("frequency_text"),
            "timing_notes": drug.get("timing_notes"),
            "times_per_day": drug.get("times_per_day"),
            "dose_times": drug.get("dose_times"),
            "rxcui": drug.get("rxcui"),
            "matched_name": drug.get("matched_name"),
            "match_type": drug.get("match_type"),
            "needs_review": drug.get("needs_review", False),
            "review_reasons": drug.get("review_reasons", []),
            "candidates": drug.get("candidates", []),
            "confidence": _confidence(drug),
        }
        for drug in resolved_drugs
    ]
    return {"drugs": drugs, "requires_user_confirmation": True}


def extract_schedule(
    input_data: bytes | str,
    is_image: bool = False,
    *,
    _fm_call: Optional[Callable[[bytes | str, bool], str]] = None,
) -> dict:
    """The single entry point the app calls: photo/text in, confirmation
    payload out. Equivalent to `propose(resolve(normalize(extract(...))))`."""
    extracted = extract(input_data, is_image=is_image, _fm_call=_fm_call)
    return propose(resolve(normalize(extracted)))


if __name__ == "__main__":
    # ------------------------------------------------------------------
    # Frequency-pattern regression test — verifies the ordering property
    # the module docstring's FREQUENCY_RULES comment argues for, rather
    # than leaving it as an unverified claim. Also demonstrates >= 12
    # recognized patterns concretely (14 here).
    # ------------------------------------------------------------------
    _ordering_checks = [
        ("twice daily", "bid"), ("three times daily", "tid"), ("four times daily", "qid"),
        ("BID", "bid"), ("TID", "tid"), ("QID", "qid"),
        ("b.i.d.", "bid"), ("t.i.d.", "tid"), ("q.i.d.", "qid"),
        ("once daily at night", "qhs"), ("once daily in the morning", "qam"),
        ("once daily in the evening", "qpm"), ("once daily", "qd"), ("qd", "qd"),
        ("every 12 hours", "q12h"), ("every 8 hours", "q8h"),
        ("every 6 hours", "q6h"), ("every 4 hours", "q4h"),
        ("at bedtime", "qhs"), ("every other day", "qod"),
        ("once weekly", "weekly"), ("as needed", "prn"), ("PRN", "prn"),
    ]
    for _text, _expected_key in _ordering_checks:
        _rule = _match_frequency(_text)
        assert _rule is not None, f"{_text!r} matched no rule"
        assert _rule.key == _expected_key, (
            f"{_text!r} matched rule {_rule.key!r}, expected {_expected_key!r} — "
            "the diurnal/specific-before-generic ordering may be broken"
        )
    print(f"PASS: {len(_ordering_checks)} frequency-pattern ordering checks "
          f"({len(FREQUENCY_RULES)} rules registered)")

    _garbage = normalize(
        [{"drug_name": "x", "strength": "", "frequency_text": "purple elephant schedule",
          "timing_notes": ""}]
    )
    assert _garbage[0]["needs_review"] is True
    assert _garbage[0]["times_per_day"] is None
    print("PASS: unrecognized frequency text flagged needs_review, no fabricated schedule")

    # ------------------------------------------------------------------
    # Retry-once-on-parse-failure self-test: first FM call returns
    # unparseable output (even after fence-stripping), second succeeds.
    # ------------------------------------------------------------------
    _flaky_state = {"calls": 0}

    def _flaky_fm_call(_input, _is_image):
        _flaky_state["calls"] += 1
        if _flaky_state["calls"] == 1:
            return 'Sure, here you go: [{"drug_name": "aspirin"'  # truncated, invalid JSON
        return '[{"drug_name": "aspirin", "strength": "81 mg", "frequency_text": "once daily", "timing_notes": ""}]'

    _retry_result = extract("irrelevant", _fm_call=_flaky_fm_call)
    assert _flaky_state["calls"] == 2, "expected exactly one retry"
    assert _retry_result[0]["drug_name"] == "aspirin"
    print("PASS: retry-once-on-parse-failure — first call malformed, second call recovered")

    # ------------------------------------------------------------------
    # Fixture 1 — clean typed sig
    # ------------------------------------------------------------------
    def _stub_fixture_1(_input, _is_image):
        return '[{"drug_name": "Lisinopril", "strength": "10 mg", "frequency_text": "once daily", "timing_notes": ""}]'

    result_1 = extract_schedule("Lisinopril 10 mg once daily", _fm_call=_stub_fixture_1)
    d1 = result_1["drugs"][0]
    assert result_1["requires_user_confirmation"] is True
    assert d1["times_per_day"] == 1 and d1["dose_times"] == ["08:00:00"]
    assert d1["needs_review"] is False, d1["review_reasons"]
    assert d1["rxcui"] == "29046" and d1["matched_name"] == "lisinopril"
    assert d1["confidence"] == "high"
    print(f"PASS: fixture 1 (clean sig) — {d1['drug_name']} -> rxcui {d1['rxcui']}, "
          f"{d1['times_per_day']}x/day at {d1['dose_times']}, confidence={d1['confidence']}")

    # ------------------------------------------------------------------
    # Fixture 2 — messy real-world sig, wrapped in a code fence to also
    # exercise fence-stripping within a realistic fixture.
    # ------------------------------------------------------------------
    def _stub_fixture_2(_input, _is_image):
        return (
            '```json\n'
            '[{"drug_name": "metformin", "strength": "500 mg", '
            '"frequency_text": "1 tab po bid pc", "timing_notes": ""}]\n'
            '```'
        )

    result_2 = extract_schedule("metformin 500mg 1 tab po bid pc", _fm_call=_stub_fixture_2)
    d2 = result_2["drugs"][0]
    assert d2["times_per_day"] == 2 and d2["dose_times"] == ["08:00:00", "20:00:00"]
    assert "after meals" in d2["timing_notes"]
    assert d2["needs_review"] is False, d2["review_reasons"]
    assert d2["rxcui"] == "6809" and d2["matched_name"] == "metformin"
    assert d2["confidence"] == "high"
    print(f"PASS: fixture 2 (messy sig, fenced) — {d2['drug_name']} -> rxcui {d2['rxcui']}, "
          f"{d2['times_per_day']}x/day, timing_notes={d2['timing_notes']!r}")

    # ------------------------------------------------------------------
    # Fixture 3 — ambiguous brand name. "Norvasc 5mg" is a REAL, live-
    # verified RxNorm tie (see module docstring) — not a fabricated case.
    # ------------------------------------------------------------------
    def _stub_fixture_3(_input, _is_image):
        return '[{"drug_name": "Norvasc 5mg", "strength": "", "frequency_text": "once daily", "timing_notes": ""}]'

    result_3 = extract_schedule("Norvasc 5mg once daily", _fm_call=_stub_fixture_3)
    d3 = result_3["drugs"][0]
    assert d3["times_per_day"] == 1 and d3["dose_times"] == ["08:00:00"]
    assert d3["match_type"] == "none"
    assert d3["needs_review"] is True
    assert d3["confidence"] == "low"
    candidate_rxcuis = {c["rxcui"] for c in d3["candidates"]}
    assert {"572722", "212549"}.issubset(candidate_rxcuis), (
        f"expected the live-verified tied RxCUIs among candidates, got {candidate_rxcuis}"
    )
    print(f"PASS: fixture 3 (ambiguous brand) — {d3['drug_name']} -> match_type=none, "
          f"needs_review=True, {len(d3['candidates'])} candidates offered: "
          f"{[c['matched_name'] for c in d3['candidates']]}")

    print("\nALL EXTRACTION.PY CHECKS PASSED")
