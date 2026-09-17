"""Multimodal messages in the verbatim tail must not crash compression.

Regression: a user image turn carries ``content`` as a LIST of parts
(``[{type:text}, {type:image_url}]``).  ``build_internal_fallback`` joined the
tail messages with a raw ``str.join``, which raises
``TypeError: sequence item N: expected str instance, list found`` the moment an
image lands inside the verbatim tail window -- killing the turn with
"unexpected error" (observed live 2026-09-17, gateway.log line 4661).

Both ``outside_tiers`` sites (plan path and lexrank path) now route through the
module's own ``_content_text`` helper, which already handled list content.
"""

from __future__ import annotations

import pytest

from agent.internal_compression_fallback import (
    _content_text,
    build_internal_fallback,
)

IMAGE_PART = {
    "type": "image_url",
    "image_url": {"url": "data:image/jpeg;base64," + ("A" * 64)},
}


def _text(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def _image(role: str = "user", caption: str = "look at this") -> dict:
    """The shape a real image turn takes: content is a list of parts."""
    return {
        "role": role,
        "content": [
            {"type": "text", "text": caption},
            IMAGE_PART,
        ],
    }


def _transcript() -> list[dict]:
    """An image turn sitting inside the verbatim tail window."""
    return [
        _text("system", "system prompt"),
        _text("user", "opening request"),
        _text("user", "old middle 0"),
        _text("assistant", "old middle 1"),
        _text("user", "old middle 2"),
        _text("assistant", "old middle 3"),
        _text("user", "recent question"),
        _image(),  # <-- the message that used to raise
    ]


PLAN = (
    "Task: Compression with an image in the tail\nStatus: manual\n"
    "Goal: survive multimodal content\nStep 2/3\n\n"
    "    Step 1: inspect\n  -> Step 2: implement\n"
    "      Summary: reproduction in place"
)


def test_content_text_handles_multimodal_parts():
    """The helper the joins must use extracts text and ignores the image part."""
    assert _content_text([{"type": "text", "text": "look at this"}, IMAGE_PART]) == (
        "look at this"
    )
    # Plain strings still pass through unchanged.
    assert _content_text("plain") == "plain"
    # Degenerate shapes must not raise.
    assert _content_text(None) == ""
    assert _content_text(123) == ""
    assert _content_text([]) == ""


def test_lexrank_path_survives_image_in_tail():
    """No plan active -> the lexrank path's outside_tiers join must not raise."""
    messages = _transcript()

    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=3,  # tail includes the image turn
        target_tokens=2_000,
    )

    assert fallback is not None
    assert fallback.tail_start <= len(messages) - 1


def test_plan_path_survives_image_in_tail():
    """Active plan -> the plan path's outside_tiers join must not raise."""
    messages = _transcript()

    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=3,
        target_tokens=4_000,
        plan_context=PLAN,
        minimal_plan_context="Goal: survive multimodal content\nCurrent step 2/3: implement",
    )

    assert fallback is not None
    assert fallback.tail_start <= len(messages) - 1


def test_raw_join_would_have_raised():
    """Pin the actual failure mode so the regression can't silently return.

    This asserts the OLD expression raises -- if a future refactor changes the
    message shape such that this stops raising, the guards above are still
    correct but this test tells us the hazard moved.
    """
    messages = _transcript()
    tail_start = 6
    with pytest.raises(TypeError, match="expected str instance, list found"):
        "\n".join(m.get("content") or "" for m in messages[tail_start:])


def test_image_before_tail_window_is_also_safe():
    """An image outside the tail is summarised by _message_units -- no raise."""
    messages = [
        _text("system", "system prompt"),
        _text("user", "opening request"),
        _image(caption="earlier image"),
        _text("assistant", "earlier reply"),
        _text("user", "middle 0"),
        _text("assistant", "middle 1"),
        _text("user", "recent question"),
        _text("assistant", "recent answer"),
    ]

    fallback = build_internal_fallback(
        messages,
        protect_head_count=2,
        protect_last_n=2,
        target_tokens=2_000,
    )

    assert fallback is not None
