"""Tests for the per-topic "last mentioned agent" sticky pointer.

Feature (t_445bc316 / bug 2026-10-05-telegram-topic-last-mentioned-agent):
after a group message mentions exactly one bot handle, that handle becomes the
topic's sticky target; subsequent un-mentioned messages in the topic route to
it when ``require_mention`` is on.  Messages naming two or more bots leave the
pointer unchanged.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageType
from plugins.platforms.telegram.topic_agents import (
    last_mentioned_agent_for,
    remember_last_mentioned_agent,
)


def _make_adapter(
    require_mention=None,
    free_response_chats=None,
    mention_patterns=None,
    exclusive_bot_mentions=None,
    allowed_topics=None,
    allowed_chats=None,
    guest_mode=None,
    bot_username="hermes_bot",
):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    extra = {
        # Pin gate behavior explicitly so ambient TELEGRAM_* env vars (which
        # third-party load_dotenv calls can leak mid-suite) cannot change
        # semantics — same pattern as allowed_topics/allowed_chats below.
        "free_response_chats": free_response_chats if free_response_chats is not None else [],
        "exclusive_bot_mentions": exclusive_bot_mentions if exclusive_bot_mentions is not None else True,
        "guest_mode": guest_mode if guest_mode is not None else False,
    }
    if require_mention is not None:
        extra["require_mention"] = require_mention
    if mention_patterns is not None:
        extra["mention_patterns"] = mention_patterns
    if allowed_topics is not None:
        extra["allowed_topics"] = allowed_topics
    else:
        extra["allowed_topics"] = []
    if allowed_chats is not None:
        extra["allowed_chats"] = allowed_chats
    else:
        extra["allowed_chats"] = []

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra=extra)
    adapter._bot = SimpleNamespace(id=999, username=bot_username)
    adapter._message_handler = AsyncMock()
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._text_batch_delay_seconds = 0.01
    adapter._text_batch_split_delay_seconds = 0.01
    adapter._mention_patterns = adapter._compile_mention_patterns()
    adapter._forum_lock = asyncio.Lock()
    adapter._forum_command_registered = set()
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._is_callback_user_authorized = lambda user_id, **_kw: True
    return adapter


def _group_message(
    text="hello",
    *,
    chat_id=-100,
    from_user_id=111,
    thread_id=None,
    reply_to_bot=False,
    entities=None,
    caption=None,
    caption_entities=None,
):
    reply_to_message = None
    if reply_to_bot:
        reply_to_message = SimpleNamespace(from_user=SimpleNamespace(id=999))
    return SimpleNamespace(
        message_id=42,
        text=text,
        caption=caption,
        entities=entities or [],
        caption_entities=caption_entities or [],
        message_thread_id=thread_id,
        is_topic_message=thread_id is not None,
        chat=SimpleNamespace(id=chat_id, type="group", title="Test Group", is_forum=thread_id is not None),
        from_user=SimpleNamespace(id=from_user_id, full_name="Alice Example", first_name="Alice"),
        reply_to_message=reply_to_message,
        date=None,
    )


def _mention_entity(text, mention="@hermes_bot"):
    offset = text.index(mention)
    return SimpleNamespace(type="mention", offset=offset, length=len(mention))


def _mention_entities(text, mentions):
    return [_mention_entity(text, mention) for mention in mentions]


def _bot_command_entity(text, command):
    offset = text.index(command)
    return SimpleNamespace(type="bot_command", offset=offset, length=len(command))


def test_single_mention_sets_pointer_and_unmentioned_message_routes():
    adapter = _make_adapter(require_mention=True, allowed_chats=["-100"])
    mention = _group_message("@hermes_bot hello", entities=[_mention_entity("@hermes_bot hello")])
    assert adapter._should_process_message(mention) is True
    assert last_mentioned_agent_for("-100", "1") == "hermes_bot"
    followup = _group_message("what's the status?")
    assert adapter._should_process_message(followup) is True


def test_two_mentions_leave_pointer_unchanged():
    adapter = _make_adapter(require_mention=True, allowed_chats=["-100"])
    first = _group_message("@hermes_bot hello", entities=[_mention_entity("@hermes_bot hello")])
    assert adapter._should_process_message(first) is True
    assert last_mentioned_agent_for("-100", "1") == "hermes_bot"

    two = _group_message(
        "@hermes_bot @otherbot hi",
        entities=_mention_entities("@hermes_bot @otherbot hi", ["@hermes_bot", "@otherbot"]),
    )
    # Mentions include self -> accepted; but pointer must NOT change.
    assert adapter._should_process_message(two) is True
    assert last_mentioned_agent_for("-100", "1") == "hermes_bot"

    followup = _group_message("what's the status?")
    assert adapter._should_process_message(followup) is True


def test_pointer_named_other_agent_is_ignored():
    adapter = _make_adapter(require_mention=True, allowed_chats=["-100"])
    other = _group_message("@otherbot hi", entities=[_mention_entity("@otherbot hi", "@otherbot")])
    # Exclusive mention routing: addressed to another bot, so dropped...
    assert adapter._should_process_message(other) is False
    # ...but the pointer still records the single mention.
    assert last_mentioned_agent_for("-100", "1") == "otherbot"
    followup = _group_message("what's the status?")
    assert adapter._should_process_message(followup) is False


def test_command_mention_does_not_update_pointer():
    adapter = _make_adapter(require_mention=True, allowed_chats=["-100"])
    cmd = _group_message("/cmd@otherbot", entities=[_bot_command_entity("/cmd@otherbot", "/cmd@otherbot")])
    assert adapter._should_process_message(cmd, is_command=True) is False
    assert last_mentioned_agent_for("-100", "1") is None
    followup = _group_message("what's the status?")
    assert adapter._should_process_message(followup) is False


def test_reply_to_bot_without_mention_does_not_update_pointer():
    adapter = _make_adapter(require_mention=True, allowed_chats=["-100"])
    remember_last_mentioned_agent("-100", "1", "hermes_bot")
    reply = _group_message("thanks!", reply_to_bot=True)
    assert adapter._should_process_message(reply) is True
    assert last_mentioned_agent_for("-100", "1") == "hermes_bot"


def test_pointer_is_scoped_per_topic():
    adapter = _make_adapter(require_mention=True, allowed_chats=["-100"])
    mention = _group_message(
        "@hermes_bot hello",
        thread_id=17,
        entities=[_mention_entity("@hermes_bot hello")],
    )
    assert adapter._should_process_message(mention) is True
    assert last_mentioned_agent_for("-100", "17") == "hermes_bot"
    assert last_mentioned_agent_for("-100", "1") is None
    assert adapter._should_process_message(_group_message("no mention", thread_id=17)) is True
    assert adapter._should_process_message(_group_message("no mention")) is False


def test_store_persists_across_adapter_instances():
    first = _make_adapter(require_mention=True, allowed_chats=["-100"])
    mention = _group_message("@hermes_bot hello", entities=[_mention_entity("@hermes_bot hello")])
    assert first._should_process_message(mention) is True

    second = _make_adapter(require_mention=True, allowed_chats=["-100"])
    followup = _group_message("what's the status?")
    assert second._should_process_message(followup) is True


def test_store_roundtrip_and_overwrite():
    assert last_mentioned_agent_for("-100", "1") is None
    remember_last_mentioned_agent("-100", "1", "zephyrdexbot")
    assert last_mentioned_agent_for("-100", "1") == "zephyrdexbot"
    remember_last_mentioned_agent("-100", "1", "neobot")
    assert last_mentioned_agent_for("-100", "1") == "neobot"
    assert last_mentioned_agent_for("-100", "2") is None
    assert last_mentioned_agent_for("-200", "1") is None


def test_unmentioned_command_still_respects_require_mention():
    adapter = _make_adapter(require_mention=True, allowed_chats=["-100"])
    mention = _group_message("@hermes_bot hello", entities=[_mention_entity("@hermes_bot hello")])
    assert adapter._should_process_message(mention) is True
    assert last_mentioned_agent_for("-100", "1") == "hermes_bot"
    # A slash command without a mention targets nobody — dropped, not routed
    # through the sticky pointer.
    assert adapter._should_process_message(_group_message("/status"), is_command=True) is False


def test_pointer_not_written_for_non_allowlisted_chat():
    adapter = _make_adapter(require_mention=True, allowed_chats=["-100"])
    mention = _group_message(
        "@hermes_bot hello",
        chat_id=-200,
        entities=[_mention_entity("@hermes_bot hello")],
    )
    # Hard allowlist gate drops the chat; the pointer must not be written
    # for a chat that can never consult it.
    assert adapter._should_process_message(mention) is False
    assert last_mentioned_agent_for("-200", "1") is None


def test_require_mention_off_and_free_response_unaffected():
    loose = _make_adapter(require_mention=False, allowed_chats=["-100"])
    assert loose._should_process_message(_group_message("no mention")) is True

    free = _make_adapter(require_mention=True, free_response_chats=["-100"])
    assert free._should_process_message(_group_message("no mention")) is True
