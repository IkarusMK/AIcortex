/* Runner setup (admins, inside "Scheduled jobs"): which model AICortex runs due jobs
   with, its key (written to the vault, never shown again) and the on/off switch.
   AICortex checks for due jobs without any model and asks the model only then. */

Object.assign(I18N.de, {
  rn_title: "Runner einrichten",
  rn_hint: "AICortex prüft jede Minute ohne Modell, ob ein Job fällig ist. Nur dann fragt es das Modell, das hier eingestellt ist — kein Container, nichts zu installieren.",
  rn_f_backend: "Modell-Zugang", rn_f_model: "Modell", rn_f_key: "Schlüssel",
  "rn_b_claude-abo": "Claude-Abo (Token aus „claude setup-token“)",
  "rn_b_claude-api": "Anthropic-API-Schlüssel",
  rn_b_openrouter: "OpenRouter", rn_b_openai: "OpenAI-API-Schlüssel", rn_b_gemini: "Google Gemini",
  rn_m_claude: "Optional, leer = claude-sonnet-5-5; Kurzformen wie sonnet, opus oder haiku gehen auch.",
  rn_m_api: "Modellname wie beim Anbieter angegeben.",
  rn_key_set: "Ein Schlüssel ist gespeichert — leer lassen, um ihn zu behalten.",
  rn_key_unset: "Noch kein Schlüssel gespeichert.",
  toast_rn_saved: "Runner-Einstellung gespeichert.",
  rn_f_builtin: "Runner an — AICortex führt fällige Jobs selbst aus",
  rn_builtin_hint: "Beim Claude-Abo nutzt AICortex deinen Token aus „claude setup-token“ direkt für Modellanfragen — so beschreibt Anthropic den Token für Skripte; Anthropic kann diese Nutzung jederzeit einschränken.",
  rn_s_off: "Runner aus.", rn_s_waiting: "Runner an — prüft ab der nächsten Minute.",
  rn_s_idle: "Runner aktiv — zuletzt geprüft {0}, nichts fällig.",
  rn_s_ran: "Runner aktiv — {0}: {1}.",
  "rn_s_no-key": "Runner an, aber noch kein Schlüssel gespeichert.",
  rn_s_error: "Runner: Fehler ({0}) — {1}",
});
Object.assign(I18N.en, {
  rn_title: "Set up the runner",
  rn_hint: "AICortex checks every minute, without any model, whether a job is due. Only then does it ask the model chosen here — no container, nothing to install.",
  rn_f_backend: "Model access", rn_f_model: "Model", rn_f_key: "Key",
  "rn_b_claude-abo": "Claude subscription (token from “claude setup-token”)",
  "rn_b_claude-api": "Anthropic API key",
  rn_b_openrouter: "OpenRouter", rn_b_openai: "OpenAI API key", rn_b_gemini: "Google Gemini",
  rn_m_claude: "Optional, empty = claude-sonnet-5-5; short names like sonnet, opus or haiku work too.",
  rn_m_api: "Model name as your provider lists it.",
  rn_key_set: "A key is stored — leave this empty to keep it.",
  rn_key_unset: "No key stored yet.",
  toast_rn_saved: "Runner setup saved.",
  rn_f_builtin: "Runner on — AICortex runs due jobs itself",
  rn_builtin_hint: "With a Claude subscription, AICortex uses your token from “claude setup-token” directly for model requests — how Anthropic describes the token for scripts; Anthropic may restrict this use at any time.",
  rn_s_off: "Runner off.", rn_s_waiting: "Runner on — checks from the next minute.",
  rn_s_idle: "Runner active — last checked {0}, nothing due.",
  rn_s_ran: "Runner active — {0}: {1}.",
  "rn_s_no-key": "Runner on, but no key stored yet.",
  rn_s_error: "Runner: error ({0}) — {1}",
});

let RUNNER = { backends: [] };

function runnerModelHint() {
  const claude = $("#rn-backend").value.startsWith("claude-");
  $("#rn-model-hint").textContent = t(claude ? "rn_m_claude" : "rn_m_api");
  $("#rn-model").required = !claude;
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

async function loadRunner() {
  const d = await api("/ui/api/runner");
  RUNNER = { backends: d.backends || [] };
  runnerBackends(RUNNER.backends, d.backend);
  $("#rn-model").value = d.model || "";
  $("#rn-key").value = "";
  $("#rn-key-state").textContent = t(d.key_set ? "rn_key_set" : "rn_key_unset");
  runnerModelHint();
  $("#rn-builtin").checked = !!d.builtin;
  const s = d.builtin_state || { state: "off" };
  const el = $("#rn-builtin-state");
  el.textContent = t(`rn_s_${s.state}`, cronStamp(s.at), s.detail || "");
  el.classList.toggle("notice", ["no-key", "error"].includes(s.state));
}

async function saveRunner() {
  const body = { backend: $("#rn-backend").value, model: $("#rn-model").value.trim(), key: $("#rn-key").value,
                 builtin: $("#rn-builtin").checked };
  const d = await api("/ui/api/runner/config", { method: "POST", body });
  toast(d.ok ? t("toast_rn_saved") : d.message, !!d.ok);
  if (d.ok) await loadRunner();
}

const CRON_VIEW = VIEWS.cron;
VIEWS.cron = async () => {
  await CRON_VIEW();
  if (isAdmin()) await loadRunner();
};

document.addEventListener("DOMContentLoaded", () => {
  const fail = (x) => toast(x.message, false);
  $("#rn-backend").addEventListener("change", runnerModelHint);
  $("#form-runner").addEventListener("submit", (e) => { e.preventDefault(); saveRunner().catch(fail); });
  $$("[data-setlang]").forEach((b) => b.addEventListener("click", () => {
    if (RUNNER.backends.length) runnerBackends(RUNNER.backends, $("#rn-backend").value);
    runnerModelHint();
  }));
});
