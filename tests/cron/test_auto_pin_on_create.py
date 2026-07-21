"""Test that the cronjob tool auto-pins the current provider on create
when neither the nested ``model`` object nor ``provider`` string is supplied.

This prevents the "global inference config drifted" error (#44585).
"""

import json
import pytest

from tools.cronjob_tools import cronjob
from cron.jobs import get_job


class TestAutoPinOnCreate:
    """The cronjob tool auto-pins the current provider when the caller
    supplies neither the nested ``model`` object nor the ``provider`` string.
    """

    def test_create_without_provider_auto_pins(self, monkeypatch):
        import hermes_cli.config as cfg_mod
        monkeypatch.setattr(
            cfg_mod, "load_config", lambda: {"model": {"provider": "deepseek"}}
        )
        created = json.loads(
            cronjob(action="create", prompt="do a thing", schedule="every 1 hour")
        )
        assert created["success"] is True
        job_id = created["job_id"]
        # Read back from storage to check the pinned field
        stored = get_job(job_id)
        assert stored["provider"] == "deepseek", (
            f"Expected provider to be auto-pinned to 'deepseek', "
            f"got {stored['provider']!r}"
        )
        # Pinned -> no snapshot needed
        assert stored.get("provider_snapshot") is None

    def test_create_with_explicit_provider_not_overridden(self, monkeypatch):
        """When the caller explicitly sets provider, auto-pin must not override it."""
        import hermes_cli.config as cfg_mod
        monkeypatch.setattr(
            cfg_mod, "load_config", lambda: {"model": {"provider": "deepseek"}}
        )
        created = json.loads(
            cronjob(
                action="create",
                prompt="do a thing",
                schedule="every 1 hour",
                provider="openrouter",
            )
        )
        stored = get_job(created["job_id"])
        assert stored["provider"] == "openrouter", (
            "Explicit provider should not be overridden by auto-pin"
        )

    def test_create_with_model_object_no_provider_keeps_pinned(self, monkeypatch):
        """When the model object has a model but no provider, the handler already
        auto-pins via _resolve_model_override. The new fallback must not double-pin
        or interfere."""
        import hermes_cli.config as cfg_mod
        # Set a config provider different from what the handler would pin,
        # to confirm the handler's pin (from _resolve_model_override) wins.
        monkeypatch.setattr(
            cfg_mod, "load_config", lambda: {"model": {"provider": "deepseek"}}
        )
        # We can't easily test through the handler since it resolves the model
        # object before calling cronjob(). So test: calling cronjob() directly
        # with model= set but provider=None should still work (the fallback
        # only fires when provider is falsy).
        created = json.loads(
            cronjob(
                action="create",
                prompt="do a thing",
                schedule="every 1 hour",
                model="claude-sonnet-4",
                # provider NOT set -> should be auto-pinned
            )
        )
        stored = get_job(created["job_id"])
        assert stored["provider"] == "deepseek", (
            f"Auto-pin should fire when provider is not set, "
            f"got {stored['provider']!r}"
        )
        assert stored["model"] == "claude-sonnet-4"
        # Provider is now pinned -> no snapshot
        assert stored.get("provider_snapshot") is None
