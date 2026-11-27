from __future__ import annotations


def _coerce_timeout(raw: object) -> float | None:
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return None
    if timeout <= 0:
        return None
    return timeout


def get_provider_request_timeout(
    provider_id: str, model: str | None = None
) -> float | None:
    """Return a configured provider request timeout in seconds, if any."""
    if not provider_id:
        return None

    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
    except Exception:
        return None

    providers = config.get("providers", {}) if isinstance(config, dict) else {}
    provider_config = (
        providers.get(provider_id, {}) if isinstance(providers, dict) else {}
    )
    if not isinstance(provider_config, dict):
        return None

    model_config = _get_model_config(provider_config, model)
    if model_config is not None:
        timeout = _coerce_timeout(model_config.get("timeout_seconds"))
        if timeout is not None:
            return timeout

    return _coerce_timeout(provider_config.get("request_timeout_seconds"))


def get_provider_stale_timeout(
    provider_id: str, model: str | None = None
) -> float | None:
    """Return a configured non-stream stale timeout in seconds, if any."""
    if not provider_id:
        return None

    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
    except Exception:
        return None

    providers = config.get("providers", {}) if isinstance(config, dict) else {}
    provider_config = (
        providers.get(provider_id, {}) if isinstance(providers, dict) else {}
    )
    if not isinstance(provider_config, dict):
        return None

    model_config = _get_model_config(provider_config, model)
    if model_config is not None:
        timeout = _coerce_timeout(model_config.get("stale_timeout_seconds"))
        if timeout is not None:
            return timeout

    return _coerce_timeout(provider_config.get("stale_timeout_seconds"))


def _get_model_config(
    provider_config: dict[str, object], model: str | None
) -> dict[str, object] | None:
    if not model:
        return None

    models = provider_config.get("models", {})
    model_config = models.get(model, {}) if isinstance(models, dict) else {}
    if isinstance(model_config, dict):
        return model_config
    return None


def _coerce_positive_int(raw: object) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


def get_provider_max_request_bytes(provider_id: str, base_url: str | None = None) -> int | None:
    """Return a configured per-provider outbound request-body cap in bytes.

    Read from ``providers.<id>.max_request_bytes``. Unset (or non-positive)
    means **no cap** — behaviour identical to today. This is the OUTBOUND
    API-body cap the provider's 413 is measured in; it is intentionally
    separate from inbound webhook limits (``gateway/platforms/webhook.py``'s
    ``max_body_bytes``).

    When the runtime provider label is an alias (e.g. ``custom``) that does
    not match a configured key, fall back to matching by ``base_url`` so the
    cap armed for the real endpoint still applies.
    """
    if provider_id:
        try:
            from hermes_cli.config import load_config_readonly
            config = load_config_readonly()
        except Exception:
            config = None

        providers = config.get("providers", {}) if isinstance(config, dict) else {}
        provider_config = (
            providers.get(provider_id, {}) if isinstance(providers, dict) else {}
        )
        if isinstance(provider_config, dict):
            value = _coerce_positive_int(provider_config.get("max_request_bytes"))
            if value is not None:
                return value

    # Alias fallback by base_url.
    if base_url:
        normalized = base_url.rstrip("/")
        try:
            from hermes_cli.config import load_config_readonly
            config = load_config_readonly()
        except Exception:
            return None
        providers = config.get("providers", {}) if isinstance(config, dict) else {}
        if isinstance(providers, dict):
            for entry in providers.values():
                if not isinstance(entry, dict):
                    continue
                entry_url = entry.get("base_url")
                if isinstance(entry_url, str) and entry_url.rstrip("/") == normalized:
                    value = _coerce_positive_int(entry.get("max_request_bytes"))
                    if value is not None:
                        return value
    return None
