# BUG-260731-1rldqv — Windows transactional install regression

Scope: CocoaSkills PR 16 (`task/TASK-260720-3t8nr3-transactional-project-hybrid`,
head `8a02e17`), CI run `30594273278`. All four `windows-latest` cells fail while
all `ubuntu-latest`, all `macos-latest` and `mypy strict` are green.

## 1. Failure inventory

Classification of the 45 failures on `Python 3.11 / windows-latest`
(job `91043117435`); the 3.12 and 3.13 cells are identical, 3.14 has 34.

| Count | Signature |
| --- | --- |
| 28 | `TransactionCorruptionError: transaction target changed while digesting: …\bin\<cmd>.cmd` |
| 12 | `PermissionError: [WinError 5] Access is denied: …\project\.claude|.codex\skills\<name>` (8 direct, 4 surfacing as `test_e2e` CLI exit 1) |
| 4 | `cache_publication_invalid: publication artifact source is not private, singly linked, owner-controlled regular state: manager home owner does not match the current manager principal` |
| 1 | `test_audit_cli` CLI exit 1 — same digest family (its fixture ships `scripts/tool.cmd`) |

Provenance from BUG-260731-2rhy74 / RUN-260731-b4fd97 holds: `main` (`b3a5031`)
is green on all four Windows cells (run `30556125542`), the transaction-engine
commits are already on `main`, and the marker-v2 commit `8a02e17` touches
nothing in this path. The regression entered with `c4131bd`, which is the first
commit that routes project materialization — including runtime command shims and
adapter entries — through the transaction engine.

## 2. Root cause 1 — synthesized execute bits (28 + 1 failures)

`os.stat()` on Windows ORs `0o111` into `st_mode` when the *path* ends in
`.bat`, `.cmd`, `.com` or `.exe`. CPython 3.13 `Modules/posixmodule.c`:

```c
static void
update_st_mode_from_path(const wchar_t *path, DWORD attr,
                         struct _Py_stat_struct *result)
{
    ...
        if (_wcsicmp(fileExtension, L".exe") == 0 ||
            _wcsicmp(fileExtension, L".bat") == 0 ||
            _wcsicmp(fileExtension, L".cmd") == 0 ||
            _wcsicmp(fileExtension, L".com") == 0) {
            result->st_mode |= 0111;
        }
}
```

It is called only from the two path-based stat implementations
(`win32_xstat_slow_impl`, `win32_xstat_fast_impl`). `os.fstat()` goes through
`Python/fileutils.c:_Py_fstat_noraise` → `_Py_attribute_data_to_stat`, which has
no path and therefore cannot apply it. For the same `tool.cmd` bytes:

* `path.lstat()` → `S_IMODE == 0o777`
* `os.fstat(handle.fileno())` → `S_IMODE == 0o666`

`transactions._digest_file` compared those two directly, so every command shim
tripped the corruption guard on the first digest pass. The same lstat-vs-fstat
mode comparison existed in `_entry_content_digest` and `_staging_prefix_digest`.

The digest payload had a second, latent form of the same defect: it hashed
`stat.S_IMODE(info.st_mode)` straight from the path stat, so identical bytes
digested differently under a staging sidecar name and under the live `.cmd`
name.

`digest_target` never fires for `.csk-install.json` because that name carries no
executable extension — which is exactly the asymmetry reported in the bug.

### Fix

`transactions._permission_identity(info)` reduces a stat result to the
permission state the platform can actually hold, reusing the existing
`_permission_mode_identity` helper (identity on POSIX, the writable bit on
Windows, where `chmod` can only toggle `FILE_ATTRIBUTE_READONLY`). It is used
both for the digest payload and for the change guards in `_digest_file`,
`_entry_content_digest` and `_staging_prefix_digest`.

This is not a relaxation: on POSIX the digests and comparisons are byte-for-byte
what they were, and on Windows the guard now compares the only permission bit
the OS maintains instead of one the filesystem never stored.

## 3. Root cause 2 — Windows symlink type (12 failures)

`adapters.stage_project_adapter_targets` stages an adapter entry as a symlink
whose destination is relative to the **live** location:

```python
staged.symlink_to(
    os.path.relpath(canonical, target.live_path.parent),
    target_is_directory=True,
)
```

While it sits in the staging root that destination is dangling by construction.
`transactions._staging_tree_entry` recorded the link type by resolving it:

```python
link_is_directory=path.is_dir(),   # False for a dangling link
```

`_create_staging_entry` then rebuilt the link with
`target_is_directory=bool(entry.link_is_directory)` — i.e. as a Windows *file*
symlink pointing at a directory. Such a link cannot be traversed: `CreateFileW`
fails with `ERROR_ACCESS_DENIED`, and CPython's `win32_xstat_slow_impl` restores
that error for a reparse point it must traverse but cannot, so `os.stat` raises
`PermissionError [WinError 5]`. POSIX ignores `target_is_directory`, which is
why only Windows broke.

This matches the observed shape exactly: the first install "succeeds" and
creates the entry, the test's own `.exists()` on it raises `WinError 5`, and a
second install fails while planning because it stats the same entry.

### Fix

The link type is a property of the reparse point, not of its destination:

```python
def _link_is_directory(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", None)
    if attributes is None:
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_DIRECTORY)
```

Windows sets `FILE_ATTRIBUTE_DIRECTORY` on a directory link's own entry; POSIX
links have no type, so the value is deterministically `False` there rather than
depending on whether the destination happens to resolve. Because it is now
deterministic, `link_is_directory` was also added to the staging-entry
comparison in `_validate_staging_entry_modes`, which strengthens the guard.

`adapters._link_probe` was creating a *file* link to probe for symlink support
while the adapter always creates directory links; it now probes with the same
link kind it will create.

## 4. Root cause 3 — Windows never grants manager ownership (4 failures)

A `windows-latest` probe (job `91111242181`) measured the host directly:

```
===== A: os.stat vs os.fstat mode for extensions =====
tool.cmd   lstat=0o0777 fstat=0o0666 same_ino=True
tool.exe   lstat=0o0777 fstat=0o0666 same_ino=True
tool.bat   lstat=0o0777 fstat=0o0666 same_ino=True
tool.txt   lstat=0o0666 fstat=0o0666 same_ino=True
tool       lstat=0o0666 fstat=0o0666 same_ino=True

===== B2: dangling relative link reports is_dir() =====
dangling dir link: is_dir()=False st_file_attributes=0x410

===== C: publication-source owner SIDs =====
_current_user_sid() = S-1-5-21-1742564184-1656218818-310408600-500
plain dir:  owner=S-1-5-32-544 matches_current=False
plain file: owner=S-1-5-32-544 matches_current=False
```

A and B2 confirm causes 1 and 2 directly. C is cause 3: the runner principal is
RID 500, the built-in Administrator, and every object it creates is owned by
`S-1-5-32-544`, `BUILTIN\Administrators` — never by the token user. That is
ordinary Windows behaviour for an elevated administrator, not a runner quirk.

Both Windows guards that failed require the *token user* to own the bytes:

* `cache_windows._open_publication_source` — the artifact the compiler wrote
  inside the manager's own private operation root.
* `cache_windows._open_manager_home` — the manager home itself.

POSIX gets that state for free: `st_uid` of anything the manager creates is the
effective uid, and `mkdir(0o700)` finishes the job. Windows grants neither the
ownership nor the DACL, so the manager has to establish them explicitly — and
CocoaSkills never did. `main` never noticed because `main`'s installer only
*plans* builds (`from .builds import planner as build_planner` and nothing
else); `c4131bd` is the commit that first compiles and publishes during
`installer.install`, so it is the first code that reaches these guards on
Windows.

### Fix

Two boundary steps, each the Windows counterpart of something POSIX does
implicitly. Neither guard was touched.

* `builds/cache.provision_manager_home` — called from
  `locking.provision_new_manager_home`, which now creates the manager home and
  provisions it. Only a home this call creates is provisioned; an established
  home is never repaired, so genuine ownership drift on an existing home still
  fails closed exactly as
  `test_windows_manager_home_permissions_and_owner_drift_fail_closed` requires.
  The other place a home comes into existence, writing the global config, goes
  through the same call.
* `builds/cache.make_publication_source_private` — called by the installer at
  the moment it takes custody of a freshly compiled artifact, before offering
  it for publication. On Windows it applies the same `_MUTABLE_FILE` profile
  the protected cache applies to every file it creates itself; on POSIX it
  clears group and other write bits, which is precisely the POSIX predicate.

The test fixture now provisions its manager home through
`locking.provision_new_manager_home` instead of a bare `mkdir`, so the tests
pass because the product establishes the state, not because the harness avoids
the platform.

## 5. Root cause 4 — the build stub was not host-faithful

With the ownership fixes in place the same four tests reached shim activation
and failed with `Command 'build-tool' receipt target 'linux' does not match
windows activation`. `_stub_trusted_toolchain`, added by `c4131bd`, hard-codes
`NativeTarget(goos="linux", ...)` and `bin/<command>` as the artifact path.
That was invisible while nothing published, because `shims` only rejects a
receipt when `(goos == "windows") != (host is Windows)`. The stub now derives
the target from the host and takes the artifact path from
`build_metadata.derived_artifact_path`, so it produces `bin/<command>.exe` on
Windows.

## 6. Residual risk to decide

Provisioning covers homes CocoaSkills creates from this change onward. A
Windows user who already has a `~/.cocoaskills` created by an elevated shell
keeps an Administrators-owned home, and schema-v6 build commands will fail
closed there with `cache_boundary_untrusted: manager home owner does not match
the current manager principal`. Repairing such a home means taking ownership of
a directory CocoaSkills did not create, which would blunt the drift guard, so
this change deliberately does not do it. Whether to add an explicit opt-in
repair (`csk doctor`-style) or to document manual re-provisioning is a product
decision, recorded here rather than taken.

## 7. Evidence

See `BUG-260731-1rldqv_evidence.md`.
