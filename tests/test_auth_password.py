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
    assert auth.hash_password("same password") != auth.hash_password("same password")


def test_short_password_is_rejected():
    with pytest.raises(auth.WeakPassword):
        auth.hash_password("a" * (auth.MIN_PASSWORD_LENGTH - 1))


def test_minimum_length_password_is_accepted():
    assert auth.hash_password("a" * auth.MIN_PASSWORD_LENGTH)
