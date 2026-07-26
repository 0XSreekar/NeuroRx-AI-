"""The accounts table's constraints must actually reject bad rows.

Reading the DDL proves nothing — CLAUDE.md's Task 3.1 note makes this point
directly: only a live database proves a CHECK is enforced.
"""

import psycopg
import pytest

HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$fakehashvalue"


def _new_patient(conn, name="Test Patient") -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO patients (display_name) VALUES (%(n)s) RETURNING patient_id",
            {"n": name},
        )
        return str(cur.fetchone()[0])


def _insert_account(conn, patient_id, email="a@b.com", name="Ada"):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO accounts (email, display_name, password_hash, patient_id)
            VALUES (%(e)s, %(n)s, %(h)s, %(p)s)
            RETURNING account_id
            """,
            {"e": email, "n": name, "h": HASH, "p": patient_id},
        )
        return str(cur.fetchone()[0])


def test_accounts_table_exists(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.accounts')")
        assert cur.fetchone()[0] is not None


def test_happy_path_insert(pg_conn):
    pid = _new_patient(pg_conn)
    assert _insert_account(pg_conn, pid)


def test_rejects_duplicate_email(pg_conn):
    _insert_account(pg_conn, _new_patient(pg_conn), email="dup@x.com")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_account(pg_conn, _new_patient(pg_conn, "Other"), email="dup@x.com")


def test_rejects_unnormalized_email(pg_conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_account(pg_conn, _new_patient(pg_conn), email="Bob@X.com")


def test_rejects_email_without_at(pg_conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_account(pg_conn, _new_patient(pg_conn), email="nobody")


def test_rejects_blank_display_name(pg_conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_account(pg_conn, _new_patient(pg_conn), name="   ")


def test_rejects_second_account_for_same_patient(pg_conn):
    pid = _new_patient(pg_conn)
    _insert_account(pg_conn, pid, email="one@x.com")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_account(pg_conn, pid, email="two@x.com")


def test_deleting_patient_cascades_to_account(pg_conn):
    pid = _new_patient(pg_conn)
    _insert_account(pg_conn, pid, email="gone@x.com")
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM patients WHERE patient_id = %(p)s", {"p": pid})
        cur.execute("SELECT count(*) FROM accounts WHERE patient_id = %(p)s", {"p": pid})
        assert cur.fetchone()[0] == 0
