# Skillfile schema 2 sources

CocoaSkills implements draft Skillfile schema 2 sources against
curator-spec `8ba9c235ec5be00d52378479516c82386fd0c178`
(`protocol/skillfile-sources.md` revision 1,
`protocol/repository-transport.md` revisions 1 and 2). Support is
`draft skillfile-sources-v1 (opt-in)`: it is off unless explicitly
enabled, every user-facing surface labels it, and no release
qualification or conformance claim is made. Passing the draft schema
vectors is not evidence of a local installation or native containment;
the [draft vectors](https://github.com/relux-works/curator-spec/tree/8ba9c235ec5be00d52378479516c82386fd0c178/conformance/draft-sources-v1)
distinguish structural checks from filesystem, resolver and execution
requirements.

## Opt-in

Schema 2 is accepted only when the global config declares
`"experimental": {"skillfile_sources": true}` or the environment sets
`CSK_EXPERIMENTAL_SKILLFILE_SOURCES=1`. The config form looks like this:

```json
{
  "schema_version": 1,
  "skills_root": "~/skills",
  "experimental": {"skillfile_sources": true},
  "projects": {"demo": {"path": "~/work/demo"}}
}
```

Without the opt-in a schema-2 Skillfile fails with the existing
unsupported-schema error text plus exactly one hint line naming the
opt-in. Released schema 1 behaviour, error text included, stays
byte-identical.

## Sources and selectors

A schema-2 Skillfile keeps `schema_version`, `project`, `agents` and
`locale`, and adds an optional `sources` map from aliases to
acquisitions. Each alias declares exactly one of `path`, `git` or
`repository`. A `path` source names a literal native directory, absolute
or relative to the project. A `git` source names an endpoint URL plus
exactly one of `tag`, `branch` or `revision`. A `repository` source
names an already-canonical `host/path` identity plus one ref. Branch
refs are admitted only in the root project Skillfile; a transitive
Skillfile must not declare host paths or branches.

```json
{
  "schema_version": 2,
  "sources": {
    "local": {"path": "."},
    "vendor": {"path": "/srv/skills/vendor"},
    "team": {"git": "https://git.example.com/team/kit.git", "tag": "v1.4.0"},
    "kit": {"repository": "git.example.com/team/kit", "revision": "0123456789abcdef0123456789abcdef01234567"}
  },
  "skills": [
    {"name": "review", "from": "local", "directory": "agents/skills/review"},
    {"from": "team", "directory": "skills", "include": ["*"], "exclude": ["release"]}
  ]
}
```

The `git` grammar is the closed core 6.3 endpoint spelling: `https://`,
`ssh://` or scp form. A `https://` declaration carries no userinfo,
password or port; `ssh://` and scp spellings may carry a username but
no password or port. Anything else fails `source_selection_invalid`
before any filesystem or network work.

The `team` and `kit` declarations above are valid, and `csk check`
plans them against the policy. Network acquisition is not implemented
in this draft: `csk install` and `csk upgrade` refuse `git` and
`repository` sources with `source_selection_invalid` and install path
sources only. The refusal reason names that bound.

## Mixed sources

The `skills` list mixes three disjoint element forms: legacy
`{name, source, tag|branch|revision}` entries, individual selectors
`{name, from, directory}` and collection selectors
`{from, directory, include, exclude?}`. A selector that mixes legacy
fields with `from` is refused. Names share one namespace: a duplicate
name fails `source_name_conflict` whether the two entries are both
legacy, both selectors, or one of each.

## Collections

A collection selector enumerates skill directories below one source
directory. `include` lists literal member names, `*`, or both;
`exclude` removes names from the set. Every listed literal must exist
and be a directory; a missing literal fails `source_member_missing`
and a non-directory fails `source_member_invalid`. An empty expansion
is refused. Membership is frozen at install time: a lock records the
admitted members, and a later filesystem change is drift, not a silent
re-expansion.

## Machine transport policy

Network acquisition is governed by a machine-owned
`source-policy.json` beside the global config (`~/.cocoaskills/`
by default, `CSK_SOURCE_POLICY` overrides the path). The policy is
validated in full before any network I/O. An invalid or unreadable
policy fails `repository_policy_invalid` and is never treated as
absent; an absent policy simply leaves identity-only sources without
endpoints.

Schema 1 maps exact canonical repository identities to endpoint lists:

```json
{
  "schema_version": 1,
  "repositories": {
    "git.example.com/team/kit": {
      "endpoints": [
        {"url": "https://git.example.com/team/kit.git", "authentication": "team-https"}
      ],
      "fallback": "none"
    }
  }
}
```

Schema 2 adds three mechanisms. Explicit ports select non-default
connection ports (`https://git.example.com:8443/team/kit.git`).
Mirrors serve a repository from another host when the endpoint
declares `mirror_of` naming the entry key exactly; a resolved host
that differs from the key host without `mirror_of` fails
`repository_mirror_undeclared`. Aliases give one connection hostname
a reusable name: an endpoint may reference an `aliases` entry instead
of embedding the host, and an unknown reference fails
`repository_alias_unknown`. A schema-2 reader accepts schema 1 with
revision-1 endpoint semantics; a revision-1 reader rejects schema 2.

## Root inputs

`root_inputs` maps a source alias to the allowlist of portable
root-relative paths that the `"."` selector directory may admit for
that alias. Selecting the source root without a `root_inputs` entry
for the alias is refused, so a Skillfile cannot silently widen one
member selector into the whole source tree. Entries must be
non-empty, duplicate-free and pairwise non-overlapping.

## Lock semantics and refresh

The first `csk install` resolves every path selector, captures immutable
snapshots under the `source-v1` namespace in the csk home, and writes
`Skillfile.lock.json` beside the Skillfile (skillfile-lock schema 1).
Machine-private bindings live under the csk home, never in the lock.
A locked install consumes the locked snapshots and refuses drift
instead of re-resolving. `csk upgrade` is the explicit refresh: the
only operation that replaces locked refs, admitted bytes or
membership, and it replaces lock and markers atomically only after
every gate succeeds. Install and upgrade refuse `git` and `repository`
sources in this draft; network acquisition is not implemented.
`csk status` reports read-only currentness against the lock and
writes nothing. `csk check` validates a schema-2 Skillfile
structurally, plans every network source against the policy without
network I/O, and verifies lock consistency. A `valid` verdict on a
Skillfile with network sources states that the declarations and
plans are sound. It does not state that install accepts them. Launch
reads installed state only and never rescans live source inputs.

One interop rule is recorded, not decided here: the lock's Git
directory agreement for legacy configured entries is strict on write
and tolerant on read, per the note in
[UNRESOLVED_QUESTIONS.md](../UNRESOLVED_QUESTIONS.md).

## Diagnostics

Every failure surfaces as one stable class with the selector or
member, the reason, and exactly one `remediation:` line. Nine classes
come from skillfile-sources section 5 (`source_alias_unknown`,
`source_selection_invalid`, `source_member_missing`,
`source_member_invalid`, `source_name_conflict`,
`source_output_overlap`, `source_snapshot_changed`,
`source_snapshot_unavailable`, `source_lock_stale`) and four from
repository-transport (`repository_policy_invalid`,
`repository_endpoint_unavailable`, `repository_mirror_undeclared`,
`repository_alias_unknown`). Rendered text never carries secrets:
a refusal names the source alias, the field and the shape of the
violation but never echoes the declaration itself, and an endpoint
subject renders from its parsed scheme, host and path. Absolute
paths redact to `<path>` and any credential-shaped residue redacts
to `***` as a second line of defence, while selectors, member names
and relative paths stay readable. A typical refusal looks like this:

```text
source_snapshot_changed: Skill 'review' changed since the lock was written; run an explicit refresh
remediation: run csk upgrade to refresh the lock, or restore the locked bytes
draft skillfile-sources-v1 (opt-in)
```

## Bounds

Draft source selection is POSIX-only. It descends with
descriptor-relative opens and refuses with
`source_selection_invalid` on runtimes without that mechanism rather
than falling back to a path-based walk. Schema-2 installs publish
context only: a member that exports commands or declares skill
requirements is refused, and the global scope stays on schema 1.
Network acquisition is not implemented in this draft: `git` and
`repository` sources validate and plan, but install and upgrade
refuse them. These bounds, like the feature itself, carry no release
qualification and no conformance claim. The design record is
[RFC 0009](v0.16-design.md); the Russian command reference is
[docs/cli.md](cli.md).

## Conformance coverage

The executable harness `tests/test_draft_sources_conformance.py` drives
the draft corpus at curator-spec
`8ba9c235ec5be00d52378479516c82386fd0c178` (the revision pinned in
`.github/ci/draft-sources-suite.json`): 115 schema cases, 94 semantic
cases and 3 snapshot vectors, plus harness self-tests. Every semantic
case has a registered driver that runs against a fixture built from the
case input; the dispatch observes each run and fails a driver that
returns without calling any tabled production entry point. That check
is exactly "at least one tabled in-process entry was called": it sees
in-process calls only, it is defined by the table (real but untabled
production code counts as untouched), and it is provenance-blind (one
tabled call on any argument satisfies it). Table membership is itself
observed, not inferred: the gate runs the product's own install,
upgrade, status and check paths on hermetic fixtures under a call
tracer (`sys.settrace`, starting at the console-script entry point
resolved from `pyproject.toml`), and every tabled entry must have
executed. The recorder keys records by `id(code)` and retains each
code object in its record for the run; membership and label projection
also confirm object identity with `is`. A same-named nested function,
method, or byte-identical clone loaded from another file therefore
cannot stand in for the entry, even when Python considers the two code
objects equal by value. An uncalled function is never observed, so no
source shape — a test-only caller, a forwarder, a nested body or
method, a reference passed as data, called or not — can certify an
entry. Coverage from the
ordinary suite was measured and rejected as the signal: the dead
`check_selected_package` entry is executed by direct unit tests, so
suite coverage would certify the exact hole the gate exists to
close. The junit artifact records passed/skipped/failed/total per
category (schema, snapshot, semantic, harness) for every run.

The corpus was run on this macOS host with Python 3.12 on 2026-09-23.
The native run and the added simulated external-build-refusal run both
exited 0 (`305 passed, 1 skipped`); the single skip is the harness
setup-phase accounting probe. Their junit properties report:

| Local macOS run | Schema | Snapshot | Semantic | Harness |
| --- | ---: | ---: | ---: | ---: |
| Native product lane | 115/0/0/115 | 3/0/0/3 | 94/0/0/94 | 92/1/0/93 |
| `CI=true`, `CSK_SIMULATE_NO_EXTERNAL_BUILDS=1` | 115/0/0/115 | 3/0/0/3 | 94/0/0/94 | 92/1/0/93 |

Each cell is passed/skipped/failed/total. The second run adds the refused
external-build scenario beside the native run; it does not stand in for a
Linux host. On ubuntu-latest the expected semantic count is 93/1/0/94,
with `case-alias` skipped because that control needs a case-insensitive
filesystem and Ubuntu runners use a case-sensitive one. Ubuntu results
have not been observed in this local run. The `fast_draft_sources`
pull-request lanes and the `merge_draft_sources` main lanes run on
ubuntu-latest and macos-latest; their verification is the orchestrator
post-landing step. windows-latest is a declared unsupported lane: draft schema-2
source selection descends with descriptor-relative opens
(`O_DIRECTORY` plus `dir_fd` for `os.open`/`os.stat`/`os.readlink`),
which Windows runtimes do not provide, so traversal drivers (and the
observed-membership gate, which needs the same mechanism) skip
there by platform bound rather than running. Windows is not a draft
conformance CI lane for this task.

On ubuntu-latest the product itself refuses the external-build
scenario (`go-repository-v1` runs on macOS and Windows only; Linux
qualification is deferred), so the observed-membership gate
certifies the refused lane instead of failing it: the scenario
asserts the structured refusal (exit 1, `source_member_invalid`,
the product's own refusal text), and the entries covered only by
that scenario are excluded by recorded coverage in
`tests/draft_sources_observed_labels.json` — a machine-written
record of the scenarios' own execution (today three entries:
`build_repository_pipeline.run_pipeline`,
`builds.currentness.compare_external_build_evidence`,
`sources.transport.acquire_plan`), re-approved against the live
trace on every capable run, never a hand-maintained list. Every
other tabled entry must still have executed there, and the
laundering-shape controls still reject there. The refused lane is
simulated locally by adding that lane's run beside the native one
(`CSK_SIMULATE_NO_EXTERNAL_BUILDS=1` forces the same support
predicate the product calls, scoped to the added run): the
capable-lane pin still runs on a capable host — the pin obligation
is derived from the real platform, so simulation can add evidence
but never remove it — and the junit categories read identically to
the native lane (115 schema, 3 snapshot, 94 semantic, 92/1/0/93
harness; the single harness skip is the setup-phase accounting
probe). Verification of the
`fast_draft_sources` pull-request and main lanes on
ubuntu-latest and macos-latest is the orchestrator post-landing
step. The semantic corpus has zero skips on the measured macOS host
and zero `not yet implemented` skips on either local run. The
`case-alias` skip is declared for case-sensitive platforms; the
setup-phase accounting probe is a harness-only skip used to verify
that junit counts include setup-phase skips.

`root-no-inputs` is driven through the live selection entry point
(`selection.resolve_individual`) and answers as the corpus expects: a
root selection without `root_inputs` refuses with
`source_output_overlap`, because `policy.root_inputs` is enforced on
the live path (BUG-260922-1o40hs, with the manifest-spelling closure in
BUG-260922-1383no). Earlier revisions of this corpus recorded that case
as a divergence while the enforcement was missing; it is not one any
longer. One table entry left the allowlist in this round:
`install_marker.validate_attestation_evidence` is never executed by
production (its one production caller returns before reaching it,
and its other caller has no production callers), so the corpus no
longer vouches for it; its drivers still touch observed marker
entries.

This statement reports what the lanes observe. It makes no release
qualification and no conformance claim: this is draft support for a
draft specification, and passing the draft corpus does not qualify any
release.
