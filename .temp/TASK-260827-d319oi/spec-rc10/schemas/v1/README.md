# Curator Protocol JSON Schemas v1

These Draft 2020-12 schemas are normative structural contracts. Semantic rules
that require filesystem, graph, cryptographic, ordering, or time context are in
the protocol documents and conformance vectors.

Rc.9 carries `agent-skill-v8`, `csk-skill-v8`, `install-marker-v4`, and
conformance claim v5. Rc.8 carries `assurance-policy-v1`,
`verified-provider-v1`, `provider-capability-receipt-v1`,
`execution-permit-v1`, `execution-receipt-v1`, `execution-checkpoint-v1`, and
conformance claim v4. These are new closed objects; no prior schema is widened
or reinterpreted.

All `$id` values are stable identifiers. Relative `$ref` values resolve from
the containing schema. `common.schema.json` is a definition library and is not
a standalone wire object.

Schema examples and expected validation outcomes live under
`../../conformance/v1/schema-cases/`.

`agent-skill-v1.schema.json` through `agent-skill-v8.schema.json` are the
canonical skill-manifest schemas. The corresponding `csk-skill-*` schemas are
the legacy filenames with byte-equivalent versioned meaning.

Manifest schema selection is exact: the integer `schema_version` selects the
same-numbered schema. Schemas 1 through 6 do not acquire schema-7
`build_repositories` or `go-repository-v1` meaning. Schema 7 adds those fields
without changing the earlier schemas or their generated fixtures. Schemas 1
through 7 do not acquire schema-8 `execution_policy`, `interpreter`, or
`modules` meaning.

## Manifest schema 8: the `script-worker-v1` opt-in

Schema 8 admits the enforced script execution policy of decision 0008. It
reuses every schema-7 definition unchanged and reaches its commands through
`$defs.commandV8`, so schema-7 bytes and fixtures stay frozen: `commandV7`
still selects the schema-7 `$defs.scriptCommand`, which has no execution
surface at all.

A schema-8 script command is `$defs.scriptCommandV8`. Beyond the schema-7
`type`, `unix_path`, and `win_path` it carries exactly two OPTIONAL fields:

- `execution_policy`, bound to the single closed constant `script-worker-v1`
  through `$defs.scriptExecutionPolicyV1`; and
- `interpreter`, bound to the closed identifier set `node-v1`, `python3-v1`
  through `$defs.scriptInterpreterV1`.

`dependentRequired` binds the two in both directions: a command declares both
or neither. Enforcement is per command, never per manifest. There is no
manifest-level default and no override resolution, so a reader determines a
command's enforcement state from the command object alone.

### Defaults

The absence of `execution_policy` is the default and the only spelling of
declared-only: the command keeps its exact schema-7 meaning — launcher `exec`,
no enforcement claim. `null`, `"none"`, `false`, and every other value are
rejected, so a manifest cannot express declared-only twice.

`$defs.capabilities` carries `default` annotations (`network: "none"`,
`filesystem: "repo"`, `exec: "none"`, `secrets: "none"`, `env_read: []`).
Draft 2020-12 `default` is an annotation: no validator materializes it and
schema 8 does not change the accepted value space. Those annotations state the
declared-only reading of an absent field. Under `script-worker-v1` an absent
capability field takes the deny-by-default meaning of decision 0008 section 3,
which is narrower than the annotation for `filesystem`. The two readings
coexist because capabilities are manifest-wide while enforcement is per
command, so one capability set serves every enforced command in a skill. The
structural schema deliberately does not resolve the difference; `protocol/`
prose is the authority for the effective value. Network host globs configure no
control under this policy and remain a reporting declaration, so the schema
neither widens nor narrows their existing shape.

### Rejection paths

- `execution_policy` or `interpreter` on a system, `go-v1`, or
  `go-repository-v1` command: rejected by that command's closed surface. The
  compiled drivers keep a manager-owned policy identity that no package
  selects.
- `execution_policy` without `interpreter`, or `interpreter` without
  `execution_policy`: rejected by `dependentRequired`.
- `manager-worker-v1` or `hardened-worker-v1` as a script `execution_policy`:
  rejected by the constant. The compiled and script identities never alias,
  and a successor such as `script-worker-v2` needs its own identity and
  revision rather than a widened constant.
- An interpreter identifier outside the closed set, including every shell
  identifier: rejected by the enum. Admitting one is a specification revision
  under `protocol/core.md` section 12.3, not a manager configuration option.
- An enforced command with neither `unix_path` nor `win_path`: rejected by the
  inherited `anyOf`.
- `execution_policy` or `interpreter` at the top level, or on any command, in
  manifest schemas 1 through 7: rejected. Schemas 2 through 7 reject them as
  unknown fields; schema 1 keeps its deployed top-level extension behavior, so
  its rejection comes from `tools/validate.py` instead, exactly as the
  schema-7 repository fields are handled.

### Install markers

A schema-8 installation records `install-marker-v4.schema.json`. Marker v4 is
marker v3 with `schema_version` 4 and `skill_schema_version` 8 and no other
difference, so every marker-v3 build-record rule — explicit receipt schema
version, explicit `execution_policy`, `build_source` present exactly when a
local `go-v1` build is active — applies unchanged. Markers v1, v2, and v3 keep
their frozen shapes and their existing manifest-version bands.

## Manifest schema 8: declared first-party module roots

Schema 8 also admits the declared module roots of decision 0009. A local
`go-v1` build command is `$defs.buildCommandV8`: the schema-6
`$defs.buildCommandV6` plus exactly one OPTIONAL field, `modules`, bound to
`$defs.pathSet` — a unique array of `$defs.portablePath`. `$defs.commandV8`
selects `buildCommandV8` in place of `buildCommandV6`. `buildCommandV6` itself
is untouched, so `$defs.commandV6` and `$defs.commandV7` keep their frozen
bytes and neither schema 6 nor schema 7 acquires the field.

`$defs.portablePath` already rejects `.`, `..`, absolute paths, backslashes,
colons, control characters, trailing spaces and dots, and the reserved Windows
device names, and `$defs.pathSet` already rejects duplicates, so the structural
half of Protocol Core section 4.2.3's containment rule is enforced here.

An absent `modules` list is the default and the only spelling of a
single-module build root; an empty array is admitted and means the same thing.
The field is per command, so a reader determines a command's declared module
set from the command object alone.

### Rejection paths

- `modules` on a script, system, or `go-repository-v1` command: rejected by
  that command's closed surface. Module roots are a local `go-v1` concern only.
- `modules` at the top level, or on any command, in manifest schemas 1 through
  7: rejected. Schemas 2 through 7 reject it as an unknown field; schema 1
  keeps its deployed top-level extension behavior, so its rejection comes from
  `tools/validate.py`, exactly as the schema-7 repository fields and the
  schema-8 script execution fields are handled.
- A non-portable, absolute, `.`, `..`-bearing, or duplicated entry: rejected by
  `pathSet` and `portablePath`.
- `modules` as a string, object, or null rather than an array: rejected by
  `pathSet`.

Everything else that section 4.2.3 requires — the bijection against the
effective replace set, the rejection of module-to-module redirects and
versioned replacement targets, disjointness from build and runtime roots,
`go.mod` presence, link-freeness, and platform-path collisions — needs
filesystem and build-graph context that Draft 2020-12 cannot express. Those are
conformance-vector rules, not structural ones.

### Schema-version numbering

Schema 8 is the single manifest bump for this protocol revision. Decision 0008
(`execution_policy` on script commands) and decision 0009 (first-party module
roots on local `go-v1` build commands) both land in it rather than taking
sequential versions: the two surfaces are disjoint, one release has never
carried two manifest versions, and a sequential pair would force a manifest
that wants both features onto the higher version while doubling the legacy
rejection matrix and the install-marker band. The module-roots change extends
the build-command branch reached from `$defs.commandV8` and adds no new
manifest schema version.

The external-repository wire family is:

- `skill-build-v1.schema.json` for repository-root logical targets;
- `skillfile-dev-v2.schema.json` for operator-only source substitutions;
- `build-receipt-v2.schema.json` for declared and effective repository input;
- `install-marker-v3.schema.json` for local, external, and mixed builds; and
- `conformance-claim-v3.schema.json` for rc.5 platform and language-driver
  assertions.

Both compiled-build policies carry a REQUIRED `execution_policy` bound to the
single closed constant `manager-worker-v1` in `common.schema.json`. Marker-v3
build records and claim-v3 driver assertions carry the same constant. Marker v2
keeps its frozen rc.4 shape and binds the execution policy transitively through
its recorded cache key and receipt hash.

Cross-field constraints that Draft 2020-12 cannot express, including selected
repository existence, `source_dir` containment, declared/effective equality,
mixed-marker top-level `build_source` presence, and the requirement that a
receipt cache key is the CCJ-1 digest of the input that carries its execution
policy, are enforced by `tools/validate.py` and covered by deterministic
generated cases.
