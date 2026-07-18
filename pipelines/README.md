# Lakeflow Declarative Pipeline — bronze → silver → gold

[`medallion_pipeline.py`](medallion_pipeline.py) builds every silver and gold table specified in [`DATA_CONTRACTS.md`](../DATA_CONTRACTS.md) §4–§5 from the bronze tables Tasks 1.1–1.4 produce. This is the deterministic safety core's foundation — see [`ARCHITECTURE.md`](../ARCHITECTURE.md) §5.

> ✅ **`pipelines/chunking.py` (Task 1.6) is now delivered.** `medallion_pipeline.py` imports `chunk_section(rxcui, set_id, drug_name, section, raw_texts)` from it — a pure, dependency-free module with its own `if __name__ == "__main__"` self-test (run `python3 pipelines/chunking.py` directly to verify it). One thing worth knowing: `chunking.py` computes `chunk_id` using `DATA_CONTRACTS.md` §4.2's frozen `concat_ws` formula, not the sha256 scheme Task 1.6's own instructions described — see that file's module docstring for why the frozen contract won.

---

## Why this file uses `@dp.materialized_view`, not `@dp.table`

Verified against current Databricks documentation before writing this pipeline (Lakeflow's Python API was renamed during the "Lakeflow Declarative Pipelines" rebrand, and the old `dlt`-based tutorials still circulating get this specific point wrong for new code):

- Import is `from pyspark import pipelines as dp` — the modern, actively-recommended API. The legacy `dlt` module still works for backward compatibility but isn't used here.
- **`@dp.table` is for streaming reads only.** Every transformation in this pipeline is a batch read over an existing Delta table, so every dataset uses `@dp.materialized_view`. Using `@dp.table` here would silently produce the wrong dataset type — not an error, just wrong, and exactly the mistake a naive port of an old DLT tutorial would make.
- Tables defined earlier in this same pipeline are read with plain `spark.read.table("catalog.schema.table")` — no special `dp.read()` call exists.
- Six of the datasets are explicitly **schema-qualified** in their `name=` parameter (e.g. `name="silver.drugs"`, `name="gold.interaction_pairs"`) rather than left bare. This is deliberate: a single pipeline has **one default target schema** (confirmed against current docs), but this pipeline must publish to both `silver` and `gold`. Every `@dp.materialized_view` name is schema-qualified for exactly this reason.

See the pipeline file's own module docstring for the complete list of verified API facts (`ai_query` syntax, the Sonnet-5 `modelParameters` gotcha, why `app/config.py` isn't imported here).

---

## Create the pipeline in the workspace UI

1. Clone/attach this repo as a **Databricks Git folder** first (rather than pasting code into the in-browser editor) — this lets the pipeline reference `medallion_pipeline.py` and `chunking.py` directly as source files, and keeps them version-controlled. Workspace → Git folders → add this repo.
2. Left sidebar → **Jobs & Pipelines**.
3. Click the **+** (create) icon → **ETL Pipeline**. A pipeline editor opens with a default name.
4. Rename it (e.g. `neurorx-medallion`).
5. In the pipeline's **catalog and schema** dropdowns, set:
   - **Catalog:** `neurorx`
   - **Schema:** leave at whatever default the UI requires, but **do not rely on it** — every table this pipeline publishes is explicitly schema-qualified (`silver.drugs`, `gold.interaction_pairs`, etc.) in code specifically so the pipeline's default schema setting doesn't matter for where tables actually land.
6. In the pipeline's **asset browser**, add the existing source files from the Git folder rather than creating new ones: `pipelines/medallion_pipeline.py` and `pipelines/chunking.py`. Lakeflow supports Python modules alongside pipeline source files in the same folder — `chunking.py` is imported by `medallion_pipeline.py` as a plain same-folder module.
7. **Compute:** Free Edition and Unity-Catalog-enabled workspaces default to serverless — no explicit compute selection is needed.
8. **Configuration** (pipeline settings, not a table): add a key `neurorx.fm_chat_endpoint` with the value of your workspace's chosen Claude endpoint (see `setup/00_workspace_runbook.md` §2 for how to find it; `databricks-claude-sonnet-5` is the default this pipeline falls back to if the key is unset). This is read via `spark.conf.get(...)` inside the pipeline — not by importing `app/config.py`, which would require Lakebase credentials this pipeline never needs.
9. Click **Run pipeline** (the play icon) to execute a full refresh.

## Verify the Phase 1 exit checkpoint

Once the pipeline has run successfully:

```sql
-- (a) warfarin+ibuprofen interaction, queryable with severity
SELECT * FROM neurorx.gold.interaction_pairs
WHERE rxcui_a = '11289' AND rxcui_b = '5640';
-- expect exactly one row, severity != 'unknown'
```

The `warfarin_ibuprofen_present` expectation on `gold.interaction_pairs` already asserts this on every pipeline run (`FAIL UPDATE` — the whole run halts if it's missing), so a successful run is itself evidence this checkpoint passes.

For the vector-search half of the checkpoint (metformin missed-dose chunk retrievable), `gold.drug_knowledge` needs a `DELTA_SYNC` Vector Search index built on top of it first — that's a separate, later step (not part of this pipeline), since Free Edition's one-endpoint quota means the index should be created against the real table, not an empty placeholder (see `setup/00_workspace_runbook.md` §3).

## Data-quality metrics

Every expectation in this file shows up in the pipeline's own **Data quality** tab in the UI after a run — this is where to check the `other_drug_rxcui_resolved` metric (how many `ai_query`-extracted drug names didn't match our curated list) and every `warn`-tier expectation's pass rate, rather than hunting for `print()` output. `gold.interaction_pairs` is the one table where every expectation is `FAIL UPDATE` — if the pipeline run fails there, that's by design: a bad row on that table is a missed drug interaction presented to a patient as safety, and the pipeline is built to halt rather than publish it.
