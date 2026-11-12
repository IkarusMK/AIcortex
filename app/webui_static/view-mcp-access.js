/* Who may use your sign-in to an OAuth MCP server (the Access dialog of its row in
   Services & devices — a form dialog like every other, dialogs.js), and the calls that
   wait for your approval (a panel above the services table). Only your own settings and
   approvals — nobody else's. Rendered with textContent/createElement only; no inline
   styles. */
"use strict";

Object.assign(I18N.de, {
  ma_access: "Zugriff",
  ma_title: "Zugriff auf deine Anmeldung bei {0}",
  ma_hint: "Wer darf deine Anmeldung benutzen? Du selbst hier in der WebUI immer — Apps, Runner und Agent-Schlüssel nur, wenn du sie hier ankreuzt.",
  ma_apps: "Apps", ma_runners: "Runner (für deine geplanten Jobs)", ma_keys: "Agent-Schlüssel",
  ma_none: "— noch keine —", ma_local: "Verbindung ohne Anmeldung (lokal)",
  ma_writes: "Aufrufe, die etwas ändern können, erst nach meiner Freigabe ausführen",
  ma_save: "Speichern", ma_saved: "Zugriff gespeichert.",
  ma_approvals: "Freigaben",
  ma_approvals_hint: "Diese Aufrufe könnten Daten ändern und warten auf dich. Erst nach „Freigeben“ führt AICortex genau diesen Aufruf einmal mit deiner Anmeldung aus.",
  ma_approve: "Freigeben", ma_reject: "Ablehnen",
  ma_meta: "angefragt über {0} · vor {1} min · läuft ab um {2}",
  ma_done: "Freigegeben und ausgeführt.", ma_rejected: "Abgelehnt.",
});
Object.assign(I18N.en, {
  ma_access: "Access",
  ma_title: "Access to your sign-in to {0}",
  ma_hint: "Who may use your sign-in? You, here in the WebUI, always — apps, runners and agent keys only when you tick them here.",
  ma_apps: "Apps", ma_runners: "Runners (for your scheduled jobs)", ma_keys: "Agent keys",
  ma_none: "— none yet —", ma_local: "Connection without sign-in (local)",
  ma_writes: "Run calls that may change something only after my approval",
  ma_save: "Save", ma_saved: "Access saved.",
  ma_approvals: "Approvals",
  ma_approvals_hint: "These calls may change data and wait for you. Only after “Approve” does AICortex run exactly this call once with your sign-in.",
  ma_approve: "Approve", ma_reject: "Reject",
  ma_meta: "asked via {0} · {1} min ago · expires at {2}",
  ma_done: "Approved and run.", ma_rejected: "Rejected.",
});

const MA_GROUPS = [["apps", "allow_apps"], ["runners", "allow_runners"], ["keys", "allow_keys"]];

/* The Access button in an OAuth server's row (called from oauthCell, view-registry.js). */
function mcpAccessButton(s, actionCell) {
  const b = document.createElement("button");
  b.className = "btn sm ghost";
  b.textContent = t("ma_access");
  b.addEventListener("click", () => mcpAccessOpen(s.name).catch((x) => toast(x.message, false)));
  actionCell.prepend(b);
}

function mcpAccessItems(box, choices, chosen) {
  box.textContent = "";
  if (!choices.length) {
    const p = document.createElement("p"); p.className = "dim small"; p.textContent = t("ma_none");
    box.append(p);
    return;
  }
  choices.forEach((c) => {
    const label = document.createElement("label"); label.className = "check";
    const input = document.createElement("input"); input.type = "checkbox";
    input.value = c.id; input.checked = chosen.includes(c.id);
    const text = document.createElement("span"); text.className = "mono";
    text.textContent = c.id === "(local)" ? t("ma_local") : c.label;
    label.append(input, text);
    box.append(label);
  });
}

async function mcpAccessOpen(name) {
  const d = await api(`/ui/api/mcp-oauth/access?server=${encodeURIComponent(name)}`);
  const dlg = $("#oa-access");
  dlg.dataset.server = d.server;
  $("#oa-access-title").textContent = t("ma_title", d.server);
  MA_GROUPS.forEach(([group, key]) =>
    mcpAccessItems($(`#oa-access-${group} .oa-items`), d.choices[group] || [], d.settings[key] || []));
  $("#oa-access-writes").checked = d.settings.approve_writes !== false;
  openDialog($("#oa-access"));               // these values are the unchanged state
}

async function mcpAccessSave(ev) {
  ev.preventDefault();
  const dlg = $("#oa-access");
  const body = { server: dlg.dataset.server, approve_writes: $("#oa-access-writes").checked };
  MA_GROUPS.forEach(([group, key]) => {
    body[key] = $$(`#oa-access-${group} input[type=checkbox]`).filter((i) => i.checked).map((i) => i.value);
  });
  const d = await api("/ui/api/mcp-oauth/access", { method: "POST", body });
  toast(d.ok ? t("ma_saved") : d.message, !!d.ok);
  if (d.ok) closeDialog(dlg);
}

/* Waiting approvals: a panel above the services table, shown only when there are any. */
function mcpApprovalsBox() {
  let box = $("#oa-approvals");
  if (box) return box;
  box = document.createElement("div");
  box.id = "oa-approvals";
  box.className = "panel accent oa-approvals hidden";
  const h = document.createElement("h3"); h.textContent = t("ma_approvals");
  const hint = document.createElement("p"); hint.className = "dim small"; hint.textContent = t("ma_approvals_hint");
  const list = document.createElement("div"); list.id = "oa-approvals-list";
  box.append(h, hint, list);
  $("#sv-table").closest(".panel").before(box);
  return box;
}

function mcpApprovalRow(a, now) {
  const row = document.createElement("div"); row.className = "oa-approval";
  const head = document.createElement("div");
  const tool = document.createElement("strong"); tool.className = "mono"; tool.textContent = a.tool;
  const on = document.createElement("span"); on.className = "tag"; on.textContent = a.server;
  head.append(tool, " ", on);
  const meta = document.createElement("p"); meta.className = "dim small";
  meta.textContent = t("ma_meta", a.requested_by, Math.max(0, Math.round((now - a.created) / 60)),
    new Date(a.expires * 1000).toLocaleTimeString());
  const args = document.createElement("pre"); args.className = "oa-args"; args.textContent = a.args;
  const btns = document.createElement("div"); btns.className = "btn-row";
  [["ma_approve", "", true], ["ma_reject", "ghost", false]].forEach(([key, cls, approve]) => {
    const b = document.createElement("button");
    b.className = `btn sm ${cls}`.trim(); b.textContent = t(key);
    b.addEventListener("click", () => mcpDecide(a.id, approve).catch((x) => toast(x.message, false)));
    btns.append(b);
  });
  row.append(head, meta, args, btns);
  return row;
}

async function mcpApprovalsLoad() {
  if (!$("#sv-table")) return;
  let d;
  try { d = await api("/ui/api/mcp-oauth/approvals"); } catch (e) { return; }   // extra, not essential
  const box = mcpApprovalsBox();
  const list = $("#oa-approvals-list");
  list.textContent = "";
  (d.approvals || []).forEach((a) => list.append(mcpApprovalRow(a, d.now)));
  if (d.message) {               // the approvals can't be read: say so, never show "none"
    const p = document.createElement("p"); p.className = "notice"; p.textContent = d.message;
    list.append(p);
  }
  box.classList.toggle("hidden", !(d.approvals || []).length && !d.message);
}

async function mcpDecide(id, approve) {
  const d = await api("/ui/api/mcp-oauth/approvals/decide", { method: "POST", body: { id, approve } });
  toast(d.ok ? t(approve ? "ma_done" : "ma_rejected") : d.message, !!d.ok);
  await mcpApprovalsLoad();
}

// After the services list (app.js) loads, show the waiting approvals too.
const svLoadBase = loadServices;
loadServices = async function () {
  await svLoadBase();
  await mcpApprovalsLoad();
};
VIEWS.services = loadServices;

document.addEventListener("DOMContentLoaded", () => {
  if (!$("#oa-access")) return;
  // × / Esc / Cancel: dialogs.js, with "Discard changes?" once something was ticked
  $("#form-oa-access").addEventListener("submit", (e) => mcpAccessSave(e).catch((x) => toast(x.message, false)));
});
