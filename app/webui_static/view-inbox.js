/* Inbox — the messages for "user" and "all" that are the person's own: cron job
   reports, webhook events, notes from their agents. Newest first; a message can be
   marked read. Above the list: whether new messages are also forwarded as a push
   (rules are set by the AI with inbox_forward_set and only send once the person
   approves them here) and how the last deliveries went. */

Object.assign(I18N.de, {
  nav_inbox: "Postfach",
  inbox_hint: "Berichte deiner Cron-Jobs, Webhook-Meldungen und Nachrichten deiner Agenten. Neueste zuerst.",
  inbox_empty: "Noch keine Nachrichten.",
  inbox_unread: "{0} ungelesen",
  inbox_from: "von {0}",
  inbox_ack: "Gelesen",
  inbox_fw_none: "Keine Weiterleitung: Neue Nachrichten stehen nur hier. Als Push aufs Handy (z. B. ntfy oder Gotify) richtet sie deine KI mit inbox_forward_set ein.",
  inbox_fw_rule: "Weitergeleitet: „{0}“ → {1} ({2}) {3}",
  inbox_fw_pending: "Wartet auf deine Freigabe: „{0}“ → {1} ({2}). Erst nach der Freigabe geht etwas dorthin.",
  inbox_fw_approve: "Freigeben",
  toast_inbox_approved: "Weiterleitung freigegeben.",
  inbox_fw_off: "aus",
  inbox_last_ok: "Letzte Zustellung {0}: ok",
  inbox_last_bad: "Letzte Zustellung {0} fehlgeschlagen: {1}",
  toast_inbox_read: "Als gelesen markiert.",
});
Object.assign(I18N.en, {
  nav_inbox: "Inbox",
  inbox_hint: "Reports of your cron jobs, webhook events and messages from your agents. Newest first.",
  inbox_empty: "No messages yet.",
  inbox_unread: "{0} unread",
  inbox_from: "from {0}",
  inbox_ack: "Mark read",
  inbox_fw_none: "No forwarding: new messages only show here. Your AI sets up a push to your phone (e.g. ntfy or Gotify) with inbox_forward_set.",
  inbox_fw_rule: "Forwarded: “{0}” → {1} ({2}) {3}",
  inbox_fw_pending: "Waiting for your approval: “{0}” → {1} ({2}). Nothing goes there until you approve it.",
  inbox_fw_approve: "Approve",
  toast_inbox_approved: "Forwarding approved.",
  inbox_fw_off: "off",
  inbox_last_ok: "Last delivery {0}: ok",
  inbox_last_bad: "Last delivery {0} failed: {1}",
  toast_inbox_read: "Marked as read.",
});

function inboxStamp(s) {
  const d = new Date(s);
  return isNaN(d) ? (s || "") : d.toLocaleString(LANG === "de" ? "de-DE" : "en-GB",
    { weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function inboxForwarding(d) {
  const box = $("#inbox-forward");
  box.textContent = "";
  const line = (text, cls) => { const p = document.createElement("p"); p.className = `small ${cls || ""}`; p.textContent = text; box.append(p); };
  if (!d.forwarding.length) { line(t("inbox_fw_none"), "dim"); return; }
  d.forwarding.forEach((r) => {
    if (r.approved) {
      line(t("inbox_fw_rule", r.recipient, r.service, r.host || "?", r.enabled ? "" : `(${t("inbox_fw_off")})`));
      return;
    }
    line(t("inbox_fw_pending", r.recipient, r.service, r.host || "?"), "notice");
    if (!isAdmin()) return;
    const btn = document.createElement("button");
    btn.className = "btn primary sm";
    btn.textContent = t("inbox_fw_approve");
    btn.addEventListener("click", () => approveForward(r.recipient).catch((x) => toast(x.message, false)));
    box.append(btn);
  });
  const last = d.deliveries[0];
  if (last) line(last.ok ? t("inbox_last_ok", inboxStamp(last.ts)) : t("inbox_last_bad", inboxStamp(last.ts), last.info),
    last.ok ? "dim" : "notice");
}

function inboxItem(m) {
  const li = document.createElement("li");
  li.className = `inbox-item${m.read ? " read" : ""}`;
  const head = document.createElement("div");
  head.className = "inbox-head";
  const title = document.createElement("strong");
  title.textContent = m.subject || (m.body || "").split("\n").find((x) => x.trim()) || "—";
  const meta = document.createElement("span");
  meta.className = "dim small";
  meta.textContent = `${inboxStamp(m.ts)} · ${t("inbox_from", m.from || "?")}`;
  head.append(title, meta);
  if (!m.read) {
    const btn = document.createElement("button");
    btn.className = "btn sm";
    btn.textContent = t("inbox_ack");
    btn.addEventListener("click", () => ackInbox(m.id).catch((x) => toast(x.message, false)));
    head.append(btn);
  }
  const body = document.createElement("pre");
  body.className = "inbox-body";
  body.textContent = m.body || "";
  li.append(head, body);
  return li;
}

async function loadInbox() {
  const d = await api("/ui/api/inbox");
  inboxForwarding(d);
  $("#inbox-unread").textContent = d.unread ? t("inbox_unread", d.unread) : "";
  const list = $("#inbox-list");
  list.textContent = "";
  d.messages.forEach((m) => list.append(inboxItem(m)));
  $("#inbox-empty").classList.toggle("hidden", d.messages.length > 0);
}

async function ackInbox(id) {
  const r = await api("/ui/api/inbox/ack", { method: "POST", body: { id } });
  if (r.ok) { toast(t("toast_inbox_read")); await loadInbox(); } else toast(r.message, false);
}

async function approveForward(recipient) {
  const r = await api("/ui/api/inbox/forward/approve", { method: "POST", body: { recipient } });
  if (r.ok) { toast(t("toast_inbox_approved")); await loadInbox(); } else toast(r.message, false);
}

VIEWS.inbox = loadInbox;
