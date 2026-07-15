-- Databricks notebook source
-- MAGIC %md
-- MAGIC # NeuroRx AI — Unity Catalog setup
-- MAGIC
-- MAGIC Phase 0 foundation. Idempotently creates the catalog, five schemas, and the
-- MAGIC bronze landing volume defined as non-negotiable in `ARCHITECTURE.md` §4
-- MAGIC (Naming conventions). Safe to re-run: every statement is `IF NOT EXISTS`.
-- MAGIC
-- MAGIC Run this on the workspace's serverless SQL warehouse (Free Edition provides
-- MAGIC exactly one, pre-created — see `setup/00_workspace_runbook.md` §1). Requires
-- MAGIC `CREATE CATALOG` privilege on the metastore, which the workspace owner has
-- MAGIC by default on a solo Free Edition workspace.

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 1. Catalog

-- COMMAND ----------

CREATE CATALOG IF NOT EXISTS neurorx
COMMENT 'NeuroRx AI — medication schedule assistant. Databricks Hackathon (Devpost) entry. See ARCHITECTURE.md.';

-- COMMAND ----------

USE CATALOG neurorx;

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 2. Schemas
-- MAGIC
-- MAGIC Medallion layers (`bronze`, `silver`, `gold`) plus two functional schemas:
-- MAGIC `app` (UC-function tools the agent calls — see ARCHITECTURE.md §5) and
-- MAGIC `evals` (the 60-case MLflow evaluation set — see ARCHITECTURE.md §6).

-- COMMAND ----------

CREATE SCHEMA IF NOT EXISTS neurorx.bronze
COMMENT 'Raw, as-ingested data: openFDA labels, RxNorm, DDInter, synthetic patient cohort.';

CREATE SCHEMA IF NOT EXISTS neurorx.silver
COMMENT 'Normalized on RxCUI; label sections chunked; interaction pairs deduped.';

CREATE SCHEMA IF NOT EXISTS neurorx.gold
COMMENT 'drug_knowledge, interaction_pairs, adherence_facts — serves the agent tools and Genie.';

CREATE SCHEMA IF NOT EXISTS neurorx.app
COMMENT 'UC-function tools: manage_schedule, search_drug_labels, check_interactions, get_adherence_stats.';

CREATE SCHEMA IF NOT EXISTS neurorx.evals
COMMENT '60-case MLflow evaluation set: grounded QA, interactions, schedule manipulation, adversarial safety.';

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 3. Bronze landing volume
-- MAGIC
-- MAGIC Managed volume for raw file drops (openFDA JSON pulls, RxNorm/DDInter exports)
-- MAGIC ahead of Lakeflow pipeline ingestion in Phase 1.

-- COMMAND ----------

CREATE VOLUME IF NOT EXISTS neurorx.bronze.raw_files
COMMENT 'Raw file landing zone: openFDA label JSON, RxNorm exports, DDInter exports.';

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 4. Verify

-- COMMAND ----------

SHOW SCHEMAS IN neurorx;

-- COMMAND ----------

SHOW VOLUMES IN neurorx.bronze;

-- COMMAND ----------

DESCRIBE VOLUME neurorx.bronze.raw_files;

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 5. Grants
-- MAGIC
-- MAGIC **This is a solo hackathon build on Databricks Free Edition.** The workspace
-- MAGIC has one user (the owner) and no account console, so default owner
-- MAGIC permissions already grant full rights on everything created above — nothing
-- MAGIC further to run. The block below is commented out; it is **not part of the
-- MAGIC Phase 0 setup** and exists only to document what production-grade
-- MAGIC governance on this catalog would look like once there is more than one
-- MAGIC principal (a second teammate, an app service principal, a read-only
-- MAGIC analyst).

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ```sql
-- MAGIC -- ============================================================
-- MAGIC -- PRODUCTION GRANTS (illustrative — not executed in Phase 0)
-- MAGIC -- ============================================================
-- MAGIC
-- MAGIC -- The Databricks App's service principal needs to invoke the four
-- MAGIC -- agent tools and read gold tables for the dashboard/Genie, but should
-- MAGIC -- never see raw bronze data or modify the catalog structure.
-- MAGIC -- GRANT USE CATALOG ON CATALOG neurorx TO `neurorx-app-sp`;
-- MAGIC -- GRANT USE SCHEMA ON SCHEMA neurorx.app TO `neurorx-app-sp`;
-- MAGIC -- GRANT EXECUTE ON SCHEMA neurorx.app TO `neurorx-app-sp`;
-- MAGIC -- GRANT USE SCHEMA ON SCHEMA neurorx.gold TO `neurorx-app-sp`;
-- MAGIC -- GRANT SELECT ON SCHEMA neurorx.gold TO `neurorx-app-sp`;
-- MAGIC
-- MAGIC -- A caregiver-analytics/Genie consumer group: read-only on gold, no
-- MAGIC -- access to bronze/silver (which may carry pre-normalization noise)
-- MAGIC -- or to app (which carries tool-invocation logic, not data).
-- MAGIC -- GRANT USE CATALOG ON CATALOG neurorx TO `neurorx-analysts`;
-- MAGIC -- GRANT USE SCHEMA ON SCHEMA neurorx.gold TO `neurorx-analysts`;
-- MAGIC -- GRANT SELECT ON SCHEMA neurorx.gold TO `neurorx-analysts`;
-- MAGIC
-- MAGIC -- A data-engineering group owns the medallion pipeline end to end but
-- MAGIC -- has no reason to touch the app/tools layer.
-- MAGIC -- GRANT USE CATALOG ON CATALOG neurorx TO `neurorx-data-eng`;
-- MAGIC -- GRANT ALL PRIVILEGES ON SCHEMA neurorx.bronze TO `neurorx-data-eng`;
-- MAGIC -- GRANT ALL PRIVILEGES ON SCHEMA neurorx.silver TO `neurorx-data-eng`;
-- MAGIC -- GRANT ALL PRIVILEGES ON SCHEMA neurorx.gold TO `neurorx-data-eng`;
-- MAGIC
-- MAGIC -- Nobody outside data-eng should read PHI-adjacent bronze/silver
-- MAGIC -- directly, even though the data is 100% synthetic in this project
-- MAGIC -- (ARCHITECTURE.md §5 PHI stance) — the access pattern should still
-- MAGIC -- model what a real deployment would require.
-- MAGIC -- REVOKE SELECT ON SCHEMA neurorx.bronze FROM `neurorx-analysts`;
-- MAGIC -- REVOKE SELECT ON SCHEMA neurorx.silver FROM `neurorx-analysts`;
-- MAGIC -- ============================================================
-- MAGIC ```
