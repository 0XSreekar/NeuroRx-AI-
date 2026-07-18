-- app/genie_assets.sql — Genie Agent setup assets (Task 5.1)
--
-- Two things in this one file:
--   PART A: COMMENT ON statements — real Unity Catalog DDL, run this against a SQL
--           warehouse before creating the Genie Agent (see genie_setup.md §1/§2).
--   PART B: the four trusted-asset "Example SQL query" bodies — NOT standalone
--           executable DDL. There is no CREATE TRUSTED ASSET statement (confirmed live
--           this session against current docs — trusted assets are UI-only, added via
--           Configure → Instructions → SQL Queries). Each block below is exactly what
--           to paste into that SQL query editor, title included as a comment above it.
--
-- See genie_setup.md for the full runbook, the "Genie Space" → "Genie Agent" rename,
-- and the "certified answer" → "trusted asset" terminology correction — both verified
-- live this session, neither assumed from the task brief's own wording.

-- =============================================================================
-- PART A — table and column comments, so Genie Code's auto-suggested descriptions
-- start from the right answer instead of a guess. Written to resolve the specific
-- ambiguities a model reading raw column names would get wrong — not a restatement
-- of DATA_CONTRACTS.md's full descriptions, which are for engineers, not for framing
-- what a caregiver's natural-language question should map to.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- neurorx.gold.adherence_facts
-- ---------------------------------------------------------------------------

COMMENT ON TABLE neurorx.gold.adherence_facts IS
    'One row per patient, per drug, per day-part, per calendar date. A twice-daily drug produces two rows per day (morning + evening), not one — always aggregate with SUM(), never assume one row equals one day. adherence_pct is precomputed per row as taken_doses / planned_doses * 100; to answer a question spanning multiple rows (a week, a month, all drugs), recompute from SUM(taken_doses) / SUM(planned_doses) * 100 rather than averaging the per-row adherence_pct values, which would incorrectly weight a once-daily drug the same as a twice-daily one.';

COMMENT ON COLUMN neurorx.gold.adherence_facts.day_part IS
    'Which part of the day this dose was scheduled for, bucketed from the scheduled time: morning = 05:00-11:59, afternoon = 12:00-16:59, evening = 17:00-20:59, night = 21:00-04:59 (wraps past midnight). Fixed boundaries, the same ones the patient-facing Today view and dashboard use — never redefine these ad hoc in a query.';

COMMENT ON COLUMN neurorx.gold.adherence_facts.planned_doses IS
    'How many doses of this drug were scheduled in this day-part on this date. The denominator for adherence.';

COMMENT ON COLUMN neurorx.gold.adherence_facts.taken_doses IS
    'How many of the planned doses the patient marked as taken (confirmed action, not just "not missed").';

COMMENT ON COLUMN neurorx.gold.adherence_facts.skipped_doses IS
    'How many of the planned doses the patient deliberately marked as skipped (an intentional non-take, e.g. "skipping today per doctor instruction") — distinct from missed, which is no action at all. Skips count against adherence_pct (a skip is still not a taken dose), but are tracked separately so "deliberately skipped" and "simply forgotten" are never conflated in an answer.';

COMMENT ON COLUMN neurorx.gold.adherence_facts.missed_doses IS
    'How many of the planned doses had no recorded action (neither taken nor skipped) by the end of the tracked window — the closest thing to "forgotten." This is the column "which drug/time does the patient miss most" should rank by, not skipped_doses or the inverse of adherence_pct.';

COMMENT ON COLUMN neurorx.gold.adherence_facts.adherence_pct IS
    'taken_doses / planned_doses * 100 for THIS row only (one drug, one day-part, one date). Skipped and missed doses both count against it. Do not average this column across rows to get an overall rate — sum the numerator and denominator first (see table comment).';

-- ---------------------------------------------------------------------------
-- neurorx.gold.schedules_synced
-- ---------------------------------------------------------------------------

COMMENT ON TABLE neurorx.gold.schedules_synced IS
    'A patient prescription list, current state only (not history) — reconstructed from Lakebase Change Data Feed, refreshed on a materialized-view schedule Databricks manages (see lakebase/sync.sql for the exact cadence). "Current drugs" or "active medications" means the status column equals active; a row where status equals stopped is a drug no longer being taken and must be excluded from anything answering what a patient is currently on.';

COMMENT ON COLUMN neurorx.gold.schedules_synced.status IS
    'Value active means currently prescribed and taken; value stopped means discontinued (soft-deleted, never a hard delete — the row is kept for history). Always filter to the active value when a question is about current medications for a patient.';

COMMENT ON COLUMN neurorx.gold.schedules_synced.rxcui IS
    'The canonical RxNorm drug identifier — join key to silver.drugs.rxcui and to interaction_pairs.rxcui_a/rxcui_b for anything involving drug names or interactions.';

COMMENT ON COLUMN neurorx.gold.schedules_synced.dose_text IS
    'Free-text dose as confirmed by the patient or caregiver (e.g. "500 mg"). Display text only — never parsed for a numeric answer; this project does not compute or advise on dosage.';

-- ---------------------------------------------------------------------------
-- neurorx.gold.interaction_pairs
-- ---------------------------------------------------------------------------

COMMENT ON TABLE neurorx.gold.interaction_pairs IS
    'The deterministic, pre-verified table of known drug-drug interactions — the same table the chat agent tool check_interactions queries, and nothing else. One row per interacting PAIR of drugs (not per drug). A pair NOT appearing here means no interaction is known IN THIS REFERENCE DATA — that is not the same as confirmed safe, and any answer summarizing this table for a caregiver should say so rather than imply an absence proves safety.';

COMMENT ON COLUMN neurorx.gold.interaction_pairs.rxcui_a IS
    'One RxCUI in an interacting pair. The pair (rxcui_a, rxcui_b) is stored in a fixed lexicographic (string) order, not numeric order — a query checking whether a specific drug is involved in an interaction must check BOTH rxcui_a and rxcui_b, never assume a drug always lands in one column.';

COMMENT ON COLUMN neurorx.gold.interaction_pairs.rxcui_b IS
    'The other RxCUI in an interacting pair — see the comment on rxcui_a for why both columns must be checked.';

COMMENT ON COLUMN neurorx.gold.interaction_pairs.severity IS
    'One of major, moderate, minor, unknown. When summarizing interactions for a caregiver, major should always be surfaced first/most prominently — this is a safety-relevant ranking, not an arbitrary sort.';

COMMENT ON COLUMN neurorx.gold.interaction_pairs.sources IS
    'Which reference source(s) attest this interaction — an array because a pair independently confirmed by more than one source (e.g. both DDInter and an FDA label) is reported as both, not one arbitrarily picked. A longer array is stronger evidence, not noise.';

-- ---------------------------------------------------------------------------
-- neurorx.silver.drugs
-- ---------------------------------------------------------------------------

COMMENT ON TABLE neurorx.silver.drugs IS
    'One row per drug (by RxCUI, ingredient-level — e.g. "metformin", not a specific branded tablet). Join target for turning any rxcui in schedules_synced or interaction_pairs into a human-readable drug name.';

COMMENT ON COLUMN neurorx.silver.drugs.generic_name IS
    'The canonical, lowercase generic drug name — use this, not brand_names, as the default display name for a drug in any answer, matching how the chat agent and dashboard already refer to drugs.';

-- =============================================================================
-- PART B — trusted-asset "Example SQL query" bodies. Paste each block's SQL into
-- Configure → Instructions → SQL Queries → new query, with the Title exactly as
-- given in the comment above each block (Genie matches incoming questions against
-- the title — see genie_setup.md §4 on why close phrasing matters for the trusted
-- path to fire). Every query defaults :patient_id to Margaret Demo's UUID so it
-- also runs standalone with no typed input for a live demo.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Trusted asset 1
-- Title: What was Margaret's adherence last month?
-- Parameter: :patient_id (String), default '12345678-1234-1234-1234-123456789012'
-- -----------------------------------------------------------------------------
SELECT
    ROUND(
        SUM(taken_doses) / NULLIF(SUM(planned_doses), 0) * 100,
        1
    ) AS adherence_pct_last_30_days,
    SUM(planned_doses) AS total_planned_doses,
    SUM(taken_doses)   AS total_taken_doses,
    SUM(skipped_doses) AS total_skipped_doses,
    SUM(missed_doses)  AS total_missed_doses
FROM neurorx.gold.adherence_facts
WHERE patient_id = :patient_id
  AND event_date >= current_date() - INTERVAL 30 DAYS
  AND event_date < current_date();

-- -----------------------------------------------------------------------------
-- Trusted asset 2
-- Title: Which drug does she miss most?
-- Parameter: :patient_id (String), default '12345678-1234-1234-1234-123456789012'
-- -----------------------------------------------------------------------------
SELECT
    drug_name,
    SUM(missed_doses) AS total_missed_doses,
    SUM(planned_doses) AS total_planned_doses,
    ROUND(SUM(taken_doses) / NULLIF(SUM(planned_doses), 0) * 100, 1) AS adherence_pct
FROM neurorx.gold.adherence_facts
WHERE patient_id = :patient_id
  AND event_date >= current_date() - INTERVAL 30 DAYS
  AND event_date < current_date()
GROUP BY drug_name
HAVING SUM(missed_doses) > 0
ORDER BY total_missed_doses DESC
LIMIT 1;

-- -----------------------------------------------------------------------------
-- Trusted asset 3
-- Title: What time of day does she miss doses?
-- Parameter: :patient_id (String), default '12345678-1234-1234-1234-123456789012'
-- -----------------------------------------------------------------------------
SELECT
    day_part,
    SUM(missed_doses) AS total_missed_doses,
    ROUND(SUM(taken_doses) / NULLIF(SUM(planned_doses), 0) * 100, 1) AS adherence_pct
FROM neurorx.gold.adherence_facts
WHERE patient_id = :patient_id
  AND event_date >= current_date() - INTERVAL 30 DAYS
  AND event_date < current_date()
GROUP BY day_part
HAVING SUM(missed_doses) > 0
ORDER BY total_missed_doses DESC;

-- -----------------------------------------------------------------------------
-- Trusted asset 4
-- Title: Any major interactions among her current drugs?
-- Parameter: :patient_id (String), default '12345678-1234-1234-1234-123456789012'
--
-- Self-cross-joins the patient's own active schedule against itself (p1.rxcui <
-- p2.rxcui avoids pairing a drug with itself and avoids counting each pair
-- twice), then matches against interaction_pairs checking BOTH orderings of
-- (rxcui_a, rxcui_b) since the canonical lexicographic order (DATA_CONTRACTS.md
-- §7.1) doesn't correspond to which of the patient's two drugs comes "first."
-- Mirrors agent/tools/check_interactions.sql's own pairing + ordering logic,
-- adapted to read the pair list from a schedule instead of a passed-in array.
-- -----------------------------------------------------------------------------
WITH active_drugs AS (
    SELECT DISTINCT rxcui
    FROM neurorx.gold.schedules_synced
    WHERE patient_id = :patient_id
      AND status = 'active'
),
pairs AS (
    SELECT
        LEAST(p1.rxcui, p2.rxcui)    AS rxcui_a,
        GREATEST(p1.rxcui, p2.rxcui) AS rxcui_b
    FROM active_drugs p1
    JOIN active_drugs p2 ON p1.rxcui < p2.rxcui
)
SELECT
    da.generic_name AS drug_a,
    db.generic_name AS drug_b,
    ip.severity,
    ip.description,
    ip.sources
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
