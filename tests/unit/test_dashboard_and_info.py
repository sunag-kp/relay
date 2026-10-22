from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr

from relay.config import Settings
from tests.conftest import HarnessFactory


async def test_info_hides_the_demo_key_unless_asked(make_harness: HarnessFactory) -> None:
    hidden = await make_harness(
        settings=Settings(app_env="test", demo_api_key=SecretStr("rk_demo"))
    )
    body = (await hidden.client.get("/info")).json()
    assert body["demo_api_key"] is None
    assert body["models"] == ["test-model"]

    shown = await make_harness(
        settings=Settings(app_env="test", demo_api_key=SecretStr("rk_demo"), demo_show_key=True)
    )
    assert (await shown.client.get("/info")).json()["demo_api_key"] == "rk_demo"


async def test_dashboard_is_served_when_built(make_harness: HarnessFactory, tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>Relay Dashboard</title>")
    harness = await make_harness(settings=Settings(app_env="test", ui_dist_dir=tmp_path))
    root = await harness.client.get("/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/dashboard/"
    page = await harness.client.get("/dashboard/")
    assert page.status_code == 200
    assert "Relay Dashboard" in page.text


async def test_root_without_a_dashboard_points_to_the_docs(make_harness: HarnessFactory) -> None:
    harness = await make_harness(
        settings=Settings(app_env="test", ui_dist_dir=Path("/nonexistent"))
    )
    assert (await harness.client.get("/")).json()["docs"] == "/docs"
