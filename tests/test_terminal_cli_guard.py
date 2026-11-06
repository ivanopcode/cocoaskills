import argparse
import io
import itertools
import json
import sys

import pytest

from csk import cli
from csk.terminal import TerminalWriter, shell_word, terminal_document


def command_paths(parser, prefix=()):
    subs = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
    if subs:
        for action in subs:
            for name, child in action.choices.items():
                yield from command_paths(child, (*prefix, name))
    else:
        choices = [action.choices for action in parser._actions if not action.option_strings and action.choices]
        for values in itertools.product(*choices):
            yield (*prefix, *values)


def commands():
    return sorted(set(path for name, factory in cli.entry_parser_factories().items()
                      for path in command_paths(factory(), (name,) if name else ())))


# Every parser-defined command/action gets real CLI admission and help coverage.
# This does not claim every operation's business behavior; specialized tests cover
# remote, manifest, validation, JSON and shell-command output after admission.
@pytest.mark.parametrize('command', commands())
def test_every_command_admission(command, capsys):
    assert cli.main([*command, '--help']) == 0
    positive = capsys.readouterr()
    assert 'usage:' in positive.out
    for plant in ['\x1b]0;FORGED\x07', '\rFORGED', '\nFORGED', '\u061c\u200e\u200f\u202eFORGED']:
        assert cli.main([*command, '--unknown-' + plant]) == 2
        output = capsys.readouterr()
        assert 'error:' in output.err
        assert terminal_document(output.out + output.err) == output.out + output.err
        assert '\nFORGED' not in output.out + output.err


# Unknown future text writers are covered at main's stdout/stderr boundary;
# preserved layout and copyable shell words are byte-identical through the guard.
def test_main_guard_covers_writes(monkeypatch, capsys):
    word = shell_word("hostile\n'\x1b\u202e")
    def raw_writer(argv):
        for stream in (sys.stdout, sys.stderr):
            stream.write('heading\n\t')
            stream.writelines(['raw\x1b\r\u061c\u200e\u200f\u200b', word])
        return 0
    monkeypatch.setattr(cli, '_main', raw_writer)
    assert cli.main(['list']) == 0
    captured = capsys.readouterr()
    for output in (captured.out, captured.err):
        assert output.startswith('heading\n\t')
        assert 'raw\\x1b\\r\\u061c\\u200e\\u200f\\u200b' in output
        assert output.endswith(word)


# JSON exemption is semantic, including bidi; current dumps uses ASCII escapes.
# Text stderr remains guarded, and a failed parse with --json never opts out.
def test_json_guard_policy(monkeypatch, capsys):
    raw = 'value\n\x1b\u202e\u200d'
    def output(args):
        print(json.dumps({'value': raw}))
        sys.stderr.write('error\u202e')
        return 0
    monkeypatch.setattr(cli, '_dispatch', output)
    assert cli.main(['status', '--json']) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)['value'] == raw
    assert '\u202e' not in captured.out
    assert captured.err == 'error\\u202e'
    stream = io.StringIO()
    guard = TerminalWriter(stream)
    guard.machine_json = True
    guard.write(json.dumps({'value': raw}, ensure_ascii=False))
    assert json.loads(stream.getvalue())['value'] == raw
    assert '\u202e' in stream.getvalue()  # Opted-out JSON is deliberately not display data.
    assert cli.main(['status', '--json', '--bad-\u202e']) == 2
    captured = capsys.readouterr()
    assert '\u202e' not in captured.err


# The sink deliberately preserves newlines; the validation source must escape them.
def test_validation_reason_newline_boundary():
    from types import SimpleNamespace
    from csk import installer, skillcheck
    plan = SimpleNamespace(decl=SimpleNamespace(name='demo'))
    issue = skillcheck.ValidationIssue('error', 'missing', 'path', 'missing\nFORGED\x1b')
    with pytest.raises(installer.InstallError) as error:
        installer._check_skill_validation_errors([(plan, issue)])
    assert str(error.value) == 'demo: missing\\nFORGED\\x1b'
    assert terminal_document(str(error.value)) == str(error.value)


# Skill-check intentionally emits non-ASCII JSON; raw bidi remains semantic data,
# unlike its guarded text display. C0 bytes are JSON-escaped by the serializer.
def test_real_skill_json_bidi(tmp_path, capsys):
    root = tmp_path / 'repo-\u202e'
    root.mkdir()
    assert cli.main(['skill', 'check', str(root), '--json']) == 1
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert '\u202e' in output
    assert str(root) in json.dumps(payload, ensure_ascii=False)
    assert cli.main(['skill', 'check', str(root)]) == 1
    output = capsys.readouterr()
    assert '\u202e' not in output.out + output.err
