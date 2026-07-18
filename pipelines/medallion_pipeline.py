"""NeuroRx AI — Bronze → Silver → Gold Lakeflow Declarative Pipeline.

Builds every silver and gold table specified in DATA_CONTRACTS.md §4-§5 from the
bronze tables produced by Tasks 1.1-1.4. This is the deterministic safety core's
foundation: `gold.interaction_pairs` is what `check_interactions` queries, and
`gold.drug_knowledge` is what the Vector Search index embeds — see
ARCHITECTURE.md §5.

┌─────────────────────────────────────────────────────────────────────────┐
│ pipelines/chunking.py (Task 1.6) — now delivered and wired in            │
│                                                                           │
│ silver_label_sections() below imports chunk_section(rxcui, set_id,       │
│ drug_name, section, raw_texts) -> list[dict] from chunking.py, which now │
│ exists. Each returned dict is a complete silver.label_sections row,      │
│ chunk_id included — computed inside chunking.py per DATA_CONTRACTS.md    │
│ §4.2's concat_ws formula, NOT the sha256 scheme Task 1.6's own spec      │
│ described (see chunking.py's module docstring for that conflict and its  │
│ resolution). This pipeline was updated to match chunking.py's actual     │
│ interface after it was delivered, since an earlier draft of this file    │
│ speculatively pre-specified a narrower one-argument interface            │
│ (chunk_section_text(str)) that Task 1.6 did not end up matching.         │
└─────────────────────────────────────────────────────────────────────────┘

Verified against current Databricks documentation before writing this file
(Lakeflow's Python API changed names during the "Lakeflow Declarative
Pipelines" rebrand — the old `dlt` module still works for backward
compatibility, but this file uses the current, actively-recommended API):

1. Import is `from pyspark import pipelines as dp` (`dlt` is legacy/back-compat
   only). Databricks' own docs recommend `dp` for new code.
2. `@dp.table` is for STREAMING reads only ("apply @table to a query that
   performs a streaming read"). Every transformation in this file is a batch
   read over an existing Delta table, so every dataset here uses
   `@dp.materialized_view`, not `@dp.table` — using `@dp.table` for these
   would be silently wrong (not an error, just the wrong dataset type, and a
   naive port of old DLT tutorial code would get this wrong since legacy `dlt`
   used `@dlt.table` for both cases).
3. Tables defined earlier in this same pipeline are read with plain
   `spark.read.table("catalog.schema.table")` — no special `dp.read()` call
   exists or is needed; confirmed against current docs and community examples.
4. Pipeline-level configuration (the Sonnet-tier FM endpoint name) is read via
   `spark.conf.get(...)`, NOT by importing `app/config.py`. That module
   requires all 9 of its env vars (including Lakebase credentials) to resolve
   at import time — the wrong coupling for a pipeline that never touches
   Lakebase. Set `neurorx.fm_chat_endpoint` in the pipeline's Configuration
   field in the UI; a sane default is hardcoded as a fallback below.
5. `ai_query`'s `returnType` parameter accepts DDL type strings including a
   top-level `ARRAY<STRUCT<...>>` — confirmed against Databricks' own
   structured-extraction examples. Using this instead of `responseFormat`'s
   JSON-schema form gives a native Spark array-of-structs column directly
   explode-able with no separate `from_json` parsing step.
6. `databricks-claude-sonnet-5` (the default FM endpoint — see
   setup/00_workspace_runbook.md §2 and CLAUDE.md §4) rejects `temperature`,
   `top_p`, and `top_k` with an HTTP 400. No `modelParameters` are passed to
   `ai_query` below for exactly this reason — don't add one without checking
   this again first.

Layer policy per DATA_CONTRACTS.md §1: bronze warns, silver enforces.
`gold.interaction_pairs` is the one table where every expectation is
`FAIL UPDATE` — a bad row there is a missed drug interaction presented to a
patient as safety, not an ordinary data-quality problem.
"""

from pyspark import pipelines as dp
from pyspark.sql import functions as F
from pyspark.sql import types as T
from pyspark.sql.window import Window

# Task 1.6 delivered pipelines/chunking.py with a richer interface than
# originally sketched here: chunk_section(rxcui, set_id, drug_name, section,
# raw_texts) -> list[dict], where each dict is already a COMPLETE
# silver.label_sections row — chunk_id included, computed inside
# chunking.py per DATA_CONTRACTS.md §4.2's concat_ws formula (see that
# module's docstring for why it does NOT use the sha256 scheme its own task
# spec described — the frozen contract's human-readable format won, and
# this pipeline now consumes chunk_id directly rather than reconstructing
# it, which is what actually needs updating here as a result).
from chunking import chunk_section  # noqa: E402

CATALOG = "neurorx"

# Pipeline-level configuration (set in the pipeline's Configuration field in
# the UI). Falls back to the recommended default from
# setup/00_workspace_runbook.md §2 / CLAUDE.md §4 if unset, so this pipeline
# still runs before that field has been configured.
FM_CHAT_ENDPOINT = spark.conf.get("neurorx.fm_chat_endpoint", "databricks-claude-sonnet-5")

# DATA_CONTRACTS.md §1 — enum values and day-part boundaries, defined once
# here rather than repeated as string literals throughout this file.
VALID_SECTIONS = [
    "dosage_and_administration",
    "drug_interactions",
    "warnings",
    "information_for_patients",
]
VALID_SEVERITIES = ["major", "moderate", "minor", "unknown"]
VALID_DAY_PARTS = ["morning", "afternoon", "evening", "night"]

# Matches chunk_section()'s actual return shape — a list of complete
# silver.label_sections rows, chunk_id included.
_CHUNK_ROW_SCHEMA = T.ArrayType(T.StructType([
    T.StructField("chunk_id", T.StringType()),
    T.StructField("rxcui", T.StringType()),
    T.StructField("set_id", T.StringType()),
    T.StructField("drug_name", T.StringType()),
    T.StructField("section", T.StringType()),
    T.StructField("chunk_index", T.IntegerType()),
    T.StructField("chunk_text", T.StringType()),
    T.StructField("token_count", T.IntegerType()),
]))

_chunk_section_udf = F.udf(chunk_section, _CHUNK_ROW_SCHEMA)


def _day_part_expr(ts_col):
    """DATA_CONTRACTS.md §1 boundaries, local time. night wraps midnight
    (21:00-04:59), so it's the fallback else-branch rather than a single
    contiguous BETWEEN.
    """
    hour = F.hour(ts_col)
    return (
        F.when((hour >= 5) & (hour < 12), F.lit("morning"))
        .when((hour >= 12) & (hour < 17), F.lit("afternoon"))
        .when((hour >= 17) & (hour < 21), F.lit("evening"))
        .otherwise(F.lit("night"))
    )


# =============================================================================
# SILVER
# =============================================================================

@dp.materialized_view(
    # Schema-qualified deliberately: a single pipeline has ONE default target
    # schema (verified against current Databricks docs), but this pipeline
    # must publish to both `silver` and `gold`. Every materialized_view name
    # below is explicitly "schema.table" for exactly this reason — set the
    # pipeline's destination CATALOG to `neurorx` and leave schema unset;
    # don't rely on a pipeline-level default schema here. Table name matches
    # DATA_CONTRACTS.md §4.1 exactly: `neurorx.silver.drugs` (the Python
    # function name below differs on purpose — `name=` decouples them).
    name="silver.drugs",
    comment="Canonical drug dimension: one row per RxCUI. DATA_CONTRACTS.md §4.1.",
)
@dp.expect_or_drop("rxcui_present", "rxcui IS NOT NULL AND rxcui RLIKE '^[0-9]+$'")
@dp.expect_or_fail("rxcui_unique", "count(*) OVER (PARTITION BY rxcui) = 1")
@dp.expect_or_drop("generic_name_present", "generic_name IS NOT NULL AND length(generic_name) > 0")
@dp.expect("has_label", "primary_set_id IS NOT NULL")
def silver_drugs():
    # F11's canonical selection rule: pick the ingredient-level (tty='IN')
    # RxCUI per queried name. bronze.rxnorm_raw's grain is one row per
    # candidate (not per query), so in the rare case of >1 IN-tty row for one
    # name, take one deterministically — rxcui_unique (FAIL UPDATE above)
    # catches it if that ever collides with a different query's answer.
    rxnorm_raw = spark.read.table(f"{CATALOG}.bronze.rxnorm_raw")

    ingredient_rows = (
        rxnorm_raw.filter(F.col("tty") == "IN")
        .withColumn(
            "_rn",
            F.row_number().over(Window.partitionBy("query_name").orderBy("rxcui")),
        )
        .filter(F.col("_rn") == 1)
        .select(
            F.col("query_name"),
            F.col("rxcui"),
            F.lower(F.col("rxnorm_name")).alias("generic_name"),
        )
    )

    # Brand names (tty='BN') for the same queried name, if any were captured.
    # In practice this ingestion pipeline only ever queries plain ingredient
    # names (Task 1.2), so brand-name rows are rare-to-absent — an empty
    # array is the expected common case, per DATA_CONTRACTS.md §4.1's own
    # note ("Empty array when none; never NULL in practice").
    brand_names = (
        rxnorm_raw.filter(F.col("tty") == "BN")
        .groupBy("query_name")
        .agg(F.collect_set("rxnorm_name").alias("brand_names"))
    )

    # F10's canonical selection rule: most recent effective_time per queried
    # name, from the openFDA ingestion's sidecar metadata field (Task 1.1) —
    # not from openfda.rxcui, which lives at SPL-package granularity, not the
    # RxNorm ingredient level this table is keyed on.
    fda_labels_raw = spark.read.table(f"{CATALOG}.bronze.fda_labels_raw")

    primary_labels = (
        fda_labels_raw
        .withColumn("_query_name", F.col("payload")["_neurorx_ingestion_meta"]["queried_drug_name"].cast("string"))
        .filter(F.col("_query_name").isNotNull())
        .withColumn(
            "_rn",
            F.row_number().over(
                Window.partitionBy("_query_name").orderBy(F.col("effective_time").desc_nulls_last())
            ),
        )
        .filter(F.col("_rn") == 1)
        .select(F.col("_query_name").alias("query_name"), F.col("set_id").alias("primary_set_id"))
    )

    return (
        ingredient_rows
        .join(brand_names, on="query_name", how="left")
        .join(primary_labels, on="query_name", how="left")
        .select(
            F.col("rxcui"),
            F.col("generic_name"),
            F.coalesce(F.col("brand_names"), F.array()).alias("brand_names"),
            F.col("primary_set_id"),
        )
    )


@dp.materialized_view(
    name="silver.label_sections",  # DATA_CONTRACTS.md §4.2: neurorx.silver.label_sections
    comment="FDA label text split into section-aware, retrieval-sized chunks. DATA_CONTRACTS.md §4.2.",
)
@dp.expect_or_drop("chunk_id_present", "chunk_id IS NOT NULL")
@dp.expect_or_fail("chunk_id_unique", "count(*) OVER (PARTITION BY chunk_id) = 1")
@dp.expect_or_drop("rxcui_present", "rxcui IS NOT NULL")
@dp.expect_or_drop("section_recognized", f"section IN ({','.join(repr(s) for s in VALID_SECTIONS)})")
@dp.expect_or_drop("chunk_text_present", "chunk_text IS NOT NULL AND length(trim(chunk_text)) > 0")
@dp.expect_or_fail("token_count_ceiling", "token_count <= 1000")
@dp.expect("token_count_floor", "token_count >= 500")
def silver_label_sections():
    fda_labels_raw = spark.read.table(f"{CATALOG}.bronze.fda_labels_raw")
    drugs = spark.read.table(f"{CATALOG}.silver.drugs")

    labels_with_name = (
        fda_labels_raw
        .withColumn("_query_name", F.col("payload")["_neurorx_ingestion_meta"]["queried_drug_name"].cast("string"))
        .join(
            drugs.select(
                F.lower(F.col("generic_name")).alias("_query_name"),
                F.col("rxcui"),
                F.col("generic_name").alias("drug_name"),
            ),
            on="_query_name",
            how="inner",  # a label with no resolved rxcui can't be cited — silver.drugs is the gate
        )
    )

    # Unpivot the four target sections (each a payload array field, per Task
    # 1.1's verified openFDA structure) into one row per (set_id, section).
    # raw_texts stays an ARRAY<STRING> here — chunk_section() itself expects
    # openFDA's own raw list-of-strings shape and does the joining
    # internally, so no F.concat_ws pre-join happens on this side.
    section_cols = [
        F.struct(
            F.lit(section).alias("section"),
            F.col("payload")[section].cast(T.ArrayType(T.StringType())).alias("raw_texts"),
        )
        for section in VALID_SECTIONS
    ]

    exploded = (
        labels_with_name
        .withColumn("_sections", F.explode(F.array(*section_cols)))
        .select(
            "set_id", "rxcui", "drug_name",
            F.col("_sections.section").alias("section"),
            F.col("_sections.raw_texts").alias("raw_texts"),
        )
        .filter(F.col("raw_texts").isNotNull() & (F.size(F.col("raw_texts")) > 0))
    )

    chunked = exploded.withColumn(
        "_rows",
        _chunk_section_udf(F.col("rxcui"), F.col("set_id"), F.col("drug_name"), F.col("section"), F.col("raw_texts")),
    )

    # chunk_section() already returns complete rows (chunk_id included) —
    # explode and project straight through, no reconstruction needed here.
    return (
        chunked
        .withColumn("_row", F.explode("_rows"))
        .select(
            F.col("_row.chunk_id").alias("chunk_id"),
            F.col("_row.rxcui").alias("rxcui"),
            F.col("_row.set_id").alias("set_id"),
            F.col("_row.drug_name").alias("drug_name"),
            F.col("_row.section").alias("section"),
            F.col("_row.chunk_index").alias("chunk_index"),
            F.col("_row.chunk_text").alias("chunk_text"),
            F.col("_row.token_count").alias("token_count"),
        )
    )


# --- silver.interactions: two sources, unioned ------------------------------

@dp.temporary_view(comment="DDInter-sourced interactions, RxCUI-mapped and canonically ordered.")
def _interactions_ddinter():
    ddinter = spark.read.table(f"{CATALOG}.bronze.ddinter_raw")
    drugs = spark.read.table(f"{CATALOG}.silver.drugs").select(
        F.lower(F.col("generic_name")).alias("_name"), F.col("rxcui")
    )

    mapped = (
        ddinter
        .join(drugs.withColumnRenamed("_name", "_a_name").withColumnRenamed("rxcui", "rxcui_a"),
              F.lower(F.col("drug_a_name")) == F.col("_a_name"), "inner")
        .join(drugs.withColumnRenamed("_name", "_b_name").withColumnRenamed("rxcui", "rxcui_b"),
              F.lower(F.col("drug_b_name")) == F.col("_b_name"), "inner")
    )

    # §7 invariant: canonical order is LEXICOGRAPHIC (rxcui is STRING). See
    # CLAUDE.md §4 / DATA_CONTRACTS.md F1 — do not cast to a numeric type
    # here; that silently inverts the warfarin+ibuprofen pair.
    return mapped.select(
        F.least(F.col("rxcui_a"), F.col("rxcui_b")).alias("rxcui_a"),
        F.greatest(F.col("rxcui_a"), F.col("rxcui_b")).alias("rxcui_b"),
        F.lower(F.col("severity_level")).alias("severity"),
        F.lit(None).cast("string").alias("description"),  # DDInter bulk export has no description column (Task 1.3)
        F.lit("ddinter").alias("source"),
    )


@dp.temporary_view(
    comment="FDA-label-derived interactions via ai_query over drug_interactions sections.",
)
@dp.expect("other_drug_rxcui_resolved", "matched_rxcui IS NOT NULL")
def _interactions_fda_label_candidates():
    """Intermediate view (pre-filter): every ai_query-extracted interaction
    candidate, with a nullable matched_rxcui column. The `other_drug_rxcui_resolved`
    expectation above is a WARN (not a drop) applied at THIS stage specifically
    so its metric — the count of extracted drug names that aren't in our
    ~200-drug list — is captured in the pipeline's own data-quality metrics,
    per this task's "dropped, counted, not erroring" requirement. The actual
    drop happens one stage downstream, in _interactions_fda_label.
    """
    label_sections = spark.read.table(f"{CATALOG}.silver.label_sections")
    drugs = spark.read.table(f"{CATALOG}.silver.drugs").select(
        F.lower(F.col("generic_name")).alias("_name"), F.col("rxcui").alias("_drugs_rxcui")
    )

    interaction_sections = (
        label_sections
        .filter(F.col("section") == "drug_interactions")
        # One drug's drug_interactions section may be split into several
        # chunks (silver.label_sections' own chunking) — reassemble the full
        # section text per (rxcui, drug_name) before prompting, so the model
        # sees complete context rather than an arbitrary mid-section slice.
        .groupBy("rxcui", "drug_name")
        .agg(F.concat_ws(" ", F.collect_list("chunk_text")).alias("full_section_text"))
    )

    prompt_prefix = (
        "You are extracting drug-drug interaction facts from an FDA drug label's "
        "official Drug Interactions section. The source drug is: "
    )
    prompt_suffix = (
        ". Below is that drug's complete Drug Interactions section text. List every "
        "OTHER drug or drug class explicitly named as interacting with the source "
        "drug. For each, give: other_drug_name (the plain generic name as written), "
        "severity (exactly one of: major, moderate, minor, unknown - your best "
        "clinical judgment from the label's own wording), and description (a short, "
        "one-sentence factual paraphrase of what the label says about this specific "
        "interaction - do not add information not present in the text). If the text "
        "names no specific other drug, return an empty list. Text: "
    )

    # Built as a real Column (F.concat + F.lit) rather than interpolated
    # directly into the SQL expression string below: the prompt text above
    # contains an apostrophe ("label's"), which would prematurely terminate a
    # single-quoted SQL string literal if embedded via an f-string. Only
    # FM_CHAT_ENDPOINT and the returnType DDL (both apostrophe-free, trusted
    # config/constants) are embedded as literals in the ai_query() call
    # below; the actual prompt text flows through as a genuine Spark column.
    with_request = interaction_sections.withColumn(
        "_request",
        F.concat(F.lit(prompt_prefix), F.col("drug_name"), F.lit(prompt_suffix), F.col("full_section_text")),
    )

    extracted = with_request.withColumn(
        "_extracted",
        F.expr(
            f"""
            ai_query(
                '{FM_CHAT_ENDPOINT}',
                _request,
                returnType => 'ARRAY<STRUCT<other_drug_name:STRING, severity:STRING, description:STRING>>',
                failOnError => false
            )
            """
        ),
    )

    candidates = (
        extracted
        .withColumn("_c", F.explode(F.coalesce(F.col("_extracted"), F.array())))
        .select(
            F.col("rxcui").alias("source_rxcui"),
            F.col("drug_name").alias("source_drug_name"),
            F.col("_c.other_drug_name").alias("other_drug_name"),
            F.lower(F.col("_c.severity")).alias("severity"),
            F.col("_c.description").alias("description"),
        )
        .filter(F.col("other_drug_name").isNotNull())
    )

    return candidates.join(
        drugs, F.lower(F.trim(F.col("other_drug_name"))) == F.col("_name"), "left"
    ).withColumnRenamed("_drugs_rxcui", "matched_rxcui")


@dp.temporary_view(comment="FDA-label-derived interactions, RxCUI-mapped and canonically ordered.")
def _interactions_fda_label():
    candidates = spark.read.table("_interactions_fda_label_candidates")

    resolved = candidates.filter(F.col("matched_rxcui").isNotNull())

    return resolved.select(
        F.least(F.col("source_rxcui"), F.col("matched_rxcui")).alias("rxcui_a"),
        F.greatest(F.col("source_rxcui"), F.col("matched_rxcui")).alias("rxcui_b"),
        F.when(F.col("severity").isin(VALID_SEVERITIES), F.col("severity")).otherwise(F.lit("unknown")).alias("severity"),
        F.col("description"),
        F.lit("fda_label").alias("source"),
    )


@dp.materialized_view(
    name="silver.interactions",  # DATA_CONTRACTS.md §4.3: neurorx.silver.interactions
    comment="Interaction pairs resolved to RxCUI, canonically ordered. One row per source. DATA_CONTRACTS.md §4.3.",
)
@dp.expect_or_drop("both_rxcui_present", "rxcui_a IS NOT NULL AND rxcui_b IS NOT NULL")
@dp.expect_or_fail("canonical_order", "rxcui_a < rxcui_b")
@dp.expect_or_drop("not_self_pair", "rxcui_a != rxcui_b")
@dp.expect_or_drop("severity_recognized", f"severity IN ({','.join(repr(s) for s in VALID_SEVERITIES)})")
@dp.expect_or_drop("source_recognized", "source IN ('ddinter', 'fda_label')")
@dp.expect_or_fail("pair_source_unique", "count(*) OVER (PARTITION BY rxcui_a, rxcui_b, source) = 1")
def silver_interactions():
    ddinter = spark.read.table("_interactions_ddinter")
    fda_label = spark.read.table("_interactions_fda_label")
    return ddinter.unionByName(fda_label)


# =============================================================================
# GOLD
# =============================================================================

@dp.materialized_view(
    name="gold.drug_knowledge",  # DATA_CONTRACTS.md §5.1: neurorx.gold.drug_knowledge
    comment="Vector Search source table — one row per citable chunk. DATA_CONTRACTS.md §5.1.",
    table_properties={"delta.enableChangeDataFeed": "true"},  # mandatory: DELTA_SYNC index requires CDF (Free Edition has no Direct Vector Access)
)
@dp.expect_or_fail("chunk_id_unique", "count(*) OVER (PARTITION BY chunk_id) = 1")
@dp.expect_or_drop(
    "citation_fields_complete",
    "chunk_id IS NOT NULL AND rxcui IS NOT NULL AND drug_name IS NOT NULL AND section IS NOT NULL AND set_id IS NOT NULL",
)
@dp.expect_or_drop("chunk_text_present", "chunk_text IS NOT NULL AND length(trim(chunk_text)) > 0")
def drug_knowledge():
    return spark.read.table(f"{CATALOG}.silver.label_sections").select(
        "chunk_id", "rxcui", "drug_name", "section", "chunk_text", "set_id"
    )


@dp.materialized_view(
    name="gold.interaction_pairs",  # DATA_CONTRACTS.md §5.2: neurorx.gold.interaction_pairs
    comment="THE deterministic safety table. check_interactions queries this and nothing else. DATA_CONTRACTS.md §5.2.",
)
@dp.expect_all_or_fail({
    "pair_unique": "count(*) OVER (PARTITION BY rxcui_a, rxcui_b) = 1",
    "canonical_order": "rxcui_a < rxcui_b",
    "severity_recognized": f"severity IN ({','.join(repr(s) for s in VALID_SEVERITIES)})",
    "sources_non_empty": "size(sources) >= 1",
    # Verified live against the RxNav API (CLAUDE.md §4): warfarin=11289,
    # ibuprofen=5640. This is the Phase 1 exit checkpoint and the eval set's
    # headline true-positive — if this expectation ever fails, the
    # lexicographic pair-ordering invariant (§7 / F1) has been broken
    # somewhere upstream.
    "warfarin_ibuprofen_present": (
        "EXISTS (SELECT 1 FROM (SELECT rxcui_a, rxcui_b FROM neurorx.gold.interaction_pairs "
        "WHERE rxcui_a = '11289' AND rxcui_b = '5640'))"
    ),
})
def interaction_pairs():
    # F6's resolution: ddinter wins over fda_label; on a tie, higher severity
    # wins. Every expectation on this table is FAIL UPDATE, not warn/drop —
    # deliberately different from every other table in this pipeline. A bad
    # row here is a missed interaction shown to a patient as safety.
    severity_rank = F.expr(
        "CASE severity WHEN 'major' THEN 3 WHEN 'moderate' THEN 2 WHEN 'minor' THEN 1 ELSE 0 END"
    )
    source_rank = F.expr("CASE source WHEN 'ddinter' THEN 2 WHEN 'fda_label' THEN 1 ELSE 0 END")

    interactions = spark.read.table(f"{CATALOG}.silver.interactions")

    ranked = interactions.withColumn("_severity_rank", severity_rank).withColumn("_source_rank", source_rank)

    winner = (
        ranked
        .withColumn(
            "_rn",
            F.row_number().over(
                Window.partitionBy("rxcui_a", "rxcui_b").orderBy(
                    F.col("_source_rank").desc(), F.col("_severity_rank").desc()
                )
            ),
        )
        .filter(F.col("_rn") == 1)
        .select("rxcui_a", "rxcui_b", "severity", "description")
    )

    sources = (
        interactions.groupBy("rxcui_a", "rxcui_b")
        .agg(F.collect_set("source").alias("sources"))
    )

    return (
        winner.join(sources, on=["rxcui_a", "rxcui_b"], how="inner")
        .withColumn("checked_at", F.current_timestamp())
    )


# TODO(Phase 3, blocked on Task 3.8): flip this constant to the Lakebase-
# synced gold table once the synthetic cohort is loaded INTO Lakebase (Task
# 3.8, superseding this file's direct-to-bronze path) and Lakebase CDF is
# running (Task 3.2, lakebase/sync_setup.md). Do NOT flip this yet — nothing
# populates gold.dose_events_synced until Task 3.8 lands; flipping now would
# just make this pipeline read an empty table.
#
# Table name corrected from an earlier version of this comment: the target
# is neurorx.gold.dose_events_synced (Task 3.2's naming — see
# lakebase/sync_setup.md's flagged naming conflict with DATA_CONTRACTS.md
# §9, which uses the bare name gold.dose_events), NOT bare gold.dose_events.
#
# This is NOT a one-line change despite what an earlier version of this
# comment claimed: gold.dose_events_synced has no rxcui column
# (DATA_CONTRACTS.md §6.3's dose_events never had one — the bronze synthetic
# table below denormalizes it in only for audit convenience, per §3.5).
# adherence_facts() will need a join to schedules (on schedule_id) to
# recover rxcui once this flips — add that join in the same commit that
# flips SOURCE_TABLE, not after the pipeline fails on a missing column.
SOURCE_TABLE = f"{CATALOG}.bronze.synthetic_dose_events_raw"  # Phase 1
# SOURCE_TABLE = f"{CATALOG}.gold.dose_events_synced"  # Phase 3 — needs a schedules join for rxcui, see above


@dp.materialized_view(
    name="gold.adherence_facts",  # DATA_CONTRACTS.md §5.3: neurorx.gold.adherence_facts
    comment="Adherence aggregates for dashboard/Genie/get_adherence_stats. DATA_CONTRACTS.md §5.3.",
)
@dp.expect_all_or_fail({
    "grain_unique": "count(*) OVER (PARTITION BY patient_id, rxcui, event_date, day_part) = 1",
    "counts_reconcile": "taken_doses + skipped_doses + missed_doses <= planned_doses",
    "adherence_pct_bounded": "adherence_pct BETWEEN 0 AND 100",
    "adherence_pct_consistent": (
        "planned_doses = 0 OR abs(adherence_pct - (taken_doses / planned_doses * 100)) < 0.01"
    ),
})
@dp.expect_or_drop("day_part_recognized", f"day_part IN ({','.join(repr(d) for d in VALID_DAY_PARTS)})")
@dp.expect_or_drop(
    "counts_non_negative",
    "planned_doses >= 0 AND taken_doses >= 0 AND skipped_doses >= 0 AND missed_doses >= 0",
)
def adherence_facts():
    dose_events = spark.read.table(SOURCE_TABLE)
    drugs = spark.read.table(f"{CATALOG}.silver.drugs").select("rxcui", "generic_name")

    with_parts = dose_events.select(
        "patient_id",
        "rxcui",
        F.to_date("planned_ts").alias("event_date"),
        _day_part_expr(F.col("planned_ts")).alias("day_part"),
        "status",
    )

    aggregated = with_parts.groupBy("patient_id", "rxcui", "event_date", "day_part").agg(
        F.count(F.lit(1)).alias("planned_doses"),
        F.sum(F.when(F.col("status") == "taken", 1).otherwise(0)).alias("taken_doses"),
        F.sum(F.when(F.col("status") == "skipped", 1).otherwise(0)).alias("skipped_doses"),
        F.sum(F.when(F.col("status") == "missed", 1).otherwise(0)).alias("missed_doses"),
    )

    return (
        aggregated.join(drugs, on="rxcui", how="left")
        .withColumn("drug_name", F.col("generic_name"))
        .withColumn(
            "adherence_pct",
            F.round(F.col("taken_doses") / F.nullif(F.col("planned_doses"), F.lit(0)) * 100, 4),
        )
        .select(
            "patient_id", "rxcui", "drug_name", "event_date", "day_part",
            "planned_doses", "taken_doses", "skipped_doses", "missed_doses", "adherence_pct",
        )
    )
