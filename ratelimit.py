"""Rate limits and transient failures, for every API call the tool makes.

Waiting beats failing: a run over a few hundred repos will hit GitHub's hourly
limit or a secondary limit, and a 502 now and then is normal. `_request` in
`migration_api` asks this module what to do with each failure, so every client
(exports, extras, for-check) gets the same behaviour.

  * 429                          wait `Retry-After`; with none (Bitbucket), back off long enough for its rolling
                                 hourly budget to recover (~95 min in all) before giving up
  * 404 "temporarily unavailable"  Bitbucket's outage answer: short backoff, a few tries
  * `X-RateLimit-NearLimit: true` (Bitbucket)   space calls to that host a few seconds apart
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
BLIND_429_ATTEMPTS = 12     # a 429 that names no wait (Bitbucket): 60, 120, 240, 480, then 600s each, ~95 min in all
BLIND_429_BASE = 60
BLIND_429_MAX = 600
NEAR_LIMIT_PACE = 4.0       # seconds between calls while Bitbucket says the budget is nearly spent (~900 calls/hour)
NEAR_LIMIT_HOLD = 120       # how long one "near limit" answer keeps the pacing on

_local = threading.local()
_lock = threading.Lock()
_quota: Dict[str, Tuple[int, float]] = {}      # host -> (remaining, reset epoch)
_pace: Dict[str, float] = {}                   # host -> pace calls until this epoch (Bitbucket near its limit)


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


def _quiet_wait(seconds: float) -> None:
    """A pause too short and too frequent to log each time (pacing), still cancellable."""
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
    near = headers.get("X-RateLimit-NearLimit") if headers is not None else None
    if near is not None:
        # Bitbucket sends no remaining/reset counts, only whether the hourly budget is nearly used up
        with _lock:
            if str(near).strip().lower() == "true":
                if host not in _pace:
                    _log(f"    {host} says the hourly API budget is nearly used up; slowing to one call "
                         f"every {NEAR_LIMIT_PACE:.0f}s")
                _pace[host] = time.time() + NEAR_LIMIT_HOLD
            else:
                _pace.pop(host, None)
    remaining = _num(headers, "X-RateLimit-Remaining", "RateLimit-Remaining")
    reset = _num(headers, "X-RateLimit-Reset", "RateLimit-Reset")
    if remaining is None or reset is None:
        return
    with _lock:
        _quota[host] = (int(remaining), reset)


def before(host: str) -> None:
    """Pause ahead of a call when the last response said the budget is spent."""
    with _lock:
        paced_until = _pace.get(host, 0)
    if paced_until > time.time():
        _quiet_wait(NEAR_LIMIT_PACE)
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
        _pace.clear()


def delay_for(code: int, headers, body: str, attempt: int) -> Optional[float]:
    """Seconds to wait before retrying this HTTP failure, or None to give up."""
    retry_after = _num(headers, "Retry-After")
    if code == 429 and retry_after is None:
        # Bitbucket Cloud: a rolling hourly budget, a 429 with no Retry-After and no reset time. Back off long enough
        # (well past an hour in total) for the window to roll over instead of giving up after a few minutes.
        if attempt >= BLIND_429_ATTEMPTS - 1:
            return None
        return min(BLIND_429_BASE * (2 ** attempt), BLIND_429_MAX)
    if attempt >= MAX_ATTEMPTS - 1:
        return None
    if code == 429:
        return retry_after
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
    if code == 404 and "temporarily unavailable" in (body or "").lower() and attempt < TRANSIENT_ATTEMPTS:
        # Bitbucket answers some outages as 404 {"errorMessage": "Site temporarily unavailable"}
        return min(5 * (2 ** attempt), 60)
    return None


def delay_for_network(attempt: int) -> Optional[float]:
    return min(2 * (2 ** attempt), 30) if attempt < TRANSIENT_ATTEMPTS else None
