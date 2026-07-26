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
      const msg = (data && data.detail) || (typeof data === "string" && data) || res.statusText;
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
  draft: "",          // add-rule composer text (survives re-render)
  preview: null,      // { compiled } | { error }
  enableNew: true,    // "enabled" for new rules
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
      case "state":   applySnapshot(msg.snapshot); break;
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
  store.snapshot = snap;
  updateChrome();
  if (store.view === "dashboard") render();   // dashboard has no text inputs to lose
}
function applySignal(sig) {
  if (!sig) return;
  upsertSignals([sig]);
  if (store.view === "sources") patchSignalValue(sig);  // keep simulate inputs intact
}
function applyAudit(entry) {
  if (!entry) return;
  store.audit.unshift(entry);
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
          try {
            const st = await API.post("/actions/protection", { enabled: on });
            if (st) { store.snapshot = { ...store.snapshot, adguard: st }; updateChrome(); render(); }
            toast(`Protection ${on ? "enabled" : "disabled"}`, "ok");
          } catch (e) { toast("Protection: " + e.message, "error"); render(); }
        },
      }),
    ),
  ));

  // one card per group
  const groups = snap.groups || [];
  if (!groups.length) wrap.append(h("p", { class: "empty" }, "No managed groups configured."));
  const grid = h("div", { class: "grid grid-cards" });
  for (const g of groups) grid.append(groupCard(g));
  wrap.append(grid);
  return wrap;
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
    const on = svc.state === "allowed";
    body.append(h("div", { class: "svc-row" },
      h("span", { class: "svc-ico" }, icon(svc.icon)),
      h("span", { class: "svc-name" }, svc.name || svc.id),
      h("span", { class: "svc-state " + (on ? "on" : "off") }, on ? "allowed" : "blocked"),
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
  if (c && c.warnings && !dyn) for (const w of c.warnings)
    meta.append(h("span", { class: "badge badge-warn" }, w));
  main.append(meta);
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
    h("button", { class: "btn btn-sm", type: "button", disabled: !c,
      onclick: () => runRule(r) }, (c && c.dynamic) ? "Evaluate now" : "▶ Run now"),
    h("button", { class: "btn btn-sm btn-danger", type: "button",
      onclick: () => deleteRule(r) }, "Delete"),
  ));
  card.append(ctl);
  return card;
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

async function runSource(key, btn, live) {
  const label = btn && btn.textContent; if (btn) { btn.disabled = true; btn.textContent = "Running…"; }
  try {
    const run = await API.post(`/sources/${key}/run?live=${live ? "true" : "false"}`);
    toast(`${key}: ${run && run.ok ? "ran" : "failed"} — ${run ? run.item_count : 0} items, ${run ? run.signal_count : 0} signals`,
      (run && run.ok) ? "ok" : "error");
    store.sources = await API.get("/sources");
    upsertSignals(await API.get("/signals"));
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

// -- Activity ----------------------------------------------------------------
function renderActivity() {
  const wrap = h("div", {});
  wrap.append(h("div", { class: "section-head" },
    h("h2", {}, "Activity"),
    h("span", { class: "hint" }, "live audit feed")));
  const list = h("div", { class: "audit-list", id: "auditList" });
  if (!store.audit.length) list.append(h("p", { class: "empty" }, "No activity recorded yet."));
  for (const e of store.audit) list.append(auditRow(e));
  wrap.append(list);
  return wrap;
}
function auditRow(e) {
  const actorKind = (e.actor || "").split(":")[0];
  const badgeCls = actorKind === "rule" ? "badge-accent"
    : actorKind === "source" ? "badge-warn"
    : actorKind === "user" ? "badge-ok" : "badge-muted";
  return h("div", { class: "audit-row" + (e.ok ? "" : " err") },
    h("span", { class: "audit-time" }, fmtTime(e.at)),
    h("span", { class: "audit-actor" }, h("span", { class: "badge " + badgeCls }, e.actor || "system")),
    h("span", { class: "audit-action" },
      h("b", {}, e.action || "—"), " ",
      e.target ? h("span", { class: "a-target" }, e.target) : null,
      e.detail ? h("span", { class: "a-target" }, " — " + e.detail) : null),
    h("span", { class: "audit-ok " + (e.ok ? "y" : "n") }, e.ok ? "✓" : "✕"),
  );
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
    if (view === "dashboard") { store.snapshot = await API.get("/state"); }
    else if (view === "rules") { store.rules = await API.get("/rules"); }
    else if (view === "sources") {
      const [srcs, sigs] = await Promise.all([API.get("/sources"), API.get("/signals")]);
      store.sources = srcs || []; upsertSignals(sigs || []);
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
