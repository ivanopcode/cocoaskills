"""Fail-closed Homebrew tap formula bump for published CocoaSkills releases."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

CONTRACT_PATH = Path(__file__).with_name("release_contract.py")


def _load_contract() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_contract", CONTRACT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load release contract: {CONTRACT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


contract = _load_contract()

URL_RE = re.compile(r'^(?P<indent>\s*)url "(?P<url>[^"]+)"$', re.MULTILINE)
SHA_RE = re.compile(r'^(?P<indent>\s*)sha256 "(?P<digest>[0-9a-f]{64})"$', re.MULTILINE)


def release_url(repo: str, tag: str, version: str) -> str:
    sdist = f"{contract.PROJECT}-{version}.tar.gz"
    return f"https://github.com/{repo}/releases/download/{tag}/{sdist}"


def rewrite_formula(source: str, *, url: str, digest: str) -> str:
    """Replace exactly one url and one sha256 row, or fail closed."""
    if len(URL_RE.findall(source)) != 1:
        raise contract.ContractError("formula must contain exactly one url row")
    if len(SHA_RE.findall(source)) != 1:
        raise contract.ContractError("formula must contain exactly one sha256 row")
    source = URL_RE.sub(lambda m: f'{m.group("indent")}url "{url}"', source, count=1)
    return SHA_RE.sub(
        lambda m: f'{m.group("indent")}sha256 "{digest}"', source, count=1
    )


def bump(
    *,
    formula_path: Path,
    checksums_path: Path,
    tag: str,
    repo: str,
) -> dict[str, str]:
    route = contract.parse_release_tag(tag)
    if route.prerelease:
        raise contract.ContractError(
            f"refusing to bump the Homebrew tap for prerelease {route.tag}"
        )
    if not formula_path.is_file():
        raise contract.ContractError(f"formula does not exist: {formula_path}")

    digests = contract.parse_checksums(checksums_path.read_bytes())
    sdist = f"{contract.PROJECT}-{route.version}.tar.gz"
    digest = digests.get(sdist)
    if digest is None:
        raise contract.ContractError(f"SHA256SUMS has no row for {sdist}")

    url = release_url(repo, route.tag, route.version)
    source = formula_path.read_text(encoding="utf-8")
    updated = rewrite_formula(source, url=url, digest=digest)
    changed = updated != source
    if changed:
        formula_path.write_text(updated, encoding="utf-8")

    return {
        "changed": str(changed).lower(),
        "digest": digest,
        "tag": route.tag,
        "url": url,
        "version": route.version,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--formula", required=True)
    parser.add_argument("--checksums", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)

    try:
        values = bump(
            formula_path=Path(args.formula),
            checksums_path=Path(args.checksums),
            tag=args.tag,
            repo=args.repo,
        )
    except contract.ContractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    contract._write_outputs(values)
    print(json.dumps(values, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
