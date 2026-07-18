# Databricks notebook source
# MAGIC %md
# MAGIC # `neurorx.app.manage_schedule` — schedule CRUD with enforced safety gates
# MAGIC
# MAGIC The only path by which a schedule is ever written. Two safety gates are
# MAGIC enforced **in this code**, not the agent's prompt (`ARCHITECTURE.md` §5(a),
# MAGIC (d)): explicit user confirmation on every mutation, and a mandatory
# MAGIC `check_interactions` call — with the write blocked on a positive result —
# MAGIC before any drug is added.
# MAGIC
# MAGIC ## ⚠️ Two architecture risks found and resolved while writing this file
# MAGIC
# MAGIC **1. Raw `psycopg` cannot work inside the deployed UC function.** Same
# MAGIC restriction discovered in Task 2.2: a UC Python function's sandbox allows
# MAGIC network traffic only on ports 80, 443, 53. Postgres's wire protocol needs
# MAGIC port 5432 — outside that allowlist, confirmed against the same Databricks
# MAGIC docs used in Task 2.2. Unlike Task 2.2's auth question (which had two
# MAGIC genuinely viable options), this one doesn't: a port-5432 TCP connection
# MAGIC from inside the sandbox will not work, full stop, so this wasn't put back
# MAGIC to the user as a fork — it's the same class of "literal instruction
# MAGIC doesn't work here, apply the documented fix" call made throughout this
# MAGIC project (e.g. Task 2.1's `COMMENT ON FUNCTION`).
# MAGIC
# MAGIC   **The fix:** Lakebase has a **Data API** — a PostgREST-compatible REST
# MAGIC   interface over HTTPS (port 443), confirmed against current Databricks
# MAGIC   docs, explicitly built for "web applications, microservices, serverless
# MAGIC   functions" — exactly this situation. The **deployed** function body
# MAGIC   below uses this over `requests`, not `psycopg`. It authenticates with
# MAGIC   the same OAuth bearer token this file already needs for its second
# MAGIC   problem (below), so this adds no new credential type.
# MAGIC
# MAGIC   **Requirement #6's local test harness, however, genuinely does use
# MAGIC   `psycopg`** — it runs on a normal dev machine outside the UC sandbox,
# MAGIC   against "any Postgres with the DDL applied" (a local/Docker Postgres,
# MAGIC   no Databricks involved), where the port restriction doesn't apply at
# MAGIC   all. `psycopg` (v3, not `psycopg2` — confirmed against current Lakebase
# MAGIC   connection docs) is exactly the right tool there, and it's what that
# MAGIC   section of this file uses.
# MAGIC
# MAGIC **2. `check_interactions` (Task 2.1) is itself a UC function — calling it
# MAGIC   from inside another UC Python function hits the identical "no `spark`
# MAGIC   session" wall.** Resolved with the **SQL Statement Execution REST API**
# MAGIC   (`POST {host}/api/2.0/sql/statements`, confirmed against current
# MAGIC   Databricks docs) — runs a SQL statement against a warehouse over HTTPS,
# MAGIC   same OAuth bearer token. Needs a running warehouse id; Free Edition has
# MAGIC   exactly one pre-created serverless warehouse (`CLAUDE.md` §4).
# MAGIC
# MAGIC ## `app/config.py`'s existing `lakebase_*` fields don't cover this
# MAGIC
# MAGIC `config.py`'s `lakebase_host`/`lakebase_db`/`lakebase_user`/`lakebase_password`
# MAGIC assume a direct psycopg/TCP connection — which is exactly right for the
# MAGIC local test harness below, but not for the deployed function, which needs a
# MAGIC REST endpoint URL and OAuth service-principal credentials instead. Four new
# MAGIC env vars are used for the deployed path (three shared with Task 2.2):
# MAGIC `NEURORX_DATABRICKS_HOST`, `NEURORX_SP_CLIENT_ID`, `NEURORX_SP_CLIENT_SECRET`,
# MAGIC plus `NEURORX_SQL_WAREHOUSE_ID` and `NEURORX_LAKEBASE_REST_ENDPOINT` (new).
# MAGIC This is flagged rather than silently patched into `config.py`, since that
# MAGIC file is shared with every other tool and changing its shape is a bigger
# MAGIC decision than one tool file should make unilaterally.
# MAGIC
# MAGIC ## Verified before writing this file
# MAGIC
# MAGIC - Lakebase Data API: base URL pattern `{REST_ENDPOINT}/{schema}/{table}`,
# MAGIC   `Authorization: Bearer <token>`, PostgREST filter syntax (`?col=eq.value`)
# MAGIC   for SELECT/UPDATE row targeting, JSON body for INSERT/UPDATE column
# MAGIC   values, POST for insert, PATCH for update — confirmed against current
# MAGIC   Databricks docs with verbatim curl examples for each.
# MAGIC - SQL Statement Execution API and OAuth client-credentials flow — same
# MAGIC   endpoints/shapes already verified for Task 2.2.
# MAGIC - `psycopg` (not `psycopg2`) is current for Lakebase; default dbname is
# MAGIC   literally `databricks_postgres`; port `5432`; `sslmode="require"` —
# MAGIC   confirmed against current Lakebase connection docs. The *deployed*
# MAGIC   function never uses any of this (point 1 above); the *local test
# MAGIC   harness* does.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

CATALOG = "neurorx"
VALID_ACTIONS = ["create_from_extraction", "add_drug", "update_timing", "remove_drug", "list"]
VALID_STATUSES = ["active", "stopped"]

# COMMAND ----------

# MAGIC %md
# MAGIC ## Pure logic: payload validation and the confirmation state machine
# MAGIC
# MAGIC Deliberately I/O-free — these are exactly what the local test harness
# MAGIC exercises directly, and what gets inlined into the deployed UC function
# MAGIC body below (a UC function body is a self-contained string; it cannot
# MAGIC `import` this notebook's other cells, so the logic is necessarily
# MAGIC duplicated there — this cell is the single source of truth for what that
# MAGIC duplicate must match).

# COMMAND ----------

import json


def validate_payload(action, payload):
    """Returns an error message string, or None if payload is well-formed for
    this action. Checks structure/types/required fields only -- does not
    touch the database.
    """
    if action not in VALID_ACTIONS:
        return f"Unknown action {action!r}. Must be one of {VALID_ACTIONS}."
    if not isinstance(payload, dict):
        return "payload must be a JSON object."

    if action in ("create_from_extraction", "add_drug"):
        drugs = payload.get("drugs") if action == "create_from_extraction" else [payload.get("drug")]
        if action == "create_from_extraction":
            if not isinstance(drugs, list) or not drugs:
                return "create_from_extraction requires a non-empty 'drugs' array."
        else:
            if not isinstance(payload.get("drug"), dict):
                return "add_drug requires a 'drug' object."
        for d in drugs:
            if not isinstance(d, dict):
                return "each drug entry must be an object."
            for field in ("rxcui", "drug_name", "dose_text", "times_per_day", "dose_times"):
                if field not in d:
                    return f"drug entry missing required field {field!r}."
            if not isinstance(d["rxcui"], str) or not d["rxcui"].isdigit():
                return f"drug.rxcui must be a numeric RxCUI string, got {d.get('rxcui')!r}."
            if not isinstance(d["times_per_day"], int) or d["times_per_day"] <= 0:
                return "drug.times_per_day must be a positive integer."
            if not isinstance(d["dose_times"], list) or len(d["dose_times"]) != d["times_per_day"]:
                return (
                    "drug.dose_times must be a list whose length equals times_per_day "
                    "(DATA_CONTRACTS.md schedules_frequency_match)."
                )

    elif action == "update_timing":
        if not isinstance(payload.get("schedule_id"), str):
            return "update_timing requires a 'schedule_id' string."
        if "dose_times" in payload and "times_per_day" in payload:
            if len(payload["dose_times"]) != payload["times_per_day"]:
                return "dose_times length must equal times_per_day."

    elif action == "remove_drug":
        if not isinstance(payload.get("schedule_id"), str):
            return "remove_drug requires a 'schedule_id' string."

    elif action == "list":
        status = payload.get("status", "active")
        if status not in VALID_STATUSES + ["all"]:
            return f"list.status must be one of {VALID_STATUSES + ['all']}, got {status!r}."

    return None


def needs_user_confirmation(payload):
    """Strict `is True` -- a truthy-but-not-boolean value (e.g. the string
    "yes") must NOT satisfy this gate. ARCHITECTURE.md §5(d) requires
    *explicit* confirmation of the exact change; accepting near-enough
    values here would make that requirement negotiable by prompt phrasing.
    """
    return payload.get("user_confirmed") is not True


def needs_interaction_confirmation(payload):
    return payload.get("confirmed_interactions") is not True


def proposed_change_summary(action, payload):
    """What gets echoed back in a needs_confirmation response so the agent
    can show the user exactly what they're being asked to confirm.
    """
    if action == "create_from_extraction":
        return {"action": action, "drugs": payload.get("drugs")}
    if action == "add_drug":
        return {"action": action, "drug": payload.get("drug")}
    if action == "update_timing":
        return {
            "action": action,
            "schedule_id": payload.get("schedule_id"),
            "times_per_day": payload.get("times_per_day"),
            "dose_times": payload.get("dose_times"),
            "timing_notes": payload.get("timing_notes"),
        }
    if action == "remove_drug":
        return {"action": action, "schedule_id": payload.get("schedule_id")}
    return {"action": action}

# COMMAND ----------

# MAGIC %md
# MAGIC ## Register the deployed function

# COMMAND ----------

create_function_sql = f"""
CREATE OR REPLACE FUNCTION {CATALOG}.app.manage_schedule(
  patient_id STRING
    COMMENT 'UUID of the patient whose schedule is being read or changed.',
  action STRING
    COMMENT 'One of: create_from_extraction (initial write of one or more drugs from a confirmed prescription extraction -- payload: {{"drugs": [{{rxcui, drug_name, dose_text, times_per_day, dose_times, timing_notes?}}], "user_confirmed": true, "confirmed_interactions": true-only-if-a-prior-call-returned-blocked_pending_confirmation}}); add_drug (add one drug to an existing schedule -- payload: {{"drug": {{rxcui, drug_name, dose_text, times_per_day, dose_times, timing_notes?}}, "user_confirmed": true, "confirmed_interactions": same-rule-as-above}}); update_timing (change times_per_day/dose_times/timing_notes on an existing row -- payload: {{"schedule_id", "times_per_day"?, "dose_times"?, "timing_notes"?, "user_confirmed": true}}); remove_drug (soft-delete: sets status to stopped, never a hard delete -- payload: {{"schedule_id", "user_confirmed": true}}); list (read-only, no confirmation needed -- payload: {{"status"?: "active"|"stopped"|"all", default "active"}}).',
  payload STRING
    COMMENT 'JSON object, shape depends on action -- see the description of the action parameter for the exact schema per action.'
)
RETURNS STRING
LANGUAGE PYTHON
ENVIRONMENT (
  dependencies = '["requests==2.32.3"]',
  environment_version = 'None'
)
COMMENT 'Reads or writes a patient medication schedule. TWO-STEP CONFIRMATION FLOW, both enforced in code: (1) every mutating action (all except list) requires payload.user_confirmed=true, echoing the exact proposed change first if it is missing -- call once without it to show the user what would change, then call again with user_confirmed=true only after they explicitly agree. (2) add_drug and create_from_extraction additionally run a mandatory interaction check against the union of the new drug(s) and the current active drugs on that patient schedule BEFORE writing anything; if any interaction is found and payload.confirmed_interactions is not true, NO write happens and the interactions are returned for you to show the user -- call again with confirmed_interactions=true only after the user has seen the specific interaction(s) and explicitly agreed to proceed anyway. Never set user_confirmed or confirmed_interactions to true on your own initiative -- only after the user has actually said so in the conversation. Returns JSON: {{"status": "needs_confirmation", "proposed_change": {{...}}}} | {{"status": "blocked_pending_confirmation", "interactions": [...], "message": "..."}} | {{"status": "success", ...}} | {{"error": "..."}}.'
AS $$
import json
import os
import requests


VALID_ACTIONS = {VALID_ACTIONS!r}
VALID_STATUSES = {VALID_STATUSES!r}
CATALOG = {CATALOG!r}


def _validate_payload(action, payload):
    if action not in VALID_ACTIONS:
        return f"Unknown action {{action!r}}. Must be one of {{VALID_ACTIONS}}."
    if not isinstance(payload, dict):
        return "payload must be a JSON object."
    if action in ("create_from_extraction", "add_drug"):
        drugs = payload.get("drugs") if action == "create_from_extraction" else [payload.get("drug")]
        if action == "create_from_extraction":
            if not isinstance(drugs, list) or not drugs:
                return "create_from_extraction requires a non-empty 'drugs' array."
        else:
            if not isinstance(payload.get("drug"), dict):
                return "add_drug requires a 'drug' object."
        for d in drugs:
            if not isinstance(d, dict):
                return "each drug entry must be an object."
            for field in ("rxcui", "drug_name", "dose_text", "times_per_day", "dose_times"):
                if field not in d:
                    return f"drug entry missing required field {{field!r}}."
            if not isinstance(d["rxcui"], str) or not d["rxcui"].isdigit():
                return f"drug.rxcui must be a numeric RxCUI string, got {{d.get('rxcui')!r}}."
            if not isinstance(d["times_per_day"], int) or d["times_per_day"] <= 0:
                return "drug.times_per_day must be a positive integer."
            if not isinstance(d["dose_times"], list) or len(d["dose_times"]) != d["times_per_day"]:
                return "drug.dose_times must be a list whose length equals times_per_day."
    elif action == "update_timing":
        if not isinstance(payload.get("schedule_id"), str):
            return "update_timing requires a 'schedule_id' string."
        if "dose_times" in payload and "times_per_day" in payload:
            if len(payload["dose_times"]) != payload["times_per_day"]:
                return "dose_times length must equal times_per_day."
    elif action == "remove_drug":
        if not isinstance(payload.get("schedule_id"), str):
            return "remove_drug requires a 'schedule_id' string."
    elif action == "list":
        status = payload.get("status", "active")
        if status not in VALID_STATUSES + ["all"]:
            return f"list.status must be one of {{VALID_STATUSES + ['all']}}, got {{status!r}}."
    return None


def _needs_user_confirmation(payload):
    return payload.get("user_confirmed") is not True


def _needs_interaction_confirmation(payload):
    return payload.get("confirmed_interactions") is not True


def _proposed_change_summary(action, payload):
    if action == "create_from_extraction":
        return {{"action": action, "drugs": payload.get("drugs")}}
    if action == "add_drug":
        return {{"action": action, "drug": payload.get("drug")}}
    if action == "update_timing":
        return {{
            "action": action, "schedule_id": payload.get("schedule_id"),
            "times_per_day": payload.get("times_per_day"),
            "dose_times": payload.get("dose_times"),
            "timing_notes": payload.get("timing_notes"),
        }}
    if action == "remove_drug":
        return {{"action": action, "schedule_id": payload.get("schedule_id")}}
    return {{"action": action}}


try:
    try:
        payload_obj = json.loads(payload) if payload else {{}}
    except json.JSONDecodeError as e:
        return json.dumps({{"error": f"payload is not valid JSON: {{e}}"}})

    err = _validate_payload(action, payload_obj)
    if err:
        return json.dumps({{"error": err}})

    # Checked before any network call: a needs_confirmation response is a
    # pure echo of the caller's own payload and requires zero backend I/O.
    # Coupling it to a successful auth/token round trip would mean a
    # misconfigured deployment couldn't even show the user what a proposed
    # change looks like -- the one response shape that should always work.
    if action != "list" and _needs_user_confirmation(payload_obj):
        return json.dumps({{"status": "needs_confirmation", "proposed_change": _proposed_change_summary(action, payload_obj)}})

    host = os.environ.get("NEURORX_DATABRICKS_HOST")
    client_id = os.environ.get("NEURORX_SP_CLIENT_ID")
    client_secret = os.environ.get("NEURORX_SP_CLIENT_SECRET")
    warehouse_id = os.environ.get("NEURORX_SQL_WAREHOUSE_ID")
    lakebase_rest_endpoint = os.environ.get("NEURORX_LAKEBASE_REST_ENDPOINT")
    if not (host and client_id and client_secret and warehouse_id and lakebase_rest_endpoint):
        return json.dumps({{"error": "configuration error: missing one or more required service credentials/endpoints"}})

    token_resp = requests.post(
        f"https://{{host}}/oidc/v1/token",
        auth=(client_id, client_secret),
        data={{"grant_type": "client_credentials", "scope": "all-apis"}},
        timeout=10,
    )
    token_resp.raise_for_status()
    access_token = token_resp.json()["access_token"]
    auth_header = {{"Authorization": f"Bearer {{access_token}}"}}

    def lakebase_get(table, filters):
        params = "&".join(f"{{k}}=eq.{{v}}" for k, v in filters.items())
        resp = requests.get(f"{{lakebase_rest_endpoint}}/public/{{table}}?{{params}}", headers=auth_header, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def lakebase_post(table, row):
        # PostgREST returns inserted rows as a JSON array even for a single
        # insert (Prefer: return=representation) -- unwrapped here so callers
        # that insert one row at a time get back a single dict, not a
        # one-element list wrapped inside their own list comprehension.
        resp = requests.post(
            f"{{lakebase_rest_endpoint}}/public/{{table}}", headers={{**auth_header, "Content-Type": "application/json", "Prefer": "return=representation"}},
            json=row, timeout=10,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            raise RuntimeError(f"insert into {{table}} returned no row")
        return rows[0]

    def lakebase_patch(table, filters, updates):
        # Same array-wrapping as lakebase_post, plus: an empty result here
        # means the filter (which always includes patient_id) matched
        # nothing -- most likely a schedule_id that doesn't exist, or one
        # that belongs to a different patient. Surfaced explicitly rather
        # than crashing on rows[0] or silently reporting success.
        params = "&".join(f"{{k}}=eq.{{v}}" for k, v in filters.items())
        resp = requests.patch(
            f"{{lakebase_rest_endpoint}}/public/{{table}}?{{params}}", headers={{**auth_header, "Content-Type": "application/json", "Prefer": "return=representation"}},
            json=updates, timeout=10,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            raise LookupError(f"no {{table}} row matched {{filters}} -- wrong schedule_id, or it belongs to a different patient")
        return rows[0]

    def check_interactions(rxcui_list):
        # Every candidate is already confirmed numeric-only by _validate_payload
        # and by the schedules_rxcui_numeric check for anything read back from
        # the DB -- re-validated here anyway before string-building the SQL
        # array literal, since this is the actual injection boundary.
        safe = [r for r in rxcui_list if isinstance(r, str) and r.isdigit()]
        if len(safe) != len(rxcui_list):
            raise ValueError("non-numeric rxcui encountered before interaction check")
        array_literal = "ARRAY(" + ", ".join(f"'{{r}}'" for r in safe) + ")"
        statement = (
            f"SELECT rxcui_a, rxcui_b, drug_a, drug_b, severity, description, source "
            f"FROM {{CATALOG}}.app.check_interactions({{array_literal}})"
        )
        resp = requests.post(
            f"https://{{host}}/api/2.0/sql/statements",
            headers={{**auth_header, "Content-Type": "application/json"}},
            json={{"statement": statement, "warehouse_id": warehouse_id, "wait_timeout": "30s"}},
            timeout=35,
        )
        resp.raise_for_status()
        body = resp.json()
        cols = [c["name"] for c in body.get("manifest", {{}}).get("schema", {{}}).get("columns", [])]
        rows = body.get("result", {{}}).get("data_array", []) or []
        return [dict(zip(cols, row)) for row in rows]

    now_iso_fn = lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()

    if action == "list":
        status = payload_obj.get("status", "active")
        filters = {{"patient_id": patient_id}}
        if status != "all":
            filters["status"] = status
        rows = lakebase_get("schedules", filters)
        return json.dumps({{"status": "success", "schedules": rows}})

    # user_confirmed already checked above, before the token was minted.

    if action in ("create_from_extraction", "add_drug"):
        new_drugs = payload_obj.get("drugs") if action == "create_from_extraction" else [payload_obj.get("drug")]
        active = lakebase_get("schedules", {{"patient_id": patient_id, "status": "active"}})
        current_rxcuis = [row["rxcui"] for row in active]
        new_rxcuis = [d["rxcui"] for d in new_drugs]
        union_rxcuis = list(dict.fromkeys(current_rxcuis + new_rxcuis))

        interactions = check_interactions(union_rxcuis) if len(union_rxcuis) > 1 else []

        if interactions and _needs_interaction_confirmation(payload_obj):
            return json.dumps({{
                "status": "blocked_pending_confirmation",
                "interactions": interactions,
                "message": (
                    "Adding this drug may interact with medications the patient "
                    "is currently taking. Show these interactions to the user "
                    "and ask them to explicitly confirm before proceeding."
                ),
            }})

        created = []
        for d in new_drugs:
            row = lakebase_post("schedules", {{
                "patient_id": patient_id,
                "rxcui": d["rxcui"],
                "drug_name": d["drug_name"],
                "dose_text": d["dose_text"],
                "times_per_day": d["times_per_day"],
                "dose_times": d["dose_times"],
                "timing_notes": d.get("timing_notes"),
                "status": "active",
            }})
            created.append(row)
        return json.dumps({{"status": "success", "created": created}})

    if action == "update_timing":
        updates = {{"updated_at": now_iso_fn()}}
        for field in ("times_per_day", "dose_times", "timing_notes"):
            if field in payload_obj:
                updates[field] = payload_obj[field]
        row = lakebase_patch("schedules", {{"schedule_id": payload_obj["schedule_id"], "patient_id": patient_id}}, updates)
        return json.dumps({{"status": "success", "updated": row}})

    if action == "remove_drug":
        row = lakebase_patch(
            "schedules", {{"schedule_id": payload_obj["schedule_id"], "patient_id": patient_id}},
            {{"status": "stopped", "updated_at": now_iso_fn()}},
        )
        return json.dumps({{"status": "success", "stopped": row}})

    return json.dumps({{"error": f"unhandled action {{action!r}}"}})

except Exception as e:
    return json.dumps({{"error": f"unexpected error: {{type(e).__name__}}: {{e}}"}})
$$
"""

spark.sql(create_function_sql)
print(f"Registered {CATALOG}.app.manage_schedule")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Local test harness — real `psycopg` against any Postgres with the
# MAGIC ## DATA_CONTRACTS.md DDL applied
# MAGIC
# MAGIC This is Task 2.3 requirement #6's explicit ask: unlike the deployed
# MAGIC function above (which cannot use raw `psycopg` at all — see the warning
# MAGIC at the top of this file), this harness runs on a normal machine, so the
# MAGIC port restriction doesn't apply, and `psycopg` is exactly the right tool.
# MAGIC It exercises the pure logic functions from the cell above directly
# MAGIC (`validate_payload`, `needs_user_confirmation`, etc.) plus its own
# MAGIC `psycopg`-based I/O — not the deployed function's HTTPS-based I/O, which
# MAGIC cannot be exercised outside an actual Databricks workspace.
# MAGIC
# MAGIC **CREATE-stub note:** the Lakebase `patients`/`schedules`/`dose_events`
# MAGIC tables don't exist anywhere yet — Task 1.4 (still broken, see `CLAUDE.md`)
# MAGIC was meant to seed synthetic data into them and the actual `neurorx-oltp`
# MAGIC schema DDL itself is Phase 3 scope (`ARCHITECTURE.md` §7). To run this
# MAGIC harness against a real local Postgres, first apply the exact DDL from
# MAGIC `DATA_CONTRACTS.md` §6.1–§6.2 (the `patients` and `schedules` table
# MAGIC definitions and their `CONSTRAINT` blocks, verbatim) — `dose_events`
# MAGIC (§6.3) is not exercised by this tool and isn't required for this harness.

# COMMAND ----------

import os
import uuid

LAKEBASE_DDL_STUB = """
-- Exact DDL from DATA_CONTRACTS.md §6.1-6.2. Apply this to any local/test
-- Postgres before running the harness below.
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- for gen_random_uuid()

CREATE TABLE IF NOT EXISTS patients (
    patient_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name TEXT NOT NULL,
    caregiver_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT patients_display_name_present CHECK (length(trim(display_name)) > 0)
);

CREATE TABLE IF NOT EXISTS schedules (
    schedule_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id UUID NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
    rxcui TEXT NOT NULL,
    drug_name TEXT NOT NULL,
    dose_text TEXT NOT NULL,
    times_per_day INTEGER NOT NULL,
    dose_times TIME[] NOT NULL,
    timing_notes TEXT,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT schedules_status_valid    CHECK (status IN ('active','stopped')),
    CONSTRAINT schedules_rxcui_numeric   CHECK (rxcui ~ '^[0-9]+$'),
    CONSTRAINT schedules_times_positive  CHECK (times_per_day > 0),
    CONSTRAINT schedules_frequency_match CHECK (cardinality(dose_times) = times_per_day),
    CONSTRAINT schedules_updated_after   CHECK (updated_at >= created_at)
);
"""

if __name__ == "__main__":
    print(LAKEBASE_DDL_STUB)
    print("^-- apply this DDL to a local/test Postgres before running the tests below.\n")

    try:
        import psycopg
    except ImportError:
        raise SystemExit(
            "psycopg not installed. Run: pip install 'psycopg[binary]'"
        )

    # Reuses config.py's existing field names -- this IS the psycopg-shaped
    # use case those fields were designed for (see the warning at the top of
    # this file for why the *deployed* function needs different env vars).
    conn = psycopg.connect(
        host=os.environ.get("LAKEBASE_HOST", "localhost"),
        port=int(os.environ.get("LAKEBASE_PORT", "5432")),
        dbname=os.environ.get("LAKEBASE_DB", "databricks_postgres"),
        user=os.environ.get("LAKEBASE_USER", "postgres"),
        password=os.environ.get("LAKEBASE_PASSWORD", ""),
        autocommit=True,
    )

    with conn.cursor() as cur:
        cur.execute(LAKEBASE_DDL_STUB)

        # --- fixture: one patient, no drugs yet ---
        patient_id = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO patients (patient_id, display_name) VALUES (%s, %s)",
            (patient_id, "Test Patient"),
        )

        def insert_schedule(rxcui, drug_name, dose_text="10mg", times_per_day=1, dose_times=None, status="active"):
            dose_times = dose_times or ["08:00"] * times_per_day
            cur.execute(
                """INSERT INTO schedules
                   (patient_id, rxcui, drug_name, dose_text, times_per_day, dose_times, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING schedule_id""",
                (patient_id, rxcui, drug_name, dose_text, times_per_day, dose_times, status),
            )
            return cur.fetchone()[0]

        # --- Test 1: pure validation logic ---
        assert validate_payload("add_drug", {"drug": {"rxcui": "11289", "drug_name": "warfarin",
            "dose_text": "5mg", "times_per_day": 1, "dose_times": ["08:00"]}}) is None
        assert validate_payload("add_drug", {"drug": {"rxcui": "not-a-number"}}) is not None
        assert validate_payload("update_timing", {"schedule_id": "x", "times_per_day": 2, "dose_times": ["08:00"]}) is not None
        print("Test 1 (payload validation): PASSED")

        # --- Test 2: confirmation-gate logic ---
        assert needs_user_confirmation({}) is True
        assert needs_user_confirmation({"user_confirmed": "yes"}) is True  # strict True only
        assert needs_user_confirmation({"user_confirmed": True}) is False
        assert needs_interaction_confirmation({"confirmed_interactions": True}) is False
        print("Test 2 (confirmation gates): PASSED")

        # --- Test 3: schedules_frequency_match constraint actually enforced by Postgres ---
        try:
            cur.execute(
                """INSERT INTO schedules (patient_id, rxcui, drug_name, dose_text, times_per_day, dose_times, status)
                   VALUES (%s, '6809', 'metformin', '500mg', 2, %s, 'active')""",
                (patient_id, ["08:00"]),  # length 1, times_per_day=2 -- must be rejected
            )
            raise AssertionError("expected a constraint violation, insert succeeded")
        except psycopg.errors.CheckViolation:
            conn.rollback()
            print("Test 3 (schedules_frequency_match enforced by DB): PASSED")

        # --- Test 4: end-to-end insert + read-back matches DATA_CONTRACTS.md shape ---
        sid = insert_schedule("11289", "warfarin", dose_text="5mg", times_per_day=1, dose_times=["20:00"])
        cur.execute("SELECT rxcui, drug_name, status, times_per_day, dose_times FROM schedules WHERE schedule_id = %s", (sid,))
        row = cur.fetchone()
        assert row[0] == "11289" and row[1] == "warfarin" and row[2] == "active"
        print("Test 4 (insert + read-back): PASSED ->", row)

        # cleanup
        cur.execute("DELETE FROM patients WHERE patient_id = %s", (patient_id,))

    conn.close()
    print("\nALL LOCAL TESTS PASSED")
