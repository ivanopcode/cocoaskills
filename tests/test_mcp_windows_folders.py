"""Known-folder identity, environment conflict and native allocation contracts."""
import ctypes
import uuid
from pathlib import PureWindowsPath
from types import SimpleNamespace

import pytest

from csk import mcp_launchers as gate
from csk import mcp_windows_folders as folders


ROOTS = {'%PROGRAMFILES%': r'D:\Applications',
         '%PROGRAMFILES(X86)%': r'D:\Applications (x86)',
         '%LOCALAPPDATA%': r'C:\Users\User\AppData\Local'}


@pytest.fixture
def windows_fs(tmp_path, monkeypatch):
    class FixturePath(PureWindowsPath):
        def local(self): return tmp_path.joinpath(self.drive[0], *self.parts[1:])
        def __fspath__(self): return str(self.local())
        def resolve(self, strict=False):
            if strict: self.local().stat()
            return self
        def stat(self): return self.local().stat()
        def is_dir(self): return self.local().is_dir()
    for root in ROOTS.values():
        file = FixturePath(root) / 'Example/server.exe'
        file.local().parent.mkdir(parents=True)
        file.local().write_bytes(b'signed fixture')
    monkeypatch.setattr(gate, 'Path', FixturePath)
    monkeypatch.setattr(gate, 'windows_known_folder', lambda key: ROOTS[key])
    for key, value in ROOTS.items(): monkeypatch.setenv(gate._WINDOWS_ENV[key], value)
    return FixturePath


def resolve(key):
    spec = gate.parse_launcher({'windows': {'install_dir': key + r'\Example',
        'executable': 'server.exe', 'publisher': 'Example Inc'}})
    return gate.resolve_launcher(spec, platform='windows',
        verifier=lambda *a: gate.WindowsVerification('valid', 'Example Inc'))


# All three environment variables must agree with OS identity; genuine relocated
# machine folders and the ordinary per-user folder remain positive controls.
@pytest.mark.parametrize('key', ROOTS)
def test_known_folder_environment_conflict(windows_fs, monkeypatch, key):
    expected = 'signed-user-writable' if key == '%LOCALAPPDATA%' else 'verified'
    assert resolve(key).trust == expected
    monkeypatch.setenv(gate._WINDOWS_ENV[key], r'C:\Users\User\UntrustedRoot')
    with pytest.raises(gate.McpLauncherError) as caught: resolve(key)
    assert caught.value.code == 'mcp_launcher_root_conflict'


# Failure of any required OS identity refuses even with plausible env values;
# absent env values use the OS result, never a guessed drive or default path.
@pytest.mark.parametrize('key', ROOTS)
def test_known_folder_failure_and_absent_environment(windows_fs, monkeypatch, key):
    monkeypatch.delenv(gate._WINDOWS_ENV[key])
    assert resolve(key).trust in {'verified', 'signed-user-writable'}
    def broken(k):
        if k == key: raise OSError('known folder unavailable')
        return ROOTS[k]
    monkeypatch.setattr(gate, 'windows_known_folder', broken)
    with pytest.raises(gate.McpLauncherError) as caught: resolve(key)
    assert caught.value.code == 'mcp_launcher_known_folder_unavailable'


# A machine folder relocated inside LocalAppData still has user-writable trust;
# the declared placeholder cannot promote its actual containment to verified.
def test_containment_controls_trust(windows_fs, monkeypatch):
    nested = ROOTS['%LOCALAPPDATA%'] + r'\RelocatedApps'
    file = windows_fs(nested) / 'Example/server.exe'
    file.local().parent.mkdir(parents=True)
    file.local().write_bytes(b'signed fixture')
    monkeypatch.setattr(gate, 'windows_known_folder',
        lambda key: nested if key == '%PROGRAMFILES%' else ROOTS[key])
    monkeypatch.setenv('ProgramFiles', nested)
    assert resolve('%PROGRAMFILES%').trust == 'signed-user-writable'


# Equivalent DOS spelling does not create a conflict; moving only the env
# value to a different directory does (covered above for every folder).
def test_equivalent_environment_spelling(windows_fs, monkeypatch):
    monkeypatch.setenv('ProgramFiles', 'd:/applications')
    assert resolve('%PROGRAMFILES%').trust == 'verified'


# A missing system DLL cannot fall back to an environment-based location.
def test_known_folder_dll_failure(windows_fs, monkeypatch):
    def broken(*args, **kwargs): raise OSError('system DLL unavailable')
    monkeypatch.setattr(ctypes, 'WinDLL', broken, raising=False)
    monkeypatch.setattr(gate, 'windows_known_folder', folders.windows_known_folder)
    with pytest.raises(gate.McpLauncherError) as caught: resolve('%PROGRAMFILES%')
    assert caught.value.code == 'mcp_launcher_known_folder_unavailable'


# The real ctypes adapter uses exact folder GUIDs, current-user/default flags,
# system DLL loading, zero-only success and frees any returned allocation even
# on failure. Fake DLLs establish the contract, not native Windows execution.
@pytest.mark.parametrize('key,guid', [
    ('%PROGRAMFILES%', '905e63b6-c1bf-494e-b29c-65b732d3d21a'),
    ('%PROGRAMFILES(X86)%', '7c5a40ef-a0fb-4bfc-874a-c0f2e0b9fa8e'),
    ('%LOCALAPPDATA%', 'f1b32785-6fba-4fcf-9d55-7b8e7f157091')])
@pytest.mark.parametrize('result,allocated', [(0, True), (-2147467259, True), (1, True), (0, False)])
def test_known_folder_binding(monkeypatch, key, guid, result, allocated):
    buffer = ctypes.create_unicode_buffer(ROOTS[key])
    freed = []
    calls = []
    def get(folder, flags, token, output):
        assert ctypes.string_at(folder, 16) == uuid.UUID(guid).bytes_le
        assert flags == 0 and token is None
        if allocated:
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
        return result
    def free(pointer): freed.append(pointer.value)
    class Function:
        def __init__(self, call): self.call = call
        def __call__(self, *args): return self.call(*args)
    def dll(name, **kwargs):
        calls.append((name, kwargs))
        return SimpleNamespace(SHGetKnownFolderPath=Function(get), CoTaskMemFree=Function(free))
    monkeypatch.setattr(ctypes, 'WinDLL', dll, raising=False)
    if result == 0 and allocated:
        assert folders.windows_known_folder(key) == ROOTS[key]
    else:
        with pytest.raises(OSError): folders.windows_known_folder(key)
    assert freed == ([ctypes.addressof(buffer)] if allocated else [])
    assert calls == [('shell32.dll', {'winmode': 0x800}), ('ole32.dll', {'winmode': 0x800})]
