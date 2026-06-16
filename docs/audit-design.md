# CocoaSkills RFC 0005 — Skill Security Audit

Status: accepted
Date: 2026-06-11
Author: Ivan Oparin
Target: v0.7.0 (foundation), later releases for cloud backends and signatures

RFC 0004 listed "signing, audit, or trust providers" as a non-goal. This RFC
specifies the audit and trust layer. Signature/provenance verification is
specified here but deferred to its own later release (section 17).

## 1. Goals

- Optionally audit a skill's installed bytes for security problems before they
  are written into a project, using pluggable analysis backends.
- Cover all three skill surfaces: runtime code, agent-facing prompts, and the
  command manifest.
- Make the security decision deterministic given (findings, policy), even when a
  backend is a non-deterministic LLM.
- Be local-first: run the analysis with a local model (e.g. via codex custom
  providers) with zero data egress; treat cloud backends as an explicit,
  warned, policy-gated choice.
- Be generic over agent systems: codex, Claude Code, and any future tool plug in
  through one backend contract.
- Establish trust as a first-class, content-addressed concept: trust-on-first-
  use, per-skill capability grants, pins, revocation, and a shared verdict cache.

## 2. Non-Goals

- Sandboxing or runtime confinement of skill command execution (that is the
  consuming agent's responsibility).
- Software composition analysis of third-party dependencies for known CVEs.
- Executing skill code to analyze it. The auditor never runs the skill.
- Proving the absence of malicious behavior. The system detects declared-vs-
  observed capability escalation and flags anything it cannot analyze; it does
  not claim soundness.

## 3. Threat Model

- Attacker: author of a third-party skill, a compromised upstream repository
  (supply chain), or a git-fetch MITM. Internal skills are not trusted by
  default (insider or account compromise).
- Attacker controls: every byte of the skill snapshot (code, prompts, manifest,
  locales, `.skill_triggers`), the git URL, and tags (which can move).
- Trust boundary: `csk` runs with user privileges; skill content is untrusted;
  the agent that later loads the skill is powerful (runs commands, reads files,
  reaches the network).
- Assets protected: (a) the machine and its secrets at install time; (b) the
  agent's runtime behavior against prompt injection; (c) confidentiality of the
  audited content against exfiltration by the auditor itself.

Hard rule: the auditor performs static analysis of bytes plus LLM reading of
bytes. It never executes skill code.

## 4. Architecture Overview

Two ideas carry the design.

### 4.1 Capability envelope (allowlist over blacklist)

A blacklist ("find `curl | sh`") is an arms race against obfuscation. Instead, a
skill declares a capability envelope and the audit detects escape from it.

A skill declares two envelopes:

- Code capabilities: network, filesystem, exec, secrets, env reads.
- Prompt directives: the domain the skill's prompts may instruct the agent in.

Detection becomes "declared vs observed". A git skill that declares
`exec: ["git"]` does not trip on running git; running `curl` is a violation.
Because dynamic languages cannot be analyzed soundly, anything unanalyzable
(`eval`, dynamic import, opaque binary) is itself a finding (`opaque`, high
severity under strict). Blacklist detectors remain a second, defense-in-depth
layer inside the envelope.

### 4.2 Staged pipeline (detection is advisory, the gate is deterministic)

```
detect → normalize → assess → policy → decide → record
```

The LLM only contributes to `detect` (advisory facts, each anchored to a
file:span). The gate (`assess` → `decide`) is deterministic given
(findings, policy). A flaky LLM cannot make the gate flaky.

## 5. Capability Manifest

Declared in `csk-skill.json` under a new `capabilities` object. Introduced with
schema v3. Skills on schema v1/v2 carry no declared envelope; section 5.1
defines how they are handled so enabling strict does not mass-block the existing
fleet.

```json
{
  "schema_version": 3,
  "runtime_roots": ["scripts"],
  "capabilities": {
    "network": "none",
    "filesystem": "repo",
    "exec": ["git"],
    "secrets": "none",
    "env_read": ["HOME"],
    "prompt_scope": "Manage issues on the configured tracker. No other actions."
  },
  "commands": { "...": {} }
}
```

Field semantics:

- `network`: `"none"` or a list of host globs the code may contact.
- `filesystem`: `"repo"` (repo subtree only), `"home-config"`
  (`repo` + `~/.config/<skill>`), or an explicit path list.
- `exec`: `"none"` or a list of allowed executables.
- `secrets`: `"none"` or a list of keyring service names the skill may read.
- `env_read`: environment variables the code may read.
- `prompt_scope`: a one-line statement of what the prompts may instruct.

Parsing/validation: `csk.audit.capabilities.parse_capabilities(data) ->
CapabilityManifest`, reusing the identifier/path validators from
`csk.identifiers` and `csk.skillspec`.

Surfacing the declared envelope to a human (capabilities are reviewed before they
are trusted) happens at every entry point, because manual `Skillfile.json` edits
produce no command event:

- `csk add` and `csk global add` print the target skill's declared capabilities
  when they write the declaration.
- `csk install --audit` and `csk audit` print the declared capabilities the first
  time an unknown content hash is seen, regardless of how the declaration got
  there. This is the path that covers hand-edited manifests.

### 5.1 Undeclared (schema v1/v2) skills — migration path

A v1/v2 skill audits against an implicit `none` envelope, so any observed
capability is a finding. To avoid turning the first strict run into a mass block
with no actionable path:

- Under `advisory`, undeclared skills produce findings (visible) and install
  proceeds.
- Under `strict`, an undeclared skill is not blindly blocked. It requires either
  (a) migration to schema v3 with an explicit capability manifest, or (b) an
  explicit legacy trust pin (`csk audit --allow <hash> --reason …`). The pin
  only clears the schema v1/v2 capability-declaration requirement. Real findings
  still pass through strict policy and may block the install. The decision for
  an unpinned legacy skill is `REQUIRE_PIN`, and the message is actionable: it
  names the skill, its schema version, and both paths (declare capabilities, or
  pin this content hash).

This gives existing skills a defined runway: advisory now, then declare-or-pin
before any team flips `strict` on.

## 6. Data Model

```python
# csk/audit/model.py
class Severity(StrEnum):  INFO; LOW; MEDIUM; HIGH; CRITICAL
class Surface(StrEnum):   CODE; PROMPT; MANIFEST
class Decision(StrEnum):  ALLOW; WARN; CONFIRM; BLOCK; REQUIRE_PIN

@dataclass(frozen=True)
class Location:
    file: str
    span: tuple[int, int] | None        # 1-based line range, None for whole-file

@dataclass(frozen=True)
class CapabilityViolation:
    capability: str                      # "network" | "exec" | ...
    declared: str                        # "none"
    observed: str                        # "api.evil.com"

@dataclass(frozen=True)
class Finding:
    id: str                              # "py.network.undeclared-host"
    surface: Surface
    category: str                        # exfiltration|rce|injection|capability-escalation|opaque|hygiene
    severity: Severity
    location: Location | None
    evidence: str                        # redacted snippet
    detector: str                        # "static:<rule-id>" | "llm"
    confidence: str                      # high|medium|low
    verifiable: bool                     # LLM facts without a checkable span are False
    capability_violation: CapabilityViolation | None = None

@dataclass(frozen=True)
class TrustRecord:
    pinned: bool
    pinned_by: str | None
    reason: str | None

@dataclass(frozen=True)
class Verdict:
    schema_version: int
    content_sha256: str
    skill: str
    source: str
    commit: str
    backend: str
    model: str | None
    cloud: bool
    prompt_version: int
    ruleset_version: int
    canary_passed: bool | None
    findings: tuple[Finding, ...]
    decision: Decision
    ran_at: str                          # timestamp injected by caller
    trust: TrustRecord
```

The verdict is persisted in the global audit store, never in the project repo.
Physical layout (resolves the "one verdict vs many verdicts per hash" ambiguity):

```
~/.cocoaskills/audit/<content_sha256>/
  <backend>-<model>-p<prompt_version>-r<ruleset_version>.json   # one Verdict per (backend,model,prompt,ruleset)
  trust.json                                                     # pins/grants for this hash, backend-independent
```

Many verdicts may exist for one content hash (different backends/models);
`trust.json` is the single backend-independent record of pins and grants for that
hash. Secret material is redacted from `evidence` before persistence or any
network send (section 12).

## 7. Pipeline and Module API

```python
# csk/audit/pipeline.py
def audit_snapshot(
    snapshot: Path,
    *,
    skill: str,
    source: str,
    commit: str,
    content_sha256: str,
    capabilities: CapabilityManifest,
    backend: AuditBackend,
    policy: Policy,
    csk_home: Path,
    now: str,
    timeout: float,
) -> Verdict:
    """detect (static + backend) → normalize → assess → policy → decide → record."""
```

Stages:

1. `detect` — run every static `Detector` (always) and, if the backend is
   available and allowed, `backend.extract(request)`.
2. `normalize` — merge both sources into `Finding` objects. LLM facts without a
   verifiable span get `verifiable=False`.
3. `assess` — `policy.assess(findings, capabilities)` assigns severity and raises
   it for capability escalations. Deterministic.
4. `policy` — `policy.classify(findings)` attaches a per-finding policy action,
   honoring per-skill grants and revocations. Deterministic.
5. `decide` — `policy.decide(findings, trust)` aggregates to one `Decision`.
   Deterministic given inputs.
6. `record` — `trust.store_verdict(csk_home, verdict)` in the shared cache.

## 8. Detector Layer

```python
# csk/audit/detectors/base.py
class Detector(Protocol):
    surface: Surface
    ruleset_version: int
    def scan(self, snapshot: Path, capabilities: CapabilityManifest) -> list[Finding]: ...
```

Static detectors (deterministic, injection-immune):

- `static_python` — AST-based (not regex). Resolves imports and calls to detect
  network use, filesystem access outside the repo, `subprocess`/`os.system`
  exec, secret/keyring reads, env reads, and flags unanalyzable constructs
  (`eval`, `exec`, dynamic `__import__`, `getattr` on `os`/`subprocess`) as
  `opaque`. Each is checked against the declared envelope.
- `static_shell` — parses `scripts/*` shells and `.cmd`, flags `curl|wget … | sh`,
  base64-pipe-to-shell, and network/exec outside the envelope.
- `static_manifest` — command names shadowing common system binaries, overly
  broad `runtime_roots`, `type: system` declarations.
- `opaque` — binary or minified artifacts under prompt-context or runtime roots
  that cannot be analyzed → `opaque` finding.

Scope of files: prompts (`SKILL.md`, `references/*.md`,
`.skill_triggers/<locale>.md`, `locales/metadata.json` — the description is
rewritten into the SKILL.md frontmatter, a distinct injection channel), code
(`scripts/`, everything under `runtime_roots`), manifest (`csk-skill.json`, the
legacy `agents/runtime.json`).

LLM extractor (advisory facts, not judgment): the backend is prompted to extract
facts — external network calls, reads outside the repo, shell exec, and prompt
directives that instruct the agent to act outside `prompt_scope` — each with a
file:span. Fact extraction is harder to subvert than a "safe?" judgment, and the
result feeds the same deterministic policy.

## 9. Backend Contract

```python
# csk/audit/backends/base.py
@dataclass(frozen=True)
class AuditRequest:
    files: Mapping[str, bytes]           # relpath -> content; scrubbed for cloud backends (see below)
    capabilities: CapabilityManifest
    contract_reference: str              # operational-contract.md text
    response_schema: dict                # JSON schema the backend MUST satisfy
    static_findings: tuple[Finding, ...] # grounding facts the LLM cannot argue away
    redacted: bool                       # True when files were scrubbed before this request

class AuditBackend(Protocol):
    name: str
    cloud: bool
    def is_available(self) -> bool: ...
    def run_canary(self) -> bool: ...    # known-malicious fixture must be flagged
    def extract(self, request: AuditRequest, *, timeout: float) -> list[Finding]: ...
```

Redaction contract for `AuditRequest.files` (this is a hard boundary, not just an
`evidence` concern):

- A local backend (`cloud=False`) may receive raw file bytes.
- A cloud backend (`cloud=True`) receives **scrubbed file content**: redaction is
  applied to the file bytes in the request, not only to `evidence`. If scrubbing
  changed any file, the request carries `redacted=True` and a `redaction`
  finding is added so the auditor knows content was altered (a secret that was
  removed cannot be analyzed as a capability).

The pipeline builds the request per backend: `build_request(snapshot, caps, *,
cloud) -> AuditRequest` scrubs when `cloud` is true.

Built-in backends:

- `command` (generic): writes the `AuditRequest` as JSON to a configured
  command's stdin and reads `Finding[]` JSON from stdout. Any future agent
  system is wrapped here. `cloud` is declared in config.
- `codex`: `codex exec --model <m>` with a configured provider; runs local Qwen/
  Gemma models with zero egress. Default local-first backend.
- `claude_code`: `claude -p --model <m> --output-format json` (headless). Cloud;
  routes through the Anthropic API. Gated by `allow_cloud` and warned.
- `null`: static-only, no LLM. The foundation ships with this wired so the seam
  is exercised before any LLM backend lands.

Exact CLI flags are validated against the installed CLI versions during
implementation. Backends are `type: system` dependencies (checked via
`shutil.which` with an actionable hint); `csk` never installs them. The
`contract_reference` (operational-contract.md) is always supplied so the auditor
can also judge contract hygiene.

## 10. Determinism, Integrity, Failure Modes

- The LLM is never the gate. `decide` is deterministic given (findings, policy,
  trust).
- Verifiability: each LLM finding carries a file:span. Under strict, unverifiable
  findings are logged but do not gate (a hallucination must not block). The gate
  considers all static findings plus verifiable LLM findings.
- Canary / integrity: before use, `backend.run_canary()` audits a built-in
  known-malicious fixture. If the backend fails to flag it, the backend is
  broken or subverted → fail closed (the backend is not used). This detects a
  compromised judge.
- Fail-closed vs fail-open: no verdict (backend unavailable, timeout, malformed
  JSON, network down) → under strict, block; under advisory, warn and proceed.
  Static detectors always run, even with no model.
- N-of-M (optional, high-stakes): multiple runs/models vote; disagreement
  escalates to a human.

## 11. Injection Defense (the judge is attackable)

The auditor reads attacker-controlled prompts that target the auditor itself.
Layers:

1. Skill content is never in the system prompt; it is a user turn marked as DATA,
   inside hard delimiters.
2. The response is constrained to the `response_schema` via structured output;
   the model cannot simply emit "SAFE".
3. Fact extraction, not judgment; policy is applied in code.
4. Static detectors are injection-immune ground truth; prose cannot argue them
   away.
5. The canary detects a subverted auditor.
6. Findings are redacted (section 12).

## 12. Egress and Confidentiality

- Each backend declares `cloud: true|false`.
- `allow_cloud: false` (default for sensitive setups) makes cloud backends
  refuse.
- A cloud backend prints a loud warning naming exactly what content is sent and
  where.
- Redaction: `csk.audit.redaction.scrub(text) -> text` removes detected secret
  material from `evidence` before persistence or any send, and from file content
  before any cloud send (section 9). Secret bytes never land in the repo or the
  cloud.

### 12.1 Source classification (formal, because it is a security boundary)

Whether a skill's content may reach a cloud backend is decided by an explicit,
ordered policy, not by implicit host sniffing:

```
classify(source) -> "internal" | "public"
```

- The policy is an ordered list of `(pattern, class)` rules in config
  (`audit.source_policy`). `pattern` matches the normalized source: the git host
  for `git@host:...` / `https://host/...` (including SSH `Host` aliases resolved
  via the user's ssh config), or the literal path for a local source.
- The first matching rule wins. Deny/`internal` rules are evaluated before
  `public` rules at the same specificity.
- `default_class` applies when nothing matches. It defaults to **`internal`**
  (fail-safe: an unknown source is never sent to the cloud by accident).
- Sources with no git URL (local path, scp-style, file:) are always `internal`.
- A GitHub/GitLab mirror is `public` only if its host matches a `public` rule;
  a mirror of a private repo on a public host stays whatever the rule says, so
  operators control it explicitly.

A cloud backend runs for a skill only if `allow_cloud` is true AND
`classify(source) == "public"`. Otherwise it falls back to a local backend or to
static-only, with a warning. This makes "did a private skill just go to the
cloud?" answerable from config, not from host-matching heuristics.

## 13. Trust Model

Audit establishes trust, not just lint output.

- Trust-on-first-use: the first install of a new source requires an explicit
  trust decision. Interactivity is bounded by the stream (`csk` is called from
  make, CI, and wrappers, so it must never hang): a prompt is shown only when
  stdin is a TTY. When stdin is not a TTY, `csk` never prompts — see section 13.1.
- Pinning: a verdict is bound to a content hash. Under strict, a revision pin is
  required (tags move); otherwise re-audit on hash change (reuses the existing
  moved-tag detection).
- Per-skill grants: a legitimate skill that must read `~/.aws` (a cloud skill) or
  run git declares it in `capabilities`; a human reviews and pins the grant. A
  grant is `(skill, capability, content_sha256, who, when, reason)`.
- Legacy pin: `csk audit --allow <hash> --reason "..."` clears only the
  schema v1/v2 capability-declaration requirement and records the decision with
  provenance. It does not override strict finding-level blocks.
- Revocation: a blocklist of `(source | content_hash)` blocks installs even when
  an old pass verdict exists.
- Shared verdict cache: keyed by `(content_sha256, backend, model,
  prompt_version, ruleset_version)` in `~/.cocoaskills/audit/`. Auditing
  skill-X@commit once serves every project that installs it.

```python
# csk/audit/trust.py
def load_cached_verdict(csk_home, content_sha256, backend, model, prompt_version, ruleset_version) -> Verdict | None
def store_verdict(csk_home, verdict: Verdict) -> None
def is_revoked(policy: Policy, source: str, content_sha256: str) -> bool
def grant_for(policy: Policy, skill: str, capability: str, content_sha256: str) -> Grant | None
def pin(csk_home, content_sha256, *, reason: str, who: str) -> None
```

### 13.1 Non-interactive behavior

`csk` is routinely invoked from make, CI, and wrappers, so the audit must be
fully defined for a non-TTY stdin and must never block on a prompt.

| mode | stdin TTY | stdin not a TTY |
|------|-----------|-----------------|
| advisory | prompt to confirm on findings / first use | no prompt: print findings + a warning, then proceed |
| strict | prompt to confirm a pin on a blocking finding | no prompt: fail closed (`BLOCK`/`REQUIRE_PIN`), exit non-zero, print the declare-or-pin instruction |

So an interactive developer gets a confirmation step; an automated run never
hangs. To gate in CI, use `--audit=strict` and pre-establish trust (a pin or a
schema v3 capability manifest committed to config); a blocking finding then
exits non-zero with an actionable message instead of waiting for input. This is
the same non-interactivity rule the skill operational contract requires of
commands.

## 14. Policy

Layered configuration: machine (`~/.cocoaskills/config.json`) sets defaults; the
project `Skillfile.json` may tighten but not loosen below the machine floor (a
machine that forbids `allow_cloud` cannot be re-enabled by a project); per-skill
grants are point exceptions.

```python
# csk/audit/policy.py
@dataclass(frozen=True)
class Policy:
    mode: str                # "advisory" | "strict"
    fail_on: Severity        # off|low|medium|high|critical
    allow_cloud: bool
    grants: tuple[Grant, ...]
    revocations: tuple[str, ...]

def assess(findings: list[Finding], caps: CapabilityManifest) -> list[Finding]: ...
def classify(findings: list[Finding], policy: Policy) -> list[Finding]: ...
def decide(findings: list[Finding], policy: Policy, trust: TrustRecord) -> Decision: ...
```

## 15. CLI Surface

```
csk install --audit                 # advisory: run, warn, confirm on findings (interactive)
csk install --audit=strict          # block on findings >= fail_on, fail-closed
csk install --no-audit              # explicit opt-out when config enables audit
csk audit [skill | --all] [--json]  # standalone audit without installing
csk audit --allow <hash> --reason … # legacy capability-declaration pin
csk audit --revoke <hash | source>  # revocation
--audit-backend codex|claude-code|command   --audit-model <id>
```

`csk audit --json` emits the `Verdict` model (section 6). Non-interactive/CI
gating uses `strict`; the human confirmation is the reviewed grants/pins in
config and the pipeline definition, not a live prompt (section 13.1).

Scope and side-effect rules:

- `csk audit --all` audits both registered projects and the global scope,
  mirroring the two install paths (`install --all` + `global install`). A bare
  `csk audit` audits the current project.
- `--dry-run --audit` runs the audit and prints findings, but writes nothing:
  no verdict files and no trust records (it may read the cache). This matches the
  existing dry-run contract that no install path mutates state under `--dry-run`.

## 16. Config Schema

```json
"audit": {
  "enabled": false,
  "mode": "advisory",
  "fail_on": "high",
  "backend": "codex",
  "model": "qwen2.5-coder:32b",
  "allow_cloud": false,
  "backends": {
    "codex":  {"kind": "codex",       "provider": "local-ollama", "cloud": false},
    "review": {"kind": "command",     "command": ["my-auditor", "--json"], "cloud": false},
    "claude": {"kind": "claude-code", "model": "claude-...", "cloud": true}
  },
  "grants": [
    {"skill": "skill-review", "capability": "exec:git", "content_sha256": "…", "reason": "…", "who": "…"}
  ],
  "revocations": ["sha256:…", "source:evil/*"],
  "source_policy": {
    "default_class": "internal",
    "rules": [
      {"pattern": "git.internal.example", "class": "internal"},
      {"pattern": "github.com/myorg/*",   "class": "internal"},
      {"pattern": "github.com/*",         "class": "public"}
    ]
  }
}
```

Parsed in `csk.config.parse_config` with the same missing/wrong-type/unsupported
discipline already used for `schema_version`.

## 17. Install Integration

There are two install paths that materialize `SkillPlan`s and write skill bytes:
`installer._install_project` (project scope) and `global_install.install` (global
scope). Both must gate, or `csk global install` is a bypass for the same
untrusted code. The gate is therefore a shared function over plans, not a hook
buried in one path:

```python
# csk/audit/pipeline.py
def gate_plans(
    plans: list[SkillPlan],
    *,
    scope: str,                 # "project" | "global"
    config: GlobalConfig,
    policy: Policy,
    csk_home: Path,
    now: str,
) -> list[SkillPlan]:
    """Audit each plan's snapshot; return the plans allowed to proceed.

    Under strict, BLOCK/REQUIRE_PIN plans are dropped (or the run aborts per
    policy); under advisory, all plans pass with warnings. Audits run per skill,
    in parallel, with a per-skill timeout/budget, short-circuiting on a cached
    verdict for an already-audited content hash. Skipped only on --no-audit.
    """
```

It is called in both paths after the scope's own plan filtering and before any
write:

- project: after `_check_system_commands(plans)` (hard-fail on missing system
  deps), before the write loop.
- global: after `_plans_with_available_system_commands(plans, result)` (which
  drops unavailable system-command skills), before the write loop.

Because each scope gates exactly the set of plans it is about to write, the
existing per-scope difference in system-dependency handling (project hard-fails,
global soft-filters) is preserved automatically — the audit decision is not what
unifies or diverges that behavior.

## 18. Module Layout

```
src/csk/audit/
  __init__.py
  model.py            # Finding, Verdict, Severity, Decision, TrustRecord
  capabilities.py     # CapabilityManifest parse/validate
  pipeline.py         # audit_snapshot + gate_plans
  policy.py           # assess / classify / decide (deterministic)
  trust.py            # TOFU, grants, pins, revocation, verdict cache
  redaction.py        # secret scrubbing
  detectors/
    base.py           # Detector Protocol
    static_python.py  # AST-based
    static_shell.py
    static_manifest.py
    opaque.py
  backends/
    base.py           # AuditBackend Protocol, AuditRequest
    null_backend.py   # static-only (foundation)
    command_backend.py
    codex_backend.py
    claude_code_backend.py
  prompts/            # versioned extraction prompt + response schema
  fixtures/           # canary known-malicious fixtures
```

Touched existing modules: `config.py` (audit config), `cli.py` (`--audit`,
`csk audit`), `installer.py` (the seam), `skillspec.py` (schema v3 capabilities),
`identifiers.py` (capability value validation).

## 19. Phasing

The foundation is built complete so later phases plug detectors and backends in
without restructuring.

1. v0.7.0 — Foundation, fully structured: capability manifest (schema v3, parse +
   declared-vs-observed), the full data model, the detect→assess→policy→decide→
   record pipeline as real module boundaries, the `AuditBackend` protocol wired
   with the `null` (static-only) backend, the static detectors, `csk audit`
   standalone, `csk install --audit` advisory, the verdict cache and trust
   records (TOFU/pins/grants/revocation data model). Deterministic, zero egress,
   no LLM. The seam for LLM is present and exercised by `null`.
2. v0.8.0 — LLM extractor backends: `command` (generic) and `codex` (local-first),
   backend canaries, timeout plumbing, and file-content redaction in the request
   path. Detailed in RFC 0006 (`docs/v0.8-design.md`).
3. v0.9.0 — `claude_code` backend, cloud policy and warnings, strict gating, the
   full trust workflow (grants/pins/revocation enforcement), N-of-M.
4. Separate later release — Signatures and provenance (section 20).

### 19.1 v0.7.0 acceptance checklist (release-blocking)

The foundation is a large slice, so the release-blocking surface is enumerated
explicitly; everything else may land as data model without blocking the release.

Release-blocking:

- schema v3 `capabilities` parse + validation; v1/v2 handled per section 5.1.
- `null` (static-only) backend wired through the full pipeline.
- static detectors: `static_python` (AST), `static_shell`, `static_manifest`,
  `opaque` — at least these four.
- `csk audit` and `csk audit --json` emitting the `Verdict` model.
- `csk install --audit` advisory gating, via `gate_plans`, on **both** project
  and global install paths.
- verdict store: cache write + cache hit (short-circuit on known content hash).
- revocation honored (a revoked hash/source blocks even with a pass verdict).
- non-TTY advisory proceeds with a warning (no hang); `--dry-run --audit` writes
  nothing.

Present as data model / wired but not release-blocking for v0.7.0:

- full grant/pin enforcement workflow and its UX (the structures exist; rich
  enforcement and N-of-M land in v0.9.0).
- cloud source classification beyond parsing (no cloud backend ships until
  v0.9.0, so it is parsed and validated but not exercised end-to-end).

## 20. Signatures and Provenance (deferred, separate release)

Specified here, shipped on its own. The audit verifies content; signatures verify
origin. The two are independent layers.

- Optional signature verification of the skill source (sigstore or minisign):
  a verified signature binds the content hash to a known signer, upgrading a
  pass verdict's trust level.
- Under strict, require a revision pin (immutable commit) rather than a tag.
- The signer identity is recorded in the verdict's `TrustRecord`.

This release is sequenced after v0.9.0 and does not block the foundation.

## 21. Decisions

- Capability manifest (schema v3) is adopted as the spine. Undeclared (v1/v2)
  skills audit against an implicit `none` envelope, with the migration path in
  section 5.1 (advisory findings now; declare-or-pin before strict) so enabling
  strict does not mass-block the fleet.
- The audit gates both install paths through one shared `gate_plans` over plans,
  so `csk global install` is not a bypass (section 17).
- Cloud egress is governed by an explicit source-classification policy with a
  fail-safe `internal` default and per-backend file scrubbing for cloud
  (sections 12.1, 9), not by implicit host sniffing.
- Behavior is fully defined for non-TTY stdin: never prompt, never hang
  (section 13.1).
- A dedicated audit subsystem, not a generic pre-install hook framework
  (arbitrary commands on install would themselves be an injection vector).
- Signatures/provenance are specified (section 20) but shipped as a separate
  later release.
- The foundation (v0.7.0) ships the complete structure, not a thin static lint,
  with a release-blocking checklist in section 19.1.

### Resolved open questions

- `csk audit --all` covers registered projects and the global scope (section 15).
- `--dry-run --audit` writes nothing — no verdict files, no trust records — and
  may read the cache (section 15).
- The audit preserves the existing per-scope system-dependency behavior (project
  hard-fail, global soft-filter) by gating exactly the plans each scope will
  write; it does not unify or change it (section 17).
