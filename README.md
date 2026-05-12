# Renpho Fitness Scale BLE Integration for Home Assistant

This custom integration connects your Renpho ES-CS20M smart bathroom scale to Home Assistant over Bluetooth Low Energy (BLE). It provides real-time weight measurements, on-device body composition metrics, and intelligent multi-user routing — all without requiring an internet connection or the Renpho app. This is an unofficial, community-built integration. It is not endorsed by, affiliated with, or supported by Renpho. See the [Disclaimer](#disclaimer) at the bottom for details.

> **Also sold as:** Renpho Elis 1, Renpho Smart Body Fat Scale, Renpho Bluetooth Body Composition Scale, and various locale-specific names — the "ES-CS20M" model code is rarely surfaced in retail listings.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/yellow_img.png)](https://www.buymeacoffee.com/ronnnnnnn)

## Features

- **Supported model:** Renpho ES-CS20M (full features: weight, body composition)
- Automatic discovery of the Renpho ES-CS20M when it advertises
- Intelligent multi-user support:
  - Automatically detects which person is using the scale based on their weight history.
  - Uses an adaptive tolerance system that adjusts to each user's weight fluctuations over time.
  - Supports linking users to Home Assistant Person entities to exclude users who are `not_home`.
- Real-time weight measurements
- Optional body composition metrics including:
  - Body Mass Index (BMI)
  - Body Fat Percentage
  - Fat-Free Mass
  - Body Water Percentage
  - Skeletal Muscle Percentage
  - Muscle Mass
  - Bone Mass
  - Protein Percentage
  - Basal Metabolic Rate
- **Athlete mode** — per-user toggle for the scale's athlete-tuned body-fat algorithm
- **Alternative body-fat algorithm** — per-user toggle to match the value shown by the official Renpho app
- Customizable display units (kg, lb), applied both on the scale and in Home Assistant
- ESPHome Bluetooth proxy support
- Battery and firmware revision reported
- Direct Bluetooth communication (no internet or Renpho app required)

## Notes

- The Renpho ES-CS20M computes body fat *on-device*. The integration commits the user's profile (sex, age, height, athlete flag, algorithm choice) to the scale during the ~2-second measurement window — that's how the scale knows which body to compute against. When the resolver can't pick a single user confidently, the scale falls back to weight-only and the integration computes body fat **off-scale** when you later attribute the measurement to a specific user.
- This integration uses the [renpho-escs20m](https://github.com/ronnnnnnnnnnnnn/renpho-escs20m) Python library for BLE communication and [multi-user-scale-core](https://github.com/ronnnnnnnnnnnnn/multi-user-scale-core) for the multi-user routing logic.

## Supported Devices

| Model           | Status          | Features                                |
| --------------- | --------------- | --------------------------------------- |
| Renpho ES-CS20M | Fully supported | Weight, body composition, display unit  |

*As an Amazon Associate I earn from qualifying purchases.*

**Where to buy ES-CS20M:** [🇺🇸 US](https://www.amazon.com/dp/B01N1UX8RW?tag=ronnnnnnn-20) · [🇬🇧 UK](https://www.amazon.co.uk/dp/B01N1UX8RW?tag=ronnnnnnn02-21) · [🇪🇸 ES](https://www.amazon.es/dp/B01N1UX8RW?tag=ronnnnnnn-21) · [🇮🇹 IT](https://www.amazon.it/dp/B01N1UX8RW?tag=ronnnnnnn0a-21) · [🇫🇷 FR](https://www.amazon.fr/dp/B01N1UX8RW?tag=ronnnnnnn0b-21)

Other Renpho BLE scale models may work but have not been tested. If you try a different model, please open an issue with what works and what doesn't.

## Installation

### HACS (Recommended)

> **Note:** This integration is not yet in the HACS default store. You must add it as a custom repository first (see below), or use the button which does this automatically.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ronnnnnnnnnnnnn&repository=renpho_fitness_scale_ble&category=integration)

1. Ensure that [HACS](https://hacs.xyz/) is installed in your Home Assistant instance.
2. Click the button above, **or** add this repository manually:
  - In HACS, go to **Integrations** → click the three-dot menu (⋮) → **Custom repositories**
  - Enter `https://github.com/ronnnnnnnnnnnnn/renpho_fitness_scale_ble` and select **Integration** as the category
3. Search for "Renpho Fitness Scale BLE" in HACS and click **Download**.
4. Restart Home Assistant.

### Manual Installation

1. Copy the `renpho_fitness_scale_ble` folder to your Home Assistant's `custom_components` directory.
2. Restart Home Assistant.

### Compatibility

Requires Home Assistant **2026.1.0+** and Python 3.13+ (matches Home Assistant's own minimum).

## Configuration

### Initial Setup

1. Step on the scale once to wake its BLE radio. Home Assistant should auto-discover it within a few seconds.
2. If auto-discovery doesn't pick it up, go to **Settings > Devices & Services**, click **Add Integration**, and search for "Renpho Fitness Scale BLE".
3. Confirm the discovered device and pick your preferred display unit (kg or lb).
4. Add the first user profile (see options below). Repeat from **Configure** to add more users.

### User Profile Configuration Options

When adding or editing user profiles (**Settings > Devices & Services > Renpho Fitness Scale BLE > Configure**), you can configure the following options:

- **User Name:** Display name for the user profile.
- **Person Entity (optional):** Link this user profile to a Home Assistant person entity. When linked, the integration uses the person's location state to improve automatic assignment:
  - If the person is marked as `not_home`, they are excluded from automatic assignment for new measurements
  - This helps avoid incorrectly assigning measurements when household members are away
- **Mobile Devices (optional):** Select one or more mobile devices (via Home Assistant companion app) to receive actionable notifications for ambiguous measurements:
  - When enabled, you'll receive a mobile notification with tap-to-assign buttons directly on your phone
  - Each candidate user gets a personalized notification with "Assign to Me" and "Not Me" buttons
- **Calculate body composition metrics:** Calculate BMI, body fat %, and the rest of the derived metrics. Requires sex, date of birth, and height.
- **Athlete mode (optional):** Switches the scale to its athlete-tuned body-fat algorithm. The standard curve assumes typical body composition; for users with above-average muscle mass it tends to over-estimate body fat. Enable this for users who train regularly.
- **Use alternative body fat algorithm (optional):** Flips the scale to its alternative on-device body-fat algorithm. Try this if the body fat percentage you see in Home Assistant doesn't match what the official Renpho app shows for the same user.

### Advanced Settings

The same Configure flow exposes integration-wide settings:

- **Display unit:** kg or lb. Sets both the unit displayed on the scale itself and the unit shown for every weight-class sensor in Home Assistant.
- **Weight history retention (days):** Measurements older than this are pruned from each user's history. Default 90.
- **Maximum stored measurements per user:** Per-user history cap (FIFO eviction by timestamp). Default 100.
- **Enable verbose library logging:** Useful for troubleshooting BLE issues.

## Multi-User Support

This integration is designed for households with multiple users. You can create a unique profile for each person using the scale, with independent history, body-composition settings, and mobile-notification routing.

### Person Detection

When a measurement starts, the integration waits for the first stable weight (~2 seconds), then tries to identify the user based on two factors:

1. **Weight History:** The measurement is compared against each user's recent weight history with adaptive tolerance.
2. **Location:** If a user profile is linked to a Home Assistant `person` entity, users marked `not_home` are excluded from automatic assignment.

If a single user is a clear match (and they have body metrics enabled), their profile is committed to the scale during the same measurement window so body fat is computed against *their* body.

### Ambiguous Measurements

If the measurement is ambiguous (e.g., two users have similar weights, or a new user has no history), the integration falls back to weight-only and notifies you:

- **Mobile Notifications (if configured):** Each candidate user receives a personalized notification on their mobile device with actionable buttons:
  - "Assign to Me" — Assigns the measurement to that user
  - "Not Me" — Removes them from the candidate list. If candidates remain, the notifications are re-sent to the remaining users; if all decline, the measurement is dropped.
- **Persistent Notifications:** A notification appears in the Home Assistant notifications panel with the measurement details and a copy-pasteable service-call snippet for manual assignment.

When you finally attribute a pending measurement to a user, the integration computes that user's body fat **off-scale** from the captured measurement data and the user's profile, so all derived body metrics still populate.

### Managing Users

You can manage user profiles by navigating to your device in **Settings > Devices & Services > Renpho Fitness Scale BLE**. Click **CONFIGURE** to:

- **Add a new user:** Create a new profile with optional person entity link, mobile devices, and body composition.
- **Edit a user:** Update a user's name, linked person entity, mobile devices, body-metric settings, athlete mode, or algorithm choice.
- **Remove a user:** Delete the profile and all associated sensor entities. The user's stored history is also dropped from the router.

## Services

The integration provides services to manage measurements, especially for handling ambiguous weigh-ins. You can use these in scripts or automations, or call them directly from **Developer Tools > Actions**.

All three take the Home Assistant `device_id` of the scale plus a `user_id` (a slug derived from the user's display name — e.g. `"jane"`, `"john2"` — visible on the **User Directory** diagnostic sensor's attributes) and, where applicable, a `measurement_id` (a stable opaque string read from the **Pending Measurements** diagnostic sensor or the per-user weight sensor's `weight_history` attribute).

### `renpho_fitness_scale_ble.assign_measurement`

Assign a pending (ambiguous) measurement to a specific user. Triggers the same sensor updates as a live measurement, including off-scale body-fat computation if the user has body metrics enabled and the scale captured the necessary measurement data.

**Example:**

```yaml
service: renpho_fitness_scale_ble.assign_measurement
data:
  device_id: <your_scale_device_id>
  measurement_id: "a3f8b2e0c4d149e6912f8a7b6c5d4e3f"
  user_id: "jane"
```

### `renpho_fitness_scale_ble.reassign_measurement`

Move a measurement from one user's history to another's. Useful when a measurement was auto-assigned to the wrong person. Body fat is intentionally cleared during the reassign (it was originally computed against the source user's profile); the destination user's body fat is recomputed off-scale from the captured measurement data.

If `measurement_id` is omitted, defaults to the most recent measurement for `from_user_id`.

**Example:**

```yaml
service: renpho_fitness_scale_ble.reassign_measurement
data:
  device_id: <your_scale_device_id>
  from_user_id: "john"
  to_user_id: "jane"
  measurement_id: "a3f8b2e0c4d149e6912f8a7b6c5d4e3f"  # optional
```

### `renpho_fitness_scale_ble.remove_measurement`

Remove a measurement from a user's history. The user's sensors refresh to whatever is now their most recent measurement (or `unavailable` if no history remains).

If `measurement_id` is omitted, defaults to the most recent measurement for `user_id`.

**Example:**

```yaml
service: renpho_fitness_scale_ble.remove_measurement
data:
  device_id: <your_scale_device_id>
  user_id: "jane"
  measurement_id: "a3f8b2e0c4d149e6912f8a7b6c5d4e3f"  # optional
```

## Diagnostic Sensors

The integration creates two diagnostic sensors to provide visibility into its state:

- **User Directory:** Shows the number of configured user profiles and lists their `user_id` and body-metric flags in the attributes.
- **Pending Measurements:** Shows the number of ambiguous measurements awaiting manual assignment, with each measurement's `measurement_id`, timestamp (ISO + locale-formatted), weight, body fat (if reported), and resistance values in the attributes.

The device's firmware revision is shown on its device card via the standard `sw_version` field; battery percentage is exposed as a regular sensor (not diagnostic).

## Troubleshooting

- **Check Settings → Repairs first.** The integration surfaces drift between its stored config and your Home Assistant state there (e.g., a person entity you linked has been deleted, or a mobile notify service is gone). If something silently stopped working, that's the first place to look.
- Make sure the scale is within Bluetooth range of your Home Assistant host, or within range of at least one ESPHome device configured as a Bluetooth proxy.
- Step on the scale once to wake its BLE radio — Renpho scales advertise only briefly after being stepped on.
- If you've previously paired the scale with the Renpho app or another integration, disconnect there first.

### `BleakOutOfConnectionSlotsError` in the log

This typically means one of two things:

- **The scale isn't actually reachable** — most common. The scale's BLE chip emits occasional advertisements even when "off", but rejects connections outside its measurement window. These errors are usually harmless and stop happening as soon as the scale's session ends.
- **Connection slot exhaustion** — your Bluetooth adapter (or ESPHome proxy) has hit its concurrent-connection limit. Built-in adapters typically support 5–7 concurrent BLE connections; if you have many connectable BLE devices, you may need to add an [ESPHome Bluetooth proxy](https://esphome.github.io/bluetooth-proxies/) near the scale.

### Body fat doesn't match the Renpho app

Try the **Use alternative body fat algorithm** toggle in the user's profile.

### Body fat seems too high for an athletic body

Enable **Athlete mode** on the user's profile.

### Mobile-app "Assign to Me" / "Not Me" buttons don't work

- Confirm the [Home Assistant companion app](https://companion.home-assistant.io/) is installed and the device's `notify.mobile_app_`* service exists in **Developer Tools > Services**.
- Confirm the user's profile has the right mobile services selected under **Mobile devices**.
- The scale only sends actionable notifications for *ambiguous* measurements. If your weight is unique enough that the resolver picks confidently every time, you'll never see them — that's expected.

### Raspberry Pi and other Linux machines using BlueZ

If you encounter a `org.bluez.Error.InProgress` error, try the following in `bluetoothctl`:

```
power off
power on
scan on
```

(See [this GitHub issue](https://github.com/home-assistant/core/issues/76186#issuecomment-1204954485) for more information.)

## Reporting Issues

Before opening a GitHub issue:

1. **Check Settings → Repairs.** If a repair card explains the problem, the description tells you how to fix it without filing anything.
2. **Download diagnostics.** Open **Settings → Devices & Services → Renpho Fitness Scale BLE → your scale's device card → Download Diagnostics**. This produces a redacted JSON dump of your config, coordinator state, and pending measurements. Attach it to your issue.
3. **Include version info:**
  - Home Assistant version (Settings → About)
    - Integration version (visible on the scale's device card under **Configuration**)
    - Scale firmware revision (also on the device card under `sw_version`)
4. **If it's a BLE / connection issue,** also enable **Verbose library logging** in the integration's advanced settings, reproduce the problem, and include the relevant log lines.

Issues go to the [GitHub issue tracker](https://github.com/ronnnnnnnnnnnnn/renpho_fitness_scale_ble/issues).

## Support the Project

If you find this unofficial project helpful, consider buying me a coffee! Your support helps maintain and improve this integration.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/yellow_img.png)](https://www.buymeacoffee.com/ronnnnnnn)

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## Disclaimer

This integration is not official. It is not endorsed by, directly affiliated with, maintained, authorized, or sponsored by Renpho or any of its affiliates or subsidiaries. All product and company names are the registered trademarks of their original owners. The use of any trade name or trademark is for identification and reference purposes only and does not imply any association with the trademark holder of their product brand.

Use this integration at your own risk.