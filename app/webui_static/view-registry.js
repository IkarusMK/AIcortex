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
  $("#sv-editor").classList.remove("hidden");
  $("#sv-editor").scrollIntoView({ behavior: "smooth", block: "nearest" });
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
  $("#sv-editor").classList.remove("hidden");
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
  $("#sv-editor").classList.add("hidden");
  await loadServices();
}

async function deleteRegistry(kind, name) {
  if (!(await confirmModal(t("confirm_sv_delete", name, kind)))) return;
  const d = await api(`/ui/api/registry/${encodeURIComponent(kind)}/delete`, { method: "POST", body: { name } });
  toast(d.ok ? t("toast_sv_deleted") : d.message, !!d.ok);
  if (d.ok) { $("#sv-editor").classList.add("hidden"); await loadServices(); }
}

document.addEventListener("DOMContentLoaded", () => {
  const fail = (x) => toast(x.message, false);
  $("#btn-sv-new").addEventListener("click", () => newRegistry().catch(fail));
  $("#sv-kind").addEventListener("change", () => { REG.kind = $("#sv-kind").value; regRenderForm(REG.kind, null); });
  $("#form-sv").addEventListener("submit", (e) => saveRegistry(e).catch(fail));
  $("#btn-sv-close").addEventListener("click", () => $("#sv-editor").classList.add("hidden"));
  $("#btn-sv-delete").addEventListener("click", () => deleteRegistry(REG.kind, REG.name).catch(fail));
});
