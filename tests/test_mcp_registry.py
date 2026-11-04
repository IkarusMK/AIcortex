"""AICortex is listed in the official MCP Registry (registry.modelcontextprotocol.io).

The registry takes a server.json and, for a Docker image, verifies ownership by the
image label io.modelcontextprotocol.server.name, which must equal the server's name;
a published version can never be changed. So the label, server.json and the release
must agree before a tag is pushed — a mismatch only shows when the release publishes,
and the fix would need another release. The listing is published by the release
workflow with GitHub's OIDC token (no stored secret) and a signature-checked
mcp-publisher.
"""
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SERVER = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8"))


def _version() -> str:
    text = (ROOT / "app" / "version.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def test_the_image_label_names_the_listed_server():
    label = re.search(r'LABEL io\.modelcontextprotocol\.server\.name="([^"]+)"',
                      (ROOT / "Dockerfile").read_text(encoding="utf-8"))
    assert label and label.group(1) == SERVER["name"] == "io.github.IkarusMK/aicortex"


def test_server_json_names_this_release_and_its_image():
    version = _version()
    assert SERVER["version"] == version
    (package,) = SERVER["packages"]
    assert package["registryType"] == "oci"
    assert package["identifier"] == f"ghcr.io/ikarusmk/aicortex:v{version}"
    assert package["transport"] == {"type": "streamable-http", "url": "http://localhost:8787/mcp"}


def test_server_json_keeps_the_registry_limits():
    assert 1 <= len(SERVER["description"]) <= 100
    assert 1 <= len(SERVER["title"]) <= 100
    assert SERVER["repository"] == {"url": "https://github.com/IkarusMK/AIcortex", "source": "github"}
    port = next(a for a in SERVER["packages"][0]["runtimeArguments"] if a["name"] == "-p")
    assert port["value"].startswith("127.0.0.1:")          # the try-out stays on this computer
    secrets = {e["name"] for e in SERVER["packages"][0]["environmentVariables"] if e.get("isSecret")}
    assert {"STORAGE_ENCRYPTION_KEY", "OIDC_CLIENT_SECRET", "JWT_SIGNING_KEY"} <= secrets


def test_the_release_publishes_the_listing_after_its_image():
    job = WORKFLOW["jobs"]["publish-registry"]
    assert job["needs"] == "build"
    assert job["if"] == "startsWith(github.ref, 'refs/tags/v')"
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    runs = "\n".join(s.get("run", "") for s in job["steps"])
    assert "jq -r .version server.json" in runs and "ghcr.io/ikarusmk/aicortex:${tag}" in runs
    assert "./mcp-publisher login github-oidc" in runs and "./mcp-publisher publish" in runs


def test_mcp_publisher_is_pinned_and_signature_checked():
    job = WORKFLOW["jobs"]["publish-registry"]
    assert re.fullmatch(r"v\d+\.\d+\.\d+", job["env"]["MCP_PUBLISHER_VERSION"])
    runs = "\n".join(s.get("run", "") for s in job["steps"])
    assert "releases/latest" not in runs
    assert "cosign verify-blob" in runs
    assert ("release.yml@refs/tags/${MCP_PUBLISHER_VERSION}" in runs
            and "token.actions.githubusercontent.com" in runs)
    cosign = next(s for s in job["steps"] if "cosign-installer" in str(s.get("uses", "")))
    assert re.search(r"@[0-9a-f]{40}$", cosign["uses"])     # pinned to a commit, not a tag
