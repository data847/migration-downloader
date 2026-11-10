"""Bitbucket's rolling hour: count our own calls, pause before the budget runs out, and on a 429 pause the whole host until
enough of the window has freed, sending nothing while paused (so waiting never pushes the limit further)."""

import pytest

import ratelimit

HOST = "api.bitbucket.org"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0
        self.waits = []
        self.lines = []

    def wait(self, seconds, reason):
        self.waits.append(round(seconds))
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(ratelimit, "_now", lambda: c.t)
    monkeypatch.setattr(ratelimit.time, "time", lambda: c.t)
    monkeypatch.setattr(ratelimit, "wait", c.wait)
    ratelimit.bind(None, c.lines.append)
    return c


def calls(c, n, every=1.0):
    for _ in range(n):
        ratelimit.before(HOST)
        c.t += every


def test_pauses_before_the_budget_runs_out_and_resumes_when_a_quarter_is_free(clock):
    calls(clock, 950)                               # 950 calls in 950 s: 95% of 1,000, no pause yet
    assert clock.waits == []
    ratelimit.before(HOST)                          # the next one would cross the margin: pause first
    # resume when at most 712 (95% x 75%) of our calls are left in the window: the 238th oldest call ages out
    assert clock.waits == [round(1_000_000 + 237 + 3600 - (1_000_000 + 950))]
    assert any("PAUSED" in l and "resumes on its own at" in l and "does not push the limit further" in l for l in clock.lines)
    assert any("resumed after" in l for l in clock.lines)


def test_a_429_pauses_until_enough_of_our_window_frees_and_sends_nothing_meanwhile(clock):
    calls(clock, 600)                               # 600 of our calls, then Bitbucket says no (another client used the rest)
    until = ratelimit.on_429(HOST)
    assert until == pytest.approx(1_000_000 + 149 + 3600)   # the 150th oldest call ages out: 450 left, a quarter freed
    assert ratelimit.budget_of(HOST) == 600                  # learnt: later pacing stays under it
    before = len(ratelimit._sent[HOST])
    assert ratelimit.paused_until() == until
    ratelimit.before(HOST)                                   # the next call waits out the pause in one go
    assert clock.waits[-1] == round(until - (1_000_000 + 600))
    assert clock.t >= until and ratelimit.paused_until() == 0
    assert len(ratelimit._sent[HOST]) <= before + 1          # only the call after the pause went out


def test_a_429_we_cannot_explain_waits_a_quarter_hour(clock):
    calls(clock, 3)
    assert ratelimit.on_429(HOST) == pytest.approx(clock.t + ratelimit.UNKNOWN_PAUSE)
    assert ratelimit.budget_of(HOST) == 1000        # three calls teach nothing about the budget


def test_every_thread_waits_on_one_pause_and_it_is_announced_once(clock):
    calls(clock, 600)
    ratelimit.on_429(HOST)
    ratelimit.on_429(HOST)                           # a second worker hitting the same wall
    assert sum("PAUSED" in l for l in clock.lines) == 1


def test_x_ratelimit_limit_sets_the_budget(clock):
    ratelimit.observe(HOST, {"X-RateLimit-Limit": "5000"})
    assert ratelimit.budget_of(HOST) == 5000


def test_other_hosts_are_not_counted(clock):
    for _ in range(5):
        ratelimit.before("api.github.com")
    assert "api.github.com" not in ratelimit._sent
