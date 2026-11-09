# Linux non-build lifecycle (L0)

The non-build skill lifecycle is tested on Ubuntu 26.04.1 x86-64 and Ubuntu
26.04 AArch64, both with glibc 2.43 and ext4. The tested commands are
`bootstrap`, `init`, `install`, repeat `install`, `status`, `update`, `upgrade`,
`audit`, and `remove` followed by `upgrade`. This claim covers skills requiring
no compilation. External Go builds remain refused on Linux.

For Skillfile schema 2/3, `csk remove NAME` changes the selection. Run
`csk upgrade` to refresh the lock and remove the installed member. A plain
`csk install` continues to refuse with `source_lock_stale`. For schema 1,
apply removal with `csk install`.

`csk audit` resolves the current schema-2/3 selection and its pinned transitive
skill dependencies through the source acquisition, confined capture and closure
pipeline. Every member receives a named report. Empty selections and missing,
malformed or unreadable members refuse. Audit neither installs skills nor runs
commands, builds or MCP servers. It leaves project files, published runtimes,
source stores and lock files unchanged. Audit results may be cached in the
manager audit directory; Git acquisition may use the network and the configured
source policy. The selector omission fixed in L0 also affected macOS.

The offline CI fixture uses a local path source and the static audit backend.
It refuses any attempt to acquire a network source or open a Python network
connection. Setup installs test dependencies before the offline test runs.
Existing Linux external-build refusal controls run in the Go E2E lanes.

Run the focused checks from an installed development checkout:

```sh
python -m pytest -v -rA tests/test_linux_l0_lifecycle.py tests/test_audit_sources.py
```

These tests create isolated fixture projects and manager homes under pytest's
temporary directory and remove them at teardown. Captured lifecycle receipts
list command arguments, exit codes and output. The PR Ubuntu core lane and the
merge/dispatch Ubuntu Python 3.14 lane run this focused step. AArch64 evidence
comes from the existing native `csk-arm64` Lima guest; no hosted AArch64 runner
is added. Native probe logs and exact revisions are recorded in the workbench
`linux-l0-outcome.md` delivery artifact.

This slice does not qualify Linux external builds, mutation retention, native
control identity, ELF admission, musl, other distributions or all agent adapters.
It does not advance or publish the protocol amendment. Qualification of those
surfaces remains governed by [the Linux build design](../design/linux-go-v1.md).
