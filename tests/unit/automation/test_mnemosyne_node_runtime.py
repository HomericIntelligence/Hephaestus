"""Tests for bounded Node runtime reads in learning validation."""

from __future__ import annotations

import ctypes
import errno
import importlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path
from types import ModuleType, SimpleNamespace
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


def _observe_bound_link_native_reader(
    runtime: ModuleType,
    real_native: Callable[..., int],
    real_fstat: Callable[[int], os.stat_result],
    capability: Any,
    descriptor: int,
    buffer: Any,
    capacity: int,
    expected_identity: tuple[int, int, int],
    observations: list[dict[str, Any]],
    *,
    before_reader: Callable[[], None] | None = None,
) -> int:
    """Record one bound native link read and keep its native ABI intact."""
    opened = real_fstat(descriptor)
    if (opened.st_dev, opened.st_ino, opened.st_mode) != expected_identity:
        return real_native(capability, descriptor, buffer, capacity)
    observation: dict[str, Any] = {
        "abi": capability.abi,
        "descriptor": descriptor,
        "identity": (opened.st_dev, opened.st_ino, opened.st_mode),
        "capacity": capacity,
    }

    def observe_reader(*args: Any) -> int:
        observation["reader_args"] = args
        if before_reader is not None:
            before_reader()
        ctypes.set_errno(0)
        result = capability.reader(*args)
        observation["reader_errno"] = ctypes.get_errno()
        observation["return_count"] = result
        if isinstance(result, int) and 0 <= result <= capacity:
            observation["returned_bytes"] = bytes(buffer[:result])
        return result

    observed_capability = runtime._PackageLinkCapability(
        capability.abi,
        capability.open_flags,
        observe_reader,
        capability.maximum_target_bytes,
        capability.close_policy,
    )
    result = real_native(observed_capability, descriptor, buffer, capacity)
    observation["wrapper_result"] = result
    observations.append(observation)
    return result


def _assert_bound_link_native_observation(
    observation: dict[str, Any],
    expected_identity: tuple[int, int, int],
    expected_target: bytes,
    expected_capacity: int,
    *,
    allow_darwin_einval_after_replacement: bool,
) -> None:
    """Check one native link read without accepting attacker data."""
    abi = observation["abi"]
    assert abi in {"linux", "darwin"}
    assert observation["identity"] == expected_identity
    assert observation["capacity"] == expected_capacity
    result = observation["return_count"]
    assert observation["wrapper_result"] == result
    if result < 0:
        assert result == -1
        assert allow_darwin_einval_after_replacement
        assert abi == "darwin"
        assert observation["reader_errno"] == errno.EINVAL
        assert "returned_bytes" not in observation
    else:
        assert result == len(expected_target)
        assert observation["reader_errno"] == 0
        assert observation["returned_bytes"] == expected_target
    reader_args = observation["reader_args"]
    if abi == "linux":
        assert len(reader_args) == 4
        assert reader_args[0] == observation["descriptor"]
        assert reader_args[1] == b""
        assert ctypes.cast(reader_args[2], ctypes.c_void_p).value
        assert reader_args[3] == observation["capacity"]
        return
    assert len(reader_args) == 3
    assert reader_args[0] == observation["descriptor"]
    assert ctypes.cast(reader_args[1], ctypes.c_void_p).value
    assert reader_args[2] == observation["capacity"]


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
    monkeypatch.setattr(runtime, "_PACKAGE_LINK_CAPABILITY", None)
    monkeypatch.setattr(runtime, "_PACKAGE_LINK_CAPABILITY_INITIALIZED", False)

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

    def track_children(descriptor: int, budget: Any, deadline: float | None = None) -> list[Any]:
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
    ) -> list[Any]:
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
    ) -> list[Any]:
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


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_node_package_tree_rejects_directory_replacement_after_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory replacement after enumeration cannot enter the package snapshot."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    dependency = dependency_file.parent
    parent_identity = dependency.parent.stat()
    detached = dependency.with_name("globby-original")
    replacement_payload = "export const replacement = true;\n"
    replaced = False
    scope: NodePackageTree | None = None
    real_children = runtime._bounded_package_children

    def replace_after_enumeration(
        directory_descriptor: int,
        entry_budget: Any,
        deadline: float | None = None,
    ) -> list[Any]:
        nonlocal replaced
        children = real_children(directory_descriptor, entry_budget, deadline)
        opened = os.fstat(directory_descriptor)
        if not replaced and (opened.st_dev, opened.st_ino) == (
            parent_identity.st_dev,
            parent_identity.st_ino,
        ):
            dependency.rename(detached)
            dependency.mkdir()
            (dependency / "replacement.js").write_text(replacement_payload, encoding="utf-8")
            replaced = True
        return children

    monkeypatch.setattr(runtime, "_bounded_package_children", replace_after_enumeration)
    try:
        with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
            scope = _node_package_tree(cli_link)
    finally:
        if scope is not None:
            scope.close()
    assert replaced
    assert (dependency / "replacement.js").read_text(encoding="utf-8") == replacement_payload


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_node_package_tree_rejects_regular_file_replacement_after_descriptor_precondition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regular file replacement after descriptor binding cannot supply its bytes."""
    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    expected = dependency_file.stat()
    detached = dependency_file.with_name("index-original.js")
    replacement_payload = b"replacement bytes must not be read\n"
    replaced = False
    replacement_read = False
    real_read = os.read
    real_fstat = os.fstat

    def replace_before_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced, replacement_read
        opened = real_fstat(descriptor)
        if not replaced and (opened.st_dev, opened.st_ino) == (expected.st_dev, expected.st_ino):
            dependency_file.rename(detached)
            dependency_file.write_bytes(replacement_payload)
            replaced = True
        payload = real_read(descriptor, size)
        if replaced and replacement_payload in payload:
            replacement_read = True
        return payload

    monkeypatch.setattr(os, "read", replace_before_read)
    with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
        _node_package_tree(cli_link)
    assert replaced
    assert not replacement_read


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_bounded_regular_file_rejects_full_identity_change_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed regular-file mode fails before descriptor bytes are read."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    path = tmp_path / "entry.js"
    path.write_bytes(b"must not be read\n")
    path.chmod(0o644)
    parent = os.open(tmp_path, runtime._PACKAGE_DIRECTORY_FLAGS)
    metadata = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
    real_open = os.open
    real_fstat = os.fstat
    real_read = os.read
    file_descriptor: int | None = None
    changed = False
    read_calls = 0

    def select_open(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal file_descriptor
        descriptor = real_open(name, flags, *args, **kwargs)
        if name == path.name and kwargs.get("dir_fd") == parent:
            file_descriptor = descriptor
        return descriptor

    def change_before_fstat(descriptor: int) -> os.stat_result:
        nonlocal changed
        if descriptor == file_descriptor and not changed:
            path.chmod(0o600)
            changed = True
        return real_fstat(descriptor)

    def observe_read(descriptor: int, size: int) -> bytes:
        nonlocal read_calls
        if descriptor == file_descriptor:
            read_calls += 1
        return real_read(descriptor, size)

    try:
        monkeypatch.setattr(os, "open", select_open)
        monkeypatch.setattr(os, "fstat", change_before_fstat)
        monkeypatch.setattr(os, "read", observe_read)
        with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
            runtime._read_bounded_regular_file(
                parent,
                path.name,
                metadata,
                len(b"must not be read\n"),
                unavailable_message="Node package dependency tree is unavailable",
                too_large_message="Node package dependency tree is too large",
            )
    finally:
        os.close(parent)
    assert changed
    assert read_calls == 0


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_node_package_tree_rejects_entry_symlink_replacement_before_target_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry link replacement after binding cannot expose its attacker target."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    parent = dependency_file.parent
    parent_identity = parent.stat()
    entry = parent / "entry-link.js"
    entry.symlink_to(dependency_file.name)
    attacker_target = parent / "attacker-entry.js"
    attacker_target.write_text("attacker target\n", encoding="utf-8")
    entry_identity = entry.lstat()
    expected_entry_identity = (
        entry_identity.st_dev,
        entry_identity.st_ino,
        entry_identity.st_mode,
    )
    attacker_identity = attacker_target.stat()
    replaced = False
    capture_seen = False
    attacker_target_read = False
    attacker_target_resolved = False
    entry_dispatch_active = False
    entry_dispatch_calls = 0
    entry_resolution_active = False
    entry_resolution_calls = 0
    native_observations: list[dict[str, Any]] = []
    scope: NodePackageTree | None = None
    real_stat = os.stat
    real_fstat = os.fstat
    real_read = os.read
    real_native = runtime._package_link_native_call
    real_link_entry = runtime._link_package_entry
    real_resolve_link = runtime._resolve_package_link

    def replace_entry() -> None:
        nonlocal replaced
        entry.unlink()
        entry.symlink_to(attacker_target.name)
        replaced = True

    def capture_metadata(
        path: Any, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        nonlocal capture_seen, attacker_target_resolved
        metadata = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if (
            path == entry.name
            and dir_fd is not None
            and not follow_symlinks
            and (real_fstat(dir_fd).st_dev, real_fstat(dir_fd).st_ino)
            == (parent_identity.st_dev, parent_identity.st_ino)
        ):
            capture_seen = True
        if (
            entry_resolution_active
            and replaced
            and path == attacker_target.name
            and dir_fd is not None
            and (real_fstat(dir_fd).st_dev, real_fstat(dir_fd).st_ino)
            == (parent_identity.st_dev, parent_identity.st_ino)
        ):
            attacker_target_resolved = True
        return metadata

    def observe_entry_dispatch(*args: Any, **kwargs: Any) -> Any:
        nonlocal entry_dispatch_active, entry_dispatch_calls
        entry_dispatch_calls += 1
        entry_dispatch_active = True
        try:
            return real_link_entry(*args, **kwargs)
        finally:
            entry_dispatch_active = False

    def observe_link_resolution(*args: Any, **kwargs: Any) -> Any:
        nonlocal entry_resolution_active, entry_resolution_calls
        entry_resolution_calls += 1
        entry_resolution_active = True
        try:
            return real_resolve_link(*args, **kwargs)
        finally:
            entry_resolution_active = False

    def observe_native(capability: Any, descriptor: int, buffer: Any, capacity: int) -> int:
        before_reader = replace_entry if entry_dispatch_active and not replaced else None
        return _observe_bound_link_native_reader(
            runtime,
            real_native,
            real_fstat,
            capability,
            descriptor,
            buffer,
            capacity,
            expected_entry_identity,
            native_observations,
            before_reader=before_reader,
        )

    def observe_read(descriptor: int, size: int) -> bytes:
        nonlocal attacker_target_read
        payload = real_read(descriptor, size)
        opened = real_fstat(descriptor)
        is_attacker_target = (opened.st_dev, opened.st_ino) == (
            attacker_identity.st_dev,
            attacker_identity.st_ino,
        )
        if replaced and is_attacker_target and b"attacker target\n" in payload:
            attacker_target_read = True
        return payload

    monkeypatch.setattr(os, "stat", capture_metadata)
    monkeypatch.setattr(os, "read", observe_read)
    monkeypatch.setattr(runtime, "_link_package_entry", observe_entry_dispatch)
    monkeypatch.setattr(runtime, "_resolve_package_link", observe_link_resolution)
    monkeypatch.setattr(runtime, "_package_link_native_call", observe_native)
    try:
        with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
            scope = _node_package_tree(cli_link)
    finally:
        if scope is not None:
            scope.close()
    assert capture_seen
    assert replaced
    assert not attacker_target_read
    assert not attacker_target_resolved
    assert entry_dispatch_calls >= 1
    assert entry_resolution_calls >= 1
    assert native_observations
    expected_target = dependency_file.name.encode()
    for observation in native_observations:
        _assert_bound_link_native_observation(
            observation,
            expected_entry_identity,
            expected_target,
            min(256, runtime._MAX_PACKAGE_LINK_TARGET_BYTES + 1),
            allow_darwin_einval_after_replacement=replaced,
        )


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_node_package_tree_rejects_intermediate_symlink_replacement_before_target_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An intermediate link replacement after binding cannot redirect link resolution."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    parent = dependency_file.parent
    parent_identity = parent.stat()
    benign = parent / "benign"
    attacker = parent / "attacker"
    benign.mkdir()
    attacker.mkdir()
    (benign / "target.js").write_text("benign\n", encoding="utf-8")
    (attacker / "target.js").write_text("attacker\n", encoding="utf-8")
    alias = parent / "alias"
    alias.symlink_to(benign.name, target_is_directory=True)
    entry = parent / "entry-link.js"
    entry.symlink_to(f"{alias.name}/target.js")
    alias_identity = alias.lstat()
    expected_alias_identity = (
        alias_identity.st_dev,
        alias_identity.st_ino,
        alias_identity.st_mode,
    )
    attacker_identity = (attacker / "target.js").stat()
    replaced = False
    attacker_target_read = False
    attacker_target_resolved = False
    expanded_alias_active = False
    expanded_alias_calls = 0
    link_resolution_active = False
    link_resolution_calls = 0
    native_observations: list[dict[str, Any]] = []
    scope: NodePackageTree | None = None
    real_stat = os.stat
    real_fstat = os.fstat
    real_read = os.read
    real_native = runtime._package_link_native_call
    real_expanded_link = runtime._expanded_package_link
    real_resolve_link = runtime._resolve_package_link

    def replace_alias() -> None:
        nonlocal replaced
        alias.unlink()
        alias.symlink_to(attacker.name, target_is_directory=True)
        replaced = True

    def capture_metadata(
        path: Any, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        nonlocal attacker_target_resolved
        metadata = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if (
            link_resolution_active
            and replaced
            and path == attacker.name
            and dir_fd is not None
            and (real_fstat(dir_fd).st_dev, real_fstat(dir_fd).st_ino)
            == (parent_identity.st_dev, parent_identity.st_ino)
        ):
            attacker_target_resolved = True
        return metadata

    def observe_expanded_link(
        parent_descriptor: int,
        component: str,
        metadata: os.stat_result,
        prefix: tuple[str, ...],
        remainder: tuple[str, ...],
        deadline: float,
    ) -> tuple[str, ...]:
        nonlocal expanded_alias_active, expanded_alias_calls
        if component != alias.name:
            return real_expanded_link(
                parent_descriptor,
                component,
                metadata,
                prefix,
                remainder,
                deadline,
            )
        expanded_alias_calls += 1
        expanded_alias_active = True
        try:
            return real_expanded_link(
                parent_descriptor,
                component,
                metadata,
                prefix,
                remainder,
                deadline,
            )
        finally:
            expanded_alias_active = False

    def observe_link_resolution(*args: Any, **kwargs: Any) -> Any:
        nonlocal link_resolution_active, link_resolution_calls
        link_resolution_calls += 1
        link_resolution_active = True
        try:
            return real_resolve_link(*args, **kwargs)
        finally:
            link_resolution_active = False

    def observe_native(capability: Any, descriptor: int, buffer: Any, capacity: int) -> int:
        before_reader = replace_alias if expanded_alias_active and not replaced else None
        return _observe_bound_link_native_reader(
            runtime,
            real_native,
            real_fstat,
            capability,
            descriptor,
            buffer,
            capacity,
            expected_alias_identity,
            native_observations,
            before_reader=before_reader,
        )

    def observe_read(descriptor: int, size: int) -> bytes:
        nonlocal attacker_target_read
        payload = real_read(descriptor, size)
        opened = real_fstat(descriptor)
        is_attacker_target = (opened.st_dev, opened.st_ino) == (
            attacker_identity.st_dev,
            attacker_identity.st_ino,
        )
        if replaced and is_attacker_target and b"attacker\n" in payload:
            attacker_target_read = True
        return payload

    monkeypatch.setattr(os, "stat", capture_metadata)
    monkeypatch.setattr(os, "read", observe_read)
    monkeypatch.setattr(runtime, "_expanded_package_link", observe_expanded_link)
    monkeypatch.setattr(runtime, "_resolve_package_link", observe_link_resolution)
    monkeypatch.setattr(runtime, "_package_link_native_call", observe_native)
    try:
        with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
            scope = _node_package_tree(cli_link)
    finally:
        if scope is not None:
            scope.close()
    assert replaced
    assert not attacker_target_read
    assert not attacker_target_resolved
    assert expanded_alias_calls >= 1
    assert link_resolution_calls >= 1
    assert native_observations
    expected_target = benign.name.encode()
    for observation in native_observations:
        _assert_bound_link_native_observation(
            observation,
            expected_alias_identity,
            expected_target,
            min(256, runtime._MAX_PACKAGE_LINK_TARGET_BYTES + 1),
            allow_darwin_einval_after_replacement=replaced,
        )


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_node_package_tree_rejects_missing_native_link_capability_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing native link capability fails before target-read dispatch."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    native_calls = 0

    def missing_capability() -> None:
        return None

    def forbidden_native(*args: Any, **kwargs: Any) -> int:
        nonlocal native_calls
        native_calls += 1
        raise AssertionError("native link target read was dispatched")

    monkeypatch.setattr(runtime, "_package_link_capability", missing_capability)
    monkeypatch.setattr(runtime, "_package_link_native_call", forbidden_native)

    with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
        _node_package_tree(cli_link)
    assert native_calls == 0


def test_package_link_capability_caches_the_complete_native_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache one complete native link record for one platform resolution."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    resolutions = 0

    def reader(*_args: Any) -> int:
        return 0

    expected = runtime._PackageLinkCapability(
        "linux",
        os.O_RDONLY,
        reader,
        runtime._MAX_PACKAGE_LINK_TARGET_BYTES,
        runtime._PACKAGE_LINK_CLOSE_POLICY,
    )

    def resolve() -> Any:
        nonlocal resolutions
        resolutions += 1
        return expected

    monkeypatch.setattr(runtime, "_PACKAGE_LINK_CAPABILITY", None)
    monkeypatch.setattr(runtime, "_PACKAGE_LINK_CAPABILITY_INITIALIZED", False)
    monkeypatch.setattr(runtime, "_resolve_package_link_capability", resolve)

    assert runtime._package_link_capability() is expected
    assert runtime._package_link_capability() is expected
    assert resolutions == 1
    assert expected.maximum_target_bytes == runtime._MAX_PACKAGE_LINK_TARGET_BYTES
    assert expected.close_policy == runtime._PACKAGE_LINK_CLOSE_POLICY


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_incomplete_cached_link_capability_rejects_before_link_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject an incomplete cached link record before its descriptor opens."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    entry = tmp_path / "entry-link.js"
    entry.symlink_to("target.js")
    parent_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    expected = entry.lstat()
    link_open_calls = 0
    real_open = os.open

    incomplete = SimpleNamespace(
        abi="linux",
        open_flags=os.O_RDONLY,
        reader=lambda *_args: 0,
        maximum_target_bytes=runtime._MAX_PACKAGE_LINK_TARGET_BYTES,
        close_policy=None,
    )

    def forbid_link_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal link_open_calls
        if path == entry.name and dir_fd == parent_descriptor:
            link_open_calls += 1
            raise AssertionError("link descriptor opened with an incomplete capability")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(runtime, "_package_link_capability", lambda: incomplete)
    monkeypatch.setattr(os, "open", forbid_link_open)
    try:
        with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
            runtime._read_package_link_target(
                parent_descriptor,
                entry.name,
                expected,
                runtime._package_deadline(),
            )
    finally:
        os.close(parent_descriptor)
    assert link_open_calls == 0


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_node_package_tree_closes_link_descriptor_after_native_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A native link-read failure closes its descriptor before returning."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    parent = dependency_file.parent
    entry = parent / "entry-link.js"
    entry.symlink_to(dependency_file.name)
    expected = entry.lstat()
    expected_identity = (expected.st_dev, expected.st_ino, expected.st_mode)
    real_open = os.open
    real_close = os.close
    real_fstat = os.fstat
    real_native = runtime._package_link_native_call
    opened: set[int] = set()
    closed: set[int] = set()

    def track_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        actual = real_fstat(descriptor)
        if (actual.st_dev, actual.st_ino, actual.st_mode) == expected_identity:
            opened.add(descriptor)
        return descriptor

    def track_close(descriptor: int) -> None:
        if descriptor in opened:
            closed.add(descriptor)
        real_close(descriptor)

    def fail_for_entry(capability: Any, descriptor: int, buffer: Any, capacity: int) -> int:
        actual = real_fstat(descriptor)
        if (actual.st_dev, actual.st_ino, actual.st_mode) == expected_identity:
            raise OSError("injected native link-read failure")
        return real_native(capability, descriptor, buffer, capacity)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "close", track_close)
    monkeypatch.setattr(runtime, "_package_link_native_call", fail_for_entry)

    with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
        _node_package_tree(cli_link)
    assert opened
    assert opened <= closed
