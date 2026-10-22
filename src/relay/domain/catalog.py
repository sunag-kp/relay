"""The provider and route catalog: which upstreams exist and how public model names map
onto them. Loaded from YAML at startup and validated here, so a typo fails the deploy rather
than the first request."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProviderKind(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    # Any server speaking the OpenAI Chat Completions protocol (vLLM, Ollama, the mock, ...).
    OPENAI_COMPATIBLE = "openai_compatible"


class RoutingPolicy(StrEnum):
    PRIORITY = "priority"
    WEIGHTED = "weighted"
    CHEAPEST = "cheapest"
    LOWEST_LATENCY = "lowest-latency"


class BreakerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    failure_rate_threshold: float = Field(default=0.5, gt=0, le=1)
    window_size: int = Field(default=20, ge=1)
    min_calls: int = Field(default=5, ge=1)
    open_seconds: float = Field(default=30.0, gt=0)
    half_open_max_calls: int = Field(default=2, ge=1)


class ProviderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    kind: ProviderKind
    base_url: str
    api_key_env: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    # Non-streaming: total time budget for one attempt.
    timeout_s: float = Field(default=60.0, gt=0)
    connect_timeout_s: float = Field(default=3.0, gt=0)
    # Streaming: time allowed until the first chunk, then between chunks.
    first_token_timeout_s: float = Field(default=20.0, gt=0)
    stream_idle_timeout_s: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=1, ge=0, le=5)
    backoff_base_s: float = Field(default=0.2, ge=0)
    backoff_max_s: float = Field(default=2.0, ge=0)
    max_connections: int = Field(default=200, ge=1)
    breaker: BreakerSpec = Field(default_factory=BreakerSpec)


class TargetSpec(BaseModel):
    """One concrete (provider, model) a route can send traffic to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str
    weight: float = Field(default=1.0, ge=0)
    # USD per one million tokens, as published on the provider's pricing page.
    input_per_mtok: Decimal = Field(default=Decimal(0), ge=0)
    output_per_mtok: Decimal = Field(default=Decimal(0), ge=0)

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}"

    def cost(self, prompt_tokens: int, completion_tokens: int) -> Decimal:
        return (
            Decimal(prompt_tokens) * self.input_per_mtok
            + Decimal(completion_tokens) * self.output_per_mtok
        ) / Decimal(1_000_000)


class RouteSpec(BaseModel):
    """A public model name and the targets that can serve it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    policy: RoutingPolicy = RoutingPolicy.PRIORITY
    targets: list[TargetSpec] = Field(min_length=1)
    description: str = ""


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    providers: list[ProviderSpec] = Field(min_length=1)
    routes: list[RouteSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_references(self) -> Catalog:
        names = [provider.name for provider in self.providers]
        if len(names) != len(set(names)):
            raise ValueError("provider names must be unique")
        models = [route.model for route in self.routes]
        if len(models) != len(set(models)):
            raise ValueError("route model names must be unique")
        for route in self.routes:
            for target in route.targets:
                if target.provider not in names:
                    raise ValueError(
                        f"route {route.model!r} references unknown provider {target.provider!r}"
                    )
        return self

    def provider(self, name: str) -> ProviderSpec:
        for provider in self.providers:
            if provider.name == name:
                return provider
        raise KeyError(name)

    def route(self, model: str) -> RouteSpec | None:
        for route in self.routes:
            if route.model == model:
                return route
        return None
