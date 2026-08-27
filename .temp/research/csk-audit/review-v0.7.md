# Review: CocoaSkills audit foundation (commits d767d01..39bd620)

Date: 2026-06-16. Tests: 196 passed (+28). Naming: clean. Reviewed against RFC 0005
(incl. the committed section 19.1 release-blocking checklist) and the 8 prior findings.

## Verdict

The scaffolding is genuinely good and correct where it counts: the gate actually
gates both install paths before any write, the cache design is elegant, schema v3
parsing is solid, config parsing is disciplined. But several items the committed
section 19.1 marks **release-blocking** are stubbed or absent in ways that give
false assurance — the dangerous failure mode for a security tool ("looks like it
audits, doesn't"). Not ready to call v0.7 done; the structure is right, the
security substance is partially hollow.

## Critical

- **C1 — REQUIRE_PIN is a dead-end with a misleading message.** Under strict, a
  schema v1/v2 skill gets `Decision.REQUIRE_PIN` (pipeline.py:279) and blocks with
  "migrate to schema v3 or pin content hash X" (pipeline.py:258-262). But there is
  **no pin path**: trust.py has no pin storage, cli.py has no `--allow`/pin command
  (grep confirms zero). So the only real escape is v3 migration; the message
  promises a command that does not exist. This breaks the F2/section 5.1 migration
  contract the prior review specifically required, and ships a lying error for the
  entire existing v1/v2 fleet. Fix: implement `csk audit --allow <hash> --reason`
  + a `trust.json` pin that `_decide` consults, OR (interim) drop the "or pin"
  half of the message and document that v0.7 strict requires v3.

## High

- **H1 — detection is regex-only; the capability-escalation engine (the RFC spine)
  is absent.** Section 8 specified AST for Python and section 19.1 lists
  `static_python (AST)`, `static_manifest`, `opaque` as release-blocking. The impl
  is one regex module (detectors.py, `detector="static.regex"`): curl|pipe,
  shell=True, URL-hosts, secret-env-names, rm -rf. No AST, no manifest detector, no
  opaque detector. Consequence: declared `exec`/`filesystem` envelopes are parsed
  but **never enforced** — a skill declaring `exec: "none"` that calls
  `subprocess.run(["curl", ...])` is not flagged; `eval`/dynamic-import/opaque
  binaries are not flagged. The allowlist spine (declared-vs-observed) only works
  for `network` (via literal URLs) and `secrets`/`env` (via name markers).
  Trivially bypassable (string-split, base64, indirection). Fix: add an AST
  detector enforcing exec/filesystem/network/secrets against the envelope, a
  manifest detector, and an `opaque` detector for unanalyzable constructs/binaries.

- **H2 — revocation is unenforced (release-blocking per section 19.1).** config
  parses `audit.revocations` but nothing reads it; trust.py has no `is_revoked`. A
  revoked hash/source still installs. Either implement enforcement in the decision
  path, or formally move revocation out of the v0.7 release-blocking set.

- **H3 — the canary is a no-op; the integrity check gives false assurance.**
  `NullBackend.run_canary()` returns `True` unconditionally (null_backend.py:14-15),
  and the static detectors — which produce every v0.7 finding — have no canary at
  all. Every verdict records `canary_passed: true` while nothing is verified, so
  section 10/11's "detect a broken/subverted auditor → fail closed" provides zero
  protection. Fix: the v0.7 canary must run the static detectors against a bundled
  known-malicious fixture and assert the expected findings; fail closed otherwise.

## Medium

- **M1 — fail-open/closed on backend error not implemented.** `audit_plans` raises
  `AuditBackendError` on unavailable/canary-fail (pipeline.py:70-74); `gate_plans`
  doesn't catch it; installer's `except Exception` turns it into a failed install.
  Under advisory, section 10 requires warn+proceed. Latent with the always-available
  null backend, but breaks when codex/claude land. Also `timeout=30` is hardcoded,
  not from config.

- **M2 — finding evidence is not redacted and is persisted.**
  `static.network.undeclared-host` evidence is the full URL (a token can ride in the
  query string), and verdicts are written to `~/.cocoaskills/audit/`. No redaction
  module exists. Low blast radius in v0.7 (local-only, no cloud), but a secret can
  land in an on-disk verdict file.

- **M3 — `csk audit --all` ≠ projects+global.** Section 15 said `--all` covers both;
  the CLI has separate `--all` (projects) and `--global`. Align one to the other.

## Low / Nits

- L1 — `source_policy`, `grants` are parsed and validated but inert (nothing reads
  them). Acceptable per section 19.1 (no cloud backend ships), but note they are
  scaffolding, not enforcement, so reviewers don't assume protection.
- L2 — one blocked skill aborts the whole project/global install (gate_plans →
  raise/return), rather than dropping just the offending skill. Section 17 allowed
  either; aborting is the safe direction, but it means a single flagged skill
  blocks the good ones.
- L3 — `_build_request` passes `files={}` (null backend ignores them; static
  detectors read the snapshot directly). Fine as a stub; must be populated +
  scrubbed-for-cloud when real backends land.

## What is solid (confirmed)

- The gate gates: both `installer._install_project` and `global_install.install`
  call `gate_plans` after their plan filtering and before any write, and abort on
  BLOCK/REQUIRE_PIN. The #1 bypass risk (F1) is correctly closed.
- Cache design is elegant: findings are cached by (hash, backend, model, prompt,
  ruleset); the **decision is re-derived from current policy on a cache hit**
  (`_report_from_verdict` → `_decide`), so changing `fail_on` re-gates without
  re-auditing. Matches section 6/10.
- dry-run: audit runs but `record=not dry_run` → no cache write. Matches OQ2.
- schema v3 parsing is solid: requires `capabilities`, rejects unknown fields,
  v1/v2 default to an implicit `none` envelope.
- REQUIRE_PIN is correctly wired for strict + schema<3 (the decision is right; only
  the escape is missing — C1).
- Clean module layout matching the RFC; thorough config parsing (reject-unknown
  discipline consistent with the rest of the codebase).
- 196 tests green (+28), no internal naming.

## Bottom line

Foundation is structurally complete and the control flow is correct. Before calling
v0.7 done, close the items the committed checklist calls release-blocking but which
are stubbed: C1 (pin dead-end), H1 (real capability-escalation detection), H2
(revocation), H3 (real canary). M1/M2 can ride into the LLM-backend phase but should
be tracked. This is exactly the "blurry acceptance" risk the prior review flagged
for the foundation slice.
