# Releasing CocoaSkills

`.scripts/release.sh` owns the stable-release sequence for this repository. It
creates a CHANGELOG cut pull request, verifies the landed trunk head before
tagging, publishes the signed tag, optionally mirrors it to Wildberries, and checks the
published distribution channels.

## Supported platforms

The release script is a zsh and POSIX-shell maintainer tool for macOS and
Linux. Windows is not a release host. On Windows, the test suite skips only the
tests that execute `.scripts/release.sh`; pure-Python `release_support.py`
helpers and other platform-independent tests still run.

## Prerequisites

- Start from a clean local `main` checkout whose commit equals `origin/main`.
- Install and authenticate `gh` for `ivanopcode/cocoaskills` and `glab` for the
  Wildberries GitLab project when publishing the mirror stage. `--no-wb` does
  not require `glab` or mirror authentication. The script suppresses provider command output
  that could contain credential details; it never prints token environment
  variables.
- Make `delivery` available on `PATH`, or set `DELIVERY_BIN` to its executable
  path. The script refuses before creating a release branch if
  `delivery trunk-verify` is unavailable.
- Configure Git SSH signing with `gpg.format=ssh`, `user.signingkey`, and a
  readable `gpg.ssh.allowedSignersFile`. The matching public key must be loaded
  into `ssh-agent`; after a restart, load it with `ssh-add <configured-key>`.
- Install `bash`, `git`, `python3`, `curl`, `mise`, `ssh-add`, and `ssh-keygen`.
- Prepare a UTF-8 Russian release note file when publishing the mirror release.
  It is not needed with `--no-wb`.

## Command

```bash
.scripts/release.sh 0.18.4 --wb-notes .temp/wb-release-0.18.4.md
```

To publish to GitHub, PyPI, Homebrew and mise now and defer the mirror:

```bash
.scripts/release.sh 0.20.0 --no-wb
```

`--no-wb` skips only the mirror push and GitLab release. The CHANGELOG pull
request, required checks, exact-head review, fast-forward, trunk verification,
signed tag, `release.yml`, `distribution-smoke.yml` and distribution version
checks still run. It never contacts the mirror or invokes `glab`.
Combining `--no-wb` with `--wb-notes` or `--resume-from` is a usage error
(exit 2) before any external command runs.

For a non-publishing plan, run:

```bash
.scripts/release.sh 0.18.4 --wb-notes .temp/wb-release-0.18.4.md --dry-run
```

The dry run resolves and prints the version, tag, branch, cut date, repository,
mirror, notes path, and every release step before it probes
`delivery trunk-verify` against the current HEAD. No pull request, Git ref,
release, or mirror command runs in this mode. A live release performs the gate
later, against the exact CHANGELOG cut head after the fast-forward to `main`.
With `--no-wb --dry-run`, the plan explicitly marks the mirror stage as
skipped and prints the exact later command with `FILE` as the notes placeholder.

## Live release sequence

1. The script checks that the checkout is the clean CocoaSkills `main` branch,
   refreshes `origin/main`, checks the release tag and branch are unused, and
   verifies that the configured SSH signing key is loaded.
2. It creates `release/vVERSION`, moves the current `[Unreleased]` section
   under `[VERSION] - YYYY-MM-DD`, starts a fresh `[Unreleased]` section,
   signs the commit, and opens a pull request to `main`.
3. It waits for required checks, confirms the pull-request head did not change,
   records an exact-head review comment, and pushes that reviewed head to
   `refs/heads/main` as a fast-forward.
4. It confirms `origin/main` equals the reviewed head, then runs
   `delivery trunk-verify --head <head>`. The script requires exit code `0`
   and one valid `delivery.trunk-verify/v1` JSON document whose `head` exactly
   matches the release SHA, `verdict` is `green`, and `exit_code` is `0`. Red
   returns exit code `1`; unknown, unavailable, malformed, mismatched, or
   non-green evidence returns exit code `2`. A refusal happens before tag
   creation.
5. It creates the signed `vVERSION` tag with `git tag -s`, verifies it, pushes
   it to GitHub, and watches the `release.yml` run selected by workflow file
   and the tag's exact commit SHA.
6. Unless `--no-wb` was selected, after `release.yml` passes it pushes `main` and the tag to
   `git@gitlab.wildberries.ru:portals/agentic-infra/cocoaskills.git` and runs
   `glab release create` from the CocoaSkills checkout with the supplied
   Russian notes file. It does not switch into another GitLab project.
7. It watches `distribution-smoke.yml` by workflow file, exact tag commit SHA,
   and a `createdAt` after the matching release run's `startedAt`. It does not
   use `headBranch` to identify the smoke run. If smoke fails, it prints every
   failed job name and link, says the release itself has already been published
   and only verification failed, then prints `gh run rerun <run-id> --failed`.
   It never reruns a smoke workflow automatically. If failed-job details cannot
   be read or validated, it still prints the manual command and exits with an
   unknown result.
8. After the operator manually reruns the failed jobs and the smoke run passes,
   run `.scripts/release.sh VERSION --resume-from verify`. This mode confirms
   the signed local tag matches the published tag, watches the exact release
   and smoke runs, then performs only the final distribution version checks.
   It does not create or publish Git refs or releases.
9. The final checks read the PyPI JSON version, the Homebrew formula source and its latest
   tap commit, and `MISE_FETCH_REMOTE_VERSIONS_CACHE=0 mise ls-remote
   pipx:cocoaskills`.

Homebrew reads use the repository in the API endpoint, for example
`gh api repos/ivanopcode/homebrew-csk/commits?path=Formula/cocoaskills.rb&per_page=1`;
`gh api` does not accept the `--repo` option.

## Publish only the mirror later

When the owner authorizes the deferred mirror stage, run:

```bash
.scripts/release.sh 0.20.0 --resume-from wb --wb-notes .temp/wb-release-0.20.0.md
```

This mode requires only `git`, `python3`, authenticated `glab`, the notes file
and the configured trust needed to verify the tag signature. It can run from
another branch; it publishes the tagged commit, not the checkout's HEAD.
It reads the tag on origin, fetches it and `origin/main` without replacing a
different local tag, verifies the signed tag with `git verify-tag`, and
requires its commit to be an ancestor of (or equal to) `origin/main`.

Before publishing, it reads and fetches the mirror's `main`, checks that it
is an ancestor of (or equal to) the tagged commit, and reads all pages of
GitLab releases to establish that this tag has no existing release. A missing,
unsigned or unverified tag, unreachable tag commit, divergent mirror main,
existing release, or unknown provider response stops before any push or
release creation. These checks may fetch local Git objects and refs.

It then pushes only `<tagged-commit>:refs/heads/main` and
`refs/tags/vVERSION:refs/tags/vVERSION` to the mirror with
`--no-follow-tags`, without force, and creates the mirror release using the
supplied notes. It runs no CHANGELOG, GitHub, signing, workflow or distribution
stage. `--resume-from verify` retains its existing post-publish checks and
does not publish the deferred mirror stage.

The release script does not store credentials or copy provider output into
files. GitHub and GitLab authentication stays in their respective CLIs. A
failure after the CHANGELOG pull request has been fast-forwarded leaves the
landed cut untagged; inspect the reported head and trunk pipeline before
continuing the release.
