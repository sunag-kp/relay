"""Admin API: key management, usage, alerts, provider health and cache control.

Authenticated with `Authorization: Bearer <ADMIN_TOKEN>`. With `DASHBOARD_PUBLIC=true` the
read-only reporting endpoints (usage, alerts, providers) are open, so a public demo
dashboard works without handing out the admin token; key management never is.
"""

from __future__ import annotations

import hmac
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field

from relay.api.deps import get_container
from relay.bootstrap import Container
from relay.domain.errors import AuthenticationFailed, InvalidRequest, ServiceUnavailable
from relay.domain.keys import ApiKey
from relay.services.keys import KeySpec
from relay.services.ports import KeyChanges

router = APIRouter(prefix="/admin", tags=["admin"])


def _token(authorization: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def require_admin(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> Container:
    container = get_container(request)
    expected = container.settings.admin_token.get_secret_value()
    # Constant-time comparison: response timing must not leak how much of a guess matched.
    if not hmac.compare_digest(_token(authorization).encode(), expected.encode()):
        raise AuthenticationFailed("Admin endpoints need 'Authorization: Bearer <ADMIN_TOKEN>'.")
    return container


def require_reader(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> Container:
    container = get_container(request)
    if container.settings.dashboard_public:
        return container
    return require_admin(request, authorization)


Admin = Annotated[Container, Depends(require_admin)]
Reader = Annotated[Container, Depends(require_reader)]


# ---------------------------------------------------------------- keys


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    owner: str = Field(default="default", min_length=1, max_length=100)
    rpm_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    tpd_limit: int | None = Field(default=None, ge=1, le=10**12)
    monthly_budget_usd: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    enforce_budget: bool = False
    allowed_models: list[str] | None = None


class KeyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    rpm_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    tpd_limit: int | None = Field(default=None, ge=1, le=10**12)
    monthly_budget_usd: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    enforce_budget: bool | None = None
    allowed_models: list[str] | None = None


class KeyOut(BaseModel):
    id: UUID
    name: str
    owner: str
    prefix: str
    rpm_limit: int
    tpd_limit: int
    monthly_budget_usd: Decimal | None
    enforce_budget: bool
    allowed_models: list[str] | None
    created_at: datetime
    revoked_at: datetime | None
    active: bool

    @classmethod
    def of(cls, key: ApiKey) -> KeyOut:
        return cls(
            id=key.id,
            name=key.name,
            owner=key.owner,
            prefix=key.prefix,
            rpm_limit=key.rpm_limit,
            tpd_limit=key.tpd_limit,
            monthly_budget_usd=key.monthly_budget_usd,
            enforce_budget=key.enforce_budget,
            allowed_models=list(key.allowed_models) if key.allowed_models is not None else None,
            created_at=key.created_at,
            revoked_at=key.revoked_at,
            active=key.active,
        )


class KeyCreated(KeyOut):
    api_key: str = Field(description="The secret key. Shown once; only its hash is stored.")


def _check_models(container: Container, models: list[str] | None) -> None:
    if models is None:
        return
    unknown = [m for m in models if container.catalog.route(m) is None]
    if unknown:
        raise InvalidRequest(f"Unknown models: {', '.join(unknown)}.", param="allowed_models")


@router.post("/keys", status_code=201, response_model=KeyCreated, summary="Create an API key")
async def create_key(body: KeyCreate, container: Admin) -> KeyCreated:
    _check_models(container, body.allowed_models)
    settings = container.settings
    key, plaintext = await container.keys.create(
        KeySpec(
            name=body.name,
            owner=body.owner,
            rpm_limit=body.rpm_limit or settings.default_rpm_limit,
            tpd_limit=body.tpd_limit or settings.default_tpd_limit,
            monthly_budget_usd=body.monthly_budget_usd,
            enforce_budget=body.enforce_budget,
            allowed_models=tuple(body.allowed_models) if body.allowed_models else None,
        )
    )
    return KeyCreated(**KeyOut.of(key).model_dump(), api_key=plaintext)


@router.get("/keys", response_model=list[KeyOut], summary="List API keys")
async def list_keys(container: Admin, include_revoked: bool = False) -> list[KeyOut]:
    return [KeyOut.of(k) for k in await container.keys.list(include_revoked=include_revoked)]


@router.get("/keys/{key_id}", response_model=KeyOut, summary="Get an API key")
async def get_key(key_id: UUID, container: Admin) -> KeyOut:
    return KeyOut.of(await container.keys.get(key_id))


@router.patch("/keys/{key_id}", response_model=KeyOut, summary="Change a key's name or limits")
async def update_key(key_id: UUID, body: KeyUpdate, container: Admin) -> KeyOut:
    """Fields left out are unchanged; `monthly_budget_usd: null` or `allowed_models: null`
    removes the budget or the model restriction. Takes effect on every replica at once."""
    _check_models(container, body.allowed_models)
    explicit = body.model_fields_set
    changes = KeyChanges(
        name=body.name,
        rpm_limit=body.rpm_limit,
        tpd_limit=body.tpd_limit,
        monthly_budget_usd=float(body.monthly_budget_usd)
        if body.monthly_budget_usd is not None
        else None,
        clear_budget="monthly_budget_usd" in explicit and body.monthly_budget_usd is None,
        enforce_budget=body.enforce_budget,
        allowed_models=body.allowed_models,
        clear_allowed_models="allowed_models" in explicit and body.allowed_models is None,
    )
    return KeyOut.of(await container.keys.update(key_id, changes))


@router.delete("/keys/{key_id}", response_model=KeyOut, summary="Revoke an API key")
async def revoke_key(key_id: UUID, container: Admin) -> KeyOut:
    """Revocation is immediate on every replica (the shared key cache is invalidated);
    the key's usage history is kept."""
    return KeyOut.of(await container.keys.revoke(key_id))


# ---------------------------------------------------------------- usage


class UsageTotals(BaseModel):
    requests: int
    errors: int
    cache_hits: int
    semantic_hits: int
    cache_hit_rate: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    saved_usd: float
    avg_latency_ms: float


class DayUsage(UsageTotals):
    day: str


class KeyUsage(UsageTotals):
    api_key_id: str
    name: str
    owner: str


class RouteUsage(UsageTotals):
    route: str


class ProviderUsage(UsageTotals):
    provider: str


class UsageSummary(BaseModel):
    since: str
    until: str
    totals: UsageTotals
    by_day: list[DayUsage]
    by_key: list[KeyUsage]
    by_route: list[RouteUsage]
    by_provider: list[ProviderUsage]


class UsageEventOut(BaseModel):
    request_id: str
    created_at: datetime
    api_key_id: UUID
    owner: str
    route: str
    provider: str | None
    provider_model: str | None
    status: str
    cache: str
    stream: bool
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal
    saved_usd: Decimal
    latency_ms: int
    attempts: int
    replica: str


def _reports(container: Container) -> Any:
    if container.reports is None:
        raise ServiceUnavailable("Usage reporting is not configured.")
    return container.reports


@router.get("/usage/summary", response_model=UsageSummary, summary="Usage, cost and cache stats")
async def usage_summary(
    container: Reader,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    key_id: UUID | None = None,
) -> dict[str, Any]:
    """Daily rollups for the last `days` days (UTC): totals, a per-day series and
    breakdowns per key, route and provider. Includes metered requests only; 429s and
    authentication failures are counted in Prometheus instead."""
    keys = await container.keys.list(include_revoked=True)
    summary: dict[str, Any] = await _reports(container).summary(
        days=days, keys=keys, api_key_id=key_id
    )
    return summary


@router.get("/usage/events", response_model=list[UsageEventOut], summary="Recent requests")
async def usage_events(
    container: Reader,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    key_id: UUID | None = None,
) -> list[UsageEventOut]:
    events = await _reports(container).recent(limit, key_id)
    return [UsageEventOut.model_validate(event, from_attributes=True) for event in events]


class AlertOut(BaseModel):
    api_key_id: UUID
    key_name: str
    owner: str
    period: str
    threshold_pct: int
    spend_usd: Decimal
    budget_usd: Decimal
    created_at: datetime


@router.get("/alerts", response_model=list[AlertOut], summary="Budget alerts")
async def alerts(
    container: Reader, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[AlertOut]:
    if container.alerts is None:
        return []
    return [
        AlertOut.model_validate(a, from_attributes=True)
        for a in await container.alerts.recent(limit)
    ]


# ---------------------------------------------------------------- providers & cache


class ProviderState(BaseModel):
    provider: str
    kind: str
    configured: bool
    worst_state: str
    replicas: dict[str, str]
    failure_rate: dict[str, float]


class RouteInfo(BaseModel):
    model: str
    policy: str
    targets: list[str]


class ProvidersOut(BaseModel):
    replicas: list[str]
    providers: list[ProviderState]
    latency_ms: dict[str, dict[str, float]]
    routes: list[RouteInfo]


_SEVERITY = {"closed": 0, "half_open": 1, "open": 2}


@router.get("/providers", response_model=ProvidersOut, summary="Provider health on every replica")
async def providers(container: Reader) -> ProvidersOut:
    """Circuit-breaker state and latency per provider, as each live replica sees it.
    Breakers are per replica by design, so the replicas can briefly disagree."""
    snapshots = await container.health_board.read_all() if container.health_board else {}
    if not snapshots:
        snapshots = {container.settings.replica_id: container.chat.health.snapshot()}
    states = []
    for spec in container.catalog.providers:
        per_replica = {
            replica: snap["providers"].get(spec.name, {}).get("state", "closed")
            for replica, snap in snapshots.items()
        }
        states.append(
            ProviderState(
                provider=spec.name,
                kind=spec.kind.value,
                configured=container.providers[spec.name].configured,
                worst_state=max(per_replica.values(), key=lambda s: _SEVERITY.get(s, 0)),
                replicas=per_replica,
                failure_rate={
                    replica: snap["providers"].get(spec.name, {}).get("failure_rate", 0.0)
                    for replica, snap in snapshots.items()
                },
            )
        )
    return ProvidersOut(
        replicas=sorted(snapshots),
        providers=states,
        latency_ms={replica: snap.get("latency_ms", {}) for replica, snap in snapshots.items()},
        routes=[
            RouteInfo(model=r.model, policy=r.policy.value, targets=[t.key for t in r.targets])
            for r in container.catalog.routes
        ],
    )


@router.post("/cache/flush", summary="Drop every cached response")
async def flush_cache(container: Admin) -> dict[str, int]:
    cache = container.chat.cache
    return await cache.flush() if cache is not None else {"exact": 0, "semantic": 0}
