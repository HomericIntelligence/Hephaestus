"""Tests for bounded Node runtime reads in learning validation."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import TypedDict

import pytest

from hephaestus.automation.mnemosyne_delivery import LearnDeliveryError
from hephaestus.automation.mnemosyne_node_runtime import NodePackageTree, node_runtime_files


class _NpmCliFixtureKwargs(TypedDict, total=False):
    """Optional keyword values for the npm fixture."""

    layout: str
    manifest_name: str
    bin_target: str
    manifest_present: bool
    ambiguous_cli_root: bool
    dangling_dependency: bool
    external_dependency: bool
    nonregular_cli_target: bool
    special_entry: bool


def _npm_cli_fixture(
    tmp_path: Path,
    *,
    layout: str = "flat",
    manifest_name: str = "markdownlint-cli2",
    bin_target: str = "markdownlint-cli2-bin.mjs",
    manifest_present: bool = True,
    ambiguous_cli_root: bool = False,
    dangling_dependency: bool = False,
    external_dependency: bool = False,
    nonregular_cli_target: bool = False,
    special_entry: bool = False,
) -> tuple[Path, Path, Path]:
    """Create one npm CLI root and one dependency entry."""
    npm_root = tmp_path / "npm" / "node_modules"
    cli_package = npm_root / "markdownlint-cli2"
    cli_package.mkdir(parents=True)

    def write_cli_package(package: Path) -> Path:
        if manifest_present:
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": manifest_name,
                        "version": "0.20.0",
                        "bin": {"markdownlint-cli2": bin_target},
                    }
                )
            )
        entry = package / "markdownlint-cli2-bin.mjs"
        entry.write_text("import { globby } from 'globby';\n")
        return entry

    write_cli_package(cli_package)
    if ambiguous_cli_root:
        cli_package = npm_root / "holder" / "node_modules" / "markdownlint-cli2"
        cli_package.mkdir(parents=True)
    cli_entry = write_cli_package(cli_package)
    bin_dir = npm_root / ".bin"
    bin_dir.mkdir()
    cli_link = bin_dir / "markdownlint-cli2"
    link_target = (
        "../holder/node_modules/markdownlint-cli2/markdownlint-cli2-bin.mjs"
        if ambiguous_cli_root
        else "../markdownlint-cli2/markdownlint-cli2-bin.mjs"
    )
    cli_link.symlink_to(link_target)
    if nonregular_cli_target:
        cli_entry.unlink()
        os.mkfifo(cli_entry)

    dependency_root = cli_package / "node_modules" if layout == "nested" else npm_root
    dependency = dependency_root / "globby"
    dependency_file = dependency / "index.js"
    if dangling_dependency:
        dependency.symlink_to(tmp_path / "missing-globby", target_is_directory=True)
    else:
        dependency.mkdir(parents=True)
        dependency_file.write_text("export const globby = [];\n")
    if external_dependency:
        escaped = tmp_path / "escaped-globby"
        escaped.mkdir()
        (escaped / "index.js").write_text("export const escaped = true;\n")
        dependency_file.unlink()
        dependency.rmdir()
        dependency.symlink_to(escaped, target_is_directory=True)
    if special_entry:
        os.mkfifo(dependency / "special")
    return npm_root, cli_link, dependency_file


def _node_package_tree(cli: Path) -> NodePackageTree:
    """Call the planned package-tree boundary without a collection error in RED."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    factory = getattr(runtime, "node_package_tree", None)
    if not callable(factory):
        pytest.fail("node_package_tree is not available")
    return factory(cli)


def test_node_runtime_collects_rpath_and_transitive_libraries(tmp_path: Path) -> None:
    """Node's library closure uses exact files without a directory grant."""
    node = tmp_path / "bin/node"
    library = tmp_path / "lib/libnode.dylib"
    dependency = tmp_path / "lib/libuv.dylib"
    for path in (node, library, dependency):
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"fixture")

    def runner(argv: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
        assert 0 < timeout <= 10
        source = Path(argv[-1])
        if argv[1] == "-l":
            output = "cmd LC_RPATH\npath @executable_path/../lib (offset 12)\n"
        elif source == node:
            output = f"{source}:\n\t@rpath/libnode.dylib (compatibility version 1)\n"
        elif source == library:
            output = f"{source}:\n\t@loader_path/libuv.dylib (compatibility version 1)\n"
        else:
            output = f"{source}:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1)\n"
        return subprocess.CompletedProcess(argv, 0, output)

    assert set(node_runtime_files(node, runner=runner)) == {node, library, dependency}


def test_node_runtime_rejects_unresolved_library(tmp_path: Path) -> None:
    """An unknown loader dependency cannot widen filesystem access."""
    node = tmp_path / "node"
    node.write_bytes(b"fixture")

    def runner(argv: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
        output = f"{node}:\n\t@rpath/missing.dylib (compatibility version 1)\n"
        return subprocess.CompletedProcess(argv, 0, output if argv[1] == "-L" else "")

    with pytest.raises(LearnDeliveryError, match="Node runtime dependency"):
        node_runtime_files(node, runner=runner)


@pytest.mark.parametrize("layout", ["flat", "nested"])
def test_node_package_tree_binds_flat_and_nested_dependencies(tmp_path: Path, layout: str) -> None:
    """The admitted npm root includes dependencies in flat or nested layout."""
    npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path, layout=layout)

    scope = _node_package_tree(cli_link)

    assert scope.root == npm_root
    assert isinstance(scope.digest, str) and len(scope.digest) == 64
    digest = scope.digest
    scope.verify()
    assert scope.digest == digest


@pytest.mark.parametrize(
    "fixture_kwargs",
    [
        {"manifest_name": "wrong-cli"},
        {"bin_target": "wrong-entry.mjs"},
        {"manifest_present": False},
        {"ambiguous_cli_root": True},
        {"dangling_dependency": True},
        {"external_dependency": True},
        {"nonregular_cli_target": True},
        {"special_entry": True},
    ],
)
def test_node_package_tree_rejects_unsafe_entries(
    tmp_path: Path, fixture_kwargs: _NpmCliFixtureKwargs
) -> None:
    """The package boundary rejects mismatched metadata and unsafe entries."""
    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path, **fixture_kwargs)

    with pytest.raises(LearnDeliveryError):
        _node_package_tree(cli_link)


def test_node_package_tree_detects_dependency_mutation(tmp_path: Path) -> None:
    """A changed admitted dependency fails the bound tree check."""
    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    scope = _node_package_tree(cli_link)

    dependency_file.write_text("export const changed = true;\n")

    with pytest.raises(LearnDeliveryError):
        scope.verify()


def test_node_package_tree_detects_regular_file_mode_change(tmp_path: Path) -> None:
    """A changed admitted file mode fails the bound tree check."""
    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    scope = _node_package_tree(cli_link)
    original_mode = dependency_file.stat().st_mode & 0o777
    dependency_file.chmod(0o600 if original_mode != 0o600 else 0o644)

    with pytest.raises(LearnDeliveryError):
        scope.verify()


def test_node_package_tree_detects_internal_symlink_target_change(tmp_path: Path) -> None:
    """A changed internal symlink target fails the bound tree check."""
    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    internal_target = dependency_file.parent / "alternate.js"
    internal_target.write_text("export const alternate = true;\n")
    internal_link = dependency_file.parent / "entry.js"
    internal_link.symlink_to("index.js")
    scope = _node_package_tree(cli_link)

    internal_link.unlink()
    internal_link.symlink_to("alternate.js")

    with pytest.raises(LearnDeliveryError):
        scope.verify()


@pytest.mark.parametrize("operation", ["add", "remove"])
def test_node_package_tree_detects_entry_set_change(tmp_path: Path, operation: str) -> None:
    """An added or removed admitted entry fails the bound tree check."""
    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    scope = _node_package_tree(cli_link)

    if operation == "add":
        dependency_file.parent.joinpath("added.js").write_text("export const added = true;\n")
    else:
        dependency_file.unlink()

    with pytest.raises(LearnDeliveryError):
        scope.verify()


@pytest.mark.parametrize("operation", ["add", "remove"])
def test_node_package_tree_detects_directory_entry_change(tmp_path: Path, operation: str) -> None:
    """An added or removed admitted directory fails the bound tree check."""
    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    directory = dependency_file.parent / "directory-entry"
    if operation == "remove":
        directory.mkdir()
    scope = _node_package_tree(cli_link)

    if operation == "add":
        directory.mkdir()
    else:
        directory.rmdir()

    with pytest.raises(LearnDeliveryError):
        scope.verify()
