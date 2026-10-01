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

_csk_store_trusted() {
  local store="$1" directory="${1%/*}" metadata mode links owner rest uid
  # Absence is ordinary unapproved state; a dangling symlink is not absence.
  if [ ! -e "$directory" ] && [ ! -L "$directory" ]; then
    return 0
  fi
  uid="$(id -u 2>/dev/null)" || return 1
  [ ! -L "$directory" ] && [ -d "$directory" ] || return 1
  metadata="$(LC_ALL=C ls -ldn -- "$directory" 2>/dev/null)" || return 1
  IFS=' ' read -r mode links owner rest <<EOF
$metadata
EOF
  [ "$owner" = "$uid" ] || return 1
  case "$mode" in d????-??-?*) ;; *) return 1 ;; esac
  if [ ! -e "$store" ] && [ ! -L "$store" ]; then
    return 0
  fi
  [ ! -L "$store" ] && [ -f "$store" ] || return 1
  metadata="$(LC_ALL=C ls -ldn -- "$store" 2>/dev/null)" || return 1
  IFS=' ' read -r mode links owner rest <<EOF
$metadata
EOF
  [ "$owner" = "$uid" ] || return 1
  case "$mode" in -????-??-?*) ;; *) return 1 ;; esac
}

_csk_env_approved() {
  local file="$1" result line key seen
  local store="$HOME/.cocoaskills/shell/approved"
  if ! _csk_store_trusted "$store"; then
    if [ "${_CSK_UNTRUSTED_APPROVAL_STORE:-}" != "$store" ]; then
      _CSK_UNTRUSTED_APPROVAL_STORE="$store"
      printf 'csk: untrusted approval store %s (chmod 700 %s; chmod 600 %s)\n' "$store" "${store%/*}" "$store" >&2
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
  if command -v shasum >/dev/null 2>&1; then
    result="$(shasum -a 256 < "$_csk_source_path" 2>/dev/null)" && _csk_checked_digest="${result%% *}"
  elif command -v sha256sum >/dev/null 2>&1; then
    result="$(sha256sum < "$_csk_source_path" 2>/dev/null)" && _csk_checked_digest="${result%% *}"
  elif command -v openssl >/dev/null 2>&1; then
    result="$(openssl dgst -sha256 < "$_csk_source_path" 2>/dev/null)" && _csk_checked_digest="${result##* }"
  fi
  case "$_csk_checked_digest" in
    *[!0-9a-f]*|'') _csk_checked_digest="unavailable" ;;
  esac
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
    printf 'csk: skipped %s: not approved (review it, then run: csk shell approve %s)\n' "$_csk_checked_path" "$_csk_checked_path" >&2
  fi
  return 1
}
'''


def _posix_hook(*, include_global: bool) -> str:
    global_part = r'''
_csk_global_env_file() {
  local cfg="${CSK_CONFIG:-$HOME/.cocoaskills/config.json}"
  local home_dir
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
    printf '%s/global/env.sh\n' "$home_dir"
  fi
}

_csk_source_global_env() {
  local global_env
  global_env="$(_csk_global_env_file 2>/dev/null || true)"
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
{_posix_approval_hook()}{global_part}
_csk_find_env() {{
  local dir="${{PWD:-}}"
  case "$dir" in
    /*) ;;
    *) return 1 ;;
  esac
  while :; do
    if [ -f "$dir/.agents/env.sh" ]; then
      printf '%s/.agents/env.sh\\n' "$dir"
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

_csk_auto_env() {{
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
  env_file="$(_csk_find_env 2>/dev/null || true)"
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
