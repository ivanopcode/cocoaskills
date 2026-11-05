"""Captured-byte fixtures prove parser behavior, never loader agreement."""
import struct
import pytest
from csk.builds import _elf_identity as elf, _linux_identity as linux, go_v1


def image(*, needed=(), symbol=True, soname=None, search=None, machine=62, interpreter=True, kind=2):
    data = bytearray(4096)
    data[:16] = b'\x7fELF\x02\x01\x01' + bytes(9)
    struct.pack_into('<HHIQQQIHHHHHH', data, 16, kind, machine, 1, 0, 64, 0, 0, 64, 56, 3 if interpreter else 2, 0, 0, 0)
    struct.pack_into('<IIQQQQQQ', data, 64, 1, 5, 0, 0x400000, 0, 4096, 4096, 4096)
    strings = bytearray(b'\0')
    def name(text):
        off = len(strings)
        strings.extend(text.encode()+b'\0')
        return off
    tags = [(1, name(n)) for n in needed]
    if soname:
        tags.append((14, name(soname)))
    if search is not None:
        tags.append((29, name(search)))
    symname = name('Py_GetVersion')
    tags += [(5, 0x400400), (10, len(strings)), (6, 0x400800), (11, 24), (4, 0x400900), (0, 0)]
    struct.pack_into('<IIQQQQQQ', data, 120, 2, 4, 256, 0x400100, 0, len(tags)*16, len(tags)*16, 8)
    for i, pair in enumerate(tags):
        struct.pack_into('<qQ', data, 256+i*16, *pair)
    if interpreter:
        text = linux.LOADERS[machine].encode()+b'\0'
        struct.pack_into('<IIQQQQQQ', data, 176, 3, 4, 768, 0x400300, 0, len(text), len(text), 1)
        data[768:768+len(text)] = text
    data[1024:1024+len(strings)] = strings
    struct.pack_into('<IBBHQQ', data, 2072, symname, 0x12, 0, 1 if symbol else 0, 0x400a00, 16)
    struct.pack_into('<IIIII', data, 2304, 1, 2, 1, 0, 0)
    return bytes(data)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o755)
    return path


@pytest.fixture
def layout(tmp_path, monkeypatch):
    loader = write(tmp_path/'loader', image(symbol=False, interpreter=False, kind=3))
    original = linux.capture
    def capture(path, **kwargs):
        return original(loader if str(path) in linux.LOADERS.values() else path, **kwargs)
    monkeypatch.setattr(linux, 'capture', capture)
    executable = write(tmp_path/'prefix/bin/python', image())
    def resolve():
        return linux.resolve_linux_identity(executable, version='3.14', cache_path=tmp_path/'cache', preload_path=tmp_path/'preload')
    return executable, resolve


# Embedded/PIE/stripped fixtures select the executable beside an irrelevant DSO.
@pytest.mark.parametrize('machine', [62, 183])
@pytest.mark.parametrize('kind', [2, 3])
def test_embedded(layout, machine, kind):
    executable, resolve = layout
    write(executable, image(machine=machine, kind=kind))
    write(executable.parents[2]/'loader', image(machine=machine, symbol=False, interpreter=False, kind=3))
    write(executable.parents[1]/'lib/libpython3.14.so.1.0', image(interpreter=False, kind=3))
    result = resolve()
    assert result.process.identity.path == result.runtime.identity.path == executable
    result.verify()


# Origin and distribution cache fixtures bind the exact canonical SONAME target.
@pytest.mark.parametrize('origin', [True, False])
@pytest.mark.parametrize('symlink', [True, False])
def test_shared(layout, tmp_path, origin, symlink):
    executable, resolve = layout
    name = 'libpython3.14.so.1.0'
    write(executable, image(needed=[name], symbol=False, search='$ORIGIN/../lib' if origin else None))
    path = executable.parents[1]/'lib'/name if origin else tmp_path/'distribution'/name
    target = path.with_suffix('.real') if symlink else path
    write(target, image(soname=name, interpreter=False, kind=3))
    if symlink:
        path.symlink_to(target.name)
    if not origin:
        strings = name.encode()+b'\0'+str(path).encode()+b'\0'
        data = bytearray(72)+strings
        data[:20] = b'glibc-ld.so.cache1.1'
        struct.pack_into('<II', data, 20, 1, len(strings))
        struct.pack_into('<iIIIQ', data, 48, 0x303, 72, 72+len(name)+1, 0, 0)
        write(tmp_path/'cache', data)
    result = resolve()
    assert result.runtime.identity.path == target
    assert bool(result.runtime.links) == symlink
    result.verify()


# Every malformed byte plant has a valid control and the precise diagnostic.
@pytest.mark.parametrize('plant', ['truncated', 'overflow', 'class', 'endian', 'machine', 'unmapped', 'unterminated'])
def test_parser_refuses(plant):
    assert elf.parse_elf(image()).defines_python
    data = bytearray(image())
    if plant == 'truncated':
        data = data[:80]
    elif plant == 'overflow':
        struct.pack_into('<Q', data, 32, 2**64-1)
    elif plant == 'class':
        data[4] = 1
    elif plant == 'endian':
        data[5] = 2
    elif plant == 'machine':
        struct.pack_into('<H', data, 18, 40)
    elif plant == 'unmapped':
        struct.pack_into('<Q', data, 264, 0xfffffff)
    else:
        data[1024:1040] = b'x'*16
    with pytest.raises(go_v1.GoV1Error) as error:
        elf.parse_elf(bytes(data))
    assert error.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


# Valid control precedes each identity plant; verification is a resnapshot, not
# evidence of transient mutation detection by a retained watch.
@pytest.mark.parametrize('plant', ['duplicate', 'escape', 'ambiguous', 'forged', 'abi', 'preload', 'cache'])
def test_resolver_refuses(layout, tmp_path, plant):
    executable, resolve = layout
    prior = resolve()
    if plant == 'duplicate':
        write(executable, image(needed=['libpython3.14.so.1.0']*2))
    elif plant == 'escape':
        write(executable, image(search='$ORIGIN/../../outside'))
    elif plant == 'ambiguous':
        write(executable, image(search='$ORIGIN/../lib:$ORIGIN/../lib'))
    elif plant == 'forged':
        write(executable, image(symbol=False))
    elif plant == 'abi':
        write(executable, image(needed=['libpython3.14t.so.1.0'], symbol=False))
    elif plant == 'preload':
        write(tmp_path/'preload', b'/unbound/library.so\n')
    else:
        write(tmp_path/'cache', b'invalid cache')
    with pytest.raises(go_v1.GoV1Error) as error:
        prior.verify()
    assert error.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


# Popen covers worker/Go spawning; both admission gates stop before identity.
def test_admission_is_disconnected(monkeypatch, tmp_path):
    import subprocess
    from csk import installer
    def forbidden(*args, **kwargs):
        pytest.fail('identity or process reached from Linux admission')
    monkeypatch.setattr(go_v1.sys, 'platform', 'linux')
    monkeypatch.setattr(linux, 'resolve_linux_identity', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    assert not installer.supports_external_builds()
    with pytest.raises(go_v1.GoV1Error) as error:
        go_v1.inventory_platform()
    assert error.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
    assert list(tmp_path.iterdir()) == []
