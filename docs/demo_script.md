# NeuroRx AI — Demo Video Script (5:00 hard limit)

**Source:** beat structure and timings are taken verbatim from `pharma-assist-build-plan.md`
§8. Product name, click paths, shot list, word budgets, rehearsal checklist, and
contingencies are this file's additions. Where the plan says "Pharma Assist," this script
says **NeuroRx AI** — the rename is already recorded in `ARCHITECTURE.md` §8's decisions
log ("The plan uses the working title 'Pharma Assist'... Superseded by the fixed naming
conventions in §4"), not a deviation introduced here.

> ⚠️ **Two placeholders MUST be resolved before recording — do not read this on camera as-is:**
>
> 1. **`<APP_URL>`** — no app is deployed yet (Phase 3 exit checkpoint not reached).
> 2. **Beat 6's eval numbers** — `evals/02_run_evaluation.py` has never been run. The
>    bracketed line in Beat 6 is an instruction, not a script. **State measured results or
>    say nothing.** Plan §11 pitfall #2 is about *having* eval numbers; reading unearned
>    ones out loud is a worse failure than omitting them.
>
> Plan §11 pitfall #6 ("stats without sources") applies to Beat 1: the $289B figure is the
> upper bound from Viswanathan et al., *Annals of Internal Medicine* 157(11), 2012 — cited
> in `README.md`. The plan rounds it to $290B; this script uses the sourced figure.

**Runtime 5:00 (300s). Word budget at 140 wpm = 2.33 words/sec ⇒ 700 words total.**

---

## Beat-by-beat

### Beat 1 — Hook · 0:00–0:20 (20s)

**Shot:** App open on `<APP_URL>`, Chat tab, safety banner visible at top. Sidebar Patient
ID pre-filled with Margaret Demo (`12345678-1234-1234-1234-123456789012`).
**Click path:** none — static opening shot.

> Half of chronic-illness patients don't take their medications as prescribed. It costs the
> US health system up to two hundred eighty-nine billion dollars a year, and roughly one
> hundred twenty-five thousand lives. Meet NeuroRx AI.

**Words: 35 / 46 budget** ✅

---

### Beat 2 — Wow moment: prescription → confirmation → schedule · 0:20–1:10 (50s)

**Shot:** Chat tab → "📷 Add a Prescription" expander → extraction confirmation table →
schedule appears.
**Click path:** Chat tab → click **"📷 Add a Prescription (Photo or Text)"** → upload the
rehearsed prescription image (or paste the sample text) → click **"📄 Extract
Prescription"** → confirmation table renders → click **"✓ Confirm & Add"**.

> This is Margaret. She's sixty-two, and she takes four prescriptions daily. Her doctor just
> added a new one. Instead of typing it out, she photographs the prescription label. NeuroRx
> reads it with Claude's vision model, pulls out the drug name, strength, and frequency, then
> resolves it against RxNorm to get a canonical drug identifier. And here's the important
> part — nothing is saved yet. She gets a confirmation card showing exactly what was
> extracted, and she can edit any field before it's written. Only when she taps Confirm does
> the schedule actually change. That confirmation step isn't a nicety; it's enforced in the
> tool code.

**Words: 104 / 116 budget** ✅

---

### Beat 3 — Grounded answer + citation chip · 1:10–2:00 (50s)

**Shot:** Chat tab → agent response → citation chip expanded showing verbatim FDA label text.
**Click path:** Chat tab → type `I missed my morning metformin — what should I do?` → wait
for response → click the **citation chip expander** under the answer → label text + set ID
visible on screen.

> Now the question every patient actually asks. Margaret missed her morning metformin, and
> she wants to know what to do. NeuroRx doesn't answer from memory. It runs a vector search
> against FDA label chunks, filtered to metformin specifically, and the answer comes back
> quoting the label's own missed-dose guidance. Every clinical sentence ends with a citation
> chip. Click it, and you see the verbatim label text and the set ID it came from — traceable
> all the way back to the source document. If the label doesn't cover the question, the agent
> says so and points her to her pharmacist. It never fills the gap with a guess.

**Words: 107 / 116 budget** ✅

---

### Beat 4 — Interaction firewall (never cut) · 2:00–2:45 (45s)

**Shot:** Chat tab → red **"🚨 Drug Interaction Alert"** card with MAJOR severity + DDInter
source + Confirm/Cancel buttons.
**Click path:** Chat tab → type `add ibuprofen to my schedule` (Margaret is already on
warfarin per the fixed demo cohort) → interaction card renders → **hover** over the severity
line, **do not click Confirm**.

> Here's the beat that matters most. Margaret is on warfarin. She asks to add ibuprofen — a
> genuinely dangerous combination. The moment that request arrives, manage_schedule calls
> check_interactions first. Not because the prompt asked it to. Because it's enforced in the
> tool's code. And check_interactions is a deterministic SQL lookup against a frozen
> interaction table built from DDInter. Say this part clearly: this check is a database
> lookup, not an LLM guess. The result — a major-severity interaction, with its source —
> blocks the write outright. The model never decided this was dangerous. The table did. The
> model only explained it.

**Words: 98 / 105 budget** ✅ — plan §8 names this beat's key line ("This check is a database
lookup, not an LLM guess") explicitly; it is kept verbatim.

---

### Beat 5 — Today view + dashboard + Genie · 2:45–3:20 (35s)

**Shot:** Today tab (checklist + countdown) → Dashboard tab (stat cards, bar chart, heatmap)
→ Caregiver Mode toggle → Genie panel.
**Click path:** Today tab → click **"Taken ✓"** on the morning metformin row → note the
**"⏰ Next Dose"** countdown → Dashboard tab → pan across header metrics (Most-Missed Drug =
metformin, Most-Missed Time = evening) and the 90-day heatmap → click **"👨‍👩‍👧 Caregiver
Mode"** → ask the rehearsed Genie question.

> Day to day, Margaret lives in the Today tab. Doses grouped by time of day, one tap to mark
> each one taken, and a live countdown to the next. Every tap writes to Lakebase Postgres.
> The Dashboard turns that history into a picture: adherence by drug, a ninety-day heatmap,
> and the pattern that matters — she misses metformin, in the evening. And her daughter, in
> caregiver mode, can just ask Genie which drug Mom misses most. No SQL required.

**Words: 78 / 81 budget** ⚠️ **TIGHT** — three UI surfaces in 35s. Rehearse with a stopwatch;
if it runs long, cut the Genie sentence (plan §9 lists Genie first on the cut list).

---

### Beat 6 — Under the hood: architecture + MLflow + guardrail log · 3:20–4:20 (60s)

**Shot:** Architecture diagram (`ARCHITECTURE.md` §2 Mermaid, rendered full-screen) for
~30s → MLflow trace view (tool-call spans) → `guardrail_blocks` table query result.
**Click path:** browser tab with rendered architecture diagram → switch to Databricks
workspace → MLflow experiment → open the trace for Beat 3's citation answer → show the span
tree → switch to the **pre-saved** SQL cell running
`SELECT * FROM neurorx.app.guardrail_blocks ORDER BY blocked_at DESC LIMIT 5`.

> Under the hood. Real regulatory data — openFDA labels, RxNorm identities, DDInter
> interactions — flows through a Lakeflow declarative pipeline with data-quality
> expectations, into bronze, silver, and gold tables, all governed by Unity Catalog. One
> supervisor agent calls four tools, and every one of them is a Unity Catalog function.
> Transactional state lives in Lakebase Postgres and syncs automatically to Delta for
> analytics. Now the part almost nobody does. Sixty evaluation cases — grounded
> question-answering, interaction detection, schedule manipulation, and fifteen adversarial
> safety cases including jailbreak attempts — scored by MLflow with built-in judges plus a
> custom safety judge.
>
> **[INSERT MEASURED RESULTS HERE — ≤14 words. e.g. "Safety: sixty of sixty. Interactions:
> fifteen of fifteen. Groundedness: ninety-four percent." Do not read targets as results.]**
>
> And here's a live trace: the actual tool calls behind that citation answer. Plus the
> guardrail block log — every response the safety net caught, in an append-only table you can
> query.

**Words: 126 + ~14 = ~140 / 140 budget** ⚠️ **AT BUDGET, NO SLACK.** This is the longest beat
and carries three shot changes. If the measured-results line runs past 14 words, cut "Now the
part almost nobody does" (6 words) to make room.

---

### Beat 7 — Scale story + disclaimer + vision · 4:20–5:00 (40s)

**Shot:** Architecture diagram or dashboard as a calm background → closing title card
(product name, repo URL, demo URL).
**Click path:** none — narration over a static or slowly-panning shot.

> So what happens when this grows? Every tool is a Unity Catalog function. Adding
> pharmacy-refill integration is one new function — not a rewrite. Every table is governed.
> Lakebase handles transactions, Delta handles analytics, and compute is serverless
> throughout, so cost scales with use, not with idle clusters. To be clear: NeuroRx AI is an
> organizational assistant, not medical advice. It doesn't diagnose, it doesn't recommend
> dosages, and it doesn't change prescriptions. What it does is make sure the facts a patient
> sees came from a real label — and can be traced back to it.

**Words: 93 / 93 budget** ⚠️ **EXACTLY AT BUDGET.** If the recorded take runs long, cut "so
cost scales with use, not with idle clusters" (10 words) — the UC-function line is the load-
bearing part of plan §8's scale story, the cost clause is reinforcement.

---

## Word-count summary

| Beat | Budget | Actual | Status |
|---|---|---|---|
| 1 — Hook | 46 | 35 | ✅ comfortable |
| 2 — Extraction | 116 | 104 | ✅ comfortable |
| 3 — Citation | 116 | 107 | ✅ comfortable |
| 4 — Interaction | 105 | 98 | ✅ ok |
| 5 — Today/dashboard/Genie | 81 | 78 | ⚠️ tight (3 UI surfaces) |
| 6 — Under the hood | 140 | ~140 | ⚠️ **at budget, 3 shot changes** |
| 7 — Scale + disclaimer | 93 | 93 | ⚠️ **exactly at budget** |
| **Total** | **700** | **~655** | ✅ ~45 words of slack for pauses |

**Beats 5, 6, and 7 are the risk.** All three are at or within 3 words of budget *and* carry
the most on-screen action — word count alone won't catch the overrun, only a stopwatch dry
run will. Cut lines are pre-identified above for each.

---

## Rehearsal checklist

Plan §11 pitfall #4 is "demo dies live" — the first three items below are that pitfall's own
named mitigations.

- [ ] **Pre-warm the serving endpoint.** Send a throwaway chat request to `neurorx-agent`
  ≥5 min before recording so Beats 2–4 aren't eaten by cold start.
- [ ] **Pre-load Margaret Demo.** Confirm the sidebar defaults to
  `12345678-1234-1234-1234-123456789012` and her cohort (metformin, lisinopril, warfarin,
  atorvastatin; 44% adherence, metformin-evening miss pattern) is live in Lakebase. Re-run
  `lakebase/07_load_cohort.py`'s Margaret Demo assertion if unsure.
- [ ] **Recorded backup of every beat** — record each beat's screen action twice before
  assembling the final cut. Non-negotiable for Beats 4 and 6 (see contingency table).
- [ ] **Pipeline freshly run** within 24h so Beat 3's citation isn't a stale chunk_id.
- [ ] **Genie sample question verified** — actually ask Beat 5's question against the live
  space and confirm a sensible answer, not "I don't understand."
- [ ] **Guardrail demo cell pre-saved and tested** — Beat 6's SQL must not be typed live.
- [ ] **Screen recorder:** 1080p min, 30fps, system audio + mic on **separate tracks** (so a
  bad take can be re-voiced without re-recording screen), cursor highlighting on,
  notifications/Slack/email fully silenced.
- [ ] **Two full stopwatch dry runs** (plan §8: "script it, rehearse it twice"). If over 5:00,
  cut from Beat 5 or 7 first — Beats 4 and 6 carry the interaction firewall, citations, and
  eval harness, which plan §9 names as never-cut.

---

## Contingency table

| Beat | Failure mode | Live fallback |
|---|---|---|
| 1 | App slow to load | Cut to backup clip; never narrate over a spinner |
| 2 | Extraction times out or misparses | Backup clip. Do **not** retry live — a second failed parse on camera is unrecoverable |
| 3 | Citation chip empty, or stale chunk_id | Backup clip; if unavailable, re-run the pipeline before attempting the take again |
| 4 | Interaction card doesn't fire | **Never-cut beat.** Use the backup clip immediately. If no backup exists, stop and record one before continuing — do not ship a take without this beat |
| 5 | Genie shows the "not configured" fallback card | Skip the Genie sentence, narrate over the dashboard charts, and note Genie's Beta status in the video description. Plan §9 puts Genie first on the cut list — this is a sanctioned cut |
| 6 | MLflow trace not flushed, or SQL cell errors | **Never-cut beat.** Backup clip. Architecture-diagram portion can carry the beat alone if the trace fails, but the guardrail table must appear somewhere |
| 6 | Eval numbers not ready by record day | Re-script to: "targets are 100% safety, 100% interaction detection, ≥90% groundedness — full results and reproduction commands are in the repo." True regardless of run status |
| 7 | Runs long | Cut "so cost scales with use, not with idle clusters"; keep the UC-function line and the disclaimer |

---

*Companion:* [`docs/submission_checklist.md`](submission_checklist.md) *— tracks whether this
video, and everything else, is actually ready for the Devpost gate.*
