/* Settings an admin changes here instead of in .env (since 2.7.1): the mail recipient
   allow-list, the default tool profile and auto-memory. A form dialog like every
   other (dialogs.js); textContent/createElement only, no inline styles. */
"use strict";
Object.assign(I18N.de, {
  st_open: "Einstellungen",
  st_title: "Einstellungen",
  st_mail: "Erlaubte Mail-Empfänger",
  st_mail_hint: "Adressen, @domain oder domain, durch Komma getrennt. Leer = alle Empfänger erlaubt.",
  st_profile: "Werkzeug-Profil für Apps",
  st_profile_hint: "core: die elf wichtigsten Werkzeuge, der Rest über integration_search. full: alle.",
  st_learn: "Automatisches Lernen: dauerhafte Fakten als Vorschläge sammeln",
  st_save: "Speichern", st_saved: "Einstellungen gespeichert.",
});
Object.assign(I18N.en, {
  st_open: "Settings",
  st_title: "Settings",
  st_mail: "Allowed mail recipients",
  st_mail_hint: "Addresses, @domain or domain, comma-separated. Empty = any recipient.",
  st_profile: "Tool profile for apps",
  st_profile_hint: "core: the eleven main tools, the rest via integration_search. full: all.",
  st_learn: "Auto-memory: collect durable facts as candidates",
  st_save: "Save", st_saved: "Settings saved.",
});

async function settingsOpen() {
  const d = await api("/ui/api/settings");
  const sel = $("#st-profile");
  sel.textContent = "";
  (d.profiles || ["core", "full"]).forEach((p) => {
    const o = document.createElement("option"); o.value = p; o.textContent = p; sel.append(o);
  });
  $("#st-mail").value = d.settings.mail_allowed_recipients || "";
  sel.value = d.settings.tool_profile;
  $("#st-learn").checked = !!d.settings.learn_autocapture;
  openDialog($("#st-editor"), { focus: $("#st-mail") });
}

async function settingsSave(ev) {
  ev.preventDefault();
  const d = await api("/ui/api/settings", { method: "POST", body: {
    mail_allowed_recipients: $("#st-mail").value,
    tool_profile: $("#st-profile").value,
    learn_autocapture: $("#st-learn").checked,
  } });
  if (!d.ok) { toast(d.message, false); return; }
  toast(t("st_saved"));
  closeDialog($("#st-editor"));
}

document.addEventListener("DOMContentLoaded", () => {
  if (!$("#st-editor")) return;
  $("#btn-settings").addEventListener("click", () => settingsOpen().catch((x) => toast(x.message, false)));
  $("#form-settings").addEventListener("submit", (e) => settingsSave(e).catch((x) => toast(x.message, false)));
});
