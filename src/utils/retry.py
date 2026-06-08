"""Retry helpers for LLM agent invocations."""

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_RATE_LIMIT_SIGNALS = ("429", "rate_limit", "rate limit", "ratelimit", "too many requests")


def is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in _RATE_LIMIT_SIGNALS)


def invoke_with_retry(
    agent: Any,
    kwargs: dict,
    config: dict,
    max_retries: int = 5,
    base_wait: float = 30.0,
) -> Any:
    """Invoke a LangGraph agent, retrying on 429 rate-limit errors with exponential backoff."""
    for attempt in range(max_retries + 1):
        try:
            return agent.invoke(kwargs, config=config)
        except Exception as e:
            if is_rate_limit_error(e) and attempt < max_retries:
                wait = base_wait * (2 ** attempt)  # 30, 60, 120, 240, 480 s
                logger.warning(
                    "Rate limit hit (attempt %d/%d) — waiting %.0fs before retry.",
                    attempt + 1, max_retries, wait,
                )
                time.sleep(wait)
            else:
                raise
