from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .locking import _ExclusiveFileLock, provision_new_manager_home


class _ApprovalLock(_ExclusiveFileLock):
    """Leaf lock: acquired last, released before any enclosing install lock."""

    def _check_order_before_acquire(self) -> None:
        pass

    def _record_acquired(self) -> None:
        pass

    def _record_acquire_failed(self) -> None:
        pass

    def _check_order_before_release(self) -> None:
        pass

    def _record_released(self) -> None:
        pass


def approval_file() -> Path:
    # Intentionally independent of project configuration and CSK_CONFIG.
    return Path.home() / '.cocoaskills' / 'shell' / 'approved'


def canonical_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if any(char in str(resolved) for char in '\r\n'):
        raise ValueError('shell approval paths must not contain newlines')
    return resolved


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def approvals() -> str:
    try:
        return approval_file().read_text(encoding='utf-8')
    except FileNotFoundError:
        return ''
    except (OSError, UnicodeError) as exc:
        raise ValueError(f'cannot read shell approvals: {exc}') from exc


def record(entries: Mapping[Path, str]) -> None:
    _update({canonical_path(path): value for path, value in entries.items()})


def revoke(path: Path) -> None:
    _update({canonical_path(path): None})


def _update(entries: Mapping[Path, str | None]) -> None:
    target = approval_file()
    try:
        provision_new_manager_home(target.parent.parent)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.parent.chmod(0o700)
        # A separate leaf lock reuses the stable cross-platform locking helper
        # without recursively acquiring the installer's manager-home lock.
        with _ApprovalLock(target.parent / '.approved.lock'):
            lines = approvals().splitlines()
            replaced_paths = {str(path) for path in entries}
            retained = [line for line in lines if line.partition('  ')[2] not in replaced_paths]
            retained.extend(f'{value}  {path}' for path, value in entries.items() if value is not None)
            fd, name = tempfile.mkstemp(prefix='.approved.', dir=target.parent)
            temporary = Path(name)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
                    handle.write(''.join(line + '\n' for line in retained))
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.chmod(0o600)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
    except (OSError, UnicodeError) as exc:
        raise ValueError(f'cannot write shell approvals: {exc}') from exc


def default_env_file() -> Path:
    from .shell_init import detect_shell

    filename = 'env.ps1' if detect_shell() == 'powershell' else 'env.sh'
    cwd = Path.cwd()
    logical_cwd = Path(os.environ.get('PWD', ''))
    if logical_cwd.is_absolute() and logical_cwd.resolve() == cwd.resolve():
        # Shell hooks walk logical PWD, including ancestors of directory aliases.
        # Ignore stale PWD inherited by callers that changed directory in Python.
        cwd = logical_cwd
    for directory in (cwd, *cwd.parents):
        candidate = directory / '.agents' / filename
        if candidate.is_file():
            return candidate
    cfg = Path(os.environ.get('CSK_CONFIG', str(Path.home() / '.cocoaskills/config.json')))
    candidate = cfg.parent / 'global' / filename
    if candidate.is_file():
        return candidate
    raise ValueError(f'no {filename} found for {cwd}')


def review(path: Path) -> tuple[str, str]:
    """Read one regular env file, owning filesystem refusals at this seam."""
    try:
        if not path.is_file():
            raise ValueError(f"not a regular env file: {path}")
        content = path.read_bytes()
        return digest(content), content.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot review env file {path}: {exc}") from exc
