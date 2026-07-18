# Databricks notebook source
# MAGIC %md
# MAGIC # RxNorm normalization ingestion
# MAGIC
# MAGIC Runs `rxnorm_client.py` over every drug name already ingested into
# MAGIC `neurorx.bronze.fda_labels_raw` (Task 1.1) and writes the results to
# MAGIC `neurorx.bronze.rxnorm_raw`, per `DATA_CONTRACTS.md` §3.2.
# MAGIC
# MAGIC Idempotent: re-running overwrites and MERGEs on `(query_name, rxcui)`.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Grain: this notebook follows `DATA_CONTRACTS.md`, not the task's literal wording
# MAGIC
# MAGIC The Task 1.2 instructions say the notebook "writes one row per lookup." Read
# MAGIC literally, that's one row per drug name queried. But `DATA_CONTRACTS.md` §3.2
# MAGIC — the frozen schema this table must match — states the grain explicitly and
# MAGIC justifies it at length (its F11): *"Grain: one row per returned candidate,
# MAGIC not per query — a name can resolve to several RxCUIs."* That finding was
# MAGIC itself verified live (`metformin` → two RxCUIs under a normalized search).
# MAGIC
# MAGIC These two instructions conflict. This notebook follows the frozen contract,
# MAGIC not the task's shorthand phrase, because:
# MAGIC - `DATA_CONTRACTS.md` is explicitly the frozen, later-tasks-depend-on-it
# MAGIC   source of truth (Task 0.5's own charter).
# MAGIC - Collapsing to one row per query would re-introduce exactly the silent
# MAGIC   information loss F11 was written to prevent — a second real candidate
# MAGIC   for a name would simply never reach bronze.
# MAGIC - `rxnorm_client.get_rxcui()` already provides the single-answer view (used
# MAGIC   below for the "unmatched names" report) — that decision layer isn't lost,
# MAGIC   it's just not what this table stores.
# MAGIC
# MAGIC **What this notebook actually stores per drug name:** every RxCUI candidate
# MAGIC from the exact tier if that tier found any (mirrors `get_rxcui`'s own
# MAGIC tier-fallback order); otherwise every deduped candidate from the
# MAGIC approximate tier (up to `rxnorm_client.DEFAULT_MAX_APPROXIMATE_ENTRIES`); or,
# MAGIC if neither tier found anything, one row with `rxcui = NULL` — per the
# MAGIC contract's own instruction that "the miss is recorded deliberately."

# COMMAND ----------

from datetime import datetime, timezone

# No sys.path setup needed: `rxnorm_client.py` lives in this same
# `data/ingestion/` folder, and Databricks puts a notebook's own directory on
# sys.path automatically for Git-folder/Repos notebooks — verified against
# current Databricks docs before writing this cell, rather than guessing a
# Workspace mount path that could easily be wrong for someone else's checkout.
import rxnorm_client as rc

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

CATALOG = "neurorx"
SOURCE_TABLE = f"{CATALOG}.bronze.fda_labels_raw"
TARGET_TABLE = f"{CATALOG}.bronze.rxnorm_raw"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Load distinct drug names from `fda_labels_raw`
# MAGIC
# MAGIC The queried name lives inside `payload._neurorx_ingestion_meta.queried_drug_name`
# MAGIC — the sidecar metadata field Task 1.1 added precisely so downstream steps
# MAGIC like this one don't need to re-derive it from `openfda.generic_name` (which,
# MAGIC as Task 1.1 found, is sometimes a combination-product string, not the plain
# MAGIC name that was actually looked up).

# COMMAND ----------

drug_names_df = spark.sql(f"""
    SELECT DISTINCT
        CAST(payload:_neurorx_ingestion_meta:queried_drug_name AS STRING) AS queried_drug_name
    FROM {SOURCE_TABLE}
    WHERE payload:_neurorx_ingestion_meta:queried_drug_name IS NOT NULL
""")

drug_names = [row["queried_drug_name"] for row in drug_names_df.collect()]
print(f"Loaded {len(drug_names)} distinct drug names from {SOURCE_TABLE}")

if not drug_names:
    raise ValueError(
        f"No drug names found in {SOURCE_TABLE}. Run data/ingestion/01_openfda_ingest.py first."
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Resolve every name: gather raw candidates for bronze, and the single-answer
# MAGIC ## decision for the unmatched-names report

# COMMAND ----------

bronze_rows = []
unmatched_for_review = []

for i, name in enumerate(drug_names):
    exact_ids = rc.search_exact(name)
    exact_url = f"{rc.RXNAV_BASE}/rxcui.json?name={name}&search=2"

    if exact_ids:
        for rxcui in exact_ids:
            props = rc.get_properties(rxcui)
            bronze_rows.append(
                {
                    "query_name": name,
                    "rxcui": rxcui,
                    "rxnorm_name": props.get("name"),
                    "tty": props.get("tty"),
                    "rank": None,  # exact tier: RxNav returns no ranking, only membership
                    "payload": {"tier": "exact", "rxnormId": exact_ids},
                    "pull_query": exact_url,
                }
            )
    else:
        approx_candidates = rc.search_approximate(name, max_entries=rc.DEFAULT_MAX_APPROXIMATE_ENTRIES)
        approx_url = (
            f"{rc.RXNAV_BASE}/approximateTerm.json?term={name}"
            f"&maxEntries={rc.DEFAULT_MAX_APPROXIMATE_ENTRIES}"
        )
        if approx_candidates:
            for cand in approx_candidates:
                props = rc.get_properties(cand["rxcui"])
                rank_raw = cand.get("rank")
                rank_int = int(rank_raw) if rank_raw is not None and str(rank_raw).isdigit() else None
                bronze_rows.append(
                    {
                        "query_name": name,
                        "rxcui": cand["rxcui"],
                        "rxnorm_name": props.get("name"),
                        "tty": props.get("tty"),
                        "rank": rank_int,
                        "payload": {"tier": "approximate", "score": cand["score"], "rank": rank_raw},
                        "pull_query": approx_url,
                    }
                )
        else:
            # Neither tier found anything — record the miss deliberately,
            # per DATA_CONTRACTS.md §3.2: "rxcui nullable so unresolved names
            # are retained."
            bronze_rows.append(
                {
                    "query_name": name,
                    "rxcui": None,
                    "rxnorm_name": None,
                    "tty": None,
                    "rank": None,
                    "payload": {"tier": "none"},
                    "pull_query": approx_url,
                }
            )

    # Single-answer decision, for the report below. Both tiers above are
    # already cached by rxnorm_client, so this costs no extra network calls
    # in the common case.
    decision = rc.get_rxcui(name)
    if decision.match_type == "none":
        unmatched_for_review.append(name)

    if (i + 1) % 25 == 0 or i == len(drug_names) - 1:
        print(f"[{i + 1}/{len(drug_names)}] processed")

now_iso = datetime.now(timezone.utc).isoformat()
for row in bronze_rows:
    row["_ingested_at"] = now_iso
    row["_source_file"] = row.pop("pull_query")  # request URL, per DATA_CONTRACTS.md F12

print(f"\n{len(bronze_rows)} candidate rows gathered for {len(drug_names)} queried names.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Write to `neurorx.bronze.rxnorm_raw`

# COMMAND ----------

from pyspark.sql import functions as F

spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {TARGET_TABLE} (
        query_name STRING,
        rxcui STRING,
        rxnorm_name STRING,
        tty STRING,
        rank INT,
        payload VARIANT,
        _ingested_at TIMESTAMP,
        _source_file STRING
    )
""")

staged_df = spark.createDataFrame(bronze_rows).select(
    F.col("query_name"),
    F.col("rxcui"),
    F.col("rxnorm_name"),
    F.col("tty"),
    F.col("rank"),
    F.parse_json(F.to_json(F.col("payload"))).alias("payload"),
    F.to_timestamp(F.col("_ingested_at")).alias("_ingested_at"),
    F.col("_source_file"),
)

staged_df.createOrReplaceTempView("staged_rxnorm")

spark.sql(f"""
    MERGE INTO {TARGET_TABLE} AS target
    USING staged_rxnorm AS source
    ON target.query_name = source.query_name AND target.rxcui <=> source.rxcui
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")

print(f"MERGE complete into {TARGET_TABLE}.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary — unmatched names for manual review
# MAGIC
# MAGIC This is the single-answer (`get_rxcui`) view, not the raw candidate count:
# MAGIC a name can have candidate rows in bronze (e.g. two tied approximate
# MAGIC matches) and still be "unmatched" here, because `get_rxcui` correctly
# MAGIC refuses to pick between them (see `rxnorm_client.py`'s safety invariant).

# COMMAND ----------

print("=" * 70)
print("RXNORM RESOLUTION SUMMARY")
print("=" * 70)
print(f"Distinct drug names queried: {len(drug_names)}")
print(f"Resolved to a single confident RxCUI: {len(drug_names) - len(unmatched_for_review)}")
print(f"Unmatched (need manual review): {len(unmatched_for_review)}")

if unmatched_for_review:
    print("\nNames requiring manual review:")
    for name in unmatched_for_review:
        print(f"  - {name}")
else:
    print("\nAll drug names resolved to a single confident RxCUI.")

required_demo_drugs = ["warfarin", "ibuprofen", "metformin", "lisinopril"]
print("\nRequired demo drugs — resolution check:")
for drug in required_demo_drugs:
    result = rc.get_rxcui(drug)
    status = "OK" if result.rxcui else "MISSING — BLOCKS PHASE 1 CHECKPOINT"
    print(f"  {drug:15s} rxcui={result.rxcui!r:10s} [{status}]")

display(spark.createDataFrame([{"query_name": n} for n in unmatched_for_review]))
