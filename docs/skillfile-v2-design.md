# CocoaSkills RFC 0007 — Skillfile v2: Dependency Closure, Lockfile, and Surfaces

Status: draft — accepted pending editorial cleanup
Implementation: green-light for v0.9 foundation
Date: 2026-06-30
Author: Ivan Oparin
Target: v0.9.0 (foundation), later releases for ranges/semver, MCP provisioning

Through RFC 0006 a project's `Skillfile.json` was a **flat list** of independently installed
skills: no skill→skill dependencies, no transitive resolution, no install ordering, no version
negotiation, no conflict handling. The only dependency concept was `type: system` (an external
binary checked with `shutil.which`).

This RFC specifies the dependency layer: a node manifest (`csk-skill.json` v4) that declares what
a skill provides and requires, a graph manifest (`Skillfile.json` v2) that owns composition and
resolution policy, a deterministic resolver with a lockfile, and three safety pillars — active
install surfaces, contract safety, and source policy integration. The driving concern is not
version math; it is preventing **hidden surface expansion**: a command reaching `PATH` without
its rules, a context entering the window unasked, or the transitive graph pulling a source the
root never actually trusted.

**v0.9 is exact-only.** Range constraints and minimal-version selection (MVS) are described as the
target model but are deferred to the skill semver contract (section 21). In v0.9 every dependency
is an exact git ref; resolution is **exact-ref unification**, not version solving.

## 1. Goals

- Let a skill declare dependencies on: a system utility, another skill's command, another skill's
  prompt-context, and an MCP server contract.
- Resolve the transitive closure of a project/workflow into a deterministic, reproducible install
  recorded in a lockfile.
- Make every installed surface (commands on `PATH`, contexts in the window, MCP requirements)
  explicit and reviewable; never expand it implicitly.
- Keep source authorization outside the project so a compromised manifest cannot grant itself
  trust.
- Preserve today's behavior for direct, flatly-declared skills (backward compatible).
- Allow workflow-to-workflow composition (`includes`) so a workflow is a reusable artifact, not a
  copy-pasted skill list.

## 2. Non-goals

- A package registry or hosting service. Sources stay git URLs; authorization is policy.
- Range constraints, semver math, or MVS in v0.9 (exact-only; section 10).
- System dependency version enforcement in v0.9. v0.9 checks only command presence. Future version
  checks must use csk-owned built-in probes, never manifest-provided commands, arguments, scripts,
  or post-install hooks (sections 7, 21, 23).
- Command aliasing / namespacing in v1 (section 14).
- A skill shipping its own MCP server as an install target (`provides.mcp_servers`); future work
  (section 17), its own RFC, because it makes csk write agent adapter configs.
- A formal skill semver contract; prerequisite for ranges, specified as a sub-RFC (section 21).

## 3. Background and current state

- `Skillfile.json`: `skills: [{ name, git, tag|branch|revision }]`, each pinned by an exact git
  ref. Reproducible, no ranges.
- `csk-skill.json`: `commands` mixes `type: script` (a command the skill **provides**) and
  `type: system` (an external utility it **requires**). Schema v2 adds `runtime_roots`; schema v3
  adds the `capabilities` envelope used by `csk audit`.
- Installer iterates the flat `skills` list — no graph, ordering, or transitive step.
- Command shims are a **flat namespace**: `.agents/bin/<command>` (project), global mirror,
  precedence project > global > system.
- Runtime store is **content-addressed by commit**: `~/.cocoaskills/runtime/<skill>/<commit>/`.
- Two install layers already exist and are central here: **prompt context**
  (`whitelist.INCLUDE_ROOTS` → `.agents/skills/<skill>/`) and **runtime** (`runtime_roots` →
  runtime store + shims). The dependency types in this RFC map onto exactly these two layers.
- `audit/source_policy.py` exists today but classifies a source as internal/public for *cloud
  audit* and normalizes git sources mostly to **host** (section 13).
- `dependencies.json` is legacy, copied to context but never parsed; removed by its own task and
  superseded by the `dependencies` block here.
- MCP is not referenced anywhere in csk today.

## 4. Motivating example (the `wk` case)

Already in the wild, only half-designed:

- `skill-wiki` provides the `wk` command (`type: script`) and requires the external `wiki` CLI
  (`type: system`).
- `skill-wiki-memory` ships an undocumented block declaring it needs `wk` from `skill-wiki`. csk
  does not act on it — a hint asks a human to add `skill-wiki` manually. The example already
  exposes the core question: is the dependency on the *command* `wk` (runtime) or on
  `skill-wiki`'s *contract* (context)? `skill-wiki-memory` declares `capabilities.exec: "none"`
  while calling `wk` — an inconsistency this RFC makes illegal.

## 5. Terminology

- **Node** — a skill, described by its `csk-skill.json`. Declares provides / requires /
  capabilities by **name and ref**, never by source.
- **Graph manifest** — a `Skillfile.json`. Owns direct skills, includes, sources, overrides, and
  produces the lockfile. The root graph manifest is the source of truth.
- **Closure** — the transitive set of nodes reached from the root through includes and node
  dependencies.
- **Surface** — what an install materializes: commands on `PATH`, prompt-contexts in the window,
  MCP requirements.
- **Activation** — the per-edge decision to materialize a specific surface of a provider. A
  property of the **edge**, not the node.
- **Source identity** — the canonical `host/namespace/project` of a git artifact (skill or
  include), normalized identically across transports.

## 6. Architecture overview

Two manifests, clean ownership:

- **`csk-skill.json` (node, schema v4)** — provides + requires + local capabilities. A node says
  *what* it needs, never *where* it comes from.
- **`Skillfile.json` (graph, schema v2)** — direct skills, `includes`, `sources` (name → git URL),
  `overrides`, `agents`, `locale`. Resolves the closure and writes `Skillfile.lock`.

The resolver builds the closure, **resolves exact refs in v0.9** (later, selects versions through
MVS once the semver contract exists), checks conflicts and source policy, writes the lock, and
installs from it across the two existing install layers.

## 7. Node manifest — `csk-skill.json` schema v4

`commands` is unchanged physically (backward compatible): it remains the **provides** surface. All
**requires** live under `dependencies`.

```jsonc
{
  "schema_version": 4,
  "runtime_roots": ["scripts"],
  "capabilities": { /* local node only; v3 envelope */ },

  // PROVIDES — exported commands (shims)
  "commands": {
    "wk": { "type": "script", "unix_path": "scripts/wk", "win_path": "scripts/wk.cmd" }
  },

  // REQUIRES
  "dependencies": {
    // command requirements — system and skill share ONE model; only the provider differs
    "commands": {
      "wiki": { "type": "system", "command": "wiki", "hint": "..." },
      "wk":   { "type": "skill", "skill": "skill-wiki", "command": "wk" }
    },
    // skill node declarations — exact ref + context intent (canonical per-provider entry)
    "skills": {
      "skill-wiki": { "ref": { "kind": "tag", "value": "v1.4.2" }, "context": { "mode": "full" } }
    },
    // MCP requirements — a provider contract, not just a server name
    "mcp_servers": {
      "wiki-mcp": { "tools": ["article.search", "article.read"], "required": true, "hint": "..." }
    }
  }
}
```

Rules:

- `commands.*.type=system` (a require living in the provides block) is **deprecated but tolerated**
  for migration. New system utilities go to `dependencies.commands` with `type: system`.
- `dependencies.commands.*.type=system` in v0.9 declares **presence only** (`command` + optional
  `hint`). It is checked with `shutil.which`; csk does not execute version probes from the manifest.
  Version constraints for system tools are deferred. When added, they must use csk-owned built-ins
  such as `version_probe: "builtin:git"` or `version_probe: "builtin:glab"`: the manifest may name
  an approved probe id, but it may not provide command arguments, shell snippets, `check`,
  `install`, `post_install`, or arbitrary scripts.
- `dependencies.commands.*` carry **no git URL**. A `type: skill` entry references a provider by
  name only.
- `dependencies.skills.<provider>` is the **canonical node declaration**: it holds the **ref** and
  the context intent. **Every `type: skill` command requirement MUST have a matching
  `dependencies.skills` entry.** The ref lives only here.
- `ref` is an exact reference: `{ "kind": "tag" | "revision", "value": "..." }`. **Dependency
  edges (`dependencies.skills.*`) accept only `tag` or `revision` in v0.9.** A semver range
  (`version: "^1.0.0"`) is a parse error until the skill semver contract exists (section 21).
  `branch` is accepted only as legacy / direct-root sugar (section 8), never on a dependency edge:
  a branch is exact as an input ref *name*, not immutable, so resolution records the concrete
  commit in the lock and `--locked` installs that commit and never follows a moved branch.
- `context` is an object: `{ "mode": "full" | "none", ... }`, leaving room for
  `{ "mode": "partial", "refs": [...] }` later (section 12).
- `dependencies.json` is not part of schema v4.

## 8. Graph manifest — `Skillfile.json` schema v2

```jsonc
{
  "schema_version": 2,
  "project": { "alias": "incident-management" },
  "agents": ["claude_code", "codex_cli"],
  "locale": "ru",

  // address book: name -> git URL. NOT a trust grant (section 13).
  "sources": {
    "skill-wiki":   { "git": "git@gitlab.wildberries.ru:portals/agentic-infra/skills/skill-wiki.git" },
    "skill-gitlab": { "git": "git@gitlab.wildberries.ru:portals/agentic-infra/skills/skill-gitlab.git" }
  },

  // direct skills: exact ref; source resolved via `sources`
  "skills": [ { "name": "skill-gitlab", "ref": { "kind": "tag", "value": "v1.1.0" } } ],

  // workflow -> workflow composition (the literal "transitive Skillfile.json")
  "includes": [
    { "name": "incident-management",
      "git": "git@gitlab.wildberries.ru:portals/agentic-infra/workflows/incident-management.git",
      "ref": { "kind": "tag", "value": "v1.0.0" } }
  ],

  // root may pin a node's exact ref, valid only if it resolves to a commit compatible with the
  // node's exact-ref constraints (section 10)
  "overrides": { "skill-wiki": { "ref": { "kind": "tag", "value": "v1.4.2" } } }
}
```

- A direct skill is an **exact ref/revision** declaration, resolved against `sources`. The legacy
  per-skill `tag`/`branch`/`revision` keys remain accepted as sugar; a legacy `skills[].git` is
  accepted and normalized as an implicit root `sources[skill.name].git`. New v2 manifests SHOULD
  use `sources`; a `skills[].git` that duplicates `sources[name]` must match or fail. A branch ref
  is resolved to a locked commit (section 7); `--locked` never follows a moved branch. The word
  *constraint* is general; in v0.9 a constraint is always an exact ref, never a range.
- `includes` contribute **skills + constraints only**. The root owns `sources`, `overrides`,
  `agents`, `locale`, and the single lock. Included policy fields are ignored (section 13.4).

## 9. Dependency taxonomy

| Kind | Meaning | Declared as | Install layer |
|------|---------|-------------|---------------|
| D1 — system utility | needs an external binary on `PATH` | `dependencies.commands.<n>.type=system` | check only (never installed) |
| D2 — skill command (runtime) | needs another skill's exported command | `dependencies.commands.<n>.type=skill` + paired `dependencies.skills` | runtime layer (shim) |
| D3 — skill context | needs another skill's SKILL.md/rules in the window | `dependencies.skills.<p>.context.mode=full` | prompt-context layer |
| D4 — MCP contract | needs MCP server + tools in the session | `dependencies.mcp_servers.<s>` | check / surface only (T1) |

D2 and D3 are **orthogonal axes** over the same provider. A skill may need only the command
(runtime), only the context, or both. The provider's supply side of D2 is just its `commands`
(`type: script`).

## 10. Resolution model

- **Closure.** From the root graph manifest, expand `includes` and each node's
  `dependencies.skills` / `dependencies.commands(type=skill)` transitively until fixpoint.
- **v0.9 is exact-ref unification, not version selection.** The algorithm per skill name:
  1. collect every incoming constraint (each an exact ref);
  2. resolve each ref to a concrete commit against the skill's `source`;
  3. if all resolve to the **same commit** → OK;
  4. if they resolve to **different commits** → conflict (error);
  5. a root `override` is compatible **iff it resolves to the same commit as every incoming exact
     ref**; future range/MVS may define broader satisfaction.
  Example: `A → skill-wiki tag v1.4.2` and `B → skill-wiki revision abc123` are **compatible iff
  `v1.4.2` resolves to `abc123`**, otherwise a conflict. The lock records **all** incoming
  constraints plus the selected commit.
- **Target model (deferred).** Once the semver contract exists, ranges + minimal version
  selection replace step 1–5; the lock and `--locked` mechanics are unchanged.
- **Source resolution.** A node names dependencies by skill name; the URL comes from the root
  `sources` map (section 13), never from the dependency edge. No source → resolver fails.
- **Provider-provides-command.** A `dependencies.commands.<n>.type=skill` edge is valid only if the
  selected provider's `commands` contains the requested command with `type: script`. A missing
  provider command is a resolver error — no closure, lock, or surface is built for a command the
  provider does not export.
- **Ordering.** Topological sort so providers install before consumers. A cycle is a hard error.
- **Lock.** Resolution writes `Skillfile.lock` (section 18): per node the exact commit, hashes,
  active surfaces, source identity, the incoming constraints, and the trust pin.
- **Install from lock, with an explicit locked mode.** `csk resolve` is the only command that
  writes the lock. `csk install --locked` installs strictly from the lock and **fails if the lock
  is absent or stale** — the reproducible CI path. Plain `csk install` may create a **missing** lock
  on first run; if a lock **exists but is stale** it **fails with an actionable message** (run
  `csk resolve`), never silently re-resolving an existing lock. Reproducibility must never depend on
  where the command ran, so CI uses `--locked`. `csk upgrade` = fetch + `csk resolve` + install for
  the old UX; `csk update` keeps its existing fetch-only meaning and does not write the lock.

Why exact + lock and not ranges: it keeps CocoaSkills' reproducibility property, needs no version
solver or semver contract yet, and makes upgrades deliberate. Ranges without a lock and a strict
source policy are explicitly rejected.

## 11. Pillar 1 — Active install surfaces

Conflicts and exposure are computed over **activated** surfaces, not over everything a node could
export.

- **Activation is an edge property.** The same provider can be activated differently by different
  consumers. A node's effective active surface is the **union over all incoming edges** (root +
  every consumer).
- **Defaults.** A **direct** skill (root `skills`) activates `context: full` + **all** its
  commands (preserves today's behavior). A **transitive runtime** dependency activates only the
  **explicitly requested** commands. A **transitive context** dependency activates prompt-context
  only, **no** shims.
- **Union precedence.**
  - Effective **context** surface: `full` wins over `none`. (Partial later: union of `refs`,
    unless any edge asks `full`.)
  - Effective **command** surface: union of explicitly activated command names.
- **Collision** is checked over the union of **active shims**: for each command name, at most one
  `(skill, commit)` may be active in the closure. A command a node exports but nobody activates is
  never written to `.agents/bin` and never collides.
- **Granularity boundary.** Activation governs the **shim** (`PATH`) and the **context** (window).
  The **runtime store is all-or-nothing per skill version**: activating any one command copies the
  whole `runtime_roots`. Acceptable (the store is out of context); no per-command runtime
  isolation.
- **The lock stores active surfaces** per node — `{ context, commands, mcp }` plus the
  `activation_edges` that produced them. The effective surface is a first-class, reviewable
  artifact: `csk audit` and a reviewer see exactly what reaches `PATH`, the window, and MCP before
  install.

## 12. Pillar 2 — Contract safety

Core invariant: **both axes are opt-in; neither implies the other; csk materializes only what is
explicitly activated.** A command dependency never auto-pulls context; a context dependency never
auto-installs commands.

- Every `dependencies.commands.*.type=skill` MUST have a paired `dependencies.skills.<provider>`;
  the ref lives only in `dependencies.skills`.
- `context` object:
  - `{ "mode": "full" }` — pull the provider's SKILL.md and rules into the consumer's context; the
    contract is the provider's.
  - `{ "mode": "none", "contract": "consumer", "refs": ["SKILL.md", "references/wiki-usage.md"] }`
    — runtime-only; the consumer takes responsibility and points at where it documents safe usage
    of the borrowed command. `contract` is meaningful only when `mode: none`.
- **Contract is checked PER runtime edge, not on the effective provider surface.** If consumer A
  uses `wk` with `mode: none` and no consumer contract, A is unsafe even if some other consumer B
  happens to pull `skill-wiki` at `mode: full`. Each runtime edge must explicitly choose
  provider-contract (`mode: full`) or consumer-contract (`mode: none` + `contract` + `refs`). No
  "safe by luck".
- **Tool-without-contract.** A `type: skill` command dependency edge with `context.mode=none` and
  no consumer contract → **warning in normal mode, failure in strict audit**. Cleared by
  `mode: full` or by `contract: consumer` + `refs`.
- **Tiered verification.** Static floor: the `refs` files exist and land in the consumer's
  `INCLUDE_ROOTS` context. Semantic: an LLM/static audit backend (RFC 0006) checks the refs
  actually describe safe usage. Static always; semantic only under audit backends. The static
  check guarantees the artifact exists, not its meaning — and says so.
- **Scope.** Contract safety applies to **skill→skill command dependencies only**. `type: system`
  keeps the declare-plus-hint model.
- **Context-without-tool.** `context.mode=full` on a command-exporting provider with **no**
  activated command → **warning** ("context describes commands, but no shims installed"). v0.9
  emits the warning only; csk never auto-installs the commands. The schema leaves room to suppress
  with explicit intent — `"commands": { "mode": "none" }` alongside `context`, or `runtime: "none"`
  on the `dependencies.skills.<provider>` entry — so context-only deps on methodological /
  rules-only skills do not stay noisy. Not required in v0.9.

## 13. Pillar 3 — Source policy integration

The graph must not be able to launder trust. **Address is not authorization.**

### 13.1 Roles

- `sources` in the root `Skillfile.json` is an **address book** (name → git URL). It does not grant
  trust.
- `source_policy` is the **only authorizer**, and lives **outside the project** (machine/org
  config), so a compromised `Skillfile.json` cannot self-authorize a malicious source.

### 13.2 Path-aware identity (one git-artifact-source model)

Today `audit/source_policy.py` normalizes git sources mostly to **host**, which is too coarse:
`portals/agentic-infra/skills/*` and any other project on the same GitLab are different trust. The
resolver needs a canonical **host + namespace + project** identity, unified across transports:

```
git@gitlab.wildberries.ru:portals/agentic-infra/skills/skill-wiki.git
https://gitlab.wildberries.ru/portals/agentic-infra/skills/skill-wiki
        -> id: gitlab.wildberries.ru/portals/agentic-infra/skills/skill-wiki
```

Normalization: lowercase host, strip a trailing `.git`, strip a leading `/`, keep the path
case-sensitive, ignore the SSH user and the `ssh://`/`https://`/`git@` transport. This is one
**git-artifact-source** model: skill repositories and `includes` (workflow repositories) normalize
through the same function and are authorized by the same policy. The shared normalizer lives in
`csk.source_identity`; the two policies that use it are kept distinct (section 13.5).

### 13.3 Source policy schema and precedence

Patterns match the **full canonical id, host included**. `default` sets the closed-world behavior.

```jsonc
"source_policy": {
  "default": "deny",
  "rules": [
    { "effect": "allow", "pattern": "gitlab.wildberries.ru/portals/agentic-infra/skills/*" },
    { "effect": "allow", "pattern": "gitlab.wildberries.ru/portals/agentic-infra/workflows/*" },
    { "effect": "deny",  "pattern": "gitlab.wildberries.ru/portals/agentic-infra/skills/experimental/*" }
  ],
  "revocations": [
    "gitlab.wildberries.ru/portals/agentic-infra/skills/compromised"
  ]
}
```

Glob semantics: `*` matches one path segment, `**` matches recursively. **Precedence, in order:**

1. **Revocations are always hard deny** and cannot be overridden by any allow — a security revoke
   is not an ordinary rule a more specific allow can route around.
2. Among `rules`, the **most specific** pattern wins.
3. On equal specificity, **deny wins** (and `default` applies when nothing matches).

### 13.4 Includes and sources

- An included `Skillfile.json` contributes **skills + constraints only**. The root supplies the
  `sources`. No root source for an included skill → resolver **fails** (no silent source import).
- The **include's own source** (the workflow repository) is itself a git-artifact source: it is
  authorized by `source_policy` at resolve time, re-validated at install (section 13.5), and
  recorded in the lock (section 18). An include can arrive by ref and change constraints and thus
  the closure, so it is supply chain, not a trusted shortcut.
- Escape valve (later): `import_sources: true` on an include, gated by `source_policy`. Default
  off. Cost: adopting a reusable workflow requires the root to know or explicitly import its
  sources — the deliberate price of not trusting an include's source claims.

### 13.5 Gating, re-validation, and the two policies

- **Authorization happens before any network git operation.** The resolver authorizes a source
  identity before any clone / fetch / archive of that skill or include; unauthorized sources are
  never cloned, fetched, or inspected. `revoke` and the rules apply to the whole closure (skills +
  includes); install re-validates the locked sources again before materialization.
- **`csk install` re-validates.** Install re-checks every locked source against the **current**
  policy; a source revoked after the lock was written fails the install even with a valid lock.
- The lock's recorded `matched_rule` is **diagnostic only, not authorization** — install always
  re-checks current policy; the lock never grants trust.
- Two distinct policies share only the normalizer: `source_policy.authorization` (allow/revoke for
  install) and `audit.source_policy.classification` (internal/public for cloud audit). They are
  named separately to avoid mixing security boundaries.

## 14. Conflicts

- **One skill name per closure** → exactly one commit across the closure.
- **One active command per `.agents/bin`.** Two different skills activating the same command name
  (`wk`, `mr`) → error.
- **Constraint conflict → error**, not a silent winner. In v0.9 this is the different-commit case
  of section 10; different majors of a command would mean a different interface that makes a
  consumer's SKILL.md instructions wrong, so they are never silently merged.
- **Root override** is allowed only if it resolves to a compatible commit (section 10).
- **No command aliasing in v0.9.** Aliasing (`skill-wiki/wk`) would make SKILL.md instructions
  diverge from the real `PATH`. Command names stay stable and match what SKILL.md references. A
  documented re-export path may be added later without a breaking change.

## 15. Capabilities: local node vs effective closure

- **Local** `capabilities` describes a single node. The consistency check is local and static: a
  node's locally declared command dependencies (`dependencies.commands.*` — the `wk` / `wiki` /
  `glab` it calls) must be a subset of that node's local `capabilities.exec`. `dependencies.skills.*`
  (context) is not necessarily `exec`.
- **Effective** capabilities = the **union of local capabilities across the closure**. `csk plan`
  / `csk audit` computes and surfaces it so the project sees what its full closure can do.
- The distinction matters: the strict check is **local declared-deps vs local declared-exec**, not
  deps vs the effective surface — the latter would be self-fulfilling. **Strict mode fails when a
  node's local dependencies and local capabilities diverge** (the `skill-wiki-memory exec:none + wk`
  case).
- **MCP and capabilities (v0.9 = option B):** MCP requirements live in `dependencies.mcp_servers`
  and the effective closure surface only; v0.9 does **not** add a `capabilities.mcp` field. Network
  egress, if known, must still be reflected in `capabilities.network`.
- Strict audit applies to **every node in the closure**, by schema generation:
  - **v1 / v2** (no capabilities): strict requires a content-hash pin or migration.
  - **v3** (capabilities, no dependency graph): strict-compatible as a **leaf / provider**, but may
    not declare a new dependency graph.
  - **v4**: a full dependency node.

## 16. Scope interaction (project / global)

- Dependencies resolve **project-local** by default; a command dependency needs a specific commit,
  and relying on a possibly-different global skill would break reproducibility.
- Global skills remain a user convenience and are **never a silent provider** for a project's
  closure. The lock always pins the project-local node. This is covered by tests (section 25),
  not prose alone.

## 17. MCP

- Modeled as a **provider contract**: `dependencies.mcp_servers.<name> = { tools: [...], required,
  hint }`. Not a `capabilities.mcp` field in v0.9 (section 15).
- T1 (this RFC): declared and surfaced; not enforced (csk cannot `shutil.which` an MCP tool).
- T2 (designed, later): if the agent's MCP config is discoverable, warn on an absent required
  server/tool.
- T3 (deferred, own RFC): a skill ships an MCP server (`provides.mcp_servers`) and csk writes
  adapter configs — a new install target with its own trust and network implications.

## 18. Lockfile — `Skillfile.lock`

Committed alongside `Skillfile.json` (intent + resolution). Generated `.agents/**` stays
uncommitted. The lock is a first-class API object, not a cache. Hashes are split so it is always
clear *what* was hashed, and policy is **not** part of the resolution hash (a benign allow-rule
edit must not make the lock stale; install re-validates policy separately).

```jsonc
{
  "schema_version": 1,
  "resolver_version": "0.9.0",

  // stale detection: root Skillfile + included Skillfiles + source addresses + exact refs.
  // policy is NOT included here.
  "resolution_input_hash": "<sha256>",
  // diagnostic only, optional; never used for trust or staleness
  "policy_snapshot_hash": "<sha256>",

  "includes": {
    "incident-management": {
      "ref": { "kind": "tag", "value": "v1.0.0" },
      "commit": "<full-sha>",
      "manifest_path": "Skillfile.json",
      "manifest_hash": "sha256:<hash of the included Skillfile only>",
      "snapshot_hash": "sha256:<optional full repo snapshot>",
      "source": { "id": "gitlab.wildberries.ru/portals/agentic-infra/workflows/incident-management",
                  "matched_rule": "allow gitlab.wildberries.ru/portals/agentic-infra/workflows/* (diagnostic)" }
    }
  },

  "nodes": {
    "skill-wiki": {
      "commit": "<full-sha>",
      "hashes": {
        "snapshot": "sha256:<full git-archive snapshot hash, what audit/trust verifies>",
        "context":  "sha256:<installed prompt-context hash, what the install marker verifies>",
        "runtime":  "sha256:<runtime_roots hash, optional>"
      },
      "source": { "id": "gitlab.wildberries.ru/portals/agentic-infra/skills/skill-wiki",
                  "matched_rule": "allow gitlab.wildberries.ru/portals/agentic-infra/skills/* (diagnostic)" },
      "pulled_by": [ { "from": "skill-wiki-memory", "ref": { "kind": "tag", "value": "v1.4.2" } } ],
      "activation_edges": [ { "from": "skill-wiki-memory", "context": "none", "commands": ["wk"] } ],
      "surfaces": { "context": "none", "commands": ["wk"], "mcp": [] },
      "trust": { "pin": "sha256:<...>" }
    }
  }
}
```

Per-node `hashes` separates the three hashes implementations otherwise conflate: `snapshot` (what
audit/trust verifies), `context` (what the install marker historically verifies), and optional
`runtime`. `matched_rule` is diagnostic; install re-checks current policy (section 13.5).

## 19. Migration and backward compatibility

- **Direct skills are unchanged.** A flat `Skillfile.json` with exact git refs keeps working; the
  legacy `tag`/`branch`/`revision` keys are accepted as sugar for `ref`. A direct skill defaults to
  `context: full` + all commands.
- **Older node schemas install as leaves.** v1/v2/v3 `csk-skill.json` have no `dependencies` block.
  Strict treatment by generation: v1/v2 require a content-hash pin or migration; **v3 stays
  strict-compatible as a leaf/provider** but may not declare dependency edges; v4 is a full node
  (section 15).
- **`commands.type=system`** stays installable, deprecated; `csk` may warn and point to
  `dependencies.commands`.
- **`dependencies.json` removed** (its own task).
- **Lockfile rollout.** `csk resolve` is the sole lock writer. `csk install --locked` requires a
  fresh lock (CI path); plain `csk install` may create a missing lock on first run but **fails on a
  stale lock** (run `csk resolve`). Drift between `Skillfile.json` / included Skillfiles and the
  lock, compared via `resolution_input_hash`, is a `csk status` signal. A policy change alone does
  not make the lock stale — install re-validates policy separately. `csk update` keeps its
  fetch-only meaning; `csk upgrade` = fetch + resolve + install.

## 20. Worked example

`workflows/incident-management` (root) declares `skill-gitlab`. Adding durable memory:

1. Root `Skillfile.json` adds `skill-wiki-memory` at `ref tag v2.1.0` and a `sources` entry for it.
2. `skill-wiki-memory`'s node manifest declares `dependencies.skills.skill-wiki` at
   `ref tag v1.4.2` with `context.mode = none, contract: consumer, refs: ["references/wiki-usage.md"]`,
   and `dependencies.commands.wk = { type: skill, skill: skill-wiki, command: wk }`.
3. Resolver closure: `{ skill-gitlab, skill-wiki-memory, skill-wiki }`. `skill-wiki`'s source must
   be in the root `sources` (else fail) and pass `source_policy`.
4. Exact-ref unification: `skill-wiki` resolves `tag v1.4.2 → commit abc123`; no other constraint;
   selected commit `abc123`. Topological order puts `skill-wiki` before `skill-wiki-memory`.
5. Active surfaces: `skill-wiki` activated **runtime-only** with command `wk` (no context), because
   the consumer chose `mode: none` and provided a consumer contract. Contract safety passes per
   edge (refs present; semantic check under audit backends).
6. Lock records all nodes (with `hashes`, incoming refs, `activation_edges`, surfaces, trust pins),
   the include, and the source identities.
7. Install: `wk` shim into `.agents/bin`; `skill-wiki` SKILL.md is **not** added to context;
   `skill-wiki-memory`'s own SKILL.md (with `references/wiki-usage.md`) carries the safe-usage
   contract.

Result: the memory pattern uses `wk` with an explicit, audited contract; `skill-wiki`'s full
context never silently enters the window.

## 21. Open questions and sub-RFCs

- **Skill semver contract**: what major/minor/patch mean for an exported command's interface.
  Until it exists, **range constraints are not accepted; v0.9 accepts only exact tags or exact
  revisions; the lock records the resolved commit and content hash.** Ranges + MVS arrive with the
  contract.
- **Security-advisory force-raise** channel: how a known-vulnerable transitive ref is raised under
  exact-only/MVS, tied to the audit/trust layer; the lock should carry a security pin.
- **System dependency version probes**: how to declare requirements such as `glab >= 1.50.0`
  without letting a skill manifest execute arbitrary checks. The intended direction is an enum of
  csk-owned built-in probes (`builtin:git`, `builtin:python`, `builtin:glab`, ...), not
  manifest-provided args or scripts.
- **Source policy distribution**: how org policy reaches a machine, and how `source_policy.py`
  grows from host-granular to the path-aware identity in section 13.2.
- **Partial context** (`context.mode = partial`, `refs`): pulling only specific provider references
  to protect the local-model context budget.
- **MCP T3** (`provides.mcp_servers` as an install target).
- **Command re-export / namespacing** beyond v1's flat fail-fast model.

## 22. v0.9 foundation slice

This RFC spans the foundation plus several large future blocks. v0.9 ships only the foundation.

In v0.9:

- `Skillfile.json` schema v2 parser: `sources`, `skills`, `includes`, `overrides`. **Exact refs
  only**; ranges rejected (section 10).
- `csk-skill.json` schema v4 parser: `dependencies.commands`, `dependencies.skills`,
  `dependencies.mcp_servers`; older schemas as leaves (section 19).
- **Path-aware `source_policy`** identity and allow/deny/revoke semantics (section 13). Blocking,
  not optional.
- Closure builder (exact-ref unification) + the active-surfaces activation model (sections 10, 11).
- Lockfile model with `resolver_version`, split hashes, `includes`, `activation_edges` (section 18).
- Install from lock over today's runtime/context primitives, with `--locked` (section 10).
- Contract safety with the **static** refs check only; semantic verification is an audit
  enhancement, not a v0.9 gate (section 12).
- MCP T1 only: metadata surface, no enforcement, option B for capabilities (sections 15, 17).
- Direct-skill flat compatibility preserved (sections 11, 19).

Deferred, each to its own release/RFC: range constraints + the skill semver contract; system
dependency version probes; the security-advisory force-raise channel; MCP T2/T3; partial context;
command re-export / namespacing; `import_sources` for includes.

## 23. Decisions (formerly open) for v0.9

- **D-1 — exact-only.** No temporary "highest matching tag" discovery; that is pseudo-semver
  without a contract.
- **D-2 — install modes.** `csk resolve` is the only lock writer; `csk install --locked` is strict;
  plain `csk install` may create a missing lock on first run but **fails on a stale lock**.
  `csk update` stays fetch-only; `csk upgrade` = fetch + resolve + install. CI and wrappers use
  only `--locked`.
- **D-3 — includes cache is a sibling store**, not `skills_root` (a workflow include is not an
  installable skill): `~/.cocoaskills/cache/workflows/<source-id-hash>/<commit>/snapshot/`. The lock
  stores include `ref`, `commit`, `manifest_hash`, and source id.
- **D-4 — root direct activation** is recorded as a synthetic edge; `["*"]` expands to the concrete
  command list in `surfaces.commands` at materialization:

  ```jsonc
  "activation_edges": [ { "from": "<root>", "reason": "direct", "context": "full", "commands": ["*"] } ]
  ```

- **D-5 — global never a provider** is enforced by tests, not prose: a transitive dependency on a
  globally-installed skill still requires a root `sources` entry and a project-local lock node.
- **D-6 — MCP T1 output.** `csk status`: a requirement summary; `csk audit --json`: part of the
  effective closure surface:

  ```jsonc
  "mcp": [ { "node": "skill-wiki-memory", "server": "wiki-mcp",
             "tools": ["article.search", "article.read"], "required": true, "enforced": false } ]
  ```

- **D-7 — system dependency versions.** v0.9 checks only that a system command exists on `PATH`.
  Future version requirements may be declared only through csk-owned built-in probes. Manifests
  must never provide executable version-check commands, arguments, shell snippets, install hooks, or
  post-install hooks.

## 24. v0.9 implementation outline

A minimal, layered cut (each step is independently testable):

1. **Source identity + authorization.** `csk.source_identity` normalizer (host/path, ssh/https/scp
   equivalence); glob matcher (`*`, `**`); `source_policy.authorization` allow/deny/revoke.
2. **Manifest parsers only.** `Skillfile.json` v2 (`sources`, `skills`, `includes`, `overrides`);
   `csk-skill.json` v4 (`dependencies.commands` / `.skills` / `.mcp_servers`). Reject ranges.
3. **Resolver data model.** `Node`, `Edge`, `Constraint`, `ActivationEdge`, `Closure`,
   `ResolvedNode`, `ResolvedInclude`.
4. **Exact-only closure builder.** Includes expansion, root source lookup, dependency expansion,
   cycle detection, same-skill ref→commit unification and conflict detection.
5. **Activation planner.** Direct / runtime / context activation; command collision over active
   shims only; static contract checks per edge.
6. **Lockfile read/write/status.** `Skillfile.lock`; stale detection via `resolution_input_hash`;
   current-policy re-validation on install.
7. **Install from lock.** Reuse current prompt-context and runtime primitives; `--locked` never
   re-resolves.
8. **Audit/status integration.** Closure, active surfaces, effective capabilities, MCP T1 metadata,
   source-policy matches as diagnostic only.

## 25. v0.9 acceptance tests

- `^1.0.0` in a v0.9 dependency → parse error.
- Two exact refs for the same skill resolving to the **same commit** → allowed.
- Two exact refs resolving to **different commits** → conflict.
- Transitive dependency without a root `sources` entry → resolver fails.
- An included Skillfile's source is authorized and lock-recorded.
- An included Skillfile's own `sources` are ignored by default.
- A revoked source fails install even with a fresh lock.
- A command exported but not activated does **not** collide.
- Two active shims with the same command name collide.
- A runtime-only skill command with `context.mode=none` and no consumer contract → warning
  normally, strict-audit failure.
- Consumer contract `refs` must exist and be under the consumer's prompt-context include roots.
- `skill-wiki-memory` with `capabilities.exec: "none"` and `dependencies.commands.wk` → strict
  failure.
- A globally-installed provider is **not** used to satisfy a project closure.
- The lock is stale when the root or an included Skillfile changes; **not necessarily stale** when
  only policy changes (policy is not in `resolution_input_hash`); install re-validates policy.
- A `dependencies.commands.*.type=skill` edge whose provider does not export that command with
  `type: script` → resolver error.
- A `branch` ref on a dependency edge → parse error; a `branch` on a direct/legacy skill resolves
  to a concrete commit, and `--locked` installs that commit even after the branch head moves.
- A source is authorized **before** any clone/fetch; an unauthorized source is never fetched.
- A stale existing lock fails plain `csk install` (not only `--locked`); a missing lock is created
  on first run.
- A v0.9 `type: system` dependency with manifest-provided `check`, command arguments, `script`,
  `install`, `post_install`, `version`, or `version_probe` fields → parse error.
