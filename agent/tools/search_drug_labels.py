# Databricks notebook source
# MAGIC %md
# MAGIC # `neurorx.app.search_drug_labels` — grounded FDA label retrieval
# MAGIC
# MAGIC Wraps `neurorx.gold.drug_knowledge_index` (Task 1.7) as a UC Python function.
# MAGIC Every clinical-question answer the agent gives must come from what this
# MAGIC function returns — never from model knowledge (`ARCHITECTURE.md` §5(b),
# MAGIC `DATA_CONTRACTS.md` §8).
# MAGIC
# MAGIC ## ⚠️ A significant, user-confirmed architecture risk — read before deploying
# MAGIC
# MAGIC **A UC Python function has no `spark` session, and Databricks' own official
# MAGIC documentation for this exact use case — "wrap a Vector Search index as a UC
# MAGIC function agent tool" — shows a *SQL* function using the native
# MAGIC `vector_search()` table function, not a Python function calling the
# MAGIC Vector Search SDK or REST API.** That SQL-function path has no auth
# MAGIC uncertainty (`vector_search()` runs in the query engine's own trust
# MAGIC boundary). This file was still written as a Python UC function because
# MAGIC that's what was explicitly requested, with the auth risk below explicitly
# MAGIC accepted rather than silently avoided.
# MAGIC
# MAGIC **The specific gap:** the one documented in-sandbox credential mechanism for
# MAGIC UC Python functions, `databricks.service_credentials.getServiceCredentialsProvider()`,
# MAGIC is scoped to *external* cloud services (AWS/Azure/GCP resources) — not
# MAGIC Databricks' own internal REST APIs like Vector Search. No auto-auth
# MAGIC mechanism for a UC Python function calling back into its own workspace's
# MAGIC internal services was found after substantial research. This function
# MAGIC therefore authenticates via the **documented OAuth service-principal
# MAGIC client-credentials flow** (`POST /oidc/v1/token`, confirmed against
# MAGIC current Databricks REST API docs) — a real, working, non-notebook-specific
# MAGIC auth mechanism — but **reads the service principal's host/client
# MAGIC id/secret from environment variables
# MAGIC (`NEURORX_DATABRICKS_HOST`/`NEURORX_SP_CLIENT_ID`/`NEURORX_SP_CLIENT_SECRET`)
# MAGIC whose availability inside a UC Python function's execution sandbox is
# MAGIC UNVERIFIED.** Databricks confirms Python UDFs may declare pip dependencies
# MAGIC via `ENVIRONMENT` and may make outbound HTTPS calls (port 443) — both used
# MAGIC below — but does not document environment-variable or secret injection
# MAGIC into that sandbox. **Before relying on this in a real deployment:**
# MAGIC provision a service principal with `EXECUTE` on this function's dependencies
# MAGIC and query access to the `neurorx-vs` endpoint, then confirm — by actually
# MAGIC deploying and calling this function — whether the three env vars above are
# MAGIC reachable from inside the function body. If they are not, the correct fix
# MAGIC is almost certainly to switch to the SQL-function-wrapping-`vector_search()`
# MAGIC pattern described above, not to keep debugging Python-sandbox auth.
# MAGIC
# MAGIC ## Verified before writing this file
# MAGIC
# MAGIC - REST endpoint: `POST {host}/api/2.0/vector-search/indexes/{index_name}/query`
# MAGIC   (confirmed against the current REST API reference).
# MAGIC - Request body field is **`filters`** (not `filters_json`) — confirmed against
# MAGIC   a verbatim curl example in current docs. That example showed a SQL-string
# MAGIC   filter value (`"language = 'en' AND country = 'us'"`), which is the
# MAGIC   storage-optimized-endpoint form; `neurorx-vs` is a `STANDARD` endpoint
# MAGIC   (Task 1.7), which per the confirmed SDK signature (`filters: str |
# MAGIC   Dict[str, Any]`) also accepts a dict. This function sends
# MAGIC   `json.dumps({...})` as the `filters` string value — inferred from that
# MAGIC   dual-type signature, not from a directly-observed STANDARD-endpoint curl
# MAGIC   example, so flagged as the one still-inferred (not directly witnessed)
# MAGIC   piece of this request shape.
# MAGIC - Response parsing reuses the **exact fix from Task 1.7**:
# MAGIC   `similarity_search()`/this same REST endpoint returns `data_array` rows
# MAGIC   with a trailing similarity-score value, and column order must be read
# MAGIC   from `manifest.columns` (a list of `{"name": ...}` dicts) — never assumed
# MAGIC   to match the requested `columns` list order. See `CLAUDE.md`'s vector
# MAGIC   search section for the full story of why (a simulated reordered-manifest
# MAGIC   response there proved a naive `zip()` silently swaps citation fields).
# MAGIC - `ENVIRONMENT` clause syntax for declaring a pip dependency inside
# MAGIC   `CREATE FUNCTION ... LANGUAGE PYTHON`, and that multi-line bodies with
# MAGIC   `import` statements are supported — confirmed against a verbatim current
# MAGIC   doc example.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Register the function

# COMMAND ----------

CATALOG = "neurorx"
INDEX_NAME = f"{CATALOG}.gold.drug_knowledge_index"
NUM_RESULTS = 4
VALID_SECTIONS = [
    "dosage_and_administration",
    "drug_interactions",
    "warnings",
    "information_for_patients",
]

create_function_sql = f"""
CREATE OR REPLACE FUNCTION {CATALOG}.app.search_drug_labels(
  rxcui STRING
    COMMENT 'The RxCUI of the drug to search labels for. Required. Get this from silver.drugs or from the patient schedule -- never guess an RxCUI.',
  section STRING
    COMMENT 'Which FDA label section to search: one of dosage_and_administration, drug_interactions, warnings, information_for_patients, or the literal string any to search all four sections at once.',
  query STRING
    COMMENT 'A natural-language description of what the user wants to know, e.g. "what should I do if I miss a dose". Used as the semantic search query against the label text -- write it as a real question or statement, not just a keyword.'
)
RETURNS STRING
LANGUAGE PYTHON
ENVIRONMENT (
  dependencies = '["requests==2.32.3"]',
  environment_version = 'None'
)
COMMENT 'Retrieves grounded, citable excerpts from official FDA drug labels via semantic search. Use this for ANY clinical question about a specific drug -- missed doses, food or timing instructions, side effects, warnings, or general "what does the label say about X" questions. Always pass the specific rxcui the question is about; pass section to narrow to one label section if the question maps clearly to one (e.g. a missed-dose question maps to information_for_patients), otherwise pass "any". Returns a JSON string: on success, {{"results": [{{chunk_id, rxcui, drug_name, section, set_id, chunk_text, score}}, ...]}}, one object per retrieved chunk -- quote or closely paraphrase ONLY chunk_text, and cite the chunk_id(s) actually used. On no results, returns {{"results": [], "instruction": "..."}}: the instruction directs you to tell the user no labeled guidance was found and to direct them to their pharmacist -- an empty result is NOT permission to answer from general knowledge, and is NOT evidence the drug is safe or that there is nothing to worry about.'
AS $$
import json
import os
import time
import requests

_EMPTY_RESULT = json.dumps({{
    "results": [],
    "instruction": (
        "No labeled guidance found. Tell the user you don't have this "
        "information and direct them to their pharmacist. Do not answer "
        "from general knowledge."
    ),
}})

VALID_SECTIONS = {VALID_SECTIONS!r}
INDEX_NAME = {INDEX_NAME!r}
NUM_RESULTS = {NUM_RESULTS}

if not rxcui or not query:
    return _EMPTY_RESULT

host = os.environ.get("NEURORX_DATABRICKS_HOST")
client_id = os.environ.get("NEURORX_SP_CLIENT_ID")
client_secret = os.environ.get("NEURORX_SP_CLIENT_SECRET")
if not (host and client_id and client_secret):
    # Fails safe: an auth-config problem must never look like "no results
    # found" (which the agent is instructed to treat as "tell the user
    # nothing was found"). A misconfigured deployment should be loud, not
    # silently indistinguishable from a real empty-retrieval case.
    return json.dumps({{
        "results": [],
        "instruction": (
            "Retrieval is unavailable due to a configuration error "
            "(missing service credentials), not because no label content "
            "exists. Tell the user you're temporarily unable to look up "
            "labeled guidance and direct them to their pharmacist. Do not "
            "answer from general knowledge."
        ),
    }})

try:
    token_resp = requests.post(
        f"https://{{host}}/oidc/v1/token",
        auth=(client_id, client_secret),
        data={{"grant_type": "client_credentials", "scope": "all-apis"}},
        timeout=10,
    )
    token_resp.raise_for_status()
    access_token = token_resp.json()["access_token"]

    section_filter = None if section == "any" else section
    filters = {{"rxcui": rxcui}}
    if section_filter is not None:
        if section_filter not in VALID_SECTIONS:
            return _EMPTY_RESULT
        filters["section"] = section_filter

    query_resp = requests.post(
        f"https://{{host}}/api/2.0/vector-search/indexes/{{INDEX_NAME}}/query",
        headers={{"Authorization": f"Bearer {{access_token}}", "Content-Type": "application/json"}},
        json={{
            "query_text": query,
            "columns": ["chunk_id", "rxcui", "drug_name", "section", "set_id", "chunk_text"],
            "filters": json.dumps(filters),
            "num_results": NUM_RESULTS,
        }},
        timeout=15,
    )
    query_resp.raise_for_status()
    payload = query_resp.json()
except Exception:
    # A transient network/auth failure must fail safe the same way a
    # config error does -- never let an exception here surface as
    # "definitely no interaction data exists."
    return json.dumps({{
        "results": [],
        "instruction": (
            "Retrieval failed due to a temporary error, not because no "
            "label content exists. Tell the user you're temporarily "
            "unable to look up labeled guidance and direct them to their "
            "pharmacist. Do not answer from general knowledge."
        ),
    }})

manifest_columns = [c["name"] for c in payload.get("manifest", {{}}).get("columns", [])]
data_rows = payload.get("result", {{}}).get("data_array", [])

if not data_rows:
    return _EMPTY_RESULT

results = []
for row in data_rows:
    full = dict(zip(manifest_columns, row))
    results.append({{
        "chunk_id": full.get("chunk_id"),
        "rxcui": full.get("rxcui"),
        "drug_name": full.get("drug_name"),
        "section": full.get("section"),
        "set_id": full.get("set_id"),
        "chunk_text": full.get("chunk_text"),
        "score": full.get("score"),
    }})

return json.dumps({{"results": results}})
$$
"""

spark.sql(create_function_sql)
print(f"Registered {CATALOG}.app.search_drug_labels")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Test cell
# MAGIC
# MAGIC Metformin's RxCUI (`6809`) verified live against the RxNav API (`CLAUDE.md`
# MAGIC §4). Requires `NEURORX_DATABRICKS_HOST` / `NEURORX_SP_CLIENT_ID` /
# MAGIC `NEURORX_SP_CLIENT_SECRET` to actually be reachable inside the function's
# MAGIC execution environment — per the warning above, this is exactly the
# MAGIC unverified piece this test is meant to expose. If it fails with the
# MAGIC "configuration error" instruction text rather than a real empty/populated
# MAGIC result, that confirms the env-var path doesn't work and the SQL-function
# MAGIC alternative should be used instead.

# COMMAND ----------

import json

result_json = spark.sql(
    "SELECT neurorx.app.search_drug_labels("
    "'6809', 'information_for_patients', 'what should I do if I miss a dose of metformin'"
    ") AS result"
).collect()[0]["result"]

result = json.loads(result_json)

assert "results" in result, f"Malformed response, missing 'results' key: {result}"

if not result["results"]:
    print("NO RESULTS returned.")
    print("instruction:", result.get("instruction"))
    print(
        "If this says 'configuration error', the env-var credential path is "
        "not reachable inside the UC Python function sandbox -- see the "
        "warning at the top of this file. If it's the plain 'no labeled "
        "guidance found' instruction, check that gold.drug_knowledge "
        "actually has a metformin information_for_patients chunk and that "
        "the neurorx-vs index has finished syncing (pipelines/05_vector_index.py)."
    )
else:
    dose_timing_terms = ["miss", "dose", "remember", "next", "skip"]
    matched = [
        r for r in result["results"]
        if any(term in r["chunk_text"].lower() for term in dose_timing_terms)
    ]
    assert matched, (
        f"Got {len(result['results'])} result(s) but none contain dose-timing "
        f"language ({dose_timing_terms}). Results: {result['results']}"
    )

    print(f"PASSED: {len(result['results'])} result(s), "
          f"{len(matched)} containing dose-timing language.\n")
    for r in result["results"]:
        print(f"--- {r['chunk_id']} (score={r['score']}) ---")
        print(f"  rxcui={r['rxcui']} drug_name={r['drug_name']!r} section={r['section']}")
        print(f"  chunk_text: {r['chunk_text'][:200]}")
        print()
