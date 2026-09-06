#!/usr/bin/env python3
"""Build and authorize the local CI image used by Linux host verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from hephaestus.automation.pipeline.host_verification_pyxis import (
    DEFAULT_HOST_VERIFICATION_PYXIS_IMAGE,
    PYXIS_AUTHORITY_SCHEMA,
    image_sha256,
    validate_pyxis_image,
)

IMAGE_TAG_PREFIX = "hephaestus-ci:host-verification"
_CONTAINERFILE = Path("ci/Containerfile")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_COMMAND_TIMEOUT_S = 1800


class HostVerificationImagePreparationError(RuntimeError):
    """Raised when the local Pyxis image cannot be prepared safely."""


def _validate_local_import_uri(uri: str) -> str:
    """Return a content-addressed local Enroot URI or fail closed."""
    if re.fullmatch(r"(?:podman|dockerd)://sha256:[0-9a-f]{64}", uri) is None:
        raise HostVerificationImagePreparationError(
            "Enroot import must use a content-addressed local image"
        )
    return uri


def _regular_path(root: Path, relative: Path) -> Path:
    """Return a non-symlink path below *root* or fail closed."""
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise HostVerificationImagePreparationError(
            f"build context path is not a regular file: {relative}"
        )
    return path


def _safe_output_path(root: Path, output: Path) -> Path:
    """Return an absolute output path with no symlinked path component."""
    candidate = output.expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    absolute = candidate.absolute()
    for component in (absolute, *absolute.parents):
        if component.is_symlink():
            raise HostVerificationImagePreparationError(
                "output image path cannot contain a symlink"
            )
    return absolute


def _engine_command(engine: str) -> tuple[str, ...]:
    """Return the selected OCI engine executable command."""
    if engine not in {"podman", "docker"}:
        raise ValueError(f"unsupported container engine: {engine}")
    return (engine,)


def _select_engine(which: Callable[[str], str | None] = shutil.which) -> str:
    """Select Podman first, then Docker, from local executables."""
    for engine in ("podman", "docker"):
        if which(engine):
            return engine
    raise HostVerificationImagePreparationError("podman or docker is unavailable")


def _run(
    argv: Sequence[str],
    *,
    cwd: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    """Run one bounded host-owned command with captured diagnostics."""
    try:
        result = runner(
            tuple(argv),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostVerificationImagePreparationError(f"preparation command failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-500:]
        raise HostVerificationImagePreparationError(
            f"preparation command failed with exit code {result.returncode}: {detail}"
        )
    return result


def _read_image_id(result: subprocess.CompletedProcess[str]) -> str:
    """Return one immutable OCI image ID from an inspect result."""
    image_id = result.stdout.strip()
    if not _IMAGE_ID_RE.fullmatch(image_id):
        raise HostVerificationImagePreparationError("container engine returned an invalid image ID")
    return image_id


def _source_revision(root: Path, runner: Callable[..., subprocess.CompletedProcess[str]]) -> str:
    """Return the exact committed build source revision."""
    revision = _run(("git", "rev-parse", "HEAD"), cwd=root, runner=runner).stdout.strip()
    if _COMMIT_RE.fullmatch(revision) is None:
        raise HostVerificationImagePreparationError("source revision is invalid")
    return revision


def _committed_containerfile_sha256(
    root: Path,
    revision: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> str:
    """Return the digest of the committed Containerfile bytes."""
    result = _run(("git", "show", f"{revision}:{_CONTAINERFILE}"), cwd=root, runner=runner)
    return hashlib.sha256(result.stdout.encode()).hexdigest()


def _extract_committed_context(
    root: Path,
    revision: str,
    destination: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Extract an exact Git tree as the OCI build context."""
    archive = destination.parent / "context.tar"
    _run(
        ("git", "archive", "--format=tar", f"--output={archive}", revision),
        cwd=root,
        runner=runner,
    )
    try:
        with tarfile.open(archive, "r:") as stream:
            stream.extractall(destination, filter="data")
    except (OSError, tarfile.TarError) as exc:
        raise HostVerificationImagePreparationError("committed build context is invalid") from exc


def _authority_path(target: Path) -> Path:
    """Return the separate host authority path for one squashfs."""
    return target.with_suffix(".authority.json")


def _write_private_json(path: Path, value: dict[str, str]) -> Path:
    """Write one owner-read-only JSON file and return its temporary path."""
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o400)
        payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
        view = memoryview(payload)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    return temporary


def _authority(
    *,
    image_id: str,
    image_reference: str,
    containerfile_sha256: str,
    source_revision: str,
    squashfs_sha256: str,
) -> dict[str, str]:
    """Return the exact independently consumed image authority."""
    return {
        "schema": PYXIS_AUTHORITY_SCHEMA,
        "containerfile": str(_CONTAINERFILE),
        "containerfile_sha256": containerfile_sha256,
        "container_image_id": image_id,
        "container_image_reference": image_reference,
        "source_revision": source_revision,
        "squashfs_sha256": squashfs_sha256,
    }


def prepare_image(
    *,
    repo_root: Path,
    output: Path = DEFAULT_HOST_VERIFICATION_PYXIS_IMAGE,
    rebuild: bool = False,
    engine: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, object]:
    """Build, export, and authorize one content-addressed local CI image."""
    root = repo_root.expanduser().resolve()
    _regular_path(root, _CONTAINERFILE)
    if (root / "ci").is_symlink():
        raise HostVerificationImagePreparationError("CI build context cannot be a symlink")
    target = _safe_output_path(root, output)
    authority_path = _safe_output_path(root, _authority_path(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    selected_engine = engine or _select_engine(which)
    executable = _engine_command(selected_engine)
    revision = _source_revision(root, runner)
    committed_containerfile_sha256 = _committed_containerfile_sha256(root, revision, runner)

    if target.exists() and not rebuild:
        if authority_path.is_symlink() or not authority_path.is_file():
            raise HostVerificationImagePreparationError("existing image authority is unavailable")
        try:
            value = json.loads(authority_path.read_text(encoding="utf-8"))
            expected_sha256 = value["squashfs_sha256"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise HostVerificationImagePreparationError(
                "existing image authority is invalid"
            ) from exc
        try:
            metadata = validate_pyxis_image(
                target, expected_sha256=expected_sha256, provenance=authority_path
            )
        except (OSError, ValueError) as exc:
            raise HostVerificationImagePreparationError("existing image is not authorized") from exc
        current_id = _read_image_id(
            _run(
                (*executable, "image", "inspect", "--format={{.Id}}", metadata.container_image_id),
                cwd=root,
                runner=runner,
            )
        )
        if (
            current_id != metadata.container_image_id
            or metadata.source_revision != revision
            or metadata.containerfile_sha256 != committed_containerfile_sha256
        ):
            raise HostVerificationImagePreparationError("existing image provenance is stale")
        return {
            "image": str(metadata.path),
            "sha256": metadata.sha256,
            "image_id": metadata.container_image_id,
            "image_reference": metadata.container_image_reference,
            "authority": str(authority_path),
            "engine": selected_engine,
            "source_revision": revision,
            "reused": True,
        }

    with tempfile.TemporaryDirectory(prefix="hephaestus-pyxis-build-") as temp_dir:
        temporary_root = Path(temp_dir)
        context = temporary_root / "context"
        context.mkdir()
        _extract_committed_context(root, revision, context, runner)
        containerfile = _regular_path(context, _CONTAINERFILE)
        containerfile_sha256 = image_sha256(containerfile)
        if containerfile_sha256 != committed_containerfile_sha256:
            raise HostVerificationImagePreparationError(
                "committed Containerfile digest changed during preparation"
            )
        tag = f"{IMAGE_TAG_PREFIX}-{revision[:12]}-{containerfile_sha256[:12]}"
        _run(
            (*executable, "build", "-f", str(_CONTAINERFILE), "-t", tag, "."),
            cwd=context,
            runner=runner,
        )
        image_id = _read_image_id(
            _run(
                (*executable, "image", "inspect", "--format={{.Id}}", tag),
                cwd=context,
                runner=runner,
            )
        )
        scheme = "podman" if selected_engine == "podman" else "dockerd"
        source_uri = _validate_local_import_uri(f"{scheme}://{image_id}")
        temporary_image = temporary_root / "hephaestus-ci.sqsh"
        _run(
            ("enroot", "import", "--output", str(temporary_image), source_uri),
            cwd=context,
            runner=runner,
        )
        if temporary_image.is_symlink() or not temporary_image.is_file():
            raise HostVerificationImagePreparationError(
                "Enroot did not create a regular squashfs image"
            )
        with temporary_image.open("rb") as stream:
            if stream.read(4) != b"hsqs":
                raise HostVerificationImagePreparationError("Enroot image is not squashfs")
        digest = image_sha256(temporary_image)
        authority = _authority(
            image_id=image_id,
            image_reference=source_uri,
            containerfile_sha256=containerfile_sha256,
            source_revision=revision,
            squashfs_sha256=digest,
        )
        temporary_authority = _write_private_json(authority_path, authority)
        try:
            temporary_image.chmod(0o400)
            os.replace(temporary_image, target)
            os.replace(temporary_authority, authority_path)
        finally:
            temporary_authority.unlink(missing_ok=True)

    metadata = validate_pyxis_image(target, expected_sha256=digest, provenance=authority_path)
    return {
        "image": str(metadata.path),
        "sha256": metadata.sha256,
        "image_id": metadata.container_image_id,
        "image_reference": metadata.container_image_reference,
        "authority": str(authority_path),
        "engine": selected_engine,
        "source_revision": revision,
        "reused": False,
    }


def _build_parser() -> argparse.ArgumentParser:
    """Build the image-preparation command parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=DEFAULT_HOST_VERIFICATION_PYXIS_IMAGE)
    parser.add_argument("--rebuild", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the image preparation command."""
    args = _build_parser().parse_args(argv)
    try:
        result = prepare_image(
            repo_root=args.repo_root,
            output=args.output,
            rebuild=args.rebuild,
        )
    except HostVerificationImagePreparationError as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
