"""Reserved command names at the skill publication planning boundary."""

from __future__ import annotations

from typing import Final


CODE_COMMAND_NAME_RESERVED: Final = "command_name_reserved"
WINDOWS_COMMAND_SUFFIXES: Final = (".exe", ".cmd", ".bat", ".ps1")
# Fixed policy, independent of the operator's PATH and installed programs.
RESERVED_SYSTEM_COMMANDS: Final = frozenset(
    {
        "git", "ssh", "ssh-add", "ssh-agent", "ssh-keygen", "scp", "sftp",
        "gpg", "gpg-agent", "sh", "bash", "zsh", "fish", "dash", "pwsh",
        "powershell", "cmd", "env", "sudo", "su", "doas", "python", "python3",
        "pip", "pip3", "uv", "go", "gofmt", "node", "npm", "npx", "ruby",
        "perl", "make", "cc", "gcc", "clang", "ld", "curl", "wget", "tar",
        "unzip", "openssl", "security", "keychain", "launchctl", "systemctl",
    }
)
# The console entry point is csk (csk.exe on Windows); csk-script.py and
# other manager csk-* launchers are covered by the prefix rule below.
RESERVED_MANAGER_COMMANDS: Final = frozenset({"csk", "cocoaskills"})


class CommandNameReservedError(ValueError):
    """A stable, actionable refusal before a command can be published."""

    code = CODE_COMMAND_NAME_RESERVED

    def __init__(self, name: str) -> None:
        self.detail = (
            f"Command {name!r} is reserved for the manager or system tools. "
            "Hint: rename the exported command in the skill manifest and "
            "update any skill requirements that select it."
        )
        super().__init__(f"{self.code}: {self.detail}")


def require_unreserved_command_name(name: str) -> None:
    """Compare on every host with case and Windows launcher suffix ignored."""

    normalized = name.casefold()
    for suffix in WINDOWS_COMMAND_SUFFIXES:
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)]
            break
    if (
        normalized in RESERVED_SYSTEM_COMMANDS
        or normalized in RESERVED_MANAGER_COMMANDS
        or normalized.startswith("csk-")
    ):
        raise CommandNameReservedError(name)
