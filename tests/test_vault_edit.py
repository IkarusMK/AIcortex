"""Editing a vault entry in the WebUI without ever seeing it (AICortex 2.3.1).

Before: an entry could only be deleted, or overwritten by adding it again under the
same name — and for a person's own entry that added a second one elsewhere. Now every
entry has *Edit*: a new value (never shown, old or new) and/or a new description, saved
to exactly the entry it is. Proves:
- a new value replaces the old one in place; neither shows up in the answer or the audit;
- an empty value keeps it and changes only the description;
- a person's entry, edited by the owner shown in the list, stays that one entry;
- an unknown entry is refused; only admins with the CSRF token may edit;
- the vault page offers Edit on every entry.
"""
import json
import sys
from pathlib import Path

import pytest

from test_cov_webui import Ui, _audit_lines, ui  # noqa: F401 - ui is a fixture

pytestmark = pytest.mark.oidc

STATIC = Path(__file__).resolve().parent.parent / "app" / "webui_static"


def _vault():
    return sys.modules["secrets_store"]


def test_a_new_value_replaces_the_old_one_unseen(ui):
    _vault().vault_set("SERVICE_TOKEN", "old-value-123", description="for the service")
    status, body = ui.call("/ui/api/secrets/update", "POST",
                           body={"name": "SERVICE_TOKEN", "owner": "", "value": "new-value-456"})
    assert status == 200 and body["ok"], body
    assert _vault()._read_all()["SERVICE_TOKEN"] == "new-value-456"
    text = json.dumps(body) + json.dumps(_audit_lines())
    assert "new-value-456" not in text and "old-value-123" not in text
    entry = next(e for e in _vault().vault_entries() if e["name"] == "SERVICE_TOKEN")
    assert entry["description"] == "for the service" and entry["updated_by"] == "boss"


def test_an_empty_value_keeps_it_and_changes_only_the_description(ui):
    _vault().vault_set("SERVICE_TOKEN", "keep-me")
    status, body = ui.call("/ui/api/secrets/update", "POST", body={
        "name": "SERVICE_TOKEN", "owner": "", "value": "", "description": "now described"})
    assert status == 200 and body["ok"]
    assert _vault()._read_all()["SERVICE_TOKEN"] == "keep-me"
    entry = next(e for e in _vault().vault_entries() if e["name"] == "SERVICE_TOKEN")
    assert entry["description"] == "now described"


def test_a_persons_entry_stays_one_entry(ui):
    _vault().vault_set("MY_KEY", "alice-old", owner="alice")
    listed = next(s for s in ui.call("/ui/api/secrets")[1]["secrets"] if s["name"] == "MY_KEY")
    status, body = ui.call("/ui/api/secrets/update", "POST",
                           body={"name": "MY_KEY", "owner": listed["owner"], "value": "alice-new"})
    assert status == 200 and body["ok"], body
    keys = [k for k in _vault()._read_all() if k.endswith("/MY_KEY")]
    assert len(keys) == 1 and _vault()._read_all()[keys[0]] == "alice-new"


def test_only_an_existing_entry_and_only_an_admin_with_the_token(ui):
    _vault().vault_set("SERVICE_TOKEN", "v")
    assert ui.call("/ui/api/secrets/update", "POST", body={"name": "NOPE", "value": "x"})[0] == 404
    assert ui.call("/ui/api/secrets/update", "POST", role="user",
                   body={"name": "SERVICE_TOKEN", "value": "x"})[0] == 403
    assert ui.call("/ui/api/secrets/update", "POST", csrf=None,
                   body={"name": "SERVICE_TOKEN", "value": "x"})[0] == 403
    assert _vault()._read_all()["SERVICE_TOKEN"] == "v"


def test_the_vault_page_offers_edit_on_every_entry():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert '"/ui/api/secrets/update"' in js and "vault_edit" in js and "vault_keep_value" in js
    # the name opens the entry; delete is there too and deletes without overwriting
    assert "nm.addEventListener(\"click\", () => editSecret(s))" in js
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="btn-sec-delete"' in html and "deleteSecret(VAULT_EDIT)" in js
