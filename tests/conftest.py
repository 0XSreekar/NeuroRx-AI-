"""Shared fixtures. Every DB test runs against a real local Postgres.

CLAUDE.md §6: modules with no Spark/Databricks dependency are actually run,
not just read. Tests skip (never fail) when no local Postgres is configured,
so the suite stays green on a machine that hasn't followed docs/local_dev.md.
"""

import os
import pathlib
import sys

import psycopg
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_SQL = REPO_ROOT / "lakebase" / "schema.sql"

# The repo root must lead sys.path so `import app.db` resolves the package and
# not app/app.py — the same shadowing hazard app/app.py's own bootstrap fixes,
# and it bites here too when pytest is invoked from another directory.
while str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))


def _local_conninfo() -> str | None:
    return os.getenv("NEURORX_LOCAL_PG")


@pytest.fixture(scope="session")
def pg_schema() -> str:
    """Apply lakebase/schema.sql once per session. Returns the conninfo."""
    conninfo = _local_conninfo()
    if not conninfo:
        pytest.skip("NEURORX_LOCAL_PG not set — see docs/local_dev.md")
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute(SCHEMA_SQL.read_text())
    return conninfo


@pytest.fixture
def pg_conn(pg_schema):
    """A connection whose work is rolled back after each test.

    Every test gets a clean database without re-applying the schema: the
    transaction is never committed, so inserts vanish on rollback. This is why
    tests must not open their own second connection to observe their writes —
    an uncommitted transaction is invisible outside it.
    """
    with psycopg.connect(pg_schema) as conn:
        yield conn
        conn.rollback()
