> **Legacy — the Home Assistant era.** Superseded by
> [Warden](../warden/README.md), which owns the client tags, groups and per-service blocking
> described here. Ignore anything below claiming Home Assistant is the source of truth for the
> switches; Warden is. Not maintained, not deployed.
>
> **Still live:** the AdGuard tag / `$ctag` / service-id reference, and **Step 2's always-on
> baseline** (safe search, parental control, safe browsing). Warden reads that baseline from
> `config/warden.yaml` but **never writes it** — so setting it remains a manual step, and this
> document plus `iac/deploy.py` are the only places it is written down.

# Organising AdGuard rules — kids locked down, services toggled from Home Assistant

Goal: kids' devices are **fully controlled** (safe baseline, always on), and you get **Home Assistant switches** to flip services like YouTube/Netflix on and off — per group, from your phone.

Control model: **switch ON = allowed, switch OFF = blocked.** Home Assistant is the source of truth for the *service switches*; it writes each client's `blocked_services` via the AdGuard API. Everything else (safe baseline, bedtime schedule) lives in AdGuard.

---

## Step 1 — Define your device groups (AdGuard UI)

**Settings → Client settings → Add client** for every managed device. Identify by **IP** (use your DHCP reservations).

- **Kids devices:** add each, and give them the tag **`user_child`**.
- **TVs:** add each Samsung TV.
- Adults' phones/laptops: leave them out — they stay unmanaged.

> Tip: name clients clearly ("Kids iPad", "Living Room TV") — the HA package matches on these names.

## Step 2 — The kids "fully controlled" baseline (static)

This is the always-on protection; it is **not** toggled by HA. Two ways to scope it:

**Simple (whole-house safe):** Settings → General →
- **Enable Safe Browsing** (blocks malware/phishing) — harmless for everyone.
- **Enforce Safe Search** — forces Google/Bing/DuckDuckGo safe + YouTube Restricted.
- Add a **family blocklist** under Filters → DNS blocklists (e.g. OISD, or a porn/gambling list).

These apply to everyone. Most families are fine with that. If adults need unfiltered search/sites, use the per-kid option below instead.

**Per-kid only (adults unaffected):** set the kids' clients to **use_global_settings = off** and enable **Parental control + Safe Search** on just those clients. This needs HA to preserve those fields when it writes — tell me and I'll switch the kids sync to a GET-merge script that keeps them intact.

Optionally add custom rules scoped to the tag, e.g. block a specific site for kids only:
```
||somesite.com^$ctag=user_child
```

## Step 3 — Recurring limits: bedtime (AdGuard native, no HA)

Per client → **Blocked services → Schedule**: block the fun services (or all) during, say, 20:00–07:00. AdGuard enforces this itself — use HA only for the *ad-hoc* overrides.

## Step 4 — The Home Assistant switches

`packages/adguard_service_switches.yaml` gives you:

- **input_booleans** per group × service: `TV · YouTube`, `Kids · Netflix`, etc.
- A generic **`rest_command`** that PUTs a client's `blocked_services`.
- Two **scripts** (`adguard_sync_tvs`, `adguard_sync_kids`) that compute `blocked_services` from the switches and push it to every device in the group.
- **Automations** that re-sync on any switch change and after HA restart, plus an optional 3-hour auto-re-block for TV YouTube.

How it works: when you flip `Kids · YouTube` off, HA rebuilds that group's blocked list (`["youtube", ...]` for every OFF switch) and writes it to each kids client. Because HA always sends the **full** desired list, it's idempotent — no drift.

### Fill in these placeholders
- `ADGUARD_IP:3000` → your AdGuard container's IP.
- `secrets.yaml`: `adguard_user` / `adguard_pass`.
- The client **names + IPs** in each script's `for_each`.
- Confirm your version's JSON shape (array vs nested) — see the header comment in the package.

### Add a service or a device (easy to extend)
- **New service** (e.g. TikTok on TVs): add an `input_boolean`, add one line to the group's `blocked` template, add the entity to that group's automation trigger.
- **New device**: add a `{ name, ids }` line to the group's `for_each`.

## Step 5 — A dashboard to flip them

Add a card (Settings → Dashboards → Edit → Add card → Entities):
```yaml
type: entities
title: Screen time
entities:
  - entity: input_boolean.kids_youtube
  - entity: input_boolean.kids_netflix
  - entity: input_boolean.kids_tiktok
  - type: divider
  - entity: input_boolean.tv_youtube
  - entity: input_boolean.tv_netflix
```

---

## Service ID reference

Get the authoritative list for your version:
```
curl.exe -u 'cliff:PASS' http://10.7.11.29/control/blocked_services/all   # port 80, not 3000
```
Common IDs: `youtube`, `netflix`, `tiktok`, `instagram`, `facebook`, `snapchat`, `discord`, `twitch`, `reddit`, `disneyplus`, `hulu`, `roblox`, `epic_games`, `steam`, `spotify`.
