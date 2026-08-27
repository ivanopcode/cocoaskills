# private-https-build-repository-credentials — rework notes (cycle 2)

Branch `feat/build-https-broker`, rework commit `a8f82f7` on top of the reviewed
`ebcb464`; base `origin/main` `8233485`; worktree
`/Users/iv/Developer/Wildberries/cocoaskills/.temp/build-https/worktree`.

Addresses every item of `TASK-260824-1dop3s_review-verdict.md`
(changes_requested -> to-dev), plus both minor notes.

## Finding 1 — run-wide token disclosure (both requested mechanisms delivered)

- New `CSK_BUILD_HTTPS_HOST`: when set, only repositories on that host receive
  the run-wide token; every other repository resolves as if the override were
  absent (scope match, then prompt, then anonymous). The pin is normalized
  (trim + lowercase) to match the lowercased canonical identity host.
- The captured override is now `installer.OperatorHTTPSToken`, a frozen
  dataclass with the token excluded from `repr` (the old tuple would have
  shown the secret if it ever reached a diagnostic). `covers(host)` holds the
  pin logic in one place.
- Docs (`external-build-repositories.md`, `cli.md`, `reference.md`, CLI
  epilog) carry the plain-language warning: the unpinned override trusts every
  HTTPS build repository host in the closure.
- Tests: `test_resolver_host_pin_limits_the_run_wide_token`,
  `test_capture_reads_the_optional_host_pin`.

## Finding 2 — run-only persist bug (fixed on both surfaces)

- Both resolvers now save `config.build_* + tuple(persisted)` instead of the
  in-run rules accumulator, which legitimately also holds "this run only"
  answers to avoid re-prompting. One-line fix each, HTTPS and SSH.
- Tests: `test_a_run_only_prompt_choice_never_reaches_the_config` in both
  `tests/test_build_https.py` and `tests/test_build_ssh.py` (two prompts in
  one run, one run-only + one persist; the saved config carries only the
  persisted scope).

## Finding 3 — resolver-layer tests (16 new, mirroring the SSH block)

`tests/test_build_https.py` now exercises:

- run-wide precedence over a matching scope (the scope names an unset env var,
  so consulting it would raise; it is never consulted);
- host pin (above), anonymous fallback with the dry-run message, transport
  skip for SSH repositories;
- longest-scope selection through `token_env` with username and message
  assertions;
- `_https_rule_credentials` fail-closed remedies: unset `token_env`, absent
  keyring token (names the `login` command), absent host credential (names
  the clone-once remedy);
- prompt: default candidate persists, "this run only" returns persist=False,
  abort;
- `_materialize_https_broker`: foreign host raises
  `CREDENTIAL_POLICY_INVALID`; the state file is exactly
  `{"host", "username"}` and the wrapper carries no secret.

## Finding 4 — docs and CHANGELOG

- The HTTPS section of `docs/external-build-repositories.md` is rewritten in
  English (the file's language on origin/main) with zero em/en dashes in the
  added lines, per `docs/prose-style.md`.
- Beyond the verdict: `docs/cli.md` gained the full
  `csk config build-https add/login/list/remove` section and `docs/reference.md`
  the `build_https` config section, both mirroring their build-ssh precedents
  (those files are Russian; the sections follow suit, dash-free).
- `CHANGELOG.md` has the feature entry under `[Unreleased]` / `Добавлено`.

## Minor notes (both fixed)

- `csk config build-https remove` claims "and its stored token" only after a
  read-back proved an entry existed; `git credential reject` exits 0 either
  way and is no longer treated as evidence.
- `_prompt_build_https_rule` now receives the pinned operator Git and uses it
  for `discover_host_material` and `store_namespaced_token`, aligning the
  prompt with `_https_rule_credentials`.

## Evidence (real exit codes, standalone commands)

- Targeted: `uv run pytest tests/test_build_https.py tests/test_build_ssh.py -q`
  -> 72 passed, exit 0.
- Full suite: `uv run --extra dev pytest -n auto` -> 1576 passed, 244 skipped,
  exit 0 (`TASK-260824-1dop3s_full-suite-rework.log`; reviewer's baseline 1560
  + 16 new tests).
- Types/lint: `uv run --extra dev mypy` (strict) -> Success, 74 files, exit 0.
- CLI smoke on a temp `CSK_CONFIG`: add (both sources), literal-token
  rejection by argparse (exit 2), list, remove, remove-absent (exit 2); no
  secret in the written config; the remove message correctly omitted "stored
  token" with no keyring entry present.

## E2E status

The verdict routes: "re-verify only if the resolver changes touch the fetch
path." This rework does not touch `git_admission.py` or the fetch mechanics;
without `CSK_BUILD_HTTPS_HOST` set, `covers()` is identically true and the
selected credentials are bit-identical to the reviewed revision. The
cycle-1 E2E attestation (private repository, 560 files, `b94468fe0`) stands.
