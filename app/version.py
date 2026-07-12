"""Single source of truth for the running AICortex version.

This is observable at runtime — logged at startup, returned by `ping`, and shown in
the `bootstrap` catalog header — so nobody has to fingerprint the container's source.

RULE: this MUST equal the newest GitHub release tag. Only bump it as PART of cutting
a release — never in a commit-only change — otherwise ping/logs would advertise a
version that has no matching release (which is misleading). Between releases, `main`
keeps the last released number and new notes go under `## [Unreleased]` in
CHANGELOG.md; the next release then adopts them under its number.
"""

__version__ = "1.12.1"
