"""Fast token estimates for admission control.

Limits are checked before the provider has counted anything, so the gateway needs an
estimate that is cheap and provider-neutral. Roughly four characters per token holds for
English across both OpenAI and Anthropic tokenizers; the reservation is settled against the
provider's real count afterwards, so the estimate only has to be in the right ballpark.
"""

from __future__ import annotations

from relay.domain.chat import ChatCompletionRequest

CHARS_PER_TOKEN = 4
PER_MESSAGE_OVERHEAD = 4  # role markers and separators


def estimate_text_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def estimate_prompt_tokens(request: ChatCompletionRequest) -> int:
    return sum(
        estimate_text_tokens(message.text()) + PER_MESSAGE_OVERHEAD for message in request.messages
    )


def estimate_reservation(request: ChatCompletionRequest, default_completion: int) -> int:
    """Tokens to reserve against the daily quota before calling the provider."""
    completion = request.output_limit or default_completion
    return estimate_prompt_tokens(request) + completion
