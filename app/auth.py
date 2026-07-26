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
