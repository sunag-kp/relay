from __future__ import annotations

from redis.asyncio import BlockingConnectionPool, Redis


def create_redis(url: str, *, max_connections: int = 100) -> Redis:
    """One shared pool per process.

    A *blocking* pool makes callers wait for a free connection under load instead of failing
    with "Too many connections", which turns a burst into latency rather than errors.
    """
    pool = BlockingConnectionPool.from_url(
        url,
        max_connections=max_connections,
        timeout=5,
        socket_timeout=2,
        socket_connect_timeout=2,
        health_check_interval=30,
        retry_on_timeout=True,
    )
    return Redis(connection_pool=pool)
