"""The fixtures themselves reach a live database."""


def test_pg_conn_is_live(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1


def test_patients_table_exists(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.patients')")
        assert cur.fetchone()[0] is not None
