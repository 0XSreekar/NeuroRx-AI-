# Home Page, Accounts, and Sign-In Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a public home page and real accounts — sign up with name, email, and password; sign in; then use the existing three features as that account.

**Architecture:** `app/app.py` becomes a thin router using `st.navigation(position="hidden")`, registering a different page set depending on auth state, so the signed-out set simply does not contain the app page. Identity lives behind one seam, `app/auth.py`, which owns password hashing and session state and delegates all SQL to `app/db.py`. The three existing views are untouched.

**Tech Stack:** Streamlit 1.59.2, psycopg 3, PostgreSQL (Lakebase; local Postgres 18 on the demo path), argon2-cffi, pytest.

**Spec:** [`docs/superpowers/specs/2026-07-26-neurorx-accounts-auth-design.md`](../specs/2026-07-26-neurorx-accounts-auth-design.md)

## Global Constraints

- **`DATA_CONTRACTS.md` is frozen.** Any schema change updates that file **first**, then the code (CLAUDE.md §6).
- **Pin exact versions**, resolved live against PyPI: `argon2-cffi==25.1.0`, `pytest==9.1.1`.
- **`app/db.py` is the only module that executes SQL.** `auth.py` must not open connections or embed SQL.
- **psycopg paramstyle is `%(name)s`**, never `:name` (that is the Databricks SQL connector's style). Do not interchange them — CLAUDE.md §4.
- **Never store, log, or place a plaintext password in `st.session_state`.** Only the argon2 hash is persisted.
- **`authenticate()` must return the same `None`** for unknown-email and wrong-password, so the form cannot enumerate accounts.
- **Auth is not an access boundary** — the demo patient switcher stays. Say so in `auth.py`'s docstring; do not imply protection.
- **Do not seed fabricated medication data** into new accounts. Empty states are correct.
- **All tests run against real local Postgres** via `NEURORX_LOCAL_PG`, per CLAUDE.md §6 ("if a module has no Spark/Databricks dependency, actually run it").
- **Every new user-facing string uses the design layer** in `app/theme.py` (`eyebrow`, `section_heading`, `stat_card`, `nrx-card`). No new emoji in headings or buttons.
- **Quote every shell path** — the project path contains a space (`/Users/guts/Projects /NeuroRx AI`).

---

## File Structure

| File | Responsibility |
|---|---|
| `app/auth.py` | **New.** The identity seam: password hash/verify, account creation/authentication, session read/write. No SQL, no widgets. |
| `app/views/home.py` | **New.** Public landing page (hero, trust cards, CTAs). |
| `app/views/signup.py` | **New.** Create-account form. |
| `app/views/login.py` | **New.** Sign-in form. |
| `app/db.py` | **Modify.** Add `accounts` reads/writes and extract `_conninfo()` so non-Streamlit callers share one conninfo. |
| `app/app.py` | **Modify.** Becomes the router; existing shell moves into a page function. |
| `app/theme.py` | **Modify.** Add auth-screen CSS (centered card, form width, hero). |
| `lakebase/schema.sql` | **Modify.** `accounts` table. |
| `DATA_CONTRACTS.md` | **Modify.** §6.5 `accounts`. Updated **first**. |
| `requirements.txt` | **Modify.** `argon2-cffi==25.1.0`. |
| `requirements-dev.txt` | **Modify.** `pytest==9.1.1`. |
| `tests/conftest.py` | **New.** Postgres fixture keyed on `NEURORX_LOCAL_PG`. |
| `tests/test_*.py` | **New.** Per-task test modules. |
| `docs/local_dev.md` | **Modify.** Auth setup + how to run tests. |

---

### Task 1: Test harness and shared conninfo

**Files:**
- Create: `tests/conftest.py`, `tests/test_conninfo.py`
- Modify: `app/db.py` (extract `_conninfo()`), `requirements-dev.txt`
- Create: `pytest.ini`

**Interfaces:**
- Consumes: nothing.
- Produces: `db._conninfo() -> str`; pytest fixtures `pg_conn` (a live `psycopg.Connection` with autocommit off) and `pg_schema` (applies `lakebase/schema.sql` once per session).

**Why this task exists first:** there is currently no pytest, no `tests/`, and no way to reach Postgres outside a Streamlit process — `db._get_pool()` is `@st.cache_resource`-decorated. Every later task needs both.

- [ ] **Step 1: Add pytest to dev requirements**

Append to `requirements-dev.txt`:

```
# --- Testing ---
# Version resolved live against the PyPI JSON API, per CLAUDE.md §6.
pytest==9.1.1
```

- [ ] **Step 2: Create `pytest.ini`**

```ini
[pytest]
testpaths = tests
python_files = test_*.py
addopts = -v --tb=short
```

- [ ] **Step 3: Write the failing test for `_conninfo()`**

Create `tests/test_conninfo.py`:

```python
"""_conninfo() must honor NEURORX_LOCAL_PG verbatim.

The local demo path passes a full libpq conninfo string; rewriting or
appending to it (e.g. forcing sslmode) would break the Unix-socket
connection the runbook sets up.
"""
import importlib

import app.db as db


def test_conninfo_uses_local_pg_verbatim(monkeypatch):
    monkeypatch.setenv("NEURORX_LOCAL_PG", "host=/tmp/nrx_pg port=5439 dbname=x")
    assert db._conninfo() == "host=/tmp/nrx_pg port=5439 dbname=x"


def test_conninfo_falls_back_to_lakebase_settings(monkeypatch):
    monkeypatch.delenv("NEURORX_LOCAL_PG", raising=False)
    result = db._conninfo()
    assert "sslmode=require" in result
    assert "port=5432" in result
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `python -m pytest tests/test_conninfo.py -v`
Expected: FAIL with `AttributeError: module 'app.db' has no attribute '_conninfo'`

- [ ] **Step 5: Extract `_conninfo()` in `app/db.py`**

Replace the body of `_get_pool()` (currently at `app/db.py:92`) so the conninfo construction lives in its own module-level function. Move `import os` to the top of the module if it is not already there.

```python
def _conninfo() -> str:
    """The libpq conninfo string for Lakebase, or the local Postgres standing
    in for it.

    NEURORX_LOCAL_PG is used verbatim — it is a complete conninfo string for
    the off-workspace demo path (docs/local_dev.md), typically a Unix socket
    with no TLS. Appending to it would break that. Extracted from _get_pool()
    so non-Streamlit callers (tests, jobs) can build the same connection
    without going through @st.cache_resource.
    """
    local = os.getenv("NEURORX_LOCAL_PG")
    if local:
        return local
    return (
        f"host={settings.lakebase_host} "
        f"dbname={settings.lakebase_db} "
        f"user={settings.lakebase_user} "
        f"password={settings.lakebase_password} "
        f"sslmode=require port=5432"
    )


@st.cache_resource
def _get_pool() -> ConnectionPool:
    """One pool per Streamlit process, memoized via st.cache_resource so a
    script rerun (Streamlit's normal execution model — the whole script
    re-executes on every interaction) reuses the same pool instead of
    leaking a new one on every rerun.
    """
    return ConnectionPool(
        _conninfo(),
        min_size=_POOL_MIN_SIZE,
        max_size=_POOL_MAX_SIZE,
        open=True,
    )
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `python -m pytest tests/test_conninfo.py -v`
Expected: PASS, 2 passed

- [ ] **Step 7: Create `tests/conftest.py`**

```python
"""Shared fixtures. Every DB test runs against a real local Postgres.

CLAUDE.md §6: modules with no Spark/Databricks dependency are actually run,
not just read. Tests skip (never fail) when no local Postgres is configured,
so the suite stays green on a machine that hasn't followed docs/local_dev.md.
"""
import os
import pathlib

import psycopg
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_SQL = REPO_ROOT / "lakebase" / "schema.sql"


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
```

- [ ] **Step 8: Verify the fixtures connect**

Create `tests/test_harness.py`:

```python
def test_pg_conn_is_live(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1


def test_patients_table_exists(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.patients')")
        assert cur.fetchone()[0] is not None
```

Run:

```bash
NEURORX_LOCAL_PG="host=/tmp/nrx_pg port=5439 user=postgres dbname=databricks_postgres" python -m pytest tests/ -v
```

Expected: PASS, 4 passed

- [ ] **Step 9: Commit**

```bash
git add pytest.ini tests/ requirements-dev.txt app/db.py
git commit -m "test: add pytest harness against real local Postgres

No test infrastructure existed. Adds a session-scoped fixture that applies
lakebase/schema.sql and a per-test connection that rolls back, so tests get
a clean DB without re-applying the schema.

Extracts db._conninfo() from _get_pool(), which is @st.cache_resource-
decorated and therefore unusable outside a Streamlit process. Tests and jobs
now build the same conninfo the app does."
```

---

### Task 2: `accounts` table — contract first, then DDL

**Files:**
- Modify: `DATA_CONTRACTS.md` (add §6.5), `lakebase/schema.sql`
- Create: `tests/test_accounts_schema.py`

**Interfaces:**
- Consumes: `pg_conn`, `pg_schema` fixtures from Task 1.
- Produces: table `accounts (account_id UUID PK, email TEXT, display_name TEXT, password_hash TEXT, patient_id UUID, created_at TIMESTAMPTZ, last_login_at TIMESTAMPTZ)` with constraints named `accounts_email_unique`, `accounts_patient_unique`, `accounts_patient_fk`, `accounts_email_normalized`, `accounts_email_shape`, `accounts_name_present`.

- [ ] **Step 1: Update `DATA_CONTRACTS.md` first**

Add after §6.4, following the existing table-section format:

```markdown
### 6.5 `accounts`

**Purpose:** Application sign-in. One account owns exactly one patient record
(1:1). Synthetic only — no PHI, ever.

**This is not an access boundary.** The demo patient switcher remains, so a
signed-in account can still view any patient's data. See
`docs/superpowers/specs/2026-07-26-neurorx-accounts-auth-design.md` §7.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `account_id` | `UUID` | No | **PK.** `DEFAULT gen_random_uuid()`. |
| `email` | `TEXT` | No | Login identifier. Stored already normalized (lowercased, trimmed) — enforced by CHECK, not only in Python. |
| `display_name` | `TEXT` | No | The person's name, collected at signup and shown in the app. Never used to sign in; may repeat across accounts. |
| `password_hash` | `TEXT` | No | argon2id encoded string (~97 chars), including its own parameters. Never the plaintext. |
| `patient_id` | `UUID` | No | **FK** → `patients.patient_id`, `ON DELETE CASCADE`. `UNIQUE` — the 1:1 model. |
| `created_at` | `TIMESTAMPTZ` | No | `DEFAULT now()`. |
| `last_login_at` | `TIMESTAMPTZ` | Yes | `NULL` until the first successful sign-in. |

```sql
CONSTRAINT accounts_email_unique     UNIQUE (email)
CONSTRAINT accounts_patient_unique   UNIQUE (patient_id)
CONSTRAINT accounts_email_normalized CHECK (email = lower(btrim(email)))
CONSTRAINT accounts_email_shape      CHECK (position('@' in email) > 1)
CONSTRAINT accounts_name_present     CHECK (length(trim(display_name)) > 0)
```

`accounts_email_normalized` exists because lowercasing only in Python means one
forgotten call creates `Bob@x.com` alongside `bob@x.com`, and `UNIQUE (email)`
never notices. `accounts_email_shape` is a sanity check, not email validation.
```

- [ ] **Step 2: Write the failing constraint tests**

Create `tests/test_accounts_schema.py`:

```python
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `NEURORX_LOCAL_PG="host=/tmp/nrx_pg port=5439 user=postgres dbname=databricks_postgres" python -m pytest tests/test_accounts_schema.py -v`
Expected: FAIL — `test_accounts_table_exists` asserts `None is not None`; the rest raise `UndefinedTable`.

- [ ] **Step 4: Add the DDL to `lakebase/schema.sql`**

Append after the `guardrail_blocks` table, before any trigger/index section:

```sql
-- ---------------------------------------------------------------------------
-- accounts — application sign-in (DATA_CONTRACTS.md §6.5)
--
-- One account owns exactly one patient (UNIQUE (patient_id)). NOT an access
-- boundary: the demo patient switcher remains, so a signed-in account can
-- still view any patient's data.
--
-- accounts_email_normalized enforces normalization in the DATABASE, not only
-- in Python: one forgotten lower() would otherwise create 'Bob@x.com'
-- alongside 'bob@x.com' and UNIQUE (email) would never notice.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS accounts (
    account_id     UUID        NOT NULL DEFAULT gen_random_uuid(),
    email          TEXT        NOT NULL,
    display_name   TEXT        NOT NULL,
    password_hash  TEXT        NOT NULL,
    patient_id     UUID        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at  TIMESTAMPTZ,

    CONSTRAINT accounts_pkey             PRIMARY KEY (account_id),
    CONSTRAINT accounts_email_unique     UNIQUE (email),
    CONSTRAINT accounts_patient_unique   UNIQUE (patient_id),
    CONSTRAINT accounts_patient_fk       FOREIGN KEY (patient_id)
                                         REFERENCES patients (patient_id)
                                         ON DELETE CASCADE,
    CONSTRAINT accounts_email_normalized CHECK (email = lower(btrim(email))),
    CONSTRAINT accounts_email_shape      CHECK (position('@' in email) > 1),
    CONSTRAINT accounts_name_present     CHECK (length(trim(display_name)) > 0)
);
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `NEURORX_LOCAL_PG="host=/tmp/nrx_pg port=5439 user=postgres dbname=databricks_postgres" python -m pytest tests/test_accounts_schema.py -v`
Expected: PASS, 8 passed

- [ ] **Step 6: Verify the schema is still idempotent**

Run twice in a row:

```bash
psql -h /tmp/nrx_pg -p 5439 -U postgres -d databricks_postgres -v ON_ERROR_STOP=1 -f "lakebase/schema.sql"
psql -h /tmp/nrx_pg -p 5439 -U postgres -d databricks_postgres -v ON_ERROR_STOP=1 -f "lakebase/schema.sql"
```

Expected: both runs exit 0 with no ERROR lines.

- [ ] **Step 7: Commit**

```bash
git add DATA_CONTRACTS.md lakebase/schema.sql tests/test_accounts_schema.py
git commit -m "feat(schema): add accounts table

DATA_CONTRACTS.md §6.5 updated first, per CLAUDE.md §6 — the contract is
frozen, so it changes before the code does.

Email normalization is a CHECK constraint, not just a Python call: one
forgotten lower() would create 'Bob@x.com' beside 'bob@x.com' and
UNIQUE (email) would never notice. Verified against real Postgres that each
constraint actually rejects its bad row, and that deleting a patient
cascades to the account."
```

---

### Task 3: Password hashing

**Files:**
- Create: `app/auth.py` (hashing only), `tests/test_auth_password.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: nothing.
- Produces: `auth.hash_password(plaintext: str) -> str`, `auth.verify_password(hash_: str, plaintext: str) -> bool`, `auth.MIN_PASSWORD_LENGTH: int = 8`, `auth.WeakPassword(Exception)`.

- [ ] **Step 1: Add argon2-cffi to `requirements.txt`**

Add under a new section:

```
# --- Auth ---
# argon2id, OWASP's first-choice password hash. Version resolved live against
# the PyPI JSON API. NOTE: the top-level package ships only a pure-Python
# wheel; the C extension lives in argon2-cffi-bindings, which does publish
# cp314 wheels — confirmed before pinning, since the parent package alone
# looks like a Python 3.14 incompatibility.
argon2-cffi==25.1.0
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_auth_password.py`:

```python
import pytest

from app import auth


def test_hash_is_argon2id_and_not_the_plaintext():
    h = auth.hash_password("correct horse battery staple")
    assert h.startswith("$argon2id$")
    assert "correct horse" not in h


def test_verify_accepts_the_right_password():
    h = auth.hash_password("correct horse battery staple")
    assert auth.verify_password(h, "correct horse battery staple") is True


def test_verify_rejects_the_wrong_password():
    h = auth.hash_password("correct horse battery staple")
    assert auth.verify_password(h, "wrong") is False


def test_verify_returns_false_on_a_malformed_hash():
    """A corrupt stored hash must read as 'wrong password', not crash the
    login screen with a 500."""
    assert auth.verify_password("not-a-hash", "anything") is False


def test_same_password_hashes_differently_each_time():
    """Distinct salts — two accounts with the same password must not share a
    hash, or the table leaks which users chose the same password."""
    assert auth.hash_password("same") != auth.hash_password("same")


def test_short_password_is_rejected():
    with pytest.raises(auth.WeakPassword):
        auth.hash_password("a" * (auth.MIN_PASSWORD_LENGTH - 1))


def test_minimum_length_password_is_accepted():
    assert auth.hash_password("a" * auth.MIN_PASSWORD_LENGTH)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_auth_password.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.auth'`

- [ ] **Step 4: Create `app/auth.py` with hashing only**

```python
"""NeuroRx AI — the identity seam.

Everything about "who is using the app" lives here: password hashing, account
creation and authentication, and the session entry. Views call this module;
this module calls `app/db.py` for SQL and never opens a connection itself.

## This is NOT an access boundary

The demo patient switcher in the app header is deliberately kept, so any
signed-in account can still view any patient's data. Signing in identifies
who you are; it does not restrict what you can read. Do not describe this
module as protection. Real per-patient authorization is explicitly out of
scope — see docs/superpowers/specs/2026-07-26-neurorx-accounts-auth-design.md §7.

## Why this module exists as a seam

The design is "demo now, real later": swapping these functions for an OIDC
provider (Streamlit 1.59 ships st.login()/st.user) should not require touching
any view. That only holds if views never reach past this module — so nothing
here returns a DB row, a hash, or a Streamlit object.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# argon2id at the library's defaults: m=64MiB, t=3, p=4. These are OWASP's
# recommended parameters and measured ~33 ms on this project's Python 3.14.
# The encoded hash carries its own parameters, so check_needs_rehash() can flag
# stale ones if these ever change.
_hasher = PasswordHasher()

# Length is the only password rule. Composition rules (a digit, a symbol, mixed
# case) push people toward predictable substitutions without adding real
# entropy, so they are deliberately not applied.
MIN_PASSWORD_LENGTH = 8


class WeakPassword(Exception):
    """Raised when a password is shorter than MIN_PASSWORD_LENGTH."""


def hash_password(plaintext: str) -> str:
    """Return an argon2id encoded hash. Never returns or logs the plaintext."""
    if len(plaintext) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    return _hasher.hash(plaintext)


def verify_password(hash_: str, plaintext: str) -> bool:
    """True if `plaintext` matches `hash_`.

    Returns False — never raises — for a wrong password AND for a malformed or
    corrupt stored hash. A bad row in the database must read as a failed login,
    not a traceback on the sign-in screen.
    """
    try:
        return _hasher.verify(hash_, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_auth_password.py -v`
Expected: PASS, 7 passed

- [ ] **Step 6: Commit**

```bash
git add app/auth.py tests/test_auth_password.py requirements.txt
git commit -m "feat(auth): argon2id password hashing

verify_password returns False rather than raising on a malformed stored
hash — a corrupt row must read as a failed login, not a traceback on the
sign-in screen.

argon2-cffi pinned after confirming the C extension (argon2-cffi-bindings)
publishes cp314 wheels; the top-level package ships only a pure-Python wheel
and alone looks like a Python 3.14 incompatibility."
```

---

### Task 4: Account persistence in `app/db.py`

**Files:**
- Modify: `app/db.py`
- Create: `tests/test_db_accounts.py`

**Interfaces:**
- Consumes: `db._conninfo()` (Task 1), the `accounts` table (Task 2).
- Produces:
  - `db.create_account_with_patient(email: str, display_name: str, password_hash: str) -> dict` — returns `{"account_id", "email", "display_name", "patient_id"}`. Raises `psycopg.errors.UniqueViolation` if the email exists.
  - `db.find_account_by_email(email: str) -> dict | None` — returns `{"account_id", "email", "display_name", "patient_id", "password_hash"}` or `None`.
  - `db.touch_last_login(account_id: str) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_db_accounts.py`:

```python
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
    try:
        db.create_account_with_patient("solo@example.com", "Orphan", HASH, conn=pg_conn)
    except psycopg.errors.UniqueViolation:
        pg_conn.rollback()
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NEURORX_LOCAL_PG="host=/tmp/nrx_pg port=5439 user=postgres dbname=databricks_postgres" python -m pytest tests/test_db_accounts.py -v`
Expected: FAIL with `AttributeError: module 'app.db' has no attribute 'create_account_with_patient'`

- [ ] **Step 3: Implement the three functions in `app/db.py`**

Add near the other writes. Note the `conn` parameter — it exists so tests can run inside one rolled-back transaction; the app passes nothing and gets a pooled connection.

```python
# ---------------------------------------------------------------------------
# Accounts (DATA_CONTRACTS.md §6.5)
# ---------------------------------------------------------------------------


def _normalize_email(email: str) -> str:
    """Lowercase and trim. The accounts_email_normalized CHECK enforces this
    database-side too — this is the convenience, not the guarantee."""
    return email.strip().lower()


@contextmanager
def _conn_or_pooled(conn):
    """Yield the caller's connection, or borrow one from the pool.

    Tests pass an explicit connection so their writes stay inside a single
    rolled-back transaction. The app passes nothing: db's pool is
    @st.cache_resource-decorated and would otherwise open a second connection
    that cannot see uncommitted rows.
    """
    if conn is not None:
        yield conn
    else:
        with _get_pool().connection() as pooled:
            yield pooled


def create_account_with_patient(
    email: str, display_name: str, password_hash: str, conn=None
) -> dict:
    """Create the patient row and its account in ONE transaction.

    Both inserts share a transaction so a duplicate-email failure cannot leave
    an orphan patient behind. Raises psycopg.errors.UniqueViolation when the
    email already exists; the caller decides how to phrase that.
    """
    normalized = _normalize_email(email)
    with _conn_or_pooled(conn) as active:
        with active.transaction():
            with active.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    INSERT INTO patients (display_name)
                    VALUES (%(name)s)
                    RETURNING patient_id
                    """,
                    {"name": display_name},
                )
                patient_id = cur.fetchone()["patient_id"]

                cur.execute(
                    """
                    INSERT INTO accounts (email, display_name, password_hash, patient_id)
                    VALUES (%(email)s, %(name)s, %(hash)s, %(patient_id)s)
                    RETURNING account_id, email, display_name, patient_id
                    """,
                    {
                        "email": normalized,
                        "name": display_name,
                        "hash": password_hash,
                        "patient_id": patient_id,
                    },
                )
                row = cur.fetchone()

    return {
        "account_id": str(row["account_id"]),
        "email": row["email"],
        "display_name": row["display_name"],
        "patient_id": str(row["patient_id"]),
    }


def find_account_by_email(email: str, conn=None) -> Optional[dict]:
    """The account for this email, or None. Includes password_hash — the only
    function that returns it, and only auth.authenticate() should call it."""
    with _conn_or_pooled(conn) as active:
        with active.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT account_id, email, display_name, patient_id, password_hash
                FROM accounts
                WHERE email = %(email)s
                """,
                {"email": _normalize_email(email)},
            )
            row = cur.fetchone()

    if row is None:
        return None
    return {
        "account_id": str(row["account_id"]),
        "email": row["email"],
        "display_name": row["display_name"],
        "patient_id": str(row["patient_id"]),
        "password_hash": row["password_hash"],
    }


def touch_last_login(account_id: str, conn=None) -> None:
    """Record a successful sign-in."""
    with _conn_or_pooled(conn) as active:
        with active.cursor() as cur:
            cur.execute(
                "UPDATE accounts SET last_login_at = now() WHERE account_id = %(a)s",
                {"a": account_id},
            )
```

Add to the imports at the top of `app/db.py` if not already present:

```python
from contextlib import contextmanager

from psycopg.rows import dict_row
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NEURORX_LOCAL_PG="host=/tmp/nrx_pg port=5439 user=postgres dbname=databricks_postgres" python -m pytest tests/test_db_accounts.py -v`
Expected: PASS, 9 passed

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_db_accounts.py
git commit -m "feat(db): account creation and lookup

create_account_with_patient does both inserts in one transaction, so a
duplicate-email failure cannot leave an orphan patient row — verified
against real Postgres, not assumed.

Every function takes an optional conn so tests can run inside a single
rolled-back transaction; db's own pool is @st.cache_resource-decorated and
would open a second connection unable to see uncommitted rows."
```

---

### Task 5: Account API and session in `app/auth.py`

**Files:**
- Modify: `app/auth.py`
- Create: `tests/test_auth_accounts.py`

**Interfaces:**
- Consumes: `db.create_account_with_patient`, `db.find_account_by_email`, `db.touch_last_login` (Task 4); `hash_password`, `verify_password`, `WeakPassword` (Task 3).
- Produces:
  - `auth.Account` dataclass: `account_id: str`, `email: str`, `display_name: str`, `patient_id: str`
  - `auth.EmailTaken(Exception)`
  - `auth.create_account(email, display_name, password) -> Account`
  - `auth.authenticate(email, password) -> Account | None`
  - `auth.sign_in(account) -> None`, `auth.sign_out() -> None`, `auth.current_account() -> Account | None`
  - `auth.SESSION_KEY: str = "account"`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_auth_accounts.py`:

```python
import pytest

from app import auth, db


@pytest.fixture(autouse=True)
def _route_db_through_the_test_connection(pg_conn, monkeypatch):
    """auth calls db.* with no conn; bind them to the rolled-back test
    connection so auth's own code path is exercised unchanged."""
    for name in ("create_account_with_patient", "find_account_by_email", "touch_last_login"):
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
    assert auth.authenticate("enum@example.com", "wrong") == auth.authenticate(
        "nosuch@example.com", "wrong"
    ) == None


def test_authenticate_is_case_insensitive_on_email(pg_conn):
    auth.create_account("ci@example.com", "CI", "hunter2hunter2")
    assert auth.authenticate("  CI@Example.COM ", "hunter2hunter2") is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NEURORX_LOCAL_PG="host=/tmp/nrx_pg port=5439 user=postgres dbname=databricks_postgres" python -m pytest tests/test_auth_accounts.py -v`
Expected: FAIL with `AttributeError: module 'app.auth' has no attribute 'Account'`

- [ ] **Step 3: Extend `app/auth.py`**

Add to the imports:

```python
from dataclasses import dataclass
from typing import Optional

import psycopg
import streamlit as st

from app import db
```

Append:

```python
SESSION_KEY = "account"


@dataclass(frozen=True)
class Account:
    """The signed-in identity, as views see it.

    Deliberately does NOT carry password_hash: this object is placed in
    st.session_state, and a hash has no business being there.
    """

    account_id: str
    email: str
    display_name: str
    patient_id: str


class EmailTaken(Exception):
    """Raised when an email already has an account."""


def create_account(email: str, display_name: str, password: str) -> Account:
    """Create an account and its patient record.

    Raises WeakPassword before touching the database, and EmailTaken (rather
    than leaking psycopg's UniqueViolation) so views handle one vocabulary.
    """
    password_hash = hash_password(password)
    try:
        row = db.create_account_with_patient(email, display_name, password_hash)
    except psycopg.errors.UniqueViolation as exc:
        raise EmailTaken(f"An account already exists for {email.strip().lower()}.") from exc
    return Account(
        account_id=row["account_id"],
        email=row["email"],
        display_name=row["display_name"],
        patient_id=row["patient_id"],
    )


def authenticate(email: str, password: str) -> Optional[Account]:
    """The Account for these credentials, or None.

    Returns the SAME None for an unknown email and for a wrong password, so
    the sign-in form cannot be used to discover which emails have accounts.
    Do not split this into distinct errors for a friendlier message.
    """
    row = db.find_account_by_email(email)
    if row is None:
        return None
    if not verify_password(row["password_hash"], password):
        return None

    db.touch_last_login(row["account_id"])
    return Account(
        account_id=row["account_id"],
        email=row["email"],
        display_name=row["display_name"],
        patient_id=row["patient_id"],
    )


def sign_in(account: Account) -> None:
    st.session_state[SESSION_KEY] = account


def sign_out() -> None:
    st.session_state.pop(SESSION_KEY, None)


def current_account() -> Optional[Account]:
    """The signed-in Account, or None.

    Backed by st.session_state, which is per-session: refreshing the browser
    signs the user out. Streamlit exposes no first-party cookie-WRITE API
    (st.context.cookies is read-only), so this is accepted for the demo and
    is one of the things swapping in OIDC would fix.
    """
    return st.session_state.get(SESSION_KEY)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NEURORX_LOCAL_PG="host=/tmp/nrx_pg port=5439 user=postgres dbname=databricks_postgres" python -m pytest tests/test_auth_accounts.py -v`
Expected: PASS, 9 passed

- [ ] **Step 5: Commit**

```bash
git add app/auth.py tests/test_auth_accounts.py
git commit -m "feat(auth): account creation, authentication, session

authenticate() returns the same None for unknown-email and wrong-password,
so the sign-in form cannot enumerate which emails have accounts — asserted
directly by a test rather than left as a comment.

The Account dataclass deliberately omits password_hash: it goes into
st.session_state, where a hash has no business being."
```

---

### Task 6: Home page

**Files:**
- Create: `app/views/home.py`, `tests/test_view_home.py`
- Modify: `app/theme.py`

**Interfaces:**
- Consumes: `theme.eyebrow`, `theme.brand` (existing).
- Produces: `home.render(on_signup: Callable[[], None], on_login: Callable[[], None]) -> None`. Callbacks are injected so this view never imports the router — Task 9 supplies them.

- [ ] **Step 1: Add hero CSS to `app/theme.py`**

Insert into `_COMPONENTS`, before the `--- citation chip ---` block:

```css
/* --- home hero ------------------------------------------------------------ */
.nrx-hero { padding: 4.5rem 0 2.5rem; max-width: 46rem; }
.nrx-hero h1 {
  font-family: var(--nrx-serif); font-weight: 400;
  font-size: clamp(2.6rem, 6vw, 4.2rem); line-height: 1.05;
  letter-spacing: -0.02em; margin: 0.7rem 0 0;
}
.nrx-hero h1 em { font-style: italic; color: var(--nrx-accent); }
.nrx-hero p {
  font-size: 1.02rem; color: var(--nrx-text-70);
  margin: 1.1rem 0 0; max-width: 34rem; line-height: 1.6;
}
.nrx-trust { display: flex; flex-wrap: wrap; gap: 0.9rem; margin-top: 3rem; }
.nrx-trust .nrx-card { flex: 1 1 15rem; }
.nrx-trust .t { font-size: 0.92rem; font-weight: 500; margin-top: 0.5rem; }
.nrx-trust .b { font-size: 0.78rem; color: var(--nrx-text-50); margin-top: 0.3rem; line-height: 1.5; }
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_view_home.py`:

```python
"""The home page is public and must never leak patient data."""
from streamlit.testing.v1 import AppTest


def _script():
    from app.views import home

    home.render(on_signup=lambda: None, on_login=lambda: None)


def test_home_renders_the_product_name():
    at = AppTest.from_function(_script).run()
    assert not at.exception
    assert any("NeuroRx" in m.value for m in at.markdown)


def test_home_offers_both_entry_points():
    at = AppTest.from_function(_script).run()
    labels = {b.label for b in at.button}
    assert "Create account" in labels
    assert "Sign in" in labels


def test_home_states_the_safety_position():
    at = AppTest.from_function(_script).run()
    body = " ".join(m.value for m in at.markdown).lower()
    assert "not medical advice" in body


def test_home_mentions_no_patient_names():
    """A public page must not name anyone from the cohort."""
    at = AppTest.from_function(_script).run()
    body = " ".join(m.value for m in at.markdown).lower()
    assert "margaret" not in body
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/test_view_home.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.views.home'`

- [ ] **Step 4: Create `app/views/home.py`**

```python
"""NeuroRx AI — public home page.

The landing surface, visible signed-out. Renders no patient data of any kind:
it is reachable without authenticating, so nothing here may touch app/db.py.

The design language comes from design/mockup.html via app/theme.py. The
mockup's animated starfield is not ported — it needs a canvas render loop,
which inside Streamlit would require a sandboxed component iframe and could
not sit behind page content. theme.py approximates it with CSS gradients.
"""

from typing import Callable

import streamlit as st

from app import theme


def render(on_signup: Callable[[], None], on_login: Callable[[], None]) -> None:
    """Render the landing page.

    Navigation is injected as callbacks rather than imported, so this view has
    no dependency on the router and stays trivially testable.
    """
    st.markdown(theme.brand(), unsafe_allow_html=True)

    st.markdown(
        '<div class="nrx-hero">'
        f'{theme.eyebrow("MEDICATION SCHEDULES, ORGANIZED")}'
        "<h1>Every answer traced back to<br>the <em>label it came from</em>.</h1>"
        "<p>NeuroRx AI turns a prescription into a schedule you can actually keep — "
        "dose reminders, interaction checks, and adherence you can see. Clinical "
        "facts come from deterministic lookups over FDA labels, each one cited. "
        "This is an organizational assistant, not medical advice.</p>"
        "</div>",
        unsafe_allow_html=True,
    )

    col_signup, col_login, _ = st.columns([1, 1, 3])
    with col_signup:
        if st.button("Create account", type="primary", use_container_width=True):
            on_signup()
    with col_login:
        if st.button("Sign in", use_container_width=True):
            on_login()

    cards = [
        (
            "CITED BY CONSTRUCTION",
            "Grounded answers",
            "Every clinical statement carries an FDA label citation you can expand and read.",
        ),
        (
            "CHECKED BEFORE SAVING",
            "Interaction checks",
            "Adding a drug runs a deterministic interaction check first. You confirm changes, not the model.",
        ),
        (
            "SYNTHETIC ONLY",
            "No real patient data",
            "Every record in this demo is generated. No PHI is stored, ever.",
        ),
    ]
    st.markdown(
        '<div class="nrx-trust">'
        + "".join(
            f'<div class="nrx-card">{theme.eyebrow(eyebrow)}'
            f'<div class="t">{title}</div><div class="b">{body}</div></div>'
            for eyebrow, title, body in cards
        )
        + "</div>",
        unsafe_allow_html=True,
    )
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_view_home.py -v`
Expected: PASS, 4 passed

- [ ] **Step 6: Commit**

```bash
git add app/views/home.py tests/test_view_home.py app/theme.py
git commit -m "feat(app): public home page

Renders no patient data — it is reachable without authenticating, so it
never imports app/db.py, and a test asserts no cohort name appears.

Navigation is injected as callbacks rather than imported, so the view has no
dependency on the router."
```

---

### Task 7: Signup screen

**Files:**
- Create: `app/views/signup.py`, `tests/test_view_signup.py`
- Modify: `app/theme.py`

**Interfaces:**
- Consumes: `auth.create_account`, `auth.EmailTaken`, `auth.WeakPassword`, `auth.MIN_PASSWORD_LENGTH`, `auth.sign_in` (Tasks 3, 5).
- Produces: `signup.render(on_success: Callable[[], None], on_login: Callable[[], None]) -> None`

- [ ] **Step 1: Add auth-card CSS to `app/theme.py`**

Insert into `_COMPONENTS`, after the hero block:

```css
/* --- auth screens --------------------------------------------------------- */
.nrx-auth { max-width: 26rem; margin: 3.5rem auto 0; }
.nrx-auth h2 {
  font-family: var(--nrx-serif); font-weight: 400; font-size: 2.1rem;
  letter-spacing: -0.015em; margin: 0.4rem 0 1.4rem;
}
.nrx-auth-note {
  font-size: 0.74rem; color: var(--nrx-text-50);
  line-height: 1.5; margin-top: 0.9rem;
}
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_view_signup.py`:

```python
import pytest
from streamlit.testing.v1 import AppTest

from app import auth, db


@pytest.fixture(autouse=True)
def _route_db_through_the_test_connection(pg_conn, monkeypatch):
    for name in ("create_account_with_patient", "find_account_by_email", "touch_last_login"):
        original = getattr(db, name)
        monkeypatch.setattr(
            db, name, lambda *a, _o=original, **k: _o(*a, **{**k, "conn": pg_conn})
        )


def _script():
    from app.views import signup

    signup.render(on_success=lambda: None, on_login=lambda: None)


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
    at = AppTest.from_function(_script).run()
    at.text_input(key="signup_name").set_value("Ada")
    at.text_input(key="signup_email").set_value("mismatch@example.com")
    at.text_input(key="signup_password").set_value("hunter2hunter2")
    at.text_input(key="signup_confirm").set_value("different-entirely")
    at.button(key="signup_submit").click().run()
    assert any("do not match" in e.value.lower() for e in at.error)


def test_short_password_is_rejected_with_the_minimum_stated():
    at = AppTest.from_function(_script).run()
    at.text_input(key="signup_name").set_value("Ada")
    at.text_input(key="signup_email").set_value("short@example.com")
    at.text_input(key="signup_password").set_value("abc")
    at.text_input(key="signup_confirm").set_value("abc")
    at.button(key="signup_submit").click().run()
    assert any(str(auth.MIN_PASSWORD_LENGTH) in e.value for e in at.error)


def test_blank_name_is_rejected():
    at = AppTest.from_function(_script).run()
    at.text_input(key="signup_name").set_value("   ")
    at.text_input(key="signup_email").set_value("noname@example.com")
    at.text_input(key="signup_password").set_value("hunter2hunter2")
    at.text_input(key="signup_confirm").set_value("hunter2hunter2")
    at.button(key="signup_submit").click().run()
    assert any("name" in e.value.lower() for e in at.error)


def test_duplicate_email_shows_a_clear_message(pg_conn):
    auth.create_account("taken@example.com", "First", "hunter2hunter2")
    at = AppTest.from_function(_script).run()
    at.text_input(key="signup_name").set_value("Second")
    at.text_input(key="signup_email").set_value("taken@example.com")
    at.text_input(key="signup_password").set_value("hunter2hunter2")
    at.text_input(key="signup_confirm").set_value("hunter2hunter2")
    at.button(key="signup_submit").click().run()
    assert any("already exists" in e.value.lower() for e in at.error)


def test_successful_signup_signs_the_user_in(pg_conn):
    at = AppTest.from_function(_script).run()
    at.text_input(key="signup_name").set_value("Grace Hopper")
    at.text_input(key="signup_email").set_value("grace@example.com")
    at.text_input(key="signup_password").set_value("hunter2hunter2")
    at.text_input(key="signup_confirm").set_value("hunter2hunter2")
    at.button(key="signup_submit").click().run()
    assert not at.error
    assert at.session_state[auth.SESSION_KEY].display_name == "Grace Hopper"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `NEURORX_LOCAL_PG="host=/tmp/nrx_pg port=5439 user=postgres dbname=databricks_postgres" python -m pytest tests/test_view_signup.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.views.signup'`

- [ ] **Step 4: Create `app/views/signup.py`**

```python
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

    if st.button("Create account", key="signup_submit", type="primary", use_container_width=True):
        _submit(name, email, password, confirm, on_success)

    if st.button("I already have an account", key="signup_to_login", use_container_width=True):
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `NEURORX_LOCAL_PG="host=/tmp/nrx_pg port=5439 user=postgres dbname=databricks_postgres" python -m pytest tests/test_view_signup.py -v`
Expected: PASS, 7 passed

- [ ] **Step 6: Commit**

```bash
git add app/views/signup.py tests/test_view_signup.py app/theme.py
git commit -m "feat(app): create-account screen

Validation runs cheapest-first, so a mismatched confirmation never reaches
the ~33ms argon2 hash or the database.

Carries an explicit 'do not reuse a real password' warning — a real
mitigation for a demo database, not a disclaimer."
```

---

### Task 8: Login screen

**Files:**
- Create: `app/views/login.py`, `tests/test_view_login.py`

**Interfaces:**
- Consumes: `auth.authenticate`, `auth.sign_in` (Task 5).
- Produces: `login.render(on_success: Callable[[], None], on_signup: Callable[[], None]) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_view_login.py`:

```python
import pytest
from streamlit.testing.v1 import AppTest

from app import auth, db


@pytest.fixture(autouse=True)
def _route_db_through_the_test_connection(pg_conn, monkeypatch):
    for name in ("create_account_with_patient", "find_account_by_email", "touch_last_login"):
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
    at = _attempt(AppTest.from_function(_script).run(), "ok@example.com", "hunter2hunter2")
    assert not at.error
    assert at.session_state[auth.SESSION_KEY].display_name == "OK Person"


def test_wrong_password_is_rejected(pg_conn):
    auth.create_account("wp@example.com", "WP", "hunter2hunter2")
    at = _attempt(AppTest.from_function(_script).run(), "wp@example.com", "nope-nope-nope")
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NEURORX_LOCAL_PG="host=/tmp/nrx_pg port=5439 user=postgres dbname=databricks_postgres" python -m pytest tests/test_view_login.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.views.login'`

- [ ] **Step 3: Create `app/views/login.py`**

```python
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

    if st.button("Sign in", key="login_submit", type="primary", use_container_width=True):
        _submit(email, password, on_success)

    if st.button("Create an account", key="login_to_signup", use_container_width=True):
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NEURORX_LOCAL_PG="host=/tmp/nrx_pg port=5439 user=postgres dbname=databricks_postgres" python -m pytest tests/test_view_login.py -v`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add app/views/login.py tests/test_view_login.py
git commit -m "feat(app): sign-in screen

One generic failure message for every rejection, asserted by a test that
compares the wrong-password and unknown-email cases character for character
— otherwise the form becomes a way to discover which emails have accounts."
```

---

### Task 9: Router

**Files:**
- Modify: `app/app.py`
- Create: `tests/test_router.py`

**Interfaces:**
- Consumes: every view's `render()` (Tasks 6, 7, 8); `auth.current_account`, `auth.sign_out` (Task 5).
- Produces: the running application. `app.py` exposes `_render_app_page()` for the signed-in shell.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_router.py`:

```python
"""The gate is structural: while signed out, the app page is not registered,
so there is nothing for that URL to route to.
"""
from streamlit.testing.v1 import AppTest

APP = "app/app.py"


def test_signed_out_lands_on_the_home_page():
    at = AppTest.from_file(APP).run()
    assert not at.exception
    body = " ".join(m.value for m in at.markdown).lower()
    assert "create account" in body or any(
        b.label == "Create account" for b in at.button
    )


def test_signed_out_does_not_render_the_app_tabs():
    at = AppTest.from_file(APP).run()
    assert not at.tabs


def test_signed_out_never_shows_the_safety_ticker():
    """The ticker belongs to the app shell; seeing it signed out would mean
    the gated page rendered."""
    at = AppTest.from_file(APP).run()
    body = " ".join(m.value for m in at.markdown)
    assert "Organizational assistant" not in body
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `NEURORX_LOCAL_PG="host=/tmp/nrx_pg port=5439 user=postgres dbname=databricks_postgres" python -m pytest tests/test_router.py -v`
Expected: FAIL — the current `app.py` renders the tabs unconditionally, so `test_signed_out_does_not_render_the_app_tabs` fails.

- [ ] **Step 3: Rewrite `app/app.py` as the router**

Keep the existing `sys.path` bootstrap block at the top exactly as it is — it is load-bearing for both launch paths. Replace everything from `import streamlit as st` onward:

```python
import streamlit as st

from app import auth, theme
from app.views import chat as chat_view
from app.views import dashboard as dashboard_view
from app.views import home as home_view
from app.views import login as login_view
from app.views import signup as signup_view
from app.views import today as today_view

st.set_page_config(
    page_title="NeuroRx AI",
    page_icon="💊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

theme.inject()

MARGARET_DEMO_PATIENT_ID = "12345678-1234-1234-1234-123456789012"


def _go(url_path: str):
    """Navigate to a page by url_path, then rerun."""
    st.session_state["_nav"] = url_path
    st.rerun()


def _render_home() -> None:
    home_view.render(
        on_signup=lambda: _go("signup"),
        on_login=lambda: _go("login"),
    )


def _render_signup() -> None:
    signup_view.render(
        on_success=lambda: _go("app"),
        on_login=lambda: _go("login"),
    )


def _render_login() -> None:
    login_view.render(
        on_success=lambda: _go("app"),
        on_signup=lambda: _go("signup"),
    )


def _render_app_page() -> None:
    """The signed-in shell: header, safety ticker, three tabs.

    The patient selector is a DEMO SWITCHER, not scoping. It defaults to the
    signed-in account's own patient, but any patient_id typed here is honoured
    — auth is deliberately not an access boundary (see app/auth.py).
    """
    account = auth.current_account()

    if "patient_id" not in st.session_state:
        st.session_state.patient_id = account.patient_id

    col_brand, col_patient, col_out = st.columns([4, 1, 1], vertical_alignment="center")

    with col_brand:
        st.markdown(theme.brand(), unsafe_allow_html=True)

    with col_patient:
        with st.popover(
            f"PATIENT  {st.session_state.patient_id[:8]}", use_container_width=True
        ):
            st.markdown(
                theme.eyebrow(f"SIGNED IN AS {account.display_name.upper()}"),
                unsafe_allow_html=True,
            )
            st.session_state.patient_id = st.text_input(
                "Patient ID",
                value=st.session_state.patient_id,
                help=(
                    "Demo switcher — not access control. Margaret Demo is "
                    f"{MARGARET_DEMO_PATIENT_ID[:8]}..."
                ),
            )
            st.caption("💊 All data is synthetic and for demo only.")

    with col_out:
        if st.button("Sign out", use_container_width=True):
            auth.sign_out()
            st.session_state.pop("patient_id", None)
            _go("home")

    theme.safety_ticker()

    tab_chat, tab_today, tab_dashboard = st.tabs(["Chat", "Today", "Dashboard"])
    with tab_chat:
        chat_view.render(patient_id=st.session_state.patient_id)
    with tab_today:
        today_view.render(patient_id=st.session_state.patient_id)
    with tab_dashboard:
        dashboard_view.render(patient_id=st.session_state.patient_id)


# ---------------------------------------------------------------------------
# Routing
#
# The signed-out page set does NOT contain the app page, so while signed out
# there is nothing for that URL to route to. The gate is structural rather
# than a render-and-return-early check that a future edit could skip.
#
# position="hidden" suppresses Streamlit's own navigation widget, leaving the
# restyled pill tabs as the only navigation.
# ---------------------------------------------------------------------------
if auth.current_account() is None:
    pages = [
        st.Page(_render_home, title="Home", url_path="home", default=True),
        st.Page(_render_login, title="Sign in", url_path="login"),
        st.Page(_render_signup, title="Create account", url_path="signup"),
    ]
else:
    pages = [st.Page(_render_app_page, title="NeuroRx AI", url_path="app", default=True)]

st.navigation(pages, position="hidden").run()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `NEURORX_LOCAL_PG="host=/tmp/nrx_pg port=5439 user=postgres dbname=databricks_postgres" python -m pytest tests/test_router.py -v`
Expected: PASS, 3 passed

- [ ] **Step 5: Run the whole suite**

Run: `NEURORX_LOCAL_PG="host=/tmp/nrx_pg port=5439 user=postgres dbname=databricks_postgres" python -m pytest tests/ -v`
Expected: PASS, 48 passed

- [ ] **Step 6: Commit**

```bash
git add app/app.py tests/test_router.py
git commit -m "feat(app): route home/login/signup vs the signed-in app

app.py becomes a router registering a different page set per auth state.
The signed-out set does not contain the app page, so the gate is structural
— there is nothing for that URL to route to — rather than a render-and-
return-early check a future edit could skip.

position=hidden suppresses Streamlit's own nav widget, so the restyled pill
tabs remain the only navigation. The patient switcher stays and is labelled
a demo switcher, not scoping."
```

---

### Task 10: End-to-end verification in a real browser, and docs

**Files:**
- Modify: `docs/local_dev.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: everything.
- Produces: no code. A verified running app and updated runbooks.

**Why a browser step:** every earlier task tested a unit. CLAUDE.md's own record shows this project's real defects — the `sys.path` shadowing bug, the vanishing chat notice — surfaced only when the app actually ran.

- [ ] **Step 1: Install the new dependency and start the app**

```bash
pip install -r requirements.txt
psql -h /tmp/nrx_pg -p 5439 -U postgres -d databricks_postgres -v ON_ERROR_STOP=1 -f "lakebase/schema.sql"
NEURORX_LOCAL_PG="host=/tmp/nrx_pg port=5439 user=postgres dbname=databricks_postgres" streamlit run app/app.py
```

- [ ] **Step 2: Walk the full flow in a browser**

Confirm each, and fix anything that fails before continuing:

1. `/` shows the home page. No safety ticker, no tabs.
2. Navigating directly to `/app` while signed out does **not** render the tabs.
3. "Create account" → the signup form; the password warning is visible.
4. Submitting a mismatched confirmation shows an error and creates nothing.
5. A valid signup lands in the app, with the display name in the patient popover.
6. Today and Dashboard render **empty states** — a new account has no schedules. This is correct, not a bug.
7. Typing Margaret's UUID into the switcher shows her populated data.
8. "Sign out" returns to the home page; the tabs are gone.
9. Signing back in with the same email and password works; a wrong password shows the generic message.

- [ ] **Step 3: Verify the rows landed**

```bash
psql -h /tmp/nrx_pg -p 5439 -U postgres -d databricks_postgres -c \
  "SELECT a.email, a.display_name, a.last_login_at IS NOT NULL AS logged_in, p.display_name AS patient
     FROM accounts a JOIN patients p USING (patient_id) ORDER BY a.created_at DESC LIMIT 5;"
```

Expected: the new account, with `logged_in = t` after step 9, joined to its own patient row.

- [ ] **Step 4: Confirm no plaintext password was stored**

```bash
psql -h /tmp/nrx_pg -p 5439 -U postgres -d databricks_postgres -c \
  "SELECT count(*) AS bad FROM accounts WHERE password_hash NOT LIKE '\$argon2id\$%';"
```

Expected: `bad = 0`

- [ ] **Step 5: Update `docs/local_dev.md`**

Add after the "Run the app" section:

```markdown
## 5. Accounts

The app now opens on a public home page. Create an account (name, email,
password) and you are signed in and dropped into the three tabs.

**A new account starts empty** — signup creates a patient with no schedules, so
Today and Dashboard show their empty states. That is correct: no fabricated
medication data is seeded. To see the populated demo cohort, open the patient
switcher in the header and paste Margaret's ID:

    12345678-1234-1234-1234-123456789012

**The switcher is not access control.** Any signed-in account can view any
patient. Signing in identifies you; it does not restrict what you can read.

**Refreshing the browser signs you out** — session state is per-session and
Streamlit has no cookie-write API.

## Running the tests

    NEURORX_LOCAL_PG="host=/tmp/nrx_pg port=5439 user=postgres dbname=databricks_postgres" \
      python -m pytest tests/ -v

Tests skip rather than fail when `NEURORX_LOCAL_PG` is unset.
```

- [ ] **Step 6: Update `CLAUDE.md`**

Add to the Phase 3 section:

```markdown
### Accounts and sign-in — home page, signup, login

`app/auth.py` (the identity seam) + `app/views/{home,signup,login}.py` + an
`accounts` table (`DATA_CONTRACTS.md` §6.5). `app/app.py` is now a router:
`st.navigation(position="hidden")` registering a different page set per auth
state, so while signed out the app page is not registered and there is nothing
for that URL to route to — a structural gate, not a return-early check.

**This is deliberately NOT an access boundary.** The demo patient switcher is
kept by choice, so any signed-in account can still view any patient's data.
Said plainly in `auth.py`'s docstring so the login screen is never mistaken for
protection.

**argon2id via `argon2-cffi==25.1.0`**, verified to actually run on this
project's Python 3.14 before being pinned: the top-level package ships only a
pure-Python wheel and alone looks like a 3.14 incompatibility — the C extension
lives in `argon2-cffi-bindings`, which does publish cp314 wheels.

**Email normalization is a CHECK constraint**, not only a Python call. One
forgotten `lower()` would create `Bob@x.com` beside `bob@x.com` and
`UNIQUE (email)` would never notice.

**`authenticate()` returns the same `None`** for unknown-email and
wrong-password, and the login screen shows one generic message — asserted by a
test that compares both cases character for character, so the form cannot be
used to discover which emails have accounts.

**First real test infrastructure in this project**: `pytest==9.1.1`, with a
session fixture that applies `lakebase/schema.sql` and a per-test connection
that rolls back. Tests skip, never fail, when `NEURORX_LOCAL_PG` is unset.

**Known gaps, documented not hidden**: refreshing the browser signs you out
(`st.session_state` is per-session; Streamlit has no cookie-write API); a new
account starts empty (no fabricated medication data is seeded); no password
reset; no login rate limiting.
```

- [ ] **Step 7: Commit**

```bash
git add docs/local_dev.md CLAUDE.md
git commit -m "docs: accounts, sign-in, and how to run the tests

Records what this deliberately is not: with the demo switcher kept, auth
identifies you but does not restrict what you can read."
```

---

## Self-Review

**Spec coverage**

| Spec section | Task |
|---|---|
| §3 Routing (page sets, `position="hidden"`, structural gate) | 9 |
| §4 Home | 6 |
| §4 Signup | 7 |
| §4 Login | 8 |
| §4 App page + display name + sign out | 9 |
| §5 Data model, contract-first, all constraints | 2 |
| §6 `auth.py` seam, argon2id, no-enumeration, session shape | 3, 5 |
| §7.1 Not an access boundary (documented) | 5 (docstring), 9 (switcher label), 10 (docs) |
| §7.2 Refresh signs you out (documented) | 5 (docstring), 10 (docs) |
| §7.3 New account empty, no seeded data | 10 (browser step 6, docs) |
| §7.4–7.5 No reset / rate limiting / seeded Margaret | 10 (docs) |
| §8 Verification (schema idempotent, constraints, transaction, argon2, no-enumeration, gate, browser flow) | 1, 2, 3, 4, 5, 9, 10 |

No spec requirement is unassigned.

**Placeholder scan:** none. Every code step carries complete code; every command states its expected output.

**Type consistency:** `Account(account_id, email, display_name, patient_id)` is defined in Task 5 and used unchanged in Tasks 7, 8, 9. `db.create_account_with_patient` / `find_account_by_email` / `touch_last_login` keep identical signatures across Tasks 4, 5, 7, 8. `SESSION_KEY` is defined in Task 5 and referenced in Tasks 7 and 8. Every view's `render()` signature in Tasks 6–8 matches the call in Task 9. Widget `key=` values used by tests in Tasks 7–8 match those in the implementations.

One deliberate deviation from the spec, noted here rather than silently: the spec's `create_account(...)` "raises EmailTaken, WeakPassword" — Task 4's `db` layer raises psycopg's `UniqueViolation`, which Task 5 translates into `EmailTaken` so views handle one vocabulary. That is the spec's intent, made explicit about where the translation happens.
