"""Load the CI candidate-consumption ledgers from a test.

The ledgers and their gate live in `.github/` because the workflow steps invoke
the gate as a script. Tests reach it through this module rather than through
`tests/conftest.py`: that conftest is part of the audited protocol surface
pinned by `.research/TASK-260803-2ol7ok_protocol-isolation-classification.json`,
and candidate-lane work must not move those bytes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


CANDIDATE_CONSUMPTION_SCRIPT = (
    Path(__file__).parents[1] / ".github" / "scripts" / "candidate_consumption.py"
)

_module: ModuleType | None = None


def load_candidate_consumption() -> ModuleType:
    """Load the candidate-consumption contract once per interpreter."""
    global _module
    if _module is None:
        spec = importlib.util.spec_from_file_location(
            "candidate_consumption", CANDIDATE_CONSUMPTION_SCRIPT
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _module = module
    return _module
