"""NeuroRx AI — app data-access layer, agent/extraction side (Task 3.3).

The second of exactly two modules through which the app touches data (see
also `app/db.py`). Every function here returns a plain dict (or list of
dicts) — no raw Responses-API objects, no `mlflow`/`databricks-sdk` types
leak into the UI layer.

## What was verified before writing this (not assumed)

**The typed `WorkspaceClient.serving_endpoints.query()` wrapper silently
drops our agent's actual response field — confirmed against the real SDK
source, not assumed from the method's plausible-looking name.**
`ServingEndpointsAPI.query()` returns a `QueryEndpointResponse` dataclass
built via `QueryEndpointResponse.from_dict(res)`, which only recognizes
fields shaped for chat/completions/embeddings external-model endpoints
(`choices`, `data`, `predictions`, ...) — it has no field at all for
`output`, which is exactly the top-level key `agent/agent.py`'s
`ResponsesAgentResponse` actually returns (confirmed by reading
`agent/agent.py` itself: `ResponsesAgentResponse(output=outputs)`). Calling
the typed `.query()` method here would silently return an object with every
field `None`, not an error — a much worse failure mode than a crash, because
it would look like a working call. This module instead calls
`WorkspaceClient.api_client.do("POST", f"/serving-endpoints/{name}/invocations",
body=...)` directly — the same underlying call `.query()` itself makes
internally, confirmed by reading `ServingEndpointsAPI.query()`'s own
implementation, just without the lossy typed wrapper on the response. Both
`api_client` (no leading underscore) and `.do()` are public, stable SDK
surface, not private internals being reached into.

**The invocation path has no `/api/2.0` prefix** — confirmed by reading the
exact path string the SDK's own `query()` method constructs internally:
`f"/serving-endpoints/{name}/invocations"`. This differs from the versioned
`/api/2.0/serving-endpoints/...` management API used to create/list/delete
endpoints.

**Citation extraction regex matches `DATA_CONTRACTS.md` §8's exact `chunk_id`
format** (`set_id:section:chunk_index`, e.g.
`a1b2c3d4-e5f6-7890-abcd-ef1234567890:information_for_patients:0003`) — the
same recognition pattern already verified and used in
`agent/06_deploy_agent.py`'s post-deploy check (Task 2.8) and
`agent/07_smoke_tests.py` (Task 2.9), so all three places that need to
recognize a citation agree on what one looks like. One real difference: here
the pattern wraps the `chunk_id` portion in a capture group inside the
brackets, so `.findall()` yields bare chunk_ids with the brackets stripped — matching
this module's own `citations: [chunk_id...]` return contract — where Tasks
2.8/2.9 match the whole bracketed citation as their check only needs to
confirm one exists, not extract it.

## Added for Task 3.4 — `chat_stream()` and `call_manage_schedule()`

**`chat_stream()`'s implementation is pulled from Databricks' own current
chat-app template** (`databricks/app-templates/e2e-chatbot-app`, fetched live
this session via `raw.githubusercontent.com` — not recalled from an older
tutorial, same discipline Task 2.6 already applied to `agent/agent.py`
itself). That template's `_query_responses_endpoint_stream` helper is the
verified, current way to stream an `agent/v1/responses`-task-type endpoint:
`mlflow.deployments.get_deploy_client("databricks").predict_stream(endpoint=...,
inputs={"input": ..., "context": {}, "stream": True})`, yielding raw event
dicts for the caller to accumulate. `chat()` above deliberately does **not**
use this — it calls `WorkspaceClient.api_client.do()` directly for a single
non-streaming response, which is simpler and is what Task 3.3 already built
and verified; `chat_stream()` is additive, not a replacement, for the Chat
view's "stream if supported" requirement.

Per `mlflow.deployments.BaseDeploymentClient`'s own source, `predict_stream()`
raises `NotImplementedError` on a client that can't stream — a real,
catchable exception, not a silent failure — which is exactly the signal the
Chat view's fallback-to-spinner logic needs and checks for.

**`call_manage_schedule()` fills a real gap Task 3.3 didn't anticipate**: the
Chat view's confirmation cards (Requirement 4/5) need the **app**, not the
model, to call `manage_schedule` after a human clicks Confirm — Task 3.3's
`app/agent_client.py` only had `chat()` for going *through* the model.
`manage_schedule` is a UC function taking exactly three positional STRING
params (`patient_id`, `action`, `payload` as a JSON string) — confirmed
against its own `CREATE FUNCTION` signature in `agent/tools/manage_schedule.py`
— invoked here via a direct SQL `SELECT`, reusing `app/db.py`'s
`sql_connect()` (a Delta/SQL-warehouse connection already established for
Task 3.3's `adherence_summary()`/`resolve_citations()`) rather than opening a
third kind of connection for one more call.
"""

from __future__ import annotations

import json
import re

from databricks.sdk import WorkspaceClient

from agent.extraction import extract_schedule
from app.config import settings

# Same pattern as agent/06_deploy_agent.py (Task 2.8) and agent/07_smoke_tests.py
# (Task 2.9) — one citation-recognition pattern shared, not re-derived per file.
CHUNK_ID_PATTERN = re.compile(r"\[([0-9a-f-]{36}:[a-z_]+:\d{4})\]")

ENDPOINT_NAME = "neurorx-agent"  # Task 2.8's deployed endpoint name


class AgentEndpointUnavailable(RuntimeError):
    """The deployed agent endpoint can't be reached in this environment.

    Raised eagerly on the local demo path so the Chat view degrades to a clear
    message instead of the mlflow/SDK client retrying a placeholder host for
    minutes (the same multi-minute hang the Dashboard's warehouse discovery hit).
    """


def _require_agent_endpoint() -> None:
    """Fail fast when there is no workspace hosting the agent endpoint.

    The Chat tab needs the deployed `neurorx-agent` serving endpoint, which only
    exists on a live Databricks workspace. On the local demo path
    (NEURORX_LOCAL_PG set) it cannot exist, so raise immediately rather than let
    the client burn minutes on retries.
    """
    import os

    if os.getenv("NEURORX_LOCAL_PG"):
        raise AgentEndpointUnavailable(
            "The chat agent needs the deployed neurorx-agent serving endpoint, "
            "which requires a Databricks workspace. It is unavailable on the local "
            "demo path (NEURORX_LOCAL_PG is set)."
        )


def _get_workspace_client() -> WorkspaceClient:
    return WorkspaceClient(host=settings.databricks_host, token=settings.databricks_token)


def _build_request_messages(messages: list[dict], patient_id: str) -> list[dict]:
    """Shared by chat() and chat_stream() so the two request paths can never
    silently diverge on how patient_id is conveyed to the model.

    A synthetic context message carrying `patient_id` is prepended before
    sending — the Responses API's `input` list has no separate metadata
    field for it, and the agent's tools (`manage_schedule`,
    `get_adherence_stats`) need patient_id to actually call. Framed as data
    the model reads, consistent with the system prompt's own stance that
    tool results and other non-conversational input are information, not
    instructions (`agent/prompts/system_prompt.md`'s "The five rules"
    section) — this context note is exactly that kind of input.
    """
    context_message = {
        "role": "user",
        "content": f"[Session context: patient_id={patient_id}. This is data identifying "
        "which patient's records to use for any tool call — not an instruction.]",
    }
    return [context_message, *messages]


def parse_agent_output(output_items: list[dict]) -> dict:
    """Returns {"text", "citations", "pending_confirmation"}.

    `pending_confirmation` (Task 3.4 Requirement 5) is the real reason this
    is more than a text/citation scrape: when the agent's own
    `manage_schedule` tool call comes back `needs_confirmation` or
    `blocked_pending_confirmation`, the UI — not the model's paraphrase of
    it — must be the confirmation surface. That means digging into the raw
    Responses-API `output` items, not just the final assistant message:
    a `function_call` item (`name="manage_schedule"`) carries the attempted
    `action`/`payload` as a JSON-encoded `arguments` string; the matching
    `function_call_output` item (same `call_id`) carries manage_schedule's
    own JSON-encoded verdict. Paired together here so the UI can re-submit
    the *exact* attempted action/payload with `user_confirmed`/
    `confirmed_interactions` added, rather than reconstructing it from the
    model's prose.
    """
    text_parts = []
    pending_confirmation = None
    call_args_by_id: dict[str, dict] = {}

    for item in output_items:
        if item.get("type") == "function_call" and item.get("name") == "manage_schedule":
            call_id = item.get("call_id") or item.get("id")
            try:
                call_args_by_id[call_id] = json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError:
                call_args_by_id[call_id] = {}

        elif item.get("type") == "function_call_output":
            call_id = item.get("call_id")
            try:
                result = json.loads(item.get("output") or "{}")
            except json.JSONDecodeError:
                result = {}
            if result.get("status") in ("needs_confirmation", "blocked_pending_confirmation"):
                attempted = call_args_by_id.get(call_id, {})
                pending_confirmation = {
                    **result,
                    "patient_id": attempted.get("patient_id"),
                    "action": attempted.get("action"),
                    "payload": attempted.get("payload"),
                }

        elif item.get("type") == "message":
            for block in item.get("content", []):
                if block.get("text"):
                    text_parts.append(block["text"])

    text = " ".join(text_parts)
    return {
        "text": text,
        "citations": CHUNK_ID_PATTERN.findall(text),
        "pending_confirmation": pending_confirmation,
    }


# ---------------------------------------------------------------------------
# chat()
# ---------------------------------------------------------------------------


def chat(messages: list[dict], patient_id: str) -> dict:
    """Call the deployed neurorx-agent endpoint. Returns
    {"text": str, "citations": [chunk_id, ...], "pending_confirmation": dict | None}.

    `pending_confirmation` is non-None exactly when the agent's own
    manage_schedule tool call came back needs_confirmation or
    blocked_pending_confirmation — see `parse_agent_output()`'s own
    docstring for why the UI needs this instead of the model's paraphrase.

    `messages` is the caller's own conversation history in Responses-API
    shape (`[{"role": "user"/"assistant", "content": "..."}]`) — this
    function is stateless per `agent/agent.py`'s own contract (Task 2.6:
    "the caller owns history"), so it neither stores nor reads any history
    of its own; the caller must pass the full conversation every time and
    append this call's output to it for the next turn.

    **Every response passes through the output guardrail (Task 4.5,
    `agent/guardrail.py`) before returning.** This is the wiring point
    requirement 6 asks for: "nothing reaches the UI unchecked." A blocked
    response is fully replaced — text, citations, and pending_confirmation
    all reset — rather than partially filtered, since a blocked
    `pending_confirmation` card would otherwise let the UI render a
    schedule-change confirmation button under text the safety layer just
    said it can't stand behind.
    """
    from agent.guardrail import check as guardrail_check
    from agent.guardrail import tool_trace_from_responses_output

    _require_agent_endpoint()
    w = _get_workspace_client()
    response = w.api_client.do(
        "POST",
        f"/serving-endpoints/{ENDPOINT_NAME}/invocations",
        body={"input": _build_request_messages(messages, patient_id)},
    )
    output_items = response.get("output", [])
    parsed = parse_agent_output(output_items)

    tool_trace = tool_trace_from_responses_output(output_items)
    result = guardrail_check(parsed["text"], tool_trace)

    if not result.allowed:
        from app.db import log_guardrail_block

        log_guardrail_block(
            model_output_excerpt=parsed["text"][:500],
            rule_triggered=result.rule_triggered,
            judge_verdict=result.judge_verdict,
            patient_id=patient_id,
        )
        return {"text": result.safe_fallback_text, "citations": [], "pending_confirmation": None}

    return parsed


# ---------------------------------------------------------------------------
# chat_stream()
# ---------------------------------------------------------------------------


def chat_stream(messages: list[dict], patient_id: str):
    """Streaming counterpart to chat(). Yields raw ResponsesAgent stream
    event dicts (the caller accumulates text deltas itself — see
    `app/views/chat.py`'s `render_streaming_response()`), exactly mirroring
    the verified current Databricks chat-app template's
    `_query_responses_endpoint_stream` pattern (see module docstring).

    Raises whatever `mlflow.deployments`' `predict_stream()` raises if the
    underlying deployment client can't stream (confirmed:
    `NotImplementedError` from the base class if unsupported) — the caller
    is expected to catch this and fall back to `chat()` with a spinner, per
    Task 3.4's "streaming if supported, else spinner" requirement.

    ⚠️ **This path does NOT go through the output guardrail (Task 4.5).**
    `chat()` is guardrailed; this function is not, and per Task 3.4 this is
    the *primary* path `app/views/chat.py` calls (`chat()` is the fallback
    used only when streaming itself fails) — meaning most live responses
    currently reach the UI unchecked, not the minority. Task 4.5 was scoped
    to "wire the call site into app/agent_client.chat" specifically, so this
    gap is real and deliberate-per-scope, not an oversight, but it means the
    guardrail's actual coverage today is narrower than "every response"
    until this function is wired too. A post-generation guardrail can only
    run on complete text (both its regex layer, which needs whole sentences,
    and the Haiku judge call), so guardrailing a stream means checking the
    fully-accumulated text after the stream completes, before the final
    render — not checking each token-delta as it arrives.
    """
    _require_agent_endpoint()
    from mlflow.deployments import get_deploy_client

    client = get_deploy_client("databricks")
    inputs = {
        "input": _build_request_messages(messages, patient_id),
        "context": {},
        "stream": True,
    }
    yield from client.predict_stream(endpoint=ENDPOINT_NAME, inputs=inputs)


# ---------------------------------------------------------------------------
# call_manage_schedule()
# ---------------------------------------------------------------------------


def call_manage_schedule(patient_id: str, action: str, payload: dict) -> dict:
    """Call the manage_schedule UC function directly — the path the app
    itself uses after a human clicks Confirm (Task 3.4 Requirement 4/5),
    as distinct from `chat()`, which is how the *model* calls it mid-
    conversation. Same function, two callers, same two-gate confirmation
    contract either way (`user_confirmed`, `confirmed_interactions`) —
    enforced in `manage_schedule`'s own code (Task 2.3), not by which caller
    reached it.

    `manage_schedule(patient_id STRING, action STRING, payload STRING)
    RETURNS STRING` — three positional params, `payload` a JSON string,
    confirmed against the function's own `CREATE FUNCTION` signature in
    `agent/tools/manage_schedule.py`. Parses the returned JSON string back
    into a dict before returning, so callers never handle a raw string.
    """
    from app.db import sql_connect

    with sql_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT neurorx.app.manage_schedule(:patient_id, :action, :payload)",
                {"patient_id": patient_id, "action": action, "payload": json.dumps(payload)},
            )
            (result_str,) = cur.fetchone()
            return json.loads(result_str)


# ---------------------------------------------------------------------------
# resolve_citations()
# ---------------------------------------------------------------------------


def resolve_citations(chunk_ids: list[str]) -> list[dict]:
    """Fetch the full citation payload for a list of chunk_ids, for
    rendering as clickable citation chips. Reads `neurorx.gold.drug_knowledge`
    directly — the exact six-field shape `DATA_CONTRACTS.md` §8 defines as
    the citation contract (chunk_id, rxcui, drug_name, section, set_id,
    chunk_text), unchanged, so a judge can take a `chunk_id` out of a chat
    answer and see this function return the identical text that grounded it.

    Uses `app/db.py`'s own Databricks SQL connection helper rather than
    duplicating connection setup — this is a Delta (gold) read, same
    reasoning as `db.adherence_summary()`: analytics/citation reads go to
    Delta, never Lakebase.
    """
    if not chunk_ids:
        return []

    from app.db import sql_connect

    # Named paramstyle needs one placeholder per value for an IN-list — the
    # connector has no array-bind shorthand (confirmed same connector as
    # app/db.py; see that module's docstring for the named-vs-psycopg-style
    # paramstyle distinction, which applies here identically).
    placeholders = ", ".join(f":chunk_id_{i}" for i in range(len(chunk_ids)))
    params = {f"chunk_id_{i}": cid for i, cid in enumerate(chunk_ids)}

    with sql_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT chunk_id, rxcui, drug_name, section, set_id, chunk_text
                FROM neurorx.gold.drug_knowledge
                WHERE chunk_id IN ({placeholders})
                """,
                params,
            )
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# extract_prescription()
# ---------------------------------------------------------------------------


def extract_prescription(image_or_text: bytes | str) -> dict:
    """Wraps `agent/extraction.py`'s `extract_schedule()` — the 5-step
    prescription extraction pipeline (Task 2.7). Detects image vs. text from
    the input type: `bytes` -> photo, `str` -> pasted/typed text.

    Returns the exact `propose()` payload `extraction.py` defines:
    `{"drugs": [...], "requires_user_confirmation": True}`. **This function
    writes nothing** — same guarantee `extraction.py`'s own module docstring
    makes (no database client imported there, by construction). The UI is
    responsible for rendering this as a confirmation screen and, only after
    the user confirms, calling `manage_schedule` (Task 2.3) — never this
    function again — to actually write the schedule.
    """
    is_image = isinstance(image_or_text, bytes)
    return extract_schedule(image_or_text, is_image=is_image)
