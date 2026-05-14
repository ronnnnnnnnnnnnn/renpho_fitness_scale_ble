"""Config flow for renpho_fitness_scale_ble."""

from __future__ import annotations

import dataclasses
import logging
import re
import unicodedata
from datetime import datetime
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfo,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_ADDRESS, UnitOfLength, UnitOfMass
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import (
    config_validation as cv,
    entity_registry as er,
    selector,
)
from homeassistant.util.unit_conversion import DistanceConverter

from .const import (
    CONF_ATHLETE,
    CONF_BIRTHDATE,
    CONF_BODY_METRICS_ENABLED,
    CONF_CREATED_AT,
    CONF_ENABLE_LIBRARY_LOGGING,
    CONF_FEET,
    CONF_HEIGHT,
    CONF_HISTORY_RETENTION_DAYS,
    CONF_INCHES,
    CONF_MAX_HISTORY_SIZE,
    CONF_MOBILE_NOTIFY_SERVICES,
    CONF_PERSON_ENTITY,
    CONF_SCALE_DISPLAY_UNIT,
    CONF_SEX,
    CONF_UPDATED_AT,
    CONF_USER_ID,
    CONF_USER_NAME,
    CONF_USER_PROFILES,
    CONF_USE_ALTERNATIVE_ALGORITHM,
    DOMAIN,
    HISTORY_RETENTION_DAYS,
    MAX_HISTORY_SIZE,
    get_sensor_unique_id,
)

_LOGGER = logging.getLogger(__name__)
_SLUG_RE = re.compile(r"[^a-z0-9]")


# Bluetooth SIG manufacturer IDs that are not scale OEMs —
# filtering them out of the manual-flow device list dramatically reduces the
# noise in a typical household (AirPods, watches, earbuds, speakers, etc.)
# without ruling out unknown Renpho variants.
_KNOWN_NON_SCALE_MFR_IDS: frozenset[int] = frozenset(
    {
        # Phones / wearables / trackers — the dominant household BT noise
        0x004C,  # Apple, Inc. — AirPods, AirTags, iBeacon, Find My, HomePod
        0x0006,  # Microsoft — Surface, Xbox controllers
        0x0075,  # Samsung Electronics Co. Ltd. — phones, watches, Galaxy Buds
        0x00E0,  # Google (legacy entry) — Nest, older Pixel ecosystem
        0x018E,  # Google LLC (current entry) — Fast Pair, Find My Device
        0x067C,  # Tile, Inc. — Bluetooth trackers
        # Audio gear
        0x009E,  # Bose Corporation — headphones, speakers, soundbars
        0x05A7,  # Sonos Inc — wireless speakers
        0x0494,  # SENNHEISER electronic GmbH & Co. KG — headphones, mics
        0x0055,  # Plantronics, Inc. (now Poly) — headsets
        0x0103,  # Bang & Olufsen A/S — high-end audio
        0x00CC,  # Beats Electronics — audio (legacy/standalone; modern uses Apple ID)
        0x0057,  # Harman International — JBL, AKG, Mark Levinson, Harman/Kardon
        0x065A,  # Marshall Group AB — speakers, headphones
        0x07C9,  # Skullcandy, Inc. — headphones, earbuds
        # Other common consumer electronics
        0x01DA,  # Logitech International SA — keyboards, mice, webcams
        0x02F2,  # GoPro, Inc. — action cameras
        0x07A2,  # Roku, Inc. — TV streaming sticks/boxes
        0x012D,  # Sony Corporation — TVs, headphones, cameras
        0x068E,  # Razer Inc. — gaming peripherals
        0x005C,  # Belkin International — accessories, chargers; owns Wemo
        0x0171,  # Amazon.com Services LLC — Echo, Fire TV, Ring, Blink, Eero
    }
)


# Service UUIDs that categorically identify a non-scale device class.
_KNOWN_NON_SCALE_SERVICE_UUIDS: frozenset[str] = frozenset(
    {
        # Generic peripherals
        "00001812-0000-1000-8000-00805f9b34fb",  # HID over GATT (keyboards, mice, gamepads)
        # Health devices that are explicitly NOT scales — scales have
        # their own dedicated 0x181D Weight Scale Service
        "00001808-0000-1000-8000-00805f9b34fb",  # Glucose
        "00001809-0000-1000-8000-00805f9b34fb",  # Health Thermometer
        "00001810-0000-1000-8000-00805f9b34fb",  # Blood Pressure
        # Fitness equipment that isn't a scale
        "00001816-0000-1000-8000-00805f9b34fb",  # Cycling Speed and Cadence
        "00001818-0000-1000-8000-00805f9b34fb",  # Cycling Power
        "00001826-0000-1000-8000-00805f9b34fb",  # Fitness Machine (treadmills, bikes)
        # Trackers / beacons not necessarily caught by mfr-ID filter
        "00001819-0000-1000-8000-00805f9b34fb",  # Location and Navigation
        "0000feaa-0000-1000-8000-00805f9b34fb",  # Eddystone (open beacon protocol)
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_user_id(display_name: str, existing_profiles: list[dict]) -> str:
    base = (
        _SLUG_RE.sub(
            "",
            unicodedata.normalize("NFKD", display_name)
            .encode("ascii", "ignore")
            .decode()
            .lower(),
        )
        or "user"
    )
    existing_ids = {p[CONF_USER_ID] for p in existing_profiles}
    slug, idx = base, 2
    while slug in existing_ids:
        slug = f"{base}{idx}"
        idx += 1
    return slug


def _validate_user_name_not_empty(name: str) -> bool:
    return bool(name and name.strip())


def _validate_user_id_unique(
    user_id: str,
    user_profiles: list[dict],
    exclude_user_id: str | None = None,
) -> bool:
    for profile in user_profiles:
        pid = profile.get(CONF_USER_ID)
        if pid == user_id and pid != exclude_user_id:
            return False
    return True


def _validate_person_entity_unique(
    person_entity: str | None,
    user_profiles: list[dict],
    exclude_user_id: str | None = None,
) -> bool:
    if not person_entity:
        return True
    for profile in user_profiles:
        if profile.get(CONF_USER_ID) == exclude_user_id:
            continue
        if profile.get(CONF_PERSON_ENTITY) == person_entity:
            return False
    return True


def _get_mobile_notify_services(hass) -> dict[str, str]:
    services: dict[str, str] = {}
    for service_name in hass.services.async_services().get("notify", {}):
        if service_name.startswith("mobile_app_"):
            display = service_name.replace("mobile_app_", "").replace("_", " ").title()
            services[service_name] = display
    return services


def _coerce_height_cm(display_unit: str, user_input: dict) -> int:
    if display_unit == UnitOfMass.KILOGRAMS:
        return int(user_input[CONF_HEIGHT])
    return round(
        DistanceConverter.convert(
            user_input[CONF_FEET], UnitOfLength.FEET, UnitOfLength.CENTIMETERS
        )
        + DistanceConverter.convert(
            user_input[CONF_INCHES], UnitOfLength.INCHES, UnitOfLength.CENTIMETERS
        )
    )


def _body_metrics_schema(display_unit: str, defaults: dict | None = None) -> vol.Schema:
    defaults = defaults or {}
    schema: dict[Any, Any] = {
        vol.Required(
            CONF_SEX, default=defaults.get(CONF_SEX, "male")
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=["male", "female"],
                translation_key="sex",
                mode=selector.SelectSelectorMode.LIST,
            )
        ),
        vol.Required(
            CONF_BIRTHDATE, default=defaults.get(CONF_BIRTHDATE)
        ): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.DATE)
        ),
    }
    if display_unit == UnitOfMass.KILOGRAMS:
        schema[vol.Required(CONF_HEIGHT, default=defaults.get(CONF_HEIGHT, 170))] = (
            selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=100,
                    max=250,
                    unit_of_measurement=UnitOfLength.CENTIMETERS,
                    step=1,
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            )
        )
    else:
        cm = defaults.get(CONF_HEIGHT, 170)
        total_in = cm / 2.54
        feet = int(total_in // 12)
        inches = round((total_in % 12) * 2) / 2
        schema[vol.Required(CONF_FEET, default=feet)] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=3,
                max=8,
                unit_of_measurement=UnitOfLength.FEET,
                step=1,
                mode=selector.NumberSelectorMode.SLIDER,
            )
        )
        schema[vol.Required(CONF_INCHES, default=inches)] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0,
                max=11.5,
                unit_of_measurement=UnitOfLength.INCHES,
                step=0.5,
                mode=selector.NumberSelectorMode.SLIDER,
            )
        )
    schema[
        vol.Required(
            CONF_ATHLETE,
            default=defaults.get(CONF_ATHLETE, False),
        )
    ] = cv.boolean
    schema[
        vol.Required(
            CONF_USE_ALTERNATIVE_ALGORITHM,
            default=defaults.get(CONF_USE_ALTERNATIVE_ALGORITHM, False),
        )
    ] = cv.boolean
    return vol.Schema(schema)


def _user_basic_schema(hass, defaults: dict | None = None) -> vol.Schema:
    """Schema for the basic user fields (name, person, mobile, body_metrics_enabled)."""
    defaults = defaults or {}
    schema: dict[Any, Any] = {
        vol.Required(CONF_USER_NAME, default=defaults.get(CONF_USER_NAME)): str,
    }
    # Person entity
    person = defaults.get(CONF_PERSON_ENTITY)
    if person:
        schema[vol.Optional(CONF_PERSON_ENTITY, default=person)] = (
            selector.EntitySelector(selector.EntitySelectorConfig(domain="person"))
        )
    else:
        schema[vol.Optional(CONF_PERSON_ENTITY)] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="person")
        )
    # Mobile notify services (optional)
    mobile_services = _get_mobile_notify_services(hass)
    if mobile_services:
        schema[
            vol.Optional(
                CONF_MOBILE_NOTIFY_SERVICES,
                default=defaults.get(CONF_MOBILE_NOTIFY_SERVICES, []),
            )
        ] = cv.multi_select(mobile_services)
    # Body metrics toggle
    schema[
        vol.Required(
            CONF_BODY_METRICS_ENABLED,
            default=defaults.get(CONF_BODY_METRICS_ENABLED, False),
        )
    ] = cv.boolean
    return vol.Schema(schema)


@dataclasses.dataclass(frozen=True)
class Discovery:
    title: str
    discovery_info: BluetoothServiceInfo


def _title(info: BluetoothServiceInfo) -> str:
    name = info.name
    if not name or name == info.address:
        return info.address
    return f"{name} {info.address}"


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------


class ScaleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Renpho scale config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_devices: dict[str, Discovery] = {}

    async def async_step_bluetooth(self, info: BluetoothServiceInfo) -> FlowResult:
        await self.async_set_unique_id(info.address)
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {"name": _title(info)}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            self.context[CONF_SCALE_DISPLAY_UNIT] = user_input[CONF_SCALE_DISPLAY_UNIT]
            return await self.async_step_add_first_user()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=self.context["title_placeholders"],
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCALE_DISPLAY_UNIT, default=UnitOfMass.KILOGRAMS
                    ): vol.In(
                        {
                            UnitOfMass.KILOGRAMS: "Metric (kg)",
                            UnitOfMass.POUNDS: "Imperial (lbs)",
                        }
                    ),
                }
            ),
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            self.context["title_placeholders"] = {
                "name": self._discovered_devices[address].title
            }
            self.context[CONF_SCALE_DISPLAY_UNIT] = user_input[CONF_SCALE_DISPLAY_UNIT]
            return await self.async_step_add_first_user()

        current_addresses = self._async_current_ids()
        for info in async_discovered_service_info(self.hass):
            if (
                info.address in current_addresses
                or info.address in self._discovered_devices
            ):
                continue
            _LOGGER.debug(
                "Manual-flow candidate %s — name=%r connectable=%s mfr_data=%s "
                "service_uuids=%s service_data=%s rssi=%s",
                info.address,
                info.name,
                info.connectable,
                info.manufacturer_data,
                info.service_uuids,
                info.service_data,
                info.rssi,
            )
            if not info.connectable:
                _LOGGER.debug(
                    "  → skipped: non-connectable advertisement (beacons can't "
                    "be scales)"
                )
                continue
            excluded_mfr = _KNOWN_NON_SCALE_MFR_IDS & info.manufacturer_data.keys()
            if excluded_mfr:
                _LOGGER.debug(
                    "  → skipped: manufacturer ID(s) %s are on the known "
                    "non-scale OEM list",
                    sorted(excluded_mfr),
                )
                continue
            excluded_svc = _KNOWN_NON_SCALE_SERVICE_UUIDS & set(
                info.service_uuids or []
            )
            if excluded_svc:
                _LOGGER.debug(
                    "  → skipped: advertises service UUID(s) %s assigned to "
                    "a non-scale device class",
                    sorted(excluded_svc),
                )
                continue
            self._discovered_devices[info.address] = Discovery(_title(info), info)

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        titles = {a: d.title for a, d in self._discovered_devices.items()}
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(titles),
                    vol.Required(
                        CONF_SCALE_DISPLAY_UNIT, default=UnitOfMass.KILOGRAMS
                    ): vol.In(
                        {
                            UnitOfMass.KILOGRAMS: "Metric (kg)",
                            UnitOfMass.POUNDS: "Imperial (lbs)",
                        }
                    ),
                }
            ),
        )

    # -- First-user steps ----------------------------------------------------

    async def async_step_add_first_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            user_name = user_input[CONF_USER_NAME]
            errors: dict[str, str] = {}
            if not _validate_user_name_not_empty(user_name):
                errors["base"] = "empty_user_name"
            if errors:
                return self.async_show_form(
                    step_id="add_first_user",
                    data_schema=_user_basic_schema(self.hass),
                    errors=errors,
                )
            body_metrics = user_input.get(CONF_BODY_METRICS_ENABLED, False)
            if body_metrics:
                self.context[CONF_USER_NAME] = user_name
                self.context[CONF_PERSON_ENTITY] = user_input.get(CONF_PERSON_ENTITY)
                self.context[CONF_MOBILE_NOTIFY_SERVICES] = user_input.get(
                    CONF_MOBILE_NOTIFY_SERVICES, []
                )
                return await self.async_step_add_first_user_body_metrics()
            return self._create_entry_with_first_user(
                user_name=user_name,
                person_entity=user_input.get(CONF_PERSON_ENTITY),
                mobile_services=user_input.get(CONF_MOBILE_NOTIFY_SERVICES, []),
                body_metrics_enabled=False,
                body_metrics_fields=None,
            )
        return self.async_show_form(
            step_id="add_first_user", data_schema=_user_basic_schema(self.hass)
        )

    async def async_step_add_first_user_body_metrics(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        unit = self.context[CONF_SCALE_DISPLAY_UNIT]
        if user_input is not None:
            height_cm = _coerce_height_cm(unit, user_input)
            return self._create_entry_with_first_user(
                user_name=self.context[CONF_USER_NAME],
                person_entity=self.context.get(CONF_PERSON_ENTITY),
                mobile_services=self.context.get(CONF_MOBILE_NOTIFY_SERVICES, []),
                body_metrics_enabled=True,
                body_metrics_fields={
                    CONF_SEX: user_input[CONF_SEX],
                    CONF_BIRTHDATE: user_input[CONF_BIRTHDATE],
                    CONF_HEIGHT: height_cm,
                    CONF_ATHLETE: user_input.get(CONF_ATHLETE, False),
                    CONF_USE_ALTERNATIVE_ALGORITHM: user_input.get(
                        CONF_USE_ALTERNATIVE_ALGORITHM, False
                    ),
                },
            )
        return self.async_show_form(
            step_id="add_first_user_body_metrics",
            data_schema=_body_metrics_schema(unit),
        )

    def _create_entry_with_first_user(
        self,
        user_name: str,
        person_entity: str | None,
        mobile_services: list[str],
        body_metrics_enabled: bool,
        body_metrics_fields: dict | None,
    ) -> FlowResult:
        now = datetime.now().isoformat()
        user_profile: dict[str, Any] = {
            CONF_USER_ID: _create_user_id(user_name, []),
            CONF_USER_NAME: user_name,
            CONF_PERSON_ENTITY: person_entity,
            CONF_MOBILE_NOTIFY_SERVICES: mobile_services,
            CONF_BODY_METRICS_ENABLED: body_metrics_enabled,
            CONF_CREATED_AT: now,
            CONF_UPDATED_AT: now,
        }
        if body_metrics_fields:
            user_profile.update(body_metrics_fields)
        return self.async_create_entry(
            title=self.context["title_placeholders"]["name"],
            data={
                CONF_SCALE_DISPLAY_UNIT: self.context[CONF_SCALE_DISPLAY_UNIT],
                CONF_USER_PROFILES: [user_profile],
            },
        )

    @staticmethod
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return ScaleOptionsFlow(entry)


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


class ScaleOptionsFlow(OptionsFlow):
    """Options flow for managing users, scale settings, and advanced options."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        # `self.config_entry` is automatically provided by the base class on
        # HA 2025.12+. We only stash convenience copies of derived state.
        self.user_profiles = config_entry.data.get(CONF_USER_PROFILES, [])
        self.display_unit = config_entry.data.get(
            CONF_SCALE_DISPLAY_UNIT, UnitOfMass.KILOGRAMS
        )
        self.history_retention_days = config_entry.data.get(
            CONF_HISTORY_RETENTION_DAYS, HISTORY_RETENTION_DAYS
        )
        self.max_history_size = config_entry.data.get(
            CONF_MAX_HISTORY_SIZE, MAX_HISTORY_SIZE
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        menu_options = ["add_user", "scale_settings", "advanced_settings"]
        if self.user_profiles:
            menu_options.insert(1, "edit_user")
            menu_options.insert(2, "remove_user")
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    # -- Add user -----------------------------------------------------------

    async def async_step_add_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            user_name = user_input[CONF_USER_NAME]
            person_entity = user_input.get(CONF_PERSON_ENTITY)
            errors: dict[str, str] = {}
            if not _validate_user_name_not_empty(user_name):
                errors["base"] = "empty_user_name"
            if person_entity and not _validate_person_entity_unique(
                person_entity, self.user_profiles
            ):
                errors["person_entity"] = "duplicate_person_entity"
            if errors:
                return self.async_show_form(
                    step_id="add_user",
                    data_schema=_user_basic_schema(self.hass),
                    errors=errors,
                )
            body_metrics = user_input.get(CONF_BODY_METRICS_ENABLED, False)
            if body_metrics:
                self.context[CONF_USER_NAME] = user_name
                self.context[CONF_PERSON_ENTITY] = person_entity
                self.context[CONF_MOBILE_NOTIFY_SERVICES] = user_input.get(
                    CONF_MOBILE_NOTIFY_SERVICES, []
                )
                return await self.async_step_add_user_body_metrics()
            new_user_id = _create_user_id(user_name, self.user_profiles)
            if not _validate_user_id_unique(new_user_id, self.user_profiles):
                return self.async_abort(reason="user_id_generation_failed")
            now = datetime.now().isoformat()
            user_profile = {
                CONF_USER_ID: new_user_id,
                CONF_USER_NAME: user_name,
                CONF_PERSON_ENTITY: person_entity,
                CONF_MOBILE_NOTIFY_SERVICES: user_input.get(
                    CONF_MOBILE_NOTIFY_SERVICES, []
                ),
                CONF_BODY_METRICS_ENABLED: False,
                CONF_CREATED_AT: now,
                CONF_UPDATED_AT: now,
            }
            await self._save_profiles(self.user_profiles + [user_profile])
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="add_user", data_schema=_user_basic_schema(self.hass)
        )

    async def async_step_add_user_body_metrics(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            user_name = self.context[CONF_USER_NAME]
            if not _validate_user_name_not_empty(user_name):
                return self.async_abort(reason="invalid_user_name")
            height_cm = _coerce_height_cm(self.display_unit, user_input)
            new_user_id = _create_user_id(user_name, self.user_profiles)
            if not _validate_user_id_unique(new_user_id, self.user_profiles):
                return self.async_abort(reason="user_id_generation_failed")
            now = datetime.now().isoformat()
            user_profile = {
                CONF_USER_ID: new_user_id,
                CONF_USER_NAME: user_name,
                CONF_PERSON_ENTITY: self.context.get(CONF_PERSON_ENTITY),
                CONF_MOBILE_NOTIFY_SERVICES: self.context.get(
                    CONF_MOBILE_NOTIFY_SERVICES, []
                ),
                CONF_BODY_METRICS_ENABLED: True,
                CONF_SEX: user_input[CONF_SEX],
                CONF_BIRTHDATE: user_input[CONF_BIRTHDATE],
                CONF_HEIGHT: height_cm,
                CONF_ATHLETE: user_input.get(CONF_ATHLETE, False),
                CONF_USE_ALTERNATIVE_ALGORITHM: user_input.get(
                    CONF_USE_ALTERNATIVE_ALGORITHM, False
                ),
                CONF_CREATED_AT: now,
                CONF_UPDATED_AT: now,
            }
            await self._save_profiles(self.user_profiles + [user_profile])
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="add_user_body_metrics",
            data_schema=_body_metrics_schema(self.display_unit),
        )

    # -- Edit user ----------------------------------------------------------

    async def async_step_edit_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            self.context["selected_user_id"] = user_input["user_id"]
            return await self.async_step_edit_user_details()
        user_options = {
            user[CONF_USER_ID]: user[CONF_USER_NAME] for user in self.user_profiles
        }
        return self.async_show_form(
            step_id="edit_user",
            data_schema=vol.Schema({vol.Required("user_id"): vol.In(user_options)}),
        )

    async def async_step_edit_user_details(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        selected_user_id = self.context["selected_user_id"]
        user_index = next(
            i
            for i, u in enumerate(self.user_profiles)
            if u[CONF_USER_ID] == selected_user_id
        )
        current_user = self.user_profiles[user_index]

        if user_input is not None:
            user_name = user_input[CONF_USER_NAME]
            person_entity = user_input.get(CONF_PERSON_ENTITY)
            errors: dict[str, str] = {}
            if not _validate_user_name_not_empty(user_name):
                errors["base"] = "empty_user_name"
            if person_entity and not _validate_person_entity_unique(
                person_entity,
                self.user_profiles,
                exclude_user_id=selected_user_id,
            ):
                errors["person_entity"] = "duplicate_person_entity"
            if errors:
                return self.async_show_form(
                    step_id="edit_user_details",
                    data_schema=_user_basic_schema(self.hass, defaults=current_user),
                    errors=errors,
                )
            body_metrics_enabled = user_input.get(CONF_BODY_METRICS_ENABLED, False)
            currently_enabled = current_user.get(CONF_BODY_METRICS_ENABLED, False)
            if not body_metrics_enabled and currently_enabled:
                # Remove body-metric entities for this user
                from .sensor import BODY_METRIC_DESCRIPTIONS

                entity_reg = er.async_get(self.hass)
                device_name = self.config_entry.title
                for desc in BODY_METRIC_DESCRIPTIONS:
                    unique_id = get_sensor_unique_id(
                        device_name, selected_user_id, desc.key
                    )
                    if entity_id := entity_reg.async_get_entity_id(
                        "sensor", DOMAIN, unique_id
                    ):
                        entity_reg.async_remove(entity_id)
            if body_metrics_enabled:
                self.context["edit_user_input"] = user_input
                return await self.async_step_edit_user_body_metrics()
            updated_user = {
                **current_user,
                CONF_USER_NAME: user_name,
                CONF_PERSON_ENTITY: person_entity,
                CONF_MOBILE_NOTIFY_SERVICES: user_input.get(
                    CONF_MOBILE_NOTIFY_SERVICES, []
                ),
                CONF_BODY_METRICS_ENABLED: False,
                CONF_UPDATED_AT: datetime.now().isoformat(),
            }
            for k in (
                CONF_SEX,
                CONF_BIRTHDATE,
                CONF_HEIGHT,
                CONF_ATHLETE,
                CONF_USE_ALTERNATIVE_ALGORITHM,
            ):
                updated_user.pop(k, None)
            updated_profiles = list(self.user_profiles)
            updated_profiles[user_index] = updated_user
            await self._save_profiles(updated_profiles)
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="edit_user_details",
            data_schema=_user_basic_schema(self.hass, defaults=current_user),
        )

    async def async_step_edit_user_body_metrics(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        selected_user_id = self.context["selected_user_id"]
        user_index = next(
            i
            for i, u in enumerate(self.user_profiles)
            if u[CONF_USER_ID] == selected_user_id
        )
        current_user = self.user_profiles[user_index]
        if user_input is not None:
            height_cm = _coerce_height_cm(self.display_unit, user_input)
            basic_info = self.context["edit_user_input"]
            updated_user = {
                **current_user,
                CONF_USER_NAME: basic_info[CONF_USER_NAME],
                CONF_PERSON_ENTITY: basic_info.get(CONF_PERSON_ENTITY),
                CONF_MOBILE_NOTIFY_SERVICES: basic_info.get(
                    CONF_MOBILE_NOTIFY_SERVICES, []
                ),
                CONF_BODY_METRICS_ENABLED: True,
                CONF_SEX: user_input[CONF_SEX],
                CONF_BIRTHDATE: user_input[CONF_BIRTHDATE],
                CONF_HEIGHT: height_cm,
                CONF_ATHLETE: user_input.get(CONF_ATHLETE, False),
                CONF_USE_ALTERNATIVE_ALGORITHM: user_input.get(
                    CONF_USE_ALTERNATIVE_ALGORITHM, False
                ),
                CONF_UPDATED_AT: datetime.now().isoformat(),
            }
            updated_profiles = list(self.user_profiles)
            updated_profiles[user_index] = updated_user
            await self._save_profiles(updated_profiles)
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="edit_user_body_metrics",
            data_schema=_body_metrics_schema(self.display_unit, defaults=current_user),
        )

    # -- Remove user --------------------------------------------------------

    async def async_step_remove_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if len(self.user_profiles) <= 1:
            return self.async_abort(reason="cannot_remove_last_user")
        if user_input is not None:
            selected_user_id = user_input["user_id"]
            from .sensor import BODY_METRIC_DESCRIPTIONS

            entity_reg = er.async_get(self.hass)
            device_name = self.config_entry.title
            keys = [d.key for d in BODY_METRIC_DESCRIPTIONS] + ["weight"]
            for sensor_key in keys:
                unique_id = get_sensor_unique_id(
                    device_name, selected_user_id, sensor_key
                )
                if entity_id := entity_reg.async_get_entity_id(
                    "sensor", DOMAIN, unique_id
                ):
                    entity_reg.async_remove(entity_id)
            updated_profiles = [
                u for u in self.user_profiles if u[CONF_USER_ID] != selected_user_id
            ]
            await self._save_profiles(updated_profiles)
            return self.async_create_entry(title="", data={})
        user_options = {
            user[CONF_USER_ID]: user[CONF_USER_NAME] for user in self.user_profiles
        }
        return self.async_show_form(
            step_id="remove_user",
            data_schema=vol.Schema({vol.Required("user_id"): vol.In(user_options)}),
        )

    # -- Scale settings -----------------------------------------------------

    async def async_step_scale_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            new_data = {
                **self.config_entry.data,
                CONF_SCALE_DISPLAY_UNIT: user_input[CONF_SCALE_DISPLAY_UNIT],
            }
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="scale_settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCALE_DISPLAY_UNIT, default=self.display_unit
                    ): vol.In(
                        {
                            UnitOfMass.KILOGRAMS: "Metric (kg)",
                            UnitOfMass.POUNDS: "Imperial (lbs)",
                        }
                    ),
                }
            ),
        )

    # -- Advanced settings --------------------------------------------------

    async def async_step_advanced_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            new_data = {
                **self.config_entry.data,
                CONF_HISTORY_RETENTION_DAYS: user_input[CONF_HISTORY_RETENTION_DAYS],
                CONF_MAX_HISTORY_SIZE: user_input[CONF_MAX_HISTORY_SIZE],
                CONF_ENABLE_LIBRARY_LOGGING: user_input[CONF_ENABLE_LIBRARY_LOGGING],
            }
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data={})
        current_library_logging = self.config_entry.data.get(
            CONF_ENABLE_LIBRARY_LOGGING, False
        )
        return self.async_show_form(
            step_id="advanced_settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HISTORY_RETENTION_DAYS,
                        default=self.history_retention_days,
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                    vol.Required(
                        CONF_MAX_HISTORY_SIZE, default=self.max_history_size
                    ): vol.All(vol.Coerce(int), vol.Range(min=10, max=1000)),
                    vol.Required(
                        CONF_ENABLE_LIBRARY_LOGGING, default=current_library_logging
                    ): bool,
                }
            ),
        )

    # -- Helpers ------------------------------------------------------------

    async def _save_profiles(self, updated_profiles: list[dict]) -> None:
        """Persist updated user_profiles back to the config entry and reload."""
        new_data = {
            **self.config_entry.data,
            CONF_USER_PROFILES: updated_profiles,
        }
        self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
        await self.hass.config_entries.async_reload(self.config_entry.entry_id)
