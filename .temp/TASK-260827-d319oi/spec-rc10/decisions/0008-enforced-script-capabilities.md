# Decision 0008: enforced capabilities for script commands

## Status

Accepted 2026-08-22.

## Context

Section 4.3 states the current contract plainly: capabilities are an audit
surface, not a runtime sandbox. For compiled commands that honesty is
acceptable because the `go-v1` boundary (decisions 0004 and 0006) makes the
artifact a pure function of the audited snapshot: vendored module mode, no
network, directive and assembly gates, one worker, one permit, one build.

Script commands have no analogous boundary. A script command's file content is
part of the audited snapshot, but its execution is a bare launcher `exec`
(section 12.1): nothing prevents a script whose manifest declares
`network: "none"` from downloading new code at run time and executing it. The
declaration and the behavior are allowed to disagree, and the reader of an
audit record cannot tell. This is exactly the class of gap the compiled-build
boundary was built to close, left open on the other command type.

The gap matters because script commands are not residual: they are the
portable way to ship interpreter-driven tooling, and several ecosystems cannot
or should not compile. Removing script commands is not on the table. Making
their declarations true is.

Decision 0006 already established the doctrine this proposal extends: a
manager-owned worker gives a pre-`exec` point to apply controls; controls are
split into mandatory portable mechanisms and a versioned native-control
inventory; what a platform cannot provide is recorded as unavailable rather
than silently claimed; manager mechanisms and kernel guarantees are named
separately so they cannot be confused.

## Decision

Define one named script execution policy, `script-worker-v1`, applying the
manager-worker doctrine of decision 0006 to script commands, with the
section 4.3 capability declaration as its policy input.

1. **Opt-in surface.** A new manifest schema (working number: schema 8)
   admits `execution_policy: "script-worker-v1"` on script commands. Schema 7
   and earlier script commands keep their current meaning: declared-only,
   launcher `exec`, no enforcement claim.

2. **Process graph.** An enforced script command runs as
   `manager parent -> identity-verified manager-owned worker -> interpreter`.
   The worker is the same security boundary defined by decision 0006: not a
   command surface, selected by no package input.

3. **Capabilities become the containment profile.** The worker derives its
   controls from the declared capabilities before the interpreter starts:

   - `network: "none"` denies network through every control the host
     inventory provides (for example: unshare/netns on Linux; the strongest
     supported per-platform mechanism elsewhere) and through mandatory
     portable mechanisms (offline environment configuration, proxy and
     resolver scrubbing) everywhere. Host globs configure, at minimum,
     audit-visible reporting; kernel-grade host filtering enters the native
     inventory per platform and is never silently claimed.
   - `exec` bounds what the script may spawn: the interpreter plus the
     declared executable names, resolved by the manager to fixed paths and
     provided through a controlled `PATH`; everything else is absent rather
     than merely discouraged. Where the platform inventory provides
     descendant-exec denial, it is applied and recorded.
   - `filesystem` confines writes to the command's private runtime area plus
     declared paths, by mandatory manager mechanisms (working directory,
     environment, tmp redirection) everywhere and by kernel mechanisms
     (Landlock, platform equivalents) where the inventory provides them.
   - Absent capability fields take deny-by-default meaning under this policy:
     no network, no exec beyond the interpreter, no writes outside the
     private runtime area.

4. **Honesty rules carry over unchanged.** Mandatory portable controls that
   cannot be applied reject before the worker starts, with a
   `script_execution_control_unavailable` diagnostic. Inventory controls the
   platform does not provide are recorded as unavailable in one closed
   `capability-evidence-v1` record per invocation-policy identity and never
   reject. Hardened kernel guarantees may not be claimed by this portable
   policy; if a hardened profile lands later it is named separately, exactly
   as decision 0006 does for builds.

5. **Audit surface.** The execution policy identity joins the audit record
   for the skill, so a reviewer can distinguish `script-worker-v1` commands
   from declared-only script commands, and registries can gate on it. Audit
   of legacy (schema <= 7) script commands gains a warning class naming them
   declared-only.

## Rejected alternatives

- **Static analysis of script content.** Rejected: sound reachability of
  "downloads and executes new code" is not decidable for shell or general
  interpreters; a scanner would produce either false confidence or unusable
  noise. Content stays in the audit surface; behavior is bounded at run time.
- **Claim kernel-enforced sandboxing on every platform.** Rejected for the
  reasons decision 0006 records: current macOS and Windows public primitives
  do not support the claim without packaging, signing, or experimental APIs.
  The claim would be false, and false claims are worse than honest partial
  enforcement.
- **Ban or deprecate script commands.** Rejected: they are the portable
  delivery form for interpreter tooling, and removal would push users toward
  unmanaged installation paths with no audit surface at all.
- **Enforce on legacy schemas implicitly.** Rejected: silently changing the
  runtime behavior of shipped skills breaks the compatibility contract;
  enforcement is an explicit, versioned opt-in with an audit-visible label
  for everything that has not opted in.

## Consequences

- `protocol/core.md`: new subsection under 4.1 (or a sibling of 4.2.1)
  defining `script-worker-v1`: process graph, mandatory portable controls,
  native-control inventory reference, capability-evidence record, failure
  boundary, and diagnostic identifiers.
- `profiles/manager.md`: manager obligations for the worker launch and the
  control inventory on each supported platform.
- Schemas: manifest schema 8 (`execution_policy` on script commands);
  capability-evidence reuse; audit-record extension for the policy identity.
- Conformance: positive and negative vectors for opt-in parsing,
  deny-by-default derivation, control preflight rejection, evidence-record
  closure, and legacy declared-only labeling.
- Implementations: Curator (Go) extends its existing worker re-execution mode;
  cocoaskills (Python) implements the same wire contracts from the shared
  vectors. Cross-implementation CI proves both against the same suite.
- SECURITY.md: the enforcement/guarantee split for scripts, mirroring the
  build-policy prose.

## Open questions deferred to the normative change

1. Schema mechanics: `execution_policy` per command versus one manifest-level
   default with per-command override.
2. Interpreter identity: whether the interpreter binary is fingerprinted like
   the Go toolchain (section 8.2) or resolved as a declared `exec` entry.
3. Whether `network` host globs under this policy configure any portable
   filtering mechanism, or remain reporting-only until a hardened profile.
4. Evidence-record cadence for long-running script commands (per invocation
   versus per install generation).
5. Windows scope for the first release: which controls enter the mandatory
   portable set versus the inventory, given the platform-case ledger
   discipline.
