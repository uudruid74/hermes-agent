"""Regression tests for profile-scoped gateway bridge sockets."""

from pathlib import Path

from gateway.mcp_bridge import bridge_socket_path


def test_bridge_socket_path_is_scoped_to_profile_home():
    gopher = bridge_socket_path(Path("/tmp/hermes-home/profiles/gopher"))
    neo = bridge_socket_path(Path("/tmp/hermes-home/profiles/neo"))

    assert gopher == "/tmp/hermes/mcp_bridge.gopher.sock"
    assert neo == "/tmp/hermes/mcp_bridge.neo.sock"
    assert gopher != neo


def test_bridge_socket_path_names_overridden_root_home_as_default_profile(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/home/user/.hermes")

    assert bridge_socket_path() == "/tmp/hermes/mcp_bridge.default.sock"
