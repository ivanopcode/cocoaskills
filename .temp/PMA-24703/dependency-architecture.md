# RFC (draft): Skill & Workflow Dependencies in CocoaSkills

Status: architecture exploration for PMA-24703. No implementation.

## 1. Problem

PMA-24703 framing: "how to work with transitive `Skillfile.json`."

A *workflow* (e.g. `workflows/incident-management`) is a project whose `Skillfile.json`
composes several skills (skill-gitlab, skill-grafana, skill-band, ...). Today CocoaSkills
installs a **flat list** of skills, each one standalone. There is no notion of a skill
depending on another skill, no transitive resolution, no install ordering, no conflict
handling, no version negotiation.

But skills already depend on each other in practice. The first concrete case is in the
wild and only half-designed (see §3). The task is to design the dependency layer properly,
because it is foundational: it touches the manifest schema, the resolver, the runtime/shim
layer, the audit/security model, and the global/project scope rules.

## 2. Current state (grounded)

- `Skillfile.json` (project manifest): `skills: [{ name, git, tag|branch|revision }]`.
  Each skill is pinned by an **exact git ref**. Reproducible, no version ranges.
- `csk-skill.json` (skill manifest):
  - `commands`: mixes two roles —
    - `type: script` — a command the skill **provides** (shim into `.agents/bin`).
    - `type: system` — an external utility the skill **requires** (checked via `shutil.which`,
      never installed by csk).
  - schema v2 adds `runtime_roots`; schema v3 adds a `capabilities` envelope
    (`network`, `filesystem`, `exec`, `secrets`, `env_read`, `prompt_scope`) for `csk audit`.
- `dependencies.json`: legacy, deprecated (PMA-24810), copied to context but never parsed by
  csk; a skill-local `bootstrap_runtime.py` reads it for a missing-tool warning.
- Installer: iterates `for decl in project_manifest.skills` — **no graph, no ordering, no
  transitive step**. The only "dependency" concept is `type: system`.
- Command shims: a **flat namespace** — `.agents/bin/<command>` (project) and
  `~/.cocoaskills/global/bin/<command>` (global), precedence project > global > system.
- Runtime store: **content-addressed by commit** — `~/.cocoaskills/runtime/<skill>/<commit>/`.
  Multiple versions of the same skill *can* physically coexist here.
- MCP: not referenced anywhere in csk. Greenfield.

## 3. The anchor: the `wk` case (already in the wild)

- `skill-wiki` (v2): **provides** `wk` (`type: script`) and **requires** the external `wiki`
  CLI (`type: system`).
- `skill-wiki-memory` (v3): no own commands; declares an **undocumented `dependencies` block**:

  ```json
  "dependencies": {
    "commands": {
      "wk": { "type": "skill", "skill": "skill-wiki", "command": "wk",
              "hint": "Install skill-wiki in the same Skillfile.json; it exports the wk command..." }
    }
  }
  ```

This is a real strawman for "skill depends on skill via an exported command." csk does **not**
act on it yet — the hint asks the human to add skill-wiki manually. PMA-24703 should formalize
and generalize this.

Note tensions this single example already exposes:
- The dependency is **direct/named** (couples to `skill-wiki` + command `wk`), not capability-based.
- `capabilities.exec` is `"none"` here, yet the skill calls `wk` (provided by another skill).
  So "dependency on a skill command" and "exec capability" are modeled separately and are
  currently inconsistent.
- It contradicts PMA-24759's wording ("declare in dependencies.json") — the real direction is a
  `dependencies` block inside `csk-skill.json`, not `dependencies.json`.

## 4. Dependency taxonomy

Five kinds the architecture must cover (from the task brief, refined):

| # | Kind | Direction | Today | Resolvable by csk? |
|---|------|-----------|-------|--------------------|
| D1 | skill → system utility | requires an external binary on PATH | `commands.type=system` + `shutil.which` | check only (never install) |
| D2 | skill → skill (command) | requires another skill's exported command | `dependencies.commands.type=skill` (wild strawman) | NOT yet |
| D3 | skill → skill (context) | needs another skill's SKILL.md/refs co-present in agent context | none | NOT yet |
| D4 | skill → MCP | needs an MCP server/tool available in the agent session | none | NOT yet |
| D5 | skill provides utility | the supply side of D2: a skill *exports* a command another may need | `commands.type=script` | provides (flat shim) |

D5 is not a separate dependency; it is the provider half of D2. The design question is whether
D2 couples to a **named skill** (D2a) or to a **named capability/command that any skill may
provide** (D2b). See §8.

## 5. Manifest schema direction (a clean v4)

Separate **provides** from **requires**, and consolidate all requires under one `dependencies`
block. This deprecates `dependencies.json` and removes the `type: system`-in-`commands` smell.

```jsonc
{
  "schema_version": 4,
  "runtime_roots": ["scripts"],
  "capabilities": { ...v3 envelope... },

  // PROVIDES — what this skill exports
  "commands": {
    "wk": { "type": "script", "unix_path": "scripts/wk", "win_path": "scripts/wk.cmd" }
  },

  // REQUIRES — everything this skill needs to function
  "dependencies": {
    "system":  [ { "command": "wiki", "hint": "..." } ],                 // D1
    "skills":  [ { "skill": "skill-wiki", "range": "^2.0.0",            // D2
                   "commands": ["wk"], "hint": "..." } ],
    "mcp":     [ { "server": "sentry", "tools": ["search","execute"],   // D4
                   "hint": "..." } ],
    "context": [ { "skill": "skill-wiki" } ]                            // D3 (optional)
  }
}
```

Reconciliation rule: every `dependencies.system`/`dependencies.skills[].commands` entry must be
covered by `capabilities.exec` (the audit allowlist). The resolver uses `dependencies`; the
auditor uses `capabilities`; they must agree.

Open: keep D2 keyed by command name (`dependencies.commands` like the wild) or by skill
(`dependencies.skills[].commands`)? Skill-keyed is cleaner for resolution; command-keyed is
closer to how the agent thinks ("I need `wk`").

## 6. Resolution model (transitive)

Treat each skill repo as carrying its own requires. The closure is a graph:

- Nodes: skills (top-level from project `Skillfile.json`, plus transitively required skills).
- Edges: D2/D3 skill→skill requirements; D1/D4 are leaf requirements (no further skills).
- Build: start from project `Skillfile.json`, read each skill's `csk-skill.json.dependencies`,
  expand transitively until closure.
- Order: topological sort so providers install before consumers (shims for `wk` exist before a
  consumer's bootstrap checks them). Detect cycles → hard error.
- Where transitive skills come from: a skill's `dependencies.skills[]` must carry enough to
  *fetch* the dependency (a `git` URL and a version), exactly like a `Skillfile.json` entry. So
  a skill manifest's dependency entry is effectively a mini-Skillfile line — this is the literal
  meaning of "transitive Skillfile.json."

Two installation shapes:
- **Hoisted/flat** (npm-like): all transitive skills install once into the project's
  `.agents/skills/`. Simple agent context, but version conflicts must collapse to one version.
- **Nested/isolated**: each consumer gets its own copy of a dependency at its pinned version.
  The runtime store already supports this (content-addressed by commit). But shims are flat
  (§9), so nesting helps runtime not the command namespace.

## 7. Versioning & semver (the central tension)

Today: exact git-ref pins. Reproducible by construction. This is a deliberate CocoaSkills value
("pinned versions, reproducible installs"). Introducing dependency edges forces a choice:

- **Option A — Minimal Version Selection (Go-style) + lockfile.** Dependencies declare a
  *minimum* semver (`>=2.1.0`); the resolver picks the lowest version that satisfies all
  constraints; a `Skillfile.lock` records the exact resolved commits + content hashes. Highly
  reproducible, no SAT solver, predictable upgrades. Fits CocoaSkills' ethos best.
- **Option B — Ranges + npm-style resolution.** Dependencies declare ranges (`^2.0.0`); the
  resolver maximizes within ranges; lockfile pins the result. Familiar, flexible, but needs
  range arithmetic and invites churn; "latest within range" fights reproducibility.
- **Option C — Exact pins only, project is the single source of truth.** Dependency edges are
  *validated, not auto-pinned*: a skill says "I need skill-wiki ~2.x" only as a compatibility
  assertion; the **project Skillfile must explicitly list every skill** (including transitive)
  at an exact ref, and csk merely checks the declared edges are satisfied. No auto-fetch of
  transitive skills. Maximum control + reproducibility, minimum magic — but pushes transitive
  bookkeeping onto the project author.

Prerequisite for A/B: skills must publish **semver tags with a compatibility contract** (what a
major/minor/patch means for an exported command's interface). Today tags are pins, not
contracts. This is itself a sub-RFC (semver policy for skills).

Recommendation to debate: **A (MVS) + `Skillfile.lock`**, with dependency declarations allowed
to be a range OR an exact ref, always resolved to exact in the lock. Keeps reproducibility,
adds the minimum machinery, and the lockfile is the artifact a workflow commits.

## 8. Conflict resolution

Two distinct conflicts:

### 8a. Version conflict (diamond)
A needs wiki@1, B needs wiki@2. Strategies:
- Single-version collapse (MVS/npm-flat): pick one; if ranges are incompatible → hard error with
  the conflicting constraint chain. Deterministic, but a real conflict blocks the install.
- Multi-version coexistence: runtime store allows it (commit-addressed), so both wiki@1 and
  wiki@2 runtimes can exist. BUT the exported `wk` shim is a singleton (§9) → the *command*
  cannot be both versions. So multi-version only helps if the dependency is consumed as a
  *library/runtime* (sibling files) rather than as a *shared command*.

Conclusion: for **command** dependencies (D2), version conflicts must collapse to one version.
Multi-version is only meaningful for non-command runtime dependencies (not yet a use case).

### 8b. Name conflict (two skills export the same command)
skill-gitlab and skill-x both export `mr`. Flat `.agents/bin` → collision. Strategies:
- Fail-fast detection: refuse the install, name both providers; the project resolves by
  removing one, renaming, or setting precedence. Deterministic, no silent winner.
- Namespacing: install `skill-gitlab/mr` and `skill-x/mr`, with optional unqualified alias when
  unambiguous. Avoids hard failure, complicates the agent's path model.
- Precedence: project Skillfile order wins (last/first), like PATH shadowing. Implicit, can hide
  bugs.

Recommendation: **fail-fast + explicit resolution** now; namespacing as a later opt-in. Matches
the project's "deterministic, no magic" posture.

## 9. Command namespace

Current: flat `.agents/bin/<command>`, global `~/.cocoaskills/global/bin/<command>`, precedence
project > global > system. Transitive skills multiply collisions. The shim namespace is the real
constraint behind §8a and §8b — runtime can be multi-version, the *command name* cannot.

Options: (a) keep flat + conflict detection; (b) per-skill namespace dirs with alias resolution;
(c) explicit re-export/rename in the consuming manifest (`uses: { wk: skill-wiki/wk }`). Start
with (a); design the manifest so (c) is addable without a breaking change.

## 10. Security / supply chain

Transitive deps install skills the project never directly vetted. Implications:

- **Audit the closure, not the direct set.** `csk audit` must walk transitive skills; strict
  install gates apply to every node. Schema <v3 (undeclared capabilities) anywhere in the
  closure blocks a strict install or requires a content-hash pin.
- **Capability propagation.** A transitive skill's `network`/`exec`/`secrets` are now part of the
  project's surface. Surface the union; let the project see/approve what its full closure can do.
- **Trust pinning.** The `Skillfile.lock` should carry content hashes for every transitive node,
  and the trust workflow (pin-by-hash) extends to them. A dependency edge must not be able to
  silently swap a transitive skill's content.
- **Fetch hardening.** Transitive `git` URLs go through the same hardened clone path
  (`GIT_ALLOW_PROTOCOL`, `--`, no remote-helper URLs) already in csk for top-level skills.

## 11. Scope interaction (project / global)

If a project skill depends on skill-wiki and skill-wiki is installed **globally**, does the
dependency resolve against the global install or force a project-local copy?

- Project precedence today is project > global. For a *command* dependency, the consumer needs a
  specific version; relying on a globally-installed (possibly different) version breaks
  reproducibility. So **dependencies resolve project-local by default**, ignoring global, and the
  lock pins the project-local version. Global stays a user convenience, never a silent dependency
  provider for a project's closure.
- Open: allow an explicit "satisfy from global if version matches" opt-in to save disk? Probably
  not worth the non-determinism initially.

## 12. The MCP dimension (D4)

MCP servers/tools are configured at the **agent/session** level (e.g. Claude Code MCP config),
not installed by csk. So D4 is fundamentally a *declare-and-check*, but the check cannot be
`shutil.which`. Tiers:

- T1 (documentary): the skill declares `dependencies.mcp` and the auditor/capabilities surface
  it; no enforcement. Lowest effort, immediate value (the agent/operator knows the skill needs
  MCP server X with tools Y).
- T2 (best-effort check): if the agent's MCP config is discoverable on the machine, csk warns
  when a required server/tool is absent — analogous to the `type: system` warning.
- T3 (provisioning): csk writes MCP server entries into agent adapter configs (a new install
  target alongside `.claude`, `.codex`, ...). Largest surface, ties into capabilities/network
  egress, and into trust (an MCP server is remote code). Defer.

Recommendation: ship T1 with the manifest (`dependencies.mcp`), design for T2; treat T3 as a
separate future RFC. Note: MCP requirements interact with `capabilities.network` (an MCP server
implies egress) — keep them consistent.

## 13. Lockfile

Strongly recommended regardless of A/B/C: a committed `Skillfile.lock` recording the fully
resolved closure — every node's exact commit, content hash, and the edge that pulled it. This is
what makes transitive resolution reproducible and auditable, and it is the natural home for the
trust pins (§10). A workflow commits `Skillfile.json` (intent) + `Skillfile.lock` (resolution);
generated `.agents/**` stays uncommitted as today.

## 14. Strawman (for debate, not decision)

1. Manifest v4: split `commands` (provides, script-only) from `dependencies`
   (`system` | `skills` | `mcp` | `context`). Migrate `type: system` out of `commands`.
2. Skill→skill (D2) is **direct/named** (skill + commands), carrying a `git` URL + version so it
   is fetchable (a transitive Skillfile line).
3. Resolution: transitive closure, topological order, cycle = error.
4. Versioning: **MVS + `Skillfile.lock`**; ranges allowed in declarations, exact in the lock;
   define a skill semver contract as a sub-RFC.
5. Conflicts: version → collapse to one (error if unsatisfiable); name → fail-fast + explicit
   resolution. Command namespace stays flat for now, with a documented path to re-export.
6. Security: audit + capability union + trust pins over the **whole closure**; hardened
   transitive fetch.
7. Scope: dependencies resolve project-local; global is never a silent provider.
8. MCP: `dependencies.mcp` at tier T1 now; T2 designed; T3 deferred.
9. `dependencies.json` removed (PMA-24810); the `dependencies` block is the single source.

## 15. Open questions (need a decision)

- Q1. MVS vs ranges vs exact-only (§7) — the single biggest fork.
- Q2. Do transitive skills auto-fetch, or must the project list them explicitly (Option C)?
- Q3. Skill-keyed vs command-keyed dependency declaration (§5).
- Q4. Command-name conflicts: fail-fast vs namespacing as the default (§8b/§9).
- Q5. Is D3 (skill→skill *context* co-presence) a real need, or is every skill→skill edge really
  about a command (D2)? (skill-wiki-memory's `prompt_scope` "operate through skill-wiki" hints D3
  might matter.)
- Q6. MCP enforcement tier to commit to first (§12).
- Q7. Does a skill semver contract exist/should it (what major/minor/patch *mean* for an exported
  command) — prerequisite for Q1.
- Q8. Lockfile format/owner and how it carries trust pins (§13/§10).

---

# Revision 2 — converged model (post-review)

The load-bearing reframe: **PMA-24703 is a `Skillfile.json` v2 RFC (the graph + its
resolution policy) + dependency closure + lockfile, NOT primarily a `csk-skill.json`
extension.** Two manifests, clean ownership:

- **`csk-skill.json` = a NODE.** Declares what the skill *provides* and *requires* by
  **name + constraint**, never by source. Local capabilities describe this node only.
- **`Skillfile.json` v2 = the GRAPH + POLICY.** Owns direct skills, `includes`
  (workflow→workflow), `sources` (name→git resolution), `overrides`, `agents`, `locale`,
  and produces `Skillfile.lock`.

## R2.1 Two dependency axes (the key correction)

`skill-wiki-memory -> wk` is ambiguous and must be split into two orthogonal axes:

- **Runtime dependency** — "I need the executable `wk` on PATH." Guarantees `.agents/bin/wk`.
  Declared in `dependencies.commands` (`type: skill` or `type: system`). The provider's
  runtime is installed **without** emitting its prompt-context.
- **Context dependency** — "I need skill-wiki's contract: its SKILL.md, safety rules,
  prompt-context." Guarantees the agent sees `skill-wiki/SKILL.md`. Declared in
  `dependencies.skills` (`context: required`).

These are independent: command-only, context-only, or both. If a consumer relies on the
provider's *rules* (not just its binary), it MUST declare a context dependency — otherwise the
agent has a tool with no contract for safe use.

**Alignment win:** this maps exactly onto csk's existing two-layer install —
prompt-context (`INCLUDE_ROOTS` → `.agents/skills/`) vs runtime (`runtime_roots` → runtime
store + shims). A runtime dep installs the runtime layer only; a context dep installs the
context layer. The split is implementable without inventing a new install mechanism.

## R2.2 Node manifest (`csk-skill.json`)

```jsonc
{
  "schema_version": 4,
  "runtime_roots": ["scripts"],
  "capabilities": { ...local node only... },

  // PROVIDES — keep physically; commands stays the export surface (non-breaking)
  "commands": {
    "wk": { "type": "script", "unix_path": "scripts/wk" }
  },

  // REQUIRES
  "dependencies": {
    // command requirements — system and skill are ONE model, provider differs
    "commands": {
      "wiki": { "type": "system", "command": "wiki", "hint": "..." },     // D1 (runtime)
      "wk":   { "type": "skill", "skill": "skill-wiki", "command": "wk" }  // D2 (runtime)
    },
    // context/semantic requirements — needs the provider's rules in prompt-context
    "skills": {
      "skill-wiki": { "version": "^1.0.0", "context": "required" }         // D3 (context)
    },
    // MCP requirements — a provider contract (tools), not just a server name
    "mcp_servers": {
      "wiki-mcp": { "tools": ["article.search", "article.read"], "required": true, "hint": "..." }
    }
  }
}
```

Rules:
- `commands.type=system` (legacy in `commands`) is **deprecated but tolerated**; new system
  utilities go to `dependencies.commands` with `type: system`.
- `dependencies.commands.*` carry **no git URL** — only name + (for skills) the skill name and a
  version constraint. Source resolution is the graph's job (`Skillfile.json sources`).
- `dependencies.json` is removed (PMA-24810); this block is the single source.

## R2.3 Graph manifest (`Skillfile.json` v2)

```jsonc
{
  "schema_version": 2,
  "project": { "alias": "incident-management" },
  "agents": ["claude_code", "codex_cli"],
  "locale": "ru",

  // where skills come from — root owns WHERE (supply-chain control)
  "sources": {
    "skill-wiki": { "git": "git@gitlab.wildberries.ru:portals/agentic-infra/skills/skill-wiki.git" }
    // or a registry base + naming convention
  },

  // direct skills (constraints; source via `sources`)
  "skills": [ { "name": "skill-gitlab", "version": "^1.1.0" } ],

  // workflow → workflow composition (transitive Skillfile.json)
  "includes": [
    { "name": "incident-management",
      "git": "git@...:workflows/incident-management.git", "tag": "v1.0.0" }
  ],

  // root may pin/force a version, but only if it satisfies all constraints
  "overrides": { "skill-wiki": { "version": "1.4.2" } }
}
```

- `includes` pull another Skillfile's **skills set**; the **root** still owns agents, locale,
  sources, overrides, and the single lock. A workflow becomes a reusable artifact, not a
  copy-pasted skill list.
- Resolution writes **`Skillfile.lock`** (the full closure: every node's exact commit + content
  hash + the edge/constraint that pulled it + trust pins).

## R2.4 Versioning — A (MVS) + lock, refined

- root `Skillfile.json` is the source of truth.
- transitive deps express **constraints**, not git URLs; the URL comes from root `sources`
  (trusted registry/sources) — a transitive dep cannot inject an arbitrary source.
- MVS picks the minimal satisfying version; root `overrides` may raise it iff still satisfying.
- final commits always written to lock; `csk install` installs **from lock**.
- `csk resolve` / `csk update` change the lock **explicitly** (MVS is conservative → security
  patches require an explicit `csk update`, by design, for reproducibility).

## R2.5 Conflicts — fail-fast (sharpened)

- one skill name per closure; one command per `.agents/bin`.
- two different skills exporting the same command (`wk`/`mr`) → error.
- incompatible version constraints → error (not "best choice").
- root override allowed only if it satisfies constraints.
- **no command aliasing in v1** — otherwise SKILL.md prompt instructions diverge from the real
  PATH. Command names must stay stable and match what SKILL.md references.

## R2.6 Capabilities — per-node local + effective closure rollup

- local `capabilities` describes its own node only.
- `csk plan`/`audit` builds **effective capabilities** for the whole closure.
- per-type consistency:
  - `dependencies.commands.*` → must be reflected in the node's effective `exec`.
  - `dependencies.mcp_servers.*` → reflected in effective MCP/network surface.
  - `dependencies.skills.*` (context) → NOT necessarily `exec`.
- strict mode: a node declaring a command dependency while its own `capabilities.exec` is
  `none` is a **failure** (the `skill-wiki-memory exec:none + wk` case).

## R2.7 MCP — provider contract (T1 now)

- model as a contract: `dependencies.mcp_servers.<name> = { tools: [...], required, hint }`.
- symmetric future: `provides.mcp_servers` if a skill ships an MCP server — but that is an
  **install target** (csk writes adapter configs), much bigger than a dependency check. Defer
  to its own RFC.
- keep consistent with `capabilities.network` (an MCP server implies egress).

## R2.8 New open tensions (from the split)

- T-A. **Tool-without-contract.** A runtime command dep on a skill, with no context dep, gives
  the agent a command it has no rules for. Should `csk audit` flag command-dep-on-skill that
  lacks a matching context dep? Or must the consumer re-document safe usage in its own SKILL.md?
- T-B. **Context dep references missing commands.** A context dep pulls skill-wiki's SKILL.md,
  which tells the agent to run `wk` — but if no command dep was declared, `wk` is absent.
  Should a context dep on a command-providing skill imply (or warn about) the command dep?
- T-C. **Context budget.** Transitive context deps inflate the prompt window — directly at odds
  with the local-model context-budget principle. Whole-provider-SKILL.md may be too coarse.
- T-D. **Sources model.** Explicit per-skill `sources` map (secure, verbose) vs registry base +
  naming convention (convenient, a trust assumption) vs both, gated by audit source policy.
- T-E. **Lock ownership with includes.** Single root-owned lock; nested/included locks are
  ignored (root re-resolves the whole closure). Confirm.

---

# Revision 3 — the three pillars (pre-technical-RFC)

The real risk is not MVS or lock — it is **hidden surface expansion**: a command lands on PATH
without its rules, or the transitive graph pulls a source the root never actually trusted. Three
sections close that.

## R3.1 Active install surfaces

Conflicts and exposure are over **activated** surfaces, not all potential exports of a node.

- **Activation is an EDGE property, not a node property.** The same provider can be activated
  differently by different consumers. A node's effective active surface in the closure is the
  **union over all incoming edges** (root + every consumer).
- Defaults:
  - **direct skill** (root `Skillfile.json`): `context: full` + all its commands — preserves
    today's behavior.
  - **transitive runtime dep**: only the explicitly requested commands (e.g. just `wk`).
  - **transitive context dep**: prompt-context only, no shims.
- **Collision** is checked over the **union of active shims**: for each command name, at most one
  (skill, version) may be ACTIVE in the closure. A node that exports `wk-admin` which nobody
  activates is never in the bin → never collides.
- **Granularity boundary:** activation governs the **shim** (PATH) and the **context** (window).
  The **runtime store is all-or-nothing per skill version** — activating any one command copies
  the whole `runtime_roots`. That is fine (runtime store is out of context); just do not expect
  per-command runtime isolation.
- **Lock stores active surfaces** per node: `{context, commands, mcp}`. This makes the effective
  surface a first-class, reviewable artifact — the concrete "no hidden surface" guarantee.

## R3.2 Contract safety (the core invariant)

**Both axes are opt-in; neither implies the other; csk materializes only what is explicitly
activated.** Command-dep never auto-pulls context; context-dep never auto-installs commands.

- Every `dependencies.commands.*.type=skill` MUST have a paired `dependencies.skills.<provider>`;
  version lives ONLY in `dependencies.skills`.
- `context` is an OBJECT from day one (room for partial later): `{ "mode": "full" }`, or
  `{ "mode": "none", "contract": "consumer", "refs": ["SKILL.md", "references/wiki-usage.md"] }`.
- **Tool-without-contract:** a `type:skill` command-dep with `context.mode=none` and no
  consumer-contract → **warning normal / fail strict**. Cleared by either `mode: full` (you take
  the provider's contract) or `contract: consumer` + `refs` (you take responsibility, in writing).
- **Verification is tiered:** static floor — the `refs` files exist and land in the consumer's
  INCLUDE_ROOTS context; semantic — an LLM/static audit backend (v0.8) checks the refs actually
  describe safe usage. Static always; semantic under audit backends.
- Scope: contract-safety applies to **skill→skill command deps only**. `type:system` keeps the
  existing declare+hint model (the external tool owns its own docs).
- **Context-without-tool:** `context.mode=full` on a command-exporting provider with **no**
  activated command → **warning** ("context describes commands, but no shims installed"). v1: just
  the warning, no suppressor. Never auto-install the commands.

## R3.3 Source policy integration (address ≠ authorization)

The graph must not be able to launder trust.

- **`sources` in root `Skillfile.json` = address book** (name → git URL). NOT a trust grant.
- **`source_policy` = the only authorizer.** It lives **outside the project** (machine/org
  config), so a compromised `Skillfile.json` cannot self-authorize a malicious source.
- **Path-aware identity.** Today `audit/source_policy.py` normalizes Git mostly to *host*. The
  resolver needs **host + namespace + project**, in a canonical form unified across SSH and HTTPS
  (`git@host:ns/proj.git` ≡ `https://host/ns/proj`), so `portals/agentic-infra/skills/*` is
  distinguishable from any other project on the same GitLab.
- **Allow + revoke with precedence.** Define glob semantics (segment vs recursive) and a
  precedence rule (most-specific wins / deny-overrides). `revoke source:<pattern>` applies to the
  whole closure **before install**.
- **Install re-validates.** `csk install` (from lock) re-checks every locked source identity
  against **current** policy. A source revoked after the lock was written fails the install — the
  lock records what passed, it does not grant trust.
- **Lock writes** the concrete source identity + the rule/class it matched.

## R3.4 Includes (strict MVP)

- included `Skillfile.json` brings **skills + constraints** only.
- root brings **sources + overrides + policy**; single root-owned lock; nested locks ignored.
- if root has no source for an included skill → resolver **fails** (no silent source import).
- escape valve (step 2): `import_sources: true`, but only through the `source_policy` gate.
  Default off — safer. Cost: adopting a reusable workflow means the root must know/import its
  sources. Acceptable price for not trusting an include's source claims.

## R3.5 Next: promote to technical RFC

Model has converged. Technical RFC = `docs/skillfile-v2-design.md` with: node manifest v4,
graph manifest (Skillfile v2), resolution (MVS + lock), the three pillars above, and the
migration path (dependencies.json removal, schema-version bumps, backward compat for direct
skills). Remaining sub-RFCs: skill semver contract; security-advisory force-raise channel; MCP
provider install target (T3).
