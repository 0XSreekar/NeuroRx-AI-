# agent/guardrail.py
"""NeuroRx AI — post-generation output guardrail (Task 4.5).

Two layers, per `ARCHITECTURE.md` §5(e) ("a lightweight check — regex + one
cheap LLM-judge call — blocks any response containing un-cited dosage
instructions"):

1. **Layer 1 (regex, fast, always runs)** — splits the response into
   sentences; a sentence carrying a real citation passes regardless of
   content; an uncited sentence matching a dosage-instruction candidate
   pattern gets flagged for Layer 2.
2. **Layer 2 (one LLM call, only if Layer 1 flagged something)** — the
   Haiku-tier FM endpoint gives a binary YES/NO verdict on whether the
   flagged text is ungrounded dosage/administration advice. YES blocks.

This is **defense-in-depth, not the primary control** — the same relationship
`agent/prompts/system_prompt.md`'s own rationale item 8 states for Rule 4
("a prompt instruction is not an enforcement mechanism... enforced in code").
Here the primary control is Rule 1 of the system prompt itself (never state
an uncited clinical fact); this file is what catches it if that fails.

## Design choices verified against this project's own established facts,
## not assumed fresh

- **Both citation forms are recognized, not just `[chunk_id]`.** The task
  brief's Layer 1 spec names `[chunk_id]` specifically, but
  `DATA_CONTRACTS.md` §8 point 3 and `evals/safety_judge.md`'s own
  Integration Spec §1 both already flag, in this exact project, that a
  single-citation-form guardrail blocks every correct interaction answer
  (`check_interactions` cites `[source: ddinter]`, never a chunk_id — it has
  no chunks to cite). Recognizing only `[chunk_id]` here would reproduce
  precisely the bug those two files already warned a guardrail must not
  make. Both forms pass Layer 1's citation check.
- **The citation regex is imported from `app/agent_client.py`
  (`CHUNK_ID_PATTERN`), not redefined.** That module's own comment already
  states the intent ("one citation-recognition pattern shared, not
  re-derived per file", referencing `agent/06_deploy_agent.py` and
  `agent/07_smoke_tests.py`) — this file follows that established
  convention rather than adding a fourth copy of the same regex.
  ⚠️ **Known gap, not fixed here without being asked:** `evals/
  02_run_evaluation.py` (Task 4.4) currently carries its own independent
  copy (`LABEL_CITATION_REGEX`/`_CHUNK_ID_CAPTURE_RE`), written before this
  file existed. It should be switched to import from `app.agent_client` too
  — flagged for a follow-up pass, not silently changed here since that file
  belongs to an already-delivered task.
- **`tool_trace` (this file's second parameter) does real work: catching
  fabricated citations, not just present-vs-absent ones.** A sentence can
  carry a well-formed `[chunk_id]` that was never actually returned by any
  `search_drug_labels` call in this turn — a hallucinated citation, which
  reads as *more* trustworthy than an uncited claim, not less. Layer 1
  checks the cited chunk_id against what the trace's tool calls actually
  returned; a citation that doesn't match anything in the trace is treated
  as uncited, exactly the same "verify provenance, not just form" check
  `evals/02_run_evaluation.py`'s `chunk_citation_groundedness_scorer` and
  `pipelines/05_vector_index.py`'s citation-corruption test already apply
  elsewhere in this project.
- **Layer 2's unparseable-output fallback is fail-safe BLOCK, not ALLOW** —
  the same asymmetry `evals/safety_judge.md` Integration Spec §3 already
  established for the eval judge ("an unparseable judge response must never
  silently score as PASS"), except sharper here: this runs on every live
  user turn, so a false ALLOW ships unvetted dosage advice to a real
  person, while a false BLOCK only costs one pharmacist-redirect fallback.
- **No `temperature`/`top_p`/`top_k` on the Layer 2 call** — `CLAUDE.md`'s
  repeatedly-verified fact: Claude on the Databricks FM API rejects these
  with a hard 400.
- **The judge endpoint is read from `app/config.py`'s `settings.
  fm_guardrail_endpoint`, not hardcoded.** `app/config.py` and
  `.env.example` already define this field (`FM_GUARDRAIL_ENDPOINT`,
  defaulting to `databricks-claude-haiku-4-5`) — evidently provisioned for
  exactly this file before it was written. Reading it here mirrors
  `agent/agent.py`'s own `LLM_ENDPOINT = settings.fm_chat_endpoint` "one
  line to change" pattern, rather than adding a second, disconnected
  hardcoded endpoint constant that could silently drift from the configured
  one.
- **`DATA_CONTRACTS.md` §6.4 anticipates a regex-only block path**
  (`judge_verdict` documented as "`NULL` when a regex rule blocked without a
  judge call"). This file's two-layer design, as Task 4.5 specifies it,
  never blocks without invoking Layer 2 — Layer 1 only ever *flags a
  candidate*, never blocks on its own. So every row this file's demo cell
  and `app/agent_client.chat()` wiring write has `judge_verdict` populated,
  never NULL. Flagged as a real (mild) discrepancy between the frozen
  schema's anticipated future and this task's actual spec, not silently
  reconciled by inventing a regex-only block rule nobody asked for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.agent_client import CHUNK_ID_PATTERN
from app.config import settings

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# From app/config.py / FM_GUARDRAIL_ENDPOINT — see module docstring.
HAIKU_ENDPOINT = settings.fm_guardrail_endpoint

SAFE_FALLBACK_TEXT = (
    "I can't verify that guidance against an official source. "
    "Please check with your pharmacist."
)

# Verbatim, per Task 4.5 requirement 3 — this exact string is sent as the
# judge's system prompt. Do not paraphrase it; the harness's own worked
# examples and this file's demo cell were both checked against this exact
# wording.
JUDGE_PROMPT = (
    "Does this sentence give medication dosage/administration advice not "
    "attributed to a citation? YES/NO."
)

# Interaction claims cite `[source: ddinter]`, never a chunk_id — see module
# docstring. `[^\]]*` tolerates the dual-attested form
# `array_join(sources, ', ')` can produce ("ddinter, fda_label") per
# `agent/tools/check_interactions.sql`.
INTERACTION_CITATION_PATTERN = re.compile(r"\[source:\s*ddinter[^\]]*\]", re.I)

# Rule 3 of the system prompt names three fixed routes; the presence of any
# of their distinguishing substrings is what identifies the response as an
# escalation, short-circuiting both layers per requirement 4. A phone
# number/frequency-looking token inside a genuine escalation message (e.g.
# "1-800-222-1222", "988") must never trip Layer 1 — detecting and bypassing
# here, before Layer 1 ever runs, is simpler and more robust than trying to
# make the dosage patterns clever enough to always ignore escalation text.
ESCALATION_MARKERS = ("911", "poison control", "1-800-222-1222", "988", "crisis lifeline")

# --- Layer 1: candidate dosage-instruction patterns ------------------------

_IMPERATIVE_DOSE_RE = re.compile(
    r"\b(take|double|skip|stop|increase|decrease|reduce|halve)\b[^.!?]{0,40}\b"
    r"(dose|doses|dosage|pill|pills|tablet|tablets|capsule|capsules|medication|mg|mcg)\b",
    re.I,
)
_DOSE_QUANTITY_RE = re.compile(
    r"\b\d+(\.\d+)?\s*(mg|mcg|milligrams?|micrograms?|tablets?|pills?|capsules?)\b", re.I,
)
_FREQUENCY_RE = re.compile(
    r"\b(daily|once a day|twice a day|three times a day|every\s*\d+\s*hours?|"
    r"per day|times a day|each morning|each evening|at bedtime|"
    r"once daily|twice daily)\b",
    re.I,
)

_LAYER1_PATTERNS = (
    (_IMPERATIVE_DOSE_RE, "imperative_dose_language"),
    (_DOSE_QUANTITY_RE, "dose_quantity"),
    (_FREQUENCY_RE, "frequency_language"),
)

# A pragmatic sentence splitter, not a full NLP tokenizer — this is a
# defense-in-depth secondary layer (see module docstring), not the primary
# safety control, so a known, documented limitation (doesn't special-case
# abbreviations like "e.g.") is an acceptable trade for staying dependency-
# free and fast (requirement 6's latency budget).
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# `system_prompt.md`'s "Citations" section mandates the citation goes at the
# END of the sentence it supports ("every clinical sentence ends with its
# citation"). A period immediately before the citation bracket — the
# convention's most literal reading, e.g. "...twice daily. [chunk_id]" — is
# itself a sentence boundary under `_SENTENCE_SPLIT_RE`, which strands the
# citation in its own one-token fragment, disconnected from the sentence it
# was meant to cover. A correctly-cited response would then read as uncited.
# Caught only by testing this exact phrasing, not by reading the regex.
# Fixed by merging a fragment that is JUST citation token(s) back into the
# fragment before it, in `_split_sentences()` below.
_CITATION_ONLY_FRAGMENT_RE = re.compile(
    r"^(\[[0-9a-f-]{36}:[a-z_]+:\d{4}\]|\[source:\s*ddinter[^\]]*\]|\s)+$", re.I
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class GuardrailResult:
    allowed: bool
    rule_triggered: str | None = None
    safe_fallback_text: str | None = None
    judge_verdict: str | None = None
    # Diagnostic only — which Layer 1 sub-pattern flagged the candidate that
    # went to the judge. Not a `guardrail_blocks` column (DATA_CONTRACTS.md
    # §6.4 has no field for it); useful for the demo/console output and for
    # a human reading a block after the fact, kept off the DB row rather
    # than silently extending a frozen schema.
    layer1_pattern: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_escalation_sentence(sentence: str) -> bool:
    """True when THIS sentence carries an escalation marker.

    ⚠️ Deliberately per-sentence, not whole-response. An earlier version
    short-circuited the ENTIRE guardrail whenever any marker appeared anywhere
    in the response — meaning "Just take a double dose to catch up. If you feel
    unwell, call 911." bypassed both layers completely, because the dosage
    advice rode in on the same response as the escalation text. That inverts
    the requirement: escalation-message *numbers* ("911", "1-800-222-1222")
    must not trip Layer 1's dose-quantity patterns, but escalation text must
    never grant amnesty to unrelated uncited dosage advice sitting next to it.
    Exempting only the marker-bearing sentences preserves the requirement's
    actual intent; every other sentence still goes through Layer 1 normally.
    """
    lowered = sentence.lower()
    return any(marker in lowered for marker in ESCALATION_MARKERS)


def _split_sentences(text: str) -> list[str]:
    raw = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]
    merged: list[str] = []
    for fragment in raw:
        if merged and _CITATION_ONLY_FRAGMENT_RE.match(fragment):
            merged[-1] = f"{merged[-1]} {fragment}"
        else:
            merged.append(fragment)
    return merged


def _returned_chunk_ids(tool_trace: list[dict]) -> set[str]:
    ids = set()
    for call in tool_trace or []:
        if call.get("tool") != "search_drug_labels":
            continue
        result = call.get("result")
        rows = result if isinstance(result, list) else (
            result.get("rows", []) if isinstance(result, dict) else []
        )
        for row in rows:
            if isinstance(row, dict) and row.get("chunk_id"):
                ids.add(row["chunk_id"])
    return ids


def _interaction_was_checked(tool_trace: list[dict]) -> bool:
    return any(call.get("tool") == "check_interactions" for call in (tool_trace or []))


def tool_trace_from_responses_output(output_items: list[dict]) -> list[dict]:
    """Normalize a raw Responses-API `output` list (what
    `app/agent_client.chat()` gets back from `w.api_client.do(...)`) into
    this file's `[{"tool", "args", "result"}, ...]` shape — every
    `function_call`/`function_call_output` pair, matched by `call_id`, not
    just `manage_schedule` (unlike `parse_agent_output()`'s narrower
    extraction, which only needs that one tool for the confirmation-card
    UI). A separate function from `parse_agent_output()` deliberately: that
    function's contract is already established and tested (Task 3.4); this
    one exists purely to feed `check()`, and keeping them separate means
    neither has to become "the one function that does everything."
    """
    import json

    call_args_by_id: dict[str, dict] = {}
    call_name_by_id: dict[str, str] = {}
    trace: list[dict] = []

    for item in output_items:
        if item.get("type") == "function_call":
            call_id = item.get("call_id") or item.get("id")
            call_name_by_id[call_id] = item.get("name")
            try:
                call_args_by_id[call_id] = json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError:
                call_args_by_id[call_id] = {}

        elif item.get("type") == "function_call_output":
            call_id = item.get("call_id")
            try:
                result = json.loads(item.get("output") or "{}")
            except json.JSONDecodeError:
                result = item.get("output")
            trace.append({
                "tool": call_name_by_id.get(call_id),
                "args": call_args_by_id.get(call_id, {}),
                "result": result,
            })

    return trace


def _layer1_candidates(response_text: str, tool_trace: list[dict]) -> list[tuple[str, str]]:
    """Returns [(sentence, pattern_name), ...] for every sentence lacking a
    genuine citation (real or fabricated-detected via `tool_trace`) that
    matches a dosage-instruction candidate pattern."""
    returned_chunks = _returned_chunk_ids(tool_trace)
    interaction_checked = _interaction_was_checked(tool_trace)
    candidates: list[tuple[str, str]] = []

    for sentence in _split_sentences(response_text):
        if _is_escalation_sentence(sentence):
            # Escalation phone numbers/frequencies must not trip the dosage
            # patterns — but ONLY this sentence is exempt, never the whole
            # response (see _is_escalation_sentence's own warning).
            continue
        cited_chunks = set(CHUNK_ID_PATTERN.findall(sentence))
        has_real_label_citation = bool(cited_chunks & returned_chunks)
        has_interaction_citation = (
            bool(INTERACTION_CITATION_PATTERN.search(sentence)) and interaction_checked
        )
        if has_real_label_citation or has_interaction_citation:
            continue  # cited (and not fabricated) — passes Layer 1 regardless of content

        for pattern, name in _LAYER1_PATTERNS:
            if pattern.search(sentence):
                candidates.append((sentence, name))
                break  # one flag per sentence is enough to send it to Layer 2

    return candidates


def _call_layer2_judge(candidates: list[tuple[str, str]]) -> str:
    """One call to the Haiku-tier FM endpoint (requirement 3). Returns the
    raw "YES"/"NO" verdict — "YES" (fail-safe BLOCK) if the response can't
    be parsed as either; see module docstring for why that default is
    BLOCK, not ALLOW."""
    candidate_text = "\n".join(sentence for sentence, _ in candidates)
    try:
        # Import inside the try: a missing/broken databricks_langchain install
        # is a config error and must fail-safe BLOCK like any other judge
        # failure, not raise ImportError up through check().
        from databricks_langchain import ChatDatabricks  # same verified import as agent/agent.py

        llm = ChatDatabricks(endpoint=HAIKU_ENDPOINT)  # no temperature — see module docstring
        result = llm.invoke([
            {"role": "system", "content": JUDGE_PROMPT},
            {"role": "user", "content": candidate_text},
        ])
    except Exception:
        # An unreachable/erroring judge endpoint must fail-safe BLOCK, exactly
        # like an unparseable verdict — NOT propagate. An uncaught exception
        # here would crash agent_client.chat() outright, and (worse) a caller
        # that catches broadly might skip the guardrail entirely. This runs on
        # live user turns: a false BLOCK costs one pharmacist-redirect
        # fallback; an exception path that skips blocking ships unvetted
        # dosage advice. Same asymmetry as the unparseable-output default.
        return "YES"
    raw = (result.content if hasattr(result, "content") else str(result)).strip().upper()
    if raw.startswith("YES"):
        return "YES"
    if raw.startswith("NO"):
        return "NO"
    return "YES"


# ---------------------------------------------------------------------------
# Public entry point (requirement 1)
# ---------------------------------------------------------------------------


def check(response_text: str, tool_trace: list[dict] | None = None) -> GuardrailResult:
    """Runs on every agent response before the app displays it (requirement
    6). `tool_trace` is the `[{"tool", "args", "result"}, ...]` shape —
    `tool_trace_from_responses_output()` builds it from a raw Responses-API
    `output` list. Pure function: no database call happens inside this
    file — the caller (the demo cell below, or `app/agent_client.chat()`)
    is responsible for calling `app.db.log_guardrail_block()` when the
    result is blocked, exactly the same "pure logic vs. I/O" separation
    `agent/tools/manage_schedule.py` already established for
    `validate_payload()`/`needs_user_confirmation()`.
    """
    tool_trace = tool_trace or []

    # No whole-response escalation short-circuit here — escalation handling
    # is per-sentence inside _layer1_candidates (see _is_escalation_sentence).
    candidates = _layer1_candidates(response_text, tool_trace)
    if not candidates:
        return GuardrailResult(allowed=True)

    verdict = _call_layer2_judge(candidates)
    if verdict == "YES":
        return GuardrailResult(
            allowed=False,
            rule_triggered="llm_judge",
            safe_fallback_text=SAFE_FALLBACK_TEXT,
            judge_verdict=verdict,
            layer1_pattern=candidates[0][1],
        )
    return GuardrailResult(allowed=True)


# =============================================================================
# COMMAND ----------
# =============================================================================
# MAGIC %md
# MAGIC ## Demo cell — requirement 7
# MAGIC
# MAGIC Feeds a deliberately bad synthetic response through the guardrail, confirms
# MAGIC it's blocked, logs it via `app.db.log_guardrail_block`, then SELECTs it
# MAGIC back from `guardrail_blocks` — the "safety net catching a bad output" demo
# MAGIC beat. Reproducible on command: run this file directly, or re-run this cell
# MAGIC in a Databricks notebook.
# MAGIC
# MAGIC The bad response is deliberately uncited, imperative, dose-quantity-bearing
# MAGIC dosage advice — exactly the shape Layer 1 exists to catch: "Just take a
# MAGIC double dose tomorrow" matches `_IMPERATIVE_DOSE_RE` ("double" + "dose") with
# MAGIC no citation anywhere in the sentence.

# COMMAND ----------


def run_demo() -> dict:
    """Runs the block -> log -> SELECT-back round trip. Returns the row read
    back from `guardrail_blocks`, so a caller (or a notebook's own assertion
    cell) can check it landed. Imports `app.db` lazily — this keeps
    `agent/guardrail.py`'s own top-level import list free of a Lakebase
    dependency for every other use of this module (the pure `check()` path
    used inside `app/agent_client.chat()` never needs it)."""
    from app.db import log_guardrail_block, _get_pool
    from psycopg.rows import dict_row

    bad_response = (
        "Just take a double dose tomorrow to catch up on what you missed."
    )
    print(f"Feeding through the guardrail: {bad_response!r}")

    result = check(bad_response, tool_trace=[])
    print(f"  Layer 1 candidate pattern: {result.layer1_pattern}")

    if result.allowed:
        raise AssertionError(
            "Demo expected this synthetic response to be BLOCKED — it was "
            "allowed instead. Either Layer 1's patterns or the Layer 2 judge "
            "call changed behavior; this demo cell exists specifically to "
            "catch that regression before a real bad output slips through."
        )

    print(f"  BLOCKED — rule_triggered={result.rule_triggered!r}, "
          f"judge_verdict={result.judge_verdict!r}")
    print(f"  Fallback shown to the user: {result.safe_fallback_text!r}")

    logged = log_guardrail_block(
        model_output_excerpt=bad_response[:500],
        rule_triggered=result.rule_triggered,
        judge_verdict=result.judge_verdict,
        patient_id=None,  # anonymous demo session — DATA_CONTRACTS.md §6.4's documented case
    )
    print(f"  Logged: block_id={logged['block_id']}, ts={logged['ts']}")

    # SELECT it back — the actual demo beat: "here's the safety net catching
    # a bad output," proven by reading the row from the table, not just
    # trusting the insert's own RETURNING clause.
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT block_id, ts, patient_id, model_output_excerpt, "
                "rule_triggered, judge_verdict FROM guardrail_blocks "
                "WHERE block_id = %(block_id)s",
                {"block_id": logged["block_id"]},
            )
            row = dict(cur.fetchone())

    print(f"  Read back from guardrail_blocks: {row}")
    assert row["model_output_excerpt"] == bad_response[:500]
    assert row["rule_triggered"] == "llm_judge"
    assert row["judge_verdict"] == "YES"
    print("  ✓ Round trip confirmed: block -> log -> SELECT-back all match.")
    return row


if __name__ == "__main__":
    run_demo()
