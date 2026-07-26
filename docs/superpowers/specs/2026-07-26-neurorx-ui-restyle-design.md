# NeuroRx AI — UI restyle to the `design/mockup.html` design language

**Date:** 2026-07-26
**Status:** approved (design), pending implementation plan
**Scope:** presentation layer only — no data-layer, agent, or pipeline changes

---

## 1. Goal

Restyle the existing Streamlit app so all three surfaces (Today, Chat, Dashboard)
match the design language of `design/mockup.html`, while continuing to run on
Streamlit and deploy through Databricks Apps.

The mockup is a bundled design prototype containing two things: a marketing
landing page (hero, animated starfield, trust cards) and a product showcase
demoing the three app surfaces with hardcoded fake data (Sertraline,
Levetiracetam, Melatonin). **Only the product-showcase design language is being
adopted.** The marketing page is not being built, and none of the mockup's fake
medication data enters the app — every surface keeps reading real data from
`app/db.py`.

## 2. Decisions taken

| Question | Decision |
|---|---|
| Delivery vehicle | Restyle Streamlit (keeps Databricks Apps deployment path) |
| Scope | Everything — global theme **and** all three views |
| App shell | Match mockup exactly: in-page header, hidden sidebar, thin mono safety ticker |
| Charts | Keep Plotly, theme it to match |
| Implementation approach | Config-first theming + a thin CSS component layer (approach A) |

### Approaches considered and rejected

- **Pure CSS override, no config, no markup changes.** Rejected: cannot produce
  the dose-row grid or status pills, so it under-delivers on "everything," and it
  fights Streamlit's own styles instead of setting them.
- **`components.v1.html` iframes for pixel-perfect panels.** Rejected as a
  correctness trap, not a taste call: component iframes are sandboxed, so buttons
  rendered inside them cannot call back into Python. The Today view's "Mark taken"
  writes would silently stop working.

## 3. Architecture

**New files**

- `.streamlit/config.toml` — every design token Streamlit can express natively.
- `app/theme.py` — the CSS component layer plus small HTML render helpers,
  injected exactly once per session from `app/app.py`.

**Changed files**

- `app/app.py` — hide sidebar; render custom header (logo, nav, patient pill) and
  the mono safety ticker; restyle the tab strip.
- `app/views/today.py` — dose rows become the mockup's 4-column grid with status
  pills; next-dose countdown and refill panel become mockup-style cards.
- `app/views/chat.py` — citation chips and the pending-confirmation card restyled
  to the mockup's diff card.
- `app/views/dashboard.py` — stat cards restyled; Plotly figures inherit theme
  colors from config; caregiver panel restyled.

**Explicitly untouched:** `app/db.py`, `app/agent_client.py`, `app/config.py`, and
everything under `agent/`, `pipelines/`, `lakebase/`, `data/`. This work does not
move anything about the deterministic-lookup spine described in `CLAUDE.md` §1.

## 4. Design tokens

Source palette is oklch (from the mockup). Streamlit's `[theme]` config takes hex,
so values were converted and **verified against the browser's own oklch renderer**
(canvas `fillStyle` round-trip) rather than eyeballed — an independent Python
oklch→sRGB implementation produced identical output for all four.

| Token | Mockup (oklch) | Hex | Config key |
|---|---|---|---|
| Page background | `oklch(0.145 0.012 258)` | `#070A0F` | `theme.backgroundColor` |
| Primary text | `oklch(0.965 0.004 250)` | `#F1F4F6` | `theme.textColor` |
| Accent (cyan) | `oklch(0.84 0.085 195)` | `#84DCDC` | `theme.primaryColor` |
| Warn (amber) | `oklch(0.86 0.075 82)` | `#EACD99` | `theme.yellowColor` |
| Hairline border | `oklch(1 0 0 / 0.1)` | rgba white 10% | `theme.borderColor` |
| Card surface | `oklch(1 0 0 / 0.03)` | rgba white 3% | `theme.secondaryBackgroundColor` |

**Fonts** (three slots, all natively supported; config accepts the
`"Name:https://fonts.googleapis.com/..."` URL form):

- `theme.font` = Manrope — body/UI
- `theme.headingFont` = Instrument Serif — display headings
- `theme.codeFont` = JetBrains Mono — eyebrow labels, timestamps, metadata

**Shape:** `theme.baseRadius` and `theme.buttonRadius` = `"full"` for the pill
geometry used throughout the mockup.

**Charts:** `theme.chartCategoricalColors` and `theme.chartSequentialColors` apply
to Plotly natively. This is what makes the "keep Plotly, theme it" decision cheap —
the palette arrives from config, so per-figure work is limited to layout (fonts,
gridlines, margins, backgrounds), not hand-coloring traces.

## 5. `app/theme.py`

Two exports:

- `inject()` — emits one `<style>` block: CSS custom properties
  (`--nrx-accent`, `--nrx-hairline`, `--nrx-surface`, …) plus component classes,
  plus the rules config cannot reach (hide sidebar, header layout, dose-row grid,
  eyebrow labels, status pills, hover lift).
- Render helpers — `eyebrow(text)`, `status_pill(label, tone)`, `stat_card(...)`,
  each returning an HTML string for `st.markdown(..., unsafe_allow_html=True)`.

**Design constraint driving this split:** interactive buttons must remain real
`st.button` calls, because only those can trigger Python callbacks. Buttons are
therefore styled via CSS on `[data-testid="stButton"]`; everything non-interactive
is rendered as HTML we control.

## 6. Per-view changes

### 6.1 Today (`app/views/today.py`)

The mockup's Today surface maps almost 1:1 onto what this view already renders.

- **Header cards row** — "NEXT DOSE IN" card (large Instrument Serif countdown,
  drug name + time beneath) beside a "REFILL STATUS" card listing each drug with a
  thin progress bar. The refill bar renders the honest unavailable state the data
  layer actually returns (`days_remaining is None`); it does not invent a supply
  figure to make the bar look full. This preserves the existing behaviour
  documented in `today.py`'s module docstring.
- **Day-part groups** — each group gets a mono uppercase eyebrow (`MORNING`) with
  a hairline rule and a right-aligned `taken/total` count.
- **Dose rows** — 4-column grid: mono time · drug name + detail line · status pill ·
  action buttons. Status pills use the mockup's tone treatment
  (`TAKEN`, `TAKEN LATE`, `DUE NOW`, `UPCOMING`, `MISSED`).
- Existing logic is unchanged: "missed" stays a display-time judgment computed from
  `planned_ts < now`, never a written status, exactly as today.

### 6.2 Chat (`app/views/chat.py`)

- Citation chips restyled to the mockup's chip treatment (mono source label +
  section label). They remain `st.expander` — Streamlit has no native chip widget,
  which is the same real API limitation already recorded in this view's docstring.
- The pending-confirmation card becomes the mockup's diff card: eyebrow reading
  "PENDING SCHEDULE CHANGE — YOU CONFIRM, NOT THE MODEL", old value → new value,
  interaction re-check note, then Confirm / Cancel buttons.
- Confirm/Cancel remain real `st.button`s; the UI-owns-confirmation property
  (`CLAUDE.md` Task 3.4 Requirement 5) is unchanged by this restyle.

### 6.3 Dashboard (`app/views/dashboard.py`)

- Four stat cards replace `st.metric` styling: mono eyebrow label, large serif
  value, delta line beneath.
- Plotly figures (adherence-by-drug bar, 90-day heatmap, time-of-day pattern) keep
  their existing data logic and pick up config colors; per-figure work is limited
  to layout theming and transparent backgrounds.
- Caregiver panel restyled to the mockup's analytics card, keeping the existing
  `GENIE_EMBED_URL` gate — no iframe is emitted when no Genie Space is configured.

## 7. Flagged deviations and risks

1. **Safety notice becomes visually subtler.** `CLAUDE.md` (Task 3.4 Requirement 1)
   describes the banner as a prominent, non-dismissable `st.warning`. The mockup's
   treatment is a thin low-contrast mono ticker, and that treatment was chosen
   deliberately. The safety-relevant properties are preserved — it remains
   permanently visible, at the top of every tab, with no dismiss control — but its
   visual weight drops. Recorded here rather than silently resolved, per
   `CLAUDE.md` §6. If a judge-facing review wants the louder treatment back, that
   is a one-line change in `app/app.py`.
2. ~~**Google Fonts is a runtime network dependency.**~~ **RESOLVED — fonts are
   self-hosted.** The URL form was skipped entirely rather than deferred: it makes
   first paint depend on an outbound call to `fonts.googleapis.com` that a deployed
   Databricks App may block, and the failure is silent (every surface quietly falls
   back to system fonts). The 8 latin/latin-ext woff2 files were extracted from
   `design/mockup.html`'s own embedded bundle into `app/static/fonts/` — 127 KB
   total, each verified to carry the `wOF2` magic number — and declared as 18
   `[[theme.fontFaces]]` tables with `server.enableStaticServing = true`. Verified
   served: `GET /app/static/fonts/Manrope-latin.woff2` → `200 font/woff2 24576`.
   Manrope and JetBrains Mono are variable fonts, so several tables share a URL and
   differ only by `weight` — mirroring the mockup's own `@font-face` CSS.
3. **Streamlit internal selectors are version-fragile.** CSS targeting
   `[data-testid="stButton"]` and similar depends on Streamlit's internal DOM,
   which is not a stable API. Mitigation: push as much as possible into
   `config.toml` (a supported API) and keep `theme.py`'s selector surface small and
   documented, so a Streamlit upgrade has one file to check.
4. **The animated starfield canvas is not being ported.** It is a marketing-page
   flourish requiring a canvas render loop; inside Streamlit it would need a
   sandboxed component iframe and could not sit behind page content. Approximated
   with CSS radial gradients instead.

## 8. Verification

Per `CLAUDE.md` §6 ("if a module has no Spark/Databricks dependency, actually run
it"), this work is verifiable end-to-end locally and must actually be run, not just
read:

- Launch against local Postgres (`docs/local_dev.md` path, `NEURORX_LOCAL_PG` set).
- Today tab: confirm Margaret's real doses render in the new grid, and that
  "Mark taken" still writes — re-query `dose_events` to confirm the status flip,
  the same check already used this session.
- Dashboard tab: confirm the themed Plotly figures render with real local
  aggregates (43% / metformin / evening for Margaret).
- Confirm the safety ticker is present on all three tabs and has no dismiss control.
- Chat tab degrades to its existing clear notice locally (no agent endpoint) —
  the restyle must not turn that into a traceback.

## 8a. Found during implementation

Recorded because each cost real time to find and would cost it again.

1. **Config must live at `app/.streamlit/`, not the repo root.** Streamlit reads
   config from `~/.streamlit/`, `${CWD}/.streamlit/`, and
   `<main script dir>/.streamlit/` (last wins). This project launches two ways —
   `streamlit run app/app.py` from the repo root locally, and `app.yaml`'s
   `streamlit run app.py` from inside `app/` when deployed. Only the script-level
   location resolves identically for both. A repo-root `.streamlit/` would have
   themed local dev and silently left the deployed app unstyled. Same class of bug
   as the `sys.path` bootstrap already documented in `app/app.py`.
2. **Streamlit 1.59 renders tabs with react-aria, not BaseWeb.** The
   `[data-baseweb="tab-list"]` / `[data-baseweb="tab"]` selectors used by nearly
   every Streamlit-CSS recipe online match **nothing** — confirmed by dumping the
   live DOM. The current shape is
   `[role="tablist"] > [data-testid="stTab"][aria-selected] > .react-aria-SelectionIndicator`.
3. **Streamlit caches imported modules in `sys.modules`.** With no `watchdog`
   installed, edits to `app/theme.py` do not take effect on a browser refresh —
   the server must be restarted. Cost one round of "the CSS isn't applying" before
   a DOM check showed the *old* stylesheet was still being served.
4. **`st.columns` cannot give paired cards equal heights.** Streamlit wraps each
   column child in fixed-height wrappers, so a card's own `height: 100%` has
   nothing to resolve against. Cards paired on a row are rendered as a single flex
   row (`theme.card_row`) instead. Only valid for cards with no widgets — anything
   containing an `st.button` must use real columns.
5. **Pre-existing bug found while verifying the Chat tab, fixed here.** On the
   local path the "chat needs the workspace" notice was painted into an
   `st.empty()` placeholder and returned `text=""`. `render()` then calls
   `st.rerun()`, which repaints the tab from history — destroying the placeholder
   and leaving an **empty assistant bubble** that reads as a hung request. The
   notice is now returned as the assistant turn's own text so it survives the
   repaint. Unrelated to the restyle (the block is unchanged from `HEAD`), but it
   is on the surface this work had to verify.

## 9. Out of scope

- The marketing landing page (hero, trust cards, stat strip).
- Any change to data access, agent behaviour, guardrails, or citations.
- Light-mode support — the design is dark-only by intent.
- Replacing Streamlit with a custom web frontend.
