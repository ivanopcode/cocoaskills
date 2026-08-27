# TASK-260826-29y82b review verdict: changes requested

Reviewer run against `CR-TASK-260826-29y82b-1` revision 1. Verdict:
**changes requested**, routed to `to-dev`.

Two of the three header facts check out. The claim the header makes about
the boundary this document covers does not, and the new pinned-agent
wording cites a source that does not exist at the pinned revision.

## Where the reviewed work actually is

The Change Request delta is empty: base `2fbbd266` and candidate tree
`746deb3f` are identical, and the patch resource has zero paths. That is a
workspace mismatch, not an idle producer. The story worktree at
`.temp/STORY-260824-3rzqxr/worktree` is a worktree of
`ivanopcode/cocoaskills-taskboard`, which contains no `docs/` directory.
The task's own `TASK-260826-29y82b_tooling-note.md` pins the work directory
to `/Users/iv/Developer/Wildberries/cocoaskills`, a different repository.
The producer edited the file there, uncommitted:

```
$ git -C /Users/iv/Developer/Wildberries/cocoaskills status --short
 M LOGBOOK.md
 M docs/external-build-repositories.md
?? TASK-260826-29y82b_results.md
```

This review reads that working-tree diff as the delivered work. The empty
CR is therefore not the reason for the verdict; the content is. The
orchestrator still has to resolve which repository this story's changes are
supposed to land in, because as it stands no reviewable revision exists on
`task-board/story/STORY-260824-3rzqxr`.

## Verified correct

**Revision hex.** `.github/workflows/ci.yml:53` sets
`RELEASED_SUITE_PIN: 0ed5c691e9208eea52f21db2fc05e226ce3516fd`, matching the
header.

**Manifest SHA-256.** Derived independently in a fresh clone, not taken
from the producer's outcome:

```
$ git clone --quiet https://github.com/relux-works/curator-spec \
    .temp/TASK-260826-29y82b/review-spec
$ cd .temp/TASK-260826-29y82b/review-spec && git checkout --quiet 0ed5c691
$ git rev-parse HEAD
0ed5c691e9208eea52f21db2fc05e226ce3516fd
$ shasum -a 256 conformance/v1/manifest.json
803918bf8672f76cf990985e51db213b826674cd5bb54fbf47731b8404b44403  conformance/v1/manifest.json
```

The value in the header matches byte for byte.

**Schema 8 exists in main.** `src/csk/skillspec.py:24` declares
`SUPPORTED_SCHEMA_VERSIONS = {1, 2, 3, 4, 5, 6, 7, 8}`, `ARCHITECTURE.md:256`
describes a schema-8 installation recorded by install marker v4, and the
`CHANGELOG.md` 0.15.0 section documents the schema 8 authoring contract.

## Finding 1 (blocking): the header contradicts the code it documents

`docs/external-build-repositories.md` documents the `go-repository-v1`
external-repository boundary. The code that owns that boundary is
`src/csk/build_repository.py`, and it still pins rc.5:

```
src/csk/build_repository.py:18:PROTOCOL_VERSION = "1.0.0-rc.5"
src/csk/build_repository.py:19:CONFORMANCE_MANIFEST_SHA256 = "b6f56aacc0e37dcc6692f73f641bff761e89b645adfe20a47a06d81c6fda204c"
```

Those are exactly the two values the producer removed from the header. A
test enforces them and it is green right now:

```
$ .venv/bin/python -m pytest tests/test_schema_v7_repository.py -q
40 passed, 3 skipped, 28 warnings in 7.17s
$ .venv/bin/python -m pytest tests/test_schema_v7_repository.py::test_rc5_contract_pin -q
1 passed, 28 warnings in 0.04s
```

The codebase keeps two distinct pins on purpose, and says so. The candidate
and released suite advanced to rc.9 with manifest `803918bf...`
(`tests/test_protocol_conformance.py:97-100`), while the
external-repository consumer stayed on the rc.5 root. That module skips
itself on the rc.5 root with the reason spelled out at
`tests/test_protocol_conformance.py:91`: `"rc.5 root is handled by the
external-repository consumer"`.

The header now reads "CocoaSkills implements the Curator Protocol
`1.0.0-rc.9` schema-8 `go-repository-v1` boundary" and "The accepted
protocol revision is `0ed5c691...`". Both statements are false for the
boundary this file documents, and the second one contradicts a passing
assertion in the suite. The task description required verification
("verify against the code and CHANGELOG 0.15.0"); the CHANGELOG half was
done, the code half was not.

Two clean resolutions exist and the choice is a scope decision, not a
wording fix:

1. Advance `PROTOCOL_VERSION` and `CONFORMANCE_MANIFEST_SHA256` in
   `src/csk/build_repository.py` together with
   `tests/test_schema_v7_repository.py`, then keep the header as written.
   This falls outside the declared docs-only scope and needs its own task.
2. Keep the docs-only scope and write a header that states both facts
   without contradicting either: the implemented `go-repository-v1`
   boundary and its accepted revision, and separately the released
   conformance suite the CI qualifies against. Note that under this option
   the `schema-8` claim also needs rechecking, because schema 8 adds
   `modules` on the local `go-v1` command and `script-worker-v1`, not
   `go-repository-v1` surface.

## Finding 2 (blocking): `curator-spec#22` is not a source at this pin

The new bullet asserts a normative source:

```
docs/external-build-repositories.md:232-234
- both: pinned-agent form. The third canonical authentication-tail form,
  RECOMMENDED, per curator-spec#22. The agent holds the private key and the
  named `.pub` pins which single key is offered.
```

At the pinned revision `0ed5c691` the spec defines exactly two forms and
marks neither RECOMMENDED. `profiles/manager.md:1384-1387`:

> The authentication tail is exactly either
> `-o IdentitiesOnly=yes -o IdentityAgent=none -i <operator-identity>` or
> `-o IdentitiesOnly=no -o IdentityFile=none -o
> IdentityAgent=<operator-agent-socket>`.

Searches over the checked-out spec at that commit return nothing for
`#22`, nothing for `RECOMMENDED` near agent or identity wording, and
`IdentitiesOnly` appears only in `profiles/manager.md`. The merged history
reaches `#27`, `#28`, `#29` with no `#22`. The TZ that raised this delta
made it conditional for exactly this reason: `.research/260822_tz-docs-0.14.md:194`
says "После посадки curator-spec#22", and `:336` repeats the precondition.
The precondition has not been met.

The removed caveat is still true. `src/csk/git_admission.py:505-519` emits a
third tail the pinned spec does not admit:

```
IdentitiesOnly=yes  IdentityAgent=<socket>  -i <identity>
```

So the edit replaced an accurate statement that csk goes beyond the spec
with a citation of a source that does not exist. Restore wording that
describes the pinned-agent form as a csk extension, or land this delta only
after `curator-spec#22` is actually in the pinned revision.

## Finding 3 (minor): the em-dash cleanup is partial and unscoped

The two untouched bullets at lines 226 and 230 had their em-dashes changed
to colons, which the acceptance criterion "no other content changed" does
not cover. The style pass is also incomplete: three em-dashes remain.

```
$ grep -n '—' docs/external-build-repositories.md
247:a menu of **detected candidates** — the live agent socket (with its loaded key
248:count) and the `.pub` files below `~/.ssh` — so the usual answer is a single
254:reports which source — flags, environment, or a config scope — covered each
```

Either finish the em-dash removal across the file as its own tracked
change, or leave the two bullets alone. Also note the new sentence "The
third canonical authentication-tail form, RECOMMENDED, per
curator-spec#22." is a fragment; the prose guide asks for complete
sentences carrying one claim each.

## What the next producer must do

Resolve Finding 1 by picking one of the two options above and saying which
in the outcome. Resolve Finding 2 by restoring the extension wording, or by
showing the third form and its RECOMMENDED status in the spec at the pinned
revision with a file-and-line citation. Resolve Finding 3 either way.
Re-derive the manifest SHA in the outcome as before; that part was correct
and reproduced cleanly here. Finally, land the edit where the story branch
can see it, or get the orchestrator to correct the workspace mapping first.
