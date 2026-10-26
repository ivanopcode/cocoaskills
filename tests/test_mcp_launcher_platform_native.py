"""Native Windows/Linux controls: no app or helper is ever executed."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from csk import mcp_launchers as gate


# Capture before stable_env replaces the profile with a nonexistent test home.
# The native shell API expands the registered LocalAppData path using it.
_NATIVE_USERPROFILE = os.environ.get('USERPROFILE')


@pytest.fixture
def native_windows_profile(stable_env, monkeypatch):
    assert _NATIVE_USERPROFILE, 'native Windows control requires a real profile'
    monkeypatch.setenv('USERPROFILE', _NATIVE_USERPROFILE)


# Matching Windows hosts execute the real allocation/free adapter for every OS
# folder. Portable fake-DLL tests cover errors; this is a native success control.
@pytest.mark.csk_e2e_native
@pytest.mark.skipif(sys.platform != 'win32', reason='native Windows known folders')
def test_native_windows_known_folders(native_windows_profile):
    for key in gate._WINDOWS_ENV:
        path = Path(gate.windows_known_folder(key))
        assert path.is_absolute() and path.is_dir()


# Real Authenticode accepts an embedded-signed Microsoft binary and rejects a byte-flip
# copy. The call-only root override keeps all fixture writes under pytest's root.
@pytest.mark.csk_e2e_native
@pytest.mark.skipif(sys.platform != 'win32', reason='native Windows Authenticode')
def test_native_windows_signature_and_tamper(tmp_path):
    from csk.mcp_authenticode import _has_certificate

    # Windows PowerShell can be catalog-only signed. PowerShell 7 carries the
    # embedded certificate required by this adapter, including after copying.
    executable = shutil.which('pwsh')
    assert executable, 'native positive control requires PowerShell 7'
    source = Path(executable)
    assert _has_certificate(source), 'positive control requires an embedded signature'
    identity = gate.verify_windows(source)
    assert identity.signature == 'valid' and identity.publisher in {'Microsoft Windows', 'Microsoft Corporation'}
    target = tmp_path / 'Example/server.exe'
    target.parent.mkdir()
    shutil.copyfile(source, target)
    spec = gate.parse_launcher({'windows': {'install_dir': r'%ProgramFiles%\Example',
        'executable': 'server.exe', 'publisher': identity.publisher, 'trust': 'allow-unverified'}})
    original = gate.resolve_launcher(spec, platform='windows', roots=(tmp_path,))
    assert original.trust == 'verified'
    with target.open('r+b') as stream:
        stream.seek(4096)
        value = stream.read(1)
        assert value
        stream.seek(4096)
        stream.write(bytes([value[0] ^ 1]))
    with pytest.raises(gate.McpLauncherError) as caught:
        gate.resolve_launcher(spec, platform='windows', roots=(tmp_path,))
    assert caught.value.code == 'mcp_launcher_untrusted'
    shutil.copyfile(source, target)
    assert gate.resolve_launcher(spec, platform='windows', roots=(tmp_path,)) == original


# Native kernel ownership/modes and the actual system package DB verify a
# packaged binary; a private-home user file requires opt-in and a writable
# parent refuses even with opt-in, then passes once repaired.
@pytest.mark.csk_e2e_native
@pytest.mark.skipif(sys.platform != 'linux', reason='native Linux ownership/package DB')
def test_native_linux_package_user_and_unsafe_parent(tmp_path, monkeypatch):
    packaged = Path('/usr/bin/dpkg') if Path('/usr/bin/dpkg').is_file() else Path('/usr/bin/rpm')
    assert packaged.is_file(), 'native positive control requires dpkg or rpm'
    spec = gate.parse_launcher({'linux': {'path': str(packaged)}})
    assert gate.resolve_launcher(spec, platform='linux').trust == 'verified'
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    file = tmp_path / '.local/bin/server'
    file.parent.mkdir(parents=True)
    (tmp_path / '.local').chmod(0o755)
    file.parent.chmod(0o755)
    file.write_bytes(b'#!/bin/sh\nexit 0\n')
    file.chmod(0o755)
    declaration = {'path': '~/.local/bin/server'}
    with pytest.raises(gate.McpLauncherError) as caught:
        gate.resolve_launcher(gate.parse_launcher({'linux': declaration}), platform='linux')
    assert caught.value.code == 'mcp_launcher_untrusted'
    declaration['trust'] = 'allow-unverified'
    allowed = gate.parse_launcher({'linux': declaration})
    assert gate.resolve_launcher(allowed, platform='linux').trust == 'unverified'
    file.parent.chmod(0o777)
    with pytest.raises(gate.McpLauncherError) as caught: gate.resolve_launcher(allowed, platform='linux')
    assert caught.value.code == 'mcp_launcher_path_invalid'
    file.parent.chmod(0o755)
    assert gate.resolve_launcher(allowed, platform='linux').trust == 'unverified'


# Real umask 002 makes ~/.local/bin group-writable. Strict admission must name
# that directory and its remedy; removing group write admits the same helper.
# Ownership, modes and package lookup remain native, without group/NSS fixtures.
@pytest.mark.csk_e2e_native
@pytest.mark.skipif(sys.platform != 'linux', reason='native Linux umask 002')
def test_native_linux_umask_002_strict_modes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    file = tmp_path / '.local/bin/server'
    previous = os.umask(0o002)
    try:
        file.parent.mkdir(parents=True)
        file.write_bytes(b'#!/bin/sh\nexit 0\n')
    finally:
        os.umask(previous)
    file.chmod(0o755)  # Ordinary executable; isolate the directory's umask mode.
    assert file.parent.stat().st_mode & 0o777 == 0o775
    raw = {'linux': {'path': '~/.local/bin/server', 'trust': 'allow-unverified'}}
    declaration = gate.parse_launcher(raw)
    with pytest.raises(gate.McpLauncherError) as caught:
        gate.resolve_launcher(declaration, platform='linux')
    import shlex
    assert caught.value.code == 'mcp_launcher_path_invalid'
    assert str(file.parent) in str(caught.value) and '0775' in str(caught.value)
    assert shlex.split(str(caught.value).split('run ', 1)[1]) == ['chmod', '--', 'g-w', str(file.parent)]
    file.parent.chmod(file.parent.stat().st_mode & ~0o020)
    assert gate.resolve_launcher(declaration, platform='linux').trust == 'unverified'
