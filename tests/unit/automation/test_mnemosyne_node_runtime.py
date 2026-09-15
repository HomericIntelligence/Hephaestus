"""Tests for bounded Node runtime reads in learning validation."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict

import pytest

from hephaestus.automation.mnemosyne_delivery import LearnDeliveryError
from hephaestus.automation.mnemosyne_node_runtime import NodePackageTree, node_runtime_files
from hephaestus.automation.mnemosyne_package_snapshot import (
    DirectoryBinding,
    PackageSnapshot,
    verify_snapshot_base as strict_verify_snapshot_base,
)

_POSIX_DESCRIPTOR_TEST = os.name == "posix" and all(
    hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
)


@pytest.fixture(autouse=True)
def _use_controlled_snapshot_ancestry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep functional snapshot tests independent of host ancestry."""
    from hephaestus.automation import (
        mnemosyne_node_runtime as runtime,
        mnemosyne_package_snapshot as snapshot_module,
    )

    def verify(binding: DirectoryBinding) -> None:
        binding.verify()

    monkeypatch.setattr(runtime, "verify_snapshot_base", verify)
    monkeypatch.setattr(snapshot_module, "verify_snapshot_base", verify)


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


def _remove_test_tree(path: Path) -> None:
    """Remove one test-owned tree after a deliberate cleanup failure."""
    if not path.exists():
        return
    for directory, children, _files in os.walk(path):
        root = Path(directory)
        root.chmod(0o700)
        for name in children:
            child = root / name
            if not child.is_symlink():
                child.chmod(0o700)
    shutil.rmtree(path)


@pytest.mark.parametrize(
    ("owner", "mode", "allowed"),
    [
        ("current", 0o700, True),
        ("root", 0o1777, True),
        ("other", 0o755, False),
        ("current", 0o770, False),
        ("root", 0o777, False),
    ],
)
def test_snapshot_base_requires_trusted_owner_and_mode(
    monkeypatch: pytest.MonkeyPatch, owner: str, mode: int, allowed: bool
) -> None:
    """The snapshot base accepts only private or root-sticky ancestry."""

    class Binding:
        descriptor = 17

        def __init__(self) -> None:
            self.descriptors = [17]

        def verify(self) -> None:
            return None

        def close(self, *, preserve_error: bool) -> None:
            return None

    owners = {"current": os.geteuid(), "root": 0, "other": os.geteuid() + 1}
    metadata = SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=owners[owner])
    monkeypatch.setattr(os, "fstat", lambda _descriptor: metadata)

    if allowed:
        strict_verify_snapshot_base(Binding())
    else:
        with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
            strict_verify_snapshot_base(Binding())


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


@pytest.mark.parametrize("layout", ["flat", "nested"])
def test_node_package_tree_builds_private_immutable_snapshot(tmp_path: Path, layout: str) -> None:
    """The admitted CLI and dependencies use one private snapshot."""
    npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path, layout=layout)
    cli = cli_link.resolve()
    scope = _node_package_tree(cli_link)
    snapshot_root = scope.snapshot_root
    snapshot_cli = scope.snapshot_cli
    snapshot_dependency = snapshot_root / dependency_file.relative_to(npm_root)
    try:
        assert snapshot_root != npm_root
        assert snapshot_cli != cli
        assert snapshot_cli.read_bytes() == cli.read_bytes()
        assert snapshot_dependency.read_bytes() == dependency_file.read_bytes()
        assert stat.S_IMODE(snapshot_root.stat().st_mode) & 0o277 == 0

        cli.write_text("attacker CLI\n", encoding="utf-8")
        dependency_file.write_text("attacker dependency\n", encoding="utf-8")

        assert b"attacker" not in snapshot_cli.read_bytes()
        assert b"attacker" not in snapshot_dependency.read_bytes()
    finally:
        scope.close()
    assert not snapshot_root.exists()


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_snapshot_destination_replacement_cannot_redirect_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replaced snapshot root cannot receive package writes elsewhere."""
    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    external = tmp_path / "external"
    external.mkdir()
    real_write = PackageSnapshot.write_file
    replaced = False
    redirected: Path | None = None
    snapshot_parent: Path | None = None

    def replace_root(
        snapshot: PackageSnapshot, relative: Path, payload: bytes, mode: int, deadline: float
    ) -> None:
        nonlocal replaced, redirected, snapshot_parent
        if not replaced:
            snapshot_parent = snapshot.parent
            root = snapshot.root
            redirected = external / relative
            redirected.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            detached = root.with_name("detached-node-modules")
            root.rename(detached)
            root.symlink_to(external, target_is_directory=True)
            replaced = True
        real_write(snapshot, relative, payload, mode, deadline)

    monkeypatch.setattr(PackageSnapshot, "write_file", replace_root)

    try:
        with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
            _node_package_tree(cli_link)
        assert replaced
        assert redirected is not None
        assert not redirected.exists()
        assert stat.S_IMODE(redirected.parent.stat().st_mode) == 0o700
    finally:
        if snapshot_parent is not None:
            _remove_test_tree(snapshot_parent)


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
@pytest.mark.parametrize("replacement", ["package", "cli", "ancestor-link"])
def test_node_package_tree_binds_validation_and_snapshot_to_one_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    """A source replacement after validation cannot enter the snapshot."""
    npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    cli = cli_link.resolve()
    package = cli.parent
    real_create = PackageSnapshot.create
    replaced = False

    def replace_before_snapshot(*args: Any, **kwargs: Any) -> PackageSnapshot:
        nonlocal replaced
        if replacement == "package":
            package.rename(package.with_name("markdownlint-cli2-original"))
            package.mkdir()
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "markdownlint-cli2",
                        "bin": {"markdownlint-cli2": cli.name},
                    }
                ),
                encoding="utf-8",
            )
            cli.write_text("attacker CLI\n", encoding="utf-8")
        elif replacement == "cli":
            cli.unlink()
            cli.write_text("attacker CLI\n", encoding="utf-8")
        else:
            npm_parent = npm_root.parent
            detached = npm_parent.with_name("npm-original")
            npm_parent.rename(detached)
            attacker = tmp_path / "attacker" / "npm"
            attacker.mkdir(parents=True)
            source = detached / "node_modules"
            attacker_root = attacker / "node_modules"
            attacker_root.mkdir()
            attacker_package = attacker_root / "markdownlint-cli2"
            attacker_package.mkdir()
            (attacker_package / "package.json").write_text(
                (source / "markdownlint-cli2" / "package.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (attacker_package / cli.name).write_text("attacker CLI\n", encoding="utf-8")
            npm_parent.symlink_to(attacker, target_is_directory=True)
        replaced = True
        return real_create(*args, **kwargs)

    monkeypatch.setattr(PackageSnapshot, "create", replace_before_snapshot)

    with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
        _node_package_tree(cli_link)
    assert replaced


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


def test_node_package_tree_rejects_parent_step_after_unresolved_link_component(
    tmp_path: Path,
) -> None:
    """A link cannot apply a parent step after an unresolved link component."""
    npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    nested = npm_root / "a" / "b"
    nested.mkdir(parents=True)
    (npm_root / "a" / "value.js").write_text("benign\n", encoding="utf-8")
    (npm_root / "value.js").write_text("different\n", encoding="utf-8")
    (npm_root / "alias").symlink_to(Path("a") / "b", target_is_directory=True)
    (npm_root / "semantic-link.js").symlink_to(Path("alias") / ".." / "value.js")

    with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
        _node_package_tree(cli_link)


def test_failed_package_admission_removes_partial_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed admission removes its partially built private snapshot."""
    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path, special_entry=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    real_create = PackageSnapshot.create
    snapshots: list[PackageSnapshot] = []

    def record_create(*args: Any, **kwargs: Any) -> PackageSnapshot:
        snapshot = real_create(*args, **kwargs)
        snapshots.append(snapshot)
        return snapshot

    monkeypatch.setattr(PackageSnapshot, "create", record_create)

    with pytest.raises(LearnDeliveryError, match="special entry"):
        _node_package_tree(cli_link)
    assert len(snapshots) == 1
    assert snapshots[0]._closed
    assert not snapshots[0].parent.exists()


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
@pytest.mark.parametrize("failure", ["fchmod", "scandir"])
def test_package_snapshot_cleanup_closes_untransferred_child_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A child setup failure closes every cleanup descriptor."""
    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    scope = _node_package_tree(cli_link)
    child_identity = scope.snapshot_cli.parent.stat()
    real_open = os.open
    real_close = os.close
    real_fchmod = os.fchmod
    real_scandir = os.scandir
    active = set(scope._source_binding.descriptors + scope._snapshot.descriptors)
    failed = False

    def track_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, flags, *args, **kwargs)
        active.add(descriptor)
        return descriptor

    def track_close(descriptor: int) -> None:
        active.discard(descriptor)
        real_close(descriptor)

    def selected_fchmod(descriptor: int, mode: int) -> None:
        nonlocal failed
        opened = os.fstat(descriptor)
        if failure == "fchmod" and (opened.st_dev, opened.st_ino) == (
            child_identity.st_dev,
            child_identity.st_ino,
        ):
            failed = True
            raise OSError("injected child fchmod failure")
        real_fchmod(descriptor, mode)

    def selected_scandir(path: Any) -> Iterator[os.DirEntry[str]]:
        nonlocal failed
        if failure == "scandir" and isinstance(path, int):
            opened = os.fstat(path)
            if (opened.st_dev, opened.st_ino) == (
                child_identity.st_dev,
                child_identity.st_ino,
            ):
                failed = True
                raise OSError("injected child scandir failure")
        return real_scandir(path)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(os, "open", track_open)
            scoped.setattr(os, "close", track_close)
            scoped.setattr(os, "fchmod", selected_fchmod)
            scoped.setattr(os, "scandir", selected_scandir)
            with pytest.raises(LearnDeliveryError, match="snapshot cleanup failed"):
                scope.close()
            assert failed
            assert active == set()
    finally:
        for descriptor in tuple(active):
            with suppress(OSError):
                real_close(descriptor)
        _remove_test_tree(scope._snapshot_parent)


def test_node_package_tree_normalizes_snapshot_allocation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A temporary snapshot allocation error has one stable contract."""
    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    allocation_calls: list[Path] = []

    def fail_allocation(base: Path, binding: DirectoryBinding, **_kwargs: Any) -> PackageSnapshot:
        allocation_calls.append(base)
        binding.close(preserve_error=True)
        raise OSError("injected snapshot allocation failure")

    monkeypatch.setattr(PackageSnapshot, "create", fail_allocation)

    with pytest.raises(LearnDeliveryError, match="Node package dependency tree is unavailable"):
        _node_package_tree(cli_link)
    assert len(allocation_calls) == 1


def test_node_package_tree_preserves_snapshot_root_creation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root creation error stays primary when snapshot cleanup also fails."""
    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    real_mkdir = os.mkdir
    real_close = PackageSnapshot.close
    cleanup_calls: list[Path] = []
    failed = False

    def fail_root_creation(path: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal failed
        if path == "node_modules" and kwargs.get("dir_fd") is not None:
            failed = True
            raise OSError("injected snapshot root creation failure")
        real_mkdir(path, *args, **kwargs)

    def fail_cleanup(snapshot: PackageSnapshot, *, preserve_error: bool = False) -> None:
        cleanup_calls.append(snapshot.parent)
        real_close(snapshot, preserve_error=preserve_error)
        raise LearnDeliveryError("Node package snapshot cleanup failed")

    monkeypatch.setattr(os, "mkdir", fail_root_creation)
    monkeypatch.setattr(PackageSnapshot, "close", fail_cleanup)
    with pytest.raises(LearnDeliveryError, match="Node package dependency tree is unavailable"):
        _node_package_tree(cli_link)
    assert failed
    assert len(cleanup_calls) == 1
    assert not cleanup_calls[0].exists()


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_snapshot_creation_cleanup_does_not_chmod_rejected_root_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed creation preserves a replacement root and its mode."""
    from hephaestus.automation import (
        mnemosyne_node_runtime as runtime,
        mnemosyne_package_snapshot as snapshot_module,
    )

    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    def verify_base(binding: DirectoryBinding) -> None:
        binding.verify()

    monkeypatch.setattr(runtime, "verify_snapshot_base", verify_base)
    monkeypatch.setattr(snapshot_module, "verify_snapshot_base", verify_base)
    real_open = os.open
    rejected: Path | None = None

    def replace_root(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal rejected
        if path != "node_modules" or kwargs.get("dir_fd") is None or rejected is not None:
            return real_open(path, flags, *args, **kwargs)
        parents = tuple(tmp_path.glob("hephaestus-node-package-*"))
        if not parents:
            return real_open(path, flags, *args, **kwargs)
        parent = parents[0]
        owned = parent / "node_modules"
        detached = parent / "owned-node_modules"
        rejected = parent / "rejected-node_modules"
        owned.rename(detached)
        owned.mkdir(mode=0o755)
        owned.chmod(0o755)
        descriptor = real_open(path, flags, *args, **kwargs)
        owned.rename(rejected)
        detached.rename(owned)
        return descriptor

    monkeypatch.setattr(os, "open", replace_root)
    try:
        with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
            _node_package_tree(cli_link)
        assert rejected is not None
        assert stat.S_IMODE(rejected.stat().st_mode) == 0o755
    finally:
        for parent in tmp_path.glob("hephaestus-node-package-*"):
            _remove_test_tree(parent)


def test_node_package_tree_rejects_oversized_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The package validator bounds the manifest before it loads JSON."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    manifest = cli_link.resolve().parent / "package.json"
    manifest.write_text(
        json.dumps(
            {
                "name": "markdownlint-cli2",
                "bin": {"markdownlint-cli2": "markdownlint-cli2-bin.mjs"},
                "padding": "x" * 256,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime, "_MAX_PACKAGE_MANIFEST_BYTES", 128, raising=False)

    with pytest.raises(LearnDeliveryError, match="manifest is too large"):
        _node_package_tree(cli_link)


@pytest.mark.parametrize("payload", ["large-integer", "deep-json"])
def test_node_package_tree_normalizes_manifest_parser_resource_failure(
    tmp_path: Path, payload: str
) -> None:
    """Manifest parser resource failures use the stable refusal contract."""
    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    manifest = cli_link.resolve().parent / "package.json"
    if payload == "large-integer":
        digits = "1" * (sys.get_int_max_str_digits() + 1)
        manifest.write_text(f'{{"padding": {digits}}}', encoding="utf-8")
    else:
        depth = 400_000
        manifest.write_text("[" * depth + "0" + "]" * depth, encoding="utf-8")

    with pytest.raises(LearnDeliveryError, match="Node package manifest is unavailable"):
        _node_package_tree(cli_link)


def test_node_package_tree_bounds_directory_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The package validator stops enumeration at the remaining entry limit."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    for index in range(10):
        (npm_root / f"extra-{index}").write_text("entry\n", encoding="utf-8")
    root_identity = npm_root.stat()
    real_scandir = os.scandir
    reads = 0

    class GuardedEntries:
        """Reject reads beyond one overflow entry."""

        def __init__(self, path: int) -> None:
            self._entries = real_scandir(path)

        def __enter__(self) -> GuardedEntries:
            self._entries.__enter__()
            return self

        def __exit__(self, *args: object) -> None:
            self._entries.__exit__(*args)

        def __iter__(self) -> GuardedEntries:
            return self

        def __next__(self) -> os.DirEntry[str]:
            nonlocal reads
            reads += 1
            if reads > 4:
                raise AssertionError("directory enumeration exceeded its resource limit")
            return next(self._entries)

    def bounded_scandir(path: Any) -> Iterator[os.DirEntry[str]] | GuardedEntries:
        if isinstance(path, int) and (os.fstat(path).st_dev, os.fstat(path).st_ino) == (
            root_identity.st_dev,
            root_identity.st_ino,
        ):
            return GuardedEntries(path)
        return real_scandir(path)

    monkeypatch.setattr(runtime, "_MAX_PACKAGE_ENTRIES", 4)
    monkeypatch.setattr(os, "scandir", bounded_scandir)

    with pytest.raises(LearnDeliveryError, match="dependency tree is too large"):
        _node_package_tree(cli_link)
    assert reads == 4


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_node_package_tree_bounds_directory_depth_and_cleans_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree that exceeds the directory-depth limit leaves no snapshot."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    current = dependency_file.parent
    for index in range(4):
        current = current / f"level-{index}"
        current.mkdir()
    real_create = PackageSnapshot.create
    snapshots: list[PackageSnapshot] = []

    def record_create(*args: Any, **kwargs: Any) -> PackageSnapshot:
        snapshot = real_create(*args, **kwargs)
        snapshots.append(snapshot)
        return snapshot

    monkeypatch.setattr(runtime, "_MAX_PACKAGE_DEPTH", 2, raising=False)
    monkeypatch.setattr(PackageSnapshot, "create", record_create)

    with pytest.raises(LearnDeliveryError, match="dependency tree is too large"):
        _node_package_tree(cli_link)

    assert len(snapshots) == 1
    assert snapshots[0]._closed
    assert not snapshots[0].parent.exists()


@pytest.mark.parametrize(
    ("limit_name", "limit", "entry_name"),
    [
        ("_MAX_PACKAGE_COMPONENT_BYTES", 4, "component"),
        ("_MAX_PACKAGE_RELATIVE_PATH_BYTES", 8, "relative-path"),
    ],
)
def test_node_package_tree_bounds_encoded_paths_before_snapshot_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit: int,
    entry_name: str,
) -> None:
    """An oversized encoded package path fails before its snapshot entry exists."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    (npm_root / entry_name).write_text("entry\n", encoding="utf-8")
    monkeypatch.setattr(runtime, limit_name, limit, raising=False)

    with pytest.raises(LearnDeliveryError, match="dependency tree is too large"):
        _node_package_tree(cli_link)


def test_node_package_tree_bounds_symlink_target_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An oversized link target fails before metadata-record allocation."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    target = dependency_file.parent / "target-with-a-long-name.js"
    target.write_text("target\n", encoding="utf-8")
    (dependency_file.parent / "entry-link.js").symlink_to(target.name)
    monkeypatch.setattr(runtime, "_MAX_PACKAGE_LINK_TARGET_BYTES", 8, raising=False)

    with pytest.raises(LearnDeliveryError, match="dependency tree is too large"):
        _node_package_tree(cli_link)


def test_node_package_tree_bounds_aggregate_metadata_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Aggregate package metadata has an independent byte limit."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    monkeypatch.setattr(runtime, "_MAX_PACKAGE_METADATA_BYTES", 64, raising=False)

    with pytest.raises(LearnDeliveryError, match="dependency tree is too large"):
        _node_package_tree(cli_link)


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_node_package_tree_bounds_active_descriptors_on_deep_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deep tree fails before it can exceed the descriptor limit."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    real_open = os.open
    real_dup = os.dup
    real_close = os.close
    real_children = runtime._bounded_package_children
    active: set[int] = set()
    peak = 0
    reached_deep_directory = False

    def track_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal peak
        descriptor = real_open(path, flags, *args, **kwargs)
        active.add(descriptor)
        peak = max(peak, len(active))
        return descriptor

    def track_close(descriptor: int) -> None:
        active.discard(descriptor)
        real_close(descriptor)

    def track_dup(descriptor: int) -> int:
        nonlocal peak
        duplicate = real_dup(descriptor)
        active.add(duplicate)
        peak = max(peak, len(active))
        return duplicate

    def track_children(
        descriptor: int, budget: Any, deadline: float | None = None
    ) -> list[os.DirEntry[str]]:
        nonlocal reached_deep_directory
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) == deep_identity:
            reached_deep_directory = True
        return real_children(descriptor, budget, deadline)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "dup", track_dup)
    monkeypatch.setattr(os, "close", track_close)

    shallow = _node_package_tree(cli_link)
    retained = len(shallow._source_binding.descriptors) + len(shallow._snapshot.descriptors)
    shallow.close()
    assert active == set()
    descriptor_limit = retained + 6
    monkeypatch.setattr(runtime, "_MAX_PACKAGE_ACTIVE_DESCRIPTORS", descriptor_limit, raising=False)
    peak = 0

    current = dependency_file.parent
    deep_identity = (-1, -1)
    for index in range(8):
        current = current / f"d{index}"
        current.mkdir()
        if index == 2:
            status = current.stat()
            deep_identity = status.st_dev, status.st_ino
    monkeypatch.setattr(runtime, "_bounded_package_children", track_children)

    with pytest.raises(LearnDeliveryError, match="dependency tree is too large"):
        _node_package_tree(cli_link)

    assert reached_deep_directory
    assert peak <= descriptor_limit
    assert active == set()


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_package_tree_records_closes_root_binding_when_initial_fstat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An initial tree metadata failure closes the complete root binding."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    npm_root, _cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    real_open = os.open
    real_close = os.close
    real_fstat = os.fstat
    real_open_root = runtime._open_package_root
    active: set[int] = set()
    target_descriptor = -1
    armed = False

    def track_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, flags, *args, **kwargs)
        active.add(descriptor)
        return descriptor

    def track_close(descriptor: int) -> None:
        active.discard(descriptor)
        real_close(descriptor)

    def open_then_arm(path: Path, **kwargs: Any) -> Any:
        nonlocal target_descriptor, armed
        binding = real_open_root(path, **kwargs)
        target_descriptor = binding.descriptor
        armed = True
        return binding

    def fail_initial_fstat(descriptor: int) -> os.stat_result:
        if armed and descriptor == target_descriptor:
            raise OSError("injected initial root fstat failure")
        return real_fstat(descriptor)

    try:
        monkeypatch.setattr(os, "open", track_open)
        monkeypatch.setattr(os, "close", track_close)
        monkeypatch.setattr(os, "fstat", fail_initial_fstat)
        monkeypatch.setattr(runtime, "_open_package_root", open_then_arm)

        with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
            runtime._package_tree_records(npm_root)
        assert active == set()
    finally:
        for descriptor in tuple(active):
            with suppress(OSError):
                real_close(descriptor)


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_manifest_cli_close_failure_does_not_close_reused_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A released CLI descriptor number is not closed a second time."""
    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    cli = cli_link.resolve()
    package_identity = cli.parent.stat()
    real_open = os.open
    real_close = os.close
    cli_descriptor = -1
    replacement_descriptor = -1
    failed = False

    def select_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal cli_descriptor
        descriptor = real_open(path, flags, *args, **kwargs)
        parent_descriptor = kwargs.get("dir_fd")
        if path == cli.name and parent_descriptor is not None:
            parent = os.fstat(parent_descriptor)
            if (parent.st_dev, parent.st_ino) == (
                package_identity.st_dev,
                package_identity.st_ino,
            ):
                cli_descriptor = descriptor
        return descriptor

    def fail_after_reuse(descriptor: int) -> None:
        nonlocal replacement_descriptor, failed
        if descriptor == cli_descriptor and not failed:
            real_close(descriptor)
            replacement_descriptor = real_open("/dev/null", os.O_RDONLY)
            assert replacement_descriptor == descriptor
            failed = True
            raise OSError("injected CLI close failure")
        real_close(descriptor)

    try:
        monkeypatch.setattr(os, "open", select_open)
        monkeypatch.setattr(os, "close", fail_after_reuse)

        with pytest.raises(LearnDeliveryError, match="CLI is not a regular file"):
            _node_package_tree(cli_link)
        assert failed
        os.fstat(replacement_descriptor)
    finally:
        if replacement_descriptor >= 0:
            with suppress(OSError):
                real_close(replacement_descriptor)


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_snapshot_cleanup_close_failure_does_not_close_reused_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Snapshot cleanup disowns a descriptor before its close attempt."""
    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    scope = _node_package_tree(cli_link)
    real_open = os.open
    real_close = os.close
    cleanup_descriptor = scope._snapshot.descriptor
    replacement_descriptor = -1
    failed = False

    def fail_after_reuse(descriptor: int) -> None:
        nonlocal replacement_descriptor, failed
        if descriptor == cleanup_descriptor and not failed:
            real_close(descriptor)
            replacement_descriptor = real_open("/dev/null", os.O_RDONLY)
            assert replacement_descriptor == descriptor
            failed = True
            raise OSError("injected snapshot close failure")
        real_close(descriptor)

    try:
        monkeypatch.setattr(os, "close", fail_after_reuse)

        with pytest.raises(LearnDeliveryError, match="snapshot cleanup failed"):
            scope.close()
        assert failed
        scope.close()
        os.fstat(replacement_descriptor)
        assert not scope._snapshot_parent.exists()
    finally:
        if replacement_descriptor >= 0:
            with suppress(OSError):
                real_close(replacement_descriptor)


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_snapshot_cleanup_preserves_replaced_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Snapshot cleanup does not unlink a child that changed identity."""
    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    scope = _node_package_tree(cli_link)
    target = scope.snapshot_cli
    parent_identity = target.parent.stat()
    replacement = b"replacement\n"
    real_stat = os.stat
    real_unlink = os.unlink
    replaced = False

    def replace_before_identity_check(
        path: Any, *args: Any, dir_fd: int | None = None, **kwargs: Any
    ) -> os.stat_result:
        nonlocal replaced
        if (
            not replaced
            and path == target.name
            and dir_fd is not None
            and (os.fstat(dir_fd).st_dev, os.fstat(dir_fd).st_ino)
            == (parent_identity.st_dev, parent_identity.st_ino)
        ):
            os.fchmod(dir_fd, 0o700)
            real_unlink(path, dir_fd=dir_fd)
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dir_fd,
            )
            try:
                os.write(descriptor, replacement)
            finally:
                os.close(descriptor)
            replaced = True
        return real_stat(path, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(os, "stat", replace_before_identity_check)

    try:
        with pytest.raises(LearnDeliveryError, match="snapshot cleanup failed"):
            scope.close()
        assert replaced
        assert target.read_bytes() == replacement
    finally:
        _remove_test_tree(scope._snapshot_parent)


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_snapshot_cleanup_uses_one_global_entry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All cleanup directories share one entry budget."""
    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    scope = _node_package_tree(cli_link)
    scope._snapshot.max_entries = 4

    try:
        with pytest.raises(LearnDeliveryError, match="snapshot cleanup failed"):
            scope.close()
    finally:
        _remove_test_tree(scope._snapshot_parent)


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_snapshot_cleanup_enforces_elapsed_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Snapshot cleanup stops after one elapsed-time limit."""
    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    scope = _node_package_tree(cli_link)
    calls = 0

    def monotonic() -> float:
        nonlocal calls
        calls += 1
        return 0.0 if calls == 1 else 11.0

    monkeypatch.setattr(time, "monotonic", monotonic)

    try:
        with pytest.raises(LearnDeliveryError, match="snapshot cleanup failed"):
            scope.close()
    finally:
        _remove_test_tree(scope._snapshot_parent)


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_link_parent_close_failure_does_not_close_reused_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Link resolution disowns a released parent descriptor number."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    root = tmp_path / "node_modules"
    target = root / "first" / "second" / "target.js"
    target.parent.mkdir(parents=True)
    target.write_text("target\n", encoding="utf-8")
    real_open = os.open
    real_close = os.close
    root_descriptor = real_open(root, runtime._PACKAGE_DIRECTORY_FLAGS)
    first_descriptor = -1
    replacement_descriptor = -1
    failed = False

    def select_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal first_descriptor
        descriptor = real_open(path, flags, *args, **kwargs)
        if path == "first":
            first_descriptor = descriptor
        return descriptor

    def fail_after_reuse(descriptor: int) -> None:
        nonlocal replacement_descriptor, failed
        if descriptor == first_descriptor and not failed:
            real_close(descriptor)
            replacement_descriptor = real_open("/dev/null", os.O_RDONLY)
            assert replacement_descriptor == descriptor
            failed = True
            raise OSError("injected link-parent close failure")
        real_close(descriptor)

    try:
        monkeypatch.setattr(os, "open", select_open)
        monkeypatch.setattr(os, "close", fail_after_reuse)

        with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
            runtime._resolve_package_link(
                root_descriptor,
                (),
                "first/second/target.js",
                descriptor_slots=3,
                deadline=time.monotonic() + 10.0,
            )
        assert failed
        os.fstat(replacement_descriptor)
    finally:
        real_close(root_descriptor)
        if replacement_descriptor >= 0:
            with suppress(OSError):
                real_close(replacement_descriptor)


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_package_tree_records_enforces_global_entry_budget_across_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pending sibling entries share one traversal-wide entry budget."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    root = tmp_path / "node_modules"
    subtree = root / "a"
    subtree.mkdir(parents=True)
    (subtree / "first.js").write_text("first\n", encoding="utf-8")
    (subtree / "second.js").write_text("second\n", encoding="utf-8")
    (root / "b.js").write_text("sibling\n", encoding="utf-8")
    real_children = runtime._bounded_package_children
    real_entry = runtime._package_tree_entry_record
    processed = 1
    pending = 0
    peak_allocated = 1

    def track_children(
        directory_descriptor: int,
        allocation_budget: Any,
        deadline: float | None = None,
    ) -> list[os.DirEntry[str]]:
        nonlocal pending, peak_allocated
        children = real_children(directory_descriptor, allocation_budget, deadline)
        pending += len(children)
        peak_allocated = max(peak_allocated, processed + pending)
        return children

    def track_entry(*args: Any, **kwargs: Any) -> Any:
        nonlocal processed, pending, peak_allocated
        pending -= 1
        processed += 1
        peak_allocated = max(peak_allocated, processed + pending)
        return real_entry(*args, **kwargs)

    monkeypatch.setattr(runtime, "_MAX_PACKAGE_ENTRIES", 4)
    monkeypatch.setattr(runtime, "_bounded_package_children", track_children)
    monkeypatch.setattr(runtime, "_package_tree_entry_record", track_entry)

    with pytest.raises(LearnDeliveryError, match="dependency tree is too large"):
        runtime._package_tree_records(root)
    assert peak_allocated <= 4


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_node_package_tree_bounds_open_directories_on_wide_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wide valid tree stays within the total descriptor limit."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    descriptor_limit = runtime._MAX_PACKAGE_ACTIVE_DESCRIPTORS
    for index in range(descriptor_limit * 2):
        (npm_root / f"wide-{index:03d}").mkdir()
    real_open = os.open
    real_dup = os.dup
    real_close = os.close
    active: set[int] = set()
    peak = 0

    def track_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal peak
        descriptor = real_open(path, flags, *args, **kwargs)
        active.add(descriptor)
        peak = max(peak, len(active))
        return descriptor

    def track_close(descriptor: int) -> None:
        active.discard(descriptor)
        real_close(descriptor)

    def track_dup(descriptor: int) -> int:
        nonlocal peak
        duplicate = real_dup(descriptor)
        active.add(duplicate)
        peak = max(peak, len(active))
        return duplicate

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "dup", track_dup)
    monkeypatch.setattr(os, "close", track_close)

    with _node_package_tree(cli_link) as scope:
        scope.verify()

    assert peak <= descriptor_limit
    assert active == set()


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_node_package_tree_keeps_nested_directory_binding_during_aba(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nested replacement cannot redirect the descriptor-bound tree read."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    dependency = dependency_file.parent
    initial = dependency.stat()
    real_children = runtime._bounded_package_children
    changed = False

    def replace_and_restore(
        directory_descriptor: int,
        allocation_budget: Any,
        deadline: float | None = None,
    ) -> list[os.DirEntry[str]]:
        nonlocal changed
        opened = os.fstat(directory_descriptor)
        if not changed and (opened.st_dev, opened.st_ino) == (initial.st_dev, initial.st_ino):
            detached = dependency.with_name("globby-detached")
            dependency.rename(detached)
            dependency.mkdir()
            replacement = dependency / "replacement.js"
            replacement.write_text("export const replacement = true;\n", encoding="utf-8")
            children = real_children(directory_descriptor, allocation_budget, deadline)
            replacement.unlink()
            dependency.rmdir()
            detached.rename(dependency)
            changed = True
            return children
        return real_children(directory_descriptor, allocation_budget, deadline)

    monkeypatch.setattr(runtime, "_bounded_package_children", replace_and_restore)

    scope = _node_package_tree(cli_link)
    try:
        snapshot_dependency = scope.snapshot_root / dependency_file.relative_to(scope.root)
        assert snapshot_dependency.read_text(encoding="utf-8") == "export const globby = [];\n"
        assert not (scope.snapshot_root / "globby" / "replacement.js").exists()
    finally:
        scope.close()
    assert changed


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
@pytest.mark.parametrize("operation", ["read", "enumeration"])
def test_node_package_tree_enforces_elapsed_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """A slow package read or enumeration fails after the elapsed deadline."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    dependency_identity = dependency_file.stat()
    dependency_directory = dependency_file.parent.stat()
    real_read = os.read
    real_scandir = os.scandir
    elapsed = 0.0

    def monotonic() -> float:
        return elapsed

    def slow_read(descriptor: int, size: int) -> bytes:
        nonlocal elapsed
        opened = os.fstat(descriptor)
        payload = real_read(descriptor, size)
        if operation == "read" and (opened.st_dev, opened.st_ino) == (
            dependency_identity.st_dev,
            dependency_identity.st_ino,
        ):
            elapsed = 11.0
        return payload

    def slow_scandir(path: Any) -> Iterator[os.DirEntry[str]]:
        nonlocal elapsed
        entries = real_scandir(path)
        if operation == "enumeration" and isinstance(path, int):
            opened = os.fstat(path)
            if (opened.st_dev, opened.st_ino) == (
                dependency_directory.st_dev,
                dependency_directory.st_ino,
            ):
                elapsed = 11.0
        return entries

    monkeypatch.setattr(time, "monotonic", monotonic)
    monkeypatch.setattr(runtime, "_PACKAGE_WALK_TIMEOUT_S", 10.0, raising=False)
    monkeypatch.setattr(os, "read", slow_read)
    monkeypatch.setattr(os, "scandir", slow_scandir)

    with pytest.raises(LearnDeliveryError, match="dependency tree timed out"):
        _node_package_tree(cli_link)


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_node_package_tree_keeps_admission_deadline_during_snapshot_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Snapshot digest work stays in the original admission time limit."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    real_seal = PackageSnapshot.seal
    real_digest = runtime._package_records_digest
    elapsed = 0.0
    digest_calls = 0

    def monotonic() -> float:
        return elapsed

    def finish_seal(snapshot: PackageSnapshot, deadline: float) -> None:
        nonlocal elapsed
        real_seal(snapshot, deadline)
        elapsed = 9.0

    def expire_during_hash(records: tuple[bytes, ...]) -> str:
        nonlocal digest_calls, elapsed
        result = real_digest(records)
        digest_calls += 1
        if digest_calls == 2:
            elapsed = 11.0
        return result

    monkeypatch.setattr(time, "monotonic", monotonic)
    monkeypatch.setattr(runtime, "_PACKAGE_WALK_TIMEOUT_S", 10.0, raising=False)
    monkeypatch.setattr(PackageSnapshot, "seal", finish_seal)
    monkeypatch.setattr(runtime, "_package_records_digest", expire_during_hash)

    with pytest.raises(LearnDeliveryError, match="dependency tree timed out"):
        _node_package_tree(cli_link)
    assert digest_calls == 2


def test_node_package_tree_fails_closed_without_descriptor_primitives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A platform without safe descriptor primitives cannot admit a package."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    monkeypatch.setattr(runtime, "_POSIX_PACKAGE_DESCRIPTOR_WALK", False, raising=False)
    monkeypatch.setattr(
        os,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("portable package walk used os.open")
        ),
    )

    with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
        _node_package_tree(cli_link)


def test_module_import_does_not_require_posix_open_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform without POSIX open flags can import the Node validator."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    names = ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    with monkeypatch.context() as scoped:
        for name in names:
            scoped.delattr(os, name, raising=False)
        reloaded = importlib.reload(runtime)
        assert callable(reloaded.node_package_tree)
        assert reloaded._POSIX_PACKAGE_DESCRIPTOR_WALK is False
    importlib.reload(runtime)


@pytest.mark.parametrize("replacement", ["fifo", "symlink"])
@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_node_package_tree_rejects_manifest_replacement_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    """A special or linked manifest replacement cannot enter validation."""
    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    manifest = cli_link.resolve().parent / "package.json"
    outside = tmp_path / "outside-package.json"
    outside.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
    real_open = os.open
    replaced = False

    def replace_then_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if not replaced and path == manifest.name and dir_fd is not None:
            manifest.unlink()
            if replacement == "fifo":
                os.mkfifo(manifest)
            else:
                manifest.symlink_to(outside)
            replaced = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replace_then_open)

    with pytest.raises(LearnDeliveryError, match="manifest is unavailable"):
        _node_package_tree(cli_link)
    assert replaced


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_node_package_tree_rejects_cli_replacement_during_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CLI replacement during snapshot admission is rejected."""
    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    cli = cli_link.resolve()
    package_identity = cli.parent.stat()
    outside = tmp_path / "outside-cli.mjs"
    outside.write_text("attacker CLI\n", encoding="utf-8")
    real_open = os.open
    replaced = False

    def replace_then_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if path == cli.name and dir_fd is not None and not replaced:
            opened = os.fstat(dir_fd)
            if (opened.st_dev, opened.st_ino) == (
                package_identity.st_dev,
                package_identity.st_ino,
            ):
                cli.unlink()
                cli.symlink_to(outside)
                replaced = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replace_then_open)

    with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
        _node_package_tree(cli_link)
    assert replaced


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
@pytest.mark.parametrize("failure_target", ["root", "child"])
def test_node_package_tree_closes_descriptor_when_fstat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_target: str
) -> None:
    """An opened directory closes when its identity check fails."""
    npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    target_name = "globby"
    root_parent = npm_root.parent.stat()
    real_open = os.open
    real_fstat = os.fstat
    real_close = os.close
    target_descriptor = -1
    closed: set[int] = set()

    def select_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal target_descriptor
        descriptor = real_open(path, flags, *args, **kwargs)
        if (
            failure_target == "root"
            and path == npm_root.name
            and kwargs.get("dir_fd") is not None
            and (
                os.fstat(kwargs["dir_fd"]).st_dev,
                os.fstat(kwargs["dir_fd"]).st_ino,
            )
            == (root_parent.st_dev, root_parent.st_ino)
        ) or (failure_target == "child" and path == target_name):
            target_descriptor = descriptor
        return descriptor

    def fail_fstat(descriptor: int) -> os.stat_result:
        if descriptor == target_descriptor:
            raise OSError("injected fstat failure")
        return real_fstat(descriptor)

    def track_close(descriptor: int) -> None:
        closed.add(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(os, "open", select_open)
    monkeypatch.setattr(os, "fstat", fail_fstat)
    monkeypatch.setattr(os, "close", track_close)

    with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
        _node_package_tree(cli_link)
    assert target_descriptor in closed


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
@pytest.mark.parametrize(
    ("overflow", "message"),
    [(False, "dependency tree is unavailable"), (True, "dependency tree is too large")],
)
def test_package_file_close_failure_has_stable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overflow: bool,
    message: str,
) -> None:
    """A file close error is stable and does not replace a primary error."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    path = tmp_path / "entry.js"
    path.write_bytes(b"x")
    parent = os.open(tmp_path, runtime._PACKAGE_DIRECTORY_FLAGS)
    metadata = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
    real_open = os.open
    real_read = os.read
    real_close = os.close
    file_descriptor = -1

    def select_open(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal file_descriptor
        descriptor = real_open(name, flags, *args, **kwargs)
        if name == path.name:
            file_descriptor = descriptor
        return descriptor

    def selected_read(descriptor: int, size: int) -> bytes:
        if overflow and descriptor == file_descriptor:
            return b"xx"
        return real_read(descriptor, size)

    def fail_close(descriptor: int) -> None:
        real_close(descriptor)
        if descriptor == file_descriptor:
            raise OSError("injected file close failure")

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(os, "open", select_open)
            scoped.setattr(os, "read", selected_read)
            scoped.setattr(os, "close", fail_close)
            with pytest.raises(LearnDeliveryError, match=message):
                runtime._read_bounded_regular_file(
                    parent,
                    path.name,
                    metadata,
                    1,
                    unavailable_message="Node package dependency tree is unavailable",
                    too_large_message="Node package dependency tree is too large",
                )
    finally:
        real_close(parent)


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_manifest_directory_close_failure_has_stable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest directory close error uses the manifest error contract."""
    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    package = cli_link.resolve().parent
    root_identity = package.parent.stat()
    real_open = os.open
    real_close = os.close
    package_descriptor = -1

    def select_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal package_descriptor
        descriptor = real_open(path, flags, *args, **kwargs)
        if (
            path == package.name
            and kwargs.get("dir_fd") is not None
            and (
                os.fstat(kwargs["dir_fd"]).st_dev,
                os.fstat(kwargs["dir_fd"]).st_ino,
            )
            == (root_identity.st_dev, root_identity.st_ino)
        ):
            package_descriptor = descriptor
        return descriptor

    def fail_close(descriptor: int) -> None:
        real_close(descriptor)
        if descriptor == package_descriptor:
            raise OSError("injected manifest close failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "open", select_open)
        scoped.setattr(os, "close", fail_close)
        with pytest.raises(LearnDeliveryError, match="manifest is unavailable"):
            _node_package_tree(cli_link)


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_package_tree_attempts_all_frame_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frame close error does not stop the remaining close attempts."""
    npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    real_open = os.open
    real_close = os.close
    targets: set[int] = set()
    attempted: set[int] = set()

    def select_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path) == npm_root or path == "globby":
            targets.add(descriptor)
        return descriptor

    def fail_close(descriptor: int) -> None:
        if descriptor in targets:
            attempted.add(descriptor)
            with suppress(OSError):
                real_close(descriptor)
            raise OSError("injected frame close failure")
        real_close(descriptor)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "open", select_open)
        scoped.setattr(os, "close", fail_close)
        with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
            _node_package_tree(cli_link)
    assert attempted == targets


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_node_package_tree_rejects_file_growth_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dependency that grows during its bounded read is rejected."""
    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    target = dependency_file.stat()
    real_read = os.read
    grew = False

    def grow_then_read(descriptor: int, size: int) -> bytes:
        nonlocal grew
        opened = os.fstat(descriptor)
        if not grew and (opened.st_dev, opened.st_ino) == (target.st_dev, target.st_ino):
            with dependency_file.open("ab") as stream:
                stream.write(b"changed during read\n")
            grew = True
        return real_read(descriptor, size)

    monkeypatch.setattr(os, "read", grow_then_read)

    with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
        _node_package_tree(cli_link)
    assert grew


@pytest.mark.parametrize("replacement", ["fifo", "symlink"])
@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_node_package_tree_rejects_file_replacement_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    """A special or linked replacement cannot enter the package digest."""
    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    parent = dependency_file.parent.stat()
    outside = tmp_path / "outside.js"
    outside.write_text("outside\n", encoding="utf-8")
    real_open = os.open
    replaced = False

    def replace_then_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if (
            not replaced
            and path == dependency_file.name
            and dir_fd is not None
            and (os.fstat(dir_fd).st_dev, os.fstat(dir_fd).st_ino) == (parent.st_dev, parent.st_ino)
        ):
            dependency_file.unlink()
            if replacement == "fifo":
                os.mkfifo(dependency_file)
            else:
                dependency_file.symlink_to(outside)
            replaced = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replace_then_open)

    with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
        _node_package_tree(cli_link)
    assert replaced


def test_node_package_tree_detects_dependency_mutation(tmp_path: Path) -> None:
    """A changed admitted dependency fails the bound tree check."""
    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    scope = _node_package_tree(cli_link)

    dependency_file.write_text("export const changed = true;\n")

    with pytest.raises(LearnDeliveryError):
        scope.verify()


def test_node_package_tree_detects_snapshot_mutation(tmp_path: Path) -> None:
    """A changed snapshot file fails the bound tree check."""
    npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    scope = _node_package_tree(cli_link)
    snapshot_dependency = scope.snapshot_root / dependency_file.relative_to(npm_root)
    snapshot_dependency.chmod(0o600)
    snapshot_dependency.write_text("changed snapshot\n", encoding="utf-8")

    try:
        with pytest.raises(LearnDeliveryError, match="dependency tree changed"):
            scope.verify()
    finally:
        with suppress(LearnDeliveryError):
            scope.close()
        _remove_test_tree(scope._snapshot_parent)


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


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_node_package_tree_resolves_link_hops_from_bound_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live-path ABA cannot change a raw link's internal target."""
    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    dependency = dependency_file.parent
    benign = dependency / "benign"
    attacker = dependency / "attacker"
    benign.mkdir()
    attacker.mkdir()
    (benign / "target.js").write_text("benign\n", encoding="utf-8")
    (attacker / "target.js").write_text("attacker\n", encoding="utf-8")
    alias = dependency / "alias"
    alias.symlink_to(benign.name, target_is_directory=True)
    entry = dependency / "entry-link.js"
    entry.symlink_to(f"{alias.name}/target.js")
    candidate = dependency / alias.name / "target.js"
    real_resolve = Path.resolve
    changed = False

    def redirect_once(path: Path, *args: Any, **kwargs: Any) -> Path:
        nonlocal changed
        if path == candidate:
            alias.unlink()
            alias.symlink_to(attacker.name, target_is_directory=True)
            try:
                resolved = real_resolve(path, *args, **kwargs)
            finally:
                alias.unlink()
                alias.symlink_to(benign.name, target_is_directory=True)
            changed = True
            return resolved
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirect_once)

    scope = _node_package_tree(cli_link)
    try:
        snapshot_entry = scope.snapshot_root / entry.relative_to(scope.root)
        assert snapshot_entry.resolve().read_text(encoding="utf-8") == "benign\n"
        assert not changed
    finally:
        scope.close()


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
