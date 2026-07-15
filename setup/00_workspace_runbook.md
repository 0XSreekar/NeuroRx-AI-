# NeuroRx AI — Workspace setup runbook

Phase 0 (Architecture & Foundations). Companion to [`01_uc_setup.sql`](01_uc_setup.sql) and the canonical [`ARCHITECTURE.md`](../ARCHITECTURE.md).

**Scope:** this runbook gets the workspace ready — it verifies access to the pieces Phase 1+ will build on, and creates the one piece of infrastructure Phase 0 owns (the Lakebase instance itself). It does **not** create Vector Search indexes, Lakebase schemas/tables, or Genie spaces — those are Phase 1/3/5 work per `ARCHITECTURE.md` §7.

**Edition assumption:** Databricks Free Edition — serverless-only, no classic compute, no Account Console. Every step below is written for that edition specifically; the quotas cited are Free Edition quotas, not general-purpose ones.

**If the UI has moved:** trust your screen over this document and note the discrepancy — this is exactly the kind of content that drifts. Every claim below is sourced; check the linked doc if something doesn't match what you see.

---

## 1. Verify the serverless SQL warehouse

Free Edition does not let you create a SQL warehouse — you get **one pre-created warehouse, capped at `2X-Small`**, and that's it.[^1] So this step is verification, not creation.

1. Left sidebar → **SQL Warehouses**.
2. Confirm one warehouse is listed. If its state is **Stopped**, click it and click **Start** — it will serve as the compute for `01_uc_setup.sql` and every SQL notebook after it.
3. Note its name; you'll pick it as the compute target when attaching a notebook.

You cannot add a second warehouse on this edition — don't spend time looking for a "Create warehouse" button.

---

## 2. Verify Foundation Model API access and record the Claude endpoint name

`ARCHITECTURE.md` §3 specifies **Claude via Databricks Foundation Model APIs** as the supervisor agent's LLM. These are Databricks-hosted, pay-per-token endpoints — no external Anthropic API key involved.

1. Left sidebar → **Serving**.
2. Look for the system-created pay-per-token endpoints; filter or scan for names prefixed `databricks-claude-`.
3. **Record the exact endpoint name(s) you see** — do not hardcode one from this doc into agent code without checking, since Databricks rotates which model versions are live. As of this writing (per current Databricks docs, July 2026) the available Claude endpoints are:[^2]

   | Endpoint name | Model |
   |---|---|
   | `databricks-claude-opus-4-8` | Claude Opus 4.8 |
   | `databricks-claude-opus-4-7` | Claude Opus 4.7 |
   | `databricks-claude-opus-4-6` | Claude Opus 4.6 |
   | `databricks-claude-opus-4-5` | Claude Opus 4.5 |
   | `databricks-claude-opus-4-1` | Claude Opus 4.1 |
   | `databricks-claude-sonnet-5` | Claude Sonnet 5 |
   | `databricks-claude-sonnet-4-6` | Claude Sonnet 4.6 |
   | `databricks-claude-sonnet-4-5` | Claude Sonnet 4.5 |
   | `databricks-claude-sonnet-4` | Claude Sonnet 4 |
   | `databricks-claude-haiku-4-5` | Claude Haiku 4.5 |
   | `databricks-claude-fable-5` | Claude Fable 5 |

   All support text input; the Opus/Sonnet/Haiku rows also accept image input (relevant to the prescription-photo extraction flow in §2 of `ARCHITECTURE.md`). **Note:** `databricks-claude-sonnet-5` does not accept `temperature`, `top_p`, or `top_k` — requests with those params get a 400 error; keep that in mind when the Phase 2 agent code sets sampling parameters.[^2]

4. **Confirm it actually answers a query** (existence in the list doesn't guarantee your quota allows it): left sidebar → **Playground**, pick a `databricks-claude-*` endpoint from the model dropdown, send a one-line test message, confirm you get a response.
5. **Recommendation for the supervisor agent:** `databricks-claude-sonnet-5` — Anthropic's current flagship Sonnet, tuned for agentic/tool-calling workloads, and the best cost/capability balance for a hackathon budget. Confirm it's present in your workspace's Serving list before locking it into Phase 2 code; fall back to the next Sonnet or Opus row if not.

---

## 3. Verify Vector Search (AI Search) availability

`ARCHITECTURE.md` §3 specifies Vector Search over FDA label chunks for grounded retrieval. Databricks has renamed this product **AI Search**; you'll see both names in the UI and docs for a while.

1. Left sidebar → **Compute** → **AI Search** tab.
2. Confirm the tab loads and you can see the (empty) endpoints list. Free Edition quota: **one AI Search endpoint, one search unit**, and only **`DELTA_SYNC`** index mode — Direct Vector Access is not supported.[^1] That's compatible with `ARCHITECTURE.md`'s design (the Vector Search index syncs off the `silver` Delta table), so no architecture change is needed — just don't plan on more than one endpoint or a self-managed index.
3. **Do not create the endpoint yet.** Phase 1 creates it against the actual `silver` label-chunk table; creating an empty one now just burns the one-endpoint quota on nothing.

---

## 4. Create the Lakebase database instance `neurorx-oltp`

`ARCHITECTURE.md` §4 fixes the instance name as `neurorx-oltp`. Free Edition quota is **one Lakebase project per account**[^1] — this is the only Lakebase instance this project (or anything else in your account) will get, so get the name right the first time.

As of March 12, 2026, new Lakebase instances are created as **Autoscaling** projects (autoscaling compute, scale-to-zero, branching, instant restore) rather than the older fixed-capacity **Provisioned** type; existing Provisioned instances are being migrated automatically starting June 2026.[^3] Since you're creating a new instance today, you'll get the Autoscaling flow.

1. Top-right **Apps** icon (grid icon) → **Lakebase Postgres**.
2. Click **Create database instance**.
3. Fill in:
   - **Name:** `neurorx-oltp` — exactly this; it's referenced verbatim in `ARCHITECTURE.md` and will be referenced again in Phase 3 sync config. (Naming rule: 1–63 characters, must start with a letter, letters/numbers/hyphens only, no double hyphens.)[^3]
   - **Capacity:** leave the default (2 CU) — this is a hackathon demo workload, not production traffic.
   - **Serverless usage policy:** optional; skip unless your account has one you need for billing attribution.
4. Leave **Advanced settings** (HA, point-in-time recovery, copy-on-write cloning) at defaults — not needed for a solo demo build, and HA in particular would spend compute budget you don't need to spend yet.
5. Click **Create**.

**This step creates the instance only.** The `patients` / `schedules` / `dose_events` schema, and the sync to Delta gold, are Phase 3 work (`ARCHITECTURE.md` §7) — don't run DDL against it yet.

---

## 5. Find your workspace URL and create a personal access token

You'll need both to point the Databricks CLI (and any local Python/SDK tooling) at this workspace.

**Workspace URL:** visible in your browser's address bar while logged into the workspace, in the form `https://dbc-xxxxxxxx-xxxx.cloud.databricks.com` (or `https://<workspace-id>.cloud.databricks.com`, depending on your deployment). Copy it now — you'll paste it into `databricks configure` or a `.databrickscfg` profile shortly.[^4]

**Personal access token:**[^4]

1. Top-right user icon → **Settings**.
2. **Developer** tab.
3. Next to **Access tokens**, click **Manage**.
4. Click **Generate new token**.
5. Give it a name you'll recognize later (e.g. `neurorx-ai-local-cli`).
6. Set a **lifetime** — don't leave it never-expiring by default; a lifetime that comfortably outlasts the hackathon's project period (e.g. 90 days) is enough and is better hygiene for a token that will live in a local `.databrickscfg` file.
7. Under scope, choose **Other APIs** (not "BI Tools" — that's scoped for Tableau/Power BI connections, not CLI/SDK use).
8. Click **Generate**.
9. **Copy the token immediately** — it's shown once. Store it in your local `.databrickscfg` (`~/.databrickscfg`), not in any file that goes into the `neurorx-ai` git repo.
10. Click **Done**.

PATs are labeled "legacy" in current Databricks docs in favor of OAuth for production service-to-service auth, but remain the documented path for local CLI/SDK use and are what this runbook uses.[^4]

---

## Exit checkpoint

Before moving to Phase 1:

- [ ] SQL warehouse confirmed running, name noted
- [ ] `01_uc_setup.sql` run successfully — `neurorx` catalog, five schemas, and `neurorx.bronze.raw_files` volume all exist (verified via the `SHOW`/`DESCRIBE` cells in that notebook)
- [ ] At least one `databricks-claude-*` endpoint confirmed live via a Playground test message; endpoint name recorded for Phase 2
- [ ] AI Search tab confirmed accessible (endpoint **not** yet created)
- [ ] Lakebase instance `neurorx-oltp` created and visible in **Apps → Lakebase Postgres**
- [ ] Workspace URL recorded; personal access token generated and stored locally (not committed)

---

[^1]: [Databricks Free Edition limitations](https://docs.databricks.com/aws/en/getting-started/free-edition-limitations) — SQL warehouse cap, AI Search endpoint/search-unit quota and DELTA_SYNC-only restriction, one Lakebase project per account.
[^2]: [Databricks-hosted foundation models available in Foundation Model APIs](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/supported-models) — verbatim endpoint names and input-modality support for each Claude model; Sonnet 5 sampling-parameter restriction.
[^3]: [Create and manage a database instance](https://docs.databricks.com/aws/en/oltp/instances/create/) — Autoscaling vs. Provisioned transition date, UI steps, instance name constraints.
[^4]: [Authenticate with Databricks personal access tokens (legacy)](https://docs.databricks.com/aws/en/dev-tools/auth/pat) — PAT generation steps, workspace URL format.
