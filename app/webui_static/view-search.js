/* Search (admin): one field that finds everything the WebUI holds — skills, tools,
   secrets, services & devices, cron jobs, runners, playbooks and runs, notes, clients
   and users. Results come from /ui/api/search; secrets, runner and client keys never
   reach the browser. Rendering uses textContent only. */

Object.assign(I18N.de, {
  nav_search: "Suchen",
  se_hint: "Durchsucht alles auf einmal: Skills, Tools, Secrets, Services & Geräte, Cron-Jobs, Runner, Playbooks, Notizen, Clients und Benutzer — nach Name, Beschreibung und Inhalt, auch auf Deutsch. Secrets und Schlüssel nur nach Namen; ihre Werte bleiben im Vault.",
  se_ph: "Suchen … (z. B. drucken, paperless, docker, backup)",
  se_none: "Keine Treffer.", se_short: "Mindestens 2 Zeichen eingeben.", se_count: "{0} Treffer",
  se_shared: "geteilt", se_private: "privat: {0}", se_on: "an", se_off: "aus",
  se_g_skills: "Skills", se_g_tools: "Tools", se_g_secrets: "Secrets",
  se_g_services: "Services & Geräte", se_g_cron: "Cron-Jobs", se_g_runners: "Runner",
  se_g_playbooks: "Playbooks", se_g_runs: "Playbook-Läufe", se_g_notes: "Notizen",
  se_g_clients: "Clients", se_g_users: "Benutzer",
});
Object.assign(I18N.en, {
  nav_search: "Search",
  se_hint: "Searches everything at once: skills, tools, secrets, services & devices, cron jobs, runners, playbooks, notes, clients and users — by name, description and content. Secrets and keys by name only; their values stay in the vault.",
  se_ph: "Search … (e.g. print, paperless, docker, backup)",
  se_none: "No matches.", se_short: "Type at least 2 characters.", se_count: "{0} matches",
  se_shared: "shared", se_private: "private: {0}", se_on: "on", se_off: "off",
  se_g_skills: "Skills", se_g_tools: "Tools", se_g_secrets: "Secrets",
  se_g_services: "Services & devices", se_g_cron: "Cron jobs", se_g_runners: "Runners",
  se_g_playbooks: "Playbooks", se_g_runs: "Playbook runs", se_g_notes: "Notes",
  se_g_clients: "Clients", se_g_users: "Users",
});

let SEARCH_TIMER = null, SEARCH_SEQ = 0;
const onOff = (on) => t(on ? "se_on" : "se_off");

// group → [tab a hit opens (null: none), (row) → {title, tag, desc, sig, open}]
const SE_GROUPS = [
  ["skills", "#skills", (r) => ({ title: r.title || r.name, tag: r.category, desc: r.summary,
    open: () => openSkill(r.name).catch((e) => toast(t("toast_error", e.message), false)) })],
  ["tools", null, (r) => ({ title: r.name, desc: r.summary, sig: r.signature })],
  ["secrets", "#vault", (r) => ({ title: r.name, desc: r.description,
    tag: r.shared ? t("se_shared") : t("se_private", r.owner_label) })],
  ["services", "#services", (r) => ({ title: r.name, tag: r.kind, desc: r.description,
    sig: [r.target, r.secret].filter(Boolean).join(" · ") })],
  ["cron", "#cron", (r) => ({ title: r.name, tag: r.schedule, desc: r.summary,
    sig: [r.owner, onOff(r.enabled)].filter(Boolean).join(" · ") })],
  ["runners", "#runners", (r) => ({ title: r.name, tag: r.kind,
    desc: [r.owner, onOff(r.enabled)].filter(Boolean).join(" · ") })],
  ["playbooks", "#playbooks", (r) => ({ title: r.title || r.name, tag: r.category, desc: r.summary })],
  ["runs", "#playbooks", (r) => ({ title: r.title || r.playbook_title, tag: r.status,
    desc: [r.playbook_title, r.project, r.person].filter(Boolean).join(" · ") })],
  ["notes", "#memory", (r) => ({ title: r.title, tag: r.type,
    desc: [r.project, r.summary].filter(Boolean).join(" — ") })],
  ["clients", "#clients", (r) => ({ title: r.name || r.keyid, tag: r.status, desc: r.owner })],
  ["users", "#users", (r) => ({ title: r.identity, tag: r.role, desc: r.note })],
];

function seItem(v, tab) {
  const it = document.createElement("div");
  it.className = "sk-item" + (v.open || tab ? "" : " static");
  const n = document.createElement("div"); n.className = "n";
  const nm = document.createElement("span"); nm.textContent = v.title || "";
  n.append(nm);
  if (v.tag) {
    const tg = document.createElement("span"); tg.className = "tag"; tg.textContent = v.tag;
    n.append(tg);
  }
  const d = document.createElement("div"); d.className = "d"; d.textContent = v.desc || "";
  it.append(n, d);
  if (v.sig) {
    const s = document.createElement("div"); s.className = "sig"; s.textContent = v.sig;
    it.append(s);
  }
  if (tab) {
    it.addEventListener("click", () => {
      location.hash = tab;                     // the router loads that tab …
      if (v.open) v.open();                    // … and a skill opens right away
    });
  }
  return it;
}

function seStatus(text) {
  $("#se-status").textContent = text;
}

async function runSearch() {
  const q = $("#se-q").value.trim();
  const seq = ++SEARCH_SEQ;
  const box = $("#se-results");
  if (q.length < 2) {
    box.textContent = "";
    seStatus(t("se_short"));
    return;
  }
  const d = await api(`/ui/api/search?q=${encodeURIComponent(q)}`);
  if (seq !== SEARCH_SEQ) return;              // a newer query is already on its way
  box.textContent = "";
  let total = 0;
  SE_GROUPS.forEach(([group, tab, view]) => {
    const rows = d[group] || [];
    if (!rows.length) return;                  // only groups with hits, to keep it scannable
    total += rows.length;
    const panel = document.createElement("div"); panel.className = "panel";
    const h = document.createElement("h3");
    h.textContent = `${t("se_g_" + group)} (${rows.length})`;
    panel.append(h);
    rows.forEach((r) => panel.append(seItem(view(r), tab)));
    box.append(panel);
  });
  seStatus(total ? t("se_count", total) : t("se_none"));
}

async function loadSearch() {
  const inp = $("#se-q");
  if (!inp.dataset.bound) {
    inp.dataset.bound = "1";
    inp.addEventListener("input", () => {
      clearTimeout(SEARCH_TIMER);
      SEARCH_TIMER = setTimeout(
        () => runSearch().catch((e) => toast(t("toast_error", e.message), false)), 250);
    });
  }
  inp.focus();
  await runSearch();
}

VIEWS.search = loadSearch;
ADMIN_VIEWS.add("search");
