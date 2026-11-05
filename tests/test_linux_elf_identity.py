"""Captured-byte fixtures prove parser behavior, never loader agreement."""
import os
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
    if os.name == "nt":
        pytest.skip("POSIX Linux filesystem fixture; byte parser tests remain portable")
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
