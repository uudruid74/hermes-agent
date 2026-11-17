"""Fixtures for tests/dispatch — the Dispatch/Wake Conformance Suite."""

import pytest

from tests.dispatch.wake_harness import build_runner


@pytest.fixture
def wake_env(monkeypatch, tmp_path):
    """(runner, adapter, recorder): real intake wired to the harness fakes."""
    return build_runner(monkeypatch, tmp_path)
