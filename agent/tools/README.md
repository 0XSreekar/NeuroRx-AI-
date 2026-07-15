# UC-function tools

Four Unity Catalog functions serving as the supervisor agent's tools:

1. `manage_schedule(patient_id, action, payload)` — CRUD on Lakebase schedules table
2. `search_drug_labels(rxcui, section, query)` — Vector Search retrieval with citations
3. `check_interactions(rxcui_list)` — deterministic SQL lookup against interaction_pairs
4. `get_adherence_stats(patient_id, window)` — SQL over dose_events

Each is registered in `neurorx.app` schema. UC functions are discoverable, governed, and reusable — adding a tool is registering a function, zero rewrite.

Phase 2 deliverable. See ARCHITECTURE.md §7.
