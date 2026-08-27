from __future__ import annotations

import socket

import pytest


@pytest.fixture(autouse=True)
def block_live_network(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("Live network access is forbidden in the test suite")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr("postdesk.http.urlopen", blocked)
