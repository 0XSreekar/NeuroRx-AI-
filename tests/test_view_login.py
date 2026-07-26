import pytest
from streamlit.testing.v1 import AppTest

from app import auth, db


@pytest.fixture(autouse=True)
def _route_db_through_the_test_connection(pg_conn, monkeypatch):
    for name in (
        "create_account_with_patient",
        "find_account_by_email",
        "touch_last_login",
    ):
        original = getattr(db, name)
        monkeypatch.setattr(
            db, name, lambda *a, _o=original, **k: _o(*a, **{**k, "conn": pg_conn})
        )


def _script():
    from app.views import login

    login.render(on_success=lambda: None, on_signup=lambda: None)


def _attempt(at, email, password):
    at.text_input(key="login_email").set_value(email)
    at.text_input(key="login_password").set_value(password)
    return at.button(key="login_submit").click().run()


def test_form_shows_email_and_password():
    at = AppTest.from_function(_script).run()
    assert not at.exception
    assert {"Email", "Password"} <= {t.label for t in at.text_input}


def test_correct_credentials_sign_the_user_in(pg_conn):
    auth.create_account("ok@example.com", "OK Person", "hunter2hunter2")
    at = _attempt(
        AppTest.from_function(_script).run(), "ok@example.com", "hunter2hunter2"
    )
    assert not at.error
    assert at.session_state[auth.SESSION_KEY].display_name == "OK Person"


def test_wrong_password_is_rejected(pg_conn):
    auth.create_account("wp@example.com", "WP", "hunter2hunter2")
    at = _attempt(
        AppTest.from_function(_script).run(), "wp@example.com", "nope-nope-nope"
    )
    assert at.error
    assert auth.SESSION_KEY not in at.session_state


def test_unknown_email_gives_the_same_message_as_a_wrong_password(pg_conn):
    """The screen must not reveal which emails have accounts."""
    auth.create_account("known@example.com", "Known", "hunter2hunter2")
    wrong_pw = _attempt(
        AppTest.from_function(_script).run(), "known@example.com", "nope-nope-nope"
    )
    unknown = _attempt(
        AppTest.from_function(_script).run(), "ghost@example.com", "nope-nope-nope"
    )
    assert [e.value for e in wrong_pw.error] == [e.value for e in unknown.error]


def test_empty_email_is_rejected_before_hashing():
    at = _attempt(AppTest.from_function(_script).run(), "", "hunter2hunter2")
    assert at.error
