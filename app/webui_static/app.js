/* AIcortex Admin — vanilla JS, no build step, no dependencies.
   All rendering uses textContent/createElement (never innerHTML with data),
   so stored strings can't inject markup. Language: DE/EN, persisted locally. */
"use strict";

/* ── i18n ────────────────────────────────────────────────────────────────── */
const I18N = {
  de: {
    tagline: "A SELF-HOSTED MCP BRAIN FOR ANY LLM",
    login_btn: "Anmelden mit SSO",
    login_hint: "Login läuft über deinen eigenen Identity-Provider (z. B. Pocket ID).",
    login_error: "Anmeldung fehlgeschlagen — bitte erneut versuchen.",
    denied_title: "Kein Zugriff",
    denied_text: "Diese Verwaltungsoberfläche ist nur für Administratoren. Deine Rolle reicht dafür nicht aus.",
    logout: "Abmelden",
    nav_overview: "Übersicht", nav_vault: "Vault", nav_skills: "Skills", nav_users: "Benutzer",
    nav_services: "Services & Geräte", nav_logs: "Logs",
    sv_badge: "read-only",
    sv_hint: "Nur Ansicht — anlegen & ändern läuft über den Assistenten (service_add, mqtt_add, …), weil dort Secrets und Spezialfelder dranhängen.",
    sv_col_kind: "Art", sv_col_name: "Name", sv_col_target: "Ziel",
    sv_col_secret: "Secret (Name)", sv_col_desc: "Beschreibung",
    sv_empty: "Noch keine Integrationen registriert.",
    sv_all: "Alle",
    lg_badge: "Audit",
    lg_hint: "Policy-Entscheidungen: wer hat wann welches Tool gerufen — erlaubt, verweigert und warum. Neueste zuerst.",
    lg_filter_ph: "filtern… (Identität, Tool, Grund)",
    lg_refresh: "Aktualisieren",
    lg_col_time: "Zeit", lg_col_identity: "Identität", lg_col_tool: "Tool / Aktion",
    lg_col_decision: "Entscheidung", lg_col_reason: "Grund",
    lg_empty: "Keine Einträge (Log leer oder Filter zu streng).",
    ov_footer: "Alles hier sind Daten auf deinem NAS — kein Redeploy nötig.",
    ov_skills: "Skills", ov_categories: "Kategorien", ov_secrets: "Secrets",
    ov_services: "Services", ov_devices: "Geräte & Endpunkte", ov_users: "Benutzer",
    ov_version: "Version",
    enforce_on: "Enforce: an", enforce_off: "Enforce: aus (Homelab)",
    vault_badge: "verschlüsselt · write-only",
    vault_list_title: "Gespeicherte Secrets",
    vault_list_hint: "Nur Namen — Werte verlassen den Vault nie.",
    vault_col_name: "Name", vault_col_owner: "Bereich",
    vault_empty: "Noch keine Secrets gespeichert.",
    vault_add_title: "Secret anlegen",
    vault_f_name: "Name", vault_ph_name: "z. B. GITHUB_TOKEN",
    vault_f_value: "Wert", vault_ph_value: "Token / API-Key / Passwort",
    vault_f_owner: "Besitzer (optional)", vault_ph_owner: "leer = geteiltes Secret",
    vault_writeonly: "Der Wert wird verschlüsselt gespeichert und nie wieder angezeigt.",
    vault_save: "Verschlüsselt speichern",
    shared: "geteilt",
    confirm_del_secret: "Secret „{0}“ endgültig löschen?",
    sk_new: "+ Neuer Skill",
    sk_all: "Alle",
    sk_new_title: "Neuen Skill anlegen",
    sk_edit_title: "Skill bearbeiten",
    sk_f_name: "Name", sk_f_category: "Kategorie", sk_f_tags: "Tags",
    sk_f_desc: "Beschreibung", sk_f_body: "Anleitung (Markdown)",
    confirm_del_skill: "Skill „{0}“ samt Ordner löschen?",
    us_list_title: "Benutzer & Rollen",
    us_list_hint: "Rollen und Datenbereiche aus policy.json — greift bei AUTH_ENFORCE=1.",
    us_col_identity: "Identität", us_col_role: "Rolle", us_col_area: "Bereich",
    us_empty: "Keine Benutzer konfiguriert — es gilt die Standard-Rolle.",
    us_add_title: "Benutzer anlegen / bearbeiten",
    us_f_identity: "Identität (Pocket-ID sub)", us_ph_identity: "z. B. steffen",
    us_f_role: "Rolle", us_role_default: "Standard (nicht gesetzt)",
    us_f_memory: "Memory-Bereich", us_own: "eigener Bereich", us_all: "alles",
    us_f_services: "Services", us_f_skills: "Skills",
    us_ph_access: "all | none | name1, name2",
    us_f_devices: "Geräte-Freigaben", us_ph_devices: "z. B. ssh=all; caldav=nextcloud-cal",
    us_f_note: "Notiz",
    confirm_del_user: "Eintrag für „{0}“ entfernen?",
    save: "Speichern", cancel: "Abbrechen", delete: "Löschen",
    clear: "Leeren", confirm: "Ja, löschen",
    toast_error: "Fehler: {0}",
  },
  en: {
    tagline: "A SELF-HOSTED MCP BRAIN FOR ANY LLM",
    login_btn: "Sign in with SSO",
    login_hint: "Login goes through your own identity provider (e.g. Pocket ID).",
    login_error: "Sign-in failed — please try again.",
    denied_title: "No access",
    denied_text: "This management UI is for administrators only. Your role isn't sufficient.",
    logout: "Sign out",
    nav_overview: "Overview", nav_vault: "Vault", nav_skills: "Skills", nav_users: "Users",
    nav_services: "Services & devices", nav_logs: "Logs",
    sv_badge: "read-only",
    sv_hint: "View only — create & modify through the assistant (service_add, mqtt_add, …), since secrets and special fields are involved.",
    sv_col_kind: "Kind", sv_col_name: "Name", sv_col_target: "Target",
    sv_col_secret: "Secret (name)", sv_col_desc: "Description",
    sv_empty: "No integrations registered yet.",
    sv_all: "All",
    lg_badge: "Audit",
    lg_hint: "Policy decisions: who called which tool when — allowed, denied and why. Newest first.",
    lg_filter_ph: "filter… (identity, tool, reason)",
    lg_refresh: "Refresh",
    lg_col_time: "Time", lg_col_identity: "Identity", lg_col_tool: "Tool / action",
    lg_col_decision: "Decision", lg_col_reason: "Reason",
    lg_empty: "No entries (log empty or filter too strict).",
    ov_footer: "Everything here is data on your NAS — no redeploy needed.",
    ov_skills: "Skills", ov_categories: "Categories", ov_secrets: "Secrets",
    ov_services: "Services", ov_devices: "Devices & endpoints", ov_users: "Users",
    ov_version: "Version",
    enforce_on: "Enforce: on", enforce_off: "Enforce: off (homelab)",
    vault_badge: "encrypted · write-only",
    vault_list_title: "Stored secrets",
    vault_list_hint: "Names only — values never leave the vault.",
    vault_col_name: "Name", vault_col_owner: "Scope",
    vault_empty: "No secrets stored yet.",
    vault_add_title: "Add a secret",
    vault_f_name: "Name", vault_ph_name: "e.g. GITHUB_TOKEN",
    vault_f_value: "Value", vault_ph_value: "token / API key / password",
    vault_f_owner: "Owner (optional)", vault_ph_owner: "empty = shared secret",
    vault_writeonly: "The value is stored encrypted and will never be shown again.",
    vault_save: "Store encrypted",
    shared: "shared",
    confirm_del_secret: "Permanently delete secret “{0}”?",
    sk_new: "+ New skill",
    sk_all: "All",
    sk_new_title: "Create a new skill",
    sk_edit_title: "Edit skill",
    sk_f_name: "Name", sk_f_category: "Category", sk_f_tags: "Tags",
    sk_f_desc: "Description", sk_f_body: "Instructions (Markdown)",
    confirm_del_skill: "Delete skill “{0}” and its folder?",
    us_list_title: "Users & roles",
    us_list_hint: "Roles and data areas from policy.json — enforced with AUTH_ENFORCE=1.",
    us_col_identity: "Identity", us_col_role: "Role", us_col_area: "Area",
    us_empty: "No users configured — the default role applies.",
    us_add_title: "Add / edit user",
    us_f_identity: "Identity (Pocket ID sub)", us_ph_identity: "e.g. steffen",
    us_f_role: "Role", us_role_default: "Default (not set)",
    us_f_memory: "Memory area", us_own: "own area", us_all: "everything",
    us_f_services: "Services", us_f_skills: "Skills",
    us_ph_access: "all | none | name1, name2",
    us_f_devices: "Device grants", us_ph_devices: "e.g. ssh=all; caldav=nextcloud-cal",
    us_f_note: "Note",
    confirm_del_user: "Remove the entry for “{0}”?",
    save: "Save", cancel: "Cancel", delete: "Delete",
    clear: "Clear", confirm: "Yes, delete",
    toast_error: "Error: {0}",
  },
};

let LANG = localStorage.getItem("aicortex-ui-lang")
  || ((navigator.language || "en").toLowerCase().startsWith("de") ? "de" : "en");
if (!I18N[LANG]) LANG = "en";
let ME = null;
let SKILLS = [], SKILL_FILTER = "", CURRENT_SKILL = "";
let SERVICES = [], SV_FILTER = "";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const t = (k, ...args) => {
  let s = (I18N[LANG] && I18N[LANG][k]) || I18N.en[k] || k;
  args.forEach((a, i) => { s = s.replace(`{${i}}`, a); });
  return s;
};

function applyI18n() {
  $$("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  $$("[data-i18n-ph]").forEach((el) => { el.placeholder = t(el.dataset.i18nPh); });
  $$(".lang-switch button").forEach((b) =>
    b.classList.toggle("active", b.dataset.setlang === LANG));
  document.documentElement.lang = LANG;
}

/* ── API ─────────────────────────────────────────────────────────────────── */
async function api(path, opts = {}) {
  const headers = { "X-CSRF": ME ? ME.csrf : "" };
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, { ...opts, headers, credentials: "same-origin" });
  let data = {};
  try { data = await r.json(); } catch (e) { /* non-JSON error body */ }
  if (!r.ok && !(data && data.message)) {
    throw new Error(data.detail || data.error || `HTTP ${r.status}`);
  }
  return data;
}

/* ── toast + confirm modal ───────────────────────────────────────────────── */
let toastTimer = null;
function toast(msg, ok = true) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("bad", !ok);
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 4200);
}

function confirmModal(text) {
  return new Promise((resolve) => {
    const m = $("#modal");
    $("#modal-text").textContent = text;
    m.classList.remove("hidden");
    const done = (v) => { m.classList.add("hidden"); cleanup(); resolve(v); };
    const okH = () => done(true), noH = () => done(false);
    const escH = (e) => { if (e.key === "Escape") done(false); };
    function cleanup() {
      $("#modal-ok").removeEventListener("click", okH);
      $("#modal-cancel").removeEventListener("click", noH);
      document.removeEventListener("keydown", escH);
    }
    $("#modal-ok").addEventListener("click", okH);
    $("#modal-cancel").addEventListener("click", noH);
    document.addEventListener("keydown", escH);
  });
}

/* ── router ──────────────────────────────────────────────────────────────── */
const VIEWS = { overview: loadOverview, vault: loadVault, skills: loadSkills,
  services: loadServices, users: loadUsers, logs: loadLogs };

function route() {
  const name = (location.hash || "#overview").slice(1);
  const view = VIEWS[name] ? name : "overview";
  $$(".view").forEach((v) => v.classList.add("hidden"));
  $(`#view-${view}`).classList.remove("hidden");
  $$(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === view));
  VIEWS[view]().catch((e) => toast(t("toast_error", e.message), false));
}

/* ── views ───────────────────────────────────────────────────────────────── */
async function loadOverview() {
  const d = await api("/ui/api/overview");
  const badge = $("#ov-enforce");
  badge.textContent = d.enforce ? t("enforce_on") : t("enforce_off");
  badge.className = "badge " + (d.enforce ? "on" : "off");
  // Each tile links to its section — a tile that looks clickable must BE clickable.
  const cells = [
    [d.skills, "ov_skills", "skills"], [d.categories, "ov_categories", "skills"],
    [d.secrets, "ov_secrets", "vault"], [d.services, "ov_services", "services"],
    [d.devices, "ov_devices", "services"], [d.users, "ov_users", "users"],
    ["v" + d.version, "ov_version", null],
  ];
  const grid = $("#ov-grid");
  grid.textContent = "";
  cells.forEach(([k, l, target]) => {
    const card = document.createElement(target ? "button" : "div");
    card.className = "stat" + (target ? " link" : "");
    if (target) {
      card.type = "button";
      card.addEventListener("click", () => { location.hash = "#" + target; });
    }
    const kv = document.createElement("div"); kv.className = "k"; kv.textContent = k;
    const lv = document.createElement("div"); lv.className = "l"; lv.textContent = t(l);
    card.append(kv, lv);
    grid.append(card);
  });
}

async function loadVault() {
  const d = await api("/ui/api/secrets");
  const tbody = $("#vault-table tbody");
  tbody.textContent = "";
  $("#vault-empty").classList.toggle("hidden", d.secrets.length > 0);
  d.secrets.forEach((s) => {
    const tr = document.createElement("tr");
    const n = document.createElement("td"); n.className = "mono"; n.textContent = s.name;
    const o = document.createElement("td");
    const tag = document.createElement("span");
    tag.className = "tag" + (s.owner ? " owner" : "");
    tag.textContent = s.owner || t("shared");
    o.append(tag);
    const del = document.createElement("td");
    const btn = document.createElement("button");
    btn.className = "btn danger sm"; btn.textContent = t("delete");
    btn.addEventListener("click", async () => {
      if (!(await confirmModal(t("confirm_del_secret", s.name)))) return;
      const r = await api("/ui/api/secrets/delete",
        { method: "POST", body: { name: s.name, owner: s.owner } });
      toast(r.message, r.ok); loadVault();
    });
    del.append(btn);
    tr.append(n, o, del);
    tbody.append(tr);
  });
}

async function loadSkills() {
  const d = await api("/ui/api/skills");
  SKILLS = d.skills;
  renderSkillChips();
  renderSkillList();
  const dl = $("#sk-cat-list");
  dl.textContent = "";
  [...new Set(SKILLS.map((s) => s.category))].sort().forEach((c) => {
    const o = document.createElement("option"); o.value = c; dl.append(o);
  });
}

function renderSkillChips() {
  const cats = [...new Set(SKILLS.map((s) => s.category))].sort();
  const box = $("#sk-cats");
  box.textContent = "";
  const mk = (label, value) => {
    const b = document.createElement("button");
    b.className = "chip" + (SKILL_FILTER === value ? " active" : "");
    b.textContent = label;
    b.addEventListener("click", () => { SKILL_FILTER = value; renderSkillChips(); renderSkillList(); });
    box.append(b);
  };
  mk(`${t("sk_all")} (${SKILLS.length})`, "");
  cats.forEach((c) =>
    mk(`${c} (${SKILLS.filter((s) => s.category === c).length})`, c));
}

function renderSkillList() {
  const list = $("#sk-list");
  list.textContent = "";
  SKILLS.filter((s) => !SKILL_FILTER || s.category === SKILL_FILTER).forEach((s) => {
    const it = document.createElement("div");
    it.className = "sk-item" + (s.name === CURRENT_SKILL ? " active" : "");
    const n = document.createElement("div"); n.className = "n";
    const nm = document.createElement("span"); nm.textContent = s.title || s.name;
    const tag = document.createElement("span"); tag.className = "tag"; tag.textContent = s.category;
    n.append(nm, tag);
    const dd = document.createElement("div"); dd.className = "d"; dd.textContent = s.description;
    it.append(n, dd);
    it.addEventListener("click", () => openSkill(s.name));
    list.append(it);
  });
}

async function openSkill(name) {
  const d = await api(`/ui/api/skills/get?name=${encodeURIComponent(name)}`);
  CURRENT_SKILL = d.name;
  $("#sk-editor-title").textContent = t("sk_edit_title");
  $("#sk-name").value = d.title;
  $("#sk-category").value = d.category === "uncategorized" ? "" : d.category;
  $("#sk-tags").value = d.tags;
  $("#sk-desc").value = d.description;
  $("#sk-body").value = d.instructions;
  $("#btn-skill-delete").classList.remove("hidden");
  $("#sk-editor").classList.remove("hidden");
  renderSkillList();
}

function newSkill() {
  CURRENT_SKILL = "";
  $("#sk-editor-title").textContent = t("sk_new_title");
  $("#form-skill").reset();
  $("#btn-skill-delete").classList.add("hidden");
  $("#sk-editor").classList.remove("hidden");
  renderSkillList();
  $("#sk-name").focus();
}

async function loadServices() {
  const d = await api("/ui/api/services");
  SERVICES = d.services;
  renderSvChips();
  renderSvTable();
}

function renderSvChips() {
  const kinds = [...new Set(SERVICES.map((s) => s.kind))].sort();
  const box = $("#sv-kinds");
  box.textContent = "";
  const mk = (label, value) => {
    const b = document.createElement("button");
    b.className = "chip" + (SV_FILTER === value ? " active" : "");
    b.textContent = label;
    b.addEventListener("click", () => { SV_FILTER = value; renderSvChips(); renderSvTable(); });
    box.append(b);
  };
  mk(`${t("sv_all")} (${SERVICES.length})`, "");
  kinds.forEach((k) =>
    mk(`${k} (${SERVICES.filter((s) => s.kind === k).length})`, k));
}

function renderSvTable() {
  const tbody = $("#sv-table tbody");
  tbody.textContent = "";
  const rows = SERVICES.filter((s) => !SV_FILTER || s.kind === SV_FILTER);
  $("#sv-empty").classList.toggle("hidden", rows.length > 0);
  rows.forEach((s) => {
    const tr = document.createElement("tr");
    const kind = document.createElement("td");
    const ktag = document.createElement("span"); ktag.className = "tag"; ktag.textContent = s.kind;
    kind.append(ktag);
    const name = document.createElement("td"); name.className = "mono"; name.textContent = s.name;
    const target = document.createElement("td"); target.className = "mono break"; target.textContent = s.target || "—";
    const sec = document.createElement("td");
    if (s.secret) {
      const stag = document.createElement("span"); stag.className = "tag owner"; stag.textContent = s.secret;
      sec.append(stag);
    } else { sec.textContent = "—"; sec.className = "dim"; }
    const desc = document.createElement("td"); desc.className = "small dim"; desc.textContent = s.description;
    tr.append(kind, name, target, sec, desc);
    tbody.append(tr);
  });
}

async function loadLogs() {
  const q = encodeURIComponent(($("#lg-filter").value || "").trim());
  const d = await api(`/ui/api/audit?limit=200&q=${q}`);
  const tbody = $("#lg-table tbody");
  tbody.textContent = "";
  $("#lg-empty").classList.toggle("hidden", d.entries.length > 0);
  d.entries.forEach((e) => {
    const tr = document.createElement("tr");
    const ts = document.createElement("td"); ts.className = "mono small";
    ts.textContent = String(e.ts || "").replace("T", " ").replace("+00:00", "Z");
    const id = document.createElement("td"); id.className = "mono"; id.textContent = e.identity || "?";
    const tool = document.createElement("td"); tool.className = "mono"; tool.textContent = e.tool || "";
    const dec = document.createElement("td");
    const dtag = document.createElement("span");
    const v = String(e.decision || "");
    dtag.className = "tag " + (v === "allow" ? "ok" : v === "deny" ? "bad" : "warn");
    dtag.textContent = v;
    dec.append(dtag);
    const why = document.createElement("td"); why.className = "small dim break"; why.textContent = e.reason || "";
    tr.append(ts, id, tool, dec, why);
    tbody.append(tr);
  });
}

async function loadUsers() {
  const d = await api("/ui/api/users");
  const badge = $("#us-enforce");
  badge.textContent = d.enforce ? t("enforce_on") : t("enforce_off");
  badge.className = "badge " + (d.enforce ? "on" : "off");
  const tbody = $("#us-table tbody");
  tbody.textContent = "";
  $("#us-empty").classList.toggle("hidden", d.users.length > 0);
  d.users.forEach((u) => {
    const tr = document.createElement("tr");
    const id = document.createElement("td"); id.className = "mono"; id.textContent = u.identity;
    const role = document.createElement("td");
    const rtag = document.createElement("span");
    const r = u.role || d.default_role;
    rtag.className = "tag" + (r === "admin" ? " role-admin" : r === "viewer" ? " role-viewer" : "");
    rtag.textContent = u.role || `${d.default_role}*`;
    role.append(rtag);
    const area = document.createElement("td");
    area.className = "small";
    const devs = Object.entries(u.devices).map(([k, v]) => `${k}=${v}`).join("; ");
    area.textContent = `mem:${u.memory} vault:${u.vault} svc:${u.services} skills:${u.skills}`
      + (devs ? ` · ${devs}` : "") + (u.note ? ` — ${u.note}` : "");
    const act = document.createElement("td");
    const edit = document.createElement("button");
    edit.className = "btn ghost sm"; edit.textContent = "✎";
    edit.addEventListener("click", () => fillUserForm(u));
    const del = document.createElement("button");
    del.className = "btn danger sm"; del.textContent = t("delete");
    del.style.marginLeft = "6px";
    del.addEventListener("click", async () => {
      if (!(await confirmModal(t("confirm_del_user", u.identity)))) return;
      const res = await api("/ui/api/users/delete",
        { method: "POST", body: { identity: u.identity } });
      toast(res.message, res.ok); loadUsers();
    });
    act.append(edit, del);
    tr.append(id, role, area, act);
    tbody.append(tr);
  });
}

function fillUserForm(u) {
  $("#us-identity").value = u.identity;
  $("#us-role").value = u.role || "default";
  $("#us-memory").value = ["own", "all"].includes(u.memory) ? u.memory : "";
  $("#us-services").value = u.services === "all" ? "" : u.services;
  $("#us-skills").value = u.skills === "all" ? "" : u.skills;
  $("#us-devices").value =
    Object.entries(u.devices).map(([k, v]) => `${k}=${v}`).join("; ");
  $("#us-note").value = u.note;
  $("#us-identity").focus();
}

/* ── wire-up ─────────────────────────────────────────────────────────────── */
function show(id) {
  ["view-login", "view-denied", "app"].forEach((v) =>
    $("#" + v).classList.toggle("hidden", v !== id));
}

async function boot() {
  applyI18n();
  try { ME = await api("/ui/api/me"); } catch (e) { ME = { authenticated: false }; }
  const err = new URLSearchParams(location.search).get("login_error");
  if (err) {
    const el = $("#login-error");
    el.textContent = t("login_error");
    el.classList.remove("hidden");
  }
  if (!ME.authenticated) { show("view-login"); return; }
  if (ME.role !== "admin") { show("view-denied"); return; }
  $("#user-name").textContent = ME.name || ME.sub;
  $("#ui-version").textContent = `AICortex v${ME.version}`;
  show("app");
  route();
}

document.addEventListener("DOMContentLoaded", () => {
  $("#btn-login").addEventListener("click", () => { location.href = "/ui/login"; });
  const logout = async () => {
    try { await api("/ui/logout", { method: "POST", body: {} }); } catch (e) { /* ignore */ }
    location.href = "/ui";
  };
  $("#btn-logout").addEventListener("click", logout);
  $("#btn-logout-denied").addEventListener("click", logout);

  $$("[data-setlang]").forEach((b) => b.addEventListener("click", () => {
    LANG = b.dataset.setlang;
    localStorage.setItem("aicortex-ui-lang", LANG);
    applyI18n();
    if (!$("#app").classList.contains("hidden")) route();
  }));

  window.addEventListener("hashchange", route);

  /* vault */
  $("#sec-toggle").addEventListener("click", () => {
    const v = $("#sec-value");
    v.type = v.type === "password" ? "text" : "password";
  });
  $("#form-secret").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api("/ui/api/secrets", { method: "POST", body: {
        name: $("#sec-name").value.trim(),
        value: $("#sec-value").value,
        owner: $("#sec-owner").value.trim(),
      }});
      toast(r.message, r.ok);
      if (r.ok) { $("#form-secret").reset(); loadVault(); }
    } catch (err2) { toast(t("toast_error", err2.message), false); }
  });

  /* skills */
  $("#btn-skill-new").addEventListener("click", newSkill);
  $("#btn-skill-close").addEventListener("click", () => {
    CURRENT_SKILL = "";
    $("#sk-editor").classList.add("hidden");
    renderSkillList();
  });
  $("#form-skill").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api("/ui/api/skills", { method: "POST", body: {
        name: $("#sk-name").value.trim(),
        category: $("#sk-category").value.trim(),
        tags: $("#sk-tags").value.trim(),
        description: $("#sk-desc").value.trim(),
        instructions: $("#sk-body").value,
      }});
      toast(r.message, r.ok !== false);
      loadSkills();
    } catch (err2) { toast(t("toast_error", err2.message), false); }
  });
  $("#btn-skill-delete").addEventListener("click", async () => {
    if (!CURRENT_SKILL) return;
    if (!(await confirmModal(t("confirm_del_skill", CURRENT_SKILL)))) return;
    const r = await api("/ui/api/skills/delete",
      { method: "POST", body: { name: CURRENT_SKILL } });
    toast(r.message, r.ok);
    CURRENT_SKILL = "";
    $("#sk-editor").classList.add("hidden");
    loadSkills();
  });

  /* logs */
  $("#lg-refresh").addEventListener("click", () =>
    loadLogs().catch((e) => toast(t("toast_error", e.message), false)));
  $("#lg-filter").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      loadLogs().catch((err2) => toast(t("toast_error", err2.message), false));
    }
  });

  /* users */
  $("#form-user").addEventListener("submit", async (e) => {
    e.preventDefault();
    const devices = {};
    $("#us-devices").value.split(";").forEach((part) => {
      const i = part.indexOf("=");
      if (i > 0) devices[part.slice(0, i).trim()] = part.slice(i + 1).trim();
    });
    try {
      const r = await api("/ui/api/users", { method: "POST", body: {
        identity: $("#us-identity").value.trim(),
        role: $("#us-role").value,
        memory: $("#us-memory").value,
        services: $("#us-services").value.trim(),
        skills: $("#us-skills").value.trim(),
        devices,
        note: $("#us-note").value.trim(),
      }});
      toast(r.message, r.ok);
      if (r.ok) { $("#form-user").reset(); loadUsers(); }
    } catch (err2) { toast(t("toast_error", err2.message), false); }
  });
  $("#btn-user-clear").addEventListener("click", () => $("#form-user").reset());

  boot();
});
