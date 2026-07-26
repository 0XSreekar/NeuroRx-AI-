"""NeuroRx AI — create-account screen.

Collects a name (displayed in the app), an email (the login identifier), and a
password. On success the new account is signed in immediately and on_success()
navigates onward.

The "do not reuse a real password" warning is deliberate and load-bearing: this
is a hackathon demo database, and people reuse passwords. It is a real
mitigation, not a disclaimer.
"""

from typing import Callable

import streamlit as st

from app import auth, theme


def render(on_success: Callable[[], None], on_login: Callable[[], None]) -> None:
    st.markdown(theme.brand(), unsafe_allow_html=True)
    st.markdown(
        f'<div class="nrx-auth">{theme.eyebrow("GET STARTED")}'
        "<h2>Create your account</h2></div>",
        unsafe_allow_html=True,
    )

    name = st.text_input("Name", key="signup_name", placeholder="Ada Lovelace")
    email = st.text_input("Email", key="signup_email", placeholder="you@example.com")
    password = st.text_input(
        "Password",
        key="signup_password",
        type="password",
        help=f"At least {auth.MIN_PASSWORD_LENGTH} characters.",
    )
    confirm = st.text_input("Confirm password", key="signup_confirm", type="password")

    st.markdown(
        '<div class="nrx-auth-note">This is a demo application storing synthetic '
        "data. <strong>Do not reuse a password</strong> from any real account.</div>",
        unsafe_allow_html=True,
    )

    if st.button(
        "Create account", key="signup_submit", type="primary", use_container_width=True
    ):
        _submit(name, email, password, confirm, on_success)

    if st.button(
        "I already have an account", key="signup_to_login", use_container_width=True
    ):
        on_login()


def _submit(
    name: str, email: str, password: str, confirm: str, on_success: Callable[[], None]
) -> None:
    """Validate, create, sign in.

    Checks run cheapest-first so a mismatched confirmation never reaches the
    ~33 ms argon2 hash or the database.
    """
    if not name.strip():
        st.error("Please enter your name.")
        return
    if not email.strip():
        st.error("Please enter your email.")
        return
    if password != confirm:
        st.error("Those passwords do not match.")
        return

    try:
        account = auth.create_account(email, name.strip(), password)
    except auth.WeakPassword as exc:
        st.error(str(exc))
        return
    except auth.EmailTaken as exc:
        st.error(str(exc))
        return

    auth.sign_in(account)
    on_success()
