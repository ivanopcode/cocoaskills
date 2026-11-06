"""Execute published remedies with interactive history enabled, not only -c."""
import errno
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import time

import pytest

from csk import published_trees
from csk.terminal import shell_word

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX shell and raw pathname contract')


def executable(shell):
    path = '/bin/bash' if shell == 'bash' and sys.platform == 'darwin' else shutil.which(shell)
    if path is None:
        if shell == 'bash':
            pytest.fail('bash is required for POSIX remedy execution coverage')
        pytest.skip('zsh is unavailable on PATH; bash coverage remains required')
    return path


def paste(shell, command, cwd):
    import pty
    import fcntl
    import termios
    master, slave = pty.openpty()
    prompt = b'CSK_PROMPT> '
    env = dict(os.environ, HOME=str(cwd), HISTFILE='/dev/null', PS1=prompt.decode(),
               PS2='CONT> ', PROMPT=prompt.decode(), TERM='dumb')
    args = ['--noprofile', '--norc', '-i'] if shell == 'bash' else ['-f', '-i']
    def attach_terminal():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    proc = subprocess.Popen([executable(shell), *args], cwd=cwd, env=env,
                            stdin=slave, stdout=slave, stderr=slave, preexec_fn=attach_terminal)
    os.close(slave)
    def until_prompt():
        data = b''
        deadline = time.monotonic() + 90
        while prompt not in data:
            assert time.monotonic() < deadline, repr(data)
            if select.select([master], [], [], 1)[0]:
                data += os.read(master, 65536)
        return data
    try:
        until_prompt()
        for line in [('set -H' if shell == 'bash' else 'setopt BANG_HIST'),
                     'ls -la /dev/null', command]:
            os.write(master, os.fsencode(line) + b'\n')
            until_prompt()
        os.write(master, b'exit\n')
        # Drain the terminal while the shell exits; waiting without reading can
        # stall an interactive shell's terminal shutdown on macOS.
        deadline = time.monotonic() + 90
        while proc.poll() is None:
            assert time.monotonic() < deadline, 'interactive shell did not exit'
            if select.select([master], [], [], 1)[0]:
                try:
                    if not os.read(master, 65536):
                        break
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
        return proc.wait(timeout=90)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=90)
        os.close(master)


def remedy(tmp_path, name):
    root = tmp_path / 'runtime'
    root.mkdir()
    neighbor = root / 'tool'
    neighbor.write_bytes(b'published')
    target = root / name
    try:
        target.write_bytes(b'foreign')
    except OSError as exc:
        if '\udcff' in name and exc.errno in {errno.EILSEQ, 92}:
            pytest.skip('Filesystem rejects invalid UTF-8 names; byte roundtrip tested separately')
        raise
    with pytest.raises(published_trees.RuntimeTreeModified) as caught:
        published_trees.validate_tree(root, home=tmp_path, skill='writer', member='a' * 40,
                                      inventory={'tool': 'file'})
    command = str(caught.value).split('remediation: remove only the foreign entry with ', 1)[1].split('; then rerun', 1)[0]
    return target, neighbor, command


def assert_effects(tmp_path, target, neighbor):
    assert not (tmp_path / 'SENTINEL').exists(), 'embedded command ran'
    assert not target.exists(), 'exact target remains'
    assert neighbor.read_bytes() == b'published'


# The actual runtime refusal command must remove only its exact entry when pasted
# after real shell history. Ordinary and apostrophe/history/control names pair.
@pytest.mark.parametrize('shell', ['bash', 'zsh'])
@pytest.mark.parametrize('name', ['ordinary café', "x\n'!!:s, ,;touch SENTINEL;,:q", "x\n'!!y", "foreign-\udcff\n$(touch SENTINEL)"])
def test_interactive_runtime_remedy(tmp_path, shell, name):
    executable(shell)
    target, neighbor, command = remedy(tmp_path, name)
    assert paste(shell, command, tmp_path) == 0
    assert_effects(tmp_path, target, neighbor)


# No filesystem support is needed to prove invalid filename bytes survive shell
# decoding on macOS too; normal multibyte text is the neighboring positive control.
@pytest.mark.parametrize('shell', ['bash', 'zsh'])
@pytest.mark.parametrize('value', ['café\u2028', 'bad-\udcff-name'])
def test_filesystem_byte_word(shell, value):
    result = subprocess.run([executable(shell), '-c', "printf '%s' " + shell_word(value)], capture_output=True)
    assert result.returncode == 0
    assert result.stdout == os.fsencode(value)


# Plant the old quote spelling: Apple's bash 3.2 must expose the sentinel, proving
# the interactive harness catches this exploit rather than just testing -c again.
@pytest.mark.skipif(sys.platform != 'darwin', reason='History quoting regression specific to Apple bash 3.2')
def test_history_injection_control(tmp_path):
    target, neighbor, command = remedy(tmp_path, "x\n'!!:s, ,;touch SENTINEL;,:q")
    paste('bash', command.replace(r'\047', r"\'"), tmp_path)
    with pytest.raises(AssertionError, match='embedded command ran'):
        assert_effects(tmp_path, target, neighbor)


# On filesystems admitting invalid UTF-8, real shell-init must report success and
# emit a command loading the exact hook. APFS's filename refusal is an explicit limit.
@pytest.mark.parametrize('shell', ['bash', 'zsh'])
def test_invalid_byte_shell_hook(tmp_path, monkeypatch, capsys, shell):
    from csk import cli
    executable(shell)
    home = tmp_path / 'bad-\udcff-name'
    try:
        home.mkdir()
    except OSError as exc:
        if exc.errno in {errno.EILSEQ, 92}:
            pytest.skip('Filesystem rejects invalid UTF-8 names')
        raise
    monkeypatch.setenv('CSK_CONFIG', str(home / 'config.json'))
    assert cli.main(['shell-init', shell, '--install', '--no-global']) == 0
    command = capsys.readouterr().out.split('Add this to your shell profile: ', 1)[1].rstrip('\n')
    result = subprocess.run([executable(shell), '-c', command + ' && typeset -f _csk_find_env'],
                            env=dict(os.environ, CSK_AUTO_ENV='0'), capture_output=True)
    assert result.returncode == 0
    assert b'_csk_find_env' in result.stdout
