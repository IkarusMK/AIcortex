/* Runner setup (admins, inside "Scheduled jobs"): which model the NAS runner uses
   for due jobs, its key (written to the vault, never shown again) and the runner
   keys it connects with. The runner itself asks for due jobs without any model. */

Object.assign(I18N.de, {
  rn_title: "Runner einrichten",
  rn_hint: "Der Runner auf der NAS fragt jede Minute ohne Modell nach fälligen Jobs. Nur wenn einer fällig ist, startet er das Modell, das hier eingestellt ist.",
  rn_f_backend: "Modell-Zugang", rn_f_model: "Modell", rn_f_key: "Schlüssel",
  "rn_b_claude-abo": "Claude-Abo (Token aus „claude setup-token“)",
  "rn_b_claude-api": "Claude Code mit Anthropic-API-Schlüssel",
  rn_b_openrouter: "OpenRouter", rn_b_anthropic: "Anthropic-API", rn_b_openai: "OpenAI", rn_b_gemini: "Google Gemini",
  rn_m_claude: "Optional, leer = Standard von Claude Code (z. B. sonnet oder opus).",
  rn_m_api: "Modellname wie beim Anbieter angegeben.",
  rn_key_set: "Ein Schlüssel ist gespeichert — leer lassen, um ihn zu behalten.",
  rn_key_unset: "Noch kein Schlüssel gespeichert.",
  rn_keys: "Runner-Schlüssel", rn_keys_none: "Noch kein Runner-Schlüssel.", rn_key_new: "Runner-Schlüssel erstellen",
  rn_snippet: "In die .env des Runners eintragen:", toast_rn_saved: "Runner-Einstellung gespeichert.",
});
Object.assign(I18N.en, {
  rn_title: "Set up the runner",
  rn_hint: "The runner on the NAS checks for due jobs every minute without any model. Only when one is due does it start the model chosen here.",
  rn_f_backend: "Model access", rn_f_model: "Model", rn_f_key: "Key",
  "rn_b_claude-abo": "Claude subscription (token from “claude setup-token”)",
  "rn_b_claude-api": "Claude Code with an Anthropic API key",
  rn_b_openrouter: "OpenRouter", rn_b_anthropic: "Anthropic API", rn_b_openai: "OpenAI", rn_b_gemini: "Google Gemini",
  rn_m_claude: "Optional, empty = Claude Code's default (e.g. sonnet or opus).",
  rn_m_api: "Model name as your provider lists it.",
  rn_key_set: "A key is stored — leave this empty to keep it.",
  rn_key_unset: "No key stored yet.",
  rn_keys: "Runner keys", rn_keys_none: "No runner key yet.", rn_key_new: "Create runner key",
  rn_snippet: "Put this into the runner's .env:", toast_rn_saved: "Runner setup saved.",
});

let RUNNER = { backends: [], mcpUrl: "" };

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

function runnerKeys(keys) {
  const ul = $("#rn-keys");
  ul.textContent = "";
  $("#rn-keys-none").classList.toggle("hidden", keys.length > 0);
  keys.forEach((k) => {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.className = "mono";
    label.textContent = `${k.name} · ${k.keyid}${k.disabled ? " · " + t("cron_off") : ""}`;
    li.append(label);
    if (!k.disabled) {
      const b = document.createElement("button");
      b.className = "btn sm danger"; b.textContent = t("cl_revoke");
      b.addEventListener("click", () => revokeRunnerKey(k).catch((x) => toast(x.message, false)));
      li.append(" ", b);
    }
    ul.append(li);
  });
}

async function loadRunner() {
  const d = await api("/ui/api/runner");
  RUNNER = { backends: d.backends || [], mcpUrl: d.mcp_url || "" };
  runnerBackends(RUNNER.backends, d.backend);
  $("#rn-model").value = d.model || "";
  $("#rn-key").value = "";
  $("#rn-key-state").textContent = t(d.key_set ? "rn_key_set" : "rn_key_unset");
  runnerModelHint();
  runnerKeys(d.keys || []);
}

async function saveRunner() {
  const body = { backend: $("#rn-backend").value, model: $("#rn-model").value.trim(), key: $("#rn-key").value };
  const d = await api("/ui/api/runner/config", { method: "POST", body });
  toast(d.ok ? t("toast_rn_saved") : d.message, !!d.ok);
  if (d.ok) await loadRunner();
}

async function createRunnerKey() {
  const d = await api("/ui/api/runner/key", { method: "POST", body: { name: "nas-runner" } });
  if (!d.ok) { toast(d.message, false); return; }
  $("#rn-key-value").value = d.key;
  $("#rn-snippet").textContent = `CONNECTOR_URL=${d.mcp_url || RUNNER.mcpUrl}\nRUNNER_TOKEN=${d.key}`;
  $("#rn-key-result").classList.remove("hidden");
  await loadRunner();
}

async function revokeRunnerKey(k) {
  if (!(await confirmModal(t("confirm_revoke_key", k.name)))) return;
  const d = await api("/ui/api/agentkeys/revoke", { method: "POST", body: { keyid: k.keyid } });
  toast(d.message, !!d.ok);
  $("#rn-key-result").classList.add("hidden");
  await loadRunner();
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
  $("#btn-rn-key").addEventListener("click", () => createRunnerKey().catch(fail));
  $("#btn-rn-copy").addEventListener("click", () => copyText($("#rn-snippet").textContent));
  $$("[data-setlang]").forEach((b) => b.addEventListener("click", () => {
    if (RUNNER.backends.length) runnerBackends(RUNNER.backends, $("#rn-backend").value);
    runnerModelHint();
  }));
});
