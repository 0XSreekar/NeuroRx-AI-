# Databricks notebook source
# MAGIC %md
# MAGIC # Vector Search infrastructure
# MAGIC
# MAGIC Creates the AI Search (Vector Search) endpoint and `DELTA_SYNC` index over
# MAGIC `neurorx.gold.drug_knowledge`, then verifies retrieval against the two
# MAGIC drugs the Phase 1 exit checkpoint and demo depend on: metformin and
# MAGIC warfarin. Idempotent — safe to re-run.
# MAGIC
# MAGIC This is a plain notebook, not a Lakeflow pipeline source file: creating a
# MAGIC search endpoint/index is a one-time infrastructure operation, not a
# MAGIC dataset transformation, and Lakeflow's `@dp.*` decorators have no
# MAGIC equivalent for it — `ARCHITECTURE.md` §2 itself draws the Vector Search
# MAGIC index as a separate node fed *by* the gold table, not part of the
# MAGIC pipeline box. Run this after `pipelines/medallion_pipeline.py` has
# MAGIC produced `neurorx.gold.drug_knowledge` at least once.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Verified before writing this notebook (not assumed)
# MAGIC
# MAGIC 1. **Package naming is mid-transition.** The product was renamed
# MAGIC    Databricks Vector Search → AI Search; the *Python package* is following
# MAGIC    the same path. Confirmed directly against `databricks-ai-search`'s own
# MAGIC    PyPI README: `databricks-vectorsearch` is now the **legacy** package
# MAGIC    name — once a companion `databricks-vectorsearch>=0.74` release lands,
# MAGIC    it becomes a thin re-export shim of `databricks-ai-search` with a
# MAGIC    deprecation warning. `VectorSearchClient` is preserved as a
# MAGIC    backward-compat alias for `AISearchClient` (literally
# MAGIC    `VectorSearchClient is AISearchClient`). This notebook installs and
# MAGIC    imports `databricks-vectorsearch` / `VectorSearchClient` because the
# MAGIC    task specifies that name and it is still fully functional — but don't
# MAGIC    be surprised by a deprecation warning on `%pip install`, and know that
# MAGIC    `databricks-ai-search` / `AISearchClient` is the forward-looking name
# MAGIC    if this notebook is revisited later.
# MAGIC 2. **Every method used below is confirmed to exist** against the current
# MAGIC    generated Python API reference (`api-docs.databricks.com/python/vector-search/`),
# MAGIC    not assumed from memory: `endpoint_exists`, `index_exists`,
# MAGIC    `create_endpoint_and_wait`, `create_delta_sync_index`,
# MAGIC    `VectorSearchIndex.wait_until_ready`, `VectorSearchIndex.similarity_search`.
# MAGIC 3. **`wait_until_ready(verbose=True)` is a real, documented method** — used
# MAGIC    for step 3 instead of a hand-rolled polling loop against `describe()`'s
# MAGIC    internal status field names, which current docs do not publish
# MAGIC    (checked directly; the field names and exact status-string values are
# MAGIC    not part of the public reference). Preferring the documented method
# MAGIC    over guessing at undocumented internals is the more defensible choice.
# MAGIC 4. **The `filters` dict combines multiple keys with AND**, confirmed with a
# MAGIC    verbatim doc example: `filters={"title": "Athena", "category": "mythology"}`
# MAGIC    matches rows satisfying both. This is exactly the rxcui+section
# MAGIC    compound filter both verification cells below need.
# MAGIC 5. **`databricks-gte-large-en` confirmed current** against the AWS-specific
# MAGIC    supported-models doc page directly (not a search-result summary):
# MAGIC    1024-dim embeddings, 8192-token window, hosted within the Databricks
# MAGIC    security perimeter. Still worth a live check against your own
# MAGIC    workspace's Serving page before relying on it, the same caveat
# MAGIC    `setup/00_workspace_runbook.md` §2 makes for the chat endpoint —
# MAGIC    endpoint availability can vary by workspace/region.
# MAGIC 6. **Free Edition quota** (`setup/00_workspace_runbook.md` §3): one AI
# MAGIC    Search endpoint, one search unit, `DELTA_SYNC` only. `endpoint_type`
# MAGIC    is hardcoded to `"STANDARD"` below — Free Edition has no
# MAGIC    `STORAGE_OPTIMIZED` option, and `STANDARD` is also the only endpoint
# MAGIC    type documented to support the dict-based `filters` syntax this
# MAGIC    notebook's verification cells rely on.

# COMMAND ----------

# MAGIC %pip install databricks-vectorsearch
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration
# MAGIC
# MAGIC `ENDPOINT_NAME` is fixed to `neurorx-vs` per this task's instruction —
# MAGIC not previously frozen anywhere else in the repo (`ARCHITECTURE.md` never
# MAGIC named one; `.env.example`'s prior placeholder, `neurorx-drug-labels`, was
# MAGIC just an illustrative example from Task 0.4, not a contract). Updated
# MAGIC `.env.example`'s `VECTOR_SEARCH_ENDPOINT` placeholder to match this value
# MAGIC so the two don't silently drift.

# COMMAND ----------

CATALOG = "neurorx"
SOURCE_TABLE = f"{CATALOG}.gold.drug_knowledge"
INDEX_NAME = f"{CATALOG}.gold.drug_knowledge_index"  # matches VECTOR_INDEX_FULLNAME in app/config.py
ENDPOINT_NAME = "neurorx-vs"
EMBEDDING_MODEL_ENDPOINT = "databricks-gte-large-en"

# Exactly DATA_CONTRACTS.md §5.1's gold.drug_knowledge columns == §8's
# citation contract fields. Passed explicitly rather than relying on an
# unverified "sync all columns by default" behavior.
SYNC_COLUMNS = ["chunk_id", "rxcui", "set_id", "drug_name", "section", "chunk_text"]

# COMMAND ----------

# MAGIC %md
# MAGIC ## Precondition check: source table exists and has CDF enabled
# MAGIC
# MAGIC `DATA_CONTRACTS.md` §5.1 requires CDF on `gold.drug_knowledge` for the
# MAGIC `DELTA_SYNC` index to work at all, and `pipelines/medallion_pipeline.py`
# MAGIC already sets this via `table_properties` when it creates the table. This
# MAGIC cell defensively re-asserts it rather than assuming the pipeline has run
# MAGIC — cheap, idempotent, and turns a cryptic Vector-Search-side failure into
# MAGIC a clear message if this notebook is ever run out of order.

# COMMAND ----------

if not spark.catalog.tableExists(SOURCE_TABLE):
    raise RuntimeError(
        f"{SOURCE_TABLE} does not exist. Run pipelines/medallion_pipeline.py "
        f"(Task 1.5) first — this notebook builds the index on top of its output."
    )

cdf_prop = spark.sql(f"SHOW TBLPROPERTIES {SOURCE_TABLE}").filter(
    "key = 'delta.enableChangeDataFeed'"
).collect()
cdf_enabled = bool(cdf_prop) and cdf_prop[0]["value"] == "true"
if not cdf_enabled:
    print(f"CDF not enabled on {SOURCE_TABLE} — enabling now.")
    spark.sql(f"ALTER TABLE {SOURCE_TABLE} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")
else:
    print(f"CDF already enabled on {SOURCE_TABLE}.")

row_count = spark.table(SOURCE_TABLE).count()
print(f"{SOURCE_TABLE}: {row_count} rows")
if row_count == 0:
    print(
        "WARNING: source table is empty. The endpoint/index will still be "
        "created below, but the verification cells will find nothing until "
        "the medallion pipeline has actually populated this table."
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — create the endpoint (if absent)

# COMMAND ----------

from databricks.vector_search.client import VectorSearchClient

client = VectorSearchClient()

if client.endpoint_exists(ENDPOINT_NAME):
    print(f"Endpoint '{ENDPOINT_NAME}' already exists — skipping creation.")
else:
    print(f"Creating endpoint '{ENDPOINT_NAME}' (type=STANDARD)...")
    client.create_endpoint_and_wait(
        name=ENDPOINT_NAME,
        endpoint_type="STANDARD",
        verbose=True,
    )
    print(f"Endpoint '{ENDPOINT_NAME}' online.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — create the delta-sync index (if absent)

# COMMAND ----------

if client.index_exists(endpoint_name=ENDPOINT_NAME, index_name=INDEX_NAME):
    print(f"Index '{INDEX_NAME}' already exists — skipping creation.")
    index = client.get_index(endpoint_name=ENDPOINT_NAME, index_name=INDEX_NAME)
else:
    print(f"Creating delta-sync index '{INDEX_NAME}' over '{SOURCE_TABLE}'...")
    index = client.create_delta_sync_index(
        endpoint_name=ENDPOINT_NAME,
        index_name=INDEX_NAME,
        source_table_name=SOURCE_TABLE,
        pipeline_type="TRIGGERED",  # matches the task's requirement; a manual/scheduled sync, not continuous streaming
        primary_key="chunk_id",
        embedding_source_column="chunk_text",
        embedding_model_endpoint_name=EMBEDDING_MODEL_ENDPOINT,
        columns_to_sync=SYNC_COLUMNS,
    )
    print(f"Index '{INDEX_NAME}' created.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — wait for the index to come online
# MAGIC
# MAGIC `wait_until_ready(verbose=True)` polls internally and prints its own
# MAGIC progress — see the "verified before writing this notebook" cell above for
# MAGIC why this is used instead of a hand-rolled loop against undocumented
# MAGIC `describe()` status fields. A TRIGGERED index also needs an explicit
# MAGIC `sync()` call to actually process rows on this and every subsequent run
# MAGIC (it does not sync automatically on a schedule).

# COMMAND ----------

print("Waiting for index to be online...")
index.wait_until_ready(verbose=True)
print("Index online. Triggering sync...")
index.sync()
index.wait_until_ready(verbose=True)
print("Sync complete, index ready for queries.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Result parsing helper
# MAGIC
# MAGIC **Verified against a complete official worked example** (Databricks'
# MAGIC own AI Search Python SDK tutorial) rather than assumed: `data_array`
# MAGIC rows are NOT simply "one value per requested column" — there is a
# MAGIC **trailing similarity-score value appended after the requested columns**
# MAGIC (the official example's own LangChain-conversion helper explicitly skips
# MAGIC `item[-1]` as the score). That same official example reads column names
# MAGIC from `results["manifest"]["columns"]` (a list of `{"name": ...}` dicts)
# MAGIC rather than trusting that the `columns=` request order matches the
# MAGIC returned row order. This helper does the same — maps by name from the
# MAGIC manifest, not by position from the request — so it can't silently
# MAGIC misalign a value under the wrong key.

# COMMAND ----------


def parse_citations(response, wanted_fields):
    """Turns a similarity_search() response into a list of dicts keyed by
    `wanted_fields`, reading actual column names from the response's own
    manifest rather than assuming they match the request order.
    """
    manifest_columns = [c["name"] for c in response.get("manifest", {}).get("columns", [])]
    data_rows = response.get("result", {}).get("data_array", [])
    citations = []
    for row in data_rows:
        full = dict(zip(manifest_columns, row))
        citations.append({field: full[field] for field in wanted_fields})
    return citations

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — verification: metformin missed-dose question
# MAGIC
# MAGIC Filtered to metformin's RxCUI (`6809` — verified live against RxNav in
# MAGIC Task 1.2/1.6, `CLAUDE.md` §4) and `section = information_for_patients`.
# MAGIC This is the Phase 1 exit checkpoint's vector-search half
# MAGIC (`ARCHITECTURE.md` §7): "vector query returns the metformin missed-dose
# MAGIC chunk."

# COMMAND ----------

METFORMIN_RXCUI = "6809"

results_metformin = index.similarity_search(
    query_text="what should I do if I miss a dose of metformin",
    columns=SYNC_COLUMNS,
    filters={"rxcui": METFORMIN_RXCUI, "section": "information_for_patients"},
    num_results=3,
)

citations_metformin = parse_citations(results_metformin, SYNC_COLUMNS)
assert len(citations_metformin) >= 1, (
    f"Expected >=1 result for the metformin missed-dose query, got {len(citations_metformin)}. "
    f"Checkpoint FAILED — check that gold.drug_knowledge actually has metformin "
    f"information_for_patients chunks and that the index has finished syncing."
)

print(f"PASSED: {len(citations_metformin)} result(s) for the metformin missed-dose query.\n")
for citation in citations_metformin:
    print("--- citation metadata (DATA_CONTRACTS.md §8 shape) ---")
    print({k: citation[k] for k in ["chunk_id", "rxcui", "drug_name", "section", "set_id"]})
    print("chunk_text:", citation["chunk_text"])
    print()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 — verification: warfarin warnings section

# COMMAND ----------

WARFARIN_RXCUI = "11289"

results_warfarin = index.similarity_search(
    query_text="what are the warnings for this medication",
    columns=SYNC_COLUMNS,
    filters={"rxcui": WARFARIN_RXCUI, "section": "warnings"},
    num_results=3,
)

citations_warfarin = parse_citations(results_warfarin, SYNC_COLUMNS)
assert len(citations_warfarin) >= 1, (
    f"Expected >=1 result for the warfarin warnings query, got {len(citations_warfarin)}. "
    f"Check that gold.drug_knowledge has a warfarin warnings chunk."
)

print(f"PASSED: {len(citations_warfarin)} result(s) for the warfarin warnings query.\n")
for citation in citations_warfarin:
    print("--- citation metadata (DATA_CONTRACTS.md §8 shape) ---")
    print({k: citation[k] for k in ["chunk_id", "rxcui", "drug_name", "section", "set_id"]})
    print("chunk_text:", citation["chunk_text"])
    print()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary

# COMMAND ----------

print("=" * 70)
print("VECTOR SEARCH INFRASTRUCTURE SUMMARY")
print("=" * 70)
print(f"Endpoint:  {ENDPOINT_NAME}")
print(f"Index:     {INDEX_NAME}")
print(f"Source:    {SOURCE_TABLE}")
print(f"Embedding: {EMBEDDING_MODEL_ENDPOINT}")
print(f"Metformin missed-dose check: PASSED ({len(citations_metformin)} result(s))")
print(f"Warfarin warnings check:     PASSED ({len(citations_warfarin)} result(s))")
print()
print("Set VECTOR_SEARCH_ENDPOINT=neurorx-vs in your .env (see .env.example) "
      "so app/config.py resolves the same endpoint this notebook just created.")
