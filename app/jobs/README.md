# Reminders job (Task 3.7)

`reminders_job.py` — every run, finds doses due in the next 60 minutes
across all active schedules, inserts a `notifications` row for each
(idempotent — reruns never duplicate a reminder for the same dose slot),
and pre-creates the corresponding `planned` `dose_events` row. The Today
view's reminder banner (Task 3.5, `app/db.py`'s `list_unacknowledged_reminders()`)
polls the `notifications` table this job writes.

**Out of scope by design: no SMS, no push notification, nothing leaves
Lakebase.** The production path for actually notifying a patient is
**Twilio SMS/push**, named as such in `ARCHITECTURE.md` §7 — deliberately
not built for this hackathon. This job writes an in-app-pollable row; a
real deployment would add a Twilio call (or similar) as a separate
downstream step reading unacknowledged `notifications` rows, not bolted
onto this file. Don't add a Twilio call here without first re-reading why
it isn't already here — it's a scope decision, not an oversight.

---

## Schedule this as a Lakeflow Job, every 15 minutes, serverless

**Verified against current Databricks Jobs docs this session** (not
recalled from an older UI): serverless compute is directly supported for
Python-script task types, and is selected by default when creating a job
with one — confirmed against current docs, not assumed to still work the
way an older Jobs UI did.

1. In the Databricks workspace, go to **Workflows** → **Jobs** → **Create
   Job**.
2. Name the job (e.g. `neurorx-reminders-job`).
3. Add a task:
   - **Task name**: `run_reminders`
   - **Type**: `Python script`
   - **Source**: this file, `app/jobs/reminders_job.py` (upload to a
     workspace path, or point at the repo if using Databricks Repos /
     Git folders)
   - **Compute**: leave as **Serverless** — confirmed current default for
     this task type, no cluster to configure.
   - **Environment variables**: the same `LAKEBASE_HOST`/`LAKEBASE_DB`/
     `LAKEBASE_USER`/`LAKEBASE_PASSWORD` this project's other standalone
     scripts use (`app/config.py`'s `Settings`) — set via job-level
     environment variables or a secret scope, never hardcoded.
4. In the **Job details** pane, find **Schedules & Triggers** → **Add
   trigger** → select **Scheduled** as the trigger type (confirmed against
   current docs — this exact navigation path is current).
5. Set the interval to **every 15 minutes**. The Jobs scheduling UI offers
   both a simple period picker (e.g. "Every 15 minutes") and an "Advanced"
   raw-cron toggle for Quartz cron syntax if you need one — **the simple
   picker's exact wording was not independently re-confirmed this
   session**, flagged here rather than asserted as verified; if it's not
   present in your workspace version, the equivalent Quartz cron expression
   is `0 */15 * * * ?` (Databricks Jobs uses Quartz cron, which has a
   leading seconds field — not the 5-field cron `*/15 * * * *` a Unix
   crontab would use; using the wrong dialect silently produces the wrong
   schedule with no error).
6. Save. The job now runs every 15 minutes with a 60-minute lookahead
   window (`reminders_job.py`'s own `LOOKAHEAD_MINUTES`), so every dose
   slot gets at least 3-4 chances to generate a reminder before it's due —
   deliberate overlap, not redundant: it means a single missed/delayed job
   run doesn't silently skip a reminder, since the idempotent insert
   (`notifications_slot_unique`, `lakebase/schema.sql`) makes re-finding
   the same due-soon dose on a later run harmless.

## Verifying it worked

```sql
-- In the Lakebase SQL editor:
SELECT * FROM notifications ORDER BY created_at DESC LIMIT 20;
```

Each run's log output (visible in the job run's stdout) prints how many
doses were found due-soon and processed — `reminders_job.main()`'s own
`print()` calls, not a separate logging setup, since a Databricks Job run's
stdout is already captured and viewable per-run without extra
configuration.
