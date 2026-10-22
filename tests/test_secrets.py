from __future__ import annotations

import keyring
import pytest
from keyring.backends import fail, null

from night_scout.core import secrets
from night_scout.core.secrets import SecretStoreUnavailable


def test_set_get_delete_roundtrip(memory_keyring) -> None:
    secrets.set_key("openrouter", "  sk-or-v1-1234567890abcdef  ")
    assert memory_keyring.store[("night-scout", "openrouter")] == "sk-or-v1-1234567890abcdef"
    assert secrets.get_key("openrouter") == "sk-or-v1-1234567890abcdef"
    assert secrets.key_source("openrouter") == "keychain"
    assert secrets.delete_key("openrouter") is True
    assert secrets.get_key("openrouter") is None
    assert secrets.delete_key("openrouter") is False
    assert secrets.key_source("openrouter") is None


def test_env_var_wins(monkeypatch) -> None:
    secrets.set_key("openrouter", "keychain-value-123456")
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-value-abcdefgh")
    assert secrets.get_key("openrouter", "OPENROUTER_API_KEY") == "env-value-abcdefgh"
    assert secrets.key_source("openrouter", "OPENROUTER_API_KEY") == "env"
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    assert secrets.get_key("openrouter", "OPENROUTER_API_KEY") == "keychain-value-123456"


def test_rejects_empty_or_odd_keys() -> None:
    with pytest.raises(ValueError):
        secrets.set_key("x", "   ")
    with pytest.raises(ValueError):
        secrets.set_key("x", "abc\ndef")


def test_masked() -> None:
    assert secrets.masked("sk-or-v1-abcdefghijwxyz") == "sk-o••••••••wxyz"
    assert secrets.masked("12345678") == "••••••••"
    assert secrets.masked("abc") == "••••••••"
    assert secrets.masked("") == ""
    assert secrets.masked(None) == ""


def test_scrub() -> None:
    key = "sk-or-v1-abcdefghijwxyz"
    assert key not in secrets.scrub(f"bad key {key} sent", key)
    assert secrets.scrub("nothing here", None) == "nothing here"


@pytest.mark.parametrize("backend", [fail.Keyring(), null.Keyring()])
def test_unusable_backend_raises_and_never_falls_back(backend, monkeypatch) -> None:
    keyring.set_keyring(backend)
    assert secrets.keychain_available() is False
    with pytest.raises(SecretStoreUnavailable, match="keychain"):
        secrets.set_key("openrouter", "sk-or-v1-abcdefghijwxyz")
    with pytest.raises(SecretStoreUnavailable):
        secrets.get_key("openrouter")
    with pytest.raises(SecretStoreUnavailable):
        secrets.delete_key("openrouter")
    # An env var still works without a keychain.
    monkeypatch.setenv("OR_KEY", "env-only-value-1234")
    assert secrets.get_key("openrouter", "OR_KEY") == "env-only-value-1234"


def test_backend_errors_become_unavailable_without_echoing_the_key(memory_keyring) -> None:
    key = "sk-or-v1-abcdefghijwxyz"

    def boom(*a):
        raise RuntimeError(f"locked, could not store {key}")

    memory_keyring.set_password = boom  # type: ignore[method-assign]
    memory_keyring.get_password = boom  # type: ignore[method-assign]
    with pytest.raises(SecretStoreUnavailable) as ei:
        secrets.set_key("openrouter", key)
    assert key not in str(ei.value) and ei.value.__cause__ is None
    with pytest.raises(SecretStoreUnavailable):
        secrets.get_key("openrouter")


def test_plaintext_backends_refused(monkeypatch) -> None:
    class PlaintextKeyring(keyring.backend.KeyringBackend):
        priority = 1  # type: ignore[assignment]

        def get_password(self, s, u):
            return None

        def set_password(self, s, u, p):
            raise AssertionError("must not store")

        def delete_password(self, s, u):
            pass

    keyring.set_keyring(PlaintextKeyring())
    with pytest.raises(SecretStoreUnavailable):
        secrets.set_key("x", "abcdefghijkl")
