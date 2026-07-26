"""NeuroRx AI — sign-in screen.

One generic failure message for every rejection. auth.authenticate() already
returns the same None for an unknown email and a wrong password; this screen
must not undo that by phrasing them differently, or the form becomes a way to
discover which emails have accounts.
"""

from typing import Callable

import streamlit as st

from app import auth, theme

_GENERIC_FAILURE = "That email or password is incorrect."


def render(on_success: Callable[[], None], on_signup: Callable[[], None]) -> None:
    st.markdown(theme.brand(), unsafe_allow_html=True)
    st.markdown(
        f'<div class="nrx-auth">{theme.eyebrow("WELCOME BACK")}'
        "<h2>Sign in</h2></div>",
        unsafe_allow_html=True,
    )

    email = st.text_input("Email", key="login_email", placeholder="you@example.com")
    password = st.text_input("Password", key="login_password", type="password")

    if st.button(
        "Sign in", key="login_submit", type="primary", use_container_width=True
    ):
        _submit(email, password, on_success)

    if st.button(
        "Create an account", key="login_to_signup", use_container_width=True
    ):
        on_signup()


def _submit(email: str, password: str, on_success: Callable[[], None]) -> None:
    if not email.strip() or not password:
        st.error(_GENERIC_FAILURE)
        return

    account = auth.authenticate(email, password)
    if account is None:
        st.error(_GENERIC_FAILURE)
        return

    auth.sign_in(account)
    on_success()
