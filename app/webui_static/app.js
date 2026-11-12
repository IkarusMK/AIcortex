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
    role_hint_none: "Noch kein Admin eingerichtet. Trage in die .env {0} ein und starte neu.",
    role_hint_ask: "Bitte einen Admin, dir Rechte zu geben — deine Kennung: {0}",
    logout: "Abmelden",
    logout_all: "Überall abmelden",
    confirm_logout_all: "Alle WebUI-Sitzungen beenden – auch deine? Danach muss sich jeder neu anmelden.",
    nav_overview: "Übersicht", nav_vault: "Vault", nav_skills: "Skills", nav_users: "Benutzer",
    nav_services: "Services & Geräte", nav_logs: "Logs",
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
    vault_new: "Neues Secret", vault_toggle: "Wert zeigen oder verbergen",
    vault_add_title: "Secret anlegen", vault_edit: "Bearbeiten", vault_edit_title: "Secret bearbeiten: {0}",
    vault_keep_value: "Neuer Wert — leer lassen, um den gespeicherten zu behalten",
    vault_edit_hint: "Den gespeicherten Wert siehst du nie. Trag nur einen neuen ein, wenn du ihn ersetzen willst — oder lösch den Eintrag ganz.",
    vault_f_name: "Name", vault_ph_name: "z. B. GITHUB_TOKEN",
    vault_f_value: "Wert", vault_ph_value: "Token / API-Key / Passwort",
    vault_f_owner: "Besitzer (optional)", vault_ph_owner: "leer = geteiltes Secret",
    vault_writeonly: "Der Wert wird verschlüsselt gespeichert und nie wieder angezeigt.",
    nav_clients: "Clients verbinden",
    cl_list_title: "Agent-Schlüssel", cl_list_hint: "Ein Schlüssel pro Agent — einzeln widerrufbar.",
    cl_col_agent: "Agent", cl_col_person: "Person", cl_col_access: "Zugriff", cl_col_created: "Erstellt",
    cl_empty: "Noch keine Agent-Schlüssel.",
    cl_new: "Neuer Agent", cl_new_title: "Neuen Agent verbinden",
    cl_f_name: "Name des Agenten", cl_ph_name: "z. B. hermes, claude-code",
    cl_f_person: "Für Person", cl_ph_person: "Identität (IdP-sub)",
    cl_f_profile: "Werkzeuge",
    cl_profile_core: "Kern — 11 Werkzeuge, Rest über Suche (empfohlen)",
    cl_profile_full: "Voll — alle Werkzeuge sichtbar",
    cl_f_scopes: "Erlaubte Werkzeuge (all, memory, tasks, …)", cl_f_ttl: "Gültig für Tage (0 = unbegrenzt)",
    cl_create: "Schlüssel erstellen",
    cl_once: "Nur jetzt sichtbar — kopieren und im Client eintragen.",
    cl_copy: "Kopieren", cl_f_client: "Einrichtung für",
    cl_revoke: "Widerrufen", confirm_revoke_key: "Schlüssel von „{0}“ widerrufen? Der Agent verliert sofort den Zugang.",
    cl_never: "unbegrenzt", cl_copied: "Kopiert.",
    vault_save: "Verschlüsselt speichern",
    vault_col_used: "Benutzt von", vault_col_updated: "Geändert",
    vault_f_desc: "Beschreibung (optional)", vault_ph_desc: "Wofür ist das Secret?",
    vault_state_empty: "Vault leer — noch nichts gespeichert.",
    vault_state_encrypted: "Vault verschlüsselt und lesbar.",
    vault_state_plaintext: "Vault liegt im KLARTEXT. Schlüssel setzen und neu verschlüsseln.",
    vault_state_unreadable: "Vault NICHT lesbar — falscher STORAGE_ENCRYPTION_KEY? Nichts wurde verändert.",
    vault_state_rotating: "Schlüssel-Wechsel aktiv (alter Schlüssel gesetzt) — jetzt neu verschlüsseln.",
    vault_state_nokey: "Kein STORAGE_ENCRYPTION_KEY gesetzt — Speichern ist gesperrt.",
    vault_rekey: "Neu verschlüsseln",
    vault_missing: "Werden verwendet, sind aber nicht gespeichert: {0}",
    vault_unused: "nicht verwendet", vault_env: "Umgebungsvariable hat Vorrang",
    confirm_overwrite_secret: "„{0}“ existiert schon ({1}). Wert überschreiben?",
    confirm_rekey: "Den ganzen Vault mit dem aktuellen Schlüssel neu verschlüsseln?",
    shared: "geteilt",
    confirm_del_secret: "Secret „{0}“ endgültig löschen?",
    sk_new: "+ Neuer Skill",
    sk_examples: "Beispiel-Skills übernehmen",
    toast_examples: "{0} Beispiel-Skills übernommen, {1} schon vorhanden.",
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
    us_new: "Neuer Benutzer", us_new_title: "Benutzer anlegen", us_edit_title: "Benutzer bearbeiten: {0}",
    us_f_identity: "Identität (Pocket-ID sub)", us_ph_identity: "z. B. alice",
    us_f_role: "Rolle", us_role_default: "Standard (nicht gesetzt)",
    us_f_memory: "Memory-Bereich", us_own: "eigener Bereich", us_all: "alles",
    us_f_services: "Services", us_f_skills: "Skills",
    us_ph_access: "all | none | name1, name2",
    us_f_devices: "Geräte-Freigaben", us_ph_devices: "z. B. ssh=all; caldav=nextcloud-cal",
    us_f_note: "Notiz",
    confirm_del_user: "Eintrag für „{0}“ entfernen?",
    us_operator: "Operator",
    us_operator_hint: "Operator: Admin bei der Anmeldung (policy.json „operators“)",
    save: "Speichern", cancel: "Abbrechen", delete: "Löschen",
    confirm: "Ja, löschen",
    toast_error: "Fehler: {0}",
  },
  en: {
    tagline: "A SELF-HOSTED MCP BRAIN FOR ANY LLM",
    login_btn: "Sign in with SSO",
    login_hint: "Login goes through your own identity provider (e.g. Pocket ID).",
    login_error: "Sign-in failed — please try again.",
    denied_title: "No access",
    denied_text: "This management UI is for administrators only. Your role isn't sufficient.",
    role_hint_none: "No admin has been set up yet. Add {0} to the .env file and restart.",
    role_hint_ask: "Ask an admin to give you access — your identity: {0}",
    logout: "Sign out",
    logout_all: "Sign out everywhere",
    confirm_logout_all: "End every WebUI session, including yours? Everyone has to sign in again.",
    nav_overview: "Overview", nav_vault: "Vault", nav_skills: "Skills", nav_users: "Users",
    nav_services: "Services & devices", nav_logs: "Logs",
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
    vault_new: "New secret", vault_toggle: "Show or hide the value",
    vault_add_title: "Add a secret", vault_edit: "Edit", vault_edit_title: "Edit secret: {0}",
    vault_keep_value: "New value — leave empty to keep the stored one",
    vault_edit_hint: "You never see the stored value. Enter a new one only to replace it — or delete the entry altogether.",
    vault_f_name: "Name", vault_ph_name: "e.g. GITHUB_TOKEN",
    vault_f_value: "Value", vault_ph_value: "token / API key / password",
    vault_f_owner: "Owner (optional)", vault_ph_owner: "empty = shared secret",
    vault_writeonly: "The value is stored encrypted and will never be shown again.",
    nav_clients: "Connect clients",
    cl_list_title: "Agent keys", cl_list_hint: "One key per agent — revocable one by one.",
    cl_col_agent: "Agent", cl_col_person: "Person", cl_col_access: "Access", cl_col_created: "Created",
    cl_empty: "No agent keys yet.",
    cl_new: "New agent", cl_new_title: "Connect a new agent",
    cl_f_name: "Agent name", cl_ph_name: "e.g. hermes, claude-code",
    cl_f_person: "For person", cl_ph_person: "identity (IdP sub)",
    cl_f_profile: "Tools",
    cl_profile_core: "Core — 11 tools, the rest via search (recommended)",
    cl_profile_full: "Full — every tool listed",
    cl_f_scopes: "Allowed tools (all, memory, tasks, …)", cl_f_ttl: "Valid for days (0 = no expiry)",
    cl_create: "Create key",
    cl_once: "Shown only now — copy it into the client.",
    cl_copy: "Copy", cl_f_client: "Setup for",
    cl_revoke: "Revoke", confirm_revoke_key: "Revoke the key of “{0}”? The agent loses access immediately.",
    cl_never: "no expiry", cl_copied: "Copied.",
    vault_save: "Store encrypted",
    vault_col_used: "Used by", vault_col_updated: "Changed",
    vault_f_desc: "Description (optional)", vault_ph_desc: "What is this secret for?",
    vault_state_empty: "Vault empty — nothing stored yet.",
    vault_state_encrypted: "Vault encrypted and readable.",
    vault_state_plaintext: "Vault is in PLAINTEXT. Set a key and re-encrypt.",
    vault_state_unreadable: "Vault NOT readable — wrong STORAGE_ENCRYPTION_KEY? Nothing was changed.",
    vault_state_rotating: "Key rotation active (old key set) — re-encrypt now.",
    vault_state_nokey: "No STORAGE_ENCRYPTION_KEY set — storing is blocked.",
    vault_rekey: "Re-encrypt",
    vault_missing: "Used but not stored: {0}",
    vault_unused: "not used", vault_env: "environment variable takes precedence",
    confirm_overwrite_secret: "“{0}” already exists ({1}). Overwrite its value?",
    confirm_rekey: "Re-encrypt the whole vault with the current key?",
    shared: "shared",
    confirm_del_secret: "Permanently delete secret “{0}”?",
    sk_new: "+ New skill",
    sk_examples: "Add starter skills",
    toast_examples: "{0} starter skills added, {1} already there.",
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
    us_new: "New user", us_new_title: "Add a user", us_edit_title: "Edit user: {0}",
    us_f_identity: "Identity (Pocket ID sub)", us_ph_identity: "e.g. alice",
    us_f_role: "Role", us_role_default: "Default (not set)",
    us_f_memory: "Memory area", us_own: "own area", us_all: "everything",
    us_f_services: "Services", us_f_skills: "Skills",
    us_ph_access: "all | none | name1, name2",
    us_f_devices: "Device grants", us_ph_devices: "e.g. ssh=all; caldav=nextcloud-cal",
    us_f_note: "Note",
    confirm_del_user: "Remove the entry for “{0}”?",
    us_operator: "operator",
    us_operator_hint: "Operator: admin when signing in (policy.json “operators”)",
    save: "Save", cancel: "Cancel", delete: "Delete",
    confirm: "Yes, delete",
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
  $$("[data-i18n-aria]").forEach((el) => {       // icon-only buttons (× close, 👁)
    el.setAttribute("aria-label", t(el.dataset.i18nAria));
    el.title = t(el.dataset.i18nAria);
  });
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

/* ── toast (the confirm dialog lives in dialogs.js) ──────────────────────── */
let toastTimer = null;
const toastLayer = (el, on) => {                 // popover = top layer: above an open dialog
  if (!el.showPopover) return;
  if (el.matches(":popover-open")) el.hidePopover();
  if (on) el.showPopover();
};
// The toast sits in the topmost open dialog — outside it the page is inert, and a screen
// reader would not hear it — or in <body>. Moving a popover closes it: show it again.
function placeToast() {
  const el = $("#toast");
  const host = topDialog() || document.body;
  if (el.parentNode !== host) host.append(el);
  toastLayer(el, !el.classList.contains("hidden"));
}
function toast(msg, ok = true) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("bad", !ok);
  el.classList.remove("hidden");
  placeToast();
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.classList.add("hidden"); toastLayer(el, false); }, 4200);
}

/* ── router ──────────────────────────────────────────────────────────────── */
const VIEWS = { overview: loadOverview, vault: loadVault, clients: loadClients, skills: loadSkills,
  services: loadServices, users: loadUsers, logs: loadLogs };

// Views of the management plane; everyone else starts in their own notes.
const ADMIN_VIEWS = new Set(["overview", "vault", "clients", "skills", "services", "users", "logs"]);
const isAdmin = () => ME && ME.role === "admin";

function route() {
  const home = isAdmin() ? "overview" : "memory";
  const name = (location.hash || `#${home}`).slice(1);
  const view = VIEWS[name] && (isAdmin() || !ADMIN_VIEWS.has(name)) ? name : home;
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

let SECRETS = [];

function renderVaultState(st, missing) {
  const key = !st.key ? "vault_state_nokey"
    : st.rotating ? "vault_state_rotating" : `vault_state_${st.state}`;
  $("#vault-state-text").textContent = t(key);
  $("#vault-state").classList.toggle("bad", ["plaintext", "unreadable"].includes(st.state) || !st.key);
  $("#btn-vault-rekey").classList.toggle("hidden",
    !(st.key && (st.rotating || st.state === "plaintext")));
  $("#vault-missing").textContent = missing.length ? t("vault_missing", missing.join(", ")) : "";
  $("#vault-missing").classList.toggle("hidden", !missing.length);
}

async function loadOwnerPicker() {
  try {
    const d = await api("/ui/api/identities");
    const list = $("#sec-owner-list");
    list.textContent = "";
    d.identities.forEach((id) => {
      const o = document.createElement("option"); o.value = id; list.append(o);
    });
  } catch (e) { /* picker is a convenience; free text still works */ }
}

let VAULT_EDIT = null;   // the entry being edited, or null when adding

async function deleteSecret(s) {
  if (!(await confirmModal(t("confirm_del_secret", s.name)))) return;
  const r = await api("/ui/api/secrets/delete", { method: "POST", body: { name: s.name, owner: s.owner } });
  toast(r.message, r.ok);
  if (r.ok && VAULT_EDIT && VAULT_EDIT.name === s.name) closeDialog($("#vault-editor"));
  loadVault();
}

// Opens the vault dialog: a new secret (s = null) or an existing one, whose stored
// value is never shown — a new value replaces it, an empty one keeps it.
function editSecret(s) {
  VAULT_EDIT = s || null;
  const form = $("#form-secret");
  form.reset();
  const editing = !!VAULT_EDIT;
  $("#vault-form-title").textContent = editing ? t("vault_edit_title", s.name) : t("vault_add_title");
  $("#sec-name").value = editing ? s.name : "";
  $("#sec-owner").value = editing ? (s.owner || "") : "";
  $("#sec-desc").value = editing ? (s.description || "") : "";
  $("#sec-name").readOnly = $("#sec-owner").readOnly = editing;
  $("#sec-value").required = !editing;
  $("#sec-value").placeholder = editing ? t("vault_keep_value") : t("vault_ph_value");
  $("#vault-edit-hint").classList.toggle("hidden", !editing);
  $("#btn-sec-delete").classList.toggle("hidden", !editing);
  openDialog($("#vault-editor"));                // focus: name (new) or the new value (edit)
}

async function loadVault() {
  const d = await api("/ui/api/secrets");
  SECRETS = d.secrets;
  renderVaultState(d.status || { state: "encrypted", key: true }, d.missing || []);
  loadOwnerPicker();
  const tbody = $("#vault-table tbody");
  tbody.textContent = "";
  $("#vault-empty").classList.toggle("hidden", d.secrets.length > 0);
  d.secrets.forEach((s) => {
    const tr = document.createElement("tr");
    const n = document.createElement("td");
    const nm = document.createElement("button");
    nm.type = "button"; nm.className = "linkish mono"; nm.textContent = s.name;
    nm.title = t("vault_edit");
    nm.addEventListener("click", () => editSecret(s));
    n.append(nm);
    if (s.description) {
      const ds = document.createElement("div"); ds.className = "dim small"; ds.textContent = s.description;
      n.append(ds);
    }
    if (s.env_override) {
      const ev = document.createElement("span"); ev.className = "tag warn"; ev.textContent = t("vault_env");
      n.append(ev);
    }
    const o = document.createElement("td");
    const tag = document.createElement("span");
    tag.className = "tag" + (s.owner ? " owner" : "");
    tag.textContent = s.owner_label || s.owner || t("shared");
    if (s.owner) tag.title = s.owner;
    o.append(tag);
    const used = document.createElement("td"); used.className = "small";
    used.textContent = (s.used_by && s.used_by.length) ? s.used_by.join(", ") : t("vault_unused");
    if (!(s.used_by && s.used_by.length)) used.classList.add("dim");
    const upd = document.createElement("td"); upd.className = "dim small";
    upd.textContent = [(s.updated || "").slice(0, 10), s.updated_by].filter(Boolean).join(" · ");
    const del = document.createElement("td");
    del.className = "actions";
    const edit = document.createElement("button");
    edit.className = "btn ghost sm"; edit.textContent = t("vault_edit");
    edit.addEventListener("click", () => editSecret(s));
    del.append(edit);
    const btn = document.createElement("button");
    btn.className = "btn danger sm"; btn.textContent = t("delete");
    btn.addEventListener("click", () => deleteSecret(s));
    del.append(btn);
    tr.append(n, o, used, upd, del);
    tbody.append(tr);
  });
}


/* ── connect clients (agent keys) ─────────────────────────────────────────── */
let MCP_URL = location.origin + "/mcp";

// Setup per client — mirrors docs/clients.md. Placeholders are filled in, never HTML.
const CLIENT_SNIPPETS = {
  "Claude Code": (u, k) => `claude mcp add --transport http aicortex ${u} \\\n  --header "Authorization: Bearer ${k}"`,
  "Claude Code — automatic start (recommended)": (u) => `# 1) keep the key in the macOS keychain (asks for it — paste the key)\nsecurity add-generic-password -s aicortex -a "$USER" -w\n\n# 2) ~/.zshrc\nexport AICORTEX_KEY="$(security find-generic-password -s aicortex -w 2>/dev/null)"\n\n# 3) connect (single quotes: the key is read at runtime, never stored)\nclaude mcp add --transport http --scope user aicortex ${u} --header 'Authorization: Bearer \${AICORTEX_KEY}'\n\n# 4) ~/.claude/settings.json — load the long-term memory at every start\n#    (copy clients/claude-code/session_start.py from the repository)\n{"env": {"AICORTEX_URL": "${u.replace(/\/mcp$/, "")}",\n         "AICORTEX_KEY_CMD": "security find-generic-password -s aicortex -w"},\n "hooks": {"SessionStart": [{"hooks": [{"type": "command", "timeout": 15,\n   "command": "python3 ~/.claude/hooks/aicortex_session_start.py"}]}]}}`,
  "OpenAI Codex": (u, k) => `# ~/.codex/config.toml\n[mcp_servers.aicortex]\nurl = "${u}"\nbearer_token_env_var = "AICORTEX_KEY"\n\n# shell\nexport AICORTEX_KEY="${k}"`,
  "Hermes Agent": (u, k) => `# ~/.hermes/config.yaml\nmcp_servers:\n  aicortex:\n    url: "${u}"\n    headers:\n      Authorization: "Bearer ${k}"\n\n# check: hermes mcp test aicortex`,
  "OpenClaw": (u, k) => `"mcp": {"servers": {"aicortex": {\n  "transport": "streamable-http",\n  "url": "${u}",\n  "headers": {"Authorization": "Bearer ${k}"}\n}}}`,
  "LibreChat": (u, k) => `# librechat.yaml\nmcpServers:\n  aicortex:\n    type: streamable-http\n    url: ${u}\n    headers:\n      Authorization: "Bearer ${k}"\n    serverInstructions: true`,
  "LM Studio": (u, k) => `{"mcpServers": {"aicortex": {\n  "url": "${u}",\n  "headers": {"Authorization": "Bearer ${k}"}\n}}}`,
  "Open WebUI": (u, k) => `Admin settings → External tools → Add\nType: MCP (Streamable HTTP)\nURL:  ${u}\nAuth: Bearer\nKey:  ${k}`,
  "Claude API": (u, k) => `"mcp_servers": [{\n  "type": "url", "url": "${u}", "name": "aicortex",\n  "authorization_token": "${k}"\n}]`,
  "OpenAI Responses API": (u, k) => `{"type": "mcp", "server_label": "aicortex",\n "server_url": "${u}",\n "authorization": "${k}", "require_approval": "never"}`,
};

function renderSnippet() {
  const fn = CLIENT_SNIPPETS[$("#cl-client").value];
  $("#cl-snippet").textContent = fn ? fn(MCP_URL, $("#cl-key").value) : "";
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast(t("cl_copied")); }
  catch (e) { toast(t("toast_error", e.message), false); }
}

async function loadClients() {
  const d = await api("/ui/api/agentkeys");
  MCP_URL = d.mcp_url || MCP_URL;
  $("#cl-url").textContent = MCP_URL;
  try {
    const ids = await api("/ui/api/identities");
    const list = $("#ak-person-list"); list.textContent = "";
    ids.identities.forEach((id) => { const o = document.createElement("option"); o.value = id; list.append(o); });
  } catch (e) { /* free text still works */ }
  const tbody = $("#cl-table tbody");
  tbody.textContent = "";
  $("#cl-empty").classList.toggle("hidden", d.keys.length > 0);
  d.keys.forEach((k) => {
    const tr = document.createElement("tr");
    const a = document.createElement("td"); a.className = "mono"; a.textContent = k.name || k.keyid;
    const p = document.createElement("td"); p.className = "mono small"; p.textContent = k.sub;
    const acc = document.createElement("td");
    const prof = document.createElement("span"); prof.className = "tag"; prof.textContent = k.profile || "core";
    const sc = document.createElement("div"); sc.className = "dim small"; sc.textContent = (k.scopes || []).join(", ");
    acc.append(prof, sc);
    const cr = document.createElement("td"); cr.className = "dim small";
    cr.textContent = (k.created || "").slice(0, 10) + " · "
      + (k.expires ? new Date(k.expires * 1000).toISOString().slice(0, 10) : t("cl_never"));
    const act = document.createElement("td");
    const btn = document.createElement("button");
    btn.className = "btn danger sm"; btn.textContent = t("cl_revoke");
    btn.addEventListener("click", async () => {
      if (!(await confirmModal(t("confirm_revoke_key", k.name || k.keyid)))) return;
      const r = await api("/ui/api/agentkeys/revoke", { method: "POST", body: { keyid: k.keyid } });
      toast(r.message, r.ok); loadClients();
    });
    act.append(btn);
    tr.append(a, p, acc, cr, act);
    tbody.append(tr);
  });
}

// The dialog has two states: the form, then — once the key exists — the key (shown
// only now) with its setup snippet and a "Done" button.
function showAgentKeyResult(on) {
  $("#form-agentkey").classList.toggle("hidden", on);
  $("#cl-result").classList.toggle("hidden", !on);
  ["#btn-ak-create", "#btn-ak-cancel"].forEach((s) => $(s).classList.toggle("hidden", on));
  $("#btn-ak-done").classList.toggle("hidden", !on);
}

function newAgentKey() {
  $("#form-agentkey").reset();
  $("#cl-key").value = "";
  showAgentKeyResult(false);
  openDialog($("#cl-editor"));
}

function initClients() {
  const sel = $("#cl-client");
  Object.keys(CLIENT_SNIPPETS).forEach((name) => {
    const o = document.createElement("option"); o.value = name; o.textContent = name; sel.append(o);
  });
  sel.addEventListener("change", renderSnippet);
  $("#btn-ak-new").addEventListener("click", newAgentKey);
  $("#cl-editor").addEventListener("close", () => {   // the key doesn't linger in the page
    $("#cl-key").value = ""; $("#cl-snippet").textContent = "";
  });
  $("#cl-copy-key").addEventListener("click", () => copyText($("#cl-key").value));
  $("#cl-copy-snippet").addEventListener("click", () => copyText($("#cl-snippet").textContent));
  $("#form-agentkey").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api("/ui/api/agentkeys", { method: "POST", body: {
        name: $("#ak-name").value.trim(), identity: $("#ak-person").value.trim(),
        profile: $("#ak-profile").value, scopes: $("#ak-scopes").value.trim() || "all",
        ttl_days: Number($("#ak-ttl").value || 0),
      }});
      toast(r.message, r.ok);
      if (!r.ok) return;
      MCP_URL = r.mcp_url || MCP_URL;
      $("#cl-key").value = r.key;
      showAgentKeyResult(true);
      renderSnippet();
      dialogRebase($("#cl-editor"));             // created — nothing left to discard
      $("#cl-copy-key").focus();
      loadClients();
    } catch (err2) { toast(t("toast_error", err2.message), false); }
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
  openDialog($("#sk-editor"));
  renderSkillList();
}

function newSkill() {
  CURRENT_SKILL = "";
  $("#sk-editor-title").textContent = t("sk_new_title");
  $("#form-skill").reset();
  $("#btn-skill-delete").classList.add("hidden");
  openDialog($("#sk-editor"));
  renderSkillList();
}

async function loadServices() {
  const d = await api("/ui/api/services");
  SERVICES = d.services;
  if (typeof oauthLoad === "function") await oauthLoad();   // view-registry.js
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

/* A URL that wraps only where a reader expects it: after "//", "/", "?" and "&"
   (<wbr> break points, text nodes only — no innerHTML). The span keeps the column at
   a readable minimum width; a single overlong piece still wraps (overflow-wrap). */
function urlText(text) {
  const span = document.createElement("span");
  span.className = "url";
  String(text).split(/(?<=[/?&])/).forEach((part, i) => {
    if (i) span.append(document.createElement("wbr"));
    span.append(document.createTextNode(part));
  });
  return span;
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
    const target = document.createElement("td"); target.className = "mono";
    target.append(urlText(s.target || "—"));
    const sec = document.createElement("td");
    if (s.secret) {
      const stag = document.createElement("span"); stag.className = "tag owner"; stag.textContent = s.secret;
      sec.append(stag);
    } else { sec.textContent = "—"; sec.className = "dim"; }
    const desc = document.createElement("td"); desc.className = "small dim";
    const dtext = document.createElement("span"); dtext.className = "desc"; dtext.textContent = s.description;
    desc.append(dtext);
    const act = document.createElement("td"); act.className = "actions";
    [["sv_edit", "ghost", () => openRegistry(s.kind, s.name)],
     ["delete", "danger", () => deleteRegistry(s.kind, s.name)]].forEach(([key, cls, fn]) => {
      const b = document.createElement("button");
      b.className = `btn sm ${cls}`; b.textContent = t(key);
      b.addEventListener("click", () => fn().catch((x) => toast(x.message, false)));
      act.append(b);
    });
    if (typeof oauthCell === "function") oauthCell(s, sec, act);   // OAuth sign-in (view-registry.js)
    tr.append(kind, name, target, sec, desc, act);
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
    // No explicit role: show what the person really gets (operator → admin, else the
    // default), marked with * — not the bare env default.
    const r = u.role || u.effective_role || d.default_role;
    rtag.className = "tag" + (r === "admin" ? " role-admin" : r === "viewer" ? " role-viewer" : "");
    rtag.textContent = u.role || `${r}*`;
    role.append(rtag);
    if (u.operator) {
      const op = document.createElement("span");
      op.className = "tag";
      op.style.marginLeft = "6px";
      op.textContent = t("us_operator");
      op.title = t("us_operator_hint");
      role.append(op);
    }
    const area = document.createElement("td");
    area.className = "small";
    const devs = Object.entries(u.devices).map(([k, v]) => `${k}=${v}`).join("; ");
    area.textContent = `mem:${u.memory} vault:${u.vault} svc:${u.services} skills:${u.skills}`
      + (devs ? ` · ${devs}` : "") + (u.note ? ` — ${u.note}` : "");
    const act = document.createElement("td");
    const edit = document.createElement("button");
    edit.className = "btn ghost sm"; edit.textContent = "✎";
    edit.title = t("vault_edit"); edit.setAttribute("aria-label", `${t("vault_edit")}: ${u.identity}`);
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

// Opens the user dialog: empty for a new entry (u = null) or filled with one.
function fillUserForm(u) {
  $("#form-user").reset();
  $("#us-editor-title").textContent = u ? t("us_edit_title", u.identity) : t("us_new_title");
  if (u) {
    $("#us-identity").value = u.identity;
    $("#us-role").value = u.role || "default";
    $("#us-memory").value = ["own", "all"].includes(u.memory) ? u.memory : "";
    $("#us-services").value = u.services === "all" ? "" : u.services;
    $("#us-skills").value = u.skills === "all" ? "" : u.skills;
    $("#us-devices").value =
      Object.entries(u.devices).map(([k, v]) => `${k}=${v}`).join("; ");
    $("#us-note").value = u.note;
  }
  openDialog($("#us-editor"), { focus: $("#us-identity") });
}

/* Not an admin: show the person their own identity (the IdP 'sub') and how to get
   admin rights — nothing about anyone else. textContent/text nodes only. */
function renderRoleHint() {
  const box = $("#role-hint");
  const visible = !!(ME && ME.authenticated && ME.sub && !isAdmin());
  box.classList.toggle("hidden", !visible);
  if (!visible) return;
  const nobody = !ME.operators_configured;
  const value = nobody ? `OPERATORS=${ME.sub}` : ME.sub;
  const [before, after = ""] = t(nobody ? "role_hint_none" : "role_hint_ask").split("{0}");
  const code = document.createElement("code");
  code.className = "mono";
  code.textContent = value;
  $("#role-hint-text").replaceChildren(before, code, after);
  const copy = $("#role-hint-copy");
  copy.textContent = t("cl_copy");
  copy.onclick = () => copyText(value);
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
  $$("[data-admin]").forEach((el) => el.classList.toggle("hidden", !isAdmin()));
  // Only signed-in sessions can be ended; the local operator (no sign-in) has none.
  $("#btn-logout-all").classList.toggle("hidden", !(isAdmin() && ME.mode === "oidc"));
  $("#user-name").textContent = ME.name || ME.sub;
  $("#ui-version").textContent = `AICortex v${ME.version}`;
  renderRoleHint();
  show("app");
  route();
  if (typeof oauthNotice === "function") oauthNotice();
}

document.addEventListener("DOMContentLoaded", () => {
  $("#btn-login").addEventListener("click", () => { location.href = "/ui/login"; });
  const logout = async () => {
    try { await api("/ui/logout", { method: "POST", body: {} }); } catch (e) { /* ignore */ }
    location.href = "/ui";
  };
  $("#btn-logout").addEventListener("click", logout);
  $("#btn-logout-denied").addEventListener("click", logout);
  $("#btn-logout-all").addEventListener("click", async () => {
    if (!(await confirmModal(t("confirm_logout_all")))) return;
    try {
      const r = await api("/ui/api/sessions/revoke", { method: "POST", body: {} });
      if (!r.ok) { toast(r.message, false); return; }
      location.href = "/ui";
    } catch (err2) { toast(t("toast_error", err2.message), false); }
  });

  $$("[data-setlang]").forEach((b) => b.addEventListener("click", () => {
    LANG = b.dataset.setlang;
    localStorage.setItem("aicortex-ui-lang", LANG);
    applyI18n();
    if (!$("#app").classList.contains("hidden")) { renderRoleHint(); route(); }
  }));

  window.addEventListener("hashchange", route);

  /* vault */
  $("#sec-toggle").addEventListener("click", () => {
    const v = $("#sec-value");
    v.type = v.type === "password" ? "text" : "password";
  });
  $("#btn-vault-rekey").addEventListener("click", async () => {
    if (!(await confirmModal(t("confirm_rekey")))) return;
    try {
      const r = await api("/ui/api/secrets/rekey", { method: "POST", body: {} });
      toast(r.message, r.ok); loadVault();
    } catch (err2) { toast(t("toast_error", err2.message), false); }
  });
  $("#btn-vault-new").addEventListener("click", () => editSecret(null));
  $("#vault-editor").addEventListener("close", () => { VAULT_EDIT = null; });
  $("#btn-sec-delete").addEventListener("click", () => { if (VAULT_EDIT) deleteSecret(VAULT_EDIT); });
  $("#form-secret").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (VAULT_EDIT) {
      try {
        const r = await api("/ui/api/secrets/update", { method: "POST", body: {
          name: VAULT_EDIT.name, owner: VAULT_EDIT.owner || "", value: $("#sec-value").value,
          description: $("#sec-desc").value.trim(),
        }});
        toast(r.message, r.ok);
        if (r.ok) { closeDialog($("#vault-editor")); loadVault(); }
      } catch (err2) { toast(t("toast_error", err2.message), false); }
      return;
    }
    const name = $("#sec-name").value.trim();
    const owner = $("#sec-owner").value.trim();
    const existing = SECRETS.find((s) => s.name === name
      && (s.owner === owner || s.owner_label === owner || (!s.owner && !owner)));
    if (existing && !(await confirmModal(t("confirm_overwrite_secret", name,
      existing.owner_label || existing.owner || t("shared"))))) return;
    try {
      const r = await api("/ui/api/secrets", { method: "POST", body: {
        name, value: $("#sec-value").value, owner,
        description: $("#sec-desc").value.trim(),
      }});
      toast(r.message, r.ok);
      if (r.ok) { closeDialog($("#vault-editor")); $("#form-secret").reset(); loadVault(); }
    } catch (err2) { toast(t("toast_error", err2.message), false); }
  });

  /* clients */
  initClients();

  /* skills */
  $("#btn-skill-new").addEventListener("click", newSkill);
  $("#btn-skill-examples").addEventListener("click", async () => {
    try {
      const r = await api("/ui/api/skills/examples", { method: "POST", body: {} });
      toast(t("toast_examples", r.added.length, r.skipped.length));
      await loadSkills();
    } catch (err) {
      toast(err.message, false);
    }
  });
  // however the dialog closes (Save, Delete, Cancel, ×, Esc): no skill is selected any more
  $("#sk-editor").addEventListener("close", () => {
    if ($("#sk-editor").open) return;            // reopened meanwhile
    CURRENT_SKILL = ""; renderSkillList();
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
      if (r.ok !== false) closeDialog($("#sk-editor"));
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
    closeDialog($("#sk-editor"));
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
      if (r.ok) { closeDialog($("#us-editor")); loadUsers(); }
    } catch (err2) { toast(t("toast_error", err2.message), false); }
  });
  $("#btn-user-new").addEventListener("click", () => fillUserForm(null));

  boot();
});
