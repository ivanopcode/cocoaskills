from __future__ import annotations


def shell_init(shell: str) -> str:
    if shell in {"zsh", "bash"}:
        return _posix_hook()
    if shell == "powershell":
        return _powershell_hook()
    raise ValueError(f"Unsupported shell: {shell}")


def _posix_hook() -> str:
    return r'''# CocoaSkill shell hook
_csk_find_env() {
  local dir="$PWD"
  while [ "$dir" != "/" ]; do
    if [ -f "$dir/.agents/env.sh" ]; then
      printf '%s/.agents/env.sh\n' "$dir"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  return 1
}

_csk_auto_env() {
  local env_file
  env_file="$(_csk_find_env 2>/dev/null || true)"
  if [ -n "$CSK_ACTIVE_ENV" ] && [ "$CSK_ACTIVE_ENV" != "$env_file" ]; then
    PATH="$CSK_OLD_PATH"
    export PATH
    unset CSK_ACTIVE_ENV
    unset CSK_OLD_PATH
  fi
  if [ -n "$env_file" ] && [ "$CSK_ACTIVE_ENV" != "$env_file" ]; then
    CSK_OLD_PATH="$PATH"
    export CSK_OLD_PATH
    . "$env_file"
    CSK_ACTIVE_ENV="$env_file"
    export CSK_ACTIVE_ENV
  fi
}

case "$SHELL" in
  *zsh*) autoload -Uz add-zsh-hook 2>/dev/null || true; add-zsh-hook chpwd _csk_auto_env 2>/dev/null || true ;;
esac
PROMPT_COMMAND="_csk_auto_env${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
_csk_auto_env
'''


def _powershell_hook() -> str:
    return r'''# CocoaSkill shell hook
function Invoke-CskAutoEnv {
  $dir = Get-Location
  $envFile = $null
  while ($dir) {
    $candidate = Join-Path $dir ".agents/env.ps1"
    if (Test-Path $candidate) { $envFile = $candidate; break }
    $parent = Split-Path -Parent $dir
    if ($parent -eq $dir) { break }
    $dir = $parent
  }
  if ($env:CSK_ACTIVE_ENV -and $env:CSK_ACTIVE_ENV -ne $envFile) {
    $env:PATH = $env:CSK_OLD_PATH
    Remove-Item Env:\CSK_ACTIVE_ENV -ErrorAction SilentlyContinue
    Remove-Item Env:\CSK_OLD_PATH -ErrorAction SilentlyContinue
  }
  if ($envFile -and $env:CSK_ACTIVE_ENV -ne $envFile) {
    $env:CSK_OLD_PATH = $env:PATH
    . $envFile
    $env:CSK_ACTIVE_ENV = $envFile
  }
}
Invoke-CskAutoEnv
'''

