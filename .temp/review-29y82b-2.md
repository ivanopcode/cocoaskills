# TASK-260826-29y82b review verdict (cycle 2, CR-TASK-260826-29y82b-2 rev 2)

**Verdict: changes requested -> `to-dev`.**

The pin facts are correct and I re-derived every one of them independently.
Two blocking defects remain: the document now contradicts itself on the schema
level it just advertised, and the outcome resource the AC names describes a
header that is not in the file.

## What I verified as correct

The revision hex and the manifest digest are right, and the previous cycle's
two blocking findings are genuinely resolved by the rc.10 move.

Literal derivation from a fresh clone of `https://github.com/relux-works/curator-spec`
into `.temp/TASK-260826-29y82b/curator-spec`:

```
$ git checkout --quiet b8b03d597ac83d158a0eadd9d0b25d2e883de1a3 && git log -1 --format='%H %d %s' && shasum -a 256 conformance/v1/manifest.json
b8b03d597ac83d158a0eadd9d0b25d2e883de1a3  (HEAD, tag: v1.0.0-rc.10, origin/main, origin/HEAD, main) Admit the pinned-agent SSH authentication tail (#22)
803918bf8672f76cf990985e51db213b826674cd5bb54fbf47731b8404b44403  conformance/v1/manifest.json

$ git checkout --quiet 0ed5c691e9208eea52f21db2fc05e226ce3516fd && git log -1 --format='%H %d %s' && shasum -a 256 conformance/v1/manifest.json
0ed5c691e9208eea52f21db2fc05e226ce3516fd  (HEAD, tag: v1.0.0-rc.9) Land schema 8 with the implementation pins that consume it (#29)
803918bf8672f76cf990985e51db213b826674cd5bb54fbf47731b8404b44403  conformance/v1/manifest.json
```

The digest is identical at rc.9 and rc.10, which confirms the orchestrator's
inline correction: #22 changed `profiles/manager.md` only.

- Header revision `b8b03d597ac83d158a0eadd9d0b25d2e883de1a3` matches
  `src/csk/build_repository.py:32` `PROTOCOL_VERSION = "1.0.0-rc.10"` and
  `build_repository.py:33` `CONFORMANCE_MANIFEST_SHA256`.
- `curator-spec#22` exists at the pinned revision. It is the pinned commit
  itself: "Admit the pinned-agent SSH authentication tail (#22)". Cycle 1's
  blocking finding 2 is resolved.
- The spec text backs the wording exactly. `profiles/manager.md:1384` lists the
  authentication tail as "exactly one of" three forms; form 3 is the
  pinned-agent form, and `profiles/manager.md:1396` reads "The pinned-agent form
  is the RECOMMENDED selection when an agent is in use". The doc's claim of a
  third canonical form, RECOMMENDED, per curator-spec#22 is accurate.
- `src/csk/git_admission.py:505-519` emits `-o IdentitiesOnly=yes -o
  IdentityAgent=<socket> -i <identity>`, byte-for-byte spec form 3. The removed
  beyond-the-spec implication is now correctly absent.
- Schema level 8 is right: `src/csk/skillspec.py:24`
  `SUPPORTED_SCHEMA_VERSIONS = {1,...,8}` and CHANGELOG 0.15.0 lands the
  `agent-skill.json` schema 8 authoring contract.
- No em-dash or en-dash survives anywhere in the file (`grep -c` returns 0).
  Cycle 1's minor prose finding is resolved.
- Scope is confined to `docs/external-build-repositories.md` and the one added
  `docs/skill-authoring.md:3` link bump. No unrelated file was touched.
- Tests green with the corpus supplied:

```
$ CURATOR_CONFORMANCE_ROOT=.../curator-spec/conformance/v1 .venv/bin/python -m pytest tests/test_schema_v7_repository.py tests/test_build_metadata.py -q
100 passed, 3 skipped in 0.16s
```

  The wider run over `tests/test_protocol_conformance.py` and
  `tests/test_rc5_external_repository_conformance.py` also exited 0.

## Blocking 1: the header says schema-8, the body still says schema-7

Bumping line 3 from `schema-7` to `schema-8` without touching the body left the
document contradicting itself about the schema level it advertises.

```
$ grep -nE "rc\.[0-9]|schema-[0-9]" docs/external-build-repositories.md
3:CocoaSkills implements the Curator Protocol `1.0.0-rc.10` schema-8
17:An `agent-skill.json` schema-7 declaration binds a canonical network identity,
217:A scope is a segment prefix of the schema-7 canonical repository identity
274:receipt-v2 cache below `<csk-home>/external-builds`; schema-7 installations use
```

Line 3 tells the reader the `go-repository-v1` boundary is schema-8. Line 17
then defines the declaration as schema-7 and line 217 calls the canonical
repository identity schema-7, so a schema-8 author is told the feature is not
theirs. The code says otherwise: `src/csk/skillspec.py:344` gates the
`go-repository-v1` build command on `schema >= 7` and `skillspec.py:371` gates
`_validate_repository_commands` the same way, so schema 8 declares it too.

Line 274 is the sharper one. It states that schema-7 installations use marker
v3, and says nothing about schema-8, which the header now advertises as the
implemented level. `src/csk/install_marker.py:772-777` defines
`InstallMarkerV4` with `skill_schema=8`, against `install_marker.py:756`
`skill_schema=7` for v3. A reader who follows the new header lands on a marker
statement that does not cover their installation.

Fix inside the file already in scope: make lines 17, 217, and 274 agree with the
`schema >= 7` gate and name the schema-8 to marker-v4 pairing.

## Blocking 2: the outcome resource contradicts the shipped file

`TASK-260826-29y82b_results.md` is the stale rev1 artifact. It states the header
reads `1.0.0-rc.9` and revision `0ed5c691e9208eea52f21db2fc05e226ce3516fd`,
quotes a `head -n 15` block showing rc.9, and embeds the rev1 diff. The file on
disk reads `1.0.0-rc.10` and `b8b03d597ac83d158a0eadd9d0b25d2e883de1a3` after
the orchestrator's inline correction.

The AC names this artifact specifically: the freshly derived manifest SHA-256
with literal derivation output in the outcome. The derivation that is there was
run at a revision the document no longer cites. Refresh the resource to the
shipped facts, with the derivation performed at `b8b03d59`, and replace the
embedded diff with the current one.

## Non-blocking: the empty CR is an unfixed workspace mapping, not a no-op

The candidate tree equals its base and the patch has zero paths because the
story worktree at `.temp/STORY-260824-3rzqxr/worktree` is a worktree of
`cocoaskills-taskboard`, which contains no `docs/`. The real edits are
uncommitted on branch `main` of `/Users/iv/Developer/Wildberries/cocoaskills`.
Cycle 1 flagged this and it is still unfixed. Accepting this revision would
record acceptance of an empty tree, and the follow-on `commit_ack=scope_committed`
would commit nothing. The orchestrator needs to repoint the story workspace at
the product repository before any cycle here can integrate.

## Non-blocking: the pin drift this task fixes has no test guard

Nothing asserts the doc header agrees with `build_repository.py`. `grep -rn
"external-build-repositories" tests/` returns no hit, and
`tests/test_schema_v7_repository.py:56` pins only the code constants. The rc.5
staleness this task repairs went unnoticed for exactly that reason, and the next
pin move can drift the same way. A cheap guard reading `PROTOCOL_VERSION` and
`CONFORMANCE_MANIFEST_SHA256` out of the header would close it.

## Non-blocking: accepted revision and corpus identity read as one fact

The header attributes `conformance/v1/manifest.json` to the rc.10 revision,
which is literally true. `build_repository.py:26-34` records that the corpus at
that revision still declares `protocol_version` `1.0.0-rc.9` and that the spec
publishes no `release/1.0.0-rc.10.json`. The code separates the two facts
deliberately and asserts them separately; one sentence in the header would stop
a reader inferring an rc.10 corpus.
