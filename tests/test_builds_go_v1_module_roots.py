"""Protocol Core 4.2.3 declared module roots at the ``go-v1`` build boundary.

Containment rejects before the fixed ``go list``; directive form and the
bijection reject after it returns and before ``go build``. The two boundaries
are tested apart because the protocol places them apart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from csk.builds import go_v1


def _module(root: Path, relative: str) -> Path:
    directory = root / relative
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "go.mod").write_text(
        f"module example.test/{relative.replace('/', '-')}\ngo 1.25\n",
        encoding="utf-8",
    )
    return directory


def _snapshot(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "snapshot"
    build_root = root / "tools" / "cli"
    (build_root / "cmd" / "tool").mkdir(parents=True)
    (build_root / "go.mod").write_text("module example.test/cli\ngo 1.25\n", encoding="utf-8")
    (build_root / "vendor").mkdir()
    return root, build_root


def _modules_txt(build_root: Path, *lines: str) -> None:
    (build_root / "vendor").mkdir(exist_ok=True)
    (build_root / "vendor" / "modules.txt").write_text(
        "".join(f"{line}\n" for line in lines),
        encoding="utf-8",
    )


def _encode(packages: list[dict[str, object]]) -> bytes:
    return b"".join(
        json.dumps(package, separators=(",", ":")).encode() + b"\n"
        for package in packages
    )


def _root_package(build_root: Path) -> dict[str, object]:
    package_dir = build_root / "cmd" / "tool"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")
    return {
        "Dir": str(package_dir),
        "ImportPath": "example.test/cli/cmd/tool",
        "Name": "main",
        "Root": str(build_root),
        "Module": {
            "Path": "example.test/cli",
            "Main": True,
            "Dir": str(build_root),
            "GoMod": str(build_root / "go.mod"),
        },
        "GoFiles": ["main.go"],
    }


def _replaced_package(
    build_root: Path,
    module_path: str = "example.test/board",
    *,
    files: dict[str, str] | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Model what ``go list -mod=vendor`` reports for a directory-replaced module.

    Go resolves the package out of the vendor tree, reports the ``require``
    version, and carries ``Replace`` with paths it never stats.
    """

    package_dir = build_root / "vendor" / Path(*module_path.split("/"))
    package_dir.mkdir(parents=True, exist_ok=True)
    for name, content in (files or {"board.go": "package board\n"}).items():
        (package_dir / name).write_text(content, encoding="utf-8")
    package: dict[str, object] = {
        "Dir": str(package_dir),
        "ImportPath": module_path,
        "Name": module_path.rsplit("/", 1)[-1],
        "DepOnly": True,
        "Module": {
            "Path": module_path,
            "Version": "v0.0.0",
            "Replace": {
                "Path": "../../pkg/board",
                "Dir": "/nonexistent/pkg/board",
                "GoMod": "/nonexistent/pkg/board/go.mod",
            },
        },
        "GoFiles": sorted(files or {"board.go": ""}),
    }
    package.update(extra or {})
    return package


# --- declaration and containment, before go list --------------------------


def test_declared_module_roots_canonicalize_against_the_snapshot(tmp_path: Path) -> None:
    root, build_root = _snapshot(tmp_path)
    _module(root, "pkg/board")
    _module(root, "pkg/remoteconfig")

    resolved = go_v1._canonical_module_directories(
        root, build_root, ("pkg/board", "pkg/remoteconfig")
    )

    assert resolved == {
        "pkg/board": root / "pkg" / "board",
        "pkg/remoteconfig": root / "pkg" / "remoteconfig",
    }


@pytest.mark.parametrize(
    "declared",
    [
        (".",),
        ("/pkg/board",),
        ("pkg\\board",),
        ("../board",),
        ("pkg/absent",),
        ("pkg/board", "pkg/board"),
    ],
)
def test_unsupported_declarations_reject_with_the_containment_diagnostic(
    tmp_path: Path, declared: tuple[str, ...]
) -> None:
    root, build_root = _snapshot(tmp_path)
    _module(root, "pkg/board")

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._canonical_module_directories(root, build_root, declared)

    assert raised.value.code == go_v1.CODE_MODULE_ROOT_CONTAINMENT_INVALID


def test_a_module_root_without_go_mod_is_not_a_module(tmp_path: Path) -> None:
    root, build_root = _snapshot(tmp_path)
    (root / "pkg" / "plain").mkdir(parents=True)

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._canonical_module_directories(root, build_root, ("pkg/plain",))

    assert raised.value.code == go_v1.CODE_MODULE_ROOT_CONTAINMENT_INVALID
    assert "go.mod" in raised.value.detail


def test_a_module_root_inside_the_build_root_is_rejected(tmp_path: Path) -> None:
    root, build_root = _snapshot(tmp_path)
    _module(root, "tools/cli/pkg/lib")

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._canonical_module_directories(root, build_root, ("tools/cli/pkg/lib",))

    assert raised.value.code == go_v1.CODE_MODULE_ROOT_CONTAINMENT_INVALID


def test_nested_module_roots_are_rejected(tmp_path: Path) -> None:
    root, build_root = _snapshot(tmp_path)
    _module(root, "pkg/board")
    _module(root, "pkg/board/codec")

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._canonical_module_directories(
            root, build_root, ("pkg/board", "pkg/board/codec")
        )

    assert raised.value.code == go_v1.CODE_MODULE_ROOT_CONTAINMENT_INVALID


def test_declarations_colliding_only_under_a_platform_folding_are_rejected(
    tmp_path: Path,
) -> None:
    root, build_root = _snapshot(tmp_path)
    _module(root, "pkg/board")
    if not (root / "pkg" / "Board" / "go.mod").is_file():
        _module(root, "pkg/Board")

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._canonical_module_directories(
            root, build_root, ("pkg/Board", "pkg/board")
        )

    assert raised.value.code == go_v1.CODE_MODULE_ROOT_CONTAINMENT_INVALID


# --- scan surface over the declared directory ------------------------------


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("trampoline.syso", "go_syso_forbidden"),
        ("bridge.c", "go_native_input_forbidden"),
        ("bridge.h", "go_native_input_forbidden"),
        ("bridge.cpp", "go_native_input_forbidden"),
        ("bridge.m", "go_native_input_forbidden"),
        ("bridge.f", "go_native_input_forbidden"),
        ("bridge.swig", "go_native_input_forbidden"),
        ("asm_arm64.s", "go_assembly_forbidden"),
        ("asm_arm64.S", "go_assembly_forbidden"),
    ],
)
def test_the_declared_directory_carries_no_non_go_input(
    tmp_path: Path, name: str, code: str
) -> None:
    root, build_root = _snapshot(tmp_path)
    module = _module(root, "pkg/board")
    (module / name).write_text("x\n", encoding="utf-8")
    declared = go_v1._canonical_module_directories(root, build_root, ("pkg/board",))

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._validate_declared_module_inputs(declared)

    assert raised.value.code == code


@pytest.mark.parametrize(
    ("directive", "code"),
    [
        ("//go:cgo_import_dynamic x", "go_forbidden_compiler_directive"),
        ("//go:generate echo", "go_generator_forbidden"),
    ],
)
def test_the_declared_directory_keeps_the_first_party_directive_profile(
    tmp_path: Path, directive: str, code: str
) -> None:
    root, build_root = _snapshot(tmp_path)
    module = _module(root, "pkg/board")
    (module / "board.go").write_text(f"package board\n{directive}\n", encoding="utf-8")
    declared = go_v1._canonical_module_directories(root, build_root, ("pkg/board",))

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._validate_declared_module_inputs(declared)

    assert raised.value.code == code


def test_a_nested_vendor_tree_of_a_declared_module_takes_no_part(
    tmp_path: Path,
) -> None:
    """4.2.3 says that tree is not a dependency source under ``-mod=vendor``."""

    root, build_root = _snapshot(tmp_path)
    module = _module(root, "pkg/board")
    (module / "board.go").write_text("package board\n", encoding="utf-8")
    nested = module / "vendor" / "example.test" / "third"
    nested.mkdir(parents=True)
    (nested / "asm.s").write_text("TEXT ·x(SB),0,$0\n", encoding="utf-8")
    declared = go_v1._canonical_module_directories(root, build_root, ("pkg/board",))

    go_v1._validate_declared_module_inputs(declared)


# --- effective replace set, after go list ---------------------------------


def test_the_effective_replace_set_reads_only_one_token_left_annotations(
    tmp_path: Path,
) -> None:
    _root, build_root = _snapshot(tmp_path)
    _modules_txt(
        build_root,
        "# example.test/board v0.0.0 => ../../pkg/board",
        "## explicit; go 1.25",
        "example.test/board",
        "# example.test/board => ../../pkg/board",
    )

    assert go_v1._effective_replacement_directives(build_root) == (
        ("example.test/board", "../../pkg/board"),
    )


def test_an_absent_vendor_manifest_is_an_empty_replace_set(tmp_path: Path) -> None:
    _root, build_root = _snapshot(tmp_path)

    assert go_v1._effective_replacement_directives(build_root) == ()


@pytest.mark.parametrize(
    "annotation",
    [
        "# example.test/board v1.2.3 => ../../pkg/board",
        "# example.test/board => example.test/fork v1.2.3",
        "# example.test/board a b => ../../pkg/board",
    ],
)
def test_unsupported_directive_forms_are_rejected(
    tmp_path: Path, annotation: str
) -> None:
    _root, build_root = _snapshot(tmp_path)
    _modules_txt(build_root, annotation)

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._effective_replacement_directives(build_root)

    assert raised.value.code == go_v1.CODE_MODULE_ROOT_DIRECTIVE_FORM_UNSUPPORTED


def test_a_line_that_is_not_a_replacement_annotation_is_ignored(
    tmp_path: Path,
) -> None:
    _root, build_root = _snapshot(tmp_path)
    _modules_txt(
        build_root,
        "# example.test/dep v1.4.0",
        "## explicit; go 1.25",
        "example.test/dep",
        "#example.test/board => ../../pkg/board",
        "example.test/other => ../../pkg/other",
    )

    assert go_v1._effective_replacement_directives(build_root) == ()


# --- bijection -------------------------------------------------------------


def test_the_bijection_admits_exactly_the_declared_directories(tmp_path: Path) -> None:
    root, build_root = _snapshot(tmp_path)
    _module(root, "pkg/board")
    _module(root, "pkg/remoteconfig")
    declared = go_v1._canonical_module_directories(
        root, build_root, ("pkg/board", "pkg/remoteconfig")
    )

    admitted = go_v1._bijected_module_roots(
        (
            ("example.test/board", "../../pkg/board"),
            ("example.test/remoteconfig", "../../pkg/remoteconfig"),
        ),
        build_root,
        declared,
    )

    assert admitted == frozenset({"example.test/board", "example.test/remoteconfig"})


@pytest.mark.parametrize(
    "replacement",
    ["../../pkg/extra", "../../../outside", "/abs/pkg/board", "example.test/fork"],
)
def test_a_replacement_naming_no_declaration_is_undeclared(
    tmp_path: Path, replacement: str
) -> None:
    root, build_root = _snapshot(tmp_path)
    _module(root, "pkg/board")
    declared = go_v1._canonical_module_directories(root, build_root, ("pkg/board",))

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._bijected_module_roots(
            (("example.test/other", replacement),), build_root, declared
        )

    assert raised.value.code == go_v1.CODE_MODULE_ROOT_DIRECTIVE_UNDECLARED


def test_two_replacements_may_not_name_one_declaration(tmp_path: Path) -> None:
    root, build_root = _snapshot(tmp_path)
    _module(root, "pkg/board")
    declared = go_v1._canonical_module_directories(root, build_root, ("pkg/board",))

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._bijected_module_roots(
            (
                ("example.test/board", "../../pkg/board"),
                ("example.test/alias", "../../pkg/board"),
            ),
            build_root,
            declared,
        )

    assert raised.value.code == go_v1.CODE_MODULE_ROOT_DIRECTIVE_UNDECLARED


def test_a_declaration_named_by_no_replacement_is_unused(tmp_path: Path) -> None:
    root, build_root = _snapshot(tmp_path)
    _module(root, "pkg/board")
    declared = go_v1._canonical_module_directories(root, build_root, ("pkg/board",))

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._bijected_module_roots((), build_root, declared)

    assert raised.value.code == go_v1.CODE_MODULE_ROOT_DECLARATION_UNUSED


def test_an_empty_declaration_requires_an_empty_replace_set(tmp_path: Path) -> None:
    _root, build_root = _snapshot(tmp_path)

    assert go_v1._bijected_module_roots((), build_root, {}) == frozenset()

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._bijected_module_roots(
            (("example.test/board", "../../pkg/board"),), build_root, {}
        )

    assert raised.value.code == go_v1.CODE_MODULE_ROOT_DIRECTIVE_UNDECLARED


# --- the package graph ------------------------------------------------------


def test_a_bijected_replaced_module_is_admitted_in_the_package_graph(
    tmp_path: Path,
) -> None:
    root, build_root = _snapshot(tmp_path)
    _module(root, "pkg/board")
    _modules_txt(build_root, "# example.test/board => ../../pkg/board")
    declared = go_v1._canonical_module_directories(root, build_root, ("pkg/board",))

    go_v1.validate_package_graph(
        _encode([_root_package(build_root), _replaced_package(build_root)]),
        build_root=build_root,
        source_dir=build_root / "cmd" / "tool",
        goroot=tmp_path / "goroot",
        module_dirs=declared,
    )


def test_a_replaced_module_without_a_declaration_stays_rejected(
    tmp_path: Path,
) -> None:
    """This is the pre-schema-8 rule, unchanged for a command that declares nothing."""

    _root, build_root = _snapshot(tmp_path)
    _modules_txt(build_root)

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.validate_package_graph(
            _encode([_root_package(build_root), _replaced_package(build_root)]),
            build_root=build_root,
            source_dir=build_root / "cmd" / "tool",
            goroot=tmp_path / "goroot",
        )

    assert raised.value.code == "vendor_metadata_inconsistent"


@pytest.mark.parametrize(
    ("files", "extra", "code"),
    [
        ({"board.go": "package board\n"}, {"SFiles": ["asm.s"]}, "go_assembly_forbidden"),
        (
            {"board.go": "package board\n//go:generate echo\n"},
            {},
            "go_generator_forbidden",
        ),
    ],
)
def test_the_audited_vendor_allowance_is_withheld_from_a_replaced_module(
    tmp_path: Path,
    files: dict[str, str],
    extra: dict[str, object],
    code: str,
) -> None:
    """A vendored third-party package may carry these; a replaced module may not.

    The bytes below the vendor tree are first-party source of the same package,
    and only their physical location happens to be the vendor tree.
    """

    root, build_root = _snapshot(tmp_path)
    _module(root, "pkg/board")
    _modules_txt(build_root, "# example.test/board => ../../pkg/board")
    declared = go_v1._canonical_module_directories(root, build_root, ("pkg/board",))
    package = _replaced_package(build_root, files=files, extra=extra)
    if "SFiles" in extra:
        (build_root / "vendor" / "example.test" / "board" / "asm.s").write_text(
            "TEXT ·x(SB),0,$0\n", encoding="utf-8"
        )

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1.validate_package_graph(
            _encode([_root_package(build_root), package]),
            build_root=build_root,
            source_dir=build_root / "cmd" / "tool",
            goroot=tmp_path / "goroot",
            module_dirs=declared,
        )

    assert raised.value.code == code


# --- the closed package command surface -------------------------------------


def _request(command_object: dict[str, object], modules: tuple[str, ...]) -> object:
    return go_v1.BuildRequest(
        toolchain_session=None,  # type: ignore[arg-type]
        source_snapshot=None,  # type: ignore[arg-type]
        command_object=command_object,
        build_root="tools/cli",
        source_dir="tools/cli/cmd/tool",
        command="tool",
        modules=modules,
    )


def test_modules_is_admitted_on_the_closed_package_command_surface() -> None:
    go_v1._validate_package_command_surface(
        _request(
            {
                "type": "build",
                "driver": "go-v1",
                "source_dir": "tools/cli/cmd/tool",
                "modules": ["pkg/board"],
            },
            ("pkg/board",),
        )
    )


def test_a_declaring_command_may_not_present_a_different_module_list() -> None:
    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._validate_package_command_surface(
            _request(
                {
                    "type": "build",
                    "driver": "go-v1",
                    "source_dir": "tools/cli/cmd/tool",
                    "modules": ["pkg/other"],
                },
                ("pkg/board",),
            )
        )

    assert raised.value.code == go_v1.CODE_PACKAGE_INFLUENCE_FORBIDDEN


def test_a_non_declaring_command_keeps_the_schema_six_surface() -> None:
    """An absent or empty list carries the schema-6 meaning, so the surface
    stays byte-identical until a package actually declares a module root."""

    with pytest.raises(go_v1.GoV1Error) as raised:
        go_v1._validate_package_command_surface(
            _request(
                {
                    "type": "build",
                    "driver": "go-v1",
                    "source_dir": "tools/cli/cmd/tool",
                    "modules": [],
                },
                (),
            )
        )

    assert raised.value.code == go_v1.CODE_PACKAGE_INFLUENCE_FORBIDDEN
