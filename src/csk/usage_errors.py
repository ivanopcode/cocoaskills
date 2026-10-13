"""Closed usage-error text for the argparse parsers.

argparse formats usage errors with the user's own argv bytes: an unknown
argument, an invalid choice, a typed value that does not convert, an explicit
argument given to a flag, a typo'd option.  Any of those can be a credential
that was pasted in the wrong place.  Instead of guessing which spellings are
credentials, the parser's ``error()`` never prints argparse's text.  It
classifies the message against an allowlist of argparse templates and rebuilds
the printed text from parser-defined parts only: option strings, dests,
metavars, declared choices and type names.  Every argv-derived part becomes
:data:`VALUE_NOT_SHOWN`.  A message that matches no template becomes
:data:`GENERIC_MESSAGE`.

Classification never copies a byte of the incoming message into the output, so
a value that imitates a template can at worst pick the wrong template.

The templates cover CPython 3.11 through 3.14.  Python 3.12 and later raise
some of them as ``ArgumentError(None, ...)``, which drops the ``argument NAME:``
prefix; both spellings are handled.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Collection

VALUE_NOT_SHOWN = "<value not shown>"
GENERIC_MESSAGE = "invalid usage (details not shown); run 'csk <command> --help'"

# Static argparse messages that carry no argument at all.
_ARITY_RE = re.compile(r"expected (?:one|at most one|at least one) argument|expected \d+ arguments?")
_STATIC_PLAIN = frozenset({"cannot have multiple subparser arguments"})

_ARGUMENT_PREFIX = "argument "


def _candidate_names(action: argparse.Action) -> list[str]:
    # The spellings argparse's own _get_action_name can produce for an action.
    if action.option_strings:
        return ["/".join(action.option_strings)]
    names: list[str] = []
    if isinstance(action.metavar, str):
        names.append(action.metavar)
    if isinstance(action.dest, str) and action.dest != argparse.SUPPRESS:
        names.append(action.dest)
    if action.choices:
        names.append("{" + ",".join(str(choice) for choice in action.choices) + "}")
    return names


def _action_index(parser: argparse.ArgumentParser) -> dict[str, list[argparse.Action]]:
    index: dict[str, list[argparse.Action]] = {}
    for action in parser._actions:
        for name in _candidate_names(action):
            index.setdefault(name, []).append(action)
    return index


def _option_strings(parser: argparse.ArgumentParser) -> frozenset[str]:
    return frozenset(
        option for action in parser._actions for option in action.option_strings
    )


def _split_argument_prefix(
    message: str, index: dict[str, list[argparse.Action]]
) -> tuple[list[argparse.Action], str, str] | None:
    # "argument NAME: inner" where NAME is one of this parser's own names.
    if not message.startswith(_ARGUMENT_PREFIX):
        return None
    best: str | None = None
    for name in index:
        if message.startswith(f"{_ARGUMENT_PREFIX}{name}: ") and (
            best is None or len(name) > len(best)
        ):
            best = name
    if best is None:
        return None
    inner = message[len(f"{_ARGUMENT_PREFIX}{best}: ") :]
    return index[best], best, inner


def _rebuild_argument_message(
    actions: list[argparse.Action],
    name: str,
    inner: str,
    static_messages: Collection[str],
    names: dict[str, list[argparse.Action]],
) -> str | None:
    prefix = f"{_ARGUMENT_PREFIX}{name}: "
    if _ARITY_RE.fullmatch(inner) or inner in static_messages:
        return prefix + inner
    if inner.startswith("invalid choice: "):
        for action in actions:
            if action.choices:
                choices = ", ".join(repr(str(choice)) for choice in action.choices)
                return f"{prefix}invalid choice: {VALUE_NOT_SHOWN} (choose from {choices})"
        return None
    for action in actions:
        type_name = getattr(action.type, "__name__", repr(action.type))
        if inner.startswith(f"invalid {type_name} value: "):
            return f"{prefix}invalid {type_name} value: {VALUE_NOT_SHOWN}"
    if inner.startswith("ignored explicit argument "):
        return f"{prefix}ignored explicit argument {VALUE_NOT_SHOWN}"
    other = "not allowed with argument "
    if inner.startswith(other) and inner[len(other) :] in names:
        return prefix + inner
    return None


def _rebuild_plain_message(
    parser: argparse.ArgumentParser,
    message: str,
    names: dict[str, list[argparse.Action]],
) -> str | None:
    if message in _STATIC_PLAIN:
        return message
    head = "unrecognized arguments: "
    if message.startswith(head):
        return head + VALUE_NOT_SHOWN
    head = "unexpected option string: "
    if message.startswith(head):
        return head + VALUE_NOT_SHOWN
    head = "the following arguments are required: "
    if message.startswith(head):
        required = message[len(head) :].split(", ")
        if required and all(item in names for item in required):
            return head + ", ".join(required)
        return None
    head, tail = "one of the arguments ", " is required"
    if message.startswith(head) and message.endswith(tail) and len(message) > len(head) + len(tail):
        group = message[len(head) : len(message) - len(tail)].split(" ")
        if all(item in names for item in group):
            return head + " ".join(group) + tail
        return None
    head = "ambiguous option: "
    if message.startswith(head) and " could match " in message:
        # The matches are parser-defined option strings, which contain no
        # spaces, so the last separator is the real one even when the typed
        # option imitates it.
        matches = message.rsplit(" could match ", 1)[1].split(", ")
        known = _option_strings(parser)
        if all(item in known for item in matches):
            return f"{head}{VALUE_NOT_SHOWN} could match {', '.join(matches)}"
    return None


def closed_message(
    parser: argparse.ArgumentParser,
    message: str,
    static_messages: Collection[str] = (),
) -> str:
    """Return the text to print for one argparse usage error.

    ``static_messages`` are the exact argv-free texts the CLI itself raises
    from ``type=`` callables and ``parser.error`` call sites.
    """

    if message in static_messages:
        return message
    names = _action_index(parser)
    split = _split_argument_prefix(message, names)
    if split is not None:
        actions, name, inner = split
        rebuilt = _rebuild_argument_message(actions, name, inner, static_messages, names)
    else:
        rebuilt = _rebuild_plain_message(parser, message, names)
    return GENERIC_MESSAGE if rebuilt is None else rebuilt
