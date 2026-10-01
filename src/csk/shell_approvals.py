from __future__ import annotations

import hashlib
import os
import re
import subprocess
import stat
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


class ShellApprovalError(ValueError):
    """A shaped approval refusal with a stable diagnostic code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def canonical_path(path: Path) -> Path:
    if any(ord(char) < 32 for char in str(path)):
        raise ShellApprovalError('shell_approval_path_invalid', 'shell approval paths must not contain control bytes')
    try:
        expanded = path.expanduser()
        try:
            resolved = expanded.resolve(strict=True)
        except FileNotFoundError:
            # Revoking an absent env path is allowed. Strict resolution still
            # exposes cyclic paths on Python 3.13+, which non-strict hides.
            resolved = expanded.resolve()
    except (RuntimeError, OSError) as exc:
        raise ShellApprovalError('shell_approval_path_invalid', f'cannot resolve {path}: {exc}') from exc
    if any(ord(char) < 32 for char in str(resolved)) or any(char in str(resolved) for char in '\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029'):
        raise ShellApprovalError('shell_approval_path_invalid', 'shell approval paths must not contain line boundaries')
    return resolved


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _check_trust(path: Path, info: os.stat_result, *, directory: bool) -> None:
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    # Windows uses the manager home's native owner/DACL provisioning. POSIX
    # ownership and write bits are meaningful only on POSIX filesystems.
    private = os.name == 'nt' or (info.st_uid == os.geteuid() and info.st_mode & 0o022 == 0)
    metadata_ok = True
    if os.name != 'nt':
        # A mode-bit stat cannot establish absence of POSIX ACL entries.
        # Only the ordinary ls mode or its macOS xattr suffix is trusted.
        try:
            result = subprocess.run(
                ['/bin/ls', '-ldn', '--', str(path.absolute())],
                capture_output=True, timeout=10, check=False,
            )
            if result.returncode != 0 or result.stdout.count(b'\n') != 1:
                _untrusted_store()
            fields = result.stdout.split()
            if fields and fields[0].endswith(b'@'):
                # macOS hides '+' behind '@' when both ACLs and xattrs exist.
                result = subprocess.run(
                    ['/bin/ls', '-ldne', '--', str(path.absolute())],
                    capture_output=True, timeout=10, check=False,
                )
                fields = result.stdout.split()
            prefix = b'd' if directory else b'-'
            metadata_ok = result.returncode == 0 and result.stdout.count(b"\n") == 1 and bool(fields) and re.fullmatch(
                prefix + rb'[r-][w-][xsS-][r-]-[xsS-][r-]-[xtT-]@?', fields[0]
            ) is not None
        except (OSError, subprocess.SubprocessError):
            metadata_ok = False
    if not kind_ok or not private or not metadata_ok:
        _untrusted_store()


def _untrusted_store() -> None:
    target = approval_file()
    raise ShellApprovalError(
        'shell_approval_store_untrusted',
        f'untrusted approval store {target} (chmod 700 {target.parent}; chmod 600 {target})',
    )


def _check_directory(path: Path) -> None:
    _check_trust(path, os.lstat(path), directory=True)
    if os.name != 'nt':
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
        except OSError as exc:
            # lstat established presence. A failed descriptor read, including
            # disappearance during open, must never become absent evidence.
            raise ShellApprovalError('shell_approval_store_read_failed', f'cannot open shell approval directory: {exc}') from exc
        try:
            _check_trust(path, os.fstat(fd), directory=True)
        finally:
            os.close(fd)


def _approval_bytes() -> bytes:
    target = approval_file()
    try:
        _check_directory(target.parent)
    except FileNotFoundError:
        return b''
    try:
        _check_trust(target, os.lstat(target), directory=False)
    except FileNotFoundError:
        return b''
    # An open failure after lstat is a read failure, never absent evidence.
    fd = os.open(target, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as handle:
        _check_trust(target, os.fstat(handle.fileno()), directory=False)
        content = handle.read()
        if any(value < 32 and value != 10 for value in content):
            _untrusted_store()
        return content


def approvals() -> str:
    try:
        return _approval_bytes().decode('utf-8')
    except (OSError, UnicodeError) as exc:
        raise ShellApprovalError('shell_approval_store_read_failed', f'cannot read shell approvals: {exc}') from exc


def record(entries: Mapping[Path, str]) -> None:
    _update({canonical_path(path): value for path, value in entries.items()})


def revoke(path: Path) -> None:
    _update({canonical_path(path): None})


def _update(entries: Mapping[Path, str | None]) -> None:
    target = approval_file()
    try:
        provision_new_manager_home(target.parent.parent)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _check_directory(target.parent)
        # A separate leaf lock reuses the stable cross-platform locking helper
        # without recursively acquiring the installer's manager-home lock.
        with _ApprovalLock(target.parent / '.approved.lock'):
            content = _approval_bytes()
            lines = content.split(b'\n')
            if lines[-1] == b'':
                lines.pop()
            replaced_paths = {str(path).encode('utf-8') for path in entries}
            retained = [line for line in lines if line.partition(b'  ')[2] not in replaced_paths]
            retained.extend(f'{value}  {path}'.encode('utf-8') for path, value in entries.items() if value is not None)
            fd, name = tempfile.mkstemp(prefix='.approved.', dir=target.parent)
            temporary = Path(name)
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(b''.join(line + b'\n' for line in retained))
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.chmod(0o600)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
    except (OSError, UnicodeError) as exc:
        raise ShellApprovalError('shell_approval_store_write_failed', f'cannot write shell approvals: {exc}') from exc


def default_env_file() -> Path:
    try:
        return _default_env_file()
    except (RuntimeError, OSError) as exc:
        raise ShellApprovalError('shell_approval_path_invalid', f'cannot resolve default env path: {exc}') from exc


def _default_env_file() -> Path:
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
