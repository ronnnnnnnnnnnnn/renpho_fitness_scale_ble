"""The renpho_fitness_scale_ble integration."""

from __future__ import annotations

import logging

import voluptuous as vol
from bleak_retry_connector import close_stale_connections_by_address
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.start import async_at_started

from .const import (
    CONF_ENABLE_LIBRARY_LOGGING,
    CONF_HISTORY_RETENTION_DAYS,
    CONF_KEEP_HISTORY_FOREVER,
    CONF_MAX_HISTORY_SIZE,
    CONF_PENDING_STATE,
    CONF_PROTOCOL,
    CONF_ROUTER_STATE,
    CONF_USER_PROFILES,
    DOMAIN,
    HISTORY_RETENTION_DAYS,
    MAX_HISTORY_SIZE,
    PASSIVE_SCAN_ISSUE_ID,
    PROTOCOL_QN,
)
from .coordinator import (
    BluetoothNotAvailableError,
    ScaleDataUpdateCoordinator,
)
from .repairs import (
    async_clear_repair_issues_for_entry,
    async_scan_repair_issues,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [Platform.SENSOR]

# Service constants
SERVICE_ASSIGN_MEASUREMENT = "assign_measurement"
SERVICE_REASSIGN_MEASUREMENT = "reassign_measurement"
SERVICE_REMOVE_MEASUREMENT = "remove_measurement"
ATTR_MEASUREMENT_ID = "measurement_id"
ATTR_USER_ID = "user_id"
ATTR_FROM_USER_ID = "from_user_id"
ATTR_TO_USER_ID = "to_user_id"
ATTR_DEVICE_ID = "device_id"

# Internal data keys
DATA_MOBILE_APP_LISTENER_UNSUB = "mobile_app_listener_unsub"


# ---------------------------------------------------------------------------
# Service helpers
# ---------------------------------------------------------------------------


def _get_coordinator_for_device(
    hass: HomeAssistant, device_id: str
) -> ScaleDataUpdateCoordinator:
    """Resolve a HA device_id to the scale coordinator that owns it."""
    from homeassistant.helpers import device_registry as dr

    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get(device_id)
    if not device_entry:
        raise HomeAssistantError(f"Device {device_id} not found")
    for entry_id in device_entry.config_entries:
        coord = hass.data.get(DOMAIN, {}).get(entry_id)
        if isinstance(coord, ScaleDataUpdateCoordinator):
            return coord
    raise HomeAssistantError(
        f"Device {device_entry.name or device_id} is not a Renpho scale"
    )


# ---------------------------------------------------------------------------
# Mobile-app event listener
# ---------------------------------------------------------------------------


def _split_action(prefix: str, action: str) -> tuple[str, str] | None:
    """Decode ``SCALE_<prefix>_<user_id>_<measurement_id>`` into ``(user_id,
    measurement_id)``.

    Correctness depends on two invariants:

    - ``user_id`` is a slug produced by ``_create_user_id`` in
      ``config_flow.py``, which strips everything outside ``[a-z0-9]``.
      So user_ids never contain ``_``.
    - ``measurement_id`` is ``uuid4().hex`` from
      ``multi-user-scale-core``: 32 hex characters, no separators.

    Together those guarantee that ``rsplit("_", 1)`` yields the right
    split — the only ``_`` in the payload is the one we put there.
    """
    if not action.startswith(prefix):
        return None
    payload = action[len(prefix) :]
    if "_" not in payload:
        return None
    user_id, measurement_id = payload.rsplit("_", 1)
    if not user_id or not measurement_id:
        return None
    return user_id, measurement_id


async def _handle_mobile_app_action(hass: HomeAssistant, event) -> None:
    """Dispatch mobile_app_notification_action events to the right coordinator."""
    action = event.data.get("action")
    if not action or not isinstance(action, str):
        return
    if not action.startswith("SCALE_"):
        return
    try:
        if action.startswith("SCALE_ASSIGN_"):
            decoded = _split_action("SCALE_ASSIGN_", action)
            if not decoded:
                _LOGGER.warning("Invalid SCALE_ASSIGN action: %s", action)
                return
            user_id, measurement_id = decoded
            for coord in hass.data.get(DOMAIN, {}).values():
                if not isinstance(coord, ScaleDataUpdateCoordinator):
                    continue
                if measurement_id in coord.get_pending_measurements():
                    valid = {p["user_id"] for p in coord.get_user_profiles()}
                    if user_id not in valid:
                        _LOGGER.warning(
                            "Mobile assign: user %s not on this scale", user_id
                        )
                        continue
                    coord.assign_pending_measurement(measurement_id, user_id)
                    return
        elif action.startswith("SCALE_NOT_ME_"):
            decoded = _split_action("SCALE_NOT_ME_", action)
            if not decoded:
                _LOGGER.warning("Invalid SCALE_NOT_ME action: %s", action)
                return
            user_id, measurement_id = decoded
            for coord in hass.data.get(DOMAIN, {}).values():
                if not isinstance(coord, ScaleDataUpdateCoordinator):
                    continue
                if measurement_id in coord.get_pending_measurements():
                    coord.ignore_candidate_for_pending_measurement(
                        measurement_id, user_id
                    )
                    return
        else:
            _LOGGER.warning("Unknown SCALE action: %s", action)
    except Exception:
        _LOGGER.exception("Error handling mobile-app action: %s", action)


# ---------------------------------------------------------------------------
# Setup / unload
# ---------------------------------------------------------------------------


@callback
def _disable_battery_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Disable any still-enabled ``*_battery`` sensor for this entry.

    Tested devices seem to report a static 100% on the Battery Level
    characteristic (see :class:`ScaleBatterySensor`), so the entity is
    disabled by default. New installs get that via
    ``entity_registry_enabled_default``; this brings existing installs in
    line. Only entities the user hasn't already disabled are touched, and the
    one-time ``minor_version`` guard in :func:`async_migrate_entry` means a
    later deliberate re-enable is never overridden.
    """
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            reg_entry.domain == "sensor"
            and reg_entry.unique_id.endswith("_battery")
            and reg_entry.disabled_by is None
        ):
            registry.async_update_entity(
                reg_entry.entity_id,
                disabled_by=er.RegistryEntryDisabler.INTEGRATION,
            )


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate older config entries. Called automatically by HA when a stored
    entry's version is behind the config flow's ``VERSION``/``MINOR_VERSION``."""
    if entry.version == 1 and entry.minor_version < 2:
        # 1.1 → 1.2: disable the (unreliable on observed hardware) battery
        # entity for installs that registered it while it was enabled.
        _disable_battery_entities(hass, entry)
        hass.config_entries.async_update_entry(entry, minor_version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Renpho scale from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    address = entry.unique_id
    assert address is not None
    await close_stale_connections_by_address(address)

    coordinator = ScaleDataUpdateCoordinator(
        hass=hass,
        address=address,
        user_profiles=entry.data.get(CONF_USER_PROFILES, []),
        device_name=entry.title,
        router_state=entry.data.get(CONF_ROUTER_STATE),
        pending_state=entry.data.get(CONF_PENDING_STATE),
        history_retention_days=entry.data.get(
            CONF_HISTORY_RETENTION_DAYS, HISTORY_RETENTION_DAYS
        ),
        max_history_size=entry.data.get(CONF_MAX_HISTORY_SIZE, MAX_HISTORY_SIZE),
        keep_history_forever=entry.data.get(CONF_KEEP_HISTORY_FOREVER, False),
        enable_library_logging=entry.data.get(CONF_ENABLE_LIBRARY_LOGGING, False),
        protocol=entry.data.get(CONF_PROTOCOL, PROTOCOL_QN),
    )
    coordinator.set_config_entry_id(entry.entry_id)
    hass.data[DOMAIN][entry.entry_id] = coordinator

    try:
        coordinator.check_bluetooth_available()
    except BluetoothNotAvailableError as err:
        raise ConfigEntryNotReady(
            "Bluetooth not available yet. Will retry automatically."
        ) from err

    # Register services on first setup
    if not hass.services.has_service(DOMAIN, SERVICE_ASSIGN_MEASUREMENT):

        async def handle_assign(call: ServiceCall) -> None:
            coord = _get_coordinator_for_device(hass, call.data[ATTR_DEVICE_ID])
            mid = call.data[ATTR_MEASUREMENT_ID]
            user_id = call.data[ATTR_USER_ID]
            valid = {p["user_id"] for p in coord.get_user_profiles()}
            if user_id not in valid:
                raise HomeAssistantError(
                    f"User '{user_id}' not found. Available: {sorted(valid)}"
                )
            if mid not in coord.get_pending_measurements():
                raise HomeAssistantError(
                    f"Measurement {mid} not pending for this scale"
                )
            if not coord.assign_pending_measurement(mid, user_id):
                raise HomeAssistantError("assign_pending_measurement failed")

        hass.services.async_register(
            DOMAIN,
            SERVICE_ASSIGN_MEASUREMENT,
            handle_assign,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_DEVICE_ID): cv.string,
                    vol.Required(ATTR_MEASUREMENT_ID): cv.string,
                    vol.Required(ATTR_USER_ID): cv.string,
                },
                extra=vol.ALLOW_EXTRA,
            ),
        )

        async def handle_reassign(call: ServiceCall) -> None:
            coord = _get_coordinator_for_device(hass, call.data[ATTR_DEVICE_ID])
            from_uid = call.data[ATTR_FROM_USER_ID]
            to_uid = call.data[ATTR_TO_USER_ID]
            mid = call.data.get(ATTR_MEASUREMENT_ID)
            valid = {p["user_id"] for p in coord.get_user_profiles()}
            for label, uid in [("from_user_id", from_uid), ("to_user_id", to_uid)]:
                if uid not in valid:
                    raise HomeAssistantError(
                        f"{label} '{uid}' not found. Available: {sorted(valid)}"
                    )
            if not coord.reassign_user_measurement(from_uid, to_uid, mid):
                raise HomeAssistantError("reassign_user_measurement failed")

        hass.services.async_register(
            DOMAIN,
            SERVICE_REASSIGN_MEASUREMENT,
            handle_reassign,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_DEVICE_ID): cv.string,
                    vol.Required(ATTR_FROM_USER_ID): cv.string,
                    vol.Required(ATTR_TO_USER_ID): cv.string,
                    vol.Optional(ATTR_MEASUREMENT_ID): cv.string,
                },
                extra=vol.ALLOW_EXTRA,
            ),
        )

        async def handle_remove(call: ServiceCall) -> None:
            coord = _get_coordinator_for_device(hass, call.data[ATTR_DEVICE_ID])
            user_id = call.data[ATTR_USER_ID]
            mid = call.data.get(ATTR_MEASUREMENT_ID)
            valid = {p["user_id"] for p in coord.get_user_profiles()}
            if user_id not in valid:
                raise HomeAssistantError(
                    f"User '{user_id}' not found. Available: {sorted(valid)}"
                )
            if not coord.remove_user_measurement(user_id, mid):
                raise HomeAssistantError("remove_user_measurement failed")

        hass.services.async_register(
            DOMAIN,
            SERVICE_REMOVE_MEASUREMENT,
            handle_remove,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_DEVICE_ID): cv.string,
                    vol.Required(ATTR_USER_ID): cv.string,
                    vol.Optional(ATTR_MEASUREMENT_ID): cv.string,
                },
                extra=vol.ALLOW_EXTRA,
            ),
        )

    # Register the shared mobile-app event listener once. The callback is
    # async so HA awaits it directly on the event loop — wrapping the call
    # in a sync lambda + hass.async_create_task is unsafe (the frame helper
    # rejects async_create_task from non-loop threads) and unnecessary.
    if DATA_MOBILE_APP_LISTENER_UNSUB not in hass.data[DOMAIN]:

        async def _on_mobile_app_event(event) -> None:
            await _handle_mobile_app_action(hass, event)

        hass.data[DOMAIN][DATA_MOBILE_APP_LISTENER_UNSUB] = hass.bus.async_listen(
            "mobile_app_notification_action", _on_mobile_app_event
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(coordinator.async_stop)

    # Surface misconfigured profiles on Settings → Repairs. Deferred via
    # `async_at_started` so the mobile_app integration has finished
    # registering its `notify.mobile_app_*` services before we check
    # whether they exist — otherwise we'd false-positive on every cold
    # boot. If HA is already up (config-entry reload after an options-
    # flow change), the callback fires immediately.
    #
    # The `@callback` decorator is required: without it, HA's `HassJob`
    # classifies the function as an executor job and runs it in a
    # thread, which then trips `verify_event_loop_thread` inside the
    # issue registry.
    @callback
    def _scan_when_ready(hass: HomeAssistant) -> None:
        async_scan_repair_issues(hass, entry)

    entry.async_on_unload(async_at_started(hass, _scan_when_ready))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        async_clear_repair_issues_for_entry(hass, entry)
        coordinator: ScaleDataUpdateCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        # Note: coordinator.async_stop is also registered via
        # entry.async_on_unload during setup; HA invokes that automatically
        # after this function returns True. We don't call it explicitly here.
        bluetooth.async_rediscover_address(hass, coordinator.address)

        remaining = [
            v
            for v in hass.data.get(DOMAIN, {}).values()
            if isinstance(v, ScaleDataUpdateCoordinator)
        ]
        if not remaining:
            if unsub := hass.data[DOMAIN].pop(DATA_MOBILE_APP_LISTENER_UNSUB, None):
                unsub()
            for service in (
                SERVICE_ASSIGN_MEASUREMENT,
                SERVICE_REASSIGN_MEASUREMENT,
                SERVICE_REMOVE_MEASUREMENT,
            ):
                if hass.services.has_service(DOMAIN, service):
                    hass.services.async_remove(DOMAIN, service)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up after a config entry is permanently removed.

    The passive-scanning repair issue is host-level rather than
    entry-scoped, so `async_clear_repair_issues_for_entry` deliberately
    leaves it alone (deleting it on every unload/reload would erase the
    user's "ignore" flag). Remove it here instead, once the integration's
    last config entry is gone - this hook only fires on permanent
    removal, never on reload.
    """
    from homeassistant.helpers import issue_registry as ir

    remaining = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id
    ]
    if not remaining:
        ir.async_delete_issue(hass, DOMAIN, PASSIVE_SCAN_ISSUE_ID)
