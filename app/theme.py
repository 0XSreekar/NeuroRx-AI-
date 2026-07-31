"""NeuroRx AI — the design layer for the app's three views.

Implements the design language of `design/mockup.html` on top of Streamlit.
See `docs/superpowers/specs/2026-07-26-neurorx-ui-restyle-design.md` for the
design decisions this file carries out.

## Division of labour: config.toml vs. this file

Most of the design is expressed in `app/.streamlit/config.toml` — palette,
the three font slots, pill radius, heading scale, and (importantly) the Plotly
chart palette. That is a *supported* Streamlit API and it survives upgrades.

This module holds only what config cannot express:

  - structural chrome the mockup has and Streamlit does not (top header,
    safety ticker, dose-row grid, mono eyebrow labels, status pills),
  - a handful of overrides that must target Streamlit's internal DOM.

**The selector surface is deliberately small and listed in one place**
(`_STREAMLIT_INTERNALS` below). `data-testid` values are internal to Streamlit,
not a stable public API, so a Streamlit upgrade needs exactly one block of this
file re-checked rather than a hunt through three view modules.

## Why buttons stay `st.button` and are styled, not re-rendered as HTML

Only a real `st.button` can trigger a Python callback. Every interactive control
in the mockup that *does* something — "Mark taken", "Skip", "Confirm change" —
therefore stays a Streamlit widget and is styled via CSS. Everything
non-interactive (dose rows, pills, eyebrows, stat cards) is emitted as HTML this
module controls.

This is also why the mockup's panels are NOT rendered through
`st.components.v1.html`: component iframes are sandboxed and cannot call back
into Python, so the Today view's mark-dose writes would silently stop working.
"""

from __future__ import annotations

import html

import streamlit as st

# ---------------------------------------------------------------------------
# Design tokens
#
# These mirror `app/.streamlit/config.toml`. They are duplicated here (rather
# than parsed out of the TOML at runtime) because CSS needs them as literals and
# a runtime TOML read would couple every page render to file I/O. If a color
# changes, it changes in both places — the config comment says the same.
#
# Source values are oklch, from the mockup. The alpha-over-background forms are
# kept as rgba here (unlike config.toml, whose keys need solid colors) because
# CSS can composite them properly against whatever sits behind.
# ---------------------------------------------------------------------------

ACCENT = "#84DCDC"  # oklch(0.84 0.085 195)
AMBER = "#EACD99"  # oklch(0.86 0.075 82)
TEXT = "#F1F4F6"  # oklch(0.965 0.004 250)
BG = "#070A0F"  # oklch(0.145 0.012 258)

_TOKENS = f"""
  --nrx-bg: {BG};
  --nrx-text: {TEXT};
  --nrx-accent: {ACCENT};
  --nrx-amber: {AMBER};

  --nrx-text-70: rgba(241, 244, 246, 0.70);
  --nrx-text-50: rgba(241, 244, 246, 0.50);
  --nrx-text-38: rgba(241, 244, 246, 0.38);

  --nrx-surface: rgba(255, 255, 255, 0.03);
  --nrx-surface-hi: rgba(255, 255, 255, 0.055);
  --nrx-hairline: rgba(255, 255, 255, 0.10);
  --nrx-hairline-hi: rgba(255, 255, 255, 0.16);

  --nrx-mono: 'JetBrains Mono', ui-monospace, monospace;
  --nrx-serif: 'Instrument Serif', Georgia, serif;

  --nrx-ease: cubic-bezier(.2, .8, .2, 1);
"""

# ---------------------------------------------------------------------------
# Streamlit-internal selectors — the ONLY place this file reaches into
# Streamlit's own DOM. Re-check this block (and nothing else) after a Streamlit
# upgrade. Verified against streamlit 1.59.2.
# ---------------------------------------------------------------------------

_STREAMLIT_INTERNALS = """
/* Sidebar is replaced by the mockup's in-page header. Both the panel and the
   little re-open chevron have to go, or the chevron floats over the header. */
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] { display: none !important; }

/* Streamlit's own top bar (Deploy / hamburger) would sit above our header. */
[data-testid="stHeader"] { background: transparent !important; height: 0 !important; }
[data-testid="stToolbar"] { display: none !important; }

/* The mockup is a wide, centered layout with generous top air. */
[data-testid="stMain"] .block-container {
  max-width: 1360px;
  padding: 1.2rem 2.5rem 5rem;
}

/* Buttons: pill geometry + the mockup's signature 1px hover lift.
   Primary = filled light pill; secondary = ghost pill with hairline border. */
[data-testid="stButton"] button {
  font-family: 'Manrope', sans-serif !important;
  font-size: 0.8rem !important;
  font-weight: 600 !important;
  letter-spacing: 0.01em;
  padding: 0.62rem 1.15rem !important;
  min-height: 0 !important;
  transition: transform .3s var(--nrx-ease), box-shadow .3s var(--nrx-ease),
              background .3s, border-color .3s, color .3s;
}
[data-testid="stButton"] button:hover {
  transform: translateY(-1px);
  box-shadow: 0 8px 22px rgba(0, 0, 0, 0.40);
}
/* Primary action = the mockup's near-white pill with dark text, NOT the accent
   cyan. `theme.primaryColor` is cyan because it drives links, the focus ring and
   the chart palette, where cyan is right; but the mockup's filled buttons are
   light-on-dark, so primary buttons are overridden here rather than by bending
   primaryColor to a value that would then be wrong everywhere else. */
[data-testid="stButton"] button[kind="primary"] {
  background: var(--nrx-text) !important;
  border: 0 !important;
  color: #0B0E12 !important;
}
[data-testid="stButton"] button[kind="primary"]:hover { background: #FFFFFF !important; }

[data-testid="stButton"] button[kind="secondary"] {
  background: transparent !important;
  border: 1px solid var(--nrx-hairline) !important;
  color: var(--nrx-text-70) !important;
  font-weight: 500 !important;
}
[data-testid="stButton"] button[kind="secondary"]:hover {
  background: var(--nrx-surface-hi) !important;
  border-color: var(--nrx-hairline-hi) !important;
  color: var(--nrx-text) !important;
}

/* Tabs become the mockup's pill segmented control.
   NOTE: Streamlit 1.59 renders tabs with react-aria, NOT BaseWeb. The
   `[data-baseweb="tab-list"] / [data-baseweb="tab"]` selectors that nearly every
   Streamlit-CSS recipe online still uses match NOTHING here — verified by
   dumping the live DOM. The current shape is
   [role="tablist"] > [data-testid="stTab"][aria-selected] > .react-aria-SelectionIndicator */
[data-testid="stTabs"] [role="tablist"] {
  gap: 0.4rem;
  background: transparent;
  border-bottom: none !important;
  padding: 0.25rem 0 1.4rem;
}
[data-testid="stTab"] {
  font-family: 'Manrope', sans-serif;
  font-size: 0.8rem;
  font-weight: 500;
  color: var(--nrx-text-50);
  background: var(--nrx-surface);
  border: 1px solid var(--nrx-hairline);
  border-radius: 999px;
  padding: 0.5rem 1.15rem;
  transition: color .3s, background .3s, border-color .3s;
}
[data-testid="stTab"]:hover {
  color: var(--nrx-text);
  background: var(--nrx-surface-hi);
}
[data-testid="stTab"][aria-selected="true"] {
  color: #0B0E12 !important;
  background: var(--nrx-text) !important;
  border-color: transparent !important;
}
[data-testid="stTab"][aria-selected="true"] * { color: #0B0E12 !important; }
/* The sliding underline is redundant once tabs are pills. */
.react-aria-SelectionIndicator { display: none !important; }

/* The patient control is an st.popover styled down to the mockup's pill.
   Its default is a full-width, tall secondary button. */
[data-testid="stPopoverButton"] {
  font-family: var(--nrx-mono) !important;
  font-size: 0.62rem !important;
  letter-spacing: 0.12em !important;
  padding: 0.5rem 0.9rem !important;
  background: var(--nrx-surface) !important;
  border: 1px solid var(--nrx-hairline) !important;
  color: var(--nrx-text-70) !important;
}

/* Bordered containers become the mockup's cards. */
[data-testid="stVerticalBlockBorderWrapper"] {
  background: var(--nrx-surface);
  border-radius: 18px;
}

/* Expanders (citation chips, refill panel) as hairline cards. */
[data-testid="stExpander"] details {
  background: var(--nrx-surface);
  border: 1px solid var(--nrx-hairline);
  border-radius: 14px;
}
[data-testid="stExpander"] summary { font-size: 0.85rem; }

/* Chat input as a pill. */
[data-testid="stChatInput"] { border-radius: 999px; }
"""

# ---------------------------------------------------------------------------
# Component classes — this module's own vocabulary, no Streamlit internals.
# ---------------------------------------------------------------------------

_COMPONENTS = """
/* Approximation of the mockup's animated starfield: two wide, very low-alpha
   radial washes. The real thing is a canvas render loop, which inside Streamlit
   would need a sandboxed component iframe and could not sit behind page
   content — so it is deliberately not ported (see the spec, risk 4). */
[data-testid="stAppViewContainer"] {
  background:
    radial-gradient(1200px 620px at 78% -8%, rgba(132, 220, 220, 0.07), transparent 60%),
    radial-gradient(900px 520px at 6% 8%, rgba(132, 220, 220, 0.035), transparent 62%),
    var(--nrx-bg);
}

/* --- eyebrow: uppercase mono section label, the mockup's most-used motif --- */
.nrx-eyebrow {
  font-family: var(--nrx-mono);
  font-size: 0.6rem;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--nrx-text-38);
}

/* --- app header ---------------------------------------------------------- */
.nrx-header {
  display: flex; align-items: center; gap: 1.1rem;
  padding: 0.2rem 0 0.9rem;
}
.nrx-logo { font-family: var(--nrx-serif); font-size: 1.5rem; letter-spacing: -0.01em; }
/* Clickable wordmark (signed-out screens only — see theme.brand's docstring).
   Styled as the wordmark itself, not as a link: no underline, inherits colour. */
.nrx-logo-link {
  text-decoration: none !important; color: inherit !important;
  display: inline-flex; align-items: center;
  transition: opacity .25s var(--nrx-ease);
}
.nrx-logo-link:hover { opacity: .72; }
.nrx-logo-badge {
  font-family: var(--nrx-mono); font-size: 0.55rem; letter-spacing: 0.18em;
  border: 1px solid var(--nrx-hairline); border-radius: 6px;
  padding: 0.18rem 0.34rem; margin-left: 0.45rem;
  color: var(--nrx-text-50); vertical-align: middle;
}
.nrx-header-spacer { flex: 1; }
.nrx-patient-pill {
  display: inline-flex; align-items: center; gap: 0.5rem;
  background: var(--nrx-surface); border: 1px solid var(--nrx-hairline);
  border-radius: 999px; padding: 0.42rem 0.85rem;
  font-family: var(--nrx-mono); font-size: 0.65rem; letter-spacing: 0.05em;
}
.nrx-patient-pill .k { color: var(--nrx-text-38); letter-spacing: 0.16em; }
.nrx-patient-pill .v { color: var(--nrx-text); }
.nrx-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--nrx-accent); }

/* --- safety ticker -------------------------------------------------------
   Persistent, present on every tab, with no dismiss control anywhere. Visually
   quieter than the st.warning box it replaces; that trade is recorded in the
   spec (flagged deviation 1), not made silently. The amber left rule and dot
   keep it legible as a caution rather than decorative chrome. */
.nrx-safety {
  display: flex; align-items: center; gap: 0.6rem;
  border-top: 1px solid var(--nrx-hairline);
  border-bottom: 1px solid var(--nrx-hairline);
  border-left: 2px solid var(--nrx-amber);
  background: rgba(234, 205, 153, 0.045);
  padding: 0.55rem 0.9rem; margin-bottom: 1.1rem;
  font-family: var(--nrx-mono); font-size: 0.6rem;
  letter-spacing: 0.11em; text-transform: uppercase;
  color: rgba(234, 205, 153, 0.86);
}
.nrx-safety .nrx-dot { background: var(--nrx-amber); flex: 0 0 auto; }

/* --- cards --------------------------------------------------------------- */
.nrx-card {
  background: var(--nrx-surface); border: 1px solid var(--nrx-hairline);
  border-radius: 18px; padding: 1.1rem 1.25rem;
}

/* Cards laid out side by side, stretched to a common height as in the mockup.
   Deliberately NOT st.columns: Streamlit wraps each column child in several
   fixed-height wrappers, so a card's own height:100% has nothing to resolve
   against and the shorter card keeps its content height. A flex row rendered in
   one markdown block sidesteps that entirely. Only safe for cards with no
   interactive widgets — anything with an st.button must use real columns. */
.nrx-card-row { display: flex; gap: 1rem; align-items: stretch; }
.nrx-card-row > .nrx-card { flex: 1 1 0; min-width: 0; }
@media (max-width: 900px) { .nrx-card-row { flex-direction: column; } }
.nrx-stat-value { font-family: var(--nrx-serif); font-size: 2.6rem; line-height: 1; }
.nrx-stat-unit  { font-family: var(--nrx-mono); font-size: 0.8rem; color: var(--nrx-text-50); margin-left: 0.25rem; }
.nrx-stat-delta { font-size: 0.72rem; color: var(--nrx-text-50); margin-top: 0.45rem; }

/* --- section heading ------------------------------------------------------ */
.nrx-section { margin: 2.2rem 0 0.9rem; }
.nrx-section h2 {
  font-family: var(--nrx-serif); font-weight: 400; font-size: 2.1rem;
  letter-spacing: -0.015em; margin: 0.3rem 0 0; padding: 0;
}

/* --- day-part group header: eyebrow + hairline rule + count --------------- */
.nrx-group {
  display: flex; align-items: center; gap: 0.8rem;
  margin: 1.5rem 0 0.6rem;
}
.nrx-group .rule { flex: 1; height: 1px; background: var(--nrx-hairline); }
.nrx-group .count { font-family: var(--nrx-mono); font-size: 0.62rem; color: var(--nrx-text-38); letter-spacing: 0.1em; }

/* --- dose row ------------------------------------------------------------- */
.nrx-dose {
  display: flex; align-items: center; gap: 1rem;
  padding: 0.15rem 0.2rem;
}
.nrx-dose-time {
  font-family: var(--nrx-mono); font-size: 0.95rem; color: var(--nrx-text-70);
  flex: 0 0 4.6rem; letter-spacing: 0.02em;
}
.nrx-dose-main { flex: 1; min-width: 0; }
.nrx-dose-drug { font-size: 1rem; font-weight: 500; }
.nrx-dose-detail { font-size: 0.76rem; color: var(--nrx-text-50); margin-top: 0.12rem; }

/* --- status pill ---------------------------------------------------------- */
.nrx-pill {
  display: inline-flex; align-items: center; gap: 0.4rem;
  border-radius: 999px; padding: 0.32rem 0.7rem;
  font-family: var(--nrx-mono); font-size: 0.58rem;
  letter-spacing: 0.13em; text-transform: uppercase; white-space: nowrap;
}
.nrx-pill .nrx-dot { width: 5px; height: 5px; }
.nrx-pill-accent  { background: rgba(132, 220, 220, 0.10); color: var(--nrx-accent);       border: 1px solid rgba(132, 220, 220, 0.28); }
.nrx-pill-accent  .nrx-dot { background: var(--nrx-accent); }
.nrx-pill-warn    { background: rgba(234, 205, 153, 0.10); color: var(--nrx-amber);        border: 1px solid rgba(234, 205, 153, 0.28); }
.nrx-pill-warn    .nrx-dot { background: var(--nrx-amber); }
.nrx-pill-muted   { background: var(--nrx-surface);        color: var(--nrx-text-38);      border: 1px solid var(--nrx-hairline); }
.nrx-pill-muted   .nrx-dot { background: var(--nrx-text-38); }

/* --- refill rows: drug, thin track, days ---------------------------------- */
.nrx-refill { margin-top: 0.7rem; display: flex; flex-direction: column; gap: 0.55rem; }
.nrx-refill-row { display: flex; align-items: center; gap: 0.7rem; font-size: 0.78rem; }
.nrx-refill-row .d { flex: 0 0 42%; color: var(--nrx-text-70); }
.nrx-refill-row .bar {
  flex: 1; height: 3px; border-radius: 999px;
  background: var(--nrx-hairline); overflow: hidden;
}
.nrx-refill-row .bar > span { display: block; height: 100%; border-radius: 999px; }
.nrx-refill-row .n { font-family: var(--nrx-mono); font-size: 0.66rem; color: var(--nrx-text-50); }
/* No bar is drawn when there is no real supply figure — see the view. */
.nrx-refill-row .u { font-family: var(--nrx-mono); font-size: 0.6rem; color: var(--nrx-text-38); letter-spacing: 0.08em; }
/* Why the figure is unavailable. Quiet, but present — the gap is a flagged
   schema gap, and a bare "not tracked" reads as a bug instead. */
.nrx-refill-note {
  margin-top: 0.7rem; padding-top: 0.6rem; border-top: 1px solid var(--nrx-hairline);
  font-size: 0.66rem; line-height: 1.45; color: var(--nrx-text-38);
}

/* --- medication chips (dashboard patient header) -------------------------- */
.nrx-meds { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.6rem; }
.nrx-med {
  display: flex; flex-direction: column; gap: 0.15rem;
  background: var(--nrx-surface); border: 1px solid var(--nrx-hairline);
  border-radius: 14px; padding: 0.55rem 0.85rem;
}
.nrx-med .n { font-size: 0.85rem; font-weight: 500; }
.nrx-med .d { font-family: var(--nrx-mono); font-size: 0.58rem; color: var(--nrx-text-38); letter-spacing: 0.05em; }

/* --- live animated background --------------------------------------------
   The mockup paints this on a <canvas>: four drifting radial-gradient blobs
   composited with 'lighter', plus 90 twinkling stars, driven by
   requestAnimationFrame. That cannot be ported directly — Streamlit strips
   <script> from st.markdown, so a render loop can only run inside a
   components.v1.html iframe, which is sandboxed and could not sit BEHIND page
   content.

   So it is rebuilt in pure CSS, which runs in the main DOM and layers behind
   everything. Same four blob colours as the mockup's own `blobs` array
   (rgb 70,190,210 / 120,130,240 / 200,170,110 / 60,150,200) and the same
   slow, non-uniform drift periods, so no two blobs ever sync up.

   Depth is real 3D, not just parallax: the container sets `perspective` and
   each star layer sits at a different translateZ, so layers genuinely scale
   differently as they drift.

   prefers-reduced-motion disables all of it — the mockup checks the same
   media query before starting its loop. */
.nrx-bg {
  position: fixed; inset: 0; z-index: 0;
  overflow: hidden; pointer-events: none;
  perspective: 600px;
}
/* Streamlit's own content has no stacking context of its own here, so it is
   lifted above the fixed background explicitly. */
[data-testid="stMain"] .block-container { position: relative; z-index: 1; }

.nrx-bg i {
  position: absolute; display: block; border-radius: 50%;
  /* 'lighter' in canvas ≈ screen blending for additive colour build-up. */
  mix-blend-mode: screen;
  filter: blur(28px);
}
.nrx-bg .b1 {
  width: 62vmax; height: 62vmax; left: -12%; top: -18%;
  background: radial-gradient(circle, rgba(70,190,210,.16) 0%, rgba(70,190,210,.05) 50%, transparent 70%);
  animation: nrx-drift-a 34s ease-in-out infinite;
}
.nrx-bg .b2 {
  width: 54vmax; height: 54vmax; right: -14%; top: -22%;
  background: radial-gradient(circle, rgba(120,130,240,.14) 0%, rgba(120,130,240,.045) 50%, transparent 70%);
  animation: nrx-drift-b 26s ease-in-out infinite;
}
.nrx-bg .b3 {
  width: 68vmax; height: 68vmax; right: -6%; bottom: -30%;
  background: radial-gradient(circle, rgba(200,170,110,.13) 0%, rgba(200,170,110,.04) 50%, transparent 70%);
  animation: nrx-drift-c 30s ease-in-out infinite;
}
.nrx-bg .b4 {
  width: 56vmax; height: 56vmax; left: -8%; bottom: -26%;
  background: radial-gradient(circle, rgba(60,150,200,.13) 0%, rgba(60,150,200,.04) 50%, transparent 70%);
  animation: nrx-drift-d 22s ease-in-out infinite;
}

/* Star layers. Each is a tiled field of 1px dots; the tile repeats, so a
   slow vertical translate reads as continuous drift with no seam. */
.nrx-bg u {
  position: absolute; inset: -50% -50%; display: block;
  background-repeat: repeat;
}
.nrx-bg .s1 {
  background-image:
    radial-gradient(1px 1px at 20px 30px, rgba(255,255,255,.40), transparent),
    radial-gradient(1px 1px at 140px 90px, rgba(255,255,255,.32), transparent),
    radial-gradient(1px 1px at 90px 170px, rgba(255,255,255,.36), transparent),
    radial-gradient(1px 1px at 210px 40px, rgba(255,255,255,.28), transparent);
  background-size: 260px 260px;
  transform: translateZ(-120px);
  animation: nrx-stars 120s linear infinite, nrx-twinkle 7s ease-in-out infinite;
}
.nrx-bg .s2 {
  background-image:
    radial-gradient(1.6px 1.6px at 60px 120px, rgba(255,255,255,.30), transparent),
    radial-gradient(1.4px 1.4px at 180px 60px, rgba(255,255,255,.24), transparent),
    radial-gradient(1.6px 1.6px at 120px 220px, rgba(255,255,255,.28), transparent);
  background-size: 340px 340px;
  transform: translateZ(-40px);
  animation: nrx-stars 80s linear infinite reverse, nrx-twinkle 11s ease-in-out infinite;
}
.nrx-bg .s3 {
  background-image:
    radial-gradient(2px 2px at 100px 80px, rgba(132,220,220,.26), transparent),
    radial-gradient(1.8px 1.8px at 240px 200px, rgba(255,255,255,.20), transparent);
  background-size: 420px 420px;
  transform: translateZ(20px);
  animation: nrx-stars 55s linear infinite, nrx-twinkle 9s ease-in-out infinite;
}

@keyframes nrx-drift-a {
  0%,100% { transform: translate3d(0,0,0)        scale(1);    }
  50%     { transform: translate3d(7vw,5vh,0)    scale(1.12); }
}
@keyframes nrx-drift-b {
  0%,100% { transform: translate3d(0,0,0)        scale(1.06); }
  50%     { transform: translate3d(-6vw,7vh,0)   scale(1);    }
}
@keyframes nrx-drift-c {
  0%,100% { transform: translate3d(0,0,0)        scale(1);    }
  50%     { transform: translate3d(-5vw,-6vh,0)  scale(1.14); }
}
@keyframes nrx-drift-d {
  0%,100% { transform: translate3d(0,0,0)        scale(1.08); }
  50%     { transform: translate3d(6vw,-5vh,0)   scale(1);    }
}
@keyframes nrx-stars {
  from { background-position: 0 0; }
  to   { background-position: 0 -1000px; }
}
@keyframes nrx-twinkle {
  0%,100% { opacity: .55; }
  50%     { opacity: 1;   }
}

@media (prefers-reduced-motion: reduce) {
  .nrx-bg i, .nrx-bg u { animation: none !important; }
}

/* --- home hero ------------------------------------------------------------ */
.nrx-hero { padding: 4.5rem 0 2.5rem; max-width: 46rem; }
.nrx-hero h1 {
  font-family: var(--nrx-serif); font-weight: 400;
  font-size: clamp(2.6rem, 6vw, 4.2rem); line-height: 1.05;
  letter-spacing: -0.02em; margin: 0.7rem 0 0;
}
.nrx-hero h1 em { font-style: italic; color: var(--nrx-accent); }
.nrx-hero p {
  font-size: 1.02rem; color: var(--nrx-text-70);
  margin: 1.1rem 0 0; max-width: 34rem; line-height: 1.6;
}
/* --- hero preview panel (fills the right of the hero) ----------------------
   The mockup puts an animated 340-point orb here, drawn on canvas. That needs
   a render loop, so instead this space shows what the product actually does:
   a dose row and the citation chip the headline promises. Marked EXAMPLE, and
   built from the same card/pill/eyebrow primitives as the real Today view, so
   the landing page cannot drift from the product's own look.

   Tilted with a real 3D rotateY and floated, tying it to the animated
   background rather than sitting flat on top of it. */
.nrx-preview-wrap { perspective: 1100px; padding-top: 1.5rem; }
.nrx-preview {
  background: linear-gradient(160deg, rgba(255,255,255,.055), rgba(255,255,255,.02));
  border: 1px solid var(--nrx-hairline);
  border-radius: 20px; padding: 1.15rem 1.25rem;
  transform: rotateY(-13deg) rotateX(4deg);
  transform-style: preserve-3d;
  box-shadow: 0 30px 70px rgba(0,0,0,.55);
  animation: nrx-float 9s ease-in-out infinite;
}
.nrx-preview .row {
  display: flex; align-items: center; gap: .7rem;
  padding: .6rem 0; border-bottom: 1px solid rgba(255,255,255,.055);
}
.nrx-preview .row:last-of-type { border-bottom: 0; }
.nrx-preview .tm {
  font-family: var(--nrx-mono); font-size: .78rem;
  color: var(--nrx-text-50); flex: 0 0 3.4rem;
}
.nrx-preview .nm { flex: 1; font-size: .92rem; }
.nrx-preview .nm small {
  display: block; font-size: .68rem; color: var(--nrx-text-38); margin-top: .1rem;
}
.nrx-preview .cite {
  margin-top: .9rem; padding-top: .85rem;
  border-top: 1px solid rgba(255,255,255,.08);
  font-size: .78rem; color: var(--nrx-text-70); line-height: 1.5;
}
.nrx-preview .cite .chip {
  display: inline-flex; align-items: center; gap: .35rem;
  margin-top: .55rem; padding: .3rem .6rem; border-radius: 999px;
  background: rgba(132,220,220,.10); border: 1px solid rgba(132,220,220,.28);
  color: var(--nrx-accent);
  font-family: var(--nrx-mono); font-size: .55rem; letter-spacing: .12em;
}
@keyframes nrx-float {
  0%,100% { transform: rotateY(-13deg) rotateX(4deg) translateY(0); }
  50%     { transform: rotateY(-10deg) rotateX(3deg) translateY(-12px); }
}
@media (prefers-reduced-motion: reduce) { .nrx-preview { animation: none; } }
@media (max-width: 900px) { .nrx-preview { transform: none; animation: none; } }

.nrx-trust { display: flex; flex-wrap: wrap; gap: 0.9rem; margin-top: 3rem; }
.nrx-trust .nrx-card { flex: 1 1 15rem; }
.nrx-trust .t { font-size: 0.92rem; font-weight: 500; margin-top: 0.5rem; }
.nrx-trust .b { font-size: 0.78rem; color: var(--nrx-text-50); margin-top: 0.3rem; line-height: 1.5; }

/* --- auth screens --------------------------------------------------------- */
.nrx-auth { max-width: 26rem; margin: 3.5rem auto 0; }
.nrx-auth h2 {
  font-family: var(--nrx-serif); font-weight: 400; font-size: 2.1rem;
  letter-spacing: -0.015em; margin: 0.4rem 0 1.4rem;
}
.nrx-auth-note {
  font-size: 0.74rem; color: var(--nrx-text-50);
  line-height: 1.5; margin-top: 0.9rem;
}

/* --- citation chip -------------------------------------------------------- */
.nrx-chip-src {
  font-family: var(--nrx-mono); font-size: 0.55rem; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--nrx-text-38);
}

/* --- diff (pending schedule change) --------------------------------------- */
.nrx-diff { display: flex; align-items: center; gap: 0.7rem; font-family: var(--nrx-mono); font-size: 0.95rem; }
.nrx-diff .from { color: var(--nrx-text-38); text-decoration: line-through; }
.nrx-diff .arrow { color: var(--nrx-text-38); }
.nrx-diff .to { color: var(--nrx-accent); }
"""


def inject() -> None:
    """Emit the design layer. Call once, from `app/app.py`, before any view.

    Idempotent within a session: Streamlit re-runs the whole script on every
    interaction, so this necessarily re-emits on each rerun — that is fine (a
    repeated identical <style> block is a no-op) and is why there is no
    session_state guard here, which would break on the first rerun after a
    widget interaction anyway.
    """
    st.markdown(
        f"<style>:root {{{_TOKENS}}}\n{_STREAMLIT_INTERNALS}\n{_COMPONENTS}</style>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Render helpers.
#
# Each returns an HTML string for `st.markdown(..., unsafe_allow_html=True)`
# rather than writing to the page itself, so callers stay in control of layout
# (columns, containers) and these compose inside f-strings.
#
# Every caller-supplied value is escaped. Drug names and dose_text come from the
# database, and this project's own extraction path can put arbitrary OCR'd text
# into them — unescaped, a stray '<' would silently break the row's markup.
# ---------------------------------------------------------------------------


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def eyebrow(text: str) -> str:
    """Uppercase mono section label — the mockup's most-repeated motif."""
    return f'<div class="nrx-eyebrow">{_esc(text)}</div>'


def status_pill(label: str, tone: str = "muted") -> str:
    """A status pill. `tone` is one of accent / warn / muted."""
    tone = tone if tone in {"accent", "warn", "muted"} else "muted"
    return (
        f'<span class="nrx-pill nrx-pill-{tone}">'
        f'<span class="nrx-dot"></span>{_esc(label)}</span>'
    )


def section_heading(eyebrow_text: str, title: str) -> str:
    """Mono eyebrow above a large serif title."""
    return (
        f'<div class="nrx-section">{eyebrow(eyebrow_text)}'
        f"<h2>{_esc(title)}</h2></div>"
    )


def group_header(label: str, count_text: str = "") -> str:
    """Day-part group header: eyebrow, hairline rule, right-aligned count."""
    return (
        f'<div class="nrx-group">{eyebrow(label)}'
        f'<span class="rule"></span>'
        f'<span class="count">{_esc(count_text)}</span></div>'
    )


def stat_card(label: str, value: str, unit: str = "", delta: str = "") -> str:
    """Mono eyebrow label, large serif value, optional unit and delta line."""
    unit_html = f'<span class="nrx-stat-unit">{_esc(unit)}</span>' if unit else ""
    delta_html = f'<div class="nrx-stat-delta">{_esc(delta)}</div>' if delta else ""
    return (
        f'<div class="nrx-card">{eyebrow(label)}'
        f'<div style="margin-top:.5rem"><span class="nrx-stat-value">{_esc(value)}</span>'
        f"{unit_html}</div>{delta_html}</div>"
    )


def card_row(*cards: str) -> str:
    """Lay cards side by side at a common height. Non-interactive cards only."""
    return f'<div class="nrx-card-row">{"".join(cards)}</div>'


def dose_row(time_text: str, drug: str, detail: str) -> str:
    """Left portion of a dose row: mono time, drug name, detail line.

    The status pill and action buttons are rendered by the caller into adjacent
    Streamlit columns, because the buttons must remain real `st.button` widgets.
    """
    detail_html = f'<div class="nrx-dose-detail">{_esc(detail)}</div>' if detail else ""
    return (
        f'<div class="nrx-dose"><div class="nrx-dose-time">{_esc(time_text)}</div>'
        f'<div class="nrx-dose-main"><div class="nrx-dose-drug">{_esc(drug)}</div>'
        f"{detail_html}</div></div>"
    )


def diff(from_value: str, to_value: str) -> str:
    """Struck-through old value, arrow, accent-colored new value."""
    return (
        f'<div class="nrx-diff"><span class="from">{_esc(from_value)}</span>'
        f'<span class="arrow">&rarr;</span>'
        f'<span class="to">{_esc(to_value)}</span></div>'
    )


# ---------------------------------------------------------------------------
# Plotly
# ---------------------------------------------------------------------------

# Adherence thresholds share one definition so the bar colors, the heatmap
# legend and any future badge cannot drift apart. Amber is the design's caution
# tone; red is reserved for genuinely bad (<70%) and is the one color here that
# is NOT from the mockup palette — the mockup has no "bad" state to borrow, and
# muting a sub-70% adherence bar into the brand palette would make the worst
# number on the page the easiest one to miss.
ADHERENCE_GOOD = ACCENT  # >= 90%
ADHERENCE_FAIR = AMBER  # 70-89%
ADHERENCE_POOR = "#E38B8B"  # < 70%

# Heatmap ramp — mirrors chartSequentialColors in config.toml. Plotly needs it
# as explicit (position, color) stops for a go.Heatmap colorscale.
HEATMAP_SCALE = [
    [0.0, "#0E141A"],
    [0.25, "#284045"],
    [0.5, "#426D70"],
    [0.75, "#5D999B"],
    [1.0, ACCENT],
]


def adherence_color(pct: float) -> str:
    """Threshold color for an adherence percentage. Single source for charts."""
    if pct < 70:
        return ADHERENCE_POOR
    if pct < 90:
        return ADHERENCE_FAIR
    return ADHERENCE_GOOD


def style_plotly(fig, height: int = 320) -> None:
    """Apply the design language to a Plotly figure, in place.

    `config.toml`'s chartCategoricalColors/chartSequentialColors already hand
    Plotly the palette. What config cannot set is layout chrome — the paper and
    plot backgrounds (which default to opaque white and would punch a bright
    rectangle into a dark page), fonts, and gridline colors. That is all this
    does; it deliberately does not touch traces or data.
    """
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Manrope, sans-serif", size=12, color="rgba(241,244,246,0.70)"),
        height=height,
        margin=dict(l=10, r=10, t=10, b=10),
        showlegend=False,
        hoverlabel=dict(
            bgcolor="#12161C",
            bordercolor="rgba(255,255,255,0.14)",
            font=dict(family="Manrope, sans-serif", color=TEXT, size=12),
        ),
    )
    axis = dict(
        gridcolor="rgba(255,255,255,0.06)",
        zerolinecolor="rgba(255,255,255,0.10)",
        linecolor="rgba(255,255,255,0.10)",
        tickfont=dict(family="JetBrains Mono, monospace", size=10,
                      color="rgba(241,244,246,0.38)"),
        title_font=dict(family="JetBrains Mono, monospace", size=10,
                        color="rgba(241,244,246,0.38)"),
    )
    fig.update_xaxes(**axis)
    fig.update_yaxes(**axis)


def live_background() -> str:
    """The animated background layer: four drifting blobs and three star fields.

    Pure CSS (see `_COMPONENTS`) — no canvas, no script, so it renders in the
    main DOM behind page content rather than inside a sandboxed component
    iframe. Emit once per page, before anything else.
    """
    return (
        '<div class="nrx-bg" aria-hidden="true">'
        '<i class="b1"></i><i class="b2"></i><i class="b3"></i><i class="b4"></i>'
        '<u class="s1"></u><u class="s2"></u><u class="s3"></u>'
        "</div>"
    )


def hero_preview() -> str:
    """The hero's right-hand panel: a small, explicitly-labelled EXAMPLE of the
    product — two dose rows and the citation chip the headline promises.

    The rows are an illustration on a public marketing page, not anyone's data,
    and they say EXAMPLE so they cannot be mistaken for a real schedule. The
    home page never touches `app/db.py`.
    """
    rows = [
        ("08:00", "Metformin", "500 mg · with food", "Taken", "accent"),
        ("19:00", "Lisinopril", "10 mg", "Due now", "warn"),
    ]
    row_html = "".join(
        f'<div class="row"><span class="tm">{_esc(t)}</span>'
        f'<span class="nm">{_esc(drug)}<small>{_esc(detail)}</small></span>'
        f"{status_pill(label, tone)}</div>"
        for t, drug, detail, label, tone in rows
    )
    return (
        '<div class="nrx-preview-wrap"><div class="nrx-preview">'
        f'{eyebrow("EXAMPLE — TODAY")}'
        f'<div style="margin-top:.7rem">{row_html}</div>'
        '<div class="cite">“Skipping one evening dose is common — take the next '
        "one at its normal time.”"
        '<span class="chip">FDA LABEL · DOSAGE &amp; ADMINISTRATION</span></div>'
        "</div></div>"
    )


def brand(href: str | None = None) -> str:
    """Wordmark + badge, the left half of the mockup's header.

    Pass `href` to make it a link. Callers use `href="/"` on the signed-OUT
    screens so the wordmark returns to the home page.

    **Do not pass href on the signed-in app page.** Following a real link is a
    full browser navigation, and `st.session_state` is per-session — so it
    would sign the user out, which is not what clicking a logo should do.
    """
    inner = (
        '<span class="nrx-logo">NeuroRx</span>'
        '<span class="nrx-logo-badge">AI</span>'
    )
    if href:
        inner = f'<a class="nrx-logo-link" href="{_esc(href)}" target="_self">{inner}</a>'
    return f'<div class="nrx-header"><div>{inner}</div></div>'


def safety_ticker() -> None:
    """The persistent safety notice. Renders directly, and takes no arguments.

    Deliberately not a string-returning helper like everything else in this
    module: this is a safety control, and a caller must not be able to build the
    page while forgetting to write it out, or to pass different text. It carries
    no dismiss control and is rendered above the tab strip, so it appears on
    every tab and cannot be closed.

    Visually quieter than the `st.warning` box it replaces — that trade is
    recorded as a flagged deviation in the design spec, not made silently.
    """
    st.markdown(
        '<div class="nrx-safety"><span class="nrx-dot"></span>'
        "Organizational assistant — not medical advice. "
        "Clinical facts come from deterministic lookups with citations. "
        "Emergencies: call 911."
        "</div>",
        unsafe_allow_html=True,
    )
