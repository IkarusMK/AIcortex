"""Single source of truth for the running AICortex version.

Bump this on every release so the version is observable at runtime — logged at
startup, returned by `ping`, and shown in the `bootstrap` catalog header — instead
of having to fingerprint the container's source. Keep it in sync with CHANGELOG.md.
"""

__version__ = "1.6.4"
