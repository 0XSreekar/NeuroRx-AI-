"""NeuroRx AI — supervisor agent (Task 2.6).

Single-agent, four Unity Catalog function tools, MLflow-traced end to end. This
file is a Models-from-Code MLflow model: importing it defines `AGENT` and calls
`mlflow.models.set_model(AGENT)`, exactly like `agent/tools/*` are UC functions
rather than a library `agent.py` is a *model*, meant to be pointed to by
`mlflow.pyfunc.log_model(python_model="agent/agent.py", ...)` — see
`agent/log_agent.py` for that logging step, kept in a separate file because that
is the documented pattern (the file that defines a Models-from-Code model does
not log itself; a separate driver does, referencing this file by path).

## What was actually verified before writing this, and how

Per the task's own instruction not to guess the authoring interface: every
non-trivial import and call pattern below was checked against currently-live
sources this session, not recalled from training data.

- **`mlflow.pyfunc.ResponsesAgent` over `ChatAgent`** — confirmed via the current
  Databricks docs (`generative-ai/agent-framework/author-agent`, fetched live):
  "Databricks recommends MLflow `ResponsesAgent` to build agents"; `ChatAgent` is
  documented as the legacy schema.
- **`databricks_langchain.ChatDatabricks` + `UCFunctionToolkit` + LangChain's
  `create_agent`** — this exact combination, including the `system_prompt=`
  kwarg, is what Databricks' own actively-maintained template uses today:
  `databricks/app-templates/agent-langgraph/agent_server/agent.py`, fetched live
  from GitHub this session (not from memory of an older tutorial).
- **The stream-event conversion logic** (`_process_agent_stream_events` below)
  is adapted line-for-line from that same live template's
  `agent_server/utils.py::process_agent_astream_events` — the exact
  `stream_mode=["updates", "messages"]` shape, the `ToolMessage` content
  stringification, and `output_to_responses_items_stream` / `create_text_delta`
  usage all come from real, current, running Databricks code, not invented glue.
  The one change made is sync instead of async (`.stream()` instead of
  `.astream()`), since nothing else in this file needs to be async and the
  `__main__` REPL is far simpler synchronous.
- **`mlflow.pyfunc.log_model(..., resources=[DatabricksFunction(...),
  DatabricksServingEndpoint(...)])`** — confirmed against current Databricks docs
  (`log-model-dependencies` family of pages).
- **pip package versions below are the actual current PyPI releases**, fetched
  live via the PyPI JSON API this session, not guessed round numbers.

## ⚠️ One requirement this file deliberately does NOT satisfy as written, and why

Task 2.6 asked for `temperature 0.1`. **`CLAUDE.md` already carries a
project-verified fact, re-confirmed live this session:** the Databricks FM API's
Claude Sonnet 5 endpoint (`settings.fm_chat_endpoint`, per CLAUDE.md's
non-negotiables table — the model this agent actually points at) **rejects
`temperature`/`top_p`/`top_k` with a hard 400.** Passing it here would not
produce a slightly-off agent; it would make every single request fail. This is
flagged, not silently resolved, per this project's own standing rule (CLAUDE.md
§6: "flag document conflicts; never silently resolve them"). `TOOL_CALL_TEMPERATURE`
is kept as a named constant so the intent (favor deterministic tool routing) is
visible and the value is ready to use the moment `LLM_ENDPOINT` points at a model
that accepts it — but it is **not** passed to `ChatDatabricks` below.

## The "swap models in one line" constant

`LLM_ENDPOINT` is the one line to change. It is not a bare string in this file —
it is read from `app/config.py`'s `settings.fm_chat_endpoint`, which in turn comes
from the `FM_CHAT_ENDPOINT` environment variable, so swapping models in
production is a config change, not a code change. The single in-code constant
exists so a demo can point at it and say "one line."
"""

import json
import logging
from pathlib import Path
from typing import Any, Generator, Iterator

import mlflow
from databricks_langchain import ChatDatabricks, UCFunctionToolkit
from langchain.agents import create_agent
from langchain.messages import AIMessageChunk, ToolMessage
from mlflow.pyfunc import ResponsesAgent
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
    create_text_delta,
    output_to_responses_items_stream,
    to_chat_completions_input,
)

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Autologging, enabled at import time so every call this module ever makes —
# from the local REPL below, from an eval run, or from a served endpoint — is
# traced with no per-call opt-in. This is the exact call and placement used in
# Databricks' own current agent template (verified live, see module docstring).
# ---------------------------------------------------------------------------
mlflow.langchain.autolog()
logging.getLogger("mlflow.utils.autologging_utils").setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

# The one line to change to swap models (see module docstring). Resolves to
# `databricks-claude-sonnet-5` today per CLAUDE.md's non-negotiables table.
LLM_ENDPOINT = settings.fm_chat_endpoint

# Requested by Task 2.6, intentionally NOT passed to ChatDatabricks below —
# see the "⚠️" section in the module docstring. Kept as a named constant so the
# unmet intent (favor deterministic tool routing over creative phrasing) stays
# visible rather than silently vanishing, and is one line to wire up if
# LLM_ENDPOINT ever points at a model that accepts sampling parameters.
TOOL_CALL_TEMPERATURE = 0.1

# The four UC functions from Tasks 2.1-2.4, per CLAUDE.md's non-negotiables
# table (`neurorx.app.*`). Built from `settings.catalog` rather than hardcoded
# so a non-default catalog name (e.g. a per-developer sandbox catalog) doesn't
# require editing this file.
UC_TOOL_NAMES = [
    f"{settings.catalog}.app.check_interactions",
    f"{settings.catalog}.app.search_drug_labels",
    f"{settings.catalog}.app.manage_schedule",
    f"{settings.catalog}.app.get_adherence_stats",
]

# Task 2.6 asks for "tool max-iterations 6". LangGraph's `recursion_limit`
# (the actual enforcement knob — confirmed via LangGraph's own current docs)
# counts graph super-steps, not raw tool calls: the ReAct-style graph built by
# `create_agent` alternates a model-turn and a tool-turn per round, so N tool
# calls costs 2N steps, plus one final model-turn to produce the answer with
# no further tool calls. 6 tool calls -> 2*6 + 1 = 13 super-steps. Setting this
# to 6 directly would silently cut off after 3 tool calls, not 6.
MAX_TOOL_ITERATIONS = 6
AGENT_RECURSION_LIMIT = 2 * MAX_TOOL_ITERATIONS + 1

# Loaded once at import time so a bad prompt file fails at startup, not on the
# first request.
SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "system_prompt.md"


def _load_system_prompt() -> str:
    """Load the prompt body from system_prompt.md — never duplicate it in code.

    Per that file's own header contract: everything between the first `---`
    (end of the file's header blockquote) and the `---` before the Appendix is
    the prompt the model actually receives. The Appendix is judge-facing
    rationale and must never reach the model.
    """
    raw = SYSTEM_PROMPT_PATH.read_text()
    if "\n---\n" not in raw:
        raise ValueError(
            f"{SYSTEM_PROMPT_PATH} is missing the expected '---' header delimiter "
            "— cannot safely locate where the prompt body starts."
        )
    after_header = raw.split("\n---\n", 1)[1]
    if "\n---\n" not in after_header:
        raise ValueError(
            f"{SYSTEM_PROMPT_PATH} is missing the expected '---' delimiter before "
            "the Appendix — cannot safely exclude judge-facing rationale from the "
            "prompt actually sent to the model."
        )
    body = after_header.split("\n---\n", 1)[0].strip()
    if "Appendix" in body:
        raise ValueError(
            f"{SYSTEM_PROMPT_PATH}: the Appendix leaked into the extracted prompt "
            "body — refusing to send judge-facing rationale to the model."
        )
    if not body.startswith("## Identity"):
        raise ValueError(
            f"{SYSTEM_PROMPT_PATH}: extracted body does not start with '## Identity' "
            f"— got {body[:40]!r}. The file's structure may have changed."
        )
    return body


SYSTEM_PROMPT = _load_system_prompt()


def _build_llm() -> ChatDatabricks:
    # No `temperature=` — see the module docstring's ⚠️ section.
    return ChatDatabricks(endpoint=LLM_ENDPOINT)


def _build_tools() -> list:
    toolkit = UCFunctionToolkit(function_names=UC_TOOL_NAMES)
    return toolkit.tools


def _build_compiled_graph():
    """Build the LangGraph ReAct-style agent. Stateless: the caller owns history
    (Task 2.6 requirement 6) — nothing here persists conversation state, and a
    fresh call to this function would produce an agent with no memory of past
    turns, which is exactly right, since it is called once per process, not
    once per request."""
    return create_agent(
        model=_build_llm(),
        tools=_build_tools(),
        system_prompt=SYSTEM_PROMPT,
    )


def _process_agent_stream_events(
    stream: Iterator[Any],
) -> Generator[ResponsesAgentStreamEvent, None, None]:
    """Convert a LangGraph `stream_mode=["updates", "messages"]` stream into
    ResponsesAgentStreamEvent objects.

    Adapted directly from Databricks' own currently-maintained template
    (`databricks/app-templates/agent-langgraph/agent_server/utils.py`,
    `process_agent_astream_events`, fetched live this session — see module
    docstring). The only change is sync (`stream`) instead of async
    (`astream`); the conversion logic is otherwise unchanged.

    "updates" events carry each node's newly-produced messages -- converted
    to complete Responses output items once the node is done. "messages"
    events carry token-by-token deltas from the model -- converted to
    incremental text-delta events for anyone consuming this as a live stream.
    """
    for event in stream:
        mode, payload = event
        if mode == "updates":
            for node_data in payload.values():
                messages = node_data.get("messages", [])
                if not messages:
                    continue
                for msg in messages:
                    # UC tool results normally arrive as JSON strings already
                    # (per each tool's own COMMENT contract), but a ToolMessage
                    # can carry non-string content depending on the toolkit
                    # version -- stringify defensively, matching the verified
                    # template exactly, since a non-string content field is not
                    # valid Responses API output.
                    if isinstance(msg, ToolMessage) and not isinstance(msg.content, str):
                        msg.content = json.dumps(msg.content)
                yield from output_to_responses_items_stream(messages)
        elif mode == "messages":
            try:
                chunk = payload[0]
                if isinstance(chunk, AIMessageChunk) and (content := chunk.content):
                    yield ResponsesAgentStreamEvent(
                        **create_text_delta(delta=content, item_id=chunk.id)
                    )
            except Exception:
                logger.exception("Error processing an agent stream event")


class NeuroRxAgent(ResponsesAgent):
    """The NeuroRx AI supervisor agent.

    Stateless (Task 2.6 requirement 6): the compiled graph is built once at
    construction and carries no per-conversation state of its own. Every
    `predict`/`predict_stream` call receives the full message history from the
    caller via `request.input` and returns only the new turn's output items —
    the caller (the Databricks App backing the Chat view) is responsible for
    appending those to its own stored history and re-sending the whole thing
    on the next call.
    """

    def __init__(self) -> None:
        self._agent = _build_compiled_graph()

    @mlflow.trace(name="neurorx_agent_predict", span_type="AGENT")
    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        outputs = [
            event.item
            for event in self.predict_stream(request)
            if event.type == "response.output_item.done"
        ]
        return ResponsesAgentResponse(output=outputs)

    def predict_stream(
        self, request: ResponsesAgentRequest
    ) -> Generator[ResponsesAgentStreamEvent, None, None]:
        messages = {
            "messages": to_chat_completions_input([i.model_dump() for i in request.input])
        }
        stream = self._agent.stream(
            messages,
            stream_mode=["updates", "messages"],
            config={"recursion_limit": AGENT_RECURSION_LIMIT},
        )
        yield from _process_agent_stream_events(stream)


# Instantiated once at import time and registered as the Models-from-Code
# entry point. `mlflow.pyfunc.log_model(python_model=__file__, ...)` (see
# `agent/log_agent.py`) re-executes this module in an isolated environment and
# picks up whatever `set_model` was called with.
AGENT = NeuroRxAgent()
mlflow.models.set_model(AGENT)


if __name__ == "__main__":
    # Local REPL for manual testing before deployment (Task 2.6 requirement 7).
    # Requires a populated `.env` — see `.env.example` and CLAUDE.md's
    # environment gotchas; app/config.py fails loudly at import if anything
    # required is missing, which is exactly what should happen here too.
    print("NeuroRx AI — local REPL. Type 'exit' or Ctrl-D to quit.\n")

    history: list[dict] = []
    while True:
        try:
            user_text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if user_text.lower() in {"exit", "quit"}:
            break
        if not user_text:
            continue

        history.append({"role": "user", "content": user_text})
        request = ResponsesAgentRequest(input=history)
        response = AGENT.predict(request)

        for item in response.output:
            if item.get("type") == "message":
                for block in item.get("content", []):
                    text = block.get("text")
                    if text:
                        print(f"agent> {text}")
            elif item.get("type") == "function_call":
                print(f"  [tool call] {item.get('name')}({item.get('arguments')})")
            elif item.get("type") == "function_call_output":
                print(f"  [tool result] {item.get('output')}")

        history.extend(response.output)
