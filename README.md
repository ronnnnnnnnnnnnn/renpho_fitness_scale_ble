# Renpho Fitness Scale BLE Integration for Home Assistant

This custom integration connects your Renpho ES-CS20M smart bathroom scale — and other compatible Renpho scales that share the same BLE protocol (see [Supported Devices](#supported-devices)) — to Home Assistant over Bluetooth Low Energy (BLE). It provides real-time weight measurements, on-device body composition metrics, and intelligent multi-user routing — all without requiring an internet connection or the Renpho app. This is an unofficial, community-built integration. It is not endorsed by, affiliated with, or supported by Renpho. See the [Disclaimer](#disclaimer) at the bottom for details.

> **Also sold as:** Renpho Elis 1, Renpho Smart Body Fat Scale, Renpho Bluetooth Body Composition Scale, and various locale-specific names — the "ES-CS20M" model code is rarely surfaced in retail listings.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/yellow_img.png)](https://www.buymeacoffee.com/ronnnnnnn)

## Features

- **Supported models:** Renpho ES-CS20M and compatible variants (see [Supported Devices](#supported-devices) for the full list and hardware-revision caveats)
- Automatic discovery when the scale advertises
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
- On models that compute the full body-composition panel on-device (currently the R-MSB01), the metrics above come straight from the scale
- **Athlete mode** — per-user toggle for the scale's athlete-tuned body-fat algorithm
- **Alternative body-fat algorithm** — per-user toggle to match the value shown by the official Renpho app
- Customizable display units (kg, lb), applied both on the scale and in Home Assistant
- ESPHome Bluetooth proxy support
- Battery and firmware revision reported (the battery entity is disabled by default — see [Troubleshooting](#battery-sensor-missing-or-always-reads-100))
- Direct Bluetooth communication (no internet or Renpho app required)

## Notes

- The integration uses the [renpho-escs20m](https://github.com/ronnnnnnnnnnnnn/renpho-escs20m) Python library for BLE communication and [multi-user-scale-core](https://github.com/ronnnnnnnnnnnnn/multi-user-scale-core) for the multi-user routing logic.
- The integration clears the scale's internal store of *offline* readings (weigh-ins taken while Home Assistant wasn't listening, e.g. during an HA restart) each time it connects. Those readings are not imported into Home Assistant, and because delivery deletes them from the scale, they won't show up in the official Renpho app either.
- **Extended-flavor models only (e.g. `ESCS20MA2`, R-MSB01)**: The scale computes body fat *on-device*. The integration commits the user's profile (sex, age, height, athlete flag, algorithm choice) to the scale during the ~2-second measurement window — that's how the scale knows which body to compute against. When the resolver can't pick a single user confidently, the scale falls back to weight-only and the integration computes body fat **off-scale** when you later attribute the measurement to a specific user.

## Supported Devices

Compatibility seems to be determined by the scale's **HVIN** (Hardware Version Identification Number), not the marketed model name. Some ES-CS20M hardware revisions speak a different BLE protocol and aren't supported; some other marketed Renpho models happen to share hardware with the ES-CS20M and work fine. The HVIN — including its trailing revision code (e.g. `…MA2` vs `…MB2` vs `…MN`) — is the reliable discriminator. Some stickers don't print HVIN as a separate field — in that case the same identifier is embedded as the trailing portion of the **FCC ID** (e.g. FCC ID `2A26P-ESCS20M` → device code `ESCS20M`). The FCC ID column below lets you match on either.

Confirmed-working:

| Marketed model | HVIN        | FCC ID              |
|----------------|-------------|---------------------|
| ES-CS20M       | `ESCS20MA2` | `2A26P-ESCS20MA2`   |
| ES-CS20M       | `ESCS20MN`  | `2A26P-ESCS20MN`    |
| ES-CS20M       | -           | `2A26P-ESCS20M`     |
| ES-26M         | `ESCS20MA2` | `2A26P-ESCS20MA2`   |
| ES-30M         | `ES30MA2`   | `2A26P-ES30MA2`     |
| ES-32MD        | `ESCS20MA2` | `2A26P-ESCS20MA2`   |
| R-MSB01        | -           | `2A26P-RMSB01`      |

- **R-MSB01** — a later hardware revision of the same platform. It computes the **full body-composition panel on-device** and sends it with each measurement, so the body-composition values come from the scale itself. The underlying library doesn't currently expose bioimpedance, so when a measurement has to be attributed to a user *after* the fact (late assignment or reassignment), body composition for that reading is recomputed as an estimate from weight, height, age and sex — the same approach used for the broadcast subvariant below.


Experimental:

| Marketed model | HVIN | FCC ID            | Protocol               |
|----------------|------|-------------------|------------------------|
| Arboleaf CS20M | -    | `2ANDX-CS20M`     | QN-series (FFE0 GATT)  |
| ES-CS20M       | -    | `2APXUES-CS20M`   | `0xaabb` (broadcast)   |

- **Arboleaf CS20M** — QN-series hardware ships the same wire protocol on two GATT service layouts, and the integration handles both: the FFF0 layout used by the Renpho models above, and the FFE0 layout seen on some other QN scales. Full feature set.
- **ES-CS20M (FCC ID `2APXUES-CS20M`)** — a **non-connectable** subvariant: it broadcasts weight in its BLE advertisements rather than connecting over GATT, using a different (`0xaabb`) protocol. This scale transmits no bioimpedance, so its body composition calculations do not use a real impedance measurement. Rather, it's an estimate based on just weight, height, age and sex (and athlete mode). The estimate is characterized, not a guess: the body-fat formulas are only weakly sensitive to impedance (worst case about ±0.5 percentage points across the plausible range), and it closely reproduces what the official Renpho app shows for this scale.

Known-incompatible:

| Marketed model | HVIN        | FCC ID              |
|----------------|-------------|---------------------|
| ES-CS20M       | `ESCS20MB2` | `2A26P-ESCS20MB2`   |

**How to find your HVIN:** look at the regulatory sticker on the back of the scale. Most stickers have a dedicated `HVIN:` field alongside the FCC ID. Some stickers don't — in that case, look at the trailing portion of the **FCC ID** (`2A26P-<code>`), which encodes the same identifier. The trailing revision suffix (`A2`, `B2`, `N`, or absent altogether) is what matters. If your HVIN or FCC ID matches a confirmed-working entry above, this integration is likely to work; if it matches a known-incompatible entry it's unlikely to work; if it doesn't match an entry in either table, try it out and report back on the issue tracker. (Note: neither HVIN nor model number is readable over BLE, so the device code can't be shown on the Home Assistant device card — you have to read it off the sticker.)

> This integration may also work with other QN-Scale non-Renpho branded scales utilizing the same protocol (on either GATT layout). Feel free to report compatibility results on the [issue tracker](https://github.com/ronnnnnnnnnnnnn/renpho_fitness_scale_ble/issues).

If your scale isn't in any of the tables above, please open an issue with the marketed model, HVIN (including the revision suffix), and whether it works. If you'd like to actively help diagnose protocol compatibility for an unsupported HVIN, see [Diagnosing Protocol Compatibility](#diagnosing-protocol-compatibility). The same compatibility list is mirrored in the [library README's compatibility section](https://github.com/ronnnnnnnnnnnnn/renpho-escs20m#device-compatibility) — that's where new entries are added first, so check there if this list looks out of date.

## Installation

### HACS (Recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ronnnnnnnnnnnnn&repository=renpho_fitness_scale_ble&category=integration)

1. Ensure that [HACS](https://hacs.xyz/) is installed in your Home Assistant instance.
2. In the HACS panel, search for "Renpho Fitness Scale BLE" (or click the button above).
3. Click **Download** on the Renpho Fitness Scale BLE integration.
4. Restart Home Assistant.

### Manual Installation

1. Copy the `renpho_fitness_scale_ble` folder to your Home Assistant's `custom_components` directory.
2. Restart Home Assistant.

### Compatibility

Requires Home Assistant **2026.1.0+** and Python 3.13+ (matches Home Assistant's own minimum).

## Configuration

### Initial Setup

1. Step on the scale once to wake its BLE radio. Home Assistant should auto-discover it within a few seconds.
2. If auto-discovery doesn't pick it up, go to **Settings → Devices & Services**, click **Add Integration**, and search for "Renpho Fitness Scale BLE".
3. Confirm the discovered device and pick your preferred display unit (kg or lb).
4. Add the first user profile (see options below). Repeat from **Configure** to add more users.

### Adding a Device the Integration Doesn't Recognize

Devices the integration can't positively identify are never auto-discovered — no discovery card is shown for them, and they can only be added through the manual picker described here. The manual "Add Integration" picker doesn't hide devices it can't identify — it labels them by how confident it is: `[QN device — unknown model]` for a scale-shaped advertisement whose exact model hasn't been seen before, or `[unknown device]` for anything else that isn't obviously non-scale hardware. Picking one of these asks you to choose a protocol yourself: **QN** covers most Renpho models and is worth trying first, while **Broadcast** is for non-connectable, weight-only scales that never accept a GATT connection. There's no guarantee an unrecognized device will work — some Renpho hardware revisions speak a protocol this integration doesn't understand at all — but many "unknown" scales turn out to be already-supported models the identifier registry just hasn't seen yet. If you try one, please [open an issue](#reporting-issues) with debug logs enabled: the log carries the scale's model identifier, and every report that gets added to the registry means the next person with that same scale sees it correctly classified from the start.

### User Profile Configuration Options

When adding or editing user profiles (**Settings → Devices & Services → Renpho Fitness Scale BLE → Configure**), you can configure the following options:

- **User name:** Display name for the user profile.
- **Person entity (optional):** Link this user profile to a Home Assistant person entity. When linked, the integration uses the person's location state to improve automatic assignment:
  - If the person is marked as `not_home`, they are excluded from automatic assignment for new measurements
  - This helps avoid incorrectly assigning measurements when household members are away
- **Mobile devices (optional):** Select one or more mobile devices (via Home Assistant companion app) to receive actionable notifications for ambiguous measurements:
  - When enabled, you'll receive a mobile notification with tap-to-assign buttons directly on your phone
  - Each candidate user gets a personalized notification with "Assign to Me" and "Not Me" buttons
- **Calculate body composition metrics:** Calculate BMI, body fat %, and the rest of the derived metrics. Requires sex, date of birth, and height.
- **Athlete mode (optional):** Switches the scale to its athlete-tuned body-fat algorithm. The standard curve assumes typical body composition; for users with above-average muscle mass it tends to over-estimate body fat. Enable this for users who train regularly.
- **Use alternative body fat algorithm (optional):** Flips the scale to its alternative on-device body-fat algorithm. Try this if the body fat percentage you see in Home Assistant doesn't match what the official Renpho app shows for the same user.

### Advanced Settings

The same Configure flow exposes integration-wide settings:

- **Display unit:** kg or lb. Sets both the unit displayed on the scale itself and the unit shown for every weight-class sensor in Home Assistant. Note: changing it overrides any unit you set on an individual weight entity; otherwise per-entity unit settings are respected.
- **Keep history forever:** Never delete weight measurements automatically. While enabled, the cleanup limits below are ignored.
- **Weight history retention (days):** Measurements older than this are pruned from each user's history. Default 90. A user's most recent measurement is always kept, so long-absent users remain recognized.
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

On models that don't report usable bioimpedance (the R-MSB01 and the broadcast subvariant), this off-scale computation cannot use a real impedance reading — body fat for such readings is an anthropometric estimate from weight, height, age and sex. In practice this matters less than it sounds: the body-fat formulas are only weakly sensitive to impedance (worst case about ±0.5 percentage points of body fat across the plausible impedance range), and the same approach reproduces the official Renpho app's numbers closely.

### Managing Users

You can manage user profiles by navigating to your device in **Settings → Devices & Services → Renpho Fitness Scale BLE**. Click **CONFIGURE** to:

- **Add a new user:** Create a new profile with optional person entity link, mobile devices, and body composition.
- **Edit a user:** Update a user's name, linked person entity, mobile devices, body-metric settings, athlete mode, or algorithm choice.
- **Remove a user:** Delete the profile and all associated sensor entities. The user's stored history is also dropped from the router.

## Services

The integration provides services to manage measurements, especially for handling ambiguous weigh-ins. You can use these in scripts or automations, or call them directly from **Developer Tools → Actions**.

All three take the Home Assistant `device_id` of the scale plus a `user_id` (a slug derived from the user's display name — e.g. `"jane"`, `"john2"` — visible on the **User Directory** diagnostic sensor's attributes) and, where applicable, a `measurement_id` (a stable opaque string read from the **Pending Measurements** diagnostic sensor or the per-user weight sensor's `weight_history` attribute).

### `renpho_fitness_scale_ble.assign_measurement`

Assign a pending (ambiguous) measurement to a specific user. Triggers the same sensor updates as a live measurement, including off-scale body-fat computation if the user has body metrics enabled — from the impedance the scale captured, or, on models that don't report usable impedance over BLE, the anthropometric estimate described under [Ambiguous Measurements](#ambiguous-measurements). (If a scale that does report impedance captured none for this measurement — e.g. the user weighed in with socks on — the reading stays weight-only.)

**Example:**

```yaml
action: renpho_fitness_scale_ble.assign_measurement
data:
  device_id: <your_scale_device_id>
  measurement_id: "a3f8b2e0c4d149e6912f8a7b6c5d4e3f"
  user_id: "jane"
```

### `renpho_fitness_scale_ble.reassign_measurement`

Move a measurement from one user's history to another's. Useful when a measurement was auto-assigned to the wrong person. Body fat is intentionally cleared during the reassign (it was originally computed against the source user's profile); the destination user's body fat is recomputed off-scale — from the captured impedance, or, on models that don't report usable impedance over BLE, the anthropometric estimate described under [Ambiguous Measurements](#ambiguous-measurements).

If `measurement_id` is omitted, defaults to the most recent measurement for `from_user_id`.

**Example:**

```yaml
action: renpho_fitness_scale_ble.reassign_measurement
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
action: renpho_fitness_scale_ble.remove_measurement
data:
  device_id: <your_scale_device_id>
  user_id: "jane"
  measurement_id: "a3f8b2e0c4d149e6912f8a7b6c5d4e3f"  # optional
```

## Diagnostic Sensors

The integration creates two diagnostic sensors to provide visibility into its state:

- **User Directory:** Shows the number of configured user profiles and lists their `user_id` and body-metric flags in the attributes.
- **Pending Measurements:** Shows the number of ambiguous measurements awaiting manual assignment, with each measurement's `measurement_id`, timestamp (ISO + locale-formatted), weight, body fat (if reported), and resistance values (when reported) in the attributes.

The device's firmware revision is shown on its device card via the standard `sw_version` field; battery percentage is exposed as a regular sensor (not diagnostic), disabled by default — see [Troubleshooting](#battery-sensor-missing-or-always-reads-100).

## Troubleshooting

- **Check Settings → Repairs first.** The integration surfaces drift between its stored config and your Home Assistant state there (e.g., a person entity you linked has been deleted, or a mobile notify service is gone). If something silently stopped working, that's the first place to look.
- Make sure the scale is within Bluetooth range of your Home Assistant host, or within range of at least one ESPHome device configured as a Bluetooth proxy.
- Step on the scale once to wake its BLE radio — Renpho scales advertise only briefly after being stepped on.
- If you've previously paired the scale with the Renpho app or another integration, disconnect there first.

### Scale connects but no readings appear

If Home Assistant pairs with the scale successfully but no weight or body-composition measurements ever come through, the most likely cause is an unsupported hardware revision. Some Renpho scales sold under the ES-CS20M name (and other model names) use a different BLE protocol that this integration doesn't speak. Check the [Supported Devices](#supported-devices) table — specifically the HVIN on the regulatory sticker on the back of your scale. If yours isn't listed (or is in the known-incompatible table), please open an issue with the marketed model and HVIN so the compatibility record can be updated.

### `BleakOutOfConnectionSlotsError` in the log

This typically means one of two things:

- **The scale isn't actually reachable** — most common. The scale's BLE chip emits occasional advertisements even when "off", but rejects connections outside its measurement window. These errors are usually harmless and stop happening as soon as the scale's session ends.
- **Connection slot exhaustion** — your Bluetooth adapter (or ESPHome proxy) has hit its concurrent-connection limit. Built-in adapters typically support 5–7 concurrent BLE connections; if you have many connectable BLE devices, you may need to add an [ESPHome Bluetooth proxy](https://esphome.github.io/bluetooth-proxies/) near the scale.

### Battery sensor missing or always reads 100%

The battery entity exists but is **disabled by default**: on observed units the firmware reports a constant 100% over BLE and doesn't decrement it as the batteries drain — cells weak enough to need replacing still read 100%. A battery sensor that never drops would mislead dashboards and mean low-battery automations never fire, so it's opt-in. If you want the raw reading anyway (it may be accurate on your hardware revision — this hasn't been confirmed either way for every unit), enable the entity from the scale's device page. The value is also always included in the diagnostics download.

### Body fat doesn't match the Renpho app

Try the **Use alternative body fat algorithm** toggle in the user's profile.

### Body fat seems too high for an athletic body

Enable **Athlete mode** on the user's profile.

### Mobile-app "Assign to Me" / "Not Me" buttons don't work

- Confirm the [Home Assistant companion app](https://companion.home-assistant.io/) is installed and the device's `notify.mobile_app_`* service exists in **Developer Tools → Services**.
- Confirm the user's profile has the right mobile services selected under **Mobile devices**.
- The scale only sends actionable notifications for *ambiguous* measurements. If your weight is unique enough that the resolver picks confidently every time, you'll never see them — that's expected.

### Bluetooth issues with a native Linux adapter (BlueZ)

If setup occasionally fails with `org.bluez.Error.InProgress`, or
measurements stop arriving on a machine using its own Bluetooth adapter,
one possible cause seen on some systems (often around Home Assistant
startup) is the integration's scanner conflicting with Home Assistant's
shared Bluetooth scanner.

**The durable fix for that cause — enable BlueZ passive scanning.**
Whenever the integration uses the machine's own adapter, it prefers
*passive* scanning, which coexists cleanly with Home Assistant's scanner.
On Linux this requires BlueZ's experimental features.

Home Assistant OS enables BlueZ experimental features out of the box, so this is
typically relevant mostly to Container/Core installs on Linux (including Raspberry
Pi). To enable BlueZ experimental features on a supported system (requires BlueZ >= 5.56 and kernel >= 5.10), on the host:

1. Edit `/etc/bluetooth/main.conf` and set, under `[General]`:
   `Experimental = true`
2. Restart the Bluetooth service: `sudo systemctl restart bluetooth`
3. Restart Home Assistant.

When passive scanning isn't available, the integration logs a warning and
raises an item under **Settings → System → Repairs**; both clear
automatically once passive scanning becomes available.

**If the adapter is already stuck** (the `InProgress` error persists across
retries), the adapter may be in a wedged state — which can have several
causes, not all related to this integration. Reset it in `bluetoothctl`:

```
power off
power on
scan on
```

then restart Home Assistant. (See [this GitHub issue](https://github.com/home-assistant/core/issues/76186#issuecomment-1204954485) for more information.)
This recovers a stuck adapter; if the cause was scanner contention,
enabling passive scanning above can help prevent it from recurring.

## Reporting Issues

Before opening a GitHub issue:

1. **Check Settings → Repairs.** If a repair card explains the problem, the description tells you how to fix it without filing anything.
2. **Download diagnostics.** Open **Settings → Devices & Services → Renpho Fitness Scale BLE → your scale's device card → Download Diagnostics**. This produces a redacted JSON dump of your config, coordinator state, and pending measurements. Attach it to your issue.
3. **Include version info:**
    - Home Assistant version (Settings → About)
    - Integration version (visible on the scale's device card under **Configuration**)
    - Scale firmware revision (also on the device card under `sw_version`)
    - **HVIN** from the regulatory sticker on the back of the scale, including the revision suffix (e.g. `ESCS20MA2`). The HVIN isn't readable over BLE so it can't be included automatically in the diagnostics dump — please type it in by hand.
4. **If it's a BLE / connection issue,** also enable verbose library logging in the integration's advanced settings, reproduce the problem, and include the relevant log lines.

Issues go to the [GitHub issue tracker](https://github.com/ronnnnnnnnnnnnn/renpho_fitness_scale_ble/issues).

## Diagnosing Protocol Compatibility

If you have a Renpho BLE scale whose HVIN isn't in the [Supported Devices](#supported-devices) tables — or that's listed but isn't behaving the way the integration expects — you can help me diagnose the protocol-level compatibility by capturing a Bluetooth log of the official Renpho Health app talking to the scale. From that I can usually tell whether the scale speaks a protocol close to what this integration already understands, something different but parseable, or something out of reach — and we can figure out next steps from there.

### Capturing the log on Android

1. Delete any old `btsnoop_hci*` files on your phone first.
2. In Developer Options, enable **Bluetooth HCI Snoop Log**.
3. Toggle Bluetooth off and on. This starts a fresh log file.
4. With the Renpho Health app, weigh yourself and **note down the exact date/time of the measurement** along with every value the app reports (weight, body fat, water %, bone mass, etc.). Also note your user profile info — sex, body height, activity level, age. All of this is the ground truth needed to verify the byte decoding against the capture.
5. Repeat step 4 at least 3 more times at noticeably different weights (e.g. yourself holding something heavy, like a crate of beer).
6. Disable **Bluetooth HCI Snoop Log**.

Then trigger a bug report from Developer Options (interactive is enough, no need for a full one). Inside the resulting zip, look under `FS\data\misc\bluetooth\logs\` for one or more files whose names begin with `btsnoop_hci` — the exact suffix varies by Android version and manufacturer (`.log`, `.log.last`, `-1.log`, `-2.log`, etc.). If you see several, send all of them.

> **Before sending: confirm the filenames start with `btsnoop_hci`, not `btsnooz_hci`.** (Note the 'z'.) The `btsnooz_hci*` variants are Android's truncated always-on diagnostic buffer — they exist even without HCI snoop logging enabled, and aren't usable for protocol analysis. If those are all you find, the snoop log wasn't actually capturing; double-check the developer option is on, toggle Bluetooth off and on, and redo from step 4. Catching this before sending saves an unnecessary round-trip.

### Capturing the log on iOS

Apple provides a signed Bluetooth-logging configuration profile that enables BLE packet capture on iOS without jailbreaking. The Twocanoes Software knowledge base has a well-illustrated walkthrough: [Capture Bluetooth Packet Trace on iOS](https://twocanoes.com/knowledge-base/capture-bluetooth-packet-trace-on-ios/). You'll need a Mac to extract the captures from the resulting sysdiagnose. Android is still the easier path if you have access to both.

### What to include in the issue

Open a [GitHub issue](https://github.com/ronnnnnnnnnnnnn/renpho_fitness_scale_ble/issues) and include:

- The marketed model name (from the box)
- The **HVIN with its revision suffix** from the regulatory sticker on the back of the scale (e.g. `ESCS20MA2`) — see [How to find your HVIN](#supported-devices) above
- All `btsnoop_hci*` log files from the bug report, attached to the issue (or a WeTransfer / Drive link if too large for GitHub) — see filename note above before attaching
- For every weigh-in in the capture: the exact timestamp, every value the Renpho Health app showed (weight, body fat, water %, bone mass, etc.), and the user profile data that was active (sex, height, activity level, age)
- A note on what the scale's display shows during a measurement and after — only the weight, or also other metrics, and whether the display changes over the course of the measurement until it stabilizes

## Support the Project

If you find this unofficial project helpful, consider buying me a coffee! Your support helps maintain and improve this integration.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/yellow_img.png)](https://www.buymeacoffee.com/ronnnnnnn)

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## Disclaimer

This integration is not official. It is not endorsed by, directly affiliated with, maintained, authorized, or sponsored by Renpho or any of its affiliates or subsidiaries. All product and company names are the registered trademarks of their original owners. The use of any trade name or trademark is for identification and reference purposes only and does not imply any association with the trademark holder of their product brand.

Use this integration at your own risk.