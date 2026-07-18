"""NeuroRx AI — Today view (Task 3.5).

The daily adherence surface: today's dose checklist grouped by day part,
a next-dose countdown, missed-dose handling, refill warnings, and an
in-app reminder banner. Persona: a 60-year-old patient managing several
prescriptions (`ARCHITECTURE.md` §1) — big font, high contrast, minimal,
one clear action per row. No business logic lives here (Requirement 6):
day-part classification, dose_text, and status all come pre-computed from
`app/db.py`'s `todays_doses()` (extended this task to add both, in SQL, not
in this view — see that function's own docstring); this file only groups,
formats, and wires buttons to `db.mark_dose()`.

## Two real gaps surfaced and handled, not silently papered over

1. **`db.refill_estimates()` cannot return a real "days remaining" number
   today** — `DATA_CONTRACTS.md` §6.2's `schedules` has no fill-quantity
   column at all (see that function's own docstring, Task 3.3). The
   Requirement 4 "<7 days" badge is wired to check a `days_remaining` field
   the data layer doesn't populate yet; this view renders the honest
   "refill tracking not available" state `refill_estimates()` actually
   returns, rather than inventing a number to make the badge fire.
2. **"The notifications table (Task 3.7)" doesn't exist yet** — Task 3.7
   (the scheduled Lakeflow Job that would populate it) hasn't been built,
   and no schema for it exists anywhere in `DATA_CONTRACTS.md`.
   `db.list_unacknowledged_reminders()` (added this task, flagged as
   provisional in its own docstring) degrades to an empty list rather than
   raising, so this view's reminder banner simply doesn't appear yet
   instead of crashing the whole Today tab over an undelivered dependency.

## "Missed" is a display-time judgment, not a stored status

Requirement 3 asks for past-time `planned` doses to render as "Missed" —
`dose_events.status` in Lakebase only ever holds `planned`/`taken`/
`skipped`/`missed` as an explicit write (`lakebase/schema.sql`'s
`dose_events_status_valid` CHECK); nothing here writes a `missed` status
just because a render happened to occur after the planned time. This view
computes `planned_ts < now()` at render time purely for **display**
(showing "Missed" instead of "Planned" in the checklist) — the underlying
row genuinely stays `status='planned'` in the database until an explicit
`mark_dose()` call (the "I took it late" button, or a future reminders job)
writes a real status. This is exactly what Requirement 3 itself asks for,
not an exception to "no new business logic in the view."
"""

from datetime import datetime, timezone

import streamlit as st

from app import db

# A few CSS bumps for a 60-year-old, polypharmacy persona (Requirement 6:
# "big-font, high-contrast, minimal") — not a full theme, just the handful
# of rules that matter for tap-target size and readability at a glance.
_TODAY_VIEW_CSS = """
<style>
div[data-testid="stButton"] button {
    font-size: 1.1rem;
    padding: 0.6rem 1.2rem;
    min-height: 2.75rem;
}
.neurorx-dose-row {
    font-size: 1.15rem;
    padding: 0.4rem 0;
}
.neurorx-dose-time {
    font-weight: 700;
}
</style>
"""

DAY_PART_ORDER = ["morning", "afternoon", "evening", "night"]
DAY_PART_LABELS = {
    "morning": "🌅 Morning",
    "afternoon": "☀️ Afternoon",
    "evening": "🌆 Evening",
    "night": "🌙 Night",
}


def render(patient_id: str) -> None:
    """Entry point called by app/app.py inside the Today tab."""
    st.markdown(_TODAY_VIEW_CSS, unsafe_allow_html=True)

    if not patient_id:
        st.info("📋 No patient selected. Choose a patient ID in the sidebar.")
        return

    now = datetime.now(timezone.utc)

    with st.spinner("💊 Loading today's schedule..."):
        doses = db.todays_doses(patient_id)

    _render_reminder_banner(patient_id)
    _render_next_dose_countdown(doses, now)
    _render_refill_warnings(patient_id)
    _render_checklist(patient_id, doses, now)


# ---------------------------------------------------------------------------
# Reminder banner (Requirement 5)
# ---------------------------------------------------------------------------


def _render_reminder_banner(patient_id: str) -> None:
    reminders = db.list_unacknowledged_reminders(patient_id)
    if not reminders:
        return  # also the normal case today — see module docstring's gap #2

    for reminder in reminders:
        col_message, col_dismiss = st.columns([5, 1])
        with col_message:
            st.info(f"🔔 {reminder['message']}")
        with col_dismiss:
            if st.button("Dismiss", key=f"ack_{reminder['notification_id']}"):
                db.acknowledge_reminder(reminder["notification_id"])
                st.rerun()


# ---------------------------------------------------------------------------
# Next-dose countdown (Requirement 2)
# ---------------------------------------------------------------------------


def _render_next_dose_countdown(doses: list[dict], now: datetime) -> None:
    upcoming = sorted(
        (d for d in doses if d["status"] == "planned" and d["planned_ts"] > now),
        key=lambda d: d["planned_ts"],
    )
    with st.container(border=True):
        if not upcoming:
            st.markdown("### ✅ All Done for Today")
            st.caption("No more scheduled doses. Great job staying on top of your medications!")
            return

        next_dose = upcoming[0]
        remaining = next_dose["planned_ts"] - now
        hours, remainder = divmod(int(remaining.total_seconds()), 3600)
        minutes = remainder // 60

        time_text = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
        st.markdown(
            f"### ⏰ Next Dose: **{next_dose['drug_name']}** ({next_dose['dose_text']})"
        )
        st.markdown(f"**{time_text}** · Scheduled for {next_dose['planned_ts'].strftime('%I:%M %p')}")


# ---------------------------------------------------------------------------
# Refill warnings (Requirement 4)
# ---------------------------------------------------------------------------


def _render_refill_warnings(patient_id: str) -> None:
    estimates = db.refill_estimates(patient_id)
    if not estimates:
        return

    with st.expander("💊 Refill Status", expanded=False):
        for est in estimates:
            days_remaining = est.get("days_remaining")
            drug_name = est["drug_name"]
            if days_remaining is not None and days_remaining < 7:
                st.warning(f"⚠️ **{drug_name}**: Refill soon — {days_remaining}-day supply remaining")
            elif days_remaining is not None:
                st.markdown(f"✅ **{drug_name}**: {days_remaining}-day supply remaining")
            else:
                st.caption(f"📊 **{drug_name}**: Refill tracking unavailable ({est.get('unavailable_reason', 'no data')})")


# ---------------------------------------------------------------------------
# Dose checklist, grouped by day part (Requirement 1) + missed handling (Requirement 3)
# ---------------------------------------------------------------------------


def _render_checklist(patient_id: str, doses: list[dict], now: datetime) -> None:
    st.markdown("## Today's Doses")

    grouped: dict[str, list[dict]] = {part: [] for part in DAY_PART_ORDER}
    for dose in doses:
        grouped.setdefault(dose["day_part"], []).append(dose)

    if not any(grouped.values()):
        st.info("📋 No active prescriptions scheduled for today.")
        return

    total_doses = sum(len(doses) for doses in grouped.values())
    st.caption(f"📊 {total_doses} dose{'s' if total_doses != 1 else ''} scheduled today")

    for part in DAY_PART_ORDER:
        part_doses = grouped.get(part, [])
        if not part_doses:
            continue
        st.markdown(f"### {DAY_PART_LABELS[part]}")
        for dose in part_doses:
            _render_dose_row(patient_id, dose, now)


def _render_dose_row(patient_id: str, dose: dict, now: datetime) -> None:
    is_overdue_unactioned = dose["status"] == "planned" and dose["planned_ts"] < now
    display_status = "missed" if is_overdue_unactioned else dose["status"]

    with st.container(border=True):
        col_info, col_taken, col_skip = st.columns([3, 1, 1])

        with col_info:
            time_str = dose["planned_ts"].strftime("%I:%M %p")
            st.markdown(
                f'<div class="neurorx-dose-row">'
                f'<span class="neurorx-dose-time">{time_str}</span> — '
                f"{dose['drug_name']} ({dose['dose_text']})"
                f"</div>",
                unsafe_allow_html=True,
            )
            st.caption(_STATUS_CAPTIONS.get(display_status, display_status.title()))

        if display_status == "missed":
            # Requirement 3: one-tap "I took it late" — records taken with
            # the actual (now) timestamp, not the originally planned one,
            # since the dose really was taken late, not on schedule.
            with col_taken:
                if st.button("I took it late", key=f"late_{dose['schedule_id']}_{dose['planned_ts']}"):
                    db.mark_dose(
                        schedule_id=dose["schedule_id"],
                        planned_ts=dose["planned_ts"],
                        status="taken",
                        ts=now,
                    )
                    st.rerun()
        elif display_status == "planned":
            with col_taken:
                if st.button("Taken ✓", key=f"taken_{dose['schedule_id']}_{dose['planned_ts']}"):
                    db.mark_dose(
                        schedule_id=dose["schedule_id"],
                        planned_ts=dose["planned_ts"],
                        status="taken",
                        ts=now,
                    )
                    st.rerun()  # optimistic UI: write first, then rerun to reflect it
            with col_skip:
                if st.button("Skip", key=f"skip_{dose['schedule_id']}_{dose['planned_ts']}"):
                    db.mark_dose(
                        schedule_id=dose["schedule_id"],
                        planned_ts=dose["planned_ts"],
                        status="skipped",
                        ts=now,
                    )
                    st.rerun()
        # taken/skipped rows show no buttons — already actioned.


_STATUS_CAPTIONS = {
    "planned": "⏳ Not yet taken",
    "taken": "✅ Taken",
    "skipped": "⏭️ Skipped",
    "missed": "❌ Missed (overdue)",
}
