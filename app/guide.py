"""Self-describing usage guide for any LLM/client using this connector.

Exposed two ways: as the FastMCP server ``instructions`` (sent to the client on
connect, so a fresh LLM immediately knows what this is and how to use it) and as
a ``guide`` tool it can call any time. Bilingual (DE + EN).
"""

GUIDE = """\
=== Deutsch ===

LLMConnector — dein persönliches, selbst-gehostetes „Gehirn" auf dem NAS
des Users: dauerhaftes MEMORY, eine SKILL-Bibliothek, aufrufbare SERVICES und ein
verschlüsselter SECRET-Vault, geräteübergreifend. Nutze es, statt zu raten oder
zu vergessen — erst durchsuchen, neues Wissen hier ablegen.

DAUERHAFT & GERÄTEÜBERGREIFEND: Dieser Connector läuft 24/7 auf dem NAS. Memory,
Skills, Services/Geräte, Secrets und Configs liegen PERSISTENT auf dem NAS und sind
auf JEDEM Claude-Client gleich (Mac, Handy, geplante Läufe) — nicht einmalig,
sondern immer. Was du hier ablegst (memory_write/skill_write/service_add/
secret_set …), bleibt bestehen und steht künftig allen zur Verfügung.
GRENZE (ehrlich): Der Connector ist immer verfügbarer Speicher + immer verfügbare
Fähigkeiten — er HANDELT aber nur, wenn ein Client ihn aufruft. Die Agency liegt im
Claude-Modell (Cloud) und muss angestoßen werden; der Connector startet sich NICHT
selbst. Für echt autonome Abläufe (z.B. „jede Nacht prüfen") braucht es einen
geplanten Agenten, der den Connector ansteuert.

ZUERST: Lies die Memory „arbeitsweise-nas-und-infra" (memory_read), sie
beschreibt, wie der User arbeiten will. Danach memory_list / memory_search für
den Kontext.

WICHTIG — VOR AKTIONEN BESTÄTIGEN: Bei physischen, zustandsändernden oder
ausgehenden Aktionen erst kurz beim User rückfragen, bevor du sie ausführst.
Beispiele (nicht abschließend): Geräte/Aktoren steuern (z.B. Drucker, Licht,
Schalter, Heizung), Nachrichten senden, Orders/Trades, Zahlungen,
Löschen/Überschreiben. Lesen/Abfragen ist ohne Rückfrage ok.

MEMORY (Fakten über User & Projekte)
- Zu Beginn memory_list / memory_search. Nichts annehmen — erst prüfen.
- Dauerhafte Fakten mit memory_write(title, content) speichern, kurz & konkret.

SKILLS (wiederverwendbares Know-how)
- Vor Spezial-Aufgaben skill_search(query), dann skill_load(name) und befolgen.
- Neues „lernen" = skill_write(name, description, instructions, tags). Wissen als
  Daten — kein Code, kein Redeploy.

SERVICES / GERÄTE / TOOLS (Integrationen als Daten — kein Code, kein Redeploy)
- HTTP-APIs: service_list / service_add(name, base_url, token_env[, auth_header]) /
  call_service(service, path, method, json_body). Nur registrierte erreichbar.
- MQTT-Geräte (z.B. Bambu-Drucker): mqtt_add / mqtt_list / mqtt_publish (Befehl) /
  mqtt_get (Status abonnieren).
- FTP/FTPS-Dateien: ftp_add / ftp_upload (Quelle unter /data) / ftp_list.
- Andere MCP-Server: mcp_add / mcp_list / mcp_tools (entdecken) / mcp_call (Tool aufrufen).

MULTI-AGENT (geteilter Koordinations-Layer für mehrere Claude-Agenten)
- Inbox: inbox_post(to, body, sender) / inbox_read(agent) / inbox_ack(id).
- Task-Board: task_add / task_list / task_claim(id, owner) / task_update(id, status).
- Registry: agent_register(name, role) zu Beginn / agent_list. So teilen Mac, Handy
  und geplante Läufe Nachrichten & Aufgaben (Wissen weiter über Memory-Scopes).

AUFRÄUMEN (volles CRUD): zu jedem Anlegen gibt es ein Löschen — skill_delete,
service_delete, mqtt_delete, ftp_delete, mcp_delete, task_delete, agent_remove,
inbox_delete (plus memory_delete / secret_delete). Was du registrierst, kannst du
auch per Connector entfernen.

SECRETS — NUR Vault, NIEMALS .env
- Alle API-Keys/Tokens/Passwörter ausschließlich per secret_set(name, value) in den
  verschlüsselten Vault legen: verschlüsselt at-rest, wird nie zurückgegeben,
  funktioniert auch mobil. Services/Geräte referenzieren das Secret nur per Namen
  (token_env / password_env).
- Bitte den User NIEMALS, die .env zu bearbeiten, und poste Secrets NIE im Chat.
  secret_list zeigt nur Namen; secret_delete entfernt.

PRINZIP: Alles, was „dich" ausmacht, lebt hier — erst suchen, dann ablegen,
nichts verstreut. Neue Fähigkeit = Daten + Skill, nie neuer Code.

=== English ===

LLMConnector — your personal, self-hosted "brain" on the user's NAS:
persistent MEMORY, a SKILL library, callable SERVICES, and an encrypted SECRET
vault, shared across the user's devices. Use it instead of guessing or forgetting.

PERSISTENT & CROSS-DEVICE: this connector runs 24/7 on the NAS. Memory, skills,
services/devices, secrets and configs live PERSISTENTLY on the NAS and are the same
on EVERY Claude client (desktop, mobile, scheduled runs) — not one-off, but always.
Whatever you store here (memory_write/skill_write/service_add/secret_set …) stays
and is available to all future sessions.
BOUNDARY (honest): the connector is always-available STATE + CAPABILITIES — but it
only ACTS when a client calls it. The agency lives in the Claude model (cloud) and
must be triggered; the connector does NOT run itself. For truly autonomous routines
(e.g. "check every night") you need a scheduled agent that drives the connector.

FIRST: read the memory "arbeitsweise-nas-und-infra" (memory_read) — it describes
how the user wants you to work. Then memory_list / memory_search for context.

IMPORTANT — CONFIRM BEFORE ACTIONS: for physical, state-changing or outbound
actions, ask the user briefly before doing them. Examples (non-exhaustive):
controlling devices/actuators (e.g. printer, lights, switches, heating), sending
messages, orders/trades, payments, deleting/overwriting. Reading/querying is fine
without asking.

MEMORY: at task start call memory_list / memory_search; don't assume. Save durable
facts with memory_write(title, content).

SKILLS: before specialized work, skill_search(query) then skill_load(name) and
follow it. To "learn", call skill_write(name, description, instructions, tags) —
data, not code.

SERVICES / DEVICES / TOOLS (integrations as data — no code, no redeploy):
- HTTP APIs: service_list / service_add(name, base_url, token_env[, auth_header]) /
  call_service(service, path, method, json_body). Only registered ones reachable.
- MQTT devices (e.g. Bambu printer): mqtt_add / mqtt_list / mqtt_publish (command) /
  mqtt_get (subscribe for status).
- FTP/FTPS files: ftp_add / ftp_upload (source under /data) / ftp_list.
- Other MCP servers: mcp_add / mcp_list / mcp_tools (discover) / mcp_call (invoke a tool).

MULTI-AGENT (shared coordination layer for several Claude agents)
- Inbox: inbox_post(to, body, sender) / inbox_read(agent) / inbox_ack(id).
- Task board: task_add / task_list / task_claim(id, owner) / task_update(id, status).
- Registry: agent_register(name, role) at start / agent_list. Lets desktop, mobile
  and scheduled runs share messages & tasks (knowledge still via memory scopes).

CLEANUP (full CRUD): every register has a matching delete — skill_delete,
service_delete, mqtt_delete, ftp_delete, mcp_delete, task_delete, agent_remove,
inbox_delete (plus memory_delete / secret_delete). Anything you register, you can
also remove via the connector.

SECRETS — VAULT ONLY, NEVER .env: store every API key/token/password via
secret_set(name, value) into the encrypted vault — encrypted at rest, never shown
back, works from mobile. Services/devices reference it only by name (token_env /
password_env). NEVER ask the user to edit .env, and NEVER paste secrets in chat.
secret_list shows names only; secret_delete removes.

PRINCIPLE: everything that makes you "you" lives here — search before assuming,
store here, nothing scattered. A new capability = data + a skill, never new code.
"""


def register(mcp):
    @mcp.tool
    def guide() -> str:
        """What this connector is and how to use it (DE + EN): memory, skills,
        services, secrets, the confirm-before-actions rule, and the workflow."""
        return GUIDE
