"""Manager-side admission for the schema-8 enforced script execution policy.

Protocol core 4.1.1 defines exactly one script execution policy,
``script-worker-v1``, and csk does not implement it. Parsing the policy and
implementing it are separate statements, and the protocol treats them
separately: a schema-8 manifest that selects the policy is a valid document, so
:func:`csk.skillspec.load_skill_spec` accepts it and the published schema cases
that mark it valid stay green.

Admission is this manager's own answer, and the protocol leaves it no room:

    A manager that does not implement this policy MUST reject such a command
    with ``script_execution_policy_unsupported``. It MUST NOT install the
    command declared-only, downgrade it, or ignore the field, because the
    resulting shim would run package code the manifest says is contained.

So admission lives one layer below the parser. Every surface that would turn a
declared command into an installed shim, and every surface that reads a
package's commands back to an operator, asks this module first and fails closed
on an enforced command. When a worker lands, this module is where the refusal
is replaced by real containment; nothing above it has to move, because nothing
above it decides the policy.
"""

from __future__ import annotations

from collections.abc import Mapping

from .skillspec import CommandSpec


# The closed 4.1.1 diagnostic for an enforced command read by a manager that
# does not implement the policy, with the manager profile's fixed state and
# severity pair for it.
SCRIPT_EXECUTION_POLICY_UNSUPPORTED = "script_execution_policy_unsupported"
STATE_UNSUPPORTED = "unsupported"
SEVERITY_ERROR = "error"


class ScriptPolicyError(RuntimeError):
    """An execution-policy refusal bound to a closed protocol diagnostic."""

    def __init__(self, *, code: str, state: str, severity: str, path: str, detail: str):
        self.code = code
        self.state = state
        self.severity = severity
        self.path = path
        self.detail = detail
        super().__init__(f"{path}: {code}: {detail}")


def is_enforced(command: CommandSpec) -> bool:
    """Report whether a command opted into an execution policy.

    Only script commands can carry one and ``script-worker-v1`` is the single
    admitted value, so any policy at all names an enforced command.
    """

    return command.execution_policy is not None


def admit(commands: Mapping[str, CommandSpec]) -> None:
    """Refuse the first enforced command in command-lexical order.

    Ordering is fixed so one manifest names one command on every host and every
    run. Returning without raising means every command is declared-only and
    installs through the ordinary schema-7 path unchanged.
    """

    for name in sorted(commands, key=lambda value: value.encode("utf-8")):
        command = commands[name]
        if not is_enforced(command):
            continue
        raise ScriptPolicyError(
            code=SCRIPT_EXECUTION_POLICY_UNSUPPORTED,
            state=STATE_UNSUPPORTED,
            severity=SEVERITY_ERROR,
            path=f"commands.{name}.execution_policy",
            detail=(
                f"this manager does not implement {command.execution_policy}, and the "
                "policy forbids installing the command declared-only, downgrading it, "
                "or ignoring the field"
            ),
        )
