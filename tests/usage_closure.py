"""Shared oracle for the closed usage-error output (value-based redaction, rev 5).

A usage error prints the parser's usage block and one ``prog: error: MESSAGE``
line.  MESSAGE must match exactly one of the allowlisted shapes below; every
argv-derived part is the fixed text ``<value not shown>``.
"""

from __future__ import annotations

import re

VALUE_NOT_SHOWN = "<value not shown>"
GENERIC = "invalid usage (details not shown); run 'csk <command> --help'"

# Pinned literally (not imported from the implementation) so drift in either fails.
ALLOWED_MESSAGES = (
    re.compile(r"unrecognized arguments: <value not shown>"),
    re.compile(r"unexpected option string: <value not shown>"),
    re.compile(r"the following arguments are required: [^<>]+"),
    re.compile(r"one of the arguments [^<>]+ is required"),
    re.compile(r"ambiguous option: <value not shown> could match [^<>]+"),
    re.compile(r"argument [^<>]+: expected (?:one|at most one|at least one) argument"),
    re.compile(r"argument [^<>]+: expected \d+ arguments?"),
    re.compile(r"argument [^<>]+: invalid choice: <value not shown> \(choose from [^<>]+\)"),
    re.compile(r"argument [^<>]+: invalid \w+ value: <value not shown>"),
    re.compile(r"argument [^<>]+: ignored explicit argument <value not shown>"),
    re.compile(r"argument [^<>]+: not allowed with argument [^<>]+"),
    re.compile(r"argument --token-source: must be one of git-credentials, keyring"),
    re.compile(r"argument --token-env: must be an environment variable name \(\[A-Za-z_\]\[A-Za-z0-9_\]\{0,127\}\)"),
    re.compile(r"--token is refused; use CSK_REGISTRY_TOKEN or --token-file for registry authorization"),
    re.compile(
        r"--token is refused; select an HTTPS credential source with --token-source or --token-env; "
        r"for audit --publish use CSK_REGISTRY_TOKEN or --token-file"
    ),
    re.compile(re.escape(GENERIC)),
)


def error_message(stderr: str) -> str | None:
    """The MESSAGE of the single ``: error: MESSAGE`` line, or None if absent."""
    lines = [line for line in stderr.splitlines() if ": error: " in line]
    if len(lines) != 1:
        return None
    return lines[0].split(": error: ", 1)[1]


def is_closed_usage_error(stderr: str) -> bool:
    message = error_message(stderr)
    return message is not None and any(
        pattern.fullmatch(message) for pattern in ALLOWED_MESSAGES
    )


# ---------------------------------------------------------------------------
# Command-data oracle.
#
# Positional arguments (and every other non-credential option value) are
# command data: a command's OWN error may show them, including a positional
# that spells like a credential option or follows a bare '--'.  Usage errors
# never show argv values.  ``marker_is_command_data`` decides, with the real
# parser, whether a marker landed in command data (a documented bound) or was
# only ever a credential-option value / an unparsed fragment (never shown).
# ---------------------------------------------------------------------------

CREDENTIAL_DESTS = frozenset({"token", "token_file", "token_source", "token_env"})


def parse_outcome(argv):
    """Parse ``argv`` like ``cli.main`` does: ("ok", ns), ("help", None) or ("usage", None)."""
    import contextlib
    import io

    from csk import cli

    words = list(argv)
    is_check = bool(words and words[0] == "check")
    is_dispatch = bool(words and words[0] == "dispatch")
    parser = (cli._build_dispatch_parser() if is_dispatch else
              cli._build_check_parser() if is_check else cli.build_parser())
    if is_check or is_dispatch:
        words = words[1:]
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            return "ok", parser.parse_args(words)
    except SystemExit as exc:
        return ("help" if exc.code == 0 else "usage"), None


def parse_namespace(argv):
    kind, namespace = parse_outcome(argv)
    return namespace


def _contains(value, marker: str) -> bool:
    if isinstance(value, (list, tuple)):
        return any(_contains(item, marker) for item in value)
    return isinstance(value, str) and marker in value


def marker_is_command_data(argv, marker: str) -> bool:
    namespace = parse_namespace(argv)
    if namespace is None:
        return False
    return any(
        _contains(value, marker)
        for dest, value in vars(namespace).items()
        if dest not in CREDENTIAL_DESTS
    )


def usage_failed(argv) -> bool:
    return parse_outcome(argv)[0] == "usage"
