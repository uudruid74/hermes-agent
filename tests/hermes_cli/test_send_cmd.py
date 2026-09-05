"""Tests for the ``hermes send`` CLI subcommand.

Covers the argument parsing / stdin / file / list behavior of
``hermes_cli.send_cmd``. The underlying ``send_message_tool`` is stubbed so
no network I/O or gateway is required.
"""

from __future__ import annotations

import io
import json

import pytest

from hermes_cli import send_cmd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(argv):
    """Build the top-level parser and return the parsed args for ``argv``."""
    import argparse

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    send_cmd.register_send_subparser(subparsers)
    return parser.parse_args(["send", *argv])


class _FakeTool:
    """Replacement for ``tools.send_message_tool.send_message_tool``."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, args, **_kw):
        self.calls.append(dict(args))
        return json.dumps(self.payload)


@pytest.fixture
def fake_tool(monkeypatch):
    """Install a fake send_message_tool and return the stub for inspection."""
    import sys
    import types

    fake = _FakeTool({"success": True, "message_id": "m123"})

    mod = types.ModuleType("tools.send_message_tool")
    mod.send_message_tool = fake
    # Register the stub so ``from tools.send_message_tool import ...`` inside
    # cmd_send resolves to our fake. Also patch the parent ``tools`` package
    # entry so attribute lookup works.
    monkeypatch.setitem(sys.modules, "tools.send_message_tool", mod)
    return fake


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------




def test_file_decode_error_suggests_media_directive(fake_tool, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    bad = tmp_path / "bad-bytes.bin"
    bad.write_bytes(b"\xff\xfe\x00")

    args = _parse(["--to", "telegram", "--file", str(bad)])
    with pytest.raises(SystemExit) as exc:
        send_cmd.cmd_send(args)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "not a text file" in err.lower()
    assert f"MEDIA:{bad}" in err
    assert "[[as_document]]" in err






# ---------------------------------------------------------------------------
# --list
# ---------------------------------------------------------------------------


def test_list_includes_configured_platform_without_discovered_channels(
    monkeypatch, capsys
):
    """A configured platform absent from the channel directory must still be
    listed (with a no-channels hint) instead of silently omitted."""
    import types
    import sys

    class _FakePlatform:
        def __init__(self, value):
            self.value = value

    class _FakeGwConfig:
        def get_connected_platforms(self):
            return [_FakePlatform("simplex")]

    fake_gw_config = types.ModuleType("gateway.config")
    fake_gw_config.load_gateway_config = lambda: _FakeGwConfig()
    monkeypatch.setitem(sys.modules, "gateway.config", fake_gw_config)

    fake_dir = types.ModuleType("gateway.channel_directory")
    fake_dir.load_directory = lambda: {"updated_at": None, "platforms": {}}

    def _format(platforms=None):
        lines = []
        for name, channels in sorted((platforms or {}).items()):
            lines.append(f"{name}:")
            if not channels:
                lines.append("  (no channels discovered yet)")
        return "\n".join(lines)

    fake_dir.format_directory_for_display = _format
    monkeypatch.setitem(sys.modules, "gateway.channel_directory", fake_dir)

    rc = send_cmd._list_targets(None, json_mode=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "simplex" in out
    assert "no channels discovered yet" in out


def test_list_json_includes_configured_platform(monkeypatch, capsys):
    import types
    import sys

    class _FakePlatform:
        def __init__(self, value):
            self.value = value

    class _FakeGwConfig:
        def get_connected_platforms(self):
            return [_FakePlatform("simplex"), _FakePlatform("local")]

    fake_gw_config = types.ModuleType("gateway.config")
    fake_gw_config.load_gateway_config = lambda: _FakeGwConfig()
    monkeypatch.setitem(sys.modules, "gateway.config", fake_gw_config)

    fake_dir = types.ModuleType("gateway.channel_directory")
    fake_dir.load_directory = lambda: {
        "updated_at": None,
        "platforms": {"telegram": [{"id": "1", "name": "home"}]},
    }
    fake_dir.format_directory_for_display = lambda platforms=None: ""
    monkeypatch.setitem(sys.modules, "gateway.channel_directory", fake_dir)

    rc = send_cmd._list_targets(None, json_mode=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["platforms"]["simplex"] == []
    assert "local" not in payload["platforms"]  # infra pseudo-platform skipped
    assert payload["platforms"]["telegram"]  # discovered entries preserved


def test_list_shows_profile_session_key_routable_target_and_bridge(
    monkeypatch, capsys
):
    import sys
    import types

    fake_dir = types.ModuleType("gateway.channel_directory")
    fake_dir.load_directory = lambda: {
        "updated_at": None,
        "platforms": {"telegram": [{"id": "123", "name": "Evan (dm)"}]},
    }
    fake_dir.format_directory_for_display = lambda platforms=None: "telegram:Evan (dm)"
    monkeypatch.setitem(sys.modules, "gateway.channel_directory", fake_dir)
    monkeypatch.setattr(
        send_cmd,
        "_list_profile_sessions",
        lambda: [{
            "profile": "zephyr",
            "session_key": "agent:main:telegram:dm:123",
            "target": "zephyr:telegram:123",
            "display_name": "Evan",
            "bridge_socket": "/tmp/hermes/mcp_bridge.zephyr.sock",
            "platform": "telegram",
        }],
        raising=False,
    )

    rc = send_cmd._list_targets(None, json_mode=False)
    out = capsys.readouterr().out

    assert rc == 0
    assert "zephyr:telegram:123" in out
    assert "agent:main:telegram:dm:123" in out
    assert "profile: zephyr" in out
    assert "/tmp/hermes/mcp_bridge.zephyr.sock" in out


def test_profile_qualified_user_target_selects_profile_before_send(
    fake_tool, monkeypatch
):
    import hermes_cli.profiles

    profile_home = "/home/user/.hermes/profiles/zephyr"
    monkeypatch.setattr(send_cmd, "_load_hermes_env", lambda: None)
    monkeypatch.setattr(hermes_cli.profiles, "profile_exists", lambda name: name == "zephyr")
    monkeypatch.setattr(hermes_cli.profiles, "get_profile_dir", lambda _name: profile_home)
    monkeypatch.setenv("HERMES_HOME", "/home/user/.hermes/profiles/neo")
    monkeypatch.setenv("HERMES_PROFILE", "neo")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    args = _parse(["-u", "zephyr:telegram:123", "wake"])
    with pytest.raises(SystemExit) as exc:
        send_cmd.cmd_send(args)

    assert exc.value.code == 0
    assert fake_tool.calls == [{
        "action": "send",
        "message": "wake",
        "target": "telegram:123",
        "internal": True,
    }]
    assert __import__("os").environ["HERMES_HOME"] == profile_home
    assert __import__("os").environ["HERMES_PROFILE"] == "zephyr"


# ---------------------------------------------------------------------------
# Parser registration contract
# ---------------------------------------------------------------------------


def test_register_send_subparser_is_reusable():
    """Sanity check: the registrar returns a parser and wires ``cmd_send``."""
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    send_parser = send_cmd.register_send_subparser(subparsers)
    assert send_parser is not None
    args = parser.parse_args(["send", "--to", "telegram", "hi"])
    assert args.func is send_cmd.cmd_send
    assert args.to == "telegram"
    assert args.message == "hi"


# ---------------------------------------------------------------------------
# Env loader
# ---------------------------------------------------------------------------


def test_load_hermes_env_bridges_config_yaml_scalars(tmp_path, monkeypatch):
    """Top-level config.yaml scalars should be bridged into os.environ.

    This mirrors the gateway/run.py bootstrap behavior: without this, running
    ``hermes send`` from a fresh shell cannot resolve the home channel
    because ``TELEGRAM_HOME_CHANNEL`` (saved by ``hermes config set``) lives
    in config.yaml, not in .env — and the gateway's config loader reads via
    ``os.getenv(...)``.
    """
    import os

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text("SOME_TOKEN=abc123\n")
    (hermes_home / "config.yaml").write_text(
        "TELEGRAM_HOME_CHANNEL: '5550001111'\nnested:\n  ignored: true\n"
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)
    monkeypatch.delenv("SOME_TOKEN", raising=False)

    # Force get_hermes_home() to re-resolve under the patched env.
    from importlib import reload

    import hermes_cli.config as _hc_config
    reload(_hc_config)

    send_cmd._load_hermes_env()

    assert os.environ.get("SOME_TOKEN") == "abc123"
    assert os.environ.get("TELEGRAM_HOME_CHANNEL") == "5550001111"


