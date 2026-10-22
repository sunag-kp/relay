from __future__ import annotations

from relay.domain.catalog import Catalog, ProviderKind, ProviderSpec
from relay.infra.providers.anthropic import AnthropicProvider
from relay.infra.providers.openai import OpenAIProvider
from relay.services.ports import ChatProvider


def build_provider(spec: ProviderSpec) -> ChatProvider:
    if spec.kind is ProviderKind.ANTHROPIC:
        return AnthropicProvider(spec)
    return OpenAIProvider(spec)


def build_providers(catalog: Catalog) -> dict[str, ChatProvider]:
    return {spec.name: build_provider(spec) for spec in catalog.providers}
