from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import keyring
import keyring.core
import keyring.errors
import pytest
from keyring.backend import KeyringBackend

from night_scout.core.db import Store
from night_scout.core.http import PoliteClient

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


Route = Callable[[httpx.Request], httpx.Response | None]


def make_client(routes: dict[str, str | int | Route]) -> tuple[PoliteClient, list[str]]:
    """A PoliteClient whose requests are answered from fixtures by URL substring.

    A route value is a fixture filename, an HTTP status code, or a callable.
    Unmatched requests fail the test loudly (no network in tests).
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        host = request.headers.get("host", "")
        if host and host != request.url.netloc.decode():  # get_public pins the IP; route by Host
            query = f"?{request.url.query.decode()}" if request.url.query else ""
            url = f"{request.url.scheme}://{host}{request.url.path}{query}"
        seen.append(url)
        for needle, answer in routes.items():
            if needle in url:
                if isinstance(answer, int):
                    return httpx.Response(answer, text="nope")
                if callable(answer):
                    resp = answer(request)
                    if resp is not None:
                        return resp
                    continue
                return httpx.Response(200, content=fixture_bytes(answer))
        raise AssertionError(f"unexpected request in test: {url}")

    client = PoliteClient(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    # Every fixture host resolves to a public address, so get_public lets it through.
    client._resolve = lambda host, port, *a, **k: [
        (None, None, None, None, ("93.184.215.14", port))
    ]
    return client, seen


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "scout.db")
    yield s
    s.close()


class MemoryKeyring(KeyringBackend):
    """In-memory keyring so tests never touch the real OS keychain."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise keyring.errors.PasswordDeleteError("not found")
        del self.store[(service, username)]


@pytest.fixture(autouse=True)
def memory_keyring() -> MemoryKeyring:
    previous = keyring.core._keyring_backend
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    yield kr
    if previous is not None:
        keyring.set_keyring(previous)


FAKE_TTS = Path(__file__).parent / "fake_tts"


@pytest.fixture(autouse=True)
def no_voice_addon(monkeypatch: pytest.MonkeyPatch) -> None:
    """By default the voice add-on is 'not installed' and nobody asks a real Python (create_app
    checks in the background). Tests that want it use the fake_voice fixture."""
    from night_scout.core import listen

    monkeypatch.setenv("NIGHT_SCOUT_TTS_PYTHON", "/nonexistent/tts-python")
    monkeypatch.setattr(listen, "_avail", {"/nonexistent/tts-python": (False, "not in tests")})


@pytest.fixture
def fake_voice(monkeypatch: pytest.MonkeyPatch) -> Path:
    """The voice add-on 'installed': this Python, with fake kokoro/soundfile/numpy modules
    (tests/fake_tts) on its path, so the real worker script runs with no model."""
    import sys

    from night_scout.core import listen

    monkeypatch.setenv("NIGHT_SCOUT_TTS_PYTHON", sys.executable)
    monkeypatch.setenv("PYTHONPATH", str(FAKE_TTS))
    monkeypatch.setattr(listen, "_avail", {})
    return FAKE_TTS
