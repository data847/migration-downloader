import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No test may touch the network or really sleep.

    A test that forgets to fake an API call fails loudly here instead of
    quietly calling GitHub with a bogus token.
    """
    import migration_api
    import ratelimit

    def refuse(*_args, **_kwargs):
        raise AssertionError("a test tried to open a real network connection")

    monkeypatch.setattr(migration_api._opener, "open", refuse)

    # git too: a test that forgets to fake a clone (the Bitbucket mirror, a wiki) must not reach a real host
    import supplementary
    real_git = supplementary.run_git

    def no_network_git(args, **kw):
        if any(str(a).startswith(("http://", "https://", "git@", "ssh://")) for a in args):
            raise AssertionError(f"a test tried a real network git command: git {' '.join(map(str, args[:2]))} …")
        return real_git(args, **kw)

    monkeypatch.setattr(supplementary, "run_git", no_network_git)
    monkeypatch.setattr(ratelimit, "_unit_sleep", lambda: None)
    ratelimit.forget()
    ratelimit.bind(None, None)
    yield
    ratelimit.forget()
