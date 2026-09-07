"""Behavior tests for detached Codex adapter deployment admission."""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from hephaestus.agents.codex_isolation import CodexIsolationError


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _regular(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _deployment(
    tmp_path: Path,
    *,
    build_installed_wheel: bool = False,
) -> tuple[Path, str, dict[str, Any]]:
    root = tmp_path / "offline"
    root.mkdir(mode=0o700)
    installed = root / "installed"
    if build_installed_wheel:
        source = root / "source"
        _regular(
            source / "pyproject.toml",
            (
                b"[build-system]\n"
                b"requires = ['setuptools']\n"
                b"build-backend = 'setuptools.build_meta'\n\n"
                b"[project]\n"
                b"name = 'example-adapter'\n"
                b"version = '1.0'\n\n"
                b"[project.entry-points.'hephaestus.codex_isolation_adapters']\n"
                b"production = 'example_adapter:factory'\n"
            ),
        )
        _regular(
            source / "example_adapter" / "__init__.py",
            b"def factory():\n    return 'locked'\n",
        )
        wheel_dir = root / "dist"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(wheel_dir),
            ],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        )
        wheel = next(wheel_dir.glob("example_adapter-1.0-*.whl"))
        subprocess.run(
            [
                shutil.which("uv") or "uv",
                "pip",
                "install",
                "--target",
                str(installed),
                "--no-deps",
                str(wheel),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        (installed / ".lock").unlink(missing_ok=True)
    else:
        _regular(
            installed / "example_adapter" / "__init__.py",
            b"def factory():\n    return 'locked'\n",
        )
        _regular(
            installed / "example_adapter-1.0.dist-info" / "METADATA",
            b"Name: example-adapter\nVersion: 1.0\n",
        )
        _regular(
            installed / "example_adapter-1.0.dist-info" / "entry_points.txt",
            b"[hephaestus.codex_isolation_adapters]\nproduction = example_adapter:factory\n",
        )
        _regular(
            installed / "example_adapter-1.0.dist-info" / "RECORD",
            (
                b"example_adapter/__init__.py,,\n"
                b"example_adapter-1.0.dist-info/METADATA,,\n"
                b"example_adapter-1.0.dist-info/entry_points.txt,,\n"
                b"example_adapter-1.0.dist-info/RECORD,,\n"
            ),
        )
        wheel = _regular(root / "example_adapter-1.0-py3-none-any.whl", b"wheel")
    manifest = []
    for path in sorted(
        (item for item in installed.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(installed).as_posix(),
    ):
        manifest.append(
            {
                "path": path.relative_to(installed).as_posix(),
                "sha256": _sha256(path),
                "size": path.stat().st_size,
                "mode": path.stat().st_mode & 0o777,
            }
        )

    artifacts = {
        "wheel": wheel,
        "guest_image": _regular(root / "guest.raw", b"signed guest"),
        "codex_archive": _regular(root / "codex.zst", b"archive"),
        "sigstore_bundle": _regular(root / "codex.sigstore", b"bundle"),
        "extracted_elf": _regular(
            root / "codex",
            b"\x7fELF\x02\x01" + b"\0" * 12 + b"\xb7\0" + b"\0" * 44,
        ),
        "trusted_root": _regular(root / "trusted-root.json", b"trusted root"),
        "rekor_key": _regular(root / "rekor.pub", b"rekor key"),
        "rekor_checkpoint": _regular(root / "rekor.checkpoint", b"checkpoint"),
        "rekor_inclusion_proof": _regular(root / "rekor.proof", b"proof"),
    }
    payload: dict[str, Any] = {
        "schema_version": 1,
        "adapter_distribution": "example-adapter",
        "adapter_version": "1.0",
        "wheel_path": str(artifacts["wheel"]),
        "wheel_sha256": _sha256(artifacts["wheel"]),
        "wheel_tags": ["py3", "none", "any"],
        "installed_tree_root": str(installed),
        "installed_tree_manifest": manifest,
        "installed_tree_sha256": hashlib.sha256(_canonical(manifest)).hexdigest(),
        "entry_point_group": "hephaestus.codex_isolation_adapters",
        "entry_point_name": "production",
        "entry_point_module": "example_adapter",
        "entry_point_factory": "factory",
        "adapter_api_version": 1,
        "request_schema_version": 1,
        "prepared_schema_version": 1,
        "result_schema_version": 1,
        "guest_image_path": str(artifacts["guest_image"]),
        "guest_image_format": "raw",
        "guest_image_platform": "aarch64-linux",
        "guest_image_sha256": _sha256(artifacts["guest_image"]),
        "codex_archive_path": str(artifacts["codex_archive"]),
        "codex_archive_sha256": _sha256(artifacts["codex_archive"]),
        "sigstore_bundle_path": str(artifacts["sigstore_bundle"]),
        "sigstore_bundle_sha256": _sha256(artifacts["sigstore_bundle"]),
        "extracted_elf_path": str(artifacts["extracted_elf"]),
        "extracted_elf_sha256": _sha256(artifacts["extracted_elf"]),
        "codex_release_tag": "rust-v0.153.4",
        "codex_archive_asset": "codex-aarch64-unknown-linux-musl.zst",
        "codex_sigstore_asset": "codex-aarch64-unknown-linux-musl.sigstore",
        "codex_target": "aarch64-unknown-linux-musl",
        "fulcio_certificate_issuer": "O=sigstore.dev, CN=sigstore-intermediate",
        "workflow_certificate_identity": "https://github.com/openai/codex/.github/workflows/rust-release.yml@refs/tags/rust-v0.153.4",
        "oidc_issuer": "https://token.actions.githubusercontent.com",
        "trusted_root_path": str(artifacts["trusted_root"]),
        "trusted_root_sha256": _sha256(artifacts["trusted_root"]),
        "rekor_public_key_path": str(artifacts["rekor_key"]),
        "rekor_public_key_sha256": _sha256(artifacts["rekor_key"]),
        "rekor_checkpoint_path": str(artifacts["rekor_checkpoint"]),
        "rekor_checkpoint_sha256": _sha256(artifacts["rekor_checkpoint"]),
        "rekor_inclusion_proof_path": str(artifacts["rekor_inclusion_proof"]),
        "rekor_inclusion_proof_sha256": _sha256(artifacts["rekor_inclusion_proof"]),
        "rekor_log_id": "a" * 64,
        "rekor_log_index": 1,
        "rekor_integration_time": 1,
    }
    lock = root / "deployment-lock.json"
    lock.write_bytes(_canonical(payload))
    lock.chmod(0o600)
    return lock, _sha256(lock), payload


def _refresh_installed_tree_lock(lock: Path, payload: dict[str, Any]) -> str:
    """Refresh fixture manifest values after a test changes installed bytes."""
    root = Path(payload["installed_tree_root"])
    manifest = []
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        manifest.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": _sha256(path),
                "size": path.stat().st_size,
                "mode": path.stat().st_mode & 0o777,
            }
        )
    payload["installed_tree_manifest"] = manifest
    payload["installed_tree_sha256"] = hashlib.sha256(_canonical(manifest)).hexdigest()
    lock.write_bytes(_canonical(payload))
    return _sha256(lock)


def _module():
    name = "hephaestus.automation.codex_adapter_admission"
    assert importlib.util.find_spec(name) is not None, "adapter admission is not implemented"
    return importlib.import_module(name)


def _sigstore_fixture(name: str) -> bytes:
    """Decode one retained upstream Sigstore behavior fixture."""
    encoded = Path(__file__).parents[2] / "fixtures" / "sigstore" / name
    return base64.b64decode(b"".join(encoded.read_bytes().split()), validate=True)


def _admit(module: Any, lock: Path, digest: str, *, importer: Any = None):
    return module._admit_codex_adapter(
        lock_path=lock,
        expected_sha256=digest,
        selected_entry_point="production",
        offline_verifier=lambda _lock: None,
        importer=importer or (lambda _root, _module, _factory: object()),
        host_platform="darwin",
        host_machine="arm64",
        virtualization_available=True,
    )


def test_tampered_detached_lock_fails_before_adapter_import(tmp_path: Path) -> None:
    """A changed lock fails before external adapter code can load."""
    lock, digest, _ = _deployment(tmp_path)
    lock.write_bytes(lock.read_bytes() + b" ")
    imported = False

    def importer(_root: str, _module: str, _factory: str) -> object:
        nonlocal imported
        imported = True
        return object()

    module = _module()
    with pytest.raises(module.CodexAdapterAdmissionError, match="deployment lock digest"):
        _admit(module, lock, digest, importer=importer)
    assert imported is False


def test_public_admission_has_no_verifier_or_importer_bypass() -> None:
    """Production callers cannot replace supply-chain or import checks."""
    parameters = inspect.signature(_module().admit_codex_adapter).parameters

    assert tuple(parameters) == ("lock_path", "expected_sha256", "selected_entry_point")


def test_public_admission_loads_verified_installed_bytes_in_a_fresh_process(
    tmp_path: Path,
) -> None:
    """A fresh process uses the public lock-to-import production path."""
    lock, digest, payload = _deployment(tmp_path, build_installed_wheel=True)
    repository = Path(__file__).resolve().parents[3]
    code = f"""
from pathlib import Path
from hephaestus.automation import codex_adapter_admission as admission
admission.sys.platform = "darwin"
admission.platform.machine = lambda: "arm64"
admission._virtualization_framework_available = lambda: True
admission._ARCHIVE_SHA256 = {payload["codex_archive_sha256"]!r}
admission._SIGSTORE_SHA256 = {payload["sigstore_bundle_sha256"]!r}
admission._default_offline_verifier = lambda _lock: None
result = admission.admit_codex_adapter(
    lock_path=Path({str(lock)!r}),
    expected_sha256={digest!r},
    selected_entry_point="production",
)
print(type(result.lock).__name__, result.lock.schema_version)
print(result.factory())
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repository)

    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.stdout == "CodexAdapterDeploymentLockV1 1\nlocked\n"
    assert completed.stderr == ""


def test_linked_deployment_lock_fails_before_adapter_import(tmp_path: Path) -> None:
    """A linked detached lock fails before external adapter code can load."""
    lock, digest, _ = _deployment(tmp_path)
    linked = tmp_path / "linked-lock.json"
    linked.symlink_to(lock)
    imported = False

    def importer(_root: str, _module: str, _factory: str) -> object:
        nonlocal imported
        imported = True
        return object()

    module = _module()
    with pytest.raises(module.CodexAdapterAdmissionError, match="deployment lock path"):
        _admit(module, linked, digest, importer=importer)
    assert imported is False


def test_unsafe_deployment_lock_parent_fails_before_adapter_import(tmp_path: Path) -> None:
    """A writable non-sticky parent cannot carry the detached trust anchor."""
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    lock, digest, _ = _deployment(unsafe)
    imported = False

    def importer(_root: str, _module: str, _factory: str) -> object:
        nonlocal imported
        imported = True
        return object()

    module = _module()
    with pytest.raises(module.CodexAdapterAdmissionError, match="path component"):
        _admit(module, lock, digest, importer=importer)
    assert imported is False


def test_lock_rejects_unsorted_installed_tree_manifest(tmp_path: Path) -> None:
    """The detached lock has one canonical sorted tree inventory."""
    lock, _, payload = _deployment(tmp_path)
    payload["installed_tree_manifest"].reverse()
    lock.write_bytes(_canonical(payload))
    module = _module()

    with pytest.raises(module.CodexAdapterAdmissionError, match="manifest"):
        _admit(module, lock, _sha256(lock))


def test_lock_rejects_wheel_tags_that_do_not_match_filename(tmp_path: Path) -> None:
    """The retained wheel name must contain the exact locked tag triple."""
    lock, _, payload = _deployment(tmp_path)
    payload["wheel_tags"] = ["cp313", "cp313", "macosx_14_0_arm64"]
    lock.write_bytes(_canonical(payload))
    module = _module()

    with pytest.raises(module.CodexAdapterAdmissionError, match="wheel tags"):
        _admit(module, lock, _sha256(lock))


def test_adapter_self_attestation_is_not_a_trust_anchor(tmp_path: Path) -> None:
    """An adapter identity must equal the host-verified lock identity."""
    lock, digest, _ = _deployment(tmp_path)
    module = _module()
    parsed = module.CodexAdapterDeploymentLockV1.from_bytes(lock.read_bytes())
    admission = module.CodexAdapterAdmission(
        lock=parsed,
        deployment_lock_sha256=digest,
        factory=object(),
    )

    with pytest.raises(module.CodexAdapterAdmissionError, match="adapter identity"):
        admission.validate_adapter_identity(
            distribution="other", version="1.0", installed_tree_sha256="0" * 64
        )


def test_production_admission_parses_the_version_1_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production admission returns the accepted version-1 deployment lock."""
    lock, digest, payload = _deployment(tmp_path)
    module = _module()
    monkeypatch.setattr(module, "_ARCHIVE_SHA256", payload["codex_archive_sha256"])
    monkeypatch.setattr(module, "_SIGSTORE_SHA256", payload["sigstore_bundle_sha256"])

    admitted = _admit(module, lock, digest)

    assert type(admitted.lock) is module.CodexAdapterDeploymentLockV1
    assert admitted.lock.schema_version == 1


def test_production_admission_rejects_a_version_2_lock(tmp_path: Path) -> None:
    """Production admission rejects a lock outside the accepted version."""
    lock, _, payload = _deployment(tmp_path)
    for name in (
        "schema_version",
        "adapter_api_version",
        "request_schema_version",
        "prepared_schema_version",
        "result_schema_version",
    ):
        payload[name] = 2
    lock.write_bytes(_canonical(payload))
    module = _module()

    with pytest.raises(module.CodexAdapterAdmissionError, match="schema"):
        _admit(module, lock, _sha256(lock))


def test_complete_offline_deployment_imports_only_the_locked_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complete locked evidence admits the exact factory after verification."""
    lock, digest, payload = _deployment(tmp_path)
    module = _module()
    monkeypatch.setattr(module, "_ARCHIVE_SHA256", payload["codex_archive_sha256"])
    monkeypatch.setattr(module, "_SIGSTORE_SHA256", payload["sigstore_bundle_sha256"])
    imported: list[tuple[str, str]] = []
    factory = object()

    def importer(_root: str, module_name: str, factory_name: str) -> object:
        imported.append((module_name, factory_name))
        return factory

    admitted = _admit(module, lock, digest, importer=importer)

    assert admitted.factory is factory
    assert admitted.deployment_lock_sha256 == digest
    assert imported == [("example_adapter", "factory")]


def test_default_importer_loads_only_from_verified_installed_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambient same-name module cannot replace the locked adapter module."""
    lock, digest, payload = _deployment(tmp_path)
    shadow = tmp_path / "shadow"
    _regular(
        shadow / "example_adapter" / "__init__.py",
        b"def factory():\n    return 'shadow'\n",
    )
    monkeypatch.syspath_prepend(str(shadow))
    sys.modules.pop("example_adapter", None)
    module = _module()
    monkeypatch.setattr(module, "_ARCHIVE_SHA256", payload["codex_archive_sha256"])
    monkeypatch.setattr(module, "_SIGSTORE_SHA256", payload["sigstore_bundle_sha256"])

    admitted = _admit(module, lock, digest, importer=module._default_importer)

    assert admitted.factory() == "locked"


def test_production_import_uses_held_verified_module_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module-path replacement after verification cannot execute changed bytes."""
    lock, digest, payload = _deployment(tmp_path)
    module = _module()
    monkeypatch.setattr(module, "_ARCHIVE_SHA256", payload["codex_archive_sha256"])
    monkeypatch.setattr(module, "_SIGSTORE_SHA256", payload["sigstore_bundle_sha256"])
    package = Path(payload["installed_tree_root"], "example_adapter", "__init__.py")
    replacement = tmp_path / "replacement.py"
    marker = tmp_path / "replacement-executed"
    replacement.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).touch()\n"
        "def factory(): return 'changed'\n",
        encoding="utf-8",
    )
    replacement.chmod(0o600)

    def replace_after_verification(_lock: object) -> None:
        replacement.replace(package)

    sys.modules.pop("example_adapter", None)
    admitted = module._admit_codex_adapter(
        lock_path=lock,
        expected_sha256=digest,
        selected_entry_point="production",
        offline_verifier=replace_after_verification,
        host_platform="darwin",
        host_machine="arm64",
        virtualization_available=True,
    )

    assert admitted.factory() == "locked"
    assert not marker.exists()


def test_production_import_rejects_ambient_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verified adapter code cannot execute a dependency from ambient paths."""
    lock, _, payload = _deployment(tmp_path)
    package = Path(payload["installed_tree_root"], "example_adapter", "__init__.py")
    package.write_text("import ambient_poison\ndef factory(): return ambient_poison.VALUE\n")
    digest = _refresh_installed_tree_lock(lock, payload)
    ambient = tmp_path / "ambient"
    marker = tmp_path / "ambient-executed"
    _regular(
        ambient / "ambient_poison.py",
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\nVALUE = 'ambient'\n".encode(),
    )
    monkeypatch.syspath_prepend(str(ambient))
    module = _module()
    monkeypatch.setattr(module, "_ARCHIVE_SHA256", payload["codex_archive_sha256"])
    monkeypatch.setattr(module, "_SIGSTORE_SHA256", payload["sigstore_bundle_sha256"])
    sys.modules.pop("example_adapter", None)
    sys.modules.pop("ambient_poison", None)

    with pytest.raises(module.CodexAdapterAdmissionError, match="dependency"):
        module._admit_codex_adapter(
            lock_path=lock,
            expected_sha256=digest,
            selected_entry_point="production",
            offline_verifier=lambda _lock: None,
            host_platform="darwin",
            host_machine="arm64",
            virtualization_available=True,
        )

    assert not marker.exists()


@pytest.mark.parametrize("phase", ["import", "factory", "method"])
@pytest.mark.parametrize("loader", ["importlib", "builtins", "sys_modules"])
def test_verified_adapter_cannot_load_ambient_code_at_any_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    loader: str,
) -> None:
    """Adapter import capabilities cannot select code from ambient paths."""
    module = _module()
    marker = tmp_path / "ambient-executed"
    ambient = tmp_path / "ambient"
    _regular(
        ambient / "ambient_poison.py",
        (
            b"from pathlib import Path\n"
            b"def load(marker):\n"
            b"    Path(marker).touch()\n"
            b"    return 'ambient'\n"
        ),
    )
    monkeypatch.syspath_prepend(str(ambient))
    sys.modules.pop("ambient_poison", None)
    if loader == "importlib":
        action = (
            "import importlib\n"
            f"return importlib.import_module('ambient_poison').load({str(marker)!r})"
        )
    elif loader == "builtins":
        action = (
            f"import builtins\nreturn builtins.__import__('ambient_poison').load({str(marker)!r})"
        )
    else:
        ambient_module = importlib.import_module("ambient_poison")
        monkeypatch.setitem(sys.modules, "ambient_poison", ambient_module)
        action = f"import sys\nreturn sys.modules['ambient_poison'].load({str(marker)!r})"
    indented_action = "\n".join(f"    {line}" for line in action.splitlines())
    if phase == "import":
        source = f"def run():\n{indented_action}\nvalue = run()\ndef factory(): return value\n"
    elif phase == "factory":
        source = f"def factory():\n{indented_action}\n"
    else:
        source = (
            "class Adapter:\n"
            "    adapter_distribution = 'example-adapter'\n"
            "    adapter_version = '1.0.0'\n"
            "    installed_tree_sha256 = 'a' * 64\n"
            "    def prepare(self, request):\n"
            + "\n".join(f"    {line}" for line in indented_action.splitlines())
            + "\n    def invoke(self, prepared, auth_path): return prepared\n"
            + "    def destroy(self, prepared): return None\n"
            + "\ndef factory(): return Adapter()\n"
        )
    tree = module._VerifiedInstalledTree(
        root=tmp_path,
        files={"example_adapter/__init__.py": source.encode()},
    )

    if phase == "import" or loader == "builtins":
        with pytest.raises(module.CodexAdapterAdmissionError):
            module._default_importer(tree, "example_adapter", "factory")
    else:
        factory = module._default_importer(tree, "example_adapter", "factory")
        with pytest.raises(module.CodexAdapterAdmissionError):
            adapter = factory()
            if phase == "method":
                adapter.prepare(None)

    assert not marker.exists()


def test_verified_adapter_cannot_use_a_direct_ambient_source_loader(
    tmp_path: Path,
) -> None:
    """A direct importlib file loader cannot execute an ambient source file."""
    module = _module()
    marker = tmp_path / "direct-loader-executed"
    ambient = _regular(
        tmp_path / "ambient_dynamic.py",
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n".encode(),
    )
    source = (
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('ambient_dynamic', {str(ambient)!r})\n"
        "loaded = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(loaded)\n"
        "def factory(): return loaded\n"
    )
    tree = module._VerifiedInstalledTree(
        root=tmp_path,
        files={"example_adapter/__init__.py": source.encode()},
    )

    with pytest.raises(module.CodexAdapterAdmissionError):
        module._default_importer(tree, "example_adapter", "factory")

    assert not marker.exists()


def test_verified_adapter_cannot_select_a_stdlib_named_ambient_shadow(
    tmp_path: Path,
) -> None:
    """A changed module path cannot select an ambient stdlib-name shadow."""
    module = _module()
    marker = tmp_path / "stdlib-shadow-executed"
    ambient = tmp_path / "ambient"
    _regular(
        ambient / "fractions.py",
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n".encode(),
    )
    source = (
        "import sys\n"
        f"sys.path.insert(0, {str(ambient)!r})\n"
        "import fractions\n"
        "def factory(): return fractions\n"
    )
    tree = module._VerifiedInstalledTree(
        root=tmp_path,
        files={"example_adapter/__init__.py": source.encode()},
    )

    with pytest.raises(module.CodexAdapterAdmissionError):
        module._default_importer(tree, "example_adapter", "factory")

    assert not marker.exists()


@pytest.mark.parametrize("phase", ["import", "factory", "method"])
@pytest.mark.parametrize(
    ("exposure", "action"),
    (
        (
            "module_sys_registry",
            "import os\nos.sys.modules['ambient_escape'] = os",
        ),
        (
            "module_sys_finder",
            "import os\nos.sys.meta_path.insert(0, object())",
        ),
        (
            "module_sys_path",
            "import os\nos.sys.path.insert(0, '/ambient')",
        ),
        ("module_builtins", "import os\n_ = os.__builtins__['__import__']"),
        ("module_spec_loader", "import pathlib\n_ = pathlib.__spec__.loader"),
        ("module_loader", "import pathlib\n_ = pathlib.__loader__"),
        ("nested_module", "import json\n_ = json.scanner.__loader__"),
        (
            "function_globals",
            "import dataclasses\ndataclasses.fields.__globals__['sys'].path.insert(0, '/ambient')",
        ),
    ),
)
def test_permitted_library_objects_have_a_transitive_closed_view(
    tmp_path: Path,
    phase: str,
    exposure: str,
    action: str,
) -> None:
    """A permitted library cannot expose unrestricted import state."""
    module = _module()
    action = f"{action}\nreturn 'escaped'"
    indented_action = "\n".join(f"    {line}" for line in action.splitlines())
    if phase == "import":
        source = f"def run():\n{indented_action}\nvalue = run()\ndef factory(): return value\n"
    elif phase == "factory":
        source = f"def factory():\n{indented_action}\n"
    else:
        source = (
            "class Adapter:\n"
            "    adapter_distribution = 'example-adapter'\n"
            "    adapter_version = '1.0.0'\n"
            "    installed_tree_sha256 = 'a' * 64\n"
            "    def prepare(self, request):\n"
            + "\n".join(f"    {line}" for line in indented_action.splitlines())
            + "\n    def invoke(self, prepared, auth_path): return prepared\n"
            + "    def destroy(self, prepared): return None\n"
            + "\ndef factory(): return Adapter()\n"
        )
    tree = module._VerifiedInstalledTree(
        root=tmp_path,
        files={"example_adapter/__init__.py": source.encode()},
    )

    static_rejection = exposure not in {"module_sys_registry", "module_sys_path"}
    if phase == "import" or static_rejection:
        with pytest.raises(module.CodexAdapterAdmissionError):
            module._default_importer(tree, "example_adapter", "factory")
    else:
        factory = module._default_importer(tree, "example_adapter", "factory")
        with pytest.raises(module.CodexAdapterAdmissionError):
            adapter = factory()
            if phase == "method":
                adapter.prepare(None)


@pytest.mark.parametrize("phase", ["import", "factory", "method"])
@pytest.mark.parametrize(
    "action",
    (
        "import os\nreturn object.__getattribute__(os, '_target')",
        "import os\nreturn type(os).__mro__",
        "return ().__class__.__base__.__subclasses__()",
        "import os\nreturn getattr(os, '_target')",
        "import pathlib\nreturn pathlib.Path.__call__.__self__",
    ),
)
def test_adapter_object_introspection_cannot_recover_capability_targets(
    tmp_path: Path,
    phase: str,
    action: str,
) -> None:
    """Adapter code cannot recover a real object from one capability."""
    module = _module()
    indented_action = "\n".join(f"    {line}" for line in action.splitlines())
    if phase == "import":
        source = f"def run():\n{indented_action}\nvalue = run()\ndef factory(): return value\n"
    elif phase == "factory":
        source = f"def factory():\n{indented_action}\n"
    else:
        source = (
            "class Adapter:\n"
            "    adapter_distribution = 'example-adapter'\n"
            "    adapter_version = '1.0.0'\n"
            "    installed_tree_sha256 = 'a' * 64\n"
            "    def prepare(self, request):\n"
            + "\n".join(f"    {line}" for line in indented_action.splitlines())
            + "\n    def invoke(self, prepared, auth_path): return prepared\n"
            + "    def destroy(self, prepared): return None\n"
            + "\ndef factory(): return Adapter()\n"
        )
    tree = module._VerifiedInstalledTree(
        root=tmp_path,
        files={"example_adapter/__init__.py": source.encode()},
    )

    with pytest.raises(
        module.CodexAdapterAdmissionError,
        match="closed capability surface",
    ):
        module._default_importer(tree, "example_adapter", "factory")


@pytest.mark.parametrize("phase", ["import", "factory", "method"])
def test_adapter_cannot_inherit_an_unexported_capability_surface(
    tmp_path: Path,
    phase: str,
) -> None:
    """A capability class cannot expose inherited ambient methods."""
    module = _module()
    assert "def __mro_entries__" not in module._ISOLATED_ADAPTER_HELPER
    ambient = tmp_path / "ambient.txt"
    ambient.write_text("ambient data", encoding="utf-8")
    action = (
        "import pathlib\n"
        "class AmbientPath(pathlib.PosixPath): pass\n"
        f"return AmbientPath({str(ambient)!r}).read_text()"
    )
    indented_action = "\n".join(f"    {line}" for line in action.splitlines())
    if phase == "import":
        source = f"def run():\n{indented_action}\nvalue = run()\ndef factory(): return value\n"
    elif phase == "factory":
        source = f"def factory():\n{indented_action}\n"
    else:
        source = (
            "class Adapter:\n"
            "    adapter_distribution = 'example-adapter'\n"
            "    adapter_version = '1.0.0'\n"
            "    installed_tree_sha256 = 'a' * 64\n"
            "    def prepare(self, request):\n"
            + "\n".join(f"    {line}" for line in indented_action.splitlines())
            + "\n    def invoke(self, prepared, auth_path): return prepared\n"
            + "    def destroy(self, prepared): return None\n"
            + "\ndef factory(): return Adapter()\n"
        )
    tree = module._VerifiedInstalledTree(
        root=tmp_path,
        files={"example_adapter/__init__.py": source.encode()},
    )

    if phase == "import":
        with pytest.raises(module.CodexAdapterAdmissionError):
            module._default_importer(tree, "example_adapter", "factory")
    else:
        factory = module._default_importer(tree, "example_adapter", "factory")
        with pytest.raises(module.CodexAdapterAdmissionError):
            adapter = factory()
            if phase == "method":
                adapter.prepare(None)


@pytest.mark.parametrize("phase", ["import", "factory", "method"])
@pytest.mark.parametrize("callback_surface", ["Thread", "Timer"])
def test_callback_capability_does_not_receive_a_raw_exported_class(
    tmp_path: Path,
    phase: str,
    callback_surface: str,
) -> None:
    """A thread callback cannot receive a raw capability class."""
    module = _module()
    ambient = tmp_path / "ambient.txt"
    ambient.write_text("ambient data", encoding="utf-8")
    marker = tmp_path / f"{phase}-{callback_surface}-callback-escape"
    prefix = (
        "import pathlib\n"
        "import threading\n"
        "def capture(path_type):\n"
        "    class AmbientPath(path_type): pass\n"
        f"    AmbientPath({str(marker)!r}).write_text("
        f"AmbientPath({str(ambient)!r}).read_text())\n"
    )
    if callback_surface == "Thread":
        action = "threading.Thread(target=capture, args=(pathlib.PosixPath,)).run()"
    else:
        action = "threading.Timer(0, capture, args=(pathlib.PosixPath,)).run()"
    if phase == "import":
        source = prefix + f"{action}\ndef factory(): return None\n"
    elif phase == "factory":
        source = prefix + f"def factory():\n    {action}\n    return None\n"
    else:
        source = (
            prefix
            + "class Adapter:\n"
            + "    adapter_distribution = 'example-adapter'\n"
            + "    adapter_version = '1.0.0'\n"
            + "    installed_tree_sha256 = 'a' * 64\n"
            + f"    def prepare(self, request):\n        {action}\n        return request\n"
            + "    def invoke(self, prepared, auth_path): return prepared\n"
            + "    def destroy(self, prepared): return None\n"
            + "def factory(): return Adapter()\n"
        )
    tree = module._VerifiedInstalledTree(
        root=tmp_path,
        files={"example_adapter/__init__.py": source.encode()},
    )

    try:
        factory = module._default_importer(tree, "example_adapter", "factory")
        if phase == "factory":
            factory()
        elif phase == "method":
            factory().prepare(None)
    except module.CodexAdapterAdmissionError:
        pass

    assert not marker.exists()


@pytest.mark.parametrize("phase", ["import", "factory", "method"])
def test_signal_callback_receives_a_closed_frame(
    tmp_path: Path,
    phase: str,
) -> None:
    """A signal callback cannot receive an unwrapped helper frame."""
    module = _module()
    marker = tmp_path / f"{phase}-signal-callback-escape"
    prefix = (
        "import os\n"
        "import signal\n"
        "def capture(_signum, frame):\n"
        "    while frame is not None:\n"
        "        if 'capability_targets' in frame.f_globals:\n"
        f"            descriptor = os.open({str(marker)!r}, "
        "os.O_WRONLY | os.O_CREAT, 0o600)\n"
        "            os.close(descriptor)\n"
        "            return\n"
        "        frame = frame.f_back\n"
        "signal.signal(signal.SIGTERM, capture)\n"
    )
    action = "os.kill(os.getpid(), signal.SIGTERM)"
    if phase == "import":
        source = prefix + f"{action}\ndef factory(): return None\n"
    elif phase == "factory":
        source = prefix + f"def factory():\n    {action}\n    return None\n"
    else:
        source = (
            prefix
            + "class Adapter:\n"
            + "    adapter_distribution = 'example-adapter'\n"
            + "    adapter_version = '1.0.0'\n"
            + "    installed_tree_sha256 = 'a' * 64\n"
            + f"    def prepare(self, request):\n        {action}\n        return request\n"
            + "    def invoke(self, prepared, auth_path): return prepared\n"
            + "    def destroy(self, prepared): return None\n"
            + "def factory(): return Adapter()\n"
        )
    tree = module._VerifiedInstalledTree(
        root=tmp_path,
        files={"example_adapter/__init__.py": source.encode()},
    )

    try:
        factory = module._default_importer(tree, "example_adapter", "factory")
        if phase == "factory":
            factory()
        elif phase == "method":
            factory().prepare(None)
    except module.CodexAdapterAdmissionError:
        pass

    assert not marker.exists()


def test_adapter_can_use_an_audited_standard_library_capability(tmp_path: Path) -> None:
    """An allowlisted standard-library function returns only copied data."""
    module = _module()
    source = (
        b"import json\n"
        b"def factory():\n"
        b"    return json.loads(json.dumps({'status': 'locked'}))['status']\n"
    )
    tree = module._VerifiedInstalledTree(
        root=tmp_path,
        files={"example_adapter/__init__.py": source},
    )

    factory = module._default_importer(tree, "example_adapter", "factory")

    assert factory() == "locked"


@pytest.mark.parametrize("phase", ["import", "factory", "method"])
@pytest.mark.parametrize(
    "builtin_name",
    (
        "breakpoint",
        "compile",
        "delattr",
        "dir",
        "eval",
        "exec",
        "getattr",
        "globals",
        "hasattr",
        "help",
        "input",
        "locals",
        "object",
        "open",
        "setattr",
        "type",
        "vars",
    ),
)
def test_adapter_builtin_capability_is_an_explicit_allowlist(
    tmp_path: Path,
    phase: str,
    builtin_name: str,
) -> None:
    """A sensitive builtin is absent during each adapter execution phase."""
    module = _module()
    action = f"import builtins\nreturn builtins.{builtin_name}"
    indented_action = "\n".join(f"    {line}" for line in action.splitlines())
    if phase == "import":
        source = f"def run():\n{indented_action}\nvalue = run()\ndef factory(): return value\n"
    elif phase == "factory":
        source = f"def factory():\n{indented_action}\n"
    else:
        source = (
            "class Adapter:\n"
            "    adapter_distribution = 'example-adapter'\n"
            "    adapter_version = '1.0.0'\n"
            "    installed_tree_sha256 = 'a' * 64\n"
            "    def prepare(self, request):\n"
            + "\n".join(f"    {line}" for line in indented_action.splitlines())
            + "\n    def invoke(self, prepared, auth_path): return prepared\n"
            + "    def destroy(self, prepared): return None\n"
            + "\ndef factory(): return Adapter()\n"
        )
    tree = module._VerifiedInstalledTree(
        root=tmp_path,
        files={"example_adapter/__init__.py": source.encode()},
    )

    if phase == "import":
        with pytest.raises(module.CodexAdapterAdmissionError):
            module._default_importer(tree, "example_adapter", "factory")
    else:
        factory = module._default_importer(tree, "example_adapter", "factory")
        with pytest.raises(module.CodexAdapterAdmissionError):
            adapter = factory()
            if phase == "method":
                adapter.prepare(None)


def test_isolated_adapter_preserves_version_1_protocol_values(tmp_path: Path) -> None:
    """The helper keeps version-1 records across all adapter operations."""
    module = _module()
    source = (
        b"class Adapter:\n"
        b"    adapter_distribution = 'example-adapter'\n"
        b"    adapter_version = '1.0.0'\n"
        b"    installed_tree_sha256 = 'a' * 64\n"
        b"    def prepare(self, request): return request\n"
        b"    def invoke(self, prepared, auth_path): return prepared\n"
        b"    def destroy(self, prepared): return None\n"
        b"def factory(): return Adapter()\n"
    )
    tree = module._VerifiedInstalledTree(
        root=tmp_path,
        files={"example_adapter/__init__.py": source},
    )
    factory = module._default_importer(tree, "example_adapter", "factory")
    assert factory.codex_isolation_api_version == 1
    adapter = factory()
    protocol = importlib.import_module("hephaestus.agents.codex_isolation")
    record = protocol.CodexDescendantInventoryV1(
        schema_version=1,
        sequence=0,
        monotonic_timestamp=0.0,
        complete=True,
        descendants=(),
        cgroup_populated=False,
    )

    assert adapter.adapter_distribution == "example-adapter"
    assert adapter.adapter_version == "1.0.0"
    assert adapter.installed_tree_sha256 == "a" * 64
    prepared = adapter.prepare(record)
    assert prepared == record
    assert adapter.invoke(prepared, "/private/auth.json") == record
    assert adapter.destroy(prepared) is None


def test_isolated_adapter_retains_unencodable_prepare_for_one_opaque_cleanup(
    tmp_path: Path,
) -> None:
    """The helper retains an unencodable prepared guest for one cleanup."""
    module = _module()
    source = (
        b"class Adapter:\n"
        b"    adapter_distribution = 'example-adapter'\n"
        b"    adapter_version = '1.0.0'\n"
        b"    installed_tree_sha256 = 'a' * 64\n"
        b"    def prepare(self, request):\n"
        b"        del request\n"
        b"        return self\n"
        b"    def invoke(self, prepared, auth_path):\n"
        b"        raise AssertionError('invoke must not run')\n"
        b"    def destroy(self, prepared):\n"
        b"        if prepared is not self: raise AssertionError('raw guest was replaced')\n"
        b"def factory(): return Adapter()\n"
    )
    tree = module._VerifiedInstalledTree(
        root=tmp_path,
        files={"example_adapter/__init__.py": source},
    )
    adapter = module._default_importer(tree, "example_adapter", "factory")()
    cleanup_error_type = importlib.import_module(
        "hephaestus.agents.codex_isolation"
    )._CodexPrepareCleanupError

    with pytest.raises(cleanup_error_type) as raised:
        adapter.prepare(None)

    cleanup = raised.value.claim_cleanup()
    assert cleanup is not None
    assert cleanup() is None
    assert raised.value.claim_cleanup() is None


def test_late_unencodable_isolated_prepare_has_one_host_cleanup_control(
    tmp_path: Path,
) -> None:
    """A late unsupported helper result has one bounded host cleanup control."""
    from hephaestus.agents import runtime as agent_runtime

    module = _module()
    source = (
        b"import time\n"
        b"class Adapter:\n"
        b"    adapter_distribution = 'example-adapter'\n"
        b"    adapter_version = '1.0.0'\n"
        b"    installed_tree_sha256 = 'a' * 64\n"
        b"    def prepare(self, request):\n"
        b"        del request\n"
        b"        time.sleep(0.04)\n"
        b"        return self\n"
        b"    def invoke(self, prepared, auth_path):\n"
        b"        raise AssertionError('invoke must not run')\n"
        b"    def destroy(self, prepared):\n"
        b"        if prepared is not self: raise AssertionError('raw guest was replaced')\n"
        b"def factory(): return Adapter()\n"
    )
    tree = module._VerifiedInstalledTree(
        root=tmp_path,
        files={"example_adapter/__init__.py": source},
    )
    adapter = module._default_importer(tree, "example_adapter", "factory")()
    controls: list[str] = []
    write = adapter._process._write

    def capture_control(payload: object) -> None:
        if isinstance(payload, dict) and isinstance(payload.get("operation"), str):
            controls.append(payload["operation"])
        write(payload)

    adapter._process._write = capture_control
    request = SimpleNamespace(
        monotonic_deadline=time.monotonic() + 0.01,
        policy=SimpleNamespace(
            term_grace_seconds=0.05,
            kill_grace_seconds=0.05,
            pipe_close_grace_seconds=0.05,
            inventory_quiescence_seconds=0.01,
        ),
    )
    active = agent_runtime._start_codex_adapter_call(lambda: adapter.prepare(None))

    with pytest.raises(
        CodexIsolationError,
        match="codex_adapter_inventory_uncertain",
    ):
        agent_runtime._complete_timed_out_codex_call(
            adapter=adapter,
            request=cast(Any, request),
            active_call=active,
            prepared=None,
        )
    with pytest.raises(
        CodexIsolationError,
        match="codex_adapter_inventory_uncertain",
    ):
        agent_runtime._complete_timed_out_codex_call(
            adapter=adapter,
            request=cast(Any, request),
            active_call=active,
            prepared=None,
        )

    assert controls.count("prepare") == 1
    assert controls.count("destroy_prepared") == 1
    assert "invoke_prepared" not in controls


def test_isolated_adapter_destroy_interrupts_one_blocked_invoke(tmp_path: Path) -> None:
    """The helper processes terminal control while one invocation is active."""
    module = _module()
    source = (
        b"import threading\n"
        b"released = threading.Event()\n"
        b"class Adapter:\n"
        b"    adapter_distribution = 'example-adapter'\n"
        b"    adapter_version = '1.0.0'\n"
        b"    installed_tree_sha256 = 'a' * 64\n"
        b"    def prepare(self, request): return request\n"
        b"    def invoke(self, prepared, auth_path):\n"
        b"        del auth_path\n"
        b"        if not released.wait(1.0): raise RuntimeError('destroy did not run')\n"
        b"        return prepared\n"
        b"    def destroy(self, prepared):\n"
        b"        del prepared\n"
        b"        released.set()\n"
        b"def factory(): return Adapter()\n"
    )
    tree = module._VerifiedInstalledTree(
        root=tmp_path,
        files={"example_adapter/__init__.py": source},
    )
    factory = module._default_importer(tree, "example_adapter", "factory")
    adapter = factory()
    outcome: list[object] = []
    prepared = adapter.prepare("prepared")

    invoke = threading.Thread(
        target=lambda: outcome.append(adapter.invoke(prepared, "/private/auth.json")),
        daemon=True,
    )
    started = time.monotonic()
    invoke.start()
    time.sleep(0.05)
    adapter.destroy(prepared)
    invoke.join(timeout=0.5)

    assert not invoke.is_alive()
    assert outcome == ["prepared"]
    assert time.monotonic() - started < 0.5


def test_retained_sigstore_fixture_verifies_offline_and_rejects_tampering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real Sigstore verifier uses retained evidence without the network."""
    module = _module()
    artifact = _sigstore_fixture("bundle_v3.txt.b64")
    bundle = _sigstore_fixture("bundle_v3.txt.sigstore.b64")
    trusted_root = _sigstore_fixture("staging-trusted-root.json.b64")

    def deny_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("offline Sigstore verification used the network")

    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    verified_bundle, _ = module._verify_retained_sigstore(
        artifact=artifact,
        bundle_bytes=bundle,
        trusted_root_bytes=trusted_root,
        identity="william@yossarian.net",
        issuer="https://github.com/login/oauth",
    )

    assert verified_bundle.log_entry._inner.log_index == 25_915_956
    for changed_artifact, changed_bundle, changed_root in (
        (artifact + b"tampered", bundle, trusted_root),
        (artifact, bundle.replace(b'"signature": "', b'"signature": "AA', 1), trusted_root),
        (artifact, bundle, trusted_root.replace(b'"keyId": "', b'"keyId": "AA', 1)),
    ):
        with pytest.raises(module.CodexAdapterAdmissionError, match="Sigstore"):
            module._verify_retained_sigstore(
                artifact=changed_artifact,
                bundle_bytes=changed_bundle,
                trusted_root_bytes=changed_root,
                identity="william@yossarian.net",
                issuer="https://github.com/login/oauth",
            )


def test_installed_wheel_tree_mismatch_fails_before_adapter_import(tmp_path: Path) -> None:
    """Changed installed bytes fail before external adapter code can load."""
    lock, digest, payload = _deployment(tmp_path)
    Path(payload["installed_tree_root"], "example_adapter", "__init__.py").write_text(
        "factory = None\n", encoding="utf-8"
    )
    module = _module()

    with pytest.raises(module.CodexAdapterAdmissionError, match="installed tree"):
        _admit(module, lock, digest)


def test_guest_image_digest_mismatch_fails_before_adapter_import(tmp_path: Path) -> None:
    """A changed guest image fails before external adapter code can load."""
    lock, digest, payload = _deployment(tmp_path)
    Path(payload["guest_image_path"]).write_bytes(b"changed")
    module = _module()

    with pytest.raises(module.CodexAdapterAdmissionError, match="guest image"):
        _admit(module, lock, digest)


@pytest.mark.parametrize("field", ["codex_archive_path", "sigstore_bundle_path"])
def test_codex_archive_or_sigstore_digest_mismatch_fails_before_adapter_import(
    tmp_path: Path, field: str
) -> None:
    """Changed Codex release evidence fails before adapter import."""
    lock, digest, payload = _deployment(tmp_path)
    Path(payload[field]).write_bytes(b"changed")
    module = _module()

    with pytest.raises(module.CodexAdapterAdmissionError, match="Codex release artifact"):
        _admit(module, lock, digest)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("oidc_issuer", "https://issuer.invalid"),
        ("workflow_certificate_identity", "https://identity.invalid"),
        ("fulcio_certificate_issuer", "untrusted"),
    ],
)
def test_oidc_issuer_or_certificate_identity_mismatch_fails_before_adapter_import(
    tmp_path: Path, field: str, value: str
) -> None:
    """A different release identity fails before adapter import."""
    lock, _, payload = _deployment(tmp_path)
    payload[field] = value
    lock.write_bytes(_canonical(payload))
    module = _module()

    with pytest.raises(module.CodexAdapterAdmissionError, match="certificate identity"):
        _admit(module, lock, _sha256(lock))


@pytest.mark.parametrize("field", ["trusted_root_path", "rekor_checkpoint_path"])
def test_trust_root_or_rekor_mismatch_fails_before_adapter_import(
    tmp_path: Path, field: str
) -> None:
    """Changed offline trust evidence fails before adapter import."""
    lock, digest, payload = _deployment(tmp_path)
    Path(payload[field]).write_bytes(b"changed")
    module = _module()

    with pytest.raises(module.CodexAdapterAdmissionError, match="offline trust artifact"):
        _admit(module, lock, digest)


def test_rekor_evidence_is_bound_to_bundle_and_trusted_root(tmp_path: Path) -> None:
    """Retained Rekor bytes must equal the verified bundle and root values."""
    lock_path, _, _ = _deployment(tmp_path)
    module = _module()
    lock = module.CodexAdapterDeploymentLockV1.from_bytes(lock_path.read_bytes())
    proof_value = {
        "checkpoint": {"envelope": "checkpoint"},
        "hashes": [],
        "logIndex": "1",
        "rootHash": "cm9vdA==",
        "treeSize": "2",
    }

    class Proof:
        checkpoint = SimpleNamespace(envelope="checkpoint")

        @staticmethod
        def to_json() -> str:
            return json.dumps(proof_value)

    entry = SimpleNamespace(
        inclusion_proof=Proof(),
        integrated_time=1,
        log_id=SimpleNamespace(key_id=bytes.fromhex(lock.rekor_log_id)),
        log_index=1,
    )
    bundle = SimpleNamespace(log_entry=SimpleNamespace(_inner=entry))
    trusted_root = SimpleNamespace(
        _inner=SimpleNamespace(
            tlogs=[
                SimpleNamespace(
                    log_id=SimpleNamespace(key_id=bytes.fromhex(lock.rekor_log_id)),
                    public_key=SimpleNamespace(raw_bytes=b"rekor key"),
                )
            ]
        )
    )

    module._validate_locked_rekor_evidence(
        lock,
        bundle=bundle,
        trusted_root=trusted_root,
        public_key_bytes=b"rekor key",
        checkpoint_bytes=b"checkpoint",
        inclusion_proof_bytes=_canonical(proof_value),
    )

    with pytest.raises(module.CodexAdapterAdmissionError, match="Rekor evidence"):
        module._validate_locked_rekor_evidence(
            lock,
            bundle=bundle,
            trusted_root=trusted_root,
            public_key_bytes=b"different key",
            checkpoint_bytes=b"checkpoint",
            inclusion_proof_bytes=_canonical(proof_value),
        )
