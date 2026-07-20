"""Coordinator for the renpho_fitness_scale_ble integration.

Owns the lifecycle of the Renpho scale BLE client (`RenphoESCS20MScale`)
and the `WeightRouter` that handles user matching. Includes ESPHome BT
proxy scanner support so the scale can be reached either directly by
the HA host's Bluetooth adapter or via a remote ESPHome proxy.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import time
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import partial
from math import floor
from typing import Any, Literal

from aioesphomeapi import APIClient, BluetoothProxyFeature
from aioesphomeapi.model import BluetoothLEAdvertisement, DeviceInfo
from bleak import BleakError
from bleak.assigned_numbers import AdvertisementDataType
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import (
    AdvertisementData,
    AdvertisementDataCallback,
    BaseBleakScanner,
    get_platform_scanner_backend_type,
)
from bluetooth_data_tools import (
    int_to_bluetooth_address,
    parse_advertisement_data_tuple,
)
from homeassistant.components import persistent_notification
from homeassistant.const import UnitOfMass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import MassConverter
from multi_user_scale_core import (
    RouterConfig,
    UserProfile,
    WeightMeasurement,
    WeightRouter,
)
from renpho_escs20m import (
    BODY_FAT_KEY,
    BluetoothScanningMode,
    Profile,
    ProfileResolver,
    RESISTANCE_1_KEY,
    RESISTANCE_2_KEY,
    RenphoAABBScale,
    RenphoQNScale,
    RenphoScale,
    ScaleData,
    ScaleProtocol,
    Sex,
    WEIGHT_KEY,
    WeightUnit,
)

from .const import (
    AABB_SYNTHETIC_RESISTANCE,
    ALGORITHM_ALTERNATIVE,
    ALGORITHM_DEFAULT,
    CONF_ATHLETE,
    CONF_BIRTHDATE,
    CONF_BODY_METRICS_ENABLED,
    CONF_HEIGHT,
    CONF_MOBILE_NOTIFY_SERVICES,
    CONF_PENDING_STATE,
    CONF_PERSON_ENTITY,
    CONF_ROUTER_STATE,
    CONF_SEX,
    CONF_USER_ID,
    CONF_USER_NAME,
    CONF_USER_PROFILES,
    CONF_USE_ALTERNATIVE_ALGORITHM,
    DOMAIN,
    HISTORY_RETENTION_DAYS,
    MAX_HISTORY_SIZE,
    PROTOCOL_AABB,
    PROTOCOL_QN,
    parse_notify_service,
)


SYSTEM = platform.system()
IS_LINUX = SYSTEM == "Linux"
IS_MACOS = SYSTEM == "Darwin"


if IS_LINUX:
    from bleak.args.bluez import BlueZScannerArgs, OrPattern

    # or_patterns is a workaround for the fact that passive scanning
    # needs at least one matcher to be set. The below matcher
    # will match all devices.
    PASSIVE_SCANNER_ARGS = BlueZScannerArgs(
        or_patterns=[
            OrPattern(0, AdvertisementDataType.FLAGS, b"\x02"),
            OrPattern(0, AdvertisementDataType.FLAGS, b"\x06"),
            OrPattern(0, AdvertisementDataType.FLAGS, b"\x1a"),
        ]
    )


_LOGGER = logging.getLogger(__name__)


# Age threshold for pending (unattributed) measurements. After this duration
# an unassigned pending measurement is dropped from memory and any open
# notifications for it are dismissed. Cleanup runs opportunistically — on
# every new pending insert and once on coordinator restore — so stale
# entries can't survive a long HA-offline window followed by a restart.
PENDING_MEASUREMENT_MAX_AGE = timedelta(hours=24)


# Backoff for scale-client restarts triggered by BT-scanner-registration
# changes (see `_async_registration_changed`). Without this, a scanner that
# keeps failing to (re)start gets a full pipeline rebuild attempt on every
# single registration event (e.g. each ESPHome proxy reconnect), which can
# run often enough to saturate the HA event loop for hours.
RESTART_BACKOFF_BASE_SECONDS = 30
RESTART_BACKOFF_MAX_SECONDS = 1800  # 30 minutes


# Hard floor between scale-client restart attempts triggered by a
# registration event pre-empting a pending backoff retry (see
# `_async_registration_changed`). HA's scanner-registration callback isn't
# itself rate-limited, so without this floor a flapping/bootlooping BT
# proxy could keep pre-empting the backoff timer on every flap and drive
# back-to-back `_async_start()` cycles - no leak (that's fixed), but
# repeated real I/O load faster than the backoff is meant to allow. Applies
# only to the pre-emption path; it does not touch the backoff delay itself
# or the no-backoff-pending (restarts currently succeeding) path.
REGISTRATION_PREEMPT_DEBOUNCE_SECONDS = 5


# How long to ignore advertisements from the scale after a successful
# measurement. The scale's BLE chip emits non-connectable advertisements
# while spinning down post-measurement; each one would otherwise trigger
# a connect cycle that fails with `BleakOutOfConnectionSlotsError`. 10
# seconds covers the immediate post-measurement tail with minimal impact
# on intentional rapid re-weighing. Stragglers past 10s and the
# genuinely-rare out-of-slots case are silenced via `_NoiseFilter` below
# rather than by lengthening the cooldown — neither is user-actionable
# from a log message, and a longer cooldown would block legitimate
# re-step measurements.
POST_MEASUREMENT_COOLDOWN_SECONDS = 10


class _NoiseFilter(logging.Filter):
    """Drop ``BleakOutOfConnectionSlotsError`` records from the library
    logger.

    The renpho-escs20m library logs *any* connect failure via
    ``logger.exception(...)`` (ERROR + traceback). In practice the only
    failure most users will ever see is this one exception, which fires
    on a non-connectable advertisement — either the scale's
    post-measurement spin-down tail or an idle-mode beacon. The
    integration retries automatically on the next real advertisement;
    the log line is just noise. Genuine connection-slot exhaustion (the
    other case raising the same exception) isn't actionable from a log
    line either — the user-facing fix is documented in the README and
    surfaced via the `BleakOutOfConnectionSlotsError` troubleshooting
    section.

    Filter is bypassed when ``enable_library_logging`` is on so debug
    sessions still see every connect attempt.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        if exc is not None and type(exc).__name__ == "BleakOutOfConnectionSlotsError":
            return False
        # Belt-and-suspenders for records that carry the message but no
        # exc_info (shouldn't happen with the current library shape, but
        # cheap to cover).
        return "BleakOutOfConnectionSlotsError" not in record.getMessage()


# The body-metric attributes we copy off `BodyMetrics` into the measurement
# dict. Names match `BodyMetrics` properties exactly. Defined at module
# scope so we don't rebuild it on every measurement.
#
# This list is duplicated by `BODY_METRIC_DESCRIPTIONS` in sensor.py, which
# additionally carries the icon / unit / device_class for each metric. The
# duplication is intentional: pulling the descriptions in here would cross
# the coordinator → sensor layer boundary, and pulling the keys out into
# `const.py` would split a single concept across two files. Keep them in
# sync by hand — a metric without a sensor description is harmless dead
# data, but tests catch the inverse (description without a metric source).
_BODY_METRIC_KEYS: tuple[str, ...] = (
    "body_mass_index",
    "body_fat_percentage",
    "fat_free_mass",
    "body_water_percentage",
    "skeletal_muscle_percentage",
    "bone_mass",
    "muscle_mass",
    "protein_percentage",
    "basal_metabolic_rate",
)


# Country codes whose default time format is 12-hour. Used as a fallback
# heuristic when ``hass.config.time_format`` is not explicitly set.
_COUNTRY_12H = frozenset({"US", "CA", "PH", "AU"})


def _norm_country_code(config: Any) -> str:
    """Extract normalized 2-letter country code from HA config."""
    raw = (getattr(config, "country", None) or "").upper()
    return raw[:2] if len(raw) >= 2 else ""


class BluetoothNotAvailableError(Exception):
    """Exception raised when no Bluetooth adapter or ESPHome proxy is available."""

    pass


class BleakScannerESPHome(BaseBleakScanner):
    """
    A BLE scanner implementation that uses ESPHome devices as Bluetooth proxies.

    This scanner connects to one or more ESPHome devices with Bluetooth proxy capability
    and uses them to scan for Bluetooth advertisements. This allows for extended range
    and coverage compared to a single local Bluetooth adapter.
    """

    def __init__(
        self,
        detection_callback: Callable[[BLEDevice, AdvertisementData], None] | None,
        service_uuids: list[str] | None,
        scanning_mode: Literal["active", "passive"],
        clients: list[APIClient],
        **kwargs,
    ):
        """
        Initialize the ESPHome scanner.

        Args:
            detection_callback: Function called when a device advertisement is detected.
            service_uuids: Optional list of service UUIDs to filter advertisements.
            scanning_mode: Whether to use active or passive scanning.
            clients: list of ESPHome API clients to use as Bluetooth proxies.
            **kwargs: Additional arguments (not used).
        """
        super().__init__(detection_callback, service_uuids)

        self._clients = list(clients)
        self._scanning = False

        # Per-client tracking
        self._client_info: dict[APIClient, DeviceInfo | None] = {
            client: None for client in self._clients
        }
        self._client_features: dict[APIClient, int] = {
            client: 0 for client in self._clients
        }
        self._client_unsubscribers: dict[APIClient, Callable[[], None] | None] = {
            client: None for client in self._clients
        }
        self._active_clients: dict[APIClient, dict[str, Any]] = {}

    async def start(self) -> None:
        """Start scanning for devices with enhanced error handling."""
        if self._scanning:
            return

        if not self._clients:
            raise BleakError("No ESPHome clients provided")

        # Track initialization success
        successful_clients = 0

        # Initialize all clients
        for client in self._clients:
            try:
                # Check if client is connected
                if hasattr(client, "is_connected") and not client.is_connected:
                    _LOGGER.warning(
                        "Client %s is not connected, skipping", client.address
                    )
                    continue

                # Get device info with timeout
                try:
                    self._client_info[client] = await asyncio.wait_for(
                        client.device_info(), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    _LOGGER.error("Timeout getting device info from %s", client.address)
                    continue

                # Detect Bluetooth features
                self._client_features[client] = self._detect_bluetooth_features(client)

                # Check if the client supports Bluetooth proxy
                supports_proxy = False
                try:
                    supports_proxy = await asyncio.wait_for(
                        self._supports_bluetooth_proxy(client), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    _LOGGER.error(
                        "Timeout checking Bluetooth proxy support for %s",
                        client.address,
                    )
                    continue

                if supports_proxy:
                    self._active_clients[client] = {
                        "name": self._client_info[client].name,
                        "features": self._client_features[client],
                    }

                    # Subscribe to advertisements with error handling
                    try:
                        self._subscribe_to_advertisements(client)
                        successful_clients += 1
                    except Exception as ex:
                        _LOGGER.error(
                            "Failed to subscribe to advertisements for %s: %s",
                            client.address,
                            ex,
                        )
                        continue

                    _LOGGER.debug(
                        "Client %s initialized with features: %s",
                        self._client_info[client].name,
                        self._client_features[client],
                    )
                else:
                    _LOGGER.warning(
                        "Client %s does not support Bluetooth proxy, skipping",
                        client.address,
                    )
            except Exception as ex:
                _LOGGER.warning(
                    "Failed to initialize client %s: %s", client.address, ex
                )

        # Check if we have any active clients
        if not successful_clients:
            raise BleakError(
                "No ESPHome clients support Bluetooth proxy or all initializations failed"
            )

        # Clear the list of seen devices
        self.seen_devices = {}

        self._scanning = True
        _LOGGER.debug(
            "ESPHome scanner started with %d active clients out of %d clients",
            successful_clients,
            len(self._clients),
        )

    async def stop(self) -> None:
        """Stop scanning for devices."""
        if not self._scanning:
            return

        # Unsubscribe from all clients
        for client, unsubscribe in self._client_unsubscribers.items():
            if unsubscribe:
                try:
                    unsubscribe()
                    _LOGGER.debug("Unsubscribed from client %s", client.address)
                except Exception as ex:
                    _LOGGER.warning(
                        "Error unsubscribing from client %s: %s", client.address, ex
                    )
                self._client_unsubscribers[client] = None

        # Clear active clients
        self._active_clients.clear()

        self._scanning = False
        _LOGGER.debug("ESPHome scanner stopped")

    def set_scanning_filter(self, **kwargs) -> None:
        """Set scanning filter for the scanner.

        Note: ESPHome doesn't support additional filters beyond
        the service_uuids provided at initialization.
        """
        # ESPHome doesn't support additional filters
        pass

    def _on_bluetooth_le_advertisement(
        self, client: APIClient, adv: BluetoothLEAdvertisement
    ) -> None:
        """Handle a Bluetooth LE advertisement from a specific client."""
        # Skip if we're filtering by service UUID and this device doesn't match
        if not self.is_allowed_uuid(adv.service_uuids):
            return

        # Create the advertisement data
        advertisement_data = AdvertisementData(
            local_name=adv.name,
            manufacturer_data=adv.manufacturer_data,
            service_data=adv.service_data,
            service_uuids=adv.service_uuids,
            tx_power=adv.tx_power,
            rssi=adv.rssi,
            platform_data=(
                adv,
                client,
            ),  # Store both the advertisement and the source client
        )

        # Update the device in our seen_devices dictionary
        address = int_to_bluetooth_address(adv.address)
        try:
            device = self.create_or_update_device(
                address,
                address,
                adv.name or "",
                adv.manufacturer_data,
                advertisement_data,
            )
        except TypeError:
            # Fallback for older versions of create_or_update_device
            _LOGGER.debug(
                "Using fallback create_or_update_device for bleak version < 1.0.0"
            )
            device = self.create_or_update_device(
                address,
                adv.name or "",
                adv.manufacturer_data,
                advertisement_data,
            )

        # Call the detection callbacks
        self.call_detection_callbacks(device, advertisement_data)

    def _on_bluetooth_le_raw_advertisement(self, client: APIClient, response) -> None:
        """Handle raw Bluetooth LE advertisements from a specific client."""
        if not hasattr(response, "advertisements"):
            _LOGGER.warning(
                "Received raw advertisement response with unknown format from %s: %s",
                client.address,
                response,
            )
            return

        for adv in response.advertisements:
            # Convert the numeric address to a string MAC address
            address = int_to_bluetooth_address(adv.address)
            rssi = adv.rssi

            # Parse the advertisement data using bluetooth_data_tools
            try:
                local_name, service_uuids, service_data, manufacturer_data, tx_power = (
                    parse_advertisement_data_tuple((adv.data,))
                )
            except Exception as ex:
                _LOGGER.debug(
                    "Error parsing advertisement data from %s for %s: %s",
                    client.address,
                    address,
                    ex,
                )
                continue

            # Skip if we're filtering by service UUID and this device doesn't match
            if not self.is_allowed_uuid(service_uuids):
                continue

            # Create the advertisement data
            advertisement_data = AdvertisementData(
                local_name=local_name,
                manufacturer_data=manufacturer_data,
                service_data=service_data,
                service_uuids=service_uuids,
                tx_power=tx_power,
                rssi=rssi,
                platform_data=(
                    adv,
                    client,
                ),  # Store both the advertisement and the source client
            )

            # Update the device in our seen_devices dictionary
            try:
                device = self.create_or_update_device(
                    address,
                    address,
                    local_name or "",
                    manufacturer_data,
                    advertisement_data,
                )
            except TypeError:
                # Fallback for older versions of create_or_update_device
                _LOGGER.debug(
                    "Using fallback create_or_update_device for bleak version < 1.0.0"
                )
                device = self.create_or_update_device(
                    address,
                    local_name or "",
                    manufacturer_data,
                    advertisement_data,
                )

            # Call the detection callbacks
            self.call_detection_callbacks(device, advertisement_data)

    def _subscribe_to_advertisements(self, client: APIClient) -> None:
        """Subscribe to the appropriate advertisement type based on features for a specific client."""
        features = self._client_features[client]

        if features & BluetoothProxyFeature.RAW_ADVERTISEMENTS:
            self._client_unsubscribers[client] = (
                client.subscribe_bluetooth_le_raw_advertisements(
                    partial(self._on_bluetooth_le_raw_advertisement, client)
                )
            )
            _LOGGER.debug("%s: Subscribed to raw advertisements", client.address)
        else:
            self._client_unsubscribers[client] = (
                client.subscribe_bluetooth_le_advertisements(
                    partial(self._on_bluetooth_le_advertisement, client)
                )
            )
            _LOGGER.debug("%s: Subscribed to processed advertisements", client.address)

    def _detect_bluetooth_features(self, client: APIClient) -> int:
        """Detect supported Bluetooth features for a specific client."""
        device_info = self._client_info.get(client)
        if not device_info:
            return 0

        # Check if the device info has the bluetooth_proxy_feature_flags_compat method
        if hasattr(device_info, "bluetooth_proxy_feature_flags_compat"):
            return device_info.bluetooth_proxy_feature_flags_compat(client.api_version)

        # Fallback detection based on features list
        features = device_info.features if device_info else []
        if any("bluetooth" in feature.lower() for feature in features):
            # Assume basic support if "bluetooth" is mentioned in features
            return BluetoothProxyFeature.ACTIVE_CONNECTIONS

        return 0

    async def _supports_bluetooth_proxy(self, client: APIClient) -> bool:
        """Check if a specific ESPHome client supports Bluetooth proxy.

        Args:
            client: The APIClient to check

        Returns:
            bool: True if the client supports Bluetooth proxy, False otherwise.
        """
        # If we've already detected features, use that information
        if self._client_features.get(client, 0) > 0:
            return True

        # Otherwise try to detect features
        if client not in self._client_info or not self._client_info[client]:
            try:
                self._client_info[client] = await client.device_info()
                self._client_features[client] = self._detect_bluetooth_features(client)
                if self._client_features[client] > 0:
                    return True
            except Exception as ex:
                _LOGGER.debug(
                    "Error getting device info for %s: %s", client.address, ex
                )
                return False

        # Fallback to subscription test
        try:
            unsub = client.subscribe_bluetooth_le_advertisements(lambda _: None)
            unsub()
            return True
        except Exception:
            return False


class BleakScannerHybrid(BaseBleakScanner):
    """
    A hybrid BLE scanner that combines native scanning with ESPHome proxies.

    This scanner uses both the local Bluetooth adapter and ESPHome devices with
    Bluetooth proxy capability to scan for advertisements. This provides the best
    coverage by combining local and remote scanning capabilities.
    """

    def __init__(
        self,
        detection_callback: Callable[[BLEDevice, AdvertisementData], None] | None,
        service_uuids: list[str] | None,
        scanning_mode: Literal["active", "passive"],
        clients: list[APIClient],
        adapter: str | None = None,
        **kwargs,
    ):
        """
        Initialize the hybrid scanner.

        Args:
            detection_callback: Function called when a device advertisement is detected.
            service_uuids: Optional list of service UUIDs to filter advertisements.
            scanning_mode: Whether to use active or passive scanning.
            clients: list of ESPHome API clients to use as Bluetooth proxies.
            adapter: The Bluetooth adapter to use for native scanning (Linux only).
            **kwargs: Additional arguments passed to the native scanner.
        """
        super().__init__(None, service_uuids)

        self._native_scanner = None
        self._proxy_scanner = None
        self._scanners: list[BaseBleakScanner] = []
        self._scanning = False

        # Try to create native scanner
        try:
            PlatformBleakScanner, _ = get_platform_scanner_backend_type()
            scanner_kwargs: dict[str, Any] = {
                "bluez": {},
                "cb": {},
            }
            if IS_LINUX:
                # Only Linux supports multiple adapters
                if adapter:
                    scanner_kwargs["adapter"] = adapter
                if scanning_mode == BluetoothScanningMode.PASSIVE:
                    scanner_kwargs["bluez"] = PASSIVE_SCANNER_ARGS
            elif IS_MACOS:
                # We want mac address on macOS
                scanner_kwargs["cb"] = {"use_bdaddr": True}

            self._native_scanner = PlatformBleakScanner(
                detection_callback,
                service_uuids,
                scanning_mode,
                **scanner_kwargs,
            )
            self._scanners.append(self._native_scanner)
            _LOGGER.debug("Native scanner initialized successfully")
        except Exception as ex:
            _LOGGER.warning("Failed to initialize native scanner: %s", ex)

        # Try to create proxy scanner
        try:
            if clients:
                self._proxy_scanner = BleakScannerESPHome(
                    detection_callback, service_uuids, scanning_mode, clients=clients
                )
                self._scanners.append(self._proxy_scanner)
                _LOGGER.debug("Proxy scanner initialized successfully")
            else:
                _LOGGER.warning("No ESPHome clients provided for proxy scanner")
        except Exception as ex:
            _LOGGER.warning("Failed to initialize proxy scanner: %s", ex)

        # Check if we have at least one scanner
        if not self._scanners:
            raise BleakError("Failed to initialize any scanner (native or proxy)")

        self.seen_devices = {}

    async def start(self) -> None:
        """Start scanning for devices.

        Tolerates a partially-failing scanner instead of failing the whole
        hybrid start. Previously a bare ``asyncio.gather`` here meant that if
        one scanner's ``start()`` raised (e.g. native passive scanning
        unavailable: "passive scanning on Linux requires BlueZ >= 5.56 with
        --experimental enabled"), the exception propagated up while the
        *other* scanner (e.g. the ESPHome proxy) had already finished
        starting — but because ``self._scanning`` was never set to ``True``,
        the ``except`` branch's call to ``self.stop()`` was a no-op (``stop``
        early-returns when ``_scanning`` is falsy). The already-started
        scanner was never stopped, leaking a live subscription. Each
        subsequent registration-change restart (see
        ``_async_registration_changed``) constructed a fresh
        ``BleakScannerHybrid`` on top of the still-running leaked one, so
        every incoming advertisement batch was processed once per leaked
        instance — after enough restarts this alone saturated the event
        loop, independent of restart frequency.

        Now each scanner starts independently; a scanner whose ``start()``
        raises is stopped explicitly and dropped, and scanning continues
        with whichever scanners did start. Only if none of them start do we
        raise.
        """
        if self._scanning:
            return

        if not self._scanners:
            raise BleakError("No scanners available")

        # Start all scanners concurrently, collecting failures instead of
        # letting the first one abort everything.
        results = await asyncio.gather(
            *[scanner.start() for scanner in self._scanners],
            return_exceptions=True,
        )

        started: list[BaseBleakScanner] = []
        for scanner, result in zip(self._scanners, results):
            if isinstance(result, BaseException):
                _LOGGER.warning(
                    "Failed to start %s (%s); continuing without it",
                    type(scanner).__name__,
                    result,
                )
                # The scanner's own start() failed, but it may still have
                # partially initialized (e.g. subscribed to advertisements)
                # before raising. Stop it explicitly rather than relying on
                # BleakScannerHybrid.stop(), which no-ops until
                # self._scanning is True.
                try:
                    await scanner.stop()
                except Exception:  # noqa: BLE001
                    pass
                if scanner is self._native_scanner:
                    self._native_scanner = None
                if scanner is self._proxy_scanner:
                    self._proxy_scanner = None
            else:
                started.append(scanner)

        self._scanners = started

        if not started:
            _LOGGER.error("Error starting hybrid scanner: no scanner could start")
            raise BleakError("Failed to start any scanner")

        self._scanning = True
        _LOGGER.debug(
            "Hybrid scanner started with %s and %s",
            "native scanner"
            if self._native_scanner in self._scanners
            else "no native scanner",
            "proxy scanner"
            if self._proxy_scanner in self._scanners
            else "no proxy scanner",
        )

    async def stop(self) -> None:
        """Stop scanning for devices.

        Stops every scanner in ``self._scanners`` unconditionally, not
        gated behind ``self._scanning``. A hybrid that never finished
        starting (e.g. ``self._scanning`` still ``False`` for any reason
        other than the already-handled partial-failure path in
        ``start()``) must still have its children stopped — otherwise a
        live, subscribed scanner could again be stranded, as happened
        before ``start()`` was fixed to clean up after itself.
        """
        for scanner in self._scanners:
            try:
                await scanner.stop()
                _LOGGER.debug(f"Stopped scanner: {type(scanner).__name__}")
            except Exception as ex:
                _LOGGER.warning(f"Error stopping {type(scanner).__name__}: {ex}")

        self._scanning = False
        _LOGGER.debug("Hybrid scanner stopped")

    def set_scanning_filter(self, **kwargs) -> None:
        """Set scanning filter for the scanner."""
        for scanner in self._scanners:
            try:
                scanner.set_scanning_filter(**kwargs)
            except Exception as ex:
                _LOGGER.warning(
                    f"Error setting filter on {type(scanner).__name__}: {ex}"
                )

    def register_detection_callback(
        self, callback: AdvertisementDataCallback | None
    ) -> Callable[[], None]:
        for scanner in self._scanners:
            try:
                scanner.register_detection_callback(callback)
            except Exception as ex:
                _LOGGER.exception(
                    f"Error registering detection callback on {type(scanner).__name__}: {ex}"
                )

    @property
    def seen_devices(self) -> dict[str, tuple[BLEDevice, AdvertisementData]]:
        """Get the dictionary of seen devices."""
        seen: dict[str, tuple[BLEDevice, AdvertisementData]] = {}

        for scanner in self._scanners:
            seen |= scanner.seen_devices

        return seen

    @seen_devices.setter
    def seen_devices(
        self, value: dict[str, tuple[BLEDevice, AdvertisementData]]
    ) -> None:
        """Set the dictionary of seen devices."""
        # This is intentionally a no-op as we don't want to override
        # the seen devices of individual scanners
        pass


class ScaleDataUpdateCoordinator:
    """Coordinator for a single Renpho scale (ES-CS20M and compatible variants).

    Owns:
        - the BLE scanner (potentially via ESPHome proxy)
        - the `RenphoESCS20MScale` library client
        - the `WeightRouter` for user-history-based matching
        - listener callback registries (per-user, all-users, diagnostic)
        - pending-measurement bookkeeping (in-memory only)
    """

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        user_profiles: list[dict],
        device_name: str,
        router_state: dict | None = None,
        pending_state: dict | None = None,
        history_retention_days: int = HISTORY_RETENTION_DAYS,
        max_history_size: int = MAX_HISTORY_SIZE,
        enable_library_logging: bool = False,
        protocol: str = PROTOCOL_QN,
    ) -> None:
        """Construct the coordinator.

        Args:
            hass: Home Assistant instance.
            address: Bluetooth MAC address of the scale.
            user_profiles: List of user-profile dicts from `entry.data`.
            device_name: Human-friendly device name from `entry.title`.
            router_state: Optional `WeightRouter.to_dict()` payload to restore.
            history_retention_days: Per-user history retention window.
            max_history_size: Per-user maximum history size.
            protocol: BLE protocol the scale speaks (`PROTOCOL_QN` for the
                connectable GATT variant, `PROTOCOL_AABB` for the broadcast-only
                weight-only variant). Selects the library scale class.
        """
        self.hass = hass
        self.address = address
        self.device_name = device_name
        self._protocol = protocol
        self._user_profiles = deepcopy(user_profiles)
        # Note: dict values are aliased into _user_profiles. Mutate user-profile
        # dicts via one of these collections only, never via both, to avoid
        # surprising shared-state bugs.
        self._user_profiles_by_id = {p[CONF_USER_ID]: p for p in self._user_profiles}

        # Listener registries (populated by add_*_listener methods).
        self._broadcast_listeners: list[Callable[[ScaleData], None]] = []
        self._user_callbacks: dict[str, list[Callable[[ScaleData], None]]] = {}
        self._diagnostic_listeners: list[Callable[[], None]] = []

        # BLE client; created on demand by `async_start`. Concrete type depends
        # on `self._protocol` (RenphoQNScale or RenphoAABBScale).
        self._scale: RenphoScale | None = None
        self._enable_library_logging = enable_library_logging
        # Unsubscribe handle for the HA `core_config_updated` listener that
        # re-renders cached `timestamp_display` values when relevant settings
        # change (time zone, time_format, country). Set in `async_start`,
        # cleared in `async_stop`.
        self._unsub_core_config_update: Callable[[], None] | None = None
        # Unregister handle for HA's BT-scanner-registration callback. Set
        # when a scanner change handler is registered in `async_start` and
        # cleared in `async_stop`.
        self._scanner_change_cb_unregister: Callable[[], None] | None = None
        # Serializes scale-client (re)starts. Without this, a registration
        # change event firing during the initial start could double-start
        # the client.
        self._start_lock = asyncio.Lock()
        # Backoff state for scanner-change restarts (see
        # `_async_registration_changed`). Counts consecutive `_async_start`
        # failures and holds the cancel handle of the (single, coalesced)
        # scheduled retry.
        self._restart_failures = 0
        self._restart_retry_unsub: Callable[[], None] | None = None
        # Monotonic timestamp of the last `_async_start` attempt made from
        # `_async_registration_changed`, used to enforce
        # `REGISTRATION_PREEMPT_DEBOUNCE_SECONDS` on the pre-emption path.
        self._last_restart_attempt_monotonic: float | None = None
        self._display_unit: WeightUnit = WeightUnit.KG
        self._config_entry_id: str | None = None

        # Pending measurements: measurement_id -> {weight_kg, resistance_1,
        # resistance_2, body_fat, candidates, timestamp_iso, raw_measurement}
        self._pending_measurements: dict[str, dict[str, Any]] = {}

        # Most recent resolver decision: cached so the on_scale_data callback
        # knows which user_id the current measurement was committed to.
        self._last_resolved_user_id: str | None = None

        # measurement_ids that currently have an open ambiguous-measurement
        # persistent notification.
        self._ambiguous_notifications: set[str] = set()

        # Restore pending measurements from a prior session if any.
        # `_restore_pending` overwrites `_pending_measurements` and seeds
        # `_ambiguous_notifications` so dismissal still works after reload.
        if pending_state:
            self._restore_pending(pending_state)

        # WeightRouter setup
        config = RouterConfig(
            history_retention_days=history_retention_days,
            max_history_size=max_history_size,
        )
        if router_state is not None:
            self._router = WeightRouter.from_dict(router_state)
            self._router.set_config(config)
        else:
            self._router = WeightRouter(config=config)
        self._router.set_users(
            [
                UserProfile(user_id=p[CONF_USER_ID], display_name=p[CONF_USER_NAME])
                for p in self._user_profiles
            ]
        )

    def get_user_profiles(self) -> list[dict]:
        """Return a copy of the current user-profile list."""
        return deepcopy(self._user_profiles)

    def set_config_entry_id(self, entry_id: str) -> None:
        """Cache the config entry id for `_update_config_entry` calls later."""
        self._config_entry_id = entry_id

    def check_bluetooth_available(self) -> None:
        """Raise `BluetoothNotAvailableError` if no usable BT scanner exists.

        Called from `async_setup_entry` so HA can raise `ConfigEntryNotReady`
        and back off retries until the BT stack initializes.
        """
        from homeassistant.components import bluetooth

        if bluetooth.async_scanner_count(self.hass, connectable=True) == 0:
            raise BluetoothNotAvailableError(
                "No connectable Bluetooth scanner available"
            )

    async def _get_bluetooth_scanner(self) -> BaseBleakScanner | None:
        """Get the optimal Bluetooth scanner based on available resources.

        Returns:
            A configured Bluetooth scanner or None if no scanner could be created.

        Raises:
            BluetoothNotAvailableError: If bluetooth_manager is not available or
                no Bluetooth adapter/proxy is available.
        """
        try:
            manager = self.hass.data.get("bluetooth_manager")
            if not manager:
                _LOGGER.debug("Bluetooth manager not available yet")
                raise BluetoothNotAvailableError(
                    "Bluetooth manager not available - Bluetooth integration may still be initializing"
                )

            # Get Bluetooth sources
            sources = manager._sources
            native = False

            # Check for native adapters with better error handling
            try:
                for adapter in manager._bluetooth_adapters.adapters.values():
                    if sources.get(adapter["address"]) is not None:
                        native = True
                        _LOGGER.debug("Found native Bluetooth adapter: %s", adapter)
                        break
                if not native:
                    for adapter in manager._bluetooth_adapters.adapters.keys():
                        if sources.get(adapter) is not None:
                            native = True
                            _LOGGER.debug("Found native Bluetooth adapter: %s", adapter)
                            break
            except (AttributeError, KeyError) as err:
                _LOGGER.warning("Error checking native Bluetooth adapters: %s", err)
                native = False

            # Get ESPHome proxies with error handling
            esphome_clients: list[APIClient] = []
            try:
                proxies = [
                    item.data["source"]
                    for item in self.hass.config_entries.async_entries("bluetooth")
                    if item.data.get("source_domain") == "esphome"
                ]
                esphome_clients = [
                    sources.get(s).connector.client.keywords["client_data"].client
                    for s in proxies
                    if sources.get(s)
                ]
                _LOGGER.debug(
                    "Found %d ESPHome Bluetooth proxies", len(esphome_clients)
                )
            except (AttributeError, KeyError, TypeError) as err:
                _LOGGER.warning("Error getting ESPHome clients: %s", err)
                esphome_clients = []

            # Initialize scanner with error handling
            scanner: BaseBleakScanner | None = None
            if len(esphome_clients) > 0:
                try:
                    if native:
                        scanner = BleakScannerHybrid(
                            None,
                            None,
                            BluetoothScanningMode.PASSIVE,
                            esphome_clients,
                        )
                        _LOGGER.debug(
                            "Created hybrid scanner with native and proxy support"
                        )
                    else:
                        scanner = BleakScannerESPHome(
                            None,
                            None,
                            BluetoothScanningMode.PASSIVE,
                            esphome_clients,
                        )
                        _LOGGER.debug("Created ESPHome proxy scanner")
                except BleakError as err:
                    _LOGGER.warning("Failed to initialize Bluetooth scanner: %s", err)
                    scanner = None
                except Exception as ex:
                    _LOGGER.exception(
                        "Unexpected error creating Bluetooth scanner: %s", ex
                    )
                    scanner = None
            elif not native:
                # No ESPHome proxies AND no native adapter = no Bluetooth available
                raise BluetoothNotAvailableError(
                    "No Bluetooth adapter or ESPHome proxy available"
                )

            return scanner
        except BluetoothNotAvailableError:
            # Re-raise to be handled by caller
            raise
        except Exception as ex:
            _LOGGER.exception("Error getting Bluetooth scanner: %s", ex)
            return None

    # ------------------------------------------------------------------
    # Display unit
    # ------------------------------------------------------------------

    def set_display_unit(self, unit: WeightUnit) -> None:
        """Set the desired display unit on the scale.

        If the library client is alive, propagate to it; the library will send
        the unit-update command on next connection.
        """
        self._display_unit = unit
        if self._scale is not None:
            self._scale.display_unit = unit

    def get_display_unit(self) -> WeightUnit:
        """Return the cached display unit."""
        return self._display_unit

    # ------------------------------------------------------------------
    # Profile / resolver
    # ------------------------------------------------------------------

    def _build_profile_for_user(self, user_profile: dict) -> Profile | None:
        """Construct a `renpho_escs20m.Profile` from a stored user profile dict.

        Returns ``None`` if the user has body metrics disabled or is missing
        any required field. The caller treats ``None`` as "use weight-only
        mode for this user".
        """
        if not user_profile.get(CONF_BODY_METRICS_ENABLED):
            return None
        height_cm = user_profile.get(CONF_HEIGHT)
        sex_str = user_profile.get(CONF_SEX)
        birthdate_str = user_profile.get(CONF_BIRTHDATE)
        if not height_cm or not sex_str or not birthdate_str:
            return None
        from datetime import date as _date

        try:
            birthdate = _date.fromisoformat(birthdate_str)
        except (TypeError, ValueError):
            return None
        # Birthday-aware age (matches the Renpho app's UI age semantics).
        today = _date.today()
        age = (
            today.year
            - birthdate.year
            - ((today.month, today.day) < (birthdate.month, birthdate.day))
        )
        # Case-insensitive: stored values are now "male"/"female", but
        # accept legacy capitalized values from any pre-0.1.0 dev profile.
        sex = Sex.Female if (sex_str or "").lower() == "female" else Sex.Male
        algorithm = (
            ALGORITHM_ALTERNATIVE
            if user_profile.get(CONF_USE_ALTERNATIVE_ALGORITHM)
            else ALGORITHM_DEFAULT
        )
        return Profile(
            sex=sex,
            age=age,
            height_m=height_cm / 100.0,
            algorithm=algorithm,
            athlete=bool(user_profile.get(CONF_ATHLETE, False)),
        )

    def _filter_candidates_by_location(self, candidate_ids: list[str]) -> list[str]:
        """Filter candidate user_ids by their linked person.X entity state.

        Excludes users whose linked person entity is in state ``not_home``.
        Users with no linked person, or whose entity is missing or in any
        other state, are kept. If filtering empties the list, returns the
        original list (so the measurement is never silently dropped).
        """
        kept: list[str] = []
        for user_id in candidate_ids:
            profile = self._user_profiles_by_id.get(user_id)
            if not profile:
                continue
            person_entity = profile.get(CONF_PERSON_ENTITY)
            if not person_entity:
                kept.append(user_id)
                continue
            state = self.hass.states.get(person_entity)
            if state is None:
                kept.append(user_id)
                continue
            if state.state.lower() == "not_home":
                continue
            kept.append(user_id)
        return kept if kept else candidate_ids

    async def _resolve_user(self, weight_kg: float) -> Profile | None:
        """Async ProfileResolver passed to `RenphoESCS20MScale`.

        Called once per measurement at first stable weight. Returns a Profile
        only when exactly one candidate is identified AND that user has body
        metrics enabled. Otherwise returns None (zero candidates, multiple
        candidates, or single candidate without body metrics). In all
        "exactly one candidate" cases, ``self._last_resolved_user_id`` is
        cached so ``_on_scale_data`` can route to that user even when no
        Profile was returned.
        """
        from datetime import datetime, timezone

        synthetic = WeightMeasurement(
            weight_kg=weight_kg,
            timestamp=datetime.now(tz=timezone.utc),
            source_id=self.address,
        )
        try:
            ranked = self._router.evaluate_measurement(synthetic)
        except Exception:
            _LOGGER.exception("Router evaluation failed; falling back to weight-only")
            self._last_resolved_user_id = None
            return None
        candidate_ids = [c.user_id for c in ranked]
        filtered = self._filter_candidates_by_location(candidate_ids)
        if len(filtered) != 1:
            _LOGGER.debug(
                "Resolver: %d candidate(s) after location filter — weight-only",
                len(filtered),
            )
            self._last_resolved_user_id = None
            return None
        user_id = filtered[0]
        self._last_resolved_user_id = user_id
        profile = self._user_profiles_by_id.get(user_id)
        if profile is None:
            return None
        return self._build_profile_for_user(profile)

    # ------------------------------------------------------------------
    # Library mode selection + lifecycle
    # ------------------------------------------------------------------

    def _select_library_mode_argument(self) -> Profile | ProfileResolver | None:
        """Decide what to pass as ``profile=`` to ``RenphoESCS20MScale``.

        - 0 users (shouldn't happen — config flow enforces ≥1) -> None
          (weight-only, defensive)
        - 1 user, body metrics off  -> None (weight-only)
        - 1 user, body metrics on   -> Profile (fixed-user)
        - 2+ users                  -> _resolve_user (async resolver)
        """
        if not self._user_profiles:
            _LOGGER.warning(
                "Coordinator started with no users; falling back to "
                "weight-only mode. The config flow should have prevented this."
            )
            return None
        if len(self._user_profiles) == 1:
            return self._build_profile_for_user(self._user_profiles[0])
        return self._resolve_user

    async def async_start(self) -> None:
        """Set up persistent listeners and start the scale client.

        Registers two HA-level callbacks:

        - ``core_config_updated`` — refreshes cached ``timestamp_display``
          values when the user changes time zone, time format, or country.
        - BT scanner-registration change — restarts the scale client when
          ESPHome BT proxies come online or offline mid-session, so a
          newly-added proxy is picked up without needing an integration
          reload.

        Both unregister handles are stored on the instance and cleared
        in ``async_stop``. The actual client construction happens in
        ``_async_start`` under ``_start_lock`` so it's safe to call again
        from the registration-change handler.
        """
        if self._unsub_core_config_update is None:
            self._unsub_core_config_update = self.hass.bus.async_listen(
                "core_config_updated", self._on_core_config_updated
            )
        # Defensive cleanup of any stale registration before re-registering.
        if self._scanner_change_cb_unregister is not None:
            self._scanner_change_cb_unregister()
            self._scanner_change_cb_unregister = None
        bluetooth_manager = self.hass.data.get("bluetooth_manager")
        if bluetooth_manager is not None:
            self._scanner_change_cb_unregister = (
                bluetooth_manager.async_register_scanner_registration_callback(
                    self._registration_changed, None
                )
            )
        async with self._start_lock:
            await self._async_start()

    async def _async_start(self) -> None:
        """(Re)build the BLE pipeline.

        Stops any existing scale client, picks the best available scanner,
        and starts a fresh ``RenphoESCS20MScale``. Called from
        ``async_start`` on first setup and from
        ``_async_registration_changed`` when scanners change at runtime.
        Must be invoked under ``_start_lock`` to serialize concurrent
        restart attempts.
        """
        if self._scale is not None:
            try:
                await self._scale.async_stop()
            except Exception:
                _LOGGER.exception("Error stopping existing scale client")
            self._scale = None
        scanner = await self._get_bluetooth_scanner()

        try:
            ScaleProtocol(self._protocol)
        except ValueError:
            _LOGGER.warning(
                "Unknown persisted protocol %r, falling back to QN",
                self._protocol,
            )
            self._protocol = PROTOCOL_QN

        # Not collapsed to a single `SCALE_CLASSES[...]` construction: the two
        # constructor surfaces aren't identical (QN takes `profile`,
        # `clear_stored_measurements`, and an explicit `cooldown_seconds`;
        # AABB takes none of those and is left at the library's own
        # `cooldown_seconds` default). Passing one class's kwargs to the
        # other has broken a scale before — keep the branch.
        if self._protocol == PROTOCOL_AABB:
            # Broadcast-only variant: weight is read from BLE advertisements.
            # No GATT connection, so the profile/connect-retry knobs don't
            # apply. The advertisement cooldown (one callback per weigh-in,
            # suppressing the re-broadcast burst of the final frame) is left
            # at the library's default — burst length is hardware knowledge
            # the library owns. Weight-history routing still happens post-hoc
            # in `_on_scale_data`; there is no body composition to resolve at
            # weigh time, so no ProfileResolver is needed.
            self._scale = RenphoAABBScale(
                address=self.address,
                notification_callback=self._on_scale_data,
                display_unit=self._display_unit,
                bleak_scanner_backend=scanner,
                logger=self._library_logger(),
            )
        else:
            profile_arg = self._select_library_mode_argument()
            self._scale = RenphoQNScale(
                address=self.address,
                notification_callback=self._on_scale_data,
                display_unit=self._display_unit,
                profile=profile_arg,
                bleak_scanner_backend=scanner,
                # Drain the scale's store of offline measurements (readings
                # taken while HA was down) each session. Delivery deletes
                # them from the scale.
                clear_stored_measurements=True,
                # The scale's BLE chip continues to emit straggler advertisements
                # for a few seconds after a successful measurement while it's
                # spinning down — but it rejects fresh connections during that
                # window. Without a cooldown, each straggler ad triggers a futile
                # 10-retry connect cycle that ends with `BleakOutOfConnectionSlotsError`.
                cooldown_seconds=POST_MEASUREMENT_COOLDOWN_SECONDS,
                logger=self._library_logger(),
            )
        await self._scale.async_start()

    @callback
    def _registration_changed(self, _registration: Any) -> None:
        """Sync entry point for HA's scanner-registration callback.

        HA fires this when a connectable scanner (local adapter or ESPHome
        BT proxy) is added or removed. We schedule the async restart so
        the new scanner gets picked up.
        """
        self.hass.async_create_task(self._async_registration_changed())

    async def _async_registration_changed(self) -> None:
        """Restart the scale client to pick up a new/changed scanner.

        Guarded by an exponential-backoff circuit breaker. Rebuilding the
        whole BLE pipeline on every scanner-registration event with no
        backoff means that once a restart starts failing persistently
        (e.g. host BlueZ without passive-scanning support), each ESPHome
        proxy reconnect triggers another full failing rebuild — and via the
        leak in ``BleakScannerHybrid.start()`` fixed above, each one adds
        more permanently-subscribed scanners on top, saturating the HA
        event loop for hours. The backoff now governs only the
        *self-scheduled* retries (the timer fired by
        ``_schedule_restart_retry``, delay growing exponentially, 30s ->
        30min cap): a *real* registration event (a scanner actually being
        added/removed) instead cancels any pending backoff retry and
        restarts immediately, since the event itself could be exactly
        what fixes the failure (e.g. an ESPHome proxy reconnecting).
        Registration events aren't rate-limited by HA itself, though, so a
        flapping/bootlooping proxy could otherwise keep pre-empting the
        backoff timer faster than the backoff is meant to allow (no leak,
        but repeated real I/O load) - `REGISTRATION_PREEMPT_DEBOUNCE_SECONDS`
        is a hard floor on that specific path. It only applies while a
        backoff retry is pending (i.e. restarts are currently failing); it
        does not throttle events while restarts are succeeding, since
        pre-emption - and therefore this risk - can't occur on that path.
        A successful restart resets the backoff.
        """
        if self._restart_retry_unsub is not None:
            # A backoff retry is already scheduled, but this is a real
            # registration-change event, not the retry timer firing (the
            # timer's own callback clears `_restart_retry_unsub` before
            # calling back in here). Normally we'd cancel the pending retry
            # and try now - but only if enough time has passed since the
            # last attempt; otherwise leave the scheduled backoff retry
            # alone so repeated events can't drive back-to-back restarts.
            # `_last_restart_attempt_monotonic` is always set by the time
            # `_restart_retry_unsub` is non-None (both happen in the same
            # call, `_last_restart_attempt_monotonic` first), so the None
            # case below is unreachable today - kept as a defensive guard
            # in case that invariant ever changes.
            since_last_attempt = (
                None
                if self._last_restart_attempt_monotonic is None
                else time.monotonic() - self._last_restart_attempt_monotonic
            )
            if (
                since_last_attempt is not None
                and since_last_attempt < REGISTRATION_PREEMPT_DEBOUNCE_SECONDS
            ):
                _LOGGER.debug(
                    "Registration-change event arrived %.1fs after the "
                    "last restart attempt (< %ds debounce floor); leaving "
                    "the scheduled backoff retry in place instead of "
                    "pre-empting it",
                    since_last_attempt,
                    REGISTRATION_PREEMPT_DEBOUNCE_SECONDS,
                )
                return
            _LOGGER.debug(
                "Scale client restart already scheduled (backoff after %d "
                "failure(s)); registration-change event pre-empting it for "
                "an immediate retry",
                self._restart_failures,
            )
            self._restart_retry_unsub()
            self._restart_retry_unsub = None
        _LOGGER.debug("BT scanner registration changed; restarting scale client")
        try:
            self._last_restart_attempt_monotonic = time.monotonic()
            async with self._start_lock:
                await self._async_start()
        except Exception:
            self._restart_failures += 1
            delay = min(
                RESTART_BACKOFF_BASE_SECONDS * (2 ** (self._restart_failures - 1)),
                RESTART_BACKOFF_MAX_SECONDS,
            )
            _LOGGER.exception(
                "Failed to restart scale client after scanner change "
                "(consecutive failure #%d); next retry in %ds",
                self._restart_failures,
                delay,
            )
            self._schedule_restart_retry(delay)
        else:
            if self._restart_failures:
                _LOGGER.info(
                    "Scale client restart succeeded after %d failed attempt(s); "
                    "resetting backoff",
                    self._restart_failures,
                )
            self._restart_failures = 0

    def _schedule_restart_retry(self, delay: float) -> None:
        """Schedule a single delayed restart retry.

        Only one retry is ever pending; `_async_registration_changed` skips
        new work while `_restart_retry_unsub` is set. The handle is cleared
        both when the timer fires and in `async_stop`.
        """

        @callback
        def _retry(_now) -> None:
            self._restart_retry_unsub = None
            self.hass.async_create_task(self._async_registration_changed())

        self._restart_retry_unsub = async_call_later(self.hass, delay, _retry)

    def _library_logger(self) -> logging.Logger:
        """Logger handed to the renpho-escs20m client.

        When the user has toggled ``enable_library_logging`` on (Advanced
        Settings), we hand the library a child logger pinned to DEBUG so
        protocol-level frames show up regardless of the integration's own
        log-level. Otherwise the library logs at the integration's level.

        Also installs ``_NoiseFilter`` to drop ``BleakOutOfConnectionSlotsError``
        records, which the library logs at ERROR with traceback. This
        exception fires whenever the scale is advertising but not actually
        connectable (typical pattern: post-measurement spin-down, or the
        scale's idle-mode beacons). It's not user-actionable from a log
        line, and the integration recovers on the next real advertisement.
        Verbose logging mode bypasses the filter so you can still see them
        when actually debugging.
        """
        logger = _LOGGER.getChild("renpho_escs20m")
        if self._enable_library_logging:
            logger.setLevel(logging.DEBUG)
            # Remove the noise filter so debug sessions see everything.
            for f in list(logger.filters):
                if isinstance(f, _NoiseFilter):
                    logger.removeFilter(f)
        else:
            # Reset to NOTSET so the parent (integration) level applies again
            # if the user previously toggled the flag on.
            logger.setLevel(logging.NOTSET)
            if not any(isinstance(f, _NoiseFilter) for f in logger.filters):
                logger.addFilter(_NoiseFilter())
        return logger

    async def async_stop(self) -> None:
        """Stop the scale client and clean up callbacks."""
        if self._unsub_core_config_update is not None:
            self._unsub_core_config_update()
            self._unsub_core_config_update = None
        if self._scanner_change_cb_unregister is not None:
            self._scanner_change_cb_unregister()
            self._scanner_change_cb_unregister = None
        # Cancel any pending backoff retry so a stopped/unloaded coordinator
        # can't restart itself later.
        if self._restart_retry_unsub is not None:
            self._restart_retry_unsub()
            self._restart_retry_unsub = None
        if self._scale is not None:
            try:
                await self._scale.async_stop()
            except Exception:
                # Benign on teardown/restart: BlueZ can report
                # `org.bluez.Error.Failed: No discovery started` when the scan
                # was already stopped. Don't let it surface as an unhandled
                # task-exception traceback.
                _LOGGER.exception("Error stopping scale client")
            self._scale = None

    # ------------------------------------------------------------------
    # Listener registries
    # ------------------------------------------------------------------

    def add_listener(self, callback: Callable[[ScaleData], None]) -> Callable[[], None]:
        """Register a callback that fires for every committed measurement,
        regardless of which user it belongs to. Returns a remover."""
        self._broadcast_listeners.append(callback)

        def _remove() -> None:
            try:
                self._broadcast_listeners.remove(callback)
            except ValueError:
                pass

        return _remove

    def add_user_listener(
        self, user_id: str, callback: Callable[[ScaleData], None]
    ) -> Callable[[], None]:
        """Register a callback for measurements belonging to a single user.

        Immediately fires the callback with the user's most recent
        measurement (with body metrics derived) if any exists, so newly
        added entities populate from history on day one — important for
        body-metric sensors after a user toggles ``body_metrics_enabled``
        on, which would otherwise stay ``unavailable`` until the next
        weigh-in.

        Returns a remover.
        """
        self._user_callbacks.setdefault(user_id, []).append(callback)
        last = self._router.get_user_last_measurement(user_id)
        if last is not None:
            data = self._scale_data_from_measurement(last)
            body_fat = data.measurements.get(BODY_FAT_KEY)
            if body_fat is None:
                body_fat = self._compute_body_fat_off_scale(data, user_id)
                if body_fat is not None:
                    data.measurements[BODY_FAT_KEY] = body_fat
            if body_fat is not None:
                self._derive_body_metrics_into(data, user_id, body_fat)
            else:
                self._maybe_compute_bmi(data, user_id)
            try:
                callback(data)
            except Exception:
                _LOGGER.exception(
                    "user listener raised on initial replay for user_id=%s",
                    user_id,
                )

        def _remove() -> None:
            try:
                self._user_callbacks.get(user_id, []).remove(callback)
            except ValueError:
                pass

        return _remove

    def add_diagnostic_listener(
        self, callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Register a no-arg callback that fires when integration-wide state
        changes (pending measurements, user directory). Returns a remover."""
        self._diagnostic_listeners.append(callback)

        def _remove() -> None:
            try:
                self._diagnostic_listeners.remove(callback)
            except ValueError:
                pass

        return _remove

    # ------------------------------------------------------------------
    # Measurement handling
    # ------------------------------------------------------------------

    def _on_scale_data(self, data: ScaleData) -> None:
        """Handle a notification from `RenphoESCS20MScale`.

        Three cases:

        - **Resolved & body fat present**: routed to the user the resolver
          picked; body metrics derived; recorded in router history.
        - **Resolved & body fat absent**: routed to the user the resolver
          picked (weight-only path); recorded in router history.
        - **Not resolved**: stored as pending; the ambiguous-notification
          flow fires (mobile push to candidate users + persistent
          notification).
        """
        from datetime import datetime, timezone

        weight_kg = data.measurements.get(WEIGHT_KEY)
        if weight_kg is None:
            _LOGGER.debug("Ignoring ScaleData with no weight")
            return
        timestamp = datetime.now(tz=timezone.utc)
        # Pre-compute the human-readable timestamp here, at record time,
        # rather than re-rendering on every attribute read. Stored in the
        # measurement's `raw` dict so it travels with the measurement
        # through router persistence; refreshed by `_on_core_config_updated`
        # if the user changes their HA time-format / timezone / country.
        timestamp_iso = timestamp.isoformat()
        measurement = WeightMeasurement(
            weight_kg=weight_kg,
            timestamp=timestamp,
            source_id=self.address,
            raw={
                "resistance_1": data.measurements.get(RESISTANCE_1_KEY),
                "resistance_2": data.measurements.get(RESISTANCE_2_KEY),
                "body_fat": data.measurements.get(BODY_FAT_KEY),
                "timestamp_display": self._format_notification_timestamp(timestamp_iso),
            },
        )
        # Decide which user this measurement is for.
        # - multi-user mode: the async resolver may have already picked one,
        #   in which case `_last_resolved_user_id` is set. Otherwise, try to
        #   resolve and if there's still no clear winner, treat as pending.
        # - fixed-user / weight-only mode (1 configured user): the library
        #   doesn't call our resolver at all, but the answer is unambiguous —
        #   route to the only user.
        user_id = self._last_resolved_user_id
        self._last_resolved_user_id = None

        candidate_ids: list[str] | None = None
        if user_id is None:
            if len(self._user_profiles) == 1:
                user_id = self._user_profiles[0][CONF_USER_ID]
            else:
                candidate_ids = self._compute_pending_candidates(measurement.weight_kg)
                if len(candidate_ids) == 1:
                    user_id = candidate_ids[0]

        if user_id is not None:
            self._commit_measurement_to_user(user_id, measurement, data)
        else:
            # Reuse the candidate list already computed above (if any) so we
            # don't run the router a second time for the same measurement.
            self._add_pending_measurement(
                measurement, data, candidate_ids=candidate_ids
            )

        # Push firmware revision into the device registry (no-op until the
        # library reads it; harmless to call repeatedly).
        self._sync_firmware_to_device_registry()
        self._notify_diagnostic_listeners()

    def _commit_measurement_to_user(
        self,
        user_id: str,
        measurement: WeightMeasurement,
        data: ScaleData,
    ) -> None:
        """Record measurement in router history and fan out to per-user sensors."""
        try:
            self._router.record_measurement_for_user(user_id, measurement)
        except Exception:
            _LOGGER.exception("Failed to record measurement for user_id=%s", user_id)
        self._emit_data_for_user(user_id, data)
        self._update_config_entry()

    def _emit_data_for_user(self, user_id: str, data: ScaleData) -> None:
        """Derive body metrics for ``data`` and fan out to listeners.

        Does NOT touch the router — caller has either already recorded the
        measurement or is replaying existing history. Used by both
        ``_commit_measurement_to_user`` and ``_replay_latest_to_user``.
        """
        body_fat = data.measurements.get(BODY_FAT_KEY)
        # If the scale didn't report body_fat (e.g., the resolver returned
        # None in multi-user mode and the scale ran the bootstrap profile
        # which has algorithm 0x00 = "no body fat calculation"), compute it
        # off-scale from resistance_1 + the user's profile.
        if body_fat is None:
            body_fat = self._compute_body_fat_off_scale(data, user_id)
            if body_fat is not None:
                data.measurements[BODY_FAT_KEY] = body_fat
        if body_fat is not None:
            self._derive_body_metrics_into(data, user_id, body_fat)
        else:
            self._maybe_compute_bmi(data, user_id)
        for cb in list(self._user_callbacks.get(user_id, [])):
            try:
                cb(data)
            except Exception:
                _LOGGER.exception("user listener raised for user_id=%s", user_id)
        for cb in list(self._broadcast_listeners):
            try:
                cb(data)
            except Exception:
                _LOGGER.exception("broadcast listener raised")

    def _scale_data_from_measurement(self, measurement: WeightMeasurement) -> ScaleData:
        """Reconstruct a ScaleData from a stored WeightMeasurement.

        Used to replay a user's most-recent measurement to sensors after
        operations like reassign / remove, where which measurement is
        "the latest" for a user has changed.
        """
        raw = measurement.raw or {}
        measurements: dict[str, str | float | None] = {
            WEIGHT_KEY: measurement.weight_kg,
        }
        if raw.get("body_fat") is not None:
            measurements[BODY_FAT_KEY] = raw["body_fat"]
        if raw.get("resistance_1") is not None:
            measurements[RESISTANCE_1_KEY] = raw["resistance_1"]
        if raw.get("resistance_2") is not None:
            measurements[RESISTANCE_2_KEY] = raw["resistance_2"]
        return ScaleData(
            name=self.device_name,
            address=self.address,
            display_unit=self._display_unit,
            measurements=measurements,
        )

    def _replay_latest_to_user(self, user_id: str) -> None:
        """Re-fire listeners with the user's current latest measurement.

        Called after operations that change which measurement is "newest"
        for a user (reassign, remove). If the user has no remaining
        history, emits an empty ScaleData to per-user listeners so their
        sensors fall back to ``unavailable``.
        """
        last = self._router.get_user_last_measurement(user_id)
        if last is None:
            empty = ScaleData(
                name=self.device_name,
                address=self.address,
                display_unit=self._display_unit,
                measurements={},
            )
            for cb in list(self._user_callbacks.get(user_id, [])):
                try:
                    cb(empty)
                except Exception:
                    _LOGGER.exception(
                        "user listener raised on empty replay for user_id=%s",
                        user_id,
                    )
            return
        self._emit_data_for_user(user_id, self._scale_data_from_measurement(last))

    def _derive_body_metrics_into(
        self, data: ScaleData, user_id: str, body_fat: float
    ) -> None:
        """Attach the 9 BodyMetrics-derived values to ``data.measurements``."""
        from renpho_escs20m import BodyMetrics

        profile = self._user_profiles_by_id.get(user_id)
        if not profile:
            return
        height_cm = profile.get(CONF_HEIGHT)
        sex_str = profile.get(CONF_SEX, "male")
        birthdate_str = profile.get(CONF_BIRTHDATE)
        if not (height_cm and birthdate_str):
            return
        from datetime import date as _date

        try:
            birthdate = _date.fromisoformat(birthdate_str)
        except (TypeError, ValueError):
            return
        today = _date.today()
        age = (
            today.year
            - birthdate.year
            - ((today.month, today.day) < (birthdate.month, birthdate.day))
        )
        # Case-insensitive: stored values are now "male"/"female", but
        # accept legacy capitalized values from any pre-0.1.0 dev profile.
        sex = Sex.Female if (sex_str or "").lower() == "female" else Sex.Male
        metrics = BodyMetrics(
            weight_kg=data.measurements[WEIGHT_KEY],
            height_m=height_cm / 100.0,
            age=age,
            sex=sex,
            body_fat_percentage=body_fat,
        )
        for key in _BODY_METRIC_KEYS:
            data.measurements[key] = getattr(metrics, key)

    def _compute_body_fat_off_scale(
        self, data: ScaleData, user_id: str
    ) -> float | None:
        """Compute body fat percentage from ``resistance_1`` + user profile.

        Used when the scale reported only weight (e.g., the measurement was
        captured under the library's bootstrap profile because the resolver
        couldn't pick a user at weigh time). Returns ``None`` if anything
        required is missing or the calculation raises.

        The renpho-escs20m library documents that ``resistance_1`` is the
        correct frame value to feed ``calculate_body_fat`` for off-scale
        approximation; ``resistance_2`` is reserved for other uses.

        The broadcast (AABB) variant transmits no impedance at all. When a full
        profile (sex + birthdate) is configured for such a user, we substitute a
        fixed synthetic resistance (``AABB_SYNTHETIC_RESISTANCE``) so the same
        anthropometric formula runs — reproducing what the official Renpho app
        shows. See ``const`` for why the exact value is immaterial.
        """
        profile = self._user_profiles_by_id.get(user_id)
        if not profile or not profile.get(CONF_BODY_METRICS_ENABLED):
            return None
        resistance = data.measurements.get(RESISTANCE_1_KEY)
        if resistance is None:
            if self._protocol == PROTOCOL_AABB:
                resistance = AABB_SYNTHETIC_RESISTANCE
            else:
                return None
        height_cm = profile.get(CONF_HEIGHT)
        sex_str = profile.get(CONF_SEX, "male")
        birthdate_str = profile.get(CONF_BIRTHDATE)
        weight_kg = data.measurements.get(WEIGHT_KEY)
        if not (height_cm and birthdate_str and weight_kg):
            return None
        from datetime import date as _date

        try:
            birthdate = _date.fromisoformat(birthdate_str)
        except (TypeError, ValueError):
            return None
        today = _date.today()
        age = (
            today.year
            - birthdate.year
            - ((today.month, today.day) < (birthdate.month, birthdate.day))
        )
        # Case-insensitive: stored values are now "male"/"female", but
        # accept legacy capitalized values from any pre-0.1.0 dev profile.
        sex = Sex.Female if (sex_str or "").lower() == "female" else Sex.Male
        algorithm = (
            ALGORITHM_ALTERNATIVE
            if profile.get(CONF_USE_ALTERNATIVE_ALGORITHM)
            else ALGORITHM_DEFAULT
        )
        from renpho_escs20m import calculate_body_fat

        try:
            return calculate_body_fat(
                weight_kg=weight_kg,
                height_m=height_cm / 100.0,
                age=age,
                sex=sex,
                resistance=int(resistance),
                algorithm=algorithm,
                athlete=bool(profile.get(CONF_ATHLETE, False)),
            )
        except (ValueError, TypeError):
            _LOGGER.exception("Off-scale body fat calculation failed")
            return None

    def _maybe_compute_bmi(self, data: ScaleData, user_id: str) -> None:
        """When body fat is unavailable, still compute BMI if we have height."""
        profile = self._user_profiles_by_id.get(user_id)
        if not profile or not profile.get(CONF_BODY_METRICS_ENABLED):
            return
        height_cm = profile.get(CONF_HEIGHT)
        weight_kg = data.measurements.get(WEIGHT_KEY)
        if not (height_cm and weight_kg):
            return
        height_m = height_cm / 100.0
        data.measurements["body_mass_index"] = (
            floor(weight_kg / (height_m**2) * 100) / 100
        )

    def _add_pending_measurement(
        self,
        measurement: WeightMeasurement,
        data: ScaleData,
        candidate_ids: list[str] | None = None,
    ) -> None:
        """Store a measurement that wasn't attributed at weigh time.

        ``candidate_ids`` may be passed by the caller when it has already
        computed the candidate list (via ``_compute_pending_candidates``), so
        the router isn't evaluated a second time for the same measurement.

        Triggers the ambiguous-notification flow (mobile push to candidate
        users + persistent notification) and keeps the measurement in
        ``_pending_measurements`` until either:

          - a user/automation calls ``assign_pending_measurement``, or
          - the measurement ages out (``PENDING_MEASUREMENT_MAX_AGE``), or
          - all candidate users decline via "Not Me".
        """
        # Opportunistic cleanup: drop any pending entries older than the
        # max age before adding the new one.
        self._cleanup_aged_pending_measurements()
        if candidate_ids is None:
            candidate_ids = self._compute_pending_candidates(measurement.weight_kg)
        timestamp_iso = measurement.timestamp.isoformat()
        # Reuse the measurement's pre-computed display string when present
        # (it always is for live measurements; falls back to a fresh render
        # for measurements built outside `_on_scale_data`, e.g. tests).
        ts_display = (measurement.raw or {}).get(
            "timestamp_display"
        ) or self._format_notification_timestamp(timestamp_iso)
        self._pending_measurements[measurement.measurement_id] = {
            "weight_kg": measurement.weight_kg,
            "resistance_1": data.measurements.get(RESISTANCE_1_KEY),
            "resistance_2": data.measurements.get(RESISTANCE_2_KEY),
            "body_fat": data.measurements.get(BODY_FAT_KEY),
            "timestamp": timestamp_iso,
            "timestamp_display": ts_display,
            "candidates": candidate_ids,
            "raw_measurement": measurement,
            "notified_mobile_services": [],
        }
        # Persist the new pending entry so it survives an integration
        # reload (e.g., triggered by the user changing display unit).
        self._update_config_entry()
        # Fire the ambiguous-measurement notification flow asynchronously so
        # the sync library callback doesn't block.
        self.hass.async_create_task(
            self._create_ambiguous_notification(measurement.measurement_id)
        )

    def _compute_pending_candidates(self, weight_kg: float) -> list[str]:
        """Return the same candidate list ``_resolve_user`` would have produced,
        but with added logic to fallback to all users if no candidates match
        the weight.

        Used in the unresolved (weight-only) path to populate the pending
        record's ``candidates`` so we can mobile-notify those users.
        """
        from datetime import datetime, timezone

        synthetic = WeightMeasurement(
            weight_kg=weight_kg,
            timestamp=datetime.now(tz=timezone.utc),
            source_id=self.address,
        )
        try:
            ranked = self._router.evaluate_measurement(synthetic)
        except Exception:
            _LOGGER.exception("Router evaluation failed in pending-candidate compute")
            ranked = []
        candidate_ids = [c.user_id for c in ranked]
        filtered_candidates = self._filter_candidates_by_location(candidate_ids)
        if filtered_candidates:
            return filtered_candidates
        if candidate_ids:
            return candidate_ids

        # Fallback: if no candidates match weight, include all users
        all_user_ids = [
            u[CONF_USER_ID] for u in self._user_profiles if CONF_USER_ID in u
        ]
        if not all_user_ids:
            return []

        filtered_all = self._filter_candidates_by_location(all_user_ids)
        if filtered_all:
            return filtered_all

        _LOGGER.debug(
            "No pending candidates found for weight %.2f kg, falling back to all configured users",
            weight_kg,
        )
        return all_user_ids

    def _update_config_entry(self) -> None:
        """Persist coordinator state to the config entry's data dict.

        Writes user_profiles, router_state, and the in-memory pending
        measurements so the latter survive an integration reload (e.g.,
        triggered by the user changing display unit). No-op if
        ``set_config_entry_id`` has not been called yet.
        """
        if not self._config_entry_id:
            return
        entry = self.hass.config_entries.async_get_entry(self._config_entry_id)
        if entry is None:
            return
        new_data = {
            **entry.data,
            CONF_USER_PROFILES: deepcopy(self._user_profiles),
            CONF_ROUTER_STATE: self._router.to_dict(),
            CONF_PENDING_STATE: self._serialize_pending(),
        }
        self.hass.config_entries.async_update_entry(entry, data=new_data)

    def _serialize_pending(self) -> dict[str, dict[str, Any]]:
        """Convert `_pending_measurements` to a JSON-safe dict.

        Used for persistence in the config entry. The ``raw_measurement``
        ``WeightMeasurement`` instance is serialized via its ``to_dict``;
        all other fields are already JSON-safe primitives.
        """
        out: dict[str, dict[str, Any]] = {}
        for measurement_id, info in self._pending_measurements.items():
            out[measurement_id] = {
                "weight_kg": info.get("weight_kg"),
                "resistance_1": info.get("resistance_1"),
                "resistance_2": info.get("resistance_2"),
                "body_fat": info.get("body_fat"),
                "timestamp": info.get("timestamp"),
                "timestamp_display": info.get("timestamp_display"),
                "candidates": list(info.get("candidates", [])),
                "raw_measurement": info["raw_measurement"].to_dict(),
                "notified_mobile_services": [
                    list(t) for t in info.get("notified_mobile_services", [])
                ],
            }
        return out

    def _restore_pending(self, payload: dict[str, dict[str, Any]]) -> None:
        """Rebuild `_pending_measurements` from a serialized payload.

        Skips entries whose ``raw_measurement`` cannot be deserialized.
        Seeds `_ambiguous_notifications` from the keys so notifications
        opened in a prior session can still be dismissed.
        """
        restored: dict[str, dict[str, Any]] = {}
        for measurement_id, info in payload.items():
            try:
                raw = WeightMeasurement.from_dict(info["raw_measurement"])
            except Exception:
                _LOGGER.warning(
                    "Could not restore pending measurement %s; skipping",
                    measurement_id,
                )
                continue
            ts = info.get("timestamp", "")
            ts_display = info.get(
                "timestamp_display"
            ) or self._format_notification_timestamp(ts)
            restored[measurement_id] = {
                "weight_kg": info.get("weight_kg"),
                "resistance_1": info.get("resistance_1"),
                "resistance_2": info.get("resistance_2"),
                "body_fat": info.get("body_fat"),
                "timestamp": ts,
                "timestamp_display": ts_display,
                "candidates": list(info.get("candidates", [])),
                "raw_measurement": raw,
                "notified_mobile_services": [
                    tuple(t) if isinstance(t, list) else t
                    for t in info.get("notified_mobile_services", [])
                ],
            }
        self._pending_measurements = restored
        self._ambiguous_notifications = set(restored.keys())
        # If HA was offline for longer than the aging window, restored
        # entries may already be stale. Drop them now (and dismiss any
        # zombie notifications) so they don't reappear post-restart.
        self._cleanup_aged_pending_measurements()

    def _notify_diagnostic_listeners(self) -> None:
        """Fan out a 'something changed' poke to diagnostic sensors."""
        for cb in list(self._diagnostic_listeners):
            try:
                cb()
            except Exception:
                _LOGGER.exception("diagnostic listener raised")

    @callback
    def _on_core_config_updated(self, _event) -> None:
        """Re-render every stored ``timestamp_display`` when relevant HA
        config changes.

        Triggered by the ``core_config_updated`` event. The settings that
        actually affect our output are:

        - ``time_zone``: changes what ``dt_util.as_local`` produces.
        - ``time_format``: 12h ↔ 24h toggle in the time portion.
        - ``country``: feeds the country-based 12h/24h heuristic when
          ``time_format`` is unset.

        Language and date-format preference changes are no-ops for our
        output (we render in English with an unambiguous date order),
        but the event fires anyway and we redo the refresh — harmless.
        Without this, stored display strings would stay frozen at the
        values they had when their measurements were recorded.
        """
        # Refresh router-managed history for every configured user.
        for user_profile in self._user_profiles:
            user_id = user_profile.get(CONF_USER_ID)
            if user_id is None:
                continue
            for m in self._router.get_user_history(user_id):
                if m.raw is None:
                    # Defensive: shouldn't happen for measurements we built,
                    # but a missing raw dict means there's nowhere to store
                    # the new display.
                    continue
                m.raw["timestamp_display"] = self._format_notification_timestamp(
                    m.timestamp.isoformat()
                )
        # Refresh in-memory pending entries.
        for info in self._pending_measurements.values():
            ts = info.get("timestamp", "")
            info["timestamp_display"] = self._format_notification_timestamp(ts)
        # Persist (router state already includes the updated raw dicts;
        # pending state is also re-serialized) and push state updates so
        # any open sensor cards re-render.
        self._update_config_entry()
        for user_profile in self._user_profiles:
            user_id = user_profile.get(CONF_USER_ID)
            if user_id is not None:
                self._replay_latest_to_user(user_id)
        self._notify_diagnostic_listeners()

    def _cleanup_aged_pending_measurements(self) -> None:
        """Drop pending measurements older than ``PENDING_MEASUREMENT_MAX_AGE``.

        Dismisses persistent + mobile notifications for the dropped entries
        and pings diagnostic listeners if anything was removed. Called
        opportunistically when a new pending measurement is added.
        """
        cutoff = datetime.now(tz=timezone.utc) - PENDING_MEASUREMENT_MAX_AGE
        aged: list[str] = []
        for measurement_id, info in self._pending_measurements.items():
            ts_str = info.get("timestamp")
            if not ts_str:
                continue
            try:
                ts = self._parse_iso_utc(ts_str)
            except (TypeError, ValueError):
                continue
            if ts < cutoff:
                aged.append(measurement_id)
        for measurement_id in aged:
            self._dismiss_mobile_notifications(measurement_id)
            self._dismiss_persistent_notification(measurement_id)
            del self._pending_measurements[measurement_id]
            _LOGGER.debug("Dropped aged pending measurement: %s", measurement_id)
        if aged:
            self._update_config_entry()
            self._notify_diagnostic_listeners()

    # ------------------------------------------------------------------
    # Pending / historical assignment
    # ------------------------------------------------------------------

    def get_pending_measurements(self) -> dict[str, dict[str, Any]]:
        """Return a deep copy of the in-memory pending measurements dict."""
        return deepcopy(self._pending_measurements)

    def assign_pending_measurement(self, measurement_id: str, user_id: str) -> bool:
        """Attribute a pending measurement to a user.

        Routes the measurement through the same path a live measurement
        would take (`_commit_measurement_to_user`) so per-user sensors and
        body-metric derivations actually fire — recording to the router
        alone wouldn't make any entity update. Dismisses the persistent +
        mobile notifications associated with the measurement on success.

        Returns True on success, False if ``measurement_id`` is unknown
        or the router rejects the measurement.
        """
        pending = self._pending_measurements.get(measurement_id)
        if pending is None:
            return False

        # Reconstruct the ScaleData that the live path would have built so
        # `_commit_measurement_to_user` can derive body metrics + fan out
        # to listeners.
        measurements: dict[str, str | float | None] = {
            WEIGHT_KEY: pending["weight_kg"],
        }
        if pending.get("body_fat") is not None:
            measurements[BODY_FAT_KEY] = pending["body_fat"]
        if pending.get("resistance_1") is not None:
            measurements[RESISTANCE_1_KEY] = pending["resistance_1"]
        if pending.get("resistance_2") is not None:
            measurements[RESISTANCE_2_KEY] = pending["resistance_2"]
        data = ScaleData(
            name=self.device_name,
            address=self.address,
            display_unit=self._display_unit,
            measurements=measurements,
        )

        # Snapshot mobile-notification services because we need to dismiss
        # after commit, but the same method reads data from the pending dict.
        notified_services = list(pending.get("notified_mobile_services", []))

        try:
            self._commit_measurement_to_user(user_id, pending["raw_measurement"], data)
        except Exception:
            _LOGGER.exception(
                "Manual assignment failed for measurement_id=%s user_id=%s",
                measurement_id,
                user_id,
            )
            return False
        del self._pending_measurements[measurement_id]
        self._update_config_entry()
        self._dismiss_mobile_notifications(measurement_id, services=notified_services)
        self._dismiss_persistent_notification(measurement_id)
        self._notify_diagnostic_listeners()
        return True

    def reassign_user_measurement(
        self,
        from_user_id: str,
        to_user_id: str,
        measurement_id: str | None = None,
    ) -> bool:
        """Move a measurement from one user's history to another.

        If ``measurement_id`` is None, the most recent measurement for
        ``from_user_id`` is moved. Both users' sensors are refreshed so
        their state reflects the post-reassign latest measurement (or
        ``unavailable`` if a user has no history left).

        Body fat in the moved measurement is cleared. Whatever value was
        there was computed against the *original* user's profile (sex,
        age, height, athlete flag) — it would be wrong to attribute it
        to a different user. The destination user's replay will then
        recompute body fat off-scale from ``resistance_1`` + their own
        profile, so all derived metrics for them reflect *their* body.
        """
        try:
            moved = self._router.reassign_measurement(
                from_user_id, to_user_id, measurement_id
            )
        except Exception:
            _LOGGER.exception("Reassignment failed")
            return False
        if moved.raw is not None:
            moved.raw["body_fat"] = None
        self._replay_latest_to_user(from_user_id)
        self._replay_latest_to_user(to_user_id)
        self._update_config_entry()
        self._notify_diagnostic_listeners()
        return True

    def remove_user_measurement(
        self, user_id: str, measurement_id: str | None = None
    ) -> bool:
        """Remove a measurement from a user's history.

        If ``measurement_id`` is None, the most recent is removed. The
        user's sensors are refreshed to reflect the new latest measurement
        (or ``unavailable`` if no history remains).
        """
        try:
            self._router.remove_measurement(user_id, measurement_id)
        except Exception:
            _LOGGER.exception("Remove failed")
            return False
        self._replay_latest_to_user(user_id)
        self._update_config_entry()
        self._notify_diagnostic_listeners()
        return True

    # ------------------------------------------------------------------
    # Firmware → DeviceInfo.sw_version
    # ------------------------------------------------------------------

    def _sync_firmware_to_device_registry(self) -> None:
        """Push the scale's ``firmware_revision`` to ``DeviceInfo.sw_version``.

        No-op if the library hasn't read the value yet, or if it already
        matches what the device registry has. The broadcast-only
        (``RenphoAABBScale``) variant never connects and exposes no
        ``firmware_revision`` attribute, so ``getattr`` keeps this a safe no-op
        there.
        """
        scale = self._scale
        if scale is None:
            return
        firmware = getattr(scale, "firmware_revision", None)
        if not firmware:
            return
        registry = _get_device_registry(self.hass)
        device = registry.async_get_device(
            connections={(CONNECTION_BLUETOOTH, self.address)}
        )
        if device is None or device.sw_version == firmware:
            return
        registry.async_update_device(device.id, sw_version=firmware)

    # ------------------------------------------------------------------
    # Ambiguous-measurement notifications
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Notification-text formatters (English-only by design — see
    # `_format_decimal` and `_format_notification_timestamp` for rationale)
    # ------------------------------------------------------------------

    def _get_time_format_preference(self) -> str | None:
        """Return the user's preferred clock format: ``"12"`` | ``"24"`` | ``None``.

        Sourced from ``hass.config.time_format``; if unset, inferred from
        ``hass.config.country`` (12h for US/CA/PH/AU; 24h elsewhere).
        Returns ``None`` if neither signal is available — caller picks
        the default.

        Language and date-format preferences are deliberately NOT read:
        the integration ships English-only translations, and date order
        is rendered as unambiguous English ``Mon DD, YYYY`` for all users
        (see ``_format_notification_timestamp``).
        """
        config = getattr(self.hass, "config", None)
        if config is None:
            return None
        time_fmt = getattr(config, "time_format", None)
        if time_fmt in ("language", "auto", ""):
            time_fmt = None
        if time_fmt is None:
            country = _norm_country_code(config)
            if country:
                time_fmt = "12" if country in _COUNTRY_12H else "24"
        return time_fmt

    def _format_time_part(
        self,
        localized: datetime,
        time_format: str | None,
        include_seconds: bool,
    ) -> str | None:
        """Format the time-of-day from the explicit 12h/24h preference.

        Returns ``None`` if ``time_format`` is unspecified — the caller
        decides the default (typically 24h, the international standard).
        """
        if time_format == "24":
            return localized.strftime("%H:%M:%S" if include_seconds else "%H:%M")
        if time_format == "12":
            fmt = "%I:%M:%S %p" if include_seconds else "%I:%M %p"
            return localized.strftime(fmt).lstrip("0")
        return None

    @staticmethod
    def _parse_iso_utc(iso_timestamp: str) -> datetime:
        """Parse an ISO 8601 timestamp, treating naive values as UTC.

        Tolerates a trailing ``Z`` (older serialized state, external
        producers) and a missing tzinfo (legacy persisted entries from
        any earlier code path that wrote naive strings). Raises
        ``TypeError`` / ``ValueError`` on unparseable input — callers
        decide how to fall back.
        """
        s = iso_timestamp.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def _format_notification_time(self, iso_timestamp: str) -> str:
        """Format the time-of-day for a measurement timestamp (mobile body).

        Honors the user's 12h/24h HA preference (via ``_format_time_part``).
        Falls back to 24h — the international default — when HA has no
        explicit or country-inferred preference.
        """
        try:
            local_dt = dt_util.as_local(self._parse_iso_utc(iso_timestamp))
        except (TypeError, ValueError):
            return iso_timestamp
        time_format = self._get_time_format_preference()
        time_str = self._format_time_part(local_dt, time_format, include_seconds=False)
        if time_str is not None:
            return time_str
        return local_dt.strftime("%H:%M")

    def _format_notification_timestamp(self, iso_timestamp: str) -> str:
        """Format a full date+time for the persistent-notification body.

        Date is always rendered as unambiguous English ``Mon DD, YYYY``
        (e.g. ``May 12, 2026``) regardless of HA's date-format preference.
        The rest of the integration ships English-only translations, so
        localizing only the timestamp would produce mixed-language UI.

        The time-of-day portion honors the user's 12h/24h preference (via
        ``_format_time_part``), defaulting to 24h when no preference is
        inferable.
        """
        try:
            local_dt = dt_util.as_local(self._parse_iso_utc(iso_timestamp))
        except (TypeError, ValueError):
            return iso_timestamp
        time_format = self._get_time_format_preference()
        time_str = self._format_time_part(local_dt, time_format, include_seconds=True)
        if time_str is None:
            time_str = local_dt.strftime("%H:%M:%S")
        date_str = local_dt.strftime("%b %d, %Y")
        return f"{date_str} at {time_str} {local_dt.strftime('%Z')}".strip()

    def _format_decimal(self, value: float, precision: int) -> str:
        """Format a decimal with a fixed precision, always using ``.`` as
        separator."""
        return f"{round(value, precision):.{precision}f}"

    def _format_weight_for_display(self, weight_kg: float, precision: int = 2) -> str:
        """Format a weight in the configured display unit (kg or lb)."""
        if self._display_unit == WeightUnit.LB:
            value = MassConverter.convert(
                weight_kg, UnitOfMass.KILOGRAMS, UnitOfMass.POUNDS
            )
            unit = "lb"
        else:
            value = weight_kg
            unit = "kg"
        return f"{self._format_decimal(value, precision)} {unit}"

    def format_measurement_for_attribute(
        self,
        *,
        measurement_id: str,
        timestamp_iso: str,
        weight_kg: float | None,
        body_fat: float | None = None,
        resistance_1: int | float | None = None,
        resistance_2: int | float | None = None,
        timestamp_display: str | None = None,
    ) -> dict[str, Any]:
        """Build a dict representation of a measurement for sensor attributes.

        Weight is converted to the user-chosen display unit so values shown
        in ``weight_history`` and the pending-measurements diagnostic match
        what the rest of the integration's UI shows.

        Two timestamp fields are emitted with distinct purposes:

        - ``timestamp`` is the raw ISO-8601 UTC string. Use this in
          templates / automations (``as_timestamp(...)``, sorting, etc.).
        - ``timestamp_display`` is a locale + timezone-aware human string
          ready to drop straight into a Lovelace card. Callers should pass
          the value they previously stored on the measurement (live path)
          or in the pending dict (pending path); only as a fallback for
          missing values do we re-render here.
        """
        if weight_kg is None:
            weight_value: float | None = None
        elif self._display_unit == WeightUnit.LB:
            weight_value = round(
                MassConverter.convert(
                    weight_kg, UnitOfMass.KILOGRAMS, UnitOfMass.POUNDS
                ),
                1,
            )
        else:
            weight_value = round(weight_kg, 2)
        unit = "lb" if self._display_unit == WeightUnit.LB else "kg"
        if timestamp_display is None:
            timestamp_display = self._format_notification_timestamp(timestamp_iso)
        out: dict[str, Any] = {
            "measurement_id": measurement_id,
            "timestamp": timestamp_iso,
            "timestamp_display": timestamp_display,
            "weight": weight_value,
            "weight_unit": unit,
            "resistance_1": resistance_1,
            "resistance_2": resistance_2,
        }
        # Only include body_fat when it's actually known. A null value is
        # uninformative noise — it shows up for measurements that originated
        # as ambiguous unattributed (so the scale didn't report body fat) and
        # for reassigned measurements (where we deliberately discard the
        # stale value computed against the original user's profile).
        if body_fat is not None:
            out["body_fat"] = body_fat
        return out

    def _notification_id(self, measurement_id: str) -> str:
        """Persistent-notification ID for a given measurement_id."""
        return f"renpho_scale_{self.address}_{measurement_id}"

    def _mobile_tag(self, measurement_id: str) -> str:
        """Mobile-notification tag for a given measurement_id."""
        return f"scale_measurement_{measurement_id}"

    async def _send_mobile_notifications_for_ambiguous_measurement(
        self,
        measurement_id: str,
        weight_kg: float,
        candidates: list[str],
    ) -> list[tuple[str, str]]:
        """Send actionable mobile notifications to each candidate user's phones.

        Returns a list of (user_id, service_name) pairs for the services that
        successfully received a notification — used later to dismiss them when
        the measurement is assigned.
        """
        notified: list[tuple[str, str]] = []
        weight_display = self._format_weight_for_display(weight_kg, precision=1)
        time_display = self._format_notification_time(
            self._pending_measurements.get(measurement_id, {}).get(
                "timestamp", datetime.now().isoformat()
            )
        )
        tag = self._mobile_tag(measurement_id)

        # Group candidate users by mobile notify service so we send one
        # notification per device, with one or more "Assign to <name>" buttons.
        device_to_users: dict[str, list[tuple[str, str]]] = {}
        for user_id in candidates:
            profile = self._user_profiles_by_id.get(user_id)
            if not profile:
                continue
            user_name = profile.get(CONF_USER_NAME, user_id)
            for service_name in profile.get(CONF_MOBILE_NOTIFY_SERVICES, []) or []:
                device_to_users.setdefault(service_name, []).append(
                    (user_id, user_name)
                )

        for service_name, users in device_to_users.items():
            try:
                if len(users) == 1:
                    user_id, user_name = users[0]
                    # Was this device shared with non-candidate users?
                    shared_with: list[str] = []
                    for profile in self._user_profiles:
                        if profile.get(CONF_USER_ID) == user_id:
                            continue
                        if service_name in (
                            profile.get(CONF_MOBILE_NOTIFY_SERVICES, []) or []
                        ):
                            shared_with.append(profile.get(CONF_USER_NAME, "Unknown"))
                    if shared_with:
                        message = (
                            f"{weight_display} at {time_display}. "
                            f"Is this {user_name}'s?"
                        )
                        assign_title = f"Assign to {user_name}"
                        not_me_title = f"Not {user_name}"
                    else:
                        message = f"{weight_display} at {time_display}. Is this yours?"
                        assign_title = "Assign to Me"
                        not_me_title = "Not Me"
                    actions = [
                        {
                            "action": f"SCALE_ASSIGN_{user_id}_{measurement_id}",
                            "title": assign_title,
                        },
                        {
                            "action": f"SCALE_NOT_ME_{user_id}_{measurement_id}",
                            "title": not_me_title,
                        },
                    ]
                else:
                    message = f"{weight_display} at {time_display}. Who stepped on?"
                    # iOS limits actionable notifications to ~3 buttons.
                    actions = [
                        {
                            "action": f"SCALE_ASSIGN_{user_id}_{measurement_id}",
                            "title": f"Assign to {user_name}",
                        }
                        for user_id, user_name in users[:3]
                    ]
                    if len(users) > 3:
                        overflow = ", ".join(name for _, name in users[3:])
                        message += f" (+{len(users) - 3} more: {overflow})"

                notify_domain, notify_name = parse_notify_service(service_name)
                await self.hass.services.async_call(
                    notify_domain,
                    notify_name,
                    {
                        "title": "❓ Unassigned Scale Measurement",
                        "message": message,
                        "data": {
                            "tag": tag,
                            "group": "scale-measurements",
                            "channel": "Scale Measurements",
                            "importance": "default",
                            "actions": actions,
                        },
                    },
                )
                for user_id, _ in users:
                    notified.append((user_id, service_name))
            except Exception:
                _LOGGER.exception(
                    "Failed to send mobile notification via %s", service_name
                )
        return notified

    async def _create_ambiguous_notification(self, measurement_id: str) -> None:
        """Create the persistent_notification + dispatch mobile notifications."""
        pending = self._pending_measurements.get(measurement_id)
        if pending is None:
            return
        weight_kg = pending["weight_kg"]
        candidates = list(pending.get("candidates", []))

        device_reg = _get_device_registry(self.hass)
        device_entry = device_reg.async_get_device(
            connections={(CONNECTION_BLUETOOTH, self.address)}
        )
        device_id = device_entry.id if device_entry else "DEVICE_ID"
        device_name = device_entry.name if device_entry else self.device_name

        # Rank candidates by distance from each user's last weight if available.
        ranked: list[tuple[str, float | None, str]] = []
        for user_id in candidates:
            profile = self._user_profiles_by_id.get(user_id)
            if not profile:
                continue
            user_name = profile.get(CONF_USER_NAME, user_id)
            last = self._router.get_user_last_measurement(user_id)
            if last is None:
                ranked.append((user_id, None, user_name))
            else:
                ranked.append((user_id, abs(weight_kg - last.weight_kg), user_name))
        ranked.sort(key=lambda x: (x[1] is None, x[1] if x[1] is not None else 0.0))

        if not ranked:
            _LOGGER.error(
                "Ambiguous notification called with zero candidates for %s",
                measurement_id,
            )
            return

        candidate_lines = ["**Candidates:**"]
        for user_id, diff, user_name in ranked:
            if diff is None:
                candidate_lines.append(f"- **{user_name}** (`{user_id}`)")
            else:
                candidate_lines.append(
                    f"- **{user_name}** (`{user_id}`) — ±{self._format_weight_for_display(diff, 1)}"
                )
        candidate_block = "\n".join(candidate_lines)

        timestamp_display = self._format_notification_timestamp(pending["timestamp"])
        weight_display = self._format_weight_for_display(weight_kg)
        message = (
            f"**Scale: {device_name}**\n\n"
            "**Multiple users could match this measurement.**\n\n"
            f"Weight: **{weight_display}**\n"
            f"Time: `{timestamp_display}`\n\n"
            f"{candidate_block}\n\n"
            "**To assign this measurement:**\n"
            "1. Copy the service call below\n"
            "2. Go to **Developer Tools → Actions**\n"
            "3. Paste, choose the right user_id, click **Perform Action**\n\n"
            "```yaml\n"
            f"action: {DOMAIN}.assign_measurement\n"
            "data:\n"
            f"  device_id: {device_id}\n"
            f'  measurement_id: "{measurement_id}"\n'
            '  user_id: "<SELECT_USER_ID_FROM_ABOVE>"\n'
            "```\n\n"
            "This notification will dismiss automatically once the "
            "measurement is assigned."
        )

        notified = await self._send_mobile_notifications_for_ambiguous_measurement(
            measurement_id, weight_kg, candidates
        )
        if measurement_id in self._pending_measurements:
            self._pending_measurements[measurement_id]["notified_mobile_services"] = (
                notified
            )

        self._ambiguous_notifications.add(measurement_id)
        persistent_notification.create(
            self.hass,
            message,
            title=f"{device_name}: Choose User",
            notification_id=self._notification_id(measurement_id),
        )
        self._notify_diagnostic_listeners()

    def _dismiss_persistent_notification(self, measurement_id: str) -> None:
        """Dismiss the persistent notification for a measurement, if any."""
        if measurement_id in self._ambiguous_notifications:
            persistent_notification.dismiss(
                self.hass,
                notification_id=self._notification_id(measurement_id),
            )
            self._ambiguous_notifications.discard(measurement_id)

    def _dismiss_mobile_notifications(
        self, measurement_id: str, services: list[tuple[str, str]] | None = None
    ) -> None:
        """Dismiss the mobile notifications previously sent for a measurement."""
        pending = self._pending_measurements.get(measurement_id)
        if services is None:
            services = pending.get("notified_mobile_services", []) if pending else []
        tag = self._mobile_tag(measurement_id)
        for _user_id, service_name in services:
            notify_domain, notify_name = parse_notify_service(service_name)
            self.hass.async_create_task(
                self.hass.services.async_call(
                    notify_domain,
                    notify_name,
                    {"message": "clear_notification", "data": {"tag": tag}},
                )
            )

    async def _clear_all_pending_notifications(self) -> None:
        """Dismiss every open ambiguous-measurement notification (mobile + persistent)."""
        for measurement_id in list(self._ambiguous_notifications):
            self._dismiss_mobile_notifications(measurement_id)
            self._dismiss_persistent_notification(measurement_id)

    def ignore_candidate_for_pending_measurement(
        self, measurement_id: str, user_id: str
    ) -> bool:
        """Drop a user from the candidate list of a pending measurement.

        If no candidates remain, the measurement is removed from pending and
        all related notifications are dismissed. Otherwise the persistent and
        mobile notifications are re-sent with the trimmed candidate list.
        """
        pending = self._pending_measurements.get(measurement_id)
        if pending is None:
            return False
        candidates = pending.get("candidates", [])
        new_candidates = [c for c in candidates if c != user_id]
        if len(new_candidates) == len(candidates):
            return False

        # Always clear current notifications before deciding what to do next.
        self._dismiss_mobile_notifications(measurement_id)
        self._dismiss_persistent_notification(measurement_id)

        if not new_candidates:
            del self._pending_measurements[measurement_id]
            self._update_config_entry()
            self._notify_diagnostic_listeners()
            return True

        pending["candidates"] = new_candidates
        pending["notified_mobile_services"] = []
        self._update_config_entry()
        # Re-create with the trimmed list (async).
        self.hass.async_create_task(self._create_ambiguous_notification(measurement_id))
        return True


def _get_device_registry(hass: HomeAssistant):
    """Indirection so tests can patch the device-registry import."""
    from homeassistant.helpers import device_registry as _dr

    return _dr.async_get(hass)
