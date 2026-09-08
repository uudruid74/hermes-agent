"""Cross-process preflight throttle for OpenAI Codex HTTP requests."""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from agent.rate_limit_tracker import parse_reset_duration_seconds
from agent.retry_utils import jittered_backoff, retry_delay_from_headers
from utils import atomic_json_write

logger = logging.getLogger(__name__)

_BASE_RAMP_TPM = 1_000_000.0
_RAMP_INTERVAL_SECONDS = 15 * 60.0
_RAMP_MULTIPLIER = 1.5
_TOKEN_WINDOW_SECONDS = 60.0
_HOOK_MARKER = "_hermes_openai_rate_limit_throttle"


def _default_state_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "rate_limits" / "openai.json"


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Hold a small cross-process lock around one state read/modify/write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            logger.warning("OpenAI throttle lock release failed: %s", exc)
        handle.close()


def _header_value(headers: Mapping[str, Any], key: str) -> Any:
    lowered = {str(name).lower(): value for name, value in headers.items()}
    return lowered.get(key.lower())


def _safe_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


class OpenAIRateLimitThrottle:
    """Reserve token capacity before Codex requests across Hermes processes."""

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        interrupt_check: Callable[[], bool] | None = None,
        status_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.state_path = state_path or _default_state_path()
        self.lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        self._clock = clock
        self._sleep = sleep
        self._interrupt_check = interrupt_check or (lambda: False)
        self._status_callback = status_callback

    def _load_state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Ignoring unreadable OpenAI throttle state %s: %s", self.state_path, exc
            )
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save_state(self, state: dict[str, Any]) -> None:
        atomic_json_write(self.state_path, state)

    def _apply_elapsed_time(self, state: dict[str, Any], now: float) -> None:
        reservations = state.get("reservations")
        if not isinstance(reservations, list):
            reservations = []
        state["reservations"] = [
            item
            for item in reservations
            if isinstance(item, dict)
            and isinstance(item.get("at"), (int, float))
            and now - float(item["at"]) < _TOKEN_WINDOW_SECONDS
        ]

        reset_at = float(state.get("reset_at", 0.0) or 0.0)
        if reset_at and now >= reset_at:
            limit = int(state.get("limit_tokens", 0) or 0)
            state["remaining_tokens"] = limit
            state["reset_at"] = 0.0

        ramp_tpm = float(state.get("ramp_tpm", _BASE_RAMP_TPM) or _BASE_RAMP_TPM)
        last_request_at = float(state.get("last_request_at", 0.0) or 0.0)
        ramp_updated_at = float(state.get("ramp_updated_at", now) or now)
        if last_request_at and now - last_request_at > _TOKEN_WINDOW_SECONDS:
            ramp_tpm = _BASE_RAMP_TPM
            ramp_updated_at = now
        elif last_request_at and now - ramp_updated_at >= _RAMP_INTERVAL_SECONDS:
            intervals = int((now - ramp_updated_at) // _RAMP_INTERVAL_SECONDS)
            ramp_tpm *= _RAMP_MULTIPLIER**intervals
            server_limit = int(state.get("limit_tokens", 0) or 0)
            if server_limit > 0:
                ramp_tpm = min(ramp_tpm, float(server_limit))
            ramp_updated_at += intervals * _RAMP_INTERVAL_SECONDS
        state["ramp_tpm"] = ramp_tpm
        state["ramp_updated_at"] = ramp_updated_at

    def reserve(self, estimated_tokens: int) -> float:
        """Reserve a request's estimated input tokens; return required delay."""
        tokens = max(1, int(estimated_tokens))
        now = self._clock()
        with _exclusive_lock(self.lock_path):
            state = self._load_state()
            self._apply_elapsed_time(state, now)

            blocked_until = float(state.get("blocked_until", 0.0) or 0.0)
            if blocked_until > now:
                self._save_state(state)
                return blocked_until - now

            remaining = state.get("remaining_tokens")
            reset_at = float(state.get("reset_at", 0.0) or 0.0)
            if isinstance(remaining, int) and tokens > remaining and reset_at > now:
                self._save_state(state)
                return reset_at - now

            ramp_tpm = float(state["ramp_tpm"])
            reservations = state["reservations"]
            reserved_tokens = sum(
                int(item.get("tokens", 0) or 0) for item in reservations
            )
            if reservations and reserved_tokens + min(tokens, int(ramp_tpm)) > ramp_tpm:
                earliest = min(float(item["at"]) for item in reservations)
                self._save_state(state)
                return max(0.0, earliest + _TOKEN_WINDOW_SECONDS - now)

            reservations.append({"at": now, "tokens": min(tokens, int(ramp_tpm))})
            state["last_request_at"] = now
            if isinstance(remaining, int):
                state["remaining_tokens"] = max(0, remaining - tokens)
            self._save_state(state)
        return 0.0

    def before_request(self, request: Any) -> None:
        """httpx request hook: wait until shared token capacity is available."""
        if str(getattr(request, "method", "")).upper() != "POST":
            return
        url = str(getattr(request, "url", ""))
        if not (url.rstrip("/").endswith("/responses") or "/chat/completions" in url):
            return
        content = getattr(request, "content", b"") or b""
        size = (
            len(content.encode("utf-8")) if isinstance(content, str) else len(content)
        )
        estimated_tokens = max(1, size // 4)

        while True:
            delay = self.reserve(estimated_tokens)
            if delay <= 0:
                return
            message = (
                f"OpenAI token throttle active — waiting {delay:.1f}s before request"
            )
            logger.warning(message)
            if self._status_callback is not None:
                self._status_callback(message)
            remaining = delay
            while remaining > 0:
                if self._interrupt_check():
                    raise InterruptedError(
                        "Agent interrupted during OpenAI token throttle"
                    )
                step = min(0.2, remaining)
                self._sleep(step)
                remaining -= step

    def after_response(self, response: Any) -> str | None:
        """httpx response hook: update shared capacity and classify status."""
        status = int(getattr(response, "status_code", 0) or 0)
        headers = getattr(response, "headers", None) or {}
        now = self._clock()

        if status == 402:
            logger.error(
                "OpenAI billing/quota exhaustion (HTTP 402); not waiting or retrying"
            )
            return "billing_or_quota"
        if status == 503:
            logger.warning(
                "OpenAI model overloaded (HTTP 503 server_is_overloaded); using retry backoff only"
            )
            return "server_is_overloaded"
        if status == 429:
            default_wait = jittered_backoff(1, base_delay=2.0, max_delay=60.0)
            delay = retry_delay_from_headers(
                headers,
                default_wait=default_wait,
            )
            with _exclusive_lock(self.lock_path):
                state = self._load_state()
                self._apply_elapsed_time(state, now)
                ramp_tpm = float(
                    state.get("ramp_tpm", _BASE_RAMP_TPM) or _BASE_RAMP_TPM
                )
                server_limit = int(state.get("limit_tokens", 0) or 0)
                backed_down = ramp_tpm / 2.0
                if server_limit > 0:
                    backed_down = min(backed_down, float(server_limit))
                state["ramp_tpm"] = max(1.0, backed_down)
                state["ramp_updated_at"] = now
                state["blocked_until"] = max(
                    float(state.get("blocked_until", 0.0) or 0.0),
                    now + delay,
                )
                self._save_state(state)
            logger.warning(
                "OpenAI slow_down (HTTP 429); shared request rate backed down for %.1fs",
                delay,
            )
            return "slow_down"

        limit = _safe_int(_header_value(headers, "x-ratelimit-limit-tokens"))
        remaining = _safe_int(_header_value(headers, "x-ratelimit-remaining-tokens"))
        reset = parse_reset_duration_seconds(
            _header_value(headers, "x-ratelimit-reset-tokens")
        )
        project_limit = _safe_int(
            _header_value(headers, "x-ratelimit-limit-project-tokens")
        )
        project_remaining = _safe_int(
            _header_value(headers, "x-ratelimit-remaining-project-tokens")
        )
        project_reset = parse_reset_duration_seconds(
            _header_value(headers, "x-ratelimit-reset-project-tokens")
        )
        candidates: list[tuple[int, int, float]] = []
        if limit is not None and remaining is not None and reset is not None:
            candidates.append((limit, remaining, reset))
        if (
            project_limit is not None
            and project_remaining is not None
            and project_reset is not None
        ):
            candidates.append((project_limit, project_remaining, project_reset))
        if not candidates:
            return None
        tightest = min(candidates, key=lambda item: item[1])
        with _exclusive_lock(self.lock_path):
            state = self._load_state()
            self._apply_elapsed_time(state, now)
            state["limit_tokens"] = int(tightest[0])
            state["remaining_tokens"] = int(tightest[1])
            state["reset_at"] = now + float(tightest[2])
            state["captured_at"] = now
            state["ramp_tpm"] = min(
                float(state.get("ramp_tpm", _BASE_RAMP_TPM) or _BASE_RAMP_TPM),
                float(tightest[0]),
            )
            self._save_state(state)
        return None


def install_openai_rate_limit_throttle(
    http_client: Any, agent: Any
) -> OpenAIRateLimitThrottle | None:
    """Attach one Codex throttle to an httpx client without duplicating hooks."""
    if http_client is None:
        return None
    existing = getattr(http_client, _HOOK_MARKER, None)
    if isinstance(existing, OpenAIRateLimitThrottle):
        return existing
    event_hooks = getattr(http_client, "event_hooks", None)
    if not isinstance(event_hooks, dict):
        logger.warning(
            "OpenAI Codex HTTP client has no mutable event_hooks; proactive throttle unavailable"
        )
        return None

    throttle = OpenAIRateLimitThrottle(
        interrupt_check=lambda: bool(getattr(agent, "_interrupt_requested", False)),
        status_callback=getattr(agent, "_emit_status", None),
    )
    event_hooks.setdefault("request", []).append(throttle.before_request)
    event_hooks.setdefault("response", []).append(throttle.after_response)
    setattr(http_client, _HOOK_MARKER, throttle)
    return throttle
