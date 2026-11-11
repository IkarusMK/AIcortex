/* Services & devices editor (admin). The form for each registry is built from
   the input schema of its *_add tool, so every registry — HTTP services, MQTT,
   FTP, printers, scanners, WebDAV, CalDAV, SSH, mail, IMAP, MCP, webhooks — is
   editable without a hand-written form. *_env fields pick a vault NAME; values
   never come near the browser. */

Object.assign(I18N.de, {
  sv_new: "Neu", sv_edit: "Bearbeiten", sv_new_title: "Neue Integration", sv_edit_title: "{0} bearbeiten",
  sv_f_kind: "Art", sv_secret_none: "— kein Secret —", sv_json_hint: "JSON",
  sv_hint: "Anlegen und ändern direkt hier oder über den Assistenten. Secrets wählst du nur per Namen aus dem Vault.",
  confirm_sv_delete: "„{0}“ ({1}) löschen?", toast_sv_saved: "Gespeichert.", toast_sv_deleted: "Gelöscht.",
});
Object.assign(I18N.en, {
  sv_new: "New", sv_edit: "Edit", sv_new_title: "New integration", sv_edit_title: "Edit {0}",
  sv_f_kind: "Kind", sv_secret_none: "— no secret —", sv_json_hint: "JSON",
  sv_hint: "Create and change them right here or through the assistant. Secrets are picked by name from the vault.",
  confirm_sv_delete: "Delete “{0}” ({1})?", toast_sv_saved: "Saved.", toast_sv_deleted: "Deleted.",
});

let REG = { kinds: null, vault: [], kind: "", name: "" };

async function regSchema() {
  if (!REG.kinds) REG.kinds = (await api("/ui/api/registry/schema")).kinds;
  REG.vault = (await api("/ui/api/secrets")).secrets.map((s) => s.name);
  return REG.kinds;
}

function regInput(f, value) {
  const id = `sv-field-${f.name}`;
  let el;
  if (f.secret_ref) {
    el = document.createElement("select");
    [["", t("sv_secret_none")], ...REG.vault.map((n) => [n, n])].forEach(([v, label]) => {
      const o = document.createElement("option"); o.value = v; o.textContent = label; el.append(o);
    });
    if (value && !REG.vault.includes(value)) {   // a reference to a missing secret stays visible
      const o = document.createElement("option"); o.value = value; o.textContent = `${value} ⚠`; el.append(o);
    }
    el.value = value || "";
  } else if (f.enum) {
    el = document.createElement("select");
    f.enum.forEach((v) => { const o = document.createElement("option"); o.value = v; o.textContent = v; el.append(o); });
    el.value = value ?? f.default ?? f.enum[0];
  } else if (f.type === "boolean") {
    el = document.createElement("input"); el.type = "checkbox";
    el.checked = value ?? f.default ?? false;
  } else if (f.type === "object" || f.type === "array") {
    el = document.createElement("textarea"); el.rows = 3; el.spellcheck = false;
    el.placeholder = t("sv_json_hint");
    const v = value ?? f.default;
    el.value = v == null || v === "" ? "" : JSON.stringify(v);
  } else {
    el = document.createElement("input");
    el.type = f.type === "integer" || f.type === "number" ? "number" : "text";
    const v = value ?? f.default;
    el.value = v == null ? "" : String(v);
  }
  el.id = id;
  el.dataset.field = f.name;
  el.dataset.type = f.type;
  if (el.type !== "checkbox") el.className = "inp" + (f.type === "object" || f.type === "array" ? " mono" : "");
  if (f.required) el.required = true;
  return el;
}

function regRenderForm(kind, config) {
  const spec = REG.kinds.find((k) => k.kind === kind);
  const box = $("#sv-fields");
  box.textContent = "";
  $("#sv-help").textContent = spec ? spec.help : "";
  (spec ? spec.fields : []).forEach((f) => {
    const label = document.createElement("label");
    label.htmlFor = `sv-field-${f.name}`;
    label.className = "mono";
    label.textContent = f.name + (f.required ? " *" : "");
    const input = regInput(f, config ? config[f.name] : undefined);
    if (f.name === "name" && REG.name) input.readOnly = true;   // renaming = a new entry
    box.append(label, input);
  });
}

async function openRegistry(kind, name) {
  await regSchema();
  const d = await api(`/ui/api/registry/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`);
  REG.kind = kind; REG.name = name;
  $("#sv-editor-title").textContent = t("sv_edit_title", name);
  $("#sv-kind").value = kind; $("#sv-kind").disabled = true;
  regRenderForm(kind, { name, ...d.config });
  $("#btn-sv-delete").classList.remove("hidden");
  openDialog($("#sv-editor"));
}

async function newRegistry() {
  const kinds = await regSchema();
  REG.kind = SV_FILTER && kinds.some((k) => k.kind === SV_FILTER) ? SV_FILTER : kinds[0].kind;
  REG.name = "";
  const sel = $("#sv-kind");
  sel.textContent = "";
  kinds.forEach((k) => { const o = document.createElement("option"); o.value = k.kind; o.textContent = k.kind; sel.append(o); });
  sel.value = REG.kind; sel.disabled = false;
  $("#sv-editor-title").textContent = t("sv_new_title");
  regRenderForm(REG.kind, null);
  $("#btn-sv-delete").classList.add("hidden");
  openDialog($("#sv-editor"));
}

function regCollect() {
  const body = {};
  $$("#sv-fields [data-field]").forEach((el) => {
    const { field, type } = el.dataset;
    if (el.type === "checkbox") { body[field] = el.checked; return; }
    const v = el.value;
    if (v === "" && (type === "integer" || type === "number" || type === "object" || type === "array")) return;
    body[field] = v;
  });
  return body;
}

async function saveRegistry(ev) {
  ev.preventDefault();
  const kind = $("#sv-kind").value;
  const d = await api(`/ui/api/registry/${encodeURIComponent(kind)}`, { method: "POST", body: regCollect() });
  if (!d.ok) { toast(d.message, false); return; }
  toast(t("toast_sv_saved"));
  closeDialog($("#sv-editor"));
  await loadServices();
}

async function deleteRegistry(kind, name) {
  if (!(await confirmModal(t("confirm_sv_delete", name, kind)))) return;
  const d = await api(`/ui/api/registry/${encodeURIComponent(kind)}/delete`, { method: "POST", body: { name } });
  toast(d.ok ? t("toast_sv_deleted") : d.message, !!d.ok);
  if (d.ok) { closeDialog($("#sv-editor")); await loadServices(); }
}

document.addEventListener("DOMContentLoaded", () => {
  const fail = (x) => toast(x.message, false);
  $("#btn-sv-new").addEventListener("click", () => newRegistry().catch(fail));
  $("#sv-kind").addEventListener("change", () => { REG.kind = $("#sv-kind").value; regRenderForm(REG.kind, null); });
  $("#form-sv").addEventListener("submit", (e) => saveRegistry(e).catch(fail));
  $("#btn-sv-delete").addEventListener("click", () => deleteRegistry(REG.kind, REG.name).catch(fail));
});

/* OAuth sign-in for MCP servers (mcp_add auth="oauth"): each person's status, the
   Sign in / Sign out button in the services table, and the notice after the provider
   sends the person back (/ui?mcp_oauth=<result>#services). Tokens never reach the
   browser — the status says only whether you are signed in and until when. */
Object.assign(I18N.de, {
  oa_signin: "Anmelden", oa_signout: "Abmelden",
  oa_confirm: "Zur Anmeldung bei {0} für den Server {1} wechseln? Prüfe dort in der Adresszeile, dass es wirklich dein Anbieter ist.",
  oa_signed_in: "angemeldet", oa_signed_out: "nicht angemeldet",
  oa_renews: "verlängert sich selbst", oa_until: "gültig bis {0}",
  oa_ok: "Bei „{0}“ angemeldet.", oa_signed_out_toast: "Abgemeldet.",
  oa_state: "Anmeldung abgebrochen oder abgelaufen — bitte neu starten.",
  oa_session: "Bitte zuerst in AICortex anmelden, dann erneut starten.",
  oa_person: "Diese Anmeldung hat jemand anderes gestartet — abgelehnt.",
  oa_denied: "Der Anbieter hat den Zugriff nicht freigegeben.",
  oa_issuer: "Die Antwort kam von einem anderen Anbieter — abgelehnt.",
  oa_exchange: "Der Anbieter hat kein Token ausgestellt.",
  oa_vault: "Kein Verschlüsselungsschlüssel gesetzt — die Anmeldung wurde nicht gespeichert.",
  oa_error: "Anmeldung fehlgeschlagen.",
});
Object.assign(I18N.en, {
  oa_signin: "Sign in", oa_signout: "Sign out",
  oa_confirm: "Go to {0} to sign in for the server {1}? Check the address bar there to make sure it really is your provider.",
  oa_signed_in: "signed in", oa_signed_out: "not signed in",
  oa_renews: "renews itself", oa_until: "valid until {0}",
  oa_ok: "Signed in to “{0}”.", oa_signed_out_toast: "Signed out.",
  oa_state: "The sign-in was cancelled or timed out — please start again.",
  oa_session: "Please sign in to AICortex first, then start again.",
  oa_person: "Someone else started this sign-in — refused.",
  oa_denied: "The provider did not grant access.",
  oa_issuer: "The answer came from another provider — refused.",
  oa_exchange: "The provider did not issue a token.",
  oa_vault: "No encryption key is set — the sign-in was not stored.",
  oa_error: "Sign-in failed.",
});

let OAUTH = {};

async function oauthLoad() {
  OAUTH = {};
  if (!SERVICES.some((s) => s.kind === "mcp")) return;
  try {
    const d = await api("/ui/api/mcp-oauth");
    (d.servers || []).forEach((s) => { OAUTH[s.name] = s; });
  } catch (e) { /* the status is extra; the table renders without it */ }
}

function oauthCell(s, secretCell, actionCell) {
  const st = s.kind === "mcp" ? OAUTH[s.name] : null;
  if (!st) return;
  secretCell.textContent = ""; secretCell.className = "";
  const tag = document.createElement("span");
  tag.className = "tag " + (st.signed_in ? "ok" : "warn");
  tag.textContent = "OAuth · " + t(st.signed_in ? "oa_signed_in" : "oa_signed_out");
  if (st.signed_in) {
    tag.title = st.renews ? t("oa_renews")
      : st.expires_at ? t("oa_until", new Date(st.expires_at * 1000).toLocaleString()) : "";
  }
  secretCell.append(tag);
  const b = document.createElement("button");
  b.className = "btn sm " + (st.signed_in ? "ghost" : "primary");
  b.textContent = t(st.signed_in ? "oa_signout" : "oa_signin");
  b.addEventListener("click", () =>
    (st.signed_in ? oauthSignOut(s.name) : oauthSignIn(s.name)).catch((x) => toast(x.message, false)));
  actionCell.prepend(b);
}

async function oauthSignIn(name) {
  const d = await api("/ui/api/mcp-oauth/start", { method: "POST", body: { server: name } });
  if (!d.ok) { toast(d.message, false); return; }
  // Say where the sign-in happens before leaving: the provider's host and the server.
  if (!(await confirmModal(t("oa_confirm", d.login_host, d.server_url)))) return;
  location.assign(d.url);      // the provider's login page (https, checked by the server)
}

async function oauthSignOut(name) {
  const d = await api("/ui/api/mcp-oauth/logout", { method: "POST", body: { server: name } });
  toast(d.ok ? t("oa_signed_out_toast") : d.message, d.ok);
  await loadServices();
}

function oauthNotice() {
  const q = new URLSearchParams(location.search);
  const code = q.get("mcp_oauth");
  if (!code) return;
  const key = "oa_" + code;
  toast(code === "ok" ? t("oa_ok", q.get("server") || "") : (I18N.en[key] ? t(key) : t("oa_error")),
    code === "ok");
  history.replaceState(null, "", location.pathname + location.hash);
}

/* Vault: the hosts each secret may be sent to (host binding). An AI can't widen this —
   only a person here, or whoever re-enters the value. Rendered with textContent only. */
Object.assign(I18N.de, {
  vault_hosts: "Erlaubte Hosts", vault_hosts_save: "Hosts speichern",
  vault_hosts_ph: "z. B. api.example.com, 192.0.2.10",
  vault_hosts_hint: "Nur an diese Hosts wird das Secret gesendet. Eine KI kann das nicht ändern – nur du hier (oder wer den Wert neu eingibt).",
  vault_hosts_unbound: "Noch nicht gebunden – es wird nirgendwohin gesendet, bis du hier Hosts einträgst (oder die Integration unter „Services & Geräte“ speicherst).",
  vault_hosts_none: "An keinen Host gebunden – es wird nirgendwohin gesendet.",
  vault_hosts_line: "Hosts: {0}", vault_hosts_open: "Hosts: noch nicht gebunden",
});
Object.assign(I18N.en, {
  vault_hosts: "Allowed hosts", vault_hosts_save: "Save hosts",
  vault_hosts_ph: "e.g. api.example.com, 192.0.2.10",
  vault_hosts_hint: "The secret is only ever sent to these hosts. An AI can't change this — only you, here (or whoever re-enters the value).",
  vault_hosts_unbound: "Not bound yet — it is sent nowhere until you enter its hosts here (or save the integration under Services).",
  vault_hosts_none: "Bound to no host — it is sent nowhere.",
  vault_hosts_line: "Hosts: {0}", vault_hosts_open: "Hosts: not bound yet",
});

function hostsText(hosts) {
  if (hosts == null) return t("vault_hosts_open");
  return t("vault_hosts_line", hosts.length ? hosts.join(", ") : "—");
}

function decorateVaultRows() {
  $$("#vault-table tbody tr").forEach((tr, i) => {
    const s = SECRETS[i];
    if (!s || !tr.firstChild) return;
    const line = document.createElement("div");
    line.className = "dim small mono";
    line.textContent = hostsText(s.hosts);
    tr.firstChild.append(line);
  });
}

function hostsBox() {
  let box = $("#sec-hosts-box");
  if (box) return box;
  box = document.createElement("div");
  box.id = "sec-hosts-box";
  box.className = "hidden";
  const label = document.createElement("label");
  label.htmlFor = "sec-hosts";
  const hint = document.createElement("p");
  hint.className = "dim small"; hint.id = "sec-hosts-hint";
  const input = document.createElement("input");
  input.id = "sec-hosts"; input.className = "inp mono"; input.autocomplete = "off";
  const state = document.createElement("p");
  state.className = "dim small"; state.id = "sec-hosts-state";
  const save = document.createElement("button");
  save.type = "button"; save.className = "btn ghost sm"; save.id = "btn-sec-hosts";
  save.addEventListener("click", () => saveHosts().catch((e) => toast(t("toast_error", e.message), false)));
  box.append(label, hint, input, state, save);
  $("#form-secret").after(box);
  return box;
}

function renderHostsBox(s) {
  const box = hostsBox();
  box.classList.toggle("hidden", !s);
  if (s) {
    box.querySelector("label").textContent = t("vault_hosts");
    $("#sec-hosts-hint").textContent = t("vault_hosts_hint");
    $("#sec-hosts").placeholder = t("vault_hosts_ph");
    $("#sec-hosts").value = (s.hosts || []).join(", ");
    $("#sec-hosts-state").textContent = s.hosts == null ? t("vault_hosts_unbound")
      : (s.hosts.length ? "" : t("vault_hosts_none"));
    $("#btn-sec-hosts").textContent = t("vault_hosts_save");
  }
  // the hosts are stored on their own: what is shown now counts as unchanged
  dialogRebase($("#vault-editor"), $("#sec-hosts"));
}

async function saveHosts() {
  const s = VAULT_EDIT;
  if (!s) return;
  const hosts = $("#sec-hosts").value.split(/[\s,]+/).filter(Boolean);
  const r = await api("/ui/api/secrets/hosts", {
    method: "POST", body: { name: s.name, owner: s.owner || "", hosts } });
  toast(r.message, r.ok);
  if (!r.ok) return;
  await loadVault();
  const fresh = SECRETS.find((x) => x.name === s.name && (x.owner || "") === (s.owner || ""));
  if (fresh) { VAULT_EDIT = fresh; renderHostsBox(fresh); }
}

// Extend the vault view from app.js: rows get their hosts, the edit form a hosts box.
const vaultLoadBase = loadVault;
loadVault = async function () {
  await vaultLoadBase();
  decorateVaultRows();
};
VIEWS.vault = loadVault;
const vaultEditBase = editSecret;
editSecret = function (s) {
  vaultEditBase(s);
  renderHostsBox(s || null);
};
