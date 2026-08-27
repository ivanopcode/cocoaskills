# Decision 0006: portable manager-worker execution policy

## Context

Decision 0004 required the manager to invoke the fingerprinted Go launcher as
its own direct child. That graph gives the manager no place to install
containment before compiler code runs: an ordinary process launch offers no
portable hook in which a child can apply restrictions after `fork` and before
`exec`. Post-exit checks cannot undo a source write, a network connection, an
escaped process, or resource exhaustion.

The reviewed architecture analysis for `TASK-260720-1zntv0` compared two
contracts. Preserving the direct-Go graph would require either an
externally prepared containment domain around the whole manager or a
platform-specific native launch subsystem; both narrow deployability, move
containment ownership outside the manager, and remain hard to port to a second
manager implementation. Adding one fixed manager-owned worker keeps package
inputs out of process selection, gives the child a safe place to apply controls
before Go starts, and keeps the parent in charge of graph validation and
publication.

The same analysis found that a fully fail-closed profile — total network denial,
kernel-enforced read-only source and toolchain, private-build-root-only writes,
hard aggregate descendant resource bounds, exact executable allowlisting, and
fail-closed preflight of all of them — is not reachable on current macOS and
Windows through supported public primitives without packaging, signing, or
experimental APIs. The earlier proposal answered that by shipping real builds
only on a hardened Linux profile and rejecting `go-v1` everywhere else.

That answer is worse than the problem for the compiled-skill delivery this
protocol exists to serve: it would make macOS and Windows, the two platforms
this release actually targets, reject every compiled skill while a hardened
profile is designed. The remaining question was therefore not "hardened Linux or
nothing" but "what is the maximum autonomously enforceable portable contract,
stated honestly, without pretending to be hardened".

## Decision

`go-v1` and `go-repository-v1` normatively identify one named execution policy.
Protocol 1.0 defines exactly one value, `manager-worker-v1`, and every
conforming manager implements it on macOS and Windows.

The fixed process graph gains one node:

```text
manager parent
  -> identity-verified manager-owned worker
       -> fingerprinted <GOROOT>/bin/go
            -> fingerprinted regular executables below
               <GOROOT>/pkg/tool/<GOHOSTOS>_<GOHOSTARCH>/
```

The worker is an exact re-execution of the installed manager executable in one
fixed hidden mode. It is a security boundary, not a command surface: no package
file, manifest, descriptor, environment value, `PATH` entry, shell, or user
option selects it. One session runs exactly one `go list`, waits for the parent
to validate the complete package graph, accepts exactly one authenticated build
permit, and runs exactly one `go build`.

The mandatory portable controls, the exhaustive native-control inventory, the six
deferred hardened guarantees, the closed capability-evidence record, and the
package-influence exclusions are normative in `protocol/core.md` section 4.2.1,
`profiles/manager.md` section 2.2.1, and `SECURITY.md`. In short:

- everything the manager can enforce on every supported host without extra
  privileges, packaging, or entitlements is mandatory, and a host that cannot
  apply all of it rejects the build before the worker or Go starts;
- every control that the versioned `rc5-native-control-inventory-v1` inventory
  marks available for the host platform is applied, and nothing outside that
  inventory is applied or reported;
- an inventory control the platform does not provide is recorded as unavailable
  and does not reject the build; and
- the six hardened guarantees are named, attributed to `STORY-260728-327soo`,
  and may never be claimed by a portable build.

Every mandatory control is a manager mechanism rather than a kernel guarantee,
and the specification names both halves so the two cannot be confused. The fixed
graph is manager selection plus per-program identity verification, not an
executable allowlist. Source and toolchain integrity is a frozen snapshot that
neither the manager nor the worker writes to, plus identity re-verification
before publication, not a read-only presentation to descendants. Policy
`network: "none"` is fixed offline Go module, proxy, checksum-database, and
version-control configuration, not kernel network denial. Private roots and a
verified staged artifact bound manager-directed writes, not every descendant
write. Parent-enforced deadline, output, and artifact limits bound the operation,
not every descendant in aggregate. Preflight covers the mandatory controls only.

There is exactly one failure boundary: a mandatory portable control that cannot
be applied rejects with `build_execution_control_unavailable` before the worker
starts, and neither an unavailable inventory control nor a missing hardened
guarantee ever rejects, warns, or blocks publication.

Host capability evidence is one closed `capability-evidence-v1` record per
operation: exactly `record_version`, `execution_policy`, `platform`, and one
`{name, availability, status, probed_at}` entry per inventory control, probed
once per operation before worker launch. Contradictions, unknown or missing
entries, and unknown record versions are errors rather than variations.

Because the policy identity is inside the canonical build input, the logical
cache key, receipt bytes, `receipt_sha256`, marker records, and conformance
claims all separate portable output from pre-revision candidate output and from
any future hardened output. Host capability evidence is deliberately excluded
from that identity: it is per-host reporting state, and letting it into the key
would fragment portable cache identity while telling readers nothing they may
rely on.

## Rejected alternatives

- **Keep the direct-Go graph.** Rejected: it leaves no pre-exec boundary, and
  the alternatives (external containment of the whole manager, or a native
  launch trampoline) change deployment ownership or add high native risk without
  producing a reviewed macOS or Windows profile.
- **Ship real builds only on a hardened Linux profile and reject `go-v1` on
  macOS and Windows.** Rejected: it withholds the entire compiled-skill feature
  from the platforms this release targets in exchange for a guarantee no
  supported host currently offers, and it would leave the portable contract
  unspecified and unversioned when the hardened profile lands.
- **Claim the hardened guarantees on macOS and Windows anyway.** Rejected: the
  claim would be false. Deprecated dynamic sandbox interfaces, entitlement- and
  packaging-dependent App Sandbox behavior, all-or-none Windows child-process
  policy, and the absence of an aggregate private storage quota do not add up to
  the advertised gates.
- **Make host capability evidence part of the cache key.** Rejected: it makes
  the same source, toolchain, and policy produce different keys per machine
  without adding any portable guarantee, and it invites a reader to treat a key
  as capability proof.
- **Add the execution policy to marker v2.** Rejected: marker v2 keeps its
  frozen shape, and the cache key and receipt hash it already records bind the
  execution policy transitively. Marker v3, which is new in this release, records
  it explicitly.
- **Reuse `go-v1` semantics without a policy identity.** Rejected: a semantic
  change to the process graph without a cache-identity change would let a
  pre-revision entry alias a portable entry.
- **Keep "every native control the host provides" as the rule.** Rejected: an
  open-ended rule lets a new operating-system primitive, or a different reading
  of "provides", change conformance without changing the policy identity, and it
  lets two conforming managers emit incomparable evidence. The inventory is
  therefore exhaustive, versioned, and normative per platform, and the evidence
  record is closed. Extending the inventory is a specification revision with a
  new inventory version; it is not an execution-policy revision, because
  inventory membership never enters a build input, an artifact, or a hashed
  identity.
- **Keep the absolute "source is read-only to children" and "only the
  fingerprinted graph may start" wording alongside the deferral.** Rejected: the
  two readings contradict each other. Either the absolute wording forces macOS
  and Windows to reject, recreating the hardened-Linux-only outcome this decision
  replaces, or the deferral silently weakens a `MUST`. The normative text now
  states the portable mechanism and the deferred guarantee separately.

## Compatibility impact

`1.0.0-rc.4` and `1.0.0-rc.5` are unreleased and unpinned, so the revision lands
in place rather than as a deprecation. Manifest schemas 1 through 5 keep their
exact published bytes. The manifest schema-6 and schema-7 package surfaces are
unchanged: no new package-controlled field exists, and the build command object
is still exactly `type`, `driver`, and `source_dir`, or exactly `type`,
`driver`, `repository`, and `target`.

`build-receipt-v1.schema.json`, `install-marker-v2.schema.json`, and
`conformance-claim-v2.schema.json` keep their bytes. The generated `go-v1`
receipt example changes, because its policy object now carries the
execution-policy identity and its logical cache key is recomputed over that
input. That is the intended effect, and a conformance test pins both the old
rc.4 digest and the requirement that it is no longer reproduced.

Marker v3 build records and claim v3 driver assertions gain a required
`execution_policy`. Claim v3 admits only `manager-worker-v1`, so a hardened
claim is structurally impossible in this schema and needs a later version.

## Security impact

The change adds the worker protocol, worker identity verification, and the
control adapters to the trusted computing base, and it makes worker
substitution, replacement races, and session replay explicit threats with
explicit answers. It removes the previous situation in which policy metadata
validation could be mistaken for native enforcement.

It does not claim that adversarial Go source is safe, and it does not claim
hardened containment. The compiler-input rejection policy remains mandatory, and
the six deferred guarantees are named in the specification so that no reader,
receipt, marker, or claim can quietly imply them.

## Follow-up

`STORY-260728-327soo` owns the fail-closed cross-platform profile. It must
introduce its own execution-policy identity, its own claim schema version, and
its own adversarial vectors, and it may not be enabled by widening the closed
`manager-worker-v1` constant or by upgrading portable evidence in place.
