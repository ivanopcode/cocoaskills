"""Closed usage-error output (value-based redaction, rev 5, design decided).

Four rounds proved that a predicate over credential *spellings* leaks every
round.  The replacement is a closed shape: every parser and subparser prints a
usage error rebuilt by ``usage_errors.closed_message`` from an allowlist of
argparse templates (CPython 3.11-3.14), where every argv-derived part is the
fixed text ``<value not shown>`` and a message that matches no template is the
generic fallback.  There is no credential predicate to evade.

Production call sites: ``cli._CskArgumentParser.error`` (the one error path
for the root parser, the check parser and every subparser) calling
``usage_errors.closed_message``.

Documented bound: positional arguments are command data.  A value passed as a
positional, including one after ``--``, may appear in that command's own
errors; usage errors never show argv values.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import os
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

from csk import build_https, cli, installer, usage_errors
from test_token_redaction_matrix import redaction_config  # noqa: F401  (fixture)
from usage_closure import (
    GENERIC,
    error_message,
    is_closed_usage_error,
    marker_is_command_data,
    usage_failed,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def stub_dispatch(monkeypatch):
    """Replace command dispatch: any output comes from the parser alone."""
    reached = []

    def dispatch(args):
        reached.append(args)
        return cli.EXIT_OK

    monkeypatch.setattr(cli, "_dispatch", dispatch)
    return reached


# ---------------------------------------------------------------------------
# Generated fuzz over every panel shape.
# ---------------------------------------------------------------------------

FUZZ_SEED = 261013
FUZZ_CASES = 1500

# Pd dashes and lookalikes (the brief's closed list plus the ones the rev-4
# panels found: Armenian hyphen, two-em dash, Mongolian hyphen, katakana-hiragana).
DASHES = (
    ["-"]
    + [chr(code) for code in range(0x2010, 0x2016)]
    + ["\u2212", "\ufe58", "\ufe63", "\uff0d", "\u058a", "\u2e3a", "\u1806", "\u30a0"]
)
COMBINING = ["\u0301", "\u0308", "\u0334", "\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad", "\u00a0"]
CONFUSABLE = {"o": ["\u043e", "\u03bf", "\u0585", "\u1d0f"], "e": ["\u0435", "\u212e"], "t": ["\u0442", "\uff54"], "k": ["\u043a"], "n": ["\u0578"]}
BASE_NAMES = [
    "token", "token-file", "token-source", "token-env",
    "tok", "t", "to", "toke", "token-f", "token-fil", "token-e", "token-s",
    "tokne", "tokn", "tkoen", "toekn", "tken", "tokenx", "token_file", "token_env",
    "Token", "TOKEN-FILE", "Token-Source", "registry", "publish", "json", "bogus",
]
VALUE_SHAPES = ["plain", "dash-lead", "spaces", "newline", "quotes", "backslash", "nbsp-shy", "equals", "unicode", "long", "tab", "empty-tail"]


def _value(rng, marker, shape):
    return {
        "plain": marker,
        "dash-lead": "-" + marker,
        "spaces": f"a {marker} b",
        "newline": f"a\n{marker}\nb",
        "quotes": f"'{marker}\"",
        "backslash": f"C:\\{marker}\\x",
        "nbsp-shy": f"\u00e9{marker}\u00a0t\u00adx",
        "equals": f"a={marker}=b",
        "unicode": f"\u00fcn\u00ef{marker}\u4e2d\u6587\U0001f600",
        "long": marker * 40,
        "tab": f"\t{marker}\t",
        "empty-tail": marker + " ",
    }[shape]


def _spelling(rng):
    kind = rng.choice(["plain", "dash", "confusable", "combining", "glued", "short", "space", "upper"])
    name = rng.choice(BASE_NAMES)
    if kind == "plain":
        return "--" + name
    if kind == "dash":
        return rng.choice(DASHES) * rng.choice([1, 2]) + name
    if kind == "confusable":
        letters = [c for c in name if c in CONFUSABLE]
        if letters:
            letter = rng.choice(letters)
            name = name.replace(letter, rng.choice(CONFUSABLE[letter]), 1)
        return "--" + name
    if kind == "combining":
        position = rng.randrange(len(name) + 1)
        return "--" + name[:position] + rng.choice(COMBINING) + name[position:]
    if kind == "glued":
        return "--" + name  # the value is glued on by the form below
    if kind == "short":
        return rng.choice(["-t", "-to", "-tok", "-T", "-token"])
    if kind == "space":
        return rng.choice([" ", "\u00a0", "\t"]) + "--" + name
    return "--" + name.upper()


def _words(rng, spelling, value):
    form = rng.choice([
        "sep", "eq", "glued", "missing", "dashdash-before", "dashdash-eq",
        "sep-dashdash", "repeat", "value-only", "double", "help",
    ])
    if form == "sep":
        return [spelling, value]
    if form == "eq":
        return [spelling + "=" + value]
    if form == "glued":
        return [spelling + value]
    if form == "missing":
        return [spelling]
    if form == "dashdash-before":
        return ["--", spelling, value]
    if form == "dashdash-eq":
        return ["--", spelling + "=" + value]
    if form == "sep-dashdash":
        return [spelling, "--", value]
    if form == "repeat":
        return [spelling, value, spelling, value + "2"]
    if form == "value-only":
        return [value]
    if form == "double":
        return [spelling + "=" + value, spelling, value]
    return [spelling, value, rng.choice(["-h", "--help"])]


def _prefixes():
    found = [()]

    def walk(parser, prefix):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    found.append(prefix + (name,))
                    walk(child, prefix + (name,))

    walk(cli.build_parser(), ())
    found.append(("check",))
    found.append(("dispatch",))
    walk(cli._build_dispatch_parser(), ("dispatch",))
    return found


def _soup(rng, marker):
    vocabulary = [
        "audit", "install", "config", "build-https", "add", "login", "list", "check", "skill", "shell-init",
        "project", "global", "--", "-h", "--json", "--publish", "--registry", "--global", "scope.example/g",
        "--token-file", "--token-env", "--token-source", "--token",
    ]
    words = []
    for _ in range(rng.randrange(2, 9)):
        pick = rng.random()
        if pick < 0.45:
            words.append(rng.choice(vocabulary))
        elif pick < 0.65:
            words.append(_spelling(rng))
        else:
            words.append(_value(rng, marker, rng.choice(VALUE_SHAPES)))
    return words


def generate_cases(seed=FUZZ_SEED, count=FUZZ_CASES):
    rng = random.Random(seed)
    prefixes = _prefixes()
    cases = []
    for index in range(count):
        marker = f"SM{rng.getrandbits(64):016x}"
        if index % 5 == 4:
            argv = _soup(rng, marker)
            if marker not in "".join(argv):
                argv.append(_value(rng, marker, rng.choice(VALUE_SHAPES)))
        else:
            words = _words(rng, _spelling(rng), _value(rng, marker, rng.choice(VALUE_SHAPES)))
            prefix = list(rng.choice(prefixes))
            slot = rng.randrange(len(prefix) + 1)
            argv = prefix[:slot] + words + prefix[slot:]
            if rng.random() < 0.3:
                argv = argv + words
        cases.append((argv, marker))
    return cases


def test_fuzz_corpus_covers_every_panel_shape():
    # The generator is only evidence if it reaches the shapes the panels found.
    cases = generate_cases()
    flat = [" ".join(argv) for argv, _ in cases]
    joined = "\n".join(flat)
    for fragment in ["--tokne", "--tokn", "\u043e", "\u0301", "\u058a", "\u2e3a", "=", " -- ", "--tok", "-t", "-h"]:
        assert fragment in joined, fragment
    assert any("\u00ad" in text for text in flat)
    assert any("\u00a0" in text for text in flat)
    assert {argv[0] for argv, _ in cases if argv} >= {"audit", "config", "check"}
    assert len(cases) == FUZZ_CASES


def test_usage_errors_never_echo_argv_values_fuzz(stub_dispatch, capsys):
    # Property: for every generated argv carrying a random secret marker, no
    # marker appears in stdout or stderr of any usage error, the exit code is
    # 2, and the printed message is allowlisted.  Dispatch is stubbed, so any
    # output is the parser's: positional command data cannot echo here.
    failures = []
    usage_errors_seen = 0
    for argv, marker in generate_cases():
        stub_dispatch.clear()
        code = cli.main(argv)
        output = capsys.readouterr()
        text = output.out + output.err
        if marker in text:
            failures.append((argv, "marker echoed", text[-300:]))
            continue
        if stub_dispatch or code == 0:
            if output.err:
                failures.append((argv, "output without a usage error", output.err[-200:]))
            continue
        usage_errors_seen += 1
        if code != 2 or output.out or not is_closed_usage_error(output.err):
            failures.append((argv, code, output.err[-300:]))
    assert not failures, f"{len(failures)} fuzz cases failed: {failures[:5]}"
    assert usage_errors_seen > FUZZ_CASES // 2


def test_usage_errors_never_echo_argv_values_through_the_module_entry():
    # The same property through python -m csk, so the real entry point and a
    # fresh interpreter agree.  Only argv the parser rejects is executed (a
    # seeded subset, one process each): no command is ever dispatched.
    cases = [
        (argv, marker) for argv, marker in generate_cases(FUZZ_SEED + 1, 150)
        if usage_failed(argv)
    ][:12]
    assert len(cases) == 12
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    for argv, marker in cases:
        proc = subprocess.run(
            [sys.executable, "-m", "csk", *argv],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 2, (argv, proc.stderr[-300:])
        assert marker not in proc.stdout + proc.stderr, (argv, proc.stderr[-300:])
        assert proc.stdout == ""
        assert is_closed_usage_error(proc.stderr), (argv, proc.stderr[-300:])


def test_every_low_code_point_and_a_stride_above_are_hidden():
    # Property over code points: argparse echoes values through repr()/str();
    # the closed text echoes nothing, so no code point can make a difference.
    # Bound: exhaustive below U+2000 (controls, Latin, combining marks, Pd dashes),
    # strided above it to keep the Windows ordinary lane inside its time budget.
    parser = cli.build_parser()
    marker = "SMCODEPOINT"
    sink = io.StringIO()
    failures = []
    for code_point in range(0x110000):
        if code_point >= 0x2000 and code_point % 61:
            continue  # every code point below U+2000, a stride above it
        value = f"{marker}{chr(code_point)}{marker}"
        for argv in (["audit", f"--bogus={value}"], ["shell-init", value]):
            sink.seek(0)
            sink.truncate()
            try:
                with contextlib.redirect_stderr(sink), contextlib.redirect_stdout(sink):
                    parser.parse_args(argv)
            except SystemExit:
                pass
            if marker in sink.getvalue():
                failures.append(hex(code_point))
                break
    assert not failures, failures[:10]


# ---------------------------------------------------------------------------
# One test per argparse error kind: it maps to an allowlisted template.
# ---------------------------------------------------------------------------

def _mini(*specs):
    parser = cli._CskArgumentParser(prog="mini")
    for spec in specs:
        spec(parser)
    return parser


def _typed(parser):
    parser.add_argument("--num", type=int)


def _pair(parser):
    parser.add_argument("--pair", nargs=2)


def _many(parser):
    parser.add_argument("--many", nargs="+")


def _target(parser):
    parser.add_argument("target")


def _choice(parser):
    parser.add_argument("--mode", choices=["fast", "slow"])


def _exclusive(parser):
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--aa", action="store_true")
    group.add_argument("--bb", action="store_true")


def _shorts(parser):
    # csk parsers set allow_abbrev=False, which newer CPython honours for
    # single-dash prefixes too; the template is still reachable on a parser
    # that re-enables it, and the closed path must not depend on the setting.
    parser.allow_abbrev = True
    parser.add_argument("-xa", action="store_true")
    parser.add_argument("-xb", action="store_true")


V = "SECRETVALUE"
KINDS = [
    # (kind, parser factory, argv, exact message printed after "<prog>: error: ")
    ("unrecognized arguments", cli.build_parser, ["audit", "--bogus", V],
     "unrecognized arguments: <value not shown>"),
    ("required arguments", cli.build_parser, ["config"],
     "the following arguments are required: config_command"),
    ("expected one argument", cli.build_parser, ["audit", "--token-file"],
     "argument --token-file: expected one argument"),
    ("invalid choice (subcommand)", cli.build_parser, [V],
     "argument command: invalid choice: <value not shown> (choose from "),
    ("invalid choice (positional)", cli.build_parser, ["shell-init", V],
     "argument shell: invalid choice: <value not shown> (choose from 'auto', 'zsh', 'bash', 'powershell')"),
    ("invalid choice (option)", lambda: _mini(_choice), ["--mode", V],
     "argument --mode: invalid choice: <value not shown> (choose from 'fast', 'slow')"),
    ("ignored explicit argument", cli.build_parser, ["audit", "--help=" + V],
     "argument -h/--help: ignored explicit argument <value not shown>"),
    ("static: token source", cli.build_parser,
     ["config", "build-https", "add", "s.example", "--token-source", V],
     "argument --token-source: must be one of git-credentials, keyring"),
    ("static: token env", cli.build_parser,
     ["config", "build-https", "add", "s.example", "--token-env", "bad name"],
     "argument --token-env: must be an environment variable name ([A-Za-z_][A-Za-z0-9_]{0,127})"),
    ("static: --token refusal (audit)", cli.build_parser, ["audit", "--token", V],
     "--token is refused; use CSK_REGISTRY_TOKEN or --token-file for registry authorization"),
    ("static: --token refusal (build-https add)", cli.build_parser,
     ["config", "build-https", "add", "s.example", "--token", V],
     "--token is refused; select an HTTPS credential source with --token-source or --token-env; "
     "for audit --publish use CSK_REGISTRY_TOKEN or --token-file"),
    ("invalid typed value", lambda: _mini(_typed), ["--num", V],
     "argument --num: invalid int value: <value not shown>"),
    ("expected N arguments", lambda: _mini(_pair), ["--pair", V],
     "argument --pair: expected 2 arguments"),
    ("expected at least one argument", lambda: _mini(_many), ["--many"],
     "argument --many: expected at least one argument"),
    ("required positional", lambda: _mini(_target), [],
     "the following arguments are required: target"),
    ("not allowed with argument", lambda: _mini(_exclusive), ["--aa", "--bb"],
     "argument --bb: not allowed with argument --aa"),
    ("one of the arguments is required", lambda: _mini(_exclusive), [],
     "one of the arguments --aa --bb is required"),
    ("ambiguous option", lambda: _mini(_shorts), ["-x"],
     "ambiguous option: <value not shown> could match -xa, -xb"),
]


@pytest.mark.parametrize("kind,factory,argv,expected", KINDS, ids=[k[0] for k in KINDS])
def test_each_argparse_error_kind_maps_to_an_allowlisted_template(kind, factory, argv, expected, capsys):
    # Production call site: _CskArgumentParser.error -> usage_errors.closed_message.
    parser = factory()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(argv)
    assert exc.value.code == 2
    output = capsys.readouterr()
    message = error_message(output.err)
    assert message is not None, output.err
    if kind == "invalid choice (subcommand)":
        # The full choice list is parser-defined; pin its head and two members.
        assert message.startswith(expected)
        assert "'audit'" in message and "'config'" in message
    else:
        assert message == expected, (kind, message)
    assert V not in output.out + output.err
    assert message != GENERIC, "a reachable error kind must have its own template"
    assert is_closed_usage_error(output.err), message


def test_argparse_messages_of_this_python_are_all_classified():
    # Version bound made executable: every message template the running
    # argparse can emit is either rebuilt by an allowlisted template, falls to
    # the generic fallback by design, or is raised at parser-construction time
    # (never from argv).  A Python that adds a message fails here until a
    # human classifies it.
    tree = ast.parse(Path(argparse.__file__).read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_" and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            found.add(node.args[0].value)
    rebuilt = {
        "unrecognized arguments: %s",
        "unexpected option string: %s",
        "the following arguments are required: %s",
        "one of the arguments %s is required",
        "ambiguous option: %(option)s could match %(matches)s",
        "argument %(argument_name)s: %(message)s",
        "expected one argument",
        "expected at most one argument",
        "expected at least one argument",
        "ignored explicit argument %r",
        "invalid %(type)s value: %(value)r",
        "invalid choice: %(value)r (choose from %(choices)s)",
        "invalid choice: %(value)r, maybe you meant %(closest)r? (choose from %(choices)s)",
        "not allowed with argument %s",
        "cannot have multiple subparser arguments",
    }
    generic_by_design = {
        # Unreachable from argv: the choice check runs first and rejects the name.
        "unknown parser %(parser_name)r (choices: %(choices)s)",
    }
    construction_or_help_only = {
        ' (default: %(default)s)', '.__call__() not defined', '%(heading)s:',
        '%(prog)s: error: %(message)s\n', '%(prog)s: warning: %(message)s\n',
        '%r is not callable', 'argument "-" with mode %r', 'cannot merge actions - two groups are named %r',
        'conflicting subparser alias: %s', 'conflicting subparser: %s', 'dest= is required for options like %r',
        'invalid conflict_resolution value: %r',
        'invalid option string %(option)r: must start with a character %(prefix_chars)r',
        'mutually exclusive arguments must be optional', 'options', 'positional arguments',
        'show this help message and exit', 'subcommands', 'usage: ', "'required' is an invalid argument for positionals",
        "show program's version number and exit",
        # Opt-in surfaces csk never enables (pinned by the two tests below).
        "argument '%(argument_name)s' is deprecated", "command '%(parser_name)s' is deprecated",
        "option '%(option)s' is deprecated", "can't open '%(filename)s': %(error)s",
    }
    unclassified = found - rebuilt - generic_by_design - construction_or_help_only
    assert not unclassified, f"new argparse messages need classification: {sorted(unclassified)}"


def _all_parsers():
    seen = []

    def walk(parser):
        seen.append(parser)
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    walk(child)

    walk(cli.build_parser())
    walk(cli._build_check_parser())
    walk(cli._build_dispatch_parser())
    return seen


def test_every_parser_and_subparser_uses_the_shared_error_path():
    parsers = _all_parsers()
    assert len(parsers) > 40
    assert {p.prog for p in parsers} >= {"csk dispatch", "csk dispatch setup", "csk dispatch recover"}
    for parser in parsers:
        assert isinstance(parser, cli._CskArgumentParser), parser.prog
        assert type(parser).error is cli._CskArgumentParser.error, parser.prog
        assert parser.allow_abbrev is False, parser.prog


def test_parser_actions_use_only_argv_free_types_and_no_opt_in_surfaces():
    # A type= callable raising ArgumentTypeError (or argparse.FileType) can put
    # argv bytes into the message; only the two audited callables are allowed,
    # and their messages are the static allowlisted texts.
    allowed = {None, cli._token_source_type, cli._token_env_type}
    for parser in _all_parsers():
        for action in parser._actions:
            assert action.type in allowed, (parser.prog, action.dest, action.type)
            assert not getattr(action, "deprecated", False), (parser.prog, action.dest)
        assert not getattr(parser, "suggest_on_error", False), parser.prog
    assert cli._TOKEN_SOURCE_MESSAGE in cli._STATIC_USAGE_MESSAGES
    assert cli._TOKEN_ENV_MESSAGE in cli._STATIC_USAGE_MESSAGES
    for function, bad in ((cli._token_source_type, V), (cli._token_env_type, "bad name")):
        with pytest.raises(argparse.ArgumentTypeError) as exc:
            function(bad)
        assert str(exc.value) in cli._STATIC_USAGE_MESSAGES
        assert bad not in str(exc.value)


# ---------------------------------------------------------------------------
# The fallback and templates imitated by values.
# ---------------------------------------------------------------------------

TEMPLATE_UNITS = [
    (lambda: _mini(_typed), "argument --num: expected one argument", "argument --num: expected one argument"),
    (lambda: _mini(_typed), "argument --num: expected at most one argument", "argument --num: expected at most one argument"),
    (lambda: _mini(_many), "argument --many: expected at least one argument", "argument --many: expected at least one argument"),
    (lambda: _mini(_pair), "argument --pair: expected 2 arguments", "argument --pair: expected 2 arguments"),
    (lambda: _subparser("config", "build-https", "add"), f"argument --token-env: {cli._TOKEN_ENV_MESSAGE}", f"argument --token-env: {cli._TOKEN_ENV_MESSAGE}"),
    (lambda: _subparser("config", "build-https", "add"), f"argument --token-source: {cli._TOKEN_SOURCE_MESSAGE}", f"argument --token-source: {cli._TOKEN_SOURCE_MESSAGE}"),
    (cli.build_parser, cli._TOKEN_REFUSED_MESSAGE, cli._TOKEN_REFUSED_MESSAGE),
    (cli.build_parser, cli._TOKEN_REFUSED_HTTPS_MESSAGE, cli._TOKEN_REFUSED_HTTPS_MESSAGE),
    # (parser factory, raw argparse text carrying a value, rebuilt text)
    (cli.build_parser, f"unrecognized arguments: --x {V} {V}", "unrecognized arguments: <value not shown>"),
    (cli.build_parser, f"unexpected option string: {V}", "unexpected option string: <value not shown>"),
    (cli.build_parser, "the following arguments are required: command", "the following arguments are required: command"),
    (lambda: _mini(_exclusive), "one of the arguments --aa --bb is required", "one of the arguments --aa --bb is required"),
    (lambda: _mini(_shorts), f"ambiguous option: -x={V} could match -xa, -xb",
     "ambiguous option: <value not shown> could match -xa, -xb"),
    (cli.build_parser, f"argument -h/--help: ignored explicit argument {V!r}",
     "argument -h/--help: ignored explicit argument <value not shown>"),
    (lambda: _mini(_typed), f"argument --num: invalid int value: {V!r}", "argument --num: invalid int value: <value not shown>"),
    (lambda: _mini(_choice), f"argument --mode: invalid choice: {V!r} (choose from 'fast', 'slow')",
     "argument --mode: invalid choice: <value not shown> (choose from 'fast', 'slow')"),
    (lambda: _mini(_choice), f"argument --mode: invalid choice: {V!r}, maybe you meant 'fast'? (choose from 'fast', 'slow')",
     "argument --mode: invalid choice: <value not shown> (choose from 'fast', 'slow')"),
    (lambda: _mini(_exclusive), "argument --bb: not allowed with argument --aa", "argument --bb: not allowed with argument --aa"),
    (cli.build_parser, "cannot have multiple subparser arguments", "cannot have multiple subparser arguments"),
]


@pytest.mark.parametrize("factory,raw,rebuilt", TEMPLATE_UNITS, ids=[u[2][:48] for u in TEMPLATE_UNITS])
def test_each_template_rebuilds_from_parser_defined_parts_only(factory, raw, rebuilt):
    # Direct call of the one classifier, so templates argparse cannot reach
    # through these parsers (unexpected option string, typed-value on a csk
    # parser) are still pinned: the value never survives the rebuild.
    printed = usage_errors.closed_message(factory(), raw, cli._STATIC_USAGE_MESSAGES)
    assert printed == rebuilt
    assert V not in printed


def _subparser(*names):
    parser = cli.build_parser()
    for name in names:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                parser = action.choices[name]
                break
    return parser


def test_static_texts_match_exactly_on_the_parser_that_declares_them():
    # A static csk message is admitted only whole: appended bytes (a value)
    # fall back to the generic text, on the parser that really owns the option.
    add = _subparser("config", "build-https", "add")
    for static in (cli._TOKEN_SOURCE_MESSAGE, cli._TOKEN_ENV_MESSAGE):
        option = "--token-source" if static == cli._TOKEN_SOURCE_MESSAGE else "--token-env"
        whole = f"argument {option}: {static}"
        assert usage_errors.closed_message(add, whole, cli._STATIC_USAGE_MESSAGES) == whole
        assert usage_errors.closed_message(add, f"{whole} {V}", cli._STATIC_USAGE_MESSAGES) == GENERIC
        assert usage_errors.closed_message(add, f"{whole}; {V}", cli._STATIC_USAGE_MESSAGES) == GENERIC
    for static in (cli._TOKEN_REFUSED_MESSAGE, cli._TOKEN_REFUSED_HTTPS_MESSAGE):
        assert usage_errors.closed_message(add, static, cli._STATIC_USAGE_MESSAGES) == static
        assert usage_errors.closed_message(add, f"{static} {V}", cli._STATIC_USAGE_MESSAGES) == GENERIC
    assert usage_errors.closed_message(
        add, f"argument --token-env: expected one argument {V}", cli._STATIC_USAGE_MESSAGES
    ) == GENERIC
    assert usage_errors.closed_message(
        add, "argument --token-env: expected one argument", cli._STATIC_USAGE_MESSAGES
    ) == "argument --token-env: expected one argument"


def test_a_message_matching_no_template_prints_the_generic_fallback(capsys):
    parser = cli.build_parser()
    for message in [
        f"something argparse never said {V}",
        f"{cli._TOKEN_REFUSED_MESSAGE} {V}",
        f"{cli._TOKEN_REFUSED_HTTPS_MESSAGE}; {V}",
        f"unknown parser '{V}' (choices: audit)",
        f"argument bogus: invalid choice: '{V}' (choose from 'a')",
        f"argument --token-file: expected one argument {V}",
        f"argument --token-file: must be one of git-credentials, keyring {V}",
        f"the following arguments are required: {V}",
        f"one of the arguments {V} is required",
        f"ambiguous option: {V} could match {V}",
        "",
    ]:
        assert usage_errors.closed_message(parser, message, cli._STATIC_USAGE_MESSAGES) == GENERIC, message
    with pytest.raises(SystemExit) as exc:
        parser.error(f"something argparse never said {V}")
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert error_message(output.err) == GENERIC
    assert V not in output.out + output.err


def test_values_imitating_a_template_tail_do_not_change_what_is_printed():
    parser = cli.build_parser()
    action = "argument command: invalid choice: "
    for value in [
        "x (choose from 'audit')", "' (choose from 'audit') ", "could match --token-file",
        "\n(choose from)", ", maybe you meant 'audit'? (choose from 'audit')",
    ]:
        message = f"{action}{value!r} (choose from {', '.join(map(repr, ['audit']))})"
        printed = usage_errors.closed_message(parser, message, cli._STATIC_USAGE_MESSAGES)
        assert printed.startswith("argument command: invalid choice: <value not shown> (choose from 'bootstrap'")
        assert value not in printed
    # The ambiguous tail is split at the LAST separator, so an option that
    # imitates it still yields only parser-defined matches.
    mini = _mini(_shorts)
    printed = usage_errors.closed_message(
        mini, "ambiguous option: -x could match -xa could match -xa, -xb"
    )
    assert printed == "ambiguous option: <value not shown> could match -xa, -xb"
    assert usage_errors.closed_message(
        mini, "ambiguous option: -x could match -xa, SECRET"
    ) == GENERIC
    exclusive = _mini(_exclusive)
    assert usage_errors.closed_message(
        exclusive, f"argument --bb: not allowed with argument {V}"
    ) == GENERIC
    assert usage_errors.closed_message(
        exclusive, "argument --bb: not allowed with argument --aa"
    ) == "argument --bb: not allowed with argument --aa"


def test_real_parse_with_template_imitating_values_is_closed(capsys):
    parser = cli.build_parser()
    for argv in (
        ["shell-init", "x (choose from 'auto') could match --token-file"],
        ["audit", "--bogus", "ambiguous option: x could match --token-file"],
        ["config", "the following arguments are required: " + V],
        ["audit", "--help=argument command: invalid choice: " + V],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
        output = capsys.readouterr()
        assert is_closed_usage_error(output.err), output.err
        assert V not in output.err
        assert "could match" not in output.err


# ---------------------------------------------------------------------------
# Named reproductions: rev-4 panels TASK-261013-3hvd29 / 3f63mn / 2j8txz.
# ---------------------------------------------------------------------------

# Spellings argparse sees as options (start with an ASCII '-'): usage errors.
REV4_F2_OPTION_NAMES = [
    "--tokne", "--tokn", "--tkoen", "--t\u0301oken", "--t\u043ek\u0435n", "--tokSECRETVALUE",
    "--to\u200dken", "--tok\u00a0en", "-t\u0301", "--token\u2060",
]
# Lookalike dashes and leading spaces do not make an option: argparse sees a
# positional, i.e. command data under the documented bound.
REV4_F2_POSITIONAL_NAMES = ["\u058a\u058atoken", "\u2e3a\u2e3atoken", "\u2014\u2014token", " --token", "\u00a0--token"]


@pytest.mark.parametrize("index,name", list(enumerate(REV4_F2_OPTION_NAMES)))
@pytest.mark.parametrize("form", ["sep", "eq"])
def test_rev4_f2_misspelled_and_confusable_names_are_closed(index, name, form, redaction_config, capsys):
    # 3hvd29 F2 / 3f63mn F2: no spelling is special; every one is an ordinary
    # unknown argument, so the usage error never shows its value or its name.
    value = f"SECRETVALUE{index}x"
    words = [name, value] if form == "sep" else [name + "=" + value]
    code = cli.main(["audit", *words])
    output = capsys.readouterr()
    assert code == cli.EXIT_CONFIG
    assert value not in output.out + output.err
    assert is_closed_usage_error(output.err), output.err


@pytest.mark.parametrize("index,name", list(enumerate(REV4_F2_POSITIONAL_NAMES)))
@pytest.mark.parametrize("form", ["sep", "eq"])
def test_rev4_f2_lookalike_dash_names_are_command_data(index, name, form, redaction_config, capsys):
    # 2j8txz dash list: U+058A, U+2E3A, U+2014 and a leading space are not
    # option prefixes, so they are positionals.  A usage error would be closed;
    # a parsed positional is the command's own data (documented bound).
    value = f"SECRETVALUE{index}y"
    words = [name, value] if form == "sep" else [name + "=" + value]
    argv = ["audit", *words]
    code = cli.main(argv)
    output = capsys.readouterr()
    assert code == cli.EXIT_CONFIG
    if usage_failed(argv):
        assert is_closed_usage_error(output.err)
        assert value not in output.out + output.err
    else:
        assert marker_is_command_data(argv, value) or value not in output.out + output.err


def test_rev4_f1_repeated_declared_option_after_separator_is_command_data(redaction_config, capsys):
    # 3hvd29 F1 / 3f63mn F1 / 2j8txz blocking finding, bound by decision: the
    # element after `--` is a positional, i.e. command data that the command's
    # own error may show.  The credential option's own value never is.
    assert cli.main(["audit", "--token-file", "FILEVALUE", "--", "--token-file=POSITIONAL"]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert output.err == "error: Unknown project alias: --token-file=POSITIONAL\n"
    assert "FILEVALUE" not in output.out + output.err
    assert not is_closed_usage_error(output.err)


def test_rev4_f1_build_https_scope_positional_is_command_data(redaction_config, capsys):
    assert cli.main(["config", "build-https", "add", "--token-env", "GOODNAME",
                     "--", "--token-env=POSITIONAL"]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert "POSITIONAL" in output.err  # the scope positional, in the command's own error
    assert "GOODNAME" not in output.out + output.err
    assert not is_closed_usage_error(output.err)


def test_rev4_f3_a_valid_command_with_dash_t_positional_is_not_refused(redaction_config, capsys):
    # 3hvd29 F3: `csk skill check -- -t.skill` is a valid command line.  There
    # is no predicate, so it reaches the command and fails (or not) on its own.
    code = cli.main(["skill", "check", "--", "-t.skill"])
    output = capsys.readouterr()
    assert not is_closed_usage_error(output.err)
    assert GENERIC not in output.err
    assert code in (cli.EXIT_OK, cli.EXIT_PARTIAL_FAIL, cli.EXIT_CONFIG)


@pytest.mark.parametrize("name", ["SECRETMARK_GHP_abcdef123456", "ci_token", "Mixed_Token", "ghp_abcdef123456"])
def test_rev4_f3_installer_does_not_echo_the_configured_env_name(monkeypatch, name):
    # 3f63mn F3: the installer's "environment variable ... is unset" error
    # echoed a --token-env NAME (an identifier-shaped secret is a valid NAME).
    monkeypatch.delenv(name, raising=False)
    rule = build_https.BuildHTTPSRule(scope="gitlab.example.com/acme", token_env=name)
    with pytest.raises(installer.InstallError) as excinfo:
        installer._https_rule_credentials(rule, "gitlab.example.com/acme/tool")
    text = str(excinfo.value)
    assert name not in text
    assert "the environment variable configured for build_https scope 'gitlab.example.com/acme' is not set" in text


def test_rev4_f5_changelog_entry_is_under_unreleased():
    # 3f63mn F5: the entry must not land inside a released section.
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = re.split(r"(?m)^## ", changelog)
    unreleased = next(section for section in sections if section.startswith("[Unreleased]"))
    assert "<value not shown>" in unreleased
    for section in sections:
        if section.startswith("[Unreleased]") or not section.startswith("["):
            continue
        assert "<value not shown>" not in section, section.splitlines()[0]


def test_legitimately_failing_command_keeps_its_own_output_and_exit_code(redaction_config, capsys):
    # A command error is not a usage error: it keeps its own text and code
    # even with a credential option present (nothing is buffered or replaced).
    code = cli.main(["config", "build-https", "add", "bad scope with spaces", "--token-env", "GOODNAME"])
    output = capsys.readouterr()
    assert code == cli.EXIT_CONFIG
    assert output.err.strip()
    assert not is_closed_usage_error(output.err)
    assert "GOODNAME" not in output.out + output.err


# Rev-6 regression pins for the rev-5 panels. The real error() path is driven
# at every public parser and at synthetic depth for kinds not enabled by csk.
@pytest.mark.parametrize("prefix", _prefixes(), ids=lambda p: " ".join(p) or "root")
@pytest.mark.parametrize("flag", ["--help=", "-h="])
def test_rev6_ignored_explicit_argument_at_every_public_depth(prefix, flag, capsys):
    marker = "SECRETDEPTH_é中"
    assert cli.main([*prefix, flag + marker]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert marker not in output.err
    assert error_message(output.err) == "argument -h/--help: ignored explicit argument <value not shown>"
    # Pin the emitting parser, not just an error bubbled back to the root.
    prog = "csk" + (" " + " ".join(prefix) if prefix else "")
    assert f"{prog}: error: " in output.err


def _deep_command(parser):
    parser.add_subparsers(dest="deep_command", required=True).add_parser("known")


def _deep_static(parser):
    parser.add_argument("--token-source", type=cli._token_source_type)
    parser.add_argument("--token-env", type=cli._token_env_type)
    parser.add_argument("--token", action=cli._RefuseTokenAction, nargs="?")


DEEP_KINDS = [
    ("subcommand-choice", (_deep_command,), ["MARK"], "argument deep_command: invalid choice: <value not shown> (choose from 'known')"),
    ("subcommand-required", (_deep_command,), [], "the following arguments are required: deep_command"),
    ("token-source", (_deep_static,), ["--token-source=MARK"], "argument --token-source: must be one of git-credentials, keyring"),
    ("token-env", (_deep_static,), ["--token-env=MARK"], "argument --token-env: must be an environment variable name ([A-Za-z_][A-Za-z0-9_]{0,127})"),
    ("token-refusal", (_deep_static,), ["--token=MARK"], cli._TOKEN_REFUSED_MESSAGE),
    ("unrecognized", (), ["--bogus=MARK"], "unrecognized arguments: <value not shown>"),
    ("typed", (_typed,), ["--num=MARK"], "argument --num: invalid int value: <value not shown>"),
    ("choice", (_choice,), ["--mode=MARK"], "argument --mode: invalid choice: <value not shown> (choose from 'fast', 'slow')"),
    ("explicit", (), ["--help=MARK"], "argument -h/--help: ignored explicit argument <value not shown>"),
    ("one", (_typed,), ["--num"], "argument --num: expected one argument"),
    ("N", (_pair,), ["--pair", "MARK"], "argument --pair: expected 2 arguments"),
    ("many", (_many,), ["--many"], "argument --many: expected at least one argument"),
    ("required", (_target,), [], "the following arguments are required: target"),
    ("mutex", (_exclusive,), ["--aa", "--bb"], "argument --bb: not allowed with argument --aa"),
    ("mutex-required", (_exclusive,), [], "one of the arguments --aa --bb is required"),
    ("ambiguous", (_shorts,), ["-x"], "ambiguous option: <value not shown> could match -xa, -xb"),
]


@pytest.mark.parametrize("depth", [2, 3])
@pytest.mark.parametrize("kind,specs,words,expected", DEEP_KINDS, ids=[k[0] for k in DEEP_KINDS])
def test_rev6_each_argparse_error_kind_raised_at_depth(depth, kind, specs, words, expected, capsys):
    root = cli._CskArgumentParser(prog="csk")
    leaf = root
    prefix = []
    for index in range(depth):
        name = f"level{index}"
        prefix.append(name)
        leaf = leaf.add_subparsers(dest=f"command{index}", required=True).add_parser(name)
    for spec in specs:
        spec(leaf)
    leaf.add_argument("--marker")
    marker = "SECRETDEEP_é中"
    argv = [*prefix, "--marker", marker, *[w.replace("MARK", marker) for w in words]]
    # parse_args rejects unknown extras at the parsing root. Call the selected
    # leaf for that kind to drive its actual error(), while all other kinds
    # originate at the leaf during a full root parse.
    parser, arguments = (leaf, argv[depth:]) if kind == "unrecognized" else (root, argv)
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(arguments)
    output = capsys.readouterr()
    assert exc.value.code == 2
    assert output.out == ""
    assert marker not in output.err
    assert f"{leaf.prog}: error: " in output.err
    assert error_message(output.err) == expected


HOSTILE_VALUES = [
    ("c0", "SECRETCONTROL\x00\x01\x08\x0b\x0c\x1fEND"),
    ("surrogate", "SECRETSURROGATE\ud800\udcff\udfffEND"),
    ("line-separators", "SECRETSEP\u0085\u2028\u2029END"),
    ("bidi", "SECRETBIDI\u202e\u2066\u2067\u2068\u2069END"),
    ("ansi", "SECRETANSI\x1b[31mRED\x1b[0mEND"),
    ("multiline", "SECRETMULTI\nline two\r\nline threeEND"),
    ("long", "SECRETLONG" + "x" * 5000 + "END"),
]


@pytest.mark.parametrize("kind,value", HOSTILE_VALUES, ids=[k[0] for k in HOSTILE_VALUES])
@pytest.mark.parametrize("prefix", [("list",), ("audit", "target"), ("config", "build-https", "list"), ("dispatch", "setup")])
def test_rev6_hostile_values_hidden_on_raw_echo_path(kind, value, prefix, capsys, monkeypatch):
    raw_messages = []
    real_error = cli._CskArgumentParser.error

    def record_error(self, message):
        raw_messages.append(message)
        return real_error(self, message)

    monkeypatch.setattr(cli._CskArgumentParser, "error", record_error)
    assert cli.main([*prefix, "--bogus", "--", value]) == 2
    output = capsys.readouterr()
    assert len(raw_messages) == 1
    assert raw_messages[0].startswith("unrecognized arguments: ")
    assert value in raw_messages[0]  # Raw str() echo, not repr() or a consumed option.
    assert output.out == ""
    assert value not in output.err
    assert "SECRET" not in output.err
    assert error_message(output.err) == "unrecognized arguments: <value not shown>"


@pytest.mark.parametrize("kind,value", HOSTILE_VALUES, ids=[k[0] for k in HOSTILE_VALUES])
def test_rev6_hostile_unmatched_message_uses_generic_fallback(kind, value, capsys):
    parser = _subparser("config", "build-https", "add")
    with pytest.raises(SystemExit) as exc:
        parser.error("unmatched error " + value)
    output = capsys.readouterr()
    assert exc.value.code == 2
    assert output.out == ""
    assert error_message(output.err) == GENERIC
    assert "SECRET" not in output.err
    assert value not in output.err


@pytest.mark.parametrize("factory,raw,rebuilt", TEMPLATE_UNITS, ids=[u[2][:48] for u in TEMPLATE_UNITS])
@pytest.mark.parametrize("value", ["é中SECRET", "-é中SECRET", "é中=SECRET"])
@pytest.mark.parametrize("depth", [0, 1, 2])
def test_rev6_every_template_has_nonascii_sample_through_error(factory, raw, rebuilt, value, depth, capsys):
    parser = factory()
    if depth:
        parent = cli._CskArgumentParser(prog="csk")
        for index in range(depth):
            options = {"parents": [parser], "add_help": False} if index == depth - 1 else {}
            parent = parent.add_subparsers(dest=f"level{index}").add_parser(f"child{index}", **options)
        parser = parent
    # For argv-free templates, exercise non-ASCII parser-defined names. For
    # dynamic templates substitute the secret itself (including '-' and '=').
    raw, rebuilt = raw.replace(V, value), rebuilt.replace(V, value)
    for action in parser._actions:
        for before in [*action.option_strings, action.dest]:
            if before in ("command", "--aa", "--bb", "--num", "--pair", "--many"):
                after = before + "é"
                raw, rebuilt = raw.replace(before, after), rebuilt.replace(before, after)
                action.option_strings = [after if opt == before else opt for opt in action.option_strings]
                if action.dest == before:
                    action.dest = after
    with pytest.raises(SystemExit) as exc:
        parser.error(raw)
    output = capsys.readouterr()
    assert exc.value.code == 2
    assert output.out == ""
    assert error_message(output.err) == rebuilt
    assert value not in output.err
    # Argv-free templates have no secret field. Appended non-ASCII data
    # must use the fallback rather than being admitted by a prefix match.
    if "SECRET" not in raw:
        with pytest.raises(SystemExit):
            parser.error(raw + " " + value)
        assert error_message(capsys.readouterr().err) == GENERIC
