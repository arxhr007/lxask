import pytest


@pytest.fixture(autouse=True)
def _machine_independent(monkeypatch):
    """The checks that look at this machine (its package manager, whether lxask runs as
    root) see no package manager and root, so they stay out of tests that do not set
    them; the tests for them set both."""
    monkeypatch.setenv("LXASK_PACKAGE_MANAGERS", "")
    monkeypatch.setenv("LXASK_AS_ROOT", "1")
