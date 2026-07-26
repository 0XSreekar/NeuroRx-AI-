"""NeuroRx AI — Dashboard view (Task 3.6).

Adherence analytics for the patient and, in caregiver mode, a secondary
audience checking in on someone else's adherence (`ARCHITECTURE.md` §1's
caregiver persona). Every number on this page comes from
`neurorx.gold.adherence_facts` (Delta), never Lakebase — the same
OLTP-vs-analytics split `app/db.py`'s own module docstring establishes and
Task 3.5's Today view already follows, now completed on the analytics side.

## Reusing the already-verified UC function instead of re-deriving a streak

`db.get_adherence_stats()` (added this task) calls the existing
`neurorx.app.get_adherence_stats` UC function (Task 2.4) directly for the
header stat cards — overall %, streak, most-missed drug, most-missed
daypart. **This view deliberately does not recompute a streak from raw
`adherence_summary()` rows.** The streak rule (consecutive days ending
yesterday, capped by the window) has its own non-trivial edge cases already
found and fixed once by running the real logic (an empty-history bug that
used to report `current_streak_days=0` instead of "no data" at all) — a
second Python implementation here would risk exactly the kind of two
silently-diverging definitions of the same fact this project has already
hit once (Task 3.5's day-part boundaries, before that fix). One correct
implementation, called from the chat agent and this dashboard alike.

The calendar heatmap and time-of-day pattern, by contrast, are simple
sum/group-by aggregations over `adherence_summary()`'s already-fetched raw
rows — not complex domain rules, so they're built here in Python rather
than adding two more narrow SQL functions to `app/db.py` for straightforward
display shaping.

## Genie embedding — verified this session, not assumed

Databricks' current docs confirm **"Embed a Genie Space" is a real,
if Beta, capability**: a Genie Space can be embedded as an iframe in an
external app, but only after a workspace admin has configured allowed
embedding surfaces, and only for a Genie Space that already exists with the
right permissions. **This project has no Genie Space yet** —
`ARCHITECTURE.md`'s own build-order cut list puts "Genie" first in line to
be cut under time pressure, meaning it may never exist for this demo. So
this view checks for an optional `GENIE_EMBED_URL` environment variable
(not part of `app/config.py`'s required nine — Genie isn't a Phase 3
dependency) and only renders a real iframe if it's set; otherwise it renders
a prominent deep-link card and says so, exactly as this task's own
instruction asks, rather than emitting a broken iframe pointed at nothing.
"""

import html
import os
from collections import defaultdict
from datetime import date, timedelta

import plotly.graph_objects as go
import streamlit as st

from app import db, theme

_WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAY_PART_ORDER = ["morning", "afternoon", "evening", "night"]
_HEATMAP_WINDOW_DAYS = 90

# Adherence threshold colors live in app/theme.py (ADHERENCE_GOOD/FAIR/POOR and
# HEATMAP_SCALE) so the bars, the heatmap ramp and any badge share one
# definition. This module keeps only the emoji indicator used in hover text,
# which is text, not color.


def _get_adherence_indicator(pct: float) -> str:
    """Emoji indicator for hover text (charts get their color from theme)."""
    if pct >= 90:
        return "🟢"
    elif pct >= 70:
        return "🟡"
    else:
        return "🔴"


def render(patient_id: str) -> None:
    """Entry point called by app/app.py inside the Dashboard tab."""
    if not patient_id:
        st.info("No patient selected. Choose a patient ID in the header to see adherence data.")
        return

    _render_patient_header(patient_id)

    caregiver_mode = st.toggle("Caregiver mode", value=False)

    with st.spinner("Loading adherence data..."):
        try:
            stats = db.get_adherence_stats(patient_id, window_days=30)
            daily_rows = db.adherence_summary(patient_id, days=_HEATMAP_WINDOW_DAYS)
        except Exception as exc:  # noqa: BLE001 — surface any analytics-store failure as one message
            # The dashboard reads adherence from Delta (gold.adherence_facts) via
            # the SQL warehouse — the deliberate OLTP-vs-analytics split (F9). That
            # path needs the live Databricks workspace: the warehouse discovery
            # (`get_warehouse_http_path()`) and the Delta query only work there.
            # On the local demo path (NEURORX_LOCAL_PG) there is no warehouse, so
            # rather than hang on SDK retries or dump a raw traceback, degrade to a
            # clear, honest notice. The Today tab still works fully off Lakebase.
            st.warning(
                "**Adherence analytics require the Databricks workspace.**\n\n"
                "This dashboard reads from `neurorx.gold.adherence_facts` (Delta) "
                "through the SQL warehouse — the analytics store, separate from the "
                "live Lakebase data the Today tab uses. Connect a workspace "
                "(populate `.env` and run the Lakeflow pipeline) to see adherence "
                "trends here."
            )
            with st.expander("Technical detail"):
                st.caption(f"`{type(exc).__name__}: {exc}`")
            return

    _render_header_stats(stats)

    col1, col2 = st.columns(2)
    with col1:
        _render_adherence_by_drug(stats)
    with col2:
        _render_time_of_day_pattern(daily_rows)

    _render_calendar_heatmap(daily_rows)

    if caregiver_mode:
        st.divider()
        _render_caregiver_panel()


# ---------------------------------------------------------------------------
# Patient identity + medication list
# ---------------------------------------------------------------------------


def _render_patient_header(patient_id: str) -> None:
    """Show who this dashboard is about — the patient's name and their active
    medications — above the adherence metrics. Reads from Lakebase (local
    Postgres on the demo path); degrades silently to just the ID if the
    lookup isn't available so it never blocks the rest of the view."""
    try:
        patient = db.get_patient(patient_id)
    except Exception:  # noqa: BLE001 — identity is nice-to-have, never fatal
        patient = None

    if not patient:
        return

    st.markdown(
        theme.section_heading("PATIENT", patient["display_name"]),
        unsafe_allow_html=True,
    )

    meds = patient.get("medications") or []
    if not meds:
        st.markdown(
            theme.eyebrow("No active medications on file for this patient"),
            unsafe_allow_html=True,
        )
        return

    chips = []
    for m in meds:
        times = ", ".join(
            t.strftime("%H:%M") if hasattr(t, "strftime") else str(t)[:5]
            for t in (m.get("dose_times") or [])
        )
        detail = " · ".join(
            part
            for part in (
                m.get("dose_text") or "",
                f"{m.get('times_per_day')}×/day" if m.get("times_per_day") else "",
                times,
                m.get("timing_notes") or "",
            )
            if part
        )
        chips.append(
            '<div class="nrx-med"><span class="n">'
            f'{html.escape(str(m["drug_name"]))}</span>'
            f'<span class="d">{html.escape(detail)}</span></div>'
        )

    st.markdown(
        f'{theme.eyebrow("CURRENT MEDICATIONS")}'
        f'<div class="nrx-meds">{"".join(chips)}</div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Header stat cards (Requirement 1)
# ---------------------------------------------------------------------------


def _render_header_stats(stats: dict) -> None:
    """Four stat cards, matching the mockup's stat row.

    Uses `theme.stat_card` rather than `st.metric`: the mockup's card is a mono
    eyebrow over a large serif value, which `st.metric`'s fixed label/value/delta
    structure cannot express. The "no data" states stay explicit — an absent
    adherence figure renders as an em dash, never as 0% or 100%.
    """
    pct = stats["overall_adherence_pct"]
    streak = stats["current_streak_days"]
    most_missed = stats["most_missed_drug"]
    most_missed_part = stats["most_missed_daypart"]

    cards = [
        theme.stat_card(
            "OVERALL ADHERENCE · 30D",
            f"{pct:.0f}" if pct is not None else "—",
            unit="%" if pct is not None else "",
            delta="Share of scheduled doses taken on time",
        ),
        theme.stat_card(
            "CURRENT STREAK",
            str(streak) if streak is not None else "—",
            unit="days" if streak is not None else "",
            delta="Consecutive days ending yesterday at ≥90%",
        ),
        theme.stat_card(
            "MOST-MISSED DRUG",
            most_missed["drug_name"] if most_missed else "None",
            delta="Lowest adherence this month",
        ),
        theme.stat_card(
            "MOST-MISSED TIME",
            most_missed_part["daypart"].title() if most_missed_part else "None",
            delta="Day part with the most missed doses",
        ),
    ]

    for col, card in zip(st.columns(4, gap="small"), cards):
        with col:
            st.markdown(card, unsafe_allow_html=True)

    if os.getenv("NEURORX_LOCAL_PG"):
        source = (
            "Source: local Postgres dose_events (local-demo path; "
            "the deployed app reads neurorx.gold.adherence_facts in Delta)"
        )
    else:
        source = (
            "Source: neurorx.app.get_adherence_stats "
            "→ neurorx.gold.adherence_facts (Delta)"
        )
    st.markdown(
        f'<div style="margin-top:.7rem">{theme.eyebrow(source)}</div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Adherence % by drug — horizontal bar chart (Requirement 2)
# ---------------------------------------------------------------------------


def _render_adherence_by_drug(stats: dict) -> None:
    st.markdown(
        theme.section_heading("BY MEDICATION", "Adherence by drug"),
        unsafe_allow_html=True,
    )
    by_drug = stats["adherence_by_drug"]
    if not by_drug:
        st.info("No adherence data available yet. Check back once doses are logged.")
        return

    by_drug_sorted = sorted(by_drug, key=lambda d: d["adherence_pct"])

    # Threshold colors come from theme.adherence_color so the bars, the heatmap
    # legend and the caption below cannot drift apart.
    bar_colors = [theme.adherence_color(d["adherence_pct"]) for d in by_drug_sorted]

    fig = go.Figure(
        go.Bar(
            x=[d["adherence_pct"] for d in by_drug_sorted],
            y=[d["drug_name"] for d in by_drug_sorted],
            orientation="h",
            marker=dict(color=bar_colors),
            text=[f"{d['adherence_pct']:.0f}%" for d in by_drug_sorted],
            textposition="outside",
            textfont=dict(family="JetBrains Mono, monospace", size=11),
            hovertemplate="%{y}: %{x:.0f}%<extra></extra>",
        )
    )
    theme.style_plotly(fig, height=max(240, 54 * len(by_drug_sorted)))
    fig.update_layout(margin=dict(l=10, r=48, t=8, b=24))
    fig.update_xaxes(range=[0, 108], title="")
    fig.update_yaxes(title="")
    st.plotly_chart(fig, use_container_width=True)
    st.markdown(
        theme.eyebrow("Worst-first — drugs needing attention at top"),
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Calendar heatmap — last 90 days (Requirement 3)
# ---------------------------------------------------------------------------


def _aggregate_daily_adherence(daily_rows: list[dict]) -> dict[date, float]:
    """Collapses `adherence_summary()`'s (drug, day_part)-grain rows to one
    adherence % per calendar day — summed across drugs and day parts, not
    averaged, since planned/taken counts are the honest unit to sum; a
    straight average-of-percentages would over-weight a drug taken once a
    day against one taken three times a day.
    """
    planned_by_date: dict[date, int] = defaultdict(int)
    taken_by_date: dict[date, int] = defaultdict(int)
    for row in daily_rows:
        planned_by_date[row["event_date"]] += row["planned_doses"]
        taken_by_date[row["event_date"]] += row["taken_doses"]

    return {
        d: (taken_by_date[d] / planned_by_date[d] * 100 if planned_by_date[d] else None)
        for d in planned_by_date
    }


def _render_calendar_heatmap(daily_rows: list[dict]) -> None:
    st.markdown(
        theme.section_heading(
            f"LAST {_HEATMAP_WINDOW_DAYS} DAYS", "Dose heatmap"
        ),
        unsafe_allow_html=True,
    )

    pct_by_date = _aggregate_daily_adherence(daily_rows)
    if not pct_by_date:
        st.info("No adherence data available yet for this period.")
        return

    today = date.today()
    start_date = today - timedelta(days=_HEATMAP_WINDOW_DAYS - 1)
    num_weeks = (_HEATMAP_WINDOW_DAYS // 7) + 1

    z = [[None] * num_weeks for _ in _WEEKDAY_LABELS]
    hover_text = [[""] * num_weeks for _ in _WEEKDAY_LABELS]
    for i in range(_HEATMAP_WINDOW_DAYS):
        current = start_date + timedelta(days=i)
        week_idx = (current - start_date).days // 7
        weekday_idx = current.weekday()
        pct = pct_by_date.get(current)
        z[weekday_idx][week_idx] = pct
        if pct is not None:
            status_emoji = _get_adherence_indicator(pct)
            hover_text[weekday_idx][week_idx] = f"{current.strftime('%a, %b %d')}: {pct:.0f}% {status_emoji}"
        else:
            hover_text[weekday_idx][week_idx] = f"{current.strftime('%a, %b %d')}: No data"

    fig = go.Figure(
        go.Heatmap(
            z=z,
            y=_WEEKDAY_LABELS,
            x=[f"Wk {w + 1}" for w in range(num_weeks)],
            colorscale=theme.HEATMAP_SCALE,
            zmin=0,
            zmax=100,
            hoverongaps=False,
            xgap=3,
            ygap=3,
            text=hover_text,
            hoverinfo="text",
            colorbar=dict(
                title="",
                ticksuffix="%",
                thickness=6,
                outlinewidth=0,
                tickfont=dict(family="JetBrains Mono, monospace", size=9),
            ),
        )
    )
    theme.style_plotly(fig, height=300)
    fig.update_layout(margin=dict(l=10, r=10, t=8, b=8))
    fig.update_xaxes(showgrid=False, title="")
    fig.update_yaxes(showgrid=False, title="")
    st.plotly_chart(fig, use_container_width=True)
    st.markdown(
        theme.eyebrow("Low ▸ high · hover a cell for its date and percentage"),
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Time-of-day pattern — grouped bars of missed doses by day part (Requirement 4)
# ---------------------------------------------------------------------------


def _render_time_of_day_pattern(daily_rows: list[dict]) -> None:
    st.markdown(
        theme.section_heading("BY DAY PART", "When doses get missed"),
        unsafe_allow_html=True,
    )

    missed_by_part: dict[str, int] = defaultdict(int)
    skipped_by_part: dict[str, int] = defaultdict(int)
    for row in daily_rows:
        missed_by_part[row["day_part"]] += row["missed_doses"]
        skipped_by_part[row["day_part"]] += row["skipped_doses"]

    if not any(missed_by_part.values()) and not any(skipped_by_part.values()):
        st.info("No missed or skipped doses in this window.")
        return

    day_part_labels = {
        "morning": "Morning",
        "afternoon": "Afternoon",
        "evening": "Evening",
        "night": "Night",
    }

    fig = go.Figure(
        data=[
            go.Bar(
                name="Missed",
                x=[day_part_labels.get(p, p.title()) for p in _DAY_PART_ORDER],
                y=[missed_by_part.get(p, 0) for p in _DAY_PART_ORDER],
                marker_color=theme.ADHERENCE_POOR,
                hovertemplate="Missed: %{y}<extra></extra>",
            ),
            go.Bar(
                name="Skipped",
                x=[day_part_labels.get(p, p.title()) for p in _DAY_PART_ORDER],
                y=[skipped_by_part.get(p, 0) for p in _DAY_PART_ORDER],
                marker_color=theme.ADHERENCE_FAIR,
                hovertemplate="Skipped: %{y}<extra></extra>",
            ),
        ]
    )
    theme.style_plotly(fig, height=300)
    fig.update_layout(
        barmode="group",
        bargap=0.45,
        margin=dict(l=10, r=10, t=8, b=36),
        showlegend=True,
        legend=dict(
            orientation="h", yanchor="top", y=-0.14, xanchor="left", x=0,
            font=dict(family="JetBrains Mono, monospace", size=10),
        ),
    )
    fig.update_xaxes(showgrid=False, title="")
    fig.update_yaxes(title="")
    st.plotly_chart(fig, use_container_width=True)
    st.markdown(
        theme.eyebrow(
            "Missed = unactioned after planned time · skipped = intentionally not taken"
        ),
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Caregiver mode — Genie space panel (Requirement 5)
# ---------------------------------------------------------------------------


def _render_caregiver_panel() -> None:
    st.markdown(
        theme.section_heading("CAREGIVER ANALYTICS", "Ask Genie"),
        unsafe_allow_html=True,
    )

    genie_embed_url = os.getenv("GENIE_EMBED_URL")
    genie_space_url = os.getenv("GENIE_SPACE_URL")

    if genie_embed_url:
        # Escaped even though this is operator-set, not user-set: a quote in the
        # value would otherwise close src="" and let the rest of the env var be
        # parsed as further iframe attributes. https-only for the same reason a
        # javascript:/data: value here would be worth catching before render.
        if not genie_embed_url.lower().startswith("https://"):
            st.error(
                "GENIE_EMBED_URL must be an https:// URL — refusing to embed "
                f"a {genie_embed_url.split(':', 1)[0]!r} URL."
            )
            return
        st.markdown(
            f'<iframe src="{html.escape(genie_embed_url, quote=True)}" '
            'width="100%" height="600" '
            'allow="clipboard-write" '
            'style="border:1px solid rgba(255,255,255,0.10);border-radius:18px;"></iframe>',
            unsafe_allow_html=True,
        )
        return

    # No Genie Space exists for this project (ARCHITECTURE.md §8's cut list puts
    # it first in line). Render the honest unconfigured state rather than an
    # iframe pointed at nothing.
    with st.container(border=True):
        st.markdown(
            f'{theme.eyebrow("NOT CONFIGURED")}'
            '<div style="margin-top:.5rem;font-size:.95rem">'
            "Natural-language questions about adherence trends become available "
            "once a workspace admin creates a Genie Space and enables embedding."
            "</div>",
            unsafe_allow_html=True,
        )
        if genie_space_url:
            st.link_button("Open Genie Space ↗", genie_space_url)
        st.markdown(
            theme.eyebrow(
                "Set GENIE_EMBED_URL (iframe) or GENIE_SPACE_URL (link) — no code change needed"
            ),
            unsafe_allow_html=True,
        )
