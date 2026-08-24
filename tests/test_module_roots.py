"""Schema-8 first-party module roots: declaration, bijection, and scan surface.

The cases mirror Protocol Core section 4.2.3 and the manager profile's fixed
order: declaration and containment before the fixed ``go list``, directive form
and bijection after it returns and before ``go build``.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from csk.builds import go_v1, module_roots


BUILD_ROOT = "tools/cli"


# --- effective replace set -------------------------------------------------


def test_only_a_hash_space_line_with_the_exact_arrow_is_an_annotation() -> None:
    text = "\n".join(
        [
            "# example.com/board v0.0.0",
            "## explicit; go 1.23",
            "example.com/board",
            "#example.com/tight => ../../pkg/tight",
            "# example.com/nospace =>../../pkg/nospace",
            "# example.com/board => ../../pkg/board",
        ]
    )

    assert module_roots.parse_effective_replacements(text) == (
        module_roots.Replacement("example.com/board", "../../pkg/board"),
    )


def test_a_selection_annotation_does_not_add_a_second_directive() -> None:
    text = "\n".join(
        [
            "# example.com/board v0.0.0 => ../../pkg/board",
            "## explicit; go 1.23",
            "example.com/board",
            "# example.com/board => ../../pkg/board",
        ]
    )

    assert module_roots.parse_effective_replacements(text) == (
        module_roots.Replacement("example.com/board", "../../pkg/board"),
    )


def test_a_versioned_left_directive_without_a_matching_unversioned_one_is_rejected() -> None:
    with pytest.raises(module_roots.ModuleRootError) as raised:
        module_roots.parse_effective_replacements("# example.com/board v1.2.3 => ../../pkg/board\n")

    assert raised.value.code == module_roots.CODE_DIRECTIVE_FORM_UNSUPPORTED


def test_a_selection_annotation_with_a_different_target_is_rejected() -> None:
    text = "\n".join(
        [
            "# example.com/board v0.0.0 => ../../pkg/other",
            "# example.com/board => ../../pkg/board",
        ]
    )

    with pytest.raises(module_roots.ModuleRootError) as raised:
        module_roots.parse_effective_replacements(text)

    assert raised.value.code == module_roots.CODE_DIRECTIVE_FORM_UNSUPPORTED


def test_a_module_to_module_redirect_is_rejected() -> None:
    with pytest.raises(module_roots.ModuleRootError) as raised:
        module_roots.parse_effective_replacements("# example.com/board => example.com/fork v1.2.3\n")

    assert raised.value.code == module_roots.CODE_DIRECTIVE_FORM_UNSUPPORTED


@pytest.mark.parametrize(
    "line",
    [
        "# a b c => ../../pkg/board",
        "# example.com/board => a b c",
        "#  => ../../pkg/board",
        "# example.com/board => ",
    ],
)
def test_an_unreadable_annotation_shape_is_rejected(line: str) -> None:
    with pytest.raises(module_roots.ModuleRootError) as raised:
        module_roots.parse_effective_replacements(line + "\n")

    assert raised.value.code == module_roots.CODE_DIRECTIVE_FORM_UNSUPPORTED


# --- bijection -------------------------------------------------------------


def _replacements(*pairs: tuple[str, str]) -> tuple[module_roots.Replacement, ...]:
    return tuple(module_roots.Replacement(path, target) for path, target in pairs)


def test_the_bijection_admits_exactly_the_declared_directories() -> None:
    admitted = module_roots.resolve_bijection(
        ("pkg/board", "pkg/remoteconfig"),
        _replacements(
            ("example.com/board", "../../pkg/board"),
            ("example.com/remoteconfig", "../../pkg/remoteconfig"),
        ),
        build_root=BUILD_ROOT,
    )

    assert admitted == frozenset({"example.com/board", "example.com/remoteconfig"})


def test_an_empty_declaration_requires_an_empty_effective_replace_set() -> None:
    assert module_roots.resolve_bijection((), (), build_root=BUILD_ROOT) == frozenset()


def test_a_replacement_escaping_the_snapshot_is_undeclared() -> None:
    with pytest.raises(module_roots.ModuleRootError) as raised:
        module_roots.resolve_bijection(
            (),
            _replacements(("example.com/escape", "../../../outside")),
            build_root=BUILD_ROOT,
        )

    assert raised.value.code == module_roots.CODE_DIRECTIVE_UNDECLARED


def test_a_replacement_naming_an_undeclared_directory_is_rejected() -> None:
    with pytest.raises(module_roots.ModuleRootError) as raised:
        module_roots.resolve_bijection(
            (),
            _replacements(("example.com/extra", "../../pkg/extra")),
            build_root=BUILD_ROOT,
        )

    assert raised.value.code == module_roots.CODE_DIRECTIVE_UNDECLARED


def test_a_declaration_named_by_no_replacement_is_rejected() -> None:
    with pytest.raises(module_roots.ModuleRootError) as raised:
        module_roots.resolve_bijection(("pkg/board",), (), build_root=BUILD_ROOT)

    assert raised.value.code == module_roots.CODE_DECLARATION_UNUSED


def test_two_replacements_may_not_name_one_declaration() -> None:
    with pytest.raises(module_roots.ModuleRootError) as raised:
        module_roots.resolve_bijection(
            ("pkg/board",),
            _replacements(
                ("example.com/board", "../../pkg/board"),
                ("example.com/mirror", "../../pkg/board"),
            ),
            build_root=BUILD_ROOT,
        )

    assert raised.value.code == module_roots.CODE_DIRECTIVE_UNDECLARED


@pytest.mark.parametrize("target", ["/pkg/board", "..\\..\\pkg\\board", ""])
def test_a_non_relative_replacement_target_never_resolves(target: str) -> None:
    with pytest.raises(module_roots.ModuleRootError) as raised:
        module_roots.resolve_bijection(
            ("pkg/board",),
            _replacements(("example.com/board", target)),
            build_root=BUILD_ROOT,
        )

    assert raised.value.code == module_roots.CODE_DIRECTIVE_UNDECLARED


# --- declaration and containment -------------------------------------------


def _snapshot(root: Path, *, modules: tuple[str, ...] = ("pkg/board",)) -> Path:
    (root / BUILD_ROOT).mkdir(parents=True, exist_ok=True)
    (root / BUILD_ROOT / "go.mod").write_text("module example.com/cli\n\ngo 1.23\n", encoding="utf-8")
    (root / "scripts").mkdir(exist_ok=True)
    for relative in modules:
        directory = root / Path(relative)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "go.mod").write_text("module example.com/x\n\ngo 1.23\n", encoding="utf-8")
    return root


def _declare(root: Path, modules: tuple[str, ...], *, runtime_roots: tuple[str, ...] = ("scripts",)) -> None:
    module_roots.validate_declaration(
        root,
        modules,
        build_root=BUILD_ROOT,
        build_roots=(BUILD_ROOT,),
        runtime_roots=runtime_roots,
        label="commands.cli",
    )


def test_a_valid_declaration_is_accepted(tmp_path: Path) -> None:
    _snapshot(tmp_path, modules=("pkg/board", "pkg/remoteconfig"))

    _declare(tmp_path, ("pkg/board", "pkg/remoteconfig"))


def test_a_missing_module_directory_is_declaration_invalid(tmp_path: Path) -> None:
    _snapshot(tmp_path, modules=())

    with pytest.raises(module_roots.ModuleRootError) as raised:
        _declare(tmp_path, ("pkg/board",))

    assert raised.value.code == module_roots.CODE_DECLARATION_INVALID


def test_a_module_directory_without_go_mod_is_declaration_invalid(tmp_path: Path) -> None:
    _snapshot(tmp_path, modules=())
    (tmp_path / "pkg" / "board").mkdir(parents=True)

    with pytest.raises(module_roots.ModuleRootError) as raised:
        _declare(tmp_path, ("pkg/board",))

    assert raised.value.code == module_roots.CODE_DECLARATION_INVALID


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_linked_module_directory_is_declaration_invalid(tmp_path: Path) -> None:
    _snapshot(tmp_path, modules=("pkg/real",))
    (tmp_path / "pkg" / "board").symlink_to(tmp_path / "pkg" / "real", target_is_directory=True)

    with pytest.raises(module_roots.ModuleRootError) as raised:
        _declare(tmp_path, ("pkg/board",))

    assert raised.value.code == module_roots.CODE_DECLARATION_INVALID


@pytest.mark.parametrize("module", [".", "../pkg/board", "pkg\\board", "pkg//board"])
def test_a_non_portable_declaration_is_declaration_invalid(tmp_path: Path, module: str) -> None:
    _snapshot(tmp_path)

    with pytest.raises(module_roots.ModuleRootError) as raised:
        _declare(tmp_path, (module,))

    assert raised.value.code == module_roots.CODE_DECLARATION_INVALID


def test_a_duplicate_declaration_is_declaration_invalid(tmp_path: Path) -> None:
    _snapshot(tmp_path)

    with pytest.raises(module_roots.ModuleRootError) as raised:
        _declare(tmp_path, ("pkg/board", "pkg/board"))

    assert raised.value.code == module_roots.CODE_DECLARATION_INVALID


def test_nested_declarations_are_containment_invalid(tmp_path: Path) -> None:
    _snapshot(tmp_path, modules=("pkg/board", "pkg/board/codec"))

    with pytest.raises(module_roots.ModuleRootError) as raised:
        _declare(tmp_path, ("pkg/board", "pkg/board/codec"))

    assert raised.value.code == module_roots.CODE_CONTAINMENT_INVALID


def test_a_declaration_below_the_build_root_is_containment_invalid(tmp_path: Path) -> None:
    _snapshot(tmp_path, modules=(f"{BUILD_ROOT}/pkg/lib",))

    with pytest.raises(module_roots.ModuleRootError) as raised:
        _declare(tmp_path, (f"{BUILD_ROOT}/pkg/lib",))

    assert raised.value.code == module_roots.CODE_CONTAINMENT_INVALID


def test_a_declaration_below_a_runtime_root_is_containment_invalid(tmp_path: Path) -> None:
    _snapshot(tmp_path)

    with pytest.raises(module_roots.ModuleRootError) as raised:
        _declare(tmp_path, ("pkg/board",), runtime_roots=("pkg",))

    assert raised.value.code == module_roots.CODE_CONTAINMENT_INVALID


def test_platform_case_folding_collisions_are_containment_invalid(tmp_path: Path) -> None:
    _snapshot(tmp_path, modules=("pkg/board",))
    upper = tmp_path / "pkg" / "Board"
    upper.mkdir(parents=True, exist_ok=True)
    (upper / "go.mod").write_text("module example.com/upper\n\ngo 1.23\n", encoding="utf-8")

    with pytest.raises(module_roots.ModuleRootError) as raised:
        _declare(tmp_path, ("pkg/Board", "pkg/board"))

    assert raised.value.code == module_roots.CODE_CONTAINMENT_INVALID


def test_a_missing_modules_txt_yields_an_empty_effective_replace_set(tmp_path: Path) -> None:
    build_root = _snapshot(tmp_path) / BUILD_ROOT

    assert module_roots.read_vendor_modules_text(build_root) == ""


def test_modules_txt_is_read_from_the_build_root_only(tmp_path: Path) -> None:
    build_root = _snapshot(tmp_path) / BUILD_ROOT
    (build_root / "vendor").mkdir()
    (build_root / "vendor" / "modules.txt").write_text(
        "# example.com/board => ../../pkg/board\n", encoding="utf-8"
    )

    assert module_roots.parse_effective_replacements(
        module_roots.read_vendor_modules_text(build_root)
    ) == (module_roots.Replacement("example.com/board", "../../pkg/board"),)


# --- driver wiring ---------------------------------------------------------


def _request(root: Path, modules: tuple[str, ...], **changes: Any) -> go_v1.BuildRequest:
    snapshot = type("Snapshot", (), {"path": root})()
    fields: dict[str, Any] = {
        "toolchain_session": None,
        "source_snapshot": snapshot,
        "command_object": {"type": "build", "driver": "go-v1", "source_dir": BUILD_ROOT},
        "build_root": BUILD_ROOT,
        "source_dir": BUILD_ROOT,
        "command": "cli",
        "modules": modules,
        "build_roots": (BUILD_ROOT,),
        "runtime_roots": ("scripts",),
    }
    fields.update(changes)
    return go_v1.BuildRequest(**fields)


def test_the_driver_rejects_a_bad_declaration_before_go_list(tmp_path: Path) -> None:
    _snapshot(tmp_path, modules=())

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._validate_declared_module_roots(_request(tmp_path, ("pkg/board",)), tmp_path)

    assert raised.value.code == module_roots.CODE_CONTAINMENT_INVALID or (
        raised.value.code == module_roots.CODE_DECLARATION_INVALID
    )


def test_the_driver_resolves_the_bijection_from_modules_txt(tmp_path: Path) -> None:
    build_root = _snapshot(tmp_path) / BUILD_ROOT
    (build_root / "vendor").mkdir()
    (build_root / "vendor" / "modules.txt").write_text(
        "# example.com/board v0.0.0 => ../../pkg/board\n"
        "## explicit; go 1.23\n"
        "example.com/board\n"
        "# example.com/board => ../../pkg/board\n",
        encoding="utf-8",
    )

    admitted = go_v1._resolve_module_root_bijection(_request(tmp_path, ("pkg/board",)), build_root)

    assert admitted == frozenset({"example.com/board"})


def test_the_driver_reports_the_stable_module_root_diagnostic(tmp_path: Path) -> None:
    build_root = _snapshot(tmp_path) / BUILD_ROOT
    (build_root / "vendor").mkdir()
    (build_root / "vendor" / "modules.txt").write_text(
        "# example.com/board => example.com/fork v1.2.3\n", encoding="utf-8"
    )

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._resolve_module_root_bijection(_request(tmp_path, ()), build_root)

    assert raised.value.code == module_roots.CODE_DIRECTIVE_FORM_UNSUPPORTED


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("host.c", "go_native_input_forbidden"),
        ("host.h", "go_native_input_forbidden"),
        ("host.cpp", "go_native_input_forbidden"),
        ("host.swigcxx", "go_native_input_forbidden"),
        ("host.s", "go_assembly_forbidden"),
        ("host.syso", "go_syso_forbidden"),
    ],
)
def test_the_scan_surface_covers_native_inputs_in_a_declared_module(
    tmp_path: Path, name: str, code: str
) -> None:
    _snapshot(tmp_path)
    (tmp_path / "pkg" / "board" / name).write_bytes(b"\x00")

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._scan_declared_module_roots(_request(tmp_path, ("pkg/board",)), tmp_path)

    assert raised.value.code == code


def test_the_scan_surface_covers_cgo_import_dynamic_in_a_declared_module(tmp_path: Path) -> None:
    _snapshot(tmp_path)
    (tmp_path / "pkg" / "board" / "board.go").write_text(
        'package board\n\n//go:cgo_import_dynamic libc_x x "libc.so"\n', encoding="utf-8"
    )

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._scan_declared_module_roots(_request(tmp_path, ("pkg/board",)), tmp_path)

    assert raised.value.code == "go_forbidden_compiler_directive"


def test_the_scan_surface_rejects_a_workspace_inside_a_declared_module(tmp_path: Path) -> None:
    _snapshot(tmp_path)
    (tmp_path / "pkg" / "board" / "go.work").write_text("go 1.23\n", encoding="utf-8")

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._scan_declared_module_roots(_request(tmp_path, ("pkg/board",)), tmp_path)

    assert raised.value.code == "workspace_dependency_forbidden"


def test_the_scan_surface_rejects_a_toolchain_directive_in_a_declared_module(tmp_path: Path) -> None:
    _snapshot(tmp_path)
    (tmp_path / "pkg" / "board" / "go.mod").write_text(
        "module example.com/board\n\ngo 1.23\n\ntoolchain go1.99.0\n", encoding="utf-8"
    )

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._scan_declared_module_roots(_request(tmp_path, ("pkg/board",)), tmp_path)

    assert raised.value.code == "toolchain_switch_forbidden"


def test_the_scan_surface_ignores_the_trees_go_ignores(tmp_path: Path) -> None:
    _snapshot(tmp_path)
    for subtree in ("testdata", "vendor", ".hidden", "_ignored"):
        directory = tmp_path / "pkg" / "board" / subtree
        directory.mkdir()
        (directory / "host.c").write_bytes(b"\x00")
    (tmp_path / "pkg" / "board" / "board.go").write_text("package board\n", encoding="utf-8")

    go_v1._scan_declared_module_roots(_request(tmp_path, ("pkg/board",)), tmp_path)


def test_the_scan_surface_accepts_a_plain_first_party_module(tmp_path: Path) -> None:
    _snapshot(tmp_path)
    (tmp_path / "pkg" / "board" / "board.go").write_text(
        "package board\n\nfunc Name() string { return \"board\" }\n", encoding="utf-8"
    )

    go_v1._scan_declared_module_roots(_request(tmp_path, ("pkg/board",)), tmp_path)


# --- package graph admission ----------------------------------------------


def _encode(packages: list[dict[str, object]]) -> bytes:
    return b"".join(json.dumps(package, separators=(",", ":")).encode() + b"\n" for package in packages)


def _graph(root: Path, *, replace: bool = True) -> list[dict[str, object]]:
    package_dir = root / "cmd"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")
    go_mod = root / "go.mod"
    go_mod.write_text("module example.com/cli\ngo 1.25\n", encoding="utf-8")
    vendored = root / "vendor" / "example.com" / "board"
    vendored.mkdir(parents=True, exist_ok=True)
    (vendored / "board.go").write_text("package board\n", encoding="utf-8")
    (root / "vendor" / "modules.txt").write_text(
        "# example.com/board v0.0.0 => ../../pkg/board\n"
        "## explicit; go 1.25\n"
        "example.com/board\n"
        "# example.com/board => ../../pkg/board\n",
        encoding="utf-8",
    )
    module: dict[str, object] = {"Path": "example.com/board", "Version": "v0.0.0"}
    if replace:
        module["Replace"] = {
            "Path": "../../pkg/board",
            "Dir": str(root.parent.parent / "pkg" / "board"),
            "GoMod": str(root.parent.parent / "pkg" / "board" / "go.mod"),
        }
    return [
        {
            "Dir": str(package_dir),
            "ImportPath": "example.com/cli/cmd",
            "Name": "main",
            "Root": str(root),
            "Module": {
                "Path": "example.com/cli",
                "Main": True,
                "Dir": str(root),
                "GoMod": str(go_mod),
            },
            "GoFiles": ["main.go"],
        },
        {
            "Dir": str(vendored),
            "ImportPath": "example.com/board",
            "Name": "board",
            "DepOnly": True,
            "Module": module,
            "GoFiles": ["board.go"],
        },
    ]


def test_a_replaced_module_is_admitted_only_on_the_bijected_set(tmp_path: Path) -> None:
    root = tmp_path / "snapshot" / "tools" / "cli"
    packages = _graph(root)

    go_v1.validate_package_graph(
        _encode(packages),
        build_root=root,
        source_dir=root / "cmd",
        goroot=tmp_path / "goroot",
        replaced_modules=frozenset({"example.com/board"}),
    )

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.validate_package_graph(
            _encode(packages),
            build_root=root,
            source_dir=root / "cmd",
            goroot=tmp_path / "goroot",
        )

    assert raised.value.code == "vendor_metadata_inconsistent"


def test_an_unreplaced_vendored_module_still_validates_without_the_set(tmp_path: Path) -> None:
    root = tmp_path / "snapshot" / "tools" / "cli"
    packages = _graph(root, replace=False)

    go_v1.validate_package_graph(
        _encode(packages),
        build_root=root,
        source_dir=root / "cmd",
        goroot=tmp_path / "goroot",
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("SFiles", ["board.s"], "go_assembly_forbidden"),
        ("SysoFiles", ["board.syso"], "go_syso_forbidden"),
    ],
)
def test_the_audited_vendor_allowance_is_withheld_from_a_replaced_module(
    tmp_path: Path, field: str, value: list[str], code: str
) -> None:
    root = tmp_path / "snapshot" / "tools" / "cli"
    packages = _graph(root)
    packages[1][field] = value
    for name in value:
        (root / "vendor" / "example.com" / "board" / name).write_bytes(b"\x00")

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.validate_package_graph(
            _encode(packages),
            build_root=root,
            source_dir=root / "cmd",
            goroot=tmp_path / "goroot",
            replaced_modules=frozenset({"example.com/board"}),
        )

    assert raised.value.code == code


def test_go_generate_stays_inert_in_third_party_vendored_code(tmp_path: Path) -> None:
    root = tmp_path / "snapshot" / "tools" / "cli"
    packages = _graph(root, replace=False)
    (root / "vendor" / "example.com" / "board" / "board.go").write_text(
        "package board\n\n//go:generate echo hi\n", encoding="utf-8"
    )

    go_v1.validate_package_graph(
        _encode(packages),
        build_root=root,
        source_dir=root / "cmd",
        goroot=tmp_path / "goroot",
    )


def test_go_generate_is_rejected_in_the_vendor_copy_of_a_replaced_module(tmp_path: Path) -> None:
    root = tmp_path / "snapshot" / "tools" / "cli"
    packages = _graph(root)
    (root / "vendor" / "example.com" / "board" / "board.go").write_text(
        "package board\n\n//go:generate echo hi\n", encoding="utf-8"
    )

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.validate_package_graph(
            _encode(packages),
            build_root=root,
            source_dir=root / "cmd",
            goroot=tmp_path / "goroot",
            replaced_modules=frozenset({"example.com/board"}),
        )

    assert raised.value.code == "go_generator_forbidden"


# --- package command surface ----------------------------------------------


def test_the_closed_command_surface_admits_modules_only_when_declared(tmp_path: Path) -> None:
    root = _snapshot(tmp_path)
    request = _request(
        root,
        ("pkg/board",),
        command_object={
            "type": "build",
            "driver": "go-v1",
            "source_dir": BUILD_ROOT,
            "modules": ["pkg/board"],
        },
    )

    go_v1._validate_package_command_surface(request)


def test_an_undeclared_modules_field_cannot_reach_the_driver(tmp_path: Path) -> None:
    root = _snapshot(tmp_path)
    request = _request(
        root,
        (),
        command_object={
            "type": "build",
            "driver": "go-v1",
            "source_dir": BUILD_ROOT,
            "modules": ["pkg/board"],
        },
    )

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._validate_package_command_surface(request)

    assert raised.value.code == go_v1.CODE_PACKAGE_INFLUENCE_FORBIDDEN


def test_a_modules_field_that_contradicts_the_validated_declaration_is_rejected(tmp_path: Path) -> None:
    root = _snapshot(tmp_path)
    request = _request(
        root,
        ("pkg/board",),
        command_object={
            "type": "build",
            "driver": "go-v1",
            "source_dir": BUILD_ROOT,
            "modules": ["pkg/other"],
        },
    )

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._validate_package_command_surface(request)

    assert raised.value.code == go_v1.CODE_PACKAGE_INFLUENCE_FORBIDDEN


# --- real go list ----------------------------------------------------------


GO = shutil.which(os.environ.get("CSK_GO_V1_GO_EXECUTABLE") or "go")


def _write_real_snapshot(root: Path) -> Path:
    for relative, module_path, body in (
        ("pkg/board", "example.com/board", 'package board\n\nfunc Name() string { return "board" }\n'),
        ("pkg/remoteconfig", "example.com/remoteconfig", 'package remoteconfig\n\nfunc Value() string { return "rc" }\n'),
    ):
        directory = root / Path(relative)
        directory.mkdir(parents=True)
        (directory / "go.mod").write_text(f"module {module_path}\n\ngo 1.23\n", encoding="utf-8")
        (directory / f"{directory.name}.go").write_text(body, encoding="utf-8")
    cli = root / BUILD_ROOT
    cli.mkdir(parents=True)
    (cli / "go.mod").write_text(
        "module example.com/cli\n\n"
        "go 1.23\n\n"
        "require (\n\texample.com/board v0.0.0\n\texample.com/remoteconfig v0.0.0\n)\n\n"
        "replace example.com/board => ../../pkg/board\n\n"
        "replace example.com/remoteconfig => ../../pkg/remoteconfig\n",
        encoding="utf-8",
    )
    (cli / "main.go").write_text(
        "package main\n\n"
        'import (\n\t"fmt"\n\n\t"example.com/board"\n\t"example.com/remoteconfig"\n)\n\n'
        "func main() { fmt.Println(board.Name(), remoteconfig.Value()) }\n",
        encoding="utf-8",
    )
    return cli


@functools.cache
def _goroot() -> Path:
    """Probe GOROOT once and resolve it, the way the manager does.

    ``toolchain`` resolves the trusted GOROOT and then pins that exact string
    into every child environment, so ``go list`` reports each standard package
    with a ``Root`` and a ``Dir`` under the resolved spelling. The test harness
    has to do the same. Passing the unresolved probe instead fails wherever the
    host spelling is a link: a GitHub Windows runner reaches its tool cache
    through ``C:\\hostedtoolcache``, a junction onto ``D:\\hostedtoolcache``, so
    the reported ``Root`` and the resolved one disagree for a reason that has
    nothing to do with module roots.
    """

    assert GO is not None
    completed = subprocess.run(
        [GO, "env", "GOROOT"],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    return Path(completed.stdout.decode().strip()).resolve(strict=True)


def _run_go(cli: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    assert GO is not None
    environment = dict(os.environ)
    environment.update(
        {
            "GOROOT": str(_goroot()),
            "GOFLAGS": "",
            "GOWORK": "off",
            "GOPROXY": "off",
            "CGO_ENABLED": "0",
        }
    )
    return subprocess.run(
        [GO, *arguments],
        cwd=cli,
        capture_output=True,
        check=False,
        env=environment,
    )


@pytest.mark.skipif(GO is None, reason="a native Go executable is not available")
def test_a_real_vendored_module_root_graph_passes_the_whole_fixed_order(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    cli = _write_real_snapshot(snapshot)
    vendored = _run_go(cli, "mod", "vendor")
    if vendored.returncode != 0:
        pytest.skip(f"go mod vendor is unavailable here: {vendored.stderr.decode(errors='replace')}")

    annotations = (cli / "vendor" / "modules.txt").read_text(encoding="utf-8")
    assert "# example.com/board => ../../pkg/board" in annotations
    assert "# example.com/board v0.0.0 => ../../pkg/board" in annotations

    request = _request(snapshot, ("pkg/board", "pkg/remoteconfig"))
    go_v1._validate_declared_module_roots(request, snapshot)
    admitted = go_v1._resolve_module_root_bijection(request, cli)
    assert admitted == frozenset({"example.com/board", "example.com/remoteconfig"})

    listed = _run_go(cli, *go_v1.LIST_ARGUMENTS)
    assert listed.returncode == 0, listed.stderr.decode(errors="replace")

    go_v1.validate_package_graph(
        listed.stdout,
        build_root=cli.resolve(strict=True),
        source_dir=cli.resolve(strict=True),
        goroot=_goroot(),
        replaced_modules=admitted,
    )
    go_v1._scan_declared_module_roots(request, snapshot)


@pytest.mark.skipif(GO is None, reason="a native Go executable is not available")
def test_a_real_replaced_module_graph_is_rejected_without_the_declaration(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    cli = _write_real_snapshot(snapshot)
    vendored = _run_go(cli, "mod", "vendor")
    if vendored.returncode != 0:
        pytest.skip(f"go mod vendor is unavailable here: {vendored.stderr.decode(errors='replace')}")
    listed = _run_go(cli, *go_v1.LIST_ARGUMENTS)
    assert listed.returncode == 0, listed.stderr.decode(errors="replace")

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.validate_package_graph(
            listed.stdout,
            build_root=cli.resolve(strict=True),
            source_dir=cli.resolve(strict=True),
            goroot=_goroot(),
        )

    assert raised.value.code == "vendor_metadata_inconsistent"
