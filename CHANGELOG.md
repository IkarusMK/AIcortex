# Changelog

All notable changes to AICortex are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/). Full notes for each version are on
the [Releases](https://github.com/IkarusMK/AIcortex/releases) page.

## [Unreleased]

### Changed
- **Search finds everything** the WebUI holds, not only skills, tools and secrets:
  services & devices, cron jobs, runners, playbooks and their runs, notes, clients
  and users too — only groups with hits are shown, each hit opens its tab. Runner and
  client keys never reach the browser, secrets stay names only.

## [2.4.0] — 2026-11-09
Scanning picks glass or feeder by itself on any eSCL scanner, and a Search tab in the WebUI.

### Added
- **Scanning picks glass or feeder by itself**, on any eSCL scanner whatever the
  vendor: `scan_document` now defaults to `source="auto"`. It reads the device's
  `ScannerCapabilities` (glass, feeder, two-sided feeder) and, when both exist, the
  feeder state in `ScannerStatus`: paper in the feeder → all pages from the feeder,
  empty feeder → the glass. A scanner with only a glass or only a feeder just uses
  it. A paper jam or an open feeder cover stops before scanning, with a clear
  message; if the device doesn't report its state, the glass is used and the result
  says so. The result names the source used.
- `scan_document(duplex=true)` scans both sides in the feeder where the device can —
  and says so instead of scanning one side when it can't.
- **WebUI tab „Search“** (admin): one field finds skills, tools and secrets at once —
  by name and description, German words too ("drucken" finds the print tools), best
  match first. Secrets are matched by name and description only; their values never
  reach the browser, not even as a match. A skill hit opens it in the Skills tab.

### Changed
- An explicit `source` the scanner doesn't have (`adf` on a glass-only device,
  `platen` on a sheet-feed scanner), or `adf` with an empty feeder, is refused
  instead of returning an empty or wrong scan. Unknown `source` words mean `auto`.

### Fixed
- The scan request sends the source as `pwg:InputSource`, as the eSCL standard (and
  sane-airscan) does. Strict devices ignored the old `scan:InputSource` and scanned
  the glass even when the feeder was asked for.

### Upgrading
- Pull `ghcr.io/ikarusmk/aicortex:v2.4.0` and redeploy; `data/` stays as it is.
- `scan_document` now chooses the source itself. Callers that passed `source=platen`
  or `source=adf` keep that choice; it is only refused when the scanner lacks that
  source or the feeder is empty.

## [2.3.4] — 2026-11-05
Free models only for an OpenRouter runner, and tools that explain themselves.

### Added
- **Free models only** for an OpenRouter runner: ticked for a new one, the runner only
  takes a model whose name ends in `:free` (on 05.11.2026 all 12 such models of 452
  cost $0) — checked when it is saved and again before every request, so a
  hand-edited `runners.json` can't run up a bill either. Existing runners stay as they
  are until it is ticked; `runner_list` marks such runners.

### Changed
- **The tools explain themselves**: the eleven tools a client sees up front now
  describe all 37 parameters (e.g. `scope`, `owner`, `expected_version`) and say what
  happens — an unknown name, no match, how many results in which order, that
  `integration_call` returns the tool's result unchanged and runs tools that change
  data, when `session_save` fits rather than `memory_write`. `bootstrap` says what
  it does instead of "Same as cortex_start", and `memory_read` points to
  `memory_search` instead of `memory_list`, which a client with the core profile
  doesn't see; a tool outside the core set is only named together with
  `integration_call`. `cortex_start` says to call it first, what comes back without
  arguments and when the briefing is shortened, and names `bootstrap`,
  `memory_search` and `task_list`; `integration_search` sets itself apart from
  `skill_search` and `memory_search`. Glama had rated the definitions B (3.8/5).
  The listing grows from 9,807 to 12,886 characters (about 770 tokens per
  conversation); a test keeps it under 13,000.
- `paramiko` (SSH) at least 5.1.0 — the image already built with it.

### Upgrading
- Pull `ghcr.io/ikarusmk/aicortex:v2.3.4` and redeploy; `data/` stays as it is.
- An OpenRouter runner from before keeps running as it was: to limit it to free
  models, open it under **Edit** and tick **Free models only** (its model name must
  then end in `:free`).

## [2.3.3] — 2026-11-04
Listed in the official MCP Registry, and ready for Glama.

### Added
- **Official MCP Registry**: every release now lists itself at
  registry.modelcontextprotocol.io as `io.github.IkarusMK/aicortex`. The image carries
  the registry's ownership label, `server.json` describes the server, and the release
  workflow publishes it right after the image — with GitHub's OIDC token (no stored
  secret) and a pinned, signature-checked `mcp-publisher`.
- **Glama**: `glama/stdio_bridge.py` lets Glama (glama.ai) build AICortex and list its
  tools in its sandbox — over stdio, on loopback only, with throw-away data under the
  working directory; `glama.json` names the maintainer. Neither is part of the image.

### Upgrading
- Pull `ghcr.io/ikarusmk/aicortex:v2.3.3` and redeploy; `data/` stays as it is.
  Nothing changes in how AICortex runs.

## [2.3.2] — 2026-11-04
Every button in view, a README that explains AICortex in 30 seconds, and one image
build per release.

### Fixed
- **Table buttons stay in view**: on a laptop screen the vault's *Delete* buttons — and
  at 1024 px the runner, job and service actions — slid behind the table's sideways
  scroll. Row actions now wrap below each other where a table gets tight, and the vault
  puts its form below the table under 1400 px.

### Changed
- **One image build per release**: only a version tag builds the image, tagged both
  `vX.Y.Z` and `latest` — `latest` is always a release, and a release is no longer
  built twice. Commits between releases build no image; the tests still run on every
  push.

### Docs
- **README**: what AICortex does in 30 seconds, a 5-minute try-out on your own
  computer (Docker, no login, port on loopback only) and new screenshots, including
  the Runners tab.

### Upgrading
- Pull `ghcr.io/ikarusmk/aicortex:v2.3.2` and redeploy; `data/` stays as it is.
  Nothing to set up.

## [2.3.1] — 2026-11-03
Scheduled jobs, easy to hand out: say "tomorrow at 8, do X" and it just runs.

### Added
- **One-off jobs**: `cron_add(…, once=True)` and *Only once* in the WebUI — the job
  switches itself off after its run, so a date like `0 8 4 11 *` doesn't come back a
  year later.
- **Your own default runner**: a job that names no runner goes to its person's default
  runner — their first runner, or the one they chose with *Make my default* — else to
  the standard runner. A job the default may not take (e.g. an admin's prompt for
  someone's Claude) falls back to the standard runner instead of waiting.
- **A step-by-step guide** to scheduled jobs and runners
  ([docs/scheduling.md](docs/scheduling.md)), a short *how it works* under Scheduled
  jobs and a link to the guide in both tabs.
- **Edit a vault entry**: click its name (or *Edit*) to give it a new value — never
  shown, old or new — or a new description; an empty value keeps the stored one.
  *Delete* is right there too. A person's own entry is changed in place (adding it
  again under the same name used to create a second one).

### Changed
- The playbook page opens on the **Library** while no run was ever started, and an
  empty run list points there — the playbooks no longer look gone.
- The vault list scrolls inside its panel on narrow screens, so *Delete* no longer slips
  out of view; button rows wrap.
- The upgrade check now also proves that playbooks and playbook runs survive an update
  (it never had a playbook folder), and checks 1.x note names only on 1.x data.

### Docs
- README: the default runner, one-off jobs, a link to the guide; the model guide
  explains `once` and the default runner.

## [2.3.0] — 2026-11-03
Runners: AICortex wakes your Claude with the due job, or asks your ChatGPT plan, an API
key or a local Ollama — each person sets up their own, and each job names its runner.

### Added
- **A Runner tab** in the WebUI, for everyone: a person's own runners (admins see all),
  their state, the link to the last Claude session, *Test*, on/off. Kinds:
  - **Claude (cloud task)** — AICortex wakes a Claude Code cloud task of yours through
    its API trigger and **sends the due job along**; Claude runs it on your own plan and
    reaches AICortex through your connector. The task needs one fixed sentence as its
    prompt, once (the WebUI shows it to copy). At most one wake-up every two minutes,
    `Retry-After` honoured, never retried (no duplicate sessions); a run is recorded with
    the session link.
  - **ChatGPT plan**, **Anthropic / OpenAI / OpenRouter / Gemini key**, and
    **OpenAI-compatible** (admins) — any address such as Ollama in the LAN, checked by
    the SSRF guard when saved and again for every request.
- **Keys from the vault**: a runner's key is typed in (stored as its own internal entry
  `RUNNER_KEY_<id>`) or picked from the vault by name — a person only their own entries,
  an admin shared ones too, the server's internal names never.
- **Each job names its runner**: `cron_add(…, runner=…)`, `runner_list`, a Runner column
  and field under *Scheduled jobs*. A job without one goes to the **standard runner**
  (one of an admin's); a Claude runner only takes jobs its owner wrote (prompt, name and
  recipient) — never ones written with an agent key.
- Limits: 10 runners per person (admins: none), keys up to 1,024 characters, job
  prompts up to 20,000 characters.
- *Test* on a runner: one tiny question without tools, or a harmless wake-up of the
  cloud task.

### Changed
- **AICortex confines each job itself**: where it asks the model, every job is a
  conversation of its own, confined to the owner's area (act-as) before the model sees
  it and released afterwards, even on errors; AICortex records the run. The model no
  longer sees act-as tokens and can't call `act_as_*`, `cron_due` or `cron_mark_run`
  (before, a job's prompt could talk the model out of confining itself).
- Runners tick in one thread and ask their models side by side in a worker (each with
  its own in-memory token, each job with five minutes), so a slow model holds up
  nobody else and never a Claude wake-up. Other people see only the name, kind and
  on/off of the standard runner.
- `cron_due` (an own runner with `RUNNER_TOKEN`) gets the jobs assigned to `external`,
  and the unassigned ones while no standard runner is on — as before.
- An act-as run is bound to the caller's identity **and credential**: the built-in
  runner and an own runner (both identity `runner`) no longer block each other.
- Vault names starting with `RUNNER_` are internal now — no service can use them.
- The 2.2 setup becomes the standard runner on first start; its key stays where it is
  and `runner_config.json` is left in place.

### Removed
- **Claude via `claude setup-token`**: Anthropic refuses a subscription token outside
  Claude Code (HTTP 429 "rate_limit_error: Error"). A 2.2 setup using it becomes a
  switched-off Claude runner with a note; set up a cloud task instead and delete the old
  token `RUNNER_LLM_KEY` in the vault.
- The runner panel under *Scheduled jobs* and its routes `/ui/api/runner` and
  `/ui/api/runner/config` (now the Runner tab and `/ui/api/runners`).

### Docs
- README: runners, the Claude cloud task, keys from the vault, Ollama; WebUI table.
- docs/authorization.md: the rights of a Claude runner; act-as per credential.

## [2.2.0] — 2026-11-03
Scheduled jobs run inside AICortex — on a Claude or ChatGPT plan or an API key, with
no runner container any more.

### Added
- **The runner is built in.** Tick *Runner on* under *Scheduled jobs → Set up the
  runner* and AICortex runs due jobs itself — no extra container, no key to copy.
  It checks once a minute without a model. A Claude subscription token (`claude
  setup-token`) or an Anthropic key goes to Anthropic's Messages API (the token as
  an OAuth bearer, no Claude Code needed); OpenRouter, OpenAI and Gemini keys go to
  their OpenAI-compatible chat APIs. The model's tool calls go through `/mcp` as the
  runner (an in-memory token), so roles, act-as and audit apply as for any caller.
  Every model answer has a size limit and a five-minute deadline, so a provider
  that never finishes can't hold up later jobs.
- **A ChatGPT plan as the runner's model access.** *Sign in with ChatGPT* in the
  runner setup uses OpenAI's sign-in for open-source apps: authorization code with
  PKCE, the ID token checked against OpenAI's keys, the tokens encrypted in the
  vault and refreshed by AICortex (an idle sign-in is renewed before its 30 days
  run out). Jobs run through OpenAI's Responses API; the model can be left empty
  for the account's first model, and the field suggests the account's models. A
  usage limit stops the run with a link to the plan's usage page; *Sign out*
  revokes the sign-in at OpenAI. A sign-in the vault can't store is revoked again
  right away, and the audit log names the ChatGPT account that was attached.

### Changed
- An Anthropic API key is one option in the runner setup; a setup saved with the
  former second option (`anthropic`) is read as that one and runs via the Messages
  API.

### Removed
- **The separate runner:** `runner/`, `runner/generic/` and the image
  `ghcr.io/ikarusmk/aicortex-runner` (no longer built). The built-in runner does
  their job.
- Creating **runner keys** and the `runner_config` tool — both existed only for the
  separate runner. The model key now never leaves AICortex except to the model
  provider. Runner keys already made and `RUNNER_TOKEN` keep working for an own
  runner.

### Fixed
- **Two runners could run the same job twice.** `cron_due` now hands a job to one
  runner per scheduled minute.

### Docs
- README *Highlights* and *Autonomy & scheduling* describe the built-in runner and
  the ChatGPT plan; `docs/clients.md`, `docker-compose.yml` and `.env.example` say
  that `RUNNER_TOKEN` is only for an own runner outside AICortex.

## [2.1.0] — 2026-11-02
The runner is set up in the WebUI, wakes the model only when a job is due, and no
longer misses minutes.

### Added
- **Runner setup in the WebUI** (*Scheduled jobs → Set up the runner*, admin):
  choose the model access — a Claude subscription token (`claude setup-token`),
  Claude Code with an Anthropic API key, or OpenRouter / Anthropic / OpenAI / Gemini
  with a model name — and create **runner keys**. A runner key connects exactly like
  `RUNNER_TOKEN` but needs no connector restart and can be revoked in the WebUI.
- `runner_config`: the runner fetches the model access from the vault. Only the
  runner gets it — never a person, an admin, an agent key, the REST API or an
  act-as run — and only once per run; services cannot use it either.
- **The runner as a ready image:** `ghcr.io/ikarusmk/aicortex-runner` is built with
  every release, so a NAS can pull it instead of building it.
- **Services with a user name and password** (HTTP Basic, e.g. AdGuard Home):
  `service_add(..., auth_scheme="Basic", username="…")` with only the password in
  the vault secret — the connector builds the header. A pre-built Base64 secret
  without a username keeps working.

### Changed
- **The runner wakes the model only when a job is due.** Both runners now ask
  `cron_due` themselves (no model) and hand the due jobs to the model in the prompt;
  before, the model was started every minute just to ask. The model working on a job
  cannot call `cron_due` or `runner_config`.
- The any-model runner (`runner/generic`) runs the model set up in the WebUI — also
  Claude via the Claude Code CLI — and falls back to `LLM_MODEL` without a setup. The
  Claude-only runner signs in with a runner key and `CLAUDE_CODE_OAUTH_TOKEN` /
  `ANTHROPIC_API_KEY`, without a login inside the container; older setups keep
  working.
- The runner prompt passes a job's act-as token to `act_as_begin`, so jobs with an
  owner run in that person's area.

### Fixed
- **Scheduled jobs could silently never run.** The runner slept 60 seconds *after*
  each run, so its checks drifted and whole minutes went unchecked, and `cron_due`
  only returned jobs due in that exact minute. Checks are now aligned to the clock,
  and `cron_due` hands out a missed minute for up to five minutes, once — never one
  from before the job existed.

### Docs
- README *Autonomy & scheduling*, `runner/README.md` and `runner/generic/README.md`
  describe the WebUI setup, runner keys and the model choice.
- WebDAV tool examples use a neutral folder name.

## [2.0.3] — 2026-11-02
A review release: playbooks are found by everyday words, starter skills ship with the
image, the scheduled-jobs page tells whether a runner is there, a much larger test
suite — and the defects it turned up are fixed. Update by pulling the new image.

### Security
- **CalDAV credentials could reach another server.** A calendar or event given as a
  full URL on a different host was requested with the endpoint's username and app
  password. Such URLs are now refused; credentials only go to the endpoint's server.
- **A line break in an event title could inject iCalendar properties** (an extra
  `ATTENDEE`, for example). Titles and descriptions are now escaped as one-line
  iCalendar text.
- **Multicast addresses passed the egress guard** although it is documented to block
  them (`224.0.0.1`, `ff02::1` count as "global" in Python). They are blocked now.
- **A task handoff message was readable by every person** under `AUTH_ENFORCE`: it
  was stored without its owner. It now belongs to the task's person.
- **The connect-time guard let multicast through as well** (it decides again when a
  client library resolves the host, against DNS rebinding); it uses the same rule now.
- **`tls_insecure` set to the text "false"** in a hand-edited config switched
  certificate checks off. Only an explicit yes (true, 1, "yes", "on") does now.
- **A line break in an FTP file name, user name or password could add options to
  curl's configuration** (curl does the FTPS uploads) — for example `output=` or `url=`.
  Line breaks are escaped now.
- **An inbound webhook revealed which hook names exist:** an oversized body got `413`
  for an existing hook but `401` for an unknown one. Authentication now comes first,
  so a stranger only ever sees `401`.
- **`secret_list` failed open:** if resolving the caller raised, a non-admin saw every
  person's private secret *names* (never values). It now falls back to shared names.
- An FTP endpoint whose vault secret is missing logged in with an empty password; it
  refuses now, like SSH, mail and IMAP.

### Fixed
- **A person confined to their own memory area saw none of their own notes** in
  `cortex_start` and `cortex_catalog` — both looked in `users/<id>` while notes are
  stored in `users_<id>`.
- `task_update` accepted any status text; it now refuses anything but open, claimed,
  blocked or done.
- Calendar events whose title the server folded over two lines (LF instead of CRLF)
  were shown cut short.
- A REST call with missing or wrong arguments answered `500 tool error`; it answers
  `400 invalid arguments` now, so a client can tell its own mistake from a server fault.
- `tenancy_status` told models that isolation is switched on with the retired
  `TENANCY_ISOLATE`; it follows `AUTH_ENFORCE`.
- The workspace quota counted the wrong way round: appends could outgrow it, and
  overwriting a file was refused although the new content fit.
- One `SKILL.md` that isn't valid UTF-8 made `skill_search` and `skill_list` fail for
  everyone; it is read tolerantly now.
- The WebUI answered `500` for a sign-in callback with a non-ASCII `state` and when
  editing a person whose entry in `policy.json` had been hand-edited into something
  other than an object.
- WebDAV listed the folder itself when `base_url` was percent-encoded (an e-mail user
  name such as `alice%40example.com`).
- Printing: a path without a leading slash (`ipp/print`) built a broken URL, and an
  HTML error page was read as a bogus IPP status instead of "no IPP response".
- The MCP gateway left an HTTP client open when the other server refused the
  handshake.
- IMAP: an unreadable `ca_bundle` surfaced as a raw error instead of "connect failed";
  an attachment named `..` made the whole fetch fail, and two attachments with the
  same name overwrote each other (the second is saved as `name-2.ext` now).

### Added
- **Starter skills ship with the image.** *Skills → Add starter skills* in the WebUI
  copies the bundled examples into the library — never over an existing skill.
- **The scheduled-jobs page shows the runner:** "no runner has checked in yet", "last
  seen …" or a warning when it has gone quiet. Jobs only run while a runner calls
  `cron_due`; before, nothing told you when none was there.

### Changed
- **`integration_search` finds the playbook tools for everyday words** — workflow,
  process, stage, approval and the German *Ablauf*, *Phase*, *Freigabe*, *Vorgehen*,
  *SDLC*. 2.0.2 found nothing for "welche Abläufe gibt es" or "start a multi-stage
  workflow with approvals" unless the word "playbook" was used.
- **`integration_search` names playbooks** in its description, next to devices, mail
  and the rest, so a model knows to look there.
- **`cortex_start` names the playbook library** in every briefing, not only when a run
  is already active.
- The start hook for Claude Code no longer assumes the MCP server is called
  "aicortex" (through a Claude account's connector it has another name).
- The install guides test the connection with `cortex_start` instead of `ping`, which
  is not among the eleven tools a client sees up front.

### Tests
- 1,530 tests (2.0.2: 266) and 98 % line coverage (59 %); every module is at
  83 % or more, 27 of 44 at 100 %. Every defect above has a test that failed before
  the fix. External systems (IMAP, SMTP, FTP, SSH, MQTT, CalDAV, WebDAV, IPP, other
  MCP servers) are replaced by fakes; DNS is scripted so the egress guard's real
  checks run.

## [2.0.2] — 2026-11-01

### Fixed
- **The last working rule could still be cut from the briefing** ("… 1 more"): the
  closing line of rule titles counted three characters too many per title, so it
  dropped a title even though the whole line fit.

## [2.0.1] — 2026-11-01
The start briefing now carries every working rule. Update by pulling the new image;
nothing else changes.

### Changed
- **`cortex_start` names every general working rule** (a `feedback` memory without a
  project). 2.0.0 showed the first four alphabetically and hid the rest behind
  "… N more", so a session started by a hook or a weak model missed most of them.
  Rules are listed with their text while there is room, the rest by title.
- **Pinned rules first:** tag a rule `pinned` (also `important`, `verbindlich`,
  `wichtig`) and it leads the list, with its text.
- **Rules that belong to a project** (a `feedback` memory with `project=…`) show up
  only when that project is worked on, under its project notes — not in every
  briefing.
- **General reference notes** (no project) are named in the briefing, so a model
  knows they exist and can `memory_read` them.

### Documentation
- `docs/clients.md`: a step-by-step guide for people, keys and connecting clients,
  including what a key can see under `AUTH_ENFORCE`; `INSTALL_FOR_AGENTS.md` gains
  the matching phase 8; `docs/pocketid-setup.md` covers client secrets in Pocket ID
  2.14 and later. The WebUI's tool profile reads "11 tools" (was "10").

## [2.0.0] — 2026-10-31
Optimized and updated, nothing removed (`bootstrap` and all tools keep working).

### Upgrading from 1.x
Pull the new image and restart with the same `data/` folder and `.env`; there is no
migration step. Checked end to end against a real 1.12.2 installation
(`tests/upgrade_check`): connected apps stay signed in (access tokens, refresh tokens
and registered clients carry over), `RUNNER_TOKEN` and REST API keys keep working, and
memory, skills, vault, services, tasks, sessions, agents and cron jobs are read as they
are. What changes for you:
- **Clients see eleven tools up front** (profile `core`), not all 128. Every tool can
  still be called by its name, and `integration_search` / `integration_call` reach all
  of them. To keep the old full list, set `TOOL_PROFILE_DEFAULT=full`.
- **`bootstrap` is still listed** and now returns the short `cortex_start` briefing;
  project instructions that say "call bootstrap" can stay.
- **Notes with umlauts or other non-ASCII titles** get readable file names now
  (`größe-der-düse.md`); notes 1.x saved under its ASCII names are still found and
  updated in place.
- **Shared skills** can be changed only by an admin or their author when `AUTH_ENFORCE`
  is on (1.x let every user role overwrite them). Skills from 1.x have no author, so
  admins change them.
- **Per-person views** of tasks, inbox and sessions apply under `AUTH_ENFORCE`; items
  from 1.x have no owner and stay visible to everyone, as before.
- **Without OIDC**: `ALLOW_INSECURE=1` setups stay open as before. Without OIDC and
  without `ALLOW_INSECURE`, 1.x listened on localhost only, without a token; 2.0 listens
  on `MCP_HOST` and asks for a bearer token (`RUNNER_TOKEN` or an agent key), and the
  WebUI's no-login admin works only on the machine itself.
- **Browser access under a dotted name** (e.g. a VPN name such as
  `nas.tail1234.ts.net` or a router name such as `nas.localdomain`) needs that origin in
  `MCP_ALLOWED_ORIGINS`; IP addresses,
  names without a dot, `.local` / `.lan` / `.home.arpa` / `.internal` names and the
  `BASE_URL` host work as before. Native clients are not affected.
- **`webhook_send` by non-admins** can attach only their own personal secrets; shared
  secrets stay with registered services (`call_service`).
- **`cron_add` refuses schedules that never run** (out-of-range fields, dates such as
  30 February) instead of storing a job that silently never fires; editing a job keeps
  its creation date and last run.
- **The WebUI is open to every signed-in person** for their own area (notes, jobs,
  playbook runs); the management pages stay admin-only as before.
- Start a new session in your AI apps after the update so they load the new tool list.

### Added
- **Playbooks** — multi-stage work where a person approves each hand-off, modelled on
  Anthropic's "The AI-native SDLC playbook" (bundled as a template, in our own words):
  every stage produces a versioned artifact (`intent.md` → `spec.md` → `plan.md` …,
  text and/or a repo path, commit or URL); approval per stage in the WebUI (default),
  in chat (only by the person in an interactive app, never an agent key, the runner or
  a scheduled job) or none. `playbook_*` tools, `cortex_start` shows each run's next
  step, WebUI library with a stage editor and runs with approve/reject. See
  `docs/playbooks.md`.
- **WebUI for everyone**: each signed-in person manages their own **notes** (search,
  filters, conflict detection), **scheduled jobs** (plain-language schedule, next/last
  run, presets with a preview) and **playbook runs**; admins also **edit services and
  devices** of every registry (forms built from the `*_add` tools). Every page takes the
  same policy path as the MCP tools. Screenshots in the README.
- **Any AI client can connect.** Per-agent keys (`ak_…`) and `RUNNER_TOKEN` work as
  bearer tokens on `/mcp`, with or without OIDC (Hermes, OpenClaw, Codex, Claude Code,
  LM Studio, Open WebUI, LibreChat, Claude API, OpenAI Responses/Agents SDK). OAuth for
  the Claude and ChatGPT apps is pinned by tests (401 challenge, resource metadata,
  PKCE S256, DCR and CIMD). Setup for every client: `docs/clients.md`.
- **`cortex_start(project, task)`** — a short per-caller briefing (≤ 4,000 characters;
  ~650 in a typical case) instead of the ~56,000-character `bootstrap` dump; `bootstrap`
  is now its alias, the full list moved to **`cortex_catalog`**.
- **Projects**: `project` on `memory_write`, `session_save` and `task_add`;
  `memory_search(project=…)`; `cortex_start` brings the project's notes, open tasks and
  last session along.
- **Tool profiles**: `core` (default) lists eleven tools (5,675 instead of 50,667
  characters of descriptions and schemas); `integration_search` / `integration_call` reach every other tool under the
  same permissions. `full` lists everything. Per agent key or `TOOL_PROFILE_DEFAULT`.
- **Tool annotations** (title, read-only, destructive, open-world) on all 128 tools.
- **Multi-agent**: everyone can `agent_register` (names belong to their person),
  attribution `person/agent` from the token, per-person views of tasks/inbox/agents/
  sessions under enforcement, leased task claims, versioned memories with
  `expected_version` conflict detection.
- **WebUI**: vault state banner, description / usage / who-and-when per secret, owner
  picker, overwrite confirmation, key rotation (`STORAGE_ENCRYPTION_KEY_OLD` +
  re-encrypt) and plaintext-vault encryption; new page **Connect clients** (agent keys
  with ready-made setup snippets).
- **Search**: `memory_search` by words with ranking and type/tag filters;
  `skill_search` weighted by name/description/tags/body with a cached index.

### Changed
- **fastmcp 3.4.7 → 4.1.4** (MCP 2026-07-28 alongside 2025-11-25). Libraries the app
  imports directly are now declared.
- **Server instructions**: 1,300 characters in English (was 27,073, bilingual, with a
  mandatory banner). They and the `cortex_start` briefing tell the model to reply in
  the user's language, so English instructions don't pull the answer into English.
- Host check: any Host header by default; `MCP_ALLOWED_HOSTS` / new
  `MCP_ALLOWED_ORIGINS` for a strict list.
- Without OIDC the WebUI's no-login admin session is limited to direct requests on the
  machine itself.

### Security
- Cron act-as was process-wide: during a scheduled job every concurrent client acted as
  the job owner. Now bound to the runner that started it.
- `*_env` references could resolve the server's own keys (e.g. `webhook_send` with
  `secret_env="STORAGE_ENCRYPTION_KEY"` by a non-admin). Server settings are never
  resolved as integration secrets.
- On fastmcp 4 the RUNNER_TOKEN lost its runner identity behind MultiAuth and fell back
  to the admin default; identity now uses `original_client_id`.
- Agent keys never become admin by default and keep their scopes on `/mcp`.
- `tools/list` hides tools the caller's role may not call.
- Security review (October 2026), every finding reproduced first and pinned by a test
  (`tests/test_security_review.py`, `tests/test_host_guard.py`):
  - **DNS rebinding**: the Host/Origin guard was never installed, so a website opened
    in a local browser could use the WebUI's no-login admin (store secrets, mint agent
    keys) and, in open mode, every tool. A new guard refuses browser requests from
    foreign sites (403) and the WebUI under foreign host names (421); native clients
    are unaffected. `MCP_ALLOWED_HOSTS` now really makes the host check strict.
  - **`webhook_send`** let any user send shared vault secrets — and, by naming the
    storage path, other people's private ones — to a URL of their choice. Non-admins
    can now send only their own personal secrets; storage paths are never resolved.
    `STORAGE_ENCRYPTION_KEY_OLD` is internal like the current key.
  - **REST keys in open mode** scheduled cron jobs as another person and fetched their
    act-as tokens; cron now uses the key's person and role.
  - `cortex_catalog` and `agent_list` showed every person's cron prompts and agents;
    `session_prune` deleted other people's sessions; `cron_add` replaced another
    person's job of the same name. All per person now (admins: everything).
  - Line breaks in memory fields forged the server-stamped `author` and `version`.
  - A non-ASCII bearer token crashed the verifier (500 instead of 401).
  - `vault_rekey` left the previous generation in `secrets.enc.bak` (plaintext, or
    readable with the retired key).
  - A second review of these fixes found no bypass; its leftovers are fixed too:
    malformed, repeated or userinfo-carrying Host/Origin headers count as foreign,
    Unicode `BASE_URL` hosts match the punycode browsers send, `*.domain` entries work
    in `MCP_ALLOWED_HOSTS`, and a `---` inside a title or description no longer ends
    the header of a note or skill early (it lost type, author and version).
  - A review of the new WebUI views, cron and playbooks (same day) found no access to
    other people's data; fixed: nested YAML aliases in playbook stages could exhaust
    memory and very long cron schedules could stall the server (both refused now,
    schedules evaluated field by field); an AI could save a playbook whose stages skip
    the human approval (only a person in the WebUI may relax an approval, and relaxed
    approvals count only in the author's own runs); the bundled template could be
    claimed or silently overwritten (admin-only, never overwrites); a template line
    `---` could break a playbook's header; weekday ranges ending in 7; a run limit per
    person; 500 errors on odd input.
  - Sending tools (`mail_send`, `webhook_send`, `mqtt_publish`, `print_document`,
    `caldav_add_event`) are marked destructive, so clients ask before running them.
  - Pillow ≥ 13 (39 advisories in 12.x for decoding untrusted images); pytest ≥ 9.0.3
    for development.

### Fixed
- Parallel tool calls lost data (40 parallel `task_add` kept 8) and an unreadable store
  file was read as empty and overwritten; stores are now locked and written atomically.
- `task_claim` let a second agent take over a claimed task.
- Partial updates could not clear a field (`auth_scheme=""` stayed `Bearer` → 401) and
  defaults overwrote stored values; only passed arguments change now.
- Per-user MQTT/FTP grants never matched (wrong parameter names); `ftp_list` was ungated.
- Personal vault secrets could not be deleted from the WebUI.
- Duplicate hint fired on common words; umlaut and non-Latin titles were mangled or
  collided as `untitled`; auto-capture re-staged candidates on every update.
- `call_service` accepts JSON arrays and pages long responses instead of cutting at
  4,000 characters.

### Fixed (WebUI)
- **WebUI: overview tiles now navigate to their section.** Tiles looked clickable but
  did nothing; each stat tile is now a real `<button>` that jumps to its page
  (skills/categories → Skills, secrets → Vault, services/devices → Services & devices,
  users → Users) with hover + focus-visible states; the version tile stays passive.

## [1.12.2] — 2026-07-12
### Fixed
- **Connector login REALLY fixed: the OIDC proxy no longer forwards `resource` upstream —
  and the v1.12.1 pin alone was NOT enough.** Deeper finding: fastmcp **3.4.3 already
  contains** the new authorize flow (CIMD, `/consent`, RFC 8707 `resource` forwarding);
  v1.11.0 only *appeared* fine because existing connector sessions renew via refresh
  tokens — the last FRESH login predated the 3.4.x flow, so the regression stayed
  invisible until a reconnect was needed. The real, version-independent fix is
  configuration: the proxy is now built with **`forward_resource=False`** (Pocket ID
  never sees the parameter it rejects) and **`require_authorization_consent="external"`**
  (no `/consent` interstitial — the IdP's passkey login IS the consent, restoring the
  familiar flow). Both are signature-checked so a fastmcp without these kwargs can never
  break boot. New guard tests (`tests/test_oauth_upstream.py`) pin this contract against
  the installed fastmcp, so a future version bump that renames or breaks either knob
  fails CI instead of breaking logins in production.

## [1.12.1] — 2026-07-12
### Fixed
- **Claude custom-connector login broke after the v1.12.0 image rebuild — fastmcp is now
  EXACT-pinned to 3.4.3.** The v1.12.0 image silently picked up fastmcp **3.4.4** (the
  requirement was a loose `>=3.4.2,<4`), which changes the OIDC proxy's upstream
  behaviour: the RFC 8707 `resource` parameter that MCP clients (Claude) send with
  `/authorize` is now **forwarded to the upstream IdP**. IdPs without Resource-Indicator
  support (e.g. Pocket ID) reject the authorize request with `invalid_request — "The
  'resource' or 'scope' parameter is invalid"`, so the connector login fails after the
  (also new) `/consent` interstitial. Root cause proven end-to-end: container logs show
  the IdP callback error, and the same Pocket ID authorize URL succeeds without
  `resource` and fails with it. 3.4.3 dropped the parameter upstream, which is why
  v1.11.0 logged in fine — nothing in the deployment (callback URIs, env, client
  registration) was at fault. Lesson applied: the auth-critical dependency is pinned
  exactly and only bumped via a tested Dependabot PR — an image rebuild can no longer
  silently change the auth stack.

## [1.12.0] — 2026-07-12
### Added
- **Admin WebUI at `/ui` — manage the brain from a browser, no terminal needed.**
  Served by the same container alongside `/mcp` (no extra service, no extra port), in the
  AICortex banner look, language switchable **DE/EN**. Login is a standard OIDC
  authorization-code flow **with PKCE against your own IdP** (e.g. Pocket ID) — register
  ONE extra redirect URI: `<BASE_URL>/ui/callback`. Sessions are signed HttpOnly cookies;
  every management endpoint is **admin-only** (role via the same `authz.role_for` chain as
  MCP), mutations carry a CSRF header, and the app is fully self-contained (strict CSP, no
  CDNs, no build step). Without OIDC (local testing) the UI is open on the
  localhost-only bind, mirroring the server's own trust model. Pages:
  - **Overview** — version, enforce status, counts (skills/categories/secrets/services/
    devices/users).
  - **Vault** — see secret NAMES (values never leave the vault), add tokens/API keys/
    passwords (shared or per-user), delete. Write-only by design.
  - **Skills** — browse by category, view, create/edit (name, category, description,
    tags, Markdown), delete. Same house rules as `skill_write` (category required).
  - **Users** — roles (admin/user/viewer) + per-user areas (memory/vault/services/skills/
    device grants) in `policy.json`, add/edit/remove.
  - **Services & devices** — read-only inventory of EVERY registry (services, mqtt, ftp,
    printers, scanners, webdav, caldav, ssh, mail, imap, mcp, webhooks) with target and
    the referenced secret NAME. Only constructed fields leave the endpoint — an unknown
    config key can never leak through the UI.
  - **Logs** — the authz audit log (who called which tool, allowed/denied and why),
    newest first, filterable; reads at most the last 1 MB per request.
  The endpoints call the SAME module-level functions as the MCP tools
  (`secrets_store.vault_*`, `skills` helpers, `tenancy._write_policy`) — UI and assistant
  cannot drift. UI actions are audited (`ui:*` in the authz audit log; never a value).
  New: `app/webui.py`, `app/webui_static/`, `tests/test_webui.py`. Opt-out: `UI_ENABLED=0`.
### Fixed
- **Bambu FTPS upload now goes through `curl` — the v1.10.2 session-reuse shim was not
  enough.** Python's ftplib still failed to resume the TLS session on the DATA channel
  against the P1S SD store, so `STOR` kept hanging into "read operation timed out". FTPS
  uploads (implicit *and* explicit) now shell out to `curl` (config via stdin — the
  password never touches argv), which implements FTPS session reuse natively and is the
  community-proven path for these printers. The vetted egress IP is pinned into curl
  (`resolve=`) so the SSRF/anti-rebinding guarantee survives the external process; TLS
  knobs (`ca_bundle` > `tls_insecure` > verify) match `netguard.ssl_context`. Plain FTP
  keeps the ftplib path. `curl` added to the image. New tests: `tests/test_ftp_curl.py`.

## [1.11.0] — 2026-07-08
### Changed
- **Unified TLS handling across every integration.** A single `netguard.ssl_context(cfg)`
  now builds the SSL context for the socket-based clients (FTP, MQTT, IMAP, SMTP) from the
  SAME two knobs the HTTP clients already resolve via `netguard.tls_verify` — `ca_bundle`
  (pin a CA/cert — the safe way) and `tls_insecure` (verification off). Secure by default
  everywhere; only the admin-only `*_add` tools ever set these.
### Added
- **`ca_bundle` / `tls_insecure` now offered on ALL TLS endpoints.** Previously only
  `service_add` / `scan_add` / `webdav_add` / `caldav_add` had the full pair. `ftp_add` and
  `mqtt_add` gained `ca_bundle`; `imap_add` and `mail_add` gained BOTH — they had *no* TLS
  opt-out at all, so a self-signed LAN mail server was unreachable. One consistent
  convention across services, scan, webdav, caldav, ftp, mqtt, imap and smtp — no more
  per-device patching (includes the v1.10.2 FTPS session-reuse fix).

## [1.10.2] — 2026-07-08
### Fixed
- **FTPS upload to Bambu Lab printers (and other `require_ssl_reuse` servers) no longer
  hangs → "read operation timed out".** `ftp_upload` over implicit FTPS opened the data
  channel with a *fresh* TLS session; servers that require the data connection to **resume
  the control channel's TLS session** (vsftpd `require_ssl_reuse`, Bambu P1S SD store)
  stall such transfers. `_ImplicitFTP_TLS` now overrides `ntransfercmd` to wrap the data
  socket with `session=self.sock.session`, so the session is reused as required. This
  unblocks sending sliced `.gcode.3mf` jobs to the printer's SD for a `project_file` start.
### Fixed
- **`scan_document` left the scanner "busy" → the next scan failed with HTTP 503.** eSCL
  requires GETting `NextDocument` repeatedly until it returns **404** — that both yields
  each page AND tells the device the job is finished so it releases. The tool stopped after
  the first page, so the scanner never got the end-of-job signal and stayed occupied; the
  operator had to cancel at the device panel and the following scan came back 503. It now
  drains to 404 (also riding out `503` warm-up) and correctly handles multi-page ADF scans
  (page 1 keeps the given filename, extra pages get `-N`; each is pushed to Paperless when
  requested). New tests: `tests/test_scan_drain.py`.
### Audited — no change needed
- **IPP printing** was checked for the same class of bug (requested alongside the scan
  fix). `print_document` submits an **atomic IPP `Print-Job`** — all operation attributes
  + `end-of-attributes` + the document bytes go in a single POST with `Content-Length`, so
  there is no poll-until-done step to forget and the printer is not left waiting/occupied.
  The other device tools (mqtt / ftp / webdav / ssh) are fire-and-forget or atomic too;
  only eSCL scanning had the drain-until-done pattern.

## [1.10.0] — 2026-07-07
### Added
- **`fs_view` — the assistant can now SEE workspace files with vision, not just OCR.**
  A new workspace tool renders any image or PDF under `/data/work` into image content the
  model reads directly: scanned pages (`scan_document`), e-mail attachments
  (`imap_fetch save_attachments`), webdav/ftp downloads and print sources. This closes a
  real gap — until now a scan came back only as a saved path or (via Paperless) mangled
  OCR text, so multi-card scans and decorative titles were unreadable. `fs_view(path[,
  page, max_pages])` downscales raster images (Pillow, EXIF-aware) and rasterizes PDF
  pages (pypdfium2). Same hard sandbox as the other `fs_*` tools, a size cap
  (`FS_VIEW_CEILING_BYTES`, default 30 MB) and graceful degradation (a clear message if an
  optional wheel is missing) instead of a crash.
### Changed
- New deps: `pillow>=11,<13`, `pypdfium2>=4,<6` — both have manylinux **cp314** wheels,
  verified via `uv pip compile --only-binary :all:` against the runtime image's Python
  before adding (pypdfium2 is Apache/BSD, bundles PDFium, needs no system libs like poppler).
- Docstrings + bootstrap catalog: `scan_document` and `imap_fetch` now point at `fs_view`
  for viewing; the workspace catalog line lists it.
### Tests
- `tests/test_imaging.py` — image normalize/downscale + PDF render round-trips (synthetic
  inputs, no fixture files).

## [1.9.5] — 2026-07-07
### Added
- **Per-service TLS options** — `service_add` now accepts `tls_insecure` and `ca_bundle`,
  and `call_service` honours them via the existing `netguard.tls_verify()` resolver
  (secure by default, #10 pattern — same as `scan_add`/`webdav_add`). This makes
  self-signed LAN services (e.g. a Crafty panel, which failed with
  `SSL: CERTIFICATE_VERIFY_FAILED`) reachable without weakening the default:
  verification stays ON unless an admin explicitly opts out, `ca_bundle` (pinned cert)
  takes precedence over `tls_insecure`, and `service_list` visibly flags such services
  with `[TLS-INSECURE]`. Merge-safe: updating other fields never resets a configured
  TLS opt-out. New tests: `tests/test_service_tls.py`.

### Changed
- `netguard.tls_verify` docstring now lists `service_add` among the admin-only writers
  of TLS opt-out configs.

## [1.9.4] — 2026-07-06
### Fixed
- **auth/transport:** the connector no longer returns **421 Misdirected Request** on the
  OAuth discovery/registration routes behind a reverse proxy. fastmcp 3.4.3 enforces the
  MCP HTTP transport's Host/Origin DNS-rebinding guard, which rejected every request whose
  `Host` was the public domain (e.g. `agent.example.com`) — so Claude's client registration
  failed with "Registrierung beim Anmeldedienst fehlgeschlagen". The server now derives the
  Host/Origin allow-list from `BASE_URL` (already required for OIDC, so it can't drift) and
  passes it to `mcp.run(...)`; `localhost`/`127.0.0.1` stay allowed by the guard's defaults,
  and `MCP_ALLOWED_HOSTS` (comma-separated) can add more for edge setups. Protection stays
  **on** — unknown hosts are still rejected. Regression introduced by the 3.4.3 bump in 1.9.3.

## [1.9.3] — 2026-07-06
### Changed
- **Runtime bumped to Python 3.14** (`python:3.14-slim` base image) and dependencies
  refreshed to current majors: **paramiko 5** (SSH/SFTP), **py-key-value-aio 0.4.5**
  (encrypted DiskStore), **fastmcp 3.4.3**. Each was validated before merging — the exact
  APIs used by `server.py`/`ssh_tools.py` still resolve, all `linux/cp314` wheels exist
  (incl. `uvloop`), and the test scripts pass on 3.12 and 3.14. No behaviour or API change
  for clients; a maintenance/hardening refresh.
- CI actions pinned to current majors (checkout, buildx, login, metadata, build-push).

### Added
- **Real test signal on every PR and push** (`tests.yml`): installs the pinned deps on the
  runtime Python, byte-compiles the app, and runs the `tests/` scripts. `build.yml` only
  builds/pushes the image, so PRs previously had no automated check.
- **Dependabot patch auto-merge** (`dependabot-auto-merge.yml`): patch-level dependency PRs
  self-merge after an in-job gate (deps resolve + byte-compile + test scripts + advisory
  `pip-audit`); minor/major bumps stay open for manual review. Self-contained — no branch
  protection or required status checks needed.

### Fixed
- Test modules no longer hardcode an absolute local path on `sys.path`; the `app/` dir is
  resolved relative to each test file, so the suite runs in any checkout and in CI.

## [1.9.2] — 2026-07-06
### Fixed
- **skills:** the frontmatter parser now falls back to tolerant, line-based parsing (like
  `memory.py`) when strict YAML fails — so a skill whose `description` (or another value)
  contains a colon (e.g. `Quelle: Paul Hudson`) no longer silently loses its category and
  description and lands in "uncategorized". Already-affected files are repaired automatically
  on read, without rewriting them.
- **skills:** `skill_write` now serializes frontmatter via `yaml.safe_dump` (automatic
  quoting for colons, quotes, `#`, unicode) — invalid frontmatter can no longer be written.

## [1.9.1] — 2026-07-03
### Performance
**bootstrap no longer re-scans the whole brain on every call.** The catalog rebuilt every
section from scratch each time — most expensively reading ALL `SKILL.md` files just to
count categories (hundreds of full-file reads per call), which is what made a cold
bootstrap on NAS storage take minutes.

- New `catalog_cache`: each file-reading section (skills, memory, the JSON service/device
  registries) is cached, keyed by a **cheap stat signature** of its source directory
  (file count + newest mtime + total size — metadata only, **no file reads**). Unchanged →
  cached lines returned without opening a file; changed → only that one section
  re-renders. Persisted to disk (`CATALOG_CACHE_FILE`, default `/data/.catalog_cache.json`)
  so even the first bootstrap after a restart is fast. Fail-open: a cache error renders
  live — never wrong, only faster. Scales as memory/skills/… fill up over months.
- `sessions.recent()` reads the session files **once** (was twice — `_prune()` + `_all()`);
  pruning stays on save/list/load, not on the frequent bootstrap read.
- Output is byte-identical to before — tests assert cold == warm and that a content edit
  invalidates + recounts. New `tests/test_catalog_cache.py`.
- **`uvloop`** (libuv) is now the event loop: uvicorn auto-detects it and `server.py`
  installs it explicitly at startup (logged, so it's verifiable), for faster async I/O on
  the connector's real bottleneck — the network/event loop. Guarded fallback to the
  default asyncio loop where uvloop isn't available (e.g. Windows).

## [1.9.0] — 2026-07-03
### Added
**Native REST API** — a plain-HTTP layer next to `/mcp` so non-MCP clients (n8n,
LangChain, OpenAI-compatible tools, scripts) can call AICortex tools directly, through
the **same** authorization and per-user areas as an OIDC session (no second permission
model):

- `GET /api/v1/tools` (tools this key may call, with JSON schemas), `POST
  /api/v1/tools/<name>` (invoke; body = JSON args), `GET /api/v1/openapi.json`
  (auto-generated OpenAPI 3.1 of this key's tools). Served outside the MCP OAuth, like
  `/hooks/*`. Optional **SSE** (`?stream=1`) with heartbeats for long-running tools.
- **Per-user API keys** (`apikey_create` / `apikey_list` / `apikey_revoke`, admin-only):
  a key maps to an identity and runs the exact same authz/tenancy pipeline —
  **never admin by default**. Keys are **hashed at rest** (SHA-256 of a 256-bit secret,
  constant-time compare), **default-deny** with a per-key `scopes` allow-list, and a
  hard **denylist** (`secret_*`, `apikey_*`, `tenancy_*`) no key can reach. Optional
  expiry; full CRUD incl. revoke(=delete). Shown once at creation.
- Per-key **rate limiting** (`API_RATE_PER_MIN`, default 60/min) and body cap
  (`API_MAX_BODY_BYTES`). Whole layer toggles with `API_ENABLED`.
- A request-scoped identity (`contextvars`) so concurrent REST requests never bleed
  identity, and in-tool self-scoping (`service_list`/`secret_list`/memory) resolves to
  the key's owner. New: `docs/rest-api.md`, `tests/test_apikeys.py`.

**Proxy:** expose `/api/*` (like `/hooks/*`) past the reverse proxy without OIDC — never
`/mcp`.

## [1.8.0] — 2026-07-02
### Security
Full-repo security audit (manual review + NAS security skills: MCP-server audit, XXE,
indirect-prompt-injection, BFLA/BOLA). All findings fixed:

- **Per-user areas now cover DEVICE endpoints** (were: services/skills only). Under
  `AUTH_ENFORCE=1` a non-admin's use of `caldav`/`imap`/`webdav`/`ssh`/`mail`/`print`/
  `scan`/`mqtt`/`ftp`/`mcp` action tools is confined to the endpoints an admin
  assigned (**default-deny**) — closing an isolation gap (BOLA/BFLA) where these
  registries were unguarded. Grant with `tenancy_set(identity, grant="caldav=…;
  ssh=all")`. Enforced centrally in the authz middleware.
- **Inbound webhook payloads are labeled UNTRUSTED** in the inbox (indirect
  prompt-injection defense) — a reader is told to treat them as data, not instructions.
- **`ssh_upload` path check uses ancestry, not string prefix** (the `/data-backup`
  sibling bypass, missed in the 1.6.3 sweep).
- **CalDAV/WebDAV XML parsed with `defusedxml`** (entity-expansion / billion-laughs
  DoS from a malicious or compromised registered server).
- **Webhook hardening:** shared token read from the `X-Webhook-Token` **header only**
  (never a URL query — no secret in proxy logs); unknown-hook and bad-auth both return
  a uniform `401` (no hook-name enumeration); byte-safe constant-time compare.
- **IMAP search injection** — quotes/CRLF stripped from the free-text query.
- **Published port binds to loopback by default** (`BIND_ADDR`, default `127.0.0.1`)
  so `/mcp` and `/hooks/*` aren't reachable from the LAN past the reverse proxy.
- **Supply-chain:** `defusedxml` added + floors tightened, a `pip-audit` advisory gate
  in CI, and Dependabot for pip / GitHub-Actions / Docker.
- **Hardening:** act-as replay-guard evicts the oldest jti (no wholesale clear);
  documented the SSRF guard's reliance on the default (socket) DNS resolver.

Verified clean: no committed secrets/data (only `.gitkeep`), container runs non-root,
SSRF egress guard on every outbound path, act-as never escalates to admin, cron
owner-scoping BOLA-safe, `*_add`/`*_delete` admin-gated, vault fail-closed.

## [1.7.2] — 2026-07-02
### Changed
- **Onboarding brought up to v1.7.** The connector `guide` (loaded by `bootstrap`
  and sent as the server `instructions`) and `docs/client-project-instructions.md`
  now cover what a fresh LLM was missing: **per-user areas / default-deny** and how
  an admin grants them (`tenancy_set`), **cron act-as** (`owner`, `act_as_begin/end`,
  the per-job token), and a **complete CRUD delete list** (imap / caldav /
  caldav_delete_event / webhook / tenancy_unset). So a new session understands the
  multi-tenant model — not just how to call individual tools.

## [1.7.1] — 2026-07-02
### Added
- **`caldav_delete_event`** — completes CalDAV CRUD. Shipping `caldav_add_event`
  without a matching delete in 1.7.0 was an oversight; every write now has its
  delete. `caldav_list_events` additionally returns each event's **href** as the
  delete handle. State-changing → confirm first.

## [1.7.0] — 2026-07-02
### Added
- **Webhooks — inbound receiver + outbound sender.** Inbound: a public
  `POST /hooks/<name>` route (served alongside `/mcp/` via FastMCP `custom_route`,
  outside the MCP OAuth) lets external services push events that land in the inbox —
  making the brain event-driven. It authenticates itself with a per-hook **shared
  secret token and/or HMAC signature** (constant-time), rejects unknown/unsigned
  requests, caps body size, and only deposits into the inbox (never the tool
  surface). `webhook_add` / `webhook_list` / `webhook_delete` (admin). Outbound:
  `webhook_send(url, json_body)` — a thin SSRF-guarded POST for notifications.
  **Operator note:** expose only `/hooks/*` past the reverse proxy's auth, never
  `/mcp`.
- **CalDAV — calendars as data.** `caldav_add` / `caldav_list_calendars` /
  `caldav_list_events` / `caldav_add_event`: discover calendars, list events in a
  time range, and create an event (PUT of an iCalendar object) — over CalDAV
  (PROPFIND / REPORT calendar-query / PUT) with httpx, no extra dependency. Same
  posture as WebDAV: SSRF-guarded connects, TLS verified by default, app-password
  from the vault; `caldav_add`/`caldav_delete_endpoint` admin-only.
- **IMAP — read incoming email.** The read-side counterpart to SMTP `mail_send`:
  `imap_add` / `imap_list` / `imap_search` / `imap_fetch`. Reads are read-only
  (BODY.PEEK, `readonly` select) so nothing is marked seen; attachments optionally
  saved to `/data/work`. Same security posture as SMTP — host passes the SSRF guard
  and the connect is wrapped in `netguard.guard()`; `imap_add`/`imap_delete_account`
  are admin-only, the read tools are viewer-safe.
- **Running version is now observable.** A single `version.__version__` is logged
  at startup (`[AICortex] version X starting`), returned by `ping`, and shown in
  the `bootstrap` catalog header — so you can tell which build a container is
  actually running without fingerprinting its source. (The stamp always mirrors the
  newest release; it moves only when a release is cut.)
- **Per-user service/skill areas + per-user cron (act-as).** Tenancy now extends from
  private data to shared capabilities and scheduled jobs, gated by `AUTH_ENFORCE`
  (the separate `TENANCY_ISOLATE` switch is retired — "enforce means enforce").
  - `policy.json` users gain `services`/`skills` = `all` | `none` | allow-list of
    names/categories. Under enforce these are **default-deny** and **fail-closed**
    (errors → deny + audit); homelab mode (`AUTH_ENFORCE=0`) runs no checks.
  - Enforced in `call_service`/`service_list` and `skill_load`/`skill_resource`/
    `skill_search`/`skill_list`; managed via `tenancy_set(services=…, skills=…)`.
  - **Cron act-as:** a job can run as an owner. A non-admin schedules only as
    themselves; an admin as anyone. `cron_due` mints a short-lived per-job capability
    token (HMAC, HKDF-derived from `STORAGE_ENCRYPTION_KEY`, 5-min TTL, single-use);
    the runner presents it via `act_as_begin`/`act_as_end`, so it holds no standing
    authority and a running job is scoped to its owner at the owner's own privilege.
    See `docs/per-user-areas.md`.

### Changed
- **Isolation now rides on `AUTH_ENFORCE` (breaking).** The separate `TENANCY_ISOLATE`
  switch is **retired** — "enforce means enforce", one switch. With `AUTH_ENFORCE=1`
  (the default) non-admins are now confined (own memory scope + private vault) and
  **default-denied** services/skills until an admin grants them; homelab mode
  (`AUTH_ENFORCE=0`) is unchanged. A leftover `TENANCY_ISOLATE=1` in `.env` becomes a
  harmless no-op. **Admins are unaffected.** Docs updated across README, `.env.example`,
  `SECURITY.md`, `docs/authorization.md`, `docs/pocketid-setup.md`, `docs/secrets.md`.

## [1.6.3] — 2026-07-01
### Security
- **SSRF hardening on mail / scan / print (CRITICAL).** These dispatchers only
  ran the `check_host()` preflight but connected *outside* `netguard.guard()`, so
  the DNS-rebinding / TOCTOU window the guard exists to close was open exactly
  there. Their actual connects (SMTP, eSCL, IPP, Paperless upload) are now wrapped
  in the guard.
- **Guard also blocks cross-host redirects.** While a guard is active, **every**
  outbound DNS resolution is re-checked against the egress policy (not just the
  named host), so an already-registered endpoint can no longer 30x-redirect the
  client onto `169.254.169.254` or a LAN panel. Public and operator allow-listed
  ranges still pass.
- **Injective per-user namespace (HIGH).** `tenancy._safe()` sanitised distinct
  identities to the same slug (`bob@x.com` and `bob.x.com` → `bob_x_com`), which
  could fold two people onto one memory scope / vault namespace. It now appends a
  short hash of the raw identity, so namespaces are collision-free. (No migration:
  isolation is opt-in and no confined users exist yet.)
- **Path checks use ancestry, not string prefix (MEDIUM).** The `/data` sandbox
  checks in `mail_send`, `print_document` and `webdav_upload` used
  `str(p).startswith("/data")`, which also matched siblings like `/data-backup`.
  Now an ancestry check (`DATA_ROOT in p.parents`).
- **Auth fail-open now leaves an audit trail (LOW).** A degrade-to-allow in the
  authz middleware is recorded to the audit log instead of passing silently.
- **`secret_list` name-leak hardened (LOW).** A non-admin caller under isolation
  (including an unresolved one) no longer sees other users' secret *names* — only
  shared plus their own. Values were never exposed. `service_add`/`service_delete`
  were already admin-gated.

## [1.6.2] — 2026-06-30
### Changed
- Clearer failure messages for `scan_document` and `print_document`. The scanner
  error now lists **every transport it tried** and, on an HTTPS certificate
  failure, points at `tls_insecure` / `ca_bundle` for a self-signed device (instead
  of only showing the last attempt). The printer error **names the IPP status**,
  adds a reachability hint on connection errors, and suggests
  `application/octet-stream` when the format is rejected.

## [1.6.1] — 2026-06-30
### Fixed
- The `*_add` registration tools now **merge on update** instead of overwriting.
  Updating one field (e.g. adding a category) no longer wipes fields you didn't
  restate — a `token_env` reference, a `write_only` ingest lock, TLS settings are
  preserved. Shared `cfgstore.write_merged` across service/scan/mqtt/ftp/webdav/
  ssh/mail/print/mcp registration. Service `category` is optional when updating an
  already-categorized service (still required for a new one). To clear a field,
  `*_delete` and re-add.

## [1.6] — 2026-06-30
### Added
- **Tiered memory catalog** — `bootstrap` groups each memory scope by tier
  (🧭 Core / 📂 Projects / 🛠 Working style / 🔗 References), derived from the
  existing `type`; short-term/current state is the sessions layer. No migration.
- **Categorized services** — `service_add` now **requires a `category`** (refuses
  without one, like `skill_write`); the catalog and `service_list` group by it, via
  a generic renderer that falls back to a flat list for uncategorized sections.

## [1.5.2] — 2026-06-30
### Security
- Verify TLS **by default** for the scanner (eSCL) and WebDAV. Self-signed LAN
  devices opt out via the admin-only `tls_insecure` / `ca_bundle` options on
  `scan_add` / `webdav_add`, so a normal caller can't disable verification (#10).

## [1.5.1] — 2026-06-30
### Fixed
- Connect to spec-compliant Streamable HTTP MCP servers (e.g. Outline's built-in
  MCP server): a minimal POST-only client that always sends
  `Accept: application/json, text/event-stream`, follows redirects, carries the
  session id, and tolerates servers without a standalone GET stream (#17).

## [1.5] — 2026-06-30
### Added
- **Per-user data isolation** (opt-in `TENANCY_ISOLATE`): each non-admin caller is
  confined to their own memory scope (`users/<sub>`) and a private vault namespace;
  an admin provisions per-user secrets (`secret_set owner=…`) — users can't create
  their own.
- **Tenancy control plane** — admin tools `tenancy_set` / `tenancy_show` /
  `tenancy_list` / `tenancy_unset` / `tenancy_status`.

## [1.4] — 2026-06-30
### Added
- **Pocket ID-aware OIDC proxy** forwards the upstream identity (`sub`, `email`,
  `groups`), so Pocket ID groups drive roles end-to-end (per-person identity).

## [1.3] — 2026-06-29
### Added
- **Authorization layer** (on by default): roles (admin / user / viewer),
  deny-by-default tool permissions (registration/secrets/identity are admin-only),
  an audit log, per-credential identity binding, optional IdP role/group claim, and
  a `mail_send` recipient allow-list.

## [1.2.1] — 2026-06-29
### Security
- Connect-time **DNS-rebinding protection** — the egress IP policy is re-applied at
  connect, not just at preflight.

## [1.2] — 2026-06-29
### Security
- Hardening after an external review: **fail-closed encrypted vault** (keeps a
  `.bak`, refuses plaintext without a key), **TLS verification on by default** for
  FTP/MQTT/WebDAV, **SSH host-key pinning**, and **resource limits** on the
  workspace file tools and printing.

## [1.1] — 2026-06-25
### Added
- **Auto-memory** — typed memories with dedup and a candidate review queue, plus a
  fail-open auto-capture hook (the brain learns each session without polluting
  itself).
- **Presence-aware multi-agent coordination** — capability-routed task pull
  (`task_next`) and context-preserving, session-linked handoff (`task_handoff`),
  plus cross-LLM session handoff (`session_save` / `session_load`).

## [1.0.0] — 2026-06-24
### Added
- Initial release: a self-hosted MCP brain — `bootstrap` onboarding, typed memory,
  a skill router, HTTP/MQTT/FTP/WebDAV/SSH/SMTP dispatchers, a sandboxed workspace
  file hub, IPP printing, eSCL scanning, an MCP gateway, cron-as-data scheduling, an
  encrypted secret vault, OAuth via your own OIDC provider, and an SSRF egress guard.

[1.6.2]: https://github.com/IkarusMK/AIcortex/releases/tag/1.6.2
[1.6.1]: https://github.com/IkarusMK/AIcortex/releases/tag/1.6.1
[1.6]: https://github.com/IkarusMK/AIcortex/releases/tag/1.6
[1.5.2]: https://github.com/IkarusMK/AIcortex/releases/tag/1.5.2
[1.5.1]: https://github.com/IkarusMK/AIcortex/releases/tag/1.5.1
[1.5]: https://github.com/IkarusMK/AIcortex/releases/tag/1.5
[1.4]: https://github.com/IkarusMK/AIcortex/releases/tag/1.4
[1.3]: https://github.com/IkarusMK/AIcortex/releases/tag/1.3
[1.2.1]: https://github.com/IkarusMK/AIcortex/releases/tag/1.2.1
[1.2]: https://github.com/IkarusMK/AIcortex/releases/tag/1.2
[1.1]: https://github.com/IkarusMK/AIcortex/releases/tag/1.1
[1.0.0]: https://github.com/IkarusMK/AIcortex/releases/tag/v1.0.0
