/* Playbook runs — where each of a person's runs stands, its artifacts, and the
   human decision at every hand-off (approve / reject with a note). The library
   lives in view-playbook-lib.js. Nothing here uses innerHTML. */

Object.assign(I18N.de, {
  nav_playbooks: "Playbooks", pb_start: "Lauf starten", pb_tab_runs: "Läufe", pb_tab_lib: "Bibliothek",
  pb_project_ph: "Projekt", pb_st_active: "Aktiv", pb_st_done: "Fertig", pb_st_cancelled: "Abgebrochen",
  pb_st_all: "Alle", pb_runs_empty: "Keine Läufe. Starte einen aus der Bibliothek.",
  pb_f_playbook: "Playbook", pb_f_project: "Projekt", pb_f_title: "Titel (optional)", pb_go: "Starten",
  pb_next: "Als Nächstes", pb_goal: "Ziel", pb_approver: "Gibt frei", pb_appr_webui: "Freigabe in der WebUI",
  pb_appr_chat: "Freigabe auch im Chat", pb_appr_none: "ohne Freigabe",
  pb_version: "Version {0} · {1} · {2}", pb_older: "Frühere Versionen ({0})", pb_no_artifact: "Noch nichts eingereicht.",
  pb_note_ph: "Notiz zur Entscheidung (bei Ablehnung: was fehlt?)", pb_approve: "Freigeben", pb_reject: "Ablehnen",
  pb_submit_self: "Selbst einreichen", pb_f_artifact: "Inhalt", pb_f_ref_path: "Datei im Repo", pb_f_ref_commit: "Commit",
  pb_f_ref_url: "Link", pb_submit: "Einreichen", pb_cancel_run: "Lauf abbrechen", pb_decision: "{0} von {1} ({2}) {3}",
  pb_dec_approve: "Freigegeben", pb_dec_reject: "Abgelehnt", pb_status_pending: "offen", pb_status_submitted: "wartet",
  pb_status_approved: "frei", pb_status_rejected: "abgelehnt",
  confirm_pb_cancel: "Lauf „{0}“ abbrechen? Der Verlauf bleibt erhalten.", toast_pb_started: "Lauf gestartet.",
  toast_pb_decided: "Entscheidung gespeichert.", toast_pb_submitted: "Eingereicht.",
  pb_pos: "Phase {0}/{1} {2}", pb_result: "das Ergebnis", pb_ns_write: "{0}: {1} schreiben und einreichen",
  pb_ns_wait: "{0}: {1} wartet auf deine Freigabe", pb_ns_wait_chat: "{0}: {1} wartet auf Freigabe (hier oder im Chat)",
  pb_ns_rejected: "{0}: abgelehnt — überarbeiten und neu einreichen",
});
Object.assign(I18N.en, {
  nav_playbooks: "Playbooks", pb_start: "Start a run", pb_tab_runs: "Runs", pb_tab_lib: "Library",
  pb_project_ph: "Project", pb_st_active: "Active", pb_st_done: "Done", pb_st_cancelled: "Cancelled",
  pb_st_all: "All", pb_runs_empty: "No runs. Start one from the library.",
  pb_f_playbook: "Playbook", pb_f_project: "Project", pb_f_title: "Title (optional)", pb_go: "Start",
  pb_next: "Next", pb_goal: "Goal", pb_approver: "Approved by", pb_appr_webui: "approval in the WebUI",
  pb_appr_chat: "approval also in chat", pb_appr_none: "no approval",
  pb_version: "Version {0} · {1} · {2}", pb_older: "Earlier versions ({0})", pb_no_artifact: "Nothing submitted yet.",
  pb_note_ph: "Note on the decision (when rejecting: what is missing?)", pb_approve: "Approve", pb_reject: "Reject",
  pb_submit_self: "Submit it yourself", pb_f_artifact: "Content", pb_f_ref_path: "File in the repo", pb_f_ref_commit: "Commit",
  pb_f_ref_url: "Link", pb_submit: "Submit", pb_cancel_run: "Cancel run", pb_decision: "{0} by {1} ({2}) {3}",
  pb_dec_approve: "Approved", pb_dec_reject: "Rejected", pb_status_pending: "open", pb_status_submitted: "waiting",
  pb_status_approved: "approved", pb_status_rejected: "rejected",
  confirm_pb_cancel: "Cancel run “{0}”? Its history is kept.", toast_pb_started: "Run started.",
  toast_pb_decided: "Decision saved.", toast_pb_submitted: "Submitted.",
  pb_pos: "Stage {0}/{1} {2}", pb_result: "the result", pb_ns_write: "{0}: write {1} and submit it",
  pb_ns_wait: "{0}: {1} waits for your approval", pb_ns_wait_chat: "{0}: {1} waits for approval (here or in chat)",
  pb_ns_rejected: "{0}: rejected — rework and submit again",
});

let PB = { tab: "runs", runs: [], run: null, library: [], timer: null };

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

function safeLink(url) {
  try {
    const u = new URL(url);
    if (u.protocol !== "https:" && u.protocol !== "http:") return null;
    const a = el("a", "linkish", url); a.href = u.href; a.target = "_blank"; a.rel = "noopener noreferrer";
    return a;
  } catch (e) { return null; }
}

function pbNextStep(run) {
  if (run.status !== "active") return t(`pb_st_${run.status}`);
  const s = run.stages[run.current];
  const pos = t("pb_pos", run.current + 1, run.stages.length, s.name);
  const art = s.artifact || t("pb_result");
  if (s.status === "submitted") return t(s.approval === "chat" ? "pb_ns_wait_chat" : "pb_ns_wait", pos, art);
  if (s.status === "rejected") return t("pb_ns_rejected", pos);
  return t("pb_ns_write", pos, art);
}

function pbStamp(s) {
  const d = new Date(s);
  return isNaN(d) ? (s || "") : d.toLocaleString(LANG === "de" ? "de-DE" : "en-GB",
    { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function pbSteps(run) {
  const ol = el("ol", "pb-steps");
  run.stages.forEach((s, i) => {
    const li = el("li", `st-${s.status}` + (i === run.current && run.status === "active" ? " current" : ""));
    li.append(el("span", "n", s.name), el("span", "a mono", s.artifact || ""));
    li.title = t(`pb_status_${s.status}`);
    ol.append(li);
  });
  return ol;
}

async function loadPlaybooks() {
  $("#pb-tab-runs").classList.toggle("active", PB.tab === "runs");
  $("#pb-tab-lib").classList.toggle("active", PB.tab === "lib");
  $("#pb-runs").classList.toggle("hidden", PB.tab !== "runs");
  $("#pb-lib").classList.toggle("hidden", PB.tab !== "lib");
  PB.library = (await api("/ui/api/playbooks")).playbooks;
  if (PB.tab === "lib") return loadPlaybookLibrary();
  const p = new URLSearchParams({ project: $("#pb-project").value, status: $("#pb-status").value });
  PB.runs = (await api(`/ui/api/playbooks/runs?${p}`)).runs;
  const list = $("#pb-run-list");
  list.textContent = "";
  if (!PB.runs.length) list.append(el("p", "dim", t("pb_runs_empty")));
  PB.runs.forEach((r) => {
    const it = el("div", "sk-item" + (PB.run && PB.run.id === r.id ? " active" : ""));
    const head = el("div", "n");
    head.append(el("span", null, r.title), el("span", "tag", r.project || r.playbook));
    it.append(head, pbSteps(r), el("div", "d", pbNextStep(r)));
    it.addEventListener("click", () => openRun(r.id).catch((x) => toast(x.message, false)));
    list.append(it);
  });
}

function versionBlock(v) {
  const box = el("div", "pb-version");
  box.append(el("div", "dim small", t("pb_version", v.n, v.by || "–", pbStamp(v.at))));
  const ref = v.ref || {};
  if (ref.path || ref.commit) box.append(el("div", "mono small", `${ref.path || ""}${ref.commit ? " @ " + ref.commit : ""}`));
  if (ref.url) { const a = safeLink(ref.url); if (a) box.append(a); }
  if (v.note) box.append(el("div", "small", v.note));
  if (v.text) box.append(el("pre", "pb-artifact mono", v.text));
  return box;
}

async function openRun(id) {
  const d = await api(`/ui/api/playbooks/runs/get?id=${encodeURIComponent(id)}`);
  const run = d.run;
  PB.run = run;
  const box = $("#pb-run");
  box.textContent = "";
  box.classList.remove("hidden");
  box.append(el("h3", null, run.title));
  box.append(el("p", "dim small", [run.playbook_title || run.playbook, run.project,
    isAdmin() ? run.person : "", t(`pb_st_${run.status}`)].filter(Boolean).join(" · ")));
  box.append(pbSteps(run));
  const next = el("p", "pb-next");
  next.append(el("strong", null, `${t("pb_next")}: `), document.createTextNode(pbNextStep(run)));
  box.append(next);

  run.stages.forEach((s, i) => {
    const isCurrent = i === run.current && run.status === "active";
    if (!isCurrent && !s.versions.length) return;
    const sec = el(isCurrent ? "section" : "details", "pb-stage" + (isCurrent ? " current" : ""));
    const title = el(isCurrent ? "h4" : "summary", null,
      `${i + 1}. ${s.name}${s.artifact ? " → " + s.artifact : ""} · ${t(`pb_status_${s.status}`)}`);
    sec.append(title);
    if (s.goal) sec.append(el("p", "small", `${t("pb_goal")}: ${s.goal}`));
    sec.append(el("p", "dim small", [s.approver ? `${t("pb_approver")}: ${s.approver}` : "",
      t(`pb_appr_${s.approval}`)].filter(Boolean).join(" · ")));
    if (s.decision) {
      const dec = s.decision;
      sec.append(el("p", "small", t("pb_decision", t(`pb_dec_${dec.decision}`), dec.by || "–", dec.via,
        dec.note ? `— ${dec.note}` : "")));
    }
    if (!s.versions.length) sec.append(el("p", "dim small", t("pb_no_artifact")));
    else {
      sec.append(versionBlock(s.versions[s.versions.length - 1]));
      if (s.versions.length > 1) {
        const old = el("details", "pb-older");
        old.append(el("summary", "small", t("pb_older", s.versions.length - 1)));
        s.versions.slice(0, -1).reverse().forEach((v) => old.append(versionBlock(v)));
        sec.append(old);
      }
    }
    if (isCurrent && s.status === "submitted") sec.append(decisionForm(run));
    if (isCurrent) sec.append(submitForm(run, s));
    box.append(sec);
  });
  if (run.status === "active") {
    const row = el("div", "btn-row");
    row.append(el("span", "spacer"));
    const cancel = el("button", "btn danger sm", t("pb_cancel_run"));
    cancel.type = "button";
    cancel.addEventListener("click", async () => {
      if (!(await confirmModal(t("confirm_pb_cancel", run.title)))) return;
      const r = await api("/ui/api/playbooks/runs/cancel", { method: "POST", body: { id: run.id } });
      toast(r.message, !!r.ok);
      await loadPlaybooks(); await openRun(run.id);
    });
    row.append(cancel);
    box.append(row);
  }
  await loadPlaybooks();
}

function decisionForm(run) {
  const wrap = el("div", "pb-decide");
  const note = el("textarea", "inp"); note.rows = 3; note.placeholder = t("pb_note_ph");
  note.setAttribute("aria-label", t("pb_note_ph"));
  const row = el("div", "btn-row");
  [["approve", "primary"], ["reject", "danger"]].forEach(([decision, cls]) => {
    const btn = el("button", `btn ${cls}`, t(`pb_${decision}`)); btn.type = "button";
    btn.addEventListener("click", async () => {
      const r = await api("/ui/api/playbooks/runs/decide",
        { method: "POST", body: { id: run.id, decision, note: note.value } });
      toast(r.ok ? t("toast_pb_decided") : r.message, !!r.ok);
      if (r.ok) await openRun(run.id);
    });
    row.append(btn);
  });
  wrap.append(note, row);
  return wrap;
}

function submitForm(run, stage) {
  const det = el("details", "pb-submit");
  det.append(el("summary", "small", t("pb_submit_self")));
  const text = el("textarea", "inp mono"); text.rows = 8; text.spellcheck = false;
  text.value = stage.versions.length ? stage.versions[stage.versions.length - 1].text : stage.template || "";
  const fields = [["artifact", text]];
  [["ref_path", "text"], ["ref_commit", "text"], ["ref_url", "url"]].forEach(([k, type]) => {
    const inp = el("input", "inp mono"); inp.type = type; fields.push([k, inp]);
  });
  fields.forEach(([k, input]) => {
    const id = `pb-sub-${k}`; input.id = id;
    const label = el("label", null, t(`pb_f_${k}`)); label.htmlFor = id;
    det.append(label, input);
  });
  const btn = el("button", "btn primary", t("pb_submit")); btn.type = "button";
  btn.addEventListener("click", async () => {
    const body = { id: run.id };
    fields.forEach(([k, input]) => { body[k] = input.value; });
    const r = await api("/ui/api/playbooks/runs/submit", { method: "POST", body });
    toast(r.ok ? t("toast_pb_submitted") : r.message, !!r.ok);
    if (r.ok) await openRun(run.id);
  });
  det.append(btn);
  return det;
}

function openStartForm(playbook) {
  const sel = $("#pb-f-playbook");
  sel.textContent = "";
  PB.library.forEach((p) => { const o = el("option", null, p.title); o.value = p.name; sel.append(o); });
  if (playbook) sel.value = playbook;
  PB.tab = "runs";
  loadPlaybooks().catch((x) => toast(x.message, false));
  $("#pb-start-form").classList.remove("hidden");
  $("#pb-f-project").focus();
}

VIEWS.playbooks = loadPlaybooks;

document.addEventListener("DOMContentLoaded", () => {
  const fail = (x) => toast(x.message, false);
  $("#pb-tab-runs").addEventListener("click", () => { PB.tab = "runs"; loadPlaybooks().catch(fail); });
  $("#pb-tab-lib").addEventListener("click", () => { PB.tab = "lib"; loadPlaybooks().catch(fail); });
  const refresh = () => { clearTimeout(PB.timer); PB.timer = setTimeout(() => loadPlaybooks().catch(fail), 250); };
  $("#pb-project").addEventListener("input", refresh);
  $("#pb-status").addEventListener("change", refresh);
  $("#btn-pb-start").addEventListener("click", () => openStartForm(""));
  $("#btn-pb-start-close").addEventListener("click", () => $("#pb-start-form").classList.add("hidden"));
  $("#form-pb-start").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api("/ui/api/playbooks/runs/start", { method: "POST", body: {
        playbook: $("#pb-f-playbook").value, project: $("#pb-f-project").value, title: $("#pb-f-title").value } });
      if (!r.ok) { toast(r.message, false); return; }
      toast(t("toast_pb_started"));
      $("#pb-start-form").classList.add("hidden");
      $("#form-pb-start").reset();
      await openRun(r.id);
    } catch (x) { fail(x); }
  });
});
