"""Diagnostics support for renpho_fitness_scale_ble.

Wired up via HA's built-in "Download Diagnostics" button on the device
card. Returns a single JSON blob with the config entry + coordinator
state, with PII redacted.

Redacted fields:
  - ``address``                — BLE MAC of the scale (privacy)
  - ``person_entity``          — links to a HA person entity (privacy)
  - ``mobile_notify_services`` — companion-app service names usually contain
                                 the user's phone hostname
  - ``birthdate`` / ``height`` / ``sex`` — user PII

What we keep verbatim:
  - User IDs (slugs derived from display names; useful for matching
    pending-measurement candidate lists in the same dump)
  - ``measurement_id`` strings (opaque uuid hex)
  - Weight values, body fat, resistance — useful for debugging, not PII
    in the same way an address is
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_BIRTHDATE,
    CONF_HEIGHT,
    CONF_MOBILE_NOTIFY_SERVICES,
    CONF_PENDING_STATE,
    CONF_PERSON_ENTITY,
    CONF_ROUTER_STATE,
    CONF_SEX,
    DOMAIN,
)
from .coordinator import ScaleDataUpdateCoordinator

# Top-level entry.data keys to redact wholesale (the router_state contains
# every persisted weight measurement; not strictly PII but huge and noisy
# for a diagnostics dump — we summarize separately below).
_TO_REDACT = {
    "address",
    "unique_id",  # entry.unique_id is the BLE MAC
    CONF_PERSON_ENTITY,
    CONF_MOBILE_NOTIFY_SERVICES,
    CONF_BIRTHDATE,
    CONF_HEIGHT,
    CONF_SEX,
    CONF_ROUTER_STATE,
    CONF_PENDING_STATE,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a Renpho scale config entry."""
    coordinator: ScaleDataUpdateCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )

    diag: dict[str, Any] = {
        "entry": {
            "version": entry.version,
            "title": entry.title,
            "source": entry.source,
            "data": async_redact_data(dict(entry.data), _TO_REDACT),
        },
    }

    if coordinator is None:
        diag["coordinator"] = None
        return diag

    # Per-user history summaries (counts only — full history is in
    # router_state which we redact above; counts are what's diagnostic).
    user_history: dict[str, dict[str, Any]] = {}
    for profile in coordinator.get_user_profiles():
        user_id = profile.get("user_id", "")
        history = coordinator._router.get_user_history(user_id)
        latest = history[-1] if history else None
        user_history[user_id] = {
            "history_count": len(history),
            "latest_timestamp": (
                latest.timestamp.isoformat() if latest is not None else None
            ),
            "latest_weight_kg": latest.weight_kg if latest is not None else None,
            "latest_has_body_fat": (
                bool((latest.raw or {}).get("body_fat") is not None)
                if latest is not None
                else False
            ),
        }

    # Pending-measurement summary — drop raw_measurement (not JSON-safe)
    # and notified_mobile_services (PII), keep the rest.
    pending_redacted: dict[str, dict[str, Any]] = {}
    for mid, info in coordinator.get_pending_measurements().items():
        pending_redacted[mid] = {
            k: v
            for k, v in info.items()
            if k not in {"raw_measurement", "notified_mobile_services"}
        }

    scale = coordinator._scale
    diag["coordinator"] = {
        "device_name": coordinator.device_name,
        "display_unit": coordinator.get_display_unit().name,
        "user_count": len(coordinator.get_user_profiles()),
        "user_ids": [p.get("user_id", "") for p in coordinator.get_user_profiles()],
        "user_has_body_metrics": {
            p.get("user_id", ""): bool(p.get("body_metrics_enabled", False))
            for p in coordinator.get_user_profiles()
        },
        "user_history": user_history,
        "pending_count": len(pending_redacted),
        "pending": pending_redacted,
        "scale_connected": scale is not None,
        "scale_firmware_revision": (
            getattr(scale, "firmware_revision", None) if scale else None
        ),
        "scale_battery_level": (
            getattr(scale, "battery_level", None) if scale else None
        ),
    }
    return diag
