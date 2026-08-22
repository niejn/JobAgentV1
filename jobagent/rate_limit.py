"""Rate-limiter adapters used by external source integrations."""

from __future__ import annotations

from typing import Protocol

from pyrate_limiter import BucketAsyncWrapper, Duration, InMemoryBucket, Limiter, Rate


class AsyncRateLimiter(Protocol):
    """Minimal seam for waiting until one outbound request is allowed."""

    async def acquire(self) -> None:
        """Wait for one permit."""


class BlockingRateLimiter(Protocol):
    """Minimal seam for synchronous transports running in worker threads."""

    def acquire(self) -> None:
        """Block the current worker thread until one permit is available."""


class PyrateAsyncRateLimiter:
    """In-memory async limiter backed by pyrate-limiter's leaky bucket."""

    def __init__(self, *, requests: int, period_seconds: float, name: str) -> None:
        rate = Rate(requests, Duration.SECOND * period_seconds)
        bucket = BucketAsyncWrapper(InMemoryBucket([rate]))
        self._limiter = Limiter(bucket)
        self._name = name

    async def acquire(self) -> None:
        """Wait asynchronously until the configured bucket grants one permit."""

        acquired = await self._limiter.try_acquire_async(self._name)
        if not acquired:  # Blocking acquisition should not normally return False.
            raise RuntimeError(f"Unable to acquire rate-limit permit for {self._name}")


class PyrateBlockingRateLimiter:
    """In-memory limiter for Spider_XHS's synchronous HTTP transport."""

    def __init__(self, *, requests: int, period_seconds: float, name: str) -> None:
        rate = Rate(requests, Duration.SECOND * period_seconds)
        self._limiter = Limiter(InMemoryBucket([rate]))
        self._name = name

    def acquire(self) -> None:
        """Block until the configured bucket grants one permit."""

        acquired = self._limiter.try_acquire(self._name)
        if not acquired:
            raise RuntimeError(f"Unable to acquire rate-limit permit for {self._name}")
