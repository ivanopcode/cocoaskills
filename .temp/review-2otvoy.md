# TASK-260819-2otvoy review: architecture-rationale

Verdict: changes requested (route to `to-dev`).
Reviewer run: RUN-260819-1551f9. Repo: `/Users/iv/Developer/Wildberries/cocoaskills` (uncommitted working tree).

## What holds

All seven rationale paragraphs exist adjacent to their mechanism: whitelist
stripped layout (`ARCHITECTURE.md:37`), canonical `.agents/skills/` root
(`:39`), content-hashed installs (`:102`), audit gate (`:104`), fail-closed
installs (`:106`), protected build cache (`:256`), manager-owned execution
(`:294`). The `## Security model` section exists (`:361`) and maps all four
listed threats. `ARCHITECTURE.ru.md` and `SECURITY.ru.md` are deleted, and a
repo-wide grep finds no remaining reference to either file or to the old
`#security-boundaries` anchor outside `.spec/` and `LOGBOOK.md` prose. Both
cross-links resolve: `SECURITY.md:3` and `SECURITY.md:157` point at
`ARCHITECTURE.md#security-model`, which matches the new heading.
Sentence-level comparison of the two documents finds zero shared sentences, so
the no-duplicated-text criterion holds. `tests/test_release_contract.py`
passes: 23 passed.

## Blocking findings

### 1. Security model carries its argument in a bullet list

`ARCHITECTURE.md:364-367`. The threat-to-mechanism mapping is four multi-sentence
bullets, each stating a threat, an attack, a mechanism, and a consequence. The
attached prose style instruction lists "Bullet lists that carry reasoning
instead of parallel facts" in the blacklist a reviewer rejects on sight, and
says reasoning lives in prose. Four threats, four paragraphs. The
`LOGBOOK.md:5` claim of "zero blacklist pattern violations" is wrong on this
point.

The lead-in `CocoaSkills maps four primary threat vectors to specific defensive
mechanisms:` exists only to announce the list and goes away with it.

### 2. Content-hashed installs are described incorrectly

`ARCHITECTURE.md:102` and `:365` both say content-hashed installs "verify
directory digests against expected commit hashes" / "verify directory digests
against explicit commit hashes". No such comparison exists. `content_sha256`
(`hashing.content_sha256`, called at `src/csk/installer.py:2449`, `:2761`,
`:2815`) hashes the installed tree and is recorded in the marker;
`ARCHITECTURE.md:237-239` already states it hashes selected installed content
and excludes the marker. Commit pinning is a separate mechanism: stage 3
resolves exact refs and fixes one commit per skill (`ARCHITECTURE.md:66-68`),
and the content-addressed snapshot cache is keyed `cache/<source>/<commit>/`
(`:178`). Two independent mechanisms are collapsed into one invented check.
Describe them separately.

### 3. The adapter paragraph contradicts the document

`ARCHITECTURE.md:39` opens with "To support multiple AI coding tools without
duplicating files". `ARCHITECTURE.md:45-47` states an adapter may mirror
context by copy instead of symlink, and `:160` says adapters maintain per-agent
directories with managed-entry tracking. Files are duplicated in copy mode.
The defensible claim is a single source of truth for skill content, not the
absence of copies.

### 4. The protected-cache rationale misplaces the check

`ARCHITECTURE.md:256` says the protected build cache "verifies file ownership,
POSIX permissions, and Windows DACLs before binary execution". The check gates
cache trust and adoption, not execution: the paragraph directly above (`:249-254`)
requires the boundary verification before a reader trusts receipt and artifact
bytes, and `:288` states the manager never runs the artifact during validation,
installation, status, repair, rollback, or GC. Say "before it adopts cached
bytes".

### 5. Seven paragraphs share one visible template

Every added paragraph runs claim, threat, restated threat, then a mechanism
sentence closing on ", which <benefit>": "which limits prompt overhead",
"which guarantees reproducible skill state", "which halts unsafe installations
deterministically", "which preserves system safety", "which blocks untrusted
executable modification", "which prevents package descriptors from injecting
custom build scripts". The trailing benefit clause restates the opening claim
in each case, which the style guide rejects as a closing restatement, and the
middle two sentences say the same thing twice ("Repository assets pollute
context space when read by an agent" then a repeat of the same point). Vary the
shape and cut the redundant sentence per paragraph.

### 6. Prose is unwrapped in an 80-column document

New prose lines run 137 to 465 characters (`ARCHITECTURE.md:3, 37, 39, 102,
104, 106, 256, 294, 362, 364-367`; `SECURITY.md:3`). Before this change the
document had exactly one non-table line over 90 characters. The tables at
`:135-170` and `:405-413` are long by nature; prose is not. Wrap the added
prose to match the file.

## Non-blocking

`ARCHITECTURE.md:3` and `ARCHITECTURE.md:362` carry the same sentence verbatim:
"For vulnerability reporting procedures and platform hardening checklists, see
[SECURITY.md](SECURITY.md)." One of the two should be reworded.

The rename from `## Security boundaries` to `## Security model` left the
pre-existing boundaries list without a label, joined by the filler transition
"System integrity relies on enforcing these boundaries across all operations:"
(`:369`). Give the enumerated boundaries their own subheading, for example
`### Enforced boundaries`, under the new section.

## Fix list for the next producer

1. Rewrite `ARCHITECTURE.md:361-367` as prose, one paragraph per threat.
2. Correct the content-hash claim at `:102` and `:365`; separate commit pinning
   from installed-tree hashing.
3. Fix the "without duplicating files" claim at `:39`.
4. Change "before binary execution" to cache adoption at `:256`.
5. Break the shared template across the seven paragraphs and drop the repeated
   middle sentence and the trailing ", which <benefit>" clause.
6. Wrap all added prose to the document's existing width.
7. Deduplicate the cross-link sentence and label the enforced-boundaries list.
8. Correct the `LOGBOOK.md:5` claim of zero blacklist violations once the list
   is gone.

## Verification commands used

    git diff HEAD -- ARCHITECTURE.md SECURITY.md
    grep -rn "ARCHITECTURE.ru.md\|SECURITY.ru.md\|security-boundaries" --include="*.md" --include="*.html" --include="*.toml" .
    awk 'length>100 {print NR": "length}' ARCHITECTURE.md
    .venv/bin/python -m pytest tests/test_release_contract.py -q    # 23 passed
