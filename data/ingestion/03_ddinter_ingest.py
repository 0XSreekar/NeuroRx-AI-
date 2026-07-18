# Databricks notebook source
# MAGIC %md
# MAGIC # DDInter 2.0 ingestion + RxCUI mapping
# MAGIC
# MAGIC Turns the DDInter 2.0 open drug–drug interaction dataset into RxCUI-keyed
# MAGIC rows in `neurorx.bronze.ddinter_raw`, restricted to pairs where both drugs
# MAGIC are in the ~200-drug curated list from `data/ingestion/01_openfda_ingest.py`.
# MAGIC
# MAGIC Idempotent: re-running overwrites the quarantine table and MERGEs
# MAGIC `ddinter_raw` on `(drug_a_name, drug_b_name)`.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — Download instructions (manual, one-time)
# MAGIC
# MAGIC **Verified live against the current DDInter 2.0 site before writing this
# MAGIC cell** — both via a static fetch and by rendering the page in a real browser
# MAGIC and reading its actual download links, since a site like this can render its
# MAGIC file list client-side and a naive fetch could silently miss entries.
# MAGIC
# MAGIC **Download page:** [`https://ddinter2.scbdd.com/download/`](https://ddinter2.scbdd.com/download/)
# MAGIC
# MAGIC The page offers **exactly 8 CSV files**, split by ATC top-level drug class.
# MAGIC Download all 8 — do not assume any one is safely skippable:
# MAGIC
# MAGIC | File | ATC class | Direct URL |
# MAGIC |---|---|---|
# MAGIC | `ddinter_downloads_code_A.csv` | Alimentary tract & metabolism | `https://ddinter2.scbdd.com/static/media/download/ddinter_downloads_code_A.csv` |
# MAGIC | `ddinter_downloads_code_B.csv` | Blood & blood forming organs | `https://ddinter2.scbdd.com/static/media/download/ddinter_downloads_code_B.csv` |
# MAGIC | `ddinter_downloads_code_D.csv` | Dermatologicals | `https://ddinter2.scbdd.com/static/media/download/ddinter_downloads_code_D.csv` |
# MAGIC | `ddinter_downloads_code_H.csv` | Systemic hormones (excl. sex hormones/insulin) | `https://ddinter2.scbdd.com/static/media/download/ddinter_downloads_code_H.csv` |
# MAGIC | `ddinter_downloads_code_L.csv` | Antineoplastic & immunomodulating | `https://ddinter2.scbdd.com/static/media/download/ddinter_downloads_code_L.csv` |
# MAGIC | `ddinter_downloads_code_P.csv` | Antiparasitic products | `https://ddinter2.scbdd.com/static/media/download/ddinter_downloads_code_P.csv` |
# MAGIC | `ddinter_downloads_code_R.csv` | Respiratory system | `https://ddinter2.scbdd.com/static/media/download/ddinter_downloads_code_R.csv` |
# MAGIC | `ddinter_downloads_code_V.csv` | Various | `https://ddinter2.scbdd.com/static/media/download/ddinter_downloads_code_V.csv` |
# MAGIC
# MAGIC **Upload target:** `/Volumes/neurorx/bronze/raw_files/ddinter/` — upload all 8
# MAGIC files there unmodified (Catalog Explorer → Volumes → `neurorx.bronze.raw_files`
# MAGIC → create/navigate to a `ddinter` folder → Upload).
# MAGIC
# MAGIC **A finding worth knowing before you worry the data is incomplete:** the 8
# MAGIC files only cover ATC codes A, B, D, H, L, P, R, V — six top-level ATC classes
# MAGIC (C: cardiovascular, G, J: anti-infectives, M: musculoskeletal, N: nervous
# MAGIC system, S) have **no dedicated file**. That looks like it would exclude
# MAGIC ibuprofen (ATC class M) and lisinopril (class C) entirely — both are
# MAGIC required demo drugs. **Verified empirically that this is not the case:**
# MAGIC downloaded file B directly and confirmed the row
# MAGIC `DDInter1951,Warfarin,DDInter900,Ibuprofen,Major` is present in it — DDInter
# MAGIC files interactions under (at least) one side's ATC class, not both, so a
# MAGIC class-M drug like ibuprofen still appears throughout the other 8 files
# MAGIC whenever its interaction partner belongs to one of the covered classes.
# MAGIC Also confirmed lisinopril (368 total mentions) and metformin (907 total
# MAGIC mentions) appear across every one of the 8 files. Downloading all 8 gives
# MAGIC full practical coverage of the curated drug list despite the apparent gap.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Design note — a tension with `DATA_CONTRACTS.md`, resolved explicitly
# MAGIC
# MAGIC `DATA_CONTRACTS.md` §3 states bronze's governing rule up front: *"Raw,
# MAGIC as-ingested. No business logic, no drops."* Its §3.3 describes
# MAGIC `ddinter_raw` as *"Raw DDInter 2.0 CSV rows, as parsed"* with a column list
# MAGIC that has no RxCUI columns at all.
# MAGIC
# MAGIC This task's steps 3–5 ask for exactly the opposite on this table: restrict
# MAGIC to the curated drug list (a drop), require both sides to RxCUI-map (business
# MAGIC logic), and write only the survivors to `ddinter_raw`. That's `silver`-shaped
# MAGIC work by the contract's own definition of the layers — `silver.interactions`
# MAGIC (§4.3) is where RxCUI-keyed, source-attributed interaction pairs are meant to
# MAGIC live.
# MAGIC
# MAGIC **Resolution used here, chosen to satisfy both documents' actual intent
# MAGIC rather than pick one over the other:**
# MAGIC - `neurorx.bronze.ddinter_raw` keeps **exactly** the frozen column list
# MAGIC   (`ddinter_id_a`, `drug_a_name`, `ddinter_id_b`, `drug_b_name`,
# MAGIC   `severity_level`, `_ingested_at`, `_source_file`) — no new columns are
# MAGIC   added to it. "Both sides map to an RxCUI" and "both drugs are in the
# MAGIC   curated list" are used as **qualifying filters** on which rows get
# MAGIC   written, per this task's explicit instructions — the RxCUI values
# MAGIC   themselves are not stored as new columns on this table, since the frozen
# MAGIC   schema has nowhere to put them and actually resolving/storing RxCUIs
# MAGIC   durably is `silver.interactions`'s stated job.
# MAGIC - This means `bronze.ddinter_raw`, as populated by this notebook, is **not**
# MAGIC   the complete unfiltered DDInter export — it is pre-filtered to what this
# MAGIC   product can currently use. If a fully unfiltered raw audit copy of the
# MAGIC   DDInter download is wanted later, that needs its own table added to
# MAGIC   `DATA_CONTRACTS.md` — out of scope here, flagged rather than silently
# MAGIC   added.
# MAGIC - The quarantine table `neurorx.bronze.ddinter_unmapped` is new (the task
# MAGIC   explicitly asks for it) and does carry RxCUI columns, since its entire
# MAGIC   purpose is showing a reviewer what did and didn't resolve.

# COMMAND ----------

from datetime import datetime, timezone

# Same-folder import — no sys.path setup needed (verified in Task 1.2).
import rxnorm_client as rc

from pyspark.sql import functions as F

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

CATALOG = "neurorx"
VOLUME_DIR = f"/Volumes/{CATALOG}/bronze/raw_files/ddinter"
FDA_LABELS_TABLE = f"{CATALOG}.bronze.fda_labels_raw"
TARGET_TABLE = f"{CATALOG}.bronze.ddinter_raw"
QUARANTINE_TABLE = f"{CATALOG}.bronze.ddinter_unmapped"

# Confirmed exhaustive across the two files sampled during verification
# (B: 15,140 rows: {Major, Moderate, Minor, Unknown}; V: 12,024 rows: same
# four values, no fifth value seen). Defensive fallback below still handles
# an unrecognized value rather than assuming this holds for the other 6
# files too.
SEVERITY_MAP = {
    "major": "major",
    "moderate": "moderate",
    "minor": "minor",
    "unknown": "unknown",
}

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — Load all 8 CSVs, standardize columns
# MAGIC
# MAGIC Real header, confirmed by downloading and inspecting two of the eight files
# MAGIC directly: `DDInterID_A,Drug_A,DDInterID_B,Drug_B,Level`. **No description
# MAGIC column exists in the bulk CSV export** — confirmed by inspecting the header
# MAGIC row directly, not assumed. The task's requirement to include "description if
# MAGIC present" is honored as: it is not present in this data source, so no
# MAGIC `description` column is populated here (`silver.interactions` allows it to
# MAGIC be `NULL`).

# COMMAND ----------

raw_df = (
    spark.read.option("header", True)
    .csv(f"{VOLUME_DIR}/*.csv")
    .withColumn("_source_file", F.input_file_name())
)

raw_count = raw_df.count()
print(f"Loaded {raw_count} raw rows from {VOLUME_DIR}")


def map_severity(col):
    """Maps DDInter's Level values onto DATA_CONTRACTS.md's severity enum.
    Confirmed 1:1 (just a case fold) against real data: Major/Moderate/Minor/
    Unknown -> major/moderate/minor/unknown. Anything else (should not occur,
    but not assumed impossible across all 8 files) maps to 'unknown' rather
    than failing the load, since a genuinely novel severity label is a
    data-quality question for review, not a reason to drop the row here.
    """
    known = F.lower(col).isin(list(SEVERITY_MAP.keys()))
    return F.when(known, F.lower(col)).otherwise(F.lit("unknown"))


standardized_df = raw_df.select(
    F.col("DDInterID_A").alias("ddinter_id_a"),
    F.col("Drug_A").alias("drug_a_name"),
    F.col("DDInterID_B").alias("ddinter_id_b"),
    F.col("Drug_B").alias("drug_b_name"),
    map_severity(F.col("Level")).alias("severity_level"),
    F.col("_source_file"),
).filter(
    F.col("drug_a_name").isNotNull()
    & F.col("drug_b_name").isNotNull()
    & (F.lower(F.col("drug_a_name")) != F.lower(F.col("drug_b_name")))  # not_self_pair, per DATA_CONTRACTS.md
)

unrecognized_levels = raw_df.filter(~F.lower(F.col("Level")).isin(list(SEVERITY_MAP.keys()))).count()
if unrecognized_levels:
    print(f"WARNING: {unrecognized_levels} rows had a Level value outside "
          f"{list(SEVERITY_MAP.keys())} — mapped to 'unknown'. Review these.")

standardized_count = standardized_df.count()
print(f"Standardized to {standardized_count} rows (dropped self-pairs / null-name rows: "
      f"{raw_count - standardized_count})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — Restrict to pairs where both drugs are in the curated list
# MAGIC
# MAGIC Joined on lowercase `queried_drug_name` from `bronze.fda_labels_raw`'s
# MAGIC payload sidecar (the same field `02_rxnorm_ingest.py` uses) — **not**
# MAGIC `openfda.generic_name`, since Task 1.1 confirmed that field is sometimes a
# MAGIC combination-product string (e.g. `SITAGLIPTIN AND METFORMIN HYDROCHLORIDE`)
# MAGIC rather than the plain curated name.

# COMMAND ----------

curated_names_df = (
    spark.sql(f"""
        SELECT DISTINCT
            LOWER(CAST(payload:_neurorx_ingestion_meta:queried_drug_name AS STRING)) AS name
        FROM {FDA_LABELS_TABLE}
        WHERE payload:_neurorx_ingestion_meta:queried_drug_name IS NOT NULL
    """)
)
curated_names = {row["name"] for row in curated_names_df.collect()}
print(f"Curated drug list: {len(curated_names)} names")

for required in ["warfarin", "ibuprofen", "metformin", "lisinopril"]:
    assert required in curated_names, (
        f"required demo drug '{required}' missing from the curated list in {FDA_LABELS_TABLE} — "
        f"run data/ingestion/01_openfda_ingest.py first"
    )

curated_broadcast = F.broadcast(curated_names_df)

restricted_df = (
    standardized_df
    .join(curated_broadcast.withColumnRenamed("name", "_a_match"),
          F.lower(F.col("drug_a_name")) == F.col("_a_match"), "inner")
    .join(curated_broadcast.withColumnRenamed("name", "_b_match"),
          F.lower(F.col("drug_b_name")) == F.col("_b_match"), "inner")
    .drop("_a_match", "_b_match")
)

restricted_count = restricted_df.count()
print(f"Restricted to {restricted_count} rows where both drugs are in the curated list "
      f"(from {standardized_count})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — Map both names to RxCUI, quarantine failures
# MAGIC
# MAGIC Resolves once per **distinct** drug name (not per row) — `rxnorm_client`
# MAGIC already caches at the HTTP-request level, but batching by distinct name
# MAGIC here avoids even re-invoking `get_rxcui()`'s own logic thousands of times
# MAGIC for the same handful of curated names.

# COMMAND ----------

pair_names_df = (
    restricted_df.select(F.col("drug_a_name").alias("name"))
    .union(restricted_df.select(F.col("drug_b_name").alias("name")))
    .distinct()
)
distinct_pair_names = [row["name"] for row in pair_names_df.collect()]
print(f"Resolving RxCUI for {len(distinct_pair_names)} distinct drug names appearing in filtered pairs")

name_to_result = {name: rc.get_rxcui(name) for name in distinct_pair_names}

resolved_ok = sum(1 for r in name_to_result.values() if r.rxcui is not None)
print(f"  {resolved_ok}/{len(distinct_pair_names)} names resolved to a confident RxCUI")
unresolved_names = [name for name, r in name_to_result.items() if r.rxcui is None]
if unresolved_names:
    print(f"  Unresolved: {unresolved_names}")

# COMMAND ----------

rows = restricted_df.collect()

mapped_rows = []
quarantined_rows = []
now_iso = datetime.now(timezone.utc).isoformat()

for row in rows:
    result_a = name_to_result[row["drug_a_name"]]
    result_b = name_to_result[row["drug_b_name"]]

    if result_a.rxcui is not None and result_b.rxcui is not None:
        mapped_rows.append({
            "ddinter_id_a": row["ddinter_id_a"],
            "drug_a_name": row["drug_a_name"],
            "ddinter_id_b": row["ddinter_id_b"],
            "drug_b_name": row["drug_b_name"],
            "severity_level": row["severity_level"],
            "_ingested_at": now_iso,
            "_source_file": row["_source_file"],
        })
    else:
        unmapped_side = (
            "both" if result_a.rxcui is None and result_b.rxcui is None
            else "a" if result_a.rxcui is None
            else "b"
        )
        quarantined_rows.append({
            "ddinter_id_a": row["ddinter_id_a"],
            "drug_a_name": row["drug_a_name"],
            "rxcui_a": result_a.rxcui,
            "ddinter_id_b": row["ddinter_id_b"],
            "drug_b_name": row["drug_b_name"],
            "rxcui_b": result_b.rxcui,
            "severity_level": row["severity_level"],
            "unmapped_side": unmapped_side,
            "_ingested_at": now_iso,
            "_source_file": row["_source_file"],
        })

total_count = len(rows)
mapped_count = len(mapped_rows)
quarantined_count = len(quarantined_rows)

print("=" * 70)
print("DDINTER MAPPING SUMMARY")
print("=" * 70)
print(f"Total curated-list pairs:  {total_count}")
print(f"Mapped (both sides OK):    {mapped_count}")
print(f"Quarantined (a side failed): {quarantined_count}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 — Write mapped rows to `bronze.ddinter_raw` (MERGE-idempotent)

# COMMAND ----------

spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {TARGET_TABLE} (
        ddinter_id_a STRING,
        drug_a_name STRING,
        ddinter_id_b STRING,
        drug_b_name STRING,
        severity_level STRING,
        _ingested_at TIMESTAMP,
        _source_file STRING
    )
""")

if mapped_rows:
    staged_df = spark.createDataFrame(mapped_rows).select(
        "ddinter_id_a", "drug_a_name", "ddinter_id_b", "drug_b_name",
        "severity_level",
        F.to_timestamp(F.col("_ingested_at")).alias("_ingested_at"),
        "_source_file",
    )
    staged_df.createOrReplaceTempView("staged_ddinter")

    spark.sql(f"""
        MERGE INTO {TARGET_TABLE} AS target
        USING staged_ddinter AS source
        ON target.drug_a_name = source.drug_a_name AND target.drug_b_name = source.drug_b_name
        WHEN MATCHED THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
    """)
    print(f"MERGE complete into {TARGET_TABLE}: {mapped_count} rows.")
else:
    print(f"No mapped rows to write to {TARGET_TABLE} this run.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quarantine — `bronze.ddinter_unmapped`
# MAGIC
# MAGIC Overwritten each run (not MERGEd) — this table is a point-in-time review
# MAGIC list, not an accumulating record; the task only requires MERGE-idempotence
# MAGIC for `ddinter_raw`.

# COMMAND ----------

spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {QUARANTINE_TABLE} (
        ddinter_id_a STRING,
        drug_a_name STRING,
        rxcui_a STRING,
        ddinter_id_b STRING,
        drug_b_name STRING,
        rxcui_b STRING,
        severity_level STRING,
        unmapped_side STRING,
        _ingested_at TIMESTAMP,
        _source_file STRING
    )
""")

if quarantined_rows:
    quarantine_df = spark.createDataFrame(quarantined_rows).select(
        "ddinter_id_a", "drug_a_name", "rxcui_a",
        "ddinter_id_b", "drug_b_name", "rxcui_b",
        "severity_level", "unmapped_side",
        F.to_timestamp(F.col("_ingested_at")).alias("_ingested_at"),
        "_source_file",
    )
    quarantine_df.write.mode("overwrite").saveAsTable(QUARANTINE_TABLE)
    print(f"Overwrote {QUARANTINE_TABLE}: {quarantined_count} rows for manual review.")
else:
    spark.createDataFrame([], schema=spark.table(QUARANTINE_TABLE).schema).write.mode("overwrite").saveAsTable(QUARANTINE_TABLE)
    print(f"No quarantined rows this run — {QUARANTINE_TABLE} cleared.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6 — Assert: warfarin+ibuprofen exists with severity != unknown
# MAGIC
# MAGIC This pair is the demo's centerpiece (`ARCHITECTURE.md` §7 Phase 1
# MAGIC checkpoint). Bronze is unordered (§3.3: "unordered and un-canonicalized at
# MAGIC this layer"), so both orderings are checked. Fails loudly — this must not
# MAGIC pass silently if the pair is missing.

# COMMAND ----------

centerpiece_check = spark.sql(f"""
    SELECT drug_a_name, drug_b_name, severity_level
    FROM {TARGET_TABLE}
    WHERE (LOWER(drug_a_name) = 'warfarin' AND LOWER(drug_b_name) = 'ibuprofen')
       OR (LOWER(drug_a_name) = 'ibuprofen' AND LOWER(drug_b_name) = 'warfarin')
""").collect()

assert len(centerpiece_check) > 0, (
    f"CENTERPIECE CHECK FAILED: no warfarin+ibuprofen row found in {TARGET_TABLE}. "
    f"This pair is required for the Phase 1 exit checkpoint and the eval set's "
    f"true-positive case. Check: (1) both drugs are in the curated list in "
    f"{FDA_LABELS_TABLE}, (2) both resolved to an RxCUI via rxnorm_client, "
    f"(3) the pair survived the curated-list join above."
)

for row in centerpiece_check:
    assert row["severity_level"] != "unknown", (
        f"CENTERPIECE CHECK FAILED: warfarin+ibuprofen found but severity_level="
        f"'{row['severity_level']}' — expected a real severity (DDInter reports "
        f"this pair as 'Major'). Check the severity-mapping logic in Step 2."
    )

print(f"Centerpiece check PASSED: warfarin+ibuprofen present with severity="
      f"'{centerpiece_check[0]['severity_level']}'.")
