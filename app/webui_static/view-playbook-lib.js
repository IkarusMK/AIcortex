/* Playbook library — read a playbook (stages, plays, guidance), adopt the bundled
   AI-native SDLC template, and edit playbooks with a stage form. Changing a
   shared playbook is for admins and its author; the server enforces that. */

Object.assign(I18N.de, {
  pb_example: "Vorlage „AI-native SDLC“ übernehmen", pb_new: "Neues Playbook", pb_lib_empty: "Die Bibliothek ist leer.",
  pb_stages: "Phasen", pb_plays: "Bausteine (Plays)", pb_guidance: "Anleitung", pb_source: "Quelle",
  pb_no_prereq: "ohne Voraussetzung", pb_needs: "braucht: {0}", pb_template: "Vorlage",
  pb_edit: "Bearbeiten", pb_run_this: "Lauf starten", pb_edit_title: "Playbook bearbeiten", pb_new_title: "Neues Playbook",
  pb_f_name: "Name", pb_f_title2: "Titel", pb_f_desc: "Beschreibung", pb_f_category: "Kategorie", pb_f_source: "Quelle (Link)",
  pb_f_stage_name: "Phase", pb_f_stage_artifact: "Ergebnis-Datei", pb_f_stage_goal: "Ziel",
  pb_f_stage_approval: "Freigabe", pb_f_stage_approver: "Wer gibt frei", pb_f_stage_template: "Vorlage für das Ergebnis",
  pb_f_plays: "Bausteine (YAML oder JSON, optional)", pb_f_guidance: "Anleitung",
  pb_add_stage: "+ Phase", pb_up: "↑", pb_down: "↓", pb_remove: "Entfernen",
  confirm_pb_delete: "Playbook „{0}“ löschen? Laufende Läufe bleiben erhalten.", toast_pb_saved: "Playbook gespeichert.",
});
Object.assign(I18N.en, {
  pb_example: "Adopt the “AI-native SDLC” template", pb_new: "New playbook", pb_lib_empty: "The library is empty.",
  pb_stages: "Stages", pb_plays: "Plays", pb_guidance: "Guidance", pb_source: "Source",
  pb_no_prereq: "no prerequisites", pb_needs: "needs: {0}", pb_template: "Template",
  pb_edit: "Edit", pb_run_this: "Start a run", pb_edit_title: "Edit playbook", pb_new_title: "New playbook",
  pb_f_name: "Name", pb_f_title2: "Title", pb_f_desc: "Description", pb_f_category: "Category", pb_f_source: "Source (link)",
  pb_f_stage_name: "Stage", pb_f_stage_artifact: "Artifact file", pb_f_stage_goal: "Goal",
  pb_f_stage_approval: "Approval", pb_f_stage_approver: "Approved by", pb_f_stage_template: "Template for the artifact",
  pb_f_plays: "Plays (YAML or JSON, optional)", pb_f_guidance: "Guidance",
  pb_add_stage: "+ Stage", pb_up: "↑", pb_down: "↓", pb_remove: "Remove",
  confirm_pb_delete: "Delete playbook “{0}”? Existing runs are kept.", toast_pb_saved: "Playbook saved.",
});

let PBL = { current: null, stages: [] };

async function loadPlaybookLibrary() {
  const list = $("#pb-lib-list");
  list.textContent = "";
  $("#btn-pb-example").classList.toggle("hidden", PB.library.some((p) => p.name === "ai-native-sdlc"));
  if (!PB.library.length) list.append(el("p", "dim", t("pb_lib_empty")));
  PB.library.forEach((p) => {
    const it = el("div", "sk-item" + (PBL.current === p.name ? " active" : ""));
    const head = el("div", "n");
    head.append(el("span", null, p.title), el("span", "tag", `${p.stages} · ${p.category}`));
    it.append(head, el("div", "d", p.description));
    it.addEventListener("click", () => openPlaybook(p.name).catch((x) => toast(x.message, false)));
    list.append(it);
  });
}

async function openPlaybook(name) {
  const d = await api(`/ui/api/playbooks/get?name=${encodeURIComponent(name)}`);
  PBL.current = d.name;
  const box = $("#pb-def");
  box.textContent = "";
  box.classList.remove("hidden");
  box.append(el("h3", null, d.title), el("p", "small", d.description));
  if (d.source) {
    const p = el("p", "dim small", `${t("pb_source")}: `);
    const a = safeLink(d.source);
    p.append(a || document.createTextNode(d.source));
    box.append(p);
  }
  const row = el("div", "btn-row");
  const run = el("button", "btn primary sm", t("pb_run_this")); run.type = "button";
  run.addEventListener("click", () => openStartForm(d.name));
  const edit = el("button", "btn ghost sm", t("pb_edit")); edit.type = "button";
  edit.addEventListener("click", () => editPlaybook(d));
  const del = el("button", "btn danger sm", t("delete")); del.type = "button";
  del.addEventListener("click", async () => {
    if (!(await confirmModal(t("confirm_pb_delete", d.title)))) return;
    const r = await api("/ui/api/playbooks/delete", { method: "POST", body: { name: d.name } });
    toast(r.message, !!r.ok);
    if (r.ok) { box.classList.add("hidden"); PBL.current = null; await loadPlaybooks(); }
  });
  row.append(run, edit, el("span", "spacer"), del);
  box.append(row);

  box.append(el("h4", null, t("pb_stages")));
  const ol = el("ol", "pb-def-stages");
  d.stages.forEach((s) => {
    const li = el("li");
    li.append(el("strong", null, s.name), document.createTextNode(s.artifact ? ` → ${s.artifact}` : ""));
    if (s.goal) li.append(el("p", "small", s.goal));
    li.append(el("p", "dim small", [s.approver, t(`pb_appr_${s.approval}`)].filter(Boolean).join(" · ")));
    if (s.template) {
      const det = el("details");
      det.append(el("summary", "small", t("pb_template")), el("pre", "pb-artifact mono", s.template));
      li.append(det);
    }
    ol.append(li);
  });
  box.append(ol);
  if (d.plays.length) {
    box.append(el("h4", null, t("pb_plays")));
    const ul = el("ul", "pb-plays");
    d.plays.forEach((p) => {
      const li = el("li", "small");
      li.append(el("span", "tag", p.stage || "–"), document.createTextNode(` ${p.title} `),
        el("span", p.requires.length ? "dim" : "badge cyan",
          p.requires.length ? t("pb_needs", p.requires.join(", ")) : t("pb_no_prereq")));
      ul.append(li);
    });
    box.append(ul);
  }
  if (d.guidance) box.append(el("h4", null, t("pb_guidance")), el("div", "pb-guidance small", d.guidance));
  await loadPlaybookLibrary();
}

function stageFieldset(s, i) {
  const fs = el("fieldset", "pb-stage-edit");
  const grid = el("div", "form-fields");          // name | artifact, approval | approver
  fs.append(el("legend", null, `${i + 1}`), grid);
  const add = (key, input, full) => {
    const id = `pbs-${i}-${key}`; input.id = id; input.dataset.key = key;
    const label = el("label", null, t(`pb_f_stage_${key}`)); label.htmlFor = id;
    const cell = el("div", full ? "fld full" : "fld");
    cell.append(label, input);
    grid.append(cell);
  };
  const text = (v) => { const x = el("input", "inp"); x.value = v || ""; return x; };
  add("name", text(s.name)); add("artifact", text(s.artifact));
  const goal = el("textarea", "inp"); goal.rows = 3; goal.value = s.goal || ""; add("goal", goal, true);
  const appr = el("select", "inp");
  ["webui", "chat", "none"].forEach((v) => { const o = el("option", null, t(`pb_appr_${v}`)); o.value = v; appr.append(o); });
  appr.value = s.approval || "webui"; add("approval", appr);
  add("approver", text(s.approver));
  const tpl = el("textarea", "inp mono"); tpl.rows = 4; tpl.spellcheck = false; tpl.value = s.template || "";
  add("template", tpl, true);
  const row = el("div", "btn-row");
  [["pb_up", -1], ["pb_down", 1]].forEach(([key, dir]) => {
    const b = el("button", "btn ghost sm", t(key)); b.type = "button";
    b.addEventListener("click", () => moveStage(i, dir)); row.append(b);
  });
  const rm = el("button", "btn danger sm", t("pb_remove")); rm.type = "button";
  rm.addEventListener("click", () => { collectStages(); PBL.stages.splice(i, 1); renderStageForm(); });
  row.append(el("span", "spacer"), rm);
  fs.append(row);
  return fs;
}

function collectStages() {
  PBL.stages = $$("#pb-stage-forms fieldset").map((fs, i) => {
    const s = { id: (PBL.stages[i] || {}).id || "" };
    fs.querySelectorAll("[data-key]").forEach((x) => { s[x.dataset.key] = x.value; });
    return s;
  });
}

function moveStage(i, dir) {
  collectStages();
  const j = i + dir;
  if (j < 0 || j >= PBL.stages.length) return;
  [PBL.stages[i], PBL.stages[j]] = [PBL.stages[j], PBL.stages[i]];
  renderStageForm();
}

function renderStageForm() {
  const box = $("#pb-stage-forms");
  box.textContent = "";
  PBL.stages.forEach((s, i) => box.append(stageFieldset(s, i)));
}

function editPlaybook(d) {
  $("#form-pb-def").reset();
  $("#pb-def-editor-title").textContent = t(d ? "pb_edit_title" : "pb_new_title");
  $("#pbd-name").value = d ? d.name : ""; $("#pbd-name").readOnly = !!d;
  $("#pbd-title").value = d ? d.title : "";
  $("#pbd-desc").value = d ? d.description : "";
  $("#pbd-category").value = d ? d.category : "";
  $("#pbd-source").value = d ? d.source : "";
  $("#pbd-plays").value = d && d.plays.length ? JSON.stringify(d.plays, null, 1) : "";
  $("#pbd-guidance").value = d ? d.guidance : "";
  PBL.stages = d ? d.stages.map((s) => ({ ...s })) : [{ approval: "webui" }];
  renderStageForm();
  openDialog($("#pb-def-editor"));
}

async function savePlaybook(ev) {
  ev.preventDefault();
  collectStages();
  const body = { name: $("#pbd-name").value, title: $("#pbd-title").value, description: $("#pbd-desc").value,
                 category: $("#pbd-category").value, source: $("#pbd-source").value,
                 plays: $("#pbd-plays").value, guidance: $("#pbd-guidance").value, stages: PBL.stages };
  const r = await api("/ui/api/playbooks", { method: "POST", body });
  if (!r.ok) { toast(r.message, false); return; }
  toast(t("toast_pb_saved"));
  closeDialog($("#pb-def-editor"));
  PB.library = (await api("/ui/api/playbooks")).playbooks;
  const name = (r.message.match(/'([^']+)'/) || [])[1];
  if (name) await openPlaybook(name);
}

document.addEventListener("DOMContentLoaded", () => {
  const fail = (x) => toast(x.message, false);
  $("#btn-pb-example").addEventListener("click", async () => {
    try {
      const r = await api("/ui/api/playbooks/example", { method: "POST", body: {} });
      toast(r.message, !!r.ok);
      PB.library = (await api("/ui/api/playbooks")).playbooks;
      if (r.ok) await openPlaybook("ai-native-sdlc");
    } catch (x) { fail(x); }
  });
  $("#btn-pb-new").addEventListener("click", () => editPlaybook(null));
  $("#btn-pb-add-stage").addEventListener("click", () => { collectStages(); PBL.stages.push({ approval: "webui" }); renderStageForm(); });
  $("#form-pb-def").addEventListener("submit", (e) => savePlaybook(e).catch(fail));
});
