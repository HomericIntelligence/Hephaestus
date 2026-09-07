#!/usr/bin/env python3
"""Provision the retained Codex Sigstore artifact test fixture.

Usage:
    python3 scripts/provision_codex_sigstore_fixture.py \
        --root /absolute/build/test-fixtures/codex-sigstore/rust-v0.153.4
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPOSITORY_ROOT / "tests" / "fixtures" / "sigstore" / "codex-sigstore-fixture.json"
RETAINED_SOURCE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "sigstore"
_ROOT_SUFFIX = ("build", "test-fixtures", "codex-sigstore", "rust-v0.153.4")
_CHUNK_SIZE = 1024 * 1024
_TIMEOUT_SECONDS = 60


class ProvisionError(RuntimeError):
    """Report an invalid or incomplete external artifact fixture."""


def _absolute_root(value: str) -> Path:
    """Parse one absolute fixture root."""
    root = Path(value)
    if not root.is_absolute():
        raise argparse.ArgumentTypeError("fixture root must be absolute")
    return root


def _safe_name(value: object) -> str:
    """Return one plain output name or reject the manifest."""
    if type(value) is not str:
        raise ProvisionError("fixture object name is invalid")
    name = value
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ProvisionError("fixture object name is invalid")
    return name


def _digest_descriptor(descriptor: int) -> tuple[int, str]:
    """Hash one held regular-file descriptor."""
    digest = hashlib.sha256()
    size = 0
    try:
        offset = 0
        while chunk := os.pread(descriptor, _CHUNK_SIZE, offset):
            size += len(chunk)
            offset += len(chunk)
            digest.update(chunk)
    except OSError as exc:
        raise ProvisionError("fixture object cannot be read") from exc
    return size, digest.hexdigest()


def _expected_record(value: object, *, label: str) -> dict[str, Any]:
    """Validate one manifest object record."""
    if not isinstance(value, dict):
        raise ProvisionError(f"{label} manifest record is invalid")
    name = _safe_name(value.get("name"))
    size = value.get("size")
    digest = value.get("sha256")
    if type(size) is not int or size < 1:
        raise ProvisionError(f"{label} size is invalid")
    if (
        type(digest) is not str
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ProvisionError(f"{label} digest is invalid")
    return {**value, "name": name, "size": size, "sha256": digest}


def _manifest() -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Load the fixed release manifest."""
    try:
        document = json.loads(MANIFEST_PATH.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvisionError("fixture manifest is invalid") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ProvisionError("fixture manifest is invalid")
    assets_value = document.get("assets")
    retained_value = document.get("retained")
    if not isinstance(assets_value, list) or not isinstance(retained_value, list):
        raise ProvisionError("fixture manifest is invalid")
    assets = [_expected_record(value, label="asset") for value in assets_value]
    retained = [_expected_record(value, label="retained") for value in retained_value]
    elf = _expected_record(document.get("extracted_elf"), label="extracted ELF")
    names = [record["name"] for record in (*assets, elf, *retained)]
    if len(names) != len(set(names)):
        raise ProvisionError("fixture object name is duplicated")
    for asset in assets:
        if (
            type(asset.get("id")) is not int
            or type(asset.get("api_url")) is not str
            or type(asset.get("download_url")) is not str
        ):
            raise ProvisionError("asset manifest record is invalid")
    return assets, elf, retained


class _HeldDirectory:
    """Hold one verified directory while fixture operations use it."""

    def __init__(self, path: Path, descriptor: int) -> None:
        self.path = path
        self.descriptor = descriptor


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _validate_directory(descriptor: int, *, managed: bool) -> None:
    """Validate one held path component before descent."""
    metadata = os.fstat(descriptor)
    mode = stat.S_IMODE(metadata.st_mode)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ProvisionError("fixture path component is not a directory")
    if managed:
        if metadata.st_uid != os.geteuid() or mode & 0o022:
            raise ProvisionError("fixture path component is not owner controlled")
        os.fchmod(descriptor, 0o700)
    elif metadata.st_uid not in {0, os.geteuid()} or (
        mode & 0o002 and not (metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX)
    ):
        raise ProvisionError("fixture path component is not safe")


def _open_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    create: bool,
    managed: bool,
) -> int:
    """Open one no-follow child directory relative to its held parent."""
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ProvisionError("fixture directory cannot be created") from exc
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except OSError as exc:
        try:
            metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            linked = stat.S_ISLNK(metadata.st_mode)
        except OSError:
            linked = False
        if linked:
            raise ProvisionError("fixture path must not be a symbolic link") from exc
        raise ProvisionError("fixture path component cannot be opened") from exc
    try:
        _validate_directory(descriptor, managed=managed)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@contextlib.contextmanager
def _prepare_root(root: Path) -> Iterator[tuple[_HeldDirectory, _HeldDirectory]]:
    """Hold the exact owner-only artifact and cache directories."""
    if not root.is_absolute():
        raise ProvisionError("fixture root must be absolute")
    if tuple(root.parts[-len(_ROOT_SUFFIX) :]) != _ROOT_SUFFIX:
        raise ProvisionError("fixture root does not have the required suffix")
    descriptors: list[int] = []
    current_path = Path(root.anchor)
    try:
        current = os.open(root.anchor, _directory_flags())
        descriptors.append(current)
        _validate_directory(current, managed=False)
        managed_at = len(root.parts) - len(_ROOT_SUFFIX)
        for index, name in enumerate(root.parts[1:], start=1):
            managed = index >= managed_at
            current = _open_directory_at(
                current,
                name,
                create=managed,
                managed=managed,
            )
            descriptors.append(current)
            current_path /= name
        root_directory = _HeldDirectory(root, current)
        cache = _open_directory_at(current, ".cache", create=True, managed=True)
        descriptors.append(cache)
        sha256 = _open_directory_at(cache, "sha256", create=True, managed=True)
        descriptors.append(sha256)
        yield root_directory, _HeldDirectory(root / ".cache" / "sha256", sha256)
    finally:
        for descriptor in reversed(descriptors):
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _temporary_name() -> str:
    return f".tmp-{os.getpid()}-{secrets.token_hex(8)}"


def _open_regular_at(directory: _HeldDirectory, name: str, flags: int, mode: int = 0o600) -> int:
    """Open one no-follow regular file relative to a held directory."""
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            mode,
            dir_fd=directory.descriptor,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise ProvisionError("fixture object is not an owned regular file")
        return descriptor
    except ProvisionError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        raise ProvisionError("fixture object cannot be opened") from exc


def _reject_unsafe_output(directory: _HeldDirectory, name: str) -> None:
    """Reject an existing non-regular output before network access."""
    try:
        metadata = os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ProvisionError("fixture object cannot be inspected") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ProvisionError("fixture object must not be a symbolic link")
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise ProvisionError("fixture object is not an owned regular file")


def _validate_file_at(
    directory: _HeldDirectory,
    name: str,
    record: Mapping[str, Any],
    *,
    label: str,
) -> bool:
    """Validate one file relative to a held directory."""
    try:
        descriptor = _open_regular_at(directory, name, os.O_RDONLY)
    except ProvisionError as exc:
        try:
            os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            pass
        raise ProvisionError(f"{label} is not a safe regular file") from exc
    try:
        size, digest = _digest_descriptor(descriptor)
        if size != record["size"] or digest != record["sha256"]:
            return False
        os.fchmod(descriptor, 0o600)
        return True
    finally:
        os.close(descriptor)


def _validate_source_file(
    path: Path,
    record: Mapping[str, Any],
    *,
    label: str,
) -> bool:
    """Validate one retained repository file without following its final name."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ProvisionError(f"{label} cannot be opened") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ProvisionError(f"{label} is not a regular file")
        size, digest = _digest_descriptor(descriptor)
        return size == record["size"] and digest == record["sha256"]
    finally:
        os.close(descriptor)


def _install_cache_file(
    source: Path,
    cache: _HeldDirectory,
    record: Mapping[str, Any],
    *,
    label: str,
) -> str:
    """Atomically put one validated object in the digest cache."""
    destination = str(record["sha256"])
    if _validate_file_at(cache, destination, record, label=label):
        return destination
    temporary = _temporary_name()
    source_descriptor = -1
    target_descriptor = -1
    try:
        source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        target_descriptor = _open_regular_at(
            cache,
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        )
        while chunk := os.read(source_descriptor, _CHUNK_SIZE):
            offset = 0
            while offset < len(chunk):
                offset += os.write(target_descriptor, chunk[offset:])
        os.fsync(target_descriptor)
        os.close(target_descriptor)
        target_descriptor = -1
        if not _validate_file_at(cache, temporary, record, label=label):
            raise ProvisionError(f"{label} does not match its manifest")
        os.replace(
            temporary,
            destination,
            src_dir_fd=cache.descriptor,
            dst_dir_fd=cache.descriptor,
        )
        return destination
    except ProvisionError:
        raise
    except OSError as exc:
        raise ProvisionError(f"{label} cannot enter the cache") from exc
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if target_descriptor >= 0:
            os.close(target_descriptor)
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=cache.descriptor)


def _origin(url: str) -> tuple[str, str | None, int | None]:
    """Return the network origin for one URL."""
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProvisionError("asset URL is invalid") from exc
    return parsed.scheme.lower(), parsed.hostname, port


class _PublicAssetRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Remove credentials when a public asset request changes origin."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> urllib.request.Request | None:
        """Build one redirect request without cross-origin credentials."""
        redirected = super().redirect_request(
            request,
            file_pointer,
            code,
            message,
            headers,
            new_url,
        )
        if redirected is not None and _origin(request.full_url) != _origin(redirected.full_url):
            for header in ("Authorization", "Cookie", "Proxy-Authorization"):
                redirected.remove_header(header)
        return redirected


_PUBLIC_ASSET_OPENER = urllib.request.build_opener(_PublicAssetRedirectHandler())


def _request(url: str, *, accept: str, authenticate: bool) -> urllib.request.Request:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ProvisionError("asset URL is invalid")
    headers = {"Accept": accept, "User-Agent": "hephaestus-codex-fixture-provisioner"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if authenticate and parsed.hostname == "api.github.com" and token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def _asset_metadata(asset: Mapping[str, Any]) -> None:
    """Verify the live asset identity before download."""
    try:
        with _PUBLIC_ASSET_OPENER.open(  # nosec B310 -- _request permits only HTTPS.
            _request(
                str(asset["api_url"]),
                accept="application/vnd.github+json",
                authenticate=True,
            ),
            timeout=_TIMEOUT_SECONDS,
        ) as response:
            document = json.loads(response.read())
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise ProvisionError("asset metadata cannot be read") from exc
    expected = {
        "id": asset["id"],
        "name": asset["name"],
        "size": asset["size"],
        "digest": f"sha256:{asset['sha256']}",
        "browser_download_url": asset["download_url"],
    }
    if not isinstance(document, dict) or any(
        document.get(key) != value for key, value in expected.items()
    ):
        raise ProvisionError("asset metadata does not match the manifest")


def _download_asset(asset: Mapping[str, Any], cache: _HeldDirectory) -> str:
    """Stream one exact official release asset into the digest cache."""
    destination = str(asset["sha256"])
    if _validate_file_at(cache, destination, asset, label="cached asset"):
        return destination
    _asset_metadata(asset)
    temporary = _temporary_name()
    digest = hashlib.sha256()
    size = 0
    target_descriptor = -1
    try:
        target_descriptor = _open_regular_at(
            cache,
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        )
        with _PUBLIC_ASSET_OPENER.open(  # nosec B310 -- _request permits only HTTPS.
            _request(
                str(asset["download_url"]),
                accept="application/octet-stream",
                authenticate=False,
            ),
            timeout=_TIMEOUT_SECONDS,
        ) as response:
            while chunk := response.read(_CHUNK_SIZE):
                size += len(chunk)
                if size > asset["size"]:
                    raise ProvisionError("asset download exceeds its manifest size")
                digest.update(chunk)
                offset = 0
                while offset < len(chunk):
                    offset += os.write(target_descriptor, chunk[offset:])
            os.fsync(target_descriptor)
        os.close(target_descriptor)
        target_descriptor = -1
        if size != asset["size"] or digest.hexdigest() != asset["sha256"]:
            raise ProvisionError("asset download does not match the manifest")
        if not _validate_file_at(cache, temporary, asset, label="downloaded asset"):
            raise ProvisionError("asset download does not match the manifest")
        os.replace(
            temporary,
            destination,
            src_dir_fd=cache.descriptor,
            dst_dir_fd=cache.descriptor,
        )
        return destination
    except ProvisionError:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise ProvisionError("asset download failed") from exc
    finally:
        if target_descriptor >= 0:
            os.close(target_descriptor)
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=cache.descriptor)


def _extract_elf(
    archive: str,
    cache: _HeldDirectory,
    record: Mapping[str, Any],
) -> str:
    """Extract and cache the fixed Linux executable."""
    destination = str(record["sha256"])
    if _validate_file_at(cache, destination, record, label="cached extracted ELF"):
        return destination
    if shutil.which("zstd") is None:
        raise ProvisionError("zstd is required to extract the Codex fixture")
    temporary = _temporary_name()
    archive_descriptor = -1
    target_descriptor = -1
    try:
        archive_descriptor = _open_regular_at(cache, archive, os.O_RDONLY)
        target_descriptor = _open_regular_at(
            cache,
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        )
        result = subprocess.run(
            ["zstd", "--decompress", "--stdout"],
            stdin=archive_descriptor,
            stdout=target_descriptor,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise ProvisionError("Codex archive extraction failed")
        os.fsync(target_descriptor)
        os.close(target_descriptor)
        target_descriptor = -1
        if not _validate_file_at(cache, temporary, record, label="extracted ELF"):
            raise ProvisionError("extracted ELF does not match the manifest")
        os.replace(
            temporary,
            destination,
            src_dir_fd=cache.descriptor,
            dst_dir_fd=cache.descriptor,
        )
        return destination
    except ProvisionError:
        raise
    except OSError as exc:
        raise ProvisionError("Codex archive extraction failed") from exc
    finally:
        if archive_descriptor >= 0:
            os.close(archive_descriptor)
        if target_descriptor >= 0:
            os.close(target_descriptor)
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=cache.descriptor)


def _publish(
    cache_object: str,
    cache: _HeldDirectory,
    destination: str,
    root: _HeldDirectory,
    record: Mapping[str, Any],
) -> Path:
    """Atomically publish one cached object under its release name."""
    if _validate_file_at(root, destination, record, label="fixture object"):
        return root.path / destination
    temporary = _temporary_name()
    try:
        os.link(
            cache_object,
            temporary,
            src_dir_fd=cache.descriptor,
            dst_dir_fd=root.descriptor,
            follow_symlinks=False,
        )
        os.replace(
            temporary,
            destination,
            src_dir_fd=root.descriptor,
            dst_dir_fd=root.descriptor,
        )
        if not _validate_file_at(root, destination, record, label="fixture object"):
            raise ProvisionError("fixture object cannot be published")
        return root.path / destination
    except OSError as exc:
        raise ProvisionError("fixture object cannot be published") from exc
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=root.descriptor)


def provision(root: Path) -> tuple[Path, ...]:
    """Provision and validate the exact external release fixture."""
    assets, elf_record, retained = _manifest()
    with _prepare_root(root) as (root_directory, cache):
        for record in (*assets, elf_record, *retained):
            _reject_unsafe_output(root_directory, str(record["name"]))
        retained_cache: list[tuple[dict[str, Any], str]] = []
        for record in retained:
            source = RETAINED_SOURCE_ROOT / record["name"]
            if not _validate_source_file(source, record, label="retained object"):
                raise ProvisionError("retained object does not match the manifest")
            retained_cache.append(
                (
                    record,
                    _install_cache_file(source, cache, record, label="retained object"),
                )
            )

        asset_cache = [(record, _download_asset(record, cache)) for record in assets]
        archive = next(name for record, name in asset_cache if str(record["name"]).endswith(".zst"))
        elf_cache = _extract_elf(archive, cache, elf_record)

        outputs: list[Path] = []
        for record, cached in (*asset_cache, (elf_record, elf_cache), *retained_cache):
            outputs.append(
                _publish(
                    cached,
                    cache,
                    str(record["name"]),
                    root_directory,
                    record,
                )
            )
        return tuple(outputs)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=_absolute_root)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Provision the configured fixture root."""
    arguments = build_parser().parse_args(argv)
    try:
        provision(arguments.root)
    except ProvisionError as exc:
        build_parser().error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
