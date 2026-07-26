"""NeuroRx AI — public home page.

The landing surface, visible signed-out. Renders no patient data of any kind:
it is reachable without authenticating, so nothing here may touch app/db.py.

The design language comes from design/mockup.html via app/theme.py. The
mockup's animated starfield is not ported — it needs a canvas render loop,
which inside Streamlit would require a sandboxed component iframe and could
not sit behind page content. theme.py approximates it with CSS gradients.
"""

from typing import Callable

import streamlit as st

from app import theme


def render(on_signup: Callable[[], None], on_login: Callable[[], None]) -> None:
    """Render the landing page.

    Navigation is injected as callbacks rather than imported, so this view has
    no dependency on the router and stays trivially testable.
    """
    st.markdown(theme.brand(), unsafe_allow_html=True)

    st.markdown(
        '<div class="nrx-hero">'
        f'{theme.eyebrow("MEDICATION SCHEDULES, ORGANIZED")}'
        "<h1>Every answer traced back to<br>the <em>label it came from</em>.</h1>"
        "<p>NeuroRx AI turns a prescription into a schedule you can actually keep — "
        "dose reminders, interaction checks, and adherence you can see. Clinical "
        "facts come from deterministic lookups over FDA labels, each one cited. "
        "This is an organizational assistant, not medical advice.</p>"
        "</div>",
        unsafe_allow_html=True,
    )

    col_signup, col_login, _ = st.columns([1, 1, 3])
    with col_signup:
        if st.button("Create account", type="primary", use_container_width=True):
            on_signup()
    with col_login:
        if st.button("Sign in", use_container_width=True):
            on_login()

    cards = [
        (
            "CITED BY CONSTRUCTION",
            "Grounded answers",
            "Every clinical statement carries an FDA label citation you can expand and read.",
        ),
        (
            "CHECKED BEFORE SAVING",
            "Interaction checks",
            "Adding a drug runs a deterministic interaction check first. You confirm changes, not the model.",
        ),
        (
            "SYNTHETIC ONLY",
            "No real patient data",
            "Every record in this demo is generated. No PHI is stored, ever.",
        ),
    ]
    st.markdown(
        '<div class="nrx-trust">'
        + "".join(
            f'<div class="nrx-card">{theme.eyebrow(eyebrow)}'
            f'<div class="t">{title}</div><div class="b">{body}</div></div>'
            for eyebrow, title, body in cards
        )
        + "</div>",
        unsafe_allow_html=True,
    )
