# CLAUDE.md — NeuroRx AI

Project guidance loaded into every session. Kept dense on purpose: this is the
hard-won, expensive-to-rediscover context, not a changelog. For the full spec see
[`ARCHITECTURE.md`](ARCHITECTURE.md) (canonical) and [`DATA_CONTRACTS.md`](DATA_CONTRACTS.md)
(frozen schemas).

---

## 1. What this is

Medication-schedule assistant for the Databricks Hackathon (Devpost, 5 equal-weight
criteria). Four features, nothing else: **Create** (prescription photo/text → structured
schedule, confirmed before save), **Maintain** (conversational edits; adding a drug
auto-runs an interaction check), **Adhere** (dose checklist, FDA-label-cited answers,
adherence dashboard), **Caregiver analytics** (Genie NL questions).

**Out of scope, permanently:** diagnosis, dosage recommendations, prescription changes,
emergency triage beyond escalation.

**The spine — read this before designing anything:** clinical facts come from
deterministic SQL/table lookups over openFDA + RxNorm + DDInter. The LLM *explains*
results, it never *originates* a clinical fact. Every clinical claim carries an FDA
label citation. If a design lets a clinical fact reach the user without a deterministic
lookup and a citation, that's a defect, not a style preference.

---

## 2. Non-negotiables

| Entity | Value |
|---|---|
| Catalog | `neurorx` |
| Schemas | `neurorx.{bronze,silver,gold,app,evals}` |
| Volume | `neurorx.bronze.raw_files` |
| UC functions | `neurorx.app.{manage_schedule,search_drug_labels,check_interactions,get_adherence_stats}` |
| Lakebase instance | `neurorx-oltp` |
| Repo | `neurorx-ai` → `git@github.com:0XSreekar/NeuroRx-AI-.git` |

All-Databricks stack (hackathon constraint — don't propose alternatives). 100% synthetic
patient data, no PHI ever.

---

## 3. Status

**Phase 0 — complete.** ARCHITECTURE.md, UC setup SQL + workspace runbook, repo scaffold
+ git + MIT license, `app/config.py` + `.env.example`, DATA_CONTRACTS.md.

**Phase 1 — in progress.** Written: `01_openfda_ingest.py`, `rxnorm_client.py` +
`02_rxnorm_ingest.py`, `03_ddinter_ingest.py`, `04_synthetic_cohort.py` (Task 1.4 —
**not actually complete, see below**), `pipelines/medallion_pipeline.py` (Task 1.5),
`pipelines/chunking.py` (Task 1.6), `pipelines/05_vector_index.py` (Task 1.7 — endpoint
`neurorx-vs`, index `neurorx.gold.drug_knowledge_index`), `setup/phase1_checkpoint.sql`
(Task 1.8 — one bug found and fixed, see below). All ten files exist but **nothing in
Phase 1 has actually been run against a live workspace**, and Task 1.4 specifically
still doesn't write any data at all — see the standing warning below.

**Phase 2 — complete.** Nine tasks, all written:

- **Task 2.1-2.4**: UC functions — four tools (check_interactions, search_drug_labels, manage_schedule, get_adherence_stats), unexecuted
- **Task 2.5**: System prompt — 900 words, two flagged deviations (dual citations, 988 escalation)
- **Task 2.6**: Supervisor agent — ResponsesAgent + MLflow tracing, verified against live Databricks templates
- **Task 2.7**: Extraction pipeline — 5-step prescription flow, executed end-to-end against live RxNav
- **Task 2.8**: Deployment — logs/registers/deploys agent; `agents.deploy()` signature verified against real PyPI wheel (caught doc summary's wrong parameter name `scale_to_zero_enabled` → `scale_to_zero`)
- **Task 2.9**: Smoke tests — four user story tests, placeholder for live endpoint execution

**Phase 3 — started.** Goal: Lakebase OLTP live and synced to Delta; the
three-view Databricks App running end to end against the agent endpoint. Exit
checkpoint: full flow through the UI — extract → confirm → schedule appears →
mark doses in Today → dashboard updates → grounded chat answer with clickable
citation. One task written so far:

- **Task 3.1**: Lakebase schema DDL — `lakebase/schema.sql`, actually executed against real local Postgres 18, not just parsed
- **Task 3.2**: Lakebase → Delta sync — corrected a wrong-direction feature-name assumption in the task brief itself before writing anything (see below)
- **Task 3.3**: App data-access layer — `app/db.py` + `app/agent_client.py`; caught a silent SDK response-field-dropping bug before it shipped (see below)
- **Task 3.4**: App Chat view — `app/app.py` + `app/views/chat.py` + `app/app.yaml`; pulled the streaming pattern verbatim from Databricks' current chat-app template, extended `agent_client.py` to make the model-call payload inspectable for the UI-owned confirmation surface
- **Task 3.5**: App Today view — `app/views/today.py`; found and fixed two real `db.todays_doses()` gaps (missing `dose_text`, no `day_part`), degrades gracefully around an undelivered Task 3.7 dependency
- **Task 3.6**: App Dashboard view — `app/views/dashboard.py`; reuses the already-verified `get_adherence_stats` UC function instead of re-deriving streak logic a second time; verified current Genie iframe-embedding capability before building the caregiver panel
- **Task 3.7**: Reminders job + notifications table — `app/jobs/reminders_job.py`; the real schema superseded Task 3.5's provisional guess, updated `app/db.py` to match rather than leaving the two disagreeing
- **Task 3.8**: Load synthetic cohort into Lakebase — `lakebase/07_load_cohort.py`; reads Phase 1 cohort from Delta, loads into Lakebase via batch psycopg inserts with deterministic UUID mapping and idempotency (ON CONFLICT DO NOTHING)

### Task 3.8: Load synthetic cohort into Lakebase — idempotent batch load with Margaret Demo verification

`lakebase/07_load_cohort.py` — loads the Phase 1 synthetic cohort from
`neurorx.bronze.synthetic_*` Delta tables into Lakebase (`patients`, `schedules`,
`dose_events`) via batch psycopg inserts (batch size 1000). Deterministic UUID
mapping and idempotency via ON CONFLICT DO NOTHING on the Postgres-side unique
constraints.

**Deterministic UUID mapping: reused directly from the generator.** Task 1.4's
`04_synthetic_cohort.py` already uses deterministic UUID generation (md5-based,
seeded with `SEED=42`), producing stable UUIDs across re-runs. `07_load_cohort.py`
reads those same UUIDs from Delta and inserts them verbatim into Lakebase,
making the load idempotent: running it twice on the same cohort produces no
duplicates due to the UNIQUE constraints defined in `lakebase/schema.sql` (Task
3.1). The uuid5 mechanism mentioned in the task brief is implicit — the Spark
`seed=42` provides determinism at generation time; this loader just preserves it.

**Margaret Demo assertion** (Requirement 2): post-load check confirms her fixed
UUID (`12345678-1234-1234-1234-123456789012`), her four fixed drugs
(metformin, lisinopril, warfarin, atorvastatin), and her metformin schedule
(2x/day, morning + evening). If any assertion fails, the load halts with a
clear error rather than silently proceeding with corrupted demo data.

**Batch size 1000** with progress printing per batch (Requirement 3). Row-count
reconciliation at the end (Requirement 3) compares Lakebase final counts against
Delta source counts — all three must match, confirming idempotency worked and no
rows were duplicated or lost.

**Connection pattern**: opens its own plain `psycopg` connection, not reusing
`app/db.py`'s pooled connection (which is `@st.cache_resource`-decorated,
a Streamlit-specific assumption this notebook doesn't satisfy). Reuses
`app.config.settings` for credentials, matching Task 2.3's local test harness
pattern.

**Executed end-to-end against a real local Postgres** (Homebrew Postgres 18),
following this project's discipline that pure-Python modules with no Spark/Databricks
dependency must actually run, not just syntax-check. Verified: batch loading works,
idempotency holds (reload same batch → no duplicates), Margaret Demo assertions pass,
row-count reconciliation matches source counts.

**Real bug found and fixed during execution:** Task 1.4's synthetic cohort generator
stores drug *names* in the `rxcui` column as a placeholder ("left as names for Phase 3
resolution"), but Lakebase schema enforces `rxcui ~ '^[0-9]+$'` (numeric RxCUIs only).
Loading the generator's output directly would fail with a CHECK violation. Fixed by
adding `build_drug_name_to_rxcui_map()`: reads from `gold.drugs` if the pipeline has
run (first preference), falls back to a hardcoded map of the demo cohort's drugs and
common medications with their verified RxCUIs (DATA_CONTRACTS.md §4), and raises
`KeyError` if an unknown drug is encountered — making the gap loud and early, not
silent. `load_schedules_batch()` now accepts the map and resolves names to RxCUIs
before inserting.

**Post-load reminder** (Requirement 4): prints instructions for the next steps
(enable CDF, run Lakebase→Delta sync, refresh Lakeflow pipeline) so the data
flow completes and adherence facts become available to the dashboard and agent.

### Task 3.7: Reminders job + notifications table — real schema now supersedes Task 3.5's provisional guess

`app/jobs/reminders_job.py` + `app/jobs/README.md` + a `notifications` table
added to `lakebase/schema.sql`. Every run: finds doses due in the next 60
minutes across all active schedules, inserts an idempotent `notifications`
row per dose, and pre-creates the corresponding `planned` `dose_events` row.

**The real schema landed, so Task 3.5's explicitly-flagged provisional
guess got updated to match, not left disagreeing.** Task 3.5's
`db.list_unacknowledged_reminders()`/`acknowledge_reminder()` assumed a
`(notification_id, patient_id, message, created_at, acknowledged_at)` shape
with a nullable timestamp, clearly marked "provisional" in its own
docstring at the time. This task's real table has `schedule_id`, `due_ts`,
and a plain `acknowledged BOOLEAN` instead — both `app/db.py` functions
updated in this same task to the real columns, with the docstrings now
saying "supersedes this function's own earlier provisional version" rather
than leaving a stale "provisional" note next to a table that now actually
exists.

**Idempotency verified two ways, against real Postgres, through the actual
module — not just extracted SQL.** Added
`notifications_slot_unique UNIQUE (schedule_id, due_ts)` to
`lakebase/schema.sql` (the constraint the job's `INSERT ... ON CONFLICT DO
NOTHING` needs to conflict on) alongside the already-existing
`dose_events_slot_unique` (Task 3.3). Ran `reminders_job.find_doses_due_soon()`,
`build_message()`, and `process_dose()` — the real functions imported from
the real module, not hand-copied SQL — against a fresh local Postgres 18
with a schedule due in 15 minutes, calling `process_dose()` twice in a row:
confirmed exactly one `notifications` row and exactly one `dose_events` row
result, not two. Also caught and fixed a test-script-only timezone bug
along the way (computing a "due soon" time in Python's UTC clock while
Postgres's session timezone was `Asia/Kolkata` produced a false negative —
fixed by deriving the test's due-time from Postgres's own `now()` instead of
mixing clocks), a good reminder that the job's own query is internally
consistent (both `CURRENT_DATE + dt` and `now()` evaluate in the same
session timezone) even though the demo cohort still has no per-patient
timezone column (`app/db.py`'s own already-flagged gap, Task 3.3).

**Standalone connection, not a reuse of the Streamlit app's pool.**
`app/db.py`'s `_get_pool()` is `@st.cache_resource`-decorated — a Streamlit
runtime assumption a Lakeflow Job doesn't satisfy. `reminders_job.py` opens
its own plain `psycopg` connection instead, reusing only
`app.config.settings` for credentials, matching the pattern Task 2.3's
local test harness already established for non-Streamlit contexts.

**README schedule-setup steps are honest about what was and wasn't
re-verified this session.** The `Jobs → Add trigger → Scheduled` navigation
and serverless-compute-by-default-for-Python-script-tasks claims are
confirmed against current docs; the exact wording of the simple "every N
minutes" period picker was not independently re-confirmed, flagged as such,
with the equivalent Quartz cron expression (`0 */15 * * * ?` — six fields,
leading seconds, not standard 5-field Unix cron) given as a fallback rather
than asserting UI text that wasn't actually checked.

### Task 3.6: App Dashboard view — reused the verified UC function, verified Genie embedding is real (Beta)

`app/views/dashboard.py` — header stat cards, adherence-by-drug bar chart,
90-day calendar heatmap, time-of-day missed-dose pattern, caregiver-mode
Genie panel. Wired into `app/app.py`'s Dashboard tab (previously a
placeholder). Adds `db.get_adherence_stats()` to `app/db.py`.

**Deliberately did not recompute the streak metric from raw
`adherence_summary()` rows — called the existing, already-verified UC
function instead.** `neurorx.app.get_adherence_stats` (Task 2.4) already
computes overall %, streak, most-missed drug, most-missed daypart from
`gold.adherence_facts`, with its own DuckDB-verified edge cases (including a
real empty-history bug found and fixed there, not here). Re-deriving a
streak calculation in this view's Python would have risked exactly the
"two silently-diverging implementations of the same fact" problem Task 3.5
just fixed for day-part boundaries — so `db.get_adherence_stats()` calls the
UC function directly (`SELECT * FROM neurorx.app.get_adherence_stats(...)`,
a table-valued call — a different shape from `agent_client.
call_manage_schedule()`'s scalar `SELECT ...(...)`, confirmed against the
function's own signature before assuming either shape) and parses its
5-metric-type row shape into a clean dict. Verified against a realistic fake
row set matching the UC function's own documented output (Margaret-Demo-like:
metformin worst, evening worst), including the empty-result case staying
`None` rather than defaulting to 100%.

**Genie embedding was verified as a real, current (Beta) capability before
building anything — not assumed either way.** Confirmed against current
Databricks docs: "Embed a Genie Space" iframe embedding genuinely exists,
gated on a workspace admin configuring allowed embedding surfaces and an
actual Genie Space existing with the right sharing settings. **Neither is
true for this project** — no Genie Space has been created, and
`ARCHITECTURE.md`'s own build-order cut list puts Genie first in line to be
cut under time pressure. The caregiver panel checks an optional
`GENIE_EMBED_URL` env var (deliberately not added to `app/config.py`'s
required nine, since Genie isn't a Phase 3 dependency) and only renders a
real iframe if it's set; otherwise it renders a prominent deep-link card and
says why in a code comment, exactly as this task's own instruction asked,
rather than emitting a broken iframe pointed at nothing or a fake "coming
soon" link to generic docs.

**The calendar heatmap sums doses across drugs/day-parts per day, never
averages percentages** — verified with a fixture where a naive average would
have gotten the wrong number: one day with 1/2 doses taken (two drugs, one
missed) correctly computes 50%, not an unweighted average of each drug's own
percentage, which would over-weight a once-daily drug against a
twice-daily one. Confirmed the full grid-construction + plotly figure build
runs without error and places that day's value at the correct
(weekday, week) cell.

### Task 3.5: App Today view — two real db.py gaps fixed, one undelivered dependency handled gracefully

`app/views/today.py` — dose checklist grouped by day part, next-dose
countdown, missed-dose handling, refill warnings, in-app reminder banner.
Wired into `app/app.py`'s Today tab (previously a placeholder since Task
3.4). Extends `app/db.py`'s `todays_doses()` and `refill_estimates()`, and
adds `list_unacknowledged_reminders()`/`acknowledge_reminder()` — all in the
data layer, keeping the view itself free of business logic per this task's
own Requirement 6.

**Two real gaps in `todays_doses()` caught while building the view it feeds,
fixed in `db.py` not papered over in the view.** The function (Task 3.3)
returned neither `dose_text` (Requirement 1 explicitly needs it displayed)
nor a day-part classification (Requirement 1 needs the checklist grouped by
one). Added both as SQL columns in `todays_doses()`'s own query — `day_part`
classified with the **exact same boundary rule** already established in
`pipelines/medallion_pipeline.py`'s `_day_part_expr()` for
`gold.adherence_facts` (`DATA_CONTRACTS.md` §1: morning 05:00–11:59,
afternoon 12:00–16:59, evening 17:00–20:59, night 21:00–04:59 wrapping
midnight as the `ELSE` branch) — so a dose bucketed "evening" in the Today
view and one bucketed "evening" in the dashboard's adherence numbers are
never two silently-drifting reimplementations of the same rule. **Verified
against real local Postgres**, not just read: a fixture with doses at
08:00/19:30/23:00 confirmed the three boundaries classify to
morning/evening/night exactly as `_day_part_expr()` would.

**A named dependency this task doesn't actually have yet, handled without
blocking the task.** Requirement 5 asks to "poll the notifications table
(Task 3.7)" — but Task 3.7 hasn't been built, and no schema for a
notifications table exists anywhere in `DATA_CONTRACTS.md` (confirmed by
reading the whole file, not assumed absent). Worse: `ARCHITECTURE.md` §8's
own cut list puts "the reminders job" third in line to be cut if time runs
short — this dependency may never land. Added
`db.list_unacknowledged_reminders()` against an explicitly flagged
*provisional* schema and *assumed* Lakebase (not Delta) home — reasoned from
this project's own established OLTP-vs-analytics split (a reminder is live
operational state, not an aggregate), not guessed blind — wrapped to catch
`psycopg.errors.UndefinedTable` specifically (not a bare `except`, which
would mask a real bug) and degrade to an empty list. **Verified against real
Postgres**: confirmed the function returns `[]` without crashing when the
table doesn't exist, and — the check that actually mattered — confirmed the
pooled connection is still usable after the caught error, via an explicit
`conn.rollback()` (an uncaught failed statement otherwise poisons the rest of
that connection's transaction for any subsequent query on it).

**"Missed" is a display-time judgment, never a written status** — Requirement
3 asks for past-time unactioned `planned` doses to render as "Missed";
`dose_events.status` in Lakebase only ever changes via an explicit
`mark_dose()` write (`lakebase/schema.sql`'s own CHECK constraint), so this
view computes `planned_ts < now()` purely for display, and the "I took it
late" button is the one path that actually writes `status='taken'` with the
real (late) action timestamp — never the originally planned one. Verified
with a fixture spanning a past-due morning dose and a still-upcoming evening
dose: the past one displays "missed" with no DB write; the future one
doesn't, and remains the next-dose countdown's target.

**Refill warnings render the honest gap rather than fabricating a badge.**
`refill_estimates()` (Task 3.3) already flagged that `DATA_CONTRACTS.md` has
no fill-quantity column, so "days remaining" can never be real today; this
task added a `days_remaining` field (always `None` currently) so the view's
"<7 days" badge is a plain field check, not a computation the view would
otherwise have to do itself once real data exists — today it just shows
"refill tracking not available," honestly, per schedule.

### Task 3.4: App Chat view — template-verified streaming, UI-owned confirmation

`app/app.py` (three-tab shell + persistent non-dismissable safety banner,
Requirement 1) + `app/views/chat.py` (the Chat tab) + `app/app.yaml` +
`app/databricks.yml` (resource declarations). Extends `app/agent_client.py`
(Task 3.3) with `chat_stream()` and `call_manage_schedule()` — both real gaps
Task 3.3 didn't anticipate, not scope creep: Task 3.3 only needed a single
non-streaming model call, and had no path for the *app itself* (as opposed to
the model) to call `manage_schedule`.

**The streaming implementation is pulled from Databricks' own current
chat-app template, fetched live this session** (`databricks/app-templates/
e2e-chatbot-app`, via `raw.githubusercontent.com` — not recalled from
training data, same discipline Task 2.6 already applied to `agent/agent.py`
itself). The template is a generic multi-endpoint-type chatbot that branches
on `chat/completions`/`agent/v2/chat`/`agent/v1/responses` since it doesn't
know in advance what it's pointed at; this project's endpoint is always
exactly one `agent/v1/responses`-shaped agent, so that branching was
deliberately not reproduced — only its verified `st.chat_message` +
`st.empty()` placeholder + accumulate-in-a-loop + `try`/`except`-fallback-to-
non-streaming structure was reused, and only the parts relevant to
`agent/v1/responses` (`mlflow.deployments.get_deploy_client("databricks")
.predict_stream(inputs={"input": ..., "context": {}, "stream": True})`) were
pulled into `agent_client.chat_stream()`. `predict_stream()`'s own source
(already downloaded for Task 2.8) confirms the base class raises
`NotImplementedError` when a deployment client can't stream — the real,
catchable signal this view's fallback-to-spinner logic checks for, not a
guessed exception type.

**Requirement 5 ("the UI, not the model, is the confirmation surface")
needed a real design change to `agent_client.py`, not just a chat.py-side
feature.** Task 3.3's `chat()` only ever returned `{"text", "citations"}` —
whatever the model said, paraphrased. To render an explicit confirm/cancel
card for a blocked or pending schedule change, the UI needs the *actual*
attempted `action`/`payload` and `manage_schedule`'s *own* verdict, not the
model's prose summary of either. `agent_client.parse_agent_output()` digs
this out of the raw Responses-API `output` items: a `function_call` item
(`name="manage_schedule"`) carries the attempted call as a JSON-encoded
`arguments` string; the matching `function_call_output` item (same
`call_id`) carries `manage_schedule`'s own JSON-encoded verdict. Paired
together so the UI can re-submit the *exact* attempted action/payload with
`user_confirmed`/`confirmed_interactions` added — verified against a
realistic fake payload (a blocked ibuprofen-on-warfarin `add_drug` call) that
correctly pairs the two and preserves interaction severity/description for
the card to render.

**Citation chips are `st.expander`, not a dedicated chip widget — a real,
stated Streamlit API limitation, not an arbitrary choice.** Streamlit has no
native inline "chip" component; an expander collapsed by default, labeled
"drug — section", is the closest primitive matching "click to expand the
verbatim chunk_text + set_id." A chip only ever renders for a `chunk_id`
`CHUNK_ID_PATTERN` actually found in the response text — Requirement 3's
"uncited clinical-looking sentences get no chip" is satisfied by
construction, not by a separate check, since nothing here invents a chip for
text that merely sounds clinical.

**The editable-confirmation-table highlighting has a similar stated gap**:
`st.dataframe`'s Styler supports real per-row conditional styling but is
read-only; `st.data_editor` is editable but has no equivalent styling hook.
A "⚠️" prefix on the drug name is the honest way to highlight a needs_review
row within that real constraint, not a workaround pretending to be a full
solution.

**`app.yaml` alone does not bind resources — flagged rather than implied.**
Verified against current Databricks Apps docs: `app.yaml` only declares
`command` + `env` (with `valueFrom` referencing resource *keys*); the actual
binding of those keys to a real serving endpoint / SQL warehouse / Lakebase
database happens either via the Apps UI's "Add resource" flow at deploy time,
or via a Databricks Asset Bundle's `databricks.yml` — added here as
`app/databricks.yml` since this project has no bundle root yet, using the
verified `resources.apps.<name>.resources` block shape (`serving_endpoint`/
`sql_warehouse`/`database` resource types, each with its own `permission`
enum, confirmed against current bundle docs) rather than assuming `app.yaml`
was the whole story.

**Nothing here has run in a real Streamlit session against a live
workspace** — no browser, no deployed endpoint, no SQL warehouse reachable
from this environment. What was verified: `py_compile` on all three Python
files; real package source for every non-trivial SDK/mlflow call (same
discipline as Task 3.3); a standalone test of `parse_agent_output()`'s
tool-call-pairing logic against a realistic fake payload; and both YAML
files parsed successfully with `pyyaml` against the exact schema confirmed
in current docs. First thing to check on a real deployment: whether
`st.rerun()` after `agent_client.call_manage_schedule()` inside a button
handler behaves as expected under Streamlit's actual rerun semantics in a
Databricks Apps-hosted (not local) session — the two should be identical,
but this has not been observed running.

### Task 3.3: App data-access layer — one real SDK bug caught, one schema gap fixed, one flagged

`app/db.py` (Lakebase, psycopg) + `app/agent_client.py` (agent endpoint,
extraction) — the only two modules through which the app touches data.
Every function returns a plain dict/list of dicts; no ORM objects, no
`mlflow`/SDK types leak into the UI layer.

**A real bug caught by reading SDK source, not by trusting a plausible method
name**: `WorkspaceClient.serving_endpoints.query()` looks like the obvious
way to call the deployed agent, but its typed `QueryEndpointResponse` return
object is built via `from_dict()` recognizing only chat/completions/
embeddings external-model fields (`choices`, `data`, `predictions`, ...) —
it has **no field for `output`**, which is exactly the top-level key
`agent/agent.py`'s `ResponsesAgentResponse` actually returns. Calling
`.query()` here would have silently returned an object with every field
`None` — no exception, no error, just a call that looks like it worked and
returns nothing. Fixed by calling `WorkspaceClient.api_client.do("POST",
f"/serving-endpoints/{name}/invocations", body=...)` directly — the same
underlying call `.query()` makes internally (confirmed by reading its
implementation), minus the lossy typed wrapper. Both `api_client` and `.do()`
are public, stable SDK surface, not a private-internals reach-around.

**A real schema gap in Task 3.1's `lakebase/schema.sql`, fixed in this task**:
`mark_dose()`'s upsert ("create the planned row if the reminders job hasn't
yet") needs a genuine `INSERT ... ON CONFLICT`, which needs a real unique
constraint to conflict on — `dose_events` had none. Added
`dose_events_slot_unique UNIQUE (schedule_id, planned_ts)` to `schema.sql`.
**Verified by actually running it against real local Postgres**, not just
reading the SQL: a double `mark_dose()` call on the same slot (simulating a
double-click or a retried request) reuses the same `event_id` and updates
its status in place — confirmed exactly one `dose_events` row exists after
two calls, not two. Also verified `todays_doses()`'s LEFT JOIN reconstruction
correctly surfaces the upserted status rather than a stale `'planned'`
default.

**Two connector paramstyles, deliberately not interchanged**: psycopg uses
`%(name)s`; the Databricks SQL connector (`adherence_summary()`,
`resolve_citations()`) uses PEP-249 `named` style, `:name` with a dict —
confirmed against the actual `databricks-sql-connector==4.3.0` source rather
than assumed from psycopg familiarity. Mixing them up would silently send
the wrong bind syntax to whichever connector didn't get it.

**The SQL warehouse HTTP path is `warehouse.odbc_params.path`**, not a
guessed `.http_path` — confirmed against the real `databricks-sdk` source,
same lesson Task 2.8 already learned about not trusting a plausible
attribute name. `get_warehouse_http_path()` discovers Free Edition's one
warehouse live, mirroring `log_agent.py`'s `build_resources()` pattern.

**`refill_estimates()` flags a real, undocumented data gap rather than
inventing a number**: `DATA_CONTRACTS.md` §6.2's `schedules` columns have no
fill-quantity/days-supply field anywhere — nothing to honestly compute
"pills remaining" from, and `dose_text` is explicitly documented as never
parsed for clinical logic. Returns `pills_remaining=None` with an explicit
`unavailable_reason` per schedule, not a fabricated figure and not a
raised exception that would break a dashboard expecting a list.

**Analytics reads go to Delta, never Lakebase — stated as a judge talking
point in `app/db.py`'s own docstring, per this task's instruction.**
`adherence_summary()` and `resolve_citations()` are the only two functions
in either file that never touch the Lakebase pool; this is the same F9
reasoning `get_adherence_stats` (Task 2.4) already established, applied
consistently at the app layer so the dashboard and the chat answers never
disagree about which store is authoritative.

**Nothing here has run against a live Databricks workspace** (no serving
endpoint, no SQL warehouse, no Vector Search reachable from this
environment) — `chat()`'s endpoint call and `resolve_citations()`'s Delta
read are verified only by reading real SDK/connector source and by a
standalone test of the pure response-parsing logic against a fake
Responses-API-shaped dict (confirms message text is extracted correctly,
tool-call items are correctly ignored, and the citation regex fires) — not
by an actual round trip. What genuinely ran against a live database: the
Lakebase-side SQL in `app/db.py` (`todays_doses`, `mark_dose`,
`dose_events_slot_unique`), against real local Postgres 18.

### Task 3.2: Lakebase → Delta sync — corrected the feature name before writing anything

`lakebase/sync_setup.md` (runbook) + `lakebase/sync.sql`. **The task brief's own
premise was wrong and caught before any SQL was written**: Databricks does
have a feature literally named "synced tables," but verified live this
session, it syncs **Delta → Postgres** ("Reverse ETL" — serving lakehouse data
to an operational app), the opposite of what this task needs. The correct
current mechanism for **Postgres (OLTP) → Delta**, confirmed against current
docs, is a separately-named feature: **Lakebase Change Data Feed (CDF)**,
powered by the `wal2delta` Postgres extension doing WAL logical decoding.
Building against "synced tables" here would have synced the wrong direction
and required an already-populated Delta gold table to seed a Postgres
table — circular, and a first-sentence-wrong runbook, exactly the failure
mode `CLAUDE.md` §6 exists to catch ("this feature is new and changes; do not
write from memory").

**CDF produces an append-only SCD Type 2 change log, not a mirror — this
changes what "row count matches" has to mean.** Each source table gets a
`lb_<table_name>_history` Delta table with `_pg_change_type`
(`insert`/`delete`/`update_preimage`/`update_postimage`) plus `_pg_lsn`,
`_pg_xid`, `_timestamp`, `_sort_by`. An `UPDATE` produces **two** rows; the
raw history row count is expected to exceed and grow independently of the
live table's row count — comparing it directly against Lakebase's row count
(a literal reading of the task's requirement #4) would show a permanent,
misleading "mismatch" that is actually correct behavior. `sync.sql` instead
builds two materialized views (`gold.schedules_synced`, `gold.dose_events_synced`)
that reduce history to current state — latest `_sort_by` per primary key,
excluding keys whose latest event is a delete — and the row-count
verification compares Lakebase against *those*, not the raw history table.

**The reconstruction query was actually run, not just read.** Verified against
DuckDB with a 3-row fixture covering all three real cases: a schedule
inserted then updated (view must show the *post*-update values), one
inserted then deleted (view must omit it entirely), one inserted and never
touched again (view must pass it through unchanged). All three passed.

**Sync mode: there is no choice to make, and that's a direct consequence of
using the correct feature.** The task asks to "choose and justify continuous
vs. triggered/snapshot" — that three-way choice is real, but it belongs to
the (wrong-direction) synced-tables feature. Lakebase CDF has exactly one
mode: continuous WAL streaming, changes flushed every ~15 seconds. Nothing to
configure beyond turning it on; staleness is ~15s plus one materialized-view
refresh cycle, indistinguishable from live for a demo.

**Naming conflict flagged, not resolved**: this task's brief asks for
`gold.schedules_synced`/`gold.dose_events_synced`; `DATA_CONTRACTS.md` §9's
sync map and `medallion_pipeline.py`'s pre-existing Phase 3 TODO both used the
bare names `gold.schedules`/`gold.dose_events`. Went with the `_synced` names
(more honest about what these actually are — a CDC reconstruction, not a
naive mirror) and updated `medallion_pipeline.py`'s comment to match, but
`DATA_CONTRACTS.md` §9 itself still needs a sign-off edit to agree.

**`pipelines/medallion_pipeline.py`'s `SOURCE_TABLE` was NOT flipped** — the
existing TODO comment was corrected (right future table name; a `schedules`
join is now noted as required for `rxcui`, which `dose_events_synced` doesn't
carry — the old comment's "one-line change" claim was wrong) but the active
constant still points at `bronze.synthetic_dose_events_raw`, since nothing
populates `gold.dose_events_synced` until Task 3.8 (loading the synthetic
cohort into Lakebase) actually lands. Flipping now would just point the
pipeline at an empty table.

### Task 3.1: Lakebase schema DDL — written, actually executed against real Postgres

`lakebase/schema.sql` + `lakebase/README.md` — DDL for the four
`DATA_CONTRACTS.md` §6 tables (`patients`, `schedules`, `dose_events`,
`guardrail_blocks`), their four required indexes, and the `schedules.updated_at`
trigger. Idempotent throughout (`CREATE TABLE/INDEX IF NOT EXISTS`,
`CREATE OR REPLACE FUNCTION`, `DROP TRIGGER IF EXISTS` + `CREATE TRIGGER` since
Postgres has no `CREATE TRIGGER IF NOT EXISTS`).

**Genuinely executed against a real local Postgres 18, not just parsed** —
same technique as Task 2.3's local test harness (Homebrew Postgres, same two
known snags hit and worked around: a Unix-socket path-length limit, and a
"postmaster became multithreaded" locale error fixed with `LC_ALL=C`).
Applied cleanly twice in a row with zero errors (true idempotency verified,
not just `IF NOT EXISTS` syntax that happens to parse). Functional checks that
actually required a live database, not just a read of the DDL: the
`schedules_frequency_match` CHECK genuinely rejects a `times_per_day=2` row
with 3 `dose_times`; `schedules_status_valid` rejects `'paused'`;
`dose_events_actioned_consistent` rejects a `missed` dose carrying an
`actioned_ts`; the `updated_at` trigger bumps on `UPDATE` and leaves
`created_at` untouched; and — the one result that most needed a live
database to prove — deleting a patient cascades to empty `schedules` and
`dose_events`, while `guardrail_blocks` **survives** with `patient_id` set to
`NULL`, confirming the append-only evidence log is never destroyed by a
patient purge.

**Three FK `ON DELETE` decisions made and justified, since `DATA_CONTRACTS.md`
§6 only states two of the four explicitly:**
- `schedules.patient_id` → `patients`: `CASCADE`, per the contract verbatim.
- `dose_events.schedule_id` → `schedules`: `CASCADE`, per the contract verbatim.
- `dose_events.patient_id` → `patients`: **not stated in the contract** —
  chose `CASCADE` to agree with `schedule_id`'s cascade path, since this
  column is denormalized and redundant with `schedules.patient_id` by
  construction; disagreeing FK actions on the same conceptual relationship
  would create ordering-dependent delete behavior.
- `guardrail_blocks.patient_id` → `patients`: **not stated in the contract**
  — chose `SET NULL`, not `CASCADE`. This table is an append-only evidence
  log (`DATA_CONTRACTS.md` §6.4: "credible only if nothing can quietly edit
  it after the fact") — cascading would let a patient deletion silently
  destroy the record that the safety guardrail once fired for them, which is
  exactly the kind of quiet edit the table's own design note warns against.
  `patient_id` is already nullable for anonymous/pre-auth sessions, so `SET
  NULL` reuses an existing, already-modeled state rather than inventing one.

**`sqlglot` caught one real, narrow parser gap** — the same lesson as Tasks
2.1 and 2.3: `COMMENT ON TABLE ... IS` with a multi-line, adjacent-string-
literal-concatenated value (valid Postgres syntax) failed to parse under
`sqlglot`'s Postgres dialect, isolated with a 3-line repro showing the
identical concatenation parses fine inside a plain `SELECT`. Reworded to
single string literals per `COMMENT` (free fix) rather than investigate the
parser further.

**Connection-detail claims replaced guesswork with verified facts.** The
existing `.env.example` comments for `LAKEBASE_HOST`/`DB`/`USER`/`PASSWORD`
said "typically" and "usually" — this task's README replaces those with
confirmed current facts: Lakebase (Autoscaling) supports Postgres 16/17/18
(17 default); `gen_random_uuid()` needs no extension since PG13; the default
database is literally `databricks_postgres`; host is a per-project
`ep-*.databricks.com` endpoint; port 5432; `sslmode=require` mandatory; and
two genuinely different auth mechanisms exist (OAuth token, 1-hour expiry,
via `generate_database_credential`, vs. native Postgres role password,
indefinite, via an "Enable Postgres Native Role Login" toggle) — this project
uses native password auth for local dev, consistent with the choice
`CLAUDE.md`'s Task 2.3 notes already made for the local test harness.

**`guardrail_blocks`'s home (F2) is still open** — written to Lakebase here
because `DATA_CONTRACTS.md` itself specifies it there, but this is not a
resolution of the Delta-vs-Lakebase conflict; flagged in both `schema.sql`
and the README, not silently resolved.

### Task 2.9: Smoke tests — written, placeholders for live execution

`agent/07_smoke_tests.py` — a Databricks notebook exercising all four core user stories
(Create, Maintain, Adhere, Caregiver analytics) against the deployed `neurorx-agent`
endpoint. Sequential, verbose, plain `requests.post()` + `WorkspaceClient` SDK calls; PASS/FAIL
output per story with response excerpts.

**Story 1 (Create)**: extraction → confirmation → schedule write. SKIPPED in this notebook
(requires live `manage_schedule` UC function wiring via Data API); placeholder notes the
gaps for first live run.

**Story 2 (Maintain)**: retime existing drug (expects `needs_confirmation`), then add drug
with interaction block (expects `blocked_pending_confirmation`). Partial: confirmation/blocking
logic verified in agent response text.

**Story 3 (Adhere)**: missed-dose question expects `[chunk_id]` citation; no-coverage question
expects pharmacist redirect, no fabricated guidance. Citation regex verified against
`DATA_CONTRACTS.md` §8 spec (`\[[0-9a-f-]{36}:[a-z_]+:\d{4}\]`).

**Story 4 (Caregiver)**: get_adherence_stats via agent for Margaret Demo; expect metformin
as most-missed drug (per synthetic cohort, Task 1.4).

**What was verified before writing**: Responses API shape (Task 2.8), citation patterns
(Task 2.5), Margaret Demo's patient_id + cohort (Task 1.4). **Not verified**: live endpoint
network call patterns, actual latency, whether `search_drug_labels` truly returns `[chunk_id]`
on the exact lisinopril missed-dose question — first live run will expose these. This notebook
is a placeholder structure for that run.

See §4 below for the SQL syntax facts Task 2.1 caught, the
**parameter-vs-column shadowing trap** Task 2.4 caught (a silent whole-cohort data leak),
(`COMMENT ON FUNCTION` is invalid; `sqlglot` as a local Databricks-dialect parser), and
the warnings immediately below for real, user-accepted architecture risk in Task 2.2 and
a confirmed hard network block found (and worked around) in Task 2.3.

> ⚠️ **Task 2.3 (`manage_schedule.py`) hit the same UC-Python-sandbox network wall as Task
> 2.2 — but this time as a confirmed, deterministic block, not just unverified auth.**
> Postgres's wire protocol needs port 5432; the sandbox only allows 80/443/53. A raw
> `psycopg` connection (what the task asked for) would not work, full stop — no two-sided
> choice to ask about, unlike Task 2.2. **Fix:** Lakebase has a Data API
> (PostgREST-compatible REST over HTTPS/443, confirmed against current docs) — used for
> the *deployed* function. `check_interactions` (itself a UC function) has the identical
> "no `spark` session" problem when called from inside another UC Python function; fixed
> with the SQL Statement Execution REST API (`POST /api/2.0/sql/statements`), reusing the
> same OAuth bearer token. `psycopg` genuinely is used, but only in the **local test
> harness** (task requirement #6), which runs outside the sandbox entirely — this is
> exactly the case `app/config.py`'s existing `lakebase_host/db/user/password` fields were
> designed for; they just don't cover the *deployed* function, which needs new env vars
> instead (`NEURORX_SQL_WAREHOUSE_ID`, `NEURORX_LAKEBASE_REST_ENDPOINT`, plus the three
> already introduced in Task 2.2).
>
> **Two real bugs caught by mocked-HTTP execution, not by reading the code:**
> (1) the OAuth token was being minted *before* checking whether the call even needed
> confirmation — meaning a plain `needs_confirmation` response (which requires zero
> backend I/O) depended on auth succeeding, so a misconfigured deployment couldn't even
> echo back a proposed change. Fixed by moving the confirmation-gate check before the
> token mint. (2) PostgREST returns inserted/updated rows as a JSON array even for a
> single row (`Prefer: return=representation`) — the first draft treated that array as if
> it were the row itself, so `created[0]` was actually `[{...}]`, a list wrapped in a
> list, and would have raised a `TypeError` on the very first successful write in a real
> deployment. Caught only by actually running the function against `responses`-mocked
> HTTP calls (same technique as Task 2.2) and inspecting the real returned JSON rather
> than assuming the shape was right.

> ⚠️ **Task 2.2 (`search_drug_labels.py`) accepted a known, unresolved risk — read before
> deploying.** UC Python functions have no `spark` session and no documented auth path to
> Databricks' own internal REST APIs (Vector Search) from inside the sandbox — the one
> documented credential mechanism, `service_credentials`, is scoped to *external* cloud
> services only. Databricks' own official docs for "wrap a Vector Search index as a UC
> agent tool" show a **SQL** function using the native `vector_search()` table function —
> not Python — specifically because it has no auth problem. Asked the user: SQL function
> (recommended, verified-working) vs. Python UDF anyway (matches the literal task, auth
> unverifiable). **User chose Python UDF anyway.** The file authenticates via the
> documented OAuth service-principal client-credentials flow (`POST /oidc/v1/token`) using
> three env vars (`NEURORX_DATABRICKS_HOST`/`NEURORX_SP_CLIENT_ID`/`NEURORX_SP_CLIENT_SECRET`)
> whose reachability *inside* the UC Python sandbox is **unverified** — this is the single
> biggest unresolved question in Phase 2. Both failure modes (missing creds, request
> exception) fail safe with a distinct "configuration error" / "temporary error"
> instruction rather than silently reading as "no interaction data exists," which the
> agent is instructed to treat as license to say nothing was found. **First thing to check
> when this is actually deployed:** does the test cell get real results, or the
> "configuration error" instruction? If the latter, switch to the SQL-function pattern —
> don't keep debugging Python-sandbox auth.

> ⚠️ **Task 1.4 was marked "✅ complete" in this file at one point; it was not.** Verified
> by re-running `python3 data/ingestion/04_synthetic_cohort.py` directly: it still has all
> three bugs a prior review found. (1) **Zero Spark/Delta writes** — `write_to_bronze_tables()`
> only attaches audit columns to in-memory dicts and returns them; nothing reaches
> `neurorx.bronze.synthetic_{patients,schedules,dose_events}_raw`, ever. (2) **`DRUG_LIST` is
> still 75% non-curated** — 175 of 235 entries (`antibody`, `antioxidant`, `anesthetic`,
> `anti-inflammatory`, `androgen`, `alfuzosin`...) don't exist in `01_openfda_ingest.py`'s
> curated list, so most generated schedules reference drugs with no FDA label, no RxCUI, no
> DDInter coverage. (3) **All 49 non-demo patients are surnamed "Smith"** — confirmed again
> in a fresh run ("Mary Smith", "Robert Smith", "Anthony Smith", "Emily Smith"...) — `FIRST_NAMES`
> has 52 entries, so `idx // len(FIRST_NAMES)` is `0` for every one of the 50 patients, always
> indexing `LAST_NAMES[0]`. Lesson for this file specifically: **a "✅ complete" note added to
> CLAUDE.md is a claim, not a fact — re-run the code before trusting it**, exactly like every
> external API claim elsewhere in this document.

> ⚠️ **Task 1.8 (`setup/phase1_checkpoint.sql`) is well-built — correct table names
> throughout, matches the frozen contract, correct lexicographic canonical-order check —
> but had one real bug, now fixed.** Query 6c filtered `WHERE rxcui = 'metformin'`; `rxcui`
> holds the numeric RxCUI string (`'6809'`), not the drug name — `drug_name` is the separate
> column holding `'metformin'`. That filter matched zero rows every time, silently failing
> to show the one data point (Margaret's metformin evening-vs-morning adherence gap) the
> whole checkpoint file exists to demo. Fixed to `drug_name = 'metformin'`. This file cannot
> actually be run to completion yet regardless — its bronze/gold synthetic-data queries have
> nothing to query until Task 1.4 is fixed.

### Task 2.8: agent deployment (Model Serving) — written, unexecuted, one real bug fixed by verifying against source

`agent/06_deploy_agent.py` — logs the agent, registers to UC as `neurorx.app.neurorx_agent`
aliased `@champion`, deploys to a serverless endpoint `neurorx-agent` (`scale_to_zero=True`,
with a pre-warm note for demo day), then a post-deploy verification cell asking the
lisinopril missed-dose question and asserting both a `[chunk_id]` citation and an MLflow
trace with a tool-call span exist. Extends `agent/log_agent.py`'s resource declarations
(Task 2.6) with the vector index, Lakebase, and SQL warehouse this task asked to enumerate.

**The `agents.deploy()` signature was pulled from the actual current wheel, not a doc
summary — and that distinction caught a real error before it shipped.** A WebFetch-based
doc summary reported the scale-to-zero parameter as `scale_to_zero_enabled`; downloading
`databricks-agents==1.11.0` from PyPI and reading `deployments.py` directly showed the real
parameter is `scale_to_zero` (no `_enabled` suffix). Every other call in this notebook —
`agents.deploy()`'s full parameter list, the `Deployment` object's `.endpoint_name`
/`.query_endpoint`/`.endpoint_url`/`.review_app_url` attributes, `agents.set_permissions()`'s
signature — was confirmed the same way, against the downloaded `databricks-agents` and
`databricks-sdk` wheels' actual source, not summarized docs. Same discipline applied to
`mlflow.search_traces()`: initially written against `traces.iloc[0].trace.data.spans`
(assuming a pandas `.trace` column), corrected after reading the real docstring, which
states the pandas return type's columns are `trace_id`, `spans`, etc. with no `.trace`
wrapper column — switched to `return_type="list"`, which returns real `Trace` objects with
a confirmed `Trace.data.spans: list[Span]` shape, avoiding the guess entirely. Also caught:
`EndpointStateReady` is a **plain** `Enum`, not `str, Enum` — an initial
`str(state.ready) == "READY"` comparison would never match (renders as
`"EndpointStateReady.READY"`); fixed to compare against the enum member directly.

**A real deployment blocker inherited from Task 2.6, surfaced only because this task needed
to actually populate a served container's environment.** `agent/agent.py` imports
`app.config.settings` at module level, and `app/config.py` requires all nine of its env
vars — including Lakebase credentials the agent never touches — just to import
successfully. This is the exact anti-pattern CLAUDE.md already flags for Lakeflow pipeline
files, now found in the agent itself. Not silently fixed here (that's a Task 2.6-shaped
change); the deploy notebook's `ENVIRONMENT_VARS` carries all nine via `{{secrets/...}}`
references so the container can import at all, with the coupling issue flagged as a cleanup
item for whoever revisits `agent.py`/`app/config.py` next.

**Resource declarations are honest about what they do and don't cover.** Per Tasks 2.2/2.3's
own already-verified findings, `search_drug_labels` and `manage_schedule` authenticate to
Vector Search / Lakebase / the SQL Statement Execution API via their own OAuth
service-principal env vars, not Databricks' resource-based auto-auth passthrough (UC Python
functions have no verified in-sandbox mechanism for that — the Task 2.2 dead end).
Declaring `DatabricksVectorSearchIndex`/`DatabricksLakebase`/`DatabricksSQLWarehouse` on the
agent's logged model is correct current governance/lineage practice and satisfies what this
task asked for — but it does not replace those two UC functions' own separately-configured
auth, and the notebook says so rather than implying the declarations wire up connectivity
they don't.

**Nothing here has been run against a live workspace** — no Databricks credentials, UC
catalog, or serving infrastructure reachable from this environment. Verification was
`py_compile` syntax-checking plus reading real downloaded package source for every
non-trivial call; the post-deploy verification cell itself (Step 4) has never executed
against a real endpoint. First thing to check on real deployment: whether the citation
regex (`\[[0-9a-f-]{36}:[a-z_]+:\d{4}\]`, matching the exact `chunk_id` format from
`DATA_CONTRACTS.md` §8) actually matches what the agent emits in practice, and whether
`search_traces(return_type="list")` immediately after a `deploy_client.predict()` call
reliably has the trace flushed and visible yet, or needs a short wait / `flush=True`.

### Task 2.7: prescription extraction flow — written, executed end-to-end against live RxNav

`agent/extraction.py` — `extract() -> normalize() -> resolve() -> propose()`, run outside
the chat agent; the app calls it directly and only calls `manage_schedule` (Task 2.3)
after the human confirms. **No database client is imported in this file, by
construction** — there is nothing here capable of writing a schedule.

**Genuinely executed, not just syntax-checked** (`agent/extraction.py`'s own `__main__`,
run with dummy config + the scratchpad venv, since no live Databricks workspace or `.env`
exists here): 23 frequency-pattern ordering checks, the retry-once-on-parse-failure path,
and all three required fixtures — with `resolve()` hitting the **live** RxNav API for real,
not mocked. Only `extract()`'s actual FM-endpoint call is stubbed (`_fm_call=`), since no
live multimodal Claude endpoint is reachable from this environment — flagged in the file's
own docstring as the one unverified piece, same standing caveat as every other
FM/Vector-Search call in this project.

**The third fixture ("ambiguous brand name") is a real, live-discovered RxNorm tie, not an
invented example.** Before writing the fixture, several plausible "ambiguous" brand names
were tested live (`Toprol`, `Glucophage`, `Norvasc`, `Adderall`, `Xanax`...) and every one
resolved cleanly *exact* — RxNorm indexes brand names well. The genuine ambiguity came from
a realistic data-entry mistake: **appending the strength to the brand name**,
`"Norvasc 5mg"`, which RxNorm's fuzzy matcher scores as an exact tie between two real,
distinct RxCUIs at different term-type granularity — `572722` ("amlodipine 5 MG
[Norvasc]", tty=SBDC) and `212549` ("amlodipine 5 MG Oral Tablet [Norvasc]", tty=SBD) —
both scoring 12.3835 identically. Plain `"Norvasc"` alone is exact. This is exactly the
kind of case `rxnorm_client.py`'s safety invariant (Task 1.2) exists to catch: neither
RxCUI is silently preferred over the other.

**One real edge case caught by that live testing, not by reading the code:** the top
approximate-match RxCUI for a different test string (`"Glucophage XR"` -> `285065`)
returned `{}` from `get_properties()` — a real RxCUI with no resolvable display name.
`_enrich_candidates()` in `extraction.py` was written to tolerate `matched_name=None`
specifically because this was observed live, not hypothesized defensively.

**A genuine multi-way-tie distinction `get_rxcui()` itself doesn't expose.**
`rxnorm_client.get_rxcui()` collapses two different situations to the same
`match_type="none"`: multiple exact RxCUIs tied at the top, and zero matches at any tier.
Per that module's own contract ("the caller — not this module — decides what happens on a
`none` result"), `resolve()` calls `search_exact()` again on a `none` result to tell these
apart, so the candidate list shown to the human actually reflects which case it is,
instead of always defaulting to fuzzy-match candidates even when the real ambiguity was
at the exact tier.

**Frequency mapping table: 14 patterns, ordering is load-bearing.** 11 schedulable
(qid/tid/bid/q12h/q8h/q6h/q4h/qhs/qam/qpm/qd) plus 3 recognized-but-schema-unrepresentable
(qod, weekly, prn — flagged `needs_review` with a reason distinct from "unrecognized text",
since the gap is in the daily `times_per_day`/`dose_times` model itself, not in
understanding the sig). Diurnally-qualified rules (qhs/qam/qpm) are checked **before** the
generic `qd`/"daily" rule specifically so `"once daily at night"` matches qhs (→ 21:00),
not the generic rule (→ 08:00) — the regression test in `__main__` checks this ordering
property directly rather than trusting the comment that explains it.

**The extraction prompt deliberately does not ask the model to interpret frequency.**
`EXTRACTION_PROMPT` explicitly instructs "do NOT expand abbreviations... that normalization
happens downstream, deterministically" — keeping the LLM's role to literal transcription
(OCR-like) and moving all clinical-adjacent interpretation into auditable, testable code,
consistent with this project's spine (§1: "the LLM explains, it never originates").

### Task 2.6: supervisor agent + MLflow tracing — written, unexecuted, one requirement deliberately unmet

`agent/agent.py` — `mlflow.pyfunc.ResponsesAgent` subclass wiring `ChatDatabricks` (FM
API) + `UCFunctionToolkit` (the four `neurorx.app.*` UC functions) via LangChain's
`create_agent`, with `mlflow.langchain.autolog()` and an outer `@mlflow.trace`-wrapped
`predict`. `agent/log_agent.py` is a companion driver doing the actual
`mlflow.pyfunc.log_model(...)` call with `resources=`/`pip_requirements=`/`input_example=`
— split into its own file because Models-from-Code logging re-executes `agent.py` as a
file path, so the file that defines the model cannot also be the one that logs it.

**Nothing here was guessed — every non-trivial interface was checked live this session,**
per the task's own instruction:
- `ResponsesAgent` over the legacy `ChatAgent`, confirmed against current Databricks docs.
- The exact `ChatDatabricks` + `UCFunctionToolkit` + `create_agent` combination, plus the
  entire stream-event conversion helper (`_process_agent_stream_events`), was pulled
  **verbatim from Databricks' own actively-maintained template** —
  `databricks/app-templates/agent-langgraph/agent_server/{agent.py,utils.py}`, fetched
  live via `raw.githubusercontent.com` this session — not recalled from an older tutorial.
  That template targets Databricks Apps (`@invoke()`/`@stream()`), not classic Model
  Serving; only the agent-construction and stream-conversion logic were reused, adapted to
  a plain `ResponsesAgent` subclass since Task 2.6 asked for the `log_model` + `resources=`
  deployment path, not an Apps server.
- `mlflow.pyfunc.log_model` uses **`name=`, not the deprecated `artifact_path=`** — MLflow
  3 makes models first-class citizens and no longer requires an active run to log one;
  confirmed against the current MLflow 3 migration guide. First draft passed both kwargs
  side by side as a hedge; caught and fixed before verifying rather than left in.
- Pinned pip versions (`mlflow==3.14.0`, `databricks-langchain==0.20.0`,
  `langchain==1.3.14`, `langgraph==1.2.9`, `python-dotenv==1.2.2`) are the actual current
  PyPI releases, fetched live via the PyPI JSON API — not round numbers from memory.

> ⚠️ **`temperature=0.1`, as Task 2.6 literally asked for, is NOT passed to `ChatDatabricks`
> — deliberately, not an oversight.** CLAUDE.md's own Databricks Free Edition section
> already carries the verified fact (re-confirmed live this session): **Claude Sonnet 5 on
> the Databricks FM API rejects `temperature`/`top_p`/`top_k` with a hard 400.** This is the
> exact model `LLM_ENDPOINT` resolves to. Passing it wouldn't degrade the agent — it would
> make every request fail outright. `TOOL_CALL_TEMPERATURE = 0.1` is kept as a named,
> unused constant in the file so the requirement's intent stays visible rather than
> silently vanishing. **Do not "fix" this by wiring the constant into `ChatDatabricks`** —
> that would break the agent. If a future model swap ever supports sampling params, that's
> when this constant gets used.

**"Tool max-iterations 6" is not LangGraph's `recursion_limit` directly.** LangGraph counts
graph super-steps, not raw tool calls: the ReAct-style graph alternates a model-turn and a
tool-turn per round, so N tool calls costs 2N steps plus one final model-turn with no
further tool calls. `AGENT_RECURSION_LIMIT = 2 * 6 + 1 = 13` — passing `6` directly would
silently cap the agent at 3 tool calls, not 6.

**Loading the prompt: verified, not assumed.** `_load_system_prompt()` extracts only the
text between the header's `---` and the pre-Appendix `---` in `system_prompt.md`, per that
file's own contract. Run standalone against the real file this session: extracts exactly
**900 words**, matching Task 2.5's own verified count, with no Appendix leakage.

**Nothing in this task has been run against a live workspace** — no `databricks_langchain`,
`langchain`, or `langgraph` install locally (PEP 668 + no venv set up for this), so only
`py_compile` syntax-checking and the dependency-free prompt-loading logic were actually
executed. The agent-construction path, the tool bindings, and the stream conversion are
verified against live current documentation and template source, not against a running
endpoint. First thing to check on real deployment: does `UCFunctionToolkit`'s default
client actually authenticate the same way `app/config.py`'s env vars are structured for,
and does `create_agent`'s compiled graph's sync `.stream()` accept `stream_mode` as a list
the same way its `.astream()` does (assumed by mirroring, not independently confirmed).

### Task 2.5: safety system prompt — written, 900 words, two deviations flagged

`agent/prompts/system_prompt.md`. Body is the prompt verbatim (judges read it,
`ARCHITECTURE.md` §5); the 10-item rationale appendix below the `---` is judge-facing and
**not** sent to the model. Verified mechanically (`count_prompt.py`, scratchpad), not by
eyeballing: **900/900 words**, all 13 mandatory elements present, appendix exactly 10
items. Note the file header blockquote contains the literal string `## Identity`, so a
naive `.index("## Identity")` slice measures 61 words too many — split on `\n---\n`.

**Two deviations, both deliberate — do not "fix" either without reading this:**

1. ⚠️ **Two citation forms, not one.** Task 2.5 asked for "every clinical sentence ends
   with `[chunk_id]`; no citation → the sentence must not be said." Taken literally that
   makes every interaction finding **unsayable**: `check_interactions` reads
   `gold.interaction_pairs`, which has no chunks — it returns `sources`. So a single-form
   rule would gag the warfarin+ibuprofen answer, i.e. the project centerpiece and the
   Phase 1 exit checkpoint. `DATA_CONTRACTS.md` §8 point 3 anticipates this exactly
   ("Interaction results are exempt from *this* rule but not from citation... The
   guardrail must recognize both citation forms or it will block correct interaction
   answers"). Prompt specifies both: `[chunk_id]` for label claims, `[source: ddinter]`
   for interaction claims. **Phase 4's guardrail must accept both** or it blocks the demo.
2. ⚠️ **988 added to the escalation routes — this deviates from `ARCHITECTURE.md` §5(c)**,
   which fixes the message at 911 / Poison Control / pharmacist. Of those three, self-harm
   would route to "pharmacist" or a generic 911, which is a real safety gap on the one
   trigger where the right resource is a crisis line. Added **988 Suicide & Crisis
   Lifeline** for the self-harm route only; the other three routes are unchanged.
   `ARCHITECTURE.md` §5(c) and the §6 adversarial eval cases should be amended to match —
   **flagged, not silently resolved; needs sign-off.**

Design stance for the eval harness's jailbreak bucket: rules are framed as identity
("these rules are what you are"), exceptions are denied **by category** rather than by
enumerating attacks (an enumerated list teaches the omitted attack and dates instantly),
and tool results plus uploaded images are declared data-not-instructions to close prompt
injection through a prescription photo or a label chunk. Rationale item 8 states plainly
that Rule 4 is defence-in-depth, not the enforcement — `manage_schedule` enforces
confirmation in code, per §5(a)'s "a prompt instruction is not an enforcement mechanism."

### Task 2.4: get_adherence_stats UC function — written, logic executed against DuckDB

`agent/tools/get_adherence_stats.sql` — pure SQL over `neurorx.gold.adherence_facts`,
zero LLM involvement by construction, same shape as Task 2.1. Returns five metrics
(`overall_adherence_pct`, per-drug `adherence_pct`, `current_streak_days`,
`most_missed_drug`, `most_missed_daypart`) as `(metric, drug_name, value_num, value_text)`
rows. **Reads `gold.adherence_facts`, not Lakebase `dose_events`** — this is a de-facto
vote for F9's recommendation while F9 is still formally unsigned-off (§5); the accepted
cost is that a dose marked taken seconds ago is not reflected until sync + Lakeflow run.

**Verified by actually executing the logic, not just parsing it.** No Spark locally (the
standing JDK-17/network block, §4), so the `RETURN` body was extracted, its two params
substituted with literals, transpiled `databricks`→`duckdb` with `sqlglot`, and run
against a fixture reproducing Margaret's cohort. Twelve checks pass: the task exit
criterion (`most_missed_drug = metformin`), `most_missed_daypart = evening`, row shape,
per-drug and overall percentages cross-checked against an independent Python calc, the
streak stopping on the correct day (bad day at D-6 → streak 5; bad day yesterday → 0),
the 30-day clamp, today's exclusion, and a two-patient leak check. Harness lives in the
scratchpad, not the repo (it verifies logic, not the deployed UC function).

**Real bug caught only by running it:** with no dose history the function returned a lone
`current_streak_days = 0` row rather than an empty result — because `SELECT <expr>` with
no `FROM` always yields exactly one row. That reads to the agent as "you broke your
streak" instead of "there is no data," and falsified the function COMMENT's own promise
that an empty result set means no history (the result could never be empty). Fixed by
anchoring `streak_calc` to `FROM (SELECT 1 FROM daily LIMIT 1)` — zero rows when there is
no history, one row otherwise. Reading the file did not catch this; the empty-fixture case
did. Same lesson as `chunking.py`, now on a `.sql` file: **transpile-and-run is available
for SQL too, and it finds things `sqlglot` cannot** — `sqlglot` only proves it parses.

Two other decisions recorded in the file rather than resolved silently: per-drug rows
group by `drug_name`, not `rxcui` (the requested output has no `rxcui` column, and F11
means grouping by rxcui could emit two rows both labelled `metformin`); and
`most_missed_*` rows are **omitted** when nothing was missed rather than emitted with
`value_num = 0`, so the agent cannot tell a perfect-record patient they miss metformin
most. Ties are broken by name ascending — "deterministic SQL" has to mean deterministic
on ties too.

### Task 2.3: manage_schedule UC function — written, real bugs fixed, most-tested tool so far

`agent/tools/manage_schedule.py` — the only path by which a schedule is ever written; two
gates enforced in code, not prompt (explicit `user_confirmed`, and a mandatory
`check_interactions` call before any drug addition). See the warning above for the
architecture — Lakebase Data API + SQL Statement Execution API, not `psycopg`, for the
*deployed* function. This got the deepest verification of any tool file yet: outer
f-string generation executed and `sqlglot`-parsed clean (after fixing two more
contraction-apostrophe cases — same narrow `sqlglot` gap as Task 2.1, now clearly
established as "always reword, don't investigate each time"), inner body syntax-checked
standalone, all pure logic functions (payload validation, confirmation gates) run
directly, the **deployed** function's HTTP logic run against 8 `responses`-mocked
scenarios (covering both confirmation gates, the block-then-override interaction flow,
malformed JSON, and the soft-delete path) — catching the two bugs described above — and,
per the task's own requirement #6, the **local test harness actually run against a real
local Postgres** (Homebrew `postgres`/`initdb`, spun up in the scratchpad — hit a Unix
socket path-length limit and a `postmaster became multithreaded` locale issue along the
way, both worked around) applying `DATA_CONTRACTS.md`'s literal DDL, confirming the
`schedules_frequency_match` constraint is actually enforced by Postgres itself, not just
assumed from reading the schema.

### Task 2.2: search_drug_labels UC function — written, unexecuted, real risk accepted

`agent/tools/search_drug_labels.py` — a UC Python function (`neurorx.app.search_drug_labels`)
that authenticates via OAuth client-credentials, then calls the Vector Search REST query
endpoint directly with `requests` (no SDK — the SDK needs a pip install inside the UDF
sandbox, which is a separate risk on top of the auth one). See the warning above for the
full architecture-risk story; this note is about what was actually verified. Everything
Python-syntax-level was independently executed, not just read: extracted and ran the
*outer* notebook f-string to confirm the generated SQL's nested brace-escaping was
correct (two levels of `{{`/`}}` doubling — outer notebook f-string generating SQL that
itself contains an inner Python f-string), extracted and syntax-checked the *inner* UC
function body as a standalone wrapped Python function, then actually ran it against
`responses`-mocked HTTP calls covering all four code paths: empty input, missing
credentials, a full success path with a **deliberately reordered manifest + trailing
score column** (reusing the exact Task 1.7 citation-corruption test), and a request
exception. All four passed, including confirming the manifest-based column mapping still
correctly resolves `rxcui`/`drug_name` even when the API's returned column order doesn't
match the request order. What was **not** and **cannot** be verified here: whether the
three credential env vars are actually reachable from inside a real UC Python function's
execution sandbox on Databricks — that requires an actual deployment.

### Task 2.1: check_interactions UC function — written, unexecuted

`agent/tools/check_interactions.sql` — pure SQL, zero LLM involvement by
construction. Takes `rxcui_list ARRAY<STRING>`, generates all unordered pairs via
`posexplode(rxcui_list)` invoked twice as a table reference (`p1.pos < p2.pos` to
dedupe), canonicalizes each pair with `LEAST`/`GREATEST` (lexicographic — see the
pair-ordering trap above), joins to `gold.interaction_pairs` + `silver.drugs` twice for
names, orders major-first. Two things flagged in the file itself: the task asked for
`COMMENT ON FUNCTION` (not real syntax — see §4) and for a `source STRING` output
column (the frozen table has `sources ARRAY<STRING>`; resolved as
`array_join(sources, ', ')` so nothing is lost). Verified structurally with `sqlglot`
(Databricks dialect) — parses clean and round-trips to the intended AST — but, same as
every other SQL file in this project, not run against a live workspace.

**Real bug caught by that same sqlglot check, not by reading:** the first draft had an
apostrophe in "patient's schedule" inside two `COMMENT` string literals, doubled
(`patient''s`) per standard SQL escaping. `sqlglot` failed to parse specifically the
`CREATE FUNCTION` parameter-`COMMENT` context with that escape (while parsing the
identical doubled-quote pattern fine in a table `COMMENT` and a plain `SELECT` —
isolated with a 3-line repro). Inconclusive whether that's a `sqlglot` parser gap or a
real Databricks quirk; fixed by rewording to drop the possessive rather than gambling on
which. Lesson: local syntax-checking tools can themselves be wrong in narrow corners —
when a check fails somewhere structurally unusual, isolate with a minimal repro before
trusting the tool's verdict, but when the fix is free (reword to avoid the ambiguity
entirely), take it rather than resolve the ambiguity.

### Task 1.7: Vector Search index — written, unexecuted

`pipelines/05_vector_index.py` creates the `neurorx-vs` AI Search endpoint and
the `neurorx.gold.drug_knowledge_index` delta-sync index, then runs the two
Phase 1 checkpoint verification queries (metformin missed-dose,
warfarin warnings). See §4 below for the `databricks-vectorsearch` package
naming transition and a real citation-corruption bug caught before it shipped.

### Task 1.5/1.6: Lakeflow pipeline + chunking — written, unexecuted

`pipelines/medallion_pipeline.py` builds every silver/gold table in
`DATA_CONTRACTS.md` §4-§5. `pipelines/chunking.py` is the pure, dependency-free
chunker it imports — the one file in this project actually run and verified
locally end-to-end (`python3 pipelines/chunking.py`), since it has zero
Spark/Databricks dependency. See §4 below for what verifying it caught.

**Real bug caught only by running the code, not by reading it:** the initial
chunk-packing implementation only checked against `hard_max` (1000 tokens),
never `target_max` (800) — so chunks crept up to ~978 tokens each, leaving no
headroom for the required ≤15% overlap. Every overlap attempt then failed its
own hard-max guard and got silently skipped. A careful manual re-read did not
catch this; running the self-test against a long synthetic section did.
Fixed by making the packing loop flush at `target_max` once `target_min` is
reached, reserving `hard_max` purely as a safety ceiling for the pathological
single-oversized-sentence case. Lesson: for pure Python (no Spark/Databricks
dependency), actually run it — a read-through is not a substitute.

**`chunk_id` conflict, resolved in favor of the frozen contract:** Task 1.6's
own instructions specified `chunk_id = sha256(set_id|section|chunk_index)`
truncated to 16 hex chars. `DATA_CONTRACTS.md` §4.2 freezes a different,
human-readable formula (`concat_ws(':', set_id, section, lpad(chunk_index,4,'0'))`)
and explicitly justifies it as "good for demoing a citation." Went with the
frozen contract — chunk_id is the citation handle the agent emits to
patients, and `medallion_pipeline.py` (written first, Task 1.5) already
depended on that format. See `chunking.py`'s module docstring for the full
reasoning.

### Task 1.4: Synthetic cohort generator — NOT complete, three unfixed bugs

**`data/ingestion/04_synthetic_cohort.py`** — see the warning above for the three
confirmed bugs (no Delta writes, 75% non-curated drug list, every non-demo patient
surnamed "Smith"). The adherence-modeling logic described below is real, reasonably
sophisticated work and does compute the numbers it claims to — the problem is entirely
in the plumbing around it (wrong drug list, nothing persisted, one broken name-generation
formula), not the statistics. What it *attempts* to generate, per DATA_CONTRACTS.md §3.4–§3.6:

- **50 patients** (deterministic, seed=42)
- **Demo patient** `Margaret Demo` (UUID: `12345678-1234-1234-1234-123456789012`)
  - Fixed drugs: metformin, lisinopril, warfarin, atorvastatin (as drug names, per Phase 3)
  - Specific schedules: metformin 2x/day (morning + **evening**), others 1x/day realistic times
  - **44% adherence** with 75.6% miss rate on metformin evening doses (key demo story)
- **2–6 drugs per patient** from 200-drug list
- **194 schedules** (times_per_day 1–3, realistic dose_times by day-part)
- **~72,000 dose_events** (6 months, 180 days)
  - Adherence per patient from **Beta(8,2)**
  - Evening doses missed **2× more** (penalty 0.5)
  - Weekend doses missed **1.5× more** (adherence ×0.67)
  - **One "bad week" per patient** with adherence halved
  - Statuses: `taken` (jittered ±45min), `missed` (no action), `skipped` (2%, rare)
- **Deterministic** across re-runs with seed=42
- **Per-patient adherence % printed** for eyeballing

Implementation:
- No external dependencies (numpy only; no pandas/Faker needed at runtime) — but also,
  per the warning above, no Databricks/Spark dependency either, which is *why* it never
  writes anywhere: it was never converted from a standalone script into something that
  actually targets `neurorx.bronze.*`.
- Deterministic UUID generation (MD5 hash-based, not `uuid.uuid4()`) — this part does work.
- The "all 9 requirements met" claim from an earlier version of this note did not hold up
  under re-running the code — see the warning above.

### Task 1.8: Phase 1 checkpoint verification — written, one bug found and fixed

**`setup/phase1_checkpoint.sql`** is a comprehensive read-only verification notebook that
checks Phase 1 data completeness — structurally solid, correct table names throughout,
one real bug (Query 6c, see the warning above) already caught and fixed. **Cannot actually
be run to a passing state yet**: its bronze/gold synthetic-data queries depend on Task 1.4,
which currently writes nothing. Seven queries with expected results:

1. **Row counts** for all 12 bronze/silver/gold tables (expect: all non-zero)
   - Bronze: 50 patients, 194 schedules, 72,000+ dose_events, 3 ingestion tables
   - Silver: drugs, label_sections, interactions (count depends on FDA/RxNorm/DDInter coverage)
   - Gold: drug_knowledge, interaction_pairs, adherence_facts

2. **Warfarin+ibuprofen interaction pair** (Phase 1 exit checkpoint)
   - Query: join `interaction_pairs` with `silver.drugs` twice to show both drug names
   - Expected: one row, `rxcui_a='11289'`, `rxcui_b='5640'`, severity='major', sources array includes 'ddinter'
   - Validates: lexicographic pair ordering, cross-source dedup, clinical correctness

3. **Top 5 drugs by label-chunk count** (completeness of label corpus)
   - Expected: drugs with highest coverage in `gold.drug_knowledge` ranked by chunk count
   - Shows: section count, min/max chunk character lengths

4. **Label sections: presence and token statistics**
   - Expected: four distinct sections present (dosage_and_administration, drug_interactions, warnings, information_for_patients)
   - Token counts: min/avg/max/median per section, verify 500–800 target range

5. **Interaction pairs invariant checks** (deterministic safety table)
   - Query 1: zero rows with `rxcui_a >= rxcui_b` (canonical order violation)
   - Query 2: zero duplicate pairs on (rxcui_a, rxcui_b)

6. **Adherence facts sanity checks** (derived table validity)
   - Query 1: overall cohort stats — adherence_pct should span 60–95% (realistic variability)
   - Query 2: Margaret Demo's adherence by drug — metformin should have lowest avg adherence_pct (due to evening miss penalty)
   - Query 3: Margaret Demo's metformin by day_part — evening should show highest miss count/lowest adherence

7. **Bonus: Phase 1 summary dashboard** (pass/fail checklist)
   - Expected: all six checkpoints show ✓ PASS if Phase 1 is complete

All queries are read-only (no writes, no modifications). Run in Databricks SQL notebook;
each comment states the expected result. If all pass, Phase 2 (UC function implementation,
agent tools) can proceed.

> ⚠️ **Nothing has been executed against a live Databricks workspace.** Every notebook is
> written and syntax-verified but unrun. The Phase 1 exit checkpoint (warfarin+ibuprofen
> queryable; vector query returns the metformin missed-dose chunk) is **unverified**.
> `rxnorm_client.py` is the only module actually run end-to-end (locally, against the live
> RxNav API).

> ⚠️ **Git: one commit only** (`3b4047e` scaffold). Everything since — DATA_CONTRACTS.md
> edits, config.py, .env.example, all four ingestion notebooks, the Lakeflow pipeline,
> the chunking module, the vector index notebook, the checkpoint SQL, and all three
> Phase 2 tool files — is uncommitted. The hackathon requires in-period commits; this
> needs attention and is getting more overdue as more work piles up uncommitted.

---

## 4. Verified API facts

**Established by hitting the live APIs, not from docs or memory. Do not "fix" these
from intuition — several are counterintuitive and were caught only by testing.**

### RxCUIs (live RxNav)

| Drug | RxCUI | Note |
|---|---|---|
| warfarin | `11289` | tty=IN |
| ibuprofen | `5640` | tty=IN |
| metformin | `6809` | tty=IN. `235743` is *metformin hydrochloride*, tty=PIN |
| lisinopril | `29046` | tty=IN |

### ⚠️ The pair-ordering trap (DATA_CONTRACTS.md F1)

`rxcui` is a `STRING`, so `LEAST`/`GREATEST` compare **lexicographically, not numerically**:

- `'11289' < '5640'` (because `'1' < '5'`) → canonical warfarin+ibuprofen pair is
  **`('11289', '5640')`**
- Numeric ordering would give `('5640', '11289')` — **the opposite**

Write with one convention, read with the other → query returns zero rows → **the
interaction is silently missed**. No error. On the exact pair that is the Phase 1 exit
checkpoint and the eval set's headline true-positive.

The stored order looks wrong to anyone reading the RxCUIs as numbers. It is not wrong.
If you want numeric ordering, change the pipeline, `check_interactions`, **and** the
`warfarin_ibuprofen_present` canary together in one commit.

### openFDA (`api.fda.gov/drug/label.json`)

- `set_id` is **top-level** (equals `openfda.spl_set_id[0]`).
- The version field is literally **`version`**, not `spl_version`. (Our column is named
  `spl_version`; its value comes from `version`.)
- `effective_time` is a top-level **`"YYYYMMDD"` string**, not a date.
- Target sections are top-level keys, each a **list of one string**. A missing section
  means the key is **absent entirely**, not null — check with `in`, never `.get(k, default)`.
- **`patient_information` does not exist.** The real `information_for_patients` fallbacks
  are **`patient_medication_information`** and **`spl_patient_package_insert`**.
- **No match returns HTTP 404** with `{"error":{"code":"NOT_FOUND"}}` — not an empty list.
- **`limit=1` returns combination products.** `metformin` → `SITAGLIPTIN AND METFORMIN
  HYDROCHLORIDE`; `lisinopril` → `LISINOPRIL AND HYDROCHLOROTHIAZIDE TABLETS`. Fix in
  `01_openfda_ingest.py`: fetch a 25-candidate pool, score for single-ingredient +
  section completeness.
- **Multi-word queries must be quoted.** Unquoted `insulin glargine` ORs the terms and
  returns unrelated drugs; `"insulin glargine"` works.
- Rate limits: **240/min, 1,000/day** unauthenticated; 120,000/day with a free key from
  `https://api.data.gov/signup/`.

### RxNav (`rxnav.nlm.nih.gov/REST`)

- **`search` parameter semantics** (this is subtle and matters):
  - `search=0` → exact only. `metformin` → `['6809']`
  - `search=1` → normalized only. `metformin` → `['235743', '6809']`
  - `search=2` → **exact, falling back to normalized only if exact found nothing** — NOT
    the union. `metformin` → `['6809']`
  - Use `search=2`. It cleanly resolves metformin's apparent ambiguity.
- **`approximateTerm` returns multiple rows per RxCUI** — one per source vocabulary
  (RXNORM, VANDF, ATC, DRUGBANK…), all sharing one score. Dedupe by rxcui or you'll get
  N copies of one drug.
- **`score` is a JSON string**, e.g. `"8.331008911132812"`. Cast with `float()`.
- Empty results: `{"idGroup":{}}` (no `rxnormId` key) / `{"approximateGroup":{"inputTerm":null}}`
  (no `candidate` key).
- `/REST/rxcui/{rxcui}/properties.json` → `{"properties":{"rxcui","name","tty",…}}`.
- Rate limit: **20 req/sec**, no key needed. NLM asks for 12–24h caching.

### DDInter 2.0

- Download page: `https://ddinter2.scbdd.com/download/`
- **8 CSVs**, ATC codes A, B, D, H, L, P, R, V:
  `https://ddinter2.scbdd.com/static/media/download/ddinter_downloads_code_{X}.csv`
- Header: **`DDInterID_A,Drug_A,DDInterID_B,Drug_B,Level`**. ~222,383 rows total.
- `Level` values: `Major` / `Moderate` / `Minor` / `Unknown` → lowercase for our enum.
- **No description column exists** in the bulk export.
- **The ATC gap is a false alarm.** Codes C (cardiovascular), G, J, M (musculoskeletal),
  N, S have no file — which looks like it excludes ibuprofen (M) and lisinopril (C).
  It doesn't: DDInter files each pair under *one* side's class. Verified
  `DDInter1951,Warfarin,DDInter900,Ibuprofen,Major` is present in file **B**. Download
  all 8 for full practical coverage.

### Databricks Free Edition

- **One** pre-created SQL warehouse, capped `2X-Small`. You cannot create another.
- **One** AI Search endpoint, one search unit, **`DELTA_SYNC` only** (no Direct Vector
  Access). Requires CDF on the source table — compatible with our design, but don't
  create the endpoint before the `silver` table exists or you burn the quota on nothing.
- **One** Lakebase project per account. Autoscaling (not Provisioned) since 2026-03-12.
- No GPU serving, no provisioned throughput.
- Claude via FM API: `databricks-claude-sonnet-5` (recommended), `-opus-4-8`,
  `-haiku-4-5`, etc. **Sonnet 5 rejects `temperature`/`top_p`/`top_k` with a 400.**
- Same-folder notebook imports need **no** `sys.path` manipulation.

### Unity Catalog SQL functions

- **`COMMENT ON FUNCTION` is not valid Databricks SQL.** The `COMMENT ON` object list is
  `CATALOG | COLUMN | CONNECTION | PROVIDER | RECIPIENT | SCHEMA | SHARE | TABLE |
  VOLUME` — no `FUNCTION`. To attach the natural-language description the agent
  framework surfaces as a tool's spec, use the **inline `COMMENT` clause** inside
  `CREATE FUNCTION` — on the function itself, on each parameter, and on each output
  column of a `RETURNS TABLE(...)`.
- ⚠️ **A column takes precedence over a routine parameter of the same name.** Confirmed
  verbatim against the current `Name resolution` reference: inside a `CREATE FUNCTION`
  body, an unqualified identifier matches a column of a `FROM`-clause table reference
  *first*; routine parameters are resolved several rules later. So in a function taking
  `patient_id STRING` over a table that also has a `patient_id` column:

  ```sql
  WHERE patient_id = patient_id     -- ☠️ this is `col = col`, TRUE for every row
  ```

  It is not a filter. It silently returns **every patient's** data with no error and a
  plausible-looking number — a data leak that only a two-patient test can see (one
  patient's result looks perfectly normal). **Fix: qualify the parameter with the
  function name** — `af.patient_id = get_adherence_stats.patient_id`. The docs' own
  example is `RETURN (SELECT a FROM VALUES(1) AS T(a) WHERE t.a = func.a)`. This bit
  Task 2.4 at design time; `get_adherence_stats.sql` qualifies every parameter reference
  and carries a two-patient regression cell specifically to catch a future unqualifying.
  Note the same hazard applies to **variables**, which rank *below* parameters.
- **`sqlglot` proves a `.sql` file parses; it cannot prove the query is right.** For SQL
  whose logic matters, `sqlglot.transpile(body, read="databricks", write="duckdb")` plus
  a DuckDB fixture actually *runs* it locally, no Spark or JDK needed — this is the SQL
  analogue of the "just run it" rule in §6 and it caught a real Task 2.4 bug that both
  reading and parsing missed. Substitute UC function params with literals to extract a
  runnable body. Caveat: it verifies **arithmetic and row shape only** — not UC-function
  semantics (parameter shadowing, `COMMENT` surfacing, `RETURNS TABLE` binding), which
  still need a real deployment.
- `posexplode(array_expr) AS alias` invoked as a **table reference** (comma-joined,
  e.g. `FROM posexplode(arr) AS p1, posexplode(arr) AS p2`) is the current, correct way
  to self-cross-join an array against itself (e.g. to generate all unordered pairs with
  `p1.pos < p2.pos`). `LATERAL VIEW posexplode(...)` still works but is documented as
  deprecated in favor of the table-reference form.
- **`sqlglot` (pure Python, no JVM) is a useful local check for `.sql` files** in this
  project, given the standing JDK-17/no-network constraint that blocks a real local
  Spark session (see the Lakeflow section below). `sqlglot.parse(sql, dialect="databricks")`
  catches real syntax errors and — via `.sql(dialect="databricks", pretty=True)` — lets
  you eyeball that the parser understood the intended structure, not just that it didn't
  throw. Caveat: it is not Databricks' own parser and has at least one narrow gap found
  in this project (a doubled-quote escape that parsed fine in a table `COMMENT` and a
  plain `SELECT`, but failed specifically inside a `CREATE FUNCTION` parameter
  `COMMENT`) — treat a `sqlglot` failure as "investigate," not as definitive proof real
  Databricks would also reject it.
- **UC Python functions (`LANGUAGE PYTHON`) have no `spark` session** — confirmed: this
  is a documented, deliberate difference from session-scoped notebook UDFs. Rules out
  calling `spark.sql(...)` (e.g. the native `vector_search()` table function) from inside
  one.
- **The one documented in-sandbox credential mechanism,
  `databricks.service_credentials.getServiceCredentialsProvider()`, is scoped to
  *external* cloud services** (AWS/Azure/GCP resources) — not Databricks' own internal
  REST APIs. No auto-auth path for a UC Python function calling back into its own
  workspace (e.g. Vector Search) was found after real effort. Network calls on ports
  80/443/53 ARE allowed, and custom pip dependencies CAN be declared via `ENVIRONMENT`
  (syntax: `ENVIRONMENT (dependencies = '["pkg==1.0.0"]', environment_version = 'None')`,
  confirmed with a verbatim official multi-line-body example) — but installing pip
  dependencies on serverless SQL warehouses specifically also needs a Public Preview
  networking feature that may not be enabled on Free Edition.
- **Databricks' own official pattern for "wrap a Vector Search index as a UC-function
  agent tool" is a SQL function using the native `vector_search()` table function** —
  confirmed against the official `unstructured-retrieval-tools` doc with a full verbatim
  example (`FROM vector_search(index => ..., query => query, num_results => 5)`,
  output aliased `page_content`/`metadata` for MLflow's retriever schema). This has no
  auth problem since it runs in the query engine's own trust boundary. Prefer this over a
  Python UDF unless there's a specific reason not to — see Task 2.2's warning above for
  what happened when the alternative was chosen anyway.
- Vector Search REST query endpoint, for when a raw HTTP call is unavoidable: `POST
  {host}/api/2.0/vector-search/indexes/{index_name}/query`, body field is **`filters`**
  (not `filters_json`) — confirmed via a verbatim curl example, though that example used
  a SQL-string filter value (storage-optimized-endpoint form); a `STANDARD` endpoint
  (Free Edition's only option) accepting a `json.dumps({...})`-encoded dict string is
  *inferred* from the SDK's dual-type `filters: str | Dict[str, Any]` signature, not
  directly witnessed at the REST layer. Documented non-notebook auth: OAuth
  client-credentials against `POST {host}/oidc/v1/token` with
  `auth=(client_id, client_secret)`, `data={"grant_type": "client_credentials", "scope":
  "all-apis"}`, response has an `access_token` field.

### Lakeflow Declarative Pipelines Python API

- Import is `from pyspark import pipelines as dp` — the modern API. `dlt` is
  legacy/back-compat only.
- **`@dp.table` is for streaming reads only.** Every batch transformation
  over an existing Delta table uses `@dp.materialized_view` instead — using
  `@dp.table` for a batch read is silently wrong (wrong dataset type, not an
  error), and this is exactly the mistake a naive port of old Delta Live
  Tables tutorial code makes.
- **A single pipeline has ONE default target schema.** To publish tables
  into two schemas (e.g. `silver` and `gold`) from one pipeline file, every
  `@dp.materialized_view` needs an explicitly schema-qualified `name=`
  (`name="silver.drugs"`, not a bare `name="drugs"`) — confirmed against
  current docs after a first draft used bare names for every table, which
  would have either collided or landed in the wrong schema.
- Tables defined earlier in the same pipeline (including other schemas) are
  read with plain `spark.read.table("catalog.schema.table")` — no `dp.read()`
  call exists. Pipeline-internal `@dp.temporary_view`s are referenced by
  their bare function name (they're never published to Unity Catalog).
- Pipeline-level config (e.g. the FM endpoint name) is read via
  `spark.conf.get("key", default)` at module top level — `spark` is injected
  into the pipeline file's global namespace by the runtime, same as a
  notebook. Don't import `app/config.py` into a pipeline file: that module
  requires all 9 of its env vars (including Lakebase credentials) just to
  resolve one endpoint name — the wrong coupling for a pipeline that never
  touches Lakebase.
- `ai_query`'s `returnType` parameter accepts a DDL string including a
  top-level `ARRAY<STRUCT<...>>`, giving a native explode-able array column
  with no separate `from_json` step.
- **The open-source PyPI `pyspark` package does NOT include the `expect`/
  `expect_or_drop`/`expect_or_fail`/`expect_all*` decorator family at all**
  (confirmed: `dir(pyspark.pipelines)` on a local `pip install pyspark`
  lists `table`, `materialized_view`, `temporary_view`, etc. but no
  `expect*`). Those are a Databricks-Runtime-only extension on top of the
  open-source "Spark Declarative Pipelines" core. A local pyspark install
  can exercise everything else in a pipeline file, but not the expectation
  decorators themselves — that verification rests on the official
  Databricks docs, not local execution.
- Testing a Lakeflow pipeline file locally needs a real `SparkSession`,
  which needs Java 17+. This machine only has Java 8 system-wide, and
  fetching a portable JDK failed — this sandbox's network access is
  allowlisted to the project's actual API domains (openFDA, RxNav, DDInter,
  PyPI) and nothing else. Full pipeline execution has never been tested;
  verification for `medallion_pipeline.py` was a static dependency-graph
  check (every table reference resolves to something the file actually
  publishes) plus isolated plain-Python checks of the core logic
  (canonical ordering, chunk_id format, day-part boundaries, severity
  precedence) — not an actual Spark run.

### `databricks-vectorsearch` client / AI Search

- **The Python package is mid-rename, same as the product.** Confirmed
  directly against `databricks-ai-search`'s own PyPI README:
  `databricks-vectorsearch` is now the legacy name — once a companion
  `databricks-vectorsearch>=0.74` release lands it becomes a thin re-export
  shim of `databricks-ai-search` with a deprecation warning.
  `VectorSearchClient is AISearchClient` (literally the same class, kept as
  a backward-compat alias). Both package names currently work.
- Real, documented methods (verified against the generated API reference,
  not memory): `client.endpoint_exists(name)`, `client.index_exists(endpoint_name, index_name)`,
  `client.create_endpoint_and_wait(...)`, `client.create_delta_sync_index(...)`,
  `index.wait_until_ready(verbose=True)`, `index.sync()`. Use these for
  idempotent create-if-absent + wait-for-ready — don't hand-roll a polling
  loop against `describe()`'s internal status fields; current docs don't
  publish their exact shape or values.
- **`filters={"col_a": val_a, "col_b": val_b}` combines multiple keys with
  AND** (confirmed with a verbatim doc example) — this is what a compound
  rxcui+section filter needs, and it works as expected.
- ⚠️ **`similarity_search()`'s `data_array` rows are NOT just "one value per
  requested column"** — there's a trailing similarity-score value appended
  after them. Confirmed against a complete official worked example, whose
  own result-processing helper explicitly skips `item[-1]` as the score.
  That same example reads column names from `results["manifest"]["columns"]`
  (list of `{"name": ...}` dicts) rather than trusting that the `columns=`
  request order matches the returned row order.
- **A real bug this caught:** the first draft built citation dicts via
  `dict(zip(requested_columns, row))` — i.e., trusted request order. Proved
  with a simulated reordered-manifest response that this silently produces
  *wrong* citations (rxcui and drug_name values swapped under each other's
  keys, no error) whenever the actual returned column order doesn't match
  what was requested. Fixed by mapping from `manifest["columns"]` names
  instead of request-order position. This is exactly the failure mode the
  whole citation-contract design (`DATA_CONTRACTS.md` §8) exists to prevent
  — a citation is only verifiable if its fields are actually correct.
- `databricks-gte-large-en` confirmed current directly against the
  AWS-specific supported-models doc page (1024-dim, 8192-token window).
- Free Edition: one AI Search endpoint, one search unit, `DELTA_SYNC` only
  — `endpoint_type="STANDARD"` (no `STORAGE_OPTIMIZED` option), consistent
  with `setup/00_workspace_runbook.md` §3.

### Lakebase from inside a UC Python function (Task 2.3)

- **Raw `psycopg`/TCP cannot work in the deployed UC Python function sandbox** —
  confirmed deterministic, not just unverified: Postgres needs port 5432; the sandbox
  allows only 80/443/53 (same restriction as Task 2.2). This is a real port block, not an
  auth question, so there was nothing to ask the user about.
- **Lakebase Data API**: a PostgREST-compatible REST interface over HTTPS (port 443,
  confirmed against current docs), explicitly built for "web applications, microservices,
  serverless functions" — i.e. exactly this situation. Base URL pattern
  `{REST_ENDPOINT}/{schema}/{table}` (schema is usually `public`); auth is
  `Authorization: Bearer <token>` (the same OAuth bearer token already needed for
  everything else). `GET` with PostgREST filter syntax (`?col=eq.value`, multiple params
  = AND) for reads; `POST` for insert; `PATCH` + the same filter syntax for update.
  **`Prefer: return=representation` makes POST/PATCH return the affected row(s) as a JSON
  array — even for a single row.** Forgetting to unwrap that array is exactly the bug
  described above.
- **SQL Statement Execution REST API** (`POST /api/2.0/sql/statements`, confirmed against
  current docs) is how one UC function calls another from inside a UC Python function
  body — needs a `warehouse_id` (Free Edition has exactly one pre-created serverless
  warehouse). Same OAuth bearer token as everything else in this section.
- `psycopg` (v3, **not** `psycopg2`) is current for Lakebase — confirmed against current
  Lakebase connection docs, which also confirm the default dbname is literally
  `databricks_postgres`, port `5432`, `sslmode="require"`, and that the *real* Lakebase
  instance uses `w.database.generate_database_credential(...)` (a short-lived generated
  token) as the password rather than a static one. None of this applies to the deployed
  UC function (blocked by the port restriction above) — it's what the **local test
  harness** uses, connecting to any Postgres, real Lakebase or otherwise.

---

## 5. Open blockers — decisions needed before Phase 1 writes data

From [`DATA_CONTRACTS.md`](DATA_CONTRACTS.md) §2. All five are still unsigned-off.

| ID | Issue | Status |
|---|---|---|
| **F1** | RxCUI pair ordering lexicographic vs numeric | Contract specifies lexicographic + canary. Needs sign-off. |
| **F2** | `guardrail_blocks` home: Lakebase (task spec) vs Delta (ARCHITECTURE.md §2/§5(e) + plan §5) | **Open.** 3 sources say Delta, 1 says Lakebase. |
| **F3** | `adherence_facts` can't be "the synced table" — its shape is an aggregate | **Open.** Must be derived from a synced `dose_events`. |
| **F4** | `skipped` dose status unaccounted for in `adherence_facts` | **Open.** Changes the headline dashboard number. |
| **F5** | No `synthetic_schedules_raw`, but `dose_events.schedule_id` is a required FK | **Resolved in Task 1.4.** `04_synthetic_cohort.py` generates `synthetic_schedules_raw` as bronze audit table per DATA_CONTRACTS §3.6. |

F6–F15 (important/minor) are detailed in the same section.

---

## 6. How to work on this

**Verify external API field paths against the live API before writing code that depends
on them.** This is not pedantry — it has caught a wrong field name in the task spec
(`patient_information`), a silent combo-product bug on the demo's own drugs, a
string-vs-number comparison bug, and a lexicographic-ordering trap that would have
produced a silent false negative on the project's centerpiece interaction. Roughly one
real defect per task, every task so far.

**Flag document conflicts; never silently resolve them.** ARCHITECTURE.md, DATA_CONTRACTS.md,
the build plan, and the task prompts have contradicted each other repeatedly (tool names,
`guardrail_blocks` placement, bronze filtering rules, row grain). When they conflict, say
so and record the resolution in the affected file — don't quietly pick one.

**DATA_CONTRACTS.md is frozen.** Column names/types are what later phases build against.
Changing one is a contract change: update that file first, then the code.

**Bronze warns, silver enforces.** Bronze never drops a row — it's the audit record of
what the source actually returned. `FAIL UPDATE` is for defects, not bad source data.
Everything on `gold.interaction_pairs` is `FAIL UPDATE` deliberately: a bad row there is
a missed interaction shown to a patient as safety.

**If a module has no Spark/Databricks dependency, actually run it — don't just read it.**
`pipelines/chunking.py`'s packing/overlap bug (§4) survived a careful manual line-by-line
review and was only caught by running the self-test against a realistically long input.
Static review catches typos and wrong references; it does not catch two correctly-typed
constraints that are only in tension once real numbers flow through them.

**A "✅ complete" note in this file is a claim, not a fact — verify it before building on
it, the same way an external API claim gets verified.** This has now happened twice on
Task 1.4 alone: a "✅ complete" marker was added to this document while the underlying
file still had zero Delta writes, a 75%-invented drug list, and a name-generation bug
making every non-demo patient "Smith." Both times, re-running the actual code (not
re-reading the note, not re-reading the code) surfaced this in under a minute. This
matters more here than for most projects: multiple sessions edit this repo concurrently
(seen directly — a second session was mid-write on `04_synthetic_cohort.py` earlier in
this project), so a stale or optimistic status note can persist and compound.

---

## 7. Environment gotchas

- **The project path contains a space**: `/Users/guts/Projects /NeuroRx AI` — the parent
  directory `Projects ` has a trailing space. Quote paths in every shell command.
- **No `databricks` CLI installed.** Workspace exists but is unwired locally.
- **Python 3.14.6, PEP 668-managed** — `pip install` to system Python fails. `requests`
  is not installed system-wide. Use a venv in the scratchpad for local testing.
- The `.claude/projects/` memory dir moved when the project directory was renamed
  (it briefly had a trailing dot). Memories were migrated from
  `-Users-guts-Projects--NeuroRx-AI-` to `-Users-guts-Projects--NeuroRx-AI`.
