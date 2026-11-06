import sys
import shutil
import json
import os
import subprocess
from pathlib import Path

import pytest



# Run the exact CLI-emitted source command in a fresh shell. --no-global and
# CSK_AUTO_ENV=0 prevent sourcing any project/global user state. An ordinary
# pathname must succeed first; every control/joiner case must load the same hook.
@pytest.mark.parametrize('shell', ['bash', 'zsh'])
@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX shell remedies use POSIX paths; Windows does not execute this shell contract')
def test_shell_init_copyable_remedy(tmp_path, monkeypatch, capsys, shell):
    executable = '/bin/bash' if shell == 'bash' and sys.platform == 'darwin' else shutil.which(shell)
    if executable is None:
        pytest.skip(f'{shell} is unavailable; other shell cases still run')
    from csk import cli
    failures = []
    for index, name in enumerate(['ordinary café', 'home-\nFORGED', 'home-\u202eFORGED\u202c', 'home-می\u200cروم', "home-' $(touch SENTINEL) ; `touch SENTINEL`"]):
        home = tmp_path / name
        monkeypatch.setenv('CSK_CONFIG', str(home / 'config.json'))
        assert cli.main(['shell-init', shell, '--install', '--no-global']) == 0
        output = capsys.readouterr().out
        command = output.split('Add this to your shell profile: ', 1)[1].rstrip('\n')
        from csk.terminal import terminal_document
        assert terminal_document(command) == command
        assert '\n' not in command
        assert (home / 'hooks' / ('csk.' + shell)).is_file()
        env = dict(os.environ, CSK_AUTO_ENV='0')
        proc = subprocess.run([executable, '-c', command + ' && typeset -f _csk_find_env'], cwd=tmp_path, env=env, capture_output=True, text=True)
        record = {'shell': shell, 'name': name, 'emitted_command': command, 'exit': proc.returncode, 'stderr': proc.stderr}
        assert not (tmp_path / 'SENTINEL').exists()
        if index == 0:
            assert proc.returncode == 0 and '_csk_find_env' in proc.stdout, record
        if proc.returncode != 0 or '_csk_find_env' not in proc.stdout:
            failures.append(record)
    assert not failures, failures
