/* Search (admin): one field that finds skills, tools and secrets at once.
   Results come from /ui/api/search. Secrets are matched by name and description
   only — their values never reach the browser. Rendering uses textContent only. */

Object.assign(I18N.de, {
  nav_search: "Suchen",
  se_hint: "Skills, Tools und Secrets auf einmal durchsuchen, nach Name und Beschreibung — auch auf Deutsch. Secrets nur nach Namen; ihre Werte bleiben im Vault.",
  se_ph: "Suchen … (z. B. drucken, paperless, docker)",
  se_skills: "Skills", se_tools: "Tools", se_secrets: "Secrets",
  se_none: "Keine Treffer.", se_short: "Mindestens 2 Zeichen eingeben.",
  se_shared: "geteilt", se_private: "privat: {0}",
});
Object.assign(I18N.en, {
  nav_search: "Search",
  se_hint: "Search skills, tools and secrets at once, by name and description. Secrets by name only; their values stay in the vault.",
  se_ph: "Search … (e.g. print, paperless, docker)",
  se_skills: "Skills", se_tools: "Tools", se_secrets: "Secrets",
  se_none: "No matches.", se_short: "Type at least 2 characters.",
  se_shared: "shared", se_private: "private: {0}",
});

let SEARCH_TIMER = null, SEARCH_SEQ = 0;

function seItem(title, tag, desc, onClick) {
  const it = document.createElement("div");
  it.className = "sk-item" + (onClick ? "" : " static");
  const n = document.createElement("div"); n.className = "n";
  const nm = document.createElement("span"); nm.textContent = title;
  n.append(nm);
  if (tag) {
    const tg = document.createElement("span"); tg.className = "tag"; tg.textContent = tag;
    n.append(tg);
  }
  const d = document.createElement("div"); d.className = "d"; d.textContent = desc || "";
  it.append(n, d);
  if (onClick) it.addEventListener("click", onClick);
  return it;
}

function seFill(group, rows, build) {
  const box = $(`#se-${group}`);
  box.textContent = "";
  $(`#se-${group}-n`).textContent = rows === null ? "" : `(${rows.length})`;
  const note = (key) => {
    const p = document.createElement("p"); p.className = "dim small"; p.textContent = t(key); box.append(p);
  };
  if (rows === null) return note("se_short");
  if (!rows.length) return note("se_none");
  rows.forEach((r) => box.append(build(r)));
}

function seOpenSkill(name) {
  location.hash = "#skills";                 // the router loads the library …
  openSkill(name).catch((e) => toast(t("toast_error", e.message), false));  // … and we open the hit
}

async function runSearch() {
  const q = $("#se-q").value.trim();
  const seq = ++SEARCH_SEQ;
  if (q.length < 2) {
    ["skills", "tools", "secrets"].forEach((g) => seFill(g, null));
    return;
  }
  const d = await api(`/ui/api/search?q=${encodeURIComponent(q)}`);
  if (seq !== SEARCH_SEQ) return;            // a newer query is already on its way
  seFill("skills", d.skills, (s) =>
    seItem(s.title || s.name, s.category, s.summary, () => seOpenSkill(s.name)));
  seFill("tools", d.tools, (tl) => {
    const it = seItem(tl.name, "", tl.summary, null);
    const sig = document.createElement("div"); sig.className = "sig"; sig.textContent = tl.signature;
    it.append(sig);
    return it;
  });
  seFill("secrets", d.secrets, (s) =>
    seItem(s.name, s.shared ? t("se_shared") : t("se_private", s.owner_label), s.description,
      () => { location.hash = "#vault"; }));
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
