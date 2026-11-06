from __future__ import annotations

import ast
from pathlib import Path

import pytest


# Deliberate request contracts, not unexamined large-file reader exemptions.
# Keep these tied to the actual site; removing a site must remove its exception.
_ALLOWED = {
    ("csk/audit/pipeline.py", "_build_request", 'remaining + 1'): "Aggregate audit input budget; test_audit_input_bounds asserts remaining+1 deliberately.",
    ("csk/audit/pipeline.py", "audit_plans", 'max(0, remaining) + 1'): "Same aggregate audit budget while constructing the private snapshot; preserve its contract.",
    ("csk/registry_token.py", "registry_token", 'MAX_TOKEN_FILE_BYTES + 1'): "64 KiB+1 token cap; existing negative control asserts this exact small request.",
    ("csk/builds/toolchain.py", "_check_goroot_release", 'MAX_VERSION_OUTPUT + 1'): "4097-byte VERSION probe, below the 64 KiB allocation bound.",
    ("csk/builds/go_v1.py", "_resolve_file_identity", 'min(remaining, 1024 * 1024)'): "Existing 1 MiB streaming digest chunks, clamped by remaining actual file size.",
    ("csk/builds/go_v1.py", "_verify_artifact", 'min(remaining, 1024 * 1024)'): "Existing 1 MiB streaming digest chunks, clamped by remaining actual artifact size.",
    ("csk/mcp_launchers.py", "_digest_stable", '1024 * 1024'): "Existing fixed 1 MiB streaming hash chunks, independent of an admission budget.",
    ("csk/transactions.py", "_digest_file", '1024 * 1024'): "Existing fixed 1 MiB streaming hash chunks, independent of an admission budget.",
    ("csk/transactions.py", "_entry_content_digest", '1024 * 1024'): "Existing fixed 1 MiB streaming hash chunks, independent of an admission budget.",
}


def _constant(node: ast.AST) -> int | None:
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.BinOp):
        left, right = _constant(node.left), _constant(node.right)
        if left is not None and right is not None:
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Mult):
                return left * right
    return None


def _budget_request(node: ast.AST, aliases: dict[str, list[ast.AST]], seen: frozenset[str] = frozenset()) -> bool:
    constant = _constant(node)
    if constant is not None:
        return constant > 64 * 1024
    if isinstance(node, ast.Name) and node.id not in seen:
        return any(_budget_request(value, aliases, seen | {node.id}) for value in aliases.get(node.id, []))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "min":
        # A positive constant clamp bounds every path, including budget+1.
        if any(0 < value <= 64 * 1024 for arg in node.args if (value := _constant(arg)) is not None):
            return False
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        if _constant(node.left) == 1 or _constant(node.right) == 1:
            return True
    if isinstance(node, ast.BinOp):
        return _budget_request(node.left, aliases, seen) or _budget_request(node.right, aliases, seen)
    if isinstance(node, ast.Call):
        return any(_budget_request(arg, aliases, seen) for arg in node.args)
    return False


def _scope_nodes(scope: ast.AST):
    for child in ast.iter_child_nodes(scope):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield child
        yield from _scope_nodes(child)


def _inventory(root: Path) -> set[tuple[str, str, str]]:
    found = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        scopes = [tree, *(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))]
        for scope in scopes:
            nodes = list(_scope_nodes(scope))
            aliases: dict[str, list[ast.AST]] = {}
            for node in nodes:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            aliases.setdefault(target.id, []).append(node.value)
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                    aliases.setdefault(node.target.id, []).append(node.value)
            for node in nodes:
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "read":
                    continue
                arguments = node.args or [kw.value for kw in node.keywords if kw.arg == "size"]
                if arguments and _budget_request(arguments[0], aliases):
                    found.add((path.relative_to(root).as_posix(), getattr(scope, "name", "<module>"), ast.unparse(arguments[0])))
    return found


def _assert_inventory(root: Path, allowed: dict[tuple[str, str, str], str]) -> None:
    found = _inventory(root)
    assert not found - allowed.keys(), f"budget-sized read request: {sorted(found - allowed.keys())}"
    assert found == allowed.keys(), f"stale read exception: {sorted(allowed.keys() - found)}"
    assert all(allowed.values()), "every exception needs a reason"


# Scan all production modules, including future files, for budget+1 requests,
# local aliases, and large literals. This is a syntax guard, not general dataflow
# analysis; runtime allocation/growth controls cover the converted readers.
def test_source_read_allocation_inventory() -> None:
    _assert_inventory(Path(__file__).resolve().parents[1] / "src", _ALLOWED)


# A newly named reader must trip the scanner; nearby clamped/small reads pass.
# Neither the plant nor the positive control depends on existing reader names.
@pytest.mark.parametrize("expression", [
    "return stream.read(budget + 1)",
    "return stream.read(1 + budget)",
    "amount = budget + 1\n    return stream.read(amount)",
    "return stream.read(16 * 1024 * 1024)",
    "return stream.read(size=budget + 1)",
])
def test_read_inventory_catches_planted_new_reader(tmp_path: Path, expression: str) -> None:
    path = tmp_path / "new_reader.py"
    path.write_text("def unforeseen(stream, budget):\n    " + expression + "\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="budget-sized read request.*new_reader.py.*unforeseen"):
        _assert_inventory(tmp_path, {})
    path.write_text("def unforeseen(stream, budget):\n    stream.read(4096)\n    return stream.read(min(64 * 1024, budget + 1))\n", encoding="utf-8")
    _assert_inventory(tmp_path, {})
