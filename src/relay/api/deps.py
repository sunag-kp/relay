from __future__ import annotations

from fastapi import Request

from relay.bootstrap import Container


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container
