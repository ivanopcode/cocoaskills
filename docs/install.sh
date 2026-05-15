#!/bin/sh
set -eu

PACKAGE_NAME="cocoaskills"
VERSION="${CSK_VERSION:-}"

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

if has pipx; then
  log "Installing $PACKAGE_SPEC with pipx..."
  pipx install --force --python "$PYTHON" "$PACKAGE_SPEC"
elif has uv; then
  log "Installing $PACKAGE_SPEC with uv tool..."
  uv tool install --force "$PACKAGE_SPEC"
else
  log "Installing $PACKAGE_SPEC with pip --user..."
  if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    "$PYTHON" -m ensurepip --upgrade >/dev/null 2>&1 || fail "pip is unavailable; install pipx or uv and retry."
  fi
  "$PYTHON" -m pip install --user --upgrade "$PACKAGE_SPEC"
fi

if has csk; then
  csk --version
else
  log "csk was installed, but it is not on PATH yet."
  log "Add your Python user bin directory to PATH, then run: csk --version"
fi
