"""API keys for model providers, kept in the OS keychain through `keyring`.

Rules: a key never goes to config.yaml, the database, logs, exception messages or the
Try-it sandbox. If there's no usable keychain we refuse (SecretStoreUnavailable) rather
than fall back to a plaintext file. A provider's `api_key_env` variable, when set,
wins over the keychain.
"""

from __future__ import annotations

import os
from typing import Literal

import keyring
import keyring.errors
from keyring.backend import KeyringBackend

SERVICE = "night-scout"
BULLETS = "••••••••"

KeySource = Literal["env", "keychain"]


class SecretStoreUnavailable(RuntimeError):
    """No secure OS keychain is available to store API keys."""


_HELP = (
    "No secure OS keychain is available, so Night Scout can't store API keys. "
    "On macOS this is the login Keychain; on Linux install and unlock a Secret Service "
    "keyring (GNOME Keyring or KWallet). Or set the provider's API key environment variable."
)


def _unsafe(backend: KeyringBackend) -> bool:
    mod = type(backend).__module__
    name = type(backend).__name__
    if mod in ("keyring.backends.fail", "keyring.backends.null"):
        return True
    # keyrings.alt ships plaintext / weakly encrypted file stores: never use those.
    return mod.startswith("keyrings.alt") or "plaintext" in name.lower()


def _backend() -> KeyringBackend:
    try:
        kr = keyring.get_keyring()
    except Exception as e:  # backend discovery itself can blow up on odd systems
        raise SecretStoreUnavailable(f"{_HELP} ({type(e).__name__})") from None
    if type(kr).__name__ == "ChainerBackend":
        members = list(getattr(kr, "backends", None) or [])
        if not members or any(_unsafe(b) for b in members):
            raise SecretStoreUnavailable(_HELP)
        return kr
    if _unsafe(kr):
        raise SecretStoreUnavailable(_HELP)
    return kr


def keychain_available() -> bool:
    try:
        _backend()
    except SecretStoreUnavailable:
        return False
    return True


def _check_id(provider_id: str) -> None:
    if not provider_id or len(provider_id) > 64 or not provider_id.isprintable():
        raise ValueError("invalid provider id")


def set_key(provider_id: str, key: str) -> None:
    _check_id(provider_id)
    key = key.strip()
    if not key:
        raise ValueError("API key is empty")
    if not key.isprintable() or len(key) > 4096:
        raise ValueError("API key contains invalid characters or is too long")
    kr = _backend()
    try:
        kr.set_password(SERVICE, provider_id, key)
    except Exception as e:  # never echo the backend's message: it could include the key
        raise SecretStoreUnavailable(
            f"Couldn't save the key in the OS keychain ({type(e).__name__})."
        ) from None


def _keychain_key(provider_id: str) -> str | None:
    kr = _backend()
    try:
        value = kr.get_password(SERVICE, provider_id)
    except Exception as e:
        raise SecretStoreUnavailable(
            f"Couldn't read the OS keychain ({type(e).__name__}). Is it locked?"
        ) from None
    return value or None


def get_key(provider_id: str, env_var: str | None = None) -> str | None:
    """The key for a provider: `env_var` if set and non-empty, else the keychain entry.
    Raises SecretStoreUnavailable only when the keychain would have to be consulted."""
    _check_id(provider_id)
    if env_var:
        value = os.environ.get(env_var, "").strip()
        if value:
            return value
    return _keychain_key(provider_id)


def key_source(provider_id: str, env_var: str | None = None) -> KeySource | None:
    """Where get_key would find the key, without returning it."""
    if env_var and os.environ.get(env_var, "").strip():
        return "env"
    try:
        return "keychain" if _keychain_key(provider_id) else None
    except SecretStoreUnavailable:
        return None


def delete_key(provider_id: str) -> bool:
    """Remove the keychain entry. True if one existed. (An env var is left alone.)"""
    _check_id(provider_id)
    kr = _backend()
    try:
        kr.delete_password(SERVICE, provider_id)
    except keyring.errors.PasswordDeleteError:
        return False
    except Exception as e:
        raise SecretStoreUnavailable(
            f"Couldn't delete the key from the OS keychain ({type(e).__name__})."
        ) from None
    return True


class Secret:
    """Holds an API key; prints as bullets."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def get(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<secret>"

    __str__ = __repr__


def masked(key: str | None) -> str:
    """first4••••••••last4 for display; short keys are all bullets."""
    if not key:
        return ""
    if len(key) <= 8:
        return BULLETS
    return f"{key[:4]}{BULLETS}{key[-4:]}"


def scrub(text: str, *secrets: str | None) -> str:
    """Replace every occurrence of each secret (len >= 4) with bullets."""
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, BULLETS)
    return text
