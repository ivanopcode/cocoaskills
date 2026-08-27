# Curator Protocol Core 1.0

This document is normative. It defines the portable objects and deterministic
algorithms shared by conforming Curator Protocol managers. Tool-specific state
and user interfaces are defined outside the core.

Protocol rc.9 carries the closed assurance selection and platform-neutral verified
provider contract in [`assurance.md`](assurance.md). That document adds new
identities and does not widen any portable object defined here.

## 1. Data model and versioning

Protocol JSON MUST be UTF-8 without a byte-order mark. Parsers MUST reject
duplicate object keys, invalid UTF-8, trailing non-whitespace data, and values
that violate the applicable schema under `../schemas/v1/`.

JSON object member order and insignificant whitespace have no meaning unless a
section explicitly defines canonical bytes. Array order is significant unless
the field is declared set-like. Writers MUST emit the current schema version;
readers MUST reject unsupported versions with an upgrade error and MUST NOT
infer a newer schema from its fields.

Schemas define structural validity. The semantic checks in this document are
additional and REQUIRED.

Manifest schemas 1 through 6, the `go-v1` driver, build receipt schema 1,
install marker schemas 1 and 2, conformance claim schemas 1 and 2, and protocol
`1.0.0-rc.4` are frozen compatibility surfaces. Implementations MUST NOT
reinterpret, broaden, relabel, or infer schema-7 behavior from any of them.
External build repositories are introduced only by the separately versioned
schema-7/rc.5 surfaces defined below.

### 1.1 Compatibility identifiers

The filenames `Skillfile.json`, `Skillfile.dev.json`, `agent-skill.json`,
`skill-build.json`, `.csk-install.json`, `.csk-managed.json`, and project root
`.agents/` are portable protocol identifiers. A manager MUST read and write
those exact names. The filename `csk-skill.json` is a reserved legacy alias for
`agent-skill.json`: managers MUST continue to read it throughout protocol 1.x,
but writers MUST emit only `agent-skill.json`. `skill-build.json` is not a
skill-manifest alias; it is valid only as the schema-7 external-repository
descriptor defined in section 4.2.2.

Machine-home directories, cache names, executable names, global environment
variables, and managed comment text are implementation-specific. A manager
MUST NOT write machine-local state into another manager's home unless the user
explicitly selected that location.

## 2. Portable identifiers and paths

Skill names, command names, MCP server names, agent identifiers, and adapter
ledger entries MUST match:

```text
^[A-Za-z0-9][A-Za-z0-9._-]*$
```

They MUST additionally satisfy the portable filename rules below. Comparison
is case-sensitive even on a case-insensitive filesystem.

A portable relative path:

1. is a non-empty Unicode string encoded as UTF-8;
2. uses `/` separators and contains no `\`, NUL, or control character;
3. is not absolute and has no empty, `.` or `..` component;
4. has no component ending in a space or `.` and no component containing `:`;
5. has no component whose case-insensitive basename before its first `.` is
   `CON`, `PRN`, `AUX`, `NUL`, `COM1` through `COM9`, or `LPT1` through `LPT9`.

Implementations MUST preserve Unicode scalar values exactly and MUST NOT apply
normalization during hashing or comparison. Filesystem extraction MUST detect
two protocol paths that map to one platform path and fail before writing.

## 3. Skill packages

A skill package is a git snapshot or a directory within one. Its root MUST
contain `SKILL.md` with YAML frontmatter containing non-empty `name` and
`description` strings. The frontmatter `name` MUST equal the declared skill
name. `triggers`, when present, is a list of non-empty strings.

An installer MUST NOT execute package-provided code while resolving,
validating, auditing, or installing a skill. Schema 6 and 7 build commands are
the only compilation extensions to this rule: a manager MAY pass validated
untrusted Go source bytes to the closed `go-v1` or `go-repository-v1` driver in
section 4.2, but MUST NOT transfer execution control to a package- or
repository-selected program, argument vector, environment, hook, plugin,
generator, build recipe, signer, or compiled output.

### 3.1 Context selection

Only these root entries are eligible for agent-facing context:

```text
SKILL.md
agents/
references/
.skill_triggers/
assets/
templates/
examples/
data/
```

`scripts/` is additionally eligible only when the package exports no commands.
At every depth the following names or glob patterns MUST be excluded:

```text
.git .github .gitlab-ci.yml .venv __pycache__ *.pyc node_modules
tests test __tests__ README* CHANGELOG* LICENSE* Makefile
setup.py pyproject.toml requirements*.txt .DS_Store .gitignore
```

Directories declared in `runtime_roots` or `build_roots` are excluded as whole
subtrees even if nested under an eligible context root. The `build_roots`
exclusion is computed from the validated manifest before locale rendering,
cache lookup, or any compiler command. It is therefore identical for a real
build, a build-cache hit, and dry-run. Build roots MUST NOT be copied into the
commit-keyed runtime store or any installed context, and generated artifacts
MUST remain outside agent-facing context.

An external build repository is never part of the consuming skill snapshot.
Its descriptor, source, object database, frozen snapshot, build root, and
generated artifact MUST NOT enter agent-facing context or the commit-keyed
runtime store. This exclusion is unconditional and identical for a real build,
cache hit, dry-run, status, and repair.

Selection uses protocol paths, sorts selected files by Unicode scalar value of
their POSIX path, rejects links, and copies regular file bytes without newline
conversion.

### 3.2 Localization

Localization is inactive when no locale is selected. When
`locales/metadata.json` exists it MUST contain an object `locales`; when
`.skill_triggers/` exists it MUST be a directory. A consistent locale has both
`locales[locale]` and `.skill_triggers/<locale>.md`. At least one consistent
locale is REQUIRED when either localization surface exists.

A locale selector is 1 through 64 ASCII letters, digits, or hyphens, starts and
ends with a letter or digit, and is compared case-sensitively without
normalization. Every `locales` member name MUST be a locale selector and its
value MUST be an object. This deliberately uses a safe BCP 47-compatible
surface without attempting language-tag canonicalization.

For a selected consistent locale, a manager replaces only the `description`
and `triggers` values in installed `SKILL.md`, preserving `name` and body. List
items beginning with `- ` outside fenced code blocks form the trigger list.
When present, `agents/openai.yaml` is rendered from `display_name`,
`short_description`, and `default_prompt`. If the selected locale is
unavailable, source context is installed unchanged and a warning lists the
available consistent locales. The warning is emitted even for an otherwise
current installation.

## 4. Skill manifest

`agent-skill.json` is OPTIONAL for a pure context skill and otherwise conforms
to exactly one of `agent-skill-v1.schema.json` through
`agent-skill-v8.schema.json`. The legacy filename `csk-skill.json` has exactly
the same object shape and schema-version semantics through
`csk-skill-v8.schema.json`.

Readers resolve manifests in this order:

1. When only `agent-skill.json` exists, read it.
2. When only `csk-skill.json` exists, read it as a legacy manifest.
3. When both exist, parse and validate both independently. They MUST represent
   equal JSON values: object member order and insignificant whitespace are
   ignored, array order and JSON value types remain significant. If equal,
   `agent-skill.json` is authoritative. If unequal, the reader MUST fail with
   `conflicting_skill_manifests` and MUST NOT install either value.
4. The existence of an invalid modern manifest is an error and MUST NOT fall
   back to the other filename or to `agents/runtime.json`.

| Schema | Added behavior |
|---|---|
| 1 | exported script and system commands |
| 2 | `runtime_roots`, command dependencies, strict top-level fields |
| 3 | REQUIRED capability declaration |
| 4 | transitive skill requirements |
| 5 | MCP server requirements |
| 6 | declarative compiled commands and context-excluded `build_roots` |
| 7 | first-class external build repositories and `go-repository-v1` |
| 8 | opt-in `script-worker-v1` script execution policy and declared first-party module roots |

Version gates are downward: a field introduced by a later version MUST be
rejected in an earlier one. Schema 1 preserves its deployed extension behavior;
schemas 2 through 8 reject unknown fields. Schema 1 through 5 MUST reject
`build_roots`, a command with `type: "build"`, and every build-only field.
Schema 1 through 6 MUST reject `build_repositories`, `repository`, `target`,
and `go-repository-v1`. Schema 1 through 7 MUST reject `modules` at the top
level and on every command. Schema 1 through 7 MUST also reject
`execution_policy` and `interpreter` at the top level and on every command:
schemas 2 through 7 reject them as unknown fields, and schema 1, which keeps
its deployed extension behavior, is rejected by the semantic checks of this
document instead. Their script, system, runtime-root, capability,
dependency, context, hash, `go-v1`, receipt-v1, and marker-v1/v2 behavior is
unchanged.

### 4.1 Runtime roots and commands

Every runtime root and script command path MUST be a portable relative path
that exists in the snapshot. Runtime roots MUST name directories, be unique,
and be pairwise disjoint. For schema 2 and later, every script command path
MUST fall within one declared runtime root when any roots are declared.

A script command declares at least one of `unix_path` and `win_path`. A system
command declares a non-empty bare executable name and MAY include a hint. A
missing system command fails installation with the hint. The command name is
the shim name and one active name has exactly one owner.

#### 4.1.1 Portable `script-worker-v1` execution policy

A script command is declared-only unless it selects the enforced execution
policy named `script-worker-v1`. Protocol 1.0 defines exactly one script
execution policy, and every conforming manager MUST implement it on macOS,
Linux, and Windows. Selection is the OPTIONAL `execution_policy` field on a
script command of manifest schema 8 or later, whose only admitted value is
`script-worker-v1`. Schema 7 and earlier script commands keep their exact
meaning above, and an absent field means declared-only on every schema.

`execution_policy` and `interpreter` are co-required. A script command that
declares one without the other is an invalid manifest and MUST be rejected by
manifest validation; a manager MUST NOT resolve the missing field to a default
and MUST NOT install such a command declared-only.

Selecting the policy is not a package-visible choice of policy. The value space
is closed and holds exactly one identity, so a package chooses only whether the
command is enforced, never how it is enforced. Every control, limit, path,
environment value, and executable stays manager-owned, and the influence of the
package field is monotonically restrictive: an enforced command may do strictly
less than the same command declared-only and never more. Section 4.2.1's rule
that the build policy identity is never a package-visible option is unchanged
and continues to govern that policy. Here the package makes exactly one visible
choice, whether the command is enforced, and cannot reach the contents of the
policy it selects.

Capabilities are declared once per skill in section 4.3, so every enforced
command of one skill derives the same containment profile. A manager MUST NOT
widen or narrow that profile per command from any other manifest value.

The fixed process graph is:

```text
manager parent
  -> identity-verified manager-owned script worker
       -> identity-verified interpreter for the declared identifier
            -> manager-resolved executables named by the `exec` capability
```

The worker is an exact re-execution of the installed manager executable in one
fixed hidden mode, the same boundary section 4.2.1 defines for builds. It is an
implementation boundary, not a user-visible command surface and not a
package-selected program. No package file, manifest value, script byte, shebang
line, file association, environment value, `PATH` lookup, shell, or user option
selects the worker executable or its mode. The fourth node exists only when
`exec` names executables; under `exec: "none"` the graph is exactly three
nodes. An implementation that cannot distribute an equivalent identity-verified
worker MUST state the exact executable graph it verifies instead and MUST treat
every mutable component of that graph, including the interpreter and its
installed package tree, as trusted computing base.

One worker session serves exactly one invocation of exactly one enforced
command. The worker applies the complete containment profile before the
interpreter starts and admits no second command, second interpreter, additional
program, shell, or control change afterwards.

An enforced script command names a closed interpreter identifier in the
`interpreter` field. Protocol 1.0 admits exactly `python3-v1` and `node-v1`.
The manager, and only the manager, resolves that identifier to an executable,
once per invocation:

- resolution is package-independent and completes before the manager enters any
  package-controlled directory. A manager MUST NOT resolve an interpreter from
  the commit-keyed runtime store, a runtime root, a project command directory,
  the caller's `PATH`, a manifest value, a project file, or a script byte;
- the resolved target MUST be a canonical regular executable file. Symlink,
  reparse-point, and hard-link substitution MUST be rejected, strong file
  identity MUST be recorded, and the executable's bytes MUST be hashed;
- that identity MUST be re-checked at the launch boundary so a replacement race
  cannot widen the graph; and
- the manager MUST NOT run the interpreter to obtain a version string, and MUST
  NOT hash the interpreter's standard-library, library, or installed-package
  trees.

The interpreter's library and installed-package trees are trusted computing
base, not verified identity, and an implementation MUST say so where it reports
this policy. The toolchain fingerprint of section 8.2 MUST NOT be applied to an
interpreter. It hashes a manager-pinned tree that stays unchanged through the
last child exit, while an interpreter installation is host-owned and
legitimately mutated between two invocations of the same command.

The shebang line, the file extension, and the Windows file association are
inert under this policy and MUST NOT select the executed program. No further
interpreter identifier is admitted in protocol 1.0. `bash-v1` and
`powershell-v1` are the obvious candidates and are deferred because neither a
POSIX shell nor PowerShell resolves on all three supported platforms;
admitting either, or any other interpreter, is a specification revision under
section 12.3 and never a manager configuration option.

The script command launcher rules of the manager profile govern declared-only
commands. An enforced command's shim MUST NOT be a symlink, a POSIX-shell
wrapper, a `.cmd` wrapper, or any other program that resolves the command
through a shell or through the inherited `PATH`. It MUST be a manager-owned
launcher that starts the worker, and the worker MUST start the interpreter by
its resolved path, never through `cmd.exe`, PowerShell, `sh -c`, `PATHEXT`, or
a file association.

Under this policy the section 4.3 declaration is the policy input. Derivation
reads the declared manifest bytes: a capability field the manifest does not
contain derives the deny-by-default meaning below, and the schema default for
that field MUST NOT widen a derived control. The declared-only audit reading of
section 4.3 is unchanged for every command that has not opted in.

- `network: "none"`, and an absent `network`, derive an offline environment.
  The manager sets offline network configuration for the interpreter and its
  descendants, scrubs proxy and resolver configuration out of the environment,
  and applies `network-isolation-domain` when the inventory probe finds it
  present. This is a manager mechanism and is not denial.
- `network` as a host-glob list derives no filtering. The manager MUST record
  the declared hosts, MUST NOT represent them as an applied control, MUST NOT
  place a host-filtering entry in the inventory or in an evidence record, and
  MUST NOT claim filtering on any surface. The command is admitted, not
  rejected, and carries the `script-command-unfiltered-declared-network` audit
  warning class.
- `exec: "none"`, and an absent `exec`, derive a `PATH` that resolves exactly
  the interpreter. A name set additionally derives exactly those names, each
  resolved by the manager to a fixed path under the interpreter resolution
  rules above. The manager MUST discard the inherited `PATH` and MUST build a
  `PATH` whose entries are manager-owned directories exposing exactly the
  resolved interpreter and the resolved declared names. A declared name the
  manager cannot resolve is absent from the built `PATH` and MUST be reported;
  it MUST NOT be resolved from the caller's `PATH` at launch or by the
  interpreter at run time.
- `filesystem` always derives an operation-private runtime area: a private
  temporary root, a private configuration root, a private cache root, and a
  manager-selected working directory, all resolved independently of package
  data. An absent `filesystem` derives that area and nothing else; the section
  4.3 schema default `"repo"` is the declared-only audit reading and MUST NOT
  widen the derived control. `"repo"` additionally derives the canonical
  project root of the invocation. `"home-config"` derives nothing beyond the
  private configuration root, because the manager redirects the platform
  configuration, cache, and temporary environment into the private roots; a
  command that resolves its configuration location from that environment writes
  into its private configuration root. A portable relative path set derives
  exactly those paths beneath the canonical project root. Every derived
  `filesystem` value bounds manager-directed write targets and the environment
  that points at them. This policy does not bound what an enforced command
  reads and MUST NOT be reported as bounding it.
- `secrets` derives no secret material in any form. The manager passes no
  secret value into the worker or the interpreter, whether the field is absent,
  `"none"`, or a non-empty identifier set. The identifiers remain an audit
  declaration, MUST NOT cause a manager to inject a value, and MUST NOT widen
  the environment. Protocol 1.0 defines no secret-provider contract for this
  policy.
- `env_read` derives exactly the named host variables that are present in the
  manager's own environment; every other inherited variable is absent. The
  manager owns every name it sets under this policy and every name that selects
  a program, a library or module search path, an interpreter startup file or
  option, a temporary or configuration root, or proxy or resolver
  configuration. An `env_read` entry naming a manager-owned name MUST NOT pass
  the inherited value through; the manager-set value stands. The exact reserved
  set per platform and per interpreter identifier is defined by the manager
  profile.
- `prompt_scope` derives no control.

The following controls are mandatory on every supported host, and they are the
only controls whose absence rejects an invocation:

- the fixed process graph above: within this execution boundary the manager and
  the worker start only the nodes above and no other program;
- worker executable identity verification before launch, and re-verification at
  the launch boundary;
- interpreter resolution and per-invocation executable identity verification as
  defined above;
- a manager-built environment: an empty bootstrap, the manager-set values, and
  exactly the `env_read` names passed through, with the inherited environment
  otherwise discarded;
- the manager-built `PATH` derived above, with the inherited `PATH` discarded;
- offline network configuration plus proxy and resolver scrubbing whenever the
  derived network capability is `none`;
- the operation-private temporary, configuration, and cache roots and the
  manager-selected working directory derived above;
- explicit manager-controlled standard-stream binding and release of unrelated
  descriptors or handles before the interpreter starts;
- application of every native control the inventory below marks available for
  the host platform, and of every `host-conditional` control the probe finds
  present;
- exactly one closed `script-capability-evidence-v1` record per invocation; and
- termination and joining of the complete worker domain before the invocation
  returns.

A manager that cannot apply all of them MUST reject the invocation with
`script_execution_control_unavailable` before starting the worker or the
interpreter. It MUST apply the same check when it publishes or updates the shim
of an enforced command and MUST reject that install or update with the same
diagnostic, so a shim that can never run is never published.

Three controls of section 4.2.1 are deliberately not carried across, because a
script command is a user-facing program and a build is not. This specification
states each divergence so that neither a reader nor an implementation inherits
the build rule by reflex:

1. Standard input stays open. Section 4.2.1 requires closed standard input.
   Under this policy the manager binds standard input explicitly, to the
   intended stream or to the host's null device, MUST NOT leave it bound to an
   inherited descriptor it did not intend, and MUST NOT close it as a matter of
   policy.
2. Output MAY stream. The bounded and redacted combined-output rule of section
   4.2.1 applies to output a manager captures. Direct pass-through streaming is
   permitted when the manager binds the streams itself, because a command
   launcher forwards the child's output and exit status transparently.
3. There is no policy deadline. An enforced command MAY run indefinitely. A
   wall-clock bound is an operator or invocation bound, not a control of this
   policy, and its absence MUST NOT be reported as an unavailable control.

Each mandatory control is a manager-enforced mechanism, not a kernel-enforced
guarantee. This specification states both sides so that neither a reader nor an
implementation can mistake one for the other:

| Portable mechanism | What it means | What it does not mean | Deferred guarantee |
|---|---|---|---|
| derived `network: "none"` | offline network configuration for the interpreter and its descendants, and proxy and resolver configuration scrubbed out of the manager-built environment | kernel-enforced network denial for the worker domain or its descendants | `script-total-network-denial` |
| declared host globs | the declared hosts are recorded and reported on the audit and result surfaces | any filtering, allowlisting, resolution failure, or denial of the named or unnamed hosts | `script-network-host-allowlisting` |
| manager-built `PATH` over the resolved interpreter and the resolved `exec` names | the inherited `PATH` is discarded and bare-name resolution reaches exactly the manager-resolved executables | kernel-enforced allowlisting of the executables a descendant may run; a script that names an absolute path, or that re-executes through an interpreter-specific mechanism, is not prevented from doing so | `script-exact-executable-allowlisting` |
| operation-private runtime area plus redirected configuration, cache, and temporary roots | every manager-directed write target is private to the invocation and the platform environment points at those private roots | kernel-enforced confinement of every descendant write to the private area and the derived paths | `script-private-runtime-area-only-writes` |
| worker and interpreter identity verification | the worker and the interpreter are canonical regular files whose identity is verified before launch and re-checked at the launch boundary | kernel-enforced read-only presentation of the runtime tree, the interpreter, or its installed package tree to descendants | `script-read-only-runtime-tree` |
| probe plus applied inventory controls | every control the inventory marks available for the platform, and every `host-conditional` control the probe finds present, is applied and recorded | hard aggregate process, memory, disk, time, and output bounds over every descendant | `script-hard-aggregate-descendant-resource-bounds` |
| mandatory-control preflight | the invocation, install, and update reject before the worker when a mandatory portable control cannot be applied | terminal rejection when an inventory or kernel-grade capability is absent | `script-fail-closed-capability-preflight` |

The script native-control inventory is exhaustive and normative per platform.
Its authority is the `native_control_inventory` section of
`conformance/v1/vectors/script-host-execution-policy.json`, version
`script-worker-v1-native-control-inventory-v1`:

| Control | macOS | Linux | Windows |
|---|---|---|---|
| `descendant-domain-termination` | available: process group and session teardown | available: process group and session teardown | available: Job Object kill-on-close |
| `active-process-count-limit` | unavailable: `no-private-aggregate-domain` | host-conditional: delegated cgroup v2 `pids.max` | available: Job Object active-process limit |
| `aggregate-memory-limit` | unavailable: `no-private-aggregate-domain` | host-conditional: delegated cgroup v2 `memory.max` | available: Job Object process and job memory limit |
| `per-file-size-limit` | available: `RLIMIT_FSIZE` | available: `RLIMIT_FSIZE` | unavailable: `no-private-aggregate-domain` |
| `inherited-handle-restriction` | available: close-on-exec plus explicit descriptor release | available: close-on-exec plus explicit descriptor release | available: explicit handle inheritance list |
| `descendant-exec-denial` | unavailable: `no-unprivileged-per-process-exec-policy` | host-conditional: Landlock execute right | unavailable: `child-process-policy-requires-appcontainer` |
| `filesystem-write-confinement` | unavailable: `no-unprivileged-filesystem-domain` | host-conditional: Landlock write rights | unavailable: `no-unprivileged-filesystem-domain` |
| `network-isolation-domain` | unavailable: `no-unprivileged-network-domain` | host-conditional: network namespace without interfaces | unavailable: `no-unprivileged-network-domain` |

This inventory is independent of `rc5-native-control-inventory-v1`. The first
five rows carry the same macOS and Windows verdicts because the underlying host
facts do not depend on what the child is, but they are copied, not referenced:
each inventory is versioned on its own, and a revision motivated by the build
policy MUST NOT re-scope script conformance, or the reverse.

A per-user resource limit is not a private aggregate domain. `RLIMIT_NPROC` and
`RLIMIT_AS` bound every process of the invoking user, not the descendants of
one invocation, so an unrelated process of that user consumes the budget and
the enforced command's descendants are not privately bounded. A manager MUST
NOT back `active-process-count-limit` or `aggregate-memory-limit` with either
limit on any platform, which is why both controls are `unavailable` on macOS
and `host-conditional` on a delegated cgroup v2 controller on Linux.
`RLIMIT_FSIZE` backs `per-file-size-limit` because that control bounds one file
write, not an aggregate over a domain.

`network-isolation-domain` is an inventory control, not a guarantee. It is not,
and MUST NOT be spelled as, `script-total-network-denial`, the guarantee this
policy defers, nor as `total-network-denial`, which names section 4.2.1's
build-policy guarantee and may not appear on a script surface at all. Applying
the control on a host that provides it does not license claiming any
policy-level guarantee: the mechanism is host-conditional, so the guarantee
stays deferred.

Availability has exactly three values in this inventory. `available` and
`unavailable` keep their section 4.2.1 meaning: a fixed normative per-platform
verdict that the probe confirms. `host-conditional` asserts that the platform
MAY provide the control and that the per-invocation probe decides, because the
mechanisms behind the host-conditional cells genuinely vary between hosts of one
platform and neither fixed verdict would be true of all of them. An `available`
control MUST report `applied`, an `unavailable` control MUST report
`unavailable`, and a `host-conditional` control MUST report `applied` or
`unavailable` exactly as probed. A `host-conditional` control that probes
unavailable MUST NOT reject the invocation.

The unavailable-reason vocabulary of this inventory is closed and contains
exactly `no-private-aggregate-domain`,
`no-unprivileged-per-process-exec-policy`,
`child-process-policy-requires-appcontainer`,
`no-unprivileged-filesystem-domain`, and `no-unprivileged-network-domain`.

A manager MUST apply exactly the controls this inventory marks available for
its platform, plus the `host-conditional` controls its probe finds present,
MUST NOT apply or report a control outside the inventory, and MUST NOT
substitute a host label for the availability probe. Availability MUST be probed
once per invocation before worker launch; a cached, inherited, or configured
result is not a probe, and an install-generation result replayed at invocation
time is a cached result. Adding, removing, or re-scoping an entry requires a new
inventory version. That is a specification revision, not an execution-policy
revision, because inventory membership never enters an installation input or
any hashed identity.

Host capability evidence is exactly one closed `script-capability-evidence-v1`
record per enforced-command invocation. The record contains exactly
`record_version`, `execution_policy`, `platform`, and `controls`.
`execution_policy` is `script-worker-v1`. `platform` is `linux`, `macos`, or
`windows`. `controls` contains exactly one entry per inventory control, and
each entry contains exactly `name`, `availability`, `status`, and `probed_at`.
`availability` is `available`, `host-conditional`, or `unavailable`, `status`
is `applied` or `unavailable`, and `probed_at` is `pre-worker-launch`.

The record describes the controls installed at launch, and that is a complete
statement for the whole session. A manager MUST NOT re-probe or emit a second
record while an enforced command runs, however long it runs, because controls
are installed before the interpreter starts and cannot change afterwards.

Each condition below is an error, not a permitted variation:

| Condition | Diagnostic |
|---|---|
| an `available` control reported with a status other than `applied` | `script_execution_capability_evidence_invalid` |
| an `unavailable` control reported with a status other than `unavailable` | `script_execution_capability_evidence_invalid` |
| a `host-conditional` control reported with a status the probe did not produce | `script_execution_capability_evidence_invalid` |
| a missing, duplicated, or extra control entry | `script_execution_capability_evidence_invalid` |
| an unknown `record_version` | `script_execution_capability_evidence_invalid` |
| availability not probed once per invocation before worker launch | `script_execution_capability_evidence_invalid` |
| more than one record for one invocation | `script_execution_capability_evidence_invalid` |
| a deferred guarantee of this policy named as a control entry | `script_execution_hardened_claim_forbidden` |
| a guarantee deferred by section 4.2.1 named as a control entry | `script_execution_hardened_claim_forbidden` |
| an `execution_policy` other than `script-worker-v1` | `script_execution_hardened_claim_forbidden` |

The record is result-only. It is exposed on manager result surfaces: invocation
plan, dry-run, and status results, plus an explicitly operator-selected
diagnostic destination that package data can never choose. It MUST NOT be
written to the command's standard output or standard error, because those
streams belong to the caller's pipeline, and it MUST NOT appear in a cache key,
receipt input, marker record, or claim. By default a manager retains at most the
most recent record per command; retention is machine-local and
operator-configurable, and a retained record MUST NOT become an input to any
hashed identity.

A `script-capability-evidence-v1` record and a `capability-evidence-v1` record
are separate closed objects. A manager MUST NOT emit either one in place of the
other, MUST NOT admit `script-worker-v1` into a `capability-evidence-v1`
record, and MUST NOT admit `manager-worker-v1` into a
`script-capability-evidence-v1` record.

This policy does not provide, and a conforming implementation MUST NOT claim,
these guarantees: `script-total-network-denial`;
`script-network-host-allowlisting`; `script-exact-executable-allowlisting`;
`script-private-runtime-area-only-writes`; `script-read-only-runtime-tree`;
`script-hard-aggregate-descendant-resource-bounds`; and
`script-fail-closed-capability-preflight`. They are reserved for a separately
named script execution policy backed by a verified provider under
[`assurance.md`](assurance.md), and none of the seven names may appear in the
mandatory-control set, the native-control inventory, or an evidence record. The
six guarantees deferred by section 4.2.1 are disjoint from these seven, describe
the build policy only, and may not appear on any script surface either.

There is exactly one portable failure boundary for control application. A
mandatory portable control that cannot be applied rejects the invocation with
`script_execution_control_unavailable` before the worker starts, and rejects
the install or update of the command's shim with the same diagnostic. An
unavailable inventory native control, a `host-conditional` control the probe
does not find, and the absence of any deferred guarantee MUST NOT reject an
enforced invocation, MUST NOT produce a diagnostic, and MUST NOT prevent the
command from running; the invocation MUST NOT record any of them as applied.

The policy-level diagnostic set of this policy is:

| Diagnostic | Condition |
|---|---|
| `script_execution_control_unavailable` | a mandatory portable control cannot be applied, at shim install or update and again before worker launch |
| `script_execution_capability_evidence_invalid` | an evidence record violates the closure rules above |
| `script_execution_hardened_claim_forbidden` | an evidence record names a deferred guarantee or a foreign execution policy |
| `script_execution_policy_unsupported` | a manager that does not implement `script-worker-v1` reads a command that selects it |

A manager that does not implement this policy MUST reject such a command with
`script_execution_policy_unsupported`. It MUST NOT install the command
declared-only, downgrade it, or ignore the field, because the resulting shim
would run package code the manifest says is contained.

Package-controlled bytes remain interpreter input only. They MUST NOT select or
modify the manager, worker, or interpreter executable, hidden mode, or
identity; any argument vector the manager builds, environment value, `PATH`
entry, working directory, or standard-stream binding; the applied controls,
limits, permitted roots, or probe result; the evidence record or any surface it
appears on; or the audit record, warning classes, or policy identity. A shebang
line, a file association, and a script byte are inert with respect to program
selection.

The execution policy identity is part of the skill's audit surface, so a
reviewer and a registry can tell an enforced command from a declared-only one.
Two audit warning classes are named by this policy and emitted on the surfaces
the manager profile defines: `script-command-declared-only`, for every script
command that has not opted in, including every script command of manifest
schema 7 and earlier; and `script-command-unfiltered-declared-network`, for an
enforced command whose declared `network` is a host-glob list.

Opting in is a breaking change for a skill whose capability declaration was
written as documentation. Under enforcement an undeclared environment variable
is absent, an undeclared executable does not resolve, and a write outside the
derived paths lands in the private runtime area or fails. A manager SHOULD
report that difference when the shim of an enforced command is installed or
updated, rather than leaving it to be discovered at first invocation.

A different execution contract requires a different execution-policy identity.
`script-worker-v1` and `manager-worker-v1` are separate closed identities that
never alias, are never substituted for one another, and are never widened in
place. Admitting a further script execution policy, a further interpreter
identifier, a further inventory control, or a further availability value is a
specification revision under section 12.3 with its own review, conformance
vectors, and record version.

### 4.2 Build roots and the closed `go-v1` command

`build_roots` and local `go-v1` commands exist in manifest schemas 6 and 7.
Schema 7 retains their exact schema-6 meaning so that local and external
commands may coexist. Every local build root MUST be a portable relative path
other than `.`, MUST name a real, link-free directory in the immutable raw
skill snapshot, and MUST be unique and pairwise disjoint. No local build root
may equal, contain, or be contained by a runtime root. Every declared local
build root MUST be referenced by at least one local build command.

A build command has exactly this package-controlled surface:

```json
{"type":"build","driver":"go-v1","source_dir":"build/cmd/tool"}
```

In manifest schemas 6 and 7 the object MUST contain exactly `type`, `driver`,
and `source_dir`. Manifest schema 8 adds exactly one OPTIONAL field, `modules`,
defined in section 4.2.3; an absent or empty list carries the schema-6 meaning
of this paragraph unchanged. The driver MUST be the closed identifier `go-v1`
in every schema. `source_dir` MUST be a real, link-free
directory below exactly one declared build root and MAY equal that root, but
MUST NOT be `.`. The containing build root is the command's `build_root`; it
MUST contain `go.mod` directly, and that file MUST be the nearest ancestor
`go.mod` of `source_dir`. An intervening module is invalid.

The manager MUST derive the artifact-relative path solely from the command
name: `bin/<command>` on Unix and `bin/<command>.exe` on Windows. A package
MUST NOT select a program, arguments, environment, tags, flags, toolchain,
output path, build script, hook, plugin, generator, or post-build action.
Unsupported drivers MUST fail without falling back to a system command or
generic build facility. Build commands participate in existing activation,
dependency-command selection, portable-name collision, shim collision, and
provider-first closure rules exactly like script commands. Within one closure
node, active build command names MUST be processed in Unicode-scalar lexical
order.

`go-v1` builds exactly one native executable from exactly one package named
`main`. The toolchain MUST be Go 1.23 or newer, operator-trusted rather than
package-selected, and from a release family tested by the manager against the
`go-v1` conformance vectors. The main module is the declared build root, and
section 4.2.3 changes neither that nor any rule of this section: it admits
first-party modules of the same package as a declared, manager-validated
surface, and every one of them still reaches the compiler only through the
vendor tree below the build root. Dependency
resolution is vendor-only and networkless, and workspaces, toolchain switching,
cross-compilation, PGO, cgo, package-controlled assembly, host objects,
generators, tests, plugins, overlays, external linking, and libgcc fallback are
forbidden.

The manager MUST run every source-aware Go command inside the fixed
`manager-worker-v1` process graph defined in section 4.2.1, never through a
shell or a joined command string. The package-independent bootstrap probes below
read no package byte and MAY run directly from the manager parent. Once per
operation the manager MUST use exactly these package-independent argument
vectors from a manager-owned empty directory as the working directory. Neither
the parent nor the worker MAY alter, extend, reorder, or repeat them. The
bootstrap environment MUST start empty except for
indispensable operating-system process variables; use operation-private user,
configuration, cache, and temporary roots; set `GOENV=off` and
`GOTOOLCHAIN=local`, `LC_ALL=C`, and `LANG=C`; and contain no inherited `GOROOT`
or target:

```text
go telemetry off
go version
go env -json GOROOT GOHOSTOS GOHOSTARCH GOOS GOARCH GO386 GOAMD64 GOARM GOARM64 GOMIPS GOMIPS64 GOPPC64 GORISCV64 GOWASM GOTELEMETRY GOTELEMETRYDIR
```

For every active build command, with canonical `source_dir` as the working
directory, it MUST then use exactly:

```text
go list -mod=vendor -deps -json -buildvcs=false -compiler=gc -pgo=off .
go build -mod=vendor -trimpath -buildvcs=false -buildmode=exe -compiler=gc -pgo=off -ldflags=-linkmode=internal -libgcc=none -o <manager-staging-artifact> .
```

Each line above denotes an argument vector; the `-ldflags=...` value is one
argument and the output is the manager-derived staging path. During bootstrap
and command builds, the environment MUST set `GOPATH`, `GOMODCACHE`, `GOCACHE`,
`GOTMPDIR`, `HOME`, `XDG_CONFIG_HOME`, `PATH`, and `TMPDIR` below
operation-private roots, with `PATH` naming a manager-owned empty directory. On
Windows, `APPDATA`, `LOCALAPPDATA`, `USERPROFILE`, `TEMP`, and `TMP` MUST also
be private.

After the bootstrap probes, the environment MUST retain the bootstrap settings
and add the resolved `GOROOT`, native `GOOS`/`GOARCH`, exactly one applicable
trusted tuning variable, and `GO111MODULE=on`, `GOFLAGS=` (empty), `GOPROXY=off`,
`GOSUMDB=off`, `GOPRIVATE=` (empty), `GONOPROXY=none`, `GONOSUMDB=none`,
`GOVCS=*:off`, `GOWORK=off`, `CGO_ENABLED=0`, `GO_EXTLINK_ENABLED=0`,
and `GOEXPERIMENT=` (empty). It MUST verify `GOTELEMETRY == "off"` and that
`GOTELEMETRYDIR` is below the operation-private platform configuration root.
Package or ambient Go, compiler, linker, and executable-search variables MUST
NOT be inherited.

The complete `go list` stream MUST contain exactly one non-`DepOnly` root with
`Name == "main"`, no incomplete result, and no `Error` or `DepsErrors`. Only a
result with both `Standard == true` and `Goroot == true` is a trusted toolchain
package; its directory and every listed input MUST stay below the fingerprinted
`GOROOT`. Every other result and its package directory, module file, active Go
file, and embedded input MUST stay below the command's build root. Every result
MUST have empty `SysoFiles`; every result with `Standard == false` MUST also
have empty `CgoFiles`, `CFiles`, `CXXFiles`, `MFiles`, `HFiles`, `FFiles`,
`SFiles`, `SwigFiles`, and `SwigCXXFiles`. Every active non-standard `GoFiles`
file MUST be a regular file below the build root and MUST NOT contain the exact
ASCII bytes
`//go:cgo_import_dynamic`. Section 4.2.3 extends the emptiness rules of this
paragraph and that exact-bytes scan to the active inputs of every declared
module directory. Any failure occurs before `go build`.

Below the worker, the manager and the worker MUST start no program other than
the fingerprinted `go` executable, which in turn runs fingerprinted regular
executables below `GOROOT/pkg/tool/<GOHOSTOS>_<GOHOSTARCH>/`. This is a
manager-selection and identity rule: the manager selects every program it starts
and verifies each identity before use. Neither the manager nor the worker MAY
write to the frozen source snapshot or to `GOROOT`, and both identities MUST be
re-verified after the last child exits; a change rejects the operation before
publication. Section 4.2.1 states exactly which portable mechanism carries each
of these rules and which stronger kernel-enforced guarantee is deferred. The one
output MUST be a bounded regular file inside manager staging. The manager MUST
hash it, set manager-defined executable permissions, and MUST NOT execute it for
validation, version discovery, smoke testing, post-processing, receipt
generation, rollback, or any other reason.

#### 4.2.1 Portable `manager-worker-v1` execution policy

Every source-aware `go-v1` and `go-repository-v1` operation runs under exactly
one named execution policy. Protocol 1.0 defines exactly one, the portable
`manager-worker-v1` policy, and every conforming manager MUST implement it on
macOS and Windows. The policy identity is a normative cache, receipt, marker,
and claim input. It is never a package-visible option, a host label, or an
operator preference.

The fixed process graph is:

```text
manager parent
  -> identity-verified manager-owned worker
       -> fingerprinted <GOROOT>/bin/go
            -> fingerprinted regular executables below
               <GOROOT>/pkg/tool/<GOHOSTOS>_<GOHOSTARCH>/
```

The worker is an exact re-execution of the installed manager executable in one
fixed hidden mode. It is an implementation boundary, not a user-visible command
surface and not a package-selected program. No package file, manifest value,
descriptor value, environment value, `PATH` lookup, shell, or user option
selects the worker executable or its mode. An implementation that cannot
distribute an equivalent identity-verified worker MUST state the exact
executable graph it verifies instead and MUST treat every mutable component of
that graph, including an interpreter and an installed package tree, as trusted
computing base.

One worker session performs exactly one `go list`, waits while the parent
validates the complete package graph, accepts exactly one authenticated build
permit, and performs exactly one `go build`. The session state machine admits
no retry, second list, second build, additional executable, shell, VCS, module
download, generator, test, run, or tool request; any other message tears the
session down without starting a compiler.

The following controls are mandatory on every supported host, and they are the
only controls whose absence rejects an operation:

- the fixed offline vendored-Go behavior, argument vectors, environment, and
  canonical working directories defined above;
- a fixed manager-selected process graph: within this execution boundary the
  manager and the worker start only the four nodes above and no other program;
- operation-private user, configuration, cache, temporary, staging, and output
  roots resolved independently of package data;
- a frozen source snapshot that neither the manager nor the worker writes to;
- a manager-derived artifact path, a bounded wall-clock deadline over the whole
  worker domain, bounded and redacted combined output, and one bounded regular
  artifact;
- closed standard input and release of unrelated descriptors or handles before
  Go starts;
- worker executable identity verification before launch, an in-session identity
  proof bound to a fresh session nonce, and re-verification of the worker,
  source-snapshot, and fingerprinted toolchain identities after the last child
  exits and before publication;
- termination and joining of the complete worker domain before the operation
  returns;
- application of every native control that the rc.5 native-control inventory
  below marks available for the host platform; and
- exactly one closed `capability-evidence-v1` record per operation.

A manager that cannot apply all of them MUST reject the build with
`build_execution_control_unavailable` before starting the worker or Go.

Each mandatory control is a manager-enforced mechanism, not a kernel-enforced
guarantee. This specification states both sides so that neither a reader nor an
implementation can mistake one for the other:

| Portable mechanism | What it means | What it does not mean | Deferred guarantee |
|---|---|---|---|
| `network: "none"` in the canonical policy | fixed offline Go module, proxy, checksum-database, and version-control configuration, and no manager-initiated or Go-initiated network access for dependency resolution or the build | kernel-enforced network denial for the worker domain or its descendants | `total-network-denial` |
| frozen snapshot plus identity re-verification | the manager freezes the validated snapshot, neither the manager nor the worker writes to it or to `GOROOT`, and a change to either before publication rejects the operation | kernel-enforced read-only presentation of the snapshot or toolchain to descendants | `read-only-source-and-toolchain` |
| fixed manager-selected graph plus identity verification | within this execution boundary the manager and worker start only the fixed four-node graph, package data cannot select or add a program, and every started program's identity is verified before and after execution | kernel-enforced allowlisting of the exact executable paths a descendant may run | `exact-executable-allowlisting` |
| manager-private roots plus artifact verification | every manager-directed write target is an operation-private root and the single artifact is verified in private staging before publication | kernel-enforced confinement of every descendant write to the private build roots | `private-build-root-only-writes` |
| parent-enforced deadline, output, and artifact bounds | the parent bounds wall-clock time, combined output, and artifact size over the worker domain and applies every available inventory control | hard aggregate process, memory, disk, time, and output bounds over every descendant | `hard-aggregate-descendant-resource-bounds` |
| mandatory-control preflight | the operation rejects before the worker when a mandatory portable control cannot be applied | terminal rejection before the worker when a hardened capability is absent | `fail-closed-capability-preflight` |

The rc.5 native-control inventory is exhaustive and normative per platform. Its
authority is the `native_control_inventory` section of
`conformance/v1/vectors/go-host-execution-policy.json`, version
`rc5-native-control-inventory-v1`:

| Control | macOS | Windows |
|---|---|---|
| `descendant-domain-termination` | available: process group and session teardown | available: Job Object kill-on-close |
| `active-process-count-limit` | unavailable: `no-private-aggregate-domain` | available: Job Object active-process limit |
| `aggregate-memory-limit` | unavailable: `no-private-aggregate-domain` | available: Job Object process and job memory limit |
| `per-file-size-limit` | available: `RLIMIT_FSIZE` | unavailable: `no-private-aggregate-domain` |
| `inherited-handle-restriction` | available: close-on-exec plus explicit descriptor release | available: explicit handle inheritance list |

A manager MUST apply exactly the controls the inventory marks available for its
platform, MUST NOT apply or report a control outside the inventory, and MUST NOT
substitute a host label for the availability probe. Availability MUST be probed
once per operation before worker launch; a cached, inherited, or configured
result is not a probe. Adding, removing, or re-scoping an entry requires a new
inventory version. That is a specification revision, not an execution-policy
revision, because inventory membership never enters a build input, an artifact,
or any hashed identity.

Host capability evidence is exactly one closed `capability-evidence-v1` record
per operation. The record contains exactly `record_version`, `execution_policy`,
`platform`, and `controls`. `platform` is `macos` or `windows`. `controls`
contains exactly one entry per inventory control, and each entry contains
exactly `name`, `availability`, `status`, and `probed_at`. `availability` is
`available` or `unavailable`, `status` is `applied` or `unavailable`, and
`probed_at` is `pre-worker-launch`.

Each condition below is an error, not a permitted variation:

| Condition | Diagnostic |
|---|---|
| an `available` control reported with a status other than `applied` | `build_execution_capability_evidence_invalid` |
| an `unavailable` control reported with a status other than `unavailable` | `build_execution_capability_evidence_invalid` |
| a missing, duplicated, or extra control entry | `build_execution_capability_evidence_invalid` |
| an unknown `record_version` | `build_execution_capability_evidence_invalid` |
| availability not probed once per operation before worker launch | `build_execution_capability_evidence_invalid` |
| a deferred hardened guarantee named as a control entry | `build_execution_hardened_claim_forbidden` |
| an `execution_policy` other than `manager-worker-v1` | `build_execution_hardened_claim_forbidden` |

The record is result-only. It is exposed in install, dry-run plan, and status
results, and it MUST NOT appear in a cache key, receipt input, marker record, or
claim, because portable cache identity is defined by the mandatory controls that
every conforming host applies.

The portable policy does not provide, and a conforming implementation MUST NOT
claim, these hardened guarantees: `total-network-denial`;
`read-only-source-and-toolchain`; `private-build-root-only-writes`;
`hard-aggregate-descendant-resource-bounds`; `exact-executable-allowlisting`;
and `fail-closed-capability-preflight`. They are specified separately by the
hardened execution profile tracked as `STORY-260728-327soo`. None of the six
names may appear in the mandatory-control set, the native-control inventory, or
a capability-evidence record.

There is exactly one portable failure boundary. A mandatory portable control
that cannot be applied rejects the operation with
`build_execution_control_unavailable` before the worker starts and publishes
nothing. An unavailable inventory native control, and the absence of any of the
six deferred hardened guarantees, MUST NOT reject a portable build, MUST NOT
produce a diagnostic, and MUST NOT prevent publication; a portable operation
MUST NOT record any of them as applied.

Package-controlled bytes remain compiler input only. They MUST NOT select or
modify the manager or worker executable, hidden mode, or identity; the Go or
tool executable paths; any argument vector, environment value, working
directory, build tag, or flag; the applied controls, limits, or permitted roots;
the worker protocol messages or the parent's build permit; the graph-validation
result, artifact verifier, cache key, receipt, marker, or claim; or any hook,
plugin, generator, post-build action, or publication step. Generator comments
and PGO paths remain inert input and MUST NOT cause a command to run.

A different execution contract requires a different execution-policy identity.
Because the identity is part of the canonical build input, a portable entry, a
pre-revision candidate entry, and a future hardened entry produce different
logical cache keys and cannot alias.

#### 4.2.2 Schema-7 external repositories and `go-repository-v1`

Manifest schema 7 MAY contain a strict top-level `build_repositories` map keyed
by portable identifiers. Each declaration MUST contain exactly `git` and
`locked_commit` and MAY contain `tag`. Every declaration MUST be selected by at
least one active or inactive command in the same manifest.

`locked_commit` MUST contain exactly `object_format` and `hex`.
`object_format` MUST be `sha1` or `sha256`; `hex` MUST be the complete lowercase
commit object ID for that format, respectively 40 or 64 hexadecimal
characters. A declaration MUST NOT use a branch, range, abbreviated ID,
`HEAD`, revision expression, or package-selected local path. When present,
`tag` MUST satisfy section 6.3 and is an assertion in addition to, not instead
of, the immutable full commit lock.

A schema-7 external build command has exactly this package-controlled surface:

```json
{
  "type": "build",
  "driver": "go-repository-v1",
  "repository": "golden-tools",
  "target": "golden-tool"
}
```

The command MUST select exactly one declared repository and exactly one target
from the effective commit's root `skill-build.json`. The command and
descriptor target drivers MUST both equal `go-repository-v1`. An unknown or
mismatched driver MUST fail before artifact-cache lookup or compiler execution
and MUST NOT fall back to `go-v1`, a script, a system command, or a generic
build facility.

The descriptor filename and location are fixed and manager-neutral:
`skill-build.json` at the repository root, and nothing else. A manager MUST NOT
read, accept, or search for a descriptor under any other filename, in any other
directory, or under an implementation-specific name, and MUST NOT treat any
such file as an alias for the descriptor.

`skill-build.json` schema 1 is strict and contains exactly
`schema_version: 1` and a non-empty `targets` map keyed by portable identifiers.
Each `go-repository-v1` target MUST contain exactly `driver`, `build_root`, and
`source_dir`. These paths are relative to the repository root. The single
value `.` MAY identify the repository root; every other value MUST be a
portable relative path. `source_dir` MUST equal or be below `build_root`.
`build_root` MUST contain `go.mod` directly, and that file MUST be the nearest
ancestor `go.mod` of `source_dir`.

Targets MAY share one build root. A nested module is admitted only when the
selected target names its root and satisfies the nearest-module rule. Managers
MUST NOT discover modules, targets, commands, or outputs. Every non-standard
module, package, source, embed, and vendor input selected by the fixed
`go list` graph MUST remain below the selected build root. Input MUST NOT come
from the consuming skill, another external repository, a sibling or parent
module, a host module cache, a workspace, or the network.

The whole external snapshot is validated, hashed, and audited. Only the
selected build root is compiler-visible, and no external repository byte is
agent-facing or runtime-copied. Allowing `build_root: "."` here MUST NOT change
the schema-6 prohibition on a local `build_root` equal to `.`.

The consuming manifest command key is the sole executable name. The manager
MUST derive `bin/<command>` on Unix or `bin/<command>.exe` on Windows and MUST
derive private staging and shim paths independently of package data. Neither
the command nor descriptor MAY select a program, argv, environment, flag,
compiler tag, toolchain, output name or path, install destination, alias, PATH
edit, signing identity, credential, hook, plugin, generator, build recipe,
post-build action, fallback, or secondary artifact.

`go-repository-v1` reuses the exact `go-v1` trusted toolchain identity, native
target, process vectors, environment, `manager-worker-v1` execution policy,
vendor-only dependency checks, compiler directive and native-input rejection,
internal-link policy, staging rules, resource controls, and
no-artifact-execution rule. It MUST NOT reinterpret or widen any of those rules. Its external acquisition, audit subject, receipt
schema, and marker state are distinct as defined in sections 6, 9, and 10.

#### 4.2.3 Schema-8 first-party module roots

Manifest schema 8 adds exactly one OPTIONAL field to the local `go-v1` build
command, `modules`: a unique list of portable relative directory paths naming
first-party Go modules of the same package that the command's build root
replaces. An absent or empty list is the default and keeps the exact schema-6
and schema-7 meaning of a single-module build root. Schemas 1 through 7 MUST
reject the field.

The field is declared per command. Commands sharing a build root read one
`go.mod` and are therefore forced by the bijection below into equal lists, so
the per-command spelling is ergonomic and carries no meaning a per-build-root
spelling would not.

The package states a claim and the manager checks it. The manager MUST NOT
read any replacement as an instruction, MUST NOT discover modules, targets, or
directories, and MUST NOT let package data select which directories it trusts.
Section 4.2's rule that a package selects no build input is unchanged: a
declared module directory is admitted only because the manager validated it
against the snapshot, never because a `go.mod` named it.

**Effective replace set.** For an active build command whose build root is `R`,
the effective replace set is every replacement directive materialized in
`R/vendor/modules.txt`, and that file is the only surface a manager reads to
determine it. It is a regular file below the build root and already an input to
`curator-build-source-v1` (section 8.1); the fixed `go list -mod=vendor`
invocation of section 4.2 reconciles it against `R/go.mod` and fails before
`go build` when the two disagree, so no replacement in `R/go.mod` can be absent
from it and none can hide from validation by going unused. The manager MUST NOT
parse `R/go.mod`, and MUST NOT treat `Module.Replace` in the `go list` stream
as the source of the set: those paths are derived lexically from `go.mod` text,
Go does not stat them, and under `-mod=vendor` the stream reports them
unchanged when the directory they name does not exist.

Within `R/vendor/modules.txt` only a line whose first two bytes are exactly
`# ` and which contains the exact ASCII bytes ` => ` is a replacement
annotation. The manager splits the line at its first such occurrence and
whitespace-tokenizes both sides after removing the `# ` prefix. Each side MUST
contain one or two tokens. The first token on each side is a module path or,
on the right, a directory path; a second token is a version.

Go writes a replacement annotation with one left-hand token for every
unversioned-left directive, including an unused directive. When that directive
selects a required module, Go also writes a selection annotation with the same
left module path and right replacement but with the selected version as the
second left-hand token. The one-token annotation is the effective directive;
the matching two-token annotation is only selection metadata and MUST NOT add
a second directive. A two-token-left annotation with no exactly matching
one-token-left annotation is a versioned-left directive and MUST be rejected.
This reconciliation is what enforces the rule that no version may appear on
the directive's left side without parsing `R/go.mod`.

**Admitted directive form.** Only directory form is admitted. Each effective
directive MUST have exactly one token on each side. A two-token right side is a
module-to-module redirect and MUST be rejected: it is a versioned dependency
decision, and versioned resolution stays vendor-only. The single right-hand
token MUST be a relative path that resolves, against `R`, to a declared module
directory of that command. Any other annotation shape MUST be rejected.

**Bijection.** For each active build command, its declared module directories
and its effective replacement directives MUST stand in one-to-one
correspondence. Every directive MUST resolve to a distinct declaration and
every declaration MUST be named by exactly one directive. A replacement naming
no declared directory MUST be rejected as undeclared; a declared directory
named by no replacement MUST be rejected as unused. This is the entire use a
manager makes of the replace records: they are checked against the declaration,
never obeyed. A command with an absent or empty `modules` list therefore MUST
have an empty effective replace set, which is the schema-6 and schema-7 rule
unchanged.

**Containment.** Each declared module directory MUST be a portable relative
path other than `.`, MUST name a real, link-free directory strictly inside the
immutable raw skill snapshot, and MUST contain `go.mod` directly. Declared
directories MUST be unique and pairwise disjoint, and MUST NOT equal, contain,
or be contained by any declared build root or any runtime root. Link-freeness
and platform path collisions already follow from snapshot validation in section
8.1; both are restated here because they are load-bearing. Beyond the
snapshot-wide rule, the disjointness comparisons of this paragraph MUST also
hold under the platform path mapping of section 2, so that two declared
directories, or a declared directory and a build or runtime root, that differ
only by case or by another platform folding are rejected even when only one of
them exists in the snapshot. The manager MUST validate every declared directory
against the snapshot itself and MUST NOT accept `Module.Replace.Dir` or
`Module.Replace.GoMod` as evidence that any path exists.

**Scan surface.** Declared module directories join the directive, cgo, and
assembly scan surface of section 4.2. The `SysoFiles` rule, the
cgo/C/C++/Objective-C/Fortran/SWIG and `SFiles` emptiness rules, and the
exact-bytes `//go:cgo_import_dynamic` scan apply to their active inputs exactly
as to the main module, both in the declared directory and in the vendor copy.
Any allowance a profile grants to audited third-party vendored code MUST NOT
extend to a module carrying a replacement.

**Vendor copy.** Under `-mod=vendor` the compiled bytes come from
`R/vendor`, not from the declared directory, and the two copies may differ
within one snapshot. The manager MUST NOT reconcile them and MUST NOT treat a
divergence as an error: the vendor copy is authoritative for the build, both
copies are regular files inside the validated snapshot, and both are already
bound by `curator-build-source-v1`. A declared module directory MAY itself
contain a `vendor/` tree; that tree takes no part in resolution under
`-mod=vendor` at `R`, and the manager MUST NOT read it as a dependency source.

**External dependencies and identity.** Module roots add no network, no proxy,
no module cache, and no workspace. Every result whose module carries no
replacement remains vendor-only and versioned, resolved strictly below
`R/vendor`, and `GOPROXY=off`, `GOWORK=off`, `GOFLAGS=` (empty), and
`CGO_ENABLED=0` are unaffected. Section 8.1 is unchanged: it already binds the
fully validated snapshot as a whole, so a declared module directory is already
an input to build-source identity and an edit below one already changes the
cache key. No algorithm, domain separator, framing, receipt identity, install
marker, or artifact-relative path changes.

**Failure boundary.** Declaration and containment validation MUST complete
before the fixed `go list`. Form and bijection validation MUST complete after
`go list` returns and before `go build`. Every failure of this section rejects
the operation before `go build`, leaving the installation, consumers, and
caches unchanged.

### 4.3 Capabilities

Schema 3 and later require `capabilities`. For every command that has not
selected an enforced execution policy, capabilities are an audit surface, not a
runtime sandbox:

- `network`: `"none"` or unique host globs without whitespace or path syntax;
- `filesystem`: `"repo"`, `"home-config"`, or unique portable paths;
- `exec`: `"none"` or unique bare executable names;
- `secrets`: `"none"` or unique non-empty secret identifiers;
- `env_read`: unique environment-variable names;
- `prompt_scope`: an OPTIONAL non-empty purpose statement.

Missing OPTIONAL capability fields take their schema defaults.

A script command that selects `script-worker-v1` reads the same declaration as
the containment profile of section 4.1.1. That policy derives its controls from
the declared manifest bytes and defines the meaning of every value it consumes;
the schema defaults above remain the declared-only reading.

### 4.4 Dependencies

`dependencies.commands` contains system requirements. Its legacy `type:
"skill"` form remains readable, does not create a shim, and produces a
migration warning.

Each `dependencies.skills` entry contains `git`, exact `ref`, activation
`mode`, and OPTIONAL command narrowing. `ref.kind` is `tag` or `revision`;
branch and range syntax is forbidden. `mode` is `full` (default), `runtime`, or
`context`. `commands` is valid only in runtime mode, is non-empty when present,
and names exported script or build commands of the provider. Build commands are
available only from schema 6 or 7 providers; schemas 1 through 5 retain
script-only command narrowing. Duplicates are rejected.

Each `dependencies.mcp_servers` entry requires a non-empty `hint`, MAY document
`transport` as `stdio` or `http`, and uses `required_in` `any` (default) or
`all`.

If neither modern manifest filename exists, `agents/runtime.json` MAY be read
as the legacy object `{ "commands": { <name>: <portable-relative-path> } }`.
Writers MUST NOT create this legacy form.

## 5. Project manifests

`Skillfile.json` conforms to `skillfile-v1.schema.json`. It contains unique
skill declarations and exactly one of `tag`, `branch`, or `revision` for each
skill. `source` is a portable relative path below the manager's configured
source root. `git` is used when the source repository is absent. Branches are
permitted only for direct project declarations and development substitutions.

`project.alias`, when present, is a non-empty, case-sensitive Unicode label of
at most 128 characters with no control characters. It is an operator-facing
matching label, not a filesystem identifier, and therefore MAY contain spaces.

Effective agents are selected in this order: manifest `agents`, registered
project agents, manager defaults. Effective locale is manifest `locale`, then
the manager preference.

`Skillfile.dev.json` conforms to `skillfile-dev-v1.schema.json` or
`skillfile-dev-v2.schema.json` and is never committed. Schema 1 retains only
skill substitutions and MUST reject external-repository substitutions. Schema
2 retains the `substitutions` map and MAY add the strict
`build_repository_substitutions` map. Its outer key is the consuming skill and
its inner key is a repository identifier declared by that skill.

Each skill substitution remains exactly one of a local Git checkout `path` or
`git` plus exact `ref`. Each external-repository substitution MUST be exactly
one operator project-relative local Git `path`, or `git` plus one structured
exact `ref` whose form is `revision`, `tag`, or `branch` under section 6.3.
Package data MUST NOT create or alter a substitution.

A local external substitution resolves relative to the canonical project root
and selects the repository's exact committed `HEAD`; dirty, staged, and
untracked worktree bytes MUST NOT become compiler input. A substitution
replaces acquisition only. It MUST NOT change repository identifier,
descriptor target, driver, command name, output, compiler policy, credential,
or signer. Strict audit MUST reject any substitution. Advisory audit MUST
report it and audit the exact effective snapshot. Substitution state MUST be
recorded in marker v3; removal, path/ref change, or selected-commit movement
MUST make the installation non-current.

## 6. Sources and snapshots

### 6.1 Canonical source identity

Network git sources use a canonical identity. Local paths and `file:` URLs have
no network identity. Protocol 1.0 network URLs:

- use `ssh`, `git`, `http`, or `https`, or SCP form `[user@]host:path`;
- have an ASCII host matching `[A-Za-z0-9][A-Za-z0-9.-]*`;
- contain no explicit port, password, query, fragment, percent escape, or
  backslash;
- contain a non-empty portable repository path with no whitespace, `%`, `?`,
  or `#` character;
- produce a canonical `host/path` identity of at most 4096 Unicode scalar
  values.

Canonicalization lowercases the host, removes user and transport, trims outer
path slashes, and removes one case-sensitive trailing `.git`. Repository path
case is preserved. The result is `host/path`. Invalid network forms MUST be
rejected, not treated as local.

Allowlist matching is segment-aware: identity `h/a/b` matches prefix `h/a` but
not `h/a-evil`. An empty allowlist permits all network identities. Local
sources bypass the network allowlist.

### 6.2 Git safety and references

Git execution MUST restrict protocols to `file`, `git`, `http`, `https`, and
`ssh`; refuse empty or dash-prefixed operands; and place untrusted operands
after `--` where supported. Submodules are unsupported. Archive extraction
MUST reject symbolic links, hard links, path escapes, duplicate platform paths,
and entries exceeding implementation limits documented by the manager.

Tags resolve `refs/tags/<value>^{commit}`. Revisions resolve
`<value>^{commit}`. Branches prefer `refs/remotes/origin/<value>` and then a
local head. A resolved commit is the full lowercase hexadecimal object id
returned by git. Snapshots are immutable regular-file trees produced from that
commit.

### 6.3 External repository source identity and exact lock

Schema-7 external repository declarations use section 6.1 canonicalization
narrowed to HTTPS and SSH. HTTPS MUST have the form `https://host/path` and
MUST NOT contain userinfo. SSH MUST have the URI form
`ssh://[user@]host/path` or SCP form `[user@]host:path`. An SSH username is
OPTIONAL ASCII matching `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` and is removed from
canonical identity. A declaration MUST NOT use `git`, `http`, `file`, another
scheme, an explicit port, password, query, fragment, percent escape, backslash,
empty path component, `.` component, or `..` component.

The host is lowercased and the path case is preserved. Leading and trailing
slashes and exactly one case-sensitive trailing `.git` are removed. The
canonical identity is `host/path` and MUST be no more than 4096 Unicode scalar
values. Transport spelling and a permitted SSH username do not change that
identity. The declared source records identity kind `network-git`, this value,
and transport `https` or `ssh`.

For `go-repository-v1`, the raw SSH repository path is further restricted to
ASCII letters, digits, `.`, `_`, `-`, and `/`, with the same non-empty,
no-`.`/no-`..` component rules. A quote, whitespace, shell metacharacter,
escape, or non-ASCII byte MUST be rejected before Git or SSH starts. HTTPS
retains the Unicode path grammar above.

A manifest tag is the name below `refs/tags/` and MUST:

1. encode to 1 through 255 UTF-8 bytes of valid Unicode scalar text;
2. neither start nor end with `/`, contain `//`, nor end with `.`;
3. have no slash component starting with `.` or ending with `.lock`;
4. contain none of NUL, ASCII control bytes, DEL, space, `~`, `^`, `:`, `?`,
   `*`, `[`, or backslash;
5. contain neither `..` nor `@{` and not equal the single character `@`; and
6. make the exact constructed `refs/tags/<tag>` pass the protocol equivalent
   of `git check-ref-format` without normalization.

A manager MUST NOT pass a tag as an unqualified revision expression. An
operator network substitution MUST use exactly one structured ref:

- `revision` is a full lowercase object ID for the effective repository object
  format;
- `tag` uses the grammar above and selects only `refs/tags/<value>`; or
- `branch` uses the same safe-name grammar and selects only
  `refs/heads/<value>`.

No other revision, peel, reflog, range, or normalization syntax is admitted.

The declared full object ID is immutable. For an unsubstituted source, its
object format MUST equal the effective repository format and the named object
MUST be a commit. A substitution MUST leave that declaration unchanged while
effective state records the substituted repository's actual object format and
full commit. An unsubstituted declaration without `tag` MUST acquire only
`<full-locked-oid>:refs/curator/locked`. An unsubstituted declaration with
`tag` MUST instead acquire only
`refs/tags/<tag>:refs/curator/tag` in fresh operation-private state, recompute
and peel the exact lightweight or annotated-tag chain, and require its terminal
commit to equal the full lock. Server policy permitting or denying direct
object-ID wants MUST NOT alter the tagged flow.

The outer annotated tag object MUST name the requested tag. Every annotated
tag target MUST be exactly `tag` or `commit`; the actual object type MUST match
that declaration. The chain MUST contain no repeated full object ID and no more
than 16 annotated tag objects. A lightweight tag MUST point directly to a
commit. The selected commit, tag records, and terminal object IDs MUST be
recomputed under the declared object format before equality is accepted.

The manager MUST NOT fall back between the tagged and untagged forms or to a
branch, all-tags fetch, alternate ref, configured refspec, named remote, clone,
checkout, archive, or source-selected fetch behavior. A tag terminating at a
different commit MUST fail `build_repository_ref_moved`. A missing,
inaccessible, or transport-unavailable exact source MUST fail
`build_repository_source_unavailable`. Malformed or incomplete fetched objects
retain the semantic failures in section 6.5 rather than being reported as a
successful source or cache result.

### 6.4 Declared and effective external source state

Every `go-repository-v1` command binds a declared state and an effective state.
Declared state MUST contain:

- the repository identifier;
- canonical `network-git` identity and transport;
- immutable `locked_commit.object_format` and full `locked_commit.hex`; and
- OPTIONAL tag.

Effective state MUST contain:

- exact identity kind and value;
- transport when the source is network Git;
- actual object format and full commit;
- Boolean `substituted`;
- typed substitution state when substituted; and
- `curator-build-source-v1` over the exact frozen snapshot.

For an unsubstituted source, `substituted` MUST be false, substitution data
MUST be absent, effective identity MUST equal declared identity, and effective
object format and commit MUST equal the lock. For a substitution, `substituted`
MUST be true and substitution type MUST be exactly `local-path` or
`network-git`; declared state MUST remain present and unchanged.

A network substitution uses the same canonical identity and transport grammar
and records its structured exact ref. A local substitution has identity kind
`operator-local-git`, no transport, and value:

```text
"sha256:" || lowercase_hex(SHA-256(CCJ-1({
  "algorithm": "curator-operator-local-git-v1",
  "project": <canonical-project-identity>,
  "selector": <normalized-project-relative-selector>
})))
```

The selector uses `/`, preserves Unicode scalar values without normalization,
removes `.` components, cancels an ordinary component followed by `..`,
preserves unmatched leading `..`, and contains no empty component. This
identity MUST NOT expose an absolute host path and MUST NOT be treated as a
network source identity or authorization token. The actual object format, full
commit, and build-source digest remain authoritative for the selected bytes.

### 6.5 Raw-object snapshot and audit equivalence

The external repository object database and its configuration are untrusted
transport input. A manager MUST use an operator-trusted, fingerprinted Git
release family, manager-owned private repository/configuration, clean
environment, fixed child graph, transport and credential policy, and bounded
raw-object reader. Repository data MUST NOT select or extend Git, SSH, HTTPS,
credential, proxy, helper, upload-pack, hook, filter, LFS, submodule,
alternate, replace, graft, checkout, archive, maintenance, or lazy-fetch
behavior.

HTTPS MUST verify TLS and MUST NOT follow redirects. SSH MUST use an operator-
trusted OpenSSH executable, operator-selected known-hosts and authentication
state, an empty manager-owned SSH configuration, and a manager-owned wrapper
that accepts only the validated destination and exact upload-pack command.
Package and repository data MUST NOT select a credential, identity file, agent,
known-hosts file, proxy/jump/local command, timeout, forwarding, control
socket, TTY, SSH option, or remote command.

Network acquisition and a local substitution MUST converge on the same
manager-owned raw-object proof. Local-substitution v1 MUST admit only an
ordinary non-bare files-ref worktree with a link-free `.git` directory directly
below its root. It MUST parse admitted configuration, refs, loose objects, and
paired pack versions 2 or 3 with index version 2 as bounded data and MUST NOT
execute source-repository Git behavior. Gitfiles, linked worktrees, bare
repositories, reftable or unknown extensions, unsafe links/layout, unsupported
pack/index state, alternates, promisor state, grafts, replace refs, shallow
state, optional pack sidecars, and incomplete object stores MUST fail closed.

The manager MUST independently recompute the object-format hash of every
consumed commit, tag, tree, and blob. It MUST parse the selected commit and
bounded annotated-tag chain with one byte grammar on both source paths, prove
the complete reachable tree/blob graph, reject links, gitlinks, special files,
invalid or colliding paths, and reject every non-empty blob below 1024 bytes
accepted by parser family `git-lfs-pointer-parser-v3.7.1`. It MUST NOT hydrate
LFS data or obtain a missing object from the network, an alternate, a checkout,
or a filter.

Known failures MUST remain distinguishable:

- unsafe local gitfile, bare, linked-worktree, layout, format, and object-
  container cases use respectively
  `build_repository_local_gitfile_unsupported`,
  `build_repository_local_bare_unsupported`,
  `build_repository_local_linked_worktree_unsupported`,
  `build_repository_local_layout_unsafe`,
  `build_repository_local_format_unsupported`, and
  `build_repository_local_object_format_unsupported`;
- missing or incomplete graph bytes use
  `build_repository_incomplete_source`;
- malformed commit/tag semantics use
  `build_repository_git_object_semantics_invalid`; and
- a matched Git LFS pointer uses `build_repository_git_lfs_unsupported`.

After raw-object proof, the manager MUST materialize exact blob bytes as one
immutable regular-file snapshot. Before audit success, artifact-cache lookup,
or a compiler child, it MUST validate the complete snapshot, compute
`curator-build-source-v1`, parse the root descriptor, validate the selected
target and module containment, and freeze that snapshot instance until the last
build child exits.

The consuming skill and each effective external repository are independent
audit subjects. The external audit subject MUST bind declared and effective
identity, object format, full effective commit, complete build-source digest,
descriptor target, substitution state, and successful exact-tag assertion when
applicable. Skill evidence MUST NOT attest an external repository; external
evidence MUST NOT attest the skill. Audit-cache reuse requires exact subject
tuple equality and MUST NOT bypass snapshot admission.

For each external repository subject, the manager MUST independently apply the
applicable allowlist, revocation, registry, tag-lock, and audit-policy gates.
Every applicable gate MUST succeed before artifact-cache lookup or compiler
work. A gate decision for the consuming skill or another external repository
MUST NOT be reused for that subject.

This ordering applies to real builds, claimed cache hits, and dry-runs that
claim source or audit coverage. A syntax-only offline operation that cannot
obtain the exact snapshot MAY warn `build_repository_unverified_offline`, but
MUST NOT claim source, audit, cache, receipt, marker, artifact, or installation
coverage. Install, update, repair, and coverage-claiming audit MUST fail
`build_repository_source_unavailable` before mutation when the exact source
cannot be obtained and audited.

## 7. Closure resolution

Direct declarations enter as `full` requirements from synthetic consumer
`<project>`. Processing a provider adds its skill requirements.

Within one closure, one skill name MUST resolve to exactly one commit and one
canonical source identity. Different identities or commits fail with every
relevant requirement chain. Different refs resolving to one commit unify.
Cycles fail and name the cycle.

Activation is edge-based. Context is active when any incoming edge is `full`
or `context`. All commands are active when any edge is `full`; otherwise the
active set is the union of runtime edges, narrowed where requested.

Provider order is deterministic Kahn topological order: among currently ready
providers select the lexicographically smallest skill name by Unicode scalar
value. The synthetic project is not emitted. Diagnostic requirement chains use
the lexicographically smallest complete chain when multiple chains are equal
in length.

## 8. Content hashes

The Curator content hash is calculated over regular files excluding the marker
itself. For each selected file in sorted protocol-path order, append:

```text
UTF8(path) || 0x00 || file_bytes
```

Join adjacent records with one additional `0x00`, hash the resulting byte
string with SHA-256, encode lowercase hexadecimal, and prefix `sha256:`. The
empty tree hashes the empty byte string. File mode, owner, timestamp, and
filesystem-native separator are not hashed. Readers MUST reject duplicate
protocol paths rather than hash one arbitrarily.

### 8.1 Build-source identity

The installed-tree `content_sha256` above remains unchanged and excludes root
`.csk-install.json`. It MUST NOT be used as compiled-artifact identity.
Compiled commands instead bind the fully validated immutable raw snapshot with
algorithm identifier `curator-build-source-v1`, including every regular file
and any package-provided root `.csk-install.json`.

Snapshot validation MUST occur before build-cache lookup or any Go command.
The snapshot MUST contain only directories and regular files; links, special
files, invalid protocol paths, duplicate encoded paths, and platform path
collisions are invalid. For every regular file, convert its relative protocol
path to `/` separators without normalization and encode its Unicode scalar
values as UTF-8. Sort those path bytes in unsigned bytewise order. Initialize
SHA-256 with exact ASCII `curator-build-source-v1` followed by `0x00`, then
append for each file:

```text
ASCII("F") || uint64be(path_byte_length) || path_utf8 ||
uint64be(content_byte_length) || file_bytes
```

Prefix the lowercase digest with `sha256:`. An empty snapshot hashes only the
domain prefix. Modes, ownership, timestamps, ACLs, and extended attributes are
not inputs. Implementations MUST NOT substitute the marker-excluding content
hash with an empty exclusion list: `curator-build-source-v1` is domain-separated
and length-framed. The validated snapshot instance MUST remain byte-for-byte
unchanged until the last build child exits.

This section is unchanged by section 4.2.3. A declared first-party module
directory is inside the snapshot, so its files and the vendor copies made from
them are already covered here; there is no separate module-root identity and no
additional input.

### 8.2 Go toolchain identity

Every `go-v1` cache input MUST identify its operator-trusted toolchain with
algorithm `curator-go-toolchain-v1`. Resolve the launcher independently of the
package and before entering a package-controlled directory. It MAY be bundled
with the manager or selected by trusted operator configuration, but MUST NOT
come from the repository, a runtime root, project `.agents/bin`, the user
`PATH`, or a manifest. Require it to be the regular executable
`<resolved-goroot>/bin/go` (`bin/go.exe` on Windows), not a wrapper or an
executable outside the tree being fingerprinted. A clean `go env GOROOT` probe
MUST resolve to the same root.

Walk `GOROOT` without following links; the root itself is not a record.
Relative components MUST contain valid
Unicode scalar values; encode `/`-joined paths as UTF-8 without case folding or
normalization and reject duplicate encoded paths and special files. Symlinks
MUST be relative, non-dangling, and resolve within `GOROOT`, and their referents
have independent tree records. Sort path bytes in unsigned bytewise order.
Initialize SHA-256 with exact ASCII
`curator-go-toolchain-v1` followed by `0x00`, then append for every entry:

```text
kind || uint64be(path_byte_length) || path_utf8 ||
uint64be(payload_byte_length) || payload
```

`kind` is ASCII `D`, `F`, or `L`; directory payload is empty, file payload is
the exact bytes, and link payload is the exact UTF-8 `readlink` value. Hard
links are independent regular-file records. Normalize `go version` stdout by
requiring at most 4096 bytes and exactly one terminal LF, optionally preceded
by CR, removing that terminator, and rejecting any other CR, LF, NUL, empty, or
invalid UTF-8 content. Append it as one final `V` record with an empty path.
Prefix the lowercase digest with `sha256:`.

Permissions, ownership, timestamps, ACLs, and extended attributes are not hash
inputs, but `bin/go` and each invoked `pkg/tool/<host>/` child MUST be regular
and executable at use time. The tree MUST remain unchanged through the last
child exit. The logical identity also records normalized `go_version` and
`go_relpath: "bin/go"`; toolchain location is not portable identity.

## 9. Compiled-artifact cache and receipts

### 9.1 Local `go-v1` receipt schema 1

For each active local `go-v1` command, the manager MUST construct a logical
build-input object containing schema version 1, driver, the
`curator-build-source-v1` identity, `build_root`, command name, `source_dir`,
native GOOS/GOARCH/tuning, the complete `curator-go-toolchain-v1` identity, and
these fixed policy values:

```json
{
  "module_mode": "vendor",
  "network": "none",
  "workspace": false,
  "cgo": false,
  "compiler_directives": "reject-nonstandard-cgo-import-dynamic-v1",
  "target_mode": "native",
  "link_mode": "internal",
  "libgcc": "none",
  "package_assembly": false,
  "host_objects": false,
  "telemetry": "off-private",
  "execution_policy": "manager-worker-v1"
}
```

`execution_policy` is the exact execution-policy identity of section 4.2.1. It
is REQUIRED, closed to the single portable value in protocol 1.0, and never
derived from a host label, a capability probe, or package data.

`network: "none"` denotes the fixed offline Go module, proxy, checksum-database,
and version-control configuration of section 4.2 and the absence of any
manager-initiated or Go-initiated network access for dependency resolution or
the build. It is not a claim of kernel-enforced network denial for the worker
domain; that guarantee is `total-network-denial`, deferred by section 4.2.1.

The logical cache key is:

```text
"sha256:" || lowercase_hex(SHA-256(CCJ-1(build_input)))
```

`CCJ-1` is defined in `registry.md`; the input bytes have no BOM,
insignificant whitespace, or terminal LF. A semantic change to arguments,
environment, execution policy, receipt interpretation, or output rules requires
a new driver identifier or an explicit versioned execution-policy revision.
Because `execution_policy` is inside the hashed input, an entry produced under a
different execution contract, or under a pre-revision input that carried no
execution policy at all, MUST miss and MUST NOT alias a portable entry. Host
capability evidence is not an input: a manager MUST NOT add it to, or infer it
from, the cache key.

Each immutable logical entry contains exactly one manager-derived artifact and
a strict build-receipt schema 1. The receipt contains its schema version,
logical cache key, the complete build input, and artifact-relative path,
SHA-256, and byte length. Stored receipt bytes MUST equal `CCJ-1(receipt)`
exactly. Define:

```text
receipt_sha256 = "sha256:" ||
  lowercase_hex(SHA-256(exact_stored_receipt_bytes))
```

### 9.2 External `go-repository-v1` receipt schema 2

Each active external command MUST use build receipt schema 2. Its logical input
MUST contain:

- schema version 2 and driver `go-repository-v1`;
- repository identifier;
- declared canonical `network-git` identity, transport, immutable object
  format and full commit lock, and OPTIONAL tag;
- effective identity, transport when applicable, actual object format, full
  commit, `substituted`, typed substitution when present, and external
  `curator-build-source-v1`;
- descriptor path fixed to `skill-build.json` and selected target;
- consuming manifest command name, selected `build_root`, and `source_dir`;
- native GOOS/GOARCH/tuning and complete `curator-go-toolchain-v1`; and
- the policy object from section 9.1 plus
  `"source_kind":"locked-external-git-v1"`.

For `substituted: false`, effective identity, object format, and commit MUST
equal the declaration and substitution MUST be absent. If declared `tag` is
present, a writer MUST NOT publish the receipt until that same operation's
exact-tag acquisition and manager-computed peel prove the terminal commit
equals the full lock. The receipt MUST NOT contain a self-asserted trust,
provenance, or `tag_verified` field.

For `substituted: true`, the declaration remains unchanged and effective source
MUST describe the actual compiled snapshot. A local substitution uses identity
kind `operator-local-git` and substitution `{"type":"local-path"}`. A network
substitution uses identity kind `network-git`, canonical value and transport,
and typed structured ref. A substituted receipt's declared tag is declaration
history only; it MUST NOT claim that the declared remote tag was consulted.

The schema-2 logical cache key is the SHA-256 of `CCJ-1` over the complete
input. The receipt MUST retain the complete input, logical cache key,
manager-derived artifact-relative path, artifact SHA-256, and byte length in
exact canonical bytes. Different declared or effective states, substitutions,
commands, targets, build roots, source directories, native targets,
toolchains, execution policies, or policy revisions MUST NOT alias.

### 9.3 Shared protected-cache rules

Before reuse, a reader MUST open a regular singly linked receipt without
following links, recanonicalize it, and require exact stored-byte equality;
recompute the build-source identity before opening the candidate; recompute and
match the cache key and entire expected input; require the manager-derived
artifact path; and open, bound, hash, and size-check one regular singly linked
artifact without following links. It MUST reject unknown receipt fields or
unsupported receipt, driver, build-source, toolchain, and execution-policy
identities. An entry whose execution policy is absent or is not the policy the
reader implements is a miss, never a hit and never an upgrade. It MUST NOT
execute the artifact.

Persistent reuse is permitted only below manager-created, manager-protected
state resolved independently of package input. On every lookup and again under
the manager-home mutation lock, the manager MUST verify ownership, private
mutation permissions or DACL, containment, regular file types, and link safety
for the boundary and every entry component. An implementation that cannot
prove this boundary MUST disable persistent reuse. A real operation rebuilds
from the revalidated snapshot into newly established protected state; dry-run
reports `would-rebuild-untrusted-cache`; status is non-current. Self-consistent
receipt, marker, artifact, and hash bytes do not repair or authenticate an
untrusted boundary.

The logical cache key, exact receipt bytes, artifact-relative path, artifact
bytes/hash/size, and validation outcomes are portable. Manager-home paths,
physical cache-root and driver-directory names, receipt filenames, lock and
quarantine names, and storage backends are implementation-specific. A manager
MUST NOT infer portable paths such as `build-cache` or `.csk-build.json`.

Entries are built in operation-private staging and published atomically and
immutably under the manager-home mutation lock. Receipt hashes are deterministic
corruption/currentness identifiers, not signatures, MACs, attestations, or
proof of provenance. The verified protected-state boundary supplies local
provenance; registry audit continues to attest source, not the compiled result.

Dry-run MAY obtain, raw-validate, hash, and independently audit an exact
external snapshot in removable operation-private state; MAY validate local
build-root exclusion; MAY compute build-source and trusted-toolchain identity;
and MAY inspect protected entries read-only. It MUST NOT invoke `go list`,
`go build`, a compiler, or linker; create Go caches, a persistent repository or
snapshot, an audit response, a persistent memo, a mutation lock or journal;
publish, quarantine, repair, or adopt cache bytes; copy build roots into
runtime; or mutate installation, registry, audit, marker, shim, adapter, or
consumer state. A claimed external cache hit requires exact snapshot admission
and audit first. After exact receipt validation, a hit MUST NOT run
source-aware Go commands.

### 9.4 Protected external snapshots, deduplication, and garbage collection

The protected external snapshot key MUST contain the complete effective
identity kind and value, effective object format, full effective commit, and
external `curator-build-source-v1` digest. A manager MUST NOT omit, replace, or
infer any key component from declared state. Snapshot bytes MAY be
deduplicated only when every component of the complete snapshot key is equal.
Audit decisions remain subject-specific and MUST NOT be deduplicated or reused
merely because snapshot bytes or snapshot keys are equal.

Garbage collection MUST revalidate protected-cache boundaries and run under
the manager-home mutation lock. Its live roots MUST include:

- for every valid marker-v3 installation, each referenced local skill raw
  snapshot, complete external frozen-snapshot key, receipt/artifact key, and
  manager-generated shim relationship;
- every staged or published snapshot and artifact referenced by an in-flight
  transaction journal and needed for commit or rollback; and
- the roots already defined for valid marker-v1 and marker-v2 installations,
  whose behavior remains unchanged.

Receipt content alone MUST NOT make an entry live. If a marker or journal is
unreadable, or a cache boundary or possible reference cannot be proved,
garbage collection MUST conservatively retain the uncertain entries. It MUST
NOT execute or adopt source or artifact bytes, repair permissions to make
candidate bytes trusted, or infer liveness from an unvalidated partial record.
Physical snapshot, artifact, receipt, lock, journal, quarantine, and
garbage-collection paths remain implementation-specific.

## 10. Install markers

Every installed closure node has `.csk-install.json`. Managers supporting
schema 7 MUST read marker schemas 1, 2, and 3, and managers supporting schema 8
MUST read marker schemas 1, 2, 3, and 4. They MUST write marker schema 2 for
schema 1 through 6 installation mutations, marker schema 3 for schema 7
installation mutations, and marker schema 4 for schema 8 installation
mutations. They MAY continue to regard a valid marker-v1 installation as
current for a schema 1 through 5 package. Marker v1 and v2 retain their
existing shapes and meanings.

Marker v2 permits `skill_schema_version` through 6 and requires sorted
`build_roots` and a `builds` object, including empty values for installations
without active compiled commands. `build_source` is REQUIRED exactly when
`builds` is non-empty and MUST otherwise be absent. Each lexically ordered
`builds` entry records driver, logical cache key, `receipt_sha256`, artifact
SHA-256, and manager-derived artifact-relative path. A build shim MUST target
the immutable cache artifact selected by the effective plan and marker, not the
commit-keyed script runtime store.

Marker v3 permits `skill_schema_version` through 7 and represents local-only,
external-only, and mixed command sets. Every build entry MUST explicitly record
its receipt schema version and its `execution_policy`, which MUST equal the
execution-policy identity inside the referenced receipt input. A local `go-v1`
entry MUST retain receipt schema 1 and marker-v2 entry semantics. An external
`go-repository-v1` entry MUST record receipt schema 2, repository identifier,
declared identity and immutable lock, OPTIONAL declared tag, effective identity,
object format and full commit, substitution state, external build-source
identity, descriptor target, logical cache key, `receipt_sha256`, artifact hash,
and manager-derived artifact path. No receipt or marker field MAY be inferred
from a driver name.

A marker-v2 build record keeps its frozen schema-6 shape and does not gain an
`execution_policy` member. Its execution-policy binding is transitive and
complete: the recorded logical cache key and `receipt_sha256` are computed over
an input that contains the execution-policy identity, so a record written under
one execution contract can never validate against another.

Marker-v3 top-level `build_source` and `build_roots` retain schema-6 meaning.
Top-level `build_source` is REQUIRED exactly when at least one active local
`go-v1` command exists and MUST otherwise be absent. External-only
installations MUST bind source per external build entry and MUST NOT use the
consuming skill's raw snapshot as external compiled-source identity. An
unsubstituted external entry MAY record declared tag only from a receipt whose
producing operation completed exact-tag acquisition and equality with the
immutable lock; the marker field is not independent proof.

Marker v4 permits `skill_schema_version` 8 and otherwise carries marker-v3
meaning unchanged. It has the same object shape, the same requirement that
every build entry explicitly record its receipt schema version and its
`execution_policy`, the same local `go-v1` and external `go-repository-v1`
entry semantics, and the same top-level `build_source` and `build_roots`
rules. An enforced `script-worker-v1` script command produces no build entry
and adds no marker member: schema 8 changes which manifests a marker may
describe, not what a marker records. Markers v1, v2, and v3 keep their frozen
shapes and their existing manifest-version bands, so a schema-8 installation
is recorded by marker v4 alone.

`locale` is always present and is a string or `null`. Required set-like arrays
are always arrays, including when empty.

The following arrays are set-like and writers sort them by Unicode scalar
value: `agents`, `commands`, `dependencies`, `files`, `requirements`,
`runtime_roots`, `build_roots`, `requirers`, `activation.commands`, and every
MCP agent list. Writers sort `builds` keys by Unicode scalar value. Object
member order and whitespace are not significant.

An installation is current only when the marker schema is supported; ref kind,
ref, commit, locale, agents, activation, substitution, MCP findings, and
attestation match the effective plan; and the installed content hash matches
`content_sha256`. For a build-enabled marker, currentness additionally requires
the declared local build roots and static context exclusion to match; every
available local or external protected snapshot's `build_source` to match the
effective plan and receipt input; each logical cache key and manager-derived
artifact path to match; and every referenced receipt/artifact to validate below
a currently verified manager-protected boundary.

For external entries, currentness also requires declared and effective
identity, object format, full commit, target, and substitution state to match;
the external snapshot boundary and source digest to validate; the receipt
schema-2 relationship to match exactly; and the shim to select the protected
artifact. A missing snapshot that the marker claims should exist, prompt-
visible or runtime-copied external source, untrusted boundary, corrupt
receipt/artifact, wrong target or toolchain, or mismatched source identity
makes the installation non-current. When existing profile semantics
distinguish unavailable evidence from drift, a result MAY be `unknown`, but it
MUST NOT be current and a checking status command MUST return non-zero.

Read-only status MUST NOT contact an external remote merely to retest tag
availability or movement. It MUST NOT fetch, repair, quarantine, alter
permissions, refresh a snapshot, invoke a compiler or signer, or execute an
artifact. Repair MUST reacquire, validate, and audit the exact effective source
and MUST rebuild into protected state rather than adopt candidate bytes. For an
unsubstituted tagged source, repair MUST use only the exact-tag path and MUST
reprove terminal equality with the immutable lock; an old snapshot or direct-
object fetch MUST NOT substitute for that assertion.

Unsupported or unreadable markers are not current. A moved tag is a warning,
or an error under strict-tag policy.

The marker and its referenced receipt hashes are not signatures and MUST NOT be
used as authorization tokens or provenance proof. Registry attestations are
reverified from signed records when fresh trust is required.

## 11. Adapter ledger

Every managed adapter root contains `.csk-managed.json` conforming to
`adapter-ledger-v1.schema.json`. `entries` is a sorted unique list of skill
names owned by the manager in that adapter root. A manager MUST remove only
entries in its preceding ledger and MUST fail rather than overwrite an
unmanaged conflicting entry.

## 12. External build publication and closed extensions

### 12.1 Publication, shims, and rollback

All `go-repository-v1` compilation MUST occur in operation-private staging
before installation mutation. The manager MUST validate and hash the sole
manager-derived artifact, generate canonical receipt and marker bytes, and
publish immutable snapshots and artifacts only under the manager-home mutation
lock. The consumer ledger MUST commit last. A failure after publication begins
MUST roll committed targets back in reverse order while retaining that lock.
Pre-publication source, object, descriptor, audit, cache, or compiler failures
MUST leave live installation state unchanged.

One manifest command key MAY select the same descriptor target as another
command key, but each command MUST derive its own artifact name, cache input,
receipt, and shim. The repository MUST NOT request aliases, sidecars,
post-build copies, or secondary PATH entries. A build shim MUST be manager-
owned and point exactly to the marker-selected protected artifact. It MUST NOT
point into a Git object database, frozen source snapshot, checkout, staging
directory, or commit-keyed script runtime. Structural validation MUST NOT use a
shell, executable lookup, or execution of the built output.

### 12.2 Operator credentials and signing

Git/SSH/HTTPS executables, credentials, host-verification state, proxy policy,
timeouts, and authentication mode are operator-owned. They MUST NOT be
selected by a manifest, descriptor, repository, substitution, compiler
environment, receipt trust field, or marker. Source credentials MUST NOT enter
the compiler environment.

The two transports disclose an operator credential differently, and a manager
MUST account for it. SSH proves possession of a key without disclosing it,
while HTTPS transmits the secret to the host that answers. An operator
selection that is not bound to a canonical identity — a single credential
offered to whatever host a declaration happens to name — therefore hands that
secret to every host in the closure over HTTPS, including one a hostile
declaration introduces. A manager offering such an unbound selection MUST let
the operator bind it to a host and MUST document the exposure; a selection
bound to a canonical-identity scope already satisfies this.

`go-repository-v1` revision 1 performs no manager post-signing, timestamping,
or notarization. Signing identities and notarization credentials are operator
or release-pipeline secrets and MUST NOT appear in a manifest, descriptor,
repository, receipt trust field, or marker. A platform policy requiring local
signing MUST reject the source-built artifact until a separately versioned and
reviewed signer profile defines the fixed signer identity, executable, argv,
entitlements/options, process graph, network policy, signed-byte and cache
identity, protected publication, and rollback rules.

### 12.3 Future closed-driver admission

The external Git envelope MAY be reused, but compiler semantics MUST NOT become
generic. Every future language driver requires a new closed identifier and an
independent protocol and security review. Its normative contract MUST define:

- complete compiler-visible input and dependency containment;
- trusted toolchain, sysroot, SDK, and runtime identity;
- fixed process graph, environment, arguments, flags, target, output, and
  signer boundary;
- offline dependency, source identity, link, and native-library policy;
- rejection of hooks, plugins, macros, generators, annotation processors,
  build tasks, recipes, response files, package linkers, and produced-program
  execution; and
- receipt/marker/cache identity, audit-before-build ordering, dry-run,
  publication, rollback/recovery, status, repair, garbage collection, and
  platform conformance vectors.

The same rule governs execution policies. A hardened execution profile MUST use
a new execution-policy identity, a new claim schema version that can express it,
and its own conformance vectors. It MUST NOT be admitted by widening the closed
`manager-worker-v1` constant, by treating an unknown execution policy as
compatible, or by upgrading a portable cache entry, receipt, marker, or claim in
place.

A manager MUST NOT admit a future driver by widening `go-v1` or
`go-repository-v1`, accepting an unknown driver, or providing a generic build
or package-manager fallback.
