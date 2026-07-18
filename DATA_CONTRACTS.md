# NeuroRx AI — Data contracts

**Single source of truth for every table schema.** Column names and types are frozen after Task 0.5; every later phase builds against exactly these. Changing a column here is a contract change — update this file first, then the code.

Derived from [`ARCHITECTURE.md`](ARCHITECTURE.md) (canonical) and `pharma-assist-build-plan.md` §3–§5 (source spec).

> **⚠️ Five decisions are OPEN and block Phase 1 writes.** See [§2 Flagged inconsistencies](#2-flagged-inconsistencies). Where the two source documents conflict, this file flags the conflict rather than silently picking a side. Schemas below are otherwise frozen; the open items affect *table placement and semantics*, not the column lists.

---

## 1. Conventions

### Type systems by layer

| Layer | Store | Type system |
|---|---|---|
| `bronze`, `silver`, `gold` | Delta / Unity Catalog | Spark SQL types (`STRING`, `TIMESTAMP`, `ARRAY<STRING>`, `VARIANT`) |
| Lakebase | Postgres (`neurorx-oltp`) | Postgres types (`UUID`, `TEXT`, `TIMESTAMPTZ`, `TIME[]`) |

These are different type systems. A Lakebase `TEXT` column arrives in Delta as `STRING` through sync; a Lakebase `TIMESTAMPTZ` arrives as `TIMESTAMP`. Do not use Postgres types in Lakeflow DDL or Spark types in Lakebase DDL.

### Audit columns — every bronze table

| Column | Type | Nullable | Description |
|---|---|---|---|
| `_ingested_at` | `TIMESTAMP` | No | Wall-clock time the row landed in bronze. Also serves as "pulled at" for API sources. |
| `_source_file` | `STRING` | No | Provenance of the row. See [F12](#f12) — semantics differ per source. |

### Enumerations

Enums are enforced as Lakeflow expectations (Delta) and `CHECK` constraints (Postgres). Values are lowercase snake_case, no exceptions.

| Enum | Values | Used by |
|---|---|---|
| `section` | `dosage_and_administration`, `drug_interactions`, `warnings`, `information_for_patients` | `silver.label_sections`, `gold.drug_knowledge` |
| `severity` | `major`, `moderate`, `minor`, `unknown` | `silver.interactions`, `gold.interaction_pairs` |
| `source` | `ddinter`, `fda_label` | `silver.interactions`, `gold.interaction_pairs` |
| `schedule_status` | `active`, `stopped` | Lakebase `schedules` |
| `dose_status` | `planned`, `taken`, `skipped`, `missed` | Lakebase `dose_events` |
| `day_part` | `morning`, `afternoon`, `evening`, `night` | `gold.adherence_facts` |

**`day_part` boundaries** (local patient time, required for deterministic bucketing — the plan does not define these, see [F13](#f13)):

| Value | Window |
|---|---|
| `morning` | 05:00:00 – 11:59:59 |
| `afternoon` | 12:00:00 – 16:59:59 |
| `evening` | 17:00:00 – 20:59:59 |
| `night` | 21:00:00 – 04:59:59 |

### Expectation notation

Lakeflow expectations are written in SQL constraint form:

```sql
CONSTRAINT <name> EXPECT (<expr>)                        -- track + warn, row retained
CONSTRAINT <name> EXPECT (<expr>) ON VIOLATION DROP ROW  -- quarantine the row
CONSTRAINT <name> EXPECT (<expr>) ON VIOLATION FAIL UPDATE -- halt the pipeline
```

Layer policy: **bronze warns, silver enforces.** Bronze must never drop a row — it is the audit record of what the source actually returned. `FAIL UPDATE` is reserved for violations that indicate a code defect rather than bad source data.

---

## 2. Flagged inconsistencies

Per the Task 0.5 instruction, these are surfaced rather than silently resolved. Each has a recommendation, but the decision is yours.

| ID | Severity | Issue | Blocks |
|---|---|---|---|
| [F1](#f1) | **Blocker** | RxCUI pair ordering is lexicographic, not numeric — silently inverts the warfarin+ibuprofen pair | Phase 1 |
| [F2](#f2) | **Blocker** | `guardrail_blocks` home contradicts the canonical architecture (Lakebase vs Delta) | Phase 4 |
| [F3](#f3) | **Blocker** | `adherence_facts` cannot be "the synced table" — its shape is an aggregate | Phase 1/3 |
| [F4](#f4) | **Blocker** | `skipped` dose status is unaccounted for in `adherence_facts` | Phase 1 |
| [F5](#f5) | **Blocker** | No `synthetic_schedules_raw`, but `dose_events.schedule_id` is a required FK | Phase 1 |
| [F6](#f6) | Important | `gold.interaction_pairs.source` cannot be single-valued after cross-source dedupe | Phase 1 |
| [F7](#f7) | Important | Gold holds 3 tables per `ARCHITECTURE.md`, but the sync map needs 3 more | Phase 1/3 |
| [F8](#f8) | Important | Synthetic cohort has two conflicting paths into the lakehouse | Phase 1/3 |
| [F9](#f9) | Important | `get_adherence_stats` target store is ambiguous (live Lakebase vs synced gold) | Phase 2 |
| [F10](#f10) | Important | `silver.drugs.set_id` is scalar, but RxCUI→SPL is 1:N | Phase 1 |
| [F11](#f11) | Important | Name→RxCUI is not 1:1 — verified: `metformin` returns two RxCUIs | Phase 1 |
| [F12](#f12) | Minor | `_source_file` is meaningless for API and generated sources | Phase 1 |
| [F13](#f13) | Minor | `day_part` boundaries undefined in the plan | Phase 1 |
| [F14](#f14) | Minor | `interaction_pairs.checked_at` semantics undefined | Phase 1 |
| [F15](#f15) | Minor | `fda_labels_raw.payload` type left as "STRING/VARIANT" | Phase 1 |

---

### F1 — RxCUI pair ordering is lexicographic, not numeric {#f1}

**Severity: Blocker.** This is a correctness defect in the deterministic safety core — the one component `ARCHITECTURE.md` §5 says must never be wrong.

The task specifies pairs stored with `rxcui_a < rxcui_b`, canonicalized via `LEAST`/`GREATEST`. But `rxcui` is a `STRING`, so `LEAST`/`GREATEST` compare **lexicographically**, not numerically. Verified against the live RxNav API:

| Drug | RxCUI |
|---|---|
| warfarin | `11289` |
| ibuprofen | `5640` |

- **Lexicographic** (what `LEAST` on `STRING` actually does): `'11289' < '5640'` because `'1' < '5'` → canonical pair is `('11289', '5640')`
- **Numeric** (what a reader will assume): `5640 < 11289` → canonical pair is `('5640', '11289')`

**These are opposite.** If the pipeline writes with one convention and `check_interactions` looks up with the other, the query returns zero rows and **the interaction is silently missed** — no error, just a false negative. The affected pair is warfarin + ibuprofen: the Phase 1 exit checkpoint in `ARCHITECTURE.md` §7, and a named true-positive in the §6 eval set. A silent false negative here is precisely the failure the whole architecture exists to prevent.

**Recommendation:** keep `rxcui` as `STRING` (RxCUIs are identifiers, never arithmetic operands) and commit to **lexicographic** ordering, applied through one shared expression that both the pipeline and the tool call. Either convention is correct; mixing them is not. Whichever you pick, it must be stated in the tool code as a comment, because "fix" this to numeric in one place only and the safety core breaks silently.

See [§7 Invariants](#7-invariants) for the exact expression.

### F2 — `guardrail_blocks` home contradicts the canonical architecture {#f2}

**Severity: Blocker.**

- Task 0.5 lists `guardrail_blocks` under **LAKEBASE (Postgres, OLTP)**.
- `ARCHITECTURE.md` §2 (canonical) places it in the **Agent layer** as `BL["Block log — Delta table"]`.
- `ARCHITECTURE.md` §5(e): *"Every block is logged to a **Delta table**."*
- Plan §5: *"Log every block to a **Delta table** — then show that table in the demo."*

Three of four sources say Delta; the task spec says Lakebase. The column list is identical either way, so the schema below is stable — only the home is contested.

Neither `ARCHITECTURE.md` nor the plan names a schema for the Delta version. If Delta, `neurorx.evals` is the better fit than `neurorx.app` (per `setup/01_uc_setup.sql`, `app` is documented as holding UC-function tools, not data).

**Recommendation:** Lakebase, synced to `neurorx.gold.guardrail_blocks`. Rationale: the guardrail runs in the request path, where a Postgres insert is cheaper than a Delta write, and the sync still yields a Delta table to show in the demo — satisfying the plan's intent without putting a Delta commit on the latency path. **This contradicts the canonical doc**, so if you accept it, `ARCHITECTURE.md` §2 and §5(e) must be amended to match. Do not leave the two files disagreeing.

### F3 — `adherence_facts` cannot be "the synced table" {#f3}

**Severity: Blocker.**

Plan §4 says gold holds `adherence_facts` *"(from Lakebase sync)"*, and `ARCHITECTURE.md` §2 draws `L -->|synced table| D`. But a synced table is a **mirror** of an OLTP table — same grain, same columns. The specified `adherence_facts` shape (`planned_doses`, `taken_doses`, `missed_doses`, `adherence_pct`, `day_part`) is an **aggregate** over `dose_events`. It cannot be both.

**Recommendation:** `dose_events` syncs to Delta as a mirror; `adherence_facts` is **derived** from that mirror by the Lakeflow pipeline. This makes the sync map in [§9](#9-lakebase--delta-sync-map) explicit and is the only reading consistent with the column list. Requires accepting [F7](#f7).

### F4 — `skipped` is unaccounted for in `adherence_facts` {#f4}

**Severity: Blocker.** This silently changes the headline adherence number.

`dose_events.status` has four values: `planned`, `taken`, `skipped`, `missed`. But `adherence_facts` has only `planned_doses`, `taken_doses`, `missed_doses`. There is no `skipped_doses`. So a dose the patient **deliberately skipped** has nowhere to go.

This matters because the two candidate readings produce different numbers on the demo dashboard:

- **Skipped rolls into missed** — `adherence_pct = taken / planned`. Punishes deliberate skips as non-adherence.
- **Skipped is excluded** — `adherence_pct = taken / (planned - skipped)`. Treats a skip as a legitimate non-event.

A clinician would distinguish these. The dashboard, Genie answers, and the `get_adherence_stats` tool all inherit whichever you choose.

**Recommendation:** add `skipped_doses INT NOT NULL` to `adherence_facts` and define `adherence_pct = taken_doses / NULLIF(planned_doses, 0)` — count skips against adherence (conservative, and the honest reading of "did the patient take the dose as scheduled"), but keep the column so the other metric stays computable without a backfill. The schema in [§5.3](#53-neurorxgoldadherence_facts) includes this column pending your decision.

### F5 — Missing `synthetic_schedules_raw` {#f5}

**Severity: Blocker.**

The task lists `neurorx.bronze.synthetic_patients_raw` and `neurorx.bronze.synthetic_dose_events_raw` — but not schedules. Yet:

- Lakebase `dose_events.schedule_id` is a **required FK** to `schedules`.
- The primary persona is *"managing 4+ chronic prescriptions"* — the demo cohort is meaningless without schedules.
- `dose_events.planned_ts` can only be generated *from* a schedule's `dose_times`.

Either a bronze table is missing, or synthetic schedules are generated directly into Lakebase and never land in bronze.

**Recommendation:** add `neurorx.bronze.synthetic_schedules_raw` for symmetry and auditability. Specified provisionally in [§3.6](#36-neurorxbronzesynthetic_schedules_raw).

### F6 — `gold.interaction_pairs.source` cannot be single-valued {#f6}

**Severity: Important.**

Plan §4 says interactions come from DDInter **plus** FDA-label parsing. The same `(rxcui_a, rxcui_b)` pair can therefore appear from both sources — with **different severities and different descriptions**. But `gold.interaction_pairs` has PK `(rxcui_a, rxcui_b)` and a single-valued `source` enum. After cross-source dedupe, a pair found in both sources cannot honestly report one `source`.

There is also no conflict-resolution rule. If DDInter says `major` and the label-derived row says `moderate`, which wins? The deterministic core must answer this the same way every time.

**Recommendation:**
- Silver PK is `(rxcui_a, rxcui_b, source)` — one row per source, no loss.
- Gold PK is `(rxcui_a, rxcui_b)` — deduped, with an explicit precedence rule: **`ddinter` wins over `fda_label`** (DDInter is severity-ranked by construction; label-parsed severity is `ai_query` inference and is the softer signal). On ties, take the higher severity (`major` > `moderate` > `minor` > `unknown`).
- Change `gold.interaction_pairs.source` to `sources ARRAY<STRING>` so a pair attested by both is reported as both — which is *stronger* evidence and better demo material than arbitrarily dropping one.

The gold schema in [§5.2](#52-neurorxgoldinteraction_pairs) reflects this. **This deviates from the task's "same shape as silver.interactions"** — flagged rather than assumed.

### F7 — Gold table count {#f7}

**Severity: Important.**

`ARCHITECTURE.md` §2 names exactly three gold tables: `drug_knowledge`, `interaction_pairs`, `adherence_facts`. But if Lakebase syncs to gold ([F3](#f3)), gold also needs `patients`, `schedules`, and `dose_events` mirrors — six tables, plus `guardrail_blocks` if [F2](#f2) resolves to Lakebase. The canonical doc's gold listing is incomplete.

**Recommendation:** accept the six/seven-table gold layer and amend `ARCHITECTURE.md` §2's node label. The alternative — syncing into `silver` — would put OLTP mirrors in a layer defined as "normalized on RxCUI," which is worse.

### F8 — Synthetic cohort has two conflicting paths {#f8}

**Severity: Important.**

`ARCHITECTURE.md` §2 draws the synthetic cohort into **bronze** (`A4 --> B`). It also draws Lakebase syncing into **gold** (`L -->|synced table| D`). So patient and dose data can reach the lakehouse two ways, and `gold.adherence_facts` could be computed from either — with no rule for which wins. If both paths run, the dashboard double-counts.

**Recommendation:** one-way flow, stated as an invariant — **Lakebase is the sole source of truth for patient state.**

```
generator → bronze.synthetic_*_raw   (audit record of what was generated; terminal, nothing reads it downstream)
generator → Lakebase                 (the live OLTP seed)
Lakebase  → gold.* (sync)            (the only path into analytics)
```

Bronze synthetic tables exist for lineage and reproducibility only. The Lakeflow pipeline must **not** build `adherence_facts` from them.

### F9 — `get_adherence_stats` target store is ambiguous {#f9}

**Severity: Important.**

Plan §5 and `ARCHITECTURE.md` §2 both say `get_adherence_stats` is *"SQL over `dose_events`"* — but `dose_events` will exist in two places: live in Lakebase and mirrored in `gold`. The choice has a visible demo consequence:

- **Lakebase (live):** a dose marked taken in the Today view is reflected instantly. Costs a Postgres round-trip from a UC function.
- **`gold` (synced):** consistent with the dashboard and Genie, but stale by the sync interval — mark a dose taken, ask "how am I doing?", and get an answer that doesn't include it. That is a bad demo beat and an easy judge question.

**Recommendation:** `get_adherence_stats` reads **`gold.adherence_facts`** for aggregate windows (streaks, % by drug, time-of-day) and the Today view reads **Lakebase directly** for today's checklist. Different consumers, different freshness needs. Decide before Phase 2 wires the tool.

### F10 — `silver.drugs.set_id` is scalar but the relationship is 1:N {#f10}

**Severity: Important.**

One RxCUI can have many SPL Set IDs — a generic drug has a separate label per manufacturer. `silver.drugs` has PK `rxcui` and a scalar `set_id`, which forces an arbitrary pick among several valid labels and silently discards the rest.

**Recommendation:** for a ~200-drug hackathon corpus, pin one label per RxCUI (the most recent `effective_time`) and rename the column `primary_set_id` to make the choice honest rather than accidental. `silver.label_sections` keeps its own `set_id` per chunk, so citations still point at the exact source label. Schema in [§4.1](#41-neurorxsilverdrugs) uses `primary_set_id`.

### F11 — Name→RxCUI is not 1:1 {#f11}

**Severity: Important.** Verified against the live RxNav API:

| Query | RxCUIs returned |
|---|---|
| `warfarin` | `11289` |
| `ibuprofen` | `5640` |
| `lisinopril` | `29046` |
| **`metformin`** | **`235743`, `6809`** ← two |

`metformin` — the drug used in the plan's own §8 demo script and Phase 1 checkpoint — resolves to two RxCUIs. `bronze.rxnorm_raw` must therefore allow multiple rows per queried name, and the silver normalization needs a documented rule for picking one.

**Recommendation:** `rxnorm_raw` grain is one row **per returned candidate** (not per query), carrying `tty` (term type). Silver picks the ingredient-level concept (`tty = 'IN'`) as canonical, since schedules are prescribed against ingredients. Without an explicit rule, "metformin" resolves nondeterministically and the Phase 1 checkpoint becomes flaky.

### F12 — `_source_file` semantics {#f12}

**Severity: Minor.** The task mandates `_source_file STRING` on every bronze table, but only DDInter is genuinely file-sourced. openFDA and RxNorm are REST APIs; the synthetic cohort is generated in-process.

**Recommendation:** keep the column and the name (uniformity is worth more than precision here), and define it per source: DDInter → filename; openFDA/RxNorm → request URL; synthetic → `generator:<module>@<git_sha>`.

### F13 — `day_part` boundaries undefined {#f13}

**Severity: Minor.** `adherence_facts.day_part` is required but neither source document defines the cutoffs, and "time-of-day patterns" is a headline dashboard feature. Boundaries proposed in [§1](#1-conventions); they are arbitrary but must be fixed somewhere, and they belong here rather than buried in pipeline code.

### F14 — `checked_at` semantics {#f14}

**Severity: Minor.** The name implies "when an interaction check ran," but `gold.interaction_pairs` is a reference table, not a check log — no patient ever causes a row to be written. Defined below as the pipeline refresh timestamp. If you intended a per-patient check audit trail, that is a **different table** and is not currently specified anywhere.

### F15 — `payload` type {#f15}

**Severity: Minor.** The task says "STRING/VARIANT payload" without choosing. Specified as `VARIANT` below (native semi-structured type, queryable without an explicit schema, avoids a `parse_json` round-trip in silver). If `VARIANT` is unavailable on the workspace's runtime, `STRING` + `parse_json()` at the silver boundary is an equivalent fallback — but pick one before Phase 1.

---

## 3. Bronze

Raw, as-ingested. No business logic, no drops. Every table carries `_ingested_at` and `_source_file` per [§1](#1-conventions).

### 3.1 `neurorx.bronze.fda_labels_raw`

**Layer:** Bronze · **Purpose:** One row per openFDA SPL label document, stored as returned. The clinical knowledge base's raw form.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `set_id` | `STRING` | No | SPL Set ID — stable identifier for a label across versions. Natural key. |
| `spl_version` | `STRING` | Yes | Label version within the Set ID. |
| `effective_time` | `DATE` | Yes | Label effective date. Used to pick the primary label per RxCUI ([F10](#f10)). |
| `payload` | `VARIANT` | No | The complete label JSON as returned by openFDA ([F15](#f15)). |
| `source_api` | `STRING` | No | Endpoint, e.g. `api.fda.gov/drug/label`. |
| `pull_query` | `STRING` | No | The exact query string used. Makes the pull reproducible. |
| `_ingested_at` | `TIMESTAMP` | No | Audit. |
| `_source_file` | `STRING` | No | Request URL ([F12](#f12)). |

**Key:** `(set_id, spl_version)` natural. Not enforced in bronze.

| Expectation | Expression | On violation |
|---|---|---|
| `set_id_present` | `set_id IS NOT NULL AND length(set_id) > 0` | warn |
| `payload_present` | `payload IS NOT NULL` | warn |
| `has_any_target_section` | `payload:dosage_and_administration IS NOT NULL OR payload:drug_interactions IS NOT NULL OR payload:warnings IS NOT NULL OR payload:information_for_patients IS NOT NULL OR payload:patient_medication_information IS NOT NULL OR payload:spl_patient_package_insert IS NOT NULL` | warn |

`has_any_target_section` tracks labels that carry none of the four sections the product needs — they are legitimate openFDA responses but contribute nothing downstream. Retained in bronze; expect a nonzero count.

**Includes both `information_for_patients` fallback fields**, not just the canonical name. Verified live against the openFDA API during `data/ingestion/01_openfda_ingest.py`: `information_for_patients` is sometimes absent while `patient_medication_information` or `spl_patient_package_insert` carries the same patient-guidance content (confirmed populated on real labels — isotretinoin, combined oral contraceptives). The task spec that produced that notebook originally named the fallback `patient_information`, which does not exist in the openFDA schema; the correct field, confirmed against openFDA's searchable-fields reference, is `patient_medication_information`. This expression originally checked only the four canonical section names and undercounted labels that carried patient content solely under a fallback key — fixed here to check all three `information_for_patients`-family paths.

### 3.2 `neurorx.bronze.rxnorm_raw`

**Layer:** Bronze · **Purpose:** Raw name→RxCUI resolution results from the RxNav REST API.

**Grain: one row per returned candidate, not per query** — a name can resolve to several RxCUIs ([F11](#f11)).

| Column | Type | Nullable | Description |
|---|---|---|---|
| `query_name` | `STRING` | No | The drug name submitted to RxNav. |
| `rxcui` | `STRING` | Yes | A returned RxCUI. `NULL` when the name resolved to nothing — the miss is recorded deliberately. |
| `rxnorm_name` | `STRING` | Yes | RxNorm's canonical name for the concept. |
| `tty` | `STRING` | Yes | Term type (`IN` ingredient, `BN` brand name, `SCD` clinical drug, …). Drives canonical selection in silver. |
| `rank` | `INT` | Yes | RxNav-returned ordering, where present. |
| `payload` | `VARIANT` | Yes | Full raw response for audit. |
| `_ingested_at` | `TIMESTAMP` | No | Audit. |
| `_source_file` | `STRING` | No | Request URL ([F12](#f12)). |

**Key:** `(query_name, rxcui)` natural; `rxcui` nullable so unresolved names are retained.

| Expectation | Expression | On violation |
|---|---|---|
| `query_name_present` | `query_name IS NOT NULL` | warn |
| `rxcui_numeric` | `rxcui IS NULL OR rxcui RLIKE '^[0-9]+$'` | warn |
| `resolution_rate` | `rxcui IS NOT NULL` | warn |

`resolution_rate` is a **coverage metric, not a defect** — it surfaces the share of the ~200-drug list RxNav could not resolve. A low rate means the input list needs cleaning before Phase 1's checkpoint can pass.

### 3.3 `neurorx.bronze.ddinter_raw`

**Layer:** Bronze · **Purpose:** Raw DDInter 2.0 CSV rows, as parsed. Column names mirror the source file.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `ddinter_id_a` | `STRING` | Yes | DDInter's internal id for drug A. |
| `drug_a_name` | `STRING` | No | Drug A name as written in DDInter. Not yet RxCUI-resolved. |
| `ddinter_id_b` | `STRING` | Yes | DDInter's internal id for drug B. |
| `drug_b_name` | `STRING` | No | Drug B name as written in DDInter. |
| `severity_level` | `STRING` | No | DDInter severity, source casing (`Major`/`Moderate`/`Minor`/`Unknown`). Normalized to lowercase in silver. |
| `_ingested_at` | `TIMESTAMP` | No | Audit. |
| `_source_file` | `STRING` | No | Source CSV filename ([F12](#f12)). |

**Key:** `(drug_a_name, drug_b_name)` natural, **unordered and un-canonicalized at this layer** — DDInter's own row order is preserved. Canonical ordering is applied in silver ([§7](#7-invariants)).

| Expectation | Expression | On violation |
|---|---|---|
| `both_drugs_present` | `drug_a_name IS NOT NULL AND drug_b_name IS NOT NULL` | warn |
| `severity_recognized` | `lower(severity_level) IN ('major','moderate','minor','unknown')` | warn |
| `not_self_pair` | `lower(drug_a_name) != lower(drug_b_name)` | warn |

### 3.4 `neurorx.bronze.synthetic_patients_raw`

**Layer:** Bronze · **Purpose:** Audit record of the generated synthetic cohort. **Terminal — nothing downstream reads this** ([F8](#f8)). 50 patients.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `patient_id` | `STRING` | No | UUID as text. Matches the value seeded into Lakebase. |
| `display_name` | `STRING` | No | Synthetic patient name. No real person. |
| `caregiver_name` | `STRING` | Yes | Synthetic caregiver name; `NULL` for patients without one. |
| `created_at` | `TIMESTAMP` | No | Synthetic account creation time. |
| `_ingested_at` | `TIMESTAMP` | No | Audit. |
| `_source_file` | `STRING` | No | `generator:<module>@<git_sha>` ([F12](#f12)). |

**Key:** `patient_id`.

| Expectation | Expression | On violation |
|---|---|---|
| `patient_id_present` | `patient_id IS NOT NULL` | warn |
| `patient_id_is_uuid` | `patient_id RLIKE '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'` | warn |

### 3.5 `neurorx.bronze.synthetic_dose_events_raw`

**Layer:** Bronze · **Purpose:** Audit record of generated dose events. **Terminal** ([F8](#f8)). ~6 months of history with realistic missed-dose patterns.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `event_id` | `STRING` | No | UUID as text. |
| `schedule_id` | `STRING` | No | FK to the generated schedule ([F5](#f5)). |
| `patient_id` | `STRING` | No | FK to the generated patient. |
| `rxcui` | `STRING` | No | Drug the event belongs to. Denormalized for audit convenience. |
| `planned_ts` | `TIMESTAMP` | No | When the dose was scheduled. |
| `actioned_ts` | `TIMESTAMP` | Yes | When the patient acted. `NULL` for `planned`/`missed`. |
| `status` | `STRING` | No | `dose_status` enum. |
| `_ingested_at` | `TIMESTAMP` | No | Audit. |
| `_source_file` | `STRING` | No | `generator:<module>@<git_sha>` ([F12](#f12)). |

**Key:** `event_id`.

| Expectation | Expression | On violation |
|---|---|---|
| `event_id_present` | `event_id IS NOT NULL` | warn |
| `status_recognized` | `status IN ('planned','taken','skipped','missed')` | warn |
| `actioned_ts_consistent` | `(actioned_ts IS NOT NULL) = (status IN ('taken','skipped'))` | warn |
| `actioned_after_planned` | `actioned_ts IS NULL OR actioned_ts >= planned_ts` | warn |

### 3.6 `neurorx.bronze.synthetic_schedules_raw`

> **Provisional — not in the Task 0.5 spec.** Added per [F5](#f5); `dose_events.schedule_id` is a required FK with no source without it.

**Layer:** Bronze · **Purpose:** Audit record of generated schedules. **Terminal** ([F8](#f8)).

| Column | Type | Nullable | Description |
|---|---|---|---|
| `schedule_id` | `STRING` | No | UUID as text. |
| `patient_id` | `STRING` | No | FK to the generated patient. |
| `rxcui` | `STRING` | No | Canonical drug identifier. |
| `drug_name` | `STRING` | No | Display name at generation time. |
| `dose_text` | `STRING` | No | Free-text dose, e.g. `500 mg`. |
| `times_per_day` | `INT` | No | Doses per day. |
| `dose_times` | `ARRAY<STRING>` | No | `HH:MM:SS` strings. Delta has no `TIME` type — becomes `TIME[]` in Lakebase. |
| `timing_notes` | `STRING` | Yes | e.g. `with food`. |
| `status` | `STRING` | No | `schedule_status` enum. |
| `created_at` | `TIMESTAMP` | No | Audit. |
| `_ingested_at` | `TIMESTAMP` | No | Audit. |
| `_source_file` | `STRING` | No | `generator:<module>@<git_sha>`. |

**Key:** `schedule_id`.

| Expectation | Expression | On violation |
|---|---|---|
| `schedule_id_present` | `schedule_id IS NOT NULL` | warn |
| `dose_times_match_frequency` | `size(dose_times) = times_per_day` | warn |
| `status_recognized` | `status IN ('active','stopped')` | warn |

---

## 4. Silver

Normalized, RxCUI-keyed, deduped. **This layer enforces** — expectations drop or fail rather than warn.

### 4.1 `neurorx.silver.drugs`

**Layer:** Silver · **Purpose:** The canonical drug dimension. One row per RxCUI.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `rxcui` | `STRING` | No | **PK.** Canonical RxNorm concept id, ingredient-level (`tty = 'IN'`) per [F11](#f11). |
| `generic_name` | `STRING` | No | RxNorm canonical ingredient name, lowercase. |
| `brand_names` | `ARRAY<STRING>` | Yes | Known brand names (`tty = 'BN'`). Empty array when none; never `NULL` in practice. |
| `primary_set_id` | `STRING` | Yes | The one SPL Set ID chosen for this drug — most recent `effective_time` ([F10](#f10)). Renamed from `set_id` to make the arbitrary pick explicit. `NULL` when no label was retrieved. |

**Key:** `rxcui` (primary).

| Expectation | Expression | On violation |
|---|---|---|
| `rxcui_present` | `rxcui IS NOT NULL AND rxcui RLIKE '^[0-9]+$'` | `DROP ROW` |
| `rxcui_unique` | `count(*) OVER (PARTITION BY rxcui) = 1` | `FAIL UPDATE` |
| `generic_name_present` | `generic_name IS NOT NULL AND length(generic_name) > 0` | `DROP ROW` |
| `has_label` | `primary_set_id IS NOT NULL` | warn |

`rxcui_unique` fails the pipeline rather than dropping: a duplicate RxCUI means the [F11](#f11) selection rule is broken, and silently dropping one would make the drug dimension nondeterministic. `has_label` only warns — a drug with no retrieved label is valid, it simply cannot be cited.

### 4.2 `neurorx.silver.label_sections`

**Layer:** Silver · **Purpose:** FDA label text split into section-aware, retrieval-sized chunks. The substrate for every citation.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `chunk_id` | `STRING` | No | **PK.** Deterministic — see construction rule below. |
| `rxcui` | `STRING` | No | FK → `silver.drugs.rxcui`. |
| `set_id` | `STRING` | No | The SPL Set ID this chunk came from. Per-chunk, so citations resolve to the exact source label. |
| `drug_name` | `STRING` | No | Denormalized display name. Avoids a join in the retrieval path. |
| `section` | `STRING` | No | `section` enum. |
| `chunk_index` | `INT` | No | 0-based ordinal within `(set_id, section)`. |
| `chunk_text` | `STRING` | No | The chunk itself. This is the text a citation quotes. |
| `token_count` | `INT` | No | Token count of `chunk_text`. Target 500–800 per plan §4. |

**`chunk_id` construction — stable across pipeline reruns:**

```sql
concat_ws(':', set_id, section, lpad(cast(chunk_index AS STRING), 4, '0'))
-- e.g. 'a1b2c3d4-...:drug_interactions:0007'
```

Human-readable (good for demoing a citation) and deterministic. **Stability is a hard requirement, not a nicety:** `chunk_id` is the PK of the Vector Search index and the citation handle the agent emits. If a rerun renumbers chunks, every previously-emitted citation silently points somewhere else. Never derive `chunk_id` from anything that varies run to run.

**Key:** `chunk_id` (primary); `(set_id, section, chunk_index)` natural.

| Expectation | Expression | On violation |
|---|---|---|
| `chunk_id_present` | `chunk_id IS NOT NULL` | `DROP ROW` |
| `chunk_id_unique` | `count(*) OVER (PARTITION BY chunk_id) = 1` | `FAIL UPDATE` |
| `rxcui_present` | `rxcui IS NOT NULL` | `DROP ROW` |
| `section_recognized` | `section IN ('dosage_and_administration','drug_interactions','warnings','information_for_patients')` | `DROP ROW` |
| `chunk_text_present` | `chunk_text IS NOT NULL AND length(trim(chunk_text)) > 0` | `DROP ROW` |
| `token_count_ceiling` | `token_count <= 1000` | `FAIL UPDATE` |
| `token_count_floor` | `token_count >= 500` | warn |

`token_count_ceiling` fails the pipeline: an oversized chunk indicates a chunker defect and risks silent truncation at embedding time — which would drop clinical text out of the retrievable corpus without any error. `token_count_floor` only warns, because the trailing chunk of a section is legitimately short.

### 4.3 `neurorx.silver.interactions`

**Layer:** Silver · **Purpose:** Interaction pairs resolved to RxCUI and canonically ordered. **One row per source** — cross-source dedupe happens in gold ([F6](#f6)).

| Column | Type | Nullable | Description |
|---|---|---|---|
| `rxcui_a` | `STRING` | No | Canonically-ordered first RxCUI. **See [§7](#7-invariants) — ordering is lexicographic.** |
| `rxcui_b` | `STRING` | No | Canonically-ordered second RxCUI. Always `rxcui_a < rxcui_b`. |
| `severity` | `STRING` | No | `severity` enum, lowercase. |
| `description` | `STRING` | Yes | Human-readable interaction description. The text the LLM is permitted to paraphrase. |
| `source` | `STRING` | No | `source` enum — `ddinter` or `fda_label`. |

**Key:** `(rxcui_a, rxcui_b, source)` — `source` is part of the key so the same pair from both sources survives as two rows.

| Expectation | Expression | On violation |
|---|---|---|
| `both_rxcui_present` | `rxcui_a IS NOT NULL AND rxcui_b IS NOT NULL` | `DROP ROW` |
| `canonical_order` | `rxcui_a < rxcui_b` | `FAIL UPDATE` |
| `not_self_pair` | `rxcui_a != rxcui_b` | `DROP ROW` |
| `severity_recognized` | `severity IN ('major','moderate','minor','unknown')` | `DROP ROW` |
| `source_recognized` | `source IN ('ddinter','fda_label')` | `DROP ROW` |
| `pair_source_unique` | `count(*) OVER (PARTITION BY rxcui_a, rxcui_b, source) = 1` | `FAIL UPDATE` |

`canonical_order` is `FAIL UPDATE`, not `DROP ROW`. A violation means the [§7](#7-invariants) invariant was not applied — dropping the row would **delete a real interaction** and produce exactly the silent false negative the deterministic core exists to prevent. Halt instead.

---

## 5. Gold

Serving layer. Feeds the agent tools, the Vector Search index, the dashboard, and Genie.

### 5.1 `neurorx.gold.drug_knowledge`

**Layer:** Gold · **Purpose:** The Vector Search source table. Its columns are exactly the [citation contract](#8-citation-contract) — one row is one citable chunk.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `chunk_id` | `STRING` | No | **PK.** The citation handle. Index primary key. |
| `rxcui` | `STRING` | No | Drug identifier, used as a Vector Search filter. |
| `drug_name` | `STRING` | No | Display name for the citation chip. |
| `section` | `STRING` | No | `section` enum. Used as a Vector Search filter. |
| `chunk_text` | `STRING` | No | The quoted text. **Embedding source column.** |
| `set_id` | `STRING` | No | SPL Set ID — lineage back to the source label. |

**Key:** `chunk_id` (primary).

**Change Data Feed is mandatory:**

```sql
ALTER TABLE neurorx.gold.drug_knowledge
SET TBLPROPERTIES (delta.enableChangeDataFeed = true);
```

Databricks AI Search `DELTA_SYNC` indexes require CDF on the source table. This is not optional and not a tuning knob: Free Edition supports **only** `DELTA_SYNC` (Direct Vector Access is unavailable — see `setup/00_workspace_runbook.md` §3), so without CDF the index cannot be created at all and Phase 1's vector checkpoint cannot pass.

**Index:** `neurorx.gold.drug_knowledge_index` (matches `VECTOR_INDEX_FULLNAME` in [`app/config.py`](app/config.py)) — `DELTA_SYNC`, PK `chunk_id`, embedding source `chunk_text`, filterable on `rxcui` and `section` (the two arguments `search_drug_labels` takes).

| Expectation | Expression | On violation |
|---|---|---|
| `chunk_id_unique` | `count(*) OVER (PARTITION BY chunk_id) = 1` | `FAIL UPDATE` |
| `citation_fields_complete` | `chunk_id IS NOT NULL AND rxcui IS NOT NULL AND drug_name IS NOT NULL AND section IS NOT NULL AND set_id IS NOT NULL` | `DROP ROW` |
| `chunk_text_present` | `chunk_text IS NOT NULL AND length(trim(chunk_text)) > 0` | `DROP ROW` |

`citation_fields_complete` drops rather than warns: a chunk missing any citation field is **unciteable**, and per `ARCHITECTURE.md` §5(b) an unciteable chunk cannot legally support a clinical claim. Better absent from the corpus than retrievable-but-uncitable.

### 5.2 `neurorx.gold.interaction_pairs`

**Layer:** Gold · **Purpose:** **The deterministic safety table.** `check_interactions` queries this and nothing else. No LLM participates in reading it.

> **Deviates from the Task 0.5 spec** ("same shape as silver.interactions, deduped, plus checked_at") — `source STRING` becomes `sources ARRAY<STRING>` per [F6](#f6). Flagged, not assumed.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `rxcui_a` | `STRING` | No | Canonically-ordered first RxCUI. **[§7](#7-invariants).** |
| `rxcui_b` | `STRING` | No | Canonically-ordered second RxCUI. |
| `severity` | `STRING` | No | Resolved `severity` enum. Precedence: `ddinter` over `fda_label`; on tie, higher severity wins ([F6](#f6)). |
| `description` | `STRING` | Yes | Description from the winning source. |
| `sources` | `ARRAY<STRING>` | No | Every source attesting this pair, e.g. `['ddinter','fda_label']`. Never empty. |
| `checked_at` | `TIMESTAMP` | No | **Pipeline refresh timestamp** — when this row was last rebuilt from silver ([F14](#f14)). Not a per-patient check audit. |

**Key:** `(rxcui_a, rxcui_b)` (primary) — deduped across sources.

| Expectation | Expression | On violation |
|---|---|---|
| `pair_unique` | `count(*) OVER (PARTITION BY rxcui_a, rxcui_b) = 1` | `FAIL UPDATE` |
| `canonical_order` | `rxcui_a < rxcui_b` | `FAIL UPDATE` |
| `severity_recognized` | `severity IN ('major','moderate','minor','unknown')` | `FAIL UPDATE` |
| `sources_non_empty` | `size(sources) >= 1` | `FAIL UPDATE` |
| `checked_at_present` | `checked_at IS NOT NULL` | `DROP ROW` |
| `warfarin_ibuprofen_present` | `(SELECT count(*) FROM neurorx.gold.interaction_pairs WHERE rxcui_a = '11289' AND rxcui_b = '5640') = 1` | `FAIL UPDATE` |

**Every expectation on this table is `FAIL UPDATE`.** This is deliberate and differs from every other table in this document. Elsewhere a bad row is a data-quality problem; here a bad or missing row is a **missed drug interaction presented to a patient as safety**. There is no acceptable partial state — halt the pipeline and fix the table.

`warfarin_ibuprofen_present` is a canary asserting the plan's own §7 true-positive and `ARCHITECTURE.md` §7's Phase 1 exit checkpoint, encoded with the [F1](#f1) lexicographic ordering. It fails loudly if the pair-ordering convention ever silently flips — the specific defect [F1](#f1) describes. Note the RxCUI order in the expression (`11289` before `5640`) looks backwards numerically; that is the point.

### 5.3 `neurorx.gold.adherence_facts`

**Layer:** Gold · **Purpose:** Adherence aggregates for the dashboard, Genie, and `get_adherence_stats`. **Derived from the synced `gold.dose_events`, not itself a synced table** ([F3](#f3)).

> Includes `skipped_doses`, which the Task 0.5 spec omits — see [F4](#f4).

**Grain: `(patient_id, rxcui, event_date, day_part)`** — one row per drug per day-part per day. A twice-daily drug produces two rows per date. The task's column list implies but does not state this grain; a `(patient_id, rxcui, event_date)` grain cannot carry a single `day_part`.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `patient_id` | `STRING` | No | FK → `gold.patients.patient_id`. |
| `rxcui` | `STRING` | No | FK → `silver.drugs.rxcui`. |
| `drug_name` | `STRING` | No | Denormalized display name. |
| `event_date` | `DATE` | No | Local calendar date of `planned_ts`. |
| `day_part` | `STRING` | No | `day_part` enum, bucketed from `planned_ts` per [§1](#1-conventions). |
| `planned_doses` | `INT` | No | Doses scheduled in this bucket. |
| `taken_doses` | `INT` | No | Marked `taken`. |
| `skipped_doses` | `INT` | No | Marked `skipped` — deliberate non-take ([F4](#f4)). |
| `missed_doses` | `INT` | No | Marked `missed` — no action within the window. |
| `adherence_pct` | `DOUBLE` | No | `taken_doses / NULLIF(planned_doses, 0) * 100`. Skips count against adherence ([F4](#f4)). |

**Key:** `(patient_id, rxcui, event_date, day_part)` (primary).

| Expectation | Expression | On violation |
|---|---|---|
| `grain_unique` | `count(*) OVER (PARTITION BY patient_id, rxcui, event_date, day_part) = 1` | `FAIL UPDATE` |
| `day_part_recognized` | `day_part IN ('morning','afternoon','evening','night')` | `DROP ROW` |
| `counts_non_negative` | `planned_doses >= 0 AND taken_doses >= 0 AND skipped_doses >= 0 AND missed_doses >= 0` | `DROP ROW` |
| `counts_reconcile` | `taken_doses + skipped_doses + missed_doses <= planned_doses` | `FAIL UPDATE` |
| `adherence_pct_bounded` | `adherence_pct BETWEEN 0 AND 100` | `FAIL UPDATE` |
| `adherence_pct_consistent` | `planned_doses = 0 OR abs(adherence_pct - (taken_doses / planned_doses * 100)) < 0.01` | `FAIL UPDATE` |

`counts_reconcile` is `<=`, not `=`: doses still `planned` (in the future, or awaiting action today) are counted in `planned_doses` but not yet in any outcome bucket. An equality check would fail every time the pipeline runs mid-day.

---

## 6. Lakebase (Postgres, OLTP)

Instance `neurorx-oltp` ([`ARCHITECTURE.md`](ARCHITECTURE.md) §4). **Postgres types, not Spark types.**

**Lakebase is the sole source of truth for patient state** ([F8](#f8)). Everything in gold about patients is a mirror or a derivative of these tables.

### 6.1 `patients`

**Purpose:** The patient record. Synthetic only — no PHI, ever ([`ARCHITECTURE.md`](ARCHITECTURE.md) §5).

| Column | Type | Nullable | Description |
|---|---|---|---|
| `patient_id` | `UUID` | No | **PK.** `DEFAULT gen_random_uuid()`. |
| `display_name` | `TEXT` | No | Synthetic patient name. |
| `caregiver_name` | `TEXT` | Yes | Synthetic caregiver name. `NULL` when the patient has no caregiver — the caregiver persona is secondary, not universal. |
| `created_at` | `TIMESTAMPTZ` | No | `DEFAULT now()`. |

```sql
CONSTRAINT patients_display_name_present CHECK (length(trim(display_name)) > 0)
```

### 6.2 `schedules`

**Purpose:** A patient's active and stopped prescriptions. Written **only** by `manage_schedule`, and only after explicit user confirmation ([`ARCHITECTURE.md`](ARCHITECTURE.md) §5(d)).

| Column | Type | Nullable | Description |
|---|---|---|---|
| `schedule_id` | `UUID` | No | **PK.** `DEFAULT gen_random_uuid()`. |
| `patient_id` | `UUID` | No | **FK** → `patients(patient_id)` `ON DELETE CASCADE`. |
| `rxcui` | `TEXT` | No | Canonical drug id. The join key to all clinical reference data. |
| `drug_name` | `TEXT` | No | Display name as confirmed by the user. |
| `dose_text` | `TEXT` | No | Free text as extracted and confirmed, e.g. `500 mg`. **Never parsed for clinical logic** — dosing is out of scope ([`ARCHITECTURE.md`](ARCHITECTURE.md) §1). |
| `times_per_day` | `INTEGER` | No | Doses per day. |
| `dose_times` | `TIME[]` | No | Scheduled times, e.g. `{08:00, 20:00}`. |
| `timing_notes` | `TEXT` | Yes | Constraint text, e.g. `with food`. |
| `status` | `TEXT` | No | `schedule_status` enum. |
| `created_at` | `TIMESTAMPTZ` | No | `DEFAULT now()`. |
| `updated_at` | `TIMESTAMPTZ` | No | `DEFAULT now()`; bump on every write. |

```sql
CONSTRAINT schedules_status_valid    CHECK (status IN ('active','stopped')),
CONSTRAINT schedules_rxcui_numeric   CHECK (rxcui ~ '^[0-9]+$'),
CONSTRAINT schedules_times_positive  CHECK (times_per_day > 0),
CONSTRAINT schedules_frequency_match CHECK (cardinality(dose_times) = times_per_day),
CONSTRAINT schedules_updated_after   CHECK (updated_at >= created_at)
```

`schedules_frequency_match` is the structured-frequency invariant: `times_per_day` and `dose_times` are two representations of one fact and must never disagree. A schedule claiming `times_per_day = 2` with one entry in `dose_times` would generate the wrong number of `dose_events`, quietly corrupting every adherence number downstream.

### 6.3 `dose_events`

**Purpose:** The adherence ledger. Written by the Today view when a patient marks a dose, and by the reminders job when it materializes upcoming doses.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `event_id` | `UUID` | No | **PK.** `DEFAULT gen_random_uuid()`. |
| `schedule_id` | `UUID` | No | **FK** → `schedules(schedule_id)` `ON DELETE CASCADE`. |
| `patient_id` | `UUID` | No | **FK** → `patients(patient_id)`. Denormalized — derivable via `schedule_id`, kept for query performance. |
| `planned_ts` | `TIMESTAMPTZ` | No | When the dose was due. Source of `event_date` and `day_part` in gold. |
| `actioned_ts` | `TIMESTAMPTZ` | Yes | When the patient acted. `NULL` unless `taken`/`skipped`. |
| `status` | `TEXT` | No | `dose_status` enum. |

```sql
CONSTRAINT dose_events_status_valid CHECK (status IN ('planned','taken','skipped','missed')),
CONSTRAINT dose_events_actioned_consistent
  CHECK ((actioned_ts IS NOT NULL) = (status IN ('taken','skipped'))),
CONSTRAINT dose_events_actioned_after_planned
  CHECK (actioned_ts IS NULL OR actioned_ts >= planned_ts)
```

**Denormalization invariant** — not expressible as a Postgres `CHECK` (it spans tables); enforce in `manage_schedule` and assert in the Lakeflow pipeline after sync:

```sql
-- must return zero rows
SELECT e.event_id FROM dose_events e
JOIN schedules s ON e.schedule_id = s.schedule_id
WHERE e.patient_id <> s.patient_id;
```

`dose_events_actioned_consistent` encodes the four-state model precisely: `planned` (future/pending) and `missed` (window elapsed, no action) both have `NULL actioned_ts`; `taken` and `skipped` both require one. Enforcing this as a biconditional prevents the ambiguous state of a `missed` dose carrying an action timestamp.

### 6.4 `guardrail_blocks`

> **Home is contested — see [F2](#f2).** Specified here per the Task 0.5 spec, but `ARCHITECTURE.md` §2/§5(e) and plan §5 all say Delta. The column list is identical either way; only the store is open. **Decide before Phase 4.**

**Purpose:** Append-only log of every response the output guardrail blocked. Shown in the demo as evidence the safety net fires ([`ARCHITECTURE.md`](ARCHITECTURE.md) §5(e)).

| Column | Type | Nullable | Description |
|---|---|---|---|
| `block_id` | `UUID` | No | **PK.** `DEFAULT gen_random_uuid()`. |
| `ts` | `TIMESTAMPTZ` | No | `DEFAULT now()`. When the block fired. |
| `patient_id` | `UUID` | Yes | **FK** → `patients(patient_id)`. `NULL` for anonymous or pre-auth sessions. |
| `model_output_excerpt` | `TEXT` | No | The blocked text. Excerpt, not the full response — enough to show a judge what was caught. |
| `rule_triggered` | `TEXT` | No | What fired: a regex rule name, or `llm_judge`. |
| `judge_verdict` | `TEXT` | Yes | The judge's raw verdict. `NULL` when a regex rule blocked without a judge call ([`ARCHITECTURE.md`](ARCHITECTURE.md) §5(e): "regex + one cheap LLM-judge call"). |

```sql
CONSTRAINT guardrail_blocks_excerpt_present CHECK (length(trim(model_output_excerpt)) > 0),
CONSTRAINT guardrail_blocks_rule_present    CHECK (length(trim(rule_triggered)) > 0)
```

**Append-only.** Never update or delete a row: this table is the evidence that the safety architecture works, and it is only credible if nothing can quietly edit it after the fact.

---

## 7. Invariants

### 7.1 Interaction pair ordering — canonical order is LEXICOGRAPHIC

Interaction pairs are unordered facts: warfarin+ibuprofen is the same interaction as ibuprofen+warfarin. Storing both directions would double the table and let the two rows drift apart. So exactly one direction is stored, chosen by a canonical rule.

**The invariant, holding on `silver.interactions` and `gold.interaction_pairs`:**

```
rxcui_a < rxcui_b     -- always, no exceptions
```

**The canonical expression** — the only permitted way to produce a pair. Use it verbatim; do not hand-roll an equivalent:

```sql
SELECT
  LEAST(rxcui_a, rxcui_b)    AS rxcui_a,
  GREATEST(rxcui_a, rxcui_b) AS rxcui_b
FROM <source>
```

**`check_interactions` must apply the identical expression to its inputs before lookup.** Generating every unordered combination from `rxcui_list`, canonicalizing each, then querying:

```sql
-- for each unordered pair (x, y) drawn from rxcui_list:
SELECT rxcui_a, rxcui_b, severity, description, sources
FROM neurorx.gold.interaction_pairs
WHERE rxcui_a = LEAST(x, y) AND rxcui_b = GREATEST(x, y)
```

> ### ⚠️ This ordering is lexicographic, not numeric
>
> `rxcui` is a `STRING`, so `LEAST`/`GREATEST` compare character by character. **Verified against the live RxNav API:**
>
> | Drug | RxCUI |
> |---|---|
> | warfarin | `11289` |
> | ibuprofen | `5640` |
>
> - Lexicographic: `'11289' < '5640'` (since `'1' < '5'`) → stored as `('11289', '5640')`
> - Numeric: `5640 < 11289` → would be `('5640', '11289')`
>
> **The two conventions produce opposite pairs.** Write with one and read with the other and the query returns zero rows: the interaction is silently missed. No error, no warning — a false negative on the exact pair named as the Phase 1 exit checkpoint.
>
> The stored order will look wrong to anyone who reads `11289` and `5640` as numbers. It is not wrong. **Do not "fix" it** — casting to `BIGINT` in the tool while the pipeline stays lexicographic breaks the safety core in the least visible way possible. If you want numeric ordering, change the pipeline, the tool, and the `warfarin_ibuprofen_present` canary in [§5.2](#52-neurorxgoldinteraction_pairs) together, in one commit. See [F1](#f1).

Guarded by `canonical_order` (`FAIL UPDATE`) on both tables, plus the `warfarin_ibuprofen_present` canary in gold.

### 7.2 Chunk identity is stable

`chunk_id` must be reproducible across pipeline reruns ([§4.2](#42-neurorxsilverlabel_sections)). It is the Vector Search PK and the citation handle the agent emits to users. Renumbering chunks on a rerun silently repoints every citation ever emitted.

### 7.3 Lakebase is the sole source of truth for patient state

Bronze `synthetic_*_raw` tables are terminal audit records. Gold patient data derives **only** from the Lakebase sync, never from bronze ([F8](#f8)).

---

## 8. Citation contract

Per [`ARCHITECTURE.md`](ARCHITECTURE.md) §5(b), every clinical claim carries a citation to an FDA label chunk.

**`search_drug_labels(rxcui, section, query)` returns a JSON array. Each element is exactly:**

```json
{
  "chunk_id":  "a1b2c3d4-e5f6-7890-abcd-ef1234567890:information_for_patients:0003",
  "rxcui":     "6809",
  "drug_name": "metformin",
  "section":   "information_for_patients",
  "set_id":    "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "chunk_text": "If you miss a dose, take it as soon as you remember..."
}
```

| Field | Type | Description |
|---|---|---|
| `chunk_id` | string | **The citation handle.** What the agent references; what the UI resolves to a citation chip. |
| `rxcui` | string | The drug the chunk describes. |
| `drug_name` | string | Display name for the chip. |
| `section` | string | `section` enum — which part of the label this came from. |
| `set_id` | string | SPL Set ID. Lineage to the source label document. |
| `chunk_text` | string | The quotable text. The **only** clinical text the agent may state or paraphrase. |

These six fields map 1:1 onto `neurorx.gold.drug_knowledge` ([§5.1](#51-neurorxgolddrug_knowledge)). No field is computed, renamed, or added at the tool boundary — the retrieval tool projects the table row and returns it. Keeping them identical is what makes the citation *verifiable*: a judge can take a `chunk_id` out of a chat answer, query the gold table, and see the same text.

### The binding rule

> **Any agent answer making a clinical claim must reference at least one `chunk_id`.**

A "clinical claim" is any statement about dosing, timing, missed doses, food or drug interactions, side effects, or warnings — anything a patient could act on medically.

Consequences, all load-bearing:

1. **An empty result set is not a gap to fill.** If retrieval returns nothing relevant, the agent says so and directs the user to their pharmacist ([`ARCHITECTURE.md`](ARCHITECTURE.md) §5(b)). It never answers from model knowledge.
2. **The guardrail enforces this after generation.** A response containing dosage instructions with no `chunk_id` is blocked and logged to [`guardrail_blocks`](#64-guardrail_blocks) ([`ARCHITECTURE.md`](ARCHITECTURE.md) §5(e)). The rule is enforced in code, not merely requested in the prompt.
3. **Interaction results are exempt from *this* rule but not from citation.** `check_interactions` returns `description` and `sources` from [`gold.interaction_pairs`](#52-neurorxgoldinteraction_pairs), which is deterministic reference data, not a label chunk. Interaction claims cite the interaction row's `sources`, not a `chunk_id`. The guardrail must recognize both citation forms or it will block correct interaction answers.

Point 3 is easy to miss and would surface as the guardrail blocking the single most important demo beat.

---

## 9. Lakebase → Delta sync map

Lakebase syncs to Delta for analytics ([`ARCHITECTURE.md`](ARCHITECTURE.md) §8: *"OLTP where OLTP belongs"*). Mirrors are **read-only in Delta** — writing to a synced table would be overwritten and would corrupt the OLTP-is-truth invariant ([§7.3](#73-lakebase-is-the-sole-source-of-truth-for-patient-state)).

| Lakebase table | Delta target | Type | Notes |
|---|---|---|---|
| `patients` | `neurorx.gold.patients` | Mirror | Read-only. |
| `schedules` | `neurorx.gold.schedules` | Mirror | Read-only. `dose_times TIME[]` → `ARRAY<STRING>`. |
| `dose_events` | `neurorx.gold.dose_events` | Mirror | Read-only. The substrate for `adherence_facts`. |
| `guardrail_blocks` | `neurorx.gold.guardrail_blocks` | Mirror | **Only if [F2](#f2) resolves to Lakebase.** If it resolves to Delta-native, this row disappears and the table is written directly. |
| — | `neurorx.gold.adherence_facts` | **Derived** | **Not a mirror** ([F3](#f3)). Computed by Lakeflow from `gold.dose_events` + `gold.schedules`. |
| — | `neurorx.gold.drug_knowledge` | Derived | From `silver.label_sections`. No Lakebase involvement. |
| — | `neurorx.gold.interaction_pairs` | Derived | From `silver.interactions`. No Lakebase involvement. |

**Resulting gold layer: seven tables** — three mirrors, three derived, plus `guardrail_blocks` pending [F2](#f2). `ARCHITECTURE.md` §2 names only three ([F7](#f7)) and needs amending.

**Type mapping across the sync boundary:**

| Postgres | Delta |
|---|---|
| `UUID` | `STRING` |
| `TEXT` | `STRING` |
| `TIMESTAMPTZ` | `TIMESTAMP` |
| `INTEGER` | `INT` |
| `TIME[]` | `ARRAY<STRING>` |

`TIME[]` is the one lossy hop: Delta has no `TIME` type, so times arrive as `HH:MM:SS` strings. Anything in gold reading `dose_times` must parse rather than compare directly.

---

## Appendix — Table index

| Table | Layer | Key | Status |
|---|---|---|---|
| `neurorx.bronze.fda_labels_raw` | Bronze | `(set_id, spl_version)` | Frozen |
| `neurorx.bronze.rxnorm_raw` | Bronze | `(query_name, rxcui)` | Frozen |
| `neurorx.bronze.ddinter_raw` | Bronze | `(drug_a_name, drug_b_name)` | Frozen |
| `neurorx.bronze.synthetic_patients_raw` | Bronze | `patient_id` | Frozen |
| `neurorx.bronze.synthetic_dose_events_raw` | Bronze | `event_id` | Frozen |
| `neurorx.bronze.synthetic_schedules_raw` | Bronze | `schedule_id` | **Provisional — [F5](#f5)** |
| `neurorx.silver.drugs` | Silver | `rxcui` | Frozen |
| `neurorx.silver.label_sections` | Silver | `chunk_id` | Frozen |
| `neurorx.silver.interactions` | Silver | `(rxcui_a, rxcui_b, source)` | Frozen |
| `neurorx.gold.drug_knowledge` | Gold | `chunk_id` | Frozen — CDF required |
| `neurorx.gold.interaction_pairs` | Gold | `(rxcui_a, rxcui_b)` | **Deviates — [F6](#f6)** |
| `neurorx.gold.adherence_facts` | Gold | `(patient_id, rxcui, event_date, day_part)` | **Deviates — [F4](#f4)** |
| `neurorx.gold.patients` | Gold | `patient_id` | Mirror — [F7](#f7) |
| `neurorx.gold.schedules` | Gold | `schedule_id` | Mirror — [F7](#f7) |
| `neurorx.gold.dose_events` | Gold | `event_id` | Mirror — [F7](#f7) |
| `patients` | Lakebase | `patient_id` | Frozen |
| `schedules` | Lakebase | `schedule_id` | Frozen |
| `dose_events` | Lakebase | `event_id` | Frozen |
| `guardrail_blocks` | Lakebase | `block_id` | **Contested home — [F2](#f2)** |
