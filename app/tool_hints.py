"""MCP tool annotations for every tool, from one set of rules.

Clients use the annotations to decide when to ask the user before a call
(Anthropic's connector directory requires them; ChatGPT shows confirmations by
them). Rules by name keep 128 tools consistent without touching each module:

- read-only: looks things up, changes nothing;
- destructive: may delete or overwrite something (non-read-only tools are
  explicitly marked either way — the MCP default for an unmarked write is
  "destructive");
- open-world: reaches an outside system (HTTP service, device, mail, cloud, SSH,
  another MCP server); everything else stays inside AICortex.
"""
from fastmcp.server.transforms import Transform
from mcp_types import ToolAnnotations

_READ_ONLY_EXACT = {
    "ping", "guide", "bootstrap", "cortex_start", "cortex_catalog", "integration_search",
    "session_load", "inbox_read", "task_next", "mqtt_get", "skill_resource",
    "memory_candidates", "fs_view", "fs_info", "tenancy_show", "tenancy_status",
}
_READ_ONLY_SUFFIX = ("_list", "_read", "_search", "_load", "_list_endpoints",
                     "_list_calendars", "_list_events", "_list_dir", "_tools")

_DESTRUCTIVE_EXACT = {
    "memory_reject", "session_prune", "fs_write", "fs_move", "memory_write",
    "skill_write", "secret_set", "integration_call", "ssh_run", "mcp_call",
    # may overwrite or delete: generic HTTP calls (PUT/DELETE), uploads/downloads
    # over existing files, promotion over an existing memory, replacing grants
    "call_service", "ftp_upload", "webdav_upload", "ssh_upload", "webdav_download",
    "ssh_download", "memory_promote", "tenancy_set",
    # can't be undone outside AICortex: a sent mail or webhook, a device command, a
    # printed page, a calendar invite — clients that confirm only destructive tools
    # must ask before these
    "mail_send", "webhook_send", "mqtt_publish", "print_document", "caldav_add_event",
}
_DESTRUCTIVE_PART = ("_delete", "_remove", "_revoke", "_unset")

_OUTSIDE_PREFIX = ("mqtt_", "ftp_", "webdav_", "caldav_", "ssh_", "mail_", "imap_",
                   "print_", "scan_", "mcp_")
_OUTSIDE_EXACT = {"call_service", "webhook_send", "integration_call"}
# Registry bookkeeping inside AICortex despite the prefix (no outside contact).
_INSIDE_EXACT = {"mqtt_add", "mqtt_delete", "mqtt_list", "ftp_add", "ftp_delete",
                 "ftp_list_endpoints", "webdav_add", "webdav_delete_endpoint",
                 "webdav_list_endpoints", "caldav_add", "caldav_delete_endpoint",
                 "caldav_list_endpoints", "ssh_add", "ssh_delete_endpoint", "ssh_list",
                 "mail_add", "mail_delete_account", "mail_list", "imap_add",
                 "imap_delete_account", "imap_list", "print_add", "print_delete",
                 "print_list", "scan_add", "scan_delete", "scan_list", "mcp_add",
                 "mcp_delete", "mcp_list"}

_WORDS = {"mqtt": "MQTT", "ftp": "FTP", "ssh": "SSH", "imap": "IMAP", "caldav": "CalDAV",
          "webdav": "WebDAV", "mcp": "MCP", "fs": "Files:", "apikey": "API key",
          "inbox": "Inbox", "cortex": "AICortex"}


def read_only(name: str) -> bool:
    return name in _READ_ONLY_EXACT or name.endswith(_READ_ONLY_SUFFIX)


def destructive(name: str) -> bool:
    return name in _DESTRUCTIVE_EXACT or any(p in name for p in _DESTRUCTIVE_PART)


def open_world(name: str) -> bool:
    if name in _INSIDE_EXACT:
        return False
    return name in _OUTSIDE_EXACT or name.startswith(_OUTSIDE_PREFIX)


def title(name: str) -> str:
    words = [_WORDS.get(w, w) for w in name.split("_")]
    text = " ".join(words)
    return text[0].upper() + text[1:]


def hints(name: str) -> ToolAnnotations:
    ro = read_only(name)
    return ToolAnnotations(
        title=title(name),
        read_only_hint=ro,
        destructive_hint=None if ro else destructive(name),
        idempotent_hint=True if ro else None,
        open_world_hint=open_world(name),
    )


class ToolHints(Transform):
    """Adds title + annotations to every listed tool (keeps any set explicitly)."""

    async def list_tools(self, tools):
        return [t if t.annotations is not None
                else t.model_copy(update={"annotations": hints(t.name),
                                          "title": t.title or title(t.name)})
                for t in tools]
