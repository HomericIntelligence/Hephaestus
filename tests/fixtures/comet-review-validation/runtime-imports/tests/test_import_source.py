"""Check imports in the selected command and its child process."""

import json
import os
import subprocess
import sys
from pathlib import Path

import comet


def test_reviewed_imports():
    """Require both processes to import the reviewed source."""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import comet,json; print(json.dumps({'value':comet.QUALIFICATION_VALUE,"
            "'path':comet.__file__}))",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    proof = {
        "main": {"value": comet.QUALIFICATION_VALUE, "path": comet.__file__},
        "child": json.loads(child.stdout),
    }
    output = Path(os.environ["HOME"]).parent / "import-proof.json"
    output.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
    for record in proof.values():
        assert record["value"] == "reviewed-source"
        assert (
            Path(record["path"]).resolve()
            == Path(__file__).resolve().parents[1] / "src/comet/__init__.py"
        )
