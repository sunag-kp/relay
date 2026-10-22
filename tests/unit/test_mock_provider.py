from __future__ import annotations

import httpx

from relay.mock_provider.app import Profile, create_mock_app


def client() -> httpx.AsyncClient:
    app = create_mock_app({"default": Profile(latency_ms=0, jitter_ms=0, stream_chunk_delay_ms=0)})
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock")


BODY = {"model": "m", "messages": [{"role": "user", "content": "hello"}]}


async def test_profiles_can_be_changed_at_runtime_and_reset() -> None:
    async with client() as c:
        ok = await c.post("/p/alpha/v1/chat/completions", json=BODY)
        assert ok.status_code == 200

        update = await c.put(
            "/_control/profiles/alpha", json={"failure_rate": 1.0, "failure_status": 502}
        )
        assert update.json()["failure_rate"] == 1.0
        failing = await c.post("/p/alpha/v1/chat/completions", json=BODY)
        assert failing.status_code == 502
        # other profiles are unaffected
        assert (await c.post("/p/beta/v1/chat/completions", json=BODY)).status_code == 200

        assert (await c.put("/_control/profiles/alpha", json={"bogus": 1})).status_code == 400
        await c.post("/_control/reset")
        profiles = (await c.get("/_control/profiles")).json()
        assert "alpha" not in profiles
        assert (await c.get("/health")).json()["requests"] == 0


async def test_anthropic_endpoint_enforces_the_messages_api_contract() -> None:
    async with client() as c:
        missing = await c.post("/v1/messages", json={"model": "m", "messages": []})
        assert missing.status_code == 400
        sampling = await c.post(
            "/v1/messages", json={"model": "m", "max_tokens": 5, "temperature": 1, "messages": []}
        )
        assert sampling.status_code == 400
        assert "temperature" in sampling.json()["error"]["message"]
        forced = await c.post(
            "/v1/messages",
            json={"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "x"}]},
            headers={"x-mock-status": "529"},
        )
        assert forced.status_code == 529
        assert forced.json()["error"]["type"] == "overloaded_error"
