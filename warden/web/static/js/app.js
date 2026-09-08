/* ============================================================================
 * Warden — app.js
 * A no-build, vanilla-JS single-page app for the Warden parental-control hub.
 *
 * Structure (top → bottom):
 *   1. dom helpers        — a tiny hyperscript `h()` (safe: strings become text)
 *   2. formatters/icons   — time, cron, trigger + action humanizers, SVG icons
 *   3. API layer          — fetch wrapper over /api/*
 *   4. store              — plain object; explicit render(), no magic reactivity
 *   5. live client        — /ws with 5s /api/state polling fallback + reconnect
 *   6. view renderers     — dashboard / rules / sources / activity / about
 *   7. boot               — wire tabs, load config, connect live, mount default
 *
 * The API returns ServiceState as the strings "allowed" (=on) / "blocked" (=off).
 * ========================================================================== */
(() => {
"use strict";

/* ── 1. dom helpers ─────────────────────────────────────────────────────── */

/** Hyperscript. attrs: {class, text, html(trusted), dataset, on<Event>, ...attrs}.
 *  Children may be nodes, strings (→ text nodes, XSS-safe), arrays, or null. */
function h(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;                 // trusted constants only
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k === "value") node.value = v;
    else if (k === "checked") node.checked = !!v;
    else if (k.startsWith("on") && typeof v === "function")
      node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v === true) node.setAttribute(k, "");
    else node.setAttribute(k, v);
  }
  appendKids(node, kids);
  return node;
}
function appendKids(node, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
}
const $ = (sel, root = document) => root.querySelector(sel);

/** Accessible switch (real checkbox styled as a track+thumb). */
function toggle({ checked, onchange, label, disabled, danger }) {
  const input = h("input", {
    type: "checkbox", checked, disabled, "aria-label": label,
    onchange: (e) => onchange(e.target.checked),
  });
  return h("label", { class: "switch" + (danger ? " switch-danger" : "") },
    input, h("span", { class: "track" }, h("span", { class: "thumb" })));
}

/* ── 2. formatters + icons ──────────────────────────────────────────────── */

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
function fmtDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function timeAgo(iso) {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (isNaN(s)) return "—";
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

/** Best-effort human hint for a 5-field cron (falls back to the raw string). */
function cronHint(cron) {
  if (!cron || typeof cron !== "string") return "";
  const p = cron.trim().split(/\s+/);
  if (p.length !== 5) return cron;
  const [m, hr] = p;
  const hhmm = (hh, mm) => `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
  // every-N-minutes window, e.g. "*/30 6-21 * * *"
  if (m.startsWith("*/")) {
    const win = /^(\d+)-(\d+)$/.exec(hr);
    const span = win ? ` between ${win[1]}:00–${win[2]}:00` : "";
    return `every ${m.slice(2)} min${span}`;
  }
  // list of fixed times, e.g. "0 7,13,18 * * *"
  if (/^\d+$/.test(m) && /^[\d,]+$/.test(hr)) {
    const times = hr.split(",").map((h2) => hhmm(h2, m)).join(", ");
    return `daily at ${times}`;
  }
  return cron;
}

const TRIGGER_ICONS = { schedule: "⏰", signal: "⚡", manual: "✋" };
function triggerInfo(trig) {
  if (!trig) return { icon: "•", label: "not compiled" };
  if (trig.kind === "schedule")
    return { icon: TRIGGER_ICONS.schedule, label: trig.describe || cronHint(trig.cron) };
  if (trig.kind === "signal")
    return { icon: TRIGGER_ICONS.signal, label: trig.describe || `when ${trig.signal}` };
  return { icon: TRIGGER_ICONS.manual, label: trig.describe || "manual" };
}

/** One-line human echo of a compiled action. */
function actionText(a) {
  const onoff = (st) => (st === "blocked" ? "block" : "allow");
  switch (a.kind) {
    case "set_service":    return `${onoff(a.state)} ${a.service} · ${a.group}`;
    case "set_group":      return `${a.state === "blocked" ? "disable all" : "enable all"} ${a.group} devices`;
    case "set_client":     return `${a.state === "blocked" ? "disable" : "enable"} ${a.client}`;
    case "set_protection": return `protection ${a.enabled ? "on" : "off"}`;
    case "add_rule":       return `+ filter ${a.rule}`;
    case "remove_rule":    return `− filter ${a.rule}`;
    default:               return a.kind;
  }
}

// small inline SVG icon set keyed by config service `icon` names (no icon font).
const ICON_PATHS = {
  globe:   `<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 2.6 2.6 15.4 0 18M12 3c-2.6 2.6-2.6 15.4 0 18"/>`,
  youtube: `<rect x="3" y="6" width="18" height="12" rx="3.5"/><path d="M11 9.3l4.2 2.7-4.2 2.7z" fill="currentColor" stroke="none"/>`,
  film:    `<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M8 4v16M16 4v16M3 9h5M16 9h5M3 15h5M16 15h5"/>`,
  music:   `<path d="M9 18V6l11-2v11"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="15" r="3"/>`,
  gamepad: `<rect x="2.5" y="7.5" width="19" height="9" rx="4.5"/><path d="M7 11v3M5.5 12.5h3"/><circle cx="16" cy="11.5" r="1"/><circle cx="18" cy="13.5" r="1"/>`,
  message: `<path d="M4.5 5h15v10H9l-4.5 4V5z"/>`,
  shield:  `<path d="M12 3l7 3v5c0 5-3.5 8-7 9.5C8.5 19 5 16 5 11V6z"/><path d="M9 12l2 2 4-4.5"/>`,
  play:    `<path d="M7 4.5l12 7.5-12 7.5z"/>`,
  stop:    `<rect x="5.5" y="5.5" width="13" height="13" rx="2"/>`,
  zap:     `<path d="M13 2L5 13h6l-1 9 8-11h-6z"/>`,
  clock:   `<circle cx="12" cy="12" r="9"/><path d="M12 7.5V12l3.5 2"/>`,
  edit:    `<path d="M4 20h4L20 8l-4-4L4 16z"/><path d="M14.5 5.5L18.5 9.5"/>`,
};
function icon(name, size = 18) {
  const inner = ICON_PATHS[name] || ICON_PATHS.globe;
  return h("span", {
    class: "ico",
    html: `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none"
             stroke="currentColor" stroke-width="1.6" stroke-linecap="round"
             stroke-linejoin="round">${inner}</svg>`,
  });
}

/* ── 3. API layer ───────────────────────────────────────────────────────── */

const API = {
  base: "/api",
  async req(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(this.base + path, opts);
    const raw = await res.text();
    let data = null;
    if (raw) { try { data = JSON.parse(raw); } catch { data = raw; } }
    if (!res.ok) {
      let msg = (data && data.detail) || (typeof data === "string" && data) || res.statusText;
      if (typeof msg !== "string")   // pydantic validation errors: [{loc, msg, ...}]
        msg = Array.isArray(msg) ? msg.map((e) => e.msg || JSON.stringify(e)).join("; ")
                                 : JSON.stringify(msg);
      const err = new Error(msg); err.status = res.status; err.data = data;
      throw err;
    }
    return data;
  },
  get(p)        { return this.req("GET", p); },
  post(p, b)    { return this.req("POST", p, b === undefined ? {} : b); },
  put(p, b)     { return this.req("PUT", p, b === undefined ? {} : b); },
  del(p, b)     { return this.req("DELETE", p, b); },
};

/* ── 4. store ───────────────────────────────────────────────────────────── */

const store = {
  view: "dashboard",
  connected: false,
  snapshot: null,     // StateSnapshot
  config: null,       // /api/config summary
  status: null,       // AdGuardStatus (about view)
  rules: [],          // Rule[]
  sources: [],        // source dicts
  signals: [],        // Signal[] (live values)
  audit: [],          // AuditEntry[]
  holds: {},          // "group/service" -> manual hold info (evaluator won't touch)
  draft: "",          // add-rule composer text (survives re-render)
  preview: null,      // { compiled } | { error }
  enableNew: true,    // "enabled" for new rules
  editing: null,      // id of the rule whose text is open in the inline editor
  editDraft: null,    // that editor's working text (survives re-render)
  targets: {},        // per-source weekly workload target, keyed by source key
  hostServices: [],   // managed services on hosts (start/stop over SSH)
  devices: [],        // DeviceInfo[] — every client AdGuard knows about
  durations: ["2h", "4h", "24h", "forever"],   // unblock windows offered by the API
  busyDevice: null,   // device name mid-request (disables its buttons)
  protectionBusy: false,   // re-enable request in flight (protection gate)
  reminders: null,    // GET /reminders payload (school days, events, forms)
  remindersBusy: false,    // a re-read is in flight (it costs an LLM call)
  remindersAllDays: false, // "show more days" expanded past the first week
  showLlm: true,      // include the "llm_call" lines in the Activity feed
  llmOpen: null,      // id of the exchange whose transcript is on screen
};

/** Merge live Signal(s) into store.signals, keyed by `key`. */
function upsertSignals(list) {
  const map = new Map(store.signals.map((s) => [s.key, s]));
  for (const s of list) map.set(s.key, s);
  store.signals = [...map.values()];
}

/* ── 5. live client (WebSocket + polling fallback) ──────────────────────── */

const live = {
  ws: null, pollTimer: null, reconnectTimer: null, backoff: 1000,

  connect() {
    clearTimeout(this.reconnectTimer);
    const proto = location.protocol === "https:" ? "wss" : "ws";
    let ws;
    try { ws = new WebSocket(`${proto}://${location.host}/ws`); }
    catch { this.scheduleReconnect(); return; }
    this.ws = ws;
    ws.onopen = () => { store.connected = true; this.backoff = 1000; this.stopPolling(); updateChrome(); };
    ws.onmessage = (e) => { try { this.onMessage(JSON.parse(e.data)); } catch {} };
    ws.onerror = () => { try { ws.close(); } catch {} };
    ws.onclose = () => {
      this.ws = null; store.connected = false; updateChrome();
      this.startPolling(); this.scheduleReconnect();
    };
  },

  scheduleReconnect() {
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => this.connect(), this.backoff);
    this.backoff = Math.min(this.backoff * 1.7, 15000);
  },

  // fallback: poll /api/state every 5s while the socket is down
  startPolling() {
    if (this.pollTimer) return;
    this.pollTimer = setInterval(async () => {
      try { applySnapshot(await API.get("/state")); } catch {}
    }, 5000);
  },
  stopPolling() { clearInterval(this.pollTimer); this.pollTimer = null; },

  onMessage(msg) {
    switch (msg.type) {
      case "state":   if (msg.holds !== undefined) store.holds = msg.holds || {};
                      applySnapshot(msg.snapshot); break;
      case "signals": upsertSignals(msg.signals || []); if (store.view === "sources") render(); break;
      case "signal":  applySignal(msg.signal); break;
      case "audit":   applyAudit(msg.entry); break;
      case "error":   toast(msg.detail || "server error", "error"); break;
    }
  },
};

// live-update appliers — targeted where inputs must survive, else re-render.
function applySnapshot(snap) {
  if (!snap) return;
  if (snap.holds !== undefined) store.holds = snap.holds || {};   // /api/state carries them
  store.snapshot = snap;
  updateChrome();
  if (store.view === "dashboard") render();   // dashboard has no text inputs to lose
}
function applySignal(sig) {
  if (!sig) return;
  upsertSignals([sig]);
  applyTargetSignal(sig);
  if (store.view === "sources") patchSignalValue(sig);  // keep simulate inputs intact
}
/** The weekly-target row's "today" figure IS these two signals, so patch it from them
 *  rather than refetching the target every time a collection lands. */
function applyTargetSignal(sig) {
  const t = sig.source && store.targets ? store.targets[sig.source] : null;
  if (!t || typeof sig.key !== "string") return;
  const field = sig.key.endsWith(".islands_due_today") ? "islands_due_today"
    : sig.key.endsWith(".islands_completed_today") ? "completed_today" : null;
  if (!field) return;
  t.today = Object.assign({}, t.today, { [field]: sig.value });
  if (sig.meta && sig.meta.basis) t.today.explain = sig.meta.basis;
  if (store.view === "sources") render();
}
function applyAudit(entry) {
  if (!entry) return;
  store.audit.unshift(entry);
  if (entry.action === "set_protection")
    store.protectionOffInfo = undefined;   // re-resolve who/why on the next gate render
  if (store.audit.length > 300) store.audit.pop();
  if (store.view === "activity") {
    const list = $("#auditList");
    if (list) { list.prepend(auditRow(entry)); while (list.children.length > 300) list.lastChild.remove(); }
  }
}

/* ── toasts ─────────────────────────────────────────────────────────────── */
function toast(text, kind = "") {
  const t = h("div", { class: "toast " + kind, text });
  $("#toasts").append(t);
  setTimeout(() => { t.style.opacity = "0"; setTimeout(() => t.remove(), 300); }, 3600);
}

/* ── chrome: status pill + live dot (updated without a full re-render) ───── */
function updateChrome() {
  const ag = store.snapshot && store.snapshot.adguard;
  const pill = $("#statusPill");
  if (pill) {
    const cls = !ag ? "status-unknown"
      : ag.mode === "live" && ag.reachable ? "status-live"
      : ag.reachable ? "status-fake" : "status-down";
    pill.className = "status-pill " + cls;
    const label = !ag ? "connecting…"
      : `AdGuard ${ag.mode}${ag.version ? " · " + ag.version : ""}`;
    pill.replaceChildren(
      ...[
        h("span", { class: "status-dot" }),
        h("span", { class: "status-label" }, label),
        ag && ag.protection_enabled === false
          ? h("span", { class: "status-sub" }, "· protection off") : null,
      ].filter(Boolean),   // DOM replaceChildren() coerces a bare null to the text "null"
    );
  }
  const dot = $("#liveDot");
  if (dot) {
    dot.className = "live-dot " + (store.connected ? "live-on" : "live-off");
    const txt = $(".live-dot-text", dot);
    if (txt) txt.textContent = store.connected ? "live" : "reconnecting";
  }
  updateProtectionGate();
}

/* ── protection gate ────────────────────────────────────────────────────────
 * Protection off means AdGuard is filtering nothing: every block Warden holds is
 * inert, on every device. Showing that as a small pill invites you to keep toggling
 * services that silently do nothing, so the whole UI is blocked until it's back on.
 *
 * Gated on `=== false` ONLY. An unreachable AdGuard reports null, and locking the UI
 * when we simply don't know the state would leave no way back in.
 */
async function enableProtection() {
  store.protectionBusy = true; updateProtectionGate();
  try {
    const st = await API.post("/actions/protection",
      { enabled: true, device: localStorage.getItem("warden_device") || "" });
    if (st) store.snapshot = { ...store.snapshot, adguard: st };
    toast("Protection re-enabled", "ok");
  } catch (e) {
    toast("Could not enable protection: " + e.message, "error");
  } finally {
    store.protectionBusy = false;
    render();                       // render() → updateChrome() → updateProtectionGate()
  }
}

/* Turning protection OFF is gated behind a reason. The server refuses an empty
 * reason and refuses "the kids want to stream" phrased any way it recognises —
 * so the dialog says so up front, and whatever is typed lands in the activity
 * log next to the device name. The name is remembered per browser. */
function protectionOffDialog() {
  return new Promise((resolve) => {
    const wrap = h("div", { class: "pgate" });      // reuse the overlay look (grid-centred)
    const err = h("p", { class: "pod-error", hidden: true });
    const reason = h("textarea", {
      class: "pod-reason", rows: 2, maxlength: 200,
      placeholder: "e.g. my work VPN isn't connecting / a site I need is being blocked",
    });
    const device = h("input", {
      class: "pod-device", type: "text", maxlength: 60,
      placeholder: "e.g. Claire's iPhone, kitchen laptop…",
      value: localStorage.getItem("warden_device") || "",
    });
    const app = $("#app");
    if (app) app.inert = true;                       // actually modal, not just aria
    let busy = false;                                // POST in flight: point of no return
    const close = (result) => {
      if (busy) return;
      document.removeEventListener("keydown", onKey);
      wrap.remove();
      if (app) app.inert = false;
      updateProtectionGate();                        // re-assert inert if the lockout shows
      resolve(result);
    };
    const onKey = (e) => { if (e.key === "Escape") close(null); };
    const fail = (m) => { err.textContent = m; err.hidden = false; };
    const submit = async () => {
      const why = reason.value.trim(), who = device.value.trim();
      if (!who) { fail("Say who is turning it off — the name is remembered, so this device only asks once."); device.focus(); return; }
      localStorage.setItem("warden_device", who);
      busy = true;
      btn.disabled = true; btn.textContent = "Turning off…";
      cancelBtn.disabled = true;                     // a cancel now would be a lie —
      try {                                          // the request is already on the wire
        const st = await API.post("/actions/protection",
          { enabled: false, reason: why, device: who });
        busy = false;
        close(st || true);
      } catch (e) {
        busy = false;
        fail(e.message);                             // the server's guidance, shown in-dialog
        btn.disabled = false; btn.textContent = "Turn everything off";
        cancelBtn.disabled = false;
      }
    };
    const btn = h("button", { class: "btn danger", type: "button", onclick: submit },
      "Turn everything off");
    const cancelBtn = h("button", { class: "btn", type: "button", onclick: () => close(null) }, "Cancel");
    wrap.append(h("div", { class: "pgate-card pod-card", role: "dialog", "aria-modal": "true" },
      h("div", { class: "pgate-badge" }, "⚠"),
      h("h1", { class: "pgate-title" }, "Turn ALL protection off?"),
      h("p", { class: "pgate-body" },
        "This switches off every filter for everyone — ad blocking, safe search, and " +
        "all the kids' blocks — until someone turns it back on. If this is about " +
        "letting the kids stream, close this and use the Streaming buttons instead."),
      h("label", { class: "pod-label" }, "Why does it need to be off? (recorded in the activity log)"),
      reason, err,
      h("label", { class: "pod-label" }, "Who is turning it off?"),
      device,
      h("div", { class: "pod-btns" }, cancelBtn, btn),
    ));
    wrap.addEventListener("click", (e) => { if (e.target === wrap) close(null); });
    document.addEventListener("keydown", onKey);
    document.body.append(wrap);
    reason.focus();
  });
}

function updateProtectionGate() {
  const gate = $("#protectionGate");
  if (!gate) return;
  const ag = store.snapshot && store.snapshot.adguard;
  const off = !!ag && ag.protection_enabled === false;

  gate.hidden = !off;
  // inert takes the app behind the overlay out of the tab order too, so "nothing
  // else works" holds for keyboard users, not just for the mouse.
  const app = $("#app");
  if (app) app.inert = off;
  if (!off) { gate.replaceChildren(); store.protectionOffInfo = undefined; return; }

  // Name the person on the lockout screen. The audit entry holds "DISABLED by <who>
  // — reason: …"; fetch it once per gate-opening so accountability is on the screen
  // every housemate sees, not just in the activity view.
  if (store.protectionOffInfo === undefined) {
    store.protectionOffInfo = null;               // fetch in flight
    API.get("/audit?limit=200").then((rows) => {
      const hit = (rows || []).find((e) =>
        e.action === "set_protection" && /^DISABLED/.test(e.detail || ""));
      if (hit) {
        store.protectionOffInfo = { at: hit.at, detail: hit.detail };
        updateProtectionGate();
      }
    }).catch(() => {});
  }
  const info = store.protectionOffInfo;

  gate.replaceChildren(h("div", {
      class: "pgate-card", role: "alertdialog", "aria-modal": "true",
      "aria-labelledby": "pgateTitle",
    },
    h("div", { class: "pgate-badge" }, "⚠"),
    h("h1", { class: "pgate-title", id: "pgateTitle" }, "Protection is off"),
    h("p", { class: "pgate-body" },
      "AdGuard is not filtering anything. Every block Warden is holding — YouTube, " +
      "Netflix, the lot — is doing nothing, on every device, for everyone in the house."),
    info && info.detail
      ? h("p", { class: "pgate-who" }, `${info.detail} · ${fmtTime(info.at)}`)
      : null,
    h("button", {
      class: "btn pgate-btn", type: "button", disabled: store.protectionBusy,
      onclick: enableProtection,
    }, store.protectionBusy ? "Turning it on…" : "Turn protection back on"),
    h("p", { class: "pgate-foot" }, "Warden is locked until protection is back on."),
  ));
}

/* ── 6. view renderers ──────────────────────────────────────────────────── */

// -- Dashboard ---------------------------------------------------------------
function renderDashboard() {
  const snap = store.snapshot;
  if (!snap) return loading();
  const ag = snap.adguard || {};
  const wrap = h("div", {});

  // AdGuard status banner + protection toggle
  wrap.append(h("div", { class: "status-banner" },
    h("div", { class: "sb-main" },
      h("div", { class: "sb-icon" }, icon("shield", 22)),
      h("div", { class: "sb-meta" },
        h("div", { class: "sb-title" }, `AdGuard Home · ${ag.mode || "?"} mode`),
        h("div", { class: "sb-sub" },
          `${ag.reachable ? "reachable" : "unreachable"}` +
          `${ag.version ? " · v" + ag.version : ""}` +
          `${ag.url ? " · " + ag.url : ""}`),
      ),
    ),
    h("div", { class: "protection-toggle" },
      h("span", { class: "pt-label" }, "Network protection"),
      h("span", { class: "pt-state", style: `color:${ag.protection_enabled === false ? "var(--danger)" : "var(--ok)"}` },
        ag.protection_enabled === false ? "OFF" : "ON"),
      toggle({
        checked: ag.protection_enabled !== false, label: "Toggle AdGuard protection",
        danger: ag.protection_enabled === false,
        onchange: async (on) => {
          if (!on) {
            // reason-gated: the dialog does the POST (or is cancelled). Re-render
            // either way so the checkbox snaps back if nothing was turned off.
            const st = await protectionOffDialog();
            if (st && st.protection_enabled !== undefined) {
              store.snapshot = { ...store.snapshot, adguard: st };
              toast("Protection disabled — logged", "warn");
            }
            updateChrome(); render();
            return;
          }
          try {
            const st = await API.post("/actions/protection",
              { enabled: true, device: localStorage.getItem("warden_device") || "" });
            if (st) { store.snapshot = { ...store.snapshot, adguard: st }; updateChrome(); render(); }
            toast("Protection enabled", "ok");
          } catch (e) { toast("Protection: " + e.message, "error"); render(); }
        },
      }),
    ),
  ));

  // one-tap presets, built entirely from config.quick_actions
  const quick = (store.config && Array.isArray(store.config.quick_actions))
    ? store.config.quick_actions : [];
  if (quick.length) {
    const bar = h("div", { class: "quick-bar" },
      h("div", { class: "quick-title" }, "Quick actions"));
    const btns = h("div", { class: "quick-btns" });
    for (const q of quick) btns.append(quickButton(q));
    bar.append(btns);
    wrap.append(bar);
  }

  // Managed services live on their own, above the per-group cards, because they are
  // house-wide: stopping one stops it for everybody, not for a group.
  const hs = store.hostServices || [];
  if (hs.length) {
    const box = h("div", { class: "hostsvc-bar" },
      h("div", { class: "hostsvc-title" }, "Whole-house services"),
      h("div", { class: "hostsvc-sub" },
        "These start and stop the real service. They affect everyone, and anything " +
        "playing will stop."));
    const row = h("div", { class: "hostsvc-row" });
    for (const s of hs) row.append(hostServiceCard(s));
    box.append(row);
    wrap.append(box);
  }

  // one card per group
  const groups = snap.groups || [];
  if (!groups.length) wrap.append(h("p", { class: "empty" }, "No managed groups configured."));
  const grid = h("div", { class: "grid grid-cards" });
  for (const g of groups) grid.append(groupCard(g));
  wrap.append(grid);
  return wrap;
}

/** One managed service (e.g. the Plex server on the NAS) with a start/stop toggle. */
function hostServiceCard(s) {
  const unknown = s.running === null || s.running === undefined;
  const card = h("div", { class: "hostsvc" });
  card.append(h("div", { class: "hostsvc-ico" }, icon(s.icon || "server", 20)));

  const hold = store.holds && store.holds[`_host/${s.id}`];
  const meta = h("div", { class: "hostsvc-meta" },
    h("div", { class: "hostsvc-name" }, s.name || s.id,
      hold ? h("span", {
        class: "badge badge-hold", style: "margin-left:8px",
        title: "Set by hand — rules leave this alone until the next scheduled reset",
      }, "✋ manual") : null),
    h("div", { class: "hostsvc-detail" },
      unknown ? (s.detail || "state unknown") : (s.description || "")));
  card.append(meta);

  card.append(h("span", {
    class: "hostsvc-state " + (unknown ? "is-unknown" : (s.running ? "is-up" : "is-down")),
  }, unknown ? "?" : (s.running ? "RUNNING" : "STOPPED")));

  card.append(toggle({
    checked: !!s.running,
    label: `Toggle ${s.name || s.id}`,
    danger: !s.running,
    onchange: async (on) => {
      if (!on && s.confirm &&
          !confirm(`Stop ${s.name}?\n\nThis stops it for EVERYONE in the house and ` +
                   `anything currently playing will stop.`)) {
        render();
        return;
      }
      try {
        await API.post(`/hosts/services/${encodeURIComponent(s.id)}`, { running: on });
        toast(`${s.name}: ${on ? "starting" : "stopping"}…`, "ok");
        store.hostServices = await API.get("/hosts/services");
        render();
      } catch (e) {
        toast(`${s.name}: ${e.message}`, "error");
        render();
      }
    },
  }));
  return card;
}

/** A single configurable quick-action button. */
function quickButton(q) {
  const cls = {
    good: "btn-good", warn: "btn-warn", danger: "btn-danger", default: "",
  }[q.style || "default"] || "";
  const btn = h("button", {
    class: `btn btn-quick ${cls}`.trim(), type: "button",
    title: q.description || q.label,
    onclick: async () => {
      if (q.confirm && !confirm(`${q.label}\n\n${q.description || ""}\n\nApply now?`)) return;
      btn.disabled = true;
      try {
        const snap = await API.post(`/actions/quick/${encodeURIComponent(q.id)}`);
        if (snap) store.snapshot = snap;
        const extra = q.revert_after_minutes ? ` — reverts in ${q.revert_after_minutes}m` : "";
        toast(`${q.label} applied${extra}`, "ok");
        render();
      } catch (e) {
        toast(`${q.label}: ${e.message}`, "error");
      } finally {
        btn.disabled = false;
      }
    },
  }, icon(q.icon || "zap", 15), h("span", {}, q.label));
  if (q.revert_after_minutes) {
    btn.append(h("span", { class: "quick-timer" }, `${q.revert_after_minutes}m`));
  }
  return btn;
}

function groupCard(g) {
  const card = h("div", { class: "card" });
  card.append(h("div", { class: "card-head" },
    h("div", { class: "card-title" },
      g.name,
      g.tag ? h("span", { class: "badge badge-muted" }, g.tag) : null,
      g.all_blocked ? h("span", { class: "badge badge-danger" }, "all off") : null,
    ),
  ));

  const body = h("div", { class: "card-body" });
  for (const svc of (g.services || [])) {
    // `mixed` = some devices block, some don't (e.g. one device has a timed
    // unblock). Render it as its own state — showing the average as "allowed"
    // once sent the family hunting through the wrong switches — and draw the
    // toggle unchecked so one tap means "allow it on EVERY device".
    const mixed = !!svc.mixed;
    const on = !mixed && svc.state === "allowed";
    const hold = store.holds && store.holds[`${g.name}/${svc.id}`];
    body.append(h("div", { class: "svc-row" },
      h("span", { class: "svc-ico" }, icon(svc.icon)),
      h("span", { class: "svc-name" }, svc.name || svc.id),
      hold ? h("span", {
        class: "badge badge-hold",
        title: "Set by hand — rules leave this alone until the next scheduled reset (the 8pm switchoff)",
      }, "✋ manual") : null,
      mixed
        ? h("span", {
            class: "svc-state mixed",
            title: `Still blocked on: ${(svc.blocked_on || []).join(", ")}`,
          }, `mixed · blocked on ${(svc.blocked_on || []).length} device` +
             ((svc.blocked_on || []).length === 1 ? "" : "s"))
        : h("span", { class: "svc-state " + (on ? "on" : "off") }, on ? "allowed" : "blocked"),
      toggle({
        checked: on, label: `${svc.name || svc.id} for ${g.name}`,
        onchange: (checked) => setService(g.name, svc.id, checked),
      }),
    ));
  }
  if (!(g.services || []).length) body.append(h("p", { class: "empty" }, "No services for this group."));

  // client chips
  if ((g.clients || []).length) {
    body.append(h("div", { class: "clients-row", style: "margin-top:12px" },
      ...g.clients.map((c) => h("span", {
        class: "chip client-chip" + (c.online === false ? " offline" : ""),
        title: (c.ids || []).join(", ") + (c.exists_in_adguard === false ? " (not in AdGuard)" : ""),
      }, c.name, c.subject ? h("span", { class: "badge-muted", style: "color:var(--text-faint)" }, " · " + c.subject) : null)),
    ));
  }
  card.append(body);

  card.append(h("div", { class: "card-foot" },
    h("button", { class: "btn btn-sm", type: "button",
      onclick: () => setGroup(g.name, "blocked") }, "⏾ All off (bedtime)"),
    h("button", { class: "btn btn-sm btn-ghost", type: "button",
      onclick: () => setGroup(g.name, "allowed") }, "All on"),
  ));
  return card;
}

async function setService(group, service, checked) {
  const state = checked ? "allowed" : "blocked";
  try { applySnapshot(await API.post("/actions/service", { group, service, state })); }
  catch (e) { toast("Toggle failed: " + e.message, "error"); render(); }
}
async function setGroup(group, state) {
  try {
    applySnapshot(await API.post("/actions/group", { group, state }));
    toast(`${group}: all ${state === "blocked" ? "off" : "on"}`, "ok");
  } catch (e) { toast("Group action failed: " + e.message, "error"); }
}

// -- Reminders ---------------------------------------------------------------
/* The daily "what does he need tomorrow?" page, built server-side from the school
 * portal (GET /api/reminders). Every reminder carries where it came from, so a claim
 * read out of a message can be traced back to the sentence it came from. */

const REMINDER_ICONS = {
  club: "🎟️", kit: "🎒", bring: "📦", wear: "👕", money: "💷", trip: "🚌",
  food: "🍎", lunch: "🍽️", form: "📝", event: "📅", note: "📌",
};

/** A form deadline is a DAY, not a minute — and the portal stamps end-of-day as
 *  23:59Z, which local time would render as 00:59 the following morning. Reading the
 *  parts back in UTC keeps "due 24 Jul" from becoming "due 25 Jul, 00:59". */
function fmtDueDate(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  return `${d.getUTCDate()} ${d.toLocaleDateString("en-GB", { month: "short", timeZone: "UTC" })}`;
}

/** "1 Sep" from an ISO date, without letting the locale pick "Sept" some places and
 *  "Sep" in others — the day cards are labelled server-side and must agree. */
function fmtDayMonth(isoDate) {
  const d = new Date(isoDate + "T12:00:00Z");
  if (isNaN(d)) return isoDate;
  return `${d.getUTCDate()} ${d.toLocaleDateString("en-GB", { month: "short", timeZone: "UTC" })}`;
}

function renderReminders() {
  const r = store.reminders;
  if (!r) return loading();
  const wrap = h("div", {});
  const subj = r.subject || {};

  wrap.append(h("div", { class: "section-head" },
    h("h2", {}, "Daily reminders"),
    h("span", { class: "hint" },
      [subj.name, subj["class"]].filter(Boolean).join(" · ") || "school portal")));

  // Not-live or stale data changes what the page is worth — say so before anything else.
  const dq = r.data || {};
  if (dq.live === false) {
    wrap.append(h("div", { class: "source-warn" }, icon("shield", 15),
      h("div", {}, h("strong", {}, "Not live school data. "),
        h("span", {}, dq.note || "showing the offline sample"))));
  } else if (typeof dq.stale_hours === "number" && dq.stale_hours > 36) {
    wrap.append(h("div", { class: "source-warn" }, icon("clock", 15),
      h("div", {}, h("strong", {}, `Last collected ${Math.round(dq.stale_hours)}h ago. `),
        h("span", {}, "The portal is polled once a day — run the Weduc source to refresh."))));
  }

  wrap.append(schoolStrip(r));
  // Things still fixable go first — a lunch not ordered or a club place still open is
  // the only reason this page needs opening today.
  // Only real gaps get a card — currently a school day with no lunch ordered. Unbooked
  // club sessions are not listed anywhere: most future days are legitimately unbooked
  // and a standing list of them is a to-do list nobody asked for.
  if ((r.alerts || []).length) wrap.append(alertsCard(r.alerts));
  if ((r.payments || []).length) wrap.append(paymentsCard(r.payments));
  if ((r.forms || []).length) wrap.append(formsCard(r.forms));

  // Days worth a row: today and tomorrow always (the answer is "nothing", and that
  // IS the answer), plus any later day that actually has something on it. Capped to a
  // week — with a lunch and two clubs on every school day, the full fortnight is a
  // scroll nobody reads, and the far end is never what you came for.
  const all = (r.days || []).filter((d) => d.delta <= 1 || (d.reminders || []).length);
  const days = store.remindersAllDays ? all : all.slice(0, 7);
  const list = h("div", { class: "rem-days" });
  for (const d of days) list.append(dayCard(d));
  wrap.append(list);
  if (all.length > days.length) {
    wrap.append(h("button", {
      class: "btn btn-sm btn-ghost rem-more", type: "button",
      onclick: () => { store.remindersAllDays = true; render(); },
    }, `Show ${all.length - days.length} more day${all.length - days.length === 1 ? "" : "s"}`));
  }

  if ((r.actions || []).length) {
    wrap.append(h("div", { class: "section-head section-head-sub" },
      h("h3", {}, "No fixed date"),
      h("span", { class: "hint" }, "still outstanding")));
    const box = h("div", { class: "rem-day-body" });
    for (const a of r.actions) box.append(reminderRow(a));
    wrap.append(box);
  }

  if (r.newsletter && r.newsletter.document) wrap.append(newsletterCard(r.newsletter));
  wrap.append(remindersFoot(r));
  return wrap;
}

/** Quiet links straight to each club's ParentPay page — a way in when you DO want to
 *  book something, without a section on the page telling you that you should. */
function clubLinks(clubsInfo) {
  const links = ((clubsInfo || {}).clubs || []).filter((c) => c.url);
  if (!links.length) return null;
  const row = h("div", { class: "rem-links" });
  for (const c of links) {
    row.append(h("a", {
      class: "btn btn-sm btn-ghost", href: c.url, target: "_blank",
      rel: "noopener noreferrer",
      title: c.cutoff || "Open this club's calendar in ParentPay",
    }, c.name));
  }
  return row;
}

/** Short label for a club, so a long official name doesn't dominate the tile. */
function clubShortName(name, clubsInfo) {
  const c = ((clubsInfo || {}).clubs || []).find((x) => x.name === name);
  return { before_school: "Breakfast club", after_school: "After-school club" }[c && c.kind]
    || name;
}

/** Today / tomorrow at a glance: school, clubs and lunch — the whole morning question.
 *  These are the two tiles you actually look at, so the day's booked clubs and meal are
 *  repeated here rather than living only in the day cards further down. */
function schoolStrip(r) {
  const s = r.school || {};
  const tile = (day, when) => {
    if (!day || !day.date) return null;
    const on = day.is_school_day;
    const cls = on === true ? "is-school" : on === false ? "is-off" : "is-unknown";
    const rems = day.reminders || [];
    const clubs = rems.filter((x) => x.kind === "club");
    const lunch = rems.find((x) => x.kind === "lunch" && !x.alert);
    const noLunch = rems.find((x) => x.kind === "lunch" && x.alert);

    const facts = h("div", { class: "rem-tile-facts" });
    if (clubs.length) {
      facts.append(h("div", { class: "rem-tile-fact" },
        h("span", { class: "rem-tile-ico" }, REMINDER_ICONS.club),
        h("span", {}, clubs.map((c) => clubShortName(c.title, r.clubs)).join(" · ")),
        h("span", { class: "badge badge-ok" }, "Booked")));
    }
    if (lunch) {
      facts.append(h("div", { class: "rem-tile-fact" },
        h("span", { class: "rem-tile-ico" }, REMINDER_ICONS.lunch),
        h("span", {}, lunch.title),
        h("span", { class: "badge badge-ok" }, "Booked")));
    } else if (noLunch) {
      facts.append(h("div", { class: "rem-tile-fact" },
        h("span", { class: "rem-tile-ico" }, REMINDER_ICONS.lunch),
        h("span", {}, "School lunch"),
        h("span", { class: "badge badge-danger" }, "Not ordered")));
    }

    return h("div", { class: "rem-tile " + cls },
      h("div", { class: "rem-tile-when" }, when),
      h("div", { class: "rem-tile-date" }, `${day.weekday} ${fmtDayMonth(day.date)}`),
      h("div", { class: "rem-tile-state" },
        on === true ? "School" : on === false ? "No school" : "Not known"),
      h("div", { class: "rem-tile-why" }, day.why || ""),
      facts.children.length ? facts : null,
    );
  };
  const strip = h("div", { class: "rem-strip" },
    tile(s.today, "Today"), tile(s.tomorrow, "Tomorrow"));

  const notes = [];
  if (s.term) notes.push(s.term);
  if (s.in_holidays && s.next_school_day) notes.push(`back on ${s.next_school_day}`);
  if (notes.length) {
    strip.append(h("div", { class: "rem-strip-note" },
      ...notes.map((n) => h("span", { class: "chip" }, n))));
  }
  return strip;
}

/** Genuine gaps only — a school day with nothing ordered for lunch. */
function alertsCard(alerts) {
  const card = h("div", { class: "card rem-alerts is-urgent" });
  card.append(h("div", { class: "card-head" },
    h("div", { class: "card-title" }, "⚠️ Nothing ordered",
      h("span", { class: "badge badge-danger" },
        `${alerts.length} school day${alerts.length === 1 ? "" : "s"}`)),
    h("span", { class: "hint", style: "color:var(--text-faint);font-size:12px" },
      "no school lunch booked")));

  const body = h("div", { class: "card-body" });
  for (const a of alerts) {
    const d = new Date(a.date + "T12:00:00Z");
    const when = isNaN(d) ? a.date
      : `${d.toLocaleDateString("en-GB", { weekday: "short", timeZone: "UTC" })} ${fmtDayMonth(a.date)}`;
    body.append(h("div", { class: "rem-alert" },
      h("span", { class: "rem-ico" }, REMINDER_ICONS[a.kind] || "⚠️"),
      h("div", { class: "rem-main" },
        h("div", { class: "rem-title" }, a.title,
          h("span", { class: "chip rem-at" }, when),
          remBadge(a.badge)),
        a.detail ? h("div", { class: "rem-detail" }, a.detail) : null)));
  }
  card.append(body);
  return card;
}

/** Charges from ParentPay that still need paying — trips, swimming, residentials.
 *  Running balances (meals, clubs) never appear here: they top up, they aren't tasks. */
function paymentsCard(payments) {
  const overdue = payments.filter((p) => p.overdue).length;
  const total = payments.reduce((n, p) => n + (typeof p.amount === "number" ? p.amount : 0), 0);
  const card = h("div", { class: "card rem-pay" + (overdue ? " has-overdue" : "") });
  card.append(h("div", { class: "card-head" },
    h("div", { class: "card-title" }, "💷 To pay",
      h("span", { class: "badge " + (overdue ? "badge-danger" : "badge-warn") },
        total ? `£${total.toFixed(2)}` : `${payments.length} item${payments.length === 1 ? "" : "s"}`)),
    overdue ? h("span", { class: "badge badge-danger" }, `${overdue} overdue`) : null));

  const body = h("div", { class: "card-body" });
  for (const p of payments) {
    const due = p.due
      ? (p.overdue ? "was due " : "due ") + fmtDueDate(p.due)
      : "no due date given";
    body.append(h("div", { class: "rem-form" + (p.overdue ? " is-overdue" : "") },
      h("div", { class: "rem-form-title" },
        // ParentPay prefixes every item with the child's name; on a page that is
        // already about Luke that is just noise in front of the thing you need to read.
        (p.title || "(untitled)").replace(/^\s*Luke\s*-\s*/i, ""),
        p.is_new ? h("span", { class: "badge badge-warn" }, "New") : null),
      h("div", { class: "rem-form-meta" },
        typeof p.amount === "number"
          ? h("span", { class: "badge badge-muted" }, `£${p.amount.toFixed(2)}`) : null,
        h("span", { class: "badge " + (p.overdue ? "badge-danger" : "badge-muted") }, due),
        p.url ? h("a", { class: "btn btn-sm", href: p.url, target: "_blank",
                         rel: "noopener noreferrer" }, "Pay in ParentPay") : null)));
  }
  card.append(body);
  return card;
}

function formsCard(forms) {
  const overdue = forms.filter((f) => f.overdue).length;
  const card = h("div", { class: "card rem-forms" + (overdue ? " has-overdue" : "") });
  card.append(h("div", { class: "card-head" },
    h("div", { class: "card-title" }, "📝 Forms to complete",
      h("span", { class: "badge " + (overdue ? "badge-danger" : "badge-warn") },
        `${forms.length} outstanding`)),
    overdue ? h("span", { class: "badge badge-danger" }, `${overdue} overdue`) : null));

  const body = h("div", { class: "card-body" });
  for (const f of forms) {
    const due = f.due_at
      ? (f.overdue ? "was due " : "due ") + fmtDueDate(f.due_at)
      : "no due date given";
    body.append(h("div", { class: "rem-form" + (f.overdue ? " is-overdue" : "") },
      h("div", { class: "rem-form-title" }, f.title || "(untitled form)"),
      h("div", { class: "rem-form-meta" },
        h("span", { class: "badge " + (f.overdue ? "badge-danger" : "badge-muted") }, due),
        f.url ? h("a", { class: "btn btn-sm", href: f.url, target: "_blank",
                         rel: "noopener noreferrer" }, "Open in Weduc") : null)));
  }
  card.append(body);
  return card;
}

function dayCard(d) {
  const rems = d.reminders || [];
  const on = d.is_school_day;
  const card = h("div", { class: "rem-day" + (d.delta === 0 ? " is-today" : "") +
                                 (on === false ? " is-off" : "") });
  card.append(h("div", { class: "rem-day-head" },
    h("span", { class: "rem-day-label" }, d.label),
    h("span", { class: "rem-day-date" }, d.weekday),
    h("span", { class: "rem-day-state " + (on === true ? "on" : on === false ? "off" : "") },
      on === true ? "school" : on === false ? (d.why || "no school") : (d.why || "")),
  ));
  const body = h("div", { class: "rem-day-body" });
  if (!rems.length) {
    body.append(h("p", { class: "empty" },
      on === false ? "Nothing on." : "Nothing to bring or do."));
  }
  for (const rem of rems) body.append(reminderRow(rem));
  card.append(body);
  return card;
}

function reminderRow(rem) {
  const src = rem.source || {};
  // Where it came from, and — for anything read out of a message — the words it was
  // read from, so a surprising reminder can be checked rather than just believed.
  const from = reminderFrom(src);

  return h("div", { class: "rem-row" + (rem.alert ? " is-alert" : "") },
    h("span", { class: "rem-ico", title: rem.kind }, REMINDER_ICONS[rem.kind] || "📌"),
    h("div", { class: "rem-main" },
      h("div", { class: "rem-title" },
        rem.title || "(untitled)",
        remBadge(rem.badge),
        rem.at ? h("span", { class: "chip rem-at" }, rem.at) : null,
        rem.certainty === "inferred"
          ? h("span", {
              class: "badge badge-warn",
              title: "The message did not name this day outright — Warden worked it out " +
                     "from the wording and the date it was sent. Worth a glance.",
            }, "inferred") : null),
      rem.detail ? h("div", { class: "rem-detail" }, rem.detail) : null,
      from ? h("div", {
        class: "rem-from",
        title: src.quote ? `“${src.quote}”` + (src.sent ? ` — sent ${src.sent}` : "") : "",
      }, from + (src.sent ? ` · ${src.sent}` : "")) : null),
  );
}

/** The status pill beside a reminder title: green "Booked", amber/red "Book by …". */
function remBadge(badge) {
  if (!badge || !badge.text) return null;
  const cls = { ok: "badge-ok", warn: "badge-warn", danger: "badge-danger" }[badge.tone]
    || "badge-muted";
  return h("span", { class: "badge " + cls }, badge.text);
}

function reminderFrom(src) {
  return src.kind === "message" ? `from “${src.title || "a message"}”`
    : src.kind === "calendar_event" ? "school calendar"
    : src.kind === "newsletter" ? `from ${src.title || "the newsletter"}`
    : src.kind === "school_calendar" ? "term dates"
    : src.kind === "parentpay" ? "ParentPay"
    : src.kind === "meals" ? "school meals" : "";
}

function newsletterCard(nl) {
  const card = h("div", { class: "card" + (nl.stale ? " is-stale" : ""),
                          style: "margin-top:18px" });
  // Over a holiday the newest newsletter can be weeks old. Say how old, so it reads as
  // the last one rather than as this week's.
  const age = nl.stale && typeof nl.age_days === "number"
    ? (nl.age_days >= 14 ? `${Math.floor(nl.age_days / 7)} weeks old` : `${nl.age_days} days old`)
    : "";
  card.append(h("div", { class: "card-head" },
    h("div", { class: "card-title" }, "📰 Latest newsletter",
      nl.child_mentioned ? h("span", { class: "badge badge-ok" }, "names Luke") : null,
      age ? h("span", { class: "badge badge-muted" }, age) : null),
    h("span", { class: "hint", style: "color:var(--text-faint);font-size:12px" },
      nl.document || "")));
  const body = h("div", { class: "card-body" });
  if (nl.summary) body.append(h("p", { class: "rem-nl-summary" }, nl.summary));
  for (const c of (nl.about_child || [])) {
    body.append(h("div", { class: "rem-nl-hit is-child" },
      h("div", { class: "rem-nl-quote" }, `“${c.quote || ""}”`),
      h("div", { class: "rem-nl-why" }, c.why || "")));
  }
  for (const c of (nl.about_class || [])) {
    body.append(h("div", { class: "rem-nl-hit" },
      h("div", { class: "rem-nl-why" },
        h("b", {}, (c["class"] || "his class") + ": "), c.detail || "")));
  }
  card.append(body);
  return card;
}

/** How the page was built + a re-read button. The reading costs an API call, so the
 *  button says what it does rather than pretending to be a free refresh. */
function remindersFoot(r) {
  const llm = r.llm || {};
  const bits = [];
  if (llm.used) {
    bits.push(`${llm.kept} reminder${llm.kept === 1 ? "" : "s"} read from messages` +
      (llm.cached ? " (cached)" : "") + (llm.model ? ` · ${llm.model}` : ""));
    if (llm.dropped) bits.push(`${llm.dropped} rejected as unverified`);
  } else if (llm.reason) {
    bits.push("messages not read: " + llm.reason);
  }
  bits.push("term dates and events from the school calendar");

  const btn = h("button", {
    class: "btn btn-sm", type: "button", disabled: !!store.remindersBusy,
    title: "Re-read the school messages instead of reusing the cached reading",
    onclick: async () => {
      store.remindersBusy = true; render();
      try {
        store.reminders = await API.post("/reminders/refresh");
        toast("Reminders re-read", "ok");
      } catch (e) { toast("Re-read failed: " + e.message, "error"); }
      finally { store.remindersBusy = false; render(); }
    },
  }, store.remindersBusy ? "Re-reading…" : "Re-read messages");

  return h("div", { class: "rem-foot" },
    h("span", { class: "rem-foot-text" }, bits.join(" · ")),
    clubLinks(r.clubs), btn);
}

// -- Rules -------------------------------------------------------------------
function renderRules() {
  const wrap = h("div", {});
  wrap.append(composer());

  wrap.append(h("div", { class: "section-head" },
    h("h2", {}, "Rules"),
    h("span", { class: "hint" }, `${store.rules.length} rule${store.rules.length === 1 ? "" : "s"}`)));

  if (!store.rules.length) wrap.append(h("p", { class: "empty" }, "No rules yet — add one above."));
  for (const r of store.rules) wrap.append(ruleCard(r));
  return wrap;
}

function composer() {
  const box = h("div", { class: "composer" });
  box.append(
    h("h2", {}, "Add a rule"),
    h("p", { class: "sub" }, "Write it in plain English — Warden compiles it live below."),
  );

  const preview = h("div", { class: "preview", id: "rulePreview" });
  const saveBtn = h("button", { class: "btn btn-primary", type: "button", disabled: true }, "Save rule");

  const ta = h("textarea", {
    id: "ruleDraft", rows: "2", value: store.draft,
    placeholder: 'e.g. "At 8pm every day, block YouTube for the kids"',
    oninput: (e) => { store.draft = e.target.value; schedulePreview(preview, saveBtn); },
  });

  saveBtn.addEventListener("click", async () => {
    const text = store.draft.trim();
    if (!text) return;
    saveBtn.disabled = true;
    try {
      await API.post("/rules", { text, enabled: store.enableNew });
      store.draft = ""; store.preview = null;
      store.rules = await API.get("/rules");
      toast("Rule saved", "ok"); render();
    } catch (e) { toast("Save failed: " + e.message, "error"); saveBtn.disabled = false; }
  });

  box.append(
    h("div", { class: "field" }, ta),
    preview,
    h("div", { class: "composer-actions" },
      saveBtn,
      h("label", { class: "enable-opt" },
        toggle({ checked: store.enableNew, label: "Enable on save",
          onchange: (v) => { store.enableNew = v; } }),
        "Enabled on save"),
    ),
  );

  // paint any preview we already have (e.g. after a re-render mid-typing)
  paintPreview(preview, saveBtn);
  return box;
}

let previewTimer = null, previewSeq = 0;
/** Debounced live compile of the composer text via POST /rules/preview. */
function schedulePreview(previewEl, saveBtn) {
  clearTimeout(previewTimer);
  const text = store.draft.trim();
  if (!text) { store.preview = null; paintPreview(previewEl, saveBtn); return; }
  previewEl.replaceChildren(h("span", { class: "pv-summary" }, h("span", { class: "spin" }), " compiling…"));
  previewEl.className = "preview";
  const seq = ++previewSeq;
  previewTimer = setTimeout(async () => {
    try {
      const compiled = await API.post("/rules/preview", { text });
      if (seq !== previewSeq) return;           // a newer keystroke won
      store.preview = { compiled };
    } catch (e) {
      if (seq !== previewSeq) return;
      store.preview = { error: e.message || "could not parse" };
    }
    paintPreview(previewEl, saveBtn);
  }, 320);
}

/** Render store.preview into the preview box (no full re-render → keeps focus). */
function paintPreview(previewEl, saveBtn) {
  const p = store.preview;
  if (!p) {
    previewEl.className = "preview";
    previewEl.replaceChildren(h("span", { style: "color:var(--text-faint)" },
      "Compiled preview appears here as you type."));
    if (saveBtn) saveBtn.disabled = true;
    return;
  }
  if (p.error) {
    previewEl.className = "preview err";
    previewEl.replaceChildren(
      h("div", { class: "pv-summary" }, "⚠︎ ", p.error));
    if (saveBtn) saveBtn.disabled = true;
    return;
  }
  const c = p.compiled;
  const ti = triggerInfo(c.trigger);
  previewEl.className = "preview ok";
  previewEl.replaceChildren(
    h("div", { class: "pv-summary" }, ti.icon + " ", c.summary || "(rule)"),
    h("div", { class: "pv-line" },
      h("span", { class: "pv-label" }, "when"),
      h("span", { class: "chip" }, ti.label)),
    h("div", { class: "pv-line" },
      h("span", { class: "pv-label" }, "then"),
      ...(c.actions || []).map((a) => h("span", { class: "chip" }, actionText(a)))),
    (c.warnings && c.warnings.length)
      ? h("ul", { class: "warn-list" }, ...c.warnings.map((w) => h("li", {}, "⚠︎ ", w))) : null,
    typeof c.confidence === "number" && c.confidence < 1
      ? h("div", { class: "pv-line" }, h("span", { class: "badge badge-warn" },
          `confidence ${(c.confidence * 100).toFixed(0)}%`)) : null,
  );
  if (saveBtn) saveBtn.disabled = false;
}

function ruleCard(r) {
  const c = r.compiled;
  const dyn = !!(c && c.dynamic);          // evaluated live by the LLM against source data
  const ti = dyn ? { icon: "🧠", label: "evaluated live by AI against source data" }
                 : triggerInfo(c && c.trigger);
  const card = h("div", { class: "rule" + (r.enabled ? "" : " is-off") });

  card.append(h("div", { class: "trig", title: ti.label }, ti.icon));

  const main = h("div", { class: "rule-main" });
  if (store.editing === r.id) {
    main.append(ruleEditor(r));
    card.append(main);
    return card;
  }
  main.append(h("div", { class: "rule-text" }, r.text));
  if (dyn) main.append(h("div", { class: "rule-sum" }, "🧠 evaluated live against Luke's real progress each cycle"));
  else if (c) main.append(h("div", { class: "rule-sum" }, c.summary || ti.label));
  if (r.compile_error) main.append(h("div", { class: "rule-err" }, "compile error: " + r.compile_error));
  // for dynamic rules the LLM's latest decision (reason) is the most useful thing to see
  if (dyn && r.last_result)
    main.append(h("div", { class: "rule-sum", style: "opacity:.72;font-style:italic" }, "last: " + r.last_result));

  const meta = h("div", { class: "rule-meta" });
  if (dyn) meta.append(h("span", { class: "badge badge-accent" }, "AI-evaluated"));
  else if (c && c.actions) for (const a of c.actions) meta.append(h("span", { class: "chip" }, actionText(a)));
  meta.append(h("span", { class: "badge badge-muted" }, r.source || "user"));
  if (r.last_fired_at)
    meta.append(h("span", { class: "badge" }, `${dyn ? "evaluated" : "fired"} ${timeAgo(r.last_fired_at)}`));
  // a dynamic rule picks its own next wake-up from its text — show when, and why
  if (dyn && r.next_check_at) {
    meta.append(h("span", {
      class: "badge badge-accent",
      title: r.next_check_reason || "chosen by the AI from this rule's wording",
    }, `next check ${whenAbs(r.next_check_at)}`));
  }
  if (c && c.warnings && !dyn) for (const w of c.warnings)
    meta.append(h("span", { class: "badge badge-warn" }, w));
  main.append(meta);
  if (dyn && r.next_check_reason)
    main.append(h("div", { class: "rule-sum", style: "opacity:.6" },
      "⏱ " + r.next_check_reason));
  card.append(main);

  // controls: enabled toggle, run-now, delete
  const ctl = h("div", { class: "rule-ctl" });
  ctl.append(toggle({
    checked: r.enabled, label: `Enable rule: ${r.text}`,
    onchange: async () => {
      try { const upd = await API.post(`/rules/${r.id}/toggle`); mergeRule(upd); render(); }
      catch (e) { toast("Toggle failed: " + e.message, "error"); }
    },
  }));
  ctl.append(h("div", { class: "rule-btns" },
    h("button", { class: "btn btn-sm", type: "button",
      onclick: () => editRule(r) }, icon("edit", 13), "Edit"),
    h("button", { class: "btn btn-sm", type: "button", disabled: !c,
      onclick: () => runRule(r) }, (c && c.dynamic) ? "Evaluate now" : "▶ Run now"),
    h("button", { class: "btn btn-sm btn-danger", type: "button",
      onclick: () => deleteRule(r) }, "Delete"),
  ));
  card.append(ctl);
  return card;
}

/** Absolute-ish rendering for a future instant: "today 16:00" / "Mon 09:00". */
function whenAbs(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const now = new Date();
  const hhmm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return `today ${hhmm}`;
  const tmr = new Date(now); tmr.setDate(now.getDate() + 1);
  if (d.toDateString() === tmr.toDateString()) return `tomorrow ${hhmm}`;
  return `${d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })} ${hhmm}`;
}

/** Inline editor for a rule's English. Saving recompiles and clears its schedule. */
function editRule(r) {
  if (store.editing === r.id) { store.editing = null; render(); return; }
  store.editing = r.id;
  store.editDraft = r.text;
  render();
}

function ruleEditor(r) {
  const box = h("div", { class: "rule-editor" });
  const ta = h("textarea", {
    class: "rule-edit-ta", rows: "5", spellcheck: "false",
    "aria-label": "Rule text",
    oninput: (e) => { store.editDraft = e.target.value; },
  });
  ta.value = store.editDraft != null ? store.editDraft : r.text;
  box.append(ta);
  box.append(h("div", { class: "rule-edit-hint" },
    "Describe both what to do and when to check — a dynamic rule reads its own " +
    "cadence from this text and schedules itself."));
  box.append(h("div", { class: "rule-btns" },
    h("button", {
      class: "btn btn-sm btn-primary", type: "button",
      onclick: async () => {
        const text = (store.editDraft || "").trim();
        if (!text) { toast("Rule text can't be empty", "error"); return; }
        try {
          const upd = await API.put(`/rules/${r.id}`, { text });
          mergeRule(upd);
          store.editing = null; store.editDraft = null;
          toast("Rule updated — it will be re-judged on its next check", "ok");
          store.rules = await API.get("/rules");
          render();
        } catch (e) { toast("Save failed: " + e.message, "error"); }
      },
    }, "Save"),
    h("button", {
      class: "btn btn-sm btn-ghost", type: "button",
      onclick: () => { store.editing = null; store.editDraft = null; render(); },
    }, "Cancel"),
  ));
  return box;
}

function mergeRule(upd) {
  if (!upd || !upd.id) return;
  const i = store.rules.findIndex((r) => r.id === upd.id);
  if (i >= 0) store.rules[i] = upd; else store.rules.push(upd);
}
async function runRule(r) {
  const dyn = !!(r.compiled && r.compiled.dynamic);
  try {
    if (dyn) toast("Evaluating with AI…", "ok");
    const res = await API.post(`/rules/${r.id}/run`, { dry: false });
    if (dyn) {
      // evaluator response: {condition_met, reason/detail, applied:[...]}
      const applied = (res && res.applied) ? res.applied : [];
      const msg = (res && (res.reason || res.detail)) || "evaluated";
      toast((applied.length ? `Applied ${applied.join(", ")} — ` : "No change — ") + msg,
        (res && res.ok !== false) ? "ok" : "error");
    } else {
      const acts = (res && res.actions) ? res.actions.length : 0;
      toast(`${(res && res.ok) ? "Ran" : "Ran (issue)"}: ${r.text} — ${acts} action${acts === 1 ? "" : "s"}`,
        (res && res.ok) ? "ok" : "error");
    }
    store.rules = await API.get("/rules");     // refresh last decision/fired
    render();
  } catch (e) { toast("Run failed: " + e.message, "error"); }
}
async function deleteRule(r) {
  if (!confirm(`Delete rule?\n\n${r.text}`)) return;
  try {
    await API.del(`/rules/${r.id}`);
    store.rules = store.rules.filter((x) => x.id !== r.id);
    toast("Rule deleted", "ok"); render();
  } catch (e) { toast("Delete failed: " + e.message, "error"); }
}

// -- Sources -----------------------------------------------------------------
function renderSources() {
  const wrap = h("div", {});

  wrap.append(h("div", { class: "section-head" },
    h("h2", {}, "Sources"),
    h("span", { class: "hint" }, "pluggable scrapers · run against fixtures offline")));

  if (!store.sources.length) wrap.append(h("p", { class: "empty" }, "No sources configured."));
  const grid = h("div", { class: "grid grid-cards" });
  for (const s of store.sources) grid.append(sourceCard(s));
  wrap.append(grid);

  wrap.append(signalsPanel());
  return wrap;
}

/** Weekly workload target — Atom publishes its own plan for the week and that is the
 *  number shown here; typing over it makes an override for this week only, which the
 *  "Use Atom's N" button takes back off. Today's share is derived from whichever wins. */
function weeklyTargetRow(key, t) {
  const plan = t.published || null;
  const planText = plan
    ? `Atom's plan for w/c ${t.week_start}: ${plan.islands} islands`
      + (plan.mock_tests ? ` + ${plan.mock_tests} mock test${plan.mock_tests > 1 ? "s" : ""}` : "")
      + ` = ${plan.total}`
    : "";
  const input = h("input", {
    type: "number", min: "0", step: "1", class: "target-input",
    "aria-label": "Weekly island target",
    title: planText,
  });
  input.value = t.islands != null ? t.islands : "";

  const save = h("button", {
    class: "btn btn-sm btn-primary", type: "button",
    onclick: async () => {
      const n = parseInt(input.value, 10);
      if (isNaN(n) || n < 0) { toast("Enter a whole number of islands", "error"); return; }
      save.disabled = true;
      try {
        const res = await API.put(`/sources/${key}/target`, { islands: n });
        store.targets[key] = res;
        // the hub re-collects in the background; the recomputed daily share arrives
        // on the islands signals (applyTargetSignal) a second or two later
        toast(`Weekly target set to ${n} for w/c ${res.week_start} — working out today's share…`, "ok");
        render();
      } catch (e) {
        toast("Target: " + e.message, "error");
      } finally { save.disabled = false; }
    },
  }, "Save");

  // Going back to Atom's number is a click, not a re-typing — the override exists for
  // the exceptional week (illness, half term), not as the way to keep the number current.
  const reset = (t.origin === "override" && plan)
    ? h("button", {
        class: "btn btn-sm btn-ghost", type: "button", title: planText,
        onclick: async () => {
          reset.disabled = true;
          try {
            const res = await API.del(`/sources/${key}/target`);
            store.targets[key] = res;
            toast(`Back to Atom's plan for w/c ${res.week_start}: ${res.islands}`, "ok");
            render();
          } catch (e) {
            toast("Target: " + e.message, "error");
          } finally { reset.disabled = false; }
        },
      }, `Use Atom's ${plan.total}`)
    : null;

  const today = t.today || {};
  const due = today.islands_due_today;
  const originText = t.origin === "override" ? " · set by you"
    : t.origin === "atom-published" ? " · from Atom"
    : " · fallback (Atom's plan unread)";

  return h("div", { class: "target-row" },
    h("span", { class: "target-label" }, "Weekly islands"),
    input,
    h("span", { class: "target-meta", title: planText }, `w/c ${t.week_start}` + originText),
    // What the week's number asks for TODAY — the whole point of setting it here.
    due != null
      ? h("span", {
          class: "target-due" + ((today.completed_today || 0) >= due ? " is-met" : ""),
          title: today.explain || "",
        }, `today: ${today.completed_today || 0}/${due}`)
      : null,
    save,
    reset,
  );
}

function sourceCard(s) {
  const key = s.key;
  const card = h("div", { class: "card" });
  card.append(h("div", { class: "card-head" },
    h("div", { class: "card-title" }, s.display_name || key,
      s.enabled ? h("span", { class: "badge badge-ok" }, "enabled")
                : h("span", { class: "badge badge-muted" }, "disabled")),
    h("div", { style: "display:flex;gap:8px" },
      h("button", { class: "btn btn-sm btn-primary", type: "button",
        title: "Fetch from the live site (falls back to fixtures if no session)",
        onclick: (e) => runSource(key, e.target, true) }, "Run live"),
      h("button", { class: "btn btn-sm", type: "button",
        title: "Run against bundled fixtures (offline demo)",
        onclick: (e) => runSource(key, e.target, false) }, "Fixtures"),
    ),
  ));

  const body = h("div", { class: "card-body" });
  body.append(h("div", { class: "source-head-meta" },
    h("span", { class: "chip" }, "adapter: " + (s.adapter || key)),
    s.subject ? h("span", { class: "chip" }, "subject: " + s.subject) : null,
    s.schedule ? h("span", { class: "chip", title: s.schedule }, "⏰ " + cronHint(s.schedule)) : null,
  ));

  // Loud banner when the stored data is the offline sample rather than the real thing —
  // rules are held back in that state, and silence here is what let it go unnoticed.
  if (s.live_data === false) {
    body.append(h("div", { class: "source-warn" },
      icon("shield", 15),
      h("div", {},
        h("strong", {}, "Not live data — rules are paused for this source. "),
        h("span", {}, s.data_note || "showing the offline sample"))));
  }

  const t = store.targets && store.targets[key];
  if (t) body.append(weeklyTargetRow(key, t));

  // last run summary
  const lr = s.last_run;
  body.append(h("div", { class: "source-runline" },
    lr
      ? [
          lr.ok ? h("span", { class: "badge badge-ok" }, "ok") : h("span", { class: "badge badge-danger" }, "failed"),
          h("span", {}, `${lr.item_count ?? 0} items · ${lr.signal_count ?? 0} signals`),
          h("span", { style: "color:var(--text-faint)" }, "· " + timeAgo(lr.finished_at || lr.started_at)),
          lr.detail ? h("span", { style: "color:var(--text-faint)" }, "· " + lr.detail) : null,
        ]
      : h("span", { class: "empty" }, "never run"),
    s.next_run ? h("span", { style: "color:var(--text-faint);margin-left:auto" }, "next " + fmtDateTime(s.next_run)) : null,
  ));

  // recent items (loaded async, painted into this container)
  const itemsBox = h("div", { id: `items-${key}` }, h("p", { class: "empty" }, "loading items…"));
  body.append(itemsBox);
  loadItems(key, itemsBox);

  card.append(body);
  return card;
}

async function loadItems(key, box) {
  try {
    const items = await API.get(`/sources/${key}/items?limit=6`);
    if (!items.length) { box.replaceChildren(h("p", { class: "empty" }, "no items yet — run the source")); return; }
    box.replaceChildren(h("ul", { class: "items-list" }, ...items.map(itemRow)));
  } catch (e) { box.replaceChildren(h("p", { class: "empty" }, "items unavailable: " + e.message)); }
}
function itemRow(it) {
  const when = it.due_at ? "due " + fmtDateTime(it.due_at)
    : it.occurred_at ? fmtDateTime(it.occurred_at) : "";
  return h("li", { class: "item" },
    h("div", { class: "it-top" },
      h("span", { class: "it-title" }, it.title || "(untitled)"),
      h("span", { class: "badge badge-muted" }, it.kind || "other")),
    it.body_text ? h("div", { class: "it-body" }, it.body_text.slice(0, 160)) : null,
    h("div", { class: "it-tags" },
      when ? h("span", { class: "chip" }, when) : null,
      ...(it.audience_tags || []).slice(0, 4).map((t) => h("span", { class: "badge badge-accent" }, t))),
  );
}

/** This week's workload target, and today's share of it, per source (absent for
 *  sources that have no such concept). */
async function loadTargets() {
  const targets = {};
  await Promise.all((store.sources || []).map(async (s) => {
    try {
      const t = await API.get(`/sources/${s.key}/target`);
      if (t && t.islands != null) targets[s.key] = t;
    } catch { /* source has no target concept */ }
  }));
  store.targets = targets;
}

async function runSource(key, btn, live) {
  const label = btn && btn.textContent; if (btn) { btn.disabled = true; btn.textContent = "Running…"; }
  try {
    const run = await API.post(`/sources/${key}/run?live=${live ? "true" : "false"}`);
    toast(`${key}: ${run && run.ok ? "ran" : "failed"} — ${run ? run.item_count : 0} items, ${run ? run.signal_count : 0} signals`,
      (run && run.ok) ? "ok" : "error");
    store.sources = await API.get("/sources");
    upsertSignals(await API.get("/signals"));
    await loadTargets();
    render();
  } catch (e) {
    toast("Run failed: " + e.message, "error");
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

/** Signals panel: every known signal (config vocab ∪ live) + a simulate control. */
function signalsPanel() {
  const card = h("div", { class: "card", style: "margin-top:22px" });
  card.append(h("div", { class: "card-head" },
    h("div", { class: "card-title" }, "⚡ Signals"),
    h("span", { class: "hint", style: "color:var(--text-faint);font-size:12px" },
      "simulate a value to watch a rule fire")));

  const body = h("div", { class: "card-body" });
  const sigs = knownSignals();
  if (!sigs.length) body.append(h("p", { class: "empty" }, "No signals defined."));
  for (const sig of sigs) body.append(signalRow(sig));
  card.append(body);
  return card;
}

/** Union of config.signals (metadata) and live bus values, keyed by key. */
function knownSignals() {
  const byKey = new Map();
  const defs = (store.config && Array.isArray(store.config.signals)) ? store.config.signals : [];
  for (const d of defs)
    byKey.set(d.key, { key: d.key, type: d.type || "bool", describe: d.describe || "",
                       subject: d.subject, source: d.source, value: undefined, at: null });
  for (const s of store.signals) {
    const cur = byKey.get(s.key) || { key: s.key, type: s.type || "bool", describe: "" };
    byKey.set(s.key, { ...cur, value: s.value, at: s.at, type: cur.type || s.type || "bool" });
  }
  return [...byKey.values()];
}

function signalRow(sig) {
  const row = h("div", { class: "signal", dataset: { sigrow: sig.key } });
  row.append(h("div", { class: "sig-main" },
    h("div", { class: "sig-key" }, sig.key),
    sig.describe ? h("div", { class: "sig-desc" }, sig.describe) : null));

  const valBox = h("div", { class: "sig-val", dataset: { sigval: sig.key } }, signalValueNode(sig));

  // simulate control depends on signal type
  let sim;
  if (sig.type === "number") {
    const input = h("input", { type: "number", value: (sig.value ?? "") === "" ? "" : String(sig.value), "aria-label": sig.key + " value" });
    sim = h("div", { class: "sim" }, input,
      h("button", { class: "btn btn-sm", type: "button",
        onclick: () => emitSignal(sig.key, Number(input.value || 0)) }, "Emit"));
  } else if (sig.type === "string") {
    const input = h("input", { type: "text", value: sig.value == null ? "" : String(sig.value), "aria-label": sig.key + " value" });
    sim = h("div", { class: "sim" }, input,
      h("button", { class: "btn btn-sm", type: "button",
        onclick: () => emitSignal(sig.key, input.value) }, "Emit"));
  } else { // bool
    sim = h("div", { class: "sim" },
      toggle({ checked: sig.value === true, label: "Simulate " + sig.key,
        onchange: (v) => emitSignal(sig.key, v) }));
  }

  row.append(h("div", { style: "display:flex;align-items:center;gap:14px" }, valBox, sim));
  return row;
}

function signalValueNode(sig) {
  if (sig.value === undefined || sig.value === null) return h("span", { class: "badge badge-muted" }, "—");
  if (sig.value === true) return h("span", { class: "badge badge-ok" }, "true");
  if (sig.value === false) return h("span", { class: "badge badge-muted" }, "false");
  if (typeof sig.value === "number") return h("span", { class: "val-num" }, String(sig.value));
  return h("span", { class: "chip" }, String(sig.value));
}

/** Update only a signal's value badge in place (keeps simulate inputs intact). */
function patchSignalValue(sig) {
  const box = document.querySelector(`[data-sigval="${cssEscape(sig.key)}"]`);
  if (box) box.replaceChildren(signalValueNode({ ...sig, type: sig.type || "bool" }));
}
function cssEscape(s) { return String(s).replace(/["\\]/g, "\\$&"); }

async function emitSignal(key, value) {
  try {
    const sig = await API.post("/signals/emit", { key, value });
    if (sig) applySignal(sig);
    toast(`Emitted ${key} = ${value}`, "ok");
  } catch (e) { toast("Emit failed: " + e.message, "error"); }
}

// -- Devices (per-device overrides) ------------------------------------------

/** "1h 20m left" / "expired" — how long an override has to run. */
function overrideLeft(o) {
  if (!o) return "";
  if (!o.expires_at) return "until cancelled";
  const ms = new Date(o.expires_at).getTime() - Date.now();
  if (isNaN(ms)) return "";
  if (ms <= 0) return "expiring…";
  const mins = Math.round(ms / 60000);
  if (mins < 60) return `${mins}m left`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m left`;
}

async function doUnblock(name, duration) {
  store.busyDevice = name; render();
  try {
    await API.post("/devices/unblock", { client: name, duration });
    toast(`${name} unblocked (${duration === "forever" ? "until cancelled" : duration})`, "ok");
  } catch (e) { toast(`Unblock failed: ${e.message}`, "error"); }
  finally { store.busyDevice = null; await loadView("devices"); }
}

async function doCancelOverride(name) {
  store.busyDevice = name; render();
  try {
    await API.del("/devices/override", { client: name, duration: "2h" });
    toast(`${name} re-blocked`, "ok");
  } catch (e) { toast(`Could not re-block: ${e.message}`, "error"); }
  finally { store.busyDevice = null; await loadView("devices"); }
}

function renderDevices() {
  const wrap = h("div", {});
  wrap.append(h("div", { class: "section-head" },
    h("h2", {}, "Devices"),
    h("span", { class: "hint" }, "everything AdGuard knows about — unblock one without touching its group")));

  const devices = store.devices || [];
  if (!devices.length) return wrap.append(loading()), wrap;

  const managed = devices.filter((d) => d.group);
  const others = devices.filter((d) => !d.group);

  const list = h("div", { class: "device-list" });
  for (const d of managed) list.append(deviceRow(d));
  wrap.append(list);

  if (others.length) {
    wrap.append(h("div", { class: "section-head section-head-sub" },
      h("h3", {}, "Not managed by Warden"),
      h("span", { class: "hint" }, "no group matched — tag it in AdGuard to bring it under control")));
    const rest = h("div", { class: "device-list" });
    for (const d of others) rest.append(deviceRow(d));
    wrap.append(rest);
  }
  return wrap;
}

function deviceRow(d) {
  const busy = store.busyDevice === d.name;
  const ov = d.override;
  const blockedCount = (d.managed_blocked || []).length;
  const total = d.managed_total || 0;

  // status line: an active override wins, else how much of the group is applied
  let statusCls = "badge-muted", statusText = "not managed";
  if (ov) { statusCls = "badge-warn"; statusText = "unblocked · " + overrideLeft(ov); }
  else if (d.group && total && blockedCount === total) { statusCls = "badge-ok"; statusText = "all blocked"; }
  else if (d.group && blockedCount) { statusCls = "badge-warn"; statusText = `${blockedCount}/${total} blocked`; }
  else if (d.group) { statusCls = "badge-muted"; statusText = "nothing blocked"; }

  const meta = [];
  if (d.ids && d.ids.length) meta.push(d.ids.join(", "));
  if (d.group) meta.push(d.discovered ? `${d.group} (by tag)` : d.group);
  if (d.tags && d.tags.length) meta.push(d.tags.join(" · "));

  const actions = h("div", { class: "device-actions" });
  if (ov) {
    actions.append(h("button", {
      class: "btn btn-sm btn-danger", type: "button", disabled: busy,
      onclick: () => doCancelOverride(d.name),
    }, busy ? "…" : "Re-block now"));
  } else if (d.group) {
    for (const dur of (store.durations || [])) {
      actions.append(h("button", {
        class: "btn btn-sm", type: "button", disabled: busy,
        title: dur === "forever" ? "Unblock until cancelled" : `Unblock for ${dur}`,
        onclick: () => doUnblock(d.name, dur),
      }, dur === "forever" ? "Forever" : dur));
    }
  } else {
    actions.append(h("span", { class: "hint" }, "—"));
  }

  return h("div", { class: "device-row" + (ov ? " is-override" : "") },
    h("div", { class: "device-main" },
      h("div", { class: "device-name" }, d.name,
        d.use_global_blocked_services
          ? h("span", { class: "badge badge-muted", title:
              "AdGuard is ignoring this device's own service list and using the global one. " +
              "Warden clears this flag the first time it writes a block." }, "global list")
          : null),
      h("div", { class: "device-meta" }, meta.join("  ·  ") || "—")),
    h("span", { class: "badge " + statusCls }, statusText),
    actions,
  );
}

// -- Activity ----------------------------------------------------------------
function renderActivity() {
  const wrap = h("div", {});
  const llmCount = store.audit.filter(isLlmRow).length;
  wrap.append(h("div", { class: "section-head" },
    h("h2", {}, "Activity"),
    h("span", { class: "hint" }, "live audit feed"),
    // Every model call lands in this feed. They are the noisiest lines in it, so they
    // can be hidden — but they are on by default: seeing what Warden asked the model,
    // and what it answered, is the point of recording them.
    h("label", { class: "audit-filter", title:
        "Show the model calls Warden made — open one to read the exact prompt and reply" },
      h("input", {
        type: "checkbox", checked: store.showLlm,
        onchange: (e) => { store.showLlm = e.target.checked; render(); },
      }),
      ` LLM calls${llmCount ? ` (${llmCount})` : ""}`)));
  const rows = store.audit.filter((e) => store.showLlm || !isLlmRow(e));
  const list = h("div", { class: "audit-list", id: "auditList" });
  if (!rows.length) list.append(h("p", { class: "empty" }, "No activity recorded yet."));
  for (const e of rows) list.append(auditRow(e));
  wrap.append(list);
  return wrap;
}
const isLlmRow = (e) => e.action === "llm_call";
function auditRow(e) {
  const actorKind = (e.actor || "").split(":")[0];
  const badgeCls = actorKind === "rule" ? "badge-accent"
    : actorKind === "source" ? "badge-warn"
    : actorKind === "user" ? "badge-ok" : "badge-muted";
  // a whole-house protection kill must jump out of the feed, not blend in
  const protOff = e.action === "set_protection" && /^DISABLED/.test(e.detail || "");
  // Anything the model was asked keeps its transcript; the row carries the id of it.
  const llmId = /^llm:(\d+)$/.exec(e.ref || "");
  return h("div", { class: "audit-row" + (e.ok ? "" : " err") + (protOff ? " prot-off" : "")
                    + (isLlmRow(e) ? " is-llm" : "") },
    h("span", { class: "audit-time" }, fmtTime(e.at)),
    h("span", { class: "audit-actor" }, h("span", { class: "badge " + badgeCls }, e.actor || "system")),
    h("span", { class: "audit-action" },
      h("b", {}, e.action || "—"), " ",
      e.target ? h("span", { class: "a-target" }, e.target) : null,
      e.detail ? h("span", { class: "a-target" }, " — " + e.detail) : null),
    llmId
      ? h("button", {
          class: "btn btn-sm btn-ghost audit-llm-btn", type: "button",
          title: "Show exactly what was sent to the model, and exactly what came back",
          onclick: () => openLlmCall(Number(llmId[1])),
        }, "prompt ⤢")
      : h("span", { class: "audit-llm-slot" }),
    h("span", { class: "audit-ok " + (e.ok ? "y" : "n") }, e.ok ? "✓" : "✕"),
  );
}

/* ── the transcript popout ───────────────────────────────────────────────── */

/** Open one LLM exchange in full: system prompt, message sent, reply received.
 *  Fetched on demand — the feed carries only the id, never the text. */
async function openLlmCall(id) {
  const wrap = h("div", { class: "llm-overlay" });
  const body = h("div", { class: "llm-body" }, h("p", { class: "empty" }, "Loading…"));
  const close = () => {
    document.removeEventListener("keydown", onKey);
    wrap.remove();
    store.llmOpen = null;
  };
  const onKey = (ev) => { if (ev.key === "Escape") close(); };
  const card = h("div", { class: "llm-card", role: "dialog", "aria-modal": "true",
                          "aria-label": "LLM call" },
    h("div", { class: "llm-head" },
      h("h3", { class: "llm-title" }, `LLM call #${id}`),
      h("button", { class: "btn btn-sm", type: "button", onclick: close }, "Close")),
    body);
  wrap.append(card);
  wrap.addEventListener("click", (ev) => { if (ev.target === wrap) close(); });
  document.addEventListener("keydown", onKey);
  document.body.append(wrap);
  store.llmOpen = id;

  try {
    const c = await API.get(`/llm/${id}`);
    const meta = [
      c.purpose, c.model, c.backend,
      (c.input_tokens != null || c.output_tokens != null)
        ? `${c.input_tokens || 0} in / ${c.output_tokens || 0} out` : null,
      c.ms ? `${(c.ms / 1000).toFixed(1)}s` : null,
      fmtDateTime(c.at),
    ].filter(Boolean).join("  ·  ");
    // replaceChildren has no opinion about null — it stringifies it — so build the
    // list first and drop the sections this call doesn't have.
    body.replaceChildren(...[
      h("div", { class: "llm-meta" }, meta),
      c.error ? h("div", { class: "llm-error" }, c.error) : null,
      c.system ? llmSection("System prompt", c.system) : null,
      llmSection("Sent", c.prompt),
      llmSection(c.ok ? "Received" : "Received (before it failed)", c.response),
    ].filter(Boolean));
  } catch (err) {
    body.replaceChildren(h("p", { class: "empty" },
      "Couldn't load it: " + err.message
      + " — the log keeps the most recent few hundred calls."));
  }
}

function llmSection(title, text) {
  const value = text || "";
  const pre = h("pre", { class: "llm-pre" }, value || "(empty)");
  const copy = h("button", {
    class: "btn btn-sm btn-ghost", type: "button",
    onclick: async () => {
      try {
        await navigator.clipboard.writeText(value);
        copy.textContent = "copied";
        setTimeout(() => { copy.textContent = "copy"; }, 1200);
      } catch { toast("Couldn't copy — select the text instead", "error"); }
    },
  }, "copy");
  return h("div", { class: "llm-section" },
    h("div", { class: "llm-section-head" },
      h("span", { class: "llm-section-title" }, title),
      h("span", { class: "hint" }, `${value.length} chars`), copy),
    pre);
}

// -- About / Settings --------------------------------------------------------
function renderAbout() {
  const ag = (store.status) || (store.snapshot && store.snapshot.adguard) || {};
  const cfg = store.config || {};
  // Rule-compiler backend, resolved server-side (API key / Claude CLI / parser).
  const comp = cfg.compiler || {};
  const backendLabel = {
    "anthropic-api": `Anthropic API · ${comp.model || "?"}`,
    "claude-cli": `Claude Code CLI · ${comp.model || "?"} (no API key)`,
    "builtin-parser": "built-in deterministic parser (no API key or CLI needed)",
  }[comp.backend] || "built-in deterministic parser";
  const authOn = !!(cfg.auth && cfg.auth.enabled);
  const wrap = h("div", {});

  wrap.append(h("div", { class: "section-head" }, h("h2", {}, "Settings & About")));

  wrap.append(h("div", { class: "card" },
    h("div", { class: "card-body", style: "padding-top:16px" },
      h("dl", { class: "about-grid" },
        h("dt", {}, "AdGuard URL"),   h("dd", {}, ag.url || "—"),
        h("dt", {}, "AdGuard mode"),  h("dd", {}, `${ag.mode || "?"}${ag.reachable ? " · reachable" : " · unreachable"}${ag.version ? " · v" + ag.version : ""}`),
        h("dt", {}, "Protection"),    h("dd", {}, ag.protection_enabled === false ? "off" : "on"),
        h("dt", {}, "Rule compiler"), h("dd", {}, backendLabel),
        h("dt", {}, "Live link"),     h("dd", {}, store.connected ? "WebSocket connected" : "polling /api/state (WS down)"),
        h("dt", {}, "Session"),       h("dd", {}, authOn
          ? h("a", { href: "/logout", style: "color:var(--danger,#ff6b6b);font-weight:600;text-decoration:none" }, "Log out")
          : "no login configured (open access)"),
      ),
      h("p", { style: "margin-top:16px;color:var(--text-muted);font-size:13px" },
        "Warden gives a web UI over AdGuard Home, runs a plain-English rules engine, " +
        "and ingests signals from pluggable website scrapers. All secrets " +
        "(AdGuard credentials, source logins, Anthropic key) live in ",
        h("code", {}, ".env"), " on the server and are never sent to the browser."),
    ),
  ));

  // quick reference of configured sources + subjects, if the summary carries them
  if (Array.isArray(cfg.subjects) && cfg.subjects.length) {
    wrap.append(h("div", { class: "card", style: "margin-top:16px" },
      h("div", { class: "card-head" }, h("div", { class: "card-title" }, "People")),
      h("div", { class: "card-body" },
        h("div", { class: "clients-row", style: "margin-top:8px" },
          ...cfg.subjects.map((s) => h("span", { class: "chip" }, (s.name || s.id) + (s.id ? ` · ${s.id}` : "")))))));
  }
  return wrap;
}

/* ── main render ────────────────────────────────────────────────────────── */
const VIEWS = {
  dashboard: renderDashboard,
  reminders: renderReminders,
  devices: renderDevices,
  rules: renderRules,
  sources: renderSources,
  activity: renderActivity,
  about: renderAbout,
};

function loading() { return h("p", { class: "loading" }, h("span", { class: "spin" }), " loading…"); }

function render() {
  // reflect active tab
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("is-active", t.dataset.view === store.view));
  const mount = $("#view");
  const build = VIEWS[store.view] || renderDashboard;
  mount.replaceChildren(build());
  updateChrome();
}

/* fetch the data a view needs, then re-render */
async function loadView(view) {
  try {
    if (view === "dashboard") {
      const st = await API.get("/state");
      if (st && st.holds !== undefined) store.holds = st.holds || {};
      store.snapshot = st;
      // separate call: each managed service is probed over SSH, so it's slower and
      // must not hold up the switches rendering
      API.get("/hosts/services")
        .then((hs) => { store.hostServices = hs || []; if (store.view === "dashboard") render(); })
        .catch(() => { store.hostServices = []; });
    }
    else if (view === "reminders") { store.reminders = await API.get("/reminders"); }
    else if (view === "rules") { store.rules = await API.get("/rules"); }
    else if (view === "sources") {
      const [srcs, sigs] = await Promise.all([API.get("/sources"), API.get("/signals")]);
      store.sources = srcs || []; upsertSignals(sigs || []);
      await loadTargets();
    }
    else if (view === "devices") {
      const d = await API.get("/devices");
      store.devices = (d && d.devices) || [];
      if (d && d.durations) store.durations = d.durations;
    }
    else if (view === "activity") { store.audit = await API.get("/audit?limit=100") || []; }
    else if (view === "about") { store.status = await API.get("/status"); }
    render();
  } catch (e) { toast(`Could not load ${view}: ${e.message}`, "error"); }
}

function setView(view) {
  if (!VIEWS[view]) view = "dashboard";
  store.view = view;
  render();          // paint immediately from cached store (may be partial)
  loadView(view);    // then refresh and re-render
}

/* ── 7. boot ────────────────────────────────────────────────────────────── */
function wireTabs() {
  $("#tabs").addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (btn && btn.dataset.view) setView(btn.dataset.view);
  });
}

async function boot() {
  wireTabs();
  // config carries the signal vocabulary + subjects the UI renders dynamically
  try { store.config = await API.get("/config"); } catch {}
  live.connect();               // pushes an initial state + signals snapshot
  setView("dashboard");
}

if (document.readyState === "loading")
  document.addEventListener("DOMContentLoaded", boot);
else boot();

})();
