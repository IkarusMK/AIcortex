"""Self-describing usage guide for any LLM/client using this connector.

Exposed two ways: as the FastMCP server ``instructions`` (sent to the client on
connect, so a fresh LLM immediately knows what this is and how to use it) and as
a ``guide`` tool it can call any time.
"""

GUIDE = """\
ClaudeNasConnector — your personal, self-hosted "brain" on the user's NAS.
It gives you persistent MEMORY, a SKILL library, callable SERVICES, and a SECRET
vault, shared across all the user's devices. Use it instead of guessing or
forgetting — search it first, and store new knowledge here.

MEMORY (facts about the user & their projects)
- At the start of a task, call `memory_list` (and `memory_search`) to recall what
  is already known. Don't assume — check first.
- When you learn a durable fact (preference, decision, ongoing project), save it
  with `memory_write(title, content)`. Keep entries short and specific.

SKILLS (reusable know-how)
- Before a specialized task, call `skill_search(query)`, then `skill_load(name)`
  for the best match and follow it. Use `skill_resource` for bundled files.
- To "learn" something new, call `skill_write(name, description, instructions,
  tags)`. Knowledge is stored as data — no code change, no redeploy.

SERVICES / TOOLS (integrations as data)
- `service_list` shows configured APIs. Call one with `call_service(service,
  path, method, json_body)`. Only registered services are reachable.
- Add a new integration with `service_add(name, base_url, token_env)`; its secret
  is referenced by `token_env` (a name), never inlined.

SECRETS
- Store credentials with `secret_set(name, value)` — encrypted on the NAS, never
  shown back. Reference the name as a service's `token_env`. No tool ever returns
  a secret value.

PRINCIPLES
- Everything that makes you "you" lives here: search before assuming; store new
  knowledge, integrations and secrets here; nothing scattered.
- A new capability = data + a skill, never new code.
"""


def register(mcp):
    @mcp.tool
    def guide() -> str:
        """What this connector is and how to use it (memory, skills, services,
        secrets + the recommended workflow). Call this when unsure."""
        return GUIDE
