from __future__ import annotations

import os

import uvicorn


def run() -> None:
    uvicorn.run(
        "relay.mock_provider.app:create_mock_app",
        factory=True,
        host=os.environ.get("MOCK_HOST", "0.0.0.0"),  # noqa: S104 - container service
        port=int(os.environ.get("MOCK_PORT", "9000")),
        workers=int(os.environ.get("MOCK_WORKERS", "1")),
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    run()
