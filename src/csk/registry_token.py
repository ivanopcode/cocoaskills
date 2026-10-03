"""Read registry authorization without placing a token in process arguments."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

MAX_TOKEN_FILE_BYTES = 64 * 1024


def registry_token(token_file: str | None) -> str:
    if token_file is None:
        token = os.environ.get("CSK_REGISTRY_TOKEN", "")
    else:
        try:
            path = Path(token_file).expanduser()
            checked = path.lstat()
            if (not stat.S_ISREG(checked.st_mode)
                    or getattr(checked, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise ValueError("--token-file must be a regular file")
            nofollow = getattr(os, "O_NOFOLLOW", None)
            if not checked.st_ino:
                raise ValueError("--token-file requires reliable file identity support")
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
            if nofollow is not None:
                flags |= nofollow
            fd = os.open(path, flags)
            try:
                stream = os.fdopen(fd, "rb")
            except BaseException:
                os.close(fd)
                raise
            with stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("--token-file must be a regular file")
                if (not info.st_ino or (checked.st_dev, checked.st_ino) != (info.st_dev, info.st_ino)
                        or stat.S_IFMT(checked.st_mode) != stat.S_IFMT(info.st_mode)):
                    raise ValueError("--token-file regular file identity changed while opening")
                if os.name != "nt" and info.st_mode & (stat.S_IRGRP | stat.S_IROTH):
                    raise ValueError("--token-file must not be readable by group or others; use chmod 600")
                if info.st_size > MAX_TOKEN_FILE_BYTES:
                    raise ValueError("--token-file must not exceed 64 KiB")
                # Bound the read too: the file may grow after fstat.
                raw = stream.read(MAX_TOKEN_FILE_BYTES + 1)
                if len(raw) > MAX_TOKEN_FILE_BYTES:
                    raise ValueError("--token-file must not exceed 64 KiB")
                token = raw.decode("utf-8")
                # A text file may end its single token line with LF or CRLF.
                if token.endswith("\r\n"):
                    token = token[:-2]
                elif token.endswith("\n"):
                    token = token[:-1]
        except OSError as exc:
            # Use only the errno description, never the path or file content.
            reason = os.strerror(exc.errno) if exc.errno is not None else "I/O error"
            raise ValueError(f"cannot read --token-file as a regular UTF-8 file: {reason}") from exc
        except (UnicodeError, RuntimeError) as exc:
            # Decode errors can contain file content.
            raise ValueError("cannot read --token-file as a regular UTF-8 file") from exc
    if not token:
        raise ValueError("--publish requires a non-empty token from CSK_REGISTRY_TOKEN or --token-file")
    if re.fullmatch(r"[A-Za-z0-9\-._~+/=]+", token) is None:
        raise ValueError("CSK_REGISTRY_TOKEN or --token-file must contain one ASCII token without spaces")
    return token
