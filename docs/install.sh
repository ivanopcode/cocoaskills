#!/bin/sh
set -eu

PACKAGE_NAME="cocoaskills"
VERSION="${CSK_VERSION:-}"
UV_MIN_VERSION="0.13.4"
PIP_MIN_VERSION="26.2.2"

if [ -n "$VERSION" ]; then
  PACKAGE_SPEC="${PACKAGE_NAME}==${VERSION}"
else
  PACKAGE_SPEC="$PACKAGE_NAME"
fi

log() {
  printf '%s\n' "$*"
}

fail() {
  printf 'cocoaskills install: %s\n' "$*" >&2
  exit 1
}

has() {
  command -v "$1" >/dev/null 2>&1
}

python_ok() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
}

find_python() {
  for cmd in python3 python; do
    if has "$cmd" && python_ok "$cmd"; then
      printf '%s\n' "$cmd"
      return 0
    fi
  done
  return 1
}

PYTHON="$(find_python)" || fail "Python 3.11 or newer is required."

# Version probes parse local output; never bootstrap a package installer
# merely to discover whether its version is acceptable.
client_version() {
  "$PYTHON" - "$1" "$2" <<'PY'
import re
import sys

client, output = sys.argv[1:]
parts = output.split()
version = "unknown"
if client == "pipx" and len(parts) == 1:
    version = parts[0]
elif len(parts) >= 2 and parts[0] == client:
    version = parts[1]
print(version if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,99}", version) else "unknown")
PY
}

version_at_least() {
  "$PYTHON" - "$1" "$2" <<'PY'
import re
import sys

version, floor = sys.argv[1:]
# Accept established stable releases; prereleases and unrecognized output
# cannot establish that the client contains the required trust update.
if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", version):
    raise SystemExit(1)
release = tuple(int(part) for part in version.split("."))
minimum = tuple(int(part) for part in floor.split("."))
width = max(len(release), len(minimum))
release += (0,) * (width - len(release))
minimum += (0,) * (width - len(minimum))
raise SystemExit(0 if release >= minimum else 1)
PY
}

REJECTED=""
reject_client() {
  REJECTED="${REJECTED}
- $*"
}

CLIENT=""
if has uv; then
  UV_OUTPUT="$(uv --version 2>/dev/null)" || UV_OUTPUT=""
  UV_VERSION="$(client_version uv "$UV_OUTPUT")"
  if version_at_least "$UV_VERSION" "$UV_MIN_VERSION"; then
    CLIENT="uv"
  else
    reject_client "uv $UV_VERSION requires >= $UV_MIN_VERSION; upgrade: uv self update"
  fi
fi

if [ -z "$CLIENT" ] && has pipx; then
  PIPX_OUTPUT="$(pipx --version 2>/dev/null)" || PIPX_OUTPUT=""
  PIPX_VERSION="$(client_version pipx "$PIPX_OUTPUT")"
  SHARED_LIBS="$(pipx environment --value PIPX_SHARED_LIBS 2>/dev/null)" || SHARED_LIBS=""
  SHARED_PIP_VERSION="unknown"
  if [ -n "$SHARED_LIBS" ] && [ -x "$SHARED_LIBS/bin/python" ]; then
    SHARED_PIP_OUTPUT="$("$SHARED_LIBS/bin/python" -m pip --version 2>/dev/null)" || SHARED_PIP_OUTPUT=""
    SHARED_PIP_VERSION="$(client_version pip "$SHARED_PIP_OUTPUT")"
  fi
  if version_at_least "$SHARED_PIP_VERSION" "$PIP_MIN_VERSION"; then
    CLIENT="pipx"
  else
    reject_client "pipx $PIPX_VERSION (shared pip $SHARED_PIP_VERSION) requires pip >= $PIP_MIN_VERSION; upgrade: pipx upgrade-shared"
  fi
fi

if [ -z "$CLIENT" ]; then
  PIP_OUTPUT="$("$PYTHON" -m pip --version 2>/dev/null)" || PIP_OUTPUT=""
  PIP_VERSION="$(client_version pip "$PIP_OUTPUT")"
  if version_at_least "$PIP_VERSION" "$PIP_MIN_VERSION"; then
    CLIENT="pip"
  else
    reject_client "pip $PIP_VERSION requires >= $PIP_MIN_VERSION; upgrade: python3 -m pip install --upgrade pip"
  fi
fi

case "$CLIENT" in
uv)
  log "Installing $PACKAGE_SPEC with uv tool..."
  UV_SYSTEM_CERTS=1 uv tool install --force "$PACKAGE_SPEC"
  ;;
pipx)
  log "Installing $PACKAGE_SPEC with pipx..."
  pipx install --force --python "$PYTHON" "$PACKAGE_SPEC"
  ;;
pip)
  log "Installing $PACKAGE_SPEC with pip --user..."
  "$PYTHON" -m pip install --user --upgrade "$PACKAGE_SPEC"
  ;;
*)
  fail "No acceptable installation client found.${REJECTED}"
  ;;
esac

if has csk; then
  csk --version
else
  log "csk was installed, but it is not on PATH yet."
  log "Add your Python user bin directory to PATH, then run: csk --version"
fi
