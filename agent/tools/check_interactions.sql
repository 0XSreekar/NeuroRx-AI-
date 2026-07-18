-- Databricks notebook source
-- MAGIC %md
-- MAGIC # `neurorx.app.check_interactions` — the deterministic safety core
-- MAGIC
-- MAGIC ============================================================
-- MAGIC DESIGN RULE (verbatim, per ARCHITECTURE.md §5(a)):
-- MAGIC
-- MAGIC   Interaction detection is a table lookup. The LLM explains
-- MAGIC   results; it never produces them.
-- MAGIC ============================================================
-- MAGIC
-- MAGIC This function is pure SQL over `neurorx.gold.interaction_pairs` — no
-- MAGIC `ai_query`, no model call, no LLM involvement of any kind, enforced by
-- MAGIC construction (there is nothing in this file capable of calling a model).
-- MAGIC The agent's only role, once this function returns, is translating rows
-- MAGIC that already exist into plain language and citing `source`. Per
-- MAGIC `ARCHITECTURE.md` §5(a), `manage_schedule`'s `add_drug` action must call
-- MAGIC this function first, enforced in that tool's own code — not by prompt
-- MAGIC instruction, which is not an enforcement mechanism.
-- MAGIC
-- MAGIC ## Two things flagged rather than silently resolved
-- MAGIC
-- MAGIC 1. **`COMMENT ON FUNCTION` is not valid Databricks SQL syntax.** Checked
-- MAGIC    directly against the current `COMMENT ON` reference: the object list is
-- MAGIC    `CATALOG | COLUMN | CONNECTION | PROVIDER | RECIPIENT | SCHEMA | SHARE |
-- MAGIC    TABLE | VOLUME` — `FUNCTION` is not among them. The mechanism this task's
-- MAGIC    requirement #4 actually needs — a natural-language description the agent
-- MAGIC    framework surfaces as the tool spec — is the **inline `COMMENT` clause**
-- MAGIC    inside `CREATE FUNCTION` (confirmed against the current `CREATE FUNCTION`
-- MAGIC    reference and its worked example). Used below on the function itself,
-- MAGIC    every parameter, and every output column.
-- MAGIC 2. **The requested output column is `source STRING`, but the frozen contract's
-- MAGIC    real column is `sources ARRAY<STRING>`.** `DATA_CONTRACTS.md` §5.2
-- MAGIC    explicitly changed this from a scalar to an array (its own F6 note) so a
-- MAGIC    pair attested by both DDInter and an FDA label reports both, not one
-- MAGIC    arbitrarily. Rather than silently return the array under a mismatched
-- MAGIC    name, or silently rename the output away from what was asked, this
-- MAGIC    function returns `array_join(sources, ', ')` — a STRING named `source`,
-- MAGIC    matching the requested shape exactly, derived transparently from the
-- MAGIC    real array so nothing is lost (a pair from both sources reads as
-- MAGIC    `"ddinter, fda_label"` rather than picking one).
-- MAGIC
-- MAGIC ## One thing not independently executed
-- MAGIC
-- MAGIC The pairing subquery below uses `posexplode(rxcui_list)` invoked twice as a
-- MAGIC table reference (the current, non-deprecated form — `LATERAL VIEW
-- MAGIC posexplode(...)` is documented as deprecated in favor of this). This
-- MAGIC exact pattern (`FROM posexplode(arr) AS p1, posexplode(arr) AS p2`) is
-- MAGIC confirmed correct Spark SQL against the current `posexplode` reference and
-- MAGIC its own worked example, but — same standing caveat as every other SQL file
-- MAGIC in this project — has not been run against a live workspace, so its
-- MAGIC behavior specifically *inside* a `CREATE FUNCTION ... RETURN` body is
-- MAGIC verified by documentation, not by execution.

-- COMMAND ----------

CREATE OR REPLACE FUNCTION neurorx.app.check_interactions(
  rxcui_list ARRAY<STRING>
    COMMENT 'RxCUIs to check against each other for known drug-drug interactions. Pass every drug currently on the patient schedule plus any drug being newly added — order does not matter and duplicates are harmless. Call this any time a drug is added to a schedule (before the write, per ARCHITECTURE.md §5(a) — manage_schedule enforces this), and any time the user directly asks whether two or more of their drugs interact.'
)
RETURNS TABLE (
  rxcui_a     STRING COMMENT 'Canonically-ordered first RxCUI of the pair.',
  drug_a      STRING COMMENT 'Generic name of rxcui_a.',
  rxcui_b     STRING COMMENT 'Canonically-ordered second RxCUI of the pair.',
  drug_b      STRING COMMENT 'Generic name of rxcui_b.',
  severity    STRING COMMENT 'One of: major, moderate, minor, unknown. Rows are ordered major-first.',
  description STRING COMMENT 'Human-readable description of the interaction, from the winning source per DATA_CONTRACTS.md F6 (ddinter over fda_label; higher severity wins a tie). This is reference text the agent may paraphrase — it is not itself a citation; it does not carry a chunk_id.',
  source      STRING COMMENT 'Which reference source(s) attest this interaction, comma-separated (e.g. "ddinter" or "ddinter, fda_label"). Cite this alongside the interaction claim per DATA_CONTRACTS.md §8 point 3 — interaction claims cite sources, not a chunk_id.'
)
COMMENT 'Deterministic drug-drug interaction lookup — a table query against neurorx.gold.interaction_pairs, never an LLM judgment. Call this whenever a drug is being added to a patient schedule (required before the write completes) or whenever the user asks if their medications interact with each other or with a specific drug. Pass every RxCUI currently on the schedule plus any drug under consideration; the function checks all pairs among them and returns one row per known interacting pair, ordered with major severity first. IMPORTANT: an empty result set means no interaction was found in this reference data (DDInter plus FDA label text) — it does NOT mean the combination is safe. Always tell the user retrieval found nothing rather than asserting safety, and direct them to their pharmacist for anything not covered here.'
RETURN
  WITH pairs AS (
    SELECT DISTINCT
      LEAST(p1.col, p2.col)    AS rxcui_a,
      GREATEST(p1.col, p2.col) AS rxcui_b
    FROM posexplode(rxcui_list) AS p1, posexplode(rxcui_list) AS p2
    WHERE p1.pos < p2.pos
  )
  SELECT
    ip.rxcui_a,
    da.generic_name AS drug_a,
    ip.rxcui_b,
    db.generic_name AS drug_b,
    ip.severity,
    ip.description,
    array_join(ip.sources, ', ') AS source
  FROM pairs p
  INNER JOIN neurorx.gold.interaction_pairs ip
    ON ip.rxcui_a = p.rxcui_a AND ip.rxcui_b = p.rxcui_b
  INNER JOIN neurorx.silver.drugs da ON da.rxcui = ip.rxcui_a
  INNER JOIN neurorx.silver.drugs db ON db.rxcui = ip.rxcui_b
  ORDER BY
    CASE ip.severity
      WHEN 'major'    THEN 1
      WHEN 'moderate' THEN 2
      WHEN 'minor'    THEN 3
      ELSE 4
    END;

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## Test cell
-- MAGIC
-- MAGIC RxCUIs verified live against the RxNav API (`CLAUDE.md` §4): warfarin
-- MAGIC `11289`, ibuprofen `5640`, metformin `6809`. Metformin has no known
-- MAGIC interaction with the other two in this reference data, so it's included
-- MAGIC specifically to prove the function doesn't over-match — the result should
-- MAGIC contain exactly the warfarin+ibuprofen row and nothing else.

-- COMMAND ----------

SELECT * FROM neurorx.app.check_interactions(ARRAY('11289', '5640', '6809'));

-- COMMAND ----------

-- MAGIC %python
-- MAGIC result = spark.sql(
-- MAGIC     "SELECT * FROM neurorx.app.check_interactions(ARRAY('11289', '5640', '6809'))"
-- MAGIC ).collect()
-- MAGIC
-- MAGIC assert len(result) >= 1, (
-- MAGIC     "Expected at least one interaction row for warfarin+ibuprofen — got none. "
-- MAGIC     "Check that neurorx.gold.interaction_pairs actually contains the "
-- MAGIC     "warfarin_ibuprofen_present canary row (DATA_CONTRACTS.md §5.2)."
-- MAGIC )
-- MAGIC
-- MAGIC warfarin_ibuprofen = [
-- MAGIC     row for row in result
-- MAGIC     if {row["rxcui_a"], row["rxcui_b"]} == {"11289", "5640"}
-- MAGIC ]
-- MAGIC assert len(warfarin_ibuprofen) == 1, (
-- MAGIC     f"Expected exactly one warfarin+ibuprofen row, found {len(warfarin_ibuprofen)}. "
-- MAGIC     f"Full result: {result}"
-- MAGIC )
-- MAGIC
-- MAGIC row = warfarin_ibuprofen[0]
-- MAGIC assert row["severity"] != "unknown", f"Expected a real severity, got {row['severity']!r}"
-- MAGIC assert row["rxcui_a"] == "11289" and row["rxcui_b"] == "5640", (
-- MAGIC     f"Canonical order violated: rxcui_a={row['rxcui_a']!r}, rxcui_b={row['rxcui_b']!r} "
-- MAGIC     f"— expected lexicographic ('11289', '5640') per DATA_CONTRACTS.md F1."
-- MAGIC )
-- MAGIC
-- MAGIC print(f"PASSED: warfarin+ibuprofen found, severity={row['severity']!r}, source={row['source']!r}")
-- MAGIC print(f"Total interacting pairs among [warfarin, ibuprofen, metformin]: {len(result)}")
-- MAGIC for r in result:
-- MAGIC     print(f"  {r['drug_a']} ({r['rxcui_a']}) + {r['drug_b']} ({r['rxcui_b']}): "
-- MAGIC           f"{r['severity']} [{r['source']}]")
