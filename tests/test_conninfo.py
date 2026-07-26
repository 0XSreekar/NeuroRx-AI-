"""_conninfo() must honor NEURORX_LOCAL_PG verbatim.

The local demo path passes a full libpq conninfo string; rewriting or
appending to it (e.g. forcing sslmode) would break the Unix-socket
connection the runbook sets up.
"""

import app.db as db


def test_conninfo_uses_local_pg_verbatim(monkeypatch):
    monkeypatch.setenv("NEURORX_LOCAL_PG", "host=/tmp/nrx_pg port=5439 dbname=x")
    assert db._conninfo() == "host=/tmp/nrx_pg port=5439 dbname=x"


def test_conninfo_falls_back_to_lakebase_settings(monkeypatch):
    monkeypatch.delenv("NEURORX_LOCAL_PG", raising=False)
    result = db._conninfo()
    assert "sslmode=require" in result
    assert "port=5432" in result
