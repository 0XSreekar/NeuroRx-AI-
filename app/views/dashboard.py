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

import os
from collections import defaultdict
from datetime import date, timedelta

import plotly.graph_objects as go
import streamlit as st

from app import db

_WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAY_PART_ORDER = ["morning", "afternoon", "evening", "night"]
_HEATMAP_WINDOW_DAYS = 90

# Color thresholds for adherence visualization: green ≥90%, amber 70–89%, red <70%
_COLOR_SCALE = ["#d62728", "#ff7f0e", "#2ca02c"]  # red, amber, green
_COLOR_SCALE_REVERSED = ["#2ca02c", "#ff7f0e", "#d62728"]  # green, amber, red (for bar chart)


def _get_adherence_color(pct: float) -> str:
    """Return a color indicator for an adherence percentage."""
    if pct >= 90:
        return "🟢"
    elif pct >= 70:
        return "🟡"
    else:
        return "🔴"


def render(patient_id: str) -> None:
    """Entry point called by app/app.py inside the Dashboard tab."""
    if not patient_id:
        st.info("📋 No patient selected. Choose a patient ID in the sidebar to see adherence data.")
        return

    caregiver_mode = st.toggle("👨‍👩‍👧 Caregiver Mode", value=False)

    with st.spinner("📊 Loading adherence data..."):
        stats = db.get_adherence_stats(patient_id, window_days=30)
        daily_rows = db.adherence_summary(patient_id, days=_HEATMAP_WINDOW_DAYS)

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
# Header stat cards (Requirement 1)
# ---------------------------------------------------------------------------


def _render_header_stats(stats: dict) -> None:
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        pct = stats["overall_adherence_pct"]
        if pct is not None:
            adherence_label = f"{pct:.0f}%"
            adherence_color = _get_adherence_color(pct)
        else:
            adherence_label = "No Data"
            adherence_color = None
        st.metric("Overall Adherence (30d)", adherence_label, help="% of scheduled doses taken on time")

    with col2:
        streak = stats["current_streak_days"]
        streak_label = f"{streak}-day streak" if streak is not None else "No Data"
        st.metric("Current Streak", streak_label, help="Consecutive days ending yesterday with ≥90% adherence")

    with col3:
        most_missed = stats["most_missed_drug"]
        most_missed_label = most_missed["drug_name"] if most_missed else "None (🎉 Perfect!)"
        st.metric("Most-Missed Drug", most_missed_label, help="Drug with lowest adherence this month")

    with col4:
        most_missed_part = stats["most_missed_daypart"]
        time_label = most_missed_part["daypart"].title() if most_missed_part else "None (🎉 Perfect!)"
        st.metric("Most-Missed Time", time_label, help="Time of day with most missed doses")

    st.caption("📊 Source: `neurorx.app.get_adherence_stats` → `neurorx.gold.adherence_facts` (Delta)")


# ---------------------------------------------------------------------------
# Adherence % by drug — horizontal bar chart (Requirement 2)
# ---------------------------------------------------------------------------


def _render_adherence_by_drug(stats: dict) -> None:
    st.markdown("### Adherence by Drug")
    by_drug = stats["adherence_by_drug"]
    if not by_drug:
        st.info("📉 No adherence data available yet. Check back once doses are logged.")
        return

    by_drug_sorted = sorted(by_drug, key=lambda d: d["adherence_pct"])

    bar_colors = [
        "#d62728" if d["adherence_pct"] < 70
        else "#ff7f0e" if d["adherence_pct"] < 90
        else "#2ca02c"
        for d in by_drug_sorted
    ]

    fig = go.Figure(
        go.Bar(
            x=[d["adherence_pct"] for d in by_drug_sorted],
            y=[d["drug_name"] for d in by_drug_sorted],
            orientation="h",
            marker=dict(color=bar_colors),
            text=[f"{d['adherence_pct']:.0f}%" for d in by_drug_sorted],
            textposition="outside",
            hovertemplate="%{y}: %{x:.0f}%<extra></extra>",
        )
    )
    fig.update_layout(
        xaxis=dict(title="Adherence %", range=[0, 105]),
        yaxis=dict(title=""),
        height=max(250, 60 * len(by_drug_sorted)),
        margin=dict(l=120, r=50, t=30, b=30),
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption("📊 Worst-first: drugs needing attention at top")


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
    st.markdown(f"### Adherence — Last {_HEATMAP_WINDOW_DAYS} Days")

    pct_by_date = _aggregate_daily_adherence(daily_rows)
    if not pct_by_date:
        st.info("📅 No adherence data available yet for this period.")
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
            status_emoji = _get_adherence_color(pct)
            hover_text[weekday_idx][week_idx] = f"{current.strftime('%a, %b %d')}: {pct:.0f}% {status_emoji}"
        else:
            hover_text[weekday_idx][week_idx] = f"{current.strftime('%a, %b %d')}: No data"

    fig = go.Figure(
        go.Heatmap(
            z=z,
            y=_WEEKDAY_LABELS,
            x=[f"Wk {w + 1}" for w in range(num_weeks)],
            colorscale=_COLOR_SCALE,
            zmin=0,
            zmax=100,
            hoverongaps=False,
            text=hover_text,
            hoverinfo="text",
            colorbar=dict(title="Adherence %", ticksuffix="%"),
        )
    )
    fig.update_layout(height=320, margin=dict(l=60, r=60, t=30, b=30), showlegend=False)
    st.plotly_chart(fig, use_container_width=True)
    st.caption("🟢 Green: ≥90%  ·  🟡 Amber: 70–89%  ·  🔴 Red: <70%")


# ---------------------------------------------------------------------------
# Time-of-day pattern — grouped bars of missed doses by day part (Requirement 4)
# ---------------------------------------------------------------------------


def _render_time_of_day_pattern(daily_rows: list[dict]) -> None:
    st.markdown("### When Doses Get Missed")

    missed_by_part: dict[str, int] = defaultdict(int)
    skipped_by_part: dict[str, int] = defaultdict(int)
    for row in daily_rows:
        missed_by_part[row["day_part"]] += row["missed_doses"]
        skipped_by_part[row["day_part"]] += row["skipped_doses"]

    if not any(missed_by_part.values()) and not any(skipped_by_part.values()):
        st.info("🎉 No missed or skipped doses in this window!")
        return

    day_part_labels = {
        "morning": "🌅 Morning",
        "afternoon": "☀️ Afternoon",
        "evening": "🌆 Evening",
        "night": "🌙 Night",
    }

    fig = go.Figure(
        data=[
            go.Bar(
                name="Missed",
                x=[day_part_labels.get(p, p.title()) for p in _DAY_PART_ORDER],
                y=[missed_by_part.get(p, 0) for p in _DAY_PART_ORDER],
                marker_color="#d62728",
                hovertemplate="Missed: %{y}<extra></extra>",
            ),
            go.Bar(
                name="Skipped",
                x=[day_part_labels.get(p, p.title()) for p in _DAY_PART_ORDER],
                y=[skipped_by_part.get(p, 0) for p in _DAY_PART_ORDER],
                marker_color="#ff7f0e",
                hovertemplate="Skipped: %{y}<extra></extra>",
            ),
        ]
    )
    fig.update_layout(
        barmode="group",
        xaxis=dict(title=""),
        yaxis=dict(title="Dose Count"),
        height=320,
        margin=dict(l=50, r=30, t=30, b=50),
        legend=dict(orientation="h", yanchor="top", y=-0.15, xanchor="center", x=0.5),
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption("🔴 Missed = unactioned after planned time  ·  🟠 Skipped = intentionally not taken")


# ---------------------------------------------------------------------------
# Caregiver mode — Genie space panel (Requirement 5)
# ---------------------------------------------------------------------------


def _render_caregiver_panel() -> None:
    st.markdown("## 👨‍👩‍👧 Caregiver: Ask Genie")
    st.caption("Natural language questions about adherence trends (requires Genie Space setup)")

    genie_embed_url = os.getenv("GENIE_EMBED_URL")
    genie_space_url = os.getenv("GENIE_SPACE_URL")

    if genie_embed_url:
        st.markdown(
            f'<iframe src="{genie_embed_url}" width="100%" height="600" '
            f'allow="clipboard-write" style="border: 1px solid #ddd; border-radius: 8px;"></iframe>',
            unsafe_allow_html=True,
        )
    else:
        with st.container(border=True):
            st.warning(
                "**Genie embedding not yet configured**  \n"
                "No Genie Space has been created (see ARCHITECTURE.md's priorities). "
                "This feature will be available once a workspace admin sets it up."
            )
            if genie_space_url:
                st.link_button("📊 Open Genie Space ↗", genie_space_url, use_container_width=True)
            st.caption(
                "To enable: set `GENIE_EMBED_URL` (for iframe) or `GENIE_SPACE_URL` (for link) "
                "in your environment — no code change needed."
            )
