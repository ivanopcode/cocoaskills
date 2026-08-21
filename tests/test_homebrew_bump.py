from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "bump_homebrew_formula.py"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
SMOKE_WORKFLOW = ROOT / ".github" / "workflows" / "distribution-smoke.yml"

FORMULA = """class Cocoaskills < Formula
  include Language::Python::Virtualenv

  desc "Local skill manager for AI agent skills"
  homepage "https://github.com/ivanopcode/cocoaskills"
  url "https://github.com/ivanopcode/cocoaskills/releases/download/v0.12.0/cocoaskills-0.12.0.tar.gz"
  sha256 "e70e904e35730a5ea2710d2dba35fdb9024acf8fc54616eaa5c1fc7ed92a7541"
  license "Apache-2.0"

  depends_on "python@3.13"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/csk --version")
  end
end
"""

SDIST_DIGEST = "7b780594d16c67a9ff886d1ebe0d192b5069c7a4c67cc7c1a118c227c33f5f9e"
WHEEL_DIGEST = "a383f32c56f5f5eae0873e0c3a8d7851f48d7181a6276fdd016f9acaa3afa8ae"


def _load_bumper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bump_homebrew_formula", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bumper = _load_bumper()


def _checksums(version: str = "0.13.0") -> str:
    return (
        f"{WHEEL_DIGEST}  cocoaskills-{version}-py3-none-any.whl\n"
        f"{SDIST_DIGEST}  cocoaskills-{version}.tar.gz\n"
    )


def _fixture(tmp_path: Path, *, formula: str = FORMULA, version: str = "0.13.0"):
    formula_path = tmp_path / "Formula" / "cocoaskills.rb"
    formula_path.parent.mkdir(parents=True)
    formula_path.write_text(formula, encoding="utf-8")
    checksums_path = tmp_path / "SHA256SUMS"
    checksums_path.write_text(_checksums(version), encoding="utf-8")
    return formula_path, checksums_path


def test_bump_rewrites_url_and_digest_from_verified_checksums(tmp_path: Path) -> None:
    formula_path, checksums_path = _fixture(tmp_path)

    result = bumper.bump(
        formula_path=formula_path,
        checksums_path=checksums_path,
        tag="v0.13.0",
        repo="ivanopcode/cocoaskills",
    )

    assert result["changed"] == "true"
    assert result["version"] == "0.13.0"
    assert result["digest"] == SDIST_DIGEST
    updated = formula_path.read_text(encoding="utf-8")
    assert (
        '  url "https://github.com/ivanopcode/cocoaskills/releases/download/'
        'v0.13.0/cocoaskills-0.13.0.tar.gz"' in updated
    )
    assert f'  sha256 "{SDIST_DIGEST}"' in updated
    assert "0.12.0" not in updated
    # Everything outside the two release rows is preserved verbatim.
    assert updated.splitlines()[0] == FORMULA.splitlines()[0]
    assert updated.endswith("end\n")


def test_bump_takes_the_sdist_digest_not_the_wheel(tmp_path: Path) -> None:
    formula_path, checksums_path = _fixture(tmp_path)

    bumper.bump(
        formula_path=formula_path,
        checksums_path=checksums_path,
        tag="v0.13.0",
        repo="ivanopcode/cocoaskills",
    )

    assert WHEEL_DIGEST not in formula_path.read_text(encoding="utf-8")


def test_bump_is_idempotent(tmp_path: Path) -> None:
    formula_path, checksums_path = _fixture(tmp_path)
    kwargs = dict(
        formula_path=formula_path,
        checksums_path=checksums_path,
        tag="v0.13.0",
        repo="ivanopcode/cocoaskills",
    )

    assert bumper.bump(**kwargs)["changed"] == "true"
    first = formula_path.read_text(encoding="utf-8")
    assert bumper.bump(**kwargs)["changed"] == "false"
    assert formula_path.read_text(encoding="utf-8") == first


@pytest.mark.parametrize("tag", ["v0.13.0-rc.4", "v1.2.3-alpha.2", "v1.2.3-beta.7"])
def test_bump_refuses_prereleases(tmp_path: Path, tag: str) -> None:
    formula_path, checksums_path = _fixture(tmp_path)

    with pytest.raises(bumper.contract.ContractError, match="prerelease"):
        bumper.bump(
            formula_path=formula_path,
            checksums_path=checksums_path,
            tag=tag,
            repo="ivanopcode/cocoaskills",
        )

    assert formula_path.read_text(encoding="utf-8") == FORMULA


def test_bump_fails_when_checksums_lack_the_sdist(tmp_path: Path) -> None:
    formula_path, checksums_path = _fixture(tmp_path, version="0.12.9")

    with pytest.raises(bumper.contract.ContractError, match="no row for"):
        bumper.bump(
            formula_path=formula_path,
            checksums_path=checksums_path,
            tag="v0.13.0",
            repo="ivanopcode/cocoaskills",
        )

    assert formula_path.read_text(encoding="utf-8") == FORMULA


def test_bump_fails_when_formula_is_missing(tmp_path: Path) -> None:
    _, checksums_path = _fixture(tmp_path)

    with pytest.raises(bumper.contract.ContractError, match="does not exist"):
        bumper.bump(
            formula_path=tmp_path / "Formula" / "absent.rb",
            checksums_path=checksums_path,
            tag="v0.13.0",
            repo="ivanopcode/cocoaskills",
        )


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda text: text.replace('  url "', '  url "', 1) + '  url "x"\n', "url row"),
        (
            lambda text: text.replace("  license", f'  sha256 "{SDIST_DIGEST}"\n  license'),
            "sha256 row",
        ),
    ],
)
def test_rewrite_fails_closed_on_ambiguous_formulas(mutate, match: str) -> None:
    with pytest.raises(bumper.contract.ContractError, match=match):
        bumper.rewrite_formula(
            mutate(FORMULA),
            url="https://example.invalid/cocoaskills-0.13.0.tar.gz",
            digest=SDIST_DIGEST,
        )


def test_release_workflow_bumps_the_tap_on_stable_tags_only() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^  bump-homebrew-tap:\n(.*?)(?=^  [a-z][a-z0-9_-]*:|\Z)", workflow
    )
    assert match is not None, "release.yml is missing the bump-homebrew-tap job"
    job = match.group(0)

    assert "needs: [build, publish-pypi]" in job
    assert "if: needs.build.outputs.prerelease == 'false'" in job
    # The digest comes from the artifact the release contract already verified.
    assert "name: checksums" in job
    assert "--checksums dist/SHA256SUMS" in job
    assert "secrets.HOMEBREW_TAP_TOKEN" in job
    assert "if: steps.formula.outputs.changed == 'true'" in job


def test_homebrew_smoke_lane_waits_for_the_bump() -> None:
    workflow = SMOKE_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^  homebrew-smoke:\n(.*?)(?=^  [a-z][a-z0-9_-]*:|\Z)", workflow
    )
    assert match is not None, "distribution-smoke.yml is missing homebrew-smoke"
    job = match.group(0)

    # `release: published` fires before the Release workflow bumps the tap, so
    # the lane must not run on it; workflow_run fires after the whole run.
    assert "github.event_name != 'release'" in job
    assert "needs.resolve-version.outputs.prerelease == 'false'" in job
    assert "github.event_name != 'workflow_dispatch' || inputs.include_homebrew" in job
    # The bump is no longer a manual precondition, so the lane is opt-out.
    assert "Enable after the tap formula is bumped" not in workflow
