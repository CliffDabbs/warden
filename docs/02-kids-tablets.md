> **Legacy — the Home Assistant era.** Superseded by
> [Warden](../warden/README.md): per-device blocking now lives on the Devices tab, with timed
> unblocks. Not maintained, not deployed.
>
> The problem this project *didn't* solve — a tablet playing content it has already downloaded,
> or moved onto a hotspot — is still open, and is now tracked in
> [amazon-parent-dashboard-RECON.md](amazon-parent-dashboard-RECON.md).

# Project B — Block the kids' tablets

A per-device internet kill switch: scheduled (bedtime / homework) plus an ad-hoc toggle. Because the tablets connect through your **UniFi APs**, the cleanest control is UniFi's built-in "block client", surfaced into Home Assistant.

---

## Step 1 — UniFi integration in Home Assistant

1. HA → Settings → Devices & Services → Add Integration → **UniFi Network**. Point it at your UniFi controller; use a **local admin account** (not your Ubiquiti SSO) for reliability.
2. Open the integration's **Configure / Options** → under **Client control**, select the kids' tablets so HA creates a **block switch** for each.
3. Find the entity IDs (Developer Tools → States, filter `block`). They'll look like `switch.kids_ipad_block_client`.

> **Switch semantics:** for these UniFi block switches, **ON = blocked**, **OFF = allowed**. The package is written that way — don't flip the logic.

## Step 2 — Reserve the tablets

In pfSense DHCP (or UniFi), give each tablet a **static reservation** so the rules always target the right device.

---

## Home Assistant config

See `packages/kids_tablets.yaml`. It provides:

- `input_boolean.kids_tablets_allowed` — manual override tile.
- **Bedtime block** at 20:30 and **morning unblock** at 07:00 (edit the times).
- A **manual toggle** automation mirroring the input_boolean to the UniFi switches.

Edit the `# TODO` list of switch entity IDs to match yours, and adjust the times.

---

## Options / upgrades

- **Per-kid schedules:** duplicate the automations with different times and switch sets.
- **Homework mode:** a second `input_boolean` that blocks everything *except* an allow-list — better done as a UniFi/pfSense firewall policy than per-client block.
- **Dashboard:** put `input_boolean.kids_tablets_allowed` and each block switch on a Lovelace card so anyone can flip them.
- **Don't fight bedtime manually:** the schedule wins; the manual toggle is for "you can have 20 more minutes" moments.
