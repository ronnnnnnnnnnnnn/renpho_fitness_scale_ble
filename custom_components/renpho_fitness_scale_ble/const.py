"""Constants for the renpho_fitness_scale_ble integration."""

DOMAIN = "renpho_fitness_scale_ble"

# --- Per-config-entry data keys ---
CONF_SCALE_DISPLAY_UNIT = "scale_display_unit"
CONF_USER_PROFILES = "user_profiles"
CONF_ROUTER_STATE = "router_state"
CONF_PENDING_STATE = "pending_state"
CONF_PROTOCOL = "protocol"

# --- Scale BLE protocols (renpho-escs20m) ---
# QN-series: connectable GATT scale with body composition (the default and
# original variant). AABB: the broadcast-only ES-CS20M subvariant that emits
# weight in its BLE advertisements — non-connectable, weight-only (BMI at most).
# Entries created before this key existed are treated as QN.
PROTOCOL_QN = "qn"
PROTOCOL_AABB = "aabb"

# --- User profile dict keys ---
CONF_USER_ID = "user_id"
CONF_USER_NAME = "name"
CONF_PERSON_ENTITY = "person_entity"
CONF_MOBILE_NOTIFY_SERVICES = "mobile_notify_services"
CONF_BODY_METRICS_ENABLED = "body_metrics_enabled"
CONF_SEX = "sex"
CONF_BIRTHDATE = "birthdate"
CONF_HEIGHT = "height"  # stored in cm
CONF_USE_ALTERNATIVE_ALGORITHM = "use_alternative_algorithm"
CONF_ATHLETE = "athlete"
CONF_CREATED_AT = "created_at"
CONF_UPDATED_AT = "updated_at"

# Imperial input field names (used in config flow only; not persisted)
CONF_FEET = "feet"
CONF_INCHES = "inches"

# --- Advanced settings (per-config-entry) ---
CONF_HISTORY_RETENTION_DAYS = "history_retention_days"
CONF_MAX_HISTORY_SIZE = "max_history_size"
CONF_ENABLE_LIBRARY_LOGGING = "enable_library_logging"

# --- Defaults that mirror RouterConfig() defaults from multi-user-scale-core ---
HISTORY_RETENTION_DAYS = 90
MAX_HISTORY_SIZE = 100

# --- Body fat algorithm identifiers (selected via use_alternative_algorithm boolean) ---
ALGORITHM_DEFAULT = 0x04
ALGORITHM_ALTERNATIVE = 0x03


def parse_notify_service(stored: str) -> tuple[str, str]:
    """Normalize a stored notify-service value into ``(domain, name)``.

    The config flow stores the short form (e.g. ``mobile_app_pixel_9a``)
    and we hardcode the ``notify`` domain when sending, so a stored
    value that happens to carry the ``notify.`` prefix would otherwise
    be sent as ``notify.notify.mobile_app_pixel_9a`` and silently fail.
    Tolerating both forms here keeps the lookup, the repair-issue
    existence check, and the actual ``services.async_call`` consistent.
    """
    if "." in stored:
        domain, name = stored.split(".", 1)
        return domain, name
    return "notify", stored


def get_sensor_unique_id(device_name: str, user_id: str, sensor_key: str) -> str:
    """Construct unique_id for a per-user sensor entity.

    Format: ``<device_name>_<user_id>_<sensor_key>``. ``user_id`` is
    always a non-empty slug, so the format is uniform across all sensors.
    """
    return f"{device_name}_{user_id}_{sensor_key}"
