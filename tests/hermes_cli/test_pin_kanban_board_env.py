"""Tests for `_pin_kanban_board_env` helper invoked by `cmd_chat`.

The ambient "current board" was ripped out (2026-11-16): pinning must be a
no-op unless the caller explicitly set `HERMES_KANBAN_BOARD`. There is no
resolved-value pinning anymore — nothing reads an ambient pointer, so
there is nothing to mirror into the env.
"""
import importlib
import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_kanban_board_env():
    """Snapshot `HERMES_KANBAN_BOARD` and restore it after the test.

    `_pin_kanban_board_env()` inspects ``os.environ`` directly, bypassing
    any ``monkeypatch.setenv`` tracking. Without this fixture the mutation
    leaks into subsequent tests and breaks anything that resolves a kanban
    path from the env.
    """
    prev = os.environ.get("HERMES_KANBAN_BOARD")
    os.environ.pop("HERMES_KANBAN_BOARD", None)
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("HERMES_KANBAN_BOARD", None)
        else:
            os.environ["HERMES_KANBAN_BOARD"] = prev


def test_pin_is_noop_when_env_unset(monkeypatch):
    """No ambient pointer exists: nothing is written when env is unset."""
    main_mod = importlib.import_module("hermes_cli.main")

    main_mod._pin_kanban_board_env()

    assert "HERMES_KANBAN_BOARD" not in main_mod.os.environ


def test_pin_does_not_overwrite_existing_env(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "preset")
    main_mod = importlib.import_module("hermes_cli.main")

    main_mod._pin_kanban_board_env()

    assert main_mod.os.environ.get("HERMES_KANBAN_BOARD") == "preset"
