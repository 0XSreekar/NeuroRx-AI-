"""Log the NeuroRx AI supervisor agent to MLflow (Task 2.6 requirement 8).

Run this from a Databricks notebook or the workspace CLI, **not** by importing
`agent.py` directly — `mlflow.pyfunc.log_model` re-executes `agent/agent.py` in
an isolated environment via the Models-from-Code pattern (`python_model=` is a
*file path*, not the already-imported module), which is exactly why the
logging step lives in this separate file rather than inside `agent.py`'s own
`__main__` block: the current "author and log an agent" pattern always splits
these two concerns.

Declaring `resources=` here is what lets the *deployed* agent authenticate to
the four UC functions and the FM API endpoint via Databricks' automatic
credential passthrough, with no secrets embedded in the model — confirmed
against Databricks' current model-dependency-logging docs (see agent.py's
module docstring for what was checked live this session and why).

## Extended for Task 2.8 — read before trusting these resource declarations

The original (Task 2.6) `RESOURCES` list covered only the FM endpoint and the
four UC functions the agent calls directly. Task 2.8 ("ensure the endpoint has
permissions/resources for: the vector index... and Lakebase connectivity")
asks for more, so `build_resources()` below also declares a
`DatabricksVectorSearchIndex` and a `DatabricksLakebase` (plus the one Free
Edition SQL warehouse, discovered live rather than hardcoded — see CLAUDE.md
§4: "One pre-created SQL warehouse... You cannot create another").

⚠️ **These three additions are governance/lineage declarations, not the actual
auth mechanism for the calls they describe** — worth being honest about rather
than implying "declaring it wires it up." Per this project's own already-
verified findings: `search_drug_labels` (Task 2.2) and `manage_schedule` (Task
2.3) each authenticate to Vector Search / the Lakebase Data API / the SQL
Statement Execution API via their **own** OAuth service-principal
client-credentials flow, using env vars set on those UC functions themselves
(`NEURORX_DATABRICKS_HOST`/`NEURORX_SP_CLIENT_ID`/`NEURORX_SP_CLIENT_SECRET`/
`NEURORX_SQL_WAREHOUSE_ID`/`NEURORX_LAKEBASE_REST_ENDPOINT`) — not via
Databricks' resource-based auto-auth passthrough, because UC Python functions
have no verified in-sandbox credential mechanism for internal Databricks REST
APIs (the dead end documented in Task 2.2's own file). Declaring
`DatabricksVectorSearchIndex`/`DatabricksLakebase` on *the agent's* logged
model documents that dependency for lineage/governance UI and is what current
docs ask every model's dependency graph to declare — but it does not replace
those two UC functions' own env-var-based auth, which is configured
separately, at the UC function's own creation time.
"""

import mlflow
from databricks.sdk import WorkspaceClient
from mlflow.models.resources import (
    DatabricksFunction,
    DatabricksLakebase,
    DatabricksServingEndpoint,
    DatabricksSQLWarehouse,
    DatabricksVectorSearchIndex,
)

from agent.agent import LLM_ENDPOINT, UC_TOOL_NAMES
from app.config import settings

# Per CLAUDE.md's non-negotiables table — fixed, not derived from settings,
# since no `LAKEBASE_INSTANCE_NAME` env var exists in app/config.py's contract.
LAKEBASE_INSTANCE_NAME = "neurorx-oltp"


def _discover_warehouse_id() -> str:
    """Free Edition provides exactly one pre-created SQL warehouse and no way
    to create another (CLAUDE.md §4) — so "the" warehouse is discovered live
    rather than hardcoded, which would silently go stale the moment this ran
    against a different workspace.
    """
    w = WorkspaceClient()
    warehouse = next(iter(w.warehouses.list()), None)
    if warehouse is None:
        raise RuntimeError(
            "No SQL warehouse found in this workspace — manage_schedule's "
            "deployed function needs one for its SQL Statement Execution API "
            "calls (CLAUDE.md §4). Create one before deploying."
        )
    return warehouse.id


def build_resources() -> list:
    """Every Databricks resource the deployed agent's dependency graph
    touches — declared explicitly rather than relying on ambient workspace
    credentials. Computed lazily (not a module-level constant) because
    `_discover_warehouse_id()` needs a live `WorkspaceClient` — importing this
    module should not require an active Databricks connection.
    """
    return [
        DatabricksServingEndpoint(endpoint_name=LLM_ENDPOINT),
        *[DatabricksFunction(function_name=name) for name in UC_TOOL_NAMES],
        DatabricksVectorSearchIndex(index_name=settings.vector_index_fullname),
        DatabricksLakebase(database_instance_name=LAKEBASE_INSTANCE_NAME),
        DatabricksSQLWarehouse(warehouse_id=_discover_warehouse_id()),
    ]


# Pinned to the actual current PyPI releases, checked live this session via the
# PyPI JSON API — not round numbers guessed from memory. Re-check before a real
# deployment if significant time has passed since this file was written.
PIP_REQUIREMENTS = [
    "mlflow==3.14.0",
    "databricks-langchain==0.20.0",
    "langchain==1.3.14",
    "langgraph==1.2.9",
    "python-dotenv==1.2.2",
]

# Matches the OpenAI Responses API `input` shape ResponsesAgentRequest expects
# (see agent.py's predict/predict_stream) — a plain single-turn user question
# that exercises one of the four tools (search_drug_labels) end to end.
INPUT_EXAMPLE = {
    "input": [
        {"role": "user", "content": "What should I do if I miss a dose of metformin?"}
    ]
}


def log_agent() -> str:
    """Log the agent and return its model URI. Call this from a notebook cell,
    then pass the returned URI to `mlflow.register_model` / `agents.deploy`.

    MLflow 3 makes models first-class citizens: no active run is required, and
    `name=` replaces the older, now-deprecated `artifact_path=` (confirmed
    against the current MLflow 3 migration guide) — using both would be
    stale 2.x-shaped code pretending to be current.
    """
    logged_agent_info = mlflow.pyfunc.log_model(
        name="agent",
        python_model="agent/agent.py",
        resources=build_resources(),
        pip_requirements=PIP_REQUIREMENTS,
        input_example=INPUT_EXAMPLE,
    )
    print(f"Logged agent: {logged_agent_info.model_uri}")
    return logged_agent_info.model_uri


if __name__ == "__main__":
    log_agent()
