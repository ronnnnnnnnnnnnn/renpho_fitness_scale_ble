"""Sensor platform for the renpho_fitness_scale_ble integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfMass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from renpho_escs20m import WEIGHT_KEY, ScaleData, WeightUnit

from .const import (
    CONF_BODY_METRICS_ENABLED,
    CONF_LAST_SYNCED_DISPLAY_UNIT,
    CONF_PROTOCOL,
    CONF_SCALE_DISPLAY_UNIT,
    CONF_USER_ID,
    CONF_USER_NAME,
    CONF_USER_PROFILES,
    DOMAIN,
    PROTOCOL_AABB,
    PROTOCOL_QN,
    WEIGHT_HISTORY_ATTR_LIMIT,
    get_sensor_unique_id,
)
from .coordinator import ScaleDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


# Body-metric sensor descriptions — the 9 metrics renpho-escs20m's
# BodyMetrics class exposes.
BODY_METRIC_DESCRIPTIONS: list[SensorEntityDescription] = [
    SensorEntityDescription(
        key="body_mass_index",
        icon="mdi:human-male-height-variant",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="body_fat_percentage",
        icon="mdi:human-handsdown",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="fat_free_mass",
        icon="mdi:run",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="body_water_percentage",
        icon="mdi:water-percent",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        # Renpho's BodyMetrics returns BMR in kcal/day per its docstring.
        key="basal_metabolic_rate",
        icon="mdi:fire",
        native_unit_of_measurement="kcal",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="skeletal_muscle_percentage",
        icon="mdi:weight-lifter",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="muscle_mass",
        icon="mdi:weight-lifter",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="bone_mass",
        icon="mdi:bone",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="protein_percentage",
        icon="mdi:egg-fried",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
]


# ---------------------------------------------------------------------------
# Per-user sensor classes
# ---------------------------------------------------------------------------


class ScaleUserSensor(RestoreSensor):
    """Per-user body-metric sensor."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_available = False

    def __init__(
        self,
        device_name: str,
        address: str,
        coordinator: ScaleDataUpdateCoordinator,
        entity_description: SensorEntityDescription,
        user_id: str,
        user_name: str,
    ) -> None:
        self.entity_description = entity_description
        self._attr_device_class = entity_description.device_class
        self._attr_state_class = entity_description.state_class
        self._attr_native_unit_of_measurement = (
            entity_description.native_unit_of_measurement
        )
        self._attr_icon = entity_description.icon
        self._user_id = user_id
        self._user_name = user_name
        self._coordinator = coordinator
        self._attr_name = (
            f"{user_name}'s {entity_description.key.replace('_', ' ').title()}"
        )
        self._attr_unique_id = get_sensor_unique_id(
            device_name, user_id, entity_description.key
        )
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, address)},
            name=device_name,
            manufacturer="Renpho",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if last_state := await self.async_get_last_sensor_data():
            self._attr_native_value = last_state.native_value
            self._attr_available = True
        self.async_on_remove(
            self._coordinator.add_user_listener(self._user_id, self._handle_update)
        )

    @callback
    def _handle_update(self, data: ScaleData) -> None:
        value = data.measurements.get(self.entity_description.key)
        if value is None:
            self._attr_available = False
            self._attr_native_value = None
        else:
            self._attr_available = True
            self._attr_native_value = value
            self._attr_force_update = True
        self.async_write_ha_state()


class ScaleUserWeightSensor(ScaleUserSensor):
    """Weight sensor with `weight_history` extra attribute.

    The history is ordered oldest→newest and formatted via the
    coordinator's ``format_measurement_for_attribute`` helper; weight
    values are converted to the unit the entity's state is actually
    displayed in (including per-entity registry overrides), so attribute
    and state always agree.
    """

    # Never record the history attribute: past the recorder's 16 KB attribute
    # limit (MAX_STATE_ATTRS_BYTES, ~68 entries at ~239 bytes each) the
    # recorder stores {} for ALL of the state's attributes, and statistics
    # compilation then sees a state without unit_of_measurement and
    # suppresses long-term statistics. That threshold is crossed at the
    # DEFAULT max_history_size of 100 — not only with keep_history_forever.
    _unrecorded_attributes = frozenset({"weight_history"})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Newest WEIGHT_HISTORY_ATTR_LIMIT entries, listed oldest→newest
        # (the pending diagnostic is the opposite — newest→oldest).
        history = self._coordinator.get_recent_user_history(
            self._user_id, limit=WEIGHT_HISTORY_ATTR_LIMIT
        )
        return {
            "weight_history": [
                self._coordinator.format_measurement_for_attribute(
                    measurement_id=m.measurement_id,
                    timestamp_iso=m.timestamp.isoformat(),
                    weight_kg=m.weight_kg,
                    # The unit the state itself is rendered in (registry
                    # override → suggested → native), so attribute and
                    # state can never disagree.
                    display_unit=self.unit_of_measurement,
                    body_fat=(m.raw or {}).get("body_fat"),
                    resistance_1=(m.raw or {}).get("resistance_1"),
                    resistance_2=(m.raw or {}).get("resistance_2"),
                    # Display string was rendered when the measurement was
                    # recorded and stored in `raw`. Pass it through so we
                    # don't re-walk Babel for every history entry on every
                    # state-write. Falls back to live formatting only for
                    # measurements that predate this storage (None).
                    timestamp_display=(m.raw or {}).get("timestamp_display"),
                )
                for m in history
            ],
        }


# ---------------------------------------------------------------------------
# Battery (regular sensor — battery is end-user-relevant, not diagnostic)
# ---------------------------------------------------------------------------


class ScaleBatterySensor(RestoreSensor):
    """Battery level. Restores the last known value across HA restarts so the
    entity isn't unavailable until the next BLE connection."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_available = False
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_name = "Battery"
    # Disabled by default: the ES-CS20M / QN-series firmware reports a static
    # 100% on the standard Battery Level characteristic and does not decrement
    # it as the cells drain, so a first-class battery entity would mislead users
    # and never fire low-battery automations. Users whose hardware happens to
    # report a real value (or who want the raw reading) can opt in by enabling
    # the entity. The value also remains available in the diagnostics download.
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        device_name: str,
        address: str,
        coordinator: ScaleDataUpdateCoordinator,
    ) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{device_name}_battery"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, address)},
            name=device_name,
            manufacturer="Renpho",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Restore the last known battery level so the entity reads correctly
        # immediately after restart instead of going Unavailable until the
        # scale connects again.
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = last.native_value
            self._attr_available = True
        self.async_on_remove(self._coordinator.add_diagnostic_listener(self._refresh))
        self._refresh()

    @callback
    def _refresh(self) -> None:
        # Only adopt the live value when the scale has actually read it —
        # otherwise we'd clobber a freshly-restored value with None.
        scale = self._coordinator._scale
        if scale is not None and scale.battery_level is not None:
            self._attr_native_value = scale.battery_level
            self._attr_available = True
        self.async_write_ha_state()


# ---------------------------------------------------------------------------
# Diagnostic sensors
# ---------------------------------------------------------------------------


class ScaleUserDirectorySensor(SensorEntity):
    """Diagnostic listing of configured users."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "User Directory"
    _attr_icon = "mdi:account-multiple"

    # Bounded, but a pure listing — recording it just churns database rows.
    _unrecorded_attributes = frozenset({"users"})

    def __init__(
        self,
        device_name: str,
        address: str,
        coordinator: ScaleDataUpdateCoordinator,
    ) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{device_name}_user_directory"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, address)},
            name=device_name,
            manufacturer="Renpho",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.add_diagnostic_listener(self.async_write_ha_state)
        )

    @property
    def native_value(self) -> int:
        return len(self._coordinator.get_user_profiles())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "users": [
                {
                    "user_id": p.get(CONF_USER_ID, ""),
                    "name": p.get(CONF_USER_NAME, ""),
                    "has_body_metrics": p.get(CONF_BODY_METRICS_ENABLED, False),
                }
                for p in self._coordinator.get_user_profiles()
            ]
        }


class ScalePendingMeasurementsSensor(SensorEntity):
    """Diagnostic listing of unattributed measurements."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Pending Measurements"
    _attr_icon = "mdi:clipboard-list"

    # The pending list has no count cap (only 24 h age pruning), so it is
    # structurally unbounded — past the recorder's 16 KB attribute limit the
    # recorder stores {} for ALL attributes. It's a diagnostic work-queue
    # with no history value anyway.
    _unrecorded_attributes = frozenset({"pending"})

    def __init__(
        self,
        device_name: str,
        address: str,
        coordinator: ScaleDataUpdateCoordinator,
    ) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{device_name}_pending_measurements"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, address)},
            name=device_name,
            manufacturer="Renpho",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.add_diagnostic_listener(self.async_write_ha_state)
        )

    @property
    def native_value(self) -> int:
        return len(self._coordinator.get_pending_measurements())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pending = [
            self._coordinator.format_measurement_for_attribute(
                measurement_id=mid,
                timestamp_iso=info.get("timestamp", ""),
                weight_kg=info.get("weight_kg"),
                body_fat=info.get("body_fat"),
                resistance_1=info.get("resistance_1"),
                resistance_2=info.get("resistance_2"),
                # Pre-rendered when the pending entry was created; refreshed
                # by `_on_core_config_updated` if time-format / timezone /
                # country changes.
                timestamp_display=info.get("timestamp_display"),
            )
            for mid, info in self._coordinator.get_pending_measurements().items()
        ]
        # Newest→oldest — deliberately opposite to `weight_history`
        # (oldest→newest): pending entries are a work-queue where the most
        # recent unattributed weigh-in is the one being acted on.
        pending.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return {"pending": pending}


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


def _sync_display_unit_overrides(
    hass: HomeAssistant,
    entry: ConfigEntry,
    display_unit: UnitOfMass,
) -> None:
    """Propagate a changed display-unit option to this entry's weight entities.

    Writes the per-entity display unit (the same registry option the entity
    settings dialog writes) for every weight-class sensor belonging to this
    config entry. Native values stay kg throughout - only the display layer
    moves, so recorded history converts cleanly.

    Runs only when the option actually changed since the last sync (tracked in
    entry data): per-entity units users set themselves survive restarts and
    reloads, and are only overridden when the user explicitly changes the
    integration-wide option.
    """
    last_synced = entry.data.get(CONF_LAST_SYNCED_DISPLAY_UNIT)
    if last_synced == display_unit:
        return

    if last_synced is not None:
        registry = er.async_get(hass)
        for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
            if (
                reg_entry.domain != SENSOR_DOMAIN
                or reg_entry.original_device_class != SensorDeviceClass.WEIGHT
            ):
                continue
            # async_update_entity_options replaces the whole per-domain options
            # dict, so carry over any other stored sensor options (e.g. display
            # precision) instead of clobbering them.
            sensor_options = {
                **(reg_entry.options.get(SENSOR_DOMAIN) or {}),
                "unit_of_measurement": display_unit,
            }
            registry.async_update_entity_options(
                reg_entry.entity_id, SENSOR_DOMAIN, sensor_options
            )
            _LOGGER.debug(
                "Updated display unit for %s: %s -> %s",
                reg_entry.entity_id,
                last_synced,
                display_unit,
            )

    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_LAST_SYNCED_DISPLAY_UNIT: display_unit}
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Construct all sensors for this scale and start the coordinator."""
    address = entry.unique_id
    assert address is not None
    coordinator: ScaleDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    user_profiles = entry.data.get(CONF_USER_PROFILES, [])
    display_unit = entry.data.get(CONF_SCALE_DISPLAY_UNIT, UnitOfMass.KILOGRAMS)
    protocol = entry.data.get(CONF_PROTOCOL, PROTOCOL_QN)

    coordinator.set_display_unit(
        WeightUnit.KG if display_unit == UnitOfMass.KILOGRAMS else WeightUnit.LB
    )

    entities: list = []
    for user in user_profiles:
        user_id = user[CONF_USER_ID]
        user_name = user.get(CONF_USER_NAME, "User")
        weight_desc = SensorEntityDescription(
            key=WEIGHT_KEY,
            icon="mdi:human-handsdown",
            device_class=SensorDeviceClass.WEIGHT,
            native_unit_of_measurement=UnitOfMass.KILOGRAMS,
            state_class=SensorStateClass.MEASUREMENT,
        )
        entities.append(
            ScaleUserWeightSensor(
                device_name=entry.title,
                address=address,
                coordinator=coordinator,
                entity_description=weight_desc,
                user_id=user_id,
                user_name=user_name,
            )
        )
        if user.get(CONF_BODY_METRICS_ENABLED, False):
            for desc in BODY_METRIC_DESCRIPTIONS:
                entities.append(
                    ScaleUserSensor(
                        device_name=entry.title,
                        address=address,
                        coordinator=coordinator,
                        entity_description=desc,
                        user_id=user_id,
                        user_name=user_name,
                    )
                )

    # The broadcast-only variant never connects, so there's no Battery Level
    # characteristic to read — don't offer a battery entity for it.
    if protocol != PROTOCOL_AABB:
        entities.append(ScaleBatterySensor(entry.title, address, coordinator))
    entities.append(ScaleUserDirectorySensor(entry.title, address, coordinator))
    entities.append(ScalePendingMeasurementsSensor(entry.title, address, coordinator))

    # Apply the user-chosen display unit to every weight-class sensor (the
    # per-user weight, plus body-metric sensors with WEIGHT device class —
    # fat_free_mass, muscle_mass, bone_mass). Native values are always
    # stored in kg; this just tells HA which unit to *display*.
    # New entities consume the suggestion once at first registration; when
    # the option changes, _sync_display_unit_overrides() re-applies it to
    # existing entities via per-entity registry options scoped to this
    # entry.
    for sensor in entities:
        if getattr(sensor, "_attr_device_class", None) == SensorDeviceClass.WEIGHT:
            sensor._attr_suggested_unit_of_measurement = display_unit
    _sync_display_unit_overrides(hass, entry, display_unit)

    async_add_entities(entities)

    await coordinator.async_start()
