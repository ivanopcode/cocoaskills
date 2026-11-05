# Linux ELF identity slice

Task: TASK-261105-1t4hyi, story STORY-261105-7e3djz.
Baseline: `e69b5755a717413b5f985d2da577ddb469ae7ae7`.

The experimental `_elf_identity` and `_linux_identity` modules implement the
image-selection portion of sections 4 and 7, row 2, of `linux-go-v1.md`.
Neither is imported by build admission. `supports_external_builds`,
`inventory_platform`, interpreter dispatch and `_native_executable_header`
remain unchanged. Existing macOS and Windows runtime selection is unchanged.

The baseline citations still name the current identity functions. The old
macOS resolver still scans for dylibs; this slice does not reuse that heuristic.
Linux has an explicit platform field in its separate result type. Integration
with `_InterpreterIdentity`, startup trees, venv configuration, mutation watches
and the worker's loaded-segment proof belongs to subsequent slices.

The parser accepts little-endian ELF64 x86-64 and AArch64 only. Bounds are
128 MiB per image, 256 program headers, 4096 dynamic entries, 4 MiB strings,
100000 symbols/hash-chain steps, 128 dependency images and 256 MiB aggregate
image data. Section headers are unnecessary. Single SysV or GNU dynamic hashes
bound the symbol scan; Python exports must also be reachable through that hash's
lookup (including GNU bloom and chain checks). Dual-hash images require equal
independently derived symbol bounds and agreement of both lookups for Python
definitions. glibc prefers GNU, so a SysV-derived bound alone cannot safely
establish absence of a GNU-visible interposer. Disagreement refuses, including
a forged shorter SysV bound or emptied GNU buckets. Versioned Python exports
also refuse; only an unversioned global function is supported. Adjacent file-contiguous LOAD segments can contain a
relocated string table; holes and conflicting mappings refuse.

The resolver accepts the two named glibc interpreters and expected CPython
3.11, 3.12, 3.13 or 3.14 contracts without ABI suffixes. A defined executable
`Py_GetVersion` symbol supplies positive embedded evidence. A direct dependency
must be exactly `libpython3.X.so.1.0`, with a matching SONAME and defined symbol.
The expected version is still a worker proof obligation: exported symbols do
not independently prove the embedded interpreter's version or loaded ownership.

Search support is deliberately narrow: one `$ORIGIN/../lib` entry or its
braced equivalent, then a new-format glibc cache with a unique matching ABI
entry. Cache-free default-directory lookup, transitive RPATH/RUNPATH layouts,
hardware-capability directories/entries, preload content, audit/filter scope,
and unresolved or ambiguous providers refuse. There is no filename glob.
Cache and preload absence, exact bytes, canonical images and symlink chains
are retained in the result and re-resolved by `verify`. Transitive DSOs are
also captured to reject hidden Python providers. This expands the image
closure, but is not a claim to bind Python's complete startup/import closure.
A resnapshot does not detect a transient write-and-restore; retention is a
separate prerequisite before admission can open.

Run the generated-byte fixtures with:

```sh
python -m pytest -q tests/test_linux_elf_identity.py
python -m mypy
```

The fixtures prove parser/selection/refusal behavior, not agreement with a real
loader. Native probes compare canonical `/proc/self/exe` and `dladdr` ownership
of `Py_GetVersion` under fixed Python flags and a loader-variable-free child
environment. They do not substitute for the future loaded-segment worker proof.
Native AArch64 agreement was independently demonstrated for Ubuntu 26.04,
glibc 2.43 and Python 3.14.4, including embedded, venv, origin-shared and
cache-shared layouts. That evidence supersedes the earlier byte-fixture-only
status; it does not qualify other libc or layout families. Musl is not admitted.

The cache parser bounds input and decoded string volume separately at 8 MiB,
entry count at 100000, each name/path at 4096 bytes and candidates per name at
64. A first pass validates applicable string spans and total expansion before
decoding. Candidate accumulation uses lists, avoiding repeated tuple copying.
All foreign ABI flags are ignored. Same-ABI hwcap/OS-conditional entries remain
unsupported candidates: ignoring one could skip the loader's preferred image.
Dynamic tags use an explicit allowlist of supported linkage/hash/version tags
and ordinary relocation/init metadata (including TLSDESC PLT/GOT, the x86-64
PLT address/size/entry descriptors on x86-64, and the zero-valued AArch64 BTI
PLT marker on AArch64); unknown tags and unsupported flag bits
refuse.

Additional deliberate availability limits: `e_flags` must be zero; interpreter
strings must match the two named loader paths exactly; captured files must be
single-link regular files; all ELF names are at most 4096 bytes. An existing but
unloadable origin candidate refuses instead of falling through to the cache.
These are supported-contract boundaries, not claims to emulate every glibc
fallback. Expanding them requires separate identity evidence. SONAME symlinks
may resolve outside the prefix, with their exact chain and target captured;
this slice does not impose prefix confinement.
