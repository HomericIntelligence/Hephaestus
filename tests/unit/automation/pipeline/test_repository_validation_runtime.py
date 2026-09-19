"""Check the bounded manifest contract for a sealed Comet runtime."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from types import ModuleType
from typing import Any

import pytest

PROJECT = "a" * 64
LOCK = "b" * 64
CAP = 16_777_216


def _api() -> ModuleType:
    name = "hephaestus.automation.repository_validation_runtime"
    assert importlib.util.find_spec(name) is not None, "Runtime admission is not available."
    return importlib.import_module(name)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _entry(path: str, *, size: int = 0, mode: int = 0o444) -> dict[str, Any]:
    return {"path": path, "size": size, "mode": mode, "sha256": hashlib.sha256(b"").hexdigest()}


def _manifest(
    extra: list[dict[str, Any]] | None = None, *, project: str = PROJECT, lock: str = LOCK
) -> dict[str, Any]:
    entries = [
        _entry(f"bin/{name}", mode=0o555) for name in ("uv", "python", "ruff", "ty", "mkdocs")
    ]
    entries.extend(extra or [])
    entries.sort(key=lambda entry: entry["path"])
    return {
        "schema": "hephaestus-comet-review-runtime-v1",
        "profile": "comet",
        "repository": "llm360/comet",
        "pyproject_sha256": project,
        "uv_lock_sha256": lock,
        "python_version": "3.12",
        "uv_version": "0.12.7",
        "entries": entries,
        "tree_sha256": hashlib.sha256(_canonical(entries)).hexdigest(),
    }


def _encode(manifest: dict[str, Any]) -> bytes:
    manifest["tree_sha256"] = hashlib.sha256(_canonical(manifest["entries"])).hexdigest()
    return _canonical(manifest)


def _parse(raw: bytes) -> Any:
    return _api().parse_runtime_manifest(raw, pyproject_sha256=PROJECT, uv_lock_sha256=LOCK)


def test_runtime_manifest_accepts_bound_inventory() -> None:
    """Keep the admitted inventory immutable and bind its exact bytes."""
    raw = _encode(_manifest([_entry("lib/python3.12/site-packages/example.py")]))
    result = _parse(raw)
    assert result.manifest_sha256 == hashlib.sha256(raw).hexdigest()
    assert result.tree_sha256 == json.loads(raw)["tree_sha256"]
    assert tuple(entry.path for entry in result.entries) == tuple(
        entry["path"] for entry in json.loads(raw)["entries"]
    )
    with pytest.raises(AttributeError):
        result.entries = ()


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "other"),
        ("profile", "other"),
        ("repository", "other/comet"),
        ("python_version", "3.13"),
        ("uv_version", "0.12.8"),
        ("pyproject_sha256", "c" * 64),
        ("uv_lock_sha256", "c" * 64),
        ("extra", 1),
    ],
)
def test_runtime_manifest_rejects_unbound_schema(field: str, value: Any) -> None:
    """Reject a schema or dependency identity outside the admitted profile."""
    data = _manifest()
    data[field] = value
    with pytest.raises(ValueError):
        _parse(_encode(data))


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "a//b",
        "a/./b",
        "a/../b",
        "a\\b",
        "a\x00b",
        "",
        "lib/python3.12/site-packages/unsafe.pth",
        "lib/python3.12/sitecustomize.py",
        "lib/python3.12/usercustomize/__init__.py",
        "lib/python3.12/__pycache__/sitecustomize.cpython-312.pyc",
        "lib/python3.12/sitecustomize.cpython-312-darwin.so",
        "lib/python3.12/usercustomize.pyd",
    ],
)
def test_runtime_manifest_rejects_unsafe_paths_and_startup_hooks(path: str) -> None:
    """Reject paths that escape the runtime or can change Python startup."""
    with pytest.raises(ValueError):
        _parse(_encode(_manifest([_entry(path)])))


@pytest.mark.parametrize(
    "field,value",
    [
        ("size", -1),
        ("size", True),
        ("size", 1_073_741_825),
        ("mode", 0o644),
        ("mode", True),
        ("mode", 0o4555),
        ("sha256", "A" * 64),
        ("sha256", "a" * 63),
        ("extra", 1),
    ],
)
def test_runtime_manifest_rejects_invalid_file_metadata(field: str, value: Any) -> None:
    """Reject file metadata outside the sealed runtime contract."""
    entry = _entry("lib/example.py")
    entry[field] = value
    with pytest.raises(ValueError):
        _parse(_encode(_manifest([entry])))


@pytest.mark.parametrize(
    "fault", ["duplicate", "unsorted", "missing_executable", "nonexecutable", "tree_digest"]
)
def test_runtime_manifest_requires_complete_ordered_inventory(fault: str) -> None:
    """Require each executable and one sorted entry for each path."""
    data = _manifest()
    if fault == "duplicate":
        data["entries"].append(dict(data["entries"][-1]))
    elif fault == "unsorted":
        data["entries"].reverse()
    elif fault == "missing_executable":
        data["entries"].pop()
    elif fault == "nonexecutable":
        data["entries"][0]["mode"] = 0o444
    raw = _encode(data)
    if fault == "tree_digest":
        data["tree_sha256"] = "0" * 64
        raw = _canonical(data)
    with pytest.raises(ValueError):
        _parse(raw)


@pytest.mark.parametrize("fault", ["duplicate_key", "whitespace", "newline", "invalid_utf8", "nan"])
def test_runtime_manifest_requires_canonical_utf8_json(fault: str) -> None:
    """Reject ambiguous, noncanonical, and malformed JSON encodings."""
    raw = _encode(_manifest())
    if fault == "duplicate_key":
        raw = b'{"profile":"comet",' + raw[1:]
    elif fault == "whitespace":
        raw = b" " + raw
    elif fault == "newline":
        raw += b"\n"
    elif fault == "invalid_utf8":
        raw = b"\xff"
    else:
        raw = raw.replace(b'"size":0', b'"size":NaN', 1)
    with pytest.raises(ValueError):
        _parse(raw)


@pytest.mark.parametrize("count,accepted", [(50_000, True), (50_001, False)])
def test_runtime_manifest_file_count_limit(count: int, accepted: bool) -> None:
    """Accept the maximum file count and reject one additional file."""
    raw = _encode(_manifest([_entry(f"lib/file-{index:05}.py") for index in range(count - 5)]))
    if accepted:
        assert len(_parse(raw).entries) == count
    else:
        with pytest.raises(ValueError):
            _parse(raw)


@pytest.mark.parametrize("extra_byte,accepted", [(0, True), (1, False)])
def test_runtime_manifest_aggregate_size_limit(extra_byte: int, accepted: bool) -> None:
    """Enforce the aggregate byte limit independently of each file size."""
    entries = [_entry(f"lib/file-{index}", size=1_073_741_824) for index in range(8)]
    entries.append(_entry("lib/last", size=extra_byte))
    raw = _encode(_manifest(entries))
    if accepted:
        assert sum(entry.size for entry in _parse(raw).entries) == 8_589_934_592
    else:
        with pytest.raises(ValueError):
            _parse(raw)


@pytest.mark.parametrize("length,accepted", [(4096, True), (4097, False)])
def test_runtime_manifest_path_byte_limit(length: int, accepted: bool) -> None:
    """Measure path length in UTF-8 bytes."""
    path = "é" * (length // 2) + ("x" if length % 2 else "")
    raw = _encode(_manifest([_entry(path)]))
    if accepted:
        assert _parse(raw).entries[-1].path == path
    else:
        with pytest.raises(ValueError):
            _parse(raw)


def test_runtime_manifest_exact_byte_cap_and_excess() -> None:
    """Accept the exact manifest byte limit and reject one additional byte."""
    data = _manifest([_entry(f"lib/{index:04}/" + "a" * 3890) for index in range(4000)])
    remaining = CAP - len(_encode(data))
    assert remaining > 0
    for entry in data["entries"]:
        if entry["path"].startswith("lib/"):
            added = min(4096 - len(entry["path"]), remaining)
            entry["path"] += "a" * added
            remaining -= added
    assert remaining == 0
    raw = _encode(data)
    assert len(raw) == CAP
    assert _parse(raw).manifest_sha256 == hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError):
        _parse(raw + b" ")


def _sealed_runtime(
    tmp_path: Any, *, project: str = PROJECT, lock: str = LOCK
) -> tuple[Any, Any, dict[str, Any]]:
    """Make a sealed file fixture without claiming an executable environment."""
    trusted = tmp_path.resolve() / "host"
    parent = trusted / "build/hephaestus-review-validation/comet"
    root = parent / lock
    environment = root / "environment"
    data = _manifest([_entry("lib/example.py")], project=project, lock=lock)
    content = b"runtime fixture"
    for entry in data["entries"]:
        path = environment / entry["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(entry["mode"])
        entry["size"] = len(content)
        entry["sha256"] = hashlib.sha256(content).hexdigest()
    (root / "runtime-manifest.json").write_bytes(_encode(data))
    (root / "runtime-manifest.json").chmod(0o400)
    for path in sorted(environment.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(0o555)
    environment.chmod(0o555)
    root.chmod(0o500)
    parent.chmod(0o700)
    return trusted, root, data


def _admit(trusted: Any, **kwargs: Any) -> Any:
    api = _api()
    assert hasattr(api, "admit_runtime"), "Runtime file admission is not available."
    return api.admit_runtime(trusted, pyproject_sha256=PROJECT, uv_lock_sha256=LOCK, **kwargs)


def test_runtime_admission_verifies_complete_sealed_files(tmp_path: Any) -> None:
    """Bind the admitted root and the exact manifest to its regular files."""
    trusted, root, data = _sealed_runtime(tmp_path)
    result = _admit(trusted)
    assert result.root == root
    assert result.manifest.tree_sha256 == data["tree_sha256"]
    assert (
        result.manifest.manifest_sha256
        == hashlib.sha256((root / "runtime-manifest.json").read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    "fault",
    [
        "parent_mode",
        "root_mode",
        "directory_mode",
        "manifest_mode",
        "file_mode",
        "missing",
        "unlisted",
        "digest",
        "size",
        "symlink",
        "hardlink",
        "directory_symlink",
        "ancestor_symlink",
    ],
)
def test_runtime_admission_rejects_unsealed_or_changed_files(tmp_path: Any, fault: str) -> None:
    """Reject modified files, incomplete inventories, links, and writable paths."""
    import os

    trusted, root, _ = _sealed_runtime(tmp_path)
    environment = root / "environment"
    target = environment / "lib/example.py"
    if fault == "parent_mode":
        root.parent.chmod(0o755)
    elif fault == "root_mode":
        root.chmod(0o700)
    elif fault == "directory_mode":
        target.parent.chmod(0o755)
    elif fault == "manifest_mode":
        (root / "runtime-manifest.json").chmod(0o600)
    elif fault == "file_mode":
        target.chmod(0o644)
    elif fault in {"digest", "size"}:
        target.chmod(0o644)
        target.write_bytes(b"changed fixture" if fault == "digest" else b"short")
        target.chmod(0o444)
    elif fault == "ancestor_symlink":
        alias = tmp_path.resolve() / "alias"
        alias.symlink_to(trusted, target_is_directory=True)
        trusted = alias
    elif fault == "directory_symlink":
        root.chmod(0o700)
        environment.rename(root / "moved")
        environment.symlink_to(root / "moved", target_is_directory=True)
        root.chmod(0o500)
    else:
        target.parent.chmod(0o755)
        if fault == "missing":
            target.unlink()
        elif fault == "unlisted":
            extra = target.parent / "extra.py"
            extra.write_bytes(b"")
            extra.chmod(0o444)
        elif fault == "symlink":
            target.unlink()
            target.symlink_to(environment / "bin/python")
        else:
            os.link(target, tmp_path / "alias-file")
        target.parent.chmod(0o555)
    with pytest.raises(ValueError):
        _admit(trusted)


def test_runtime_admission_rechecks_each_call(tmp_path: Any) -> None:
    """Do not reuse a prior admission after a runtime file changes."""
    trusted, root, _ = _sealed_runtime(tmp_path)
    _admit(trusted)
    target = root / "environment/lib/example.py"
    target.chmod(0o644)
    target.write_bytes(b"changed fixture")
    target.chmod(0o444)
    with pytest.raises(ValueError):
        _admit(trusted)


def test_runtime_admission_checks_owner(tmp_path: Any, monkeypatch: Any) -> None:
    """Require runtime ownership by the effective user."""
    import os

    trusted, _, _ = _sealed_runtime(tmp_path)
    uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: uid + 1)
    with pytest.raises(ValueError):
        _admit(trusted)


def test_runtime_admission_bounds_manifest_before_parsing(tmp_path: Any, monkeypatch: Any) -> None:
    """Reject an oversized manifest before JSON parsing."""
    trusted, root, _ = _sealed_runtime(tmp_path)
    manifest = root / "runtime-manifest.json"
    manifest.chmod(0o600)
    manifest.write_bytes(b"{" * (CAP + 1))
    manifest.chmod(0o400)

    def unexpected_parse(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("An oversized manifest must not reach the parser.")

    monkeypatch.setattr(_api(), "parse_runtime_manifest", unexpected_parse)
    with pytest.raises(ValueError):
        _admit(trusted)


def test_runtime_admission_honors_cancellation(tmp_path: Any) -> None:
    """Stop before reading runtime files when cancellation is set."""
    import threading

    event = threading.Event()
    event.set()
    with pytest.raises(InterruptedError):
        _admit(tmp_path, shutdown=event)


def test_runtime_admission_honors_deadline(tmp_path: Any) -> None:
    """Stop before file reads when the admission deadline has elapsed."""
    from unittest.mock import patch

    with patch("time.monotonic", side_effect=(0.0, 121.0)):
        with pytest.raises(TimeoutError):
            _admit(tmp_path)


def _install_archive(
    root: Any, data: dict[str, Any], content: bytes, name: str = "python312.zip"
) -> None:
    """Add an archive and update the sealed fixture inventory."""
    archive = root / "environment/lib" / name
    archive.parent.chmod(0o755)
    archive.write_bytes(content)
    archive.chmod(0o444)
    archive.parent.chmod(0o555)
    data["entries"].append(
        {
            "path": f"lib/{name}",
            "size": len(content),
            "mode": 0o444,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    )
    data["entries"].sort(key=lambda entry: entry["path"])
    manifest = root / "runtime-manifest.json"
    manifest.chmod(0o600)
    manifest.write_bytes(_encode(data))
    manifest.chmod(0o400)


def _archive(member: str) -> bytes:
    """Make a small import archive with one regular module."""
    import io
    import zipfile

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr(member, b"VALUE = 1\n")
    return stream.getvalue()


@pytest.mark.parametrize(
    "member,accepted",
    [
        ("encodings/__init__.py", True),
        ("sitecustomize.py", False),
        ("usercustomize/__init__.py", False),
        ("sitecustomize.pyc", False),
        ("usercustomize.cpython-312-darwin.so", False),
    ],
)
@pytest.mark.parametrize("archive_name", ["python312.zip", "python312.ZIP", "dependencies.EGG"])
def test_runtime_admission_inspects_python_import_archives(
    tmp_path: Any, member: str, accepted: bool, archive_name: str
) -> None:
    """Reject startup modules inside an otherwise sealed Python import archive."""
    trusted, root, data = _sealed_runtime(tmp_path)
    _install_archive(root, data, _archive(member), archive_name)
    if accepted:
        assert _admit(trusted).manifest.tree_sha256 == data["tree_sha256"]
    else:
        with pytest.raises(ValueError):
            _admit(trusted)


@pytest.mark.parametrize("fault", ["count", "directory_size", "hidden_records", "zip64"])
def test_runtime_admission_bounds_archives_before_zip_reader(
    tmp_path: Any, monkeypatch: Any, fault: str
) -> None:
    """Reject excessive or inconsistent archive metadata before allocation."""
    import struct
    import zipfile

    raw = bytearray(_archive("encodings/__init__.py"))
    end = raw.rfind(b"PK\x05\x06")
    if fault == "count":
        struct.pack_into("<HH", raw, end + 8, 50_001, 50_001)
    elif fault == "directory_size":
        struct.pack_into("<L", raw, end + 12, CAP + 1)
    elif fault == "hidden_records":
        struct.pack_into("<HH", raw, end + 8, 0, 0)
    else:
        raw[end:end] = b"PK\x06\x07" + b"\0" * 16
    trusted, root, data = _sealed_runtime(tmp_path)
    _install_archive(root, data, bytes(raw))

    def unexpected_reader(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Invalid archive metadata must not reach the ZIP reader.")

    monkeypatch.setattr(zipfile, "ZipFile", unexpected_reader)
    with pytest.raises(ValueError):
        _admit(trusted)


def _execution_runtime(tmp_path: Any) -> tuple[Any, Any]:
    """Bind current-profile metadata to a sealed file fixture."""
    from dataclasses import replace

    from tests.unit.automation.pipeline.test_jobs import _repository_execution

    execution = _repository_execution(tmp_path / "checkout")
    sources = {path: digest for path, _, digest in execution.plan.checks[0].source_digests}
    trusted, root, data = _sealed_runtime(
        tmp_path / "capability", project=sources["pyproject.toml"], lock=sources["uv.lock"]
    )
    execution = replace(
        execution,
        runtime_root=root,
        runtime_manifest_sha256=hashlib.sha256(
            (root / "runtime-manifest.json").read_bytes()
        ).hexdigest(),
        runtime_tree_sha256=data["tree_sha256"],
    )
    return trusted, execution


def _admit_execution(trusted: Any, execution: Any) -> Any:
    api = _api()
    assert hasattr(api, "admit_execution_runtime"), "Execution runtime admission is not available."
    return api.admit_execution_runtime(execution, trusted_root=trusted)


def test_execution_runtime_admission_binds_profile_and_fixed_host_root(tmp_path: Any) -> None:
    """Admit the exact runtime identities without starting a subprocess."""
    from unittest.mock import patch

    trusted, execution = _execution_runtime(tmp_path)
    with patch("subprocess.Popen", side_effect=AssertionError("No subprocess is allowed.")):
        result = _admit_execution(trusted, execution)
    assert result.root == execution.runtime_root
    assert result.manifest.manifest_sha256 == execution.runtime_manifest_sha256
    assert result.manifest.tree_sha256 == execution.runtime_tree_sha256


@pytest.mark.parametrize(
    "fault", ["host_root", "profile", "command", "manifest_digest", "tree_digest", "nonce"]
)
def test_execution_runtime_admission_rejects_unbound_metadata(tmp_path: Any, fault: str) -> None:
    """Reject foreign runtime roots, altered commands, and stale identities."""
    from dataclasses import replace
    from unittest.mock import patch

    trusted, execution = _execution_runtime(tmp_path)
    if fault == "host_root":
        trusted = tmp_path / "other-host"
    elif fault == "profile":
        execution = replace(execution, plan=replace(execution.plan, profile_digest="0" * 64))
    elif fault == "command":
        check = replace(execution.plan.checks[0], argv=("uv", "run", "other"))
        execution = replace(
            execution, plan=replace(execution.plan, checks=(check, *execution.plan.checks[1:]))
        )
    elif fault == "manifest_digest":
        execution = replace(execution, runtime_manifest_sha256="0" * 64)
    elif fault == "tree_digest":
        execution = replace(execution, runtime_tree_sha256="0" * 64)
    else:
        object.__setattr__(execution, "request_nonce", "invalid")
    with patch("subprocess.Popen", side_effect=AssertionError("No subprocess is allowed.")):
        with pytest.raises(ValueError):
            _admit_execution(trusted, execution)


def test_execution_runtime_admission_rejects_network_dependent_check(tmp_path: Any) -> None:
    """Keep the validator that downloads actionlint unavailable to local execution."""
    from dataclasses import replace
    from unittest.mock import patch

    from tests.unit.automation.test_pipeline_github_review_validation import _ci_collection_fixture

    trusted, execution = _execution_runtime(tmp_path)
    invocation, _ = _ci_collection_fixture(
        tmp_path / "checkout", (("M", "scripts/deployed_artifact_validators.py"),)
    )
    check_id = "comet.contract.workflow-contracts"
    assert check_id in {check.check_id for check in invocation.plan.checks}
    execution = replace(execution, plan=invocation.plan, check_id=check_id)
    with patch("subprocess.Popen", side_effect=AssertionError("No subprocess is allowed.")):
        with pytest.raises(ValueError):
            _admit_execution(trusted, execution)


@pytest.mark.parametrize("reviewed_imports", [True, False], ids=["reviewed", "negative-control"])
def test_real_runtime_fixed_command_imports_reviewed_source(
    tmp_path: Any, reviewed_imports: bool
) -> None:
    """Qualify the optional real capability without substituting a dummy runtime.

    This test uses a small source fixture and the fixed PR command. It proves
    runtime admission and import precedence, not the worker isolation boundary.
    The normal unit suite can run without this optional host capability.
    """
    import shutil
    import subprocess
    from pathlib import Path

    from hephaestus.automation.pipeline.worker_pool import (
        _host_verification_env,
        _repository_validation_environment,
        _seal_host_runtime,
    )
    from hephaestus.automation.pipeline_github_review_validation import (
        COMET_PROFILE_ID,
        comet_validation_checks,
    )

    root = Path(__file__).resolve().parents[4]
    capability = root / "build/hephaestus-review-validation/comet"
    if not capability.exists():
        pytest.skip("The optional real Comet runtime is not prepared on this host.")
    check = next(
        check
        for check in comet_validation_checks(COMET_PROFILE_ID, (("M", "tests/test_pool.py"),))
        if check.check_id == "comet.python.pr-tests"
    )
    sources = {path: digest for path, _, digest in check.source_digests}
    runtime = _api().admit_runtime(
        root, pyproject_sha256=sources["pyproject.toml"], uv_lock_sha256=sources["uv.lock"]
    )
    fixture = root / "tests/fixtures/comet-review-validation"
    source, scratch = tmp_path / "source", tmp_path / "scratch"
    shutil.copytree(fixture / "runtime-imports", source)
    scratch.mkdir()
    for name in ("pyproject.toml", "uv.lock", "scripts/ci/run-test-tier.py"):
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(fixture / "current" / name, target)
    _seal_host_runtime(source)
    executable = runtime.root / "environment/bin/uv"
    environment = _host_verification_env(scratch, str(executable), runtime.root / "environment")
    dependencies = [
        "typer",
        "pydantic",
        "jinja2",
        "httpx",
        "grpc",
        "grpc_health",
        "yaml",
        "fastapi",
        "uvicorn",
        "uvloop",
        "asyncpg",
        "huggingface_hub",
        "pytest",
        "pytest_asyncio",
        "pytest_cov",
        "jsonschema",
        "mkdocs",
        "pymdownx",
    ]
    probe = subprocess.run(
        [
            str(runtime.root / "environment/bin/python"),
            "-c",
            "import importlib,json,sys; "
            "[importlib.import_module(name) for name in json.loads(sys.argv[1])]; "
            "import comet; print(comet.QUALIFICATION_VALUE)",
            json.dumps(dependencies),
        ],
        cwd=scratch,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "installed-old"
    if reviewed_imports:
        environment = _repository_validation_environment(environment, source, runtime)
    result = subprocess.run(
        [str(executable), *check.argv[1:]],
        cwd=source,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == (0 if reviewed_imports else 1), result.stdout + result.stderr
    proof = json.loads((scratch / "import-proof.json").read_text(encoding="utf-8"))
    expected = "reviewed-source" if reviewed_imports else "installed-old"
    assert proof["main"]["value"] == proof["child"]["value"] == expected
    assert (
        _api().admit_runtime(
            root, pyproject_sha256=sources["pyproject.toml"], uv_lock_sha256=sources["uv.lock"]
        )
        == runtime
    )
