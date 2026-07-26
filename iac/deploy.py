#!/usr/bin/env python3
"""
screentime deploy tool — reconcile one YAML config to AdGuard Home + Home Assistant.

  python deploy.py --dry-run          # show what would change, touch nothing
  python deploy.py                    # apply to AdGuard, write HA files, reload HA
  python deploy.py --adguard          # AdGuard only
  python deploy.py --ha               # Home Assistant files only
  python deploy.py --reset-services   # also reset AdGuard blocked_services to config defaults
  python deploy.py --print            # print generated HA package to stdout

Secrets come from environment or a local .env file (see .env.example):
  ADGUARD_URL (optional, overrides config)  ADGUARD_USER  ADGUARD_PASS
  HA_CONFIG_DIR   (e.g. \\\\homeassistant\\config  — the Samba share)
  HA_URL  HA_TOKEN   (optional, to auto-reload HA after writing)
"""
import argparse
import json
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency: pip install pyyaml")
try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

HERE = Path(__file__).resolve().parent
DAY_ORDER = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
ABBR = {d[:3]: d for d in DAY_ORDER}


# ── helpers ──────────────────────────────────────────────────────────────────
def load_env():
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path):
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    # light validation
    for key in ("groups", "clients", "services"):
        if key not in cfg:
            sys.exit(f"config error: missing top-level '{key}'")
    known = {s["id"] for s in cfg["services"]}
    for g, gd in cfg["groups"].items():
        for sid in gd.get("default_blocked", []):
            if sid not in known:
                print(f"  ! warning: group '{g}' default_blocked '{sid}' is not in services[]")
    return cfg


def hhmmss(t):
    return t if t.count(":") == 2 else t + ":00"


def expand_days(token):
    token = token.strip().lower()
    if token in ("everyday", "daily", "all", "mon-sun"):
        return DAY_ORDER[:]
    if "-" in token:
        a, b = (x.strip()[:3] for x in token.split("-", 1))
        return DAY_ORDER[DAY_ORDER.index(ABBR[a]): DAY_ORDER.index(ABBR[b]) + 1]
    return [ABBR.get(token[:3], token)]


def group_services(cfg, group):
    """service ids controllable for a group, in config order."""
    return [s["id"] for s in cfg["services"] if group in s["groups"]]


def yaml_block(top_key, obj):
    return yaml.safe_dump({top_key: obj}, sort_keys=False, allow_unicode=True, default_flow_style=False)


# ── AdGuard ──────────────────────────────────────────────────────────────────
class AdGuard:
    def __init__(self, url, user, pw):
        self.base = url.rstrip("/")
        self.auth = (user, pw)

    def clients(self):
        r = requests.get(self.base + "/control/clients", auth=self.auth, timeout=10)
        r.raise_for_status()
        return {c["name"]: c for c in (r.json().get("clients") or [])}

    def add(self, obj):
        r = requests.post(self.base + "/control/clients/add", json=obj, auth=self.auth, timeout=10)
        r.raise_for_status()

    def update(self, name, obj):
        r = requests.post(self.base + "/control/clients/update",
                          json={"name": name, "data": obj}, auth=self.auth, timeout=10)
        r.raise_for_status()


def desired_client(client, group):
    baseline = group.get("baseline") or {}
    ss = bool(baseline.get("safe_search"))
    tags = ([group["tag"]] if group.get("tag") else []) + list(client.get("tags", []))
    return {
        "name": client["name"],
        "ids": [str(x) for x in client["ids"]],
        "tags": tags,
        "use_global_settings": False,
        "filtering_enabled": True,
        "parental_enabled": bool(baseline.get("parental")),
        "safebrowsing_enabled": bool(baseline.get("safebrowsing")),
        "safesearch_enabled": ss,
        "safe_search": {"enabled": ss, "google": True, "bing": True, "duckduckgo": True,
                        "ecosia": True, "pixabay": True, "yandex": True, "youtube": True},
        "use_global_blocked_services": False,
        "blocked_services_schedule": {"time_zone": "Local"},
        "blocked_services": list(group.get("default_blocked", [])),
    }


def reconcile_adguard(cfg, dry, reset_services):
    url = os.environ.get("ADGUARD_URL") or cfg["adguard"]["url"]
    user, pw = os.environ.get("ADGUARD_USER"), os.environ.get("ADGUARD_PASS")
    if not (user and pw):
        sys.exit("AdGuard creds missing: set ADGUARD_USER / ADGUARD_PASS (env or .env)")
    ag = AdGuard(url, user, pw)
    print(f"AdGuard @ {url}")
    existing = ag.clients()
    for client in cfg["clients"]:
        group = cfg["groups"][client["group"]]
        want = desired_client(client, group)
        cur = existing.get(want["name"])
        if cur is None:
            print(f"  CREATE {want['name']:16} ids={want['ids']} tags={want['tags']} "
                  f"blocked={want['blocked_services']}")
            if not dry:
                ag.add(want)
        else:
            # runtime owns blocked_services — preserve live value unless --reset-services
            if not reset_services:
                want["blocked_services"] = cur.get("blocked_services", want["blocked_services"])
            changes = _diff(cur, want)
            if changes:
                print(f"  UPDATE {want['name']:16} {changes}")
                if not dry:
                    ag.update(want["name"], want)
            else:
                print(f"  ok     {want['name']}")


def _diff(cur, want):
    out = []
    for k in ("ids", "tags", "parental_enabled", "safebrowsing_enabled",
              "safesearch_enabled", "blocked_services", "use_global_settings"):
        if cur.get(k) != want.get(k):
            out.append(f"{k}: {cur.get(k)!r}->{want.get(k)!r}")
    return "; ".join(out)


# ── Home Assistant generation ────────────────────────────────────────────────
REST_CMD = """rest_command:
  adguard_update_client:
    url: "__URL__/control/clients/update"
    method: POST
    username: !secret adguard_user
    password: !secret adguard_pass
    content_type: "application/json"
    payload: >
      {"name": {{ name | to_json }},
       "data": {"name": {{ name | to_json }},
                "ids": {{ ids | to_json }},
                "tags": {{ tags | to_json }},
                "use_global_settings": false,
                "filtering_enabled": true,
                "parental_enabled": {{ parental | to_json }},
                "safebrowsing_enabled": {{ safebrowsing | to_json }},
                "safesearch_enabled": {{ safesearch | to_json }},
                "safe_search": {"enabled": {{ safesearch | to_json }}, "google": true, "bing": true, "duckduckgo": true, "ecosia": true, "pixabay": true, "yandex": true, "youtube": true},
                "use_global_blocked_services": false,
                "blocked_services": {{ blocked | to_json }}}}
"""


def gen_input_boolean(cfg):
    ib = {}
    for svc in cfg["services"]:
        for grp in svc["groups"]:
            blocked = svc["id"] in (cfg["groups"][grp].get("default_blocked") or [])
            ib[f"{grp}_{svc['id']}"] = {
                "name": f"{grp.capitalize()} · {svc['name']}",
                "icon": svc.get("icon", "mdi:web"),
                "initial": (not blocked),   # ON = allowed
            }
    for ov in cfg.get("overrides", []):
        ib[ov["name"]] = {"name": ov.get("title", ov["name"]),
                          "icon": ov.get("icon", "mdi:toggle-switch"), "initial": False}
    if cfg.get("schedules"):
        ib["holiday_mode"] = {"name": "Holiday mode (relax schedules)", "icon": "mdi:beach",
                              "initial": False}
    return yaml_block("input_boolean", ib)


def gen_schedule(cfg):
    sched = {}
    for name, s in (cfg.get("schedules") or {}).items():
        entry = {"name": name.replace("_", " ").capitalize()}
        for token, windows in s["weekly"].items():
            for d in expand_days(token):
                entry.setdefault(d, [])
                entry[d] += [{"from": hhmmss(w["from"]), "to": hhmmss(w["to"])} for w in windows]
        sched[name] = entry
    return yaml_block("schedule", sched) if sched else ""


def gen_scripts(cfg):
    parts = ["script:"]
    for grp in cfg["groups"]:
        sids = group_services(cfg, grp)
        if not sids:
            continue
        base = cfg["groups"][grp].get("baseline") or {}
        blocked_lines = ",\n".join(
            f"              '{sid}' if not is_state('input_boolean.{grp}_{sid}','on') else ''"
            for sid in sids)
        items = "\n".join(
            f'            - {{ name: "{c["name"]}", ids: {json.dumps([str(i) for i in c["ids"]])}, '
            f'tags: {json.dumps(([cfg["groups"][grp]["tag"]] if cfg["groups"][grp].get("tag") else []) + list(c.get("tags", [])))}, '
            f'parental: {str(bool(base.get("parental"))).lower()}, '
            f'safebrowsing: {str(bool(base.get("safebrowsing"))).lower()}, '
            f'safesearch: {str(bool(base.get("safe_search"))).lower()} }}'
            for c in cfg["clients"] if c["group"] == grp)
        parts.append(f"""  adguard_sync_{grp}:
    alias: AdGuard sync {grp}
    sequence:
      - variables:
          blocked: >-
            {{{{ [
{blocked_lines}
            ] | select | list }}}}
      - repeat:
          for_each:
{items}
          sequence:
            - service: rest_command.adguard_update_client
              data:
                name: "{{{{ repeat.item.name }}}}"
                ids: "{{{{ repeat.item.ids }}}}"
                tags: "{{{{ repeat.item.tags }}}}"
                parental: "{{{{ repeat.item.parental }}}}"
                safebrowsing: "{{{{ repeat.item.safebrowsing }}}}"
                safesearch: "{{{{ repeat.item.safesearch }}}}"
                blocked: "{{{{ blocked }}}}\"""")
    return "\n".join(parts) + "\n"


def _entity_list(entities):
    return "[" + ", ".join(entities) + "]"


def gen_automations(cfg):
    a = ["automation:"]
    sync_groups = [g for g in cfg["groups"] if group_services(cfg, g)]

    for grp in sync_groups:
        ents = [f"input_boolean.{grp}_{s}" for s in group_services(cfg, grp)]
        a.append(f"""  - alias: "IaC · sync {grp} on switch change"
    trigger:
      - platform: state
        entity_id: {_entity_list(ents)}
    action:
      - service: script.adguard_sync_{grp}""")

    a.append(f"""  - alias: "IaC · re-assert all on HA start"
    trigger:
      - platform: homeassistant
        event: start
    action:
{chr(10).join(f'      - service: script.adguard_sync_{g}' for g in sync_groups)}""")

    for name, s in (cfg.get("schedules") or {}).items():
        grp = s["group"]
        ents = [f"input_boolean.{grp}_{sid}" for sid in s["allow"]]
        a.append(f"""  - alias: "IaC · schedule {name}"
    trigger:
      - platform: state
        entity_id: schedule.{name}
    action:
      - choose:
          - conditions: "{{{{ trigger.to_state.state == 'on' }}}}"
            sequence:
              - service: input_boolean.turn_on
                target:
                  entity_id: {_entity_list(ents)}
        default:
          - condition: state
            entity_id: input_boolean.holiday_mode
            state: "off"
          - service: input_boolean.turn_off
            target:
              entity_id: {_entity_list(ents)}""")

    for ov in cfg.get("overrides", []):
        grp = ov["group"]
        ents = [f"input_boolean.{grp}_{s}" for s in ov["services"]]
        a.append(f"""  - alias: "IaC · {ov['name']} on"
    trigger:
      - platform: state
        entity_id: input_boolean.{ov['name']}
        to: "on"
    action:
      - service: input_boolean.turn_on
        target:
          entity_id: {_entity_list(ents)}""")
        if ov.get("auto_off"):
            a.append(f"""  - alias: "IaC · {ov['name']} auto-off"
    trigger:
      - platform: time
        at: "{hhmmss(ov['auto_off'])}"
    condition:
      - condition: state
        entity_id: input_boolean.{ov['name']}
        state: "on"
    action:
      - service: input_boolean.turn_off
        target: {{ entity_id: input_boolean.{ov['name']} }}
      - service: input_boolean.turn_off
        target:
          entity_id: {_entity_list(ents)}""")
    return "\n".join(a) + "\n"


def gen_package(cfg):
    header = ("# GENERATED by deploy.py from screentime.yaml — do not edit by hand.\n"
              "# Re-run the deploy tool to regenerate.\n\n")
    blocks = [gen_input_boolean(cfg), gen_schedule(cfg),
              REST_CMD.replace("__URL__", (os.environ.get("ADGUARD_URL") or cfg["adguard"]["url"]).rstrip("/")),
              gen_scripts(cfg), gen_automations(cfg)]
    return header + "\n".join(b for b in blocks if b.strip()) + "\n"


def gen_dashboard(cfg):
    cards = []
    for grp in cfg["groups"]:
        sids = group_services(cfg, grp)
        if not sids:
            continue
        cards.append({"type": "entities", "title": grp.capitalize(),
                      "entities": [f"input_boolean.{grp}_{s}" for s in sids]})
    extra = [f"input_boolean.{o['name']}" for o in cfg.get("overrides", [])]
    if cfg.get("schedules"):
        extra.append("input_boolean.holiday_mode")
    if extra:
        cards.append({"type": "entities", "title": "Overrides", "entities": extra})
    sched_ents = [f"schedule.{n}" for n in (cfg.get("schedules") or {})]
    if sched_ents:
        cards.append({"type": "entities", "title": "Schedules", "entities": sched_ents})
    dash = {"title": "Screen time",
            "views": [{"title": "Screen time", "path": "screen-time",
                       "icon": "mdi:television-guide", "cards": cards}]}
    return "# GENERATED by deploy.py\n" + yaml.safe_dump(dash, sort_keys=False, allow_unicode=True)


def write_ha(cfg, dry):
    out_dir = os.environ.get("HA_CONFIG_DIR") or str(HERE / "out")
    pkg_dir = Path(out_dir) / "packages"
    pkg_path = pkg_dir / "screentime_generated.yaml"
    dash_path = Path(out_dir) / "screentime-dashboard.yaml"
    pkg = gen_package(cfg)
    dash = gen_dashboard(cfg)
    print(f"HA output dir: {out_dir}")
    if dry:
        print(f"  would write {pkg_path}  ({pkg.count(chr(10))} lines)")
        print(f"  would write {dash_path}")
        return
    pkg_dir.mkdir(parents=True, exist_ok=True)
    pkg_path.write_text(pkg, encoding="utf-8")
    dash_path.write_text(dash, encoding="utf-8")
    print(f"  wrote {pkg_path}")
    print(f"  wrote {dash_path}")
    reload_ha()


def reload_ha():
    ha_url, token = os.environ.get("HA_URL"), os.environ.get("HA_TOKEN")
    if not (ha_url and token):
        print("  (set HA_URL + HA_TOKEN to auto-reload; else restart HA once to pick up new entities)")
        return
    try:
        r = requests.post(ha_url.rstrip("/") + "/api/services/homeassistant/reload_all",
                          headers={"Authorization": f"Bearer {token}"}, timeout=30)
        r.raise_for_status()
        print("  HA reload_all OK (first-time NEW entities may still need one full restart)")
    except Exception as e:
        print(f"  HA reload failed: {e}")


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    load_env()
    ap = argparse.ArgumentParser(description="Reconcile screentime.yaml to AdGuard + Home Assistant")
    ap.add_argument("--config", default=str(HERE / "screentime.yaml"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--adguard", action="store_true", help="AdGuard only")
    ap.add_argument("--ha", action="store_true", help="Home Assistant only")
    ap.add_argument("--reset-services", action="store_true",
                    help="reset AdGuard blocked_services to config defaults")
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="print generated HA package and exit")
    args = ap.parse_args()

    cfg = load_config(args.config)

    if args.do_print:
        print(gen_package(cfg))
        return

    do_ag = args.adguard or not args.ha
    do_ha = args.ha or not args.adguard
    mode = "DRY-RUN" if args.dry_run else "APPLY"
    print(f"=== screentime deploy [{mode}] ===")
    if do_ag:
        reconcile_adguard(cfg, args.dry_run, args.reset_services)
    if do_ha:
        write_ha(cfg, args.dry_run)
    print("done.")


if __name__ == "__main__":
    main()
