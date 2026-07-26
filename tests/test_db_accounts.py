"""Account persistence, against real Postgres.

These call db functions with an explicit connection so they run inside the
rolled-back transaction from conftest. db's own pool is @st.cache_resource-
decorated and would open a SECOND connection that could not see uncommitted
rows — which is why every function under test takes an optional conn.
"""

import psycopg
import pytest

from app import db

HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$fakehashvalue"


def test_create_returns_account_and_patient_ids(pg_conn):
    acct = db.create_account_with_patient(
        "ada@example.com", "Ada Lovelace", HASH, conn=pg_conn
    )
    assert acct["email"] == "ada@example.com"
    assert acct["display_name"] == "Ada Lovelace"
    assert acct["account_id"] and acct["patient_id"]


def test_create_also_creates_the_patient_row(pg_conn):
    acct = db.create_account_with_patient(
        "grace@example.com", "Grace Hopper", HASH, conn=pg_conn
    )
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT display_name FROM patients WHERE patient_id = %(p)s",
            {"p": acct["patient_id"]},
        )
        assert cur.fetchone()[0] == "Grace Hopper"


def test_create_normalizes_email(pg_conn):
    """The CHECK constraint would reject a non-normalized email, so this also
    proves normalization happens before the insert, not after."""
    acct = db.create_account_with_patient(
        "  MiXeD@Example.COM  ", "Mixed Case", HASH, conn=pg_conn
    )
    assert acct["email"] == "mixed@example.com"


def test_duplicate_email_raises(pg_conn):
    db.create_account_with_patient("dup@example.com", "First", HASH, conn=pg_conn)
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.create_account_with_patient("dup@example.com", "Second", HASH, conn=pg_conn)


def test_duplicate_email_leaves_no_orphan_patient(pg_conn):
    """Signup must be ONE transaction. If the account insert fails, the patient
    row created moments earlier must not survive."""
    db.create_account_with_patient("solo@example.com", "First", HASH, conn=pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM patients WHERE display_name = 'Orphan'")
        before = cur.fetchone()[0]
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.create_account_with_patient("solo@example.com", "Orphan", HASH, conn=pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM patients WHERE display_name = 'Orphan'")
        assert cur.fetchone()[0] == before


def test_find_by_email_returns_the_account(pg_conn):
    db.create_account_with_patient("find@example.com", "Findable", HASH, conn=pg_conn)
    found = db.find_account_by_email("find@example.com", conn=pg_conn)
    assert found["display_name"] == "Findable"
    assert found["password_hash"] == HASH


def test_find_by_email_is_case_insensitive(pg_conn):
    db.create_account_with_patient("case@example.com", "Case", HASH, conn=pg_conn)
    assert db.find_account_by_email("  CASE@Example.com ", conn=pg_conn) is not None


def test_find_by_email_returns_none_when_absent(pg_conn):
    assert db.find_account_by_email("nobody@example.com", conn=pg_conn) is None


def test_touch_last_login_sets_the_timestamp(pg_conn):
    acct = db.create_account_with_patient("tl@example.com", "TL", HASH, conn=pg_conn)
    db.touch_last_login(acct["account_id"], conn=pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT last_login_at FROM accounts WHERE account_id = %(a)s",
            {"a": acct["account_id"]},
        )
        assert cur.fetchone()[0] is not None
