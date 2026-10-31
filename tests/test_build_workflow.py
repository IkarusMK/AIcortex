"""The image build must not publish the pusher's email address.

In a public repository docker/build-push-action defaults to provenance mode=max,
which embeds the complete GitHub event — for a push that includes the pusher's
email — in the image's attestation. buildx also writes that provenance into the
"Metadata" block the action prints to the log, and into the build record and
summary. Found in October 2026: every published image and build log carried it.
"""
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "build.yml"


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_image_carries_no_buildkit_provenance():
    steps = _workflow()["jobs"]["build"]["steps"]
    build = [s for s in steps if "docker/build-push-action" in str(s.get("uses", ""))]
    assert build, "build-push-action step not found"
    for step in build:
        assert step["with"].get("provenance") is False


def test_log_record_and_summary_carry_no_build_metadata():
    env = _workflow().get("env") or {}
    assert env.get("BUILDX_METADATA_PROVENANCE") == "disabled"
    assert env.get("DOCKER_BUILD_RECORD_UPLOAD") == "false"
    assert env.get("DOCKER_BUILD_SUMMARY") == "false"
