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
    from app.views import signup

    signup.render(on_success=lambda: None, on_login=lambda: None)


def _fill(at, name, email, password, confirm):
    at.text_input(key="signup_name").set_value(name)
    at.text_input(key="signup_email").set_value(email)
    at.text_input(key="signup_password").set_value(password)
    at.text_input(key="signup_confirm").set_value(confirm)
    return at.button(key="signup_submit").click().run()


def test_form_shows_name_email_and_password():
    at = AppTest.from_function(_script).run()
    assert not at.exception
    labels = {t.label for t in at.text_input}
    assert {"Name", "Email", "Password", "Confirm password"} <= labels


def test_warns_against_reusing_a_real_password():
    at = AppTest.from_function(_script).run()
    body = " ".join(m.value for m in at.markdown).lower()
    assert "do not reuse" in body


def test_mismatched_passwords_are_rejected():
    at = _fill(
        AppTest.from_function(_script).run(),
        "Ada",
        "mismatch@example.com",
        "hunter2hunter2",
        "different-entirely",
    )
    assert any("do not match" in e.value.lower() for e in at.error)


def test_short_password_is_rejected_with_the_minimum_stated():
    at = _fill(
        AppTest.from_function(_script).run(),
        "Ada",
        "short@example.com",
        "abc",
        "abc",
    )
    assert any(str(auth.MIN_PASSWORD_LENGTH) in e.value for e in at.error)


def test_blank_name_is_rejected():
    at = _fill(
        AppTest.from_function(_script).run(),
        "   ",
        "noname@example.com",
        "hunter2hunter2",
        "hunter2hunter2",
    )
    assert any("name" in e.value.lower() for e in at.error)


def test_duplicate_email_shows_a_clear_message(pg_conn):
    auth.create_account("taken@example.com", "First", "hunter2hunter2")
    at = _fill(
        AppTest.from_function(_script).run(),
        "Second",
        "taken@example.com",
        "hunter2hunter2",
        "hunter2hunter2",
    )
    assert any("already exists" in e.value.lower() for e in at.error)


def test_successful_signup_signs_the_user_in(pg_conn):
    at = _fill(
        AppTest.from_function(_script).run(),
        "Grace Hopper",
        "grace@example.com",
        "hunter2hunter2",
        "hunter2hunter2",
    )
    assert not at.error
    assert at.session_state[auth.SESSION_KEY].display_name == "Grace Hopper"
