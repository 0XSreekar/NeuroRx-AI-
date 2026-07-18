# NeuroRx AI — Genie setup runbook (Task 5.1)

> ⚠️ **Terminology has changed since `ARCHITECTURE.md`/`CLAUDE.md` were written, verified
> live this session (search result dated 2026-07-14 — five days before this session).**
> "Genie Space" is now **"Genie Agent"** in the current Databricks UI and docs. Confirmed
> against `docs.databricks.com/aws/en/genie-agents/set-up`: *"Genie Agents were formerly
> known as Genie Spaces."* This file uses **Genie Agent** as the primary term (matching the
> UI you'll actually see) and keeps "Genie space" parenthetically since that's what the
> task brief, `ARCHITECTURE.md` §8, and Task 3.6's `GENIE_EMBED_URL` code comment call it —
> same object, renamed underneath the project mid-flight. This is exactly the kind of
> Beta-feature drift `CLAUDE.md` §6 warns about ("this feature is new and changes; do not
> write from memory") — re-verify before demo day if more time has passed.

> ⚠️ **Also renamed/refined since the task brief was written:** the brief calls the
> deterministic-answer mechanism "Genie's certified-answer mechanism." The current, real
> term (verified against `docs.databricks.com/genie/trusted-assets`) is **"trusted
> assets."** Two kinds exist — **example SQL queries** (parameterized, what this file
> uses) and **SQL functions** (Unity Catalog UDFs, for logic too complex for a static
> query — not needed here, see §4 below for why). "Certified answer" isn't current
> Databricks terminology; corrected throughout this file, not silently used as written.

> ⚠️ **Standing project blocker, unchanged from every other Genie-adjacent task:**
> `ARCHITECTURE.md`'s own §8 cut list puts Genie first in line to be cut under time
> pressure, and `CLAUDE.md`'s Task 3.6 note already flags that no Genie Agent exists yet
> and none of this has been created against a live workspace. Everything below is a
> **runbook to execute**, not evidence that it has been. The SQL in `genie_assets.sql`
> has been checked for syntax and logic consistency against this project's own frozen
> contracts (`DATA_CONTRACTS.md`) and the already-shipped `check_interactions.sql` /
> `get_adherence_stats.sql`, but **not run against a live Unity Catalog** — no workspace
> is reachable from this environment, the same standing limitation as every other
> `.sql` file in this project.

---

## 1. Prerequisites

- All four source tables/views exist and are populated: `neurorx.gold.adherence_facts`,
  `neurorx.gold.schedules_synced`, `neurorx.gold.interaction_pairs`, `neurorx.silver.drugs`.
  **`schedules_synced` is Task 3.2's actual materialized-view name** (`lakebase/sync.sql`),
  not the bare `gold.schedules` `DATA_CONTRACTS.md` §9's sync map still shows — that
  mismatch is Task 3.2's own already-flagged, still-open discrepancy; this task follows
  the real, built name.
- Run `genie_assets.sql`'s `COMMENT ON` statements against Unity Catalog **before**
  creating the Genie Agent — confirmed live this session: creating an Agent triggers
  "Genie Code," which auto-reads existing table/column descriptions to suggest context.
  Comments landing after Agent creation just means re-running Genie Code's suggestion
  step, not a hard requirement, but doing it first avoids a wasted pass.
- A pro or serverless SQL warehouse with at least `CAN USE` granted to whoever will query
  the Agent (confirmed current requirement). This project has exactly one pre-created
  serverless warehouse (Free Edition, `CLAUDE.md` §4) — use that; there's no other choice.

## 2. Create the Genie Agent

Confirmed against the current UI flow (`docs.databricks.com/aws/en/genie-agents/set-up`,
fetched live this session):

1. Sidebar → **Genie Agents**.
2. Click **New** (upper-right).
3. **Choose the data sources** — select these four, all in `neurorx`:
   - `gold.adherence_facts`
   - `gold.schedules_synced`
   - `gold.interaction_pairs`
   - `silver.drugs`

   (Current limit: up to 30 tables/views per Agent — four is nowhere near it.)
4. Click **Create**.
5. Genie Code runs automatically and proposes table/column descriptions and example
   queries by reading the data. **Review its suggestions before accepting anything** —
   it has no way to know `checked_at` is a pipeline-refresh timestamp and not a
   per-patient check log ([`DATA_CONTRACTS.md`](../DATA_CONTRACTS.md) F14), or that
   `sources` can legitimately be a two-element array. The `COMMENT ON` statements in
   `genie_assets.sql` exist precisely so Genie Code has the right answer already and
   proposes less that needs correcting.

## 3. Configure the Agent

All under **Configure**, confirmed against the current UI (same source):

- **Configure → Settings**
  - **Title**: `NeuroRx Caregiver Analytics`
  - **Default warehouse**: the project's one Free Edition serverless warehouse
  - **Description** (Markdown supported):

    > Caregiver-facing analytics over NeuroRx AI's medication adherence and drug
    > interaction data. Ask about adherence trends, which medications get missed most,
    > when doses are typically missed, and whether a patient's current medications have
    > any known interactions. All answers are grounded in the same deterministic tables
    > the chat agent uses — `gold.adherence_facts` for adherence, `gold.interaction_pairs`
    > for interactions — never inferred. 100% synthetic data; no real patients.

  - **Common questions** (optional examples shown on the landing page — this is the
    current UI's actual name for what the task brief calls "sample questions"; add the
    four from §4 below, worded exactly as their trusted-asset titles).

- **Configure → Data**: confirm the four tables from step 2 are listed; `Add`/trash-can
  icon to adjust if Genie Code pulled in something extra (e.g. a table it auto-detected
  via a foreign key it inferred incorrectly — `schedules_synced` has no declared FK to
  `interaction_pairs`, so this shouldn't happen, but check).

## 4. Trusted assets — the four sample questions

Confirmed mechanism (`docs.databricks.com/genie/trusted-assets` +
`docs.databricks.com/aws/en/genie/tune-quality`, fetched live this session):

- **Configure → Instructions → SQL Queries tab** → add each query.
- Each has a **Title** ("use the most typical phrasing of the user's question" — this is
  what Genie matches incoming questions against) and a **SQL query editor**.
- Parameters use **`:parameter_name`** syntax directly in the query text; the UI's gear
  icon lets you set each parameter's type (String / Date / Date and Time / Decimal /
  Integer) and a comment.
- **"When the exact text of a parameterized query is used to generate a response, Genie
  provides a verified answer."** This is a semantic-match decision Genie itself makes —
  there's no guarantee a rephrased version of a question still triggers the trusted path
  instead of Genie generating fresh (unverified) SQL. **Practical consequence for demo
  day: ask the four questions close to their titles below, not paraphrased**, or the
  demo risks falling back to freeform generation, which this task exists to avoid.
- SQL-function trusted assets (Unity Catalog UDFs, `RETURNS TABLE`) were considered and
  **not used** for any of the four — every one of them is expressible as a single
  parameterized query with no cross-table procedural logic, so a UDF would be
  unneeded complexity for what these four checks need (this project's own standing
  rule: no abstraction beyond what the task requires).

The four queries below are also written out in full in `genie_assets.sql` — paste each
`SQL query editor` block verbatim; nothing here needs adaptation.

### 4.1 "What was Margaret's adherence last month?"

- **Parameter**: `:patient_id` (String) — defaults to Margaret Demo's UUID
  (`12345678-1234-1234-1234-123456789012`) so the trusted asset works standalone in a
  demo with no typed input, but a caregiver could re-run it for a different patient.
- Aggregates `gold.adherence_facts` over the trailing 30 days, `SUM(taken_doses) /
  NULLIF(SUM(planned_doses), 0) * 100` — the exact same formula
  [`DATA_CONTRACTS.md`](../DATA_CONTRACTS.md) §5.3 defines for the column itself, just
  summed across drugs/day-parts rather than read per-row, so this can never silently
  disagree with what `adherence_pct` already means.

### 4.2 "Which drug does she miss most?"

- Same `:patient_id` parameter and default.
- Groups by `drug_name`, ranks by `missed_doses` descending — deliberately **not**
  `100 - adherence_pct` ascending: a low-volume once-daily drug with one missed dose out
  of two prescribed would rank as "worst" by percentage while mattering far less than a
  twice-daily drug missed 15 times a month. Counting raw misses is what "which drug does
  she miss most" actually asks.

### 4.3 "What time of day does she miss doses?"

- Same `:patient_id` parameter and default.
- Groups by `day_part`, sums `missed_doses` — the four `day_part` values are exactly
  [`DATA_CONTRACTS.md`](../DATA_CONTRACTS.md) §1's frozen boundaries (morning 05:00–11:59,
  afternoon 12:00–16:59, evening 17:00–20:59, night 21:00–04:59), already baked into the
  gold table by the Lakeflow pipeline — this query does no time-bucketing of its own, so
  it can't drift from the boundary the dashboard and the `get_adherence_stats` tool also
  use.

### 4.4 "Any major interactions among her current drugs?"

- Same `:patient_id` parameter and default.
- Joins `schedules_synced` (filtered `status = 'active'`) against `interaction_pairs` on
  **both** `rxcui_a` and `rxcui_b` — a drug pair can appear with either of the patient's
  active RxCUIs on either side of the canonical (lexicographically ordered) pair, so a
  single-sided join would silently miss half of them. This mirrors
  `agent/tools/check_interactions.sql`'s own `posexplode`-based self-cross-join
  structure, adapted to read from a schedule instead of an arbitrary passed-in list.
  Orders `major` first, same precedence `check_interactions.sql` already established.

## 5. ⚠️ The demo dependency — verify before demo day, not assumed

**Question 4.2's expected answer is "metformin, evenings."** This is the single most
load-bearing number in the whole Genie demo — get it wrong live and the caregiver
persona's centerpiece breaks in front of judges.

**What's actually verified, and what isn't:**

- ✅ **The synthetic-cohort generator's own design targets this exactly.**
  `data/ingestion/04_synthetic_cohort.py`'s own comment: *"Margaret Demo misses metformin
  evening doses ~75% (key demo story)"* — deliberate, not incidental.
- ✅ **The exact SQL logic that would compute this was independently verified against a
  DuckDB fixture reproducing Margaret's cohort** (Task 2.4, `get_adherence_stats.sql`):
  `most_missed_drug = metformin`, `most_missed_daypart = evening`, confirmed via twelve
  passing checks including a cross-check against an independent Python calculation.
- ❌ **Neither of the above is the same as querying live `gold.adherence_facts`.** Per
  `CLAUDE.md`'s standing warning (repeated across nearly every task in this project):
  Task 1.4's generator has **zero Spark/Delta writes** — nothing from it has ever reached
  `neurorx.bronze.synthetic_*`, let alone flowed through the Lakeflow pipeline into
  `gold.adherence_facts`. **This table does not exist with real rows yet.** The DuckDB
  fixture proves the *arithmetic* is right; it says nothing about whether the *real*
  pipeline, run end-to-end, produces the same answer — a bug anywhere upstream (a wrong
  join, a dropped row, a mis-bucketed day-part) could still change question 4.2's answer
  without anyone having run the query that would catch it.

**Before demo day, in this exact order:**

1. Fix Task 1.4 (three known bugs — CLAUDE.md's own standing warning: zero Delta writes,
   75% non-curated drug list, every non-demo patient surnamed "Smith").
2. Run the ingests, the Lakeflow pipeline, Lakebase load (`lakebase/07_load_cohort.py`,
   Task 3.8 — already verified end-to-end against local Postgres), and the Lakebase→Delta
   sync (Task 3.2).
3. Run `setup/phase1_checkpoint.sql`'s Query 6b/6c (Margaret's adherence by drug and by
   day-part) directly — not the Genie Agent yet.
4. **Only after that query independently returns metformin/evening**, ask question 4.2
   through the actual Genie Agent and confirm the trusted asset fires (not a freeform
   fallback — check the response is marked "verified") and returns the same answer.

Skipping straight to step 4 risks discovering a real pipeline bug live, in front of
judges, on the one question built specifically to showcase the caregiver persona.
