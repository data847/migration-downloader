"""Rate limits and transient failures, for every API call the tool makes.

Waiting beats failing: a run over a few hundred repos will hit GitHub's hourly
limit or a secondary limit, and a 502 now and then is normal. `_request` in
`migration_api` asks this module what to do with each failure, so every client
(exports, extras, for-check) gets the same behaviour.

  * 429                          wait `Retry-After`; with none (Bitbucket's rolling hour) PAUSE the whole host, sending
                                 nothing, until enough of the hour has freed (computed from our own calls; see below)
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
    learn_limit(host, headers)
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
    _hold(host)            # a host paused after a 429: nothing goes out until it ends
    _pace_budget(host)     # our own count of the rolling hour says the budget is nearly used
    note_call(host)
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
        _sent.clear()
        _budget.clear()
        _paused.clear()


# ── Bitbucket's rolling hour: count our own calls, pause instead of probing ──────────────────────────────────────────
#
# Bitbucket Cloud limits each user to a budget of calls over a ONE-HOUR ROLLING WINDOW (1,000/hour for repository data with
# an API token; more for workspace access tokens on big plans) and its 429 names no Retry-After and no reset time
# (https://support.atlassian.com/bitbucket-cloud/docs/api-request-limits/). A call frees up exactly one hour after it was
# made. Retrying every few minutes only spends calls that may themselves count and pushes the window further out, so:
#   * every call to such a host is timestamped here, and before the budget is used up the run pauses until enough of the
#     oldest calls have aged out (it never reaches the 429 on its own);
#   * on a 429 anyway (the same user's token used elsewhere, a lower limit) the whole host is PAUSED: no call at all goes
#     out until the computed resume time, every job and thread waits on the same pause, and the budget learnt from the
#     429 (the calls the window held) keeps later pacing below it;
#   * the pause is announced once, with the time it resumes, and a pause never counts as a failed attempt.

WINDOW = 3600.0
DEFAULT_BUDGET = {"api.bitbucket.org": 1000}    # the documented base limit for repository data
BUDGET_MARGIN = 0.95                            # leave 5% for anything else using the same account
RESUME_FREE = 0.25                              # resume once a quarter of the budget is free again
MIN_PAUSE = 60.0
LEARN_SHARE = 0.5                               # our calls must fill half the budget before a 429 teaches us a lower one
UNKNOWN_PAUSE = 900.0                           # a 429 we cannot explain from our own calls: wait 15 min, then one call
MAX_RATE_PAUSES = 6                             # per call: six silent pauses (6+ hours) without one success is a real problem

_sent: Dict[str, "list[float]"] = {}            # host -> times of our calls in the last hour, oldest first
_budget: Dict[str, int] = {}                    # host -> calls per rolling hour (documented, from X-RateLimit-Limit, or learnt)
_paused: Dict[str, float] = {}                  # host -> epoch the pause ends


def _now() -> float:            # a seam so tests control the clock
    return time.time()


def _window(host: str, now: float) -> "list[float]":
    sent = _sent.setdefault(host, [])
    cut = now - WINDOW
    while sent and sent[0] <= cut:
        sent.pop(0)
    return sent


def budget_of(host: str) -> Optional[int]:
    with _lock:
        return _budget.get(host) or DEFAULT_BUDGET.get(host)


def _resume_after(sent: "list[float]", keep: int, now: float) -> float:
    """When at most `keep` of these calls are still inside the window."""
    drop = len(sent) - max(0, keep)
    if drop <= 0:
        return now
    return sent[drop - 1] + WINDOW


def _clock(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


def _announce(host: str, until: float, why: str) -> None:
    mins = max(1, int(round((until - _now()) / 60)))
    _log(f"  ⏸ PAUSED: {why}. Leave this run going in the background: it resumes on its own at {_clock(until)} "
         f"(in about {mins} min) and carries on where it stopped. Nothing is sent to {host} until then, so waiting does "
         f"not push the limit further.")


def paused_until() -> float:
    """The latest pause end of any host still paused (0 when none), for the job status."""
    now = _now()
    with _lock:
        ends = [u for u in _paused.values() if u > now]
    return max(ends) if ends else 0.0


def on_429(host: str) -> float:
    """A 429 that names no wait (Bitbucket): pause the host until enough of the window frees, learning the budget."""
    now = _now()
    with _lock:
        sent = _window(host, now)
        known = _budget.get(host) or DEFAULT_BUDGET.get(host) or 0
        if known and len(sent) >= known * LEARN_SHARE:
            # our own calls filled the window: that many is the budget we really have, and we know when they age out
            _budget[host] = min(known, len(sent))
            until = _resume_after(sent, int(len(sent) * (1 - RESUME_FREE)), now)
        else:
            # the hour was used up before this run or by another client: we cannot see when it frees, so wait a quarter of
            # it and try one call (at most four calls an hour, which hardly moves the window)
            until = now + UNKNOWN_PAUSE
        until = max(until, now + MIN_PAUSE)
        first = _paused.get(host, 0) <= now
        _paused[host] = max(_paused.get(host, 0), until)
        until = _paused[host]
    if first:
        _announce(host, until, f"{host} says this account's hourly API limit is used up")
    return until


def _hold(host: str) -> None:
    """Wait out a pause of the host, if any; the wait itself sends nothing."""
    with _lock:
        until = _paused.get(host, 0)
    now = _now()
    if until > now:
        wait(until - now, f"paused for {host}'s rate limit (resumes at {_clock(until)})")
        with _lock:
            if _paused.get(host, 0) <= until:
                _paused.pop(host, None)
        _log(f"  ▶ resumed after the {host} rate-limit pause")


def _pace_budget(host: str) -> None:
    """Pause before the call that would use up the rolling budget, until a quarter of it is free again."""
    b = budget_of(host)
    if not b:
        return
    now = _now()
    with _lock:
        sent = _window(host, now)
        if len(sent) < int(b * BUDGET_MARGIN):
            return
        until = max(_resume_after(sent, int(b * BUDGET_MARGIN * (1 - RESUME_FREE)), now), now + 1)
        first = _paused.get(host, 0) <= now
        _paused[host] = max(_paused.get(host, 0), until)
        until = _paused[host]
    if first:
        _announce(host, until, f"this run has used {len(sent)} of the about {b} calls {host} allows per hour")
    _hold(host)


def note_call(host: str) -> None:
    """Count a call about to go out (a failed or refused one may count too, so every attempt is counted)."""
    if not budget_of(host) and host not in _sent:
        return
    now = _now()
    with _lock:
        _window(host, now).append(now)


def learn_limit(host: str, headers) -> None:
    """X-RateLimit-Limit (Bitbucket, on scaled endpoints) says the real hourly budget."""
    limit = _num(headers, "X-RateLimit-Limit")
    if limit and limit > 0:
        with _lock:
            _budget[host] = int(limit)


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
