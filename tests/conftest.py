"""Shared pytest fixtures: project import path, isolated config, state reset,
and a TestClient bound to an allowed Host."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    """Isolate config.json and clear process-global state between tests."""
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "config.json")
    original_config = dict(server.CONFIG)
    server.ALBUMS.clear()
    server.CACHE.clear()
    server.SCAN.update(
        {"state": "idle", "scanned": 0, "total": 0, "error": None, "message": ""}
    )
    yield
    server.CONFIG.clear()
    server.CONFIG.update(original_config)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    # base_url host must be in server.ALLOWED_HOSTS (TrustedHostMiddleware).
    with TestClient(server.app, base_url="http://localhost") as c:
        yield c
