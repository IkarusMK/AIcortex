/* Runners — who carries out a person's scheduled jobs: a Claude cloud task AICortex
   wakes, the ChatGPT plan, an API key, or an OpenAI-compatible address (Ollama).
   Everyone manages their own; admins see all, choose the standard runner and sign
   in with ChatGPT. Keys go to the vault and never come back — only names. */

Object.assign(I18N.de, {
  nav_runners: "Runner", rn_new: "Neuer Runner",
  rn_hint: "Ein Runner führt deine Cron-Jobs aus. AICortex prüft jede Minute ohne Modell, ob ein Job fällig ist — erst dann weckt es Claude oder fragt das Modell des Runners. Jobs ohne eigenen Runner laufen beim Standard-Runner.",
  rn_col_name: "Name", rn_col_kind: "Art", rn_col_state: "Status", rn_col_on: "An", rn_col_owner: "Gehört",
  rn_empty: "Noch kein Runner — lege einen an, damit deine Jobs laufen.",
  rn_standard: "Standard", rn_beat_none: "Kein Runner an — Jobs laufen erst, wenn einer eingeschaltet ist.",
  rn_beat_ok: "AICortex prüft jede Minute, zuletzt {0}.", rn_beat_stale: "AICortex hat seit {0} nicht mehr geprüft.",
  rn_beat_waiting: "AICortex prüft ab der nächsten Minute.",
  rn_new_title: "Neuer Runner", rn_edit_title: "Runner bearbeiten",
  rn_f_kind: "Art", rn_f_name: "Name", rn_f_model: "Modell", rn_f_url: "Adresse (OpenAI-kompatibel)",
  rn_f_trigger: "API-Adresse der Cloud-Aufgabe (oder ihre trig_…-ID)", rn_f_enabled: "Eingeschaltet",
  "rn_k_claude-cloud": "Claude (Cloud-Aufgabe wecken)", rn_k_chatgpt: "ChatGPT-Abo",
  rn_k_anthropic: "Anthropic-API-Schlüssel", rn_k_openai: "OpenAI-API-Schlüssel", rn_k_openrouter: "OpenRouter",
  rn_k_gemini: "Google Gemini", rn_k_compatible: "OpenAI-kompatibel (eigene Adresse, z. B. Ollama)",
  "rn_h_claude-cloud": "AICortex weckt eine Cloud-Aufgabe in deinem Claude-Konto und schickt den fälligen Job mit. Claude läuft mit deinem Abo und erreicht AICortex über deinen Connector — auch wenn dein Rechner aus ist.",
  rn_h_chatgpt: "AICortex fragt ein Modell deines ChatGPT-Abos („Sign in with ChatGPT“). Bei Plus teilen sich alle Apps das 5-Stunden-Limit.",
  rn_h_key: "AICortex fragt das Modell mit deinem API-Schlüssel; abgerechnet wird beim Anbieter.",
  rn_h_compatible: "Für Ollama, LM Studio und andere OpenAI-kompatible Server. Kleine lokale Modelle verhaspeln sich oft bei Werkzeugen — teste den Runner mit einem Job.",
  rn_cl_step1: "Auf claude.ai/code/routines eine neue Aufgabe anlegen, als Auslöser „API“ wählen und den AICortex-Connector eingeschaltet lassen.",
  rn_cl_step2: "Als Auftrag genau diesen Satz eintragen — einmalig, er gilt für alle Jobs:",
  rn_cl_step3: "Beim API-Auslöser „Generate token“ klicken und die API-Adresse und den Token unten eintragen — beide stehen nur einmal da.",
  rn_cl_rights: "Ein Job dieses Runners läuft in deinem Claude-Konto — mit deinen vollen Rechten in AICortex und all deinen claude.ai-Connectoren. Deshalb führt er nur Jobs aus, die du selbst geschrieben hast.",
  rn_url_hint: "Mit /v1 am Ende, z. B. http://192.168.1.20:11434/v1. Heimnetz-Adressen müssen in INTERNAL_ALLOW_CIDRS freigegeben sein.",
  rn_m_claude: "Optional, leer = claude-sonnet-5-5; Kurzformen wie sonnet, opus oder haiku gehen auch.",
  rn_m_chatgpt: "Optional, leer = das erste Modell deines Kontos.", rn_m_api: "Modellname wie beim Anbieter angegeben.",
  rn_f_key: "Schlüssel", rn_f_token: "Token der Cloud-Aufgabe", rn_f_key_opt: "Schlüssel (falls der Server einen will)",
  rn_src_new: "Neu eingeben", rn_src_keep: "Gespeicherten behalten ({0})", rn_src_vault: "Aus dem Vault: {0}",
  rn_src_shared: "{0} (gemeinsam)", rn_key_hidden: "Wird verschlüsselt gespeichert und nie wieder angezeigt.",
  rn_s_off: "aus", rn_s_waiting: "prüft ab der nächsten Minute", rn_s_idle: "zuletzt geprüft {0}, nichts fällig",
  rn_s_ran: "{0}: {1}", "rn_s_no-key": "kein brauchbarer Schlüssel/Token gespeichert",
  "rn_s_no-signin": "noch nicht mit ChatGPT angemeldet", rn_s_error: "Fehler ({0}) — {1}",
  rn_session: "Claude-Sitzung öffnen ↗", rn_test: "Testen", rn_make_std: "Als Standard",
  confirm_rn_test_cloud: "Claude jetzt zum Test wecken? Das startet eine kurze Sitzung in deinem Konto.",
  confirm_rn_delete: "Runner „{0}“ löschen? Seine Jobs laufen danach beim Standard-Runner.",
  toast_rn_saved: "Runner gespeichert.", toast_rn_deleted: "Runner gelöscht.",
  rn_cg_out: "Noch nicht mit ChatGPT angemeldet.", rn_cg_in: "Mit ChatGPT angemeldet als {0}.",
  rn_cg_noplan: "Die Nutzung deines ChatGPT-Abos ist nicht erlaubt — melde dich erneut an und erlaube sie.",
  rn_cg_again: "Die ChatGPT-Anmeldung ist abgelaufen oder wurde getrennt — bitte neu anmelden.",
  rn_cg_start: "Mit ChatGPT anmelden", rn_cg_signout: "Abmelden",
  rn_cg_step1: "1. Öffne die Anmeldung, melde dich mit deinem ChatGPT-Konto an und erlaube AICortex die Nutzung deines Abos.",
  rn_cg_open: "ChatGPT-Anmeldung öffnen ↗",
  rn_cg_step2: "2. Danach zeigt der Browser eine Fehlerseite von 127.0.0.1 — das ist richtig so. Kopiere die komplette Adresse aus der Adressleiste hier hinein.",
  rn_cg_url: "Adresse nach der Anmeldung", rn_cg_finish: "Anmeldung abschließen",
  confirm_rn_cg_signout: "Von ChatGPT abmelden? Der ChatGPT-Runner kann dann keine Jobs mehr ausführen.",
});
Object.assign(I18N.en, {
  nav_runners: "Runners", rn_new: "New runner",
  rn_hint: "A runner carries out your scheduled jobs. AICortex checks every minute without any model whether a job is due — only then does it wake Claude or ask the runner's model. Jobs without a runner of their own go to the standard runner.",
  rn_col_name: "Name", rn_col_kind: "Kind", rn_col_state: "State", rn_col_on: "On", rn_col_owner: "Owner",
  rn_empty: "No runner yet — set one up so your jobs run.",
  rn_standard: "Standard", rn_beat_none: "No runner is on — jobs only run once one is switched on.",
  rn_beat_ok: "AICortex checks every minute, last at {0}.", rn_beat_stale: "AICortex hasn't checked since {0}.",
  rn_beat_waiting: "AICortex checks from the next minute.",
  rn_new_title: "New runner", rn_edit_title: "Edit runner",
  rn_f_kind: "Kind", rn_f_name: "Name", rn_f_model: "Model", rn_f_url: "Address (OpenAI-compatible)",
  rn_f_trigger: "The cloud task's API address (or its trig_… id)", rn_f_enabled: "Switched on",
  "rn_k_claude-cloud": "Claude (wake a cloud task)", rn_k_chatgpt: "ChatGPT plan",
  rn_k_anthropic: "Anthropic API key", rn_k_openai: "OpenAI API key", rn_k_openrouter: "OpenRouter",
  rn_k_gemini: "Google Gemini", rn_k_compatible: "OpenAI-compatible (own address, e.g. Ollama)",
  "rn_h_claude-cloud": "AICortex wakes a cloud task in your Claude account and sends the due job along. Claude runs on your plan and reaches AICortex through your connector — even while your computer is off.",
  rn_h_chatgpt: "AICortex asks a model on your ChatGPT plan (“Sign in with ChatGPT”). On Plus, all apps share the 5-hour limit.",
  rn_h_key: "AICortex asks the model with your API key; the provider bills you.",
  rn_h_compatible: "For Ollama, LM Studio and other OpenAI-compatible servers. Small local models often stumble over tools — test the runner with a job.",
  rn_cl_step1: "On claude.ai/code/routines create a new task, choose “API” as its trigger and leave the AICortex connector on.",
  rn_cl_step2: "Enter exactly this sentence as its prompt — once, it covers every job:",
  rn_cl_step3: "Click “Generate token” on the API trigger and enter the API address and the token below — both are shown only once.",
  rn_cl_rights: "A job of this runner runs in your Claude account — with your full rights in AICortex and all your claude.ai connectors. That is why it only runs jobs you wrote yourself.",
  rn_url_hint: "Ending in /v1, e.g. http://192.168.1.20:11434/v1. LAN addresses must be allowed in INTERNAL_ALLOW_CIDRS.",
  rn_m_claude: "Optional, empty = claude-sonnet-5-5; short names like sonnet, opus or haiku work too.",
  rn_m_chatgpt: "Optional, empty = your account's first model.", rn_m_api: "Model name as the provider gives it.",
  rn_f_key: "Key", rn_f_token: "The cloud task's token", rn_f_key_opt: "Key (if the server wants one)",
  rn_src_new: "Enter a new one", rn_src_keep: "Keep the stored one ({0})", rn_src_vault: "From the vault: {0}",
  rn_src_shared: "{0} (shared)", rn_key_hidden: "Stored encrypted and never shown again.",
  rn_s_off: "off", rn_s_waiting: "checks from the next minute", rn_s_idle: "last checked {0}, nothing due",
  rn_s_ran: "{0}: {1}", "rn_s_no-key": "no usable key/token stored",
  "rn_s_no-signin": "not signed in with ChatGPT yet", rn_s_error: "error ({0}) — {1}",
  rn_session: "Open the Claude session ↗", rn_test: "Test", rn_make_std: "Make standard",
  confirm_rn_test_cloud: "Wake Claude now for a test? This starts a short session in your account.",
  confirm_rn_delete: "Delete runner “{0}”? Its jobs then go to the standard runner.",
  toast_rn_saved: "Runner saved.", toast_rn_deleted: "Runner deleted.",
  rn_cg_out: "Not signed in with ChatGPT yet.", rn_cg_in: "Signed in with ChatGPT as {0}.",
  rn_cg_noplan: "Using your ChatGPT plan isn't allowed — sign in again and allow it.",
  rn_cg_again: "The ChatGPT sign-in expired or was disconnected — please sign in again.",
  rn_cg_start: "Sign in with ChatGPT", rn_cg_signout: "Sign out",
  rn_cg_step1: "1. Open the sign-in, sign in with your ChatGPT account and allow AICortex to use your plan.",
  rn_cg_open: "Open ChatGPT sign-in ↗",
  rn_cg_step2: "2. Your browser then shows an error page from 127.0.0.1 — that's expected. Copy the whole address from the address bar into this field.",
  rn_cg_url: "Address after signing in", rn_cg_finish: "Complete sign-in",
  confirm_rn_cg_signout: "Sign out of ChatGPT? The ChatGPT runner can then no longer run jobs.",
});

let RUNNERS = { list: [], data: {}, names: [], editing: null };
const rnKind = () => $("#rn-kind").value;

function rnStateCell(rec) {
  const td = document.createElement("td");
  td.className = "small";
  const s = rec.state || { state: "waiting" };
  const text = document.createElement("span");
  text.textContent = t(`rn_s_${s.state}`, cronStamp(s.at), s.detail || "");
  if (["no-key", "no-signin", "error"].includes(s.state)) text.className = "err";
  td.append(text);
  if (String(s.link || "").startsWith("https://claude.ai/")) {
    const a = document.createElement("a");
    a.href = s.link; a.target = "_blank"; a.rel = "noopener noreferrer";
    a.className = "rn-link"; a.textContent = t("rn_session");
    td.append(document.createElement("br"), a);
  }
  if (rec.note) {
    const note = document.createElement("div");
    note.className = "dim"; note.textContent = rec.note;
    td.append(note);
  }
  return td;
}

function rnButton(key, cls, fn) {
  const b = document.createElement("button");
  b.className = `btn sm ${cls}`; b.textContent = t(key);
  b.addEventListener("click", () => fn().catch((x) => toast(x.message, false)));
  return b;
}

function rnRow(rec) {
  const tr = document.createElement("tr");
  const name = document.createElement("td");
  name.className = "rn-name"; name.textContent = rec.name;
  if (rec.standard) {
    const tag = document.createElement("span");
    tag.className = "tag ok"; tag.textContent = t("rn_standard");
    name.append(tag);
  }
  const kind = document.createElement("td");
  kind.className = "small"; kind.textContent = t(`rn_k_${rec.kind}`);
  const on = document.createElement("td");
  const box = document.createElement("input");
  box.type = "checkbox"; box.checked = !!rec.enabled;
  box.disabled = !(rec.mine || isAdmin());
  box.setAttribute("aria-label", `${t("rn_col_on")}: ${rec.name}`);
  box.addEventListener("change", () => rnPost("/ui/api/runners/enabled", { id: rec.id, enabled: box.checked })
    .catch((x) => toast(x.message, false)));
  on.append(box);
  tr.append(name, kind, rnStateCell(rec), on);
  if (isAdmin()) {
    const owner = document.createElement("td");
    owner.className = "mono small"; owner.textContent = rec.owner || "–";
    tr.append(owner);
  }
  const act = document.createElement("td");
  act.className = "actions";
  if (rec.mine) act.append(rnButton("rn_test", "ghost", () => testRunner(rec)), rnButton("sv_edit", "ghost", () => openRunner(rec)));
  if (isAdmin() && rec.mine && !rec.standard && rec.kind !== "claude-cloud") {
    act.append(rnButton("rn_make_std", "ghost", () => rnPost("/ui/api/runners/standard", { id: rec.id })));
  }
  if ((rec.mine || isAdmin()) && !rec.standard) act.append(rnButton("delete", "danger", () => deleteRunner(rec)));
  tr.append(act);
  return tr;
}

function showBeat(d) {
  const el = $("#rn-beat");
  const beat = d.heartbeat || {};
  const anyOn = (d.runners || []).some((r) => r.enabled);
  const waiting = anyOn && !beat.last_seen;
  el.textContent = !anyOn ? t("rn_beat_none") : waiting ? t("rn_beat_waiting")
    : beat.ok ? t("rn_beat_ok", cronStamp(beat.last_seen)) : t("rn_beat_stale", cronStamp(beat.last_seen));
  el.classList.toggle("notice", !anyOn || (!beat.ok && !waiting));
  el.classList.toggle("dim", anyOn && (!!beat.ok || waiting));
  el.classList.remove("hidden");
}

async function loadRunners() {
  const d = await api("/ui/api/runners");
  RUNNERS.data = d; RUNNERS.list = d.runners || [];
  showBeat(d);
  const tbody = $("#rn-table tbody");
  tbody.textContent = "";
  $("#rn-empty").classList.toggle("hidden", RUNNERS.list.length > 0);
  RUNNERS.list.forEach((rec) => tbody.append(rnRow(rec)));
  showChatgpt(d.chatgpt);
}

async function rnPost(path, body) {
  const d = await api(path, { method: "POST", body });
  toast(d.message, !!d.ok);
  await loadRunners();
  return d;
}

function fillKinds(current) {
  const sel = $("#rn-kind");
  sel.textContent = "";
  const d = RUNNERS.data;
  (d.kinds || []).filter((k) => (!["chatgpt", "compatible"].includes(k) || d.admin) && (k !== "claude-cloud" || d.cloud_ok))
    .forEach((k) => {
      const o = document.createElement("option");
      o.value = k; o.textContent = t(`rn_k_${k}`);
      sel.append(o);
    });
  if (current) sel.value = current;
}

function fillKeySources(rec) {
  const sel = $("#rn-key-src");
  sel.textContent = "";
  const add = (value, label) => { const o = document.createElement("option"); o.value = value; o.textContent = label; sel.append(o); };
  if (rec && rec.key) add("keep", t("rn_src_keep", rec.key.name));
  add("new", t("rn_src_new"));
  if (rnKind() !== "claude-cloud") {
    RUNNERS.names.forEach((n) => add(`vault:${n.shared ? 1 : 0}:${n.name}`,
      t("rn_src_vault", n.shared ? t("rn_src_shared", n.name) : n.name)));
  }
  sel.value = rec && rec.key ? "keep" : "new";
  keySourceView();
}

function keySourceView() {
  const typing = $("#rn-key-src").value === "new";
  $("#rn-key").classList.toggle("hidden", !typing);
  $("#rn-key-state").textContent = typing ? t("rn_key_hidden") : "";
}

function kindView() {
  const k = rnKind();
  const cloud = k === "claude-cloud", chat = k === "chatgpt", compat = k === "compatible";
  $("#rn-kind-hint").textContent = t(cloud ? "rn_h_claude-cloud" : chat ? "rn_h_chatgpt"
    : compat ? "rn_h_compatible" : "rn_h_key");
  $("#rn-cloud-box").classList.toggle("hidden", !cloud);
  $("#rn-url-box").classList.toggle("hidden", !compat);
  $("#rn-model-box").classList.toggle("hidden", cloud);
  $("#rn-key-box").classList.toggle("hidden", chat);
  $("#rn-chatgpt").classList.toggle("hidden", !chat);
  $("#rn-key-label").textContent = t(cloud ? "rn_f_token" : compat ? "rn_f_key_opt" : "rn_f_key");
  $("#rn-model-hint").textContent = t(k === "anthropic" ? "rn_m_claude" : chat ? "rn_m_chatgpt" : "rn_m_api");
  $("#rn-model").required = !cloud && !chat && k !== "anthropic";
  $("#rn-url").required = compat;
  $("#rn-trigger").required = cloud;
  $("#rn-cl-sentence").textContent = ((RUNNERS.data.setup_sentence || {})[LANG]) || "";
  fillKeySources(RUNNERS.editing);
  if (chat && (RUNNERS.data.chatgpt || {}).plan) loadChatgptModels().catch(() => {});
}

async function openRunner(rec) {
  RUNNERS.editing = rec || null;
  RUNNERS.names = (await api("/ui/api/runners/secrets")).names || [];
  $("#form-runner").reset();
  $("#rn-editor-title").textContent = t(rec ? "rn_edit_title" : "rn_new_title");
  fillKinds(rec ? rec.kind : "");
  $("#rn-kind").disabled = !!rec;
  $("#rn-name").value = rec ? rec.name : "";
  $("#rn-model").value = rec ? rec.model || "" : "";
  $("#rn-url").value = rec ? rec.base_url || "" : "";
  $("#rn-trigger").value = rec ? rec.trigger || "" : "";
  $("#rn-enabled").checked = rec ? !!rec.enabled : true;
  kindView();
  $("#rn-editor").classList.remove("hidden");
  $("#rn-editor").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function saveRunner() {
  const k = rnKind();
  const body = { kind: k, name: $("#rn-name").value.trim(), enabled: $("#rn-enabled").checked,
                 model: $("#rn-model").value.trim(), base_url: $("#rn-url").value.trim(),
                 trigger: $("#rn-trigger").value.trim() };
  if (RUNNERS.editing) body.id = RUNNERS.editing.id;
  const src = $("#rn-key-src").value;
  if (src === "new" && k !== "chatgpt") body.key = $("#rn-key").value;
  if (src.startsWith("vault:")) {
    const [, shared, ...name] = src.split(":");
    body.key_ref = { name: name.join(":"), shared: shared === "1" };
  }
  const d = await api("/ui/api/runners", { method: "POST", body });
  toast(d.ok ? t("toast_rn_saved") : d.message, !!d.ok);
  $("#rn-key").value = "";
  if (!d.ok) return;
  $("#rn-editor").classList.add("hidden");
  await loadRunners();
}

async function testRunner(rec) {
  if (rec.kind === "claude-cloud" && !(await confirmModal(t("confirm_rn_test_cloud")))) return;
  await rnPost("/ui/api/runners/test", { id: rec.id });
}

async function deleteRunner(rec) {
  if (!(await confirmModal(t("confirm_rn_delete", rec.name)))) return;
  const d = await api("/ui/api/runners/delete", { method: "POST", body: { id: rec.id } });
  toast(d.ok ? t("toast_rn_deleted") : d.message, !!d.ok);
  $("#rn-editor").classList.add("hidden");
  await loadRunners();
}

/* ── the ChatGPT sign-in (admins) ── */
function showChatgpt(s) {
  const st = s || {};
  const el = $("#rn-cg-state");
  el.textContent = st.signed_in
    ? `${t("rn_cg_in", st.email || "?")}${st.plan ? "" : " " + t("rn_cg_noplan")}`
    : t(st.problem === "sign_in_again" ? "rn_cg_again" : "rn_cg_out");
  el.classList.toggle("notice", !st.plan);
  $("#btn-rn-cg-signout").classList.toggle("hidden", !st.signed_in);
}

async function loadChatgptModels() {
  const d = await api("/ui/api/runner/chatgpt/models");
  const list = $("#rn-models");
  list.textContent = "";
  (d.models || []).forEach((m) => {
    const o = document.createElement("option");
    o.value = m.slug; o.label = m.name;
    list.append(o);
  });
  $("#rn-model").placeholder = (d.models && d.models[0] && d.models[0].slug) || "";
}

async function startChatgpt() {
  const d = await api("/ui/api/runner/chatgpt/start", { method: "POST", body: {} });
  if (!d.ok || !String(d.url).startsWith("https://auth.openai.com/")) { toast(d.message || "", false); return; }
  $("#rn-cg-link").href = d.url;
  $("#rn-cg-url").value = "";
  $("#rn-cg-finish").classList.remove("hidden");
  $("#rn-cg-link").focus();
}

async function finishChatgpt() {
  const d = await api("/ui/api/runner/chatgpt/finish", { method: "POST", body: { url: $("#rn-cg-url").value.trim() } });
  toast(d.message, !!d.ok);
  if (!d.ok) return;
  $("#rn-cg-finish").classList.add("hidden");
  $("#rn-cg-url").value = "";
  await loadRunners();
}

async function signoutChatgpt() {
  if (!(await confirmModal(t("confirm_rn_cg_signout")))) return;
  const d = await api("/ui/api/runner/chatgpt/signout", { method: "POST", body: {} });
  toast(d.message, !!d.ok);
  await loadRunners();
}

VIEWS.runners = loadRunners;

document.addEventListener("DOMContentLoaded", () => {
  const fail = (x) => toast(x.message, false);
  $("#btn-rn-new").addEventListener("click", () => openRunner(null).catch(fail));
  $("#btn-rn-close").addEventListener("click", () => $("#rn-editor").classList.add("hidden"));
  $("#rn-kind").addEventListener("change", kindView);
  $("#rn-key-src").addEventListener("change", keySourceView);
  $("#btn-rn-cl-copy").addEventListener("click", () => copyText($("#rn-cl-sentence").textContent));
  $("#form-runner").addEventListener("submit", (e) => { e.preventDefault(); saveRunner().catch(fail); });
  $("#btn-rn-cg-start").addEventListener("click", () => startChatgpt().catch(fail));
  $("#btn-rn-cg-finish").addEventListener("click", () => finishChatgpt().catch(fail));
  $("#btn-rn-cg-signout").addEventListener("click", () => signoutChatgpt().catch(fail));
  $$("[data-setlang]").forEach((b) => b.addEventListener("click", () => {
    if (!$("#view-runners").classList.contains("hidden")) loadRunners().catch(() => {});
    if (!$("#rn-editor").classList.contains("hidden")) { fillKinds(rnKind()); kindView(); }
  }));
});
