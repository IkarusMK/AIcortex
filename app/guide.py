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

SERVICES / TOOLS (Integrationen als Daten)
- service_list zeigt APIs; call_service(service, path, method, json_body) ruft auf;
  service_add(name, base_url, token_env) fügt hinzu. Nur registrierte erreichbar.

SECRETS
- secret_set(name, value) — verschlüsselt, wird nie zurückgegeben. Referenz per
  token_env-Name. Kein Tool gibt je einen Secret-Wert aus.

PRINZIP: Alles, was „dich" ausmacht, lebt hier — erst suchen, dann ablegen,
nichts verstreut. Neue Fähigkeit = Daten + Skill, nie neuer Code.

=== English ===

LLMConnector — your personal, self-hosted "brain" on the user's NAS:
persistent MEMORY, a SKILL library, callable SERVICES, and an encrypted SECRET
vault, shared across the user's devices. Use it instead of guessing or forgetting.

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

SERVICES/TOOLS: service_list shows APIs; call_service(...) calls one; service_add(
name, base_url, token_env) adds one. Only registered services are reachable.

SECRETS: secret_set(name, value) — encrypted, never shown back; referenced by
token_env name. No tool returns a secret value.

PRINCIPLE: everything that makes you "you" lives here — search before assuming,
store here, nothing scattered. A new capability = data + a skill, never new code.
"""


def register(mcp):
    @mcp.tool
    def guide() -> str:
        """What this connector is and how to use it (DE + EN): memory, skills,
        services, secrets, the confirm-before-actions rule, and the workflow."""
        return GUIDE
