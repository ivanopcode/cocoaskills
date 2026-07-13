from __future__ import annotations


def shell_init(shell: str, *, include_global: bool = True) -> str:
    if shell in {"zsh", "bash"}:
        return _posix_hook(include_global=include_global)
    if shell == "powershell":
        return _powershell_hook(include_global=include_global)
    raise ValueError(f"Unsupported shell: {shell}")


def _posix_hook(*, include_global: bool) -> str:
    global_part = r'''
_csk_global_env_file() {
  local cfg="${CSK_CONFIG:-$HOME/.cocoaskills/config.json}"
  local home_dir
  home_dir="$(dirname "$cfg")"
  if [ -f "$home_dir/global/env.sh" ]; then
    printf '%s/global/env.sh\n' "$home_dir"
  fi
}

_csk_source_global_env() {
  local global_env
  global_env="$(_csk_global_env_file 2>/dev/null || true)"
  if [ -n "$global_env" ] && [ "$CSK_ACTIVE_GLOBAL_ENV" != "$global_env" ]; then
    . "$global_env"
    CSK_ACTIVE_GLOBAL_ENV="$global_env"
    export CSK_ACTIVE_GLOBAL_ENV
  fi
}
''' if include_global else ""
    source_global = "  _csk_source_global_env\n" if include_global else ""
    return f'''# CocoaSkill shell hook
{global_part}
_csk_find_env() {{
  local dir="$PWD"
  while [ "$dir" != "/" ]; do
    if [ -f "$dir/.agents/env.sh" ]; then
      printf '%s/.agents/env.sh\\n' "$dir"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  return 1
}}

_csk_auto_env() {{
{source_global}  local env_file
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
}}

case "$SHELL" in
  *zsh*) autoload -Uz add-zsh-hook 2>/dev/null || true; add-zsh-hook chpwd _csk_auto_env 2>/dev/null || true ;;
esac
PROMPT_COMMAND="_csk_auto_env${{PROMPT_COMMAND:+;$PROMPT_COMMAND}}"
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
{source_global}  $dir = Get-Location
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
