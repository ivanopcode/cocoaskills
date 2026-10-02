"""Environment contract for audit children, including configured overrides."""

from __future__ import annotations

import os
from collections.abc import Mapping


BASE_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "TEMP", "TMP",
})
WINDOWS_ENV_ALLOWLIST = frozenset({"SYSTEMROOT", "COMSPEC"})
# Codex uses this optional override to locate its configuration/state directory.
CODEX_REQUIRED_ENV = frozenset({"CODEX_HOME"})
COMMAND_REQUIRED_ENV: frozenset[str] = frozenset()


def backend_environment(
    required: frozenset[str], *, overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    allowed = BASE_ENV_ALLOWLIST | required
    if os.name == "nt":
        allowed |= WINDOWS_ENV_ALLOWLIST
    candidates = {name.upper() if os.name == "nt" else name: value for name, value in os.environ.items()}
    if overrides:
        candidates.update({name.upper() if os.name == "nt" else name: value for name, value in overrides.items()})
    result = {}
    for name, value in candidates.items():
        # Even LC_* and backend-specific entries cannot carry these secrets.
        security_name = name.upper()
        if security_name.endswith(("_TOKEN", "_KEY")) or security_name == "SSH_AUTH_SOCK" or security_name.startswith("GIT_"):
            continue
        if name in allowed or name.startswith("LC_"):
            result[name] = value
    return result
