# NeuroRx AI — Demo Video Script (5:00 hard limit)

> ⚠️ **Source note, read before using this file.** The task that produced this script
> asked to "adapt the plan's §8 video script" from `pharma-assist-build-plan.md`. **That
> file does not exist anywhere in this repo** — only `ARCHITECTURE.md` (which is derived
> from it) is checked in, and `ARCHITECTURE.md` has no §8 video script of its own (its §8
> is the Decisions Log; the submission gate and phase map only *mention* that a video is
> required, at §7/§275). So this is **not an adaptation of an existing script** — it's an
> original 5:00 structure written from `ARCHITECTURE.md`'s real user stories (§1), safety
> architecture (§5), and evaluation targets (§6), plus the actual, currently-built app UI
> (`app/app.py`, `app/views/*.py`). If a real `pharma-assist-build-plan.md` §8 turns up,
> reconcile beat order/timing against it before recording.

> ⚠️ **Three things in this script are placeholders that MUST be filled in before
> recording — do not read this script on camera as-is:**
> 1. **`<APP_URL>`** — no app is deployed yet (Phase 3 exit checkpoint not reached per
>    `CLAUDE.md`'s status tracking). Replace every occurrence once deployed.
> 2. **Eval numbers in Beat 8** — no evaluation run has actually been executed yet
>    (`evals/02_run_evaluation.py`, Task 4.4, is written but unrun — see
>    `docs/submission_checklist.md` gate 4/5). The numbers below are the **targets**
>    from `ARCHITECTURE.md` §6, not measured results. **Swap in real numbers from the
>    actual MLflow run before recording**, or rephrase to avoid stating unearned results.
> 3. **Every commit referenced as "in the repo" or "on GitHub"** assumes the working
>    tree is committed and pushed. As of this writing, only the original scaffold
>    commit is on the remote — everything else is local and uncommitted. Push first.

**Total runtime: 5:00 (300s). Word budget throughout is 140 words/minute (2.33 words/sec).**

---

## Beat-by-beat script

| # | Timestamp | Dur. | Shot list | Click path (tab → element → patient) | Spoken script | Words (budget / actual) |
|---|---|---|---|---|---|---|
| 1 | 0:00–0:20 | 20s | App: Chat tab, safety banner visible at top | App already open on `<APP_URL>`, sidebar Patient ID pre-filled with **Margaret Demo** (`12345678-...`) | "Half of chronic-illness patients don't take their medications as prescribed. It costs the US health system up to two hundred eighty-nine billion dollars a year, and roughly one hundred twenty-five thousand lives. NeuroRx AI is an organizational assistant, not medical advice — and it never forgets that." | 47 / 46 ✅ |
| 2 | 0:20–0:50 | 30s | Architecture diagram (`ARCHITECTURE.md` §2 Mermaid, rendered full-screen) | Switch to a browser tab showing the rendered `ARCHITECTURE.md` diagram, or a static export of it | "Everything starts from real data: openFDA drug labels, RxNorm for drug identity, DDInter for interaction severity, flowing through a medallion pipeline into Unity Catalog. A single supervisor agent calls four governed tools — search labels, check interactions, manage the schedule, and pull adherence stats. Lakebase holds transactional state; it syncs automatically to Delta for analytics. Every tool call is traced in MLflow." | 70 / 61 ✅ |
| 3 | 0:50–1:25 | 35s | App: Chat tab → "📷 Add a Prescription" expander → extraction confirmation card | Chat tab → click **"📷 Add a Prescription (Photo or Text)"** expander → paste sample text → click **"📄 Extract Prescription"** → confirmation table appears → click **"✓ Confirm & Add"** | "Say Margaret's doctor just wrote her a new prescription. In the Chat tab, she opens 'Add a Prescription,' pastes the text, and hits Extract. The agent reads it, resolves the drug against RxNorm, and shows a confirmation table — nothing is saved yet. Margaret checks it, hits Confirm and Add, and only then does the schedule actually change." | 82 / 57 ✅ |
| 4 | 1:25–2:15 | 50s | App: Chat tab → blocked-interaction confirmation card (severity + sources) — **centerpiece beat** | Chat tab → type `add ibuprofen to my schedule` (Margaret is already on warfarin, per the fixed demo cohort) → agent auto-calls `check_interactions` → red **"🚨 Drug Interaction Alert"** card appears with MAJOR severity + DDInter source → point at **Confirm/Cancel** buttons without clicking Confirm | "Now watch the safety core. Margaret asks to add ibuprofen — she's already on warfarin. The moment that request comes in, manage_schedule automatically calls check_interactions first, in code, not because the prompt asked nicely. It's a deterministic SQL lookup against a frozen interaction table — no LLM is guessing here. The result: a major-severity warfarin-ibuprofen interaction, sourced from DDInter, blocks the write outright. The UI shows the real verdict — not the model's paraphrase — with Confirm and Cancel buttons. This is the single most important thirty seconds of this demo: the LLM never decided this was dangerous. The table did." | 117 / 97 ✅ |
| 5 | 2:15–3:00 | 45s | App: Today tab → dose checklist + countdown; App: Chat tab → cited answer with expanded citation chip | Today tab → click **"Taken ✓"** on the morning metformin row → note the "⏰ Next Dose" countdown card → switch to Chat tab → type `what if I miss my evening metformin dose?` → click the resulting **citation chip expander** | "In the Today tab, Margaret sees her doses grouped by time of day, marks her morning metformin taken with one tap, and sees a live countdown to her next dose. Back in Chat, she asks what happens if she misses her evening metformin. The answer comes straight from the FDA label — and every clinical sentence ends in a citation chip. Click it, and you see the actual label text and the set ID it came from. If the label doesn't cover something, the agent says so and points her to her pharmacist — it never fills the gap." | 105 / 97 ✅ |
| 6 | 3:00–3:35 | 35s | App: Dashboard tab → header stat cards, adherence-by-drug bar, calendar heatmap; toggle to Caregiver panel | Dashboard tab → point at header metrics (Overall Adherence, Current Streak, Most-Missed Drug = metformin, Most-Missed Time = evening) → scroll to bar chart + heatmap → click **"👨‍👩‍👧 Caregiver Mode"** toggle → Genie panel appears | "The Dashboard turns Margaret's history into something a caregiver can actually read: overall adherence, current streak, which drug and time of day she misses most — metformin, evenings, exactly the pattern the synthetic cohort was built to demonstrate. Flip on Caregiver Mode and her daughter can ask Genie plain-language questions about the same data, no SQL required." | 82 / 56 ✅ |
| 7 | 3:35–4:20 | 45s | MLflow trace view (tool-call spans); `guardrail_blocks` Delta table (SQL editor or notebook cell) | Switch to Databricks workspace tab → MLflow experiment → open the trace for the Beat 5 citation answer → show span tree (search_drug_labels → citation) → switch to a SQL editor cell running `SELECT * FROM neurorx.app.guardrail_blocks ORDER BY blocked_at DESC LIMIT 5` (or Delta equivalent per F2's final home) | "Two more things prove this isn't just a prompt wrapper. First, MLflow traces every single response — here's the trace behind that citation answer, showing the exact tool calls and their arguments, end to end. Second, after the model generates anything, a lightweight guardrail scans it for uncited dosage language before it ever reaches Margaret. If it catches one, it blocks the response and logs the attempt to this guardrail_blocks table — an append-only safety record you can query, not a black box you have to trust." | 105 / 85 ✅ |
| 8 | 4:20–5:00 | 40s | Eval results table (MLflow run summary or README table); final title card with repo/demo links | Switch to MLflow run summary page, or the results table in `README.md` §Evaluation, once real numbers exist → cut to a closing title card: product name, repo URL, demo URL | "All of this is measured, not just claimed: a sixty-case MLflow evaluation harness scores safety, interaction detection, and groundedness against fixed targets. **[REPLACE: state the actual measured percentages here once `evals/02_run_evaluation.py` has run — do not read target numbers as if they were results.]** That's NeuroRx AI: deterministic safety core, cited answers, and a measured eval, built end-to-end on Databricks. Thanks for watching." | 93 / 60 (excl. bracketed instruction) ✅ |

**Word-count check summary:** all 8 beats land under their 140-wpm budget (46–97 words
actual against 47–117 word budgets) — **no beat is over budget**, but beats 4, 5, and 7
are the tightest (within 10–20 words of budget) and are also the beats with the most
on-screen action (waiting for a card to render, a chart to load, a trace to open) —
rehearse these three with a stopwatch, not just a word count, since UI load time eats
into spoken time that a script alone won't show.

---

## Rehearsal checklist

Run through, in order, the day of recording — not the week before:

- [ ] **Pre-warm the serving endpoint.** Send one throwaway chat request to
  `neurorx-agent` at least 5 minutes before recording so Beat 4/5's response isn't
  eaten by a cold-start delay on camera.
- [ ] **Pre-load Margaret Demo.** Confirm the sidebar Patient ID field defaults to
  `12345678-1234-1234-1234-123456789012` and that her cohort (metformin, lisinopril,
  warfarin, atorvastatin; 44% adherence with the metformin-evening miss pattern) is
  actually present in Lakebase — re-run `lakebase/07_load_cohort.py`'s Margaret Demo
  assertion if in doubt.
- [ ] **Pipeline freshly run.** Re-run the Lakeflow medallion pipeline and Vector Search
  sync within 24h of recording so Beat 5's citation isn't pointing at a stale chunk_id.
- [ ] **Genie sample questions verified.** Actually ask the Beat 6 caregiver question
  against the live Genie space before recording — confirm it returns a sensible answer,
  not a generic "I don't understand."
- [ ] **Guardrail demo cell ready.** Have the Beat 7 `guardrail_blocks` query saved and
  tested in a notebook cell beforehand — don't type SQL live on camera.
- [ ] **Screen recorder settings.** 1080p minimum, 30fps, system audio + mic on separate
  tracks (so a bad take can be re-voiced without re-recording screen), cursor
  highlighting on, notifications/Slack/email fully silenced.
- [ ] **Recorded backup per beat.** Record each beat's screen action **twice** —
  once as the "live" take, once as a backup clip — before combining into the final
  edit. If Beat 4's interaction card is slow to render on the live take, cut to the
  backup clip rather than re-recording the whole 5:00 in one pass.
- [ ] **Full dry run with a stopwatch**, end to end, at least once before the take you
  intend to keep. If total runtime exceeds 5:00, cut from Beat 2 (architecture) or
  Beat 6 (dashboard) first — Beat 4 (interaction firewall) and Beat 7 (guardrail +
  trace) are the two beats `ARCHITECTURE.md` §5/§9 name as never-cut.

---

## Contingency table

| Beat | Failure mode | Live fallback |
|---|---|---|
| 1 | Banner/app doesn't load in time | Cut immediately to the pre-recorded Beat 1 backup clip; don't narrate a loading spinner |
| 2 | Mermaid diagram fails to render in the browser tab | Have a static PNG/SVG export of the same diagram open in a second tab as the fallback shot |
| 3 | Extraction call times out or returns a bad parse | Cut to the backup clip recorded during rehearsal; do not attempt a live retry on camera |
| 4 | Interaction card doesn't appear (endpoint cold, or a code regression) | This is the never-cut beat — use the backup clip without hesitation; if no backup exists, do not record this take live until one does |
| 5 | Citation chip has no chunk_id, or Today tab shows stale data | Switch to the backup clip; if neither works, re-run the pipeline (see rehearsal checklist) before attempting again |
| 6 | Genie panel shows the "not configured" fallback card instead of an answer | Skip the Genie sub-beat live, narrate over the static dashboard charts instead, and note in the video description that Genie is Beta/optional per `ARCHITECTURE.md`'s own cut list |
| 7 | MLflow trace hasn't finished flushing, or the SQL cell errors | Use the backup clip recorded in rehearsal; this is the second never-cut beat — do not skip it, substitute the recording instead |
| 8 | Real eval numbers aren't ready by recording day | Do **not** state target numbers as results. Re-script this beat to say "targets are 100% safety, 100% interaction detection, ≥90% groundedness — full results and the reproduction commands are in the repo," which is true regardless of whether the run has completed |

---

*Companion file:* [`docs/submission_checklist.md`](submission_checklist.md) *tracks whether
this video (and everything else) is actually ready for the Devpost gate.*
