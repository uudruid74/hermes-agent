"""CLI subcommand: ``hermes send`` — pipe text from shell scripts to any
configured messaging platform (Telegram, Discord, Slack, Signal, SMS, etc.).

This is a thin wrapper around ``tools.send_message_tool.send_message_tool``
that exposes its functionality as a standalone CLI entry point so ops
scripts, cron jobs, CI hooks, and monitoring daemons can reuse the gateway's
already-configured credentials without having to reimplement each platform's
REST API client.

Design notes:

* No LLM, no agent loop — the subcommand just resolves arguments, reads the
  message body, calls the shared tool function, and prints/returns the
  result. It is intentionally fast, cheap, and side-effect-only.
* For platforms that send via bot token (Telegram, Discord, Slack, Signal,
  SMS, WhatsApp-CloudAPI, …) no running gateway is required. The tool
  talks directly to each platform's REST endpoint. For platforms that rely
  on a persistent adapter connection (plugin platforms, Matrix in some
  modes, …) a live gateway is needed; the underlying tool surfaces that
  error to the caller.
* Exit codes follow the classic Unix convention:
    0 — delivery (or list) succeeded
    1 — delivery failed at the platform level
    2 — usage / argument / config error (argparse already uses 2)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional


_USAGE_EXIT = 2
_FAILURE_EXIT = 1
_SUCESS_EXIT = 0


def _read_message_body(
    positional: Optional[str],
    file_path: Optional[str],
) -> Optional[str]:
    """Resolve the message body from (in order):

    1. An explicit positional message argument.
    2. ``--file PATH`` or ``--file -`` (where ``-`` means stdin).
    3. Piped stdin when it is not attached to a TTY.

    Returns ``None`` when nothing is available — callers must treat that as
    a usage error.
    """
    if positional:
        return positional

    if file_path:
        if file_path == "-":
            return sys.stdin.read()
        try:
            return Path(file_path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            print(
                f"hermes send: {file_path} is not a text file. --file reads the "
                "message *body* (logs, reports, markdown).\n"
                "To send an image/document/audio file as a native attachment, "
                "reference it with MEDIA: in the message text instead:\n"
                f'  hermes send --to telegram "MEDIA:{file_path}"\n'
                f'  hermes send --to telegram "optional caption MEDIA:{file_path}"\n'
                "Add [[as_document]] to deliver an image as an uncompressed file:\n"
                f'  hermes send --to telegram "[[as_document]] MEDIA:{file_path}"',
                file=sys.stderr,
            )
            sys.exit(_USAGE_EXIT)
        except OSError as exc:
            print(f"hermes send: cannot read {file_path}: {exc}", file=sys.stderr)
            sys.exit(_USAGE_EXIT)

    # Piped input: only consume stdin when it is not a TTY. Reading from a
    # TTY would block the user in a half-broken "type your message" state,
    # which is a poor default for an ops CLI.
    if not sys.stdin.isatty():
        data = sys.stdin.read()
        if data:
            return data

    return None


def _resolve_target(arg_to: Optional[str]) -> Optional[str]:
    """Return a cleaned ``--to`` value, or ``None`` when nothing is set."""
    if arg_to and arg_to.strip():
        return arg_to.strip()
    return None


def _select_user_target_profile(user_target: str) -> str:
    """Select a profile-qualified wake target and return its platform target."""
    profile, separator, target = user_target.partition(":")
    if not separator or not target:
        return user_target

    from hermes_cli.profiles import get_profile_dir, profile_exists

    try:
        if not profile_exists(profile):
            return user_target
        profile_home = get_profile_dir(profile)
    except ValueError:
        return user_target

    os.environ["HERMES_HOME"] = str(profile_home)
    os.environ["HERMES_PROFILE"] = profile
    return target


def _profile_home_channel_target(profile: str) -> str:
    """Resolve a profile's configured home channel to ``platform:chat[:thread]``.

    Reads ``platforms.telegram.home_channel`` (the ``HomeChannel`` dict
    ``/sethome`` persists) from the profile's own ``config.yaml``. No
    fallback: a profile without a configured chat resolves to its bare
    platform, which is ``hermes send``'s documented home-channel default
    shape — nothing here hardcodes a chat id.
    """
    import yaml

    from hermes_cli.profiles import get_profile_dir

    config_path = get_profile_dir(profile) / "config.yaml"
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raw = {}
    home = ((raw.get("platforms") or {}).get("telegram") or {}).get("home_channel") or {}
    return ":".join(
        part
        for part in (
            str(home.get("platform") or "telegram").strip() or "telegram",
            str(home.get("chat_id") or "").strip(),
            str(home.get("thread_id") or "").strip(),
        )
        if part
    )


def _resolve_agent_wake_target(user_flag: str) -> str:
    """Expand a bare profile name passed to ``-t`` into a profile-qualified
    wake target (agent wake).

    ``hermes send -t gopher "msg"`` is an AGENT WAKE — it resolves gopher →
    ``gopher:<platform>:<home channel>`` and routes through that profile's
    gateway bridge exactly like the old ``tell`` tool did. The home channel
    comes from the profile's own config.yaml (see
    ``_profile_home_channel_target``). Full targets (with colons) and
    platform names (``telegram``, ``discord``…) pass through untouched, so
    existing wake-target shapes are preserved. Profile names never collide
    with platform names. The same expansion also names the delivery target
    for the mandatory ``-u`` copy of a wake (see ``_wake_copy_target``).
    """
    clean = user_flag.strip()
    if ":" in clean:
        return clean
    from hermes_cli.profiles import profile_exists

    if profile_exists(clean):
        return f"{clean}:{_profile_home_channel_target(clean)}"
    return clean


def _wake_copy_target(wake_target: str) -> Optional[str]:
    """Delivery target for the mandatory ``-u`` copy of a ``-t`` wake.

    The copy must land in a HUMAN-VISIBLE chat under the same profile's
    credentials (the visibility doctrine: no secret agent-to-agent traffic).
    A ``cli:`` session wake has no chat of its own, so its copy goes to the
    owning profile's home channel; every other wake already names its chat
    and the copy posts to the same one. ``_resolve_agent_wake_target`` is
    the single source of truth for every shape (it profile-qualifies the
    target so the spawned ``hermes send -u`` delivers with the target
    profile's credentials). ``None`` means no resolvable home channel
    exists — the caller must report that, not bury it.
    """
    clean = wake_target.strip()
    if clean.lower().startswith("cli:"):
        from hermes_cli.profiles import get_active_profile_name, profile_exists

        active = get_active_profile_name()
        return _resolve_agent_wake_target(active) if profile_exists(active) else None
    if ":cli:" in clean.lower():
        return _resolve_agent_wake_target(clean.split(":", 1)[0])
    return _resolve_agent_wake_target(clean)


def _list_profile_sessions() -> list[dict]:
    """Return active gateway sessions across profiles as routable wake targets."""
    from hermes_cli.profiles import list_profiles
    from hermes_state import SessionDB

    targets = []
    for profile in list_profiles():
        db_path = profile.path / "state.db"
        if not db_path.exists():
            continue

        session_db = SessionDB(db_path=db_path)
        try:
            rows = session_db.list_gateway_sessions(active_only=True)
        finally:
            session_db.close()

        for row in rows:
            origin = {}
            origin_json = row.get("origin_json")
            if origin_json:
                try:
                    parsed = json.loads(origin_json)
                except (TypeError, ValueError):
                    parsed = None
                if isinstance(parsed, dict):
                    origin = parsed

            platform = str(origin.get("platform") or row.get("source") or "").lower()
            chat_id = origin.get("chat_id") or row.get("chat_id")
            if not platform or chat_id in (None, ""):
                continue
            thread_id = origin.get("thread_id") or row.get("thread_id")
            target = f"{profile.name}:{platform}:{chat_id}"
            if thread_id:
                target = f"{target}:{thread_id}"
            targets.append({
                "profile": profile.name,
                "session_key": row.get("session_key") or "",
                "target": target,
                "display_name": (
                    row.get("display_name")
                    or origin.get("chat_name")
                    or origin.get("user_name")
                    or str(chat_id)
                ),
                "bridge_socket": f"/tmp/hermes/mcp_bridge.{profile.name}.sock",
                "platform": platform,
            })
    return targets


def _with_copy_error(result_json: str, copy_error: Optional[str]) -> str:
    """Surface a failed mandatory ``-u`` copy inside the wake's result payload.

    The wake result stays the primary outcome; the copy is doctrine, so its
    failure is reported alongside rather than replacing or hiding the wake.
    """
    if not copy_error:
        return result_json
    payload = json.loads(result_json) if result_json else {}
    payload["copy_error"] = copy_error
    return json.dumps(payload)


def _emit_result(
    result_json: str,
    *,
    json_mode: bool,
    quiet: bool,
) -> int:
    """Print the tool result in the requested format and return the exit code.

    The underlying ``send_message_tool`` always returns a JSON string. We
    parse it, decide success/failure, and format accordingly.
    """
    try:
        payload = json.loads(result_json) if result_json else {}
    except json.JSONDecodeError:
        # Shouldn't happen with the shared tool, but be defensive — pass the
        # raw string through so the user can still see what went wrong.
        payload = {"error": "invalid JSON from send_message_tool", "raw": result_json}

    if json_mode:
        print(json.dumps(payload, indent=2))
    elif quiet:
        pass
    else:
        if payload.get("error"):
            print(f"hermes send: {payload['error']}", file=sys.stderr)
        elif payload.get("success"):
            note = payload.get("note")
            if note:
                print(note)
            else:
                print("sent")
        else:
            # Unknown shape — dump it so nothing is silently dropped.
            print(json.dumps(payload, indent=2))

    if payload.get("error"):
        return _FAILURE_EXIT
    if payload.get("skipped"):
        return _SUCESS_EXIT
    if payload.get("success"):
        return _SUCESS_EXIT
    # Unknown / unexpected — treat as failure so scripts notice.
    return _FAILURE_EXIT


def _list_targets(platform_filter: Optional[str], *, json_mode: bool) -> int:
    """Print channel targets plus profile-qualified active agent sessions."""
    try:
        from gateway.channel_directory import (
            format_directory_for_display,
            load_directory,
        )
    except Exception as exc:
        print(f"hermes send: failed to load channel directory: {exc}", file=sys.stderr)
        return _FAILURE_EXIT

    try:
        raw = load_directory()
        sessions = _list_profile_sessions()
        from gateway.config import load_gateway_config

        gateway_config = load_gateway_config()
    except Exception as exc:
        print(f"hermes send: failed to read targets: {exc}", file=sys.stderr)
        return _FAILURE_EXIT

    platforms = dict(raw.get("platforms") or {})
    for configured_platform in gateway_config.get_connected_platforms():
        platform_name = configured_platform.value
        if platform_name not in {"local", "api_server", "webhook"}:
            platforms.setdefault(platform_name, [])
    all_platforms = set(platforms)
    all_platforms.update(session["platform"] for session in sessions)

    if platform_filter:
        key = platform_filter.strip().lower()
        platforms = {k: v for k, v in platforms.items() if k.lower() == key}
        sessions = [session for session in sessions if session["platform"] == key]
        if not platforms and not sessions:
            print(
                f"hermes send: no targets found for platform '{platform_filter}'. "
                f"Configured: {', '.join(sorted(all_platforms)) or '(none)'}",
                file=sys.stderr,
            )
            return _FAILURE_EXIT

    if json_mode:
        print(json.dumps(
            {"platforms": platforms, "sessions": sessions},
            indent=2,
            default=str,
        ))
        return _SUCESS_EXIT

    if not platforms and not sessions:
        print("No messaging platforms configured or no channels discovered yet.")
        print("Set one up with `hermes gateway setup`, or run the gateway once so")
        print("channel discovery can populate ~/.hermes/channel_directory.json.")
        return _SUCESS_EXIT

    if platform_filter is None:
        channel_display = format_directory_for_display(platforms)
        if channel_display:
            print(channel_display)
    else:
        for plat_name in sorted(platforms):
            channels = platforms[plat_name]
            print(f"{plat_name}:")
            if not channels:
                print("  (no channels discovered yet)")
                continue
            for ch in channels:
                target_id = ch.get("id", "?")
                name = ch.get("name", "?")
                print(f"  {plat_name}:{target_id} — {name}")
            print()

    if sessions:
        print("Agent sessions (-t):")
        for session in sessions:
            print(f"  {session['target']} — {session['display_name']}")
            print(f"    session: {session['session_key']}")
            print(f"    profile: {session['profile']}")
            print(f"    bridge: {session['bridge_socket']}")

    return _SUCESS_EXIT


def _load_hermes_env() -> None:
    """Populate ``os.environ`` from ``~/.hermes/.env`` AND bridge top-level
    ``config.yaml`` keys into the environment so the underlying gateway
    config loader sees platform credentials and home channel IDs.

    ``send_message_tool`` reads tokens and home-channel IDs via
    ``os.getenv(...)`` on each call. The gateway process does two things at
    startup that ``hermes send`` must replicate when invoked standalone:

    1. ``load_dotenv(~/.hermes/.env)`` — brings bot tokens into the env.
    2. Bridge top-level simple values from ``~/.hermes/config.yaml`` into
       ``os.environ`` (without overriding existing env vars). This is where
       ``TELEGRAM_HOME_CHANNEL`` and friends live when the user saved them
       via ``hermes config set``.

    See ``gateway/run.py`` for the canonical version of this bridge — we
    intentionally reimplement the minimum needed here so ``hermes send``
    doesn't pull in the full gateway module just to resolve a home channel.
    """
    # Step 1: dotenv
    try:
        from dotenv import load_dotenv
    except Exception:
        load_dotenv = None  # type: ignore[assignment]

    try:
        from hermes_cli.config import get_hermes_home
        home = get_hermes_home()
    except Exception:
        return

    env_path = home / ".env"
    if load_dotenv and env_path.exists():
        try:
            load_dotenv(str(env_path), override=True, encoding="utf-8")
        except UnicodeDecodeError:
            try:
                load_dotenv(str(env_path), override=True, encoding="latin-1")
            except Exception:
                pass
        except Exception:
            pass

    # Step 2: bridge top-level config.yaml values into the environment so
    # gateway.config.load_gateway_config() sees them. Scalars only; don't
    # override values already in the env.
    import os
    config_path = home / "config.yaml"
    if not config_path.exists():
        return

    try:
        import yaml  # type: ignore[import-not-found]
    except Exception:
        return

    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except Exception:
        return

    try:
        from hermes_cli.config import _expand_env_vars
        raw = _expand_env_vars(raw)
    except Exception:
        pass

    # Managed scope: overlay administrator-pinned values before bridging to env,
    # so a managed top-level scalar wins here too. Fail-open via the helper.
    try:
        from hermes_cli import managed_scope
        raw = managed_scope.apply_managed_overlay(raw if isinstance(raw, dict) else {})
    except Exception:
        pass

    if not isinstance(raw, dict):
        return

    for key, val in raw.items():
        if not isinstance(val, (str, int, float, bool)):
            continue
        if key in os.environ:
            continue
        os.environ[key] = str(val)

    # Step 3: derive session identity from TELEGRAM_ALLOWED_USERS so that
    # ``hermes send -t`` carries the real user identity even when called from
    # a subprocess (kanban hook, cron job) that lacks HERMES_SESSION_* env vars.
    # Without this, _handle_send falls back to user_id="cli" and the auth gate
    # in _is_user_authorized() drops the message.
    telegram_id = os.environ.get("HERMES_SESSION_TELEGRAM_ID") or os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")[0].strip()
    if telegram_id:
        os.environ.setdefault("HERMES_SESSION_TELEGRAM_ID", telegram_id)
        os.environ.setdefault("HERMES_SESSION_USER_ID", telegram_id)
        os.environ.setdefault("HERMES_SESSION_USER_NAME", "CLI User")


def cmd_send(args: argparse.Namespace) -> None:
    """Entry point wired into the top-level argparse dispatcher."""

    target = _resolve_target(getattr(args, "to", None))
    wake_raw = _resolve_target(getattr(args, "user", None))
    user_flag = wake_raw
    if user_flag:
        user_flag = _resolve_agent_wake_target(user_flag)
        user_flag = _select_user_target_profile(user_flag)
    elif target:
        # Profile-qualified `-u` targets (`zephyr:telegram:chat`) deliver
        # through the TARGET profile's credentials/home, same as `-t`.
        target = _select_user_target_profile(target)

    # Bridge ~/.hermes/.env and ~/.hermes/config.yaml into os.environ so the
    # gateway config loader (invoked downstream by send_message_tool and by
    # the channel directory) can see platform credentials and home channels.
    _load_hermes_env()

    # --list short-circuits everything else.
    if getattr(args, "list_targets", False):
        # When `--list telegram` is used, argparse stores "telegram" in the
        # `message` positional (since list_targets takes no argument).
        platform_filter = getattr(args, "message", None)
        exit_code = _list_targets(platform_filter, json_mode=getattr(args, "json", False))
        sys.exit(exit_code)

    if not target and not user_flag:
        print(
            "hermes send: -u/--to PLATFORM[:channel[:thread]] (system→user "
            "delivery) or -t/--user <wake-target> (from-user wake) is required\n"
            "Examples:\n"
            "  hermes send -u telegram \"hello\"\n"
            "  hermes send -u discord:#ops --file report.md\n"
            "  hermes send -t gopher \"agent wake event\"\n"
            "  hermes send -t telegram:-1001234567890:17585 \"cmd\"\n"
            "  hermes send -t cli:session_id \"wake the agent with this\"\n"
            "  hermes send --list      # list available targets",
            file=sys.stderr,
        )
        sys.exit(_USAGE_EXIT)

    message = _read_message_body(
        getattr(args, "message", None),
        getattr(args, "file", None),
    )
    if message is None or not message.strip():
        print(
            "hermes send: no message provided. Pass text as a positional "
            "argument, use --file PATH, or pipe data via stdin.",
            file=sys.stderr,
        )
        sys.exit(_USAGE_EXIT)

    # Optional: prepend a subject line. Useful for alerting scripts that
    # want a consistent header without inlining it into every call.
    subject = getattr(args, "subject", None)
    if subject:
        message = f"{subject}\n\n{message.lstrip()}"

    # One-call rule (Evan spec 2026-10-18): a `-t` wake ALWAYS also sends the
    # visible copy down the `-u` path — callers make ONE call and get both the
    # wake and the human-readable post. The copy is spawned FIRST so an
    # interrupted process can never leave a wake with no visible trace.
    copy_error = None
    if wake_raw:
        try:
            copy_target = _wake_copy_target(wake_raw)
        except ValueError as exc:
            copy_target = None
            copy_error = f"no visible copy target resolves for wake {wake_raw!r}: {exc}"
        if copy_target is None:
            copy_error = copy_error or f"no visible copy target resolves for wake {wake_raw!r}"
        else:
            from hermes_cli._subprocess_compat import spawn_detached

            copy_proc = spawn_detached(
                ["hermes", "send", "-u", copy_target, message]
            )
            if copy_proc is None:
                copy_error = (
                    "failed to launch visible copy 'hermes send -u' for "
                    f"{copy_target}"
                )
        if copy_error:
            print(f"hermes send: {copy_error}", file=sys.stderr)

    # Import lazily so `hermes send --help` stays fast and does not pull in
    # the full tool registry / gateway config stack.
    if user_flag and user_flag.lower().startswith("cli:"):
        session_id = user_flag[4:].strip()
        if not session_id:
            print("hermes send: cli target requires cli:<session_id>", file=sys.stderr)
            sys.exit(_USAGE_EXIT)
        from hermes_state import SessionDB
        from hermes_constants import get_hermes_home

        session_db = SessionDB(db_path=get_hermes_home() / "state.db")
        try:
            queued = session_db.enqueue_session_notice(session_id, message)
            # Signal half of the wake (t_8140688e): look up the live CLI
            # process for this session and SIGUSR1 it so the queued message is
            # injected as a real user turn instead of sitting in the queue.
            # The heartbeat check handles stale/gone sessions.
            pid = session_db.get_cli_wake_pid(session_id)
        finally:
            session_db.close()

        if not queued:
            result = json.dumps({"error": f"CLI session not found: {session_id}"})
        elif pid is None:
            # Message is queued but no live CLI to wake — surface it so the
            # caller knows the wake may happen later (or never) rather than
            # silently pretending it was delivered.
            result = json.dumps({
                "success": True,
                "note": f"queued for CLI session {session_id} (no live process to wake)",
                "queued": True,
                "woken": False,
            })
        else:
            import signal as _sig

            _woken = False
            try:
                os.kill(pid, _sig.SIGUSR1)  # windows-footgun: ok — POSIX signal, guarded by get_cli_wake_pid only finding live POSIX CLI registrations
                _woken = True
            except (ProcessLookupError, PermissionError, OSError):
                _woken = False
            result = json.dumps({
                "success": True,
                "note": f"queued for CLI session {session_id} and woken (pid {pid})"
                        if _woken else f"queued for CLI session {session_id} (signal failed)",
                "queued": True,
                "woken": _woken,
                "pid": pid,
            })
        sys.exit(_emit_result(
            _with_copy_error(result, copy_error),
            json_mode=getattr(args, "json", False),
            quiet=getattr(args, "quiet", False),
        ))

    from tools.send_message_tool import send_message_tool

    # send_message_tool auto-loads gateway config + env and routes to the
    # appropriate platform adapter (bot-token path for Telegram/Discord/Slack/
    # Signal/SMS/WhatsApp; live-adapter path for plugin platforms).
    #
    # ========================================================================
    # EVAN-LOCKED (flag semantics corrected per Evan's 2026-10-18 spec — the
    # t_9550c744 close that pinned this injection path to `-u` shipped the
    # INVERTED flags and is void; see outbox/tell-u-t-semantics-inversion):
    # `hermes send -t/--user` MUST CREATE AN INBOUND USER TURN (the wake).
    #
    # It is NOT an outbound send. It must retain the user's identity and route
    # through the gateway bridge when this CLI process has no local adapter.
    # `-u/--to` is plain system→user delivery and never injects. Do not alter
    # this path, add a platform-delivery fallback, or reinterpret
    # `internal=True` without Evan's explicit prior approval.
    # ========================================================================
    tool_args: dict = {
        "action": "send",
        "message": message,
    }
    if user_flag:
        tool_args["target"] = user_flag
        tool_args["internal"] = True
    else:
        tool_args["target"] = target

    result = _with_copy_error(send_message_tool(tool_args), copy_error)
    exit_code = _emit_result(
        result,
        json_mode=getattr(args, "json", False),
        quiet=getattr(args, "quiet", False),
    )
    sys.exit(exit_code)


def register_send_subparser(subparsers) -> argparse.ArgumentParser:
    """Create the ``send`` subparser and return it.

    Kept as a standalone function so the top-level parser builder can wire
    it in next to the other messaging subcommands without cluttering
    ``_parser.py`` or ``main.py``.
    """
    parser = subparsers.add_parser(
        "send",
        help="Send a message to a configured platform (scripts, cron jobs, CI).",
        description=(
            "Pipe text from any shell script to any messaging platform Hermes "
            "is already configured for. Reuses the gateway's platform "
            "credentials (~/.hermes/.env + ~/.hermes/config.yaml) — no LLM, "
            "no agent loop, no running gateway required for bot-token "
            "platforms like Telegram/Discord/Slack/Signal."
        ),
        epilog=(
            "Examples:\n"
            "  hermes send -u telegram \"deploy finished\"\n"
            "  echo \"RAM 92%\" | hermes send -u telegram:-1001234567890\n"
            "  hermes send -u discord:#ops --file /tmp/report.md\n"
            "  hermes send -u slack:#eng --subject \"[CI]\" --file build.log\n"
            "  hermes send -u telegram \"MEDIA:/tmp/chart.png\"   # send a media attachment\n"
            "  hermes send -t gopher \"hello\"                   # wake agent profile by name\n"
            "  hermes send -t cli:session_id \"wake the agent with this\"\n"
            "  hermes send -t telegram:-1001234567890:17585 \"wake\"\n"
            "  hermes send --list                  # all platforms\n"
            "  hermes send --list telegram         # filter by platform\n"
            "\n"
            "Exit codes: 0 ok, 1 delivery/backend error, 2 usage error."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # -u and -t are mutually exclusive — plain system→user delivery OR a
    # from-user wake event (which itself carries the visible -u copy).
    target_group = parser.add_mutually_exclusive_group()

    target_group.add_argument(
        "-u",
        "--to",
        metavar="TARGET",
        default=None,
        help=(
            "System -> user delivery (stock hermes, UNCHANGED). Posts the "
            "human-readable text to the user's chat: cron output, kanban "
            "notices, scripts. NEVER an agent wake, never a user-turn "
            "injection. Format: 'platform' (home "
            "channel), 'platform:chat_id', 'platform:chat_id:thread_id', or "
            "'platform:#channel-name'. Examples: telegram, "
            "telegram:-1001234567890:17585, discord:#ops, slack:C0123ABCD."
        ),
    )

    target_group.add_argument(
        "-t",
        "--user",
        metavar="ORIGIN",
        default=None,
        help=(
            "From-user wake (fork addition): injects the message AS A USER "
            "TURN so the target agent wakes and acts on it. One call always "
            "gives both halves — the wake AND a visible copy down the -u "
            "path. Format: "
            "'profile:platform:chat_id', 'platform' (home channel), "
            "'platform:chat_id', 'platform:chat_id:thread_id', or a BARE "
            "AGENT PROFILE NAME ('gopher', 'ornith', …) which is expanded to "
            "the agent's telegram DM wake target. Use "
            "'cli:session_id' to wake a live CLI session: queues the message "
            "and SIGUSR1s the target process so the agent acts on it as a real "
            "user turn. Examples: -t telegram, "
            "-t gopher, -t telegram:-1001234567890:17585, -t cli:session_id."
        ),
    )

    parser.add_argument(
        "message",
        nargs="?",
        default=None,
        help="Message text. If omitted, read from --file or stdin.",
    )

    parser.add_argument(
        "-f",
        "--file",
        metavar="PATH",
        default=None,
        help=(
            "Read message body from PATH (text only). Use '-' to force stdin. "
            "To send an image/document as an attachment, use MEDIA:<path> in "
            "the message text instead."
        ),
    )

    parser.add_argument(
        "-s",
        "--subject",
        metavar="LINE",
        default=None,
        help="Prepend a subject/header line before the message body.",
    )

    parser.add_argument(
        "-l",
        "--list",
        dest="list_targets",
        action="store_true",
        default=False,
        help="List available targets. Optional positional filter: `hermes send --list telegram`.",
    )

    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=False,
        help="Suppress stdout on success (exit code only).",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit raw JSON result instead of human-readable output.",
    )

    parser.set_defaults(func=cmd_send)
    return parser


__all__ = ["cmd_send", "register_send_subparser"]
