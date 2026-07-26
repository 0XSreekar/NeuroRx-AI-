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

import html
from datetime import datetime, timezone

import streamlit as st

from app import db, theme

DAY_PART_ORDER = ["morning", "afternoon", "evening", "night"]
DAY_PART_LABELS = {
    "morning": "MORNING",
    "afternoon": "AFTERNOON",
    "evening": "EVENING",
    "night": "NIGHT",
}

# Display status -> (pill label, pill tone). Tones are the design layer's three:
# accent (cyan, the good/live state), warn (amber, needs attention), muted.
_STATUS_PILLS = {
    "planned": ("Upcoming", "muted"),
    "taken": ("Taken", "accent"),
    "skipped": ("Skipped", "muted"),
    "missed": ("Missed", "warn"),
    "due": ("Due now", "accent"),
}


def render(patient_id: str) -> None:
    """Entry point called by app/app.py inside the Today tab."""
    if not patient_id:
        st.info("📋 No patient selected. Choose a patient ID in the sidebar.")
        return

    now = datetime.now(timezone.utc)

    with st.spinner("Loading today's schedule..."):
        doses = db.todays_doses(patient_id)

    _render_reminder_banner(patient_id)

    # The mockup pairs the countdown and refill status as two equal-height cards
    # on one row. Rendered as a single flex row (theme.card_row) rather than
    # st.columns — see that helper's docstring for why.
    st.markdown(
        theme.card_row(
            _next_dose_card(doses, now),
            _refill_card(patient_id),
        ),
        unsafe_allow_html=True,
    )

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


def _next_dose_card(doses: list[dict], now: datetime) -> str:
    upcoming = sorted(
        (d for d in doses if d["status"] == "planned" and d["planned_ts"] > now),
        key=lambda d: d["planned_ts"],
    )
    if not upcoming:
        return theme.stat_card(
            "NEXT DOSE IN",
            "—",
            delta="All done for today. No more scheduled doses.",
        )

    next_dose = upcoming[0]
    remaining = next_dose["planned_ts"] - now
    hours, remainder = divmod(int(remaining.total_seconds()), 3600)
    minutes = remainder // 60
    time_text = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"

    return theme.stat_card(
        "NEXT DOSE IN",
        time_text,
        delta=(
            f"{next_dose['drug_name']} {next_dose['dose_text']} · "
            f"{next_dose['planned_ts'].strftime('%H:%M')}  ·  "
            f"{len(upcoming)} of {len(doses)} doses left today"
        ),
    )


# ---------------------------------------------------------------------------
# Refill warnings (Requirement 4)
# ---------------------------------------------------------------------------


def _refill_card(patient_id: str) -> str:
    """Refill card.

    The mockup draws a filled progress bar per drug. `refill_estimates()`
    cannot supply a real figure — `DATA_CONTRACTS.md` §6.2's `schedules` has no
    fill-quantity column at all (see that function's docstring) — so the bar is
    rendered only when `days_remaining` is genuinely populated. Where it is not,
    the row says so in words. A bar drawn at an invented width would be the one
    element on this screen that looks like data and isn't.
    """
    estimates = db.refill_estimates(patient_id)
    if not estimates:
        return theme.stat_card("REFILL STATUS", "—", delta="No active prescriptions")

    rows = []
    reasons: list[str] = []
    for est in estimates:
        days = est.get("days_remaining")
        # Escaped for the same reason theme.py escapes everything it renders:
        # drug_name reaches this row from the DB, and the photo-OCR extraction
        # path can put arbitrary text in it. Unescaped, a stray '<' silently
        # breaks the row's markup. dashboard.py and chat.py already escape it;
        # this card was the one surface that did not.
        drug = html.escape(str(est["drug_name"]), quote=True)
        if days is None:
            reason = est.get("unavailable_reason")
            if reason and reason not in reasons:
                reasons.append(str(reason))
            rows.append(
                f'<div class="nrx-refill-row"><span class="d">{drug}</span>'
                f'<span class="u">not tracked</span></div>'
            )
            continue
        pct = max(0, min(100, round(days / 90 * 100)))
        tone = "var(--nrx-amber)" if days < 7 else "var(--nrx-accent)"
        rows.append(
            f'<div class="nrx-refill-row"><span class="d">{drug}</span>'
            f'<span class="bar"><span style="width:{pct}%;background:{tone}"></span></span>'
            f'<span class="n">{days} days</span></div>'
        )

    # `refill_estimates()` returns days_remaining=None for every drug today and
    # says why in `unavailable_reason` (db.py §819: the schedules contract has no
    # fill-quantity column). The expander this card replaced surfaced that reason;
    # a bare "not tracked" would drop it and leave the gap looking like a bug
    # rather than the flagged schema gap it is. Reasons are deduped because the
    # same one currently applies to every drug.
    note = (
        f'<div class="nrx-refill-note">{html.escape(" · ".join(reasons))}</div>'
        if reasons
        else ""
    )

    return (
        f'<div class="nrx-card">{theme.eyebrow("REFILL STATUS")}'
        f'<div class="nrx-refill">{"".join(rows)}</div>{note}</div>'
    )


# ---------------------------------------------------------------------------
# Dose checklist, grouped by day part (Requirement 1) + missed handling (Requirement 3)
# ---------------------------------------------------------------------------


def _render_checklist(patient_id: str, doses: list[dict], now: datetime) -> None:
    grouped: dict[str, list[dict]] = {part: [] for part in DAY_PART_ORDER}
    for dose in doses:
        grouped.setdefault(dose["day_part"], []).append(dose)

    if not any(grouped.values()):
        st.markdown(theme.section_heading("TODAY", "Today's doses"), unsafe_allow_html=True)
        st.info("No active prescriptions scheduled for today.")
        return

    total_doses = sum(len(part) for part in grouped.values())
    actioned = sum(1 for d in doses if d["status"] in {"taken", "skipped"})
    st.markdown(
        theme.section_heading(
            f"{actioned} OF {total_doses} ACTIONED", "Today's doses"
        ),
        unsafe_allow_html=True,
    )

    for part in DAY_PART_ORDER:
        part_doses = grouped.get(part, [])
        if not part_doses:
            continue
        taken_here = sum(1 for d in part_doses if d["status"] in {"taken", "skipped"})
        st.markdown(
            theme.group_header(DAY_PART_LABELS[part], f"{taken_here} / {len(part_doses)}"),
            unsafe_allow_html=True,
        )
        for dose in part_doses:
            _render_dose_row(patient_id, dose, now)


def _render_dose_row(patient_id: str, dose: dict, now: datetime) -> None:
    is_overdue_unactioned = dose["status"] == "planned" and dose["planned_ts"] < now
    display_status = "missed" if is_overdue_unactioned else dose["status"]

    label, tone = _STATUS_PILLS.get(display_status, (display_status.title(), "muted"))

    with st.container(border=True):
        col_info, col_pill, col_actions = st.columns(
            [5, 2, 3], vertical_alignment="center"
        )

        with col_info:
            st.markdown(
                theme.dose_row(
                    dose["planned_ts"].strftime("%H:%M"),
                    dose["drug_name"],
                    dose["dose_text"] or "",
                ),
                unsafe_allow_html=True,
            )

        with col_pill:
            st.markdown(theme.status_pill(label, tone), unsafe_allow_html=True)

        with col_actions:
            _render_dose_actions(dose, display_status, now)


def _render_dose_actions(dose: dict, display_status: str, now: datetime) -> None:
    """Action buttons for a dose row.

    These stay real `st.button` widgets — only those can trigger the
    `db.mark_dose()` write — and pick up their pill styling from `app/theme.py`.
    Rows already actioned (taken/skipped) render no buttons at all.
    """
    key_base = f"{dose['schedule_id']}_{dose['planned_ts']}"

    if display_status == "missed":
        # Requirement 3: one-tap "I took it late" — records taken with the
        # actual (now) timestamp, not the originally planned one, since the
        # dose really was taken late, not on schedule.
        if st.button("I took it late", key=f"late_{key_base}", type="primary"):
            db.mark_dose(
                schedule_id=dose["schedule_id"],
                planned_ts=dose["planned_ts"],
                status="taken",
                ts=now,
            )
            st.rerun()
        return

    if display_status == "planned":
        col_take, col_skip = st.columns(2)
        with col_take:
            if st.button("Mark taken", key=f"taken_{key_base}", type="primary"):
                db.mark_dose(
                    schedule_id=dose["schedule_id"],
                    planned_ts=dose["planned_ts"],
                    status="taken",
                    ts=now,
                )
                st.rerun()  # optimistic UI: write first, then rerun to reflect it
        with col_skip:
            if st.button("Skip", key=f"skip_{key_base}"):
                db.mark_dose(
                    schedule_id=dose["schedule_id"],
                    planned_ts=dose["planned_ts"],
                    status="skipped",
                    ts=now,
                )
                st.rerun()
