# Databricks notebook source
# MAGIC %md
# MAGIC # Deploy the NeuroRx AI supervisor agent (Task 2.8)
# MAGIC
# MAGIC Logs the agent (`agent/agent.py`, Task 2.6), registers it to Unity Catalog as
# MAGIC `neurorx.app.neurorx_agent` aliased `@champion`, deploys it to a serverless
# MAGIC Model Serving endpoint named `neurorx-agent`, and verifies the deployment
# MAGIC actually answers a real question with a citation and a traced tool call.
# MAGIC
# MAGIC Run this after `agent/log_agent.py`'s dependencies are in place — i.e. after
# MAGIC Phase 1 has produced live data and all four UC functions (Tasks 2.1–2.4)
# MAGIC exist in the workspace. **Not run against a live workspace in this session**
# MAGIC — see the verification notes below for what was and wasn't checked live.

# COMMAND ----------

# MAGIC %md
# MAGIC ## What was verified live this session, and how
# MAGIC
# MAGIC Per this project's own standing rule (CLAUDE.md §6: verify external API facts
# MAGIC live, don't guess): the exact current signature of `databricks.agents.deploy()`
# MAGIC was **not** taken from a doc summary — a doc-summarizing tool paraphrased its
# MAGIC `scale_to_zero_enabled` parameter, which turned out to be wrong. Instead, the
# MAGIC actual current `databricks-agents==1.11.0` wheel was downloaded from PyPI this
# MAGIC session and its real source inspected directly:
# MAGIC
# MAGIC ```python
# MAGIC def deploy(
# MAGIC     model_name: str,
# MAGIC     model_version: int,
# MAGIC     scale_to_zero: bool = False,          # NOT "scale_to_zero_enabled" — a doc
# MAGIC                                            # summary got this wrong; the real
# MAGIC                                            # wheel source is what's used here.
# MAGIC     environment_vars: Dict[str, str] = None,
# MAGIC     instance_profile_arn: str = None,
# MAGIC     tags: Dict[str, str] = None,
# MAGIC     workload_size: str = "Small",
# MAGIC     endpoint_name: str = None,
# MAGIC     budget_policy_id: str = None,          # DEPRECATED, use usage_policy_id
# MAGIC     description: Optional[str] = None,
# MAGIC     deploy_feedback_model: bool = False,
# MAGIC     usage_policy_id: str = None,
# MAGIC     **kwargs,
# MAGIC ) -> Deployment
# MAGIC ```
# MAGIC
# MAGIC The returned `Deployment` exposes `.endpoint_name`, `.query_endpoint` (the full
# MAGIC invocations URL), `.endpoint_url` (the workspace UI page), and
# MAGIC `.review_app_url` — confirmed by reading `_construct_query_endpoint()` and the
# MAGIC library's own post-deploy print statement in `deployments.py`, not guessed.
# MAGIC
# MAGIC Also verified live: `mlflow.models.resources.DatabricksVectorSearchIndex` takes
# MAGIC `index_name`, `DatabricksLakebase` takes `database_instance_name`, and
# MAGIC `DatabricksSQLWarehouse` takes `warehouse_id` (all via search against current
# MAGIC MLflow source); the `{{secrets/scope/key}}` template syntax for referencing a
# MAGIC secret as a serving `environment_vars` value (current Databricks docs); and
# MAGIC `MlflowClient.set_registered_model_alias(name, alias, version)` as the current,
# MAGIC non-deprecated way to set a UC model alias.
# MAGIC
# MAGIC ## ⚠️ A real deployment blocker inherited from Task 2.6, surfaced here
# MAGIC
# MAGIC `agent/agent.py` does `from app.config import settings` at module import time,
# MAGIC and `app/config.py`'s `_load_settings()` requires **all nine** of its env vars
# MAGIC (including `LAKEBASE_HOST`/`DB`/`USER`/`PASSWORD`) just to import successfully —
# MAGIC even though the agent itself only ever reads `settings.fm_chat_endpoint` and
# MAGIC `settings.catalog`. This is the exact anti-pattern CLAUDE.md already flags for
# MAGIC Lakeflow pipeline files ("don't import app/config.py into a pipeline file: that
# MAGIC module requires all 9 of its env vars... just to resolve one endpoint name —
# MAGIC the wrong coupling") — it was written into the agent in Task 2.6 and not caught
# MAGIC until this deployment notebook needed to actually populate the served
# MAGIC container's environment. **Not silently fixed here** — refactoring
# MAGIC `agent.py`/`app/config.py` is a Task 2.6-shaped change, out of scope for a
# MAGIC deploy notebook, and risky to do as a drive-by edit. The practical
# MAGIC consequence, handled below: `environment_vars=` on `agents.deploy()` must carry
# MAGIC all nine vars — including a Lakebase password the agent never uses — via secret
# MAGIC references, never plaintext. Flagged as a cleanup item for a future Task 2.6
# MAGIC revision: scope `app/config.py` access in `agent.py` down to only the fields it
# MAGIC actually reads, or split `Settings` so the agent doesn't need to resolve
# MAGIC Lakebase credentials just to find its own FM endpoint name.

# COMMAND ----------

# MAGIC %pip install databricks-agents==1.11.0 mlflow==3.14.0 databricks-sdk==0.121.0
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import time

import mlflow
from databricks.sdk import WorkspaceClient
from mlflow import MlflowClient

from agent.log_agent import INPUT_EXAMPLE, log_agent

CATALOG = "neurorx"
UC_MODEL_NAME = f"{CATALOG}.app.neurorx_agent"
ALIAS = "champion"
ENDPOINT_NAME = "neurorx-agent"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — Log and register to Unity Catalog, alias `@champion`

# COMMAND ----------

mlflow.set_registry_uri("databricks-uc")

model_uri = log_agent()  # agent/log_agent.py, Task 2.6 — resources/pip/input_example

registered_model_info = mlflow.register_model(model_uri=model_uri, name=UC_MODEL_NAME)
model_version = registered_model_info.version

client = MlflowClient()
client.set_registered_model_alias(name=UC_MODEL_NAME, alias=ALIAS, version=model_version)

print(f"Registered {UC_MODEL_NAME} version {model_version}, aliased @{ALIAS}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — Deploy to a serverless endpoint
# MAGIC
# MAGIC `scale_to_zero=True` for cost — this is a hackathon demo, not a production
# MAGIC SLA, and Free Edition has no dedicated budget for an always-on endpoint. The
# MAGIC trade, stated plainly for demo day: a cold-started request after idle time
# MAGIC incurs a real latency spike (Databricks' own docs: "capacity is not
# MAGIC guaranteed when scaled to zero... additional latency (cold start)"). **Do the
# MAGIC one warm-up call in Step 4 a few minutes before showing this to judges** —
# MAGIC don't let the very first request of the demo be the cold-start one.
# MAGIC
# MAGIC `environment_vars` uses `{{secrets/scope/key}}` references, never plaintext —
# MAGIC per the coupling issue flagged above, all nine of `app/config.py`'s required
# MAGIC vars must be present for `agent.py` to import inside the served container, not
# MAGIC just the ones the agent logic actually touches.

# COMMAND ----------

SECRET_SCOPE = "neurorx"  # create with `databricks secrets create-scope neurorx` first

ENVIRONMENT_VARS = {
    "DATABRICKS_HOST": "{{secrets/" + SECRET_SCOPE + "/databricks_host}}",
    "DATABRICKS_TOKEN": "{{secrets/" + SECRET_SCOPE + "/databricks_token}}",
    "VECTOR_SEARCH_ENDPOINT": "neurorx-vs",
    "FM_CHAT_ENDPOINT": "databricks-claude-sonnet-5",
    "FM_GUARDRAIL_ENDPOINT": "databricks-claude-haiku-4-5",
    # Unused by the agent itself (see the ⚠️ note above) — present only because
    # app/config.py's _load_settings() fails the whole import without them.
    "LAKEBASE_HOST": "{{secrets/" + SECRET_SCOPE + "/lakebase_host}}",
    "LAKEBASE_DB": "{{secrets/" + SECRET_SCOPE + "/lakebase_db}}",
    "LAKEBASE_USER": "{{secrets/" + SECRET_SCOPE + "/lakebase_user}}",
    "LAKEBASE_PASSWORD": "{{secrets/" + SECRET_SCOPE + "/lakebase_password}}",
}

# COMMAND ----------

from databricks import agents  # noqa: E402 (after %pip install + restartPython)

deployment = agents.deploy(
    model_name=UC_MODEL_NAME,
    model_version=model_version,
    endpoint_name=ENDPOINT_NAME,
    scale_to_zero=True,
    environment_vars=ENVIRONMENT_VARS,
    tags={"project": "neurorx-ai", "phase": "2"},
    description="NeuroRx AI supervisor agent — medication schedule assistant.",
)

print(f"Deploying {UC_MODEL_NAME} v{model_version} to endpoint {deployment.endpoint_name}")
print(f"Status page: {deployment.endpoint_url}")
print(f"Review App:  {deployment.review_app_url}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — Resources and permissions, enumerated explicitly
# MAGIC
# MAGIC Per the current agent-deployment docs, resource access is declared at
# MAGIC **log time**, not deploy time — `agents.deploy()` itself takes no `resources=`
# MAGIC parameter (confirmed against the real `deploy()` signature above). The
# MAGIC declarations live in `agent/log_agent.py::build_resources()` (extended for
# MAGIC this task) and cover every resource this task asks to enumerate:
# MAGIC
# MAGIC | Resource | Declared as | Covers |
# MAGIC |---|---|---|
# MAGIC | FM chat endpoint | `DatabricksServingEndpoint` | The LLM the agent calls directly |
# MAGIC | 4 UC functions | `DatabricksFunction` × 4 | `check_interactions`, `search_drug_labels`, `manage_schedule`, `get_adherence_stats` |
# MAGIC | Vector index | `DatabricksVectorSearchIndex` | `neurorx.gold.drug_knowledge_index` — see the ⚠️ below |
# MAGIC | Lakebase | `DatabricksLakebase` | `neurorx-oltp` — see the ⚠️ below |
# MAGIC | SQL warehouse | `DatabricksSQLWarehouse` | The one Free Edition warehouse, discovered live, not hardcoded |
# MAGIC
# MAGIC ⚠️ **Read `log_agent.py`'s module docstring before assuming these three
# MAGIC additions "wire up" Vector Search / Lakebase access.** They don't — per
# MAGIC Tasks 2.2 and 2.3's own already-verified findings, `search_drug_labels` and
# MAGIC `manage_schedule` authenticate to those services via their own OAuth
# MAGIC service-principal env vars, not resource-based passthrough (UC Python
# MAGIC functions have no verified in-sandbox mechanism for that). These
# MAGIC declarations are the correct, current governance/lineage practice — but the
# MAGIC actual auth for those two tools is configured separately, on the UC
# MAGIC functions themselves.
# MAGIC
# MAGIC **Human access to the endpoint / review app** (as opposed to the endpoint's
# MAGIC own service-principal resource access, above) is a separate, later step via
# MAGIC `agents.set_permissions(UC_MODEL_NAME, users=[...], permission_level=...)` —
# MAGIC not run here since this notebook has no real reviewer email list to grant;
# MAGIC left as a TODO for whoever runs this against a live workspace with an actual
# MAGIC judge/teammate list.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — Post-deploy verification
# MAGIC
# MAGIC Waits for the endpoint to come up, asks the Phase 1 exit-checkpoint-adjacent
# MAGIC question ("I missed my 8 AM lisinopril, what should I do?"), and asserts:
# MAGIC (a) the response contains at least one `[chunk_id]`-shaped citation
# MAGIC (`search_drug_labels`'s citation form, per `agent/prompts/system_prompt.md`'s
# MAGIC Citations section), and (b) MLflow recorded a trace for the request with at
# MAGIC least one tool-call span — i.e. this didn't answer from the model's own
# MAGIC knowledge, per the safety spine (CLAUDE.md §1).

# COMMAND ----------

import re

import mlflow.deployments
from databricks.sdk.service.serving import EndpointStateReady

deploy_client = mlflow.deployments.get_deploy_client("databricks")

TEST_QUESTION = "I missed my 8 AM lisinopril, what should I do?"

# Endpoint readiness can take up to ~15 minutes per agents.deploy()'s own
# printed message — poll rather than assume it's already serving.
# `state.ready` is an `EndpointStateReady` enum (plain Enum, not `str, Enum` —
# confirmed against the real databricks-sdk source), so it must be compared
# against the enum member itself; `str(state.ready) == "READY"` would render
# as "EndpointStateReady.READY" and never match.
w = WorkspaceClient()
for attempt in range(30):
    state = w.serving_endpoints.get(ENDPOINT_NAME).state
    if state and state.ready == EndpointStateReady.READY:
        break
    print(f"  endpoint not ready yet (attempt {attempt + 1}/30) — waiting 30s")
    time.sleep(30)
else:
    raise TimeoutError(f"Endpoint {ENDPOINT_NAME} did not become ready in time.")

response = deploy_client.predict(
    endpoint=ENDPOINT_NAME,
    inputs={"input": [{"role": "user", "content": TEST_QUESTION}]},
)

# Pull all assistant text out of the Responses-API-shaped output items.
response_text = " ".join(
    block.get("text", "")
    for item in response.get("output", [])
    if item.get("type") == "message"
    for block in item.get("content", [])
)

CHUNK_ID_PATTERN = re.compile(r"\[[0-9a-f-]{36}:[a-z_]+:\d{4}\]")
citations = CHUNK_ID_PATTERN.findall(response_text)
assert citations, (
    f"Expected at least one [chunk_id] citation in the response, found none. "
    f"Full response text: {response_text!r}"
)
print(f"PASS (a): {len(citations)} citation(s) found — {citations}")

# (b) A trace exists for this request with a tool-call span. The served
# endpoint logs traces to the experiment agents.deploy() wires up for this
# model version — search recent traces and confirm at least one span names a
# UC function this agent could have called. `return_type="list"` gives real
# `Trace` objects (confirmed shape: `Trace.data.spans: list[Span]`,
# `Span.name`) rather than depending on a guessed pandas column name — no
# filter_string needed since recency + max_results=1 already isolates the
# request just made above.
traces = mlflow.search_traces(
    max_results=1,
    order_by=["timestamp_ms DESC"],
    return_type="list",
)
assert traces, "Expected at least one recent trace, found none."

latest_trace = traces[0]
span_names = {span.name for span in latest_trace.data.spans}
tool_span_present = any(
    any(tool in name for tool in
        ["search_drug_labels", "check_interactions", "manage_schedule", "get_adherence_stats"])
    for name in span_names
)
assert tool_span_present, (
    f"Expected a tool-call span for one of the four UC functions, got spans: {span_names}. "
    "If the agent answered without calling search_drug_labels, that's a Rule 1 "
    "violation (system_prompt.md) — clinical facts must come from a tool, never "
    "the model's own knowledge."
)
print(f"PASS (b): trace found with tool-call span(s): {span_names}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 — Endpoint URL and curl example for the app

# COMMAND ----------

print(f"Endpoint name:  {deployment.endpoint_name}")
print(f"Query endpoint: {deployment.query_endpoint}")
print(f"Status page:    {deployment.endpoint_url}")
print(f"Review App:     {deployment.review_app_url}")
print()
print("curl example (the app should use this shape):")
print(
    f"""
curl -X POST '{deployment.query_endpoint}' \\
  -H 'Authorization: Bearer $DATABRICKS_TOKEN' \\
  -H 'Content-Type: application/json' \\
  -d '{{
    "input": [
      {{"role": "user", "content": "What should I do if I miss a dose of metformin?"}}
    ]
  }}'
"""
)
