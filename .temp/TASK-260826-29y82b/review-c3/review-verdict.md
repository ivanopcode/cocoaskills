# TASK-260826-29y82b review verdict (cycle 3, CR-TASK-260826-29y82b-3 rev 3)

**Verdict: changes requested -> `to-dev`.**

Both cycle-2 blockers are genuinely fixed and I re-derived every pin fact from a
fresh clone. One defect of the same class as cycle-2 blocking 1 survives: the
document still names marker v3 exclusively in a fourth place, so a schema-8
reader is told substitution state lives in a marker version their install does
not use.

## Cycle-2 blocking 1 is resolved and verified against the code

The three named lines now read:

```
$ grep -nE 'rc\.[0-9]|schema-[0-9]|marker v[0-9]|marker-v[0-9]' docs/external-build-repositories.md
3:CocoaSkills implements the Curator Protocol `1.0.0-rc.10` schema-8
17:An `agent-skill.json` schema-7 or schema-8 declaration binds a canonical network identity,
217:A scope is a segment prefix of the schema-7 or schema-8 canonical repository identity
274:receipt-v2 cache below `<csk-home>/external-builds`; schema-7 installations use
275:marker v3 and schema-8 installations use marker v4; both may contain local
374:receipt v2 and marker v3 and never aliases the declared source. Strict audit
```

Each claim checks out against production code:

- `src/csk/skillspec.py:344` gates the `go-repository-v1` build command on
  `schema >= 7` and `skillspec.py:371` gates `_validate_repository_commands`
  the same way. `skillspec.py:24` caps the supported set at 8, so "schema-7 or
  schema-8" is the complete admitted set at lines 17 and 217.
- The marker pairing at lines 274-275 is exact at the production call site.
  `src/csk/installer.py:3584-3597` selects `InstallMarkerV3` when
  `plan.spec.schema_version == 7` and `InstallMarkerV4` otherwise, under the
  comment "Marker v3 records a schema-7 installation and marker v4 a schema-8
  one." `install_marker.py:756` requires `skill_schema=7` for v3 and
  `install_marker.py:777` requires `skill_schema=8` for v4;
  `install_marker.py:891-892` confirms the same pairing in `marker_can_be_current`.
- "both may contain local receipt-v1 and external receipt-v2 commands together"
  holds for v4 because `InstallMarkerV3` and `InstallMarkerV4` share
  `_InstallMarkerExternalCapable` (`install_marker.py:696-702`) verbatim.

## Cycle-2 blocking 2 is resolved

`TASK-260826-29y82b_results.md` now describes the shipped header. It derives at
`b8b03d597ac83d158a0eadd9d0b25d2e883de1a3`, quotes a `head -n 15` block reading
`1.0.0-rc.10` / `schema-8`, and embeds the current diff including the three
reconciled schema lines. The stale rc.9 artifact is gone.

## Pin facts re-derived independently

Fresh clone into
`.temp/TASK-260826-29y82b/review-c3/curator-spec`, not the producer's clone:

```
$ git clone --quiet https://github.com/relux-works/curator-spec .temp/TASK-260826-29y82b/review-c3/curator-spec
$ git checkout --quiet b8b03d597ac83d158a0eadd9d0b25d2e883de1a3 && git log -1 --format='%H %d %s' && shasum -a 256 conformance/v1/manifest.json
b8b03d597ac83d158a0eadd9d0b25d2e883de1a3  (HEAD, tag: v1.0.0-rc.10, origin/main, origin/HEAD, main) Admit the pinned-agent SSH authentication tail (#22)
803918bf8672f76cf990985e51db213b826674cd5bb54fbf47731b8404b44403  conformance/v1/manifest.json
```

The header revision and digest match `src/csk/build_repository.py:32-33`
(`PROTOCOL_VERSION = "1.0.0-rc.10"`, `CONFORMANCE_MANIFEST_SHA256`).

The pinned-agent wording matches the spec at the pinned revision.
`profiles/manager.md:1384` opens "The authentication tail is exactly one of:"
and lists form 3 as "the pinned-agent form"; `profiles/manager.md:1396` reads
"The pinned-agent form is the RECOMMENDED selection when an agent is in use".
`src/csk/git_admission.py:505-519` emits exactly that argv, so the removed
beyond-the-spec implication is correctly absent.

No em-dash or en-dash survives in the file: `grep -c '—\|–'` returns `0`.

## Tests

```
$ CURATOR_CONFORMANCE_ROOT=.../review-c3/curator-spec/conformance/v1 \
  .venv/bin/python -m pytest tests/test_schema_v7_repository.py tests/test_build_metadata.py -q -p no:randomly
100 passed, 3 skipped, 28 warnings in 0.13s
```

`tests/test_protocol_conformance.py` and
`tests/test_rc5_external_repository_conformance.py` reached roughly 85 percent
with zero failures before my bounded 500-second window expired (`pytest-exit=124`,
a timeout kill, not a failure). I did not observe their completion and do not
claim it. The delta under review is documentation only and touches no code path
those suites exercise.

## Blocking: line 374 is the fourth marker-v3 mention and it is now wrong

The delta taught the document that schema-8 installs use marker v4. Line 374 was
not updated with the other three, so the document now tells a schema-8 reader
that substitution state is recorded in a marker version their install does not
write:

```
$ sed -n '371,375p' docs/external-build-repositories.md
A network substitution instead declares `git` plus one typed `revision` or
`tag`. Local selection admits a narrow ordinary `.git` layout and records a
host-path-free operator-local identity. Substitution state is explicit in
receipt v2 and marker v3 and never aliases the declared source. Strict audit
refuses substituted installs.
```

Marker v4 records substitution state identically. `install_marker.py:701`
declares `builds: Mapping[str, InstallMarkerBuildV3]` on the shared
`_InstallMarkerExternalCapable` body, and `install_marker.py:345-346` puts
`substituted` and `substitution` on `InstallMarkerBuildV3`, so both marker
versions carry the same fields. `installer.py:3582-3583` states it outright:
"The two shapes are identical; only the manifest band differs."

The consequence is not cosmetic. `Strict audit refuses substituted installs`,
so a schema-8 operator reading this paragraph concludes their marker does not
carry the record the audit gate reads. Fix: name both marker versions at line
374, the same way line 275 already does.

This is exactly the defect cycle-2 blocking 1 named; my cycle-2 grep pattern
matched only `rc\.[0-9]|schema-[0-9]` and missed the bare "marker v3" spelling,
so the producer fixed the three lines it was handed. The pattern above catches
all four.

## Non-blocking: lines 17 and 217 broke the file's wrap width

The producer inserted " or schema-8" without rewrapping, leaving two prose lines
well past the roughly 78-column wrap the rest of the file keeps:

```
$ awk 'length>82 {printf "%d(%d)\n", NR, length}' docs/external-build-repositories.md
17(90)
110(177)   <- table row, pre-existing
111(171)   <- table row, pre-existing
112(86)    <- table row, pre-existing
131(83)    <- code block, pre-existing
134(90)    <- code block, pre-existing
217(85)
253(100)   <- pre-existing
```

Lines 17 and 217 are the only new offenders. Rewrap them in the same pass as the
line-374 fix.

## Non-blocking: a task artifact leaked into the product repo root

`TASK-260826-29y82b_results.md` sits untracked at
`/Users/iv/Developer/Wildberries/cocoaskills/TASK-260826-29y82b_results.md`,
byte-identical to the board resource, and `git check-ignore` does not cover it.
Per the project structure rule, task scratch belongs under
`.temp/TASK-260826-29y82b/`. It must not reach the commit the orchestrator makes
for this scope.

## Non-blocking, third cycle unfixed: the empty CR is a workspace mapping bug

The candidate tree `746deb3f` equals base `2fbbd266` and the patch has zero
paths, because the story workspace at `.temp/STORY-260824-3rzqxr/worktree` is a
worktree of `cocoaskills-taskboard`, whose tree holds only board files and no
`docs/`. The real edits are uncommitted on branch `main` of
`/Users/iv/Developer/Wildberries/cocoaskills`.

Addressing the empty delta explicitly, as required: no repository change was
**not** the right outcome here. The producer did change repository files; the
Change Request simply cannot see them. Accepting rev 3 would record acceptance
of an empty tree, and the follow-on `commit_ack=scope_committed` would commit
nothing while marking the delta integrated. This was raised in cycle 1 and again
in cycle 2 and is still unfixed. The orchestrator has to repoint the story
workspace at the product repository before any cycle on this leaf can integrate.

## Non-blocking, carried: no test guards the doc header

Re-verified this cycle: `grep -rn "external-build-repositories" tests/ .github/`
returns nothing. Nothing asserts that the header revision and digest agree with
`build_repository.py:32-33`. The rc.5 staleness this task repairs survived for
exactly that reason, and the next pin move can drift the same way. A guard that
reads `PROTOCOL_VERSION` and `CONFORMANCE_MANIFEST_SHA256` out of the header
would close it.

## What the next producer must do

1. Update line 374 so it names marker v3 and marker v4, matching line 275.
2. Rewrap lines 17 and 217 to the file's wrap width.
3. Move `TASK-260826-29y82b_results.md` out of the product repo root into
   `.temp/TASK-260826-29y82b/`.
4. Verify each edit with `grep -nE 'rc\.[0-9]|schema-[0-9]|marker v[0-9]|marker-v[0-9]'`
   and `awk 'length>82'`, and paste the literal output into the refreshed
   outcome resource.

Everything else in the delta is accepted as correct.
