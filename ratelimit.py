"""Rate limits and transient failures, for every API call the tool makes.

Waiting beats failing: a run over a few hundred repos will hit GitHub's hourly
limit or a secondary limit, and a 502 now and then is normal. `_request` in
`migration_api` asks this module what to do with each failure, so every client
(exports, extras, for-check) gets the same behaviour.

  * 429                          wait `Retry-After`, else back off
  * 403 + `X-RateLimit-Remaining: 0`   wait until `X-RateLimit-Reset`
  * 403 + `Retry-After` or "secondary rate limit" in the body   wait, then retry
  * any other 403                a real permission error: no retry
  * 500/502/503/504, network error     short exponential backoff, a few tries
  * a response saying 0-1 calls remain  pause *before* the next call to that host

Waits are cancellable and logged through whatever the running job bound with
`bind()` (thread-local, because each job runs in its own thread).
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Optional, Tuple

MAX_ATTEMPTS = 5            # per call, rate limits included
TRANSIENT_ATTEMPTS = 3      # 5xx / network: give up sooner
MAX_WAIT = 3700             # seconds; one GitHub window is an hour
SECONDARY_BACKOFF = 60      # when a secondary limit names no Retry-After
LOW_WATER = 1               # pause before the call that would use the last one

_local = threading.local()
_lock = threading.Lock()
_quota: Dict[str, Tuple[int, float]] = {}      # host -> (remaining, reset epoch)


class Cancelled(Exception):
    """Raised from a wait when the job was cancelled."""


def bind(cancelled: Optional[Callable[[], bool]] = None, log: Optional[Callable[[str], None]] = None) -> None:
    _local.cancelled = cancelled
    _local.log = log


def _log(message: str) -> None:
    fn = getattr(_local, "log", None)
    if fn:
        fn(message)


def _unit_sleep() -> None:      # a seam so tests don't actually wait
    time.sleep(1)


def wait(seconds: float, reason: str) -> None:
    seconds = max(0.0, min(float(seconds), MAX_WAIT))
    if seconds <= 0:
        return
    _log(f"    {reason}; waiting {int(seconds)}s")
    cancelled = getattr(_local, "cancelled", None)
    remaining = seconds
    while remaining > 0:
        if cancelled and cancelled():
            raise Cancelled()
        _unit_sleep()
        remaining -= 1


def _num(headers, *names) -> Optional[float]:
    for name in names:
        value = headers.get(name) if headers is not None else None
        if value not in (None, ""):
            try:
                return float(value)
            except ValueError:
                pass
    return None


def observe(host: str, headers) -> None:
    """Remember how many calls the host says we have left."""
    remaining = _num(headers, "X-RateLimit-Remaining", "RateLimit-Remaining")
    reset = _num(headers, "X-RateLimit-Reset", "RateLimit-Reset")
    if remaining is None or reset is None:
        return
    with _lock:
        _quota[host] = (int(remaining), reset)


def before(host: str) -> None:
    """Pause ahead of a call when the last response said the budget is spent."""
    with _lock:
        state = _quota.get(host)
    if not state:
        return
    remaining, reset = state
    delay = reset - time.time() + 1
    if remaining <= LOW_WATER and 0 < delay <= MAX_WAIT:
        wait(delay, f"{host} rate limit nearly used up")
        with _lock:
            _quota.pop(host, None)


def forget() -> None:
    with _lock:
        _quota.clear()


def delay_for(code: int, headers, body: str, attempt: int) -> Optional[float]:
    """Seconds to wait before retrying this HTTP failure, or None to give up."""
    if attempt >= MAX_ATTEMPTS - 1:
        return None
    retry_after = _num(headers, "Retry-After")
    if code == 429:
        return retry_after if retry_after is not None else min(30 * (2 ** attempt), 300)
    if code == 403:
        remaining = _num(headers, "X-RateLimit-Remaining", "RateLimit-Remaining")
        reset = _num(headers, "X-RateLimit-Reset", "RateLimit-Reset")
        if remaining == 0 and reset is not None:
            return max(1.0, reset - time.time() + 1)
        if retry_after is not None:
            return retry_after
        lowered = (body or "").lower()
        if "secondary rate limit" in lowered or "abuse detection" in lowered or "rate limit exceeded" in lowered:
            return SECONDARY_BACKOFF * (attempt + 1)
        return None                 # a genuine permission failure
    if code in (500, 502, 503, 504) and attempt < TRANSIENT_ATTEMPTS:
        return min(2 * (2 ** attempt), 60)
    return None


def delay_for_network(attempt: int) -> Optional[float]:
    return min(2 * (2 ** attempt), 30) if attempt < TRANSIENT_ATTEMPTS else None
