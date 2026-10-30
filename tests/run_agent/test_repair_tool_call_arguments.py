"""Tests for _repair_tool_call_arguments — malformed JSON repair pipeline."""

import json

from run_agent import _repair_tool_call_arguments


class TestRepairToolCallArguments:
    """Verify each repair stage in the pipeline."""

    # -- Stage 1: empty / whitespace-only --

    def test_empty_string_returns_empty_object(self):
        assert _repair_tool_call_arguments("", "t") == "{}"



    # -- Stage 2: Python None literal --



    # -- Stage 3: trailing comma repair --


    def test_trailing_comma_in_array(self):
        result = _repair_tool_call_arguments('{"a": [1, 2,]}', "t")
        parsed = json.loads(result)
        assert parsed == {"a": [1, 2]}


    # -- Stage 4: unclosed brackets --



    # -- Stage 5: excess closing delimiters --



    # -- Stage 3b: truncation salvage (2026-10-30 empty-args spiral fix) --

    def test_truncated_string_value_is_salvaged(self):
        result = _repair_tool_call_arguments('{"truncated": "val', "t")
        assert json.loads(result) == {"truncated": "val"}

    def test_truncated_nested_payload_is_salvaged(self):
        raw = '{"name": "fabric_search", "arguments": {"query": "bea'
        result = _repair_tool_call_arguments(raw, "t")
        assert json.loads(result) == {
            "name": "fabric_search",
            "arguments": {"query": "bea"},
        }

    def test_dangling_key_trims_to_last_pair(self):
        result = _repair_tool_call_arguments('{"a": 1, "dangling"', "t")
        assert json.loads(result) == {"a": 1}

    def test_dangling_backslash_is_dropped(self):
        result = _repair_tool_call_arguments('{"a": "x\\', "t")
        assert json.loads(result) == {"a": "x"}

    # -- Stage 6: last resort --

    def test_unrepairable_junk_returns_empty_object(self):
        # No salvageable structure at all
        assert _repair_tool_call_arguments('}"', "t") == "{}"

    # -- Valid JSON passthrough (this path is via except, but still works) --


    # -- Combined repairs --



    # -- Stage 0: strict=False (literal control chars in strings) --
    # llama.cpp backends sometimes emit literal tabs/newlines inside JSON
    # string values. strict=False accepts these; we re-serialise to the
    # canonical wire form (#12068).




    # -- Stage 4: control-char escape fallback --


