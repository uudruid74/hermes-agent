# 413 Fix: byte-aware request pressure

Fix the byte-blindness that lets image-heavy sessions blow past the provider's request-body
cap and then mis-recover. **Fix the unit, not the budget.**

SOURCES OF TRUTH: `agent/context_compressor.py`, `agent/conversation_loop.py`,
`agent/model_metadata.py` source at HEAD (verified this session); Evan brief 2026-11-27
relayed via Neo + Zephyr cross-check. **Source wins on conflict** with the brief.

GROUND RULES
- OUT OF SCOPE: relaxing the `internal_only max=1` single-pass law; injected-block dedup
  (measured 106 KB — not the issue). Do not touch either.
- Any new user-facing API/config surface needs Evan approval before it ships.
- Simplest possible implementation wins. No speculative generality.

## Add a serialized-bytes estimator alongside the token estimator

**LAW: the token currency budgets compaction. The byte currency predicts the 413. They are
different units and MUST NOT be merged.**

Why not "just price images by base64 length": a ~1.1 MB base64 data URL is ~1.1M chars.
`_IMAGE_TOKEN_ESTIMATE = 1600` (`agent/context_compressor.py:633`) prices it at 1,600
tokens; `estimate_tokens_rough` (`agent/model_metadata.py:3059`, `(len(text)+3)//4`)
would price it at ~275k. Neither is the provider's true pixel-based cost — but the REQUEST
BODY carries the full base64 string verbatim, so **serialized bytes is exactly the unit the
413 is measured in.** Re-pricing images in the *token* estimator would make `should_compress`
fire constantly on image-heavy sessions and summarise far too early — a worse regression
than the bug being fixed.

DELIVERABLE: add `estimate_messages_bytes_rough(messages)` and
`estimate_request_bytes_rough(api_messages, tools=None)` in `agent/model_metadata.py` as
siblings of the existing token estimators, sharing their call shape so existing call sites
(`agent/conversation_loop.py:5034`/`:5040`/`:5063`, `cli.py:11301`, `tui_gateway/*`) can
switch with a one-line import change. Sum the JSON-serialized size of the message array;
count image parts by their real `data:` URL length, not a flat constant.

CHECK the reuse lead before writing new code: `agent/conversation_loop.py:1758` says
`api_content` is "the persistence sidecar carrying the exact bytes" — if a byte-accurate
serialized form already exists on the message, use it rather than re-serializing.

ACCEPTANCE: on a fixture carrying 4 base64 image tool rows, the new byte estimators return
a value within 5% of `len(json.dumps(api_messages))`. The token estimators are **unchanged**
— byte-identical output on the existing corpus (`tests/agent/test_compressor_image_tokens.py`
must pass with zero edits). Evidence: worker's documented run of those two test modules.

## Add a per-provider request cap and a preflight body guard

**LAW: no hardcoded cap anywhere. The cap is per-provider configuration, and unset means
disabled.**

DELIVERABLE:
1. Config key for the provider's request-body cap, read alongside the existing provider
   config. *Veto-able default:* `providers.<name>.max_request_bytes`, integer bytes,
   **default unset = no cap = behaviour identical to today.** Hold-and-ask if the config
   schema you find has a clearly better existing home for a per-provider numeric limit —
   `gateway/platforms/webhook.py:231` (`extra.get("max_body_bytes", ...)`) is the in-tree
   precedent for the name/shape, but that is an *inbound webhook* limit and must not be
   confused with this outbound API-body cap.
2. Preflight guard immediately before the provider call, after `api_messages` is assembled
   (`agent/conversation_loop.py:1754-1880`). If `estimate_request_bytes_rough(...)` exceeds
   the configured cap, do NOT send. Instead downgrade the oldest image-bearing tool messages
   via the existing `_try_strip_image_parts_from_tool_messages`
   (`run_agent.py:6637`, already wired at `agent/conversation_loop.py:4280` and `:5075`),
   then re-measure and send. Loop at most until no image-bearing tool messages remain, then
   send anyway and let the provider decide.

ACCEPTANCE: a request whose serialized bytes exceed a configured cap is downgraded **before**
the API call is attempted — the mock provider must see zero 413s in the test. With the cap
unset, the request goes out unchanged. Evidence: worker's documented run of the new test in
`tests/run_agent/test_413_compression.py`.

## Make the compression trigger two-currency

**LAW: compression pre-fires when EITHER currency crosses its threshold.**

DELIVERABLE: `ContextCompressor.should_compress` / `should_compress_info`
(`agent/context_compressor.py:2639`/`:2661`) gain a byte-pressure term: when serialized
bytes exceed the configured cap, return `(True, "bytes:<n>")` even when
`tokens < self.threshold_tokens` (:2699). This is what stops the 413 arriving "as a
surprise" — the compressor now sees an image-heavy window as heavy. The existing
anti-thrashing and `_automatic_compression_blocked()` guards (:2701) continue to apply to
the byte path too; do not let the byte trigger bypass them.

ACCEPTANCE: the 4-image fixture that today reports "~75k tokens" (well under threshold) now
returns `should_compress() == True` with a `bytes:` reason. A text-only window at the same
token count still returns `False`. Evidence: worker's documented run of
`tests/agent/test_context_compressor.py`.

## Make the 413-recovery success check compare serialized bytes

**LAW: for a 413, the only meaningful success criterion is whether the serialized body
dropped under the cap. Token deltas are not evidence.**

Today's check (`agent/conversation_loop.py:5066`) is
`len(messages) < original_len or (new_tokens > 0 and new_tokens < original_tokens * 0.95)`.
This misjudges in the dangerous direction: general compression can drop text while leaving
every image attached, which satisfies the token test, retries with the images still present,
and collects another 413 — burning an attempt against work that never changed.

DELIVERABLE: measure `original_bytes` / `new_bytes` with the step-1 estimator beside
`:5034`/`:5063`, and gate the retry decision on bytes falling below the cap. If bytes are
still over after compression, fall through to the existing strip path (`:5075`) in the
*same* attempt rather than retrying first. Keep the `compression_attempts = 0` reset at
`:5092` exactly as it is — that is the `internal_only max=1` survival fix and must not move.

ACCEPTANCE: a 413 caused by 4 image rows recovers in ONE pass (compression no-ops → strip
→ retry succeeds), not two. Evidence: worker's documented run of
`tests/run_agent/test_413_strip_retry_budget_reset.py` plus the new step-5 test.

## Regression test pinning the defect shape, then full-suite verification

**LAW: the test must fail on today's code. A test that passes before and after the fix
proves nothing.**

DELIVERABLE: a lockstep regression test in `tests/run_agent/test_413_compression.py` pinning
the defect shape itself — **a session with N image-bearing tool rows whose estimated TOKENS
sit far under threshold but whose serialized BYTES exceed the provider cap MUST trigger the
preflight guard.** Assert the mock provider receives a body under the cap and that image
parts were downgraded. Run it first against the pre-fix tree and confirm it FAILS, then land
the fix and confirm it passes.

*Mirror the harness style of the existing `tests/run_agent/test_413_compression.py`
(`_make_tool_defs`, `_mock_response`, `_make_413_error`) — not a new harness.*

VERIFICATION before handoff: full unit suite green; confirm
`tests/agent/test_compressor_media_stripping.py`,
`tests/run_agent/test_multimodal_tool_content_recovery.py`,
`tests/run_agent/test_413_strip_retry_budget_reset.py` all still pass (they are the closest
existing coverage and the likeliest to regress).

ACCEPTANCE: the new test demonstrably red-before / green-after, and the full suite is green.
Evidence: worker's documented before/after test output plus the suite run.
