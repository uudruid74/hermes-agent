"""Mirror role at the send_message call site (bug 2026-10-27-wake-mirror).

The visible-copy mirror of a delivery (``hermes send -u`` / wake copies) must
land in the target transcript as ``role="user"`` — the text is NOT the target
agent speaking.  The interactive ``send_message`` mirror (the agent's own
outgoing reply) stays ``role="assistant"``.  An assistant-role delivery twin
is indistinguishable from a real assistant turn after the SQLite boundary
drops the mirror flag (issue #2221) and poisons the model's own voice.
"""

from unittest.mock import patch


def _mirror_role_of(monkeypatch, message, mirror_kw):
    """Drive send_message_tool through the normal delivery path; return the
    role kwarg handed to mirror_to_session."""
    from gateway.config import Platform
    import tools.send_message_tool as smt

    fake_pconfig = type("P", (), {"enabled": True, "token": None, "extra": {}})()
    fake_config = type("C", (), {"platforms": {Platform.TELEGRAM: fake_pconfig}})()
    captured = {}

    def fake_mirror(platform, chat_id, text, **kw):
        captured.update(kw)
        return True

    async def fake_send_to_platform(*a, **kw):
        return {"success": True, "platform": "telegram", "chat_id": "12345"}

    with patch("gateway.config.load_gateway_config", lambda: fake_config), \
         patch("gateway.session_context.get_session_env", lambda *a, **kw: ""), \
         patch.object(smt, "_send_to_platform", fake_send_to_platform), \
         patch("gateway.mirror.mirror_to_session", fake_mirror), \
         patch("tools.interrupt.is_interrupted", lambda: False):
        smt.send_message_tool(
            {"action": "send", "target": "telegram:12345", "message": message},
            **mirror_kw,
        )
    return captured.get("role")


def test_delivery_mirror_writes_user_role(monkeypatch):
    """The CLI delivery path (hermes send -u / wake copies) mirrors as 'user'."""
    role = _mirror_role_of(
        monkeypatch, "ornith has completed step 8, task id t_test",
        {"mirror_role": "user"},
    )
    assert role == "user", f"delivery mirror must write role='user', got {role!r}"


def test_agent_reply_mirror_writes_assistant_role(monkeypatch):
    """Regression: interactive send_message mirror stays 'assistant'."""
    role = _mirror_role_of(
        monkeypatch, "here is my reply to the user", {},
    )
    assert role == "assistant", (
        f"agent-reply mirror must write role='assistant', got {role!r}"
    )
