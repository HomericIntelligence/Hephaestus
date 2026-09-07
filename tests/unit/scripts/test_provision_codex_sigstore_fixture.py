"""Tests for the Codex Sigstore fixture provisioner."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "provision_codex_sigstore_fixture.py"
MANIFEST = SCRIPT.parents[1] / "tests" / "fixtures" / "sigstore" / "codex-sigstore-fixture.json"
PRODUCTION_ROOT = SCRIPT.parents[1] / "hephaestus"


def _load() -> ModuleType:
    """Load the standalone provisioner."""
    spec = importlib.util.spec_from_file_location("provision_codex_sigstore_fixture", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_document(
    archive: bytes,
    bundle: bytes,
    elf: bytes,
    retained: dict[str, bytes],
) -> dict[str, Any]:
    """Build one small valid manifest for a hermetic behavior test."""
    return {
        "schema_version": 1,
        "release": {
            "repository": "openai/codex",
            "tag": "rust-v0.153.4",
            "target": "aarch64-unknown-linux-musl",
        },
        "assets": [
            {
                "id": 545043499,
                "api_url": "https://api.github.test/assets/545043499",
                "download_url": "https://download.github.test/codex.zst",
                "name": "codex-aarch64-unknown-linux-musl.zst",
                "size": len(archive),
                "sha256": _digest(archive),
            },
            {
                "id": 545043543,
                "api_url": "https://api.github.test/assets/545043543",
                "download_url": "https://download.github.test/codex.sigstore",
                "name": "codex-aarch64-unknown-linux-musl.sigstore",
                "size": len(bundle),
                "sha256": _digest(bundle),
            },
        ],
        "extracted_elf": {
            "name": "codex-aarch64-unknown-linux-musl",
            "size": len(elf),
            "sha256": _digest(elf),
        },
        "retained": [
            {"name": name, "size": len(data), "sha256": _digest(data)}
            for name, data in retained.items()
        ],
    }


def _write_test_manifest(
    tmp_path: Path,
    module: Any,
    *,
    archive: bytes = b"archive",
    bundle: bytes = b"bundle",
    elf: bytes = b"elf",
) -> tuple[Path, dict[str, bytes], dict[str, bytes]]:
    retained = {
        "codex-production-trusted-root.json": b"root",
        "codex-rekor.pub": b"key",
        "codex-rekor.checkpoint": b"checkpoint",
        "codex-rekor.proof": b"proof",
    }
    source = tmp_path / "source"
    source.mkdir()
    for name, data in retained.items():
        (source / name).write_bytes(data)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(_manifest_document(archive, bundle, elf, retained)),
        encoding="utf-8",
    )
    module.MANIFEST_PATH = manifest
    module.RETAINED_SOURCE_ROOT = source
    return manifest, {"archive": archive, "bundle": bundle, "elf": elf}, retained


class _Response:
    """Supply one context-managed URL response."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._position = 0

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._data) - self._position
        start = self._position
        self._position = min(len(self._data), start + size)
        return self._data[start : self._position]


def test_provisioner_requires_an_absolute_release_root(tmp_path: Path) -> None:
    """A relative output root cannot select the retained artifact store."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", "build/test-fixtures"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "absolute" in result.stderr


def test_tracked_manifest_binds_the_official_release_objects() -> None:
    """The tracked manifest contains the reviewed upstream identities."""
    document = json.loads(MANIFEST.read_bytes())

    assert document["schema_version"] == 1
    assert document["release"] == {
        "repository": "openai/codex",
        "tag": "rust-v0.153.4",
        "target": "aarch64-unknown-linux-musl",
    }
    assert document["assets"] == [
        {
            "id": 545043499,
            "api_url": "https://api.github.com/repos/openai/codex/releases/assets/545043499",
            "download_url": "https://github.com/openai/codex/releases/download/"
            "rust-v0.153.4/codex-aarch64-unknown-linux-musl.zst",
            "name": "codex-aarch64-unknown-linux-musl.zst",
            "size": 65_376_650,
            "sha256": "7a148fb7e7ed8a4bfa3ac4ffe014336070398eff780893582afab9195c6652f7",
        },
        {
            "id": 545043543,
            "api_url": "https://api.github.com/repos/openai/codex/releases/assets/545043543",
            "download_url": "https://github.com/openai/codex/releases/download/"
            "rust-v0.153.4/codex-aarch64-unknown-linux-musl.sigstore",
            "name": "codex-aarch64-unknown-linux-musl.sigstore",
            "size": 8_565,
            "sha256": "847b47e73068f86635c23ab5501647a93fab9ad0c450d6661a88481dfcd6d759",
        },
    ]
    assert document["extracted_elf"] == {
        "name": "codex-aarch64-unknown-linux-musl",
        "size": 222_567_456,
        "sha256": "4d76e542c222ea8c75861d8c4ade60a1a332a63255ce1c60bdaebf7c2a2869e6",
    }
    assert {item["name"] for item in document["retained"]} == {
        "codex-production-trusted-root.json",
        "codex-rekor.pub",
        "codex-rekor.checkpoint",
        "codex-rekor.proof",
    }


def test_production_sources_do_not_read_the_test_fixture_environment() -> None:
    """Production code cannot select the test-only external fixture root."""
    fixture_environment = "HEPHAESTUS_CODEX_SIGSTORE_FIXTURE_ROOT"

    consumers = [
        path.relative_to(PRODUCTION_ROOT).as_posix()
        for path in PRODUCTION_ROOT.rglob("*.py")
        if fixture_environment in path.read_text(encoding="utf-8")
    ]

    assert consumers == []


def test_provision_fetches_validates_extracts_and_caches_every_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One run creates only validated owner-controlled fixture objects."""
    module = _load()
    _, fetched, retained = _write_test_manifest(tmp_path, module)
    source_modes = {
        name: (module.RETAINED_SOURCE_ROOT / name).stat().st_mode & 0o777 for name in retained
    }
    calls: list[str] = []
    metadata = {
        "https://api.github.test/assets/545043499": {
            "id": 545043499,
            "name": "codex-aarch64-unknown-linux-musl.zst",
            "size": len(fetched["archive"]),
            "digest": f"sha256:{_digest(fetched['archive'])}",
            "browser_download_url": "https://download.github.test/codex.zst",
        },
        "https://api.github.test/assets/545043543": {
            "id": 545043543,
            "name": "codex-aarch64-unknown-linux-musl.sigstore",
            "size": len(fetched["bundle"]),
            "digest": f"sha256:{_digest(fetched['bundle'])}",
            "browser_download_url": "https://download.github.test/codex.sigstore",
        },
    }
    downloads = {
        "https://download.github.test/codex.zst": fetched["archive"],
        "https://download.github.test/codex.sigstore": fetched["bundle"],
    }

    def urlopen(request: Any, *, timeout: int) -> _Response:
        assert timeout == 60
        url = request.full_url
        calls.append(url)
        if url in metadata:
            return _Response(json.dumps(metadata[url]).encode())
        return _Response(downloads[url])

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == ["zstd", "--decompress", "--stdout"]
        os.write(cast(int, kwargs["stdout"]), fetched["elf"])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module, "_PUBLIC_ASSET_OPENER", SimpleNamespace(open=urlopen))
    monkeypatch.setattr(module.shutil, "which", lambda _name: "zstd")
    monkeypatch.setattr(module.subprocess, "run", run)
    root = tmp_path / "build" / "test-fixtures" / "codex-sigstore" / "rust-v0.153.4"

    outputs = module.provision(root)

    expected = {
        "codex-aarch64-unknown-linux-musl.zst": fetched["archive"],
        "codex-aarch64-unknown-linux-musl.sigstore": fetched["bundle"],
        "codex-aarch64-unknown-linux-musl": fetched["elf"],
        **retained,
    }
    assert {path.name for path in outputs} == set(expected)
    for name, data in expected.items():
        output = root / name
        assert output.read_bytes() == data
        assert output.stat().st_mode & 0o077 == 0
        cache = root / ".cache" / "sha256" / _digest(data)
        assert cache.read_bytes() == data
        assert cache.stat().st_mode & 0o077 == 0
    assert calls == [
        "https://api.github.test/assets/545043499",
        "https://download.github.test/codex.zst",
        "https://api.github.test/assets/545043543",
        "https://download.github.test/codex.sigstore",
    ]
    for directory in (root, root / ".cache", root / ".cache" / "sha256"):
        assert directory.stat().st_mode & 0o077 == 0
    assert {
        name: (module.RETAINED_SOURCE_ROOT / name).stat().st_mode & 0o777 for name in retained
    } == source_modes


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", "wrong-name", "metadata"),
        ("size", 999, "metadata"),
        ("digest", "sha256:" + "0" * 64, "metadata"),
        ("browser_download_url", "https://wrong.test/file", "metadata"),
    ],
)
def test_asset_metadata_mismatch_fails_before_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    message: str,
) -> None:
    """Each upstream metadata field must match the tracked manifest."""
    module = _load()
    _, fetched, _ = _write_test_manifest(tmp_path, module)
    document = {
        "id": 545043499,
        "name": "codex-aarch64-unknown-linux-musl.zst",
        "size": len(fetched["archive"]),
        "digest": f"sha256:{_digest(fetched['archive'])}",
        "browser_download_url": "https://download.github.test/codex.zst",
    }
    document[field] = value
    calls = 0

    def urlopen(_request: Any, *, timeout: int) -> _Response:
        nonlocal calls
        calls += 1
        return _Response(json.dumps(document).encode())

    monkeypatch.setattr(module, "_PUBLIC_ASSET_OPENER", SimpleNamespace(open=urlopen))
    root = tmp_path / "build" / "test-fixtures" / "codex-sigstore" / "rust-v0.153.4"

    with pytest.raises(module.ProvisionError, match=message):
        module.provision(root)

    assert calls == 1


def test_github_token_is_limited_to_metadata_and_removed_on_cross_origin_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A public asset host cannot receive the GitHub metadata token."""
    module = _load()
    monkeypatch.setenv("GITHUB_TOKEN", "metadata-secret")
    metadata = module._request(
        "https://api.github.com/repos/openai/codex/releases/assets/545043499",
        accept="application/vnd.github+json",
        authenticate=True,
    )
    download = module._request(
        "https://github.com/openai/codex/releases/download/rust-v0.153.4/codex.zst",
        accept="application/octet-stream",
        authenticate=False,
    )

    assert metadata.get_header("Authorization") == "Bearer metadata-secret"
    assert download.get_header("Authorization") is None
    other_host = module._request(
        "https://downloads.example.test/private",
        accept="application/octet-stream",
        authenticate=True,
    )
    assert other_host.get_header("Authorization") is None

    source = module.urllib.request.Request(
        "https://github.com/openai/codex/releases/download/rust-v0.153.4/codex.zst",
        headers={"Authorization": "Bearer must-not-cross-origin"},
    )
    redirected = module._PublicAssetRedirectHandler().redirect_request(
        source,
        None,
        302,
        "Found",
        {},
        "https://release-assets.githubusercontent.com/codex.zst",
    )

    assert redirected is not None
    assert redirected.get_header("Authorization") is None


def test_metadata_cross_origin_redirect_does_not_forward_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real metadata redirect cannot send the GitHub token to a new origin."""
    module = _load()
    received_authorization: list[str | None] = []
    document = {
        "id": 545043499,
        "name": "codex-aarch64-unknown-linux-musl.zst",
        "size": 7,
        "digest": f"sha256:{_digest(b'archive')}",
        "browser_download_url": "https://download.github.test/codex.zst",
    }

    class DestinationHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received_authorization.append(self.headers.get("Authorization"))
            body = json.dumps(document).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    destination = ThreadingHTTPServer(("127.0.0.1", 0), DestinationHandler)
    destination_url = f"http://127.0.0.1:{destination.server_port}/metadata"

    class SourceHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", destination_url)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    source = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
    source_url = f"http://127.0.0.1:{source.server_port}/metadata"
    threads = [
        threading.Thread(target=destination.serve_forever, daemon=True),
        threading.Thread(target=source.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    monkeypatch.setattr(
        module,
        "_request",
        lambda *_args, **_kwargs: urllib.request.Request(
            source_url,
            headers={"Authorization": "Bearer metadata-secret"},
        ),
    )
    try:
        module._asset_metadata(
            {
                "api_url": "https://api.github.com/asset",
                "id": document["id"],
                "name": document["name"],
                "size": document["size"],
                "sha256": _digest(b"archive"),
                "download_url": document["browser_download_url"],
            }
        )
    finally:
        source.shutdown()
        destination.shutdown()
        for thread in threads:
            thread.join(timeout=2)
        source.server_close()
        destination.server_close()

    assert received_authorization == [None]


@pytest.mark.parametrize("url", ["http://github.test/asset", "file:///private/asset"])
def test_non_https_asset_url_fails_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    """The provisioner rejects a release URL that is not HTTPS."""
    module = _load()
    manifest, fetched, retained = _write_test_manifest(tmp_path, module)
    document = _manifest_document(fetched["archive"], fetched["bundle"], fetched["elf"], retained)
    document["assets"][0]["api_url"] = url
    manifest.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("network must not start"),
    )

    with pytest.raises(module.ProvisionError, match="URL"):
        module.provision(tmp_path / "build" / "test-fixtures" / "codex-sigstore" / "rust-v0.153.4")


def test_download_digest_mismatch_leaves_no_partial_cache_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changed release bytes cannot enter the content-addressed cache."""
    module = _load()
    _, fetched, _ = _write_test_manifest(tmp_path, module)
    metadata = {
        "id": 545043499,
        "name": "codex-aarch64-unknown-linux-musl.zst",
        "size": len(fetched["archive"]),
        "digest": f"sha256:{_digest(fetched['archive'])}",
        "browser_download_url": "https://download.github.test/codex.zst",
    }
    responses = iter([_Response(json.dumps(metadata).encode()), _Response(b"changed")])
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(
        module,
        "_PUBLIC_ASSET_OPENER",
        SimpleNamespace(open=lambda *_args, **_kwargs: next(responses)),
    )
    root = tmp_path / "build" / "test-fixtures" / "codex-sigstore" / "rust-v0.153.4"

    with pytest.raises(module.ProvisionError, match="download"):
        module.provision(root)

    cache = root / ".cache" / "sha256"
    assert not (cache / _digest(fetched["archive"])).exists()
    assert not any(path.name.startswith(".tmp-") for path in cache.iterdir())


def test_download_stops_when_bytes_first_exceed_declared_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The download stops at the first byte beyond the manifest size."""
    module = _load()

    class ByteResponse(_Response):
        def __init__(self, data: bytes) -> None:
            super().__init__(data)
            self.read_calls = 0

        def read(self, _size: int = -1) -> bytes:
            self.read_calls += 1
            return super().read(1)

    response = ByteResponse(b"abcdef")
    asset = {
        "download_url": "https://download.github.test/codex.zst",
        "name": "codex-aarch64-unknown-linux-musl.zst",
        "size": 3,
        "sha256": _digest(b"abc"),
    }
    monkeypatch.setattr(module, "_asset_metadata", lambda _asset: None)
    monkeypatch.setattr(
        module,
        "_PUBLIC_ASSET_OPENER",
        SimpleNamespace(open=lambda *_args, **_kwargs: response),
    )
    root = tmp_path / "build" / "test-fixtures" / "codex-sigstore" / "rust-v0.153.4"

    with module._prepare_root(root) as (_root_directory, cache):
        with pytest.raises(module.ProvisionError, match="download"):
            module._download_asset(asset, cache)

    assert response.read_calls == 4


def test_extracted_elf_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed decompressed executable cannot enter the fixture root."""
    module = _load()
    _, fetched, _ = _write_test_manifest(tmp_path, module)
    metadata_by_url = {
        "https://api.github.test/assets/545043499": {
            "id": 545043499,
            "name": "codex-aarch64-unknown-linux-musl.zst",
            "size": len(fetched["archive"]),
            "digest": f"sha256:{_digest(fetched['archive'])}",
            "browser_download_url": "https://download.github.test/codex.zst",
        },
        "https://api.github.test/assets/545043543": {
            "id": 545043543,
            "name": "codex-aarch64-unknown-linux-musl.sigstore",
            "size": len(fetched["bundle"]),
            "digest": f"sha256:{_digest(fetched['bundle'])}",
            "browser_download_url": "https://download.github.test/codex.sigstore",
        },
    }
    payload_by_url = {
        "https://download.github.test/codex.zst": fetched["archive"],
        "https://download.github.test/codex.sigstore": fetched["bundle"],
    }

    def urlopen(request: Any, *, timeout: int) -> _Response:
        url = request.full_url
        value = metadata_by_url.get(url)
        return _Response(json.dumps(value).encode() if value else payload_by_url[url])

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        os.write(cast(int, kwargs["stdout"]), b"changed")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(module, "_PUBLIC_ASSET_OPENER", SimpleNamespace(open=urlopen))
    monkeypatch.setattr(module.shutil, "which", lambda _name: "zstd")
    monkeypatch.setattr(module.subprocess, "run", run)
    root = tmp_path / "build" / "test-fixtures" / "codex-sigstore" / "rust-v0.153.4"

    with pytest.raises(module.ProvisionError, match="extracted ELF"):
        module.provision(root)

    assert not (root / "codex-aarch64-unknown-linux-musl").exists()


@pytest.mark.parametrize("link_target", ["root", "destination", "cache"])
def test_symlinked_fixture_paths_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, link_target: str
) -> None:
    """A link cannot select or replace a provisioned fixture path."""
    module = _load()
    _write_test_manifest(tmp_path, module)
    base = tmp_path / "build" / "test-fixtures" / "codex-sigstore"
    base.mkdir(parents=True)
    root = base / "rust-v0.153.4"
    outside = tmp_path / "outside"
    outside.mkdir()
    if link_target == "root":
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir()
        if link_target == "destination":
            (root / "codex-aarch64-unknown-linux-musl.zst").symlink_to(outside / "archive")
        else:
            (root / ".cache").symlink_to(outside, target_is_directory=True)

    with pytest.raises(module.ProvisionError, match="link"):
        module.provision(root)


@pytest.mark.parametrize("component", ["build", "test-fixtures", "codex-sigstore"])
def test_intermediate_fixture_symlink_fails_before_outside_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
) -> None:
    """An intermediate link cannot receive a create, mode change, or replace."""
    module = _load()
    _write_test_manifest(tmp_path, module)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o750)
    outside_mode = outside.stat().st_mode
    parent = tmp_path
    for name in ("build", "test-fixtures", "codex-sigstore"):
        path = parent / name
        if name == component:
            path.symlink_to(outside, target_is_directory=True)
            break
        path.mkdir(mode=0o700)
        parent = path
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("network must not start"),
    )
    root = tmp_path / "build" / "test-fixtures" / "codex-sigstore" / "rust-v0.153.4"

    with pytest.raises(module.ProvisionError, match="symbolic link"):
        module.provision(root)

    assert list(outside.iterdir()) == []
    assert outside.stat().st_mode == outside_mode


def test_retained_object_mismatch_fails_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changed local trust data stops before a release request."""
    module = _load()
    _, _, retained = _write_test_manifest(tmp_path, module)
    (module.RETAINED_SOURCE_ROOT / next(iter(retained))).write_bytes(b"changed")
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("network must not start"),
    )
    root = tmp_path / "build" / "test-fixtures" / "codex-sigstore" / "rust-v0.153.4"

    with pytest.raises(module.ProvisionError, match="retained"):
        module.provision(root)


def test_fixture_root_must_have_the_release_suffix(tmp_path: Path) -> None:
    """An absolute root outside the test-fixture layout is not valid."""
    module = _load()

    with pytest.raises(module.ProvisionError, match="suffix"):
        module.provision(tmp_path / "somewhere-else")


def test_manifest_rejects_unsafe_output_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest name cannot escape the selected fixture root."""
    module = _load()
    manifest, fetched, retained = _write_test_manifest(tmp_path, module)
    document = _manifest_document(fetched["archive"], fetched["bundle"], fetched["elf"], retained)
    document["retained"][0]["name"] = "../outside"
    manifest.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(module.ProvisionError, match="name"):
        module.provision(tmp_path / "build" / "test-fixtures" / "codex-sigstore" / "rust-v0.153.4")
