# Project C — Charge batteries on Octopus free-electricity sessions

When Octopus runs a **Free Electricity session** (the "opt in, free power between X and Y on date Z" emails — aka Power-ups), force the Sunsynk to **grid-charge** the battery until a target SoC, then revert to normal self-use.

Flow: **Octopus integration** detects the session → **Home Assistant** automation → **MQTT publish** to Solar Assistant → Sunsynk grid charge on.

---

## Step 1 — Octopus Energy integration (HACS)

1. Install **HACS** if you haven't, then add the **Octopus Energy** integration (BottleCapDave).
2. Configure with your Octopus **API key** + **account ID**.
3. It exposes free-session entities. Confirm the exact names in Developer Tools → States (they include your account id), typically:
   - `binary_sensor.octopus_energy_<ACCOUNT>_free_electricity_session` — **on** while a session is live.
   - `event.octopus_energy_<ACCOUNT>_free_electricity_session_events` — upcoming sessions (for an opt-in reminder).

> **Opt-in is still manual** on Octopus's side (the app/email link). HA can *remind* you to opt in and then *act* once the session goes live — it can't opt in for you. The package includes a reminder automation.

## Step 2 — Solar Assistant → Home Assistant (MQTT)

1. In **Solar Assistant → Configuration → Home Assistant**, enable the MQTT/Home Assistant integration. This auto-creates entities like `sensor.battery_state_of_charge`, `sensor.battery_power`, etc. Confirm your **SoC** sensor's entity id.
2. **Find the grid-charge control topic.** Install the **MQTT Explorer** add-on (or use any MQTT client) and browse the `solar_assistant/...` tree while you toggle grid charge once in the Solar Assistant UI. Watch which topic changes — that's your control topic. Common shapes:
   - `solar_assistant/inverter_1/grid_charge/set`
   - `solar_assistant/total/grid_charge_enabled/set`

   Put the real topic + payload (`true`/`false`, `1`/`0`, or `Enabled`/`Disabled` — match what you see) into the package.

> If Solar Assistant turns out to be **read-only over MQTT** for your Sunsynk, the fallback is a direct **Modbus/RS485 dongle** into the inverter with a Sunsynk/Deye HACS integration, which gives guaranteed write access to grid-charge and TOU registers. Tell me and I'll switch project C to that path.

## Step 3 — The automation

See `packages/octopus_free_charge.yaml`:

- `input_number.battery_charge_target_soc` — how full to charge during a free session (default 95%).
- **Start:** session goes `on` **and** SoC below target → notify + publish grid-charge **on**.
- **Stop:** session ends **or** SoC reaches target → publish grid-charge **off** (back to self-use).
- **Reminder:** when an upcoming free session is announced → push notification to opt in.

Edit the `# TODO` entity IDs, MQTT topic/payloads, and your `notify.mobile_app_*` service.

---

## Smart extras (once the basics work)

- **Don't grid-charge what the sun will fill:** add a condition skipping grid charge if it's a sunny midday and forecast solar will top the battery anyway (use the Solcast/forecast integration).
- **Pre-session top-down:** if you *also* want max free energy, let the battery run down a bit before a known session so there's room — only worth it if sessions are reliably announced ahead.
- **Charge current:** if Solar Assistant exposes a max-charge-current topic, bump it during the session for a faster fill, then restore it.
- **Safety cap:** keep the target ≤ your battery's recommended max (e.g. 95–100%) and let the inverter's BMS manage the final taper.
