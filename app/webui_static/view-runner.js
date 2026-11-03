/* Runner setup (admins, inside "Scheduled jobs"): which model AICortex runs due jobs
   with, its key (written to the vault, never shown again) or — for a ChatGPT plan —
   the sign-in with ChatGPT, and the on/off switch. AICortex checks for due jobs
   without any model and asks the model only then. */

Object.assign(I18N.de, {
  rn_title: "Runner einrichten",
  rn_hint: "AICortex prüft jede Minute ohne Modell, ob ein Job fällig ist. Nur dann fragt es das Modell, das hier eingestellt ist — kein Container, nichts zu installieren.",
  rn_f_backend: "Modell-Zugang", rn_f_model: "Modell", rn_f_key: "Schlüssel",
  "rn_b_claude-abo": "Claude-Abo (Token aus „claude setup-token“)",
  "rn_b_chatgpt-abo": "ChatGPT-Abo (Mit ChatGPT anmelden)",
  "rn_b_claude-api": "Anthropic-API-Schlüssel",
  rn_b_openrouter: "OpenRouter", rn_b_openai: "OpenAI-API-Schlüssel", rn_b_gemini: "Google Gemini",
  rn_m_claude: "Optional, leer = claude-sonnet-5-5; Kurzformen wie sonnet, opus oder haiku gehen auch.",
  rn_m_chatgpt: "Optional, leer = das erste Modell deines Kontos. Nach der Anmeldung schlägt das Feld deine Modelle vor.",
  rn_m_api: "Modellname wie beim Anbieter angegeben.",
  rn_key_set: "Ein Schlüssel ist gespeichert — leer lassen, um ihn zu behalten.",
  rn_key_unset: "Noch kein Schlüssel gespeichert.",
  toast_rn_saved: "Runner-Einstellung gespeichert.",
  rn_f_builtin: "Runner an — AICortex führt fällige Jobs selbst aus",
  rn_builtin_hint: "Beim Claude-Abo nutzt AICortex deinen Token aus „claude setup-token“ direkt für Modellanfragen — so beschreibt Anthropic den Token für Skripte; Anthropic kann diese Nutzung jederzeit einschränken. Beim ChatGPT-Abo nutzt AICortex OpenAIs „Sign in with ChatGPT“; bei Plus teilen sich alle Apps das 5-Stunden-Limit.",
  rn_s_off: "Runner aus.", rn_s_waiting: "Runner an — prüft ab der nächsten Minute.",
  rn_s_idle: "Runner aktiv — zuletzt geprüft {0}, nichts fällig.",
  rn_s_ran: "Runner aktiv — {0}: {1}.",
  "rn_s_no-key": "Runner an, aber noch kein Schlüssel gespeichert.",
  "rn_s_no-signin": "Runner an, aber noch nicht mit ChatGPT angemeldet (oder die Abo-Nutzung ist nicht erlaubt).",
  rn_s_error: "Runner: Fehler ({0}) — {1}",
  rn_cg_out: "Noch nicht mit ChatGPT angemeldet.",
  rn_cg_in: "Mit ChatGPT angemeldet als {0}.",
  rn_cg_noplan: "Die Nutzung deines ChatGPT-Abos ist nicht erlaubt — melde dich erneut an und erlaube sie.",
  rn_cg_again: "Die ChatGPT-Anmeldung ist abgelaufen oder wurde getrennt — bitte neu anmelden.",
  rn_cg_start: "Mit ChatGPT anmelden", rn_cg_signout: "Abmelden",
  rn_cg_step1: "1. Öffne die Anmeldung, melde dich mit deinem ChatGPT-Konto an und erlaube AICortex die Nutzung deines Abos.",
  rn_cg_open: "ChatGPT-Anmeldung öffnen ↗",
  rn_cg_step2: "2. Danach zeigt der Browser eine Fehlerseite von 127.0.0.1 — das ist richtig so. Kopiere die komplette Adresse aus der Adressleiste hier hinein.",
  rn_cg_url: "Adresse nach der Anmeldung", rn_cg_finish: "Anmeldung abschließen",
  confirm_rn_cg_signout: "Von ChatGPT abmelden? Der Runner kann dann keine Jobs mehr mit deinem ChatGPT-Abo ausführen.",
});
Object.assign(I18N.en, {
  rn_title: "Set up the runner",
  rn_hint: "AICortex checks every minute, without any model, whether a job is due. Only then does it ask the model chosen here — no container, nothing to install.",
  rn_f_backend: "Model access", rn_f_model: "Model", rn_f_key: "Key",
  "rn_b_claude-abo": "Claude subscription (token from “claude setup-token”)",
  "rn_b_chatgpt-abo": "ChatGPT plan (Sign in with ChatGPT)",
  "rn_b_claude-api": "Anthropic API key",
  rn_b_openrouter: "OpenRouter", rn_b_openai: "OpenAI API key", rn_b_gemini: "Google Gemini",
  rn_m_claude: "Optional, empty = claude-sonnet-5-5; short names like sonnet, opus or haiku work too.",
  rn_m_chatgpt: "Optional, empty = your account's first model. After signing in, the field suggests your models.",
  rn_m_api: "Model name as your provider lists it.",
  rn_key_set: "A key is stored — leave this empty to keep it.",
  rn_key_unset: "No key stored yet.",
  toast_rn_saved: "Runner setup saved.",
  rn_f_builtin: "Runner on — AICortex runs due jobs itself",
  rn_builtin_hint: "With a Claude subscription, AICortex uses your token from “claude setup-token” directly for model requests — how Anthropic describes the token for scripts; Anthropic may restrict this use at any time. With a ChatGPT plan, AICortex uses OpenAI's “Sign in with ChatGPT”; on Plus, all apps share the five-hour limit.",
  rn_s_off: "Runner off.", rn_s_waiting: "Runner on — checks from the next minute.",
  rn_s_idle: "Runner active — last checked {0}, nothing due.",
  rn_s_ran: "Runner active — {0}: {1}.",
  "rn_s_no-key": "Runner on, but no key stored yet.",
  "rn_s_no-signin": "Runner on, but not signed in with ChatGPT (or using the plan is not allowed).",
  rn_s_error: "Runner: error ({0}) — {1}",
  rn_cg_out: "Not signed in with ChatGPT yet.",
  rn_cg_in: "Signed in with ChatGPT as {0}.",
  rn_cg_noplan: "Using your ChatGPT plan is not allowed — sign in again and allow it.",
  rn_cg_again: "The ChatGPT sign-in has expired or was disconnected — please sign in again.",
  rn_cg_start: "Sign in with ChatGPT", rn_cg_signout: "Sign out",
  rn_cg_step1: "1. Open the sign-in, sign in with your ChatGPT account and allow AICortex to use your plan.",
  rn_cg_open: "Open the ChatGPT sign-in ↗",
  rn_cg_step2: "2. Your browser then shows an error page from 127.0.0.1 — that's expected. Copy the whole address from the address bar into this field.",
  rn_cg_url: "Address after signing in", rn_cg_finish: "Finish signing in",
  confirm_rn_cg_signout: "Sign out of ChatGPT? The runner can then no longer run jobs on your ChatGPT plan.",
});

let RUNNER = { backends: [], chatgpt: {} };

const isChatgpt = () => $("#rn-backend").value === "chatgpt-abo";

function runnerModelHint() {
  const b = $("#rn-backend").value;
  const hint = b.startsWith("claude-") ? "rn_m_claude" : isChatgpt() ? "rn_m_chatgpt" : "rn_m_api";
  $("#rn-model-hint").textContent = t(hint);
  $("#rn-model").required = hint === "rn_m_api";
}

function runnerBackendView() {
  $("#rn-key-box").classList.toggle("hidden", isChatgpt());
  $("#rn-chatgpt").classList.toggle("hidden", !isChatgpt());
  runnerModelHint();
  showChatgpt(RUNNER.chatgpt);
  if (isChatgpt() && RUNNER.chatgpt.plan) loadChatgptModels().catch(() => {});
}

function runnerBackends(list, current) {
  const sel = $("#rn-backend");
  sel.textContent = "";
  list.forEach((b) => {
    const o = document.createElement("option");
    o.value = b; o.textContent = t(`rn_b_${b}`);
    sel.append(o);
  });
  sel.value = current || list[0] || "";
}

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

async function loadRunner() {
  const d = await api("/ui/api/runner");
  RUNNER = { backends: d.backends || [], chatgpt: d.chatgpt || {} };
  runnerBackends(RUNNER.backends, d.backend);
  $("#rn-model").value = d.model || "";
  $("#rn-key").value = "";
  $("#rn-key-state").textContent = t(d.key_set ? "rn_key_set" : "rn_key_unset");
  $("#rn-builtin").checked = !!d.builtin;
  const s = d.builtin_state || { state: "off" };
  const el = $("#rn-builtin-state");
  el.textContent = t(`rn_s_${s.state}`, cronStamp(s.at), s.detail || "");
  el.classList.toggle("notice", ["no-key", "no-signin", "error"].includes(s.state));
  runnerBackendView();
}

async function saveRunner() {
  const body = { backend: $("#rn-backend").value, model: $("#rn-model").value.trim(),
                 key: isChatgpt() ? "" : $("#rn-key").value, builtin: $("#rn-builtin").checked };
  const d = await api("/ui/api/runner/config", { method: "POST", body });
  toast(d.ok ? t("toast_rn_saved") : d.message, !!d.ok);
  if (d.ok) await loadRunner();
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
  await loadRunner();
}

async function signoutChatgpt() {
  if (!(await confirmModal(t("confirm_rn_cg_signout")))) return;
  const d = await api("/ui/api/runner/chatgpt/signout", { method: "POST", body: {} });
  toast(d.message, !!d.ok);
  await loadRunner();
}

const CRON_VIEW = VIEWS.cron;
VIEWS.cron = async () => {
  await CRON_VIEW();
  if (isAdmin()) await loadRunner();
};

document.addEventListener("DOMContentLoaded", () => {
  const fail = (x) => toast(x.message, false);
  $("#rn-backend").addEventListener("change", runnerBackendView);
  $("#form-runner").addEventListener("submit", (e) => { e.preventDefault(); saveRunner().catch(fail); });
  $("#btn-rn-cg-start").addEventListener("click", () => startChatgpt().catch(fail));
  $("#btn-rn-cg-finish").addEventListener("click", () => finishChatgpt().catch(fail));
  $("#btn-rn-cg-signout").addEventListener("click", () => signoutChatgpt().catch(fail));
  $$("[data-setlang]").forEach((b) => b.addEventListener("click", () => {
    if (RUNNER.backends.length) runnerBackends(RUNNER.backends, $("#rn-backend").value);
    runnerModelHint();
    showChatgpt(RUNNER.chatgpt);
  }));
});
