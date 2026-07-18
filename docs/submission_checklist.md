# NeuroRx AI — Devpost Submission Checklist

> ⚠️ **This checklist was produced without the Devpost rules text, the contest dates,
> the deployed app URL, or a video link being provided** — the task that requested it
> named those as inputs to supply, but they weren't pasted into the conversation. Every
> item below is either **verified against the real repo/GitHub state** (marked with the
> exact command or fetch that produced it) or marked **ACTION NEEDED** with the precise
> command/click path to resolve it. Nothing below is guessed.

**Repo:** `https://github.com/0XSreekar/NeuroRx-AI-` (from `git remote -v`, confirmed
matches `CLAUDE.md`'s non-negotiable naming table)

---

## 🚨 Read this first — the single biggest risk to this submission

**Only one commit exists on the public GitHub repo, and it's the original empty
scaffold.** Verified two ways:

- Local: `git log --all` shows exactly one commit — `3b4047e chore: scaffold NeuroRx AI
  repo` (2026-07-15 18:08:36 +0530).
- Remote (fetched live, unauthenticated, GitHub's own repo page): confirms **1 commit**,
  and the README rendered on GitHub still reads *"Placeholder. Filled in Phase 5..."* —
  **not** the real README written this session. `git status` locally shows 40+ files
  either modified or untracked (`agent/`, `app/`, `data/ingestion/`, `evals/`,
  `lakebase/`, `pipelines/`, `README.md`, `ARCHITECTURE.md`, `CLAUDE.md`, all of it).

**Everything this project actually is — the agent, the app, the eval harness, the real
README — is sitting uncommitted on disk. None of it is visible to a judge looking at the
public repo right now.** This blocks gates 3, 5, 6, and 7 below simultaneously. Fix
before anything else:

```bash
cd "/Users/guts/Projects /NeuroRx AI"
git add -A
git status   # review what's staged before committing — check nothing secret is included
git commit -m "..."
git push origin main
```

**Also broken, separately, and not fixable by a commit:** the GitHub repo's **About**
sidebar description currently reads *"AI-powered clinical decision support system that
collects patient symptoms, recommends lab investigations, analyzes uploaded reports, and
generates evidence-based treatment plans using Claude Sonnet 4.5 with RAG over clinical
guidelines."* **This describes a different, unrelated product** — not a medication-
adherence assistant. This is repo metadata, not a file, so no commit fixes it.
**Action needed:** GitHub → repo page → gear icon next to "About" → replace the
description with NeuroRx AI's actual one-liner (see `README.md`'s opening paragraph) →
Save changes.

---

## Gate-by-gate

### 1. GitHub repo public (tested in incognito-equivalent, unauthenticated fetch)

**PASS.** Fetched `https://github.com/0XSreekar/NeuroRx-AI-` with no authentication —
returned a live public repo page (file listing, commit count, license badge all
visible), not a 404 or a sign-in wall.

**Evidence:** `https://github.com/0XSreekar/NeuroRx-AI-`

### 2. OSS license present (MIT) at repo root

**PASS.** Confirmed twice:

- Local: `head -5 LICENSE` → `MIT License / Copyright (c) 2026 0XSreekar`, file at repo
  root.
- Remote: GitHub's own license badge on the repo page reads "MIT license."

**Evidence:** [`LICENSE`](../LICENSE)

### 3. Commits within the official project period

**CANNOT DETERMINE — contest start/end dates were not provided.** Once you have them,
run:

```bash
git log --since="<CONTEST_START>" --until="<CONTEST_END>" --date=iso --pretty=format:"%h %ad %s"
```

**What's known without the dates:** only one commit exists at all —
`3b4047e 2026-07-15 18:08:36 +0530 chore: scaffold NeuroRx AI repo`. If the contest
period doesn't cover 2026-07-15, this gate fails outright until the uncommitted work
(see the 🚨 section above) is committed and pushed with timestamps inside the period.
**Even if the period does cover today, a single-commit history for a project this size
is a plausibility problem for judges** — commit the real history in logical chunks
(by phase, e.g. "Phase 1: data ingestion," "Phase 2: agent + tools," etc.) rather than
one giant commit, so the timeline reads as real incremental work.

### 4. Video ≤5:00, link plays logged-out, audio audible

**FAIL — no video exists yet.** `docs/demo_script.md` (this session's companion
deliverable) is the script, timed to exactly 5:00 across 8 beats, but nothing has been
recorded. Once recorded and uploaded:

- **ACTION NEEDED:** open the video URL in an incognito/private window (no platform
  login) and confirm it plays without a sign-in prompt.
- **ACTION NEEDED:** confirm runtime ≤5:00 in the player's own timestamp — don't trust
  the script's timing alone, actual recorded pacing (UI load times, pauses) will differ.
- **ACTION NEEDED:** watch with headphones/external speakers to confirm voiceover is
  audible over any system/UI sound effects.

### 5. Judges can access the working app: URL + testing instructions + credentials

**FAIL — no app is deployed.** Per this project's own status tracking (`CLAUDE.md`),
Phase 3's exit checkpoint ("full flow through the UI — extract → confirm → schedule
appears → mark doses in Today → dashboard updates → grounded chat answer with clickable
citation") has not been reached; nothing has run against a live Databricks workspace.
`README.md`'s Setup section already states plainly: *"a live demo URL and any read-only
testing credentials will be provided through the hackathon submission form once the app
is deployed."*

**ACTION NEEDED, in order:** (1) deploy per `README.md` §Setup steps 1–5, (2) confirm the
Phase 3 exit checkpoint manually in a browser, (3) create a read-only demo login if the
app requires auth, (4) paste the URL + credentials into the Devpost submission's
"Testing Instructions" field — not just the README, since judges read Devpost first.

### 6. Text description on Devpost covers features + tech used, matches README

**CANNOT VERIFY — no Devpost access in this session, and no draft text was provided.**

**ACTION NEEDED:** Devpost → your submission → Edit → paste a description covering the
same ground as `README.md`'s pitch paragraph (four features: Create, Maintain, Adhere,
Caregiver analytics) and the component table (§Architecture) for "tech used." **Do not
reuse the GitHub About-sidebar text** — as the 🚨 section above documents, it currently
describes an unrelated product and would actively contradict the README if copied.
Before submitting, re-read both side by side and confirm no factual drift (e.g. don't
let Devpost claim a live demo works if gate 5 above is still failing).

### 7. All five judging criteria addressed somewhere visible

**PASS, mapped below** — this is the one gate fully verifiable from the repo content
itself, since `README.md` (Task 5.3, this session) and `docs/demo_script.md` (this
session) both exist now.

| Criterion | README section | Demo beat |
|---|---|---|
| **Business Applicability** | [§The problem](../README.md#the-problem) — WHO ~50% non-adherence stat, $100–289B/yr + ~125k deaths/yr cited to *Annals of Internal Medicine* 2012 | Beat 1 (0:00–0:20) |
| **Data Relevance** | [§Data](../README.md#data) — openFDA, RxNorm, DDInter, synthetic cohort table | Beat 2 (0:20–0:50, architecture diagram shows the same pipeline) |
| **Creativity** | [§Safety design](../README.md#safety-design) — the interaction firewall + citation-gated claims as "the creative angle," per `ARCHITECTURE.md` §9 | Beat 4 (1:25–2:15, the interaction-block centerpiece) |
| **Thoroughness** | [§Evaluation](../README.md#evaluation) — 60-case harness, reproduce commands (though results are still pending — see gate 4/5 discussion) | Beats 7–8 (3:35–5:00, trace + guardrail log + eval results) |
| **Well-Architected** | [§Architecture](../README.md#architecture) — component table with UC-function tools, Lakebase/Delta split, serverless throughout; [§Scale story](../README.md#scale-story) | Beat 2 (architecture) + Beat 6 (3:00–3:35, dashboard/Genie as the analytics half of the split) |

**Caveat carried over from gate 4/5:** this mapping shows *where* each criterion is
addressed in the written materials — it does not by itself mean the underlying claim is
demonstrable live yet (the app isn't deployed, the eval hasn't run). Fix gates 4 and 5
before this mapping is judge-ready, not just repo-ready.

### 8. Team/eligibility fields complete on Devpost

**CANNOT VERIFY — no Devpost access in this session.**

**ACTION NEEDED:** Devpost → your submission → confirm: team members added (or solo
entrant flag set), eligibility questions answered (location, age/student status if the
contest asks), and any required category/track selected. This is a Devpost-form-only
check with nothing to cross-reference in the repo — verify directly on the platform.

---

## Summary

| Gate | Status |
|---|---|
| 1. Repo public | ✅ PASS |
| 2. MIT license | ✅ PASS |
| 3. Commits in period | ⚠️ CANNOT DETERMINE (need contest dates) — and only 1 commit exists regardless |
| 4. Video ≤5:00, plays logged-out | ❌ FAIL — not recorded |
| 5. Working app + credentials | ❌ FAIL — not deployed |
| 6. Devpost text matches README | ⚠️ CANNOT VERIFY — no Devpost access; watch for the stale About-text contamination risk |
| 7. Five criteria mapped | ✅ PASS (mapping done; underlying demos still gated on 4/5) |
| 8. Team/eligibility fields | ⚠️ CANNOT VERIFY — no Devpost access |

**Net: not submission-ready.** Priority order to fix: (1) commit and push the real
working tree, (2) fix the GitHub About description, (3) deploy the app and reach the
Phase 3 exit checkpoint, (4) run the actual eval (`evals/02_run_evaluation.py`) and drop
real numbers into `README.md` and `docs/demo_script.md` Beat 8, (5) record the video per
`docs/demo_script.md`, (6) fill in the three Devpost-only fields (gates 6, 8, and the
testing-instructions part of gate 5).
