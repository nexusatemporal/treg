"""The shared key-value store: expiring counters and optional admission leases.

Render Key Value (Redis protocol) in production, an in-process dictionary when `TREG_KV_URL` is
unset (local development, tests, self-hosters without one). The store holds nothing that must
survive: every key expires, and every caller treats "unavailable" as a safe answer. Reads and
writes are bounded by `_TIMEOUT_S` so a slow store can never hold a call open.

Review-invitation budgets fail closed; money admission leases report unavailability explicitly
so the caller can retain PostgreSQL's original correctness path. New tenants add narrow methods,
not a generic get/set surface. Local counters do not pretend to provide distributed leases.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Literal, Protocol

from ..config import get_settings

_TIMEOUT_S = 0.1
_LOCAL_MAX_KEYS = 10_000
log = logging.getLogger("treg.kv")

AcquireResult = Literal["acquired", "busy", "unavailable"]
RenewResult = Literal["renewed", "lost", "unavailable"]
ReleaseResult = Literal["released", "lost", "unavailable"]
_RENEW_LEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE_LEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class Store(Protocol):
    async def take(self, key: str, limit: int, ttl_s: int) -> bool:
        """Count one use of `key` inside a window that starts at its first use and lasts `ttl_s`.
        True while the window holds `limit` or fewer uses, False past it OR when the store cannot
        answer: a budget that cannot be checked is spent, never free."""

    async def ping(self) -> bool: ...

    async def acquire_lease(self, key: str, token: str, ttl_ms: int) -> AcquireResult: ...

    async def renew_lease(self, key: str, token: str, ttl_ms: int) -> RenewResult: ...

    async def release_lease(self, key: str, token: str) -> ReleaseResult: ...

    async def aclose(self) -> None: ...


class LocalStore:
    """Per-process fallback with the same contract. Bounded so a flood of keys cannot grow it."""

    def __init__(self) -> None:
        self._windows: dict[str, tuple[float, int]] = {}

    async def take(self, key: str, limit: int, ttl_s: int) -> bool:
        now = time.monotonic()
        expires, count = self._windows.get(key, (0.0, 0))
        if expires <= now:
            if len(self._windows) >= _LOCAL_MAX_KEYS:
                self._evict(now)
            expires, count = now + ttl_s, 0
        count += 1
        self._windows[key] = (expires, count)
        return count <= limit

    def _evict(self, now: float) -> None:
        live = {k: v for k, v in self._windows.items() if v[0] > now}
        if len(live) >= _LOCAL_MAX_KEYS:
            live = dict(sorted(live.items(), key=lambda kv: kv[1][0])[_LOCAL_MAX_KEYS // 2:])
        self._windows = live

    async def ping(self) -> bool:
        return True

    async def acquire_lease(self, key: str, token: str, ttl_ms: int) -> AcquireResult:
        return "unavailable"  # Local counters cannot promise cross-process exclusion.

    async def renew_lease(self, key: str, token: str, ttl_ms: int) -> RenewResult:
        return "unavailable"

    async def release_lease(self, key: str, token: str) -> ReleaseResult:
        return "unavailable"

    async def aclose(self) -> None:
        self._windows.clear()


class RedisStore:
    def __init__(self, url: str) -> None:
        import redis.asyncio as redis  # server extra; imported here so the light CLI never loads it

        self._client = redis.Redis.from_url(
            url, socket_timeout=_TIMEOUT_S, socket_connect_timeout=_TIMEOUT_S,
            decode_responses=True,
        )

    async def take(self, key: str, limit: int, ttl_s: int) -> bool:
        try:
            async with asyncio.timeout(_TIMEOUT_S * 2):
                async with self._client.pipeline(transaction=True) as pipe:
                    pipe.incr(key)
                    pipe.expire(key, ttl_s, nx=True)  # the window starts at the first use
                    count, _ = await pipe.execute()
            return int(count) <= limit
        except Exception as exc:  # noqa: BLE001 — a store fault is a spent budget, never a call fault
            _note_fault(exc)
            return False

    async def ping(self) -> bool:
        try:
            async with asyncio.timeout(_TIMEOUT_S * 2):
                return bool(await self._client.ping())
        except Exception as exc:  # noqa: BLE001
            _note_fault(exc)
            return False

    async def acquire_lease(self, key: str, token: str, ttl_ms: int) -> AcquireResult:
        """A short-lived admission lease, never the authority for a money write.

        Unavailable is deliberately distinct from contention. Lease callers aggregate faults;
        unlike optional invitation budgets, an outage must not discard a pending settlement.
        """
        try:
            async with asyncio.timeout(_TIMEOUT_S * 2):
                acquired = await self._client.set(key, token, nx=True, px=ttl_ms)
            return "acquired" if acquired else "busy"
        except Exception:  # noqa: BLE001 - cancellation still propagates to the owning scope
            return "unavailable"

    async def renew_lease(self, key: str, token: str, ttl_ms: int) -> RenewResult:
        try:
            async with asyncio.timeout(_TIMEOUT_S * 2):
                renewed = await self._client.eval(_RENEW_LEASE, 1, key, token, ttl_ms)
            return "renewed" if renewed else "lost"
        except Exception:  # noqa: BLE001
            return "unavailable"

    async def release_lease(self, key: str, token: str) -> ReleaseResult:
        try:
            async with asyncio.timeout(_TIMEOUT_S * 2):
                released = await self._client.eval(_RELEASE_LEASE, 1, key, token)
            return "released" if released else "lost"
        except Exception:  # noqa: BLE001
            return "unavailable"

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001
            pass


def _note_fault(exc: BaseException) -> None:
    from .. import analytics  # lazy: analytics imports config, and this module is imported early

    log.warning("kv unavailable: %s: %s", type(exc).__name__, exc)
    analytics.capture_fault(exc, component="kv")


_store: Store | None = None


def store() -> Store:
    """The process-wide store, built on first use from `TREG_KV_URL`."""
    global _store
    if _store is None:
        url = get_settings().kv_url
        _store = RedisStore(url) if url else LocalStore()
    return _store


def configured() -> bool:
    return bool(get_settings().kv_url)


async def close() -> None:
    global _store
    if _store is not None:
        await _store.aclose()
        _store = None
