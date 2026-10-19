"""An opt-in ordinary-user route that never falls back to paid transports."""

from __future__ import annotations

from .request_budget import ordinary_request


def ordinary_free_only() -> bool:
    if not ordinary_request():
        return False
    from .model_selector import model_selector

    return bool(model_selector.get_config_value("ordinary_model_pools", {}))


def free_transport_allowed(model, url=None) -> bool:
    """Check trusted catalog metadata, never model-provided billing labels."""
    from .model_selector import model_selector

    state = model_selector._active_state()
    identity = f"{model.get('model')} ({model.get('provider')})"
    names = {name for pool in state.model_config.get("ordinary_model_pools", {}).values() for name in pool}
    trusted = state.models.get(identity, {})
    return (
        identity in names
        and trusted.get("billing_type") == "free"
        and trusted.get("model_type", "chat") == "chat"
        and trusted.get("url") == model.get("url")
        and (url is None or str(url) == trusted.get("url"))
    )
