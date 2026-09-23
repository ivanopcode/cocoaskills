"""SCRATCH probe for BUG-260923-398wee. Never lands: draft probe PR only.

Reports, per Windows interpreter, what the fault injector can observe of
Path.resolve() and the two failing sweeps' unfaulted touch baselines.
"""
import errno
import os
import sys
from pathlib import Path

import pytest

import fs_boundary_support as fbs

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows probe only")


def test_probe_report(tmp_path):
    import importlib
    import ntpath
    import pathlib

    nt = importlib.import_module("nt")
    lines = [f"PROBE python={sys.version}"]
    lines.append(
        "ntpath._getfinalpathname is nt._getfinalpathname: "
        f"{getattr(ntpath, '_getfinalpathname', None) is getattr(nt, '_getfinalpathname', None)}"
    )
    lines.append(f"RUNTIME_PATH_FAST_PATH_NAMES={sorted(fbs.RUNTIME_PATH_FAST_PATH_NAMES)}")
    target = tmp_path / "proj"
    target.mkdir()

    calls = []
    real = {}
    for mod, name in ((os, "stat"), (os, "lstat"), (nt, "_getfinalpathname"),
                      (ntpath, "_getfinalpathname"), (nt, "stat"), (nt, "lstat")):
        if hasattr(mod, name):
            fn = getattr(mod, name)
            real[(mod, name)] = fn

            def wrap(*a, _fn=fn, _label=f"{mod.__name__}.{name}", **k):
                calls.append((_label, str(a[0]) if a else None))
                return _fn(*a, **k)

            setattr(mod, name, wrap)
    try:
        Path(target).resolve()
    finally:
        for (mod, name), fn in real.items():
            setattr(mod, name, fn)
    lines.append(f"resolve() raw calls: {calls}")

    # Does a fault in ntpath's own binding escape Path.resolve()?
    for label, mod in (("ntpath", ntpath), ("nt", nt)):
        orig = getattr(mod, "_getfinalpathname")

        def boom(*a, **k):
            raise OSError(errno.EACCES, os.strerror(errno.EACCES))

        setattr(mod, "_getfinalpathname", boom)
        try:
            Path(target).resolve()
            lines.append(f"fault in {label}._getfinalpathname: swallowed")
        except Exception as exc:  # noqa: BLE001 - probe records any outcome
            lines.append(f"fault in {label}._getfinalpathname: escaped {type(exc).__name__}: {exc}")
        finally:
            setattr(mod, "_getfinalpathname", orig)
    pytest.fail("\n".join(lines))
