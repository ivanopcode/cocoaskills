#!/usr/bin/env bash
set -euo pipefail

readonly GITHUB_REPOSITORY="ivanopcode/cocoaskills"
readonly WB_REMOTE="git@gitlab.wildberries.ru:portals/agentic-infra/cocoaskills.git"
readonly WB_PROJECT="gitlab.wildberries.ru/portals/agentic-infra/cocoaskills"
readonly HOMEBREW_TAP_REPOSITORY="ivanopcode/homebrew-csk"
readonly RUN_DISCOVERY_ATTEMPTS=30
readonly RUN_DISCOVERY_DELAY=10

release_version=""
wb_notes=""
dry_run=false
resume_from=""

usage() {
  cat <<'USAGE'
Usage: .scripts/release.sh VERSION --wb-notes FILE [--dry-run]
       .scripts/release.sh VERSION --resume-from verify

VERSION is a stable MAJOR.MINOR.PATCH release. The notes file must contain the
Russian release notes for the Wildberries GitLab release. The verify resume
continues only the post-publish checks after a manual distribution-smoke rerun.
USAGE
}

refuse() {
  local code="$1"
  shift
  printf 'release refused: %s\n' "$*" >&2
  exit "$code"
}

# Provider and delivery diagnostics can include response details. Keep those
# out of terminal output while preserving stdout and the command's real status.
quiet_external() {
  "$@" 2>/dev/null
}

if (($# < 1)); then
  usage >&2
  exit 2
fi

release_version="$1"
shift
while (($# > 0)); do
  case "$1" in
    --dry-run)
      dry_run=true
      ;;
    --resume-from)
      if (($# < 2)); then
        refuse 2 "--resume-from requires a stage"
      fi
      resume_from="$2"
      shift
      ;;
    --resume-from=*)
      resume_from="${1#*=}"
      ;;
    --wb-notes)
      if (($# < 2)); then
        refuse 2 "--wb-notes requires a file path"
      fi
      wb_notes="$2"
      shift
      ;;
    --wb-notes=*)
      wb_notes="${1#*=}"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      refuse 2 "unknown argument: $1"
      ;;
  esac
  shift
done

if [[ ! "$release_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  refuse 2 "VERSION must be stable MAJOR.MINOR.PATCH"
fi
if [[ -n "$resume_from" && "$resume_from" != "verify" ]]; then
  refuse 2 "the only supported resume stage is verify"
fi
if [[ "$dry_run" == true && -n "$resume_from" ]]; then
  refuse 2 "--dry-run cannot be combined with --resume-from"
fi
if [[ -z "$resume_from" && -z "$wb_notes" ]]; then
  refuse 2 "--wb-notes FILE is required for a live release"
fi
if [[ -n "$wb_notes" ]]; then
  caller_directory="$(pwd)"
  if [[ "$wb_notes" != /* ]]; then
    wb_notes="$caller_directory/$wb_notes"
  fi
  if [[ ! -f "$wb_notes" ]]; then
    refuse 2 "Wildberries notes file does not exist"
  fi
fi

if script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"; then
  :
else
  refuse 2 "cannot locate the release script directory"
fi
if repo_root="$(git -C "$script_directory/.." rev-parse --show-toplevel)"; then
  :
else
  refuse 2 "cannot locate the CocoaSkills Git worktree"
fi
cd "$repo_root"

support_script="$repo_root/.scripts/release_support.py"
if [[ ! -f "$support_script" ]]; then
  refuse 2 "release support helper is missing"
fi

release_tag="v$release_version"
release_branch="release/$release_tag"
release_date="$(date +%Y-%m-%d)"

require_command() {
  local executable="$1"
  if ! command -v "$executable" >/dev/null; then
    refuse 2 "required command is unavailable: $executable"
  fi
}

resolve_delivery() {
  local requested resolved
  requested="${DELIVERY_BIN:-delivery}"
  if [[ "$requested" == */* ]]; then
    if [[ "$requested" == /* ]]; then
      resolved="$requested"
    else
      resolved="$caller_directory/$requested"
    fi
  else
    if resolved="$(command -v "$requested")"; then
      :
    else
      resolved=""
    fi
  fi
  if [[ -z "$resolved" || ! -x "$resolved" ]]; then
    refuse 2 "delivery trunk-verify is unavailable; install delivery or set DELIVERY_BIN"
  fi
  printf '%s' "$resolved"
}

verify_trunk() {
  local delivery_command="$1"
  local verify_head="$2"
  local verify_output verify_status
  if [[ ! "$verify_head" =~ ^[0-9a-f]{40}$ ]]; then
    refuse 2 "release head is not an exact 40-character SHA"
  fi
  if verify_output="$(quiet_external "$delivery_command" trunk-verify --head "$verify_head")"; then
    verify_status=0
  else
    verify_status=$?
  fi
  case "$verify_status" in
    0)
      if ! python3 "$support_script" trunk-evidence "$verify_head" <<<"$verify_output" >/dev/null; then
        refuse 2 "delivery trunk-verify evidence is malformed, mismatched, or not green for $verify_head; no tag was created"
      fi
      printf 'trunk-verify accepted %s\n' "$verify_head"
      ;;
    1)
      refuse 1 "delivery trunk-verify returned red for $verify_head; no tag was created"
      ;;
    2)
      refuse 2 "delivery trunk-verify returned unknown for $verify_head; no tag was created"
      ;;
    *)
      refuse 2 "delivery trunk-verify returned unsupported exit $verify_status for $verify_head; no tag was created"
      ;;
  esac
}

print_pre_gate_plan() {
  printf 'Release version: %s\n' "$release_version"
  printf 'Release tag: %s\n' "$release_tag"
  printf 'Release branch: %s\n' "$release_branch"
  printf 'CHANGELOG date: %s\n' "$release_date"
  printf 'GitHub repository: %s\n' "$GITHUB_REPOSITORY"
  printf 'Wildberries mirror: %s\n' "$WB_REMOTE"
  printf 'Wildberries GitLab project: %s\n' "$WB_PROJECT"
  printf 'Homebrew tap repository: %s\n' "$HOMEBREW_TAP_REPOSITORY"
  printf 'PyPI project: cocoaskills\n'
  printf 'Wildberries notes file: %s\n' "$wb_notes"
  printf '%s\n' \
    '1. Refresh main, cut CHANGELOG.md with the resolved date, and sign the release branch commit.'
  printf '2. Live: run git push --no-follow-tags --set-upstream origin "refs/heads/%s:refs/heads/%s", open a pull request to main, and wait for required checks on its exact head.\n' \
    "$release_branch" "$release_branch"
  printf '%s\n' \
    '3. Leave an exact-head review comment, then run git push --no-follow-tags origin "<reviewed-cut-head>:refs/heads/main" to fast-forward main.' \
    'The PR number and cut commit ID are captured after the signed branch is created.'
}

print_post_gate_plan() {
  printf '4. Live: run git tag -s %s -m "CocoaSkills %s" <reviewed-cut-head>, verify it, then run git push --no-follow-tags origin "refs/tags/%s:refs/tags/%s". Dry-run probe head: %s.\n' \
    "$release_tag" "$release_version" "$release_tag" "$release_tag" "$planning_head"
  printf '5. Live: watch release.yml for workflow file release.yml and head SHA <reviewed-cut-head>.\n'
  printf '6. Live: run git push --no-follow-tags "%s" "<reviewed-cut-head>:refs/heads/main" "refs/tags/%s:refs/tags/%s".\n' \
    "$WB_REMOTE" "$release_tag" "$release_tag"
  printf '7. Create the Wildberries GitLab release: glab release create %s --repo %s --name "CocoaSkills %s" --notes-file "%s".\n' \
    "$release_tag" "$WB_PROJECT" "$release_version" "$wb_notes"
  printf '%s\n' \
    '8. Watch distribution-smoke.yml by workflow file, exact head SHA, and createdAt after the release run started.' \
    'If it fails, inspect every failed job link, run gh run rerun <run-id> --failed manually, then use --resume-from verify.'
  printf '%s\n' \
    '9. Verify PyPI JSON, the Homebrew tap formula and latest commit, and MISE_FETCH_REMOTE_VERSIONS_CACHE=0 mise ls-remote pipx:cocoaskills.'
}

check_origin_repository() {
  local origin_url
  if origin_url="$(git remote get-url origin)"; then
    :
  else
    refuse 2 "cannot read the origin remote"
  fi
  case "$origin_url" in
    https://github.com/ivanopcode/cocoaskills|https://github.com/ivanopcode/cocoaskills.git|\
      git@github.com:ivanopcode/cocoaskills|git@github.com:ivanopcode/cocoaskills.git|\
      ssh://git@github.com/ivanopcode/cocoaskills|ssh://git@github.com/ivanopcode/cocoaskills.git)
      ;;
    *)
      refuse 2 "origin must identify the ivanopcode/cocoaskills repository"
      ;;
  esac
}

preflight_ssh_signing() {
  local signing_format signing_key signers_file signing_status public_key_file
  local expected_fingerprint loaded_keys loaded_status
  if signing_format="$(git config --get gpg.format)"; then
    :
  else
    signing_status=$?
    if ((signing_status == 1)); then
      signing_format="openpgp"
    else
      refuse 2 "cannot read Git signing format"
    fi
  fi
  if [[ "$signing_format" != "ssh" ]]; then
    refuse 2 "Git must use SSH signing (gpg.format=ssh) for releases"
  fi

  if signing_key="$(git config --path --get user.signingkey)"; then
    :
  else
    signing_status=$?
    if ((signing_status == 1)); then
      refuse 2 "Git user.signingkey is not configured"
    fi
    refuse 2 "cannot read Git user.signingkey"
  fi
  if signers_file="$(git config --path --get gpg.ssh.allowedSignersFile)"; then
    :
  else
    signing_status=$?
    if ((signing_status == 1)); then
      refuse 2 "Git SSH allowedSignersFile is not configured"
    fi
    refuse 2 "cannot read Git SSH allowedSignersFile"
  fi
  if [[ ! -r "$signers_file" ]]; then
    refuse 2 "Git SSH allowedSignersFile is not readable"
  fi

  require_command ssh-add
  require_command ssh-keygen
  if loaded_keys="$(ssh-add -l)"; then
    :
  else
    loaded_status=$?
    if ((loaded_status == 1)); then
      refuse 2 "SSH signing key is not loaded; run ssh-add <configured signing key> and retry"
    fi
    refuse 2 "cannot inspect ssh-agent; load the configured signing key with ssh-add and retry"
  fi

  case "$signing_key" in
    SHA256:*)
      expected_fingerprint="$signing_key"
      ;;
    ssh-*|ecdsa-*)
      if expected_fingerprint="$(printf '%s\n' "$signing_key" | ssh-keygen -lf /dev/stdin | awk 'NR == 1 {print $2}')"; then
        :
      else
        refuse 2 "cannot inspect the configured SSH signing key"
      fi
      ;;
    *)
      if [[ "$signing_key" == *.pub ]]; then
        public_key_file="$signing_key"
      else
        public_key_file="${signing_key}.pub"
      fi
      if [[ ! -r "$public_key_file" ]]; then
        refuse 2 "cannot inspect the configured SSH signing public key"
      fi
      if expected_fingerprint="$(ssh-keygen -lf "$public_key_file" | awk 'NR == 1 {print $2}')"; then
        :
      else
        refuse 2 "cannot inspect the configured SSH signing public key"
      fi
      ;;
  esac
  if [[ -z "$expected_fingerprint" ]] ||
    ! printf '%s\n' "$loaded_keys" | awk -v fingerprint="$expected_fingerprint" '$2 == fingerprint {found = 1} END {exit !found}'; then
    refuse 2 "configured SSH signing key is not loaded; run ssh-add <configured signing key> and retry"
  fi
}

wait_for_run() {
  local workflow="$1"
  local wanted_event="$2"
  local wanted_head="$3"
  local after_created_at="$4"
  local attempt run_list selected select_status run_id run_started_at
  local -a match_args
  attempt=0
  while ((attempt < RUN_DISCOVERY_ATTEMPTS)); do
    if run_list="$(quiet_external gh run list --repo "$GITHUB_REPOSITORY" \
      --workflow "$workflow" --limit 50 \
      --json databaseId,event,headBranch,headSha,createdAt,startedAt,url)"; then
      :
    else
      return 2
    fi

    match_args=(match-run "$wanted_event" "$wanted_head")
    if [[ -n "$after_created_at" ]]; then
      match_args+=(--after-created-at "$after_created_at")
    fi
    if selected="$(python3 "$support_script" "${match_args[@]}" <<<"$run_list")"; then
      IFS=$'\t' read -r run_id run_started_at <<<"$selected"
      if [[ -n "$run_id" ]]; then
        printf '%s\t%s\n' "$run_id" "$run_started_at"
        return 0
      fi
      return 2
    else
      select_status=$?
    fi
    if ((select_status != 1)); then
      return 2
    fi
    attempt=$((attempt + 1))
    if ((attempt < RUN_DISCOVERY_ATTEMPTS)); then
      if sleep "$RUN_DISCOVERY_DELAY"; then
        :
      else
        return 2
      fi
    fi
  done
  return 1
}

show_run_jobs() {
  local run_id="$1"
  local jobs_json job_rows
  if jobs_json="$(quiet_external gh run view "$run_id" --repo "$GITHUB_REPOSITORY" --json jobs)"; then
    :
  else
    printf 'Failed-job details for run %s could not be read.\n' "$run_id" >&2
    return 2
  fi
  if job_rows="$(python3 "$support_script" failed-jobs "$run_id" <<<"$jobs_json")"; then
    :
  else
    printf 'Failed-job details for run %s were malformed or empty.\n' "$run_id" >&2
    return 2
  fi
  while IFS=$'\t' read -r job_name job_url; do
    [[ -n "$job_name" ]] || continue
    printf '  - %s: %s\n' "$job_name" "$job_url" >&2
  done <<<"$job_rows"
}

report_smoke_failure() {
  local run_id="$1"
  local report_status=0
  printf 'The release itself has already been published; only distribution verification failed.\n' >&2
  printf 'Failed jobs for distribution-smoke run %s:\n' "$run_id" >&2
  if show_run_jobs "$run_id"; then
    :
  else
    printf 'Failed job links are unavailable; rerun remains an operator action.\n' >&2
    report_status=2
  fi
  printf 'gh run rerun %s --failed\n' "$run_id" >&2
  printf 'After that run passes, continue with .scripts/release.sh %s --resume-from verify\n' \
    "$release_version" >&2
  return "$report_status"
}

watch_release_run() {
  local run_id="$1"
  if quiet_external gh run watch "$run_id" --repo "$GITHUB_REPOSITORY" \
    --exit-status --interval 30 >/dev/null; then
    return 0
  fi
  printf 'release.yml failed for run %s.\n' "$run_id" >&2
  if show_run_jobs "$run_id"; then
    return 1
  fi
  return 2
}

watch_smoke_run() {
  local run_id="$1"
  if quiet_external gh run watch "$run_id" --repo "$GITHUB_REPOSITORY" \
    --exit-status --interval 30 >/dev/null; then
    printf 'distribution-smoke.yml passed for %s.\n' "$release_tag"
    return 0
  fi
  if report_smoke_failure "$run_id"; then
    return 1
  fi
  return 2
}

verify_distribution_versions() {
  local pypi_json pypi_version formula_content formula_source tap_commit mise_versions
  if pypi_json="$(quiet_external curl -fsS --max-time 30 "https://pypi.org/pypi/cocoaskills/json")"; then
    :
  else
    refuse 2 "cannot read the published PyPI version"
  fi
  if pypi_version="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])' <<<"$pypi_json")"; then
    :
  else
    refuse 2 "PyPI response is malformed"
  fi
  if [[ "$pypi_version" != "$release_version" ]]; then
    refuse 1 "PyPI reports $pypi_version instead of $release_version"
  fi
  printf 'PyPI reports cocoaskills %s.\n' "$pypi_version"

  if formula_content="$(quiet_external gh api \
    "repos/${HOMEBREW_TAP_REPOSITORY}/contents/Formula/cocoaskills.rb" \
    --jq .content)"; then
    :
  else
    refuse 2 "cannot read the Homebrew tap formula"
  fi
  if formula_source="$(python3 -c 'import base64,sys; data=b"".join(sys.argv[1].encode().split()); print(base64.b64decode(data, validate=True).decode("utf-8"), end="")' "$formula_content")"; then
    :
  else
    refuse 2 "Homebrew tap formula response is malformed"
  fi
  if python3 "$support_script" check-homebrew "$release_version" <<<"$formula_source"; then
    :
  else
    refuse 1 "Homebrew formula has not reached $release_version"
  fi
  if tap_commit="$(quiet_external gh api \
    "repos/${HOMEBREW_TAP_REPOSITORY}/commits?path=Formula/cocoaskills.rb&per_page=1" \
    --jq '.[0].sha')"; then
    :
  else
    refuse 2 "cannot read the latest Homebrew formula commit"
  fi
  if [[ ! "$tap_commit" =~ ^[0-9a-fA-F]{40}$ ]]; then
    refuse 2 "Homebrew formula commit identity is malformed"
  fi
  printf 'Homebrew formula is at %s in tap commit %s.\n' "$release_version" "$tap_commit"

  if mise_versions="$(quiet_external env MISE_FETCH_REMOTE_VERSIONS_CACHE=0 mise ls-remote pipx:cocoaskills)"; then
    :
  else
    refuse 2 "cannot read mise remote versions"
  fi
  if python3 "$support_script" check-mise "$release_version" <<<"$mise_versions"; then
    :
  else
    refuse 1 "mise does not list cocoaskills $release_version"
  fi
  printf 'mise lists cocoaskills %s.\n' "$release_version"
}

select_and_watch_run() {
  local workflow="$1"
  local event="$2"
  local head="$3"
  local after_created_at="$4"
  local purpose="$5"
  local run_info run_status run_id run_started_at
  if run_info="$(wait_for_run "$workflow" "$event" "$head" "$after_created_at")"; then
    IFS=$'\t' read -r run_id run_started_at <<<"$run_info"
  else
    run_status=$?
    return "$run_status"
  fi
  printf 'Found %s run %s for head %s.\n' "$workflow" "$run_id" "$head" >&2
  if [[ "$purpose" == "release" ]]; then
    printf '%s\t%s\n' "$run_id" "$run_started_at"
  else
    printf '%s\n' "$run_id"
  fi
}

resume_verify() {
  local release_head local_tag_object remote_tag_info remote_tag_object tag_ref
  local release_info release_run_id release_started_at smoke_run_id smoke_status run_status
  require_command git
  require_command gh
  require_command python3
  require_command curl
  require_command mise
  check_origin_repository

  tag_ref="refs/tags/$release_tag"
  if local_tag_object="$(git rev-parse "$tag_ref")"; then
    :
  else
    refuse 2 "cannot resolve local release tag $release_tag"
  fi
  if release_head="$(git rev-parse "$tag_ref^{commit}")"; then
    :
  else
    refuse 2 "cannot resolve commit for release tag $release_tag"
  fi
  if [[ ! "$release_head" =~ ^[0-9a-fA-F]{40,64}$ ]]; then
    refuse 2 "release tag does not resolve to a full commit ID"
  fi
  if ! git verify-tag "$release_tag" >/dev/null; then
    refuse 2 "release tag $release_tag is not verified as signed"
  fi
  if remote_tag_info="$(git ls-remote --tags origin "$tag_ref")"; then
    :
  else
    refuse 2 "cannot verify the published release tag"
  fi
  remote_tag_object=""
  while read -r remote_oid remote_ref; do
    if [[ "$remote_ref" == "$tag_ref" ]]; then
      remote_tag_object="$remote_oid"
    fi
  done <<<"$remote_tag_info"
  if [[ -z "$remote_tag_object" ]]; then
    refuse 1 "release tag $release_tag is not published on origin"
  fi
  if [[ "$remote_tag_object" != "$local_tag_object" ]]; then
    refuse 2 "local release tag $release_tag differs from the published tag"
  fi

  if release_info="$(select_and_watch_run release.yml push "$release_head" "" release)"; then
    :
  else
    run_status=$?
    refuse 2 "cannot establish a passing release.yml run for the published tag (status $run_status)"
  fi
  IFS=$'\t' read -r release_run_id release_started_at <<<"$release_info"
  if [[ -z "$release_started_at" ]]; then
    refuse 2 "release.yml start time is unavailable"
  fi
  if watch_release_run "$release_run_id"; then
    :
  else
    run_status=$?
    if ((run_status == 1)); then
      refuse 1 "release.yml failed for $release_tag"
    fi
    refuse 2 "release.yml outcome could not be established for $release_tag"
  fi

  if smoke_run_id="$(select_and_watch_run distribution-smoke.yml workflow_run \
    "$release_head" "$release_started_at" smoke)"; then
    :
  else
    refuse 2 "distribution-smoke.yml for the published release was not established"
  fi
  if watch_smoke_run "$smoke_run_id"; then
    :
  else
    smoke_status=$?
    if ((smoke_status == 1)); then
      refuse 1 "distribution smoke verification failed after publication"
    fi
    refuse 2 "distribution smoke verification could not be established after publication"
  fi
  verify_distribution_versions
  printf 'Post-publish verification passed for %s.\n' "$release_tag"
}

run_release() {
  local delivery_command planning_head current_branch dirty_state base_head
  local origin_main remote_refs cut_head pr_url pr_number pr_head
  local failed_checks check_status checked_head reviewed_head release_info
  local release_run_id release_started_at smoke_run_id status_code tag_ref smoke_status

  require_command git
  require_command gh
  require_command glab
  require_command curl
  require_command python3
  require_command mise

  delivery_command="$(resolve_delivery)"
  if planning_head="$(git rev-parse HEAD)"; then
    :
  else
    refuse 2 "cannot resolve the current Git head"
  fi
  if [[ ! "$planning_head" =~ ^[0-9a-fA-F]{40,64}$ ]]; then
    refuse 2 "current Git head is not a full commit ID"
  fi

  check_origin_repository
  if current_branch="$(git branch --show-current)"; then
    :
  else
    refuse 2 "cannot read the current Git branch"
  fi
  if [[ "$current_branch" != main ]]; then
    refuse 2 "run releases from a clean main checkout"
  fi
  if dirty_state="$(git status --porcelain --untracked-files=normal)"; then
    :
  else
    refuse 2 "cannot read checkout status; release state is unknown"
  fi
  if [[ -n "$dirty_state" ]]; then
    refuse 2 "release checkout has uncommitted changes"
  fi
  if ! git fetch origin main --tags >/dev/null; then
    refuse 2 "cannot refresh origin/main and release tags"
  fi
  if base_head="$(git rev-parse HEAD)"; then
    :
  else
    refuse 2 "cannot resolve local main"
  fi
  if origin_main="$(git rev-parse refs/remotes/origin/main)"; then
    :
  else
    refuse 2 "cannot resolve origin/main"
  fi
  if [[ "$base_head" != "$origin_main" ]]; then
    refuse 2 "local main is not at the current origin/main head"
  fi

  tag_ref="refs/tags/$release_tag"
  if git show-ref --verify --quiet "$tag_ref"; then
    refuse 1 "tag $release_tag already exists locally"
  else
    status_code=$?
    if ((status_code != 1)); then
      refuse 2 "cannot determine whether tag $release_tag exists locally"
    fi
  fi
  if remote_refs="$(git ls-remote --tags origin "$tag_ref")"; then
    :
  else
    refuse 2 "cannot read remote release tag state"
  fi
  if [[ -n "$remote_refs" ]]; then
    refuse 1 "tag $release_tag already exists on origin"
  fi
  if git show-ref --verify --quiet "refs/heads/$release_branch"; then
    refuse 1 "release branch $release_branch already exists locally"
  else
    status_code=$?
    if ((status_code != 1)); then
      refuse 2 "cannot determine whether release branch $release_branch exists locally"
    fi
  fi
  if remote_refs="$(git ls-remote --heads origin "refs/heads/$release_branch")"; then
    :
  else
    refuse 2 "cannot read remote release branch state"
  fi
  if [[ -n "$remote_refs" ]]; then
    refuse 1 "release branch $release_branch already exists on origin"
  fi

  preflight_ssh_signing
  if ! git switch -c "$release_branch" "$base_head" >/dev/null; then
    refuse 2 "cannot create release branch $release_branch"
  fi
  if ! python3 "$support_script" cut-changelog CHANGELOG.md "$release_version" "$release_date"; then
    refuse 1 "CHANGELOG.md cannot be cut for $release_version"
  fi
  if ! git add -- CHANGELOG.md >/dev/null ||
    ! git commit -S -m "docs: cut $release_version changelog" >/dev/null; then
    refuse 2 "cannot create the signed CHANGELOG cut commit"
  fi
  if cut_head="$(git rev-parse HEAD)"; then
    :
  else
    refuse 2 "cannot resolve the CHANGELOG cut head"
  fi
  if [[ ! "$cut_head" =~ ^[0-9a-fA-F]{40,64}$ ]] ||
    ! git verify-commit "$cut_head" >/dev/null; then
    refuse 2 "CHANGELOG cut commit is not verified as signed"
  fi
  if ! git push --no-follow-tags --set-upstream origin \
    "refs/heads/$release_branch:refs/heads/$release_branch" >/dev/null; then
    refuse 2 "cannot publish the CHANGELOG cut branch"
  fi

  if pr_url="$(quiet_external gh pr create --base main --head "$release_branch" \
    --title "docs: cut $release_version changelog" \
    --body "Signed CHANGELOG cut for $release_tag. Required checks and an exact-head review are required before fast-forwarding main." \
    --repo "$GITHUB_REPOSITORY")"; then
    :
  else
    refuse 1 "cannot open the CHANGELOG cut pull request"
  fi
  pr_number="${pr_url##*/}"
  if [[ ! "$pr_number" =~ ^[0-9]+$ ]]; then
    refuse 2 "GitHub did not return a pull-request number"
  fi
  printf 'Opened CHANGELOG cut pull request #%s.\n' "$pr_number"

  if pr_head="$(quiet_external gh pr view "$pr_number" --repo "$GITHUB_REPOSITORY" \
    --json headRefOid --jq .headRefOid)"; then
    :
  else
    refuse 2 "cannot resolve the pull-request head"
  fi
  if [[ ! "$pr_head" =~ ^[0-9a-fA-F]{40,64}$ ]]; then
    refuse 2 "pull-request head is malformed"
  fi
  if [[ "$pr_head" != "$cut_head" ]]; then
    refuse 2 "pull-request head does not match the signed CHANGELOG cut"
  fi
  if quiet_external gh pr checks "$pr_number" --repo "$GITHUB_REPOSITORY" \
    --required --watch --interval 10 >/dev/null; then
    :
  else
    check_status=$?
    if failed_checks="$(quiet_external gh pr checks "$pr_number" --repo "$GITHUB_REPOSITORY" \
      --required --json name,bucket \
      --jq '.[] | select(.bucket == "fail" or .bucket == "cancel") | .name')"; then
      :
    else
      refuse 2 "cannot read required pull-request check results (watch exit $check_status)"
    fi
    if [[ -n "$failed_checks" ]]; then
      printf 'Failed required checks for PR #%s:\n%s\n' "$pr_number" "$failed_checks" >&2
      refuse 1 "required pull-request checks failed"
    fi
    refuse 2 "required pull-request checks are not confirmed green"
  fi
  if checked_head="$(quiet_external gh pr view "$pr_number" --repo "$GITHUB_REPOSITORY" \
    --json headRefOid --jq .headRefOid)"; then
    :
  else
    refuse 2 "cannot re-read the pull-request head after checks"
  fi
  if [[ "$checked_head" != "$pr_head" ]]; then
    refuse 2 "pull-request head changed while checks were running"
  fi
  review_body="Exact-head review of $pr_head: CHANGELOG-only cut for $release_tag. Required checks are green; the cut commit is signed. Verdict: accepted; landing by fast-forward."
  if ! quiet_external gh pr review "$pr_number" --repo "$GITHUB_REPOSITORY" \
    --comment --body "$review_body" >/dev/null; then
    refuse 2 "cannot record the exact-head review comment"
  fi
  if reviewed_head="$(quiet_external gh pr view "$pr_number" --repo "$GITHUB_REPOSITORY" \
    --json headRefOid --jq .headRefOid)"; then
    :
  else
    refuse 2 "cannot re-read the pull-request head after review"
  fi
  if [[ "$reviewed_head" != "$pr_head" ]]; then
    refuse 2 "pull-request head changed after the review comment"
  fi

  if ! git fetch origin main >/dev/null; then
    refuse 2 "cannot refresh origin/main before fast-forward"
  fi
  if origin_main="$(git rev-parse refs/remotes/origin/main)"; then
    :
  else
    refuse 2 "cannot resolve origin/main before fast-forward"
  fi
  if ! git merge-base --is-ancestor "$origin_main" "$pr_head"; then
    refuse 2 "origin/main moved outside the signed CHANGELOG cut; fast-forward refused"
  fi
  if ! git push --no-follow-tags origin "${pr_head}:refs/heads/main" >/dev/null; then
    refuse 2 "main did not accept the reviewed head as a fast-forward"
  fi
  if ! git fetch origin main >/dev/null; then
    refuse 2 "cannot verify the fast-forwarded origin/main head"
  fi
  if origin_main="$(git rev-parse refs/remotes/origin/main)"; then
    :
  else
    refuse 2 "cannot resolve the fast-forwarded origin/main head"
  fi
  if [[ "$origin_main" != "$pr_head" ]]; then
    refuse 2 "origin/main is not the exact reviewed pull-request head"
  fi

  verify_trunk "$delivery_command" "$pr_head"
  if ! git tag -s "$release_tag" -m "CocoaSkills $release_version" "$pr_head" >/dev/null; then
    refuse 2 "cannot create the signed release tag"
  fi
  if ! git verify-tag "$release_tag" >/dev/null; then
    refuse 2 "signed release tag did not verify"
  fi
  if ! git push --no-follow-tags origin \
    "refs/tags/$release_tag:refs/tags/$release_tag" >/dev/null; then
    refuse 2 "cannot publish the signed release tag"
  fi
  printf 'Published signed tag %s on %s.\n' "$release_tag" "$pr_head"

  if release_info="$(select_and_watch_run release.yml push "$pr_head" "" release)"; then
    :
  else
    status_code=$?
    refuse 2 "cannot establish a passing release.yml run for the published tag (status $status_code)"
  fi
  IFS=$'\t' read -r release_run_id release_started_at <<<"$release_info"
  if [[ -z "$release_started_at" ]]; then
    refuse 2 "release.yml start time is unavailable"
  fi
  if watch_release_run "$release_run_id"; then
    :
  else
    status_code=$?
    if ((status_code == 1)); then
      refuse 1 "release.yml failed for $release_tag"
    fi
    refuse 2 "release.yml outcome could not be established for $release_tag"
  fi
  printf 'release.yml passed for %s.\n' "$release_tag"

  if ! git push --no-follow-tags "$WB_REMOTE" "${pr_head}:refs/heads/main" \
    "refs/tags/$release_tag:refs/tags/$release_tag" >/dev/null; then
    refuse 2 "cannot push main and $release_tag to the Wildberries mirror"
  fi
  printf 'Pushed main and %s to the Wildberries mirror.\n' "$release_tag"

  if ! quiet_external glab release create "$release_tag" --repo "$WB_PROJECT" \
    --name "CocoaSkills $release_version" --notes-file "$wb_notes" >/dev/null; then
    refuse 2 "cannot create the Wildberries GitLab release from the supplied notes"
  fi
  printf 'Created the Wildberries GitLab release for %s.\n' "$release_tag"

  if smoke_run_id="$(select_and_watch_run distribution-smoke.yml workflow_run \
    "$pr_head" "$release_started_at" smoke)"; then
    :
  else
    refuse 2 "distribution-smoke.yml run for the published release was not established"
  fi
  if watch_smoke_run "$smoke_run_id"; then
    :
  else
    smoke_status=$?
    if ((smoke_status == 1)); then
      refuse 1 "distribution smoke verification failed after publication"
    fi
    refuse 2 "distribution smoke verification could not be established after publication"
  fi
  verify_distribution_versions
  printf 'Release verification passed for %s.\n' "$release_tag"
}

if [[ "$dry_run" == true ]]; then
  require_command git
  require_command python3
  delivery_command="$(resolve_delivery)"
  if planning_head="$(git rev-parse HEAD)"; then
    :
  else
    refuse 2 "cannot resolve the current Git head"
  fi
  if [[ ! "$planning_head" =~ ^[0-9a-fA-F]{40,64}$ ]]; then
    refuse 2 "current Git head is not a full commit ID"
  fi
  printf '%s\n' 'Dry run: no pull request, Git ref, release, or mirror will be published.'
  print_pre_gate_plan
  print_post_gate_plan
  printf 'Dry-run gate probe on current HEAD: delivery trunk-verify --head %s\n' "$planning_head"
  printf '%s\n' 'The live release gates the exact cut head after its fast-forward to main.'
  verify_trunk "$delivery_command" "$planning_head"
  printf '%s\n' 'Dry run passed; no publishing command was invoked.'
  exit 0
fi

if [[ "$resume_from" == "verify" ]]; then
  resume_verify
  exit 0
fi

run_release
