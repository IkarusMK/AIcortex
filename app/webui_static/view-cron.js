/* Scheduled jobs — a person's own jobs (admins: everyone's, with owner). The
   schedule form offers presets and previews the next runs before saving; the
   server computes them with the runner's own matcher. Times are server time. */

Object.assign(I18N.de, {
  nav_cron: "Cron-Jobs", cron_new: "Neuer Job", cron_hint: "Geplante Aufträge an deine KI. Zeiten in Serverzeit; ausgeführt werden sie vom Runner des Jobs (Reiter „Runner“).",
  cron_col_name: "Name", cron_col_when: "Wann", cron_col_next: "Nächster Lauf", cron_col_last: "Letzter Lauf",
  cron_col_on: "An", cron_col_owner: "Für", cron_empty: "Noch keine Jobs.",
  cron_runner_none: "Noch kein Runner an — Jobs laufen erst, wenn einer im Reiter „Runner“ eingeschaltet ist.",
  cron_runner_stale: "Der Runner hat seit {0} nicht mehr geprüft — ist er noch an?",
  cron_runner_ok: "Runner aktiv, zuletzt gesehen {0}.",
  cron_new_title: "Neuer Job", cron_edit_title: "Job bearbeiten",
  cron_f_name: "Name", cron_f_when: "Wann", cron_f_time: "Uhrzeit", cron_f_dow: "Wochentag",
  cron_f_expr: "Zeitplan (Cron-Ausdruck)", cron_f_prompt: "Auftrag an die KI", cron_f_notify: "Ergebnis an (Inbox)",
  cron_f_owner: "Läuft als (leer = Runner)", cron_f_enabled: "Eingeschaltet",
  cron_col_runner: "Runner", cron_f_runner: "Runner",
  cron_runner_hint: "Wer den Job ausführt. Ein Claude-Runner führt nur Jobs aus, die du selbst geschrieben hast.",
  cron_runner_std: "Standard-Runner ({0})", cron_runner_std_none: "Standard-Runner (noch keiner)",
  cron_runner_std_short: "Standard", cron_runner_ext: "Eigener Runner (holt seine Jobs ab)", cron_runner_ext_short: "extern",
  cron_p_daily: "Täglich", cron_p_weekdays: "Werktags", cron_p_weekly: "Wöchentlich", cron_p_hourly: "Stündlich",
  cron_next: "Nächste Läufe: {0}", cron_never: "–", cron_off: "aus",
  cron_d_daily: "täglich {0}", cron_d_weekdays: "werktags {0}", cron_d_weekly: "{1} {0}", cron_d_hourly: "stündlich zur Minute {0}",
  cron_d_monthly: "monatlich am {1}., {0}",
  confirm_cron_delete: "Job „{0}“ löschen?", toast_cron_saved: "Job gespeichert.", toast_cron_deleted: "Job gelöscht.",
});
Object.assign(I18N.en, {
  nav_cron: "Scheduled jobs", cron_new: "New job", cron_hint: "Scheduled tasks for your AI. Times are server time; the job's runner carries them out (tab “Runners”).",
  cron_col_name: "Name", cron_col_when: "When", cron_col_next: "Next run", cron_col_last: "Last run",
  cron_col_on: "On", cron_col_owner: "For", cron_empty: "No jobs yet.",
  cron_runner_none: "No runner is on yet — jobs only run once one is switched on in the “Runners” tab.",
  cron_runner_stale: "The runner hasn't checked since {0} — is it still on?",
  cron_runner_ok: "Runner active, last seen {0}.",
  cron_new_title: "New job", cron_edit_title: "Edit job",
  cron_f_name: "Name", cron_f_when: "When", cron_f_time: "Time", cron_f_dow: "Weekday",
  cron_f_expr: "Schedule (cron expression)", cron_f_prompt: "Task for the AI", cron_f_notify: "Send result to (inbox)",
  cron_f_owner: "Runs as (empty = runner)", cron_f_enabled: "Switched on",
  cron_col_runner: "Runner", cron_f_runner: "Runner",
  cron_runner_hint: "Who carries the job out. A Claude runner only runs jobs you wrote yourself.",
  cron_runner_std: "Standard runner ({0})", cron_runner_std_none: "Standard runner (none yet)",
  cron_runner_std_short: "Standard", cron_runner_ext: "Own runner (fetches its jobs)", cron_runner_ext_short: "external",
  cron_p_daily: "Daily", cron_p_weekdays: "Weekdays", cron_p_weekly: "Weekly", cron_p_hourly: "Hourly",
  cron_next: "Next runs: {0}", cron_never: "–", cron_off: "off",
  cron_d_daily: "daily {0}", cron_d_weekdays: "weekdays {0}", cron_d_weekly: "{1} {0}", cron_d_hourly: "hourly at minute {0}",
  cron_d_monthly: "monthly, day {1}, {0}",
  confirm_cron_delete: "Delete job “{0}”?", toast_cron_saved: "Job saved.", toast_cron_deleted: "Job deleted.",
});

let CRON = { jobs: [], editing: null, preset: "daily", timer: null, runners: { runners: [] } };
const CRON_DAYS = [1, 2, 3, 4, 5, 6, 0];   // Monday first; cron counts Sunday as 0

function cronDayName(d) {
  const ref = new Date(2026, 10, 1 + ((d + 7) % 7));   // 1 Nov 2026 is a Sunday
  return ref.toLocaleDateString(LANG === "de" ? "de-DE" : "en-GB", { weekday: "long" });
}

function cronStamp(s) {
  if (!s) return t("cron_never");
  const d = new Date(s);
  return isNaN(d) ? s : d.toLocaleString(LANG === "de" ? "de-DE" : "en-GB",
    { weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function cronDescribe(expr) {
  const p = (expr || "").split(/\s+/);
  if (p.length !== 5 || !/^\d+$/.test(p[0])) return expr;
  const hhmm = /^\d+$/.test(p[1]) ? `${p[1].padStart(2, "0")}:${p[0].padStart(2, "0")}` : "";
  if (p[1] === "*" && p.slice(2).every((x) => x === "*")) return t("cron_d_hourly", p[0]);
  if (hhmm && /^\d+$/.test(p[2]) && p[3] === "*" && p[4] === "*") return t("cron_d_monthly", hhmm, p[2]);
  if (!hhmm || p[2] !== "*" || p[3] !== "*") return expr;
  if (p[4] === "*") return t("cron_d_daily", hhmm);
  if (p[4] === "1-5") return t("cron_d_weekdays", hhmm);
  if (/^[0-7]$/.test(p[4])) return t("cron_d_weekly", hhmm, cronDayName(+p[4]));
  return expr;
}

function cronRunnerName(id) {
  if (id === "external") return t("cron_runner_ext_short");
  const rec = (CRON.runners.runners || []).find((r) => r.id === id);
  return id && rec ? rec.name : t("cron_runner_std_short");
}

function fillCronRunners(current) {
  const sel = $("#cron-runner-sel");
  sel.textContent = "";
  const add = (value, label) => { const o = document.createElement("option"); o.value = value; o.textContent = label; sel.append(o); };
  const d = CRON.runners, list = d.runners || [];
  const std = list.find((r) => r.id === d.standard);
  add("", std ? t("cron_runner_std", std.name) : t("cron_runner_std_none"));
  list.filter((r) => r.id !== d.standard && (r.mine || (isAdmin() && r.kind !== "claude-cloud")))
    .forEach((r) => add(r.id, isAdmin() && !r.mine ? `${r.name} · ${r.owner}` : r.name));
  if (isAdmin() && d.external) add("external", t("cron_runner_ext"));
  if (current && ![...sel.options].some((o) => o.value === current)) {
    const rec = list.find((r) => r.id === current);
    add(current, rec ? `${rec.name} · ${rec.owner}` : current);
  }
  sel.value = current || "";
}

function showRunner(runner) {
  const el = $("#cron-runner");
  const r = runner || {};
  el.textContent = !r.last_seen ? t("cron_runner_none")
    : t(r.ok ? "cron_runner_ok" : "cron_runner_stale", cronStamp(r.last_seen));
  el.classList.toggle("notice", !r.ok);
  el.classList.toggle("dim", !!r.ok);
  el.classList.remove("hidden");
}

async function loadCron() {
  const [d, runners] = await Promise.all([api("/ui/api/cron"), api("/ui/api/runners")]);
  CRON.jobs = d.jobs;
  CRON.runners = runners;
  showRunner(d.runner);
  const tbody = $("#cron-table tbody");
  tbody.textContent = "";
  $("#cron-empty").classList.toggle("hidden", CRON.jobs.length > 0);
  CRON.jobs.forEach((j) => {
    const tr = document.createElement("tr");
    const cell = (text, cls) => { const td = document.createElement("td"); td.textContent = text; if (cls) td.className = cls; return td; };
    const when = cell(cronDescribe(j.schedule));
    when.title = j.schedule;
    const last = cell(cronStamp(j.last_run), "small");
    if (j.last_result) last.title = j.last_result;
    const on = document.createElement("td");
    const box = document.createElement("input"); box.type = "checkbox"; box.checked = j.enabled;
    box.setAttribute("aria-label", `${t("cron_col_on")}: ${j.name}`);
    box.addEventListener("change", () => saveCronJob({ ...j, enabled: box.checked, runner: undefined })
      .catch((x) => toast(x.message, false)));
    on.append(box);
    tr.append(cell(j.name), when, cell(j.enabled ? cronStamp(j.next_run) : t("cron_off"), "small"), last,
              cell(cronRunnerName(j.runner), "small"), on);
    if (isAdmin()) tr.append(cell(j.owner || "runner", "mono small"));
    const act = document.createElement("td"); act.className = "actions";
    [["sv_edit", "ghost", () => openCron(j)], ["delete", "danger", () => deleteCron(j)]].forEach(([key, cls, fn]) => {
      const b = document.createElement("button"); b.className = `btn sm ${cls}`; b.textContent = t(key);
      b.addEventListener("click", () => fn().catch((x) => toast(x.message, false)));
      act.append(b);
    });
    tr.append(act);
    tbody.append(tr);
  });
}

function cronFromPreset() {
  const [h, m] = ($("#cron-time").value || "06:30").split(":").map((x) => String(parseInt(x, 10)));
  const dow = $("#cron-dow").value;
  const expr = { daily: `${m} ${h} * * *`, weekdays: `${m} ${h} * * 1-5`,
                 weekly: `${m} ${h} * * ${dow}`, hourly: `${m} * * * *` }[CRON.preset];
  if (expr) { $("#cron-expr").value = expr; cronPreview(); }
}

function cronSetPreset(name) {
  CRON.preset = name;
  $$("#cron-presets .chip").forEach((b) => b.classList.toggle("active", b.dataset.preset === name));
  $("#cron-dow").disabled = name !== "weekly";
  cronFromPreset();
}

function cronPreview() {
  clearTimeout(CRON.timer);
  CRON.timer = setTimeout(async () => {
    const d = await api(`/ui/api/cron/preview?schedule=${encodeURIComponent($("#cron-expr").value)}`);
    const el = $("#cron-preview");
    el.classList.toggle("err", !d.ok);
    el.textContent = d.ok ? t("cron_next", d.next.map(cronStamp).join(" · ")) : d.message;
  }, 250);
}

function openCron(job) {
  CRON.editing = job || null;
  $("#form-cron").reset();
  $("#cron-editor-title").textContent = t(job ? "cron_edit_title" : "cron_new_title");
  $("#cron-name").value = job ? job.name : "";
  $("#cron-name").readOnly = !!job;
  $("#cron-prompt").value = job ? job.prompt : "";
  $("#cron-notify").value = job ? job.notify || "user" : "user";
  $("#cron-owner").value = job ? job.owner || "" : "";
  $("#cron-enabled").checked = job ? job.enabled : true;
  fillCronRunners(job ? job.runner || "" : "");
  $("#btn-cron-delete").classList.toggle("hidden", !job);
  $("#cron-editor").classList.remove("hidden");
  if (job) {
    CRON.preset = "";
    $$("#cron-presets .chip").forEach((b) => b.classList.remove("active"));
    $("#cron-expr").value = job.schedule;
    cronPreview();
  } else {
    cronSetPreset("daily");
  }
  $("#cron-editor").scrollIntoView({ behavior: "smooth", block: "nearest" });
  return Promise.resolve();
}

async function saveCronJob(job) {
  const body = { name: job.name, schedule: job.schedule, prompt: job.prompt,
                 notify: job.notify || "user", enabled: job.enabled };
  if (job.owner) body.owner = job.owner;
  if (job.runner !== undefined) body.runner = job.runner;
  const d = await api("/ui/api/cron", { method: "POST", body });
  toast(d.ok ? t("toast_cron_saved") : d.message, !!d.ok);
  await loadCron();
  return d.ok;
}

async function deleteCron(job) {
  if (!(await confirmModal(t("confirm_cron_delete", job.name)))) return;
  const d = await api("/ui/api/cron/delete", { method: "POST", body: { name: job.name } });
  toast(d.ok ? t("toast_cron_deleted") : d.message, !!d.ok);
  $("#cron-editor").classList.add("hidden");
  await loadCron();
}

VIEWS.cron = loadCron;

document.addEventListener("DOMContentLoaded", () => {
  const fail = (x) => toast(x.message, false);
  const dow = $("#cron-dow");
  CRON_DAYS.forEach((d) => { const o = document.createElement("option"); o.value = d; dow.append(o); });
  const fillDays = () => $$("#cron-dow option").forEach((o) => { o.textContent = cronDayName(+o.value); });
  fillDays();
  $$("[data-setlang]").forEach((b) => b.addEventListener("click", fillDays));
  $$("#cron-presets .chip").forEach((b) => b.addEventListener("click", () => cronSetPreset(b.dataset.preset)));
  ["#cron-time", "#cron-dow"].forEach((s) => $(s).addEventListener("change", cronFromPreset));
  $("#cron-expr").addEventListener("input", () => {
    CRON.preset = "";
    $$("#cron-presets .chip").forEach((b) => b.classList.remove("active"));
    cronPreview();
  });
  $("#btn-cron-new").addEventListener("click", () => openCron(null));
  $("#btn-cron-close").addEventListener("click", () => $("#cron-editor").classList.add("hidden"));
  $("#btn-cron-delete").addEventListener("click", () => deleteCron(CRON.editing).catch(fail));
  $("#form-cron").addEventListener("submit", (e) => {
    e.preventDefault();
    const job = { name: $("#cron-name").value.trim(), schedule: $("#cron-expr").value.trim(),
                  prompt: $("#cron-prompt").value, notify: $("#cron-notify").value.trim(),
                  enabled: $("#cron-enabled").checked, owner: isAdmin() ? $("#cron-owner").value.trim() : "",
                  runner: $("#cron-runner-sel").value };
    saveCronJob(job).then((ok) => { if (ok) $("#cron-editor").classList.add("hidden"); }).catch(fail);
  });
});
