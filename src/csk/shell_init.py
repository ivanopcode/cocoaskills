from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path


_HOOK_FILENAMES = {
    "zsh": "csk.zsh",
    "bash": "csk.bash",
    "powershell": "csk.ps1",
}


def detect_shell(
    *,
    env: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> str:
    """Return the best supported shell for the current process environment."""
    values = os.environ if env is None else env
    configured = values.get("SHELL", "").strip().replace("\\", "/")
    name = configured.rsplit("/", 1)[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if name in {"zsh", "bash"}:
        # SHELL wins on Windows too so Git Bash keeps its POSIX hook.
        return name

    effective_platform = platform_name or ("windows" if os.name == "nt" else "posix")
    if effective_platform == "windows" or values.get("PSModulePath"):
        return "powershell"
    # Preserve the historical, portable fallback for containers and minimal CI.
    return "bash"


def shell_init(shell: str, *, include_global: bool = True) -> str:
    if shell in {"zsh", "bash"}:
        return _posix_hook(include_global=include_global)
    if shell == "powershell":
        return _powershell_hook(include_global=include_global)
    raise ValueError(f"Unsupported shell: {shell}")


def install_shell_hook(shell: str, csk_home: Path, *, include_global: bool = True) -> Path:
    try:
        filename = _HOOK_FILENAMES[shell]
    except KeyError as exc:
        raise ValueError(f"Unsupported shell: {shell}") from exc
    hooks_dir = csk_home / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    target = hooks_dir / filename
    fd, temporary_name = tempfile.mkstemp(prefix=f".{filename}.", dir=hooks_dir)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(shell_init(shell, include_global=include_global))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


def refresh_existing_cached_hooks(csk_home: Path, *, include_global: bool = True) -> list[Path]:
    """Rewrite every cached hook that already exists to the current version.

    Install lanes call this so an old cached hook cannot stay ungated after
    an upgrade. Only existing files are rewritten; absent hooks are never
    created, so plain installs do not change shell startup.
    """
    refreshed: list[Path] = []
    hooks_dir = csk_home / "hooks"
    for shell, filename in _HOOK_FILENAMES.items():
        target = hooks_dir / filename
        if target.is_symlink() or target.is_file():
            refreshed.append(
                install_shell_hook(shell, csk_home, include_global=include_global)
            )
    return refreshed


def source_command(shell: str, hook_path: Path) -> str:
    value = str(hook_path)
    if shell == "powershell":
        return ". '" + value.replace("'", "''") + "'"
    if shell in {"zsh", "bash"}:
        return ". '" + value.replace("'", "'\"'\"'") + "'"
    raise ValueError(f"Unsupported shell: {shell}")


def _posix_approval_hook() -> str:
    return r'''
_csk_realpath() {
  local file="$1" parent target
  local _CSK_CHECKING_ENV=1
  while [ -L "$file" ]; do
    command -v readlink >/dev/null 2>&1 || return 1
    target="$(readlink "$file")" || return 1
    case "$target" in
      /*) file="$target" ;;
      *) file="${file%/*}/$target" ;;
    esac
  done
  parent="${file%/*}"
  [ "$parent" != "$file" ] || parent="."
  [ -n "$parent" ] || parent="/"
  parent="$(cd -P -- "$parent" && pwd -P)" || return 1
  file="${parent%/}/${file##*/}"
  printf '%s\n' "$file"
}

_csk_display_and_quote() {
  # $1 is a raw path. Sets _csk_display (every control byte shown as '?')
  # and _csk_remedy (the same text single-quoted for copy-paste, with an
  # apostrophe spelled '\'' ). No forks: notice paths stay dependency-free.
  _csk_display=""
  _csk_remedy="'"
  _csk_rest="$1"
  while [ -n "$_csk_rest" ]; do
    _csk_char="${_csk_rest%"${_csk_rest#?}"}"
    case "$_csk_char" in
      [[:cntrl:]])
        _csk_display="${_csk_display}?"
        _csk_remedy="${_csk_remedy}?"
        ;;
      "'")
        _csk_display="${_csk_display}'"
        _csk_remedy="${_csk_remedy}'\\''"
        ;;
      *)
        _csk_display="${_csk_display}${_csk_char}"
        _csk_remedy="${_csk_remedy}${_csk_char}"
        ;;
    esac
    _csk_rest="${_csk_rest#?}"
  done
  _csk_remedy="${_csk_remedy}'"
}

_csk_check_meta() {
  # $1 path, $2 kind (d/-), $3 label, $4 fix mode. Shared ls/owner/mode and
  # metadata-suffix check; sets _csk_store_cause on failure. Reads the
  # current uid from _csk_uid, set by the caller. No output on success.
  local metadata mode links owner rest base suffix
  _csk_display_and_quote "$1"
  metadata="$(LC_ALL=C ls -ldn -- "$1" 2>/dev/null)" || {
    _csk_store_cause="cannot verify metadata of $3 $_csk_display"
    return 1
  }
  case "$metadata" in *'
'*)
    _csk_store_cause="cannot verify metadata of $3 $_csk_display"
    return 1
    ;;
  esac
  IFS=' ' read -r mode links owner rest <<EOF
$metadata
EOF
  case "$mode" in *@)
    # macOS hides '+' behind '@' when both ACLs and xattrs exist. Extra
    # output lines after -e are the ACL entries themselves.
    metadata="$(LC_ALL=C ls -ldne -- "$1" 2>/dev/null)" || {
      _csk_store_cause="cannot verify extended metadata of $3 $_csk_display"
      return 1
    }
    case "$metadata" in *'
'*)
      _csk_store_cause="$3 $_csk_display has ACL entries (macOS: chmod -N $_csk_remedy; Linux: setfacl -b $_csk_remedy)"
      return 1
      ;;
    esac
    IFS=' ' read -r mode links owner rest <<EOF
$metadata
EOF
    ;;
  esac
  [ "$owner" = "$_csk_uid" ] || {
    _csk_store_cause="$3 $_csk_display is owned by uid $owner, expected $_csk_uid"
    return 1
  }
  base="${mode%"${mode#??????????}"}"
  suffix="${mode#??????????}"
  case "$base" in
    ?????w????|????????w?)
      _csk_store_cause="$3 $_csk_display has mode $base writable by group or others (fix: chmod $4 $_csk_remedy)"
      return 1
      ;;
  esac
  case "$base" in
    "$2"[r-][w-][xsS-][r-]-[xsS-][r-]-[xtT-]) ;;
    *)
      _csk_store_cause="cannot verify metadata of $3 $_csk_display"
      return 1
      ;;
  esac
  # A GNU ls SELinux context label '.' is accepted like no suffix: it is a
  # label, not an access grant. ACL '+' and unknown suffixes stay untrusted.
  case "$suffix" in
    ""|"."|"@") ;;
    "+")
      _csk_store_cause="$3 $_csk_display has ACL entries (macOS: chmod -N $_csk_remedy; Linux: setfacl -b $_csk_remedy)"
      return 1
      ;;
    *)
      _csk_display_and_quote "$suffix"
      _csk_suffix_display="$_csk_display"
      _csk_display_and_quote "$1"
      _csk_store_cause="$3 $_csk_display has unrecognized ls metadata suffix '$_csk_suffix_display'"
      return 1
      ;;
  esac
}

_csk_store_trusted() {
  local store="$1" directory="${1%/*}"
  _csk_store_cause=""
  # Absence is ordinary unapproved state; a dangling symlink is not absence.
  if [ ! -e "$directory" ] && [ ! -L "$directory" ]; then
    return 0
  fi
  _csk_uid="$(id -u 2>/dev/null)" || {
    _csk_store_cause="cannot determine current user"
    return 1
  }
  if [ -L "$directory" ]; then
    _csk_display_and_quote "$directory"
    _csk_store_cause="store directory $_csk_display is a symlink"
    return 1
  fi
  if [ ! -d "$directory" ]; then
    _csk_display_and_quote "$directory"
    _csk_store_cause="store directory $_csk_display is not a directory"
    return 1
  fi
  _csk_check_meta "$directory" d "store directory" 700 || return 1
  if [ ! -e "$store" ] && [ ! -L "$store" ]; then
    return 0
  fi
  if [ -L "$store" ]; then
    _csk_display_and_quote "$store"
    _csk_store_cause="approval store $_csk_display is a symlink"
    return 1
  fi
  if [ ! -f "$store" ]; then
    _csk_display_and_quote "$store"
    _csk_store_cause="approval store $_csk_display is not a regular file"
    return 1
  fi
  _csk_check_meta "$store" - "approval store" 600 || return 1
  if [ ! -r "$store" ]; then
    _csk_display_and_quote "$store"
    _csk_store_cause="approval store $_csk_display cannot be read"
    return 1
  fi
  # Shell read (notably bash) can discard NULs. Establish byte safety before
  # any record read; lack or failure of either tool makes trust unknown.
  if ! command -v tr >/dev/null 2>&1 || ! command -v cmp >/dev/null 2>&1; then
    _csk_store_cause="cannot verify store bytes (missing tr or cmp)"
    return 1
  fi
  if (set -o pipefail; LC_ALL=C tr -d '\000-\011\013-\037' < /dev/null | cmp -s - /dev/null); then
    : byte tools work
  else
    _csk_store_cause="cannot verify store bytes (tr or cmp failed)"
    return 1
  fi
  if (set -o pipefail; LC_ALL=C tr -d '\000-\011\013-\037' < "$store" | cmp -s - "$store"); then
    return 0
  fi
  _csk_display_and_quote "$store"
  _csk_store_cause="approval store $_csk_display contains control bytes"
  return 1
}

_csk_env_approved() {
  local file="$1" result line key seen
  local store="$HOME/.cocoaskills/shell/approved"
  if ! _csk_store_trusted "$store"; then
    if [ "${_CSK_UNTRUSTED_APPROVAL_STORE:-}" != "$store" ]; then
      _CSK_UNTRUSTED_APPROVAL_STORE="$store"
      _csk_display_and_quote "$store"
      printf 'csk: untrusted approval store %s: %s\n' "$_csk_display" "$_csk_store_cause" >&2
    fi
    return 1
  fi
  _csk_checked_path="$(_csk_realpath "$file" 2>/dev/null)" || _csk_checked_path="$file"
  _csk_source_path="$_csk_checked_path"
  # Compare Python's native Windows path while sourcing the POSIX spelling
  # so generated env.sh can use BASH_SOURCE unchanged in Git Bash.
  if command -v cygpath >/dev/null 2>&1; then
    _csk_checked_path="$(cygpath -wa "$_csk_source_path")" || return 1
  fi
  _csk_checked_digest="unavailable"
  _csk_have_digest_tool=0
  if command -v shasum >/dev/null 2>&1; then
    _csk_have_digest_tool=1
    result="$(shasum -a 256 < "$_csk_source_path" 2>/dev/null)" && _csk_checked_digest="${result%% *}"
  elif command -v sha256sum >/dev/null 2>&1; then
    _csk_have_digest_tool=1
    result="$(sha256sum < "$_csk_source_path" 2>/dev/null)" && _csk_checked_digest="${result%% *}"
  elif command -v openssl >/dev/null 2>&1; then
    _csk_have_digest_tool=1
    result="$(openssl dgst -sha256 < "$_csk_source_path" 2>/dev/null)" && _csk_checked_digest="${result##* }"
  fi
  case "$_csk_checked_digest" in
    *[!0-9a-f]*|'') _csk_checked_digest="unavailable" ;;
  esac
  if [ "$_csk_checked_digest" = "unavailable" ] && [ "$_csk_have_digest_tool" = 0 ]; then
    if [ "${_CSK_NO_DIGEST_TOOL:-}" != 1 ]; then
      _CSK_NO_DIGEST_TOOL=1
      printf 'csk: no sha256 tool found; env files are not sourced\n' >&2
    fi
    return 1
  fi
  if [ "${#_csk_checked_digest}" = 64 ] && [ -r "$store" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      if [ "$line" = "$_csk_checked_digest  $_csk_checked_path" ]; then
        return 0
      fi
    done < "$store"
  fi
  key="$_csk_checked_digest  $_csk_checked_path"
  seen=0
  while IFS= read -r line; do
    [ "$line" != "$key" ] || seen=1
  done <<EOF
${_CSK_SKIPPED_ENVS:-}
EOF
  if [ "$seen" = 0 ]; then
    _CSK_SKIPPED_ENVS="${_CSK_SKIPPED_ENVS:+$_CSK_SKIPPED_ENVS
}$key"
    _csk_display_and_quote "$_csk_checked_path"
    printf 'csk: skipped %s: not approved (review it, then run: csk shell approve %s)\n' "$_csk_display" "$_csk_remedy" >&2
  fi
  return 1
}
'''


def _posix_hook(*, include_global: bool) -> str:
    finder_part = r'''
_csk_global_env_file() {
  # Sets _csk_found_global to the global env lexical path, or to the empty
  # string. No forks: the memoised fast path calls this on every prompt.
  local cfg="${CSK_CONFIG:-$HOME/.cocoaskills/config.json}"
  local home_dir
  _csk_found_global=""
  case "$cfg" in
    [A-Za-z]:\\*|[A-Za-z]:/*) cfg="${cfg//\\//}" ;;
  esac
  home_dir="${cfg%/*}"
  if [ "$home_dir" = "$cfg" ]; then
    home_dir="."
  elif [ -z "$home_dir" ]; then
    home_dir="/"
  fi
  if [ -f "$home_dir/global/env.sh" ]; then
    _csk_found_global="$home_dir/global/env.sh"
  fi
  return 0
}
'''
    global_part = r'''
_csk_source_global_env() {
  local global_env
  _csk_global_env_file
  global_env="$_csk_found_global"
  if [ -n "$global_env" ] && _csk_env_approved "$global_env"; then
    global_env="$_csk_checked_path"
    if [ "${_CSK_ACTIVE_GLOBAL_LINE:-}" != "$_csk_checked_digest  $global_env" ]; then
      CSK_ACTIVE_GLOBAL_ENV="$global_env"
      _CSK_ACTIVE_GLOBAL_LINE="$_csk_checked_digest  $global_env"
      export CSK_ACTIVE_GLOBAL_ENV
      . "$_csk_source_path"
    fi
  fi
}
''' if include_global else ""
    source_global = "  _csk_source_global_env\n" if include_global else ""
    return f'''# CocoaSkill shell hook
{_posix_approval_hook()}{finder_part}{global_part}
_csk_find_env() {{
  # Sets _csk_found_env to the nearest project env lexical path, or to the
  # empty string. No forks: the memoised fast path calls this on every prompt.
  local dir="${{PWD:-}}"
  _csk_found_env=""
  case "$dir" in
    /*) ;;
    *) return 1 ;;
  esac
  while :; do
    if [ -f "$dir/.agents/env.sh" ]; then
      _csk_found_env="$dir/.agents/env.sh"
      return 0
    fi
    if [ "$dir" = "/" ]; then
      break
    fi
    dir="${{dir%/*}}"
    if [ -z "$dir" ]; then
      dir="/"
    fi
  done
  return 1
}}

_csk_stat_key() {{
  # $1 project realpath, $2 global realpath, $3 store, $4 store dir; an empty
  # argument means absent and is not listed. Prints one ls -ldin listing used
  # as the memo key: inode, size and mtime of both env files, the store and
  # its directory. Runs inside $(...) as the single warm-path fork.
  local p1="$1" p2="$2"
  set -- "$3" "$4"
  [ -n "$p1" ] && set -- "$p1" "$@"
  [ -n "$p2" ] && set -- "$p2" "$@"
  LC_ALL=C ls -ldin -- "$@" 2>/dev/null
}}

_csk_memo_populate() {{
  # Records the current trust and approval inputs after a slow check. Any
  # resolution or listing failure leaves the memo invalid, so the next call
  # runs the slow check again instead of trusting a partial key.
  _CSK_MEMO_VALID=0
  _CSK_MEMO_AUTO_ENV="${{CSK_AUTO_ENV:-1}}"
  _CSK_MEMO_HOME="$HOME"
  _CSK_MEMO_PROJ="$_csk_found_env"
  _CSK_MEMO_GLOB="$_csk_found_global"
  _CSK_MEMO_PROJ_REAL=""
  _CSK_MEMO_GLOB_REAL=""
  if [ -n "$_csk_found_env" ]; then
    _CSK_MEMO_PROJ_REAL="$(_csk_realpath "$_csk_found_env" 2>/dev/null)" || return 0
  fi
  if [ -n "$_csk_found_global" ]; then
    _CSK_MEMO_GLOB_REAL="$(_csk_realpath "$_csk_found_global" 2>/dev/null)" || return 0
  fi
  # A bare assignment from a failing substitution would kill set -e shells,
  # so the rc is captured through the || list instead of $?.
  _csk_memo_rc_new=0
  _csk_memo_key_new="$(_csk_stat_key "$_CSK_MEMO_PROJ_REAL" "$_CSK_MEMO_GLOB_REAL" "$HOME/.cocoaskills/shell/approved" "$HOME/.cocoaskills/shell")" || _csk_memo_rc_new=$?
  if [ "$_csk_memo_rc_new" != 0 ] && ! command -v ls >/dev/null 2>&1; then
    return 0
  fi
  _CSK_MEMO_KEY="$_csk_memo_key_new"
  _CSK_MEMO_RC="$_csk_memo_rc_new"
  _CSK_MEMO_VALID=1
}}

_csk_auto_env() {{
  [ "${{_CSK_CHECKING_ENV:-0}}" != 1 ] || return 0
  # Warm path: when the located files, their resolved identities and one ls
  # listing all match the previous slow check, no decision can have changed.
  _csk_found_env=""
  _csk_found_global=""
  _csk_find_env || true
  _csk_global_env_file
  if [ "${{_CSK_MEMO_VALID:-0}}" = 1 ] \
     && [ "${{_CSK_MEMO_AUTO_ENV:-}}" = "${{CSK_AUTO_ENV:-1}}" ] \
     && [ "${{_CSK_MEMO_HOME:-}}" = "$HOME" ] \
     && [ "${{_CSK_MEMO_PROJ:-}}" = "$_csk_found_env" ] \
     && [ "${{_CSK_MEMO_GLOB:-}}" = "$_csk_found_global" ]; then
    _csk_memo_ok=1
    if [ -n "$_csk_found_env" ]; then
      [ "$_csk_found_env" -ef "${{_CSK_MEMO_PROJ_REAL:-}}" ] || _csk_memo_ok=0
    elif [ -n "${{_CSK_MEMO_PROJ_REAL:-}}" ]; then
      _csk_memo_ok=0
    fi
    if [ -n "$_csk_found_global" ]; then
      [ "$_csk_found_global" -ef "${{_CSK_MEMO_GLOB_REAL:-}}" ] || _csk_memo_ok=0
    elif [ -n "${{_CSK_MEMO_GLOB_REAL:-}}" ]; then
      _csk_memo_ok=0
    fi
    if [ "$_csk_memo_ok" = 1 ]; then
      _csk_memo_rc_new=0
      _csk_memo_key_new="$(_csk_stat_key "${{_CSK_MEMO_PROJ_REAL:-}}" "${{_CSK_MEMO_GLOB_REAL:-}}" "$HOME/.cocoaskills/shell/approved" "$HOME/.cocoaskills/shell")" || _csk_memo_rc_new=$?
      if [ "$_csk_memo_rc_new" = "${{_CSK_MEMO_RC:-}}" ] && [ "$_csk_memo_key_new" = "${{_CSK_MEMO_KEY:-}}" ]; then
        if [ "$_csk_memo_rc_new" = 0 ] || command -v ls >/dev/null 2>&1; then
          return 0
        fi
      fi
    fi
  fi
  _csk_auto_env_slow
  _csk_memo_populate
  return 0
}}

_csk_auto_env_slow() {{
  local env_file approved=0
  [ "${{_CSK_CHECKING_ENV:-0}}" != 1 ] || return 0
{source_global}  if [ "${{CSK_AUTO_ENV:-1}}" = "0" ]; then
    if [ -n "$CSK_ACTIVE_ENV" ]; then
      PATH="$CSK_OLD_PATH"
      export PATH
      unset CSK_ACTIVE_ENV
      unset CSK_OLD_PATH
    fi
    return 0
  fi
  _csk_find_env || true
  env_file="$_csk_found_env"
  if [ -n "$env_file" ] && _csk_env_approved "$env_file"; then
    env_file="$_csk_checked_path"
    approved=1
  fi
  if [ -n "$CSK_ACTIVE_ENV" ] && {{ [ "$CSK_ACTIVE_ENV" != "$env_file" ] || [ "$approved" = 0 ] || [ "${{_CSK_ACTIVE_ENV_LINE:-}}" != "$_csk_checked_digest  $env_file" ]; }}; then
    PATH="$CSK_OLD_PATH"
    export PATH
    unset CSK_ACTIVE_ENV
    unset CSK_OLD_PATH
  fi
  if [ "$approved" = 1 ] && [ "$CSK_ACTIVE_ENV" != "$env_file" ]; then
    CSK_OLD_PATH="$PATH"
    export CSK_OLD_PATH
    # Mark the environment active before sourcing it. zsh runs chpwd hooks for
    # a cd inside env.sh command substitutions, so setting this afterwards can
    # recursively source the same file.
    CSK_ACTIVE_ENV="$env_file"
    _CSK_ACTIVE_ENV_LINE="$_csk_checked_digest  $env_file"
    export CSK_ACTIVE_ENV
    . "$_csk_source_path"
  fi
  return 0
}}

case "$SHELL" in
  *zsh*)
    autoload -Uz add-zsh-hook 2>/dev/null || true
    add-zsh-hook -d chpwd _csk_auto_env 2>/dev/null || true
    add-zsh-hook chpwd _csk_auto_env 2>/dev/null || true
    ;;
esac
case ";${{PROMPT_COMMAND:-}};" in
  *";_csk_auto_env;"*) ;;
  *) PROMPT_COMMAND="_csk_auto_env${{PROMPT_COMMAND:+;$PROMPT_COMMAND}}" ;;
esac
_csk_auto_env
'''


def _powershell_hook(*, include_global: bool) -> str:
    global_part = r'''
function Get-CskGlobalEnvFile {
  $cfg = if ($env:CSK_CONFIG) { $env:CSK_CONFIG } else { Join-Path $HOME ".cocoaskills/config.json" }
  $homeDir = Split-Path -Parent $cfg
  $candidate = Join-Path $homeDir "global/env.ps1"
  if (Test-Path $candidate) { return $candidate }
  return $null
}

function Invoke-CskGlobalEnv {
  $globalEnv = Get-CskGlobalEnvFile
  if ($globalEnv -and $env:CSK_ACTIVE_GLOBAL_ENV -ne $globalEnv) {
    . $globalEnv
    $env:CSK_ACTIVE_GLOBAL_ENV = $globalEnv
  }
}
''' if include_global else ""
    source_global = "  Invoke-CskGlobalEnv\n" if include_global else ""
    return f'''# CocoaSkill shell hook
{global_part}
function Invoke-CskAutoEnv {{
{source_global}  if ($env:CSK_AUTO_ENV -eq "0") {{
    if ($env:CSK_ACTIVE_ENV) {{
      $env:PATH = $env:CSK_OLD_PATH
      Remove-Item Env:\\CSK_ACTIVE_ENV -ErrorAction SilentlyContinue
      Remove-Item Env:\\CSK_OLD_PATH -ErrorAction SilentlyContinue
    }}
    return
  }}
  $dir = Get-Location
  $envFile = $null
  while ($dir) {{
    $candidate = Join-Path $dir ".agents/env.ps1"
    if (Test-Path $candidate) {{ $envFile = $candidate; break }}
    $parent = Split-Path -Parent $dir
    if ($parent -eq $dir) {{ break }}
    $dir = $parent
  }}
  if ($env:CSK_ACTIVE_ENV -and $env:CSK_ACTIVE_ENV -ne $envFile) {{
    $env:PATH = $env:CSK_OLD_PATH
    Remove-Item Env:\\CSK_ACTIVE_ENV -ErrorAction SilentlyContinue
    Remove-Item Env:\\CSK_OLD_PATH -ErrorAction SilentlyContinue
  }}
  if ($envFile -and $env:CSK_ACTIVE_ENV -ne $envFile) {{
    $env:CSK_OLD_PATH = $env:PATH
    . $envFile
    $env:CSK_ACTIVE_ENV = $envFile
  }}
}}
if (-not $global:CskPromptWrapped) {{
  $global:CskOriginalPrompt = (Get-Item Function:prompt -ErrorAction SilentlyContinue).ScriptBlock
  function global:prompt {{
    Invoke-CskAutoEnv
    if ($global:CskOriginalPrompt) {{
      return & $global:CskOriginalPrompt
    }}
    return "PS $($executionContext.SessionState.Path.CurrentLocation)> "
  }}
  $global:CskPromptWrapped = $true
}}
Invoke-CskAutoEnv
'''
