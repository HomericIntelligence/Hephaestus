"""Tests for bounded Node runtime reads in learning validation."""

from __future__ import annotations

import importlib
import json
import os
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any, TypedDict

import pytest

from hephaestus.automation.mnemosyne_delivery import LearnDeliveryError
from hephaestus.automation.mnemosyne_node_runtime import NodePackageTree, node_runtime_files

_POSIX_DESCRIPTOR_TEST = os.name == "posix" and all(
    hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
)


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


def test_node_package_tree_builds_private_immutable_snapshot(tmp_path: Path) -> None:
    """The admitted CLI and dependencies use one private snapshot."""
    npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
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


def test_failed_package_admission_removes_partial_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed admission removes its partially built private snapshot."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path, special_entry=True)
    temporary_root = Path(tempfile.gettempdir())
    before = set(temporary_root.glob("hephaestus-node-package-*"))
    real_chmod = os.chmod

    def reject_snapshot_chmod(path: Any, mode: int) -> None:
        if Path(path).name.startswith("hephaestus-node-package-"):
            raise OSError("injected cleanup traversal failure")
        real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", reject_snapshot_chmod)
    try:
        with pytest.raises(LearnDeliveryError, match="special entry"):
            _node_package_tree(cli_link)
        assert set(temporary_root.glob("hephaestus-node-package-*")) == before
    finally:
        monkeypatch.setattr(os, "chmod", real_chmod)
        for leftover in set(temporary_root.glob("hephaestus-node-package-*")) - before:
            runtime._remove_package_snapshot(leftover)


def test_node_package_tree_normalizes_snapshot_allocation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A temporary snapshot allocation error has one stable contract."""
    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)

    def fail_allocation(*_args: Any, **_kwargs: Any) -> str:
        raise OSError("injected snapshot allocation failure")

    monkeypatch.setattr(tempfile, "mkdtemp", fail_allocation)

    with pytest.raises(LearnDeliveryError, match="Node package dependency tree is unavailable"):
        _node_package_tree(cli_link)


def test_node_package_tree_preserves_snapshot_root_creation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root creation error stays primary when snapshot cleanup also fails."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    snapshot_parent = tmp_path / "private-snapshot"
    snapshot_parent.mkdir()
    snapshot_root = snapshot_parent / "node_modules"
    real_mkdir = Path.mkdir
    cleanup_calls: list[Path] = []

    def fail_root_creation(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == snapshot_root:
            raise OSError("injected snapshot root creation failure")
        real_mkdir(path, *args, **kwargs)

    def fail_cleanup(path: Path) -> None:
        cleanup_calls.append(path)
        raise LearnDeliveryError("Node package snapshot cleanup failed")

    monkeypatch.setattr(tempfile, "mkdtemp", lambda **_kwargs: str(snapshot_parent))
    monkeypatch.setattr(Path, "mkdir", fail_root_creation)
    monkeypatch.setattr(runtime, "_remove_package_snapshot", fail_cleanup)
    try:
        with pytest.raises(LearnDeliveryError, match="Node package dependency tree is unavailable"):
            _node_package_tree(cli_link)
    finally:
        snapshot_parent.rmdir()
    assert cleanup_calls == [snapshot_parent]


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
def test_node_package_tree_bounds_open_directories_on_wide_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wide valid tree keeps only its active directory depth open."""
    _npm_root, cli_link, _dependency_file = _npm_cli_fixture(tmp_path)
    for index in range(32):
        (cli_link.resolve().parent.parent / f"wide-{index:02d}").mkdir()
    real_open = os.open
    real_close = os.close
    directories: set[int] = set()
    peak = 0

    def track_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal peak
        descriptor = real_open(path, flags, *args, **kwargs)
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directories.add(descriptor)
            peak = max(peak, len(directories))
        return descriptor

    def track_close(descriptor: int) -> None:
        directories.discard(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "close", track_close)

    _node_package_tree(cli_link)

    assert peak <= 4
    assert directories == set()


@pytest.mark.skipif(not _POSIX_DESCRIPTOR_TEST, reason="POSIX descriptor boundary")
def test_node_package_tree_rejects_nested_directory_aba(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nested directory cannot leave and return during one tree read."""
    from hephaestus.automation import mnemosyne_node_runtime as runtime

    _npm_root, cli_link, dependency_file = _npm_cli_fixture(tmp_path)
    dependency = dependency_file.parent
    initial = dependency.stat()
    real_children = runtime._bounded_package_children
    changed = False

    def replace_and_restore(
        directory_descriptor: int,
        remaining_entries: int,
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
            children = real_children(directory_descriptor, remaining_entries, deadline)
            replacement.unlink()
            dependency.rmdir()
            detached.rename(dependency)
            changed = True
            return children
        return real_children(directory_descriptor, remaining_entries, deadline)

    monkeypatch.setattr(runtime, "_bounded_package_children", replace_and_restore)

    with pytest.raises(LearnDeliveryError, match="dependency tree is unavailable"):
        _node_package_tree(cli_link)
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
    real_freeze = runtime._freeze_package_snapshot
    real_digest = runtime._package_records_digest
    elapsed = 0.0
    digest_calls = 0

    def monotonic() -> float:
        return elapsed

    def finish_freeze(snapshot_root: Path, deadline: float) -> None:
        nonlocal elapsed
        real_freeze(snapshot_root, deadline)
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
    monkeypatch.setattr(runtime, "_freeze_package_snapshot", finish_freeze)
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
    real_open = os.open
    real_fstat = os.fstat
    real_close = os.close
    target_descriptor = -1
    closed: set[int] = set()

    def select_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal target_descriptor
        descriptor = real_open(path, flags, *args, **kwargs)
        if (failure_target == "root" and Path(path) == npm_root) or (
            failure_target == "child" and path == target_name
        ):
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
    real_open = os.open
    real_close = os.close
    package_descriptor = -1

    def select_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal package_descriptor
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path) == package:
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
