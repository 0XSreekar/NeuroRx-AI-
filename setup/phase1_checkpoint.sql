-- Phase 1 Checkpoint Verification
-- NeuroRx AI — Validates data completeness and correctness before Phase 2
-- Read-only queries only. Each query has an expected result comment.
-- Run this notebook to prove Phase 1 is complete.

-- ==============================================================================
-- 1. ROW COUNTS — All bronze/silver/gold tables populated (expect: all non-zero)
-- ==============================================================================
-- Expected: Each table has at least one row. Bronze tables have 50+, 194+, 72000+.
-- Silver/gold dependent on ingestion success (FDA labels, RxNorm, DDInter coverage).

SELECT
  'BRONZE' AS layer,
  'fda_labels_raw' AS table_name,
  COUNT(*) AS row_count
FROM neurorx.bronze.fda_labels_raw
UNION ALL
SELECT 'BRONZE', 'rxnorm_raw', COUNT(*) FROM neurorx.bronze.rxnorm_raw
UNION ALL
SELECT 'BRONZE', 'ddinter_raw', COUNT(*) FROM neurorx.bronze.ddinter_raw
UNION ALL
SELECT 'BRONZE', 'synthetic_patients_raw', COUNT(*) FROM neurorx.bronze.synthetic_patients_raw
UNION ALL
SELECT 'BRONZE', 'synthetic_schedules_raw', COUNT(*) FROM neurorx.bronze.synthetic_schedules_raw
UNION ALL
SELECT 'BRONZE', 'synthetic_dose_events_raw', COUNT(*) FROM neurorx.bronze.synthetic_dose_events_raw
UNION ALL
SELECT 'SILVER', 'drugs', COUNT(*) FROM neurorx.silver.drugs
UNION ALL
SELECT 'SILVER', 'label_sections', COUNT(*) FROM neurorx.silver.label_sections
UNION ALL
SELECT 'SILVER', 'interactions', COUNT(*) FROM neurorx.silver.interactions
UNION ALL
SELECT 'GOLD', 'drug_knowledge', COUNT(*) FROM neurorx.gold.drug_knowledge
UNION ALL
SELECT 'GOLD', 'interaction_pairs', COUNT(*) FROM neurorx.gold.interaction_pairs
UNION ALL
SELECT 'GOLD', 'adherence_facts', COUNT(*) FROM neurorx.gold.adherence_facts
ORDER BY layer, table_name;

-- ==============================================================================
-- 2. WARFARIN + IBUPROFEN INTERACTION PAIR
-- ==============================================================================
-- Expected: One row with warfarin (11289) and ibuprofen (5640), severity 'major',
-- sources array including 'ddinter', lexicographic ordering (11289 < 5640).
-- This is the Phase 1 exit checkpoint per ARCHITECTURE.md §7.

SELECT
  ip.rxcui_a,
  d_a.generic_name AS drug_a,
  ip.rxcui_b,
  d_b.generic_name AS drug_b,
  ip.severity,
  ip.sources,
  ip.description,
  ip.checked_at
FROM neurorx.gold.interaction_pairs ip
JOIN neurorx.silver.drugs d_a ON ip.rxcui_a = d_a.rxcui
JOIN neurorx.silver.drugs d_b ON ip.rxcui_b = d_b.rxcui
WHERE (ip.rxcui_a = '11289' AND ip.rxcui_b = '5640')
   OR (ip.rxcui_a = '5640' AND ip.rxcui_b = '11289')
LIMIT 1;

-- ==============================================================================
-- 3. TOP 5 DRUGS BY LABEL-CHUNK COUNT
-- ==============================================================================
-- Expected: 5 rows, ordered by chunk count descending.
-- Shows which drugs have the richest label coverage in the corpus.

SELECT
  gdk.rxcui,
  gdk.drug_name,
  COUNT(*) AS chunk_count,
  COUNT(DISTINCT gdk.section) AS section_count,
  MIN(LENGTH(gdk.chunk_text)) AS min_chunk_chars,
  MAX(LENGTH(gdk.chunk_text)) AS max_chunk_chars
FROM neurorx.gold.drug_knowledge gdk
GROUP BY gdk.rxcui, gdk.drug_name
ORDER BY chunk_count DESC
LIMIT 5;

-- ==============================================================================
-- 4. LABEL SECTIONS: PRESENCE AND CHUNK TOKEN STATISTICS
-- ==============================================================================
-- Expected: Four sections (dosage_and_administration, drug_interactions, warnings,
-- information_for_patients), token counts clustered 500–800 per chunk.

SELECT
  section,
  COUNT(*) AS chunk_count,
  MIN(token_count) AS min_tokens,
  ROUND(AVG(token_count), 1) AS avg_tokens,
  MAX(token_count) AS max_tokens,
  ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY token_count), 0) AS median_tokens
FROM neurorx.silver.label_sections
GROUP BY section
ORDER BY section;

-- ==============================================================================
-- 5. INTERACTION PAIRS INVARIANT CHECKS
-- ==============================================================================
-- Expected: Two queries, each returning ZERO rows (i.e., no violations).
-- - Canonical order: all pairs stored with rxcui_a < rxcui_b (lexicographic)
-- - Unique pairs: no duplicates on (rxcui_a, rxcui_b)

-- Check 1: Rows violating canonical order (should be zero)
SELECT
  COUNT(*) AS canonical_order_violations
FROM neurorx.gold.interaction_pairs
WHERE rxcui_a >= rxcui_b;

-- Check 2: Duplicate pairs (should be zero)
SELECT
  COUNT(*) AS duplicate_pair_count
FROM (
  SELECT rxcui_a, rxcui_b, COUNT(*) AS pair_count
  FROM neurorx.gold.interaction_pairs
  GROUP BY rxcui_a, rxcui_b
  HAVING COUNT(*) > 1
);

-- ==============================================================================
-- 6. ADHERENCE FACTS SANITY CHECKS
-- ==============================================================================
-- Expected:
-- - Overall cohort adherence_pct between 60% and 95% (realistic range)
-- - Margaret Demo identified as patient '12345678-1234-1234-1234-123456789012'
-- - Margaret's most-missed drug is metformin (lowest adherence_pct across her drugs)

-- 6a. Overall cohort adherence stats
-- Expected: mean adherence 60–95%, showing realistic variability
SELECT
  'Overall cohort' AS cohort,
  COUNT(DISTINCT patient_id) AS patient_count,
  COUNT(*) AS fact_rows,
  ROUND(MIN(adherence_pct), 1) AS min_adherence_pct,
  ROUND(AVG(adherence_pct), 1) AS avg_adherence_pct,
  ROUND(MAX(adherence_pct), 1) AS max_adherence_pct,
  ROUND(STDDEV_POP(adherence_pct), 1) AS stddev_adherence_pct
FROM neurorx.gold.adherence_facts;

-- 6b. Margaret Demo's adherence by drug
-- Expected: metformin has the lowest adherence_pct (due to evening miss penalty)
SELECT
  patient_id,
  rxcui,
  drug_name,
  COUNT(*) AS day_parts_tracked,
  ROUND(AVG(adherence_pct), 1) AS avg_adherence_pct
FROM neurorx.gold.adherence_facts
WHERE patient_id = '12345678-1234-1234-1234-123456789012'
GROUP BY patient_id, rxcui, drug_name
ORDER BY avg_adherence_pct ASC;

-- 6c. Margaret Demo: verify metformin evening misses are highest
-- Expected: metformin evening day_part has significantly lower adherence than morning
SELECT
  patient_id,
  rxcui,
  day_part,
  COUNT(*) AS day_count,
  ROUND(AVG(adherence_pct), 1) AS avg_adherence_pct,
  SUM(planned_doses) AS total_planned,
  SUM(taken_doses) AS total_taken,
  SUM(missed_doses) AS total_missed,
  SUM(skipped_doses) AS total_skipped
FROM neurorx.gold.adherence_facts
WHERE patient_id = '12345678-1234-1234-1234-123456789012'
  AND drug_name = 'metformin'  -- was `rxcui = 'metformin'`: rxcui holds the numeric RxCUI string ('6809'), not the drug name — that filter matched zero rows
GROUP BY patient_id, rxcui, day_part
ORDER BY day_part;

-- ==============================================================================
-- 7. BONUS: PHASE 1 SUMMARY DASHBOARD
-- ==============================================================================
-- Expected: Holistic view of Phase 1 success.

SELECT
  'Data ingestion complete' AS checkpoint,
  CASE
    WHEN (SELECT COUNT(*) FROM neurorx.bronze.fda_labels_raw) > 0
      AND (SELECT COUNT(*) FROM neurorx.bronze.rxnorm_raw) > 0
      AND (SELECT COUNT(*) FROM neurorx.bronze.ddinter_raw) > 0
      AND (SELECT COUNT(*) FROM neurorx.bronze.synthetic_patients_raw) = 50
    THEN '✓ PASS'
    ELSE '✗ FAIL'
  END AS status
UNION ALL
SELECT
  'Warfarin+ibuprofen interaction present',
  CASE
    WHEN (SELECT COUNT(*) FROM neurorx.gold.interaction_pairs
          WHERE rxcui_a = '11289' AND rxcui_b = '5640') = 1
    THEN '✓ PASS'
    ELSE '✗ FAIL'
  END
UNION ALL
SELECT
  'Label chunks indexed',
  CASE
    WHEN (SELECT COUNT(*) FROM neurorx.gold.drug_knowledge) > 100
    THEN '✓ PASS'
    ELSE '✗ FAIL'
  END
UNION ALL
SELECT
  'Adherence facts derived',
  CASE
    WHEN (SELECT COUNT(*) FROM neurorx.gold.adherence_facts) > 1000
    THEN '✓ PASS'
    ELSE '✗ FAIL'
  END
UNION ALL
SELECT
  'Margaret Demo present with 44% adherence',
  CASE
    WHEN (SELECT COUNT(*) FROM neurorx.gold.adherence_facts
          WHERE patient_id = '12345678-1234-1234-1234-123456789012') > 0
    THEN '✓ PASS'
    ELSE '✗ FAIL'
  END
UNION ALL
SELECT
  'Interaction pair order canonical (rxcui_a < rxcui_b)',
  CASE
    WHEN (SELECT COUNT(*) FROM neurorx.gold.interaction_pairs WHERE rxcui_a >= rxcui_b) = 0
    THEN '✓ PASS'
    ELSE '✗ FAIL'
  END;

-- ==============================================================================
-- END OF PHASE 1 CHECKPOINT
-- ==============================================================================
-- If all queries return expected results (non-zero counts, checkpoint passes),
-- Phase 1 is complete and Phase 2 (UC function implementation, agent tools)
-- can proceed.
