import pytest

from app import auth, db


@pytest.fixture(autouse=True)
def _route_db_through_the_test_connection(pg_conn, monkeypatch):
    """auth calls db.* with no conn; bind them to the rolled-back test
    connection so auth's own code path is exercised unchanged."""
    for name in (
        "create_account_with_patient",
        "find_account_by_email",
        "touch_last_login",
    ):
        original = getattr(db, name)
        monkeypatch.setattr(
            db, name, lambda *a, _o=original, **k: _o(*a, **{**k, "conn": pg_conn})
        )


def test_create_account_returns_an_account(pg_conn):
    acct = auth.create_account("ada@example.com", "Ada Lovelace", "hunter2hunter2")
    assert isinstance(acct, auth.Account)
    assert acct.display_name == "Ada Lovelace"
    assert acct.patient_id


def test_account_never_exposes_the_hash():
    """The dataclass must not carry password_hash — it reaches session_state."""
    assert "password_hash" not in auth.Account.__dataclass_fields__


def test_create_account_rejects_a_short_password(pg_conn):
    with pytest.raises(auth.WeakPassword):
        auth.create_account("short@example.com", "Short", "abc")


def test_create_account_rejects_a_duplicate_email(pg_conn):
    auth.create_account("dup@example.com", "First", "hunter2hunter2")
    with pytest.raises(auth.EmailTaken):
        auth.create_account("dup@example.com", "Second", "hunter2hunter2")


def test_authenticate_accepts_correct_credentials(pg_conn):
    auth.create_account("ok@example.com", "OK", "hunter2hunter2")
    assert auth.authenticate("ok@example.com", "hunter2hunter2") is not None


def test_authenticate_rejects_a_wrong_password(pg_conn):
    auth.create_account("wp@example.com", "WP", "hunter2hunter2")
    assert auth.authenticate("wp@example.com", "wrong-password") is None


def test_authenticate_returns_none_for_an_unknown_email(pg_conn):
    assert auth.authenticate("ghost@example.com", "hunter2hunter2") is None


def test_unknown_email_and_wrong_password_are_indistinguishable(pg_conn):
    """Both must be None, so the login form cannot enumerate which emails
    have accounts."""
    auth.create_account("enum@example.com", "Enum", "hunter2hunter2")
    wrong_password = auth.authenticate("enum@example.com", "wrong")
    unknown_email = auth.authenticate("nosuch@example.com", "wrong")
    assert wrong_password is None
    assert unknown_email is None


def test_authenticate_is_case_insensitive_on_email(pg_conn):
    auth.create_account("ci@example.com", "CI", "hunter2hunter2")
    assert auth.authenticate("  CI@Example.COM ", "hunter2hunter2") is not None
