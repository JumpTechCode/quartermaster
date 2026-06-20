"""The composition root assembles a working app; /healthz answers without a DB."""

from __future__ import annotations

import httpx
import pytest


async def test_build_app_serves_healthz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QM_DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/qm")
    # Imported here, not at module level: build_app() constructs Settings() at
    # call time, and Settings() reads QM_DATABASE_URL on construction, so the
    # env var must already be set before this import/call chain runs.
    from quartermaster.app import build_app

    app = build_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
