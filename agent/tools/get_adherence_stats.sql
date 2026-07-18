-- Databricks notebook source
-- MAGIC %md
-- MAGIC # `neurorx.app.get_adherence_stats` — deterministic adherence facts
-- MAGIC
-- MAGIC ============================================================
-- MAGIC DESIGN RULE (per ARCHITECTURE.md §5 and CLAUDE.md §1):
-- MAGIC
-- MAGIC   The numbers are computed here, in SQL. The LLM relays them.
-- MAGIC   It never re-derives, re-estimates, or rounds them into a
-- MAGIC   different number.
-- MAGIC ============================================================
-- MAGIC
-- MAGIC Pure SQL over `neurorx.gold.adherence_facts` — no `ai_query`, no model call,
-- MAGIC no LLM involvement of any kind, enforced by construction (there is nothing in
-- MAGIC this file capable of calling a model). Same shape as
-- MAGIC `check_interactions.sql`: the agent's only job once this returns is turning
-- MAGIC rows that already exist into plain language.
-- MAGIC
-- MAGIC ## ⚠️ The parameter-vs-column shadowing trap (read before editing)
-- MAGIC
-- MAGIC The parameter `patient_id` has the same name as
-- MAGIC `gold.adherence_facts.patient_id`. Databricks name resolution puts **columns
-- MAGIC ahead of routine parameters** — confirmed against the current `Name
-- MAGIC resolution` reference, which states verbatim *"A column takes precedence over
-- MAGIC a parameter"* and gives this example:
-- MAGIC
-- MAGIC ```sql
-- MAGIC CREATE OR REPLACE TEMPORARY FUNCTION func(a INT) RETURNS INT
-- MAGIC   RETURN (SELECT a FROM VALUES(1) AS T(a) WHERE t.a = func.a);
-- MAGIC ```
-- MAGIC
-- MAGIC So the natural-looking filter
-- MAGIC
-- MAGIC ```sql
-- MAGIC WHERE patient_id = patient_id          -- ☠️ DO NOT
-- MAGIC ```
-- MAGIC
-- MAGIC resolves **both sides to the column**. It is not a filter at all: it is
-- MAGIC `col = col`, true for every row, and the function would silently return
-- MAGIC aggregates over the **entire cohort** to whoever asked — every patient
-- MAGIC getting back a blend of all 50 patients, with no error and a
-- MAGIC plausible-looking number. Every filter below therefore qualifies the
-- MAGIC parameter with the function name (`get_adherence_stats.patient_id`), which
-- MAGIC is the documented way to force parameter resolution. **Do not "simplify"
-- MAGIC these qualifiers away** — the unqualified form is not a style variant, it is
-- MAGIC a data-leak defect that no test short of a two-patient fixture would catch.
-- MAGIC
-- MAGIC ## Source table: `gold.adherence_facts`, per F9
-- MAGIC
-- MAGIC `ARCHITECTURE.md` §2 and plan §5 both describe this tool as *"SQL over
-- MAGIC `dose_events`"*, but `dose_events` exists in two places (live Lakebase and
-- MAGIC the `gold` mirror). `DATA_CONTRACTS.md` F9 flags that ambiguity and
-- MAGIC recommends: aggregate windows read **`gold.adherence_facts`**, while the
-- MAGIC Today view reads Lakebase directly for the live checklist. This task states
-- MAGIC `gold.adherence_facts` outright, matching the F9 recommendation, so that is
-- MAGIC what is used here — recording it because F9 is still formally unsigned-off
-- MAGIC (CLAUDE.md §5) and this file is now a de-facto vote for its recommendation.
-- MAGIC The consequence to keep in mind for the demo: a dose marked taken seconds
-- MAGIC ago is **not** reflected here until the Lakebase→Delta sync plus the
-- MAGIC Lakeflow derivation have run. That is the accepted trade in F9, not a bug.
-- MAGIC
-- MAGIC ## Definitions chosen where the task left them open
-- MAGIC
-- MAGIC 1. **The window is `window_days` complete days ending yesterday** —
-- MAGIC    `[current_date() - window_days, current_date() - 1]` inclusive. Today is
-- MAGIC    excluded on purpose: today is still in progress, so its unactioned doses
-- MAGIC    are `planned`, not `missed`, and counting them would drag every number
-- MAGIC    down as a pure artifact of the hour the question was asked. This also
-- MAGIC    makes the window agree with the streak definition the task gave
-- MAGIC    ("ending yesterday") rather than fighting it.
-- MAGIC 2. **`overall_adherence_pct` is dose-weighted, not an average of averages** —
-- MAGIC    `sum(taken) / sum(planned)`, not `avg(adherence_pct)`. These differ:
-- MAGIC    `avg(adherence_pct)` weights a day-part with one planned dose the same as
-- MAGIC    one with three. Dose-weighted is the honest reading of "what fraction of
-- MAGIC    prescribed doses did the patient actually take" and matches the
-- MAGIC    `adherence_pct` formula frozen in DATA_CONTRACTS.md §5.3.
-- MAGIC 3. **Skips count against adherence** — inherited, not decided here.
-- MAGIC    `adherence_facts.adherence_pct` is frozen as `taken / planned` (F4), so
-- MAGIC    `skipped_doses` depresses adherence exactly as `missed_doses` does. But
-- MAGIC    `most_missed_drug` and `most_missed_daypart` rank on `missed_doses`
-- MAGIC    **alone**, per the task wording — a deliberate skip is not a miss. F4 is
-- MAGIC    still unsigned-off; if it flips to "skipped is excluded", the pipeline
-- MAGIC    formula changes and metric 1 here follows it automatically, while
-- MAGIC    metrics 4 and 5 are unaffected.
-- MAGIC 4. **Per-drug rows group by `drug_name`, not `rxcui`** — the output contract
-- MAGIC    the task specifies has a `drug_name` column and no `rxcui` column.
-- MAGIC    Grouping by `rxcui` could emit two rows both labelled `metformin`
-- MAGIC    (F11: name→RxCUI is not 1:1 — `metformin` really does resolve to two
-- MAGIC    RxCUIs), which reads as a duplicate and is unusable by the agent.
-- MAGIC    Grouping by name gives one row per drug as the patient understands it.
-- MAGIC 5. **Ties broken by `drug_name` / `day_part` ascending** — without an
-- MAGIC    explicit tiebreak, `ORDER BY missed DESC LIMIT 1` is non-deterministic on
-- MAGIC    a tie, so the same question could get two different answers. "Deterministic
-- MAGIC    SQL" has to mean deterministic on ties too.
-- MAGIC 6. **`most_missed_*` rows are omitted entirely when nothing was missed**
-- MAGIC    rather than emitted with `value_num = 0`. A `most_missed_drug` row saying
-- MAGIC    `metformin, 0` invites the agent to say "you miss metformin most" to a
-- MAGIC    patient with a perfect record. Absence is documented in the COMMENT as
-- MAGIC    meaning nothing was missed.
-- MAGIC
-- MAGIC ## Streak semantics and their limits
-- MAGIC
-- MAGIC `current_streak_days` counts back from yesterday over days where every
-- MAGIC planned dose was taken, stopping at the most recent day that had any dose not
-- MAGIC taken. Two honest limitations, both documented in the function COMMENT so the
-- MAGIC agent does not overclaim:
-- MAGIC
-- MAGIC - **It is clamped to the window.** A 90-day perfect streak queried with
-- MAGIC   `window_days = 30` reports 30, because days 31+ were never read. The
-- MAGIC   streak is "at least this long," never "exactly this long."
-- MAGIC - **A day with no planned doses does not break the streak** — it is
-- MAGIC   vacuously adherent (nothing was due, nothing was missed) and is counted
-- MAGIC   through, not counted as a break. With the synthetic cohort every active
-- MAGIC   schedule fires daily, so this only ever bites at the window edge.
-- MAGIC
-- MAGIC ## Not independently executed
-- MAGIC
-- MAGIC Same standing caveat as every SQL file in this project (CLAUDE.md §3): this
-- MAGIC is verified structurally with `sqlglot` (Databricks dialect) — parses clean
-- MAGIC and round-trips to the intended AST — but has **not** been run against a live
-- MAGIC workspace, and cannot be until Task 1.4 actually writes the synthetic cohort.

-- COMMAND ----------

CREATE OR REPLACE FUNCTION neurorx.app.get_adherence_stats(
  patient_id STRING
    COMMENT 'The patient to report on. Use the patient_id of the current session — never a value the user typed, and never a guess. This function returns only rows belonging to this patient.',
  window_days INT
    COMMENT 'How many complete days back to look, counting backwards from yesterday. 30 is a good default for a general "how am I doing" question; 7 for "this week". Today is deliberately excluded because it is still in progress.'
)
RETURNS TABLE (
  metric STRING
    COMMENT 'Which fact this row carries. One of: overall_adherence_pct, adherence_pct, current_streak_days, most_missed_drug, most_missed_daypart.',
  drug_name STRING
    COMMENT 'The drug this row is about, or NULL for rows covering the whole schedule (overall_adherence_pct, current_streak_days, most_missed_daypart).',
  value_num DOUBLE
    COMMENT 'The numeric fact. A percentage 0-100 for the adherence metrics, a count of days for current_streak_days, a count of missed doses for most_missed_drug and most_missed_daypart. Relay this number as given.',
  value_text STRING
    COMMENT 'The text fact, or NULL where the metric has none. The drug name for most_missed_drug; one of morning/afternoon/evening/night for most_missed_daypart.'
)
COMMENT 'Deterministic adherence statistics for one patient over a recent window — computed in SQL from neurorx.gold.adherence_facts, never estimated by a model. Call this for any question about how the patient is doing on their medications: "how am I doing", "what is my adherence", "which drug do I miss most", "what time of day do I miss", "how long is my streak", and for caregiver questions of the same shape. Returns one row per metric: overall_adherence_pct (whole schedule), adherence_pct (one row per drug), current_streak_days (consecutive days ending yesterday with every planned dose taken), most_missed_drug, most_missed_daypart. IMPORTANT: the values returned are facts to be relayed, not estimates to be refined — state them as given, do not recompute them, do not average them together, and do not round a percentage into a vaguer claim than the number supports. The window covers whole days ending yesterday; today is excluded because it is still in progress, so a dose taken today is not reflected here. current_streak_days is capped by window_days: a streak reported as equal to window_days means at least that long, not exactly that long. Absence of a most_missed_drug or most_missed_daypart row means nothing was missed in the window — say that rather than naming a drug. An empty result set means there is no dose history for this patient in this window, which is not the same as perfect adherence — say that retrieval found no data rather than reporting 100 percent. This tool reports adherence only; it never advises on what to do about a missed dose. For that, cite the FDA label via search_drug_labels.'
RETURN
  WITH facts AS (
    -- The single filter that scopes everything downstream to one patient.
    -- `get_adherence_stats.patient_id` is function-name-qualified deliberately:
    -- unqualified, it would resolve to the column and match every patient.
    -- See the header note before touching this.
    SELECT
      af.drug_name,
      af.event_date,
      af.day_part,
      af.planned_doses,
      af.taken_doses,
      af.missed_doses
    FROM neurorx.gold.adherence_facts af
    WHERE af.patient_id = get_adherence_stats.patient_id
      AND af.event_date >= date_sub(current_date(), get_adherence_stats.window_days)
      AND af.event_date <= date_sub(current_date(), 1)
  ),

  -- One row per calendar day: was every dose due that day actually taken?
  daily AS (
    SELECT
      f.event_date,
      SUM(f.planned_doses) AS planned_doses,
      SUM(f.taken_doses)   AS taken_doses
    FROM facts f
    GROUP BY f.event_date
  ),

  -- The most recent day in the window on which any planned dose was not taken.
  -- This is what the streak counts up to. NULL means the whole window is clean.
  --
  -- The `FROM (SELECT 1 FROM daily LIMIT 1)` anchor is load-bearing, not noise:
  -- a bare `SELECT <expr>` with no FROM always yields exactly one row, so a
  -- patient with no dose history would emit `current_streak_days = 0` — which
  -- reads as "you broke your streak" rather than "there is no data", and would
  -- make the function COMMENT promise ("an empty result set means no history")
  -- a lie, since the result could never be empty. Anchoring to `daily` yields
  -- zero rows when there is no history and exactly one row otherwise.
  streak_calc AS (
    SELECT
      CASE
        -- Clean window: count from the earliest day actually covered, not from
        -- the window start, so a patient whose history begins 5 days ago gets 5
        -- rather than a fabricated 30.
        WHEN (SELECT MAX(d.event_date) FROM daily d WHERE d.taken_doses < d.planned_doses) IS NULL
          THEN datediff(
                 date_sub(current_date(), 1),
                 (SELECT MIN(d.event_date) FROM daily d)
               ) + 1
        -- Otherwise the streak is the gap between yesterday and the last bad day.
        -- If yesterday itself was bad this is 0, which is correct.
        ELSE datediff(
               date_sub(current_date(), 1),
               (SELECT MAX(d.event_date) FROM daily d WHERE d.taken_doses < d.planned_doses)
             )
      END AS streak_days
    FROM (SELECT 1 FROM daily LIMIT 1)
  ),

  -- Ranked separately rather than inline in the UNION so that ORDER BY / LIMIT
  -- binds to this subquery and not to the whole union.
  worst_drug AS (
    SELECT
      f.drug_name,
      SUM(f.missed_doses) AS missed_doses
    FROM facts f
    GROUP BY f.drug_name
    HAVING SUM(f.missed_doses) > 0
    ORDER BY SUM(f.missed_doses) DESC, f.drug_name ASC
    LIMIT 1
  ),

  worst_daypart AS (
    SELECT
      f.day_part,
      SUM(f.missed_doses) AS missed_doses
    FROM facts f
    GROUP BY f.day_part
    HAVING SUM(f.missed_doses) > 0
    ORDER BY SUM(f.missed_doses) DESC, f.day_part ASC
    LIMIT 1
  ),

  rows_out AS (
    -- 1. Overall adherence across the whole schedule. Dose-weighted.
    SELECT
      1                                                                    AS sort_key,
      'overall_adherence_pct'                                              AS metric,
      CAST(NULL AS STRING)                                                 AS drug_name,
      CAST(SUM(f.taken_doses) AS DOUBLE)
        / NULLIF(SUM(f.planned_doses), 0) * 100                            AS value_num,
      CAST(NULL AS STRING)                                                 AS value_text
    FROM facts f
    HAVING SUM(f.planned_doses) > 0

    UNION ALL

    -- 2. Adherence per drug, one row each.
    SELECT
      2                                                                    AS sort_key,
      'adherence_pct'                                                      AS metric,
      f.drug_name                                                          AS drug_name,
      CAST(SUM(f.taken_doses) AS DOUBLE)
        / NULLIF(SUM(f.planned_doses), 0) * 100                            AS value_num,
      CAST(NULL AS STRING)                                                 AS value_text
    FROM facts f
    GROUP BY f.drug_name
    HAVING SUM(f.planned_doses) > 0

    UNION ALL

    -- 3. Current streak of fully-adherent days ending yesterday.
    SELECT
      3                                                                    AS sort_key,
      'current_streak_days'                                                AS metric,
      CAST(NULL AS STRING)                                                 AS drug_name,
      CAST(s.streak_days AS DOUBLE)                                        AS value_num,
      CAST(NULL AS STRING)                                                 AS value_text
    FROM streak_calc s

    UNION ALL

    -- 4. The drug with the most missed doses in the window.
    SELECT
      4                                                                    AS sort_key,
      'most_missed_drug'                                                   AS metric,
      w.drug_name                                                          AS drug_name,
      CAST(w.missed_doses AS DOUBLE)                                       AS value_num,
      w.drug_name                                                          AS value_text
    FROM worst_drug w

    UNION ALL

    -- 5. The day-part with the most missed doses in the window.
    SELECT
      5                                                                    AS sort_key,
      'most_missed_daypart'                                                AS metric,
      CAST(NULL AS STRING)                                                 AS drug_name,
      CAST(p.missed_doses AS DOUBLE)                                       AS value_num,
      p.day_part                                                           AS value_text
    FROM worst_daypart p
  )

  SELECT
    r.metric,
    r.drug_name,
    r.value_num,
    r.value_text
  FROM rows_out r
  ORDER BY r.sort_key ASC, r.drug_name ASC NULLS FIRST;

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## Test cell
-- MAGIC
-- MAGIC Margaret Demo, `12345678-1234-1234-1234-123456789012` (DATA_CONTRACTS.md §3.4),
-- MAGIC window 30. Her cohort is generated with metformin dosed twice daily and a
-- MAGIC 75.6% miss rate specifically on the **evening** dose, against ~44% overall
-- MAGIC adherence — so the two assertions that matter are `most_missed_drug =
-- MAGIC metformin` (the task exit criterion) and, as a bonus that exercises the same
-- MAGIC story from the other axis, `most_missed_daypart = evening`.
-- MAGIC
-- MAGIC ⚠️ This **cannot pass yet**: `04_synthetic_cohort.py` (Task 1.4) currently
-- MAGIC writes nothing to `neurorx.bronze.*`, so `gold.adherence_facts` has no rows
-- MAGIC for Margaret and this returns an empty result set. That is a Task 1.4 defect,
-- MAGIC not a defect here — see CLAUDE.md §3.

-- COMMAND ----------

SELECT * FROM neurorx.app.get_adherence_stats('12345678-1234-1234-1234-123456789012', 30);

-- COMMAND ----------

-- MAGIC %python
-- MAGIC MARGARET = "12345678-1234-1234-1234-123456789012"
-- MAGIC
-- MAGIC rows = spark.sql(
-- MAGIC     f"SELECT * FROM neurorx.app.get_adherence_stats('{MARGARET}', 30)"
-- MAGIC ).collect()
-- MAGIC
-- MAGIC assert rows, (
-- MAGIC     "Empty result set — no adherence history for Margaret Demo in the last 30 days. "
-- MAGIC     "Expected: Task 1.4 (04_synthetic_cohort.py) has not been fixed to actually "
-- MAGIC     "write to neurorx.bronze.synthetic_* yet, so gold.adherence_facts is empty. "
-- MAGIC     "See CLAUDE.md §3."
-- MAGIC )
-- MAGIC
-- MAGIC by_metric = {}
-- MAGIC for r in rows:
-- MAGIC     by_metric.setdefault(r["metric"], []).append(r)
-- MAGIC
-- MAGIC # --- The task exit criterion ------------------------------------------------
-- MAGIC assert "most_missed_drug" in by_metric, (
-- MAGIC     f"No most_missed_drug row. Per the function contract that means zero missed "
-- MAGIC     f"doses in the window — impossible for Margaret, who is generated at ~44% "
-- MAGIC     f"adherence. Metrics present: {sorted(by_metric)}"
-- MAGIC )
-- MAGIC worst = by_metric["most_missed_drug"][0]
-- MAGIC assert worst["value_text"] == "metformin", (
-- MAGIC     f"Expected most_missed_drug = 'metformin', got {worst['value_text']!r} "
-- MAGIC     f"(missed {worst['value_num']}). Margaret is generated with a 75.6% miss "
-- MAGIC     f"rate on metformin evening doses (DATA_CONTRACTS.md §3.4) — if another "
-- MAGIC     f"drug outranks it, the cohort generator is not producing the demo story."
-- MAGIC )
-- MAGIC
-- MAGIC # --- The same story from the day-part axis ----------------------------------
-- MAGIC assert "most_missed_daypart" in by_metric, "No most_missed_daypart row."
-- MAGIC daypart = by_metric["most_missed_daypart"][0]
-- MAGIC assert daypart["value_text"] == "evening", (
-- MAGIC     f"Expected most_missed_daypart = 'evening', got {daypart['value_text']!r}. "
-- MAGIC     f"The evening miss penalty is the whole demo narrative."
-- MAGIC )
-- MAGIC
-- MAGIC # --- Shape checks -----------------------------------------------------------
-- MAGIC assert len(by_metric.get("overall_adherence_pct", [])) == 1, "Expected exactly one overall_adherence_pct row."
-- MAGIC assert len(by_metric.get("current_streak_days", [])) == 1, "Expected exactly one current_streak_days row."
-- MAGIC
-- MAGIC overall = by_metric["overall_adherence_pct"][0]["value_num"]
-- MAGIC assert 0 <= overall <= 100, f"overall_adherence_pct out of bounds: {overall}"
-- MAGIC
-- MAGIC streak = by_metric["current_streak_days"][0]["value_num"]
-- MAGIC assert 0 <= streak <= 30, f"current_streak_days must be within the 30-day window, got {streak}"
-- MAGIC
-- MAGIC per_drug = by_metric.get("adherence_pct", [])
-- MAGIC assert per_drug, "Expected one adherence_pct row per drug on the schedule."
-- MAGIC names = [r["drug_name"] for r in per_drug]
-- MAGIC assert len(names) == len(set(names)), f"Duplicate per-drug rows: {names}"
-- MAGIC # Margaret's fixed drugs, per DATA_CONTRACTS.md §3.4.
-- MAGIC assert "metformin" in names, f"metformin missing from per-drug rows: {names}"
-- MAGIC
-- MAGIC # metformin should be her worst drug by adherence too, not just by raw misses.
-- MAGIC met_pct = next(r["value_num"] for r in per_drug if r["drug_name"] == "metformin")
-- MAGIC assert met_pct == min(r["value_num"] for r in per_drug), (
-- MAGIC     f"metformin ({met_pct}%) is not the lowest-adherence drug: "
-- MAGIC     f"{ {r['drug_name']: r['value_num'] for r in per_drug} }"
-- MAGIC )
-- MAGIC
-- MAGIC print(f"PASSED: most_missed_drug = {worst['value_text']!r} ({worst['value_num']:.0f} missed doses)")
-- MAGIC print(f"        most_missed_daypart = {daypart['value_text']!r} ({daypart['value_num']:.0f} missed doses)")
-- MAGIC print(f"        overall_adherence_pct = {overall:.1f}%, current_streak_days = {streak:.0f}")
-- MAGIC for r in sorted(per_drug, key=lambda r: r["value_num"]):
-- MAGIC     print(f"        {r['drug_name']}: {r['value_num']:.1f}%")

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## Cohort-leak check — the parameter-shadowing regression test
-- MAGIC
-- MAGIC The header describes why `get_adherence_stats.patient_id` is qualified. This
-- MAGIC cell is what would actually catch a regression if someone unqualifies it: ask
-- MAGIC for Margaret, ask for a second patient, and confirm the two answers differ.
-- MAGIC With `WHERE patient_id = patient_id` the filter silently matches every row,
-- MAGIC so both calls return the **identical** whole-cohort aggregate — which looks
-- MAGIC completely plausible in isolation. A single-patient test cannot see this;
-- MAGIC only a two-patient comparison can.

-- COMMAND ----------

-- MAGIC %python
-- MAGIC MARGARET = "12345678-1234-1234-1234-123456789012"
-- MAGIC
-- MAGIC other = spark.sql(
-- MAGIC     f"SELECT patient_id FROM neurorx.gold.adherence_facts "
-- MAGIC     f"WHERE patient_id <> '{MARGARET}' LIMIT 1"
-- MAGIC ).collect()
-- MAGIC assert other, "Need a second patient in gold.adherence_facts to run the leak check."
-- MAGIC other_id = other[0]["patient_id"]
-- MAGIC
-- MAGIC def overall_for(pid):
-- MAGIC     rows = spark.sql(
-- MAGIC         f"SELECT value_num FROM neurorx.app.get_adherence_stats('{pid}', 30) "
-- MAGIC         f"WHERE metric = 'overall_adherence_pct'"
-- MAGIC     ).collect()
-- MAGIC     return rows[0]["value_num"] if rows else None
-- MAGIC
-- MAGIC margaret_pct = overall_for(MARGARET)
-- MAGIC other_pct = overall_for(other_id)
-- MAGIC
-- MAGIC assert margaret_pct != other_pct, (
-- MAGIC     f"LEAK: Margaret and patient {other_id} both report {margaret_pct}% overall "
-- MAGIC     f"adherence. The patient filter is matching every row — check that every "
-- MAGIC     f"reference to the patient_id parameter is qualified as "
-- MAGIC     f"`get_adherence_stats.patient_id`. An unqualified `patient_id` resolves "
-- MAGIC     f"to the COLUMN, making the predicate col = col. See the header note."
-- MAGIC )
-- MAGIC
-- MAGIC # Margaret is generated at ~44% adherence; the cohort draws from Beta(8,2),
-- MAGIC # i.e. ~80% mean. If the filter leaked, Margaret would read as cohort-average.
-- MAGIC print(f"PASSED: per-patient scoping holds — Margaret {margaret_pct:.1f}%, "
-- MAGIC       f"patient {other_id[:8]} {other_pct:.1f}%")
