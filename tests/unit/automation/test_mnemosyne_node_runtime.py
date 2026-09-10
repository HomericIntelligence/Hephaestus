"""Tests for bounded Node runtime reads in learning validation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hephaestus.automation.mnemosyne_delivery import LearnDeliveryError
from hephaestus.automation.mnemosyne_node_runtime import node_runtime_files


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
