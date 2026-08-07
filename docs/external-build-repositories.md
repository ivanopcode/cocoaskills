# External build repositories

CocoaSkills implements the Curator Protocol `1.0.0-rc.5` schema-7
`go-repository-v1` boundary. It builds an executable from a separately locked
Git repository while keeping the skill package unable to select credentials,
Git configuration, hooks, compiler flags, output paths, wrappers, or signing.

The accepted protocol revision is
`f5d7673039226ab81de2f4f87e2155ae995c4df3`; its `conformance/v1/manifest.json`
SHA-256 is
`b6f56aacc0e37dcc6692f73f641bff761e89b645adfe20a47a06d81c6fda204c`.
The external-repository corpus is supplied to tests independently, so the csk
consumer imports no Curator implementation package or internal fixture value.

## Skill declaration

An `agent-skill.json` schema-7 declaration binds a canonical network identity,
an exact Git object ID, and optionally an exact tag:

```json
{
  "schema_version": 7,
  "capabilities": {},
  "build_repositories": {
    "golden-tools": {
      "git": "https://github.com/example/golden-tools.git",
      "locked_commit": {
        "object_format": "sha1",
        "hex": "0123456789abcdef0123456789abcdef01234567"
      },
      "tag": "v1.4.0"
    }
  },
  "commands": {
    "golden-tool": {
      "type": "build",
      "driver": "go-repository-v1",
      "repository": "golden-tools",
      "target": "golden-tool"
    }
  }
}
```

The referenced repository contains a closed `skill-build.json` descriptor:

```json
{
  "schema_version": 1,
  "targets": {
    "golden-tool": {
      "driver": "go-repository-v1",
      "build_root": ".",
      "source_dir": "cmd/golden-tool"
    }
  }
}
```

`build_root` must contain `go.mod`. Only that root is exposed to the compiler.
The output is always the manager-derived `bin/golden-tool` on macOS or
`bin/golden-tool.exe` on Windows. Arbitrary argv, environment, output, hook,
plugin, generator, credential, helper, filter, and signing fields are rejected.

## Admission and audit order

Every install reacquires the exact object or tag through an operator-selected
Git executable. CocoaSkills clears ambient Git configuration and helper state,
uses one exact refspec, proves raw object identities and the reachable graph,
rejects LFS pointers, submodules, links and special modes, materializes and
rehashes the complete snapshot, validates `skill-build.json`, and runs the
independent external audit before any protected artifact lookup or compiler
call.

For a declared tag, the fetched tag must still terminate at `locked_commit`.
A moved tag, missing tag/object, inaccessible source, malformed raw object, or
failed audit stops without publishing a shim or marker. An untagged source may
reuse the exact protected snapshot recorded by an existing marker when the
network is unavailable; a tagged source always requires a fresh tag proof.

## Build, cache, and lifecycle

The fixed Go contract is the same `manager-worker-v1` session documented in the
main README: native toolchain, vendored modules, no network, no workspace, no
cgo, internal linking, and manager-derived output. External builds use a
receipt-v2 cache below `<csk-home>/external-builds`; schema-7 installations use
marker v3 and may contain local receipt-v1 and external receipt-v2 commands
together.

Project install publishes `.agents/bin/<command>`; global install publishes
`<csk-home>/global/bin/<command>`. Both managed launchers point directly at the
validated protected artifact, preserve arguments and exit status, and retain
the inherited PATH. Do not copy a compiled artifact into `scripts`, add a
hand-written wrapper, or prepend a private cache directory to PATH. Agents
already resolve project then global managed shims. For optional interactive
bare commands, use the documented `csk shell-init --install` hook.

Run ordinary lifecycle commands:

```text
csk install
csk install --dry-run
csk status
csk install                 # repair/reinstall
csk global install
csk global status
csk gc
```

To uninstall, remove the skill declaration from the project or global
`Skillfile.json` and run the matching install command; reconciliation removes
the stale marker and shim transactionally.

Dry-run still acquires, proves, validates, audits, and inspects the candidate
cache, but does not compile or mutate. A corrupt receipt, artifact, or snapshot
is never patched or adopted: a mutating install quarantines it and rebuilds
from a newly proved source. Project/global marker and shim publication uses the
existing transaction engine, so a build, collision, crash, or consumer-marker
failure leaves the prior complete installation current or recoverable.

## Toolchain fingerprint deadline

Every build session hashes the complete selected GOROOT to pin the toolchain
identity, and each hashing pass is bounded by a deadline. Reading a cold Go
installation is much slower than reading a warm one, especially on Windows,
where on-access antivirus scans each file the first time it is touched. When
the pass does not finish in time the install fails closed with:

```text
go-v1 toolchain_timeout: toolchain fingerprint deadline exceeded
```

Operators raise the bound with `CSK_GO_FINGERPRINT_TIMEOUT`, a number of
seconds:

```bash
CSK_GO_FINGERPRINT_TIMEOUT=1800 csk install
```

The default is 600 seconds and the accepted range is 0 exclusive to 3600
seconds inclusive; a larger value is clamped to 3600 and a missing, empty, or
unparseable value falls back to the default rather than failing the install.
The deadline is a liveness bound, not a trust decision: raising it never admits
a toolchain that would otherwise be refused, and it can never be removed
entirely. Callers embedding CocoaSkills set the same bound in code through
`ToolchainConfig(fingerprint_timeout=...)` or the `timeout` argument of
`fingerprint_toolchain`, which take precedence over the environment.

Prefer raising this bound over retrying a failed install. A retry only appears
to help because the first attempt warmed the operating system cache.

## Development substitutions

`Skillfile.dev.json` schema 2 may replace one declared repository for local
development without changing the package declaration:

```json
{
  "schema_version": 2,
  "substitutions": {},
  "build_repository_substitutions": {
    "golden-skill": {
      "golden-tools": {"path": "../golden-tools"}
    }
  }
}
```

A network substitution instead declares `git` plus one typed `revision` or
`tag`. Local selection admits a narrow ordinary `.git` layout and records a
host-path-free operator-local identity. Substitution state is explicit in
receipt v2 and marker v3 and never aliases the declared source. Strict audit
refuses substituted installs. Keep `Skillfile.dev.json` ignored; csk verifies
that boundary before use.

## Platform qualification

`go-repository-v1` is supported and qualified only on native macOS and Windows
hosts. Linux support is deliberately deferred and is not implied by generic
CocoaSkills script/system-command support. Platform evidence must record the
exact OS, architecture, Python, Git, Go, csk, Curator consumer, protocol commit,
and corpus manifest used by the run.
