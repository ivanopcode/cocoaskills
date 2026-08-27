# TASK-260827-d319oi — accept-protocol-rc10 (finalization + verification)

Advance the accepted Curator Protocol revision in csk from `1.0.0-rc.5` to
`1.0.0-rc.10`. Acceptance-pin move, not a behavior change: csk already emits the
pinned-agent authentication tail and already implements schema 8.

Working tree only — **no commits made**. `docs/` untouched (`git status --short docs/` → 0 lines).

## 1. The central corpus fact, verified independently

rc.10 (curator-spec#22, `b8b03d597ac83d158a0eadd9d0b25d2e883de1a3`) changed
`profiles/manager.md` **only**. It republishes the rc.9 conformance corpus byte
for byte. So *the accepted revision* and *the corpus identity* are two different
facts and must be pinned separately:

```
$ git -C ~/Developer/ReluxWorks/curator-spec rev-parse 'v1.0.0-rc.10^{commit}'
b8b03d597ac83d158a0eadd9d0b25d2e883de1a3

$ git show b8b03d5:conformance/v1/manifest.json | shasum -a 256
803918bf8672f76cf990985e51db213b826674cd5bb54fbf47731b8404b44403  -

$ git show b8b03d5:conformance/v1/manifest.json | python3 -c "..."
protocol_version: 1.0.0-rc.9          <-- corpus still declares rc.9

$ git ls-tree -r --name-only b8b03d5 | grep release/
release/1.0.0-rc.5.json ... release/1.0.0-rc.9.json   <-- no release/1.0.0-rc.10.json

$ git diff --stat v1.0.0-rc.9 b8b03d5
 profiles/manager.md | 26 ++++++++++++++++++++------
```

This is why the code declares three constants, not two. A pin move that advanced
one without the other now goes red instead of silently collapsing them.

## 2. Per-pin-site changes

| Site | Change |
|---|---|
| `src/csk/build_repository.py:32-34` | `PROTOCOL_VERSION = "1.0.0-rc.10"`; `CONFORMANCE_MANIFEST_SHA256 = 803918bf…`; **new** `CONFORMANCE_CORPUS_PROTOCOL_VERSION = "1.0.0-rc.9"` |
| `src/csk/build_repository.py:116` | Hardcoded "released rc.5 grammar" error text → f-string on `PROTOCOL_VERSION`; stops drifting on the next pin move |
| `tests/test_schema_v7_repository.py` | `RC5_MANIFEST_SHA256` → `ACCEPTED_MANIFEST_SHA256` + `ACCEPTED_CORPUS_PROTOCOL_VERSION`; `test_accepted_contract_pin` asserts both pins **and** that they differ; corpus test now also asserts the manifest's declared `protocol_version`; case counts 95→103 and 96→144 (+ new `rejected == 132`) |
| `tests/test_protocol_conformance.py` | Removed the module-level rc.5 skip; claim-version map extended to v4/v5 |
| `tests/test_rc5_external_repository_conformance.py` | `PROTOCOL = "1.0.0-rc.5"` **deliberately unchanged** + comment; separately versioned corpus, own digest, own env var — advancing it would assert something its own pinned bytes contradict |
| `tests/test_build_metadata.py:31` | Comment only; digest was already 803918bf |

Remaining `rc.5` strings in tests are correct history, not stale pins: the v3
claim schema is genuinely pinned at rc.5 in the corpus
(`conformance-claim-v3.schema.json` `const == "1.0.0-rc.5"`), and the
external-repository corpus genuinely declares rc.5.

## 3. No fail-closed check weakened — two were tightened

- `test_protocol_conformance.py`: the module-level skip that routed a b6f56aac
  (rc.5) root **past this entire conformance consumer** is deleted. Nothing
  consumes an rc.5 conformance root any more, so a root this module cannot
  authenticate is now a *wrong root* that fails in `_root()` rather than
  silently skipping the module.
- `test_schema_v7_repository.py`: authenticating bytes proves *which* corpus was
  read; the added `protocol_version` assertion proves it is the one the accepted
  revision republishes rather than a later corpus handed over.

## 4. Corpus counts derived from the corpus, not from the old numbers

Recomputed directly against the extracted rc.10 tree — 144 / 132 match exactly:

```
1..6 case_count: 144 rejected: 132
```

## 5. Evidence — commands, real exit codes

### 5a. Required subset, no corpus roots set (exit 0)
```
$ .venv/bin/pytest tests/test_schema_v7_repository.py tests/test_protocol_conformance.py \
    tests/test_rc5_external_repository_conformance.py tests/test_build_metadata.py -q
EXIT=0
40 passed, 168 skipped, 28 warnings in 0.40s
```

**168 of 208 skipped.** Without `CURATOR_*` roots the corpus consumers do not
run at all, so this run — and the orchestrator's full-suite run, which had no
`CURATOR_*` in its environment — does **not** exercise a single corpus
assertion. That is why the rooted run below was necessary.

### 5b. Same corpus tests WITH the rc.10 root — the run that proves the AC (exit 0)
```
$ CURATOR_SCHEMA_V7_ROOT=<rc.10 conformance/v1> .venv/bin/pytest tests/test_schema_v7_repository.py -q
EXIT=0
43 passed, 28 warnings in 5.34s
```
43 passed, **0 skipped** — `case_count == 103`, `== 144`, `rejected == 132`,
the manifest SHA and the declared `protocol_version == 1.0.0-rc.9` all executed
against the real rc.10 corpus.

### 5c. Negative evidence — the new bounds actually bind
Narrowed the gates rather than deleting them; both go red:
```
Mutant A (case_count 103 -> 102):
E       assert 103 == 102
1 failed, 42 deselected

Mutant B (ACCEPTED_CORPUS_PROTOCOL_VERSION rc.9 -> rc.10):
E       AssertionError: assert '1.0.0-rc.9' == '1.0.0-rc.10'
2 failed, 41 deselected
```
Mutant B is the exact conflation this pin move guards against: asserting the
corpus declares rc.10 fails, because it declares rc.9. File restored byte-identical
afterwards (`cmp` → `RESTORE OK`).

### 5d. Lint / type gate (exit 0)
The project's lint gate is mypy strict (`python -m mypy`); there is no ruff in
this project — an earlier `ruff` invocation in this run found no such binary.
```
$ .venv/bin/python -m mypy
MYPY_EXIT=0
Success: no issues found in 75 source files
```

### 5e. Full suite — accepted from the orchestrator, not rerun by me
Per the finalization note, the orchestrator ran the full suite on this exact tree:
```
1797 passed, 245 skipped, 28 warnings in 444.91s (0:07:24)
```
I did **not** rerun it. Caveat stated plainly: that run had no `CURATOR_*` roots
set, so its 245 skips include the corpus consumers; 5b/5c above are what covers
them.

### What I did not run
- A rooted run of `tests/test_protocol_conformance.py`: with
  `CURATOR_CONFORMANCE_ROOT` set it exceeded the 9m20s shell bound and was
  killed (exit 143). Its pins are verified statically (§1) and its unrooted
  collection passes, but its rooted assertions were **not** executed by me.
- `tests/test_rc5_external_repository_conformance.py` rooted: the
  external-repository corpus is separately versioned and not part of the rc.10
  spec tree, so `CURATOR_EXTERNAL_REPOSITORY_CORPUS_ROOT` could not be
  materialized here. That file's pin is intentionally unchanged.

## 6. Pin verification in-file (tooling note requirement)
```
$ grep -n "PROTOCOL_VERSION\|CONFORMANCE_MANIFEST_SHA256\|CONFORMANCE_CORPUS" src/csk/build_repository.py
32:PROTOCOL_VERSION = "1.0.0-rc.10"
33:CONFORMANCE_MANIFEST_SHA256 = "803918bf8672f76cf990985e51db213b826674cd5bb54fbf47731b8404b44403"
34:CONFORMANCE_CORPUS_PROTOCOL_VERSION = "1.0.0-rc.9"
116:            f"repository git source is not in the released {PROTOCOL_VERSION} grammar"
```

## 7. Files changed (working tree, uncommitted)
```
 .research/TASK-260803-2ol7ok_protocol-isolation-classification.json | 11 ++-
 LOGBOOK.md                                                         | 83 +++++++++++
 src/csk/build_repository.py                                        | 23 ++-
 tests/test_build_metadata.py                                       | 12 +-
 tests/test_protocol_conformance.py                                 | 35 +++--
 tests/test_rc5_external_repository_conformance.py                  |  5 +
 tests/test_schema_v7_repository.py                                 | 59 +++++--
```
