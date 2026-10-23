"""Name/stat-only base-system snapshot; never open bodies or execute programs.

Run with ``uv run python .scripts/scan_mcp_base_commands.py``. The JSON output
is review input for tests/fixtures/mcp/base-system-commands.json.
"""
from __future__ import annotations

import datetime
import json
import os
import platform
import stat

ROOTS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def scan(roots=ROOTS):
    entries, errors = [], []
    for root in roots:
        try:
            with os.scandir(root) as paths:
                for path in paths:
                    try:
                        info = path.stat()
                        if stat.S_ISREG(info.st_mode) and info.st_mode & 0o111:
                            entries.append({"path": path.path, "name": path.name})
                    except OSError as exc:
                        errors.append({"path": path.path, "error": type(exc).__name__})
                        # Keep an uninspectable OS entry conservative; the
                        # snapshot does not claim its executable metadata.
                        entries.append({"path": path.path, "name": path.name,
                                        "metadata_error": type(exc).__name__})
        except OSError as exc:
            errors.append({"path": root, "error": type(exc).__name__})
    return {"date": datetime.date.today().isoformat(),
            "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
            "roots": list(roots), "entries": sorted(entries, key=lambda e: e["path"]),
            "errors": errors, "source": ".scripts/scan_mcp_base_commands.py",
            "limits": "Executable-mode regular files, following kernel stat. Names and metadata only; no body reads or execution."}


if __name__ == "__main__":
    print(json.dumps(scan(), indent=2, sort_keys=True))
