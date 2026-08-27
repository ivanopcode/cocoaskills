# TASK-260826-29y82b Execution Results: spec22-pin-delta

## 1. Curator Spec Manifest SHA-256 Derivation

### Derivation Command
```bash
mkdir -p .temp/TASK-260826-29y82b && git clone https://github.com/relux-works/curator-spec .temp/TASK-260826-29y82b/curator-spec && cd .temp/TASK-260826-29y82b/curator-spec && git checkout b8b03d597ac83d158a0eadd9d0b25d2e883de1a3 && git log -1 --format='%H %d %s' && shasum -a 256 conformance/v1/manifest.json
```

### Literal Output
```
b8b03d597ac83d158a0eadd9d0b25d2e883de1a3  (HEAD, tag: v1.0.0-rc.10, origin/main, origin/HEAD, main) Admit the pinned-agent SSH authentication tail (#22)
803918bf8672f76cf990985e51db213b826674cd5bb54fbf47731b8404b44403  conformance/v1/manifest.json
```

## 2. Summary of Documentation Changes

Updated `docs/external-build-repositories.md`:
- Advanced Curator Protocol version in header from `1.0.0-rc.5` to `1.0.0-rc.10`.
- Advanced protocol schema level from `schema-7` to `schema-8` matching `CHANGELOG.md` 0.15.0 and main implementation.
- Updated accepted protocol revision commit hex to `b8b03d597ac83d158a0eadd9d0b25d2e883de1a3`.
- Updated `conformance/v1/manifest.json` SHA-256 digest to `803918bf8672f76cf990985e51db213b826674cd5bb54fbf47731b8404b44403`.
- Replaced em-dashes in credential selection list and SSH section prose with colons and parentheses per prose style guide.
- Revised line 232 credential forms wording for the pinned-agent form (`both`) to cite `curator-spec#22` as its source: the third canonical authentication-tail form, RECOMMENDED, per `curator-spec#22`, removing any implication that `csk` goes beyond the specification.
- Reconciled schema-7 mentions throughout the body (lines 17, 217, 274) with `schema >= 7` implementation gate and schema-8 / marker-v4 pairing.
  - Line 17: Updated to "schema-7 or schema-8 declaration".
  - Line 217: Updated to "schema-7 or schema-8 canonical repository identity".
  - Line 274: Updated to specify schema-7 installations use marker v3 and schema-8 installations use marker v4.

Updated `docs/skill-authoring.md`:
- Bumped Curator spec core link from `v1.0.0-rc.5` to `v1.0.0-rc.10`.

## 3. Tooling Verification — Literal File Content Checks

### `head -n 15 docs/external-build-repositories.md`
```markdown
# External build repositories

CocoaSkills implements the Curator Protocol `1.0.0-rc.10` schema-8
`go-repository-v1` boundary. It builds an executable from a separately locked
Git repository while keeping the skill package unable to select credentials,
Git configuration, hooks, compiler flags, output paths, wrappers, or signing.

The accepted protocol revision is
`b8b03d597ac83d158a0eadd9d0b25d2e883de1a3`; its `conformance/v1/manifest.json`
SHA-256 is
`803918bf8672f76cf990985e51db213b826674cd5bb54fbf47731b8404b44403`.
The external-repository corpus is supplied to tests independently, so the csk
consumer imports no Curator implementation package or internal fixture value.

## Skill declaration
```

### Credential Forms Wording (`docs/external-build-repositories.md:L225-L235`)
```markdown
A scope needs at least one of `agent` or `identity`; each alone is a complete
selection:

- `{"agent": "auto"}`: agent-only. The install adopts the operator's live
  `SSH_AUTH_SOCK` at run time and the agent signs with its loaded keys in
  turn. No key file is named, so a populated agent can exhaust the server's
  `MaxAuthTries` budget before reaching the right key.
- `{"identity": "~/.ssh/key"}`: identity-file only, for an unencrypted key
  on disk (`IdentityAgent=none`).
- both: pinned-agent form. The third canonical authentication-tail form,
  RECOMMENDED, per curator-spec#22. The agent holds the private key and the
  named `.pub` pins which single key is offered.
```

### Reconciled Schema Lines Wording (`docs/external-build-repositories.md:L17,L217,L274-L275`)
```markdown
17: An `agent-skill.json` schema-7 or schema-8 declaration binds a canonical network identity,
217: A scope is a segment prefix of the schema-7 or schema-8 canonical repository identity
274-275: receipt-v2 cache below `<csk-home>/external-builds`; schema-7 installations use
marker v3 and schema-8 installations use marker v4; both may contain local
```

## 4. Literal Git Diff Output

```diff
diff --git a/docs/external-build-repositories.md b/docs/external-build-repositories.md
index aa0db55..f1ce777 100644
--- a/docs/external-build-repositories.md
+++ b/docs/external-build-repositories.md
@@ -1,20 +1,20 @@
 # External build repositories
 
-CocoaSkills implements the Curator Protocol `1.0.0-rc.5` schema-7
+CocoaSkills implements the Curator Protocol `1.0.0-rc.10` schema-8
 `go-repository-v1` boundary. It builds an executable from a separately locked
 Git repository while keeping the skill package unable to select credentials,
 Git configuration, hooks, compiler flags, output paths, wrappers, or signing.
 
 The accepted protocol revision is
-`f5d7673039226ab81de2f4f87e2155ae995c4df3`; its `conformance/v1/manifest.json`
+`b8b03d597ac83d158a0eadd9d0b25d2e883de1a3`; its `conformance/v1/manifest.json`
 SHA-256 is
-`b6f56aacc0e37dcc6692f73f641bff761e89b645adfe20a47a06d81c6fda204c`.
+`803918bf8672f76cf990985e51db213b826674cd5bb54fbf47731b8404b44403`.
 The external-repository corpus is supplied to tests independently, so the csk
 consumer imports no Curator implementation package or internal fixture value.
 
 ## Skill declaration
 
-An `agent-skill.json` schema-7 declaration binds a canonical network identity,
+An `agent-skill.json` schema-7 or schema-8 declaration binds a canonical network identity,
 an exact Git object ID, and optionally an exact tag:
 
 ```json
@@ -214,7 +214,7 @@ selection in the global config, keyed by a canonical-identity prefix:
 }
 ```
 
-A scope is a segment prefix of the schema-7 canonical repository identity
+A scope is a segment prefix of the schema-7 or schema-8 canonical repository identity
 (`host/path`): matching happens only on whole `/` boundaries, and the longest
 matching scope wins, so a key granted to one namespace never reaches a
 repository outside it. Flags win over `CSK_BUILD_SSH_*`, and both win over
@@ -223,14 +223,15 @@ every configured scope.
 A scope needs at least one of `agent` or `identity`; each alone is a complete
 selection:
 
-- `{"agent": "auto"}` — agent-only. The install adopts the operator's live
+- `{"agent": "auto"}`: agent-only. The install adopts the operator's live
   `SSH_AUTH_SOCK` at run time and the agent signs with its loaded keys in
   turn. No key file is named, so a populated agent can exhaust the server's
   `MaxAuthTries` budget before reaching the right key.
-- `{"identity": "~/.ssh/key"}` — identity-file only, for an unencrypted key
+- `{"identity": "~/.ssh/key"}`: identity-file only, for an unencrypted key
   on disk (`IdentityAgent=none`).
-- both — the recommended form for passphrase-protected keys: the agent holds
-  the private key and the named `.pub` pins which single key is offered.
+- both: pinned-agent form. The third canonical authentication-tail form,
+  RECOMMENDED, per curator-spec#22. The agent holds the private key and the
+  named `.pub` pins which single key is offered.
 
 Manage the map with:
 
@@ -243,14 +244,14 @@ csk config build-ssh remove gitlab.example.com/portals/infra
 
 Before any fetch, the install resolves credentials for every declared SSH
 build repository. On an operator terminal an unmatched repository prompts with
-a menu of **detected candidates** — the live agent socket (with its loaded key
-count) and the `.pub` files below `~/.ssh` — so the usual answer is a single
+a menu of **detected candidates** (the live agent socket with its loaded key
+count and the `.pub` files below `~/.ssh`), so the usual answer is a single
 Enter on the default "agent + pinned key" entry. Discovery only lists what
 exists; nothing is ever used without the operator's explicit selection, and
 nothing persists without the explicit scope choice. A non-interactive run
 fails closed with `build_repository_ssh_credential_missing` and ready-to-run
 `csk config build-ssh add` commands built from the same detected candidates. `csk install --dry-run`
-reports which source — flags, environment, or a config scope — covered each
+reports which source (flags, environment, or a config scope) covered each
 repository.
 
 CocoaSkills writes a private wrapper carrying one pinned `ssh` argv and points
@@ -271,8 +272,8 @@ The fixed Go contract is the same `manager-worker-v1` session documented in the
 main README: native toolchain, vendored modules, no network, no workspace, no
 cgo, internal linking, and manager-derived output. External builds use a
 receipt-v2 cache below `<csk-home>/external-builds`; schema-7 installations use
-marker v3 and may contain local receipt-v1 and external receipt-v2 commands
-together.
+marker v3 and schema-8 installations use marker v4; both may contain local
+receipt-v1 and external receipt-v2 commands together.
 
 Project install publishes `.agents/bin/<command>`; global install publishes
 `<csk-home>/global/bin/<command>`. Both managed launchers point directly at the
diff --git a/docs/skill-authoring.md b/docs/skill-authoring.md
index 9b41af8..b5dbfbd 100644
--- a/docs/skill-authoring.md
+++ b/docs/skill-authoring.md
@@ -1,6 +1,6 @@
 # Руководство по созданию скиллов CocoaSkills
 
-Это руководство определяет рекомендуемый контракт для репозиториев скиллов CocoaSkills. Документ служит практическим руководством для авторов к [RFC 0003](v0.5-design.md) и текущему [принятому ядру протокола Curator rc.5](https://github.com/relux-works/curator-spec/blob/v1.0.0-rc.5/protocol/core.md).
+Это руководство определяет рекомендуемый контракт для репозиториев скиллов CocoaSkills. Документ служит практическим руководством для авторов к [RFC 0003](v0.5-design.md) и текущему [принятому ядру протокола Curator rc.10](https://github.com/relux-works/curator-spec/blob/v1.0.0-rc.10/protocol/core.md).
 
 ## 1. Структура репозитория
 
```
