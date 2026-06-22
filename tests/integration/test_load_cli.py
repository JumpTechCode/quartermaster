"""The CLI runs a small sweep against a real Postgres and writes JSON."""

from __future__ import annotations

import json
from pathlib import Path

from loadtest.__main__ import main


async def test_cli_runs_and_writes_json(postgres_url: str, tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    code = await main(
        [
            "--database-url",
            postgres_url,
            "--seed",
            "5",
            "--skus",
            "2",
            "--orders",
            "16",
            "--on-hand",
            "4",
            "--qty",
            "1",
            "--concurrency",
            "8",
            "--out",
            str(out),
        ]
    )
    assert code == 0  # guarded + read_cas clean; naive red is allowed
    payload = json.loads(out.read_text())
    names = {row["strategy"] for row in payload}
    assert names == {"naive", "read_cas", "guarded"}
    naive = next(row for row in payload if row["strategy"] == "naive")
    assert naive["oversell"] > 0
