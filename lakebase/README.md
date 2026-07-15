# Lakebase schema and sync config

Schema DDL for the Lakebase Postgres instance `neurorx-oltp` (created in Phase 0):
- `patients` — patient records
- `schedules` — prescription schedules per patient
- `dose_events` — timestamped adherence events (marked taken/skipped/missed)

Sync config to replicate `dose_events` and `schedules` back to Delta `neurorx.gold` for analytics and Genie.

Phase 3 deliverable. See ARCHITECTURE.md §7.
