> **Legacy — the Home Assistant era.** Superseded by
> [Warden](../warden/README.md), which drives AdGuard directly: the per-service switches here
> are now the Dashboard's toggles, and the HA automations are Warden rules. Not maintained,
> not deployed.
>
> **Still live and still the only record of it:** the DNS-bypass countermeasures at the
> firewall — the port-53 NAT redirect, the DoH/DoT blocklist, TCP 853 and QUIC on UDP/443.
> Every block Warden applies silently depends on those being in place, and Warden does not
> configure them.

# Project A — Block YouTube for kids, allow on TVs on demand

YouTube is genuinely hard to block because the apps use QUIC (UDP/443), a huge shared Google CDN (`googlevideo.com`), and will fall back to their own DNS (DoH / hardcoded 8.8.8.8) if you only block at one layer. So this is **four layers**. Skip any one and it leaks.

**Default behaviour:** YouTube blocked for kids' devices **and** the TVs. You flip a Home Assistant switch to allow it on the TVs, and it auto-re-blocks after a few hours.

---

## Layer 1 — Install AdGuard Home (the DNS filter)

1. Home Assistant → Settings → Add-ons → Add-on Store → **AdGuard Home** → Install → Start.
2. Open its Web UI, complete setup, set an **admin username + password** (you'll need these for the HA API calls in project A's package).
3. Point your network at it: pfSense → **System → General Setup → DNS Servers** = AdGuard's IP. Then in pfSense DHCP, hand out AdGuard as the DNS server to clients.

## Layer 2 — Per-client blocking in AdGuard

AdGuard has a maintained **"YouTube" blocked-service** list (covers `googlevideo`, `youtubei`, etc.) — use it rather than hand-listing domains.

1. AdGuard → **Settings → Client settings → Add client** for each device. Identify by **IP** (use your DHCP reservations) or MAC.
   - Kids' tablets → set **Blocked services → YouTube** (always on).
   - Samsung TVs → also add YouTube to blocked services **by default** (HA will toggle this off on demand).
   - Your/adult phones → leave unblocked.
2. Note each TV's exact client **name** and **IP** — the HA package needs them.

## Layer 3 — Force all DNS through AdGuard (pfSense)

Without this, the TV/tablet ignores your DNS and the block does nothing.

- **Redirect port 53** → pfSense → Firewall → NAT → Port Forward:
  - Interface: LAN; Protocol: TCP/UDP; Dest port 53; Redirect target = AdGuard IP:53; for **any source except AdGuard itself**.
  - Add the matching rule for any other VLANs the kids' devices sit on.
- **Block DoH/DoT** (stops apps bypassing you via encrypted DNS):
  - Install **pfBlockerNG** → enable a **DoH/DoT blocklist** feed (e.g. the public "DoH IP" / `dibdot DoH-IP-blocklists`).
  - Also block outbound **TCP 853** (DoT).

## Layer 4 — Block QUIC so the YouTube app can't bypass DNS

- pfSense → Firewall → Rules → LAN: add a rule **above** your allow rules:
  - Action **Block**, Protocol **UDP**, Source = kids/TV devices (or LAN net), Dest port **443**.
  - This forces YouTube to fall back to TCP/443, where SNI/DNS filtering applies. Normal HTTPS browsing still works over TCP.
- (Optional, stricter) also block UDP/80.

---

## Home Assistant: the "Allow YouTube on TVs" toggle

See `packages/youtube_tv.yaml`. It creates:

- `input_boolean.allow_youtube_tvs` — your switch / dashboard tile.
- `rest_command`s that call AdGuard's API to add/remove **YouTube** from each TV client's blocked services.
- Automations: ON → allow on TVs, OFF → block, plus an **auto-re-block after 3 hours**.

### Getting the exact AdGuard client JSON

AdGuard's `clients/update` API wants the **whole client object**, so grab the current one first and only change `blocked_services`:

```bash
curl -s -u USER:PASS http://YOUR_ADGUARD:3000/control/clients | jq '.clients[] | select(.name=="Living Room TV")'
```

Paste that object into both `rest_command` payloads in the package — the **allow** variant has `"blocked_services": []`, the **block** variant has `"blocked_services": ["youtube"]`. (Field names vary slightly by AdGuard version — match what the GET returns.)

---

## Test it

1. On a kids' device: open YouTube → should fail to load. In AdGuard → **Query Log**, you'll see the blocked requests.
2. Turn QUIC/DoH layers on, then re-test the **app** (not just the website) — the app is what bypasses weak setups.
3. Flip `input_boolean.allow_youtube_tvs` ON → YouTube works on the TV within a few seconds (may need the app reopened). Wait for auto-re-block, or flip it off.
