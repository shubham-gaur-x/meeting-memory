"""Shared resilience utilities."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = (429, 500, 502, 503, 504, 529)


def _is_retryable(exc: BaseException, retryable_status: tuple[int, ...]) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in retryable_status
    # Anthropic SDK — avoid hard import; check via module name reflection
    mod = type(exc).__module__
    if mod.startswith("anthropic"):
        status = getattr(exc, "status_code", None)
        if status is not None:
            return status in retryable_status
        name = type(exc).__name__.lower()
        if "connection" in name or "timeout" in name:
            return True
    return False


def with_retry(
    fn: Callable[[], Any],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    retryable_status: tuple[int, ...] = _RETRYABLE_STATUS,
) -> Any:
    """Call fn() up to max_attempts times with exponential backoff.

    Retries on transient network errors and HTTP 429/5xx.
    Re-raises immediately on any non-retryable exception.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if not _is_retryable(exc, retryable_status):
                raise
            if attempt == max_attempts:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), 30.0)
            # Respect Retry-After header on 429 responses
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                retry_after = exc.response.headers.get("retry-after")
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        pass  # non-numeric Retry-After (e.g. HTTP-date), ignore
            elif getattr(exc, "status_code", None) == 429:
                # Anthropic SDK RateLimitError (not httpx.HTTPStatusError)
                _headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
                retry_after = _headers.get("retry-after")
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        pass
            status_hint = getattr(
                getattr(exc, "response", None), "status_code", type(exc).__name__
            )
            logger.warning(
                "[retry] attempt %d/%d after %s — waiting %.0fs",
                attempt, max_attempts, status_hint, delay,
            )
            time.sleep(delay)
    raise RuntimeError("unreachable")  # satisfies type checker
