"""Captured-byte fixtures prove parser behavior, never loader agreement."""
import hashlib
import os
import struct
import sys
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
    if os.name == "nt":
        pytest.skip("POSIX Linux filesystem fixture; byte parser tests remain portable")
    loader = write(tmp_path/'loader', image(symbol=False, interpreter=False, kind=3))
    original = linux.capture
    def capture(path, **kwargs):
        return original(loader if str(path) in linux.LOADERS.values() else path, **kwargs)
    monkeypatch.setattr(linux, 'capture', capture)
    executable = write(tmp_path/'prefix/bin/python3.14', image())
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
@pytest.mark.skipif(os.name == "nt", reason="Linux admission simulation requires POSIX")
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


# GNU-only indexing accepts a defined symbol; an unterminated chain cannot
# overrun the captured image or satisfy the parser by reaching unrelated bytes.
def test_gnu_hash_bounds():
    data = gnu_image(image())
    assert elf.parse_elf(bytes(data)).defines_python
    data[2396:] = bytes(len(data)-2396)
    with pytest.raises(go_v1.GoV1Error) as error:
        elf.parse_elf(bytes(data))
    assert error.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


# A structurally valid cache change still invalidates a captured identity.
def test_loader_configuration_mutation(layout, tmp_path):
    _, resolve = layout
    before = resolve()
    write(tmp_path/'preload', b'')
    with pytest.raises(go_v1.GoV1Error) as error:
        before.verify()
    assert error.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    resolve().verify()


# Native-control admission and interpreter dispatch remain closed even when a
# synthetic resolver could succeed. No native probe or worker/Go subprocess runs.
def test_native_dispatch_stays_closed(layout, monkeypatch):
    import subprocess
    executable, resolve = layout
    identity = resolve()
    calls = []
    monkeypatch.setattr(go_v1.sys, 'platform', 'linux')
    monkeypatch.setattr(linux, 'resolve_linux_identity', lambda *a, **k: calls.append('resolver') or identity)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('worker/Go started'))
    with pytest.raises(go_v1.GoV1Error) as error:
        go_v1.probe_native_controls(go_v1.ResourceLimits(), _native_probe=lambda *a: calls.append('probe') or True)
    assert error.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
    with pytest.raises(go_v1.GoV1Error) as error:
        go_v1._resolve_interpreter_runtime(go_v1._resolve_executable_identity(executable))
    assert error.value.code == go_v1.CODE_CONTROL_UNAVAILABLE
    assert calls == []


# A string table can cross adjacent LOAD segments with identical file mapping
# (a native relocated-table layout); a virtual hole still rejects.
def test_adjacent_load_string_table():
    data = bytearray(image())
    struct.pack_into('<H', data, 56, 4)
    struct.pack_into('<Q', data, 64+32, 1030)
    struct.pack_into('<Q', data, 64+40, 1030)
    struct.pack_into('<IIQQQQQQ', data, 232, 1, 5, 1030, 0x400406, 0, 4096-1030, 4096-1030, 1)
    # Move dynamic entries because the additional program header overlaps them.
    data[512:608] = image()[256:352]
    struct.pack_into('<Q', data, 120+8, 512)
    struct.pack_into('<Q', data, 120+16, 0x400200)
    assert elf.parse_elf(bytes(data)).defines_python
    struct.pack_into('<Q', data, 232+16, 0x400407)
    with pytest.raises(go_v1.GoV1Error) as error:
        elf.parse_elf(bytes(data))
    assert error.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


# Loader-altering tags cannot hide an unmodeled Python provider. BIND_NOW is
# the nearby allowed control; this does not test all glibc dynamic tags.
@pytest.mark.parametrize('tag,value', [(0x7fffffff, 1), (0x6ffffefc, 1), (0x7ffffffe, 1), (0x70000002, 1), (30, 2), (0x6ffffffb, 0x800)])
def test_loader_scope_flags(tag, value):
    data = bytearray(image())
    struct.pack_into('<Q', data, 120+32, 112)
    struct.pack_into('<Q', data, 120+40, 112)
    struct.pack_into('<qQ', data, 336, 30, 8)
    struct.pack_into('<qQ', data, 352, 0, 0)
    assert elf.parse_elf(bytes(data)).defines_python
    struct.pack_into('<qQ', data, 336, tag, value)
    with pytest.raises(go_v1.GoV1Error) as error:
        elf.parse_elf(bytes(data))
    assert error.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


# Match the existing interpreter resolver's sixteen-step walk budget: a
# fifteen-link control resolves; adding the sixteenth link refuses.
def test_link_walk_budget(layout, tmp_path):
    executable, _ = layout
    path = executable
    for i in range(15):
        link = tmp_path / f'link-{i}'
        link.symlink_to(path)
        path = link
    assert linux.capture(path).identity.path == executable
    extra = tmp_path/'extra-link'
    extra.symlink_to(path)
    with pytest.raises(go_v1.GoV1Error) as error:
        linux.capture(extra)
    assert error.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


# Absolute/common/reserved symbols are not loaded definitions in the image,
# even when their forged value happens to fall inside a LOAD segment.
@pytest.mark.parametrize('section', [0xff00, 0xfff1, 0xfff2, 0xffff])
def test_special_python_symbol_is_not_embedding(section):
    data = bytearray(image())
    assert elf.parse_elf(bytes(data)).defines_python
    struct.pack_into('<H', data, 2072+6, section)
    with pytest.raises(go_v1.GoV1Error) as error:
        elf.parse_elf(bytes(data))
    assert error.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


def dynamic_tag(data, tag, value):
    data = bytearray(data)
    for pos in range(256, 768, 16):
        current, = struct.unpack_from('<q', data, pos)
        if current in (0, tag):
            struct.pack_into('<qQ', data, pos, tag, value)
            if current == 0:
                struct.pack_into('<qQ', data, pos + 16, 0, 0)
                for field in (152, 160):
                    struct.pack_into('<Q', data, field, pos + 32 - 256)
            return data
    raise AssertionError('fixture dynamic table exhausted')


def gnu_image(data, *, both=False):
    data = bytearray(data)
    if not both:
        for pos in range(256, 768, 16):
            if struct.unpack_from('<q', data, pos)[0] == 4:
                struct.pack_into('<qQ', data, pos, 0x6ffffef5, 0x400940)
                break
    else:
        data = dynamic_tag(data, 0x6ffffef5, 0x400940)
    hashed = 5381
    for byte in b'Py_GetVersion':
        hashed = (hashed * 33 + byte) & 0xffffffff
    bloom = (1 << (hashed % 64)) | (1 << ((hashed >> 5) % 64))
    struct.pack_into('<IIIIQII', data, 2368, 1, 1, 1, 5, bloom, 1, hashed | 1)
    return data


# Single-hash lookup must honor buckets, chains, bloom and hash values, not
# merely scan dynsym. Captured bytes do not prove native loader agreement.
@pytest.mark.parametrize('plant', ['sysv-count', 'sysv-bucket', 'sysv-cycle',
                                  'gnu-bloom', 'gnu-bucket', 'gnu-hash', 'gnu-shift'])
def test_symbol_lookup_reachability(plant):
    data = gnu_image(image()) if plant.startswith('gnu') else bytearray(image())
    assert elf.parse_elf(bytes(data)).defines_python
    offset, value = {'sysv-count': (2308, 1), 'sysv-bucket': (2312, 0),
                     'sysv-cycle': (2320, 1), 'gnu-bloom': (2384, 0),
                     'gnu-bucket': (2392, 0), 'gnu-hash': (2396, 1),
                     'gnu-shift': (2380, 32)}[plant]
    struct.pack_into('<Q' if plant == 'gnu-bloom' else '<I', data, offset, value)
    if plant == 'gnu-bucket':
        # An empty GNU table exports nothing; bytes after its bound are irrelevant.
        assert not elf.parse_elf(bytes(data)).defines_python
    else:
        with pytest.raises(go_v1.GoV1Error) as error:
            elf.parse_elf(bytes(data))
        assert error.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


# Reproduce the review's dependency: SysV nchain=1 conceals a GNU-visible
# Py_GetVersion interposer. The intact provider is identified and rejected by
# the resolver; corrupt tables refuse in the parser. An ordinary dependency
# resolves the exact intended runtime; bytes alone do not prove loader agreement.
@pytest.mark.parametrize('machine', [62, 183])
@pytest.mark.parametrize('plant', ['unpatched', 'short-sysv', 'empty-gnu', 'empty-sysv', 'empty-bloom'])
def test_forged_dual_hash_dependency(layout, machine, plant):
    executable, resolve = layout
    write(executable.parents[2]/'loader', image(machine=machine, symbol=False, interpreter=False, kind=3))
    runtime = 'libpython3.14.so.1.0'
    write(executable, image(machine=machine, symbol=False, needed=['libevil.so.1', runtime], search='$ORIGIN/../lib'))
    lib = executable.parents[1]/'lib'
    expected = write(lib/runtime, gnu_image(image(machine=machine, soname=runtime, interpreter=False, kind=3)))
    provider = lib/'libevil.so.1'
    write(provider, gnu_image(image(machine=machine, symbol=False, soname=provider.name, interpreter=False, kind=3)))
    assert resolve().runtime.identity.path == expected
    forged = gnu_image(image(machine=machine, soname=provider.name, interpreter=False, kind=3), both=True)
    assert elf.parse_elf(bytes(forged)).defines_python
    if plant == 'short-sysv':
        struct.pack_into('<I', forged, 2308, 1)
    elif plant == 'empty-gnu':
        struct.pack_into('<I', forged, 2392, 0)
    elif plant == 'empty-sysv':
        struct.pack_into('<I', forged, 2312, 0)
    elif plant == 'empty-bloom':
        struct.pack_into('<Q', forged, 2384, 0)
    write(provider, forged)
    with pytest.raises(go_v1.GoV1Error) as error:
        resolve()
    assert error.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


def cache_fixture(entries, *, name='libpython3.14.so.1.0', path='/lib/libpython3.14.so.1.0'):
    # Shared offsets deliberately stress expansion rather than input size.
    strings = name.encode() + b'\0' + path.encode() + b'\0'
    end = 48 + 24 * len(entries)
    data = bytearray(end) + strings
    data[:20] = b'glibc-ld.so.cache1.1'
    struct.pack_into('<II', data, 20, len(entries), len(strings))
    for i, (flags, osversion, hwcap) in enumerate(entries):
        struct.pack_into('<iIIIQ', data, 48 + i*24, flags, end, end+len(name)+1, osversion, hwcap)
    return bytes(data)


# F2's shared 64 KiB strings and other structured amplification cases refuse
# before expansion. A supported cache paired with each plant must still parse.
@pytest.mark.parametrize('plant', ['review-oom', 'total-output', 'candidates', 'count', 'machine'])
def test_cache_expansion_bounds(plant):
    good = cache_fixture([(0x303, 0, 0)])
    assert linux.cache_entries(good) == {'libpython3.14.so.1.0': ('/lib/libpython3.14.so.1.0',)}
    machine = 62
    if plant == 'review-oom':
        data = cache_fixture([(0x303, 0, 0)] * 50000, name='x'*65536, path='/'+'y'*65535)
    elif plant == 'total-output':
        data = cache_fixture([(0x303, 0, 0)] * 2048, name='x'*4096, path='/'+'y'*4095)
    elif plant == 'candidates':
        data = cache_fixture([(0x303, 0, 0)] * 65)
    elif plant == 'count':
        data = bytearray(good)
        struct.pack_into('<I', data, 20, 0xffffffff)
    else:
        data, machine = good, 40
    with pytest.raises(go_v1.GoV1Error) as error:
        linux.cache_entries(data, machine)
    assert error.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


# Unknown foreign ABIs are ignored, like known foreign ABIs. Same-ABI hwcap/OS
# conditions remain a deliberate refusal at selection: their precedence is unmodeled.
@pytest.mark.parametrize('machine,flags', [(62, 0x303), (183, 0xa03)])
def test_cache_abi_and_conditional_entries(machine, flags):
    good = cache_fixture([(flags, 0, 0)])
    expected = linux.cache_entries(good, machine)
    assert expected == {'libpython3.14.so.1.0': ('/lib/libpython3.14.so.1.0',)}
    for foreign in (0x3, 0x803, 0xb03, 0x123403):
        assert linux.cache_entries(cache_fixture([(foreign, 0, 0), (flags, 0, 0)]), machine) == expected
    for osversion, hwcap in ((1, 0), (0, 1)):
        result = linux.cache_entries(cache_fixture([(flags, osversion, hwcap)]), machine)
        assert result == {'libpython3.14.so.1.0': ('!unsupported-cache-entry',)}


# Bounded deterministic fuzzing covers random bytes, valid mutations, extreme
# counts/offsets and repeated strings. It is a finite corpus, not all-input proof;
# every case must return a bounded mapping or the exact identity diagnostic.
def test_cache_bounded_fuzz():
    import random
    rng = random.Random(20261106)
    seed = cache_fixture([(0x303, 0, 0), (0xb03, 0, 0)])
    accepted = refused = 0
    for i in range(2000):
        data = bytearray(seed) if i % 3 else bytearray(rng.randbytes(rng.randrange(256)))
        if i % 3 == 1:
            for _ in range(rng.randrange(1, 6)):
                data[rng.randrange(len(data))] = rng.randrange(256)
        elif i % 3 == 2 and i % 5:
            struct.pack_into('<I', data, rng.choice([20, 24, 52, 56, 76, 80]), rng.choice([0, 1, 4096, 100000, 0xffffffff]))
        try:
            result = linux.cache_entries(bytes(data), rng.choice([62, 183]))
            assert sum(len(paths) for paths in result.values()) <= linux.MAX_CACHE_ENTRIES
            assert all(len(paths) <= linux.MAX_CACHE_CANDIDATES for paths in result.values())
            assert sum(len(name)+len(path) for name, paths in result.items() for path in paths) <= linux.MAX_CACHE_OUTPUT
            accepted += 1
        except go_v1.GoV1Error as error:
            assert error.code == go_v1.CODE_WORKER_IDENTITY_INVALID
            refused += 1
    assert accepted and refused


# A supported global export remains valid; version-qualified/hidden/local
# version entries refuse instead of guessing an unversioned loader match.
@pytest.mark.parametrize('version', [0, 2, 0x8001, 0x8002])
def test_versioned_python_export_refuses(version):
    data = dynamic_tag(image(), 0x6ffffff0, 0x400b00)
    struct.pack_into('<HH', data, 2816, 0, 1)
    assert elf.parse_elf(bytes(data)).defines_python
    struct.pack_into('<H', data, 2818, version)
    with pytest.raises(go_v1.GoV1Error) as error:
        elf.parse_elf(bytes(data))
    assert error.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


# An unknown foreign ABI cannot poison selection. Same-ABI conditional entries
# refuse with the identity diagnostic, paired with the same exact-image control.
@pytest.mark.parametrize('condition', [(1, 0), (0, 1)])
def test_cache_conditional_selection_refuses(layout, tmp_path, condition):
    executable, resolve = layout
    name = 'libpython3.14.so.1.0'
    write(executable, image(symbol=False, needed=[name]))
    runtime = write(tmp_path/'distribution'/name, image(soname=name, interpreter=False, kind=3))
    write(tmp_path/'cache', cache_fixture([(0xb03, 0, 0), (0x303, 0, 0)], path=str(runtime)))
    assert resolve().runtime.identity.path == runtime
    osversion, hwcap = condition
    write(tmp_path/'cache', cache_fixture([(0x303, osversion, hwcap), (0x303, 0, 0)], path=str(runtime)))
    with pytest.raises(go_v1.GoV1Error) as error:
        resolve()
    assert error.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


# Native AArch64 libraries carry this presence-only BTI marker. It is admitted
# only on that architecture with the supported zero value; other tags refuse.
@pytest.mark.parametrize('machine,tag,value', [(183, 0x70000000, 0), (183, 0x70000001, 1), (183, 0x70000002, 0)])
def test_aarch64_bti_tag_scope(machine, tag, value):
    assert elf.parse_elf(bytes(dynamic_tag(image(machine=183), 0x70000001, 0))).defines_python
    with pytest.raises(go_v1.GoV1Error) as error:
        elf.parse_elf(bytes(dynamic_tag(image(machine=machine), tag, value)))
    assert error.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


# Explicit relocation descriptors are accepted without interpreting relocations;
# the neighboring unknown tag still refuses. This is identity-only evidence.
@pytest.mark.parametrize('tag', [0x70000000, 0x70000001, 0x70000003, 0x6ffffef6, 0x6ffffef7])
def test_known_relocation_tags(tag):
    assert elf.parse_elf(bytes(dynamic_tag(image(), tag, 16))).defines_python
    with pytest.raises(go_v1.GoV1Error) as error:
        elf.parse_elf(bytes(dynamic_tag(image(), 0x70000002, 16)))
    assert error.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


def _profile(executable, tmp_path, **kwargs):
    # The fixture tree stands in for the distribution root /usr: seams
    # name its prefix and the fixture files' owner, while production
    # defaults keep /usr and uid 0.
    from pathlib import Path as _Path
    target = _Path(os.path.realpath(executable))
    kwargs.setdefault('host_machine', 'x86_64')
    kwargs.setdefault('_distribution_root', target.parents[1])
    kwargs.setdefault('_distribution_owner', target.stat().st_uid)
    return linux.resolve_linux_interpreter_identity(
        executable, cache_path=tmp_path / 'cache',
        preload_path=tmp_path / 'preload', **kwargs)


def _musl_image():
    data = bytearray(image())
    text = b'/lib/ld-musl-x86_64.so.1\0'
    data[768:768 + len(text)] = text
    struct.pack_into('<Q', data, 176 + 32, len(text))
    struct.pack_into('<Q', data, 176 + 40, len(text))
    return bytes(data)


# The U3 profile adapter selects the embedded runtime; a neighbouring
# libpython that does not own Py_GetVersion cannot override it.
def test_profile_embedded_neighbouring_non_owner(layout, tmp_path):
    executable, _ = layout
    write(executable.parents[1] / 'lib/libpython3.14.so.1.0',
          image(interpreter=False, kind=3, symbol=False))
    result = _profile(executable, tmp_path)
    runtime = result.interpreter.runtime
    assert runtime.process_image.path == executable
    assert runtime.runtime_image.path == executable
    assert runtime.base_executable.path == executable
    assert result.linux.loader.identity.path in result.watch_paths()
    result.verify()


# The U3 profile adapter selects a cache-shared runtime that owns the
# symbol; the same layout with a non-owning libpython refuses, and every
# retained image's links and their parents are watched.
def test_profile_shared_cache_and_non_owner(layout, tmp_path):
    executable, _ = layout
    name = 'libpython3.14.so.1.0'
    write(executable, image(needed=[name], symbol=False))
    runtime_target = write(
        tmp_path / 'distribution' / (name + '.1'),
        image(soname=name, interpreter=False, kind=3))
    # The SONAME link lives in a different directory from its target, so
    # only the retained-image link parents (not the target's own parent,
    # already watched by the base identity) can add it to watch_paths.
    runtime_path = tmp_path / 'links' / name
    runtime_path.parent.mkdir(parents=True)
    runtime_path.symlink_to('../distribution/' + runtime_target.name)
    write(tmp_path / 'cache',
          cache_fixture([(0x303, 0, 0)], path=str(runtime_path)))
    result = _profile(executable, tmp_path)
    assert result.interpreter.runtime.runtime_image.path == runtime_target
    assert result.interpreter.runtime.process_image.path == executable
    assert runtime_path.parent != runtime_target.parent
    assert runtime_path in result.watch_paths()
    assert runtime_path.parent in result.watch_paths()
    result.verify()
    write(runtime_target, image(soname=name, interpreter=False, kind=3, symbol=False))
    with pytest.raises(go_v1.GoV1Error) as error:
        _profile(executable, tmp_path)
    assert error.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID


# A package-venv invocation retains its pyvenv.cfg, resolves to the base
# executable and keeps python_home at the prefix; a home naming another
# directory refuses, with the direct invocation as positive control.
def test_profile_package_venv(layout, tmp_path):
    executable, _ = layout
    direct = _profile(executable, tmp_path)
    assert direct.interpreter.runtime.configuration is None
    assert direct.interpreter.runtime.python_home == executable.parents[1]
    venv_bin = tmp_path / 'venv' / 'bin'
    venv_bin.mkdir(parents=True)
    link = venv_bin / 'python'
    link.symlink_to(executable)
    config = tmp_path / 'venv' / 'pyvenv.cfg'
    config.write_text(f'home = {executable.parent}\n')
    result = _profile(link, tmp_path)
    assert result.interpreter.executable.path == executable
    assert result.interpreter.runtime.configuration is not None
    assert result.interpreter.runtime.configuration.path == config
    assert result.interpreter.runtime.python_home == executable.parents[1]
    assert result.interpreter.links
    result.verify()
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    config.write_text(f'home = {elsewhere}\n')
    with pytest.raises(go_v1.GoV1Error, match='does not name the base'):
        _profile(link, tmp_path)
    config.write_text(f'home = {executable.parent}\n')
    assert _profile(link, tmp_path).interpreter.runtime.python_home == (
        executable.parents[1])


# Each unsupported profile property refuses by name, with the supported
# embedded control resolving first in the same fixture. The pyenv,
# /usr/local and embedded-3.13 plants parse as ELF but are not the
# distribution interpreter; owner and writability plants use the seams.
@pytest.mark.parametrize('plant', [
    'version', 'machine', 'musl', 'standalone', 'pyenv', 'local',
    'embedded313', 'owner', 'writable',
])
def test_profile_refuses_unsupported(layout, tmp_path, plant):
    executable, resolve = layout
    assert _profile(executable, tmp_path).linux.version == '3.14'
    if plant == 'version':
        with pytest.raises(go_v1.GoV1Error, match='unsupported Python version 3.13'):
            _profile(executable, tmp_path, version='3.13')
    elif plant == 'machine':
        write(tmp_path / 'loader',
              image(machine=183, symbol=False, interpreter=False, kind=3))
        write(executable, image(machine=183))
        assert resolve().process.identity.path == executable
        with pytest.raises(go_v1.GoV1Error, match='unsupported ELF machine 183'):
            _profile(executable, tmp_path)
    elif plant == 'musl':
        write(executable, _musl_image())
        with pytest.raises(go_v1.GoV1Error, match='musl'):
            _profile(executable, tmp_path)
    elif plant == 'standalone':
        name = 'libpython3.14.so.1.0'
        write(executable, image(needed=[name], symbol=False,
                                search='$ORIGIN/../lib'))
        write(executable.parents[1] / 'lib' / name,
              image(soname=name, interpreter=False, kind=3))
        assert resolve().runtime.identity.path == executable.parents[1] / 'lib' / name
        with pytest.raises(go_v1.GoV1Error, match='standalone'):
            _profile(executable, tmp_path)
    elif plant in ('pyenv', 'local', 'embedded313'):
        other = {
            'pyenv': tmp_path / 'pyenv/versions/3.14.0/bin/python3.14',
            'local': tmp_path / 'usr-local/bin/python3.14',
            'embedded313': tmp_path / 'opt/python3.13/bin/python3.13',
        }[plant]
        write(other, image())
        with pytest.raises(go_v1.GoV1Error, match='non-distribution'):
            _profile(other, tmp_path,
                     _distribution_root=executable.parents[1])
    elif plant == 'owner':
        with pytest.raises(go_v1.GoV1Error, match='root-owned'):
            _profile(executable, tmp_path,
                     _distribution_owner=executable.stat().st_uid + 1)
    else:
        executable.chmod(0o775)
        with pytest.raises(go_v1.GoV1Error, match='writable by group or other'):
            _profile(executable, tmp_path)
        executable.chmod(0o755)
        assert _profile(executable, tmp_path).linux.version == '3.14'


_MAPS_ADDR = 0x400100


def _device_parts(device):
    # Hosts without os.major/os.minor (Windows) split the device with
    # the Linux encoding the parser rebuilds, so the round trip holds.
    if hasattr(os, 'major'):
        return os.major(device), os.minor(device)
    major = ((device >> 8) & 0xFFF) | ((device >> 32) & 0xFFFFF000)
    minor = (device & 0xFF) | ((device >> 12) & 0xFFFFFF00)
    return major, minor


def _maps_pathname(path):
    # Real maps pathnames are absolute POSIX paths on every host.
    text = path.as_posix()
    return text if text.startswith('/') else '/' + text


def _maps_line(path, *, start=0x400000, end=0x401000, inode=None, suffix=''):
    info = os.stat(path)
    major, minor = _device_parts(info.st_dev)
    return (
        f'{start:x}-{end:x} r-xp 00000000 '
        f'{major:02x}:{minor:02x} '
        f'{info.st_ino if inode is None else inode} '
        f'{_maps_pathname(path)}{suffix}\n'
    )


def _prove(retained, **kwargs):
    kwargs.setdefault('address', _MAPS_ADDR)
    kwargs.setdefault('version_info', (3, 14))
    return linux.prove_loaded_identities(retained, **kwargs)


# Maps parsing is a pure function on every host: exact fields parse,
# while malformed, empty, over-line-bound and over-byte-bound maps
# text refuse by name; the same holds through the bounded _proc_maps
# file reader, next to its accepted file control.
def test_parse_proc_maps(tmp_path):
    target = write(tmp_path / 'python3.14', image())
    entries = go_v1._parse_proc_maps(_maps_line(target))
    info = os.stat(target)
    assert entries[0].device == info.st_dev
    assert entries[0].inode == info.st_ino
    assert entries[0].pathname == _maps_pathname(target)
    # One otherwise valid line that is over the 1 MiB byte bound: only
    # the byte bound can refuse it (line count and syntax are fine).
    over_bytes = (
        '00400000-00401000 r-xp 00000000 08:01 1 /'
        + 'a' * (1024 * 1024) + '\n'
    )
    assert len(over_bytes.encode()) > go_v1._MAX_PROC_MAPS_BYTES
    assert len(over_bytes.splitlines()) == 1
    for text, match in [
        ('garbage\n', 'cannot parse worker process maps'),
        ('zz-zz r-xp 0 08:01 1 /x\n', 'cannot parse worker process maps'),
        ('', 'empty or exceed'),
        (_maps_line(target) * 4097, 'empty or exceed'),
        (over_bytes, 'exceed their size bound'),
    ]:
        with pytest.raises(go_v1.GoV1Error, match=match) as error:
            go_v1._parse_proc_maps(text)
        assert error.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    assert go_v1._parse_proc_maps(_maps_line(target))[0].inode == info.st_ino
    # File-reader path: an accepted maps file is the positive control;
    # a missing maps file and an over-bound maps file refuse by name.
    proc_exe = tmp_path / 'proc-exe'
    proc_exe.symlink_to(target)
    maps_file = write(tmp_path / 'maps', _maps_line(target).encode())
    seam = dict(_proc_exe=proc_exe, _address=_MAPS_ADDR,
                _version_info=(3, 14))
    loaded = go_v1._linux_loaded_identities(
        _proc_maps=maps_file, **seam)
    assert loaded[0].path == type(proc_exe)(os.readlink(proc_exe))
    assert os.path.samefile(loaded[0].path, target)
    assert (loaded[0].device, loaded[0].inode) == (info.st_dev, info.st_ino)
    assert loaded[1] == loaded[0]
    with pytest.raises(go_v1.GoV1Error, match='procfs is missing'):
        go_v1._linux_loaded_identities(
            _proc_maps=tmp_path / 'missing-maps', **seam)
    over_file = write(tmp_path / 'maps-over', over_bytes.encode())
    with pytest.raises(go_v1.GoV1Error, match='exceed their size bound'):
        go_v1._linux_loaded_identities(_proc_maps=over_file, **seam)
    # Positive control still passes after every refusal plant.
    assert go_v1._linux_loaded_identities(
        _proc_maps=maps_file, **seam)[0].inode == info.st_ino


# Loaded-identity proof takes both owners from kernel data (maps inode
# and /proc/self/exe through procfs); deleted, replaced-then-restored,
# anonymous, ambiguous, unparsable and wrong-version owners refuse by
# name, next to the exact retained positive control.
def test_prove_loaded_identities(layout, tmp_path):
    executable, resolve = layout
    retained = resolve()
    proc_exe = tmp_path / 'proc-exe'
    proc_exe.symlink_to(executable)
    good_maps = _maps_line(executable)
    loaded = _prove(retained, proc_exe=proc_exe, maps_text=good_maps)
    assert loaded[0] == retained.process.identity
    assert loaded[1] == retained.runtime.identity
    other = write(tmp_path / 'other-python', image())
    libpython = write(tmp_path / 'libpython3.14.so.1.0',
                      image(interpreter=False, kind=3))
    deleted_proc = tmp_path / 'deleted-proc'
    deleted_proc.symlink_to(f'{executable} (deleted)')
    plants = [
        (dict(proc_exe=tmp_path / 'missing-proc', maps_text=good_maps),
         'procfs is missing'),
        (dict(proc_exe=deleted_proc, maps_text=good_maps),
         'worker process image is a deleted mapping'),
        (dict(proc_exe=proc_exe, maps_text=_maps_line(executable, suffix=' (deleted)')),
         'deleted mapping owns Py_GetVersion'),
        # Replaced then restored: the mapping keeps the replacement's
        # inode while the restored original sits at the path again.
        (dict(proc_exe=proc_exe,
              maps_text=_maps_line(executable, inode=os.stat(other).st_ino)),
         'was replaced at its path'),
        (dict(proc_exe=proc_exe,
              maps_text=_maps_line(executable, start=0x500000, end=0x501000)),
         'no mapping in /proc/self/maps contains'),
        (dict(proc_exe=proc_exe, maps_text=good_maps + _maps_line(other)),
         'ambiguous mapping owner'),
        (dict(proc_exe=proc_exe,
              maps_text='00400000-00401000 r-xp 00000000 00:00 0\n'),
         'anonymous mapping owns'),
        (dict(proc_exe=proc_exe,
              maps_text=good_maps + _maps_line(libpython, start=0x600000, end=0x601000)),
         'another libpython image is mapped'),
        (dict(proc_exe=proc_exe, maps_text=_maps_line(other)),
         'does not equal /proc/self/exe'),
        (dict(proc_exe=proc_exe, maps_text='garbage\n'),
         'cannot parse worker process maps'),
        (dict(proc_exe=proc_exe, maps_text=good_maps, version_info=(3, 13)),
         'worker Python version 3.13'),
    ]
    for plant, match in plants:
        with pytest.raises(go_v1.GoV1Error, match=match) as error:
            _prove(retained, **plant)
        assert error.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    # Positive control still passes after every refusal plant.
    assert _prove(retained, proc_exe=proc_exe, maps_text=good_maps)[1] == (
        retained.runtime.identity)


# A shared-runtime proof accepts the exact mapped libpython identity;
# the same path restored over a different mapped inode refuses.
def test_prove_loaded_identities_shared(layout, tmp_path):
    executable, resolve = layout
    name = 'libpython3.14.so.1.0'
    write(executable, image(needed=[name], symbol=False))
    runtime_path = write(tmp_path / 'distribution' / name,
                         image(soname=name, interpreter=False, kind=3))
    write(tmp_path / 'cache',
          cache_fixture([(0x303, 0, 0)], path=str(runtime_path)))
    retained = resolve()
    proc_exe = tmp_path / 'proc-exe'
    proc_exe.symlink_to(executable)
    loaded = _prove(retained, proc_exe=proc_exe,
                    maps_text=_maps_line(runtime_path))
    assert loaded[0] == retained.process.identity
    assert loaded[1] == retained.runtime.identity
    other = write(tmp_path / 'replacement', image())
    with pytest.raises(go_v1.GoV1Error, match='was replaced at its path'):
        _prove(retained, proc_exe=proc_exe,
               maps_text=_maps_line(runtime_path,
                                    inode=os.stat(other).st_ino))
    assert _prove(retained, proc_exe=proc_exe,
                  maps_text=_maps_line(runtime_path))[1] == (
        retained.runtime.identity)


# The Linux session helpers derive loaded images from procfs and maps
# (exact reported target text, the same file as the written image);
# missing procfs, a deleted process mapping, a deleted runtime
# mapping and a wrong worker Python version each refuse by name.
def test_linux_loaded_image_helpers(tmp_path):
    image_path = write(tmp_path / 'python3.14', image())
    proc_exe = tmp_path / 'proc-exe'
    proc_exe.symlink_to(image_path)
    reported = type(proc_exe)(os.readlink(proc_exe))
    assert go_v1._linux_loaded_process_image(_proc_exe=proc_exe) == reported
    assert os.path.samefile(reported, image_path)
    with pytest.raises(go_v1.GoV1Error, match='procfs is missing'):
        go_v1._linux_loaded_process_image(_proc_exe=tmp_path / 'missing')
    deleted = tmp_path / 'deleted-proc'
    deleted.symlink_to(f'{image_path} (deleted)')
    with pytest.raises(
            go_v1.GoV1Error, match='worker process image is a deleted mapping'):
        go_v1._linux_loaded_process_image(_proc_exe=deleted)
    seam = dict(_proc_exe=proc_exe, _address=_MAPS_ADDR, _version_info=(3, 14))
    assert go_v1._linux_loaded_python_runtime_image(
        _maps_text=_maps_line(image_path), **seam) == reported
    with pytest.raises(go_v1.GoV1Error, match='deleted mapping owns'):
        go_v1._linux_loaded_python_runtime_image(
            _maps_text=_maps_line(image_path, suffix=' (deleted)'), **seam)
    with pytest.raises(go_v1.GoV1Error, match='worker Python version 3.13'):
        go_v1._linux_loaded_python_runtime_image(
            _maps_text=_maps_line(image_path),
            **{**seam, '_version_info': (3, 13)})
    # Positive control still passes after every refusal plant.
    assert go_v1._linux_loaded_python_runtime_image(
        _maps_text=_maps_line(image_path), **seam) == reported


# The process identity hashes the file opened through procfs, not the
# file named by the readlink target: a divergent reported target keeps
# the opened file's hash, (dev, ino) and size, next to the default
# readlink positive control (exact target text, the same opened file).
def test_linux_process_identity_hashes_opened_file(tmp_path):
    opened = write(tmp_path / 'opened-python', image())
    reported = write(
        tmp_path / 'reported-python',
        image(interpreter=False, kind=3, symbol=False) + b'reported-tail')
    proc_exe = tmp_path / 'proc-exe'
    proc_exe.symlink_to(opened)
    opened_info = os.stat(opened)
    control = go_v1._linux_process_identity(proc_exe)
    assert control.path == type(proc_exe)(os.readlink(proc_exe))
    assert os.path.samefile(control.path, opened)
    assert (control.device, control.inode) == (
        opened_info.st_dev, opened_info.st_ino)
    divergent = go_v1._linux_process_identity(
        proc_exe, _readlink=lambda _path: str(reported))
    # The target text is only the reported path; every other field is
    # the opened file's, and none of them is the reported file's.
    assert divergent.path == reported
    assert divergent.sha256 == (
        'sha256:' + hashlib.sha256(opened.read_bytes()).hexdigest())
    assert divergent.sha256 != (
        'sha256:' + hashlib.sha256(reported.read_bytes()).hexdigest())
    assert (divergent.device, divergent.inode) == (
        opened_info.st_dev, opened_info.st_ino)
    assert divergent.inode != os.stat(reported).st_ino
    assert divergent.size == opened_info.st_size
    assert divergent.size != os.stat(reported).st_size
    assert go_v1._linux_process_identity(proc_exe) == control


# The worker native proof selects the Linux branch only when inventory
# names Linux and then carries full kernel identities; the macOS branch
# with different sentinel paths is the nearby positive control.
def test_worker_native_proof_linux_branch(tmp_path, monkeypatch):
    linux_process = go_v1._ExecutableIdentity(
        path=tmp_path / 'linux-process', sha256='sha256:' + 'a' * 64,
        size=11, device=101, inode=201, mode=0o755)
    linux_runtime = go_v1._ExecutableIdentity(
        path=tmp_path / 'linux-runtime', sha256='sha256:' + 'b' * 64,
        size=22, device=102, inode=202, mode=0o755)
    monkeypatch.setattr(
        go_v1, '_linux_loaded_identities',
        lambda: (linux_process, linux_runtime))
    monkeypatch.setattr(go_v1, 'inventory_platform', lambda: go_v1.PLATFORM_LINUX)
    assert go_v1._worker_native_runtime_proof() == {
        'process_image': linux_process.to_dict(),
        'runtime_image': linux_runtime.to_dict(),
    }
    macos_process = tmp_path / 'macos-process'
    macos_runtime = tmp_path / 'macos-runtime'
    monkeypatch.setattr(go_v1, '_macos_loaded_process_image', lambda: macos_process)
    monkeypatch.setattr(
        go_v1, '_macos_loaded_python_runtime_image', lambda: macos_runtime)
    monkeypatch.setattr(go_v1, 'inventory_platform', lambda: go_v1.PLATFORM_MACOS)
    assert go_v1._worker_native_runtime_proof() == {
        'process_image': str(macos_process),
        'runtime_image': str(macos_runtime),
    }


_ON_LINUX_314 = sys.platform == 'linux' and sys.version_info[:2] == (3, 14)


# Real kernel identities match /proc/self/exe stat and the /proc/self/maps
# owner of Py_GetVersion; only a Linux CPython 3.14 process can prove it.
@pytest.mark.skipif(
    not _ON_LINUX_314,
    reason='requires a real Linux CPython 3.14 process with procfs')
def test_real_linux_loaded_images():
    import ctypes
    process, runtime = go_v1._linux_loaded_identities()
    exe_info = os.stat('/proc/self/exe')
    assert (process.device, process.inode) == (exe_info.st_dev, exe_info.st_ino)
    entries = go_v1._parse_proc_maps(
        open('/proc/self/maps', encoding='utf-8').read())
    address = ctypes.cast(ctypes.pythonapi.Py_GetVersion, ctypes.c_void_p).value
    owner = go_v1._proc_mapping_for_address(entries, address, 'Py_GetVersion')
    assert (runtime.device, runtime.inode) == (owner.device, owner.inode)
    assert go_v1._linux_loaded_process_image() == process.path
    assert go_v1._linux_loaded_python_runtime_image() == runtime.path


# Real worker native proof on Linux carries the same kernel identities
# through the session branch, not paths alone.
@pytest.mark.skipif(
    not _ON_LINUX_314,
    reason='requires a real Linux CPython 3.14 process with procfs')
def test_real_linux_worker_native_proof(monkeypatch):
    monkeypatch.setattr(go_v1, 'inventory_platform', lambda: go_v1.PLATFORM_LINUX)
    proof = go_v1._worker_native_runtime_proof()
    process, runtime = go_v1._linux_loaded_identities()
    assert proof == {
        'process_image': process.to_dict(),
        'runtime_image': runtime.to_dict(),
    }


# With production defaults the running distribution interpreter resolves
# and proves against its retained identity, also from a child started
# with a relative argv[0] (kernel data, never argv strings).
@pytest.mark.skipif(
    not _ON_LINUX_314
    or os.path.realpath(sys.executable) != '/usr/bin/python3.14',
    reason='requires distribution CPython 3.14 at /usr/bin/python3.14 on Linux')
def test_real_linux_default_proof_and_relative_argv0():
    import subprocess
    from pathlib import Path
    retained = linux.resolve_linux_interpreter_identity(Path(sys.executable))
    loaded = linux.prove_loaded_identities(retained.linux)
    assert loaded[0] == retained.linux.process.identity
    assert loaded[1] == retained.linux.runtime.identity
    src_root = str(Path(go_v1.__file__).resolve().parents[2])
    code = (
        'from csk.builds import go_v1\n'
        'p, r = go_v1._linux_loaded_identities()\n'
        'print(p.device, p.inode, r.device, r.inode)\n'
    )
    result = subprocess.run(
        ['python3.14', '-c', code],
        executable='/usr/bin/python3.14',
        env={**os.environ, 'PYTHONPATH': src_root},
        capture_output=True, text=True, timeout=60, check=True)
    child = tuple(int(value) for value in result.stdout.split())
    assert child == (loaded[0].device, loaded[0].inode,
                     loaded[1].device, loaded[1].inode)
