# NeuroRx AI — home page, accounts, and sign-in

**Date:** 2026-07-26
**Status:** approved (design), pending implementation plan
**Depends on:** [`2026-07-26-neurorx-ui-restyle-design.md`](2026-07-26-neurorx-ui-restyle-design.md)
(the design layer this work reuses)

---

## 1. Goal

Give the app a public home page and real accounts: sign up with a name, email, and
password; sign in; then use the existing three features as that account.

Today there is no identity at all — the app opens straight onto Margaret Demo's data
and any patient's UUID can be typed into a header control to read their medication
history.

## 2. Decisions taken

| Question | Decision |
|---|---|
| Purpose | Demo now, real later — build the UI, keep identity behind one swappable seam |
| Credentials | Email + password, hashed with argon2id |
| Login identifier | Email only. The name is collected at signup and displayed, never used to sign in |
| Account model | Account = patient, 1:1 |
| Patient switcher | Kept as a demo switcher — so auth is **not** an access boundary |
| Routing | Multipage via `st.navigation`, real `/login` and `/signup` URLs |

### Approaches considered and rejected

- **Single gate in `app.py`, no routing.** Smallest diff, but no real URLs and no home
  page route. Rejected in favour of the multipage shape once a home page was in scope.
- **Per-view guards.** Three places to get wrong instead of one, and no single seam to
  swap for OIDC later — the opposite of what "demo now, real later" asks for.
- **No-password sign-in.** Recommended initially as it stores no credentials at all;
  the user chose passwords, so they are implemented properly rather than half-way.
- **Name as the login identifier.** What the user first asked for, but names collide.
  Offered a UNIQUE-name variant; the user chose email, which removes the collision
  problem entirely.

## 3. Routing

`app/app.py` becomes a thin router that registers **different page sets depending on
auth state**:

```python
signed out →  st.navigation([home, login, signup], position="hidden")   # home is default
signed in  →  st.navigation([app_page, logout],    position="hidden")   # app_page is default, url_path="app"
```

`position="hidden"` suppresses Streamlit's own nav widget, so the restyled pill tabs
remain the only navigation — verified against `st.navigation`'s signature in
Streamlit 1.59.2, along with `st.Page(url_path=..., default=..., visibility=...)` and
`st.switch_page(page, query_params=...)`.

**The gate is structural, not a redirect.** While signed out, `/app` is not in the
registered page set, so there is nothing for that URL to route to. This is why the
page-set switch is preferred over rendering the main page and returning early.

`app.yaml`'s `streamlit run app.py` still works — `app/app.py` remains the entry point.

## 4. Screens

| Route | Visible when | Content |
|---|---|---|
| `/` **Home** | signed out (default) | The mockup's landing page — hero wordmark, tagline, trust cards, "Create account" / "Sign in". Explicitly back **in** scope; the restyle spec had excluded it. |
| `/signup` | signed out | Name, email, password, confirm. Carries the "don't reuse a real password" warning. |
| `/login` | signed out | Email, password. One generic failure message. |
| `/app` | signed in (default) | Existing header + safety ticker + three tabs, unchanged. Header gains the display name and a Sign out control. |

## 5. Data model

New `accounts` table in Lakebase. `DATA_CONTRACTS.md` is frozen, so it is updated
**first**, then the code (CLAUDE.md §6).

```sql
CREATE TABLE IF NOT EXISTS accounts (
    account_id     UUID        NOT NULL DEFAULT gen_random_uuid(),
    email          TEXT        NOT NULL,
    display_name   TEXT        NOT NULL,
    password_hash  TEXT        NOT NULL,   -- argon2id encoded string, ~97 chars
    patient_id     UUID        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at  TIMESTAMPTZ,

    CONSTRAINT accounts_pkey             PRIMARY KEY (account_id),
    CONSTRAINT accounts_email_unique     UNIQUE (email),
    CONSTRAINT accounts_patient_unique   UNIQUE (patient_id),
    CONSTRAINT accounts_patient_fk       FOREIGN KEY (patient_id)
                                         REFERENCES patients(patient_id) ON DELETE CASCADE,
    CONSTRAINT accounts_email_normalized CHECK (email = lower(btrim(email))),
    CONSTRAINT accounts_email_shape      CHECK (position('@' in email) > 1),
    CONSTRAINT accounts_name_present     CHECK (length(trim(display_name)) > 0)
);
```

Two choices worth stating:

- **`accounts_email_normalized` puts normalization in the database.** Lowercasing only
  in Python means one forgotten call creates `Bob@x.com` alongside `bob@x.com`, and
  `UNIQUE (email)` never notices. The CHECK makes that row impossible to insert.
- **`UNIQUE (patient_id)` + `ON DELETE CASCADE`** encode the 1:1 account↔patient model.
  Signup creates the `patients` row and the `accounts` row in **one transaction**, so a
  failure cannot leave an orphan patient.

`accounts_email_shape` is a sanity check (`@` present, not leading), not validation —
correct email validation is not a CHECK constraint's job.

## 6. `app/auth.py` — the seam

Pure functions, no Streamlit widgets, so the module stays swappable for OIDC later:

```python
create_account(email, display_name, password) -> Account   # raises EmailTaken, WeakPassword
authenticate(email, password) -> Account | None            # None on bad email OR bad password
current_account() -> Account | None                        # reads st.session_state
sign_in(account) -> None
sign_out() -> None
```

**Password handling**

- `argon2-cffi==25.1.0`, `PasswordHasher()` defaults (argon2id, m=64 MB, t=3, p=4).
  Both the library and these parameters were verified to run on this project's
  Python 3.14: hash + verify round-trip passes, wrong password rejected, ~33 ms.
- The C extension lives in `argon2-cffi-bindings`, which does publish cp314 and abi3
  wheels — checked, because the parent package's own single wheel is pure-Python and
  would otherwise look like a 3.14 incompatibility.
- Only the hash is stored. The plaintext is never written to the database, never
  logged, and never placed in `st.session_state`.
- `authenticate()` returns the same `None` for unknown-email and wrong-password, so the
  login form cannot be used to enumerate which emails have accounts.
- A **minimum of 8 characters** is enforced at signup, raising `WeakPassword`. Length
  is the only rule: composition rules (a digit, a symbol, mixed case) push people toward
  predictable substitutions and are not applied here.

**Session shape:** `st.session_state["account"]` holds the `Account` dataclass —
`account_id`, `email`, `display_name`, `patient_id`. No hash, no password.

## 7. Known limitations — documented, not papered over

1. **Auth is not an access boundary.** The demo switcher is deliberately kept, so any
   signed-in user can still view any patient's data. Stated in `auth.py`'s own
   docstring so nobody later mistakes the login screen for protection.
2. **Refreshing the browser signs you out.** `st.session_state` is per-session and
   Streamlit exposes no first-party cookie-*write* API (`st.context.cookies` is
   read-only). Accepted for a demo; OIDC handles this properly when the seam is swapped.
3. **A new account starts empty.** Signup creates a patient with no schedules, so Today
   and Dashboard render their empty states. The switcher is how Margaret's populated
   cohort is reached. Fabricated medication data is deliberately **not** seeded into new
   accounts — inventing clinical-looking data is exactly what this project's spine
   forbids.
4. **No password reset**, and **no login rate limiting.** Both need infrastructure
   beyond this scope; flagged rather than half-built.
5. **No seeded Margaret account.** That would require a password committed to the repo.
   The kept switcher makes it unnecessary.

## 8. Verification

All of it runs locally against real Postgres, per CLAUDE.md §6 ("if a module has no
Spark/Databricks dependency, actually run it"):

- `lakebase/schema.sql` applies twice with zero errors (true idempotency).
- Each constraint actually rejects its bad row: duplicate email, non-normalized email
  (`Bob@x.com`), missing `@`, blank display name, second account on one `patient_id`.
- Signup is one transaction — an induced failure after the `patients` insert leaves
  **no** orphan patient row.
- argon2 hash/verify round-trip; wrong password rejected; `check_needs_rehash` false.
- `authenticate()` returns `None` identically for unknown-email and wrong-password.
- `/app` cannot render while signed out.
- Full flow driven in a real browser: home → create account → lands in the app with the
  display name in the header → sign out → sign in again.

## 9. Out of scope

- Password reset, email verification, rate limiting, account deletion.
- Caregiver accounts and invites (the account model chosen is 1:1 patient).
- Real per-patient authorization (see limitation 1).
- Replacing the demo switcher.
