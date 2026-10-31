/* Notes view — a person's own memories (admins: the shared scope) plus, for
   admins, the review queue of auto-captured candidates. Same rules as the
   memory_* tools; the server confines every call to the person's area. */

Object.assign(I18N.de, {
  nav_memory: "Notizen", mem_new: "Neue Notiz", mem_search_ph: "Suchen …",
  mem_project_ph: "Projekt", mem_type_all: "Alle Typen", mem_tab_notes: "Notizen",
  mem_tab_cand: "Kandidaten", mem_new_title: "Neue Notiz", mem_edit_title: "Notiz bearbeiten",
  mem_f_title: "Titel", mem_f_type: "Typ", mem_f_project: "Projekt", mem_f_tags: "Schlagworte",
  mem_f_review: "Prüfen am", mem_f_body: "Inhalt",
  mem_type_user: "Person", mem_type_feedback: "Arbeitsweise", mem_type_project: "Projekt",
  mem_type_reference: "Nachschlagen",
  mem_meta: "Version {0} · geändert {1} · von {2}",
  mem_conflict: "Diese Notiz wurde inzwischen geändert. Lade sie neu und übernimm deine Änderung dann erneut.",
  mem_reload: "Neu laden", mem_empty: "Noch keine Notizen.", mem_cand_empty: "Keine Kandidaten.",
  mem_promote: "Übernehmen", mem_reject: "Verwerfen", confirm_mem_delete: "Notiz „{0}“ löschen?",
  toast_mem_saved: "Gespeichert (Version {0}).", toast_mem_deleted: "Notiz gelöscht.",
});
Object.assign(I18N.en, {
  nav_memory: "Notes", mem_new: "New note", mem_search_ph: "Search …",
  mem_project_ph: "Project", mem_type_all: "All types", mem_tab_notes: "Notes",
  mem_tab_cand: "Candidates", mem_new_title: "New note", mem_edit_title: "Edit note",
  mem_f_title: "Title", mem_f_type: "Type", mem_f_project: "Project", mem_f_tags: "Tags",
  mem_f_review: "Review on", mem_f_body: "Content",
  mem_type_user: "Person", mem_type_feedback: "Way of working", mem_type_project: "Project",
  mem_type_reference: "Reference",
  mem_meta: "Version {0} · changed {1} · by {2}",
  mem_conflict: "This note was changed in the meantime. Reload it, then apply your change again.",
  mem_reload: "Reload", mem_empty: "No notes yet.", mem_cand_empty: "No candidates.",
  mem_promote: "Keep", mem_reject: "Discard", confirm_mem_delete: "Delete note “{0}”?",
  toast_mem_saved: "Saved (version {0}).", toast_mem_deleted: "Note deleted.",
});

const MEM_TYPES = ["user", "feedback", "project", "reference"];
let MEM = { notes: [], current: null, version: 0, tab: "notes", timer: null };

async function loadMemory() {
  $("#mem-tab-cand").classList.toggle("hidden", ME.role !== "admin");
  $("#mem-tab-notes").classList.toggle("active", MEM.tab === "notes");
  $("#mem-tab-cand").classList.toggle("active", MEM.tab === "cand");
  $("#mem-notes").classList.toggle("hidden", MEM.tab !== "notes");
  $("#mem-cand").classList.toggle("hidden", MEM.tab !== "cand");
  if (MEM.tab === "cand") return loadCandidates();
  const p = new URLSearchParams({ q: $("#mem-q").value, type: $("#mem-type").value,
                                  project: $("#mem-project").value });
  const d = await api(`/ui/api/memory?${p}`);
  MEM.notes = d.notes;
  renderMemList();
  const dl = $("#mem-proj-list");
  dl.textContent = "";
  [...new Set(MEM.notes.map((n) => n.project).filter(Boolean))].sort().forEach((v) => {
    const o = document.createElement("option"); o.value = v; dl.append(o);
  });
}

function renderMemList() {
  const list = $("#mem-list");
  list.textContent = "";
  if (!MEM.notes.length) {
    const e = document.createElement("p"); e.className = "dim"; e.textContent = t("mem_empty");
    list.append(e);
  }
  MEM.notes.forEach((n) => {
    const it = document.createElement("div");
    it.className = "sk-item" + (MEM.current === n.name ? " active" : "");
    const head = document.createElement("div"); head.className = "n";
    const title = document.createElement("span"); title.textContent = n.title;
    const tag = document.createElement("span"); tag.className = "tag";
    tag.textContent = t(`mem_type_${n.type || "reference"}`) + (n.project ? ` · ${n.project}` : "");
    head.append(title, tag);
    const line = document.createElement("div"); line.className = "d";
    line.textContent = n.snippet || n.first;
    it.append(head, line);
    it.addEventListener("click", () => openMemory(n.name));
    list.append(it);
  });
}

function fillTypeSelect(sel, withAll) {
  sel.textContent = "";
  if (withAll) {
    const o = document.createElement("option"); o.value = ""; o.textContent = t("mem_type_all");
    sel.append(o);
  }
  MEM_TYPES.forEach((v) => {
    const o = document.createElement("option"); o.value = v; o.textContent = t(`mem_type_${v}`);
    sel.append(o);
  });
}

async function openMemory(name) {
  const d = await api(`/ui/api/memory/get?name=${encodeURIComponent(name)}`);
  MEM.current = d.name;
  MEM.version = parseInt(d.meta.version || "0", 10);
  $("#mem-editor-title").textContent = t("mem_edit_title");
  $("#mem-title").value = d.meta.title || d.name;
  $("#mem-title").readOnly = true;             // a new title would be a new note
  $("#mem-f-type").value = d.meta.type || "reference";
  $("#mem-f-project").value = d.meta.project || "";
  $("#mem-f-tags").value = (d.meta.tags || []).join(", ");
  $("#mem-f-review").value = (d.meta.review || "").slice(0, 10);
  $("#mem-body").value = d.body;
  $("#mem-meta").textContent = t("mem_meta", MEM.version, d.meta.updated || "–", d.meta.author || "–");
  $("#mem-conflict").classList.add("hidden");
  $("#btn-mem-delete").classList.remove("hidden");
  $("#mem-editor").classList.remove("hidden");
  renderMemList();
}

function newMemory() {
  MEM.current = null; MEM.version = 0;
  $("#form-mem").reset();
  $("#mem-title").readOnly = false;
  $("#mem-editor-title").textContent = t("mem_new_title");
  $("#mem-meta").textContent = "";
  $("#mem-conflict").classList.add("hidden");
  $("#btn-mem-delete").classList.add("hidden");
  $("#mem-editor").classList.remove("hidden");
  $("#mem-title").focus();
}

async function saveMemory(ev) {
  ev.preventDefault();
  const body = { title: $("#mem-title").value, type: $("#mem-f-type").value,
                 project: $("#mem-f-project").value, tags: $("#mem-f-tags").value,
                 review: $("#mem-f-review").value, content: $("#mem-body").value };
  if (MEM.current) body.expected_version = MEM.version;
  const d = await api("/ui/api/memory", { method: "POST", body });
  if (!d.ok) {
    if (/changed/i.test(d.message || "")) { $("#mem-conflict").classList.remove("hidden"); return; }
    toast(d.message, false); return;
  }
  toast(t("toast_mem_saved", d.version));
  await loadMemory();
  await openMemory(d.name);
}

async function deleteMemory() {
  if (!(await confirmModal(t("confirm_mem_delete", $("#mem-title").value)))) return;
  const d = await api("/ui/api/memory/delete", { method: "POST", body: { name: MEM.current } });
  if (!d.ok) { toast(d.message, false); return; }
  toast(t("toast_mem_deleted"));
  MEM.current = null;
  $("#mem-editor").classList.add("hidden");
  await loadMemory();
}

async function loadCandidates() {
  const d = await api("/ui/api/memory/candidates");
  const box = $("#mem-cand-list");
  box.textContent = "";
  if (!d.candidates.length) {
    const e = document.createElement("p"); e.className = "dim"; e.textContent = t("mem_cand_empty");
    box.append(e);
  }
  d.candidates.forEach((c) => {
    const it = document.createElement("div"); it.className = "sk-item";
    const head = document.createElement("div"); head.className = "n";
    const title = document.createElement("span"); title.textContent = c.title;
    const tag = document.createElement("span"); tag.className = "tag"; tag.textContent = c.source;
    head.append(title, tag);
    const line = document.createElement("div"); line.className = "d"; line.textContent = c.first;
    const row = document.createElement("div"); row.className = "btn-row";
    [["promote", "primary"], ["reject", "ghost"]].forEach(([action, cls]) => {
      const b = document.createElement("button");
      b.className = `btn sm ${cls}`; b.textContent = t(`mem_${action}`);
      b.addEventListener("click", async () => {
        const r = await api(`/ui/api/memory/candidates/${action}`,
                            { method: "POST", body: { name: c.name } });
        toast(r.message, !!r.ok);
        loadCandidates();
      });
      row.append(b);
    });
    it.append(head, line, row);
    box.append(it);
  });
}

VIEWS.memory = loadMemory;

document.addEventListener("DOMContentLoaded", () => {
  fillTypeSelect($("#mem-type"), true);
  fillTypeSelect($("#mem-f-type"), false);
  $("#btn-mem-new").addEventListener("click", newMemory);
  $("#form-mem").addEventListener("submit", (e) => saveMemory(e).catch((x) => toast(x.message, false)));
  $("#btn-mem-close").addEventListener("click", () => $("#mem-editor").classList.add("hidden"));
  $("#btn-mem-delete").addEventListener("click", () => deleteMemory().catch((x) => toast(x.message, false)));
  $("#btn-mem-reload").addEventListener("click", () => openMemory(MEM.current));
  const refresh = () => { clearTimeout(MEM.timer); MEM.timer = setTimeout(() => loadMemory().catch((x) => toast(x.message, false)), 250); };
  ["#mem-q", "#mem-project"].forEach((s) => $(s).addEventListener("input", refresh));
  $("#mem-type").addEventListener("change", refresh);
  $("#mem-tab-notes").addEventListener("click", () => { MEM.tab = "notes"; loadMemory(); });
  $("#mem-tab-cand").addEventListener("click", () => { MEM.tab = "cand"; loadMemory(); });
});
