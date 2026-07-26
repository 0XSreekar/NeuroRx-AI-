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

import sys
from pathlib import Path

# Put the repo root on sys.path BEFORE any `app.*` import.
#
# Every module in this project imports itself as `app.config` / `app.db` /
# `app.views.*` — i.e. it assumes `app` is an importable package rooted at the
# repo root. Nothing was actually arranging for that to be true:
#
#   - `streamlit run app/app.py` (from the repo root) puts the *script's own*
#     directory — `app/` — on sys.path, not the repo root. So `app` is not
#     importable and this file died with "No module named 'app.views'".
#   - `app.yaml`'s `command: ['streamlit', 'run', 'app.py']` runs from *inside*
#     `app/`, which has the same problem for the deployed app.
#
# Deriving the root from `__file__` rather than cwd makes both launch paths work
# identically, which matters because the deployed app and local dev must not
# diverge here. Found by actually running the app, not by reading it.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
# Force the repo root to the FRONT of sys.path, ahead of Streamlit's script
# directory (`app/`). A plain `if _REPO_ROOT not in sys.path` guard is not
# enough: when the repo root is already present but at a *later* position than
# `app/` (e.g. PYTHONPATH includes it, or the deployed App host adds it), the
# guard skips and `app/app.py` shadows the `app` package —
# "No module named 'app.views'; 'app' is not a package". Remove any existing
# occurrence, then insert at position 0 so the package always wins.
while _REPO_ROOT in sys.path:
    sys.path.remove(_REPO_ROOT)
sys.path.insert(0, _REPO_ROOT)

import streamlit as st

from app import auth, theme
from app.views import chat as chat_view
from app.views import dashboard as dashboard_view
from app.views import home as home_view
from app.views import login as login_view
from app.views import signup as signup_view
from app.views import today as today_view

st.set_page_config(
    page_title="NeuroRx AI",
    page_icon="💊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# The design layer (`design/mockup.html`). Must run before any view renders, so
# nothing paints unstyled on first frame.
theme.inject()

MARGARET_DEMO_PATIENT_ID = "12345678-1234-1234-1234-123456789012"


# Page objects, populated below before st.navigation(...).run(). Callbacks fire
# during run(), by which point this is filled in.
_PAGES: dict[str, "st.navigation"] = {}


def _go(name: str):
    """Move between pages within the CURRENT page set.

    st.navigation routes by URL, so switching pages needs st.switch_page with
    the actual page object — an earlier version set a session_state key that
    nothing read, which silently did nothing but rerun the same page. Found by
    clicking the button, not by reading the code.
    """
    st.switch_page(_PAGES[name])


def _auth_state_changed():
    """Rerun after signing in or out, so the page SET is rebuilt.

    Deliberately not st.switch_page: the destination belongs to the other page
    set and does not exist yet at this point. Re-running the script re-evaluates
    which pages are registered and lands on that set's default.
    """
    st.rerun()


def _render_home() -> None:
    home_view.render(
        on_signup=lambda: _go("signup"),
        on_login=lambda: _go("login"),
    )


def _render_signup() -> None:
    signup_view.render(
        on_success=_auth_state_changed,
        on_login=lambda: _go("login"),
    )


def _render_login() -> None:
    login_view.render(
        on_success=_auth_state_changed,
        on_signup=lambda: _go("signup"),
    )


def _render_app_page() -> None:
    """The signed-in shell: header, safety ticker, three tabs.

    The patient selector is a DEMO SWITCHER, not scoping. It defaults to the
    signed-in account's own patient, but any patient_id typed here is honoured
    — auth is deliberately not an access boundary (see app/auth.py).
    """
    account = auth.current_account()

    if "patient_id" not in st.session_state:
        st.session_state.patient_id = account.patient_id

    col_brand, col_patient, col_out = st.columns([4, 1, 1], vertical_alignment="center")

    with col_brand:
        st.markdown(theme.brand(), unsafe_allow_html=True)

    with col_patient:
        with st.popover(
            f"PATIENT  {st.session_state.patient_id[:8]}", use_container_width=True
        ):
            st.markdown(
                theme.eyebrow(f"SIGNED IN AS {account.display_name.upper()}"),
                unsafe_allow_html=True,
            )
            entered = st.text_input(
                "Patient ID",
                value=st.session_state.patient_id,
                help=(
                    "Demo switcher — not access control. Margaret Demo is "
                    f"{MARGARET_DEMO_PATIENT_ID[:8]}..."
                ),
            )
            st.caption("💊 All data is synthetic and for demo only.")

    # Rerun on a change so the popover's own label (rendered above, from the
    # pre-edit value) cannot disagree with the patient the tabs are showing.
    # Without this the header reads one patient while the body shows another
    # until some unrelated interaction happens to rerun the script.
    if entered != st.session_state.patient_id:
        st.session_state.patient_id = entered
        st.rerun()

    with col_out:
        if st.button("Sign out", use_container_width=True):
            auth.sign_out()
            # Drop the switcher's patient so the next account does not inherit
            # whichever patient this session was last looking at.
            st.session_state.pop("patient_id", None)
            _auth_state_changed()

    # Persistent safety notice — every tab, no dismiss control (Task 3.4
    # Requirement 1). Rendered before the tab widget, so it sits above all
    # three tabs rather than inside any one of them.
    theme.safety_ticker()

    tab_chat, tab_today, tab_dashboard = st.tabs(["Chat", "Today", "Dashboard"])
    with tab_chat:
        chat_view.render(patient_id=st.session_state.patient_id)
    with tab_today:
        today_view.render(patient_id=st.session_state.patient_id)
    with tab_dashboard:
        dashboard_view.render(patient_id=st.session_state.patient_id)


# ---------------------------------------------------------------------------
# Routing
#
# The signed-out page set does NOT contain the app page, so while signed out
# there is nothing for that URL to route to. The gate is structural rather
# than a render-and-return-early check that a future edit could skip.
#
# position="hidden" suppresses Streamlit's own navigation widget, leaving the
# restyled pill tabs as the only navigation.
# ---------------------------------------------------------------------------
if auth.current_account() is None:
    _PAGES["home"] = st.Page(_render_home, title="Home", url_path="home", default=True)
    _PAGES["login"] = st.Page(_render_login, title="Sign in", url_path="login")
    _PAGES["signup"] = st.Page(
        _render_signup, title="Create account", url_path="signup"
    )
    pages = [_PAGES["home"], _PAGES["login"], _PAGES["signup"]]
else:
    _PAGES["app"] = st.Page(
        _render_app_page, title="NeuroRx AI", url_path="app", default=True
    )
    pages = [_PAGES["app"]]

st.navigation(pages, position="hidden").run()
