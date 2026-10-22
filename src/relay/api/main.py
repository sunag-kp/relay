from __future__ import annotations

import uvicorn

from relay.config import get_settings


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "relay.api.app:create_app_from_env",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_graceful_shutdown=20,
    )


if __name__ == "__main__":
    run()
