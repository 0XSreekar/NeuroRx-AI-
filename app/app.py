"""NeuroRx AI — Databricks App shell (Task 3.4; Today, Dashboard wired in
Tasks 3.5/3.6).

Three tabs, one app: Chat, Today, Dashboard (`ARCHITECTURE.md` §2's "App
views (three views, one app)"). This file owns only the shell — the
persistent safety banner every tab shows, patient-selector state shared
across tabs, and tab routing. All view-specific logic lives in
`app/views/*.py`; this file contains none of its own.

Built starting from Databricks' current chat-app template structure
(`databricks/app-templates/e2e-chatbot-app`, fetched live this session —
see `app/views/chat.py`'s own docstring for exactly what was reused from it
and why), adapted from a single-purpose chat app into one tab of a
three-tab app.

All three tabs are now implemented: **Chat** (Task 3.4), **Today**
(Task 3.5), **Dashboard** (Task 3.6).
"""

import streamlit as st

from app.views import chat as chat_view
from app.views import dashboard as dashboard_view
from app.views import today as today_view

st.set_page_config(page_title="NeuroRx AI", page_icon="💊", layout="wide")

# ---------------------------------------------------------------------------
# Persistent safety banner — every tab, non-dismissable (Task 3.4 Requirement 1)
#
# Rendered before the tab widget itself, so it is the first thing on every
# tab and cannot be scrolled past or closed — there is deliberately no
# close/dismiss control anywhere in this block. This is UI-level
# reinforcement of `agent/prompts/system_prompt.md`'s own Identity section
# ("You are an organizational assistant... not a medical professional") —
# belt-and-suspenders with the prompt, not a substitute for it.
# ---------------------------------------------------------------------------
st.warning(
    "⚠️ **NeuroRx AI is an organizational assistant, not medical advice.**  \n"
    "For medical questions, contact your pharmacist or doctor. **Emergencies: call 911.**",
)

# ---------------------------------------------------------------------------
# Patient selector — shared session state across all three tabs
#
# Defaults to Margaret Demo (CLAUDE.md's non-negotiables: patient_id
# 12345678-1234-1234-1234-123456789012), the one patient every synthetic
# fixture in this project (Task 1.4's cohort, Task 2.9's smoke tests) is
# built around — a new session should land on a tab that already has real
# data to show, not an empty state.
# ---------------------------------------------------------------------------
MARGARET_DEMO_PATIENT_ID = "12345678-1234-1234-1234-123456789012"

if "patient_id" not in st.session_state:
    st.session_state.patient_id = MARGARET_DEMO_PATIENT_ID

with st.sidebar:
    st.markdown("### 👤 Patient")
    st.session_state.patient_id = st.text_input(
        "Patient ID",
        value=st.session_state.patient_id,
        help=f"Defaults to Margaret Demo ({MARGARET_DEMO_PATIENT_ID[:8]}...), the project's canonical demo patient.",
    )
    st.caption("💊 All data is synthetic and for demo only.")

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_chat, tab_today, tab_dashboard = st.tabs(["💬 Chat", "✅ Today", "📊 Dashboard"])

with tab_chat:
    chat_view.render(patient_id=st.session_state.patient_id)

with tab_today:
    today_view.render(patient_id=st.session_state.patient_id)

with tab_dashboard:
    dashboard_view.render(patient_id=st.session_state.patient_id)
